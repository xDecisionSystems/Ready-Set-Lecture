"""Compact custom-painted microphone level bar."""
from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget


# The bar spans -50 dBFS (empty) to 0 dBFS (full); 0.76 is -12 dBFS, where speech peaks start to clip.
_RED_ABOVE = 0.76


class AudioLevelBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self.setMinimumHeight(12)
        self.setMaximumHeight(16)
        self.setToolTip("Microphone level after gain: empty is -50 dBFS, full is 0 dBFS.\n"
                        "Aim for about 60% while speaking; red (above -12 dBFS) means too loud.")

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setBrush(QColor("#22303d"))
        painter.setPen(QColor("#536475"))
        painter.drawRoundedRect(rect, 4, 4)
        if self._level:
            color = QColor("#e85d4a") if self._level > _RED_ABOVE else QColor("#5ac878")
            painter.setPen(color)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width() * self._level, rect.height()), 4, 4)
