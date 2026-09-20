from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.recorder.core.ffmpeg_record import CameraCaptureConfig, ScreenCaptureConfig
from app.recorder.ui import screen_wash
from app.recorder.ui.screen_wash import MARK_FLASH_MS, PAUSED_COLOR, MarkFlash, ScreenWash
from app.recorder.ui.setup_window import RecorderMainWindow

_app = QApplication.instance() or QApplication([])


class ScreenWashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wash = ScreenWash(PAUSED_COLOR)
        self.addCleanup(self.wash.hide)

    def test_it_covers_each_screen_completely(self) -> None:
        self.wash.show()
        screens = QGuiApplication.screens()
        self.assertEqual(len(self.wash.windows), len(screens))
        for window, screen in zip(self.wash.windows, screens):
            self.assertTrue(window.isVisible())
            self.assertEqual(window.geometry(), screen.geometry())

    def test_it_makes_one_window_per_screen_however_many_there_are(self) -> None:
        screen = QGuiApplication.primaryScreen()
        with patch.object(screen_wash.QGuiApplication, "screens", return_value=[screen, screen, screen]):
            self.wash.show()
        self.assertEqual(len(self.wash.windows), 3)

    def test_showing_twice_does_not_stack_a_second_wash(self) -> None:
        self.wash.show()
        first = self.wash.windows
        self.wash.show()
        self.assertEqual(self.wash.windows, first)

    def test_hiding_removes_it_and_hiding_again_is_harmless(self) -> None:
        self.wash.show()
        windows = self.wash.windows
        self.wash.hide()
        self.assertFalse(self.wash.visible)
        self.assertTrue(all(not w.isVisible() for w in windows))
        self.wash.hide()
        self.wash.show()  # and it can come back
        self.assertTrue(self.wash.visible)

    def test_clicks_pass_through_it_and_it_never_takes_focus(self) -> None:
        self.wash.show()
        for window in self.wash.windows:
            flags = window.windowFlags()
            self.assertTrue(flags & Qt.WindowType.WindowTransparentForInput)
            self.assertTrue(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
            self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)

    def test_the_paused_wash_is_a_translucent_red_you_can_see_through(self) -> None:
        self.wash.show()
        _app.processEvents()
        colour = self.wash.windows[0].grab().toImage().pixelColor(5, 5)
        self.assertTrue(60 <= colour.alpha() <= 200, f"should be see-through, alpha {colour.alpha()}")
        self.assertTrue(colour.red() > 180 and colour.green() < 60 and colour.blue() < 60, colour.getRgb())

    def test_every_wash_window_is_kept_out_of_the_recording(self) -> None:
        excluded: list = []
        with patch.object(screen_wash, "exclude_from_capture", side_effect=excluded.append):
            self.wash.show()
        self.assertEqual(excluded, self.wash.windows)


class MarkFlashTests(unittest.TestCase):
    def make(self, duration_ms: int = MARK_FLASH_MS) -> MarkFlash:
        flash = MarkFlash(duration_ms=duration_ms)
        self.addCleanup(flash.cancel)
        return flash

    def test_it_lasts_two_seconds(self) -> None:
        self.assertEqual(MARK_FLASH_MS, 2000)
        flash = self.make()  # the default duration
        self.assertEqual(flash._timer.interval(), 2000)

    def test_flashing_covers_every_screen_in_blue(self) -> None:
        flash = self.make()
        flash.flash()
        _app.processEvents()
        self.assertEqual(len(flash.windows), len(QGuiApplication.screens()))
        colour = flash.windows[0].grab().toImage().pixelColor(5, 5)
        self.assertTrue(colour.blue() > 200 and colour.red() < 60 and colour.green() < 130, colour.getRgb())
        self.assertTrue(60 <= colour.alpha() <= 220, f"still see-through: alpha {colour.alpha()}")
        self.assertTrue(flash._timer.isActive())

    def test_it_goes_away_by_itself_when_the_time_is_up(self) -> None:
        flash = self.make()
        flash.flash()
        flash._timer.timeout.emit()
        self.assertFalse(flash.visible)

    def test_it_really_goes_away_after_its_duration(self) -> None:
        flash = self.make(duration_ms=200)
        flash.flash()
        self.assertTrue(flash.visible)
        QTest.qWait(450)
        self.assertFalse(flash.visible)

    def test_marking_again_while_it_shows_starts_the_time_afresh_and_does_not_stack(self) -> None:
        flash = self.make(duration_ms=400)
        flash.flash()
        first = flash.windows
        QTest.qWait(250)
        flash.flash()  # would have ended at 400 ms
        self.assertEqual(flash.windows, first)
        QTest.qWait(250)  # 500 ms after the first, 250 after the second
        self.assertTrue(flash.visible)
        QTest.qWait(300)
        self.assertFalse(flash.visible)

    def test_cancelling_ends_it_at_once(self) -> None:
        flash = self.make()
        flash.flash()
        flash.cancel()
        self.assertFalse(flash.visible)
        self.assertFalse(flash._timer.isActive())
        flash.cancel()  # harmless twice

    def test_its_windows_are_kept_out_of_the_recording_too(self) -> None:
        excluded: list = []
        flash = self.make()
        with patch.object(screen_wash, "exclude_from_capture", side_effect=excluded.append):
            flash.flash()
        self.assertEqual(excluded, flash.windows)


class _FakeWash:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")


def _window(*, overlay: bool = True, square_on: bool = True, state: str | None = "paused", resume_pressed: bool = False):
    """A stand-in for RecorderMainWindow carrying just what the red-screen decision looks at."""
    raised: list[bool] = []
    fake = SimpleNamespace(
        _floating_overlay=SimpleNamespace(pause_tint_button=SimpleNamespace(isChecked=lambda: square_on), raise_=lambda: raised.append(True))
        if overlay else None,
        _controller=SimpleNamespace(state=state),
        _resume_pressed=resume_pressed,
        _paused_tint=_FakeWash(),
        raised=raised,
    )
    fake._sync_pause_tint = lambda: RecorderMainWindow._sync_pause_tint(fake)
    return fake


class RedScreenDecisionTests(unittest.TestCase):
    def last(self, fake) -> str:
        return fake._paused_tint.calls[-1]

    def test_red_only_while_paused_with_the_square_on(self) -> None:
        fake = _window()
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "show")
        self.assertEqual(fake.raised, [True], "the controls must be raised back above the wash")

    def test_never_red_when_the_square_is_off(self) -> None:
        fake = _window(square_on=False)
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "hide")

    def test_never_red_while_recording_or_with_no_recording(self) -> None:
        for state in ("recording", None):
            fake = _window(state=state)
            fake._sync_pause_tint()
            self.assertEqual(self.last(fake), "hide", state)

    def test_never_red_once_the_controls_are_gone(self) -> None:
        fake = _window(overlay=False)
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "hide")

    def test_back_to_normal_as_soon_as_resume_is_pressed(self) -> None:
        # The recording is still paused while the 3-2-1 runs, but the screen must already be normal.
        fake = _window(state="paused", resume_pressed=True)
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "hide")

    def test_a_full_pause_resume_pause_cycle(self) -> None:
        fake = _window(state="recording")
        fake._state_changed_for_tint = lambda state: RecorderMainWindow._state_changed_for_tint(fake, state)

        fake._controller.state = "paused"
        fake._state_changed_for_tint("paused")
        self.assertEqual(self.last(fake), "show")                      # paused: red

        fake._resume_pressed = True                                    # Resume pressed
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "hide")                      # the 3-2-1 runs on a normal screen

        fake._controller.state = "recording"
        fake._state_changed_for_tint("recording")
        self.assertEqual(self.last(fake), "hide")                      # recording again: still normal

        fake._controller.state = "paused"
        fake._state_changed_for_tint("paused")
        self.assertEqual(self.last(fake), "show")                      # a second pause is red again

    def test_switching_the_square_while_paused_takes_effect_at_once(self) -> None:
        fake = _window(square_on=False)
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "hide")
        fake._floating_overlay.pause_tint_button.isChecked = lambda: True
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "show")
        fake._floating_overlay.pause_tint_button.isChecked = lambda: False
        fake._sync_pause_tint()
        self.assertEqual(self.last(fake), "hide")


class MarkFlashDecisionTests(unittest.TestCase):
    """When a mark makes the screen flash (RecorderMainWindow._mark_break)."""

    def window(self, source, overlay: bool = True):
        calls: list[str] = []
        fake = SimpleNamespace(
            _marker_breaks=[],
            _controller=SimpleNamespace(elapsed_seconds=12.5),
            _pending_config=SimpleNamespace(source=source) if source is not None else None,
            _mark_flash=SimpleNamespace(flash=lambda: calls.append("flash")),
            _floating_overlay=SimpleNamespace(raise_=lambda: calls.append("raise")) if overlay else None,
        )
        return fake, calls

    def test_a_camera_recording_flashes_and_keeps_the_controls_on_top(self) -> None:
        fake, calls = self.window(CameraCaptureConfig("Camera", (1280, 720)))
        RecorderMainWindow._mark_break(fake)
        self.assertEqual(fake._marker_breaks, [12.5])
        self.assertEqual(calls, ["flash", "raise"])

    def test_a_screen_recording_never_flashes_but_still_marks(self) -> None:
        fake, calls = self.window(ScreenCaptureConfig((0, 0, 640, 360)))
        RecorderMainWindow._mark_break(fake)
        self.assertEqual(fake._marker_breaks, [12.5])
        self.assertEqual(calls, [])

    def test_no_recording_no_flash(self) -> None:
        fake, calls = self.window(None)
        RecorderMainWindow._mark_break(fake)
        self.assertEqual(calls, [])

    def test_a_camera_flash_without_the_controls_does_not_crash(self) -> None:
        fake, calls = self.window(CameraCaptureConfig("Camera", (1280, 720)), overlay=False)
        RecorderMainWindow._mark_break(fake)
        self.assertEqual(calls, ["flash"])


class ClickerActionTests(unittest.TestCase):
    """What a clicker press does to the recording (RecorderMainWindow._clicker_action)."""

    def window(self, overlay: bool = True):
        calls: list[str] = []
        fake = SimpleNamespace(
            _floating_overlay=SimpleNamespace(marker_button=SimpleNamespace(flash=lambda: calls.append("flash"))) if overlay else None,
            _toggle_pause=lambda: calls.append("toggle"),
            _mark_break=lambda: calls.append("mark"),
        )
        return fake, calls

    def test_record_pause_toggles_pause(self) -> None:
        fake, calls = self.window()
        RecorderMainWindow._clicker_action(fake, "pause")
        self.assertEqual(calls, ["toggle"])

    def test_mark_adds_a_mark_and_lights_the_clapperboard(self) -> None:
        fake, calls = self.window()
        RecorderMainWindow._clicker_action(fake, "mark")
        self.assertEqual(sorted(calls), ["flash", "mark"])

    def test_nothing_happens_without_a_recording_or_for_an_unknown_action(self) -> None:
        fake, calls = self.window(overlay=False)
        RecorderMainWindow._clicker_action(fake, "pause")
        RecorderMainWindow._clicker_action(fake, "mark")
        fake, calls2 = self.window()
        RecorderMainWindow._clicker_action(fake, "explode")
        self.assertEqual((calls, calls2), ([], []))


if __name__ == "__main__":
    unittest.main()
