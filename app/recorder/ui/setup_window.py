"""The recorder setup window and wiring for its recording session."""
from __future__ import annotations

import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRegularExpression, Qt, Signal
from PySide6.QtGui import QGuiApplication, QRegularExpressionValidator
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from app.core import settings as app_settings
from app.core.ffmpeg_utils import probe_duration, unique_path
from app.core.video_encoder import prefetch_encoders
from app.recorder.core.audio_meter import AudioLevelMeter
from app.recorder.core.clicker import ACTION_MARK, ACTION_PAUSE, ClickerConfig
from app.recorder.core.devices import (
    HARDWARE_FORMATS, CameraDevice, CameraInput, MicDevice, ScreenSource, cached_camera_modes, camera_format_choices, list_dshow_camera_modes,
    list_dshow_devices, list_screens, plan_camera_input,
)
from app.recorder.core.ffmpeg_record import (
    AudioCaptureConfig, CameraCaptureConfig, RecordConfig, RecordingController, ScreenCaptureConfig, describe_recording, preview_frame_size,
)
from app.recorder.core.file_names import ILLEGAL_NAME_CHARACTERS, MAX_NAME_LENGTH, clean_file_name, default_recording_name
from app.recorder.core.marker_export import write_marker_sidecar
from app.recorder.core.raw_input import ClickerService
from app.recorder.ui.auto_gain_dialog import AutoGainDialog
from app.recorder.ui.camera_preview import CameraPreviewWindow
from app.recorder.ui.clicker_dialog import ClickerDialog
from app.recorder.ui.countdown_overlay import CountdownOverlay
from app.recorder.ui.floating_overlay import FloatingOverlay
from app.recorder.ui.level_bar import AudioLevelBar
from app.recorder.ui.mic_test_panel import MicTestPanel
from app.recorder.ui.region_outline import RegionOutline
from app.recorder.ui.region_picker import CameraRegionPicker, ScreenRegionPicker
from app.recorder.ui.save_location_combo import SaveLocationCombo
from app.recorder.ui.screen_wash import PAUSED_COLOR, MarkFlash, ScreenWash


MIN_MIC_GAIN = 0.5
MAX_MIC_GAIN = 1.5  # 1.0x is the midpoint of the slider


class RecorderMainWindow(QMainWindow):
    camera_modes_ready = Signal(str)  # a camera's formats have been read (from a background thread): its capture id

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Ready, Set, Lecture! Recorder")
        self._audio_meter = AudioLevelMeter(self)
        self._controller = RecordingController(self)
        self._floating_overlay: FloatingOverlay | None = None
        self._camera_preview: CameraPreviewWindow | None = None
        self._countdown: CountdownOverlay | None = None
        self._resume_countdown: CountdownOverlay | None = None
        self._paused_tint = ScreenWash(PAUSED_COLOR)
        self._mark_flash = MarkFlash(self)
        self._clicker = ClickerService(self)
        self._resume_pressed = False  # Resume was pressed: the 3-2-1 is running, so the screen is no longer red
        self._selected_region: tuple[int, int, int, int] | None = None
        self._region_frame_size: tuple[int, int] | None = None  # the camera frame size the selected area was picked on
        self._region_outline: RegionOutline | None = None
        self._save_dir: Path | None = None
        self._default_name = default_recording_name(datetime.now())  # what the file name box holds until the user edits it
        self._marker_breaks: list[float] = []
        self._pending_config: RecordConfig | None = None
        self._pending_output: Path | None = None
        self._screens: list[ScreenSource] = []
        self._cameras: list[CameraDevice] = []
        self._mics: list[MicDevice] = []
        self._build_ui()
        self._wire_signals()
        self._populate_devices()
        self._init_save_location()
        prefetch_encoders()  # so the first recording doesn't wait for ffmpeg to list its encoders

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        form = QFormLayout()
        self.source_combo = QComboBox()
        self.select_area_button = QPushButton("Select area…")
        self.reset_area_button = QPushButton("Reset area")
        self.reset_area_button.setToolTip("Go back to recording the whole screen or camera frame.")
        self.reset_area_button.setEnabled(False)
        self.area_label = QLabel("Full source")
        area_row = QHBoxLayout()
        area_row.addWidget(self.select_area_button)
        area_row.addWidget(self.reset_area_button)
        area_row.addWidget(self.area_label, 1)
        self.mic_combo = QComboBox()
        self.gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.gain_slider.setRange(round(MIN_MIC_GAIN * 100), round(MAX_MIC_GAIN * 100))
        self.gain_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.gain_slider.setTickInterval(50)
        self.gain_value = QLabel()
        gain_row = QHBoxLayout()
        self.auto_gain_button = QPushButton("Auto-adjust")
        self.auto_gain_button.setToolTip("Speak in your normal voice for a few seconds and the gain is set so your voice "
                                         "lands at the ideal level (about 60% on the meter).")
        gain_row.addWidget(self.gain_slider)
        gain_row.addWidget(self.gain_value)
        gain_row.addWidget(self.auto_gain_button)
        self.level_bar = AudioLevelBar()
        self.mic_test = MicTestPanel()
        self.save_combo = SaveLocationCombo()
        self.favorite_button = QPushButton("☆")
        self.favorite_button.setFixedWidth(38)
        self.favorite_button.setStyleSheet("font-size: 16px;")
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_combo, 1)
        save_row.addWidget(self.favorite_button)
        self.name_edit = QLineEdit(self._default_name)
        self.name_edit.setMaxLength(MAX_NAME_LENGTH)
        self.name_edit.setValidator(QRegularExpressionValidator(QRegularExpression("[^" + re.escape(ILLEGAL_NAME_CHARACTERS) + r"\x00-\x1f]*")))
        self.name_edit.setToolTip("The recording is saved under this name. Left as it is, the time in the name is set when you press Start. "
                                  "If a file with the name already exists, a number is added.")
        name_row = QHBoxLayout()
        name_row.addWidget(self.name_edit, 1)
        name_row.addWidget(QLabel(".mp4"))
        self.camera_format_combo = QComboBox()
        self.camera_format_combo.setToolTip(
            "What to take from the camera. H.264 or H.265 from the camera: the camera compresses the picture and the computer only saves it, "
            "so there is almost no load, but an area can't be cropped without the computer re-encoding it. MJPEG or Uncompressed: "
            "the computer encodes the picture (more load, smaller files, any area). Automatic prefers the camera's own H.264, then H.265.")
        form.addRow("Source", self.source_combo)
        form.addRow("Camera format", self.camera_format_combo)
        form.addRow("Area", area_row)
        form.addRow("Microphone", self.mic_combo)
        form.addRow("Mic gain", gain_row)
        form.addRow("Level", self.level_bar)
        form.addRow("Mic test", self.mic_test)
        form.addRow("Save to", save_row)
        form.addRow("File name", name_row)
        self.clicker_button = QPushButton("Setup BT Clicker")
        self.clicker_button.setToolTip("Use a Bluetooth clicker to pause/resume and to mark while you record.")
        self.clicker_label = QLabel()
        clicker_row = QHBoxLayout()
        clicker_row.addWidget(self.clicker_button)
        clicker_row.addWidget(self.clicker_label, 1)
        form.addRow("BT clicker", clicker_row)
        self._form = form
        form.setRowVisible(self.camera_format_combo, False)  # only for a camera source; _refresh_camera_formats shows it
        layout.addLayout(form)
        self.start_button = QPushButton("Start recording")
        self.start_button.setDefault(True)
        layout.addWidget(self.start_button)
        self.statusBar().showMessage("Choose a source and optional microphone.")
        self.resize(560, 370)
        self._refresh_clicker_label()

    def _wire_signals(self) -> None:
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.select_area_button.clicked.connect(self._select_area)
        self.reset_area_button.clicked.connect(self._reset_area)
        self.mic_combo.currentIndexChanged.connect(self._mic_changed)
        self.gain_slider.valueChanged.connect(self._gain_changed)
        self.auto_gain_button.clicked.connect(self._auto_adjust_gain)
        self.save_combo.location_chosen.connect(self._location_chosen)
        self.save_combo.other_chosen.connect(self._choose_other_folder)
        self.save_combo.star_clicked.connect(self._toggle_favorite_for)
        self.favorite_button.clicked.connect(self._toggle_favorite)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.mic_test.busy_changed.connect(self._mic_test_busy_changed)
        self._audio_meter.level_changed.connect(self.level_bar.set_level)
        self._audio_meter.error.connect(lambda message: self.statusBar().showMessage(f"Level meter unavailable: {message}"))
        self._controller.finished.connect(self._recording_finished)
        self._controller.failed.connect(self._recording_failed)
        self._controller.preview_image.connect(self._preview_frame)
        self._controller.state_changed.connect(self._preview_state)
        self._controller.state_changed.connect(self._state_changed_for_tint)
        self.clicker_button.clicked.connect(self._setup_clicker)
        self._clicker.action_triggered.connect(self._clicker_action)
        self.camera_format_combo.currentIndexChanged.connect(self._camera_format_changed)
        self.camera_modes_ready.connect(self._camera_modes_ready)

    def _populate_devices(self) -> None:
        self._screens = list_screens()
        self._cameras, self._mics = list_dshow_devices()
        last_source = app_settings.get_last_record_source()
        for screen in self._screens:
            self.source_combo.addItem(screen.label, screen)
        for camera in self._cameras:
            self.source_combo.addItem(f"Camera — {camera.name}", camera)
        if not self.source_combo.count():
            self.source_combo.addItem("No screen or camera detected", None)
            self.start_button.setEnabled(False)
        else:
            for index in range(self.source_combo.count()):
                if self._source_key(self.source_combo.itemData(index)) == last_source:
                    self.source_combo.setCurrentIndex(index)
                    break
        self.mic_combo.addItem("None (no audio)", None)
        last_mic = app_settings.get_last_mic_device()
        for mic in self._mics:
            self.mic_combo.addItem(mic.name, mic)
        if self._mics:
            self.mic_combo.setCurrentIndex(next((index for index in range(1, self.mic_combo.count()) if self.mic_combo.itemData(index).name == last_mic), 1))
        self.gain_slider.setValue(round(app_settings.get_record_gain() * 100))
        self._gain_changed(self.gain_slider.value())  # setValue is silent when the value doesn't change (e.g. already at the minimum)
        self._source_changed()
        self._mic_changed()

    @staticmethod
    def _source_key(source: ScreenSource | CameraDevice | None) -> str | None:
        if isinstance(source, ScreenSource):
            return f"screen:{source.qt_name}"
        if isinstance(source, CameraDevice):
            return f"camera:{source.name}"
        return None

    def _show_region_outline(self, region: QRect | None) -> None:
        if region is None:
            self._clear_region_outline()
            return
        if self._region_outline is None:
            self._region_outline = RegionOutline()
        self._region_outline.set_region(region)
        self._region_outline.show()

    def _clear_region_outline(self) -> None:
        if self._region_outline is not None:
            self._region_outline.close()
            self._region_outline.deleteLater()
            self._region_outline = None

    def _reset_area(self) -> None:
        """Back to recording the whole screen or camera frame."""
        self._selected_region = None
        self._region_frame_size = None
        self._clear_region_outline()
        self.area_label.setText("Full source")
        self.reset_area_button.setEnabled(False)

    def _source_changed(self) -> None:
        self._reset_area()
        source = self.source_combo.currentData()
        self.select_area_button.setEnabled(source is not None)
        if isinstance(source, CameraDevice) and cached_camera_modes(source.capture_id) is None:
            # Asking the camera what it offers takes about half a second: do it in the background, and fill in "Camera format" when done.
            threading.Thread(target=self._load_camera_modes, args=(source.capture_id,), daemon=True).start()
        self._refresh_camera_formats()

    def _load_camera_modes(self, capture_id: str) -> None:
        list_dshow_camera_modes(capture_id)
        try:
            self.camera_modes_ready.emit(capture_id)
        except RuntimeError:  # the window has already gone
            pass

    def _camera_modes_ready(self, capture_id: str) -> None:
        source = self.source_combo.currentData()
        if isinstance(source, CameraDevice) and source.capture_id == capture_id:
            self._refresh_camera_formats()

    def _refresh_camera_formats(self) -> None:
        """Fill "Camera format" with what the chosen camera offers (shown only for a camera), keeping the remembered choice if offered."""
        source = self.source_combo.currentData()
        is_camera = isinstance(source, CameraDevice)
        self._form.setRowVisible(self.camera_format_combo, is_camera)
        if not is_camera:
            return
        modes = cached_camera_modes(source.capture_id)
        combo = self.camera_format_combo
        combo.blockSignals(True)  # filling the list is not the person choosing
        combo.clear()
        if modes is None:
            combo.addItem("Automatic (asking the camera what it offers…)", "auto")
        else:
            for choice, label in camera_format_choices(modes):
                combo.addItem(label, choice)
            combo.setCurrentIndex(max(0, combo.findData(app_settings.get_camera_format())))
        combo.blockSignals(False)
        self._clear_area_if_frame_changed(source)
        self._update_area_label()

    def _camera_format_changed(self, _index: int = 0) -> None:
        source = self.source_combo.currentData()
        if not isinstance(source, CameraDevice):
            return
        app_settings.set_camera_format(self.camera_format_combo.currentData() or "auto")
        self.statusBar().showMessage(f"Camera format: {self.camera_format_combo.currentText()}")
        self._clear_area_if_frame_changed(source)
        self._update_area_label()

    def _camera_input(self, source: CameraDevice) -> CameraInput:
        """The size and format to record from this camera, for the chosen "Camera format"."""
        return plan_camera_input(list_dshow_camera_modes(source.capture_id), self.camera_format_combo.currentData() or "auto")

    def _clear_area_if_frame_changed(self, source: CameraDevice) -> None:
        """A selected area is a rectangle of one particular frame size: another format can mean another size, so it can't be kept."""
        if self._selected_region is not None and self._region_frame_size != self._camera_input(source).size:
            self._reset_area()
            self.statusBar().showMessage("The camera frame is a different size in that format, so the selected area was cleared.")

    def _update_area_label(self) -> None:
        if self._selected_region is None:
            self.area_label.setText("Full source")
            return
        text = "Custom area selected"
        source = self.source_combo.currentData()
        if isinstance(source, CameraDevice) and self._camera_input(source).input_format in HARDWARE_FORMATS:
            text += " (the computer re-encodes the picture)"  # a crop can't be applied to a stream the camera has already compressed
        self.area_label.setText(text)

    def _mic_test_busy_changed(self, busy: bool) -> None:
        # The test holds the mic, so it can't be swapped, and a recording can't start, until it ends.
        self.mic_combo.setEnabled(not busy)
        self.start_button.setEnabled(not busy and self.source_combo.currentData() is not None)
        self._refresh_auto_gain_button()

    def _refresh_auto_gain_button(self) -> None:
        self.auto_gain_button.setEnabled(isinstance(self.mic_combo.currentData(), MicDevice) and not self.mic_test.busy)

    def _auto_adjust_gain(self) -> None:
        mic = self.mic_combo.currentData()
        if not isinstance(mic, MicDevice):
            return
        dialog = AutoGainDialog(mic, self._audio_meter, MIN_MIC_GAIN, MAX_MIC_GAIN, self)
        if dialog.exec() == dialog.DialogCode.Accepted and dialog.result_gain() is not None:
            self.gain_slider.setValue(round(dialog.result_gain() * 100))
            self.statusBar().showMessage(f"Mic gain auto-adjusted to {dialog.result_gain():.2f}×.")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.mic_test.cancel()
        self._clear_region_outline()  # a visible parentless window would keep the app running
        self._discard_resume_countdown()
        self._close_camera_preview()
        self._paused_tint.hide()
        self._mark_flash.cancel()
        self._clicker.stop()
        super().closeEvent(event)

    def _mic_changed(self) -> None:
        mic = self.mic_combo.currentData()
        self.mic_test.set_mic(mic if isinstance(mic, MicDevice) else None)
        self._refresh_auto_gain_button()
        if not isinstance(mic, MicDevice):
            self._audio_meter.stop()
            return
        matched = next((device for device in QMediaDevices.audioInputs() if device.description() == mic.name), None)
        if matched is None:
            self._audio_meter.stop()
            self.statusBar().showMessage("Selected microphone cannot provide a Qt level meter; recording remains available.")
        else:
            self._audio_meter.start(matched)

    def _gain_changed(self, value: int) -> None:
        gain = value / 100.0
        self.gain_value.setText(f"{gain:.2f}×")
        self._audio_meter.set_gain(gain)
        self.mic_test.set_gain(gain)

    def _select_area(self) -> None:
        source = self.source_combo.currentData()
        if isinstance(source, ScreenSource):
            screen = next((candidate for candidate in QGuiApplication.screens() if candidate.name() == source.qt_name), None)
            if screen is None:
                QMessageBox.warning(self, "Screen unavailable", "That display is no longer available.")
                return
            dialog = ScreenRegionPicker(source, screen, self)
            if self._region_outline is not None:
                self._region_outline.hide()  # keep the old frame out of the frozen screenshot
            if dialog.exec() == dialog.DialogCode.Accepted:
                self._selected_region = dialog.picked_absolute_rect()
                self._show_region_outline(dialog.picked_logical_rect())
            elif self._region_outline is not None and self._selected_region is not None:
                self._region_outline.show()
        elif isinstance(source, CameraDevice):
            native_size = self._camera_input(source).size
            qt_device = next((device for device in QMediaDevices.videoInputs() if device.description() == source.name), None)
            if qt_device is None:
                QMessageBox.information(self, "Camera preview unavailable", "This camera cannot be opened for preview. The full camera frame will be recorded.")
                return
            dialog = CameraRegionPicker(qt_device, native_size, self)
            if dialog.exec() == dialog.DialogCode.Accepted:
                self._selected_region = dialog.picked_rect()
                self._region_frame_size = native_size
        else:
            return
        self._update_area_label()
        self.reset_area_button.setEnabled(self._selected_region is not None)

    def _init_save_location(self) -> None:
        # Only starred folders are remembered: start on the first one, or on Videos when none is starred yet.
        favorites = app_settings.get_favorite_output_dirs()
        self._save_dir = favorites[0] if favorites else Path.home() / "Videos"
        self._refresh_save_combo()

    def _refresh_save_combo(self) -> None:
        self.save_combo.set_locations(app_settings.get_favorite_output_dirs(), self._save_dir)
        starred = self._save_dir is not None and app_settings.is_favorite_output_dir(self._save_dir)
        self.favorite_button.setEnabled(self._save_dir is not None)
        self.favorite_button.setText("★" if starred else "☆")
        self.favorite_button.setToolTip("Starred: click to remove this folder from your favorites." if starred
                                        else "Click to star this folder so it stays in the list.")

    def _location_chosen(self, folder: Path) -> None:
        self._save_dir = folder
        self._refresh_save_combo()

    def _choose_other_folder(self) -> None:
        start = self._save_dir if self._save_dir is not None and self._save_dir.exists() else Path.home()
        folder = QFileDialog.getExistingDirectory(self, "Choose where to save recordings", str(start))
        if folder:  # for this session only, unless it gets starred
            self._save_dir = Path(folder)
        self._refresh_save_combo()  # also puts the selection back after a cancel

    def _toggle_favorite_for(self, folder: Path) -> None:
        if app_settings.is_favorite_output_dir(folder):
            app_settings.remove_favorite_output_dir(folder)
        else:
            app_settings.add_favorite_output_dir(folder)
        self._refresh_save_combo()

    def _toggle_favorite(self) -> None:
        if self._save_dir is not None:
            self._toggle_favorite_for(self._save_dir)

    def _usable_save_dir(self) -> Path | None:
        """The chosen folder, created if needed, or None (with a warning) if recordings can't be written there."""
        folder = self._save_dir
        if folder is None:
            return None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=folder):  # a real write test; permissions can't be judged reliably otherwise
                pass
        except OSError as exc:
            QMessageBox.warning(self, "Can't save here", f"Recordings can't be saved to:\n{folder}\n\n{exc}\n\nChoose another folder.")
            return None
        return folder

    def _file_name_for_start(self) -> str | None:
        """The name to record under, or None (after telling the user) if the box doesn't hold a usable one."""
        text = self.name_edit.text()
        if text == self._default_name:
            text = default_recording_name(datetime.now())  # untouched default: stamp it with the moment recording starts
        name = clean_file_name(text)
        if name is None:
            QMessageBox.warning(self, "File name", "Please enter a file name for the recording. It can't be empty, end with a dot, "
                                f"contain any of  {ILLEGAL_NAME_CHARACTERS}  or be a reserved Windows name such as CON or NUL.")
            self.name_edit.setFocus()
        return name

    def _reset_file_name(self) -> None:
        """A fresh default (with the current time) for the next recording."""
        self._default_name = default_recording_name(datetime.now())
        self.name_edit.setText(self._default_name)

    def _on_start_clicked(self) -> None:
        source = self.source_combo.currentData()
        if not isinstance(source, (ScreenSource, CameraDevice)):
            return
        name = self._file_name_for_start()
        if name is None:
            return
        output_dir = self._usable_save_dir()
        if output_dir is None:
            return
        mic = self.mic_combo.currentData()
        audio = AudioCaptureConfig(mic.capture_id if isinstance(mic, MicDevice) else None, self.gain_slider.value() / 100.0)
        if isinstance(source, ScreenSource):
            capture = ScreenCaptureConfig(self._selected_region or source.physical_rect, draw_cursor=not app_settings.get_hide_cursor())
        else:
            plan = self._camera_input(source)
            recorded_size = self._selected_region[2:] if self._selected_region else plan.size
            capture = CameraCaptureConfig(source.capture_id, plan.size, self._selected_region, preview_size=preview_frame_size(recorded_size),
                                          input_format=plan.input_format)
        output = unique_path(output_dir / f"{name}.mp4")
        self._pending_config = RecordConfig(capture, audio, output)
        self._pending_output = output
        self._marker_breaks = []
        if isinstance(mic, MicDevice):
            app_settings.set_last_mic_device(mic.name)
        app_settings.set_record_gain(audio.gain)
        app_settings.set_last_record_source(self._source_key(source) or "")
        self._start_countdown(source)

    def _place_countdown(self, countdown: CountdownOverlay, source: ScreenSource | CameraDevice | None) -> None:
        """Centre the countdown on the screen being recorded (the primary screen for a camera)."""
        screen = QGuiApplication.primaryScreen()
        if isinstance(source, ScreenSource):
            screen = next((candidate for candidate in QGuiApplication.screens() if candidate.name() == source.qt_name), screen)
        if screen:
            countdown.move(screen.geometry().center() - countdown.rect().center())

    def _start_countdown(self, source: ScreenSource | CameraDevice) -> None:
        self._open_camera_preview_early()  # a live picture while it counts down, before the shown-on-top countdown
        self._countdown = CountdownOverlay(self)
        self._place_countdown(self._countdown, source)
        self._countdown.countdown_finished.connect(self._begin_recording)
        self._countdown.start(3)

    def _camera_source_with_preview(self) -> CameraCaptureConfig | None:
        source = self._pending_config.source if self._pending_config is not None else None
        return source if isinstance(source, CameraCaptureConfig) and source.preview_size else None

    def _start_resume_countdown(self) -> None:
        """Resume only after a 3-2-1, just like the start. The recording stays paused (nothing is captured) meanwhile."""
        if self._resume_countdown is not None or self._floating_overlay is None:
            return
        self._floating_overlay.set_resuming()
        self._resume_pressed = True
        self._sync_pause_tint()  # the screen goes back to normal for the 3-2-1
        source = self._camera_source_with_preview()
        if source is not None and self._camera_preview is not None and self._camera_preview.isVisible():
            self._camera_preview.set_paused(False)  # live again while it counts down, not a frozen "Paused" picture
            self._controller.start_preview_feed(source)
        # No parent: the recorder window is hidden while recording, and a child window would go with it.
        self._resume_countdown = CountdownOverlay()
        self._place_countdown(self._resume_countdown, self.source_combo.currentData())
        self._resume_countdown.countdown_finished.connect(self._finish_resume)
        self._resume_countdown.start(3)

    def _finish_resume(self) -> None:
        self._discard_resume_countdown(stop_feed=False)  # resume() releases the camera itself, then opens it for recording
        if self._camera_preview is not None and self._camera_preview.isVisible():
            self._camera_preview.set_status("Resuming…")
        self._controller.resume()

    def _discard_resume_countdown(self, stop_feed: bool = True) -> None:
        if self._resume_countdown is not None:
            self._resume_countdown.cancel()
            self._resume_countdown.deleteLater()
            self._resume_countdown = None
        if stop_feed:
            self._controller.stop_preview_feed()

    def _begin_recording(self) -> None:
        if self._pending_config is None or self._pending_output is None:
            return
        self._controller.stop_preview_feed()  # the camera can only be open in one place: free it for the recorder
        if self._camera_preview is not None and self._camera_preview.isVisible():
            self._camera_preview.set_status("Starting the recording…")
        self.hide()
        self._floating_overlay = FloatingOverlay(self._audio_meter)
        self._floating_overlay.record_pause_clicked.connect(self._toggle_pause)
        self._floating_overlay.marker_break_clicked.connect(self._mark_break)
        self._floating_overlay.stop_clicked.connect(self._controller.stop)
        self._floating_overlay.set_pause_tint_checked(app_settings.get_red_when_paused())
        self._floating_overlay.pause_tint_toggled.connect(self._pause_tint_toggled)
        self._resume_pressed = False
        self._offer_source_options()
        self._controller.state_changed.connect(self._floating_overlay.set_recording_state)
        self._controller.elapsed_changed.connect(self._floating_overlay.set_elapsed)
        primary = QGuiApplication.primaryScreen()
        if primary:
            geometry = primary.availableGeometry()
            self._floating_overlay.move(geometry.right() - self._floating_overlay.sizeHint().width() - 20, geometry.top() + 20)
        self._floating_overlay.show()
        self._start_clicker()
        work_dir = Path(tempfile.mkdtemp(prefix="readysetlecturerecorder_"))
        self._controller.start(self._pending_config, work_dir, self._pending_output)

    def _offer_source_options(self) -> None:
        """The controls that only make sense for one kind of source. 'Hide cursor' is for a screen recording only: a camera
        recording has no mouse cursor in it, so the option is not shown there at all. The camera preview switch is the reverse."""
        overlay, source = self._floating_overlay, self._pending_config.source
        if isinstance(source, ScreenCaptureConfig):
            overlay.show_cursor_option(hidden=not source.draw_cursor)
            overlay.hide_cursor_toggled.connect(self._cursor_option_toggled)
        elif self._camera_source_with_preview() is not None:
            self._attach_camera_preview()

    def _new_camera_preview(self) -> CameraPreviewWindow:
        window = CameraPreviewWindow(self.screen())
        window.dismissed.connect(lambda: self._set_preview_shown(False))
        return window

    def _open_camera_preview_early(self) -> None:
        """At the start of the countdown: the full-screen picture appears and runs live, so you can see yourself count down."""
        source = self._camera_source_with_preview()
        if source is None or self._camera_preview is not None:
            return
        self._camera_preview = self._new_camera_preview()
        if app_settings.get_show_camera_preview():
            self._camera_preview.show_on_screen()
            self._controller.start_preview_feed(source)

    def _attach_camera_preview(self) -> None:
        """Once recording: offer the Preview checkbox on the controls, keeping them above the full-screen picture."""
        shown = app_settings.get_show_camera_preview()
        if self._camera_preview is None:
            self._camera_preview = self._new_camera_preview()
            if shown:
                self._camera_preview.show_on_screen()
        self._floating_overlay.show_preview_option(shown)
        self._floating_overlay.preview_toggled.connect(self._set_preview_shown)
        if shown:
            self._floating_overlay.raise_()

    def _set_preview_shown(self, shown: bool) -> None:
        app_settings.set_show_camera_preview(shown)
        if self._camera_preview is None:
            return
        if shown:
            self._camera_preview.show_on_screen()
        else:
            self._camera_preview.hide()
        if self._floating_overlay is not None:
            self._floating_overlay.set_preview_checked(shown)
            self._floating_overlay.raise_()

    def _preview_frame(self, image) -> None:
        if self._camera_preview is not None:
            self._camera_preview.set_image(image)

    def _preview_state(self, state: str) -> None:
        if self._camera_preview is not None:
            self._camera_preview.set_paused(state == "paused")

    def _close_camera_preview(self) -> None:
        self._controller.stop_preview_feed()
        if self._camera_preview is not None:
            self._camera_preview.hide()
            self._camera_preview.deleteLater()
            self._camera_preview = None

    def _cursor_option_toggled(self, hidden: bool) -> None:
        app_settings.set_hide_cursor(hidden)  # remembered for the next recording
        self._controller.set_cursor_visible(not hidden)

    def _refresh_clicker_label(self) -> None:
        summary = ClickerConfig.from_json(app_settings.get_clicker_config()).summary()
        self.clicker_label.setText(summary)
        self.clicker_label.setStyleSheet("color: gray;" if summary == "Not set up" else "")

    def _setup_clicker(self) -> None:
        ClickerDialog(self).exec()
        self._refresh_clicker_label()

    def _start_clicker(self) -> None:
        """Listen for the BT clicker for the length of the recording. Never allowed to get in the recording's way."""
        try:
            self._clicker.start(ClickerConfig.from_json(app_settings.get_clicker_config()))
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"BT clicker unavailable: {exc}")

    def _clicker_action(self, action: str) -> None:
        overlay = self._floating_overlay
        if overlay is None:
            return
        if action == ACTION_PAUSE:
            self._toggle_pause()
        elif action == ACTION_MARK:
            overlay.marker_button.flash()  # the same glow as pressing the clapperboard
            self._mark_break()

    def _pause_tint_toggled(self, on: bool) -> None:
        app_settings.set_red_when_paused(on)  # remembered for the next recording
        self._sync_pause_tint()

    def _state_changed_for_tint(self, state: str) -> None:
        if state == "paused":
            self._resume_pressed = False  # a fresh pause
        self._sync_pause_tint()

    def _sync_pause_tint(self) -> None:
        """Red while paused with the red square on; normal otherwise, and from the moment Resume is pressed."""
        overlay = self._floating_overlay
        red = (overlay is not None and overlay.pause_tint_button.isChecked()
               and self._controller.state == "paused" and not self._resume_pressed)
        if red:
            self._paused_tint.show()
            overlay.raise_()  # the wash is a newer top-most window: keep the controls above it
        else:
            self._paused_tint.hide()

    def _toggle_pause(self) -> None:
        if self._controller.state == "recording":
            self._controller.pause()
        elif self._controller.state == "paused":
            self._start_resume_countdown()

    def _mark_break(self) -> None:
        self._marker_breaks.append(self._controller.elapsed_seconds)  # the button lights up by itself when pressed
        if isinstance(getattr(self._pending_config, "source", None), CameraCaptureConfig):
            # Camera recordings only: the screen flashes blue for two seconds. A screen recording never flashes.
            self._mark_flash.flash()
            if self._floating_overlay is not None:
                self._floating_overlay.raise_()  # the wash is a newer top-most window: keep the controls above it

    def _recording_finished(self, output: Path) -> None:
        if self._floating_overlay:
            self._floating_overlay.hide()
            self._floating_overlay.deleteLater()
            self._floating_overlay = None
        self._sync_pause_tint()  # no controls left: the screen goes back to normal
        self._mark_flash.cancel()
        self._clicker.stop()
        self._discard_resume_countdown()  # e.g. Stop pressed while a resume countdown was running
        self._close_camera_preview()
        self.show()
        self._reset_file_name()  # so the next recording doesn't reuse this name
        try:
            try:
                duration = probe_duration(output)
            except Exception:
                duration = None
            write_marker_sidecar(output, self._marker_breaks, duration)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Marker sidecar", f"Recording saved, but marker breaks could not be saved:\n{exc}")
        note = ""
        how = describe_recording(self._pending_config.source) if self._pending_config is not None else ""
        if how:
            note += f"\n\n{how}"
        if self._controller.dropped_frame_warnings:
            note += ("\n\nSome frames were dropped: the camera's picture arrived faster than it could be processed. "
                     "A lower camera resolution, or closing other programs, would help.")
        QMessageBox.information(self, "Recording saved", f"Saved recording:\n{output}{note}")
        self._pending_config = None
        self._pending_output = None

    def _recording_failed(self, message: str) -> None:
        if self._floating_overlay:
            self._floating_overlay.hide()
            self._floating_overlay.deleteLater()
            self._floating_overlay = None
        self._sync_pause_tint()
        self._mark_flash.cancel()
        self._clicker.stop()
        self._discard_resume_countdown()
        self._close_camera_preview()
        self.show()
        if self.name_edit.text() == self._default_name:
            self._reset_file_name()  # nothing was saved, so keep a name the user typed, but refresh an untouched default
        self._pending_config = None
        self._pending_output = None
        QMessageBox.warning(self, "Recording failed", message)
