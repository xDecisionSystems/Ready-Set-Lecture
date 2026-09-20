"""Setup-window control: record from the selected microphone, then play it back to check the level."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from app.recorder.core.devices import MicDevice
from app.recorder.core.mic_test import MicTestWorker, describe_level, peak_dbfs


class MicTestPanel(QWidget):
    """Test mic starts recording; Play ends it and plays it back (and replays it afterwards)."""

    busy_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._mic: MicDevice | None = None
        self._gain = 1.0
        self._state = "idle"  # idle | recording | stopping | playing
        self._clip: Path | None = None  # the last test recording, kept so Play can replay it
        self._clip_ready = False
        self._result = ""
        self._elapsed = 0
        self._worker: MicTestWorker | None = None
        self._effect: QSoundEffect | None = None
        self._played = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.test_button = QPushButton("Test mic")
        self.play_button = QPushButton("▶ Play")
        self.status = QLabel("Test mic records; Play stops it and plays it back.")
        self.status.setWordWrap(True)
        self.test_button.clicked.connect(self._start)
        self.play_button.clicked.connect(self._play_clicked)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.test_button)
        layout.addWidget(self.play_button)
        layout.addWidget(self.status, 1)
        self._refresh_buttons()

    @property
    def busy(self) -> bool:
        return self._state != "idle"

    def set_mic(self, mic: MicDevice | None) -> None:
        if mic != self._mic and self._state == "idle":
            self._discard_clip()  # a clip from another microphone would be misleading
        self._mic = mic
        self._refresh_buttons()

    def set_gain(self, gain: float) -> None:
        self._gain = gain

    def cancel(self) -> None:
        """Abort whatever is running and delete the clip (window closing); safe to call when idle."""
        self._timer.stop()
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(3000)
        self._release_effect()
        self._discard_clip()
        self._set_state("idle")

    def _release_effect(self) -> None:
        # Detach first: stop() emits playingChanged synchronously, which must not re-enter with a live effect.
        effect, self._effect = self._effect, None
        if effect is not None:
            effect.stop()
            effect.deleteLater()

    def _start(self) -> None:
        if self._mic is None or self._state != "idle":
            return
        self._discard_clip()
        handle, name = tempfile.mkstemp(prefix="readysetlecturerecorder_mictest_", suffix=".wav")
        os.close(handle)
        self._clip = Path(name)
        self._elapsed = 0
        self._set_state("recording")
        self._show_elapsed()
        self._timer.start()
        self._worker = MicTestWorker(self._mic.capture_id, self._gain, self._clip, self)
        self._worker.done.connect(self._recorded)
        self._worker.error.connect(self._failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _play_clicked(self) -> None:
        if self._state == "recording":
            self._set_state("stopping")
            self._timer.stop()
            self.status.setText("Finishing…")
            if self._worker is not None:
                self._worker.stop()
        elif self._state == "playing":
            self._finish_playback()
        elif self._state == "idle" and self._clip_ready:
            self._play_clip()

    def _tick(self) -> None:
        self._elapsed += 1
        self._show_elapsed()

    def _show_elapsed(self) -> None:
        self.status.setText(f"Recording… speak, then press Play ({self._elapsed // 60}:{self._elapsed % 60:02d})")

    def _recorded(self, path: Path) -> None:
        self._timer.stop()
        self._clip_ready = True
        self._result = describe_level(peak_dbfs(path))
        self._play_clip()

    def _play_clip(self) -> None:
        if self._clip is None:
            return
        self._played = False
        self._set_state("playing")
        self.status.setText("Playing back…")
        self._effect = QSoundEffect(self)
        self._effect.statusChanged.connect(self._effect_status)
        self._effect.playingChanged.connect(self._effect_playing)
        self._effect.setSource(QUrl.fromLocalFile(str(self._clip)))

    def _effect_status(self) -> None:
        if self._effect is None:
            return
        if self._effect.status() == QSoundEffect.Status.Ready:
            self._effect.play()
        elif self._effect.status() == QSoundEffect.Status.Error:
            self._failed("The recording could not be played back.")

    def _effect_playing(self) -> None:
        if self._effect is None:
            return
        if self._effect.isPlaying():
            self._played = True
        elif self._played:
            self._finish_playback()

    def _finish_playback(self) -> None:
        if self._state != "playing":
            return
        self._release_effect()
        self.status.setText(self._result)
        self._set_state("idle")

    def _failed(self, message: str) -> None:
        self._timer.stop()
        self._release_effect()
        self._discard_clip()
        self.status.setText(f"Mic test failed: {message}")
        self._set_state("idle")

    def _worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _discard_clip(self) -> None:
        self._clip_ready = False
        if self._clip is not None:
            try:
                self._clip.unlink(missing_ok=True)
            except OSError:
                pass  # still held open by the audio backend; it lives in the temp dir
            self._clip = None

    def _set_state(self, state: str) -> None:
        was_busy = self._state != "idle"
        self._state = state
        self._refresh_buttons()
        if (state != "idle") != was_busy:
            self.busy_changed.emit(state != "idle")

    def _refresh_buttons(self) -> None:
        idle = self._state == "idle"
        self.test_button.setEnabled(idle and self._mic is not None)
        self.play_button.setEnabled(self._state in ("recording", "playing") or (idle and self._clip_ready))
        self.play_button.setText("■ Stop" if self._state == "playing" else "▶ Play")
