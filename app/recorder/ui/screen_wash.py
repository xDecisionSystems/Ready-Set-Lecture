"""Translucent colour washes over every screen: red while a recording is paused, blue when a mark is made in a camera recording."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from app.recorder.ui.capture_exclusion import exclude_from_capture

PAUSED_COLOR = QColor(230, 20, 20, 120)  # red enough to be unmistakable, light enough to keep working on what is underneath
MARK_FLASH_COLOR = QColor(20, 90, 255, 150)  # strong enough that the light from the screen shows blue on the person in front of it
MARK_FLASH_MS = 2000


class _WashWindow(QWidget):
    """One screen's worth of colour. Clicks go straight through it, it never takes focus, and it is kept out of the
    recording (and out of screen sharing), like the recorder's other windows."""

    def __init__(self, screen, color: QColor) -> None:
        super().__init__(None)
        self._color = color
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        QPainter(self).fillRect(self.rect(), self._color)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        exclude_from_capture(self)


class ScreenWash:
    """Shows and hides a colour wash on all screens. Windows are made when shown and dropped when hidden."""

    def __init__(self, color: QColor) -> None:
        self._color = color
        self._windows: list[_WashWindow] = []

    @property
    def windows(self) -> list[QWidget]:
        return list(self._windows)

    @property
    def visible(self) -> bool:
        return bool(self._windows)

    def show(self) -> None:
        if self._windows:
            return
        for screen in QGuiApplication.screens():
            window = _WashWindow(screen, self._color)
            self._windows.append(window)
            window.show()

    def hide(self) -> None:
        windows, self._windows = self._windows, []
        for window in windows:
            window.hide()
            window.deleteLater()


class MarkFlash(QObject):
    """A blue wash over every screen for two seconds. Marking again while it shows starts the two seconds afresh."""

    def __init__(self, parent=None, duration_ms: int = MARK_FLASH_MS) -> None:
        super().__init__(parent)
        self._wash = ScreenWash(MARK_FLASH_COLOR)
        self._timer = QTimer(self, singleShot=True, interval=duration_ms)
        self._timer.timeout.connect(self._wash.hide)

    @property
    def windows(self) -> list[QWidget]:
        return self._wash.windows

    @property
    def visible(self) -> bool:
        return self._wash.visible

    def flash(self) -> None:
        self._wash.show()
        self._timer.start()

    def cancel(self) -> None:
        self._timer.stop()
        self._wash.hide()
