from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.recorder.core import ffmpeg_record
from app.recorder.core.ffmpeg_record import CameraCaptureConfig, RecordingController
from app.recorder.ui.setup_window import RecorderMainWindow

_app = QApplication.instance() or QApplication([])

CAMERA = CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720))


class _FakeController:
    """Just the preview-feed side of RecordingController, recording what the window asks of it."""

    def __init__(self, state: str | None) -> None:
        self.state = state
        self.previewing = False
        self.calls: list[str] = []

    def start_preview_feed(self, source) -> None:
        self.calls.append("start")
        self.previewing = True

    def stop_preview_feed(self) -> None:
        self.calls.append("stop")
        self.previewing = False


def _window(*, state: str | None = "paused", source=CAMERA, visible: bool = True):
    """A stand-in for RecorderMainWindow carrying just what the paused-preview decision looks at."""
    fake = SimpleNamespace(
        _controller=_FakeController(state),
        _camera_preview=SimpleNamespace(isVisible=lambda: visible, set_paused=lambda paused: None),
        _camera_source_with_preview=lambda: source,
    )
    fake._sync_paused_preview_feed = lambda: RecorderMainWindow._sync_paused_preview_feed(fake)
    return fake


class PausedPreviewDecisionTests(unittest.TestCase):
    def test_pausing_starts_the_live_picture(self) -> None:
        fake = _window()
        fake._sync_paused_preview_feed()
        self.assertEqual(fake._controller.calls, ["start"])
        self.assertTrue(fake._controller.previewing)

    def test_it_is_not_restarted_while_it_is_already_live(self) -> None:
        # The resume 3-2-1 syncs again: the picture must carry on, not blink while the camera is reopened.
        fake = _window()
        fake._sync_paused_preview_feed()
        fake._sync_paused_preview_feed()
        self.assertEqual(fake._controller.calls, ["start"])

    def test_no_live_picture_while_recording_or_with_no_recording(self) -> None:
        for state in ("recording", None):
            fake = _window(state=state)
            fake._sync_paused_preview_feed()
            self.assertNotIn("start", fake._controller.calls, state)
            self.assertFalse(fake._controller.previewing, state)

    def test_no_live_picture_when_the_picture_is_hidden(self) -> None:
        fake = _window(visible=False)
        fake._sync_paused_preview_feed()
        self.assertNotIn("start", fake._controller.calls)

    def test_no_live_picture_for_a_recording_with_no_camera_preview(self) -> None:
        # A screen recording, or a camera one with no preview size: _camera_source_with_preview finds nothing.
        fake = _window(source=None)
        fake._sync_paused_preview_feed()
        self.assertNotIn("start", fake._controller.calls)

    def test_no_live_picture_once_the_controls_have_no_picture_window(self) -> None:
        fake = _window()
        fake._camera_preview = None
        fake._sync_paused_preview_feed()
        self.assertNotIn("start", fake._controller.calls)

    def test_hiding_the_picture_while_paused_lets_the_camera_go_and_showing_it_takes_it_again(self) -> None:
        shown = [True]
        fake = _window()
        fake._camera_preview.isVisible = lambda: shown[0]
        fake._sync_paused_preview_feed()
        self.assertTrue(fake._controller.previewing)

        shown[0] = False
        fake._sync_paused_preview_feed()
        self.assertFalse(fake._controller.previewing)

        shown[0] = True
        fake._sync_paused_preview_feed()
        self.assertTrue(fake._controller.previewing)

    def test_resuming_hands_the_camera_back_to_the_recording(self) -> None:
        fake = _window()
        fake._sync_paused_preview_feed()
        fake._controller.state = "recording"
        fake._sync_paused_preview_feed()
        self.assertFalse(fake._controller.previewing)
        self.assertEqual(fake._controller.calls[-1], "stop")

    def test_a_full_pause_resume_pause_cycle(self) -> None:
        fake = _window(state="recording")
        for state, live in (("paused", True), ("recording", False), ("paused", True)):
            fake._controller.state = state
            fake._sync_paused_preview_feed()
            self.assertEqual(fake._controller.previewing, live, state)


class ResumeCountdownTests(unittest.TestCase):
    """The 3-2-1 before a resume goes on with the picture that was already live."""

    def test_it_leaves_the_live_picture_running_and_drops_the_paused_banner(self) -> None:
        paused_calls: list[bool] = []
        fake = _window()
        fake._camera_preview.set_paused = paused_calls.append
        fake._sync_paused_preview_feed()  # the pause
        fake._controller.calls.clear()
        fake._floating_overlay = SimpleNamespace(set_resuming=lambda: None)
        fake._resume_countdown = None
        fake._resume_pressed = False
        fake._sync_pause_tint = lambda: None
        fake._place_countdown = lambda countdown, source: None
        fake._finish_resume = lambda: None
        fake.source_combo = SimpleNamespace(currentData=lambda: None)

        with patch("app.recorder.ui.setup_window.CountdownOverlay"):
            RecorderMainWindow._start_resume_countdown(fake)

        self.assertEqual(fake._controller.calls, [], "the camera must not be closed and reopened for the countdown")
        self.assertTrue(fake._controller.previewing)
        self.assertEqual(paused_calls, [False])
        self.assertTrue(fake._resume_pressed)

    def test_it_starts_the_picture_if_the_pause_could_not(self) -> None:
        fake = _window()  # paused, but the feed never started
        fake._camera_preview.set_paused = lambda paused: None
        fake._floating_overlay = SimpleNamespace(set_resuming=lambda: None)
        fake._resume_countdown = None
        fake._resume_pressed = False
        fake._sync_pause_tint = lambda: None
        fake._place_countdown = lambda countdown, source: None
        fake._finish_resume = lambda: None
        fake.source_combo = SimpleNamespace(currentData=lambda: None)

        with patch("app.recorder.ui.setup_window.CountdownOverlay"):
            RecorderMainWindow._start_resume_countdown(fake)

        self.assertEqual(fake._controller.calls, ["start"])


class ControllerPreviewingTests(unittest.TestCase):
    def test_previewing_follows_the_feed(self) -> None:
        controller = RecordingController()
        self.assertFalse(controller.previewing)
        with patch.object(ffmpeg_record, "PreviewFeed", MagicMock()) as feed_class:
            controller.start_preview_feed(CAMERA)
            self.assertTrue(controller.previewing)
            feed_class.return_value.start.assert_called_once()
            controller.stop_preview_feed()
            feed_class.return_value.stop.assert_called_once()
        self.assertFalse(controller.previewing)

    def test_a_feed_that_cannot_start_is_not_previewing(self) -> None:
        controller = RecordingController()
        failing = MagicMock()
        failing.return_value.start.side_effect = OSError("no ffmpeg")
        with patch.object(ffmpeg_record, "PreviewFeed", failing):
            controller.start_preview_feed(CAMERA)
        self.assertFalse(controller.previewing)


if __name__ == "__main__":
    unittest.main()
