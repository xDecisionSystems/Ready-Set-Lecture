from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.recorder.core import ffmpeg_record
from app.recorder.core.ffmpeg_record import AudioCaptureConfig, RecordConfig, RecordingController, RecordingSession, ScreenCaptureConfig


def config(output: Path, *, allow_gpu: bool = True) -> RecordConfig:
    return RecordConfig(ScreenCaptureConfig((0, 0, 640, 360)), AudioCaptureConfig(None), output, allow_gpu=allow_gpu)


class RecordingIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.output = self.root / "lecture.mp4"
        self.session = RecordingSession(config(self.output), self.work)
        self.segment = self.work / "segment_001.mp4"
        self.segment.write_bytes(b"video")
        self.segment.with_suffix(".log").write_text("ok", encoding="utf-8")
        self.session.segment_paths = [self.segment]

    def test_single_nonempty_but_invalid_segment_is_rejected_and_preserved(self) -> None:
        with patch.object(self.session, "_has_video", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "usable recording"):
                self.session.finalize(self.output)
        self.assertTrue(self.segment.exists())
        self.assertFalse(self.output.exists())

    def test_success_is_validated_published_without_overwrite_and_cleans_work_dir(self) -> None:
        with patch.object(self.session, "_has_video", return_value=True):
            self.assertEqual(self.session.finalize(self.output), self.output)
        self.assertEqual(self.output.read_bytes(), b"video")
        self.assertFalse(self.work.exists())

    def test_file_that_appears_during_recording_is_never_overwritten(self) -> None:
        self.output.write_bytes(b"someone else's file")
        with patch.object(self.session, "_has_video", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "now exists"):
                self.session.finalize(self.output)
        self.assertEqual(self.output.read_bytes(), b"someone else's file")
        self.assertTrue(self.segment.exists())

    def test_forced_stop_is_not_reported_as_success(self) -> None:
        self.session._forced_stop = True
        with patch.object(self.session, "_has_video", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "terminated"):
                self.session.finalize(self.output)
        self.assertTrue(self.segment.exists())

    def test_abort_is_idempotent_and_can_discard_artifacts(self) -> None:
        self.session.abort(preserve_files=False)
        self.session.abort(preserve_files=False)
        self.assertFalse(self.work.exists())

    def test_spawn_failure_closes_and_removes_the_log(self) -> None:
        empty_work = self.root / "spawn"
        session = RecordingSession(config(self.output), empty_work)
        with patch.object(ffmpeg_record, "build_ffmpeg_argv", return_value=["ffmpeg"]), \
                patch.object(ffmpeg_record.subprocess, "Popen", side_effect=OSError("cannot spawn")):
            with self.assertRaisesRegex(OSError, "cannot spawn"):
                session.start_segment()
        self.assertFalse(empty_work.exists())


class RecorderPolicyAndTimingTests(unittest.TestCase):
    def test_session_honors_explicit_gpu_off_policy(self) -> None:
        session = RecordingSession(config(Path("out.mp4"), allow_gpu=False), Path("work"))
        self.assertFalse(session._allow_gpu)

    def test_mark_time_includes_fraction_since_last_timer_tick(self) -> None:
        controller = RecordingController()
        controller._state = "recording"
        controller.session = MagicMock()
        controller._last_tick = 10.0
        controller.elapsed_seconds = 2.0
        with patch.object(ffmpeg_record.time, "monotonic", return_value=10.075):
            self.assertAlmostEqual(controller.mark_time(), 2.075)

    def test_pause_accumulates_time_before_stopping_timer(self) -> None:
        controller = RecordingController()
        controller._state = "recording"
        controller.session = MagicMock()
        controller._last_tick = 20.0
        controller.elapsed_seconds = 3.0
        with patch.object(ffmpeg_record.time, "monotonic", return_value=20.09), patch.object(controller, "_start_worker"):
            controller.pause()
        self.assertAlmostEqual(controller.elapsed_seconds, 3.09)


if __name__ == "__main__":
    unittest.main()
