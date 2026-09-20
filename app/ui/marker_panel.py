"""Marker list + classification controls.

Shows every marker in the current EditProject (color-marker candidates and
manually/already-classified ones), lets the user add new markers at the
current playback position, classify a candidate via right-click, and delete
markers. Mutates the EditProject in place and emits project_changed so
MainWindow can re-render the timeline and autosave.

The add/delete buttons are created here (so their logic stays with the rest
of this panel's marker-editing code) but are NOT added to this widget's own
layout — MainWindow places them under the video instead, keeping the right
panel to just the marker list.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.edit_model import CANDIDATE_BREAK_WINDOW_SECONDS, EditProject, MarkerKind
from app.ui.format_utils import format_time

_LABELS = {
    MarkerKind.CANDIDATE: "Color candidate",
    MarkerKind.CUT_START: "Cut start",
    MarkerKind.CUT_END: "Cut end",
    MarkerKind.SPLIT: "Split point",
}

class MarkerPanel(QWidget):
    seek_requested = Signal(float)
    project_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project: EditProject | None = None
        self.get_current_position: Callable[[], float] | None = None
        self.get_duration: Callable[[], float] | None = None
        self._highlighted_marker_id: int | None = None

        self.add_candidate_break_button = QPushButton("+ Marker Break")
        self.add_candidate_break_button.setToolTip(
            "Adds a 30-second candidate span starting at the current playback "
            "position, as if the colored paper had been held up there — for spots "
            "where you forgot to hold it up. Classify it like any other candidate."
        )
        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self.list_widget)

        self.add_candidate_break_button.clicked.connect(self._add_candidate_break)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)

    def set_project(self, project: EditProject) -> None:
        self.project = project
        self.refresh()

    def set_highlighted_marker(self, marker_id: int | None) -> None:
        """Bolds the given marker's row (e.g. when its span is clicked on
        the seek bar or waveform), so it's obvious which list entry a
        marker-break region on the timeline corresponds to."""
        if marker_id == self._highlighted_marker_id:
            return
        self._highlighted_marker_id = marker_id
        self.refresh()

    def refresh(self) -> None:
        self.list_widget.clear()
        if not self.project:
            return
        highlighted_item: QListWidgetItem | None = None
        for marker in sorted(self.project.markers, key=lambda m: m.timestamp):
            label = f"{format_time(marker.timestamp):>8}   {_LABELS[marker.kind]}"
            if marker.source == "color":
                label += "  [color]"

            item = QListWidgetItem()
            item.setData(1000, marker.id)
            self.list_widget.addItem(item)

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            label_widget = QLabel(label)
            if marker.id == self._highlighted_marker_id:
                font = label_widget.font()
                font.setBold(True)
                label_widget.setFont(font)
                highlighted_item = item
            row_layout.addWidget(label_widget, stretch=1)
            trash_button = QPushButton("🗑")
            trash_button.setFlat(True)
            trash_button.setFixedWidth(28)
            trash_button.setToolTip("Delete this marker")
            trash_button.clicked.connect(lambda checked=False, mid=marker.id: self._delete(mid))
            row_layout.addWidget(trash_button)
            item.setSizeHint(row.sizeHint())
            self.list_widget.setItemWidget(item, row)

        if highlighted_item is not None:
            self.list_widget.scrollToItem(highlighted_item)

    def _add_candidate_break(self) -> None:
        if not self.project or not self.get_current_position:
            return
        start = self.get_current_position()
        duration = self.get_duration() if self.get_duration else None
        end = start + CANDIDATE_BREAK_WINDOW_SECONDS
        if duration:
            end = min(end, duration)
        self.project.checkpoint()
        self.project.add_color_candidates([(start, end)])
        self.refresh()
        self.project_changed.emit()

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        if not self.project:
            return
        marker_id = item.data(1000)
        marker = next((m for m in self.project.markers if m.id == marker_id), None)
        if marker:
            self.seek_requested.emit(marker.timestamp)

    def _on_context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if not item or not self.project:
            return
        marker_id = item.data(1000)
        marker = next((m for m in self.project.markers if m.id == marker_id), None)
        if not marker:
            return

        menu = QMenu(self)
        for kind in (MarkerKind.CUT_START, MarkerKind.CUT_END, MarkerKind.SPLIT):
            if kind is marker.kind:
                continue
            action = menu.addAction(f"Classify as {_LABELS[kind]}")
            action.triggered.connect(lambda checked=False, k=kind: self._classify(marker_id, k))
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(lambda: self._delete(marker_id))
        menu.exec(self.list_widget.viewport().mapToGlobal(pos))

    def _classify(self, marker_id: int, kind: MarkerKind) -> None:
        if not self.project:
            return
        self.project.checkpoint()
        self.project.set_marker_kind(marker_id, kind)
        self.refresh()
        self.project_changed.emit()

    def _delete(self, marker_id: int) -> None:
        if not self.project:
            return
        self.project.checkpoint()
        self.project.remove_marker(marker_id)
        self.refresh()
        self.project_changed.emit()
