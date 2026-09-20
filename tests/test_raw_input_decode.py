from __future__ import annotations

import ctypes
import struct
import unittest

from app.recorder.core.raw_input_win32 import (
    RAWINPUTHEADER, RAWKEYBOARD, RAWMOUSE, RI_KEY_BREAK, RI_KEY_E0, RIM_TYPEHID, RIM_TYPEKEYBOARD, RIM_TYPEMOUSE, Decoded, decode_raw_input,
)


def header(kind: int, body_size: int, handle: int) -> bytes:
    return bytes(RAWINPUTHEADER(kind, ctypes.sizeof(RAWINPUTHEADER) + body_size, handle, 0))


def keyboard(vkey: int, flags: int = 0, handle: int = 0x1234) -> bytes:
    body = bytes(RAWKEYBOARD(0x21, flags, 0, vkey, 0x100, 0))
    return header(RIM_TYPEKEYBOARD, len(body), handle) + body


def mouse(buttons: int, handle: int = 0x1234) -> bytes:
    body = bytes(RAWMOUSE(0, buttons, 0, 0, 0, 0))
    return header(RIM_TYPEMOUSE, len(body), handle) + body


def hid(reports: list[bytes], handle: int = 0x1234) -> bytes:
    size = len(reports[0])
    body = struct.pack("<II", size, len(reports)) + b"".join(reports)
    return header(RIM_TYPEHID, len(body), handle) + body


class KeyboardPacketTests(unittest.TestCase):
    def test_press_and_release(self) -> None:
        self.assertEqual(decode_raw_input(keyboard(0x22)), [Decoded(0x1234, "key", "22", True)])
        self.assertEqual(decode_raw_input(keyboard(0x22, RI_KEY_BREAK)), [Decoded(0x1234, "key", "22", False)])

    def test_extended_keys_are_told_apart(self) -> None:
        self.assertEqual(decode_raw_input(keyboard(0x22, RI_KEY_E0)), [Decoded(0x1234, "key", "22e", True)])

    def test_windows_fake_keys_are_ignored(self) -> None:
        self.assertEqual(decode_raw_input(keyboard(0xFF)), [])
        self.assertEqual(decode_raw_input(keyboard(0x00)), [])

    def test_injected_input_has_no_device(self) -> None:
        self.assertEqual(decode_raw_input(keyboard(0x87, handle=0)), [Decoded(0, "key", "87", True)])


class MousePacketTests(unittest.TestCase):
    def test_button_presses_and_releases(self) -> None:
        self.assertEqual(decode_raw_input(mouse(0x0001)), [Decoded(0x1234, "mouse", "left", True)])
        self.assertEqual(decode_raw_input(mouse(0x0002)), [Decoded(0x1234, "mouse", "left", False)])
        self.assertEqual(decode_raw_input(mouse(0x0010)), [Decoded(0x1234, "mouse", "middle", True)])
        self.assertEqual(decode_raw_input(mouse(0x0100)), [Decoded(0x1234, "mouse", "x2", True)])

    def test_movement_and_the_wheel_say_nothing(self) -> None:
        self.assertEqual(decode_raw_input(mouse(0x0000)), [])
        self.assertEqual(decode_raw_input(mouse(0x0400)), [])  # wheel

    def test_two_buttons_in_one_packet_give_two_events(self) -> None:
        self.assertEqual(len(decode_raw_input(mouse(0x0001 | 0x0004))), 2)


class HidPacketTests(unittest.TestCase):
    def test_a_report_is_kept_as_hex(self) -> None:
        self.assertEqual(decode_raw_input(hid([bytes([0x01, 0xE9, 0x00])])), [Decoded(0x1234, "hid", "01e900", None)])

    def test_several_reports_in_one_packet(self) -> None:
        out = decode_raw_input(hid([bytes([1, 2]), bytes([3, 4])]))
        self.assertEqual([d.code for d in out], ["0102", "0304"])

    def test_idle_all_zero_reports_are_dropped(self) -> None:
        self.assertEqual(decode_raw_input(hid([bytes(3)])), [])
        self.assertEqual([d.code for d in decode_raw_input(hid([bytes(2), bytes([0, 5])]))], ["0005"])


class MalformedPacketTests(unittest.TestCase):
    def test_short_or_truncated_packets_are_ignored_not_crashed_on(self) -> None:
        good = keyboard(0x22)
        for cut in (0, 4, ctypes.sizeof(RAWINPUTHEADER) - 1, ctypes.sizeof(RAWINPUTHEADER), len(good) - 1):
            self.assertEqual(decode_raw_input(good[:cut]), [], cut)
        self.assertEqual(decode_raw_input(hid([bytes([1, 2, 3])])[:-1]), [])  # the report is cut short
        self.assertEqual(decode_raw_input(header(RIM_TYPEHID, 8, 1) + struct.pack("<II", 0, 0)), [])  # zero-size reports
        self.assertEqual(decode_raw_input(header(7, 0, 1)), [])  # a type nobody knows


if __name__ == "__main__":
    unittest.main()
