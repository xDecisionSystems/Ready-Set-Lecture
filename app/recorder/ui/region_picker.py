"""Screen and camera area pickers: a frozen screenshot or a live camera preview with a draw-and-adjust box."""
from __future__ import annotations

import time

from PySide6.QtCore import QRect, Qt, QThread
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QCamera, QCameraDevice, QCameraFormat, QMediaCaptureSession, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QWidget

from app.recorder.core.devices import ScreenSource
from app.recorder.ui.box_selector import SelectionCanvas


def selection_to_physical(
    selection: tuple[int, int, int, int],
    view_size: tuple[int, int],
    physical_rect: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Map a selection in the picker's on-screen (logical) pixels to absolute physical desktop pixels.

    *view_size* is the picker's on-screen size and *physical_rect* the monitor's rect in physical pixels
    (its origin may be negative on a multi-monitor desktop). The result is clamped to the monitor.
    With an origin of (0, 0) it maps a selection onto a camera frame instead.
    """
    sel_x, sel_y, sel_w, sel_h = selection
    view_w, view_h = max(1, view_size[0]), max(1, view_size[1])
    screen_x, screen_y, screen_w, screen_h = physical_rect
    scale_x, scale_y = screen_w / view_w, screen_h / view_h
    x = max(0, min(round(sel_x * scale_x), screen_w - 1))
    y = max(0, min(round(sel_y * scale_y), screen_h - 1))
    width = max(1, min(round(sel_w * scale_x), screen_w - x))
    height = max(1, min(round(sel_h * scale_y), screen_h - y))
    return screen_x + x, screen_y + y, width, height


def _fill_with(dialog: QDialog, canvas: SelectionCanvas) -> None:
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(canvas)


class ScreenRegionPicker(QDialog):
    """Snipping-Tool-style picker: the screen is frozen and dimmed. Drag to draw the area, grab its edges and
    corners to adjust (or drag inside to move it), then click the check (or press Enter) to confirm."""

    def __init__(self, screen_source: ScreenSource, qscreen, parent=None) -> None:
        super().__init__(parent)
        self.screen_source = screen_source
        self._screen = qscreen
        self.setWindowTitle("Select recording area")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.canvas = SelectionCanvas(lambda selection, view: selection_to_physical(selection, view, self.screen_source.physical_rect)[2:])
        _fill_with(self, self.canvas)
        self.canvas.confirmed.connect(self._confirmed)
        self.canvas.cancelled.connect(self.reject)
        self._shot = QPixmap()
        self._picked: QRect | None = None
        self._hidden_window: QWidget | None = None

    def exec(self) -> int:
        # Like Snipping Tool, capture the screen without our own window in the way; done() brings it back.
        owner = self.parentWidget().window() if self.parentWidget() is not None else None
        if owner is not None and owner.isVisible():
            self._hidden_window = owner
            owner.hide()
            QApplication.processEvents()
            QThread.msleep(200)  # let the compositor drop the window before the grab
            QApplication.processEvents()
        self._grab_screen()
        self.setGeometry(self._screen.geometry())
        return super().exec()

    def done(self, result: int) -> None:  # noqa: N802 - Qt override
        if self._hidden_window is not None:
            self._hidden_window.show()
            self._hidden_window.activateWindow()
            self._hidden_window = None
        super().done(result)

    def picked_absolute_rect(self) -> tuple[int, int, int, int]:
        """The chosen area in physical desktop pixels (the whole monitor if nothing was picked)."""
        if self._picked is None:
            return self.screen_source.physical_rect
        picked = self._picked
        return selection_to_physical((picked.x(), picked.y(), picked.width(), picked.height()),
                                     (self.canvas.width(), self.canvas.height()), self.screen_source.physical_rect)

    def picked_logical_rect(self) -> QRect | None:
        """The chosen area in global logical (Qt) coordinates, for drawing the on-screen outline."""
        return None if self._picked is None else self._picked.translated(self._screen.geometry().topLeft())

    def _grab_screen(self) -> None:
        shot = self._screen.grabWindow(0)
        shot.setDevicePixelRatio(1.0)  # work in physical pixels; painting scales down to the on-screen size
        self._shot = shot
        self.canvas.set_picture(shot.toImage())

    def _confirmed(self) -> None:
        self._picked = self.canvas.selection()
        self.accept()


class CameraRegionPicker(QDialog):
    """Live camera preview with the same draw/adjust/confirm box as the screen picker.

    The preview is painted by the canvas itself (frames from a video sink) rather than by a QVideoWidget,
    because a video widget draws natively on top of anything placed over it, hiding the selection box.
    """

    def __init__(self, qt_camera_device: QCameraDevice, native_size: tuple[int, int], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select camera area")
        self._native_size = native_size
        self._picked: QRect | None = None
        self._last_frame = 0.0
        width, height = native_size
        display_width = min(960, width)
        display_height = max(1, round(display_width * height / width))
        self.canvas = SelectionCanvas(lambda selection, view: selection_to_physical(selection, view, (0, 0, *native_size))[2:], "Starting camera…")
        self.canvas.setFixedSize(display_width, display_height)
        _fill_with(self, self.canvas)
        self.canvas.confirmed.connect(self._confirmed)
        self.canvas.cancelled.connect(self.reject)
        self._camera = QCamera(qt_camera_device, self)
        matching = self._matching_format(qt_camera_device, native_size)
        if matching is not None:
            self._camera.setCameraFormat(matching)  # same field of view as the ffmpeg capture
        self._session = QMediaCaptureSession(self)
        self._session.setCamera(self._camera)
        self._sink = QVideoSink(self)
        self._session.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._frame)
        self._camera.start()

    @staticmethod
    def _matching_format(device: QCameraDevice, native_size: tuple[int, int]) -> QCameraFormat | None:
        matches = [fmt for fmt in device.videoFormats() if (fmt.resolution().width(), fmt.resolution().height()) == tuple(native_size)]
        return max(matches, key=lambda fmt: fmt.maxFrameRate()) if matches else None

    def picked_rect(self) -> tuple[int, int, int, int] | None:
        """The chosen area in camera-frame pixels, or None if nothing was confirmed."""
        if self._picked is None:
            return None
        picked = self._picked
        return selection_to_physical((picked.x(), picked.y(), picked.width(), picked.height()),
                                     (self.canvas.width(), self.canvas.height()), (0, 0, *self._native_size))

    def done(self, result: int) -> None:  # noqa: N802 - Qt override
        self._camera.stop()
        super().done(result)

    def _frame(self, frame: QVideoFrame) -> None:
        now = time.monotonic()
        if now - self._last_frame < 0.04:  # ~25 fps is plenty for aiming a crop
            return
        self._last_frame = now
        image = frame.toImage()
        if not image.isNull():
            self.canvas.set_picture(image)

    def _confirmed(self) -> None:
        self._picked = self.canvas.selection()
        self.accept()
