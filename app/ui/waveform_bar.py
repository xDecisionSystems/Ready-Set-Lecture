"""Waveform display, stacked directly under SeekBar.

Draws the audio amplitude envelope as its background, then the exact same
cut-region/candidate-span/split/playhead overlay SeekBar draws (inherited
from TimelineWidget) — so the two always show identical markers, and
dragging a boundary or scrubbing works the same on either one.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter

from app.ui.timeline_widget import TimelineWidget

_BACKGROUND_COLOR = QColor("#1c2b3a")
_WAVEFORM_COLOR = QColor("#7fc4e8")
_PLACEHOLDER_TEXT_COLOR = QColor("#7f97a8")
_HEIGHT = 60  # taller than SeekBar's thin bar — amplitude needs room to actually be legible


class WaveformBar(TimelineWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_HEIGHT)
        self._peaks: np.ndarray | None = None  # (N, 2) of (min, max) in [-1, 1]
        self._loading = False

    def set_waveform(self, peaks: np.ndarray | None) -> None:
        self._peaks = peaks
        self._loading = False
        self.update()

    def set_loading(self) -> None:
        self._peaks = None
        self._loading = True
        self.update()

    def _paint_background(self, painter: QPainter, bar: QRectF) -> None:
        painter.fillRect(bar, _BACKGROUND_COLOR)

        if self._peaks is None or self.duration <= 0:
            if self._loading:
                painter.setPen(_PLACEHOLDER_TEXT_COLOR)
                painter.drawText(bar, Qt.AlignmentFlag.AlignCenter, "Loading waveform…")
            return

        width = int(bar.width())
        height = bar.height()
        mid_y = height / 2
        n_peaks = len(self._peaks)

        painter.setPen(_WAVEFORM_COLOR)
        # One vertical line per pixel column: map that column's time range
        # (given the current zoom) to the peak buckets it covers, and draw
        # the tallest excursion among them — keeps the shape stable while
        # zoomed in or out instead of just picking one nearest sample.
        for px in range(width):
            t0 = self._time_for_x(px)
            t1 = self._time_for_x(px + 1)
            i0 = max(0, min(n_peaks - 1, int(t0 / self.duration * n_peaks)))
            i1 = max(i0 + 1, min(n_peaks, int(t1 / self.duration * n_peaks) + 1))
            chunk = self._peaks[i0:i1]
            lo = float(chunk[:, 0].min())
            hi = float(chunk[:, 1].max())
            y0 = mid_y - hi * mid_y
            y1 = mid_y - lo * mid_y
            painter.drawLine(px, int(y0), px, int(max(y1, y0 + 1)))
