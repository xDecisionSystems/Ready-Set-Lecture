"""A picture with a draw-and-adjust selection box, shared by the screen and camera area pickers.

Drag to draw a box, grab its edges and corners to resize it (or drag inside to move it), then click the
check (or press Enter) to confirm, or the X (or Esc) to cancel.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QRegion
from PySide6.QtWidgets import QWidget

Rect = tuple[int, int, int, int]

MIN_SELECTION_PX = 16  # smaller drags are treated as a stray click, and a box can't be resized below this
HANDLE_TOLERANCE_PX = 8  # how close to an edge or corner still grabs it
BUTTON_SIZE = 30
BUTTON_GAP = 8
BUTTON_INSET = 12  # how far the buttons sit in from the box's right edge, clear of the corner's grab zone
OUTLINE_GREEN = QColor("#43c76b")
CANCEL_RED = QColor("#b94444")


def drag_rect(start: QPoint, end: QPoint) -> QRect:
    """The rectangle spanned by a drag, exactly as wide and tall as the drag (QRect(p1, p2) would add a pixel)."""
    return QRect(QPoint(min(start.x(), end.x()), min(start.y(), end.y())), QSize(abs(end.x() - start.x()), abs(end.y() - start.y())))


def hit_zone(rect: Rect, pos: tuple[int, int], tolerance: int = HANDLE_TOLERANCE_PX) -> str:
    """What a point grabs on *rect*: 'topleft'/'top'/... for a corner or edge, 'move' inside it, 'new' outside."""
    x, y, width, height = rect
    px, py = pos
    left, top, right, bottom = x, y, x + width, y + height
    within_x = left - tolerance <= px <= right + tolerance
    within_y = top - tolerance <= py <= bottom + tolerance
    horizontal = "left" if abs(px - left) <= tolerance and within_y else "right" if abs(px - right) <= tolerance and within_y else ""
    vertical = "top" if abs(py - top) <= tolerance and within_x else "bottom" if abs(py - bottom) <= tolerance and within_x else ""
    if vertical or horizontal:
        return vertical + horizontal
    return "move" if left <= px <= right and top <= py <= bottom else "new"


def resize_selection(rect: Rect, zone: str, dx: int, dy: int, bounds: tuple[int, int], min_size: int = MIN_SELECTION_PX) -> Rect:
    """Drag the *zone* edge or corner of *rect* by (dx, dy), staying inside *bounds* and at least *min_size* wide and tall."""
    x, y, width, height = rect
    left, top, right, bottom = x, y, x + width, y + height
    if "left" in zone:
        left = max(0, min(left + dx, right - min_size))
    if "right" in zone:
        right = min(bounds[0], max(right + dx, left + min_size))
    if "top" in zone:
        top = max(0, min(top + dy, bottom - min_size))
    if "bottom" in zone:
        bottom = min(bounds[1], max(bottom + dy, top + min_size))
    return left, top, right - left, bottom - top


def move_selection(rect: Rect, dx: int, dy: int, bounds: tuple[int, int]) -> Rect:
    """Slide *rect* by (dx, dy), stopping at the edges of *bounds*."""
    x, y, width, height = rect
    return max(0, min(x + dx, bounds[0] - width)), max(0, min(y + dy, bounds[1] - height)), width, height


def action_button_rects(
    selection: Rect, bounds: tuple[int, int], size: int = BUTTON_SIZE, gap: int = BUTTON_GAP, inset: int = BUTTON_INSET,
) -> tuple[Rect, Rect]:
    """(cancel, confirm) button rects at the selection's lower right: just below it, or inside it if there is no room."""
    x, y, width, height = selection
    right, bottom = x + width, y + height
    top = bottom + gap if bottom + gap + size <= bounds[1] else bottom - gap - size
    confirm_x = right - inset - size
    cancel_x = confirm_x - gap - size
    if cancel_x < 0:
        confirm_x, cancel_x = confirm_x - cancel_x, 0
    return (cancel_x, top, size, size), (confirm_x, top, size, size)


_ZONE_CURSORS = {
    "new": Qt.CursorShape.CrossCursor, "move": Qt.CursorShape.SizeAllCursor,
    "left": Qt.CursorShape.SizeHorCursor, "right": Qt.CursorShape.SizeHorCursor,
    "top": Qt.CursorShape.SizeVerCursor, "bottom": Qt.CursorShape.SizeVerCursor,
    "topleft": Qt.CursorShape.SizeFDiagCursor, "bottomright": Qt.CursorShape.SizeFDiagCursor,
    "topright": Qt.CursorShape.SizeBDiagCursor, "bottomleft": Qt.CursorShape.SizeBDiagCursor,
    "confirm": Qt.CursorShape.PointingHandCursor, "cancel": Qt.CursorShape.PointingHandCursor,
}


class SelectionCanvas(QWidget):
    """Shows a picture stretched over the widget (dimmed outside the box) and lets the user draw and adjust the box.

    *size_of* turns a box (x, y, width, height) and this widget's (width, height) into the real-pixel size shown
    in the size label. set_picture() may be called repeatedly, e.g. for every frame of a live camera.
    """

    confirmed = Signal()
    cancelled = Signal()

    def __init__(self, size_of: Callable[[Rect, tuple[int, int]], tuple[int, int]], placeholder: str = "", parent=None) -> None:
        super().__init__(parent)
        self._size_of = size_of
        self._placeholder = placeholder
        self._picture = QImage()
        self._selection: QRect | None = None
        self._mode: str | None = None  # 'new', 'move' or an edge/corner name while a drag is in progress
        self._press = QPoint()
        self._press_selection: QRect | None = None
        self._hover = "new"
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_picture(self, picture: QImage) -> None:
        self._picture = picture
        self.update()

    def selection(self) -> QRect | None:
        return None if self._selection is None else QRect(self._selection)

    def has_valid_selection(self) -> bool:
        return self._is_big_enough(self._selection)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.setFocus()

    def _bounds(self) -> tuple[int, int]:
        return self.width(), self.height()

    @staticmethod
    def _as_tuple(rect: QRect) -> Rect:
        return rect.x(), rect.y(), rect.width(), rect.height()

    @staticmethod
    def _is_big_enough(selection: QRect | None) -> bool:
        return selection is not None and selection.width() >= MIN_SELECTION_PX and selection.height() >= MIN_SELECTION_PX

    def _button_rects(self) -> tuple[QRect, QRect] | None:
        """(cancel, confirm) while a box is shown and not being dragged."""
        if self._selection is None or self._mode is not None:
            return None
        cancel, confirm = action_button_rects(self._as_tuple(self._selection), self._bounds())
        return QRect(*cancel), QRect(*confirm)

    def _zone_at(self, pos: QPoint) -> str:
        buttons = self._button_rects()
        if buttons is not None:
            if buttons[1].contains(pos):
                return "confirm"
            if buttons[0].contains(pos):
                return "cancel"
        if self._selection is None:
            return "new"
        return hit_zone(self._as_tuple(self._selection), (pos.x(), pos.y()))

    def _update_hover(self, pos: QPoint) -> None:
        zone = self._zone_at(pos)
        if zone == self._hover:
            return
        highlight_changed = "confirm" in (zone, self._hover) or "cancel" in (zone, self._hover)
        self._hover = zone
        self.setCursor(_ZONE_CURSORS[zone])
        if highlight_changed:
            self.update()

    def _confirm(self) -> None:
        if self._is_big_enough(self._selection):
            self.confirmed.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        zone = self._zone_at(pos)
        if zone == "confirm":
            self._confirm()
        elif zone == "cancel":
            self.cancelled.emit()
        else:
            self._mode, self._press = zone, pos
            self._press_selection = QRect(self._selection) if self._selection is not None else None
            if zone == "new":
                self._set_selection(drag_rect(pos, pos))
            else:
                self.update()  # hide the buttons while dragging

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        pos = event.position().toPoint()
        if self._mode is None:
            self._update_hover(pos)
        elif self._mode == "new":
            self._set_selection(drag_rect(self._press, pos))
        elif self._press_selection is not None:
            start = self._as_tuple(self._press_selection)
            dx, dy = pos.x() - self._press.x(), pos.y() - self._press.y()
            if self._mode == "move":
                self._set_selection(QRect(*move_selection(start, dx, dy, self._bounds())))
            else:
                self._set_selection(QRect(*resize_selection(start, self._mode, dx, dy, self._bounds())))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton or self._mode is None:
            return
        was_new, self._mode = self._mode == "new", None
        if was_new and not self._is_big_enough(self._selection):
            # A stray click: keep whatever box was there before it.
            self._set_selection(self._press_selection)
        self._hover = ""
        self._update_hover(event.position().toPoint())
        self.update()

    def _set_selection(self, selection: QRect | None) -> None:
        previous = self._selection
        self._selection = selection.intersected(self.rect()) if selection is not None else None
        dirty = self._selection if previous is None else (previous if self._selection is None else previous.united(self._selection))
        # Generous margin: the size label, handles and buttons sit outside the selection.
        self.update(self.rect() if dirty is None else dirty.adjusted(-150, -50, 150, 90))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        selection = self._selection if self._selection is not None and not self._selection.isEmpty() else None
        if self._picture.isNull():
            painter.fillRect(self.rect(), QColor(20, 24, 28))
            if self._placeholder:
                painter.setPen(Qt.GlobalColor.white)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
        else:
            painter.drawImage(self.rect(), self._picture)
        # Dim everything except the box, which stays as bright as the picture.
        outside = QRegion(self.rect()) if selection is None else QRegion(self.rect()).subtracted(QRegion(selection))
        painter.setClipRegion(outside, Qt.ClipOperation.IntersectClip)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        painter.setClipping(False)
        if selection is None:
            self._draw_hint(painter, "Drag to select the area to record.  Esc to cancel.")
            return
        painter.setPen(QPen(OUTLINE_GREEN, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(selection)
        self._draw_size_label(painter, selection)
        buttons = self._button_rects()
        if buttons is not None:
            self._draw_handles(painter, selection)
            self._draw_button(painter, buttons[0], CANCEL_RED, "cancel")
            self._draw_button(painter, buttons[1], OUTLINE_GREEN.darker(115), "confirm")
            self._draw_hint(painter, "Drag edges or corners to adjust.  Enter to confirm, Esc to cancel.")

    def _draw_hint(self, painter: QPainter, text: str) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 24, 28, 200))
        box = QRect(0, 0, painter.fontMetrics().horizontalAdvance(text) + 28, 30)
        box.moveCenter(QPoint(self.width() // 2, 42))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_size_label(self, painter: QPainter, selection: QRect) -> None:
        width, height = self._size_of(self._as_tuple(selection), self._bounds())
        text = f"{width} × {height}"
        label = QRect(0, 0, painter.fontMetrics().horizontalAdvance(text) + 14, 22)
        label.moveTopLeft(selection.topLeft() + QPoint(0, -26 if selection.y() >= 28 else 4))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 24, 28, 220))
        painter.drawRoundedRect(label, 4, 4)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(label, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_handles(self, painter: QPainter, selection: QRect) -> None:
        left, top, right, bottom = selection.x(), selection.y(), selection.x() + selection.width(), selection.y() + selection.height()
        middle_x, middle_y = (left + right) // 2, (top + bottom) // 2
        points = [(left, top), (right, top), (left, bottom), (right, bottom)]
        if selection.width() >= 48:
            points += [(middle_x, top), (middle_x, bottom)]
        if selection.height() >= 48:
            points += [(left, middle_y), (right, middle_y)]
        painter.setPen(QPen(OUTLINE_GREEN, 2))
        painter.setBrush(Qt.GlobalColor.white)
        for x, y in points:
            painter.drawRect(x - 4, y - 4, 8, 8)

    def _draw_button(self, painter: QPainter, rect: QRect, color: QColor, kind: str) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color.lighter(125) if self._hover == kind else color)
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QPen(Qt.GlobalColor.white, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        cx, cy = rect.center().x(), rect.center().y()
        if kind == "confirm":
            painter.drawPolyline([QPoint(cx - 7, cy), QPoint(cx - 2, cy + 5), QPoint(cx + 7, cy - 5)])
        else:
            painter.drawLine(cx - 6, cy - 6, cx + 6, cy + 6)
            painter.drawLine(cx - 6, cy + 6, cx + 6, cy - 6)
