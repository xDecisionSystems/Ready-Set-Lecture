"""Shared state, coordinate math, and mouse/keyboard interaction for the two
horizontal timeline widgets — SeekBar and WaveformBar. Both need to show the
exact same cut regions/candidate spans/splits/playhead/zoom window and
support the exact same editing gestures (scrub, seek, zoom, drag a candidate
boundary), so that editing on either one is interchangeable and they never
visually disagree with each other. Each subclass only supplies its own
background (a solid bar for SeekBar, waveform peaks for WaveformBar) via
_paint_background(); this base class paints the shared overlay on top and
owns all the event handling.

MainWindow is what actually keeps two instances in sync: every state-setter
call here (set_duration, set_cut_regions, ...) gets made on both widgets, and
their view_changed signals are cross-wired to each other's set_view() so
zooming either one zooms both.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget

from app.core.edit_model import CutRegion

BAR_HEIGHT = 28
CUT_COLOR = QColor(107, 107, 107, 150)  # translucent, so the waveform stays visible under a trimmed region
CUT_BOUNDARY_COLOR = QColor("#e53935")
CANDIDATE_SPAN_COLOR = QColor("#9e9e9e")
CANDIDATE_BOUNDARY_COLOR = QColor("#e6c200")
CANDIDATE_BOUNDARY_SELECTED_COLOR = QColor("#ffffff")
SPLIT_COLOR = QColor("#8b0000")  # a distinct dark red, so it doesn't blend with the brighter cut-boundary red
PLAYHEAD_COLOR = QColor("#ffffff")
_BOUNDARY_HIT_TOLERANCE_PX = 6
_MIN_SPAN_SECONDS = 0.2
_MIN_VIEW_SECONDS = 10.0
_ZOOM_STEP_FACTOR = 0.85  # multiplier per wheel notch when zooming in; its inverse applies when zooming out


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    scrub_requested = Signal(float)
    # marker_id, new_range_start, new_range_end, is_final (False while dragging, True on release)
    candidate_span_changed = Signal(int, float, float, bool)
    # new_view_start, new_view_duration — emitted only when THIS widget's own
    # zoom gesture changes the view, not when set_view() is called
    # programmatically, so cross-wiring two widgets' signals to each other's
    # set_view() can't loop.
    view_changed = Signal(float, float)
    # marker_id (int) of the candidate span just clicked, or None if the
    # click landed outside every span — lets MarkerPanel highlight the
    # matching entry in the marker list.
    candidate_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(BAR_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.duration = 0.0
        self.position = 0.0
        # The visible time window — defaults to the whole video; zooming
        # (mouse wheel) narrows it down to as little as _MIN_VIEW_SECONDS.
        self.view_start = 0.0
        self.view_duration = 0.0
        self.regions: list[CutRegion] = []
        self.candidate_spans: list[tuple[int, float, float]] = []  # (marker_id, start, end)
        self.splits: list[float] = []
        self._dragging = False
        self._dragging_boundary: tuple[int, str] | None = None  # (marker_id, "start" | "end")
        # Persists after a plain click (not just during a drag), so left/right
        # arrow keys can snap that boundary to the current playhead position.
        self._selected_boundary: tuple[int, str] | None = None

    @property
    def view_end(self) -> float:
        return self.view_start + self.view_duration

    def set_duration(self, duration: float) -> None:
        if duration != self.duration:
            # A genuinely new/changed video — reset the zoom to show it all.
            # (This is called repeatedly with the same value whenever markers
            # change too, which must NOT reset an in-progress zoom.)
            self.view_start = 0.0
            self.view_duration = duration
        self.duration = duration
        self.update()

    def set_position(self, position: float) -> None:
        self.position = position
        self.update()

    def set_cut_regions(self, regions: list[CutRegion]) -> None:
        self.regions = regions
        self.update()

    def set_candidate_spans(self, spans: list[tuple[int, float, float]]) -> None:
        self.candidate_spans = spans
        if self._selected_boundary is not None:
            selected_id, _edge = self._selected_boundary
            if not any(mid == selected_id for mid, _s, _e in spans):
                self._selected_boundary = None
        self.update()

    def set_splits(self, splits: list[float]) -> None:
        self.splits = splits
        self.update()

    def set_view(self, view_start: float, view_duration: float) -> None:
        """Applies a view window from elsewhere (the other timeline widget's
        own zoom) without re-emitting view_changed, so the two widgets'
        view_changed -> set_view cross-wiring can't cycle."""
        self.view_start = view_start
        self.view_duration = view_duration
        self.update()

    def _bar_rect(self) -> QRectF:
        return QRectF(0, 0, self.width(), self.height())

    def _x_for_time(self, t: float) -> float:
        """May land outside [0, width] when t is outside the current zoomed
        view — that's fine for drawing (Qt just won't render it), but don't
        feed the result back into further position math."""
        if self.view_duration <= 0:
            return 0.0
        return (t - self.view_start) / self.view_duration * self.width()

    def _time_for_x(self, x: float) -> float:
        """Clamped to the current view window — used for interpreting clicks."""
        if self.view_duration <= 0 or self.width() <= 0:
            return self.view_start
        frac = max(0.0, min(1.0, x / self.width()))
        return self.view_start + frac * self.view_duration

    def _paint_background(self, painter: QPainter, bar: QRectF) -> None:
        """Subclasses draw whatever sits behind the shared overlay: a solid
        color for SeekBar, waveform peaks for WaveformBar."""
        raise NotImplementedError

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = self._bar_rect()

        self._paint_background(painter, bar)

        if self.duration > 0:
            self._paint_overlay(painter, bar)

    def _paint_overlay(self, painter: QPainter, bar: QRectF) -> None:
        for marker_id, span_start, span_end in self.candidate_spans:
            x0 = self._x_for_time(span_start)
            x1 = self._x_for_time(span_end)
            painter.fillRect(QRectF(x0, 0, max(1.0, x1 - x0), bar.height()), CANDIDATE_SPAN_COLOR)
            start_color = (
                CANDIDATE_BOUNDARY_SELECTED_COLOR
                if self._selected_boundary == (marker_id, "start")
                else CANDIDATE_BOUNDARY_COLOR
            )
            end_color = (
                CANDIDATE_BOUNDARY_SELECTED_COLOR
                if self._selected_boundary == (marker_id, "end")
                else CANDIDATE_BOUNDARY_COLOR
            )
            painter.setPen(QPen(start_color, 2))
            painter.drawLine(int(x0), 0, int(x0), int(bar.height()))
            painter.setPen(QPen(end_color, 2))
            painter.drawLine(int(x1), 0, int(x1), int(bar.height()))

        for region in self.regions:
            x0 = self._x_for_time(region.start)
            x1 = self._x_for_time(region.end)
            painter.fillRect(QRectF(x0, 0, max(1.0, x1 - x0), bar.height()), CUT_COLOR)
            painter.setPen(QPen(CUT_BOUNDARY_COLOR, 2))
            painter.drawLine(int(x0), 0, int(x0), int(bar.height()))
            painter.drawLine(int(x1), 0, int(x1), int(bar.height()))

        for split_t in self.splits:
            x = self._x_for_time(split_t)
            painter.setPen(QPen(SPLIT_COLOR, 3))
            painter.drawLine(int(x), 0, int(x), int(bar.height()))

        playhead_x = self._x_for_time(self.position)
        painter.setPen(QPen(PLAYHEAD_COLOR, 3))
        painter.drawLine(int(playhead_x), 0, int(playhead_x), int(bar.height()))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if self.duration <= 0:
            return
        self.setFocus()
        x = event.position().x()
        boundary = self._candidate_boundary_near(x)
        if boundary is not None:
            marker_id, _edge = boundary
            self._dragging_boundary = boundary
            self._selected_boundary = boundary
            self.candidate_selected.emit(marker_id)
            self.update()
            return
        self._selected_boundary = None
        self.candidate_selected.emit(self._candidate_at(x))
        self._dragging = True
        self.scrub_requested.emit(self._time_for_x(x))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        x = event.position().x()
        if self._dragging_boundary is not None:
            self._drag_boundary_to(x, is_final=False)
        elif self._dragging and self.duration > 0:
            self.scrub_requested.emit(self._time_for_x(x))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        x = event.position().x()
        if self._dragging_boundary is not None:
            self._drag_boundary_to(x, is_final=True)
            self._dragging_boundary = None
        elif self._dragging:
            self._dragging = False
            self.seek_requested.emit(self._time_for_x(x))

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt override)
        notches = event.angleDelta().y() / 120.0
        if notches == 0:
            return
        # Scroll up = zoom in (smaller window); scroll down = zoom out.
        self._zoom_by_notches(notches)
        event.accept()

    def zoom_in(self) -> None:
        self._zoom_by_notches(1.0)

    def zoom_out(self) -> None:
        self._zoom_by_notches(-1.0)

    def _zoom_by_notches(self, notches: float) -> None:
        if self.duration <= 0:
            return
        new_view_duration = self.view_duration * (_ZOOM_STEP_FACTOR**notches)
        new_view_duration = max(_MIN_VIEW_SECONDS, min(self.duration, new_view_duration))
        self._set_view_centered_on_playhead(new_view_duration)

    def _set_view_centered_on_playhead(self, view_duration: float) -> None:
        half = view_duration / 2
        start = self.position - half
        end = self.position + half
        if start < 0:
            start = 0.0
            end = min(self.duration, view_duration)
        elif end > self.duration:
            end = self.duration
            start = max(0.0, self.duration - view_duration)
        self.view_start = start
        self.view_duration = end - start
        self.update()
        self.view_changed.emit(self.view_start, self.view_duration)

    def _candidate_boundary_near(self, x: float) -> tuple[int, str] | None:
        best: tuple[int, str] | None = None
        best_dist = _BOUNDARY_HIT_TOLERANCE_PX + 1
        for marker_id, start, end in self.candidate_spans:
            for edge, t in (("start", start), ("end", end)):
                dist = abs(self._x_for_time(t) - x)
                if dist < best_dist:
                    best = (marker_id, edge)
                    best_dist = dist
        return best

    def _candidate_at(self, x: float) -> int | None:
        """The candidate span (marker id) whose body — not just a boundary —
        contains x, if any. Used so clicking anywhere on a marker-break
        region can highlight it in the marker list, not just its edges."""
        t = self._time_for_x(x)
        for marker_id, start, end in self.candidate_spans:
            if start <= t <= end:
                return marker_id
        return None

    def _drag_boundary_to(self, x: float, is_final: bool) -> None:
        marker_id, edge = self._dragging_boundary
        self._set_boundary_time(marker_id, edge, self._time_for_x(x), is_final)

    def _set_boundary_time(self, marker_id: int, edge: str, new_time: float, is_final: bool) -> None:
        for i, (mid, start, end) in enumerate(self.candidate_spans):
            if mid != marker_id:
                continue
            if edge == "start":
                start = max(0.0, min(new_time, end - _MIN_SPAN_SECONDS))
            else:
                end = min(self.duration, max(new_time, start + _MIN_SPAN_SECONDS))
            self.candidate_spans[i] = (mid, start, end)
            self.update()
            self.candidate_span_changed.emit(mid, start, end, is_final)
            return

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt override)
        if self._selected_boundary is None or self.duration <= 0:
            super().keyPressEvent(event)
            return
        if event.key() not in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            super().keyPressEvent(event)
            return

        marker_id, edge = self._selected_boundary
        current = next(
            (start if edge == "start" else end for mid, start, end in self.candidate_spans if mid == marker_id),
            None,
        )
        if current is None:
            return

        # The playhead (white line) must actually be on the side matching the
        # key pressed — Left only if it's to the left of the boundary, Right
        # only if it's to the right — so the key both confirms the direction
        # and snaps the boundary exactly to wherever playback is paused/scrubbed to.
        if event.key() == Qt.Key.Key_Left and self.position < current:
            self._set_boundary_time(marker_id, edge, self.position, is_final=True)
        elif event.key() == Qt.Key.Key_Right and self.position > current:
            self._set_boundary_time(marker_id, edge, self.position, is_final=True)
