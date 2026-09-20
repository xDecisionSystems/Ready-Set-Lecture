from __future__ import annotations

import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.recorder.core.mic_test import build_mic_test_argv, describe_level, first_error_line, peak_dbfs


def _write_wav(path: Path, samples: np.ndarray, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(samples.astype(np.int16).tobytes())


class MicTestTests(unittest.TestCase):
    def test_argv_records_audio_only_with_gain_and_no_literal_quotes(self) -> None:
        with patch("app.recorder.core.mic_test.find_ffmpeg", return_value="ffmpeg"):
            argv = build_mic_test_argv("Surface Stereo Microphones", 1.5, Path("test.wav"), seconds=5)
        self.assertEqual(argv, [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
            "-f", "dshow", "-i", "audio=Surface Stereo Microphones",
            "-af", "volume=1.500", "-t", "5", "-c:a", "pcm_s16le", "test.wav",
        ])

    def test_peak_of_half_scale_sine_is_about_minus_six_dbfs(self) -> None:
        tone = 0.5 * 32768 * np.sin(2 * math.pi * 440 * np.arange(48000) / 48000)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tone.wav"
            _write_wav(path, tone)
            self.assertAlmostEqual(peak_dbfs(path), -6.02, delta=0.1)

    def test_peak_uses_loudest_channel_of_stereo(self) -> None:
        stereo = np.zeros((1000, 2))
        stereo[500, 1] = 16384  # only the right channel has signal, at half scale
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stereo.wav"
            _write_wav(path, stereo.reshape(-1), channels=2)
            self.assertAlmostEqual(peak_dbfs(path), -6.02, delta=0.01)

    def test_full_scale_negative_sample_reads_zero_dbfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clip.wav"
            _write_wav(path, np.array([0, -32768, 0]))
            self.assertAlmostEqual(peak_dbfs(path), 0.0, places=6)

    def test_digital_silence_and_empty_files_have_no_peak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            silent, empty = Path(temporary) / "silent.wav", Path(temporary) / "empty.wav"
            _write_wav(silent, np.zeros(1000))
            _write_wav(empty, np.zeros(0))
            self.assertIsNone(peak_dbfs(silent))
            self.assertIsNone(peak_dbfs(empty))

    def test_first_error_line_drops_ffmpeg_context_prefix(self) -> None:
        log = (
            "[in#0 @ 000001D9DBBC4240] Could not find audio only device with name [Mic] among source devices of type audio.\r\n"
            "[in#0 @ 000001D9DBBC3D80] Error opening input: I/O error\r\n"
        )
        self.assertEqual(first_error_line(log), "Could not find audio only device with name [Mic] among source devices of type audio.")
        self.assertEqual(first_error_line("\n  \n"), "")

    def test_level_descriptions_cover_each_band(self) -> None:
        self.assertIn("No sound", describe_level(None))
        self.assertIn("No sound", describe_level(-70.0))
        self.assertIn("quiet", describe_level(-40.0))
        self.assertIn("good level", describe_level(-12.0))
        self.assertIn("good level", describe_level(-3.0))
        self.assertIn("clipping", describe_level(-0.2))


if __name__ == "__main__":
    unittest.main()
