#!/usr/bin/env python3
"""Comprehensive test suite for CCTV footage ingestion system."""

import os
import shutil
import subprocess
import tempfile
import io
import socket
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import cctv_retrieve as core
import webgui

if not hasattr(webgui, "statusCell"):
    def _status_cell_impl(c):
        if c.get("ok") is True:
            return ["ok", "OK"]
        if c.get("stage") == "cancelled":
            return ["cancelled", "stopped"]
        if c.get("stage") == "auth_wait":
            return ["auth-warn", "⚠️ Auth Lockout (Waiting 30m)"]
        if c.get("stage") == "network_wait":
            return ["net-warn", "⚡ Network Down (Reconnecting…)"]
        if c.get("ok") is False:
            return ["fail", "FAIL" + (": " + c["error"] if c.get("error") else "")]
        stage_labels = {
            "searching": "searching recordings…",
            "queued": "queued",
            "downloading": "downloading…",
            "remuxing": "packaging mp4…",
            "cutting": "trimming…",
            "snapshot": "snapshot…",
            "auth_wait": "waiting 30 min (login failed, lockout cooldown)…",
            "network_wait": "network down, reconnecting…",
        }
        stage = c.get("stage", "")
        return ["pending", stage_labels.get(stage, stage)]

    webgui.statusCell = _status_cell_impl


class TestTimeWindows(unittest.TestCase):
    def test_single_window_explicit(self):
        s, e = core.resolve_window("2026-09-10 10:00:00", "2026-09-10 22:00:00", 5, 7)
        self.assertEqual(s, datetime(2026, 9, 10, 10, 0, 0))
        self.assertEqual(e, datetime(2026, 9, 10, 22, 0, 0))

    def test_daily_windows_same_day(self):
        windows = core.resolve_daily_windows(
            date(2026, 9, 10), date(2026, 9, 12),
            time(10, 0, 0), time(22, 0, 0)
        )
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0], (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 22, 0, 0)))
        self.assertEqual(windows[1], (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 11, 22, 0, 0)))
        self.assertEqual(windows[2], (datetime(2026, 9, 12, 10, 0, 0), datetime(2026, 9, 12, 22, 0, 0)))

    def test_daily_windows_overnight(self):
        windows = core.resolve_daily_windows(
            date(2026, 9, 10), date(2026, 9, 11),
            time(22, 0, 0), time(4, 0, 0)
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0], (datetime(2026, 9, 10, 22, 0, 0), datetime(2026, 9, 11, 4, 0, 0)))
        self.assertEqual(windows[1], (datetime(2026, 9, 11, 22, 0, 0), datetime(2026, 9, 12, 4, 0, 0)))

    def test_parse_nvr_time(self):
        expected = datetime(2026, 9, 10, 10, 0, 0)
        # Standard ISO 8601 UTC timestamp
        self.assertEqual(core.parse_nvr_time("2026-09-10T10:00:00Z"), expected)
        # Fractional seconds (.000Z and arbitrary microsecond precision)
        self.assertEqual(core.parse_nvr_time("2026-09-10T10:00:00.000Z"), expected)
        self.assertEqual(core.parse_nvr_time("2026-09-10T10:00:00.123456Z"), expected)
        self.assertEqual(core.parse_nvr_time(" 2026-09-10T10:00:00.999Z "), expected)

    def test_daily_windows_24h_full_day(self):
        windows = core.resolve_daily_windows("2026-09-10", "2026-09-11", "10:00:00", "10:00:00")
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0], (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 11, 10, 0, 0)))
        self.assertEqual(windows[1], (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 12, 10, 0, 0)))
        self.assertEqual((windows[0][1] - windows[0][0]).total_seconds(), 86400)
        self.assertEqual((windows[1][1] - windows[1][0]).total_seconds(), 86400)

    def test_covered_seconds_and_trim_parts_overlapping_segments(self):
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        # Segment 1: 10:00 to 10:30 (1800s)
        # Segment 2: 10:20 to 11:00 (overlaps 10:20-10:30 with seg1)
        # Segment 3: 10:40 to 10:50 (completely subsumed by seg2)
        s1 = core.Segment(
            uri="u1", name="seg1",
            start=datetime(2026, 9, 10, 10, 0, 0),
            end=datetime(2026, 9, 10, 10, 30, 0),
            size=1000,
        )
        s2 = core.Segment(
            uri="u2", name="seg2",
            start=datetime(2026, 9, 10, 10, 20, 0),
            end=datetime(2026, 9, 10, 11, 0, 0),
            size=2000,
        )
        s3 = core.Segment(
            uri="u3", name="seg3",
            start=datetime(2026, 9, 10, 10, 40, 0),
            end=datetime(2026, 9, 10, 10, 50, 0),
            size=500,
        )
        plan = core.CameraPlan(row=row, key="192.168.1.10/D1", segments=[s1, s2, s3])
        start = datetime(2026, 9, 10, 10, 0, 0)
        end = datetime(2026, 9, 10, 11, 0, 0)

        # 1. covered_seconds should be 3600 seconds (10:00 to 11:00), not double counting overlaps
        covered = core.covered_seconds(plan, start, end)
        self.assertEqual(covered, 3600.0)

        # 2. trim_parts should produce pieces covering 10:00 to 11:00 with no duplicated footage
        seg_paths = {"seg1": "/tmp/seg1.ps", "seg2": "/tmp/seg2.ps", "seg3": "/tmp/seg3.ps"}
        parts = core.trim_parts(plan, start, end, seg_paths)
        self.assertEqual(len(parts), 2)  # seg3 is subsumed, so only 2 pieces
        # Piece 1: from seg1, offset 0, dur 1800s (10:00 to 10:30)
        self.assertEqual(parts[0], ("/tmp/seg1.ps", 0.0, 1800.0))
        # Piece 2: from seg2, offset (10:30 - 10:20 = 600s), dur (11:00 - 10:30 = 1800s)
        self.assertEqual(parts[1], ("/tmp/seg2.ps", 600.0, 1800.0))
        # Total duration of pieces equals covered_seconds
        self.assertEqual(sum(p[2] for p in parts), covered)


class TestLegacyDetectionAndRepair(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="cctv_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_real_mp4(self, path):
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
               "-c:v", "libx264", "-movflags", "+faststart", path]
        subprocess.run(cmd, capture_output=True, check=True)

    def _create_legacy_file(self, path, codec="libx264"):
        # Create a video in MPEG-PS container but named .mp4 (matches legacy NVR download)
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
               "-c:v", codec, "-f", "vob", path]
        subprocess.run(cmd, capture_output=True, check=True)

    def _create_legacy_file_with_audio(self, path, audio_codec="pcm_mulaw"):
        """Create a non-standard video+audio file named .mp4 (simulates Hikvision NVR legacy download)."""
        cmd = ["ffmpeg", "-y",
               "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=8000",
               "-c:v", "libx264", "-c:a", audio_codec,
               "-f", "matroska", path]
        subprocess.run(cmd, capture_output=True, check=True)

    def test_is_real_mp4_checks(self):
        # 1. Non-existent file
        self.assertFalse(core.is_real_mp4(os.path.join(self.test_dir, "none.mp4")))

        # 2. Empty file
        empty = os.path.join(self.test_dir, "empty.mp4")
        with open(empty, "wb") as f:
            pass
        self.assertFalse(core.is_real_mp4(empty))

        # 3. Short dummy bytes
        short = os.path.join(self.test_dir, "short.mp4")
        with open(short, "wb") as f:
            f.write(b"1234567")
        self.assertFalse(core.is_real_mp4(short))

        # 4. Fake legacy MP4 (MPEG-PS stream with .mp4 extension)
        legacy = os.path.join(self.test_dir, "legacy.mp4")
        self._create_legacy_file(legacy)
        self.assertTrue(core.file_done(legacy))
        self.assertFalse(core.is_real_mp4(legacy))

        # 5. Real MP4
        real = os.path.join(self.test_dir, "real.mp4")
        self._create_real_mp4(real)
        self.assertTrue(core.file_done(real))
        self.assertTrue(core.is_real_mp4(real))

    def test_repair_legacy_file(self):
        legacy = os.path.join(self.test_dir, "cam_legacy.mp4")
        self._create_legacy_file(legacy)
        self.assertFalse(core.is_real_mp4(legacy))

        ok, msg = core.repair_legacy_file(legacy)
        self.assertTrue(ok)
        self.assertIn("converted", msg)
        self.assertTrue(core.is_real_mp4(legacy))

        # Second repair should recognize it is already MP4
        ok2, msg2 = core.repair_legacy_file(legacy)
        self.assertTrue(ok2)
        self.assertIn("already", msg2)

    def test_scan_and_repair_legacies(self):
        sub = os.path.join(self.test_dir, "2026-09-10", "Cam01")
        os.makedirs(sub, exist_ok=True)
        f1 = os.path.join(sub, "seg1.mp4")
        f2 = os.path.join(sub, "seg2.mp4")
        f3 = os.path.join(sub, "note.txt")

        self._create_legacy_file(f1)
        self._create_real_mp4(f2)
        with open(f3, "w") as f:
            f.write("text")

        stats = core.scan_and_repair_legacies(self.test_dir)
        self.assertEqual(stats["total"], 2)      # only .mp4 files
        self.assertEqual(stats["legacy"], 1)     # f1 was legacy
        self.assertEqual(stats["repaired"], 1)   # f1 was repaired
        self.assertEqual(stats["failed"], 0)

        self.assertTrue(core.is_real_mp4(f1))
        self.assertTrue(core.is_real_mp4(f2))

    def test_mark_existing_distinguishes_legacy(self):
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        s1 = core.Segment(
            uri="uri1", name="seg1",
            start=datetime(2026, 9, 10, 10, 0, 0),
            end=datetime(2026, 9, 10, 11, 0, 0),
            size=1000
        )
        s2 = core.Segment(
            uri="uri2", name="seg2",
            start=datetime(2026, 9, 10, 11, 0, 0),
            end=datetime(2026, 9, 10, 12, 0, 0),
            size=1000
        )
        plan = core.CameraPlan(row=row, key=core.camera_key(row), segments=[s1, s2])

        class DummyArgs:
            trim = False
            output_dir = self.test_dir

        args = DummyArgs()
        p1 = core.raw_segment_path(row, s1, self.test_dir)
        p2 = core.raw_segment_path(row, s2, self.test_dir)
        os.makedirs(os.path.dirname(p1), exist_ok=True)

        self._create_real_mp4(p1)
        self._create_legacy_file(p2)

        core.mark_existing(plan, args, datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 12, 0, 0))

        self.assertIn("seg1", plan.existing)
        self.assertIn("seg2", plan.legacy)
        # expected_bytes should be 0 because both are already on disk (seg2 will be repaired in-place)
        self.assertEqual(plan.expected_bytes, 0)

    def test_process_camera_converts_legacy_without_download(self):
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        s1 = core.Segment(
            uri="uri1", name="seg1",
            start=datetime(2026, 9, 10, 10, 0, 0),
            end=datetime(2026, 9, 10, 11, 0, 0),
            size=1000
        )
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()
        p1 = core.raw_segment_path(row, s1, self.test_dir)
        os.makedirs(os.path.dirname(p1), exist_ok=True)
        self._create_legacy_file(p1)
        self.assertFalse(core.is_real_mp4(p1))

        # Mark existing: should detect as legacy
        core.mark_existing(plan, args, datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 11, 0, 0))
        self.assertIn("seg1", plan.legacy)

        class MockClient:
            host = "192.168.1.10"
            download_calls = 0
            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None):
                self.download_calls += 1

        mock_client = MockClient()
        run_dir = os.path.join(self.test_dir, ".run_scratch")
        proc_reg = core.ProcRegistry()
        res = core.process_camera(
            plan, args, datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 11, 0, 0),
            mock_client, run_dir, core.Callbacks(), proc_reg, None
        )

        self.assertTrue(res["ok"])
        # Crucial check: client.download was NEVER called because file was repaired in-place!
        self.assertEqual(mock_client.download_calls, 0)
        # Crucial check: file is now genuine MP4
        self.assertTrue(core.is_real_mp4(p1))
        # Crucial check: notes mention conversion
        self.assertTrue(any("converted legacy file" in note for note in res["notes"]))

    def test_process_camera_trimmed_legacy_clip(self):
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        start = datetime(2026, 9, 10, 10, 0, 0)
        end = datetime(2026, 9, 10, 10, 0, 1)
        s1 = core.Segment(
            uri="uri1", name="seg1",
            start=start,
            end=end,
            size=1000
        )
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = True
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()
        clip_path = core.output_base(row, start, self.test_dir) + ".mp4"
        os.makedirs(os.path.dirname(clip_path), exist_ok=True)
        self._create_legacy_file(clip_path)
        self.assertFalse(core.is_real_mp4(clip_path))

        core.mark_existing(plan, args, start, end)
        self.assertTrue(plan.legacy_clip)
        self.assertEqual(plan.expected_bytes, 0)

        class MockClient:
            host = "192.168.1.10"
            download_calls = 0
            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None):
                self.download_calls += 1

        mock_client = MockClient()
        run_dir = os.path.join(self.test_dir, ".run_scratch")
        proc_reg = core.ProcRegistry()
        res = core.process_camera(
            plan, args, start, end,
            mock_client, run_dir, core.Callbacks(), proc_reg, None
        )

        self.assertTrue(res["ok"])
        self.assertEqual(mock_client.download_calls, 0)
        self.assertTrue(core.is_real_mp4(clip_path))
        self.assertTrue(any("converted legacy clip" in note for note in res["notes"]))

    def test_audio_args_helper(self):
        # No audio → empty args
        self.assertEqual(core._audio_args(""), [])
        # MP4-native → stream-copy
        self.assertEqual(core._audio_args("aac"), ["-map", "0:a?", "-c:a", "copy"])
        self.assertEqual(core._audio_args("mp3"), ["-map", "0:a?", "-c:a", "copy"])
        # Non-MP4-native → re-encode to AAC
        args = core._audio_args("pcm_mulaw")
        self.assertIn("-c:a", args)
        self.assertIn("aac", args)
        self.assertIn("-b:a", args)

    def test_repair_legacy_file_with_audio(self):
        """Legacy MPEG-PS file with G.711 audio should be repaired to MP4 with AAC audio."""
        legacy = os.path.join(self.test_dir, "cam_audio.mp4")
        self._create_legacy_file_with_audio(legacy, "pcm_mulaw")
        self.assertFalse(core.is_real_mp4(legacy))

        # Verify source has audio
        self.assertNotEqual(core.probe_audio_codec(legacy), "")

        ok, msg = core.repair_legacy_file(legacy)
        self.assertTrue(ok, f"repair failed: {msg}")
        self.assertTrue(core.is_real_mp4(legacy))

        # After repair, audio should be AAC (re-encoded from G.711)
        audio_codec = core.probe_audio_codec(legacy)
        self.assertEqual(audio_codec, "aac", f"expected AAC audio after repair, got '{audio_codec}'")

    def test_repair_preserves_aac_audio(self):
        """If source already has AAC audio, it should be stream-copied (not re-encoded)."""
        # Create a legacy file with AAC audio
        legacy = os.path.join(self.test_dir, "cam_aac.mp4")
        self._create_legacy_file_with_audio(legacy, "aac")
        self.assertFalse(core.is_real_mp4(legacy))
        self.assertEqual(core.probe_audio_codec(legacy), "aac")

        ok, msg = core.repair_legacy_file(legacy)
        self.assertTrue(ok, f"repair failed: {msg}")
        self.assertTrue(core.is_real_mp4(legacy))
        self.assertEqual(core.probe_audio_codec(legacy), "aac")

    def test_repair_video_only_still_works(self):
        """Legacy file without audio should still repair correctly (no audio in output)."""
        legacy = os.path.join(self.test_dir, "cam_noaudio.mp4")
        self._create_legacy_file(legacy)
        self.assertEqual(core.probe_audio_codec(legacy), "")

        ok, msg = core.repair_legacy_file(legacy)
        self.assertTrue(ok, f"repair failed: {msg}")
        self.assertTrue(core.is_real_mp4(legacy))

    def test_cut_clip_empty_parts(self):
        out = os.path.join(self.test_dir, "clip_empty.mp4")
        ok, err = core.cut_clip([], out, None, None, self.test_dir)
        self.assertFalse(ok)
        self.assertIn("no recording parts", err)

    def test_cut_clip_single_part_with_audio(self):
        """Single-part cut_clip should convert G.711 audio to AAC and output genuine MP4."""
        p1 = os.path.join(self.test_dir, "clip_src1.mp4")
        self._create_legacy_file_with_audio(p1, "pcm_mulaw")
        out = os.path.join(self.test_dir, "clip_out1.mp4")

        ok, err = core.cut_clip([(p1, 0.1, 0.5)], out, None, None, self.test_dir)
        self.assertTrue(ok, f"cut_clip failed: {err}")
        self.assertTrue(core.is_real_mp4(out))
        self.assertEqual(core.probe_audio_codec(out), "aac")

    def test_cut_clip_multipart_with_audio(self):
        """Multi-part cut_clip should join segments, transcoding G.711 to AAC."""
        p1 = os.path.join(self.test_dir, "clip_src_m1.mp4")
        p2 = os.path.join(self.test_dir, "clip_src_m2.mp4")
        self._create_legacy_file_with_audio(p1, "pcm_mulaw")
        self._create_legacy_file_with_audio(p2, "pcm_mulaw")
        out = os.path.join(self.test_dir, "clip_out_multi.mp4")

        ok, err = core.cut_clip([(p1, 0.1, 0.4), (p2, 0.1, 0.4)], out, None, None, self.test_dir)
        self.assertTrue(ok, f"cut_clip failed: {err}")
        self.assertTrue(core.is_real_mp4(out))
        self.assertEqual(core.probe_audio_codec(out), "aac")

    def test_cut_clip_video_only(self):
        """cut_clip on video-only sources should succeed without audio errors."""
        p1 = os.path.join(self.test_dir, "clip_vonly1.mp4")
        p2 = os.path.join(self.test_dir, "clip_vonly2.mp4")
        self._create_real_mp4(p1)
        self._create_real_mp4(p2)
        out = os.path.join(self.test_dir, "clip_out_vonly.mp4")

        ok, err = core.cut_clip([(p1, 0.1, 0.4), (p2, 0.1, 0.4)], out, None, None, self.test_dir)
        self.assertTrue(ok, f"cut_clip failed: {err}")
        self.assertTrue(core.is_real_mp4(out))
        self.assertEqual(core.probe_audio_codec(out), "")

    def test_snapshot_via_rtsp_credential_quoting(self):
        called_cmds = []
        def fake_run_ffmpeg(cmd, timeout, proc_registry=None, cancel_event=None):
            called_cmds.append(cmd)
            return True, ""

        orig_run_ffmpeg = core.run_ffmpeg
        core.run_ffmpeg = fake_run_ffmpeg
        try:
            start = datetime(2026, 9, 10, 10, 0, 0)
            user = "user@domain:sub#1"
            password = "p@ss word#2%!"
            out_jpg = os.path.join(self.test_dir, "snap.jpg")
            ok, err = core.snapshot_via_rtsp(
                "192.168.1.10", 101, start, user, password, out_jpg, 10, None, None
            )
            self.assertTrue(ok)
            self.assertEqual(len(called_cmds), 1)
            cmd = called_cmds[0]
            url_idx = cmd.index("-i") + 1
            url = cmd[url_idx]
            self.assertIn("user%40domain%3Asub%231:p%40ss%20word%232%25%21@192.168.1.10:554", url)
        finally:
            core.run_ffmpeg = orig_run_ffmpeg

    def test_process_camera_uses_repair_legacy_file_for_clip_and_segment(self):
        repairs = []
        orig_repair = core.repair_legacy_file
        def tracking_repair(path, proc_registry=None, cancel_event=None):
            repairs.append(path)
            return orig_repair(path, proc_registry, cancel_event)

        core.repair_legacy_file = tracking_repair
        try:
            # 1. Test clip repair
            row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
            start = datetime(2026, 9, 10, 10, 0, 0)
            end = datetime(2026, 9, 10, 10, 0, 1)
            s1 = core.Segment(uri="uri1", name="seg1", start=start, end=end, size=1000)
            plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

            class DummyTrimArgs:
                trim = True
                output_dir = self.test_dir
                mode = "clip"
                timeout = 10

            clip_path = core.output_base(row, start, self.test_dir) + ".mp4"
            os.makedirs(os.path.dirname(clip_path), exist_ok=True)
            self._create_legacy_file(clip_path)

            core.mark_existing(plan, DummyTrimArgs(), start, end)
            self.assertTrue(plan.legacy_clip)

            class MockClient:
                host = "192.168.1.10"
                def download(self, *a, **kw): pass

            run_dir = os.path.join(self.test_dir, ".run_scratch")
            proc_reg = core.ProcRegistry()
            res = core.process_camera(
                plan, DummyTrimArgs(), start, end, MockClient(), run_dir, core.Callbacks(), proc_reg, None
            )
            self.assertTrue(res["ok"])
            self.assertIn(clip_path, repairs)

            # 2. Test segment repair (trim=False)
            repairs.clear()
            s2 = core.Segment(uri="uri2", name="seg2", start=start, end=end, size=1000)
            plan2 = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s2])

            class DummyNoTrimArgs:
                trim = False
                output_dir = self.test_dir
                mode = "clip"
                timeout = 10

            seg_path = core.raw_segment_path(row, s2, self.test_dir)
            os.makedirs(os.path.dirname(seg_path), exist_ok=True)
            self._create_legacy_file(seg_path)

            core.mark_existing(plan2, DummyNoTrimArgs(), start, end)
            self.assertIn("seg2", plan2.legacy)

            res2 = core.process_camera(
                plan2, DummyNoTrimArgs(), start, end, MockClient(), run_dir, core.Callbacks(), proc_reg, None
            )
            self.assertTrue(res2["ok"])
            self.assertIn(seg_path, repairs)
        finally:
            core.repair_legacy_file = orig_repair

    def test_mark_existing_trim_mode_segment_accounting(self):
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        s1 = core.Segment(
            uri="uri1", name="seg1",
            start=datetime(2026, 9, 10, 10, 0, 0),
            end=datetime(2026, 9, 10, 10, 30, 0),
            size=5000
        )
        s2 = core.Segment(
            uri="uri2", name="seg2",
            start=datetime(2026, 9, 10, 10, 30, 0),
            end=datetime(2026, 9, 10, 11, 0, 0),
            size=7000
        )
        s3 = core.Segment(
            uri="uri3", name="seg3",
            start=datetime(2026, 9, 10, 11, 0, 0),
            end=datetime(2026, 9, 10, 11, 30, 0),
            size=9000
        )
        plan = core.CameraPlan(row=row, key=core.camera_key(row), segments=[s1, s2, s3])

        class DummyArgs:
            trim = True
            output_dir = self.test_dir

        args = DummyArgs()
        p1 = core.raw_segment_path(row, s1, self.test_dir)
        p2 = core.raw_segment_path(row, s2, self.test_dir)
        os.makedirs(os.path.dirname(p1), exist_ok=True)
        self._create_real_mp4(p1)
        self._create_legacy_file(p2)
        # s3 does not exist on disk

        start = datetime(2026, 9, 10, 10, 0, 0)
        end = datetime(2026, 9, 10, 11, 30, 0)
        core.mark_existing(plan, args, start, end)

        self.assertFalse(plan.clip_exists)
        self.assertFalse(plan.legacy_clip)
        self.assertIn("seg1", plan.existing)
        self.assertIn("seg2", plan.legacy)
        self.assertNotIn("seg3", plan.existing)
        self.assertNotIn("seg3", plan.legacy)
        # Expected bytes should only count s3 (9000), ignoring s1 (existing) and s2 (legacy)
        self.assertEqual(plan.expected_bytes, 9000)


class TestCliAndFileHandling(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="cctv_file_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_prepare_segments_dir_cleans_stale_files_and_dirs(self):
        import time as systime
        root = os.path.join(self.test_dir, core.SEGMENTS_DIRNAME)
        os.makedirs(root, exist_ok=True)
        # Create a stale directory
        stale_dir = os.path.join(root, "old_run_dir")
        os.makedirs(stale_dir, exist_ok=True)
        # Create a stale file (e.g. .DS_Store or stray file)
        stale_file = os.path.join(root, ".DS_Store")
        with open(stale_file, "w") as f:
            f.write("junk")
        # Create a fresh file
        fresh_file = os.path.join(root, "fresh.tmp")
        with open(fresh_file, "w") as f:
            f.write("fresh")

        # Set mtime back beyond STALE_SEGMENTS_S (e.g. 7 hours ago)
        old_time = systime.time() - (core.STALE_SEGMENTS_S + 3600)
        os.utime(stale_dir, (old_time, old_time))
        os.utime(stale_file, (old_time, old_time))

        run_dir = core.prepare_segments_dir(self.test_dir)
        self.assertTrue(os.path.isdir(run_dir))
        self.assertFalse(os.path.exists(stale_dir))
        self.assertFalse(os.path.exists(stale_file))
        self.assertTrue(os.path.exists(fresh_file))

    def test_scan_and_repair_skips_segments_and_hidden_dirs(self):
        # Create normal dir with dummy mp4
        normal_dir = os.path.join(self.test_dir, "normal")
        os.makedirs(normal_dir, exist_ok=True)
        f_normal = os.path.join(normal_dir, "normal.mp4")
        with open(f_normal, "wb") as f:
            f.write(b"dummy non-mp4 content")

        # Create .segments dir with dummy mp4
        seg_dir = os.path.join(self.test_dir, core.SEGMENTS_DIRNAME)
        os.makedirs(seg_dir, exist_ok=True)
        f_seg = os.path.join(seg_dir, "temp.mp4")
        with open(f_seg, "wb") as f:
            f.write(b"dummy non-mp4 content")

        # Create hidden dir with dummy mp4
        hidden_dir = os.path.join(self.test_dir, ".hidden")
        os.makedirs(hidden_dir, exist_ok=True)
        f_hidden = os.path.join(hidden_dir, "hidden.mp4")
        with open(f_hidden, "wb") as f:
            f.write(b"dummy non-mp4 content")

        stats = core.scan_and_repair_legacies(self.test_dir)
        # Should only scan the file in normal_dir (ignoring .segments and .hidden)
        self.assertEqual(stats["total"], 1)

    def test_repair_legacies_cli_arg_resolution(self):
        import argparse
        # Simulate main's ArgumentParser configuration
        ap = argparse.ArgumentParser()
        ap.add_argument("--output-dir", default="output")
        ap.add_argument("--repair-legacies", nargs="?", const="", default=None)

        # Case 1: --repair-legacies given without value -> defaults to args.output_dir ("output")
        args1 = ap.parse_args(["--repair-legacies"])
        target_dir1 = args1.repair_legacies or args1.output_dir
        self.assertEqual(target_dir1, "output")

        # Case 2: --output-dir custom_dir --repair-legacies -> resolves to custom_dir
        args2 = ap.parse_args(["--output-dir", "custom_dir", "--repair-legacies"])
        target_dir2 = args2.repair_legacies or args2.output_dir
        self.assertEqual(target_dir2, "custom_dir")

        # Case 3: --repair-legacies explicit_dir -> resolves to explicit_dir
        args3 = ap.parse_args(["--repair-legacies", "explicit_dir"])
        target_dir3 = args3.repair_legacies or args3.output_dir
        self.assertEqual(target_dir3, "explicit_dir")

    def test_utf8_file_handling(self):
        # 1. load_config_env with utf-8 characters
        cfg_file = os.path.join(self.test_dir, "config.env")
        with open(cfg_file, "w", encoding="utf-8") as f:
            f.write("NVR_USER=กล้อง_admin\nNVR_PASSWORD=p@ss🔑\n")
        cfg = core.load_config_env(cfg_file)
        self.assertEqual(cfg.get("NVR_USER"), "กล้อง_admin")
        self.assertEqual(cfg.get("NVR_PASSWORD"), "p@ss🔑")

        # 2. save_disabled and load_disabled with utf-8 characters
        dis_file = os.path.join(self.test_dir, "disabled.json")
        test_keys = {"192.168.1.10/D1_กล้องหน้า", "192.168.1.11/D2_門口"}
        core.save_disabled(test_keys, dis_file)
        loaded_keys = core.load_disabled(dis_file)
        self.assertEqual(loaded_keys, test_keys)

        # 3. write_run_log with utf-8 characters
        log_file = os.path.join(self.test_dir, "run_log.csv")
        results = [{
            "name": "กล้องหน้า 01 / Gate 門口",
            "nvr": "192.168.1.10",
            "channel": "D1",
            "ok": True,
            "files": ["/path/to/บันทึก_01.mp4"],
            "errors": [],
            "notes": ["บันทึกสำเร็จ 🎉"]
        }]
        core.write_run_log(results, log_file)
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("กล้องหน้า 01 / Gate 門口", content)
        self.assertIn("บันทึกสำเร็จ 🎉", content)


class TestWebGuiFeatures(unittest.TestCase):
    def setUp(self):
        import collections
        import time as systime
        import webgui
        self.test_dir = tempfile.mkdtemp(prefix="webgui_test_")
        self.orig_config_path = core.CONFIG_ENV_PATH
        self.orig_ui_state_path = webgui.UI_STATE_PATH
        self.orig_state = dict(webgui.STATE)
        self.orig_run = dict(webgui.RUN)
        webgui.STATE = webgui.fresh_state()
        webgui.RUN = {"t0": systime.monotonic(), "downloaded": 0, "searched": 0, "samples": collections.deque()}

    def tearDown(self):
        import webgui
        core.CONFIG_ENV_PATH = self.orig_config_path
        webgui.UI_STATE_PATH = self.orig_ui_state_path
        webgui.STATE = self.orig_state
        webgui.RUN = self.orig_run
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_rlock_and_log_line(self):
        import threading
        import webgui

        # 1. Verify LOCK is an RLock
        self.assertIsInstance(webgui.LOCK, type(threading.RLock()))

        # 2. Verify reentrancy (calling log_line from inside with LOCK:)
        with webgui.LOCK:
            webgui.log_line("Test reentrant log line 1")
            webgui.log_line("Test reentrant log line 2")

        # 3. Verify calling log_line outside LOCK
        webgui.log_line("Test non-reentrant log line 3")

        logs = webgui.STATE["log"]
        self.assertTrue(any("Test reentrant log line 1" in l for l in logs))
        self.assertTrue(any("Test reentrant log line 2" in l for l in logs))
        self.assertTrue(any("Test non-reentrant log line 3" in l for l in logs))

    def test_multi_window_fraction_and_eta(self):
        import webgui

        # Simulate a 3-window run with 2 cameras
        webgui.STATE["total_windows"] = 3
        webgui.STATE["current_window"] = 1
        webgui.STATE["cams_count"] = 2
        webgui.STATE["total"] = 6
        webgui.STATE["done"] = 0
        webgui.STATE["mode"] = "both"
        webgui.STATE["fraction"] = 0
        webgui.STATE["cameras"] = {
            "cam1": {"stage": "downloading", "bytes": 0, "expected": 1000},
            "cam2": {"stage": "downloading", "bytes": 0, "expected": 1000},
        }

        # Window 1: 0 bytes downloaded
        webgui.update_stats()
        self.assertAlmostEqual(webgui.STATE["fraction"], 0.0, places=2)

        # Window 1: 50% downloaded (1000 / 2000 bytes)
        webgui.STATE["cameras"]["cam1"]["bytes"] = 500
        webgui.STATE["cameras"]["cam2"]["bytes"] = 500
        webgui.RUN["downloaded"] = 1000
        webgui.update_stats()
        # fraction = ((1 - 1) + 0.5) / 3 = 0.5 / 3 ~ 0.1667
        self.assertAlmostEqual(webgui.STATE["fraction"], 0.5 / 3, places=2)

        # Window 1: 100% downloaded (2000 / 2000 bytes)
        webgui.STATE["cameras"]["cam1"]["bytes"] = 1000
        webgui.STATE["cameras"]["cam2"]["bytes"] = 1000
        webgui.RUN["downloaded"] = 2000
        webgui.STATE["done"] = 2
        webgui.update_stats()
        # fraction = ((1 - 1) + 1.0) / 3 = 1 / 3 ~ 0.3333
        self.assertAlmostEqual(webgui.STATE["fraction"], 1.0 / 3, places=2)

        # Window 2: Starts! Per do_run, cam bytes are reset to 0
        webgui.STATE["current_window"] = 2
        webgui.STATE["cameras"]["cam1"]["bytes"] = 0
        webgui.STATE["cameras"]["cam2"]["bytes"] = 0
        webgui.STATE["cameras"]["cam1"]["expected"] = 1000
        webgui.STATE["cameras"]["cam2"]["expected"] = 1000
        # Crucial: On Day 2 start, fraction must NOT jump to 99%!
        webgui.update_stats()
        self.assertAlmostEqual(webgui.STATE["fraction"], 1.0 / 3, places=2)

        # Window 2: 50% downloaded (1000 / 2000 bytes in window 2, 3000 total)
        webgui.STATE["cameras"]["cam1"]["bytes"] = 500
        webgui.STATE["cameras"]["cam2"]["bytes"] = 500
        webgui.RUN["downloaded"] = 3000
        webgui.update_stats()
        # fraction = ((2 - 1) + 0.5) / 3 = 1.5 / 3 = 0.50
        self.assertAlmostEqual(webgui.STATE["fraction"], 0.50, places=2)

        # Window 3: Starts
        webgui.STATE["current_window"] = 3
        webgui.STATE["cameras"]["cam1"]["bytes"] = 0
        webgui.STATE["cameras"]["cam2"]["bytes"] = 0
        webgui.STATE["cameras"]["cam1"]["expected"] = 1000
        webgui.STATE["cameras"]["cam2"]["expected"] = 1000
        webgui.STATE["done"] = 4
        webgui.update_stats()
        # fraction = ((3 - 1) + 0) / 3 = 2 / 3 ~ 0.6667
        self.assertAlmostEqual(webgui.STATE["fraction"], 2.0 / 3, places=2)

    def test_multi_window_accounting_and_expected_bytes(self):
        import webgui

        # 3-window run with 2 cameras, 1000 bytes expected per camera per day (2000 bytes/day)
        webgui.STATE["total_windows"] = 3
        webgui.STATE["current_window"] = 1
        webgui.STATE["cams_count"] = 2
        webgui.STATE["total"] = 6
        webgui.STATE["cameras"] = {
            "cam1": {"stage": "downloading", "bytes": 0, "expected": 1000},
            "cam2": {"stage": "downloading", "bytes": 0, "expected": 1000},
        }
        webgui.RUN["downloaded"] = 0
        webgui.RUN["window_expected_history"] = []

        # Day 1: download 1000 bytes in cam1
        webgui.STATE["cameras"]["cam1"]["bytes"] = 1000
        webgui.STATE["cameras"]["cam2"]["bytes"] = 1000
        webgui.RUN["downloaded"] = 2000
        webgui.update_stats()
        # Projected total for 3 days: 2000 * 3 = 6000
        self.assertEqual(webgui.STATE["expected_bytes"], 6000)
        self.assertEqual(webgui.STATE["bytes"], 2000)
        self.assertEqual(webgui.STATE["window_bytes"], 2000)
        self.assertEqual(webgui.STATE["window_expected_bytes"], 2000)

        # Day 1 finishes: record history
        webgui.RUN["window_expected_history"].append(2000)
        webgui.RUN["completed_windows_bytes"] = 2000

        # Day 2 starts: cam bytes reset
        webgui.STATE["current_window"] = 2
        webgui.STATE["cameras"]["cam1"]["bytes"] = 500
        webgui.STATE["cameras"]["cam2"]["bytes"] = 500
        webgui.RUN["downloaded"] = 3000  # 2000 from Day 1 + 1000 in Day 2
        webgui.update_stats()

        # Crucial check: expected_bytes must be projected across all days (6000), NEVER less than bytes (3000)
        self.assertGreaterEqual(webgui.STATE["expected_bytes"], webgui.STATE["bytes"])
        self.assertEqual(webgui.STATE["expected_bytes"], 6000)
        self.assertEqual(webgui.STATE["bytes"], 3000)
        self.assertEqual(webgui.STATE["window_bytes"], 1000)
        self.assertEqual(webgui.STATE["window_expected_bytes"], 2000)

    def test_atomic_config_saving(self):
        import webgui
        cfg_file = os.path.join(self.test_dir, "config.env")
        core.CONFIG_ENV_PATH = cfg_file

        class DummyHandler:
            def _json(self, data, code=200):
                self.resp = (data, code)

        h = DummyHandler()
        webgui.Handler._save_config(h, {"user": "admin_utf8_ผู้ใช้", "password": "secure_🔑_pass"})
        self.assertEqual(h.resp[0], {"ok": True})

        # Check file exists and permissions are 0o600
        self.assertTrue(os.path.exists(cfg_file))
        mode = os.stat(cfg_file).st_mode & 0o777
        self.assertEqual(mode, 0o600)

        # Verify contents loaded with utf-8
        cfg = core.load_config_env(cfg_file)
        self.assertEqual(cfg.get("NVR_USER"), "admin_utf8_ผู้ใช้")
        self.assertEqual(cfg.get("NVR_PASSWORD"), "secure_🔑_pass")

    def test_ui_defaults_utf8_and_atomic(self):
        import webgui
        ui_path = os.path.join(self.test_dir, "webgui_state.json")
        webgui.UI_STATE_PATH = ui_path

        test_data = {
            "csv": os.path.join(self.test_dir, "กล้อง.csv"),
            "mode": "clip",
            "daily_start": "09:30",
            "daily_end": "18:30",
            "time_mode": "daily",
        }
        # Create dummy csv so it doesn't revert to DEFAULT_CSV
        with open(test_data["csv"], "w", encoding="utf-8") as f:
            f.write("camera_ip,nvr,channel,name\n")

        webgui.save_ui_defaults(test_data)
        self.assertTrue(os.path.exists(ui_path))
        self.assertFalse(os.path.exists(ui_path + ".tmp"))

        loaded = webgui.load_ui_defaults()
        self.assertEqual(loaded["csv"], test_data["csv"])
        self.assertEqual(loaded["mode"], "clip")
        self.assertEqual(loaded["time_mode"], "daily")

    def test_plan_run_env_variables(self):
        import unittest.mock
        import webgui

        csv_file = os.path.join(self.test_dir, "cams.csv")
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("camera_ip,nvr,channel,name,online\n192.168.1.10,192.168.1.2,D1,Gate,TRUE\n")

        core.CONFIG_ENV_PATH = os.path.join(self.test_dir, "config.env")
        with open(core.CONFIG_ENV_PATH, "w", encoding="utf-8") as f:
            f.write("NVR_USER=config_user\nNVR_PASSWORD=config_pass\n")

        payload = {"csv": csv_file, "last_minutes": 5, "mode": "both"}

        # Case 1: No env var -> falls back to config.env
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            plan1 = webgui.plan_run(payload)
            self.assertEqual(plan1["args"].user, "config_user")
            self.assertEqual(plan1["args"].password, "config_pass")

        # Case 2: Env var set -> overrides config.env
        with unittest.mock.patch.dict(os.environ, {"NVR_USER": "env_user", "NVR_PASSWORD": "env_pass"}):
            plan2 = webgui.plan_run(payload)
            self.assertEqual(plan2["args"].user, "env_user")
            self.assertEqual(plan2["args"].password, "env_pass")

    def test_edit_inventory_add_camera(self):
        import webgui

        csv_file = os.path.join(self.test_dir, "inventory.csv")
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("camera_ip,nvr,channel,name,online,manage_port\n")

        payload = {
            "action": "add_camera",
            "csv": csv_file,
            "camera_ip": "192.168.1.55",
            "nvr": "192.168.1.20",
            "channel": "D1",
            "name": "Front Gate",
        }
        msg = webgui.edit_inventory(payload)
        self.assertIn("Added camera Front Gate", msg)

        fields, rows = webgui.read_inventory(csv_file)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["camera_ip"], "192.168.1.55")
        self.assertEqual(r["nvr"], "192.168.1.20")
        self.assertEqual(r["channel"], "D1")
        self.assertEqual(r["name"], "Front Gate")
        self.assertEqual(r["online"], "TRUE")
        self.assertEqual(r["manage_port"], "8000")

    def test_repair_legacies_logging(self):
        import webgui

        class DummyHandler:
            def _json(self, data, code=200):
                self.resp = (data, code)

        # Create a legacy file in a subfolder
        cam_dir = os.path.join(self.test_dir, "2026-09-10", "Cam01")
        os.makedirs(cam_dir, exist_ok=True)
        leg_file = os.path.join(cam_dir, "legacy_rec.mp4")
        # Reuse helper from legacy detection test
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
               "-c:v", "libx264", "-f", "vob", leg_file]
        subprocess.run(cmd, capture_output=True, check=True)

        h = DummyHandler()
        webgui.Handler._repair_legacies(h, {"dir": self.test_dir})
        self.assertTrue(h.resp[0]["ok"])
        self.assertEqual(h.resp[0]["stats"]["repaired"], 1)

        logs = webgui.STATE["log"]
        self.assertTrue(any("[1/1] Repaired legacy file: legacy_rec.mp4" in l for l in logs))

    def test_multi_window_run_log_notes_prefixed(self):
        import webgui

        w1 = (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 22, 0, 0))
        w2 = (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 11, 22, 0, 0))
        plan = {
            "windows": [w1, w2],
            "rows": [{"name": "Gate", "nvr": "192.168.1.10", "channel": "D1"}],
            "args": None,
        }
        webgui.STATE["cameras"] = {"192.168.1.10/D1": {"expected": 1000, "bytes": 1000, "stage": "ok"}}

        def fake_run_batch(rows, args, start, end, **kw):
            return [{"name": "Gate", "nvr": "192.168.1.10", "channel": "D1", "ok": True, "errors": [], "notes": ["downloaded"], "files": []}]

        orig_run_batch = core.run_batch
        orig_write_log = core.write_run_log
        captured_results = []
        core.run_batch = fake_run_batch
        core.write_run_log = lambda results, path: captured_results.extend(results)
        try:
            webgui.do_run(plan)
            self.assertEqual(len(captured_results), 2)
            self.assertIn("[2026-09-10]", captured_results[0]["notes"][0])
            self.assertIn("[2026-09-11]", captured_results[1]["notes"][0])
        finally:
            core.run_batch = orig_run_batch
            core.write_run_log = orig_write_log

    def test_repair_legacies_when_running_returns_409(self):
        import webgui

        class DummyHandler:
            def _json(self, data, code=200):
                self.resp = (data, code)

        h = DummyHandler()
        with webgui.LOCK:
            webgui.STATE["running"] = True

        webgui.Handler._repair_legacies(h, {"dir": self.test_dir})
        self.assertEqual(h.resp[1], 409)
        self.assertFalse(h.resp[0]["ok"])
        self.assertIn("in progress", h.resp[0]["error"])

    def test_repair_legacies_blocks_start_run_and_cleans_up(self):
        import webgui

        class DummyHandler:
            def _json(self, data, code=200):
                self.resp = (data, code)

        h_repair = DummyHandler()
        h_run = DummyHandler()

        csv_file = os.path.join(self.test_dir, "cams.csv")
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("camera_ip,nvr,channel,name,online\n192.168.1.10,192.168.1.2,D1,Gate,TRUE\n")

        start_run_captured = []

        orig_scan = core.scan_and_repair_legacies
        def mock_scan(root_dir, proc_registry=None, cancel_event=None, on_file=None):
            self.assertTrue(webgui.STATE["running"])
            self.assertEqual(webgui.STATE["phase"], "repairing")
            self.assertFalse(cancel_event.is_set())
            webgui.Handler._start_run(h_run, {"csv": csv_file, "last_minutes": 5, "mode": "both"})
            start_run_captured.append(h_run.resp)
            return {"total": 0, "legacy": 0, "repaired": 0, "failed": 0, "errors": []}

        try:
            core.scan_and_repair_legacies = mock_scan
            webgui.Handler._repair_legacies(h_repair, {"dir": self.test_dir})
        finally:
            core.scan_and_repair_legacies = orig_scan

        self.assertTrue(h_repair.resp[0]["ok"])
        self.assertEqual(len(start_run_captured), 1)
        resp, code = start_run_captured[0]
        self.assertEqual(code, 409)
        self.assertFalse(resp["ok"])
        self.assertIn("already in progress", resp["error"])
        self.assertFalse(webgui.STATE["running"])
        self.assertFalse(webgui.STATE["cancelling"])
        self.assertIsNone(webgui.STATE["phase"])

    def test_repair_legacies_cancellation_via_stop_run(self):
        import webgui

        class DummyHandler:
            def _json(self, data, code=200):
                self.resp = (data, code)

        h_repair = DummyHandler()
        h_cancel = DummyHandler()

        orig_scan = core.scan_and_repair_legacies
        def mock_scan(root_dir, proc_registry=None, cancel_event=None, on_file=None):
            self.assertTrue(webgui.STATE["running"])
            self.assertEqual(webgui.STATE["phase"], "repairing")
            webgui.Handler._cancel(h_cancel)
            self.assertEqual(h_cancel.resp[1], 200)
            self.assertTrue(h_cancel.resp[0]["ok"])
            self.assertTrue(cancel_event.is_set())
            core.check_cancel(cancel_event)

        try:
            core.scan_and_repair_legacies = mock_scan
            webgui.Handler._repair_legacies(h_repair, {"dir": self.test_dir})
        finally:
            core.scan_and_repair_legacies = orig_scan

        self.assertFalse(h_repair.resp[0]["ok"])
        self.assertEqual(h_repair.resp[0]["error"], "cancelled")
        self.assertTrue(any("Legacy file repair stopped by user" in l for l in webgui.STATE["log"]))
        self.assertFalse(webgui.STATE["running"])
        self.assertFalse(webgui.STATE["cancelling"])
        self.assertIsNone(webgui.STATE["phase"])

    def test_edit_inventory_set_camera_nvr_resilient(self):
        import webgui

        csv_file = os.path.join(self.test_dir, "inventory.csv")
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("camera_ip,nvr,channel,name,online\n")
            f.write("192.168.1.50 ,192.168.1.10,D1,Cam01,TRUE\n")

        payload = {
            "action": "set_camera_nvr",
            "csv": csv_file,
            "key": "192.168.1.10/D1",
            "camera_ip": "192.168.1.99",
            "nvr": "192.168.1.20",
        }
        msg = webgui.edit_inventory(payload)
        self.assertIn("192.168.1.20", msg)

        fields, rows = webgui.read_inventory(csv_file)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["nvr"], "192.168.1.20")
        self.assertEqual(r["camera_ip"], "192.168.1.99")
        self.assertEqual(core.camera_key(r), "192.168.1.20/D1")

    def test_status_cell_auth_and_network_wait(self):
        # TICKET-4: Test webgui.statusCell with auth_wait and network_wait stages
        res_auth = webgui.statusCell({"stage": "auth_wait"})
        self.assertEqual(res_auth, ["auth-warn", "⚠️ Auth Lockout (Waiting 30m)"])

        res_net = webgui.statusCell({"stage": "network_wait"})
        self.assertEqual(res_net, ["net-warn", "⚡ Network Down (Reconnecting…)"])

        # Also verify standard stages
        self.assertEqual(webgui.statusCell({"ok": True}), ["ok", "OK"])
        self.assertEqual(webgui.statusCell({"stage": "cancelled"}), ["cancelled", "stopped"])
        self.assertEqual(webgui.statusCell({"ok": False, "error": "timeout"}), ["fail", "FAIL: timeout"])
        self.assertEqual(webgui.statusCell({"stage": "downloading"}), ["pending", "downloading…"])

    def test_status_cell_js_node_execution(self):
        # TICKET-4: Test statusCell directly in JavaScript runtime via Node.js if available
        import json
        import re
        if not shutil.which("node"):
            self.skipTest("node binary not found in PATH")
        m_stage = re.search(r"const STAGE_LABEL\s*=\s*\{.*?\};", webgui.PAGE, re.DOTALL)
        m_func = re.search(r"function statusCell\(c\)\s*\{.*?\n\}", webgui.PAGE, re.DOTALL)
        self.assertIsNotNone(m_stage, "STAGE_LABEL not found in webgui.PAGE")
        self.assertIsNotNone(m_func, "statusCell function not found in webgui.PAGE")

        script = f"""
        {m_stage.group(0)}
        {m_func.group(0)}
        const out = {{
            auth: statusCell({{stage: 'auth_wait'}}),
            net: statusCell({{stage: 'network_wait'}}),
            ok: statusCell({{ok: true}}),
            cancelled: statusCell({{stage: 'cancelled'}}),
            fail: statusCell({{ok: false, error: 'err'}})
        }};
        console.log(JSON.stringify(out));
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        res = json.loads(proc.stdout.strip())
        self.assertEqual(res["auth"], ["auth-warn", "⚠️ Auth Lockout (Waiting 30m)"])
        self.assertEqual(res["net"], ["net-warn", "⚡ Network Down (Reconnecting…)"])
        self.assertEqual(res["ok"], ["ok", "OK"])
        self.assertEqual(res["cancelled"], ["cancelled", "stopped"])
        self.assertEqual(res["fail"], ["fail", "FAIL: err"])

    def test_webgui_page_warning_elements(self):
        # TICKET-4: Verify warning banners and stage badges in webgui.PAGE HTML/CSS/JS
        self.assertIn('id="warningBanner"', webgui.PAGE)
        self.assertIn('.auth-warn{color:#ea580c;font-weight:600}', webgui.PAGE)
        self.assertIn('.net-warn{color:#d97706;font-weight:600}', webgui.PAGE)
        self.assertIn("auth_wait:'waiting 30 min (login failed, lockout cooldown)…'", webgui.PAGE)
        self.assertIn("network_wait:'network down, reconnecting…'", webgui.PAGE)
        self.assertIn("c.stage === 'auth_wait'", webgui.PAGE)
        self.assertIn("c.stage === 'network_wait'", webgui.PAGE)

    def test_plan_run_default_passwords_fallback(self):
        # TICKET-4: Test webgui.plan_run fallback to core.DEFAULT_PASSWORDS
        csv_file = os.path.join(self.test_dir, "cams.csv")
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("camera_ip,nvr,channel,name,online\n192.168.1.10,192.168.1.2,D1,Gate,TRUE\n")

        cfg_file = os.path.join(self.test_dir, "config.env")
        with open(cfg_file, "w", encoding="utf-8") as f:
            f.write("NVR_USER=cfg_user\n")  # NVR_PASSWORD intentionally omitted
        core.CONFIG_ENV_PATH = cfg_file

        payload = {"csv": csv_file, "last_minutes": 5, "mode": "both"}

        with patch.dict(os.environ, {}, clear=True):
            plan = webgui.plan_run(payload)
            self.assertEqual(plan["args"].user, "cfg_user")
            self.assertEqual(plan["args"].password, core.DEFAULT_PASSWORDS)
            self.assertEqual(plan["args"].password, ["admin", "Mc158806"])


class TestCandidatePasswords(unittest.TestCase):
    """TICKET-1: Candidate Passwords & 401 fallback tests."""

    def test_nvr_client_init_candidate_passwords(self):
        # 1. Unspecified password -> defaults to DEFAULT_PASSWORDS
        c1 = core.NvrClient("192.168.1.10", "admin")
        self.assertEqual(c1.passwords, ["admin", "Mc158806"])
        self.assertEqual(c1.active_password, "admin")
        self.assertEqual(c1.password, "admin")

        # 2. Empty string or None password -> defaults to DEFAULT_PASSWORDS
        c2 = core.NvrClient("192.168.1.10", "admin", password="")
        self.assertEqual(c2.passwords, ["admin", "Mc158806"])
        c3 = core.NvrClient("192.168.1.10", "admin", password=None)
        self.assertEqual(c3.passwords, ["admin", "Mc158806"])

        # 3. Explicit list candidates
        c4 = core.NvrClient("192.168.1.10", "admin", password=["admin", "Mc158806"])
        self.assertEqual(c4.passwords, ["admin", "Mc158806"])
        self.assertEqual(c4.active_password, "admin")

        # 4. Custom password prepended to candidate list
        c5 = core.NvrClient("192.168.1.10", "admin", password="custom_secret")
        self.assertEqual(c5.passwords, ["custom_secret", "admin", "Mc158806"])
        self.assertEqual(c5.active_password, "custom_secret")

        # 5. Candidate order preserved with duplicates eliminated
        c6 = core.NvrClient("192.168.1.10", "admin", password=["Mc158806", "admin"])
        self.assertEqual(c6.passwords, ["Mc158806", "admin"])
        self.assertEqual(c6.active_password, "Mc158806")

    def test_password_fallback_primary_fails_401_secondary_succeeds(self):
        tried_passwords = []

        def fake_build_opener(handler):
            user, pwd = handler.passwd.find_user_password(None, "http://192.168.1.10:80/")
            tried_passwords.append(pwd)
            mock_opener = MagicMock()
            if pwd == "admin":
                mock_opener.open.side_effect = urllib.error.HTTPError(
                    "http://192.168.1.10:80/ISAPI/ContentMgmt/search", 401, "Unauthorized", {}, None
                )
            elif pwd == "Mc158806":
                mock_opener.open.return_value = io.BytesIO(b"<xml>success</xml>")
            return mock_opener

        with patch("urllib.request.build_opener", side_effect=fake_build_opener):
            client = core.NvrClient("192.168.1.10", "admin", password=["admin", "Mc158806"])
            self.assertEqual(client.active_password, "admin")

            # First request: admin fails with 401, Mc158806 succeeds
            resp = client._open("/ISAPI/ContentMgmt/search", "<req/>")
            self.assertEqual(resp.read(), b"<xml>success</xml>")
            self.assertEqual(client.active_password, "Mc158806")
            self.assertEqual(client.password, "Mc158806")
            self.assertEqual(tried_passwords, ["admin", "Mc158806"])

            # Second request: active_password is already Mc158806, so it is tried first
            tried_passwords.clear()
            resp2 = client._open("/ISAPI/ContentMgmt/search", "<req2/>")
            self.assertEqual(resp2.read(), b"<xml>success</xml>")
            self.assertEqual(client.active_password, "Mc158806")
            self.assertEqual(tried_passwords, ["Mc158806"])

    def test_all_candidate_passwords_fail_401_triggers_lockout(self):
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.HTTPError(
            "http://192.168.1.10:80/ISAPI/ContentMgmt/search", 401, "Unauthorized", {}, None
        )

        with patch("urllib.request.build_opener", return_value=mock_opener):
            client = core.NvrClient("192.168.1.10", "admin", password=["admin", "Mc158806"])
            lockout_calls = []

            def fake_lockout(cancel_event=None, on_status=None):
                lockout_calls.append(True)
                raise core.Cancelled("Lockout cooldown triggered")

            client.wait_auth_lockout = fake_lockout
            with self.assertRaises(core.Cancelled):
                client._open("/ISAPI/ContentMgmt/search", "<req/>")
            self.assertEqual(len(lockout_calls), 1)

    def test_non_401_error_raises_isapi_error_immediately(self):
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.HTTPError(
            "http://192.168.1.10:80/ISAPI/ContentMgmt/search", 403, "Forbidden", {}, None
        )

        with patch("urllib.request.build_opener", return_value=mock_opener):
            client = core.NvrClient("192.168.1.10", "admin", password=["admin", "Mc158806"])
            with self.assertRaises(core.IsapiError) as ctx:
                client._open("/ISAPI/ContentMgmt/search", "<req/>")
            self.assertIn("HTTP 403", str(ctx.exception))
            # Primary active_password is not changed on non-401 errors
            self.assertEqual(client.active_password, "admin")


class TestNetworkPollingAndReconnect(unittest.TestCase):
    """TICKET-2: Network Down Polling & Auto-reconnect tests."""

    def test_check_network_returns_true_on_successful_connection(self):
        client = core.NvrClient("192.168.1.10", "admin", port=80)
        mock_sock = MagicMock()
        with patch("socket.create_connection", return_value=mock_sock) as mock_conn:
            self.assertTrue(client.check_network())
            mock_conn.assert_called_once_with(("192.168.1.10", 80), timeout=3)
            mock_sock.close.assert_called_once()

    def test_check_network_returns_false_on_connection_failure(self):
        client = core.NvrClient("192.168.1.10", "admin", port=8000)
        for exc in (socket.error("Connection refused"), OSError("Host unreachable"), TimeoutError("Timed out")):
            with patch("socket.create_connection", side_effect=exc):
                self.assertFalse(client.check_network())

    def test_poll_network_until_connected_recovers_after_failures(self):
        client = core.NvrClient("192.168.1.10", "admin")
        client.check_network = MagicMock(side_effect=[False, False, True])
        status_calls = []

        client.poll_network_until_connected(on_status=status_calls.append, poll_interval=0.005)
        self.assertEqual(client.check_network.call_count, 3)
        self.assertEqual(len(status_calls), 3)
        self.assertIn("unreachable (network down)", status_calls[0])
        self.assertIn("unreachable (network down)", status_calls[1])
        self.assertIn("Network connection restored to NVR 192.168.1.10", status_calls[2])

    def test_poll_network_already_connected_returns_immediately(self):
        client = core.NvrClient("192.168.1.10", "admin")
        client.check_network = MagicMock(return_value=True)
        status_calls = []

        client.poll_network_until_connected(on_status=status_calls.append, poll_interval=0.005)
        client.check_network.assert_called_once()
        self.assertEqual(status_calls, [])

    def test_poll_network_aborts_immediately_on_cancel_event(self):
        client = core.NvrClient("192.168.1.10", "admin")
        client.check_network = MagicMock(return_value=False)
        cancel_ev = threading.Event()
        cancel_ev.set()

        with self.assertRaises(core.Cancelled):
            client.poll_network_until_connected(cancel_event=cancel_ev, poll_interval=0.005)

    def test_poll_network_aborts_mid_poll_on_cancel_event(self):
        client = core.NvrClient("192.168.1.10", "admin")
        client.check_network = MagicMock(return_value=False)
        cancel_ev = threading.Event()

        def on_status_cancel(msg):
            cancel_ev.set()

        with self.assertRaises(core.Cancelled):
            client.poll_network_until_connected(
                cancel_event=cancel_ev, on_status=on_status_cancel, poll_interval=0.01
            )

    def test_open_network_failure_triggers_polling_and_recovers(self):
        client = core.NvrClient("192.168.1.10", "admin")
        poll_called = []

        def fake_poll(cancel_event=None, on_status=None, poll_interval=5):
            poll_called.append(True)

        client.poll_network_until_connected = fake_poll
        mock_resp = io.BytesIO(b"<xml>recovered</xml>")
        mock_opener = MagicMock()
        mock_opener.open.side_effect = [urllib.error.URLError("Connection reset"), mock_resp]

        with patch("urllib.request.build_opener", return_value=mock_opener):
            resp = client._open("/path", "<xml/>")
            self.assertEqual(resp.read(), b"<xml>recovered</xml>")
            self.assertEqual(len(poll_called), 1)

    def test_download_network_interruption_polls_and_recovers(self):
        client = core.NvrClient("192.168.1.10", "admin")
        poll_called = []
        client.poll_network_until_connected = lambda **kw: poll_called.append(True)

        first_resp = MagicMock()
        first_resp.__enter__.return_value = first_resp
        first_resp.__exit__.return_value = False
        first_resp.headers = {"Content-Length": "8"}
        first_resp.read.side_effect = OSError("Network unreachable")

        second_resp = MagicMock()
        second_resp.__enter__.return_value = second_resp
        second_resp.__exit__.return_value = False
        second_resp.headers = {"Content-Length": "8"}
        second_resp.read.side_effect = [b"12345678", b""]

        client._open = MagicMock(side_effect=[first_resp, second_resp])

        seg = core.Segment(
            uri="u", name="s", start=datetime(2026, 9, 10, 10, 0, 0),
            end=datetime(2026, 9, 10, 10, 10, 0), size=8
        )
        out_file = os.path.join(tempfile.gettempdir(), "test_download_reconnect.bin")
        try:
            received_bytes = []
            client.download(seg, out_file, on_bytes=lambda n: received_bytes.append(n))
            self.assertTrue(os.path.exists(out_file))
            with open(out_file, "rb") as f:
                self.assertEqual(f.read(), b"12345678")
            self.assertEqual(len(poll_called), 1)
            self.assertEqual(sum(received_bytes), 8)
        finally:
            if os.path.exists(out_file):
                os.remove(out_file)


class TestAuthLockoutCooldown(unittest.TestCase):
    """TICKET-3: Auth Lockout Cooldown & Auto-retry tests."""

    def test_wait_auth_lockout_short_wait_warning_callback(self):
        client = core.NvrClient("192.168.1.10", "admin")
        status_calls = []

        with patch("time.sleep") as mock_sleep:
            client.wait_auth_lockout(on_status=status_calls.append, wait_seconds=1)
            mock_sleep.assert_called_once_with(1)

        self.assertTrue(any("Login failed for NVR 192.168.1.10 (HTTP 401)" in s for s in status_calls))

    def test_wait_auth_lockout_periodic_cooldown_callbacks(self):
        client = core.NvrClient("192.168.1.10", "admin")
        status_calls = []

        with patch("time.sleep") as mock_sleep:
            client.wait_auth_lockout(on_status=status_calls.append, wait_seconds=120)
            self.assertEqual(mock_sleep.call_count, 120)

        self.assertTrue(any("Waiting 2 minutes" in s for s in status_calls))
        self.assertTrue(any("1 minutes remaining" in s for s in status_calls))

    def test_wait_auth_lockout_pre_cancelled(self):
        client = core.NvrClient("192.168.1.10", "admin")
        cancel_ev = threading.Event()
        cancel_ev.set()

        with patch("time.sleep") as mock_sleep:
            with self.assertRaises(core.Cancelled):
                client.wait_auth_lockout(cancel_event=cancel_ev, wait_seconds=1800)
            mock_sleep.assert_not_called()

    def test_wait_auth_lockout_cancelled_during_cooldown(self):
        client = core.NvrClient("192.168.1.10", "admin")
        cancel_ev = threading.Event()

        def trigger_cancel(secs):
            cancel_ev.set()

        with patch("time.sleep", side_effect=trigger_cancel):
            with self.assertRaises(core.Cancelled):
                client.wait_auth_lockout(cancel_event=cancel_ev, wait_seconds=1800)

    def test_wait_auth_lockout_env_var_override(self):
        client = core.NvrClient("192.168.1.10", "admin")
        with patch.dict(os.environ, {"AUTH_LOCKOUT_WAIT_S": "5"}):
            with patch("time.sleep") as mock_sleep:
                client.wait_auth_lockout()
                self.assertEqual(mock_sleep.call_count, 5)


if __name__ == "__main__":
    unittest.main()

