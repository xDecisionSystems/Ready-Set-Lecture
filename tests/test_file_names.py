from __future__ import annotations

import unittest
from datetime import datetime

from app.recorder.core.file_names import MAX_NAME_LENGTH, clean_file_name, default_recording_name


class DefaultNameTests(unittest.TestCase):
    def test_default_name_carries_the_date_and_time(self) -> None:
        self.assertEqual(default_recording_name(datetime(2026, 9, 19, 15, 4, 7)), "recording_20260919_150407")

    def test_the_default_name_is_itself_valid(self) -> None:
        self.assertEqual(clean_file_name(default_recording_name(datetime(2026, 1, 2, 3, 4, 5))), "recording_20260102_030405")


class CleanFileNameTests(unittest.TestCase):
    def test_ordinary_names_pass_through_unchanged(self) -> None:
        for name in ("Lecture 3", "week-4_intro", "Dr. Smith - demo (final)", "café notes", "2026.09.19 lab"):
            self.assertEqual(clean_file_name(name), name)

    def test_surrounding_spaces_are_dropped(self) -> None:
        self.assertEqual(clean_file_name("   Lecture 3  "), "Lecture 3")

    def test_a_typed_mp4_extension_is_not_doubled(self) -> None:
        self.assertEqual(clean_file_name("Lecture 3.mp4"), "Lecture 3")
        self.assertEqual(clean_file_name("Lecture 3.MP4"), "Lecture 3")
        self.assertEqual(clean_file_name("Lecture 3 .mp4"), "Lecture 3")

    def test_other_dots_are_kept(self) -> None:
        self.assertEqual(clean_file_name("v1.2 demo"), "v1.2 demo")

    def test_empty_or_blank_is_rejected(self) -> None:
        for name in ("", "   ", ".mp4", "  .mp4"):
            self.assertIsNone(clean_file_name(name), repr(name))

    def test_characters_windows_forbids_are_rejected(self) -> None:
        for character in '\\/:*?"<>|':
            self.assertIsNone(clean_file_name(f"a{character}b"), character)
        self.assertIsNone(clean_file_name("tab\there"))
        self.assertIsNone(clean_file_name("new\nline"))

    def test_reserved_device_names_are_rejected_in_any_case_and_with_a_suffix(self) -> None:
        for name in ("CON", "con", "Nul", "COM1", "lpt9", "AUX.txt", "prn.mp4.mp4"):
            self.assertIsNone(clean_file_name(name), name)
        self.assertEqual(clean_file_name("CONFERENCE"), "CONFERENCE")  # only the exact device names are reserved
        self.assertEqual(clean_file_name("COM10"), "COM10")

    def test_a_trailing_dot_is_rejected(self) -> None:
        self.assertIsNone(clean_file_name("notes."))

    def test_very_long_names_are_rejected(self) -> None:
        self.assertIsNotNone(clean_file_name("a" * MAX_NAME_LENGTH))
        self.assertIsNone(clean_file_name("a" * (MAX_NAME_LENGTH + 1)))


if __name__ == "__main__":
    unittest.main()
