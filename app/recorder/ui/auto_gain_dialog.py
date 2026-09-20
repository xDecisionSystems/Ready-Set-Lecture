"""Prompt the user to speak normally for a few seconds, then work out the right microphone gain."""
from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from app.recorder.core.audio_meter import AudioLevelMeter
from app.recorder.core.auto_gain import AUTO_GAIN_SECONDS, read_wav, suggest_gain
from app.recorder.core.devices import MicDevice
from app.recorder.core.mic_test import MicTestWorker
from app.recorder.ui.level_bar import AudioLevelBar


class AutoGainDialog(QDialog):
    """Listens for AUTO_GAIN_SECONDS as soon as it opens. result_gain() is the chosen gain after OK, else None."""

    def __init__(self, mic: MicDevice, audio_meter: AudioLevelMeter, min_gain: float, max_gain: float, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto-adjust microphone")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._mic, self._min_gain, self._max_gain = mic, min_gain, max_gain
        self._audio_meter = audio_meter
        self._gain: float | None = None
        self._finished = False
        self._started = False
        self._remaining = AUTO_GAIN_SECONDS
        self._path: Path | None = None
        self._worker: MicTestWorker | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.prompt = QLabel("Speak in your normal voice")
        self.prompt.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.detail = QLabel("Talk the way you will while recording, and keep going until the countdown ends.")
        self.detail.setWordWrap(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.level_bar = AudioLevelBar()
        self.button = QPushButton("Cancel")
        self.button.clicked.connect(self._button_clicked)
        layout = QVBoxLayout(self)
        for widget in (self.prompt, self.detail, self.level_bar, self.status, self.button):
            layout.addWidget(widget)
        layout.setAlignment(self.button, Qt.AlignmentFlag.AlignRight)
        audio_meter.level_changed.connect(self.level_bar.set_level)

    def result_gain(self) -> float | None:
        return self._gain

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._started:
            self._started = True
            QTimer.singleShot(0, self._begin)

    def done(self, result: int) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        try:
            self._audio_meter.level_changed.disconnect(self.level_bar.set_level)
        except (RuntimeError, TypeError):
            pass
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(3000)
        self._delete_recording()
        super().done(result)

    def _begin(self) -> None:
        handle, name = tempfile.mkstemp(prefix="readysetlecturerecorder_autogain_", suffix=".wav")
        os.close(handle)
        self._path = Path(name)
        self.status.setText(f"Listening… {self._remaining} s")
        # Measured at gain 1.0 so the answer doesn't depend on where the slider happens to be.
        self._worker = MicTestWorker(self._mic.capture_id, 1.0, self._path, self)
        self._worker.done.connect(self._recorded)
        self._worker.error.connect(self._failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()
        self._timer.start()

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining > 0:
            self.status.setText(f"Listening… {self._remaining} s")
            return
        self._timer.stop()
        self.status.setText("Working out the gain…")
        self.button.setEnabled(False)
        if self._worker is not None:
            self._worker.stop()

    def _recorded(self, path: Path) -> None:
        try:
            samples, rate = read_wav(path)
        except (wave.Error, OSError, ValueError) as exc:
            self._failed(f"The recording could not be read: {exc}")
            return
        result = suggest_gain(samples, rate, self._min_gain, self._max_gain)
        self._delete_recording()
        self._gain = result.gain
        self._show_result("Done" if result.gain is not None else "No speech heard", result.message, "OK" if result.gain is not None else "Close")

    def _failed(self, message: str) -> None:
        self._timer.stop()
        self._delete_recording()
        self._show_result("Couldn't listen to the microphone", message, "Close")

    def _show_result(self, title: str, message: str, button_text: str) -> None:
        self._finished = True
        self.prompt.setText(title)
        self.detail.hide()
        self.status.setText(message)
        self.button.setText(button_text)
        self.button.setEnabled(True)
        self.button.setDefault(True)

    def _button_clicked(self) -> None:
        if self._finished and self._gain is not None:
            self.accept()
        else:
            self.reject()

    def _worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _delete_recording(self) -> None:
        if self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass  # still held open; it lives in the temp dir
            self._path = None
