from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from app.recorder.core.audio_meter import IDEAL_SPEECH_DBFS, level_from_rms
from app.recorder.core.auto_gain import read_wav, suggest_gain

RATE = 48000
MIN_GAIN, MAX_GAIN = 0.5, 1.5


def speech(rms_dbfs: float, seconds: float = 6.0, pauses: bool = True, noise_dbfs: float = -70.0, channels: int = 1) -> np.ndarray:
    """Speech-like test signal: Gaussian bursts at *rms_dbfs* separated by pauses of quiet background noise."""
    rng = np.random.default_rng(1)
    frames = int(seconds * RATE)
    signal = rng.normal(0, 1, frames) * 10 ** (noise_dbfs / 20)
    burst, gap, pos = int(0.8 * RATE), int(0.4 * RATE) if pauses else 0, 0
    while pos < frames:
        end = min(pos + burst, frames)
        signal[pos:end] = rng.normal(0, 1, end - pos) * 10 ** (rms_dbfs / 20)
        pos = end + gap
    return np.repeat(np.clip(signal, -1, 1)[:, None], channels, axis=1).astype(np.float32)


class SuggestGainTests(unittest.TestCase):
    def test_ideal_speech_keeps_gain_at_one(self) -> None:
        result = suggest_gain(speech(-20), RATE, MIN_GAIN, MAX_GAIN)
        self.assertAlmostEqual(result.gain, 1.0, delta=0.03)
        self.assertAlmostEqual(result.speech_dbfs, -20, delta=1.0)
        self.assertIn("ideal level", result.message)

    def test_quiet_speech_is_boosted_to_the_ideal_level(self) -> None:
        result = suggest_gain(speech(-23), RATE, MIN_GAIN, MAX_GAIN)  # 3 dB below ideal -> about 1.41x
        self.assertAlmostEqual(result.gain, 1.41, delta=0.04)
        self.assertIn("ideal level", result.message)

    def test_loud_speech_is_reduced(self) -> None:
        result = suggest_gain(speech(-16), RATE, MIN_GAIN, MAX_GAIN)  # 4 dB above ideal -> about 0.63x
        self.assertAlmostEqual(result.gain, 0.63, delta=0.03)

    def test_very_quiet_speech_stops_at_the_maximum_and_says_why(self) -> None:
        result = suggest_gain(speech(-30), RATE, MIN_GAIN, MAX_GAIN)
        self.assertEqual(result.gain, MAX_GAIN)
        self.assertIn("maximum", result.message)
        self.assertIn("Windows sound settings", result.message)

    def test_very_loud_speech_stops_at_the_minimum_and_says_why(self) -> None:
        result = suggest_gain(speech(-9), RATE, MIN_GAIN, MAX_GAIN)
        self.assertEqual(result.gain, MIN_GAIN)
        self.assertIn("minimum", result.message)

    def test_pauses_and_breaths_do_not_drag_the_speech_level_down(self) -> None:
        with_pauses = suggest_gain(speech(-20, pauses=True), RATE, MIN_GAIN, MAX_GAIN)
        continuous = suggest_gain(speech(-20, pauses=False), RATE, MIN_GAIN, MAX_GAIN)
        self.assertAlmostEqual(with_pauses.speech_dbfs, continuous.speech_dbfs, delta=1.0)
        self.assertAlmostEqual(with_pauses.gain, continuous.gain, delta=0.03)

    def test_stereo_gives_the_same_answer_as_mono(self) -> None:
        mono = suggest_gain(speech(-24, channels=1), RATE, MIN_GAIN, MAX_GAIN)
        stereo = suggest_gain(speech(-24, channels=2), RATE, MIN_GAIN, MAX_GAIN)
        self.assertEqual(mono.gain, stereo.gain)

    def test_no_speech_changes_nothing(self) -> None:
        silence = np.random.default_rng(2).normal(0, 1, RATE * 6).astype(np.float32)[:, None] * 10 ** (-70 / 20)
        result = suggest_gain(silence, RATE, MIN_GAIN, MAX_GAIN)
        self.assertIsNone(result.gain)
        self.assertIn("No speech", result.message)

    def test_steady_background_noise_is_not_mistaken_for_quiet_speech(self) -> None:
        noise = np.random.default_rng(3).normal(0, 1, RATE * 6).astype(np.float32)[:, None] * 10 ** (-43 / 20)
        self.assertIsNone(suggest_gain(noise, RATE, MIN_GAIN, MAX_GAIN).gain)  # must not be boosted to the maximum

    def test_a_soft_voice_is_still_recognised_as_speech(self) -> None:
        result = suggest_gain(speech(-36), RATE, MIN_GAIN, MAX_GAIN)
        self.assertEqual(result.gain, MAX_GAIN)
        self.assertIn("maximum", result.message)

    def test_a_too_short_recording_changes_nothing(self) -> None:
        result = suggest_gain(np.zeros((100, 1), dtype=np.float32), RATE, MIN_GAIN, MAX_GAIN)
        self.assertIsNone(result.gain)

    def test_gain_is_held_back_when_peaks_would_clip(self) -> None:
        signal = speech(-28)
        for start in range(RATE // 2, len(signal), RATE):  # a 5 ms full-band click every second, peaking at -4 dBFS
            signal[start:start + RATE // 200, 0] = 0.63 * np.sign(np.sin(np.arange(RATE // 200) * 0.5))
        result = suggest_gain(signal, RATE, MIN_GAIN, MAX_GAIN)
        wanted = 10 ** ((IDEAL_SPEECH_DBFS - result.speech_dbfs) / 20)
        self.assertLess(result.gain, wanted)
        self.assertAlmostEqual(result.gain, 1.12, delta=0.04)  # -3 dBFS headroom over a -4 dBFS peak = +1 dB
        self.assertIn("clipping", result.message)

    def test_result_is_rounded_to_the_sliders_one_percent_steps(self) -> None:
        gain = suggest_gain(speech(-22.3), RATE, MIN_GAIN, MAX_GAIN).gain
        self.assertEqual(gain, round(gain, 2))

    def test_the_ideal_speech_level_is_sixty_percent_on_the_meter(self) -> None:
        self.assertAlmostEqual(level_from_rms(10 ** (IDEAL_SPEECH_DBFS / 20), 1.0), 0.60, places=3)


class ReadWavTests(unittest.TestCase):
    def test_reads_stereo_16_bit_as_normalised_floats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "in.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(44100)
                wav.writeframes(np.array([16384, -16384, 0, 32767], dtype=np.int16).tobytes())
            samples, rate = read_wav(path)
        self.assertEqual(rate, 44100)
        self.assertEqual(samples.shape, (2, 2))
        self.assertAlmostEqual(float(samples[0, 0]), 0.5, places=4)
        self.assertAlmostEqual(float(samples[0, 1]), -0.5, places=4)


if __name__ == "__main__":
    unittest.main()
