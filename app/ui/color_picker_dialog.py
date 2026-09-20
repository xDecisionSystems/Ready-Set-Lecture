"""Modal dialog for calibrating a ColorSpec.

Shows one video frame and lets the user drag a box around the colored paper
marker; the pixels inside that box fix both the region to search and the
color to match (see color_scanner.calibrate_color_spec).
"""
from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.core.color_scanner import ColorSpec, calibrate_color_spec

_MAX_PREVIEW_WIDTH = 960
_MAX_PREVIEW_HEIGHT = 600
_MIN_SELECTION_PX = 4


def _bgr_to_pixmap(frame_bgr: np.ndarray) -> QPixmap:
    rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
    h, w, _ = rgb.shape
    image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888)
    return QPixmap.fromImage(image.copy())  # .copy(): image aliases the numpy buffer, which goes out of scope


class _DragLabel(QLabel):
    """Displays the frame pixmap and reports a dragged rectangle in the
    label's own (scaled-preview) pixel coordinates."""

    selection_changed = Signal(QRect)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._origin: QPoint | None = None
        self.selection = QRect()
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_selection(self, rect: QRect) -> None:
        self.selection = rect
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        self._origin = event.position().toPoint()
        self.selection = QRect(self._origin, self._origin)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if self._origin is None:
            return
        self.selection = QRect(self._origin, event.position().toPoint()).normalized()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if self._origin is None:
            return
        self.selection = QRect(self._origin, event.position().toPoint()).normalized()
        self._origin = None
        self.update()
        self.selection_changed.emit(self.selection)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt override)
        super().paintEvent(event)
        if self.selection.width() < 2 or self.selection.height() < 2:
            return
        painter = QPainter(self)
        painter.setPen(QPen(Qt.GlobalColor.yellow, 2, Qt.PenStyle.DashLine))
        painter.drawRect(self.selection)


class ColorRegionDialog(QDialog):
    def __init__(
        self,
        frame_bgr: np.ndarray,
        initial_spec: ColorSpec | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set Marker Region")
        self.result_spec: ColorSpec | None = None
        self._frame = frame_bgr
        frame_h, frame_w = frame_bgr.shape[:2]

        self._scale = min(1.0, _MAX_PREVIEW_WIDTH / frame_w, _MAX_PREVIEW_HEIGHT / frame_h)
        scaled_w = max(1, round(frame_w * self._scale))
        scaled_h = max(1, round(frame_h * self._scale))

        pixmap = _bgr_to_pixmap(frame_bgr).scaled(
            scaled_w,
            scaled_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Drag a box around the colored paper marker."))

        self._image_label = _DragLabel(self)
        self._image_label.setPixmap(pixmap)
        self._image_label.setFixedSize(scaled_w, scaled_h)
        self._image_label.selection_changed.connect(self._on_selection_changed)
        layout.addWidget(self._image_label)

        self._swatch_label = QLabel("No region selected yet.")
        self._swatch_label.setMinimumHeight(28)
        layout.addWidget(self._swatch_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout.addWidget(self._buttons)

        if initial_spec is not None:
            rect = QRect(
                round(initial_spec.roi_x * scaled_w),
                round(initial_spec.roi_y * scaled_h),
                round(initial_spec.roi_w * scaled_w),
                round(initial_spec.roi_h * scaled_h),
            )
            self._image_label.set_selection(rect)
            self._on_selection_changed(rect)

    def _on_selection_changed(self, rect: QRect) -> None:
        if rect.width() < _MIN_SELECTION_PX or rect.height() < _MIN_SELECTION_PX:
            return
        # Map the dialog's scaled-preview rect back to the original frame's
        # pixel coordinates before sampling color, so calibration isn't
        # distorted by the preview's downscaling.
        frame_h, frame_w = self._frame.shape[:2]
        x = max(0, round(rect.x() / self._scale))
        y = max(0, round(rect.y() / self._scale))
        w = min(max(1, round(rect.width() / self._scale)), frame_w - x)
        h = min(max(1, round(rect.height() / self._scale)), frame_h - y)

        try:
            spec = calibrate_color_spec(self._frame, (x, y, w, h))
        except ValueError:
            return

        self.result_spec = spec
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

        b, g, r = (int(c) for c in self._frame[y : y + h, x : x + w].reshape(-1, 3).mean(axis=0))
        luminance = (r * 299 + g * 587 + b * 114) / 1000
        self._swatch_label.setText(f"Sampled color: rgb({r}, {g}, {b})   region {w}×{h}px")
        self._swatch_label.setStyleSheet(
            f"background-color: rgb({r},{g},{b}); padding: 6px; "
            f"color: {'black' if luminance > 128 else 'white'};"
        )
