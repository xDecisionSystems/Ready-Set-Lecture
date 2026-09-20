from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtWidgets import QApplication, QToolTip

from app.recorder.core.audio_meter import AudioLevelMeter
from app.recorder.ui import floating_overlay
from app.recorder.ui.floating_overlay import FloatingOverlay

_app = QApplication.instance() or QApplication([])


def _overlay(option: str) -> FloatingOverlay:
    overlay = FloatingOverlay(AudioLevelMeter())
    if option == "screen":
        overlay.show_cursor_option(False)
    elif option == "camera":
        overlay.show_preview_option(True)
    overlay.move(20, 20)
    overlay.show()
    _app.processEvents()
    return overlay


def _measure(overlay: FloatingOverlay) -> dict[str, tuple[int, int]]:
    _app.processEvents()
    names = ("dot", "elapsed", "pause_button", "marker_button", "stop_button", "preview_button", "pause_tint_button")
    return {"overlay": (overlay.width(), overlay.height()), **{n: (getattr(overlay, n).width(), getattr(overlay, n).height()) for n in names}}


class FloatingOverlayConstantSizeTests(unittest.TestCase):
    def test_nothing_changes_size_through_pause_resume_and_marking(self) -> None:
        for option in ("screen", "camera"):
            overlay = _overlay(option)
            self.addCleanup(overlay.deleteLater)
            baseline = _measure(overlay)
            steps = (
                ("paused", lambda: overlay.set_recording_state("paused")),
                ("resuming", overlay.set_resuming),
                ("recording again", lambda: overlay.set_recording_state("recording")),
                ("marker pressed", overlay.marker_button.click),
                ("resuming while lit", lambda: (overlay.set_resuming(), overlay.marker_button.click())),
                ("timer 07:41", lambda: overlay.set_elapsed(7 * 60 + 41)),
                ("timer 125:00", lambda: overlay.set_elapsed(125 * 60)),
                ("glasses off", lambda: overlay.set_preview_checked(False)),
                ("glasses clicked on", overlay.preview_button.click),
                ("glasses clicked off", overlay.preview_button.click),
                ("red square clicked on", overlay.pause_tint_button.click),
                ("red square clicked off", overlay.pause_tint_button.click),
            )
            for name, step in steps:
                step()
                self.assertEqual(_measure(overlay), baseline, f"{option}: sizes changed at '{name}'")

    def test_the_size_is_fixed_before_the_controls_are_shown_too(self) -> None:
        # _begin_recording positions the overlay from sizeHint() before show(); it must be the size that is then shown.
        overlay = FloatingOverlay(AudioLevelMeter())
        overlay.show_cursor_option(True)
        expected = overlay.sizeHint()
        overlay.show()
        _app.processEvents()
        self.addCleanup(overlay.deleteLater)
        self.assertEqual((overlay.width(), overlay.height()), (expected.width(), expected.height()))

    def test_the_pause_button_has_room_for_its_longest_label(self) -> None:
        overlay = _overlay("screen")
        self.addCleanup(overlay.deleteLater)
        widest = max(overlay.pause_button.fontMetrics().horizontalAdvance(t) for t in ("Pause", "Resume", "Resuming…"))
        self.assertGreaterEqual(overlay.pause_button.width(), widest)
        widths = set()
        for step in (lambda: overlay.set_recording_state("paused"), overlay.set_resuming, lambda: overlay.set_recording_state("recording")):
            step()
            _app.processEvents()
            widths.add(overlay.pause_button.width())
        self.assertEqual(len(widths), 1)

    def test_no_control_takes_keyboard_focus(self) -> None:
        # A clicker's Space/Enter must never press a focused overlay button on top of the action it triggers.
        overlay = _overlay("camera")
        self.addCleanup(overlay.deleteLater)
        for name in ("pause_button", "marker_button", "stop_button", "preview_button", "pause_tint_button", "hide_cursor_checkbox"):
            self.assertEqual(getattr(overlay, name).focusPolicy(), Qt.FocusPolicy.NoFocus, name)

    def test_the_timer_has_room_for_recordings_over_99_minutes(self) -> None:
        overlay = _overlay("screen")
        self.addCleanup(overlay.deleteLater)
        overlay.set_elapsed(125 * 60)
        self.assertEqual(overlay.elapsed.text(), "125:00")
        self.assertGreaterEqual(overlay.elapsed.width(), overlay.elapsed.fontMetrics().horizontalAdvance("125:00"))


class ClapperboardButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.overlay = _overlay("screen")
        self.addCleanup(self.overlay.deleteLater)
        self.button = self.overlay.marker_button

    def test_it_is_an_icon_with_no_text_but_keeps_its_name_for_screen_readers(self) -> None:
        self.assertEqual(self.button.text(), "")
        self.assertEqual(self.button.accessibleName(), "Marker Break")

    def test_pressing_it_reports_a_marker_break_and_lights_it_up(self) -> None:
        pressed = []
        self.overlay.marker_break_clicked.connect(lambda: pressed.append(True))
        self.assertEqual(self.button.glow, 0.0)
        self.button.click()
        self.assertEqual(pressed, [True])
        self.assertGreater(self.button.glow, 0.9)

    def test_the_glow_fades_out_completely_and_can_be_triggered_again(self) -> None:
        self.button.click()
        self.button._fade.setCurrentTime(self.button._fade.duration())
        self.assertEqual(self.button.glow, 0.0)
        self.button.click()
        self.assertGreater(self.button.glow, 0.9)  # a second press lights it again, from full brightness

    def test_the_glow_gets_dimmer_as_time_passes(self) -> None:
        self.button.click()
        seen = []
        for fraction in (0.25, 0.6, 0.9):
            self.button._fade.setCurrentTime(int(self.button._fade.duration() * fraction))
            seen.append(self.button.glow)
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertLess(seen[-1], seen[0])

    def test_painting_while_lit_and_dark_does_not_fail(self) -> None:
        for glow in (0.0, 0.3, 1.0):
            self.button._set_glow(glow)
            self.assertFalse(self.button.grab().isNull())


class GlassesButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.overlay = _overlay("camera")
        self.addCleanup(self.overlay.deleteLater)
        self.button = self.overlay.preview_button
        self.toggled: list[bool] = []
        self.overlay.preview_toggled.connect(self.toggled.append)

    def test_it_is_an_icon_with_no_text_but_keeps_its_name_for_screen_readers(self) -> None:
        self.assertEqual(self.button.text(), "")
        self.assertEqual(self.button.accessibleName(), "Camera preview")
        self.assertTrue(self.button.isCheckable())

    def test_it_starts_in_the_state_the_preview_is_in(self) -> None:
        for shown in (True, False):
            overlay = FloatingOverlay(AudioLevelMeter())
            self.addCleanup(overlay.deleteLater)
            overlay.show_preview_option(shown)
            self.assertEqual(overlay.preview_button.isChecked(), shown)

    def test_clicking_it_switches_the_preview_and_says_so(self) -> None:
        self.assertTrue(self.button.isChecked())
        self.button.click()
        self.button.click()
        self.button.click()
        self.assertEqual(self.toggled, [False, True, False])
        self.assertFalse(self.button.isChecked())

    def test_the_preview_being_dismissed_elsewhere_updates_it_without_announcing(self) -> None:
        # e.g. a double-click on the full-screen picture: the switch follows, and must not re-toggle the preview.
        self.overlay.set_preview_checked(False)
        self.assertFalse(self.button.isChecked())
        self.overlay.set_preview_checked(True)
        self.assertTrue(self.button.isChecked())
        self.assertEqual(self.toggled, [])

    def test_it_only_appears_for_camera_recordings(self) -> None:
        screen = _overlay("screen")
        self.addCleanup(screen.deleteLater)
        self.assertFalse(screen.preview_button.isVisible())
        self.assertTrue(self.button.isVisible())
        self.assertFalse(self.overlay.hide_cursor_checkbox.isVisible())

    def test_on_and_off_look_different(self) -> None:
        def face_colour():
            _app.processEvents()
            colour = self.button.grab().toImage().pixelColor(5, self.button.height() // 2)
            return colour.red(), colour.blue()

        self.overlay.set_preview_checked(True)
        red, blue = face_colour()
        self.assertGreater(blue - red, 100, "the lit switch should be clearly blue")
        self.overlay.set_preview_checked(False)
        red, blue = face_colour()
        self.assertLess(abs(blue - red), 40, "the switched-off button should not be blue")

    def test_the_tooltip_says_what_a_click_will_do(self) -> None:
        self.assertIn("on", self.overlay._tips[self.button])
        self.assertIn("hide", self.overlay._tips[self.button])
        self.button.click()
        self.assertIn("off", self.overlay._tips[self.button])
        self.assertIn("show", self.overlay._tips[self.button])
        self.overlay.set_preview_checked(True)  # changed from outside: the tip follows that too
        self.assertIn("hide", self.overlay._tips[self.button])


class RedSquareButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.overlay = _overlay("screen")
        self.addCleanup(self.overlay.deleteLater)
        self.button = self.overlay.pause_tint_button
        self.toggled: list[bool] = []
        self.overlay.pause_tint_toggled.connect(self.toggled.append)

    def _pixel(self, x: int, y: int) -> tuple[int, int, int]:
        _app.processEvents()
        colour = self.button.grab().toImage().pixelColor(x, y)
        return colour.red(), colour.green(), colour.blue()

    def test_it_is_a_text_free_square_switch_that_starts_off(self) -> None:
        self.assertEqual(self.button.text(), "")
        self.assertTrue(self.button.isCheckable())
        self.assertFalse(self.button.isChecked())
        self.assertEqual(self.button.accessibleName(), "Red screen while paused")

    def test_it_sits_at_the_right_hand_end_of_the_controls(self) -> None:
        others = (self.overlay.grip, self.overlay.dot, self.overlay.elapsed, self.overlay.pause_button, self.overlay.marker_button,
                  self.overlay.stop_button, self.overlay.hide_cursor_checkbox, self.overlay.level_bar)
        self.assertGreater(self.button.x(), max(w.geometry().right() for w in others))

    def test_clicking_it_switches_it_and_says_so(self) -> None:
        self.button.click()
        self.button.click()
        self.button.click()
        self.assertEqual(self.toggled, [True, False, True])
        self.assertTrue(self.button.isChecked())

    def test_setting_it_from_the_remembered_choice_does_not_announce_it(self) -> None:
        self.overlay.set_pause_tint_checked(True)
        self.assertTrue(self.button.isChecked())
        self.overlay.set_pause_tint_checked(False)
        self.assertFalse(self.button.isChecked())
        self.assertEqual(self.toggled, [])

    def test_the_square_is_red_when_on_and_grey_when_off(self) -> None:
        centre = (self.button.width() // 2, self.button.height() // 2)
        red, green, blue = self._pixel(*centre)
        self.assertTrue(abs(red - green) < 30 and abs(green - blue) < 30, f"off should be grey, got {(red, green, blue)}")
        self.overlay.set_pause_tint_checked(True)
        red, green, blue = self._pixel(*centre)
        self.assertTrue(red > 200 and green < 90 and blue < 90, f"on should be red, got {(red, green, blue)}")

    def test_the_button_face_stays_ordinary_when_on(self) -> None:
        # A checked platform button turns accent-blue behind the square; the face must look the same either way.
        edge = (4, self.button.height() // 2)
        off = self._pixel(*edge)
        self.overlay.set_pause_tint_checked(True)
        on = self._pixel(*edge)
        self.assertLess(abs((on[2] - on[0]) - (off[2] - off[0])), 25, f"face changed colour: {off} -> {on}")

    def test_the_tooltip_says_what_a_click_will_do(self) -> None:
        self.assertIn("off", self.overlay._tips[self.button])
        self.assertIn("turn it on", self.overlay._tips[self.button])
        self.button.click()
        self.assertIn("on", self.overlay._tips[self.button])
        self.assertIn("turn it off", self.overlay._tips[self.button])
        self.overlay.set_pause_tint_checked(False)  # changed from outside: the tip follows that too
        self.assertIn("turn it on", self.overlay._tips[self.button])


class ToolTipCaptureTests(unittest.TestCase):
    def test_the_overlays_own_tooltips_are_kept_out_of_the_recording(self) -> None:
        overlay = _overlay("camera")
        self.addCleanup(overlay.deleteLater)
        for widget in (overlay.grip, overlay.marker_button, overlay.preview_button, overlay.pause_tint_button):
            self.assertEqual(widget.toolTip(), "")  # Qt's own tooltip window would be recorded
            excluded: list = []
            with patch.object(floating_overlay, "exclude_from_capture", side_effect=excluded.append):
                QApplication.sendEvent(widget, QHelpEvent(QEvent.Type.ToolTip, QPoint(3, 3), widget.mapToGlobal(QPoint(3, 3))))
            self.assertTrue(excluded, "the tooltip window was not excluded from capture")
            self.assertTrue(all(w.windowType() == Qt.WindowType.ToolTip for w in excluded))
            QToolTip.hideText()


if __name__ == "__main__":
    unittest.main()
