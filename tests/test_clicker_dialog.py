from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from app.recorder.core.clicker import ACTION_MARK, ACTION_PAUSE, ClickerConfig, RawEvent
from app.recorder.core.input_devices import InputDevice
from app.recorder.ui.clicker_dialog import ClickerDialog

_app = QApplication.instance() or QApplication([])

T01 = InputDevice("05ac:022c:8&243a709b&0", "T01", True, frozenset({"hid"}), frozenset({(0x0C, 0x01), (0x0D, 0x04)}))
PEN = InputDevice("045e:0c0f:aabbccddeeff", "Test Pen", True, frozenset({"keyboard", "hid"}), frozenset({(0x01, 0x06), (0x0C, 0x01)}))
KEYBOARD = InputDevice("046d:c52b:8&2f5d1a3e&0", "Desk keyboard", False, frozenset({"keyboard"}), frozenset({(0x01, 0x06)}))


class FakeListener(QObject):
    input_event = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.started: list[frozenset] = []

    def start(self, usages) -> bool:
        self.running = True
        self.started.append(frozenset(usages))
        return True

    def stop(self) -> None:
        self.running = False


class MemoryStore:
    def __init__(self, config: ClickerConfig | None = None) -> None:
        self.config = config or ClickerConfig()
        self.saves = 0

    def load(self) -> ClickerConfig:
        return ClickerConfig.from_json(self.config.to_json())

    def save(self, config: ClickerConfig) -> None:
        self.config = ClickerConfig.from_json(config.to_json())
        self.saves += 1


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class DialogCase(unittest.TestCase):
    devices = [T01, PEN]

    def make(self, config: ClickerConfig | None = None, devices=None) -> ClickerDialog:
        self.store = MemoryStore(config)
        self.listener = FakeListener()
        self.clock = Clock()
        self.asked: list[bool] = []
        shown = self.devices if devices is None else devices

        def lister(include_all: bool):
            self.asked.append(include_all)
            return [d for d in shown if d.bluetooth or include_all] + ([KEYBOARD] if include_all else [])

        dialog = ClickerDialog(store=self.store, device_lister=lister, listener=self.listener, clock=self.clock)
        self.addCleanup(dialog.done, 0)
        return dialog

    def choose(self, dialog: ClickerDialog, name: str) -> None:
        for row in range(dialog.device_list.count()):
            if dialog.device_list.item(row).text().startswith(name):
                dialog.device_list.setCurrentRow(row)
                return
        raise AssertionError(f"{name} is not in the list")

    def press(self, dialog: ClickerDialog, event: RawEvent, advance: float = 1.0) -> None:
        self.clock.now += advance
        self.listener.input_event.emit(event)

    def key_down(self, device: InputDevice, code: str) -> RawEvent:
        return RawEvent(device.key, "key", code, True)


class DeviceListTests(DialogCase):
    def test_bluetooth_devices_are_listed_and_nothing_is_chosen_yet(self) -> None:
        dialog = self.make()
        self.assertEqual([dialog.device_list.item(i).text() for i in range(dialog.device_list.count())], ["T01", "Test Pen"])
        self.assertEqual(self.asked, [False])
        self.assertFalse(dialog._set_buttons[ACTION_PAUSE].isEnabled(), "buttons can't be set before a device is chosen")

    def test_show_all_adds_the_other_input_devices_and_says_which_are_not_bluetooth(self) -> None:
        dialog = self.make()
        dialog.show_all_checkbox.setChecked(True)
        texts = [dialog.device_list.item(i).text() for i in range(dialog.device_list.count())]
        self.assertIn("Desk keyboard   (not Bluetooth)", texts)
        self.assertEqual(self.asked[-1], True)

    def test_no_devices_says_what_to_do(self) -> None:
        dialog = self.make(devices=[])
        self.assertIn("Pair it in Windows Bluetooth settings", dialog.status_label.text())
        self.assertEqual(dialog.device_list.count(), 0)

    def test_choosing_a_device_remembers_it_and_starts_listening_to_it(self) -> None:
        dialog = self.make()
        self.choose(dialog, "T01")
        self.assertEqual((self.store.config.device_key, self.store.config.device_name), (T01.key, "T01"))
        self.assertEqual(self.listener.started[-1], T01.usages)  # its own collections: media control and touch
        self.assertTrue(dialog._set_buttons[ACTION_PAUSE].isEnabled())
        self.assertIn("Selected T01", dialog.status_label.text())

    def test_the_saved_device_stays_selected_even_when_it_is_switched_off(self) -> None:
        config = ClickerConfig()
        config.select_device("dead:beef:gone", "Old clicker")
        config.bind(ACTION_PAUSE, "key:22")
        dialog = self.make(config)
        self.assertEqual(dialog.device_list.currentItem().text(), "Old clicker   (not connected)")
        self.assertEqual(dialog._value_labels[ACTION_PAUSE].text(), "Key: Page Down")  # its buttons are still shown
        self.assertEqual(self.listener.started[-1], frozenset({(0x01, 0x06), (0x0C, 0x01)}))  # listens for the usual kinds

    def test_refreshing_does_not_re_register_with_windows_unless_something_changed(self) -> None:
        dialog = self.make()
        self.choose(dialog, "Test Pen")
        starts = len(self.listener.started)
        for _ in range(5):
            dialog.refresh_devices()  # the periodic refresh
        self.assertEqual(len(self.listener.started), starts)
        self.assertEqual(dialog.device_list.currentItem().text(), "Test Pen")  # and the choice survives


class LearningTests(DialogCase):
    def setUp(self) -> None:
        self.dialog = self.make()
        self.choose(self.dialog, "Test Pen")

    def test_pressing_set_waits_for_a_button_and_locks_the_rest(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        self.assertIn("Press the button on Test Pen that should pause and resume the recording", d.status_label.text())
        self.assertEqual(d._set_buttons[ACTION_PAUSE].text(), "Cancel")
        self.assertFalse(d._set_buttons[ACTION_MARK].isEnabled())
        self.assertFalse(d.device_list.isEnabled())

    def test_the_next_press_from_the_chosen_device_becomes_the_button(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        self.press(d, self.key_down(PEN, "22"))
        self.assertEqual(d._value_labels[ACTION_PAUSE].text(), "Key: Page Down")
        self.assertEqual(self.store.config.selected_bindings(), {ACTION_PAUSE: "key:22"})
        self.assertIn("Record / Pause is now: Key: Page Down", d.status_label.text())
        self.assertEqual(d._set_buttons[ACTION_PAUSE].text(), "Set…")  # learning is over, everything is unlocked again
        self.assertTrue(d.device_list.isEnabled())

    def test_presses_from_other_devices_are_ignored_while_learning(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        self.press(d, self.key_down(KEYBOARD, "22"))  # the ordinary keyboard
        self.press(d, RawEvent(None, "key", "22", True))
        self.assertEqual(self.store.config.selected_bindings(), {})
        self.assertIn("Press the button", d.status_label.text())

    def test_a_release_is_not_a_press(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        self.press(d, RawEvent(PEN.key, "key", "22", False))
        self.assertEqual(self.store.config.selected_bindings(), {})

    def test_a_button_can_only_do_one_thing(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        self.press(d, self.key_down(PEN, "22"))
        d._set_buttons[ACTION_MARK].click()
        self.press(d, RawEvent(PEN.key, "key", "22", False), advance=0.1)
        self.press(d, self.key_down(PEN, "22"))
        self.assertIn("already used for Record / Pause", d.status_label.text())
        self.assertEqual(self.store.config.selected_bindings(), {ACTION_PAUSE: "key:22"})
        self.assertEqual(d._value_labels[ACTION_MARK].text(), "Not set")

    def test_a_remote_button_takes_two_presses_and_says_so(self) -> None:
        d = self.dialog
        self.choose(d, "T01")
        d._set_buttons[ACTION_MARK].click()
        self.press(d, RawEvent(T01.key, "hid", "010500", None))
        self.assertIn("Press the same button once more", d.status_label.text())
        self.assertEqual(self.store.config.selected_bindings(), {})
        self.press(d, RawEvent(T01.key, "hid", "010900", None))
        self.assertEqual(self.store.config.selected_bindings(), {ACTION_MARK: "hid:01??00"})
        self.assertEqual(d._value_labels[ACTION_MARK].text(), "Remote button (01 ?? 00)")

    def test_cancel_leaves_things_as_they_were(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        d._set_buttons[ACTION_PAUSE].click()  # the button now says Cancel
        self.assertEqual(d.status_label.text(), "Cancelled.")
        self.assertEqual(d._set_buttons[ACTION_PAUSE].text(), "Set…")
        self.assertEqual(self.store.config.selected_bindings(), {})

    def test_giving_up_waiting_explains_what_to_check(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        d._learn_timed_out()
        self.assertIn("No button press came from Test Pen", d.status_label.text())
        self.assertIsNone(d._learning)

    def test_clearing_removes_the_button(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        self.press(d, self.key_down(PEN, "22"))
        self.assertTrue(d._clear_buttons[ACTION_PAUSE].isEnabled())
        d._clear_buttons[ACTION_PAUSE].click()
        self.assertEqual(d._value_labels[ACTION_PAUSE].text(), "Not set")
        self.assertEqual(self.store.config.selected_bindings(), {})
        self.assertFalse(d._clear_buttons[ACTION_PAUSE].isEnabled())

    def test_each_device_keeps_its_own_setup_when_switching(self) -> None:
        d = self.dialog
        d._set_buttons[ACTION_PAUSE].click()
        self.press(d, self.key_down(PEN, "22"))
        self.choose(d, "T01")
        self.assertEqual(d._value_labels[ACTION_PAUSE].text(), "Not set")
        self.choose(d, "Test Pen")
        self.assertEqual(d._value_labels[ACTION_PAUSE].text(), "Key: Page Down")


class TestingAButtonTests(DialogCase):
    def test_pressing_a_button_shows_what_it_is_and_what_it_does(self) -> None:
        config = ClickerConfig()
        config.select_device(PEN.key, "Test Pen")
        config.bind(ACTION_PAUSE, "key:22")
        d = self.make(config)
        self.press(d, self.key_down(PEN, "21"))
        self.assertEqual(d.last_seen_label.text(), "Last button seen from Test Pen: Key: Page Up")
        self.press(d, RawEvent(PEN.key, "key", "21", False))
        self.press(d, self.key_down(PEN, "22"))
        self.assertEqual(d.last_seen_label.text(), "Last button seen from Test Pen: Key: Page Down  →  Record / Pause")

    def test_other_devices_never_show_up_there(self) -> None:
        config = ClickerConfig()
        config.select_device(PEN.key, "Test Pen")
        d = self.make(config)
        self.press(d, self.key_down(KEYBOARD, "41"))
        self.assertEqual(d.last_seen_label.text(), "")


class KeyGuardTests(DialogCase):
    def setUp(self) -> None:
        self.dialog = self.make()
        self.choose(self.dialog, "Test Pen")
        self.clicks = 0
        self.dialog.close_button.clicked.connect(lambda: setattr(self, "clicks", self.clicks + 1))

    def space(self, button) -> None:
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(button, QKeyEvent(kind, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " "))

    def test_keys_from_the_clicker_cannot_press_this_windows_buttons_while_learning(self) -> None:
        self.dialog._set_buttons[ACTION_PAUSE].click()
        self.space(self.dialog.close_button)
        self.assertEqual(self.clicks, 0)

    def test_and_just_after_a_clicker_key_arrives(self) -> None:
        self.press(self.dialog, self.key_down(PEN, "20"), advance=0.0)  # the raw event that comes with the key
        self.space(self.dialog.close_button)
        self.assertEqual(self.clicks, 0)
        self.clock.now += 1.0  # ...but the ordinary keyboard works again a moment later
        self.space(self.dialog.close_button)
        self.assertEqual(self.clicks, 1)

    def test_ordinary_keyboard_use_is_untouched_when_not_learning(self) -> None:
        self.space(self.dialog.close_button)
        self.assertEqual(self.clicks, 1)


class ClosingTests(DialogCase):
    def test_closing_stops_listening_and_stops_swallowing_keys(self) -> None:
        d = self.make()
        self.choose(d, "T01")
        d._set_buttons[ACTION_PAUSE].click()
        self.assertTrue(self.listener.running)
        d.done(0)
        self.assertFalse(self.listener.running)
        self.assertFalse(d._swallow_keys)
        self.assertFalse(d._refresh_timer.isActive())
        self.assertFalse(d._learn_timeout.isActive())


if __name__ == "__main__":
    unittest.main()
