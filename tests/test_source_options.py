from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.recorder.core.audio_meter import AudioLevelMeter
from app.recorder.core.ffmpeg_record import CameraCaptureConfig, ScreenCaptureConfig
from app.recorder.ui.floating_overlay import FloatingOverlay
from app.recorder.ui.setup_window import RecorderMainWindow

_app = QApplication.instance() or QApplication([])


def _controls_offered(source) -> FloatingOverlay:
    """Run the real 'which controls does this kind of recording get' decision and show the result."""
    overlay = FloatingOverlay(AudioLevelMeter())
    window = SimpleNamespace(_floating_overlay=overlay, _pending_config=SimpleNamespace(source=source), _cursor_option_toggled=lambda hidden: None)
    window._camera_source_with_preview = lambda: RecorderMainWindow._camera_source_with_preview(window)
    window._attach_camera_preview = lambda: overlay.show_preview_option(True)
    RecorderMainWindow._offer_source_options(window)
    overlay.move(20, 20)
    overlay.show()
    _app.processEvents()
    return overlay


class SourceOptionTests(unittest.TestCase):
    def offered(self, source) -> FloatingOverlay:
        overlay = _controls_offered(source)
        self.addCleanup(overlay.deleteLater)
        return overlay

    def test_a_screen_recording_offers_hide_cursor_and_not_the_camera_preview_switch(self) -> None:
        overlay = self.offered(ScreenCaptureConfig((0, 0, 1920, 1080)))
        self.assertTrue(overlay.hide_cursor_checkbox.isVisible())
        self.assertFalse(overlay.preview_button.isVisible())

    def test_a_camera_recording_does_not_offer_hide_cursor(self) -> None:
        overlay = self.offered(CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720)))
        self.assertFalse(overlay.hide_cursor_checkbox.isVisible())
        self.assertTrue(overlay.preview_button.isVisible())

    def test_that_holds_for_every_kind_of_camera_recording(self) -> None:
        for camera in (CameraCaptureConfig("Camera", (3840, 2160), preview_size=(1280, 720), input_format="h264"),
                       CameraCaptureConfig("Camera", (1920, 1080), (100, 100, 800, 600), preview_size=(800, 600)),
                       CameraCaptureConfig("Camera", (1280, 720))):  # even one with no live preview
            overlay = self.offered(camera)
            self.assertFalse(overlay.hide_cursor_checkbox.isVisible(), camera)

    def test_the_hide_cursor_option_starts_in_the_state_the_screen_recording_started_in(self) -> None:
        self.assertTrue(self.offered(ScreenCaptureConfig((0, 0, 640, 360), draw_cursor=False)).hide_cursor_checkbox.isChecked())
        self.assertFalse(self.offered(ScreenCaptureConfig((0, 0, 640, 360), draw_cursor=True)).hide_cursor_checkbox.isChecked())

    def test_a_camera_recording_after_a_screen_one_starts_clean(self) -> None:
        # Each recording gets its own controls, so nothing carries over from the previous one.
        self.offered(ScreenCaptureConfig((0, 0, 1920, 1080), draw_cursor=False))
        overlay = self.offered(CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720)))
        self.assertFalse(overlay.hide_cursor_checkbox.isVisible())


if __name__ == "__main__":
    unittest.main()
