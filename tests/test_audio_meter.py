from __future__ import annotations

import unittest

from app.recorder.core.audio_meter import level_from_rms


class LevelFromRmsTests(unittest.TestCase):
    def test_ideal_speech_level_reads_sixty_percent(self) -> None:
        self.assertAlmostEqual(level_from_rms(0.1, 1.0), 0.60, places=3)  # -20 dBFS RMS

    def test_level_is_measured_after_gain(self) -> None:
        self.assertAlmostEqual(level_from_rms(0.1 / 1.2, 1.2), level_from_rms(0.1, 1.0), places=6)

    def test_gain_of_two_adds_six_db(self) -> None:
        self.assertAlmostEqual(level_from_rms(0.05, 2.0), 0.60, places=3)
        self.assertAlmostEqual(level_from_rms(0.1, 1.0) - level_from_rms(0.05, 1.0), 6.0206 / 50, places=4)

    def test_silence_and_floor_read_empty(self) -> None:
        self.assertEqual(level_from_rms(0.0, 1.0), 0.0)
        self.assertEqual(level_from_rms(0.1, 0.0), 0.0)
        self.assertEqual(level_from_rms(0.0001, 1.0), 0.0)  # -80 dBFS, below the -50 dBFS floor

    def test_full_scale_and_above_are_clamped_full(self) -> None:
        self.assertAlmostEqual(level_from_rms(1.0, 1.0), 1.0, places=6)
        self.assertEqual(level_from_rms(1.0, 1.2), 1.0)

    def test_minus_twelve_dbfs_is_the_red_threshold(self) -> None:
        self.assertAlmostEqual(level_from_rms(10 ** (-12 / 20), 1.0), 0.76, places=3)


if __name__ == "__main__":
    unittest.main()
