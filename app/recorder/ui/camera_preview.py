"""Full-screen live view of what the camera is recording."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QWidget

_HINT = "Camera preview.  Click the glasses on the controls, or double-click, to hide it."
_HINT_SECONDS = 5


class CameraPreviewWindow(QWidget):
    """Fills a screen with the live picture (letterboxed, never stretched).

    It never takes keyboard focus or activation, so clicking it cannot raise it above the floating controls, and
    it does not steal focus from whatever the user is doing. Frames come from the recording process itself, so it
    shows exactly what is being recorded, crop included.
    """

    dismissed = Signal()  # the user double-clicked it away

    def __init__(self, screen=None) -> None:
        super().__init__(None)
        self.setWindowTitle("Camera preview")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._screen = screen
        self._image = QImage()
        self._paused = False
        self._status = ""
        self._show_hint = False
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._hide_hint)

    def show_on_screen(self) -> None:
        screen = self._screen or QGuiApplication.primaryScreen()
        self.setGeometry(screen.geometry())
        self._show_hint = True
        self._hint_timer.start(_HINT_SECONDS * 1000)
        self.showFullScreen()

    def set_image(self, image: QImage) -> None:
        self._image = image
        self._status = ""  # a new picture means whatever we were waiting for has happened
        if self.isVisible():
            self.update()

    def set_status(self, text: str) -> None:
        """A short note over the picture, e.g. 'Starting the recording…'; cleared by the next frame."""
        self._status = text
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.dismissed.emit()

    def _hide_hint(self) -> None:
        self._show_hint = False
        self.update()

    def _picture_rect(self) -> QRect:
        size = self._image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        rect = QRect(0, 0, size.width(), size.height())
        rect.moveCenter(self.rect().center())
        return rect

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))
        if self._image.isNull():
            painter.setPen(QColor(200, 208, 216))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for the camera…")
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self._picture_rect(), self._image)
        if self._paused:
            self._draw_banner(painter, "Paused", self.rect().center())
        elif self._status:
            self._draw_banner(painter, self._status, self.rect().center())
        if self._show_hint:
            self._draw_banner(painter, _HINT, QPoint(self.width() // 2, self.height() - 70))

    def _draw_banner(self, painter: QPainter, text: str, center: QPoint) -> None:
        metrics = painter.fontMetrics()
        box = QRect(0, 0, metrics.horizontalAdvance(text) + 40, metrics.height() + 20)
        box.moveCenter(center)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(box, 10, 10)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
