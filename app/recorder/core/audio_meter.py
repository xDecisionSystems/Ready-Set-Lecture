"""A lightweight shared-mode microphone RMS meter for recorder UI feedback."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSource

# The bar is linear in dBFS: empty at _FLOOR_DBFS, full at 0 dBFS (digital full scale). With -50 the ideal
# speech level, IDEAL_SPEECH_DBFS (RMS), sits at 60%.
IDEAL_SPEECH_DBFS = -20.0
_FLOOR_DBFS = -50.0


def level_from_rms(rms: float, gain: float) -> float:
    """Bar fill 0.0-1.0 for a microphone RMS (fraction of full scale) after *gain*."""
    level = rms * gain
    if level <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 + 20 * math.log10(level) / -_FLOOR_DBFS))


class AudioLevelMeter(QObject):
    level_changed = Signal(float)
    error = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._source: QAudioSource | None = None
        self._device = None
        self._gain = 1.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)

    def start(self, device: QAudioDevice) -> None:
        self.stop()
        if device.isNull():
            return
        try:
            audio_format = device.preferredFormat()
            # Explicit Int16 keeps polling and RMS conversion simple. Fall
            # back to the device's preferred format only when it rejects it.
            audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            if not device.isFormatSupported(audio_format):
                audio_format = device.preferredFormat()
            if audio_format.sampleFormat() != QAudioFormat.SampleFormat.Int16:
                raise RuntimeError("The microphone does not provide 16-bit PCM for level monitoring.")
            self._source = QAudioSource(device, audio_format, self)
            self._device = self._source.start()
            if self._device is None:
                raise RuntimeError("Could not open microphone for level monitoring.")
            self._timer.start()
        except Exception as exc:  # noqa: BLE001 - meter failure must never block recording
            self.stop()
            self.error.emit(str(exc))

    def stop(self) -> None:
        self._timer.stop()
        if self._source is not None:
            self._source.stop()
            self._source.deleteLater()
        self._source = None
        self._device = None
        self.level_changed.emit(0.0)

    def set_gain(self, gain: float) -> None:
        self._gain = max(0.0, gain)

    def _poll(self) -> None:
        if self._device is None:
            return
        data = bytes(self._device.readAll())
        if len(data) < 2:
            return
        samples = np.frombuffer(data[: len(data) - (len(data) % 2)], dtype=np.int16)
        if not len(samples):
            return
        rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32) / 32768.0))))
        self.level_changed.emit(level_from_rms(rms, self._gain))
