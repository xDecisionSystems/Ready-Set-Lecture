from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings

from app.core import settings


class RecorderSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.temp.name)

    def setUp(self) -> None:
        # Never clear() anything but the scratch store: the native one is the user's real saved settings.
        self.assertTrue(settings._settings().fileName().startswith(self.temp.name.replace("\\", "/")),
                        "settings are not redirected to the scratch file; refusing to touch the real store")
        current = settings._settings()
        current.clear()
        current.sync()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_gpu_encoding_is_on_until_it_is_switched_off_and_the_choice_is_kept(self) -> None:
        self.assertTrue(settings.get_gpu_encoding())
        settings.set_gpu_encoding(False)
        self.assertFalse(settings.get_gpu_encoding())
        settings.set_gpu_encoding(True)
        self.assertTrue(settings.get_gpu_encoding())

    def test_settings_go_to_the_redirected_store_not_the_native_one(self) -> None:
        settings.set_record_gain(1.25)
        self.assertEqual(settings._settings().format(), QSettings.Format.IniFormat)
        self.assertNotIn("HKEY", settings._settings().fileName())

    def test_favorite_folders_keep_the_order_they_were_starred_in(self) -> None:
        first, second = Path(self.temp.name) / "lectures", Path(self.temp.name) / "demos"
        self.assertEqual(settings.get_favorite_output_dirs(), [])
        settings.add_favorite_output_dir(first)
        settings.add_favorite_output_dir(second)
        self.assertEqual(settings.get_favorite_output_dirs(), [first, second])
        self.assertTrue(settings.is_favorite_output_dir(first))

    def test_starring_the_same_folder_twice_does_not_duplicate_it(self) -> None:
        folder = Path(self.temp.name) / "lectures"
        settings.add_favorite_output_dir(folder)
        settings.add_favorite_output_dir(folder)
        settings.add_favorite_output_dir(Path(self.temp.name) / "other" / ".." / "lectures")  # same folder, spelled differently
        self.assertEqual(len(settings.get_favorite_output_dirs()), 1)

    def test_unstarring_removes_only_that_folder(self) -> None:
        keep, drop = Path(self.temp.name) / "keep", Path(self.temp.name) / "drop"
        settings.add_favorite_output_dir(keep)
        settings.add_favorite_output_dir(drop)
        settings.remove_favorite_output_dir(Path(self.temp.name) / "other" / ".." / "drop")  # same folder, spelled differently
        self.assertEqual(settings.get_favorite_output_dirs(), [keep])
        self.assertFalse(settings.is_favorite_output_dir(drop))
        settings.remove_favorite_output_dir(drop)  # removing again is harmless
        self.assertEqual(settings.get_favorite_output_dirs(), [keep])

    def test_hide_cursor_defaults_to_showing_it_and_round_trips(self) -> None:
        self.assertFalse(settings.get_hide_cursor())
        settings.set_hide_cursor(True)
        self.assertTrue(settings.get_hide_cursor())
        settings.set_hide_cursor(False)
        self.assertFalse(settings.get_hide_cursor())

    def test_camera_preview_defaults_to_shown_and_round_trips(self) -> None:
        self.assertTrue(settings.get_show_camera_preview())
        settings.set_show_camera_preview(False)
        self.assertFalse(settings.get_show_camera_preview())
        settings.set_show_camera_preview(True)
        self.assertTrue(settings.get_show_camera_preview())

    def test_red_screen_when_paused_defaults_to_off_and_round_trips(self) -> None:
        self.assertFalse(settings.get_red_when_paused())
        settings.set_red_when_paused(True)
        self.assertTrue(settings.get_red_when_paused())
        settings.set_red_when_paused(False)
        self.assertFalse(settings.get_red_when_paused())

    def test_camera_format_defaults_to_automatic_and_round_trips(self) -> None:
        self.assertEqual(settings.get_camera_format(), "auto")
        for choice in ("h264", "hevc", "mjpeg", "raw", "auto"):
            settings.set_camera_format(choice)
            self.assertEqual(settings.get_camera_format(), choice)

    def test_a_camera_format_nobody_knows_is_ignored(self) -> None:
        settings.set_camera_format("hevc")
        settings.set_camera_format("av1")  # not a choice: rejected, the previous one stays
        self.assertEqual(settings.get_camera_format(), "hevc")
        settings._settings().setValue("record/camera_format", "garbage")  # a damaged saved value reads as automatic
        self.assertEqual(settings.get_camera_format(), "auto")

    def test_bt_clicker_setup_defaults_to_nothing_and_round_trips(self) -> None:
        from app.recorder.core.clicker import ClickerConfig
        self.assertFalse(ClickerConfig.from_json(settings.get_clicker_config()).is_ready())
        config = ClickerConfig()
        config.select_device("05ac:022c:8&243a709b&0", "T01")
        config.bind("pause", "key:22")
        settings.set_clicker_config(config.to_json())
        loaded = ClickerConfig.from_json(settings.get_clicker_config())
        self.assertEqual((loaded.device_name, loaded.selected_bindings()), ("T01", {"pause": "key:22"}))

    def test_only_starred_folders_are_ever_saved(self) -> None:
        settings.add_favorite_output_dir(Path(self.temp.name) / "starred")
        keys = settings._settings().allKeys()
        self.assertEqual([key for key in keys if key.startswith("record/")], ["record/favorite_output_dirs"])

    def _legacy_store(self) -> QSettings:
        legacy = QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, settings._LEGACY_ORG, settings._LEGACY_APP)
        self.assertTrue(legacy.fileName().startswith(self.temp.name.replace("\\", "/")), "the old store is not the scratch one")
        legacy.clear()
        return legacy

    def test_settings_saved_under_the_old_program_name_are_carried_over(self) -> None:
        legacy = self._legacy_store()
        legacy.setValue("export/output_dir", str(Path(self.temp.name) / "lectures"))
        legacy.setValue("record/hide_cursor", True)
        legacy.sync()
        settings._stores_checked.clear()  # as at the start of a run
        self.assertEqual(settings.get_output_dir(), Path(self.temp.name) / "lectures")
        self.assertTrue(settings.get_hide_cursor())
        self.assertEqual(legacy.value("export/output_dir"), str(Path(self.temp.name) / "lectures"), "the old copy is left alone")

    def test_carried_over_settings_do_not_overwrite_ones_already_saved(self) -> None:
        legacy = self._legacy_store()
        legacy.setValue("export/output_dir", "old")
        legacy.sync()
        settings.set_record_gain(1.5)  # the new store already has something
        settings._stores_checked.clear()
        self.assertIsNone(settings.get_output_dir())
        self.assertEqual(settings.get_record_gain(), 1.5)

    def test_nothing_to_carry_over_is_fine(self) -> None:
        self._legacy_store().sync()
        settings._stores_checked.clear()
        self.assertIsNone(settings.get_output_dir())

    def test_gain_and_device_preferences_round_trip(self) -> None:
        settings.set_last_mic_device("Microphone")
        settings.set_last_record_source("camera:Webcam")
        settings.set_record_gain(1.75)
        self.assertEqual(settings.get_last_mic_device(), "Microphone")
        self.assertEqual(settings.get_last_record_source(), "camera:Webcam")
        self.assertEqual(settings.get_record_gain(), 1.75)


if __name__ == "__main__":
    unittest.main()
