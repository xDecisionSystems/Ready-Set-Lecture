"""A folder dropdown whose entries carry a clickable star: filled for favorites, hollow for a one-off folder."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QComboBox

from app.core.settings import same_folder

_PATH_ROLE = Qt.ItemDataRole.UserRole
_STAR_ROLE = Qt.ItemDataRole.UserRole + 1  # True = favorite, False = one-off folder, None = "Other…" (no star)
_ICON_PX = 16
_STAR_HIT_WIDTH = 30  # a click this close to the row's left edge hits the star, not the row


def _star_polygon(radius: float, inner: float) -> QPolygonF:
    points = []
    for tip in range(10):
        angle = -math.pi / 2 + tip * math.pi / 5
        r = radius if tip % 2 == 0 else inner
        points.append(QPointF(_ICON_PX / 2 + r * math.cos(angle), _ICON_PX / 2 + r * math.sin(angle)))
    return QPolygonF(points)


def _icon(kind: bool | None) -> QIcon:
    """Filled gold star (True), hollow grey star (False), or an empty spacer that keeps 'Other…' aligned (None)."""
    ratio = 2
    pixmap = QPixmap(_ICON_PX * ratio, _ICON_PX * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    if kind is not None:
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        star = _star_polygon(7.2, 3.0)
        if kind:
            painter.setPen(QPen(QColor("#b8860b"), 1))
            painter.setBrush(QColor("#f5b301"))
        else:
            painter.setPen(QPen(QColor("#8a8f98"), 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(star)
        painter.end()
    return QIcon(pixmap)


class SaveLocationCombo(QComboBox):
    """Favorite folders, then the current folder if it isn't one, then 'Other…'.

    Clicking the star at the left of a folder row in the open list toggles its favorite state without selecting it.
    """

    location_chosen = Signal(object)  # a folder (Path) was picked from the list
    other_chosen = Signal()  # 'Other…' was picked
    star_clicked = Signal(object)  # the star beside a folder (Path) was clicked

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(28)
        self.setIconSize(QSize(_ICON_PX, _ICON_PX))
        self._icons = {True: _icon(True), False: _icon(False), None: _icon(None)}
        self.activated.connect(self._activated)
        self.view().viewport().installEventFilter(self)

    def set_locations(self, favorites: list[Path], current: Path | None) -> None:
        """Rebuild the list and select *current*. Safe to call while the list is open."""
        self.blockSignals(True)
        self.clear()
        for folder in favorites:
            self._add(folder, True)
        if current is not None and not any(same_folder(folder, current) for folder in favorites):
            self._add(current, False)
        self.addItem(self._icons[None], "Other…")
        for row in range(self.count()):
            folder = self.itemData(row, _PATH_ROLE)
            if current is not None and folder is not None and same_folder(folder, current):
                self.setCurrentIndex(row)
                break
        self.blockSignals(False)
        self.setToolTip(str(current or ""))

    def is_starred(self, row: int) -> bool | None:
        return self.itemData(row, _STAR_ROLE)

    def _add(self, folder: Path, starred: bool) -> None:
        self.addItem(self._icons[starred], str(folder))
        row = self.count() - 1
        self.setItemData(row, folder, _PATH_ROLE)
        self.setItemData(row, starred, _STAR_ROLE)
        self.setItemData(row, f"{folder}\n" + ("Starred: click the star to remove it from your favorites." if starred
                                               else "Click the star to add it to your favorites."), Qt.ItemDataRole.ToolTipRole)

    def _activated(self, row: int) -> None:
        folder = self.itemData(row, _PATH_ROLE)
        if folder is None:
            self.other_chosen.emit()
        else:
            self.location_chosen.emit(folder)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is self.view().viewport() and event.type() in (
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick,
        ) and event.button() == Qt.MouseButton.LeftButton:
            position = event.position().toPoint()
            index = self.view().indexAt(position)
            if index.isValid() and self.is_starred(index.row()) is not None and position.x() - self.view().visualRect(index).left() <= _STAR_HIT_WIDTH:
                if event.type() == QEvent.Type.MouseButtonRelease:
                    self.star_clicked.emit(self.itemData(index.row(), _PATH_ROLE))
                return True  # swallow it: the row must not be selected and the list must stay open
        return super().eventFilter(watched, event)
