"""A green frame around the screen area being recorded, visible before and during recording."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from app.recorder.ui.capture_exclusion import exclude_from_capture

_BORDER = 3  # logical pixels


class RegionOutline(QWidget):
    """Click-through, always-on-top frame. It sits just outside the recorded area and is excluded from
    screen capture, so it never appears in the video even if exclusion is unavailable."""

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def set_region(self, region: QRect) -> None:
        """*region* is the recorded area in global logical (Qt) coordinates."""
        self.setGeometry(region.adjusted(-_BORDER, -_BORDER, _BORDER, _BORDER))
        # Only the ring exists as a window; the recorded area itself is not covered at all.
        self.setMask(QRegion(self.rect()).subtracted(QRegion(QRect(_BORDER, _BORDER, region.width(), region.height()))))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        exclude_from_capture(self)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        QPainter(self).fillRect(self.rect(), QColor("#43c76b"))
