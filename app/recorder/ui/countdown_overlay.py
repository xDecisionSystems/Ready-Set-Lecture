"""Brief non-interactive 3-2-1 overlay before a recording begins or resumes."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel

from app.recorder.ui.capture_exclusion import exclude_from_capture


class CountdownOverlay(QLabel):
    countdown_finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("color: black;")
        font = QFont()
        font.setPointSize(60)
        font.setBold(True)
        self.setFont(font)
        self.resize(180, 150)
        self._remaining = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Drawn by hand: a style-sheet background is not painted on this translucent window, which left the
        # digits floating directly over the desktop. Black digits on a light box; the border keeps it visible on white.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(0, 0, 0, 70), 2))
        painter.setBrush(QColor(255, 255, 255, 225))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 24, 24)
        painter.end()
        super().paintEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        exclude_from_capture(self)  # a countdown must never end up in the recording, even if it hides a moment late

    def start(self, seconds: int = 3) -> None:
        self._remaining = max(1, seconds)
        self.setText(str(self._remaining))
        self.show()
        self.raise_()
        self._timer.start(1000)

    def cancel(self) -> None:
        """Stop counting and disappear without announcing that the countdown finished."""
        self._timer.stop()
        self.hide()

    def _advance(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self.hide()
            self.countdown_finished.emit()
        else:
            self.setText(str(self._remaining))
