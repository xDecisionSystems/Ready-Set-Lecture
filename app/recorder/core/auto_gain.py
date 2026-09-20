"""Pick a microphone gain from a few seconds of normal speech."""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.recorder.core.audio_meter import IDEAL_SPEECH_DBFS

AUTO_GAIN_SECONDS = 6

_WINDOW_SECONDS = 0.05
_SPEECH_BAND_DB = 20.0  # windows within this of the loud ones count as speech; pauses and breaths are ignored
_NO_SPEECH_DBFS = -40.0  # if even the loud windows are quieter than this it's background noise, not speech
_PEAK_HEADROOM_DBFS = -3.0  # never pick a gain whose loud peaks would rise above this
_PEAK_PERCENTILE = 99.9  # ignores single clicks and taps


@dataclass(frozen=True)
class AutoGainResult:
    gain: float | None  # None when no speech was heard
    speech_dbfs: float | None  # average speech level at gain 1.0
    message: str


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A 16-bit PCM WAV as float samples of shape (frames, channels) in [-1, 1], and its sample rate."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("Expected 16-bit audio.")
        channels, rate = wav.getnchannels(), wav.getframerate()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    frames = data.size // channels
    return data[: frames * channels].reshape(frames, channels).astype(np.float32) / 32768.0, rate


def suggest_gain(samples: np.ndarray, sample_rate: int, min_gain: float, max_gain: float) -> AutoGainResult:
    """Choose the gain that brings the speech in *samples* (recorded at gain 1.0) to IDEAL_SPEECH_DBFS."""
    window = max(1, int(sample_rate * _WINDOW_SECONDS))
    windows = samples.shape[0] // window
    if windows == 0:
        return AutoGainResult(None, None, "The recording was too short. Please try again.")
    power = np.mean(np.square(samples[: windows * window]).reshape(windows, -1), axis=1)
    window_db = 10 * np.log10(np.maximum(power, 1e-12))
    loud_db = float(np.percentile(window_db, 95))
    if loud_db < _NO_SPEECH_DBFS:
        return AutoGainResult(None, None, "No speech was heard. Check the microphone and speak a little louder, then try again.")
    speech_db = 10 * np.log10(float(np.mean(power[window_db >= loud_db - _SPEECH_BAND_DB])))
    peak_db = 20 * np.log10(max(float(np.percentile(np.abs(samples), _PEAK_PERCENTILE)), 1e-9))
    wanted_db = IDEAL_SPEECH_DBFS - speech_db
    limit_db = _PEAK_HEADROOM_DBFS - peak_db  # the most gain that keeps the loud peaks clear of clipping
    gain = 10 ** (min(wanted_db, limit_db) / 20)
    chosen = round(min(max(gain, min_gain), max_gain), 2)
    heard = f"Your voice averaged {speech_db:.0f} dBFS."
    if gain > max_gain:
        message = (f"{heard} Gain set to {chosen:.2f}×, the maximum, but your voice is still quiet. "
                   "Raise the microphone level in Windows sound settings, or move closer.")
    elif gain < min_gain:
        message = (f"{heard} Gain set to {chosen:.2f}×, the minimum, but your voice is still loud. "
                   "Lower the microphone level in Windows sound settings, or move back.")
    elif limit_db < wanted_db:
        message = f"{heard} Gain set to {chosen:.2f}×, lowered a little to keep loud peaks from clipping."
    else:
        message = f"{heard} Gain set to {chosen:.2f}×, so your voice now sits at the ideal level (about 60% on the meter)."
    return AutoGainResult(chosen, float(speech_db), message)
