"""Primary seek bar: a thick, custom-painted progress bar that greys out
cut regions (with a red line at each cut boundary) so it's obvious while
scrubbing which parts of the video will actually survive the export.

Supports zooming (mouse wheel) into a sub-range of the video, centered on
the playhead, down to a 10-second window and back out to the whole video.

All the state/interaction logic (zoom, drag, coordinate math) lives in
TimelineWidget, shared with WaveformBar, so editing gestures behave
identically on either widget.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter

from app.ui.timeline_widget import TimelineWidget

_KEPT_COLOR = QColor("#4a90d9")


class SeekBar(TimelineWidget):
    def _paint_background(self, painter: QPainter, bar: QRectF) -> None:
        painter.fillRect(bar, _KEPT_COLOR)
