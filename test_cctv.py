#!/usr/bin/env python3
"""Comprehensive test suite for CCTV footage ingestion system."""

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, time

import cctv_retrieve as core


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


if __name__ == "__main__":
    unittest.main()
