"""Main window: playback shell (milestone 1) plus the timeline/marker editing
UI and color-marker scan integration (milestone 2-3)."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.core import settings as app_settings
from app.core.color_scanner import ColorScanWorker, ColorSpec, grab_frame
from app.core.edit_model import EditProject, MarkerKind
from app.core.exporter import ExportWorker
from app.core.waveform import WaveformWorker
from app.ui.color_picker_dialog import ColorRegionDialog
from app.ui.export_dialog import ExportDialog
from app.ui.format_utils import format_time
from app.ui.marker_panel import MarkerPanel
from app.ui.seek_bar import SeekBar
from app.ui.video_widget import VideoWidget
from app.ui.waveform_bar import WaveformBar

SPEEDS = (1.0, 1.5, 2.0, 3.0, 4.0)
SMALL_SKIP_SECONDS = 10
LARGE_SKIP_SECONDS = 60
# Landing exactly on a span's end could still read as "inside" it on the next
# position update (float/seek imprecision), re-triggering the skip in a loop.
CANDIDATE_SKIP_BUFFER_SECONDS = 0.15
# Playback naturally reports positions in discrete steps, not continuously,
# so trigger the loop-back slightly before the exact edge rather than risk
# overshooting past it and never quite matching.
LOOP_TRIGGER_EPSILON_SECONDS = 0.1
# QScrollBar's range/value are ints; scaling seconds up by this factor keeps
# millisecond precision for panning instead of snapping to whole seconds.
_SCROLLBAR_SCALE = 1000


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Ready, Set, Lecture!")

        self.video = VideoWidget(self)
        self.project: EditProject | None = None
        self._duration = 0.0
        self._position = 0.0
        self._candidate_spans: list[tuple[int, float, float]] = []
        self._scan_worker: ColorScanWorker | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._scan_start_time: float = 0.0
        self._scan_eta_ema: float | None = None
        self._export_worker: ExportWorker | None = None
        self._export_progress_dialog: QProgressDialog | None = None
        self._waveform_worker: WaveformWorker | None = None
        self._drag_in_progress = False  # one undo checkpoint per drag gesture, not per mouseMoveEvent

        self._build_ui()
        self._wire_signals()
        self._size_to_screen(fraction=0.66)

        # Ctrl+Z / Ctrl+Shift+Z specifically (not QKeySequence.StandardKey.Undo/Redo,
        # whose platform-default Redo binding is Ctrl+Y on Windows, not Shift+Ctrl+Z).
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.activated.connect(self._undo)
        self._redo_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        self._redo_shortcut.activated.connect(self._redo)

    def _size_to_screen(self, fraction: float) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        available = screen.availableGeometry()
        width = int(available.width() * fraction)
        height = int(available.height() * fraction)
        self.resize(width, height)
        self.move(available.center() - self.rect().center())

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Built early so its buttons exist to place in the row layouts below —
        # only its marker list actually lives in the right-hand panel.
        self.marker_panel = MarkerPanel(self)
        self.marker_panel.get_current_position = lambda: self._position
        self.marker_panel.get_duration = lambda: self._duration

        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.video, stretch=1)

        controls = QWidget(self)
        controls_layout = QVBoxLayout(controls)

        seek_row = QHBoxLayout()
        self.time_label = QLabel("0:00 / 0:00")
        self.seek_bar = SeekBar(self)
        self.waveform_bar = WaveformBar(self)
        # Stacked in their own column (not directly in seek_row) so both
        # widgets share exactly the same width/x-mapping as each other,
        # independent of the zoom buttons and time label alongside them.
        timeline_column = QVBoxLayout()
        timeline_column.setSpacing(2)
        timeline_column.addWidget(self.seek_bar)
        timeline_column.addWidget(self.waveform_bar)
        self.timeline_scrollbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.timeline_scrollbar.setToolTip(
            "Pans the zoomed-in view without moving the playhead — repositions "
            "both the seek bar and the waveform together."
        )
        self.timeline_scrollbar.setRange(0, 0)  # nothing to pan until a video is loaded and/or zoomed in
        self.timeline_scrollbar.setEnabled(False)
        timeline_column.addWidget(self.timeline_scrollbar)
        self.zoom_in_button = QPushButton("🔍+")
        self.zoom_in_button.setToolTip("Zoom in on the timeline, centered on the playhead")
        self.zoom_out_button = QPushButton("🔍−")
        self.zoom_out_button.setToolTip("Zoom out on the timeline, centered on the playhead")
        seek_row.addLayout(timeline_column, stretch=1)
        seek_row.addWidget(self.zoom_in_button)
        seek_row.addWidget(self.zoom_out_button)
        seek_row.addWidget(self.time_label)
        controls_layout.addLayout(seek_row)

        # Row 1: playback transport — the controls used constantly while watching.
        playback_row = QHBoxLayout()
        self.back_1m_button = QPushButton("« 1m")
        self.back_10s_button = QPushButton("« 10s")
        self.play_button = QPushButton("Play/Pause")
        self.play_button.setCheckable(True)
        self.forward_10s_button = QPushButton("10s »")
        self.forward_1m_button = QPushButton("1m »")
        for b in (
            self.back_1m_button,
            self.back_10s_button,
            self.play_button,
            self.forward_10s_button,
            self.forward_1m_button,
        ):
            playback_row.addWidget(b)

        playback_row.addStretch(1)

        self.speed_buttons: dict[float, QPushButton] = {}
        for speed in SPEEDS:
            label = "1x" if speed == 1.0 else f"{speed:g}x"
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(speed == 1.0)
            self.speed_buttons[speed] = btn
            playback_row.addWidget(btn)
        controls_layout.addLayout(playback_row)

        # Row 2: project/marker-editing actions — used occasionally, not while watching.
        editing_row = QHBoxLayout()
        self.open_button = QPushButton("Open…")
        self.set_region_button = QPushButton("Set Marker Region…")
        self.set_region_button.setToolTip(
            "Pauses on the current frame and lets you drag a box around the colored "
            "paper marker, to calibrate where to look and what color to match."
        )
        self.set_region_button.setEnabled(False)
        self.scan_button = QPushButton("Scan")
        self.scan_button.setToolTip("Scans the video for holds of the calibrated color marker.")
        self.scan_button.setEnabled(False)
        self.export_button = QPushButton("Export…")
        self.export_button.setEnabled(False)
        self.add_split_button = QPushButton("+ Split")
        self.add_split_button.setToolTip("Adds a split point at the current playback position.")
        for b in (
            self.open_button,
            self.set_region_button,
            self.scan_button,
            self.export_button,
            self.add_split_button,
            self.marker_panel.add_candidate_break_button,
        ):
            editing_row.addWidget(b)
        controls_layout.addLayout(editing_row)

        left_layout.addWidget(controls)
        splitter.addWidget(left)

        self.marker_panel.setMinimumWidth(260)
        splitter.addWidget(self.marker_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)

    def _wire_signals(self) -> None:
        self.open_button.clicked.connect(self._open_file)
        self.set_region_button.clicked.connect(self._set_marker_region)
        self.scan_button.clicked.connect(self._scan_for_color)
        self.export_button.clicked.connect(self._export)
        self.add_split_button.clicked.connect(self._add_split_here)
        self.play_button.clicked.connect(self.video.toggle_pause)
        self.back_1m_button.clicked.connect(lambda: self.video.seek_relative(-LARGE_SKIP_SECONDS))
        self.back_10s_button.clicked.connect(lambda: self.video.seek_relative(-SMALL_SKIP_SECONDS))
        self.forward_10s_button.clicked.connect(lambda: self.video.seek_relative(SMALL_SKIP_SECONDS))
        self.forward_1m_button.clicked.connect(lambda: self.video.seek_relative(LARGE_SKIP_SECONDS))

        for speed, btn in self.speed_buttons.items():
            btn.clicked.connect(lambda checked, s=speed: self._set_speed(s))

        # Either the seek bar or the waveform can drive zoom — both need to
        # end up at the same view window, so their view_changed signals are
        # cross-wired to each other's set_view (which doesn't re-emit
        # view_changed, so this can't loop). The zoom buttons only need to
        # act on one of them; the cross-wiring propagates it to the other.
        self.zoom_in_button.clicked.connect(self.seek_bar.zoom_in)
        self.zoom_out_button.clicked.connect(self.seek_bar.zoom_out)
        self.seek_bar.view_changed.connect(self.waveform_bar.set_view)
        self.waveform_bar.view_changed.connect(self.seek_bar.set_view)

        # The scrollbar pans the shared view (repositioning which window of
        # the video is visible) without touching the playhead. It also needs
        # to reflect view changes that come from zooming instead of dragging
        # it directly, so it's kept in sync with both view_changed signals.
        self.timeline_scrollbar.valueChanged.connect(self._on_scrollbar_moved)
        self.seek_bar.view_changed.connect(self._sync_scrollbar_from_view)
        self.waveform_bar.view_changed.connect(self._sync_scrollbar_from_view)

        self.video.position_changed.connect(self._on_position_changed)
        self.video.duration_changed.connect(self._on_duration_changed)
        self.video.pause_changed.connect(self._on_pause_changed)

        # The waveform supports the exact same editing gestures as the seek
        # bar (scrub, seek, drag a candidate boundary) by routing to the
        # same handlers — see TimelineWidget.
        for timeline in (self.seek_bar, self.waveform_bar):
            timeline.seek_requested.connect(self.video.seek_absolute)
            timeline.scrub_requested.connect(self.video.seek_preview)
            timeline.candidate_span_changed.connect(self._on_candidate_span_changed)
            timeline.candidate_selected.connect(self.marker_panel.set_highlighted_marker)

        self.marker_panel.seek_requested.connect(self.video.seek_absolute)
        self.marker_panel.project_changed.connect(self._on_project_changed)

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open lecture video",
            "",
            "Video files (*.mp4 *.mov *.mkv *.avi *.m4v);;All files (*.*)",
        )
        if not path:
            return

        self.video.load(path)
        self.set_region_button.setEnabled(True)
        self.export_button.setEnabled(True)

        sidecar = EditProject.existing_sidecar_for(path)
        if sidecar is not None:
            try:
                self.project = EditProject.load(sidecar)
            except Exception:
                self.project = EditProject(video_path=path)
        else:
            self.project = EditProject(video_path=path)

        if self.project.color_spec is None:
            # The camera rig and paper location are typically reused across
            # many recordings, so pre-seed from the last calibration instead
            # of making the user redraw the box for every video.
            saved = app_settings.get_last_color_spec()
            if saved:
                try:
                    self.project.color_spec = ColorSpec.from_dict(saved)
                except (KeyError, TypeError, ValueError):
                    pass
        self.scan_button.setEnabled(self.project.color_spec is not None)

        self.marker_panel.set_project(self.project)
        self._refresh_timeline()
        self._load_waveform(path)

    def _load_waveform(self, path: str) -> None:
        self.waveform_bar.set_loading()
        self._waveform_worker = WaveformWorker(path, parent=self)
        self._waveform_worker.finished_waveform.connect(self._on_waveform_finished)
        self._waveform_worker.failed.connect(self._on_waveform_failed)
        self._waveform_worker.start()

    def _on_waveform_finished(self, peaks) -> None:
        self.waveform_bar.set_waveform(peaks)
        self._waveform_worker = None

    def _on_waveform_failed(self, message: str) -> None:
        # Non-fatal: editing still works fine off the seek bar alone.
        self.waveform_bar.set_waveform(None)
        self._waveform_worker = None

    def _set_marker_region(self) -> None:
        if not self.project:
            return
        self.video.set_paused(True)
        try:
            frame = grab_frame(self.project.video_path, self._position)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Couldn't read frame", str(exc))
            return

        dialog = ColorRegionDialog(frame, initial_spec=self.project.color_spec, parent=self)
        if dialog.exec() != ColorRegionDialog.DialogCode.Accepted or dialog.result_spec is None:
            return

        self.project.color_spec = dialog.result_spec
        app_settings.set_last_color_spec(dialog.result_spec.to_dict())
        self.scan_button.setEnabled(True)
        self._on_project_changed()

    def _scan_for_color(self) -> None:
        if not self.project or self._scan_worker is not None or not self.project.color_spec:
            return

        self._progress_dialog = QProgressDialog("Estimating time remaining…", None, 0, 100, self)
        self._progress_dialog.setWindowTitle("Scanning for Color Marker")
        bar = QProgressBar()
        bar.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._progress_dialog.setBar(bar)
        self._progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setValue(0)
        self._scan_start_time = time.monotonic()
        self._scan_eta_ema = None

        self._scan_worker = ColorScanWorker(self.project.video_path, self.project.color_spec, parent=self)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished_scan.connect(self._on_scan_finished)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

    def _on_scan_progress(self, pct: int) -> None:
        if not self._progress_dialog:
            return
        self._progress_dialog.setValue(pct)
        elapsed = time.monotonic() - self._scan_start_time
        if pct > 0 and elapsed > 1.0:
            # The scan isn't linear (a fast full-video sweep, then much
            # slower fine-grained passes over just the hits it found), so a
            # fresh elapsed/pct estimate swings wildly at that transition.
            # Smoothing it against the running estimate keeps the displayed
            # number from jumping around, at the cost of lagging behind a
            # real trend change by a few updates.
            raw_estimate = elapsed * (100 - pct) / pct
            if self._scan_eta_ema is None:
                self._scan_eta_ema = raw_estimate
            else:
                self._scan_eta_ema = 0.2 * raw_estimate + 0.8 * self._scan_eta_ema
            self._progress_dialog.setLabelText(f"About {format_time(self._scan_eta_ema)} remaining")
        else:
            self._progress_dialog.setLabelText("Estimating time remaining…")

    def _on_scan_finished(self, markers) -> None:
        if self._progress_dialog:
            self._progress_dialog.close()
            self._progress_dialog = None
        if self.project:
            self.project.checkpoint()
            self.project.clear_candidates()
            self.project.add_color_candidates([(m.start, m.end) for m in markers])
            self.marker_panel.refresh()
            self._on_project_changed()
        self._scan_worker = None

    def _on_scan_failed(self, message: str) -> None:
        if self._progress_dialog:
            self._progress_dialog.close()
            self._progress_dialog = None
        self._scan_worker = None
        QMessageBox.warning(self, "Scan failed", message)

    def _export(self) -> None:
        if not self.project or self._export_worker is not None:
            return

        video_path = Path(self.project.video_path)
        default_dir = app_settings.get_output_dir() or video_path.parent
        dialog = ExportDialog(default_base_name=video_path.stem, default_output_dir=default_dir, parent=self)
        if dialog.exec() != ExportDialog.DialogCode.Accepted:
            return
        settings = dialog.export_settings()

        self._export_progress_dialog = QProgressDialog("Exporting… 0%", None, 0, 100, self)
        self._export_progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._export_progress_dialog.setMinimumDuration(0)
        self._export_progress_dialog.setValue(0)

        self._export_worker = ExportWorker(self.project, self._duration, settings, self)
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.finished_export.connect(self._on_export_finished)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.start()

    def _on_export_progress(self, pct: int) -> None:
        if self._export_progress_dialog:
            self._export_progress_dialog.setValue(pct)
            self._export_progress_dialog.setLabelText(f"Exporting… {pct}%")

    def _on_export_finished(self, outputs: list[Path]) -> None:
        if self._export_progress_dialog:
            self._export_progress_dialog.close()
            self._export_progress_dialog = None
        self._export_worker = None

        names = "\n".join(p.name for p in outputs)
        box = QMessageBox(self)
        box.setWindowTitle("Export complete")
        box.setText(f"Wrote {len(outputs)} file(s):\n{names}")
        open_button = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_button and outputs:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(outputs[0].parent)))

    def _on_export_failed(self, message: str) -> None:
        if self._export_progress_dialog:
            self._export_progress_dialog.close()
            self._export_progress_dialog = None
        self._export_worker = None
        QMessageBox.warning(self, "Export failed", message)

    def _add_split_here(self) -> None:
        if not self.project:
            return
        self.project.checkpoint()
        self.project.add_marker(self._position, MarkerKind.SPLIT, source="manual")
        self.marker_panel.refresh()
        self._on_project_changed()

    def _on_project_changed(self) -> None:
        if not self.project:
            return
        self._refresh_timeline()
        try:
            self.project.save(EditProject.sidecar_path_for(self.project.video_path))
        except OSError:
            pass  # non-fatal: edits still live in memory for this session

    def _on_candidate_span_changed(self, marker_id: int, new_start: float, new_end: float, is_final: bool) -> None:
        if not self.project:
            return
        if not self._drag_in_progress:
            # One checkpoint per drag gesture, not per mouseMoveEvent: this
            # fires repeatedly with is_final=False while dragging, and only
            # once more with is_final=True on release.
            self.project.checkpoint()
            self._drag_in_progress = True
        self.project.set_marker_range(marker_id, new_start, new_end)
        self._refresh_timeline()
        self.marker_panel.refresh()
        if is_final:
            self._drag_in_progress = False
            try:
                self.project.save(EditProject.sidecar_path_for(self.project.video_path))
            except OSError:
                pass  # non-fatal: edits still live in memory for this session

    def _undo(self) -> None:
        if not self.project or not self.project.undo():
            return
        self.marker_panel.refresh()
        self._on_project_changed()

    def _redo(self) -> None:
        if not self.project or not self.project.redo():
            return
        self.marker_panel.refresh()
        self._on_project_changed()

    def _refresh_timeline(self) -> None:
        if not self.project:
            return
        regions, _unmatched = self.project.cut_regions()
        splits = self.project.split_points()
        candidate_spans = [
            (m.id, m.range_start, m.range_end)
            for m in self.project.markers
            if m.kind is MarkerKind.CANDIDATE and m.range_start is not None and m.range_end is not None
        ]
        self._candidate_spans = candidate_spans
        # The waveform mirrors the seek bar's markers exactly, so editing
        # either one always shows the same state on both. Each gets its own
        # list copy — set_candidate_spans mutates the list it's given while
        # dragging, and the two widgets must never silently share one.
        for timeline in (self.seek_bar, self.waveform_bar):
            timeline.set_duration(self._duration)
            timeline.set_cut_regions(list(regions))
            timeline.set_splits(list(splits))
            timeline.set_candidate_spans(list(candidate_spans))
        # set_duration above may have reset the view to the full duration
        # (e.g. a newly-loaded video), so the scrollbar needs to catch up too.
        self._sync_scrollbar_from_view()

    def _on_scrollbar_moved(self, value: int) -> None:
        """User dragged the scrollbar (or clicked its track/arrows): pan the
        shared view to match, leaving the playhead and zoom level untouched."""
        view_start = value / _SCROLLBAR_SCALE
        view_duration = self.seek_bar.view_duration
        self.seek_bar.set_view(view_start, view_duration)
        self.waveform_bar.set_view(view_start, view_duration)

    def _sync_scrollbar_from_view(self, view_start: float | None = None, view_duration: float | None = None) -> None:
        """Reflects the current (or just-changed) view window on the
        scrollbar. Signal-blocked while updating so this can't loop back
        into _on_scrollbar_moved — this is a readout of the view, not a
        request to change it."""
        if view_start is None:
            view_start = self.seek_bar.view_start
            view_duration = self.seek_bar.view_duration
        duration = self.seek_bar.duration

        self.timeline_scrollbar.blockSignals(True)
        try:
            if duration <= 0 or view_duration >= duration:
                self.timeline_scrollbar.setRange(0, 0)
                self.timeline_scrollbar.setEnabled(False)
            else:
                self.timeline_scrollbar.setEnabled(True)
                self.timeline_scrollbar.setRange(0, round((duration - view_duration) * _SCROLLBAR_SCALE))
                self.timeline_scrollbar.setPageStep(round(view_duration * _SCROLLBAR_SCALE))
                self.timeline_scrollbar.setValue(round(view_start * _SCROLLBAR_SCALE))
        finally:
            self.timeline_scrollbar.blockSignals(False)

    def _set_speed(self, speed: float) -> None:
        self.video.set_speed(speed)
        for s, btn in self.speed_buttons.items():
            btn.setChecked(s == speed)

    def _on_position_changed(self, position: float) -> None:
        self._position = position
        self.time_label.setText(f"{format_time(position)} / {format_time(self._duration)}")
        self.seek_bar.set_position(position)
        self.waveform_bar.set_position(position)

        if self.play_button.isChecked():  # actively playing, not paused
            for _marker_id, start, end in self._candidate_spans:
                if start <= position < end:
                    self.video.seek_absolute(end + CANDIDATE_SKIP_BUFFER_SECONDS)
                    return
            # Loop within whatever the seek bar is currently showing — the
            # whole video by default, or just the zoomed-in window if zoomed.
            if position >= self.seek_bar.view_end - LOOP_TRIGGER_EPSILON_SECONDS:
                self.video.seek_absolute(self.seek_bar.view_start)

    def _on_duration_changed(self, duration: float) -> None:
        self._duration = duration
        self._refresh_timeline()  # also applies set_duration to both timeline widgets

    def _on_pause_changed(self, paused: bool) -> None:
        self.play_button.setChecked(not paused)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.video.shutdown()
        super().closeEvent(event)
