#!/usr/bin/env python3
"""Comprehensive test suite for CCTV footage ingestion system."""

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

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


if __name__ == "__main__":
    unittest.main()
