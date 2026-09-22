#!/usr/bin/env python3
"""Comprehensive test suite for CCTV footage ingestion system."""

import collections
import io
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time as systime
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
        orig_prefetcher = core.WindowPrefetcher

        class DummyPrefetcher:
            def __init__(self, *a, **kw):
                pass
            def start_prefetch(self, *a, **kw):
                pass
            def get_prefetched(self, *a, **kw):
                return {}
            def cancel(self):
                pass

        captured_results = []
        core.run_batch = fake_run_batch
        core.write_run_log = lambda results, path: captured_results.extend(results)
        core.WindowPrefetcher = DummyPrefetcher
        try:
            webgui.do_run(plan)
            self.assertEqual(len(captured_results), 2)
            self.assertIn("[2026-09-10]", captured_results[0]["notes"][0])
            self.assertIn("[2026-09-11]", captured_results[1]["notes"][0])
        finally:
            core.run_batch = orig_run_batch
            core.write_run_log = orig_write_log
            core.WindowPrefetcher = orig_prefetcher

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


class TestBackgroundProcessor(unittest.TestCase):
    """Unit tests for Core Background Processor and Concurrency-Throttled Task Queue."""

    def test_fifo_execution_and_on_success(self):
        bp = core.BackgroundProcessor(max_workers=1)
        executed_tasks = []
        success_results = []

        def make_fn(tid):
            def _fn():
                executed_tasks.append(tid)
                return f"res_{tid}"
            return _fn

        for i in range(5):
            tid = f"task_{i}"
            task = core.BackgroundTask(
                task_id=tid,
                camera_key="cam1",
                fn=make_fn(tid),
                on_success=success_results.append,
            )
            bp.submit(task)

        finished = bp.wait_all(timeout=5.0)
        self.assertTrue(finished)
        self.assertEqual(executed_tasks, ["task_0", "task_1", "task_2", "task_3", "task_4"])
        self.assertEqual(success_results, ["res_task_0", "res_task_1", "res_task_2", "res_task_3", "res_task_4"])
        bp.shutdown(wait=True)

    def test_task_args_and_kwargs(self):
        bp = core.BackgroundProcessor(max_workers=1)
        res_holder = []

        def calc(a, b, multiplier=1):
            return (a + b) * multiplier

        task = core.BackgroundTask(
            task_id="calc_1",
            camera_key="cam1",
            fn=calc,
            args=(10, 20),
            kwargs={"multiplier": 3},
            on_success=res_holder.append,
        )
        bp.submit(task)
        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertEqual(res_holder, [90])
        bp.shutdown(wait=True)

    def test_concurrency_cap_verification(self):
        import time as systime
        max_workers = 2
        bp = core.BackgroundProcessor(max_workers=max_workers)
        active_count = 0
        max_active_seen = 0
        lock = threading.Lock()
        barrier = threading.Barrier(max_workers)
        completed = []

        def concurrent_fn(task_idx):
            nonlocal active_count, max_active_seen
            with lock:
                active_count += 1
                if active_count > max_active_seen:
                    max_active_seen = active_count
            if task_idx < max_workers:
                try:
                    barrier.wait(timeout=2.0)
                except threading.BrokenBarrierError:
                    pass
            systime.sleep(0.05)
            with lock:
                active_count -= 1
            return task_idx

        for i in range(5):
            task = core.BackgroundTask(
                task_id=f"concurrent_{i}",
                camera_key=f"cam_{i}",
                fn=concurrent_fn,
                args=(i,),
                on_success=completed.append,
            )
            bp.submit(task)

        finished = bp.wait_all(timeout=5.0)
        self.assertTrue(finished)
        self.assertEqual(len(completed), 5)
        self.assertEqual(max_active_seen, 2)
        self.assertLessEqual(bp._peak_active_tasks, 2)
        bp.shutdown(wait=True)

    def test_error_isolation(self):
        bp = core.BackgroundProcessor(max_workers=2)
        successes = []
        errors = []

        def failing_fn():
            raise ValueError("Task intentionally failed")

        def normal_fn(val):
            return val

        t1 = core.BackgroundTask(
            task_id="t1",
            camera_key="cam1",
            fn=normal_fn,
            args=(1,),
            on_success=successes.append,
            on_error=errors.append,
        )
        t2 = core.BackgroundTask(
            task_id="t2",
            camera_key="cam2",
            fn=failing_fn,
            on_success=successes.append,
            on_error=errors.append,
        )
        t3 = core.BackgroundTask(
            task_id="t3",
            camera_key="cam3",
            fn=normal_fn,
            args=(3,),
            on_success=successes.append,
            on_error=errors.append,
        )

        bp.submit(t1)
        bp.submit(t2)
        bp.submit(t3)

        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertIn(1, successes)
        self.assertIn(3, successes)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(str(errors[0]), "Task intentionally failed")

        # Confirm workers remain healthy and process subsequent tasks
        t4 = core.BackgroundTask(
            task_id="t4",
            camera_key="cam4",
            fn=normal_fn,
            args=(4,),
            on_success=successes.append,
            on_error=errors.append,
        )
        bp.submit(t4)
        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertIn(4, successes)
        bp.shutdown(wait=True)

    def test_temp_files_cleaned_up_on_error(self):
        bp = core.BackgroundProcessor(max_workers=1)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
            f.write(b"temp data")

        self.assertTrue(os.path.exists(tmp_path))

        def failing_task():
            raise RuntimeError("Task error")

        task = core.BackgroundTask(
            task_id="fail_clean",
            camera_key="cam1",
            fn=failing_task,
            temp_files=[tmp_path],
        )
        bp.submit(task)
        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertFalse(os.path.exists(tmp_path))
        bp.shutdown(wait=True)

    def test_temp_files_preserved_on_success(self):
        bp = core.BackgroundProcessor(max_workers=1)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
            f.write(b"temp data")

        self.assertTrue(os.path.exists(tmp_path))

        task = core.BackgroundTask(
            task_id="succ_preserve",
            camera_key="cam1",
            fn=lambda: "ok",
            temp_files=[tmp_path],
        )
        bp.submit(task)
        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertTrue(os.path.exists(tmp_path))
        os.remove(tmp_path)
        bp.shutdown(wait=True)

    def test_cancellation_flushes_pending_tasks(self):
        bp = core.BackgroundProcessor(max_workers=1)
        task1_started = threading.Event()
        task1_block = threading.Event()
        task2_executed = threading.Event()
        task2_errors = []

        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path2 = f.name

        def task1_fn():
            task1_started.set()
            task1_block.wait(timeout=2.0)

        def task2_fn():
            task2_executed.set()

        t1 = core.BackgroundTask(task_id="t1", camera_key="cam1", fn=task1_fn)
        t2 = core.BackgroundTask(
            task_id="t2",
            camera_key="cam2",
            fn=task2_fn,
            temp_files=[tmp_path2],
            on_error=task2_errors.append,
        )

        bp.submit(t1)
        task1_started.wait(timeout=2.0)
        bp.submit(t2)

        bp.cancel()
        task1_block.set()

        res = bp.wait_all(timeout=5.0)
        self.assertFalse(res)
        self.assertTrue(bp.is_cancelled)
        self.assertFalse(task2_executed.is_set())
        self.assertEqual(len(task2_errors), 1)
        self.assertIsInstance(task2_errors[0], core.Cancelled)
        self.assertFalse(os.path.exists(tmp_path2))
        bp.shutdown(wait=True)

    def test_cancellation_kills_running_procs(self):
        proc_registry = core.ProcRegistry()
        mock_proc = MagicMock()
        proc_registry.register(mock_proc)

        bp = core.BackgroundProcessor(max_workers=1, proc_registry=proc_registry)
        task_started = threading.Event()
        task_block = threading.Event()

        def block_fn():
            task_started.set()
            task_block.wait(timeout=2.0)

        task = core.BackgroundTask(task_id="proc_task", camera_key="cam1", fn=block_fn)
        bp.submit(task)
        task_started.wait(timeout=2.0)

        bp.cancel()
        task_block.set()

        bp.wait_all(timeout=2.0)
        mock_proc.kill.assert_called()
        bp.shutdown(wait=True)

    def test_wait_all_barrier_synchronization(self):
        import time as systime
        bp = core.BackgroundProcessor(max_workers=2)
        done_items = []

        def worker_fn(val):
            systime.sleep(0.02)
            done_items.append(val)
            return val

        for i in range(6):
            bp.submit(core.BackgroundTask(task_id=f"w_{i}", camera_key="cam", fn=worker_fn, args=(i,)))

        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertEqual(len(done_items), 6)
        self.assertEqual(bp.active_tasks, 0)
        bp.shutdown(wait=True)

    def test_wait_all_timeout(self):
        import time as systime
        bp = core.BackgroundProcessor(max_workers=1)
        block_ev = threading.Event()

        task = core.BackgroundTask(task_id="slow", camera_key="cam", fn=lambda: block_ev.wait(timeout=2.0))
        bp.submit(task)

        start_t = systime.monotonic()
        res = bp.wait_all(timeout=0.1)
        elapsed = systime.monotonic() - start_t

        self.assertFalse(res)
        self.assertLess(elapsed, 0.5)
        block_ev.set()
        bp.cancel()
        bp.shutdown(wait=True)

    def test_wait_all_unblocks_on_cancel_event(self):
        import time as systime
        cancel_ev = threading.Event()
        bp = core.BackgroundProcessor(max_workers=1, cancel_event=cancel_ev)
        block_ev = threading.Event()

        task = core.BackgroundTask(task_id="slow_cancel", camera_key="cam", fn=lambda: block_ev.wait(timeout=2.0))
        bp.submit(task)

        def trigger():
            systime.sleep(0.05)
            cancel_ev.set()

        threading.Thread(target=trigger, daemon=True).start()

        start_t = systime.monotonic()
        res = bp.wait_all(timeout=5.0)
        elapsed = systime.monotonic() - start_t

        self.assertFalse(res)
        self.assertLess(elapsed, 1.5)
        block_ev.set()
        bp.shutdown(wait=True)

    def test_submit_after_shutdown_raises(self):
        bp = core.BackgroundProcessor(max_workers=1)
        bp.shutdown(wait=True)
        self.assertTrue(bp.is_shutdown)
        with self.assertRaises(RuntimeError):
            bp.submit(core.BackgroundTask(task_id="t", camera_key="c", fn=lambda: None))

    def test_cli_remux_workers_argument(self):
        import argparse
        self.assertEqual(core.DEFAULT_REMUX_WORKERS, 2)

        with patch("sys.argv", ["cctv_retrieve.py", "--remux-workers", "4"]):
            ap = argparse.ArgumentParser()
            ap.add_argument("--remux-workers", type=int, default=core.DEFAULT_REMUX_WORKERS)
            args = ap.parse_args(["--remux-workers", "4"])
            self.assertEqual(args.remux_workers, 4)

            args_def = ap.parse_args([])
            self.assertEqual(args_def.remux_workers, 2)


class TestDecoupledRemuxing(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_remux = core.remux_to_mp4

    def tearDown(self):
        core.remux_to_mp4 = self.orig_remux
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_camera_task_tracker_lifecycle_and_aggregation(self):
        tracker = core.CameraTaskTracker()
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        cam_key = core.camera_key(row)
        dummy_work_dir = os.path.join(self.test_dir, "work_dir")
        os.makedirs(dummy_work_dir, exist_ok=True)

        stages = []

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row, **info):
                stages.append((stage, info))

        cb = MockCallbacks()
        tracker.register_camera(cam_key, row=row, work_dir=dummy_work_dir, callbacks=cb)
        self.assertTrue(tracker.has_camera(cam_key))
        self.assertTrue(tracker.has_pending_tasks(cam_key))

        # Submit 2 tasks
        tracker.task_submitted(cam_key, "t1")
        tracker.task_submitted(cam_key, "t2")

        # Download finishes while tasks are running -> stage 'remuxing'
        tracker.complete_download(cam_key, result={"notes": ["download note"], "files": ["existing.mp4"]})
        self.assertEqual(stages[-1][0], "remuxing")
        self.assertFalse(tracker.is_finalized(cam_key))
        self.assertTrue(os.path.exists(dummy_work_dir))

        # Task 1 completes
        tracker.task_completed(cam_key, "t1", file="seg1.mp4", notes=["remux1 ok"])
        self.assertFalse(tracker.is_finalized(cam_key))
        self.assertTrue(os.path.exists(dummy_work_dir))

        # Task 2 completes -> all finished -> stage 'ok' and work_dir cleaned up
        tracker.task_completed(cam_key, "t2", file="seg2.mp4", notes=["remux2 ok"])
        self.assertTrue(tracker.is_finalized(cam_key))
        self.assertEqual(stages[-1][0], "ok")
        self.assertFalse(os.path.exists(dummy_work_dir))

        res = tracker.get_result(cam_key)
        self.assertTrue(res["ok"])
        self.assertEqual(res["files"], ["existing.mp4", "seg1.mp4", "seg2.mp4"])
        self.assertIn("download note", res["notes"])
        self.assertIn("remux1 ok", res["notes"])
        self.assertIn("remux2 ok", res["notes"])

    def test_process_camera_pipelined_segment_downloads(self):
        """Verify that download for segment 2 is called before remux for segment 1 finishes."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        t2 = datetime(2026, 9, 10, 10, 20, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        s2 = core.Segment(uri="u2", name="seg2", start=t1, end=t2, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1, s2])

        class DummyArgs:
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()
        seg1_remux_started = threading.Event()
        seg2_download_started = threading.Event()
        seg1_remux_finished = threading.Event()

        class MockClient:
            host = "192.168.1.10"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"data")
                if seg.name == "seg2":
                    # Crucial pipelining assertion: seg1 remux must NOT have finished yet!
                    test_case.assertFalse(
                        seg1_remux_finished.is_set(),
                        "Segment 2 download started, but Segment 1 remux had already finished!"
                    )
                    seg2_download_started.set()

        test_case = self

        def fake_remux(src, dst, proc_reg, cancel_ev):
            if "seg1" in src:
                seg1_remux_started.set()
                # Segment 1 remux waits until segment 2 has started downloading!
                seg2_download_started.wait(timeout=5.0)
                with open(dst, "wb") as f:
                    f.write(b"mp4_1")
                seg1_remux_finished.set()
                return True, ""
            else:
                with open(dst, "wb") as f:
                    f.write(b"mp4_2")
                return True, ""

        core.remux_to_mp4 = fake_remux

        bp = core.BackgroundProcessor(max_workers=2)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        res = core.process_camera(
            plan, args, t0, t2, MockClient(), run_dir, core.Callbacks(), proc_reg, None,
            bg_processor=bp, camera_tracker=tracker
        )
        self.assertTrue(res["ok"])
        self.assertTrue(seg2_download_started.is_set())

        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertTrue(seg1_remux_started.is_set())
        self.assertTrue(seg1_remux_finished.is_set())

        final_res = tracker.get_result(plan.key)
        self.assertTrue(final_res["ok"])
        self.assertEqual(len(final_res["files"]), 2)
        bp.shutdown(wait=True)

    def test_run_batch_early_nvr_slot_release(self):
        """Configure 2 cameras on the same NVR with per_nvr=1.
        Verify Camera 2 starts downloading immediately after Camera 1 finishes its network download,
        while Camera 1's remux is still executing in the background.
        """
        row_a = {"name": "CamA", "nvr": "192.168.1.10", "channel": "D1"}
        row_b = {"name": "CamB", "nvr": "192.168.1.10", "channel": "D2"}
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        sa = core.Segment(uri="u1", name="seg_a", start=t0, end=t1, size=1000)
        sb = core.Segment(uri="u2", name="seg_b", start=t0, end=t1, size=1000)
        plan_a = core.CameraPlan(row=row_a, key=core.camera_key(row_a), track_id=101, segments=[sa])
        plan_b = core.CameraPlan(row=row_b, key=core.camera_key(row_b), track_id=102, segments=[sb])

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        cam_a_remux_started = threading.Event()
        cam_b_download_started = threading.Event()
        cam_a_remux_finished = threading.Event()

        orig_plan_camera = core.plan_camera
        orig_mark_existing = core.mark_existing
        try:
            def fake_plan_camera(r, client, start, end, mode, **kw):
                if r["channel"] == "D1":
                    return plan_a
                return plan_b

            core.plan_camera = fake_plan_camera
            core.mark_existing = lambda p, args, start, end: None

            orig_download = core.NvrClient.download

            def fake_download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"data")
                if seg.name == "seg_b":
                    cam_b_download_started.set()

            core.NvrClient.download = fake_download

            def fake_remux(src, dst, proc_reg, cancel_ev):
                if "seg_a" in src:
                    cam_a_remux_started.set()
                    # Wait for Camera B download to start!
                    # If NVR slot was not released early, Camera B could never download while Cam A remuxes!
                    cam_b_download_started.wait(timeout=5.0)
                    with open(dst, "wb") as f:
                        f.write(b"mp4_a")
                    cam_a_remux_finished.set()
                    return True, ""
                else:
                    with open(dst, "wb") as f:
                        f.write(b"mp4_b")
                    return True, ""

            core.remux_to_mp4 = fake_remux

            results = core.run_batch([row_a, row_b], args, t0, t1)

            self.assertTrue(cam_b_download_started.is_set())
            self.assertTrue(cam_a_remux_finished.is_set())
            self.assertEqual(len(results), 2)
            self.assertTrue(all(r["ok"] for r in results))
            self.assertEqual(sum(len(r["files"]) for r in results), 2)
        finally:
            core.plan_camera = orig_plan_camera
            core.mark_existing = orig_mark_existing
            core.NvrClient.download = orig_download

    def test_remux_failure_propagates_to_camera_result(self):
        """Simulate ffmpeg failure in background remux; verify camera result is marked ok=False with appropriate error."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()

        class MockClient:
            host = "192.168.1.10"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"data")

        def fake_failing_remux(src, dst, proc_reg, cancel_ev):
            return False, "corrupted moov atom"

        core.remux_to_mp4 = fake_failing_remux

        stages = []

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row, **info):
                stages.append((stage, info))

        cb = MockCallbacks()
        bp = core.BackgroundProcessor(max_workers=1)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        core.process_camera(
            plan, args, t0, t1, MockClient(), run_dir, cb, proc_reg, None,
            bg_processor=bp, camera_tracker=tracker
        )
        bp.wait_all(timeout=5.0)

        final_res = tracker.get_result(plan.key)
        self.assertFalse(final_res["ok"])
        self.assertTrue(any("corrupted moov atom" in err for err in final_res["errors"]))
        self.assertEqual(stages[-1][0], "fail")
        bp.shutdown(wait=True)

    def test_backward_compatibility_sync_process_camera(self):
        """Call process_camera with bg_processor=None; verify it runs synchronously and returns identical result structures."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()

        class MockClient:
            host = "192.168.1.10"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"data")

        def fake_sync_remux(src, dst, proc_reg, cancel_ev):
            with open(dst, "wb") as f:
                f.write(b"sync_mp4")
            return True, ""

        core.remux_to_mp4 = fake_sync_remux

        stages = []

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row, **info):
                stages.append((stage, info))

        cb = MockCallbacks()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        # Called with bg_processor=None (default)
        res = core.process_camera(
            plan, args, t0, t1, MockClient(), run_dir, cb, proc_reg, None
        )

        self.assertTrue(res["ok"])
        self.assertEqual(len(res["files"]), 1)
        self.assertTrue(os.path.exists(res["files"][0]))
        self.assertEqual(stages[-1][0], "ok")
        # Ensure work_dir was cleaned up
        cam_work_dir = os.path.join(run_dir, core.safe_name(plan.key))
        self.assertFalse(os.path.exists(cam_work_dir))


class TestAsyncTrimAndLegacyOffloading(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="cctv_trim_test_")
        self.orig_cut_clip = core.cut_clip
        self.orig_repair_legacy = core.repair_legacy_file
        self.orig_remux = core.remux_to_mp4
        self.orig_plan = core.plan_camera
        self.orig_mark = core.mark_existing
        self.orig_download = core.NvrClient.download
        self.orig_snapshot_clip = core.snapshot_from_clip

    def tearDown(self):
        core.cut_clip = self.orig_cut_clip
        core.repair_legacy_file = self.orig_repair_legacy
        core.remux_to_mp4 = self.orig_remux
        core.plan_camera = self.orig_plan
        core.mark_existing = self.orig_mark
        core.NvrClient.download = self.orig_download
        core.snapshot_from_clip = self.orig_snapshot_clip
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_legacy_file(self, path):
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
            "-c:v", "libx264", "-f", "vob", path,
        ]
        subprocess.run(cmd, capture_output=True, check=True)

    def test_trim_mode_releases_nvr_slot_before_cut(self):
        """Verify that in trim mode, active[host] is decremented before cut_clip starts/finishes in background."""
        row_a = {"name": "CamA", "nvr": "192.168.1.10", "channel": "D1"}
        row_b = {"name": "CamB", "nvr": "192.168.1.10", "channel": "D2"}
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        sa = core.Segment(uri="u1", name="seg_a", start=t0, end=t1, size=1000)
        sb = core.Segment(uri="u2", name="seg_b", start=t0, end=t1, size=1000)
        plan_a = core.CameraPlan(row=row_a, key=core.camera_key(row_a), track_id=101, segments=[sa])
        plan_b = core.CameraPlan(row=row_b, key=core.camera_key(row_b), track_id=102, segments=[sb])

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = True
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        cam_a_cut_started = threading.Event()
        cam_b_download_started = threading.Event()
        cam_a_cut_finished = threading.Event()

        def fake_plan_camera(r, client, start, end, mode, **kw):
            return plan_a if r["channel"] == "D1" else plan_b

        core.plan_camera = fake_plan_camera
        core.mark_existing = lambda p, args, start, end: None

        def fake_download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
            with open(dest, "wb") as f:
                f.write(b"data")
            if seg.name == "seg_b":
                cam_b_download_started.set()

        core.NvrClient.download = fake_download

        def fake_cut(parts, out_path, proc_registry, cancel_event, work_dir):
            if "CamA" in out_path:
                cam_a_cut_started.set()
                # Wait for Camera B download to start!
                # If NVR slot was not released early, Camera B could never download while Cam A cuts!
                cam_b_download_started.wait(timeout=5.0)
                with open(out_path, "wb") as f:
                    f.write(b"cut_mp4_a")
                cam_a_cut_finished.set()
                return True, ""
            else:
                with open(out_path, "wb") as f:
                    f.write(b"cut_mp4_b")
                return True, ""

        core.cut_clip = fake_cut

        results = core.run_batch([row_a, row_b], args, t0, t1)

        self.assertTrue(cam_b_download_started.is_set())
        self.assertTrue(cam_a_cut_finished.is_set())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sum(len(r["files"]) for r in results), 2)

    def test_cut_clip_background_success(self):
        """Multi-segment trim completes in background, creates valid .mp4, and cleans scratch files."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 5, 0)
        t2 = datetime(2026, 9, 10, 10, 10, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        s2 = core.Segment(uri="u2", name="seg2", start=t1, end=t2, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1, s2])

        class DummyArgs:
            trim = True
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()

        class MockClient:
            host = "192.168.1.10"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"dummy segment data")

        cut_executed = threading.Event()

        def fake_cut(parts, out_path, proc_registry, cancel_event, work_dir):
            cut_executed.set()
            self.assertEqual(len(parts), 2)
            self.assertTrue(os.path.isdir(work_dir))
            with open(out_path, "wb") as f:
                f.write(b"mp4 content")
            return True, ""

        core.cut_clip = fake_cut

        stages = []

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row=None, **info):
                stages.append((stage, info))

        cb = MockCallbacks()
        bp = core.BackgroundProcessor(max_workers=1)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        core.process_camera(
            plan, args, t0, t2, MockClient(), run_dir, cb, proc_reg, None,
            bg_processor=bp, camera_tracker=tracker,
        )

        self.assertTrue(bp.wait_all(timeout=5.0))
        self.assertTrue(cut_executed.is_set())

        final_res = tracker.get_result(plan.key)
        self.assertTrue(final_res["ok"])
        self.assertEqual(len(final_res["files"]), 1)
        self.assertTrue(os.path.exists(final_res["files"][0]))

        stage_names = [s[0] for s in stages]
        self.assertIn("cutting", stage_names)
        self.assertEqual(stage_names[-1], "ok")

        cam_work_dir = os.path.join(run_dir, core.safe_name(plan.key))
        self.assertFalse(os.path.exists(cam_work_dir))
        bp.shutdown(wait=True)

    def test_cut_clip_background_failure(self):
        """Simulate ffmpeg concat error; verify camera status is set to fail, error is reported, and temporary files in work_dir are cleaned up."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 5, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = True
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()

        class MockClient:
            host = "192.168.1.10"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"data")

        def fake_failing_cut(parts, out_path, proc_registry, cancel_event, work_dir):
            return False, "corrupted concat list"

        core.cut_clip = fake_failing_cut

        stages = []

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row=None, **info):
                stages.append((stage, info))

        cb = MockCallbacks()
        bp = core.BackgroundProcessor(max_workers=1)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        core.process_camera(
            plan, args, t0, t1, MockClient(), run_dir, cb, proc_reg, None,
            bg_processor=bp, camera_tracker=tracker,
        )

        self.assertTrue(bp.wait_all(timeout=5.0))

        final_res = tracker.get_result(plan.key)
        self.assertFalse(final_res["ok"])
        self.assertTrue(any("corrupted concat list" in err for err in final_res["errors"]))
        self.assertEqual(stages[-1][0], "fail")

        cam_work_dir = os.path.join(run_dir, core.safe_name(plan.key))
        self.assertFalse(os.path.exists(cam_work_dir))
        bp.shutdown(wait=True)

    def test_legacy_repair_offloaded_to_background(self):
        """Pre-existing legacy files on disk are queued and converted by the background processor without blocking download slots."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()
        raw_dest = core.raw_segment_path(row, s1, self.test_dir)
        os.makedirs(os.path.dirname(raw_dest), exist_ok=True)
        self._create_legacy_file(raw_dest)
        self.assertFalse(core.is_real_mp4(raw_dest))

        core.mark_existing(plan, args, t0, t1)
        self.assertIn("seg1", plan.legacy)

        class MockClient:
            host = "192.168.1.10"
            download_calls = 0

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                self.download_calls += 1

        client = MockClient()
        stages = []

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row=None, **info):
                stages.append((stage, info))

        cb = MockCallbacks()
        bp = core.BackgroundProcessor(max_workers=1)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        core.process_camera(
            plan, args, t0, t1, client, run_dir, cb, proc_reg, None,
            bg_processor=bp, camera_tracker=tracker,
        )

        self.assertEqual(client.download_calls, 0)
        self.assertTrue(bp.wait_all(timeout=5.0))

        final_res = tracker.get_result(plan.key)
        self.assertTrue(final_res["ok"])
        self.assertEqual(len(final_res["files"]), 1)
        self.assertEqual(final_res["files"][0], raw_dest)
        self.assertTrue(core.is_real_mp4(raw_dest))
        self.assertTrue(any("converted legacy file" in note for note in final_res["notes"]))
        self.assertEqual(stages[-1][0], "ok")
        bp.shutdown(wait=True)

    def test_trimmed_legacy_clip_offloaded_to_background(self):
        """Pre-existing trimmed legacy clip is queued and repaired by BackgroundProcessor."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 0, 1)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = True
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10

        args = DummyArgs()
        clip_path = core.output_base(row, t0, self.test_dir) + ".mp4"
        os.makedirs(os.path.dirname(clip_path), exist_ok=True)
        self._create_legacy_file(clip_path)
        self.assertFalse(core.is_real_mp4(clip_path))

        core.mark_existing(plan, args, t0, t1)
        self.assertTrue(plan.legacy_clip)

        class MockClient:
            host = "192.168.1.10"
            download_calls = 0

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                self.download_calls += 1

        client = MockClient()
        bp = core.BackgroundProcessor(max_workers=1)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        core.process_camera(
            plan, args, t0, t1, client, run_dir, core.Callbacks(), proc_reg, None,
            bg_processor=bp, camera_tracker=tracker,
        )

        self.assertEqual(client.download_calls, 0)
        self.assertTrue(bp.wait_all(timeout=5.0))

        final_res = tracker.get_result(plan.key)
        self.assertTrue(final_res["ok"])
        self.assertEqual(len(final_res["files"]), 1)
        self.assertEqual(final_res["files"][0], clip_path)
        self.assertTrue(core.is_real_mp4(clip_path))
        self.assertTrue(any("converted legacy clip" in note for note in final_res["notes"]))
        bp.shutdown(wait=True)

    def test_cut_clip_background_mode_both(self):
        """In trim mode with mode=='both', background cut task generates .mp4 and extracts snapshot .jpg."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 5, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = True
            output_dir = self.test_dir
            mode = "both"
            timeout = 10

        args = DummyArgs()

        class MockClient:
            host = "192.168.1.10"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"data")

        def fake_cut(parts, out_path, proc_registry, cancel_event, work_dir):
            with open(out_path, "wb") as f:
                f.write(b"mp4 content")
            return True, ""

        def fake_snapshot_clip(clip_path, out_path, proc_registry, cancel_event):
            with open(out_path, "wb") as f:
                f.write(b"jpg content")
            return True, ""

        core.cut_clip = fake_cut
        core.snapshot_from_clip = fake_snapshot_clip

        bp = core.BackgroundProcessor(max_workers=1)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()

        core.process_camera(
            plan, args, t0, t1, MockClient(), run_dir, core.Callbacks(), proc_reg, None,
            bg_processor=bp, camera_tracker=tracker,
        )

        self.assertTrue(bp.wait_all(timeout=5.0))

        final_res = tracker.get_result(plan.key)
        self.assertTrue(final_res["ok"])
        self.assertEqual(len(final_res["files"]), 2)
        base = core.output_base(row, t0, self.test_dir)
        self.assertIn(base + ".mp4", final_res["files"])
        self.assertIn(base + ".jpg", final_res["files"])
        bp.shutdown(wait=True)


class TestDecoupledSnapshotQuotas(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="cctv_snap_test_")
        self.orig_snapshot_rtsp = core.snapshot_via_rtsp
        self.orig_snapshot_clip = core.snapshot_from_clip
        self.orig_cut_clip = core.cut_clip
        self.orig_remux = core.remux_to_mp4
        self.orig_plan = core.plan_camera
        self.orig_mark = core.mark_existing
        self.orig_download = core.NvrClient.download

    def tearDown(self):
        core.snapshot_via_rtsp = self.orig_snapshot_rtsp
        core.snapshot_from_clip = self.orig_snapshot_clip
        core.cut_clip = self.orig_cut_clip
        core.remux_to_mp4 = self.orig_remux
        core.plan_camera = self.orig_plan
        core.mark_existing = self.orig_mark
        core.NvrClient.download = self.orig_download
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_snapshot_in_both_mode_nonblocking(self):
        """In mode == 'both', NVR download slot is released while RTSP snapshot executes in background."""
        row_a = {"name": "CamA", "nvr": "192.168.1.10", "channel": "D1"}
        row_b = {"name": "CamB", "nvr": "192.168.1.10", "channel": "D2"}
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        sa = core.Segment(uri="u1", name="seg_a", start=t0, end=t1, size=1000)
        sb = core.Segment(uri="u2", name="seg_b", start=t0, end=t1, size=1000)
        plan_a = core.CameraPlan(row=row_a, key=core.camera_key(row_a), track_id=101, segments=[sa])
        plan_b = core.CameraPlan(row=row_b, key=core.camera_key(row_b), track_id=102, segments=[sb])

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "both"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        cam_a_snap_started = threading.Event()
        cam_b_download_started = threading.Event()

        def fake_plan_camera(r, client, start, end, mode, **kw):
            return plan_a if r["channel"] == "D1" else plan_b

        core.plan_camera = fake_plan_camera
        core.mark_existing = lambda p, args, start, end: None

        def fake_download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
            with open(dest, "wb") as f:
                f.write(b"data")
            if seg.name == "seg_b":
                cam_b_download_started.set()

        core.NvrClient.download = fake_download

        def fake_remux(src, dest, proc_registry, cancel_event):
            with open(dest, "wb") as f:
                f.write(b"mp4_data")
            return True, ""

        core.remux_to_mp4 = fake_remux

        def fake_snapshot_rtsp(host, track_id, start, user, password, out_path, timeout_s, proc_registry, cancel_event):
            if track_id == 101:
                cam_a_snap_started.set()
                # Cam B must be able to start downloading while Cam A's snapshot is running!
                # If NVR slot was not released early, Cam B would be blocked by per_nvr=1.
                cam_b_download_started.wait(timeout=5.0)
            with open(out_path, "wb") as f:
                f.write(b"jpg_data")
            return True, ""

        core.snapshot_via_rtsp = fake_snapshot_rtsp

        results = core.run_batch([row_a, row_b], args, t0, t1)

        self.assertTrue(cam_a_snap_started.is_set())
        self.assertTrue(cam_b_download_started.is_set())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        base_a = core.output_base(row_a, t0, self.test_dir)
        base_b = core.output_base(row_b, t0, self.test_dir)
        res_a = next(r for r in results if r["channel"] == "D1")
        res_b = next(r for r in results if r["channel"] == "D2")
        self.assertIn(base_a + ".jpg", res_a["files"])
        self.assertIn(base_b + ".jpg", res_b["files"])

    def test_snapshot_from_clip_chained_after_trim(self):
        """In trim mode with both, snapshot is generated from clip in background."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 5, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}
        plan = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyArgs:
            trim = True
            output_dir = self.test_dir
            mode = "both"
            timeout = 10

        args = DummyArgs()

        class MockClient:
            host = "192.168.1.10"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"raw_data")

        cut_calls = []

        def fake_cut(parts, out_path, proc_registry, cancel_event, work_dir):
            cut_calls.append(out_path)
            with open(out_path, "wb") as f:
                f.write(b"mp4 content")
            return True, ""

        snap_calls = []

        def fake_snapshot_clip(clip_path, out_path, proc_registry, cancel_event):
            snap_calls.append((clip_path, out_path))
            with open(out_path, "wb") as f:
                f.write(b"jpg content")
            return True, ""

        core.cut_clip = fake_cut
        core.snapshot_from_clip = fake_snapshot_clip

        bp = core.BackgroundProcessor(max_workers=1)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch")
        proc_reg = core.ProcRegistry()
        stage_events = []

        class TrackingCallbacks(core.Callbacks):
            def stage(self, key, stage, row=None, **info):
                stage_events.append(stage)

        cb = TrackingCallbacks()

        core.process_camera(
            plan, args, t0, t1, MockClient(), run_dir, cb, proc_reg, None,
            bg_processor=bp, camera_tracker=tracker,
        )

        self.assertTrue(bp.wait_all(timeout=5.0))

        final_res = tracker.get_result(plan.key)
        self.assertTrue(final_res["ok"])
        base = core.output_base(row, t0, self.test_dir)
        self.assertEqual(len(cut_calls), 1)
        self.assertEqual(cut_calls[0], base + ".mp4")
        self.assertEqual(len(snap_calls), 1)
        self.assertEqual(snap_calls[0], (base + ".mp4", base + ".jpg"))
        self.assertIn(base + ".mp4", final_res["files"])
        self.assertIn(base + ".jpg", final_res["files"])
        self.assertIn("snapshot", stage_events)
        bp.shutdown(wait=True)

    def test_snapshot_warning_does_not_fail_camera(self):
        """Snapshot failure records warning note and camera stays ok=True."""
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 5, 0)
        s1 = core.Segment(uri="u1", name="seg1", start=t0, end=t1, size=1000)
        row = {"name": "Cam01", "nvr": "192.168.1.10", "channel": "D1"}

        class MockClient:
            host = "192.168.1.10"
            active_password = "pwd"

            def download(self, seg, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
                with open(dest, "wb") as f:
                    f.write(b"data")

        # 1. Non-trim mode (RTSP snapshot failure)
        plan_rtsp = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyNoTrimArgs:
            trim = False
            output_dir = self.test_dir
            mode = "both"
            timeout = 10
            user = "admin"
            password = "pwd"

        def fake_remux(src, dest, proc_registry, cancel_event):
            with open(dest, "wb") as f:
                f.write(b"mp4_data")
            return True, ""

        def fake_snapshot_rtsp_fail(host, track_id, start, user, password, out_path, timeout_s, proc_registry, cancel_event):
            return False, "rtsp connection timed out"

        core.remux_to_mp4 = fake_remux
        core.snapshot_via_rtsp = fake_snapshot_rtsp_fail

        bp = core.BackgroundProcessor(max_workers=2)
        tracker = core.CameraTaskTracker()
        run_dir = os.path.join(self.test_dir, "run_scratch_1")
        proc_reg = core.ProcRegistry()

        core.process_camera(
            plan_rtsp, DummyNoTrimArgs(), t0, t1, MockClient(), run_dir, core.Callbacks(), proc_reg, None,
            bg_processor=bp, camera_tracker=tracker,
        )

        self.assertTrue(bp.wait_all(timeout=5.0))
        res_rtsp = tracker.get_result(plan_rtsp.key)
        self.assertTrue(res_rtsp["ok"], "Camera must remain ok=True when snapshot fails")
        self.assertEqual(len(res_rtsp["errors"]), 0)
        self.assertTrue(any("snapshot failed (clip saved): rtsp connection timed out" in n for n in res_rtsp["notes"]))
        self.assertTrue(any(f.endswith(".mp4") for f in res_rtsp["files"]))
        bp.shutdown(wait=True)

        # 2. Trim mode (clip snapshot failure)
        plan_trim = core.CameraPlan(row=row, key=core.camera_key(row), track_id=101, segments=[s1])

        class DummyTrimArgs:
            trim = True
            output_dir = self.test_dir
            mode = "both"
            timeout = 10
            user = "admin"
            password = "pwd"

        def fake_cut(parts, out_path, proc_registry, cancel_event, work_dir):
            with open(out_path, "wb") as f:
                f.write(b"cut_mp4")
            return True, ""

        def fake_snapshot_clip_fail(clip_path, out_path, proc_registry, cancel_event):
            return False, "moov atom not found"

        core.cut_clip = fake_cut
        core.snapshot_from_clip = fake_snapshot_clip_fail

        bp2 = core.BackgroundProcessor(max_workers=2)
        tracker2 = core.CameraTaskTracker()
        run_dir2 = os.path.join(self.test_dir, "run_scratch_2")

        core.process_camera(
            plan_trim, DummyTrimArgs(), t0, t1, MockClient(), run_dir2, core.Callbacks(), proc_reg, None,
            bg_processor=bp2, camera_tracker=tracker2,
        )

        self.assertTrue(bp2.wait_all(timeout=5.0))
        res_trim = tracker2.get_result(plan_trim.key)
        self.assertTrue(res_trim["ok"], "Camera must remain ok=True when snapshot fails in trim mode")
        self.assertEqual(len(res_trim["errors"]), 0)
        self.assertTrue(any("snapshot failed (clip saved): moov atom not found" in n for n in res_trim["notes"]))
        base = core.output_base(row, t0, self.test_dir)
        self.assertIn(base + ".mp4", res_trim["files"])
        self.assertNotIn(base + ".jpg", res_trim["files"])
        bp2.shutdown(wait=True)

    def test_snapshot_cancelled_promptly(self):
        """ProcRegistry kills ffmpeg if cancelled."""
        import time as systime
        proc_reg = core.ProcRegistry()
        cancel_ev = threading.Event()
        mock_proc = MagicMock()
        proc_killed = threading.Event()

        def fake_kill():
            proc_killed.set()

        mock_proc.kill.side_effect = fake_kill
        mock_proc.returncode = -9

        def fake_communicate(timeout=None):
            proc_killed.wait(timeout=2.0)
            return (b"", b"Killed")

        mock_proc.communicate.side_effect = fake_communicate

        cancelled_caught = threading.Event()

        def run_target():
            try:
                core.snapshot_via_rtsp(
                    "192.168.1.10", 101, datetime(2026, 9, 10, 10, 0, 0),
                    "admin", "pwd", os.path.join(self.test_dir, "snap.jpg"),
                    10, proc_reg, cancel_ev,
                )
            except core.Cancelled:
                cancelled_caught.set()

        with patch("subprocess.Popen", return_value=mock_proc):
            t = threading.Thread(target=run_target)
            t.start()

            start_time = systime.time()
            while not proc_reg._procs and systime.time() - start_time < 2.0:
                systime.sleep(0.01)

            self.assertIn(mock_proc, proc_reg._procs)

            cancel_ev.set()
            proc_reg.kill_all()

            t.join(timeout=3.0)
            self.assertFalse(t.is_alive())
            mock_proc.kill.assert_called()
            self.assertTrue(cancelled_caught.is_set())


class TestStreamingSearchPipeline(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_plan_camera = core.plan_camera
        self.orig_mark_existing = core.mark_existing
        self.orig_download = core.NvrClient.download
        self.orig_remux = core.remux_to_mp4

    def tearDown(self):
        core.plan_camera = self.orig_plan_camera
        core.mark_existing = self.orig_mark_existing
        core.NvrClient.download = self.orig_download
        core.remux_to_mp4 = self.orig_remux
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_intra_batch_streaming_tail_latency_elimination(self):
        """Fast camera search (0.05s) starts downloading immediately while slow camera search (0.5s) is still running."""
        row_fast = {"name": "CamFast", "nvr": "192.168.1.10", "channel": "D1"}
        row_slow = {"name": "CamSlow", "nvr": "192.168.1.20", "channel": "D2"}
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        s_fast = core.Segment(uri="u1", name="seg_fast", start=t0, end=t1, size=1000)
        s_slow = core.Segment(uri="u2", name="seg_slow", start=t0, end=t1, size=1000)
        plan_fast = core.CameraPlan(row=row_fast, key=core.camera_key(row_fast), track_id=101, segments=[s_fast])
        plan_slow = core.CameraPlan(row=row_slow, key=core.camera_key(row_slow), track_id=102, segments=[s_slow])

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        slow_search_started = threading.Event()
        fast_download_started = threading.Event()

        search_times = {}
        download_times = {}

        def fake_plan_camera(r, client, start, end, mode, **kw):
            if r["name"] == "CamFast":
                systime.sleep(0.05)
                search_times["fast_search_end"] = systime.time()
                return plan_fast
            else:
                slow_search_started.set()
                # CamSlow simulates a slow NVR search (0.5s)
                systime.sleep(0.5)
                search_times["slow_search_end"] = systime.time()
                return plan_slow

        core.plan_camera = fake_plan_camera

        def fake_download(self, seg, dest, **kw):
            download_times[seg.name] = systime.time()
            with open(dest, "wb") as f:
                f.write(b"data")
            if seg.name == "seg_fast":
                fast_download_started.set()

        core.NvrClient.download = fake_download

        results = core.run_batch([row_fast, row_slow], args, t0, t1)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertTrue(fast_download_started.is_set())
        self.assertIn("seg_fast", download_times)
        self.assertIn("slow_search_end", search_times)
        # CamFast download started before CamSlow search finished!
        self.assertLess(download_times["seg_fast"], search_times["slow_search_end"])

    def test_concurrency_limits_during_streaming_search(self):
        """Verify active downloads never exceed args.workers globally or args.per_nvr per NVR."""
        rows = [
            {"name": "Cam1", "nvr": "192.168.1.10", "channel": "D1"},
            {"name": "Cam2", "nvr": "192.168.1.10", "channel": "D2"},
            {"name": "Cam3", "nvr": "192.168.1.20", "channel": "D3"},
            {"name": "Cam4", "nvr": "192.168.1.20", "channel": "D4"},
        ]
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        def fake_plan_camera(r, client, start, end, mode, **kw):
            idx = int(r["name"][-1])
            systime.sleep(0.01 * idx)
            seg = core.Segment(uri=f"u{idx}", name=f"seg{idx}", start=t0, end=t1, size=1000)
            return core.CameraPlan(row=r, key=core.camera_key(r), track_id=100 + idx, segments=[seg])

        core.plan_camera = fake_plan_camera

        active_global = 0
        max_global_seen = 0
        active_per_nvr = collections.Counter()
        max_per_nvr_seen = collections.Counter()
        lock = threading.Lock()

        def fake_download(self, seg, dest, **kw):
            nonlocal active_global, max_global_seen
            with lock:
                active_global += 1
                active_per_nvr[self.host] += 1
                if active_global > max_global_seen:
                    max_global_seen = active_global
                if active_per_nvr[self.host] > max_per_nvr_seen[self.host]:
                    max_per_nvr_seen[self.host] = active_per_nvr[self.host]

            systime.sleep(0.05)
            with open(dest, "wb") as f:
                f.write(b"data")

            with lock:
                active_global -= 1
                active_per_nvr[self.host] -= 1

        core.NvrClient.download = fake_download

        results = core.run_batch(rows, args, t0, t1)

        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertLessEqual(max_global_seen, args.workers)
        self.assertEqual(max_global_seen, args.workers)
        for host in ["192.168.1.10", "192.168.1.20"]:
            self.assertLessEqual(max_per_nvr_seen[host], args.per_nvr)

    def test_prefetched_plans_bypasses_search_pool(self):
        """Passing prefetched_plans starts downloads without invoking search."""
        row_a = {"name": "CamA", "nvr": "192.168.1.10", "channel": "D1"}
        row_b = {"name": "CamB", "nvr": "192.168.1.20", "channel": "D2"}
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        s_a = core.Segment(uri="u1", name="seg_a", start=t0, end=t1, size=1000)
        s_b = core.Segment(uri="u2", name="seg_b", start=t0, end=t1, size=1000)
        plan_a = core.CameraPlan(row=row_a, key=core.camera_key(row_a), track_id=101, segments=[s_a])
        plan_b = core.CameraPlan(row=row_b, key=core.camera_key(row_b), track_id=102, segments=[s_b])

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        def forbidden_search(*a, **kw):
            raise AssertionError("Search pool should not be invoked for prefetched plans")

        core.plan_camera = forbidden_search

        stages_recorded = collections.defaultdict(list)

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row=None, **info):
                stages_recorded[key].append(stage)

        cb = MockCallbacks()

        def fake_download(self, seg, dest, **kw):
            with open(dest, "wb") as f:
                f.write(b"data")

        core.NvrClient.download = fake_download

        prefetched = {plan_a.key: plan_a, plan_b.key: plan_b}
        results = core.run_batch([row_a, row_b], args, t0, t1, callbacks=cb, prefetched_plans=prefetched)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertNotIn("searching", stages_recorded[plan_a.key])
        self.assertNotIn("searching", stages_recorded[plan_b.key])
        self.assertIn("queued", stages_recorded[plan_a.key])
        self.assertIn("queued", stages_recorded[plan_b.key])

    def test_error_isolation_during_streaming_search(self):
        """Camera failing search records failure immediately and does not prevent others from downloading."""
        row_err = {"name": "CamErr", "nvr": "192.168.1.10", "channel": "D1"}
        row_ok = {"name": "CamOk", "nvr": "192.168.1.20", "channel": "D2"}
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        s_ok = core.Segment(uri="u2", name="seg_ok", start=t0, end=t1, size=1000)
        plan_err = core.CameraPlan(row=row_err, key=core.camera_key(row_err), track_id=101, segments=[])
        plan_err.error = "no recording on NVR for track 101"
        plan_ok = core.CameraPlan(row=row_ok, key=core.camera_key(row_ok), track_id=102, segments=[s_ok])

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        def fake_plan_camera(r, client, start, end, mode, **kw):
            if r["name"] == "CamErr":
                return plan_err
            return plan_ok

        core.plan_camera = fake_plan_camera

        def fake_download(self, seg, dest, **kw):
            with open(dest, "wb") as f:
                f.write(b"data")

        core.NvrClient.download = fake_download

        results = core.run_batch([row_err, row_ok], args, t0, t1)

        self.assertEqual(len(results), 2)
        res_err = next(r for r in results if r["name"] == "CamErr")
        res_ok = next(r for r in results if r["name"] == "CamOk")
        self.assertFalse(res_err["ok"])
        self.assertIn("no recording on NVR for track 101", res_err["errors"])
        self.assertTrue(res_ok["ok"])
        self.assertEqual(len(res_ok["files"]), 1)

    def test_prefetched_plans_with_error(self):
        """Camera with error in prefetched_plans fails immediately without downloading."""
        row_err = {"name": "CamErr", "nvr": "192.168.1.10", "channel": "D1"}
        row_ok = {"name": "CamOk", "nvr": "192.168.1.20", "channel": "D2"}
        t0 = datetime(2026, 9, 10, 10, 0, 0)
        t1 = datetime(2026, 9, 10, 10, 10, 0)
        s_ok = core.Segment(uri="u2", name="seg_ok", start=t0, end=t1, size=1000)
        plan_err = core.CameraPlan(row=row_err, key=core.camera_key(row_err), track_id=101, segments=[])
        plan_err.error = "search timeout"
        plan_ok = core.CameraPlan(row=row_ok, key=core.camera_key(row_ok), track_id=102, segments=[s_ok])

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        def forbidden_search(*a, **kw):
            raise AssertionError("plan_camera should not be called")

        core.plan_camera = forbidden_search

        def fake_download(self, seg, dest, **kw):
            with open(dest, "wb") as f:
                f.write(b"data")

        core.NvrClient.download = fake_download

        stages_recorded = collections.defaultdict(list)

        class MockCallbacks(core.Callbacks):
            def stage(self, key, stage, row=None, **info):
                stages_recorded[key].append(stage)

        cb = MockCallbacks()

        prefetched = {plan_err.key: plan_err, plan_ok.key: plan_ok}
        results = core.run_batch([row_err, row_ok], args, t0, t1, callbacks=cb, prefetched_plans=prefetched)

        self.assertEqual(len(results), 2)
        res_err = next(r for r in results if r["name"] == "CamErr")
        res_ok = next(r for r in results if r["name"] == "CamOk")
        self.assertFalse(res_err["ok"])
        self.assertIn("search timeout", res_err["errors"])
        self.assertTrue(res_ok["ok"])
        self.assertIn("fail", stages_recorded[plan_err.key])
        self.assertIn("queued", stages_recorded[plan_ok.key])


class TestWindowPrefetching(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="cctv_prefetch_")
        self._orig_plan_camera = core.plan_camera
        self._orig_mark_existing = core.mark_existing
        self._orig_download = core.NvrClient.download
        self._orig_remux = core.remux_to_mp4

    def tearDown(self):
        core.plan_camera = self._orig_plan_camera
        core.mark_existing = self._orig_mark_existing
        core.NvrClient.download = self._orig_download
        core.remux_to_mp4 = self._orig_remux
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_window_prefetcher_concurrent_execution(self):
        """Verifies that prefetch search for Window 2 executes concurrently while Window 1 is running."""
        rows = [
            {"name": "Cam1", "nvr": "192.168.1.10", "channel": "D1"},
            {"name": "Cam2", "nvr": "192.168.1.20", "channel": "D2"},
        ]
        w1 = (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 11, 0, 0))
        w2 = (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 11, 11, 0, 0))

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        w1_download_started = threading.Event()
        w2_search_completed = threading.Event()
        timeline = {}
        lock = threading.Lock()

        def fake_plan_camera(r, client, start, end, mode, **kw):
            if start == w1[0]:
                with lock:
                    timeline[f"{r['name']}_w1_search"] = systime.monotonic()
            elif start == w2[0]:
                w1_download_started.wait(timeout=1.0)
                with lock:
                    timeline[f"{r['name']}_w2_search_start"] = systime.monotonic()
            seg = core.Segment(uri="u1", name=f"{r['name']}_{start.day}", start=start, end=end, size=1000)
            plan = core.CameraPlan(row=r, key=core.camera_key(r), track_id=101, segments=[seg])
            if start == w2[0]:
                with lock:
                    timeline[f"{r['name']}_w2_search_end"] = systime.monotonic()
                    if "Cam1_w2_search_end" in timeline and "Cam2_w2_search_end" in timeline:
                        w2_search_completed.set()
            return plan

        core.plan_camera = fake_plan_camera

        def fake_download(self, seg, dest, **kw):
            if "10" in seg.name:  # W1 segment
                with lock:
                    timeline[f"{seg.name}_dl_start"] = systime.monotonic()
                w1_download_started.set()
                # Wait until W2 search has finished to prove concurrency
                w2_search_completed.wait(timeout=1.0)
                with lock:
                    timeline[f"{seg.name}_dl_end"] = systime.monotonic()
            with open(dest, "wb") as f:
                f.write(b"data")

        core.NvrClient.download = fake_download

        results = core.run_windows(rows, args, [w1, w2])

        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertTrue(w2_search_completed.is_set())
        # Assert that W2 search started after W1 download started, and finished before W1 download ended
        w1_first_dl_start = min(timeline["Cam1_10_dl_start"], timeline["Cam2_10_dl_start"])
        w1_last_dl_end = max(timeline["Cam1_10_dl_end"], timeline["Cam2_10_dl_end"])
        self.assertLess(w1_first_dl_start, timeline["Cam1_w2_search_start"])
        self.assertLess(timeline["Cam1_w2_search_end"], w1_last_dl_end)
        self.assertLess(timeline["Cam2_w2_search_end"], w1_last_dl_end)

    def test_zero_search_handoff_between_windows(self):
        """Verifies Window 2 starts with pre-fetched plans without synchronous search delays."""
        rows = [
            {"name": "Cam1", "nvr": "192.168.1.10", "channel": "D1"},
            {"name": "Cam2", "nvr": "192.168.1.20", "channel": "D2"},
        ]
        w1 = (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 11, 0, 0))
        w2 = (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 11, 11, 0, 0))

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        active_window = None
        searches_during_window = collections.defaultdict(int)
        lock = threading.Lock()

        def fake_plan_camera(r, client, start, end, mode, **kw):
            with lock:
                searches_during_window[active_window] += 1
            seg = core.Segment(uri="u1", name=f"{r['name']}_{start.day}", start=start, end=end, size=1000)
            return core.CameraPlan(row=r, key=core.camera_key(r), track_id=101, segments=[seg])

        core.plan_camera = fake_plan_camera

        def fake_download(self, seg, dest, **kw):
            with open(dest, "wb") as f:
                f.write(b"data")

        core.NvrClient.download = fake_download

        orig_run_batch = core.run_batch

        def spy_run_batch(*b_args, **b_kwargs):
            nonlocal active_window
            start = b_args[2] if len(b_args) > 2 else b_kwargs.get("start")
            active_window = start
            return orig_run_batch(*b_args, **b_kwargs)

        with patch.object(core, "run_batch", side_effect=spy_run_batch):
            results = core.run_windows(rows, args, [w1, w2])

        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["ok"] for r in results))
        # During W1's run_batch: W1 cameras searched, plus W2 cameras pre-fetched in background
        # During W2's run_batch: exactly 0 searches because all plans were pre-fetched!
        self.assertEqual(searches_during_window[w2[0]], 0)

    def test_window_prefetcher_clean_cancellation(self):
        """Verifies setting cancel_event halts prefetcher cleanly without thread leaks."""
        rows = [
            {"name": f"Cam{i}", "nvr": f"192.168.1.{10+i}", "channel": "D1"}
            for i in range(4)
        ]
        start = datetime(2026, 9, 11, 10, 0, 0)
        end = datetime(2026, 9, 11, 11, 0, 0)

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None

        cancel_ev = threading.Event()
        search_started = threading.Event()

        def hanging_plan_camera(r, client, s, e, mode, cancel_event=None, **kw):
            search_started.set()
            while cancel_event and not cancel_event.is_set():
                systime.sleep(0.01)
            raise core.Cancelled("cancelled")

        core.plan_camera = hanging_plan_camera

        initial_threads = threading.active_count()
        clients = {}
        prefetcher = core.WindowPrefetcher(rows, clients, args, cancel_event=cancel_ev, max_workers=4)

        try:
            prefetcher.start_prefetch(start, end)
            self.assertTrue(search_started.wait(timeout=1.0))
            cancel_ev.set()
            plans = prefetcher.get_prefetched(timeout=1.0)
            self.assertIsNotNone(plans)
            prefetcher.cancel()
        finally:
            cancel_ev.set()
            prefetcher.cancel()

        systime.sleep(0.1)
        final_threads = threading.active_count()
        self.assertLessEqual(final_threads, initial_threads + 1)

    def test_multi_window_results_match_expected(self):
        """Verify results across multiple windows match expected data structures and file paths."""
        rows = [
            {"name": "CamA", "nvr": "192.168.1.10", "channel": "D1"},
            {"name": "CamB", "nvr": "192.168.1.20", "channel": "D2"},
        ]
        windows = [
            (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 11, 0, 0)),
            (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 11, 11, 0, 0)),
            (datetime(2026, 9, 12, 10, 0, 0), datetime(2026, 9, 12, 11, 0, 0)),
        ]

        class DummyArgs:
            workers = 2
            per_nvr = 1
            remux_workers = 2
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None

        def fake_remux(src, dst, proc_reg, cancel_ev):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(b"mp4data")
            return True, ""

        core.remux_to_mp4 = fake_remux

        def fake_plan_camera(r, client, start, end, mode, **kw):
            seg = core.Segment(uri=f"u_{r['name']}_{start.day}", name=f"{r['name']}_{start.day}",
                               start=start, end=end, size=1000)
            return core.CameraPlan(row=r, key=core.camera_key(r), track_id=101, segments=[seg])

        core.plan_camera = fake_plan_camera

        def fake_download(self, seg, dest, **kw):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(b"data")

        core.NvrClient.download = fake_download

        results = core.run_windows(rows, args, windows)

        self.assertEqual(len(results), 6)
        self.assertTrue(all(r["ok"] for r in results))
        for r in results:
            self.assertEqual(len(r["files"]), 1)
            self.assertTrue(os.path.exists(r["files"][0]))
            self.assertEqual(r["errors"], [])

    def test_window_prefetcher_reuses_persistent_clients(self):
        """Verifies persistent NvrClient instances are reused across all windows and prefetcher."""
        rows = [{"name": "Cam1", "nvr": "192.168.1.10", "channel": "D1"}]
        windows = [
            (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 11, 0, 0)),
            (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 11, 11, 0, 0)),
        ]

        class DummyArgs:
            workers = 1
            per_nvr = 1
            remux_workers = 1
            trim = False
            output_dir = self.test_dir
            mode = "clip"
            timeout = 10
            user = "admin"
            password = "pwd"

        args = DummyArgs()
        core.mark_existing = lambda p, args, start, end: None
        core.remux_to_mp4 = lambda src, dst, proc_reg, cancel_ev: (True, "")

        clients_seen = []

        def fake_plan_camera(r, client, start, end, mode, **kw):
            clients_seen.append(client)
            seg = core.Segment(uri="u1", name=f"seg_{start.day}", start=start, end=end, size=1000)
            return core.CameraPlan(row=r, key=core.camera_key(r), track_id=101, segments=[seg])

        core.plan_camera = fake_plan_camera

        def fake_download(self, seg, dest, **kw):
            with open(dest, "wb") as f:
                f.write(b"data")

        core.NvrClient.download = fake_download

        results = core.run_windows(rows, args, windows)

        self.assertEqual(len(results), 2)
        self.assertEqual(len(clients_seen), 2)
        self.assertIs(clients_seen[0], clients_seen[1])

    def test_window_prefetcher_standalone_methods(self):
        """Verifies get_prefetched returns None when not started, and handles empty rows."""
        class DummyArgs:
            workers = 1
            user = "admin"
            password = "pwd"
            timeout = 10

        args = DummyArgs()
        prefetcher = core.WindowPrefetcher([], {}, args)
        self.assertIsNone(prefetcher.get_prefetched())
        prefetcher.start_prefetch(datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 11, 0, 0))
        plans = prefetcher.get_prefetched()
        self.assertEqual(plans, {})
        prefetcher.cancel()


class TestWebGuiPipelinedExecution(unittest.TestCase):
    def setUp(self):
        import webgui
        webgui.CANCEL_EVENT.clear()
        self.orig_webgui_state = dict(webgui.STATE)
        self.orig_run_batch = core.run_batch
        self.orig_prefetcher = core.WindowPrefetcher
        self.orig_write_log = core.write_run_log

    def tearDown(self):
        import webgui
        webgui.CANCEL_EVENT.clear()
        webgui.STATE.clear()
        webgui.STATE.update(self.orig_webgui_state)
        core.run_batch = self.orig_run_batch
        core.WindowPrefetcher = self.orig_prefetcher
        core.write_run_log = self.orig_write_log

    def test_plan_run_remux_workers_parsing(self):
        import webgui
        payload = {
            "csv": "camera_n_nvr.csv",
            "mode": "both",
            "workers": "4",
            "per_nvr": "2",
            "remux_workers": "3",
            "timeout": "30",
            "time_mode": "single",
            "start": "2026-09-10 10:00:00",
            "end": "2026-09-10 11:00:00",
            "trim": "false",
        }
        plan = webgui.plan_run(payload)
        self.assertEqual(plan["args"].remux_workers, 3)
        self.assertEqual(plan["ui"]["remux_workers"], 3)

    def test_webgui_do_run_prefetch_integration(self):
        import webgui
        w1 = (datetime(2026, 9, 10, 10, 0, 0), datetime(2026, 9, 10, 22, 0, 0))
        w2 = (datetime(2026, 9, 11, 10, 0, 0), datetime(2026, 9, 11, 22, 0, 0))
        plan = {
            "windows": [w1, w2],
            "rows": [{"name": "Gate", "nvr": "192.168.1.10", "channel": "D1"}],
            "args": None,
        }
        prefetches_started = []

        class MockPrefetcher:
            def __init__(self, rows, clients, args, cancel_event=None):
                self.rows = rows
                self.clients = clients
                self.args = args
                self.cancelled = False

            def start_prefetch(self, start, end):
                prefetches_started.append((start, end))

            def get_prefetched(self, timeout=None):
                seg = core.Segment(uri="u1", name="s1", start=w2[0], end=w2[1], size=500)
                plan_cam = core.CameraPlan(row=plan["rows"][0], key="192.168.1.10/D1", segments=[seg])
                return {"192.168.1.10/D1": plan_cam}

            def cancel(self):
                self.cancelled = True

        batches_run = []
        def mock_run_batch(rows, args, start, end, **kw):
            batches_run.append((start, end, kw.get("prefetched_plans")))
            return [{"name": "Gate", "nvr": "192.168.1.10", "channel": "D1", "ok": True, "errors": [], "notes": [], "files": []}]

        core.run_batch = mock_run_batch
        core.WindowPrefetcher = MockPrefetcher
        core.write_run_log = lambda r, p: None

        webgui.do_run(plan)
        self.assertEqual(len(batches_run), 2)
        self.assertEqual(len(prefetches_started), 1)
        self.assertEqual(prefetches_started[0], w2)
        self.assertIsNotNone(batches_run[1][2])
        self.assertIn("192.168.1.10/D1", batches_run[1][2])

    def test_webgui_cancel_stops_prefetcher(self):
        import webgui
        class MockPrefetcher:
            def __init__(self):
                self.cancelled = False
            def cancel(self):
                self.cancelled = True

        mock_pref = MockPrefetcher()
        webgui.ACTIVE_PREFETCHER = mock_pref
        with webgui.LOCK:
            webgui.STATE["running"] = True
            webgui.STATE["cancelling"] = False

        ok, err = webgui._cancel()
        self.assertTrue(ok)
        self.assertTrue(mock_pref.cancelled)
        self.assertTrue(webgui.STATE["cancelling"])


if __name__ == "__main__":
    unittest.main()



