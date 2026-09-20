"""The 'Setup BT Clicker' pop-up: choose the Bluetooth clicker, then teach the app which of its buttons does what."""
from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from app.core import settings as app_settings
from app.recorder.core.clicker import (
    ACTION_LABELS, ACTION_MARK, ACTION_PAUSE, ACTIONS, ButtonLearner, ClickerConfig, ClickerMatcher, PressDetector, RawEvent,
    describe_signature,
)
from app.recorder.core.input_devices import InputDevice, list_input_devices, usages_to_listen_on
from app.recorder.core.raw_input import RawInputListener

_WHAT_IT_DOES = {ACTION_PAUSE: "pause and resume the recording", ACTION_MARK: "add a mark"}


class SettingsClickerStore:
    """Where the setup is kept between runs."""

    def load(self) -> ClickerConfig:
        return ClickerConfig.from_json(app_settings.get_clicker_config())

    def save(self, config: ClickerConfig) -> None:
        app_settings.set_clicker_config(config.to_json())


class ClickerDialog(QDialog):
    LEARN_TIMEOUT_MS = 30_000  # room to wake the clicker up
    SETTLE_MS = 600  # after a button is learned, its release and key repeat must not reach this window's buttons
    REFRESH_MS = 3_000
    KEY_QUIET_SECONDS = 0.4

    def __init__(self, parent=None, *, store=None, device_lister: Callable[[bool], list[InputDevice]] = list_input_devices,
                 listener: RawInputListener | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent)
        self.setWindowTitle("Setup BT Clicker")
        self.setMinimumWidth(560)
        self._store = store or SettingsClickerStore()
        self._device_lister = device_lister
        self._clock = clock
        self._config = self._store.load()
        self._devices: list[InputDevice] = []
        self._learning: str | None = None
        self._learner: ButtonLearner | None = None
        self._swallow_keys = False
        self._swallow_until = 0.0  # a key from the clicker is also delivered to this window: keep it away from the buttons
        self._listening_for: frozenset[tuple[int, int]] | None = None
        self._monitor = PressDetector()
        self._listener = listener or RawInputListener(self)
        self._listener.input_event.connect(self._on_input)
        self._build_ui()
        self._learn_timeout = QTimer(self, singleShot=True, interval=self.LEARN_TIMEOUT_MS)
        self._learn_timeout.timeout.connect(self._learn_timed_out)
        self._settle = QTimer(self, singleShot=True, interval=self.SETTLE_MS)
        self._settle.timeout.connect(self._stop_swallowing_keys)
        self._refresh_timer = QTimer(self, interval=self.REFRESH_MS)
        self._refresh_timer.timeout.connect(self._auto_refresh)
        QApplication.instance().installEventFilter(self)
        self.refresh_devices()
        self._refresh_timer.start()

    # ---- layout ---------------------------------------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel("Pair and connect your clicker in Windows first (Bluetooth &amp; devices). It appears below once it is connected. "
                       "Then choose it, press <b>Set…</b> next to what you want it to do, and press that button on the clicker.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        self.bluetooth_settings_button = QPushButton("Open Windows Bluetooth settings")
        self.bluetooth_settings_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("ms-settings:bluetooth")))
        layout.addWidget(self.bluetooth_settings_button, 0, Qt.AlignmentFlag.AlignLeft)

        layout.addWidget(QLabel("Clicker"))
        self.device_list = QListWidget()
        self.device_list.setFixedHeight(104)
        self.device_list.currentItemChanged.connect(self._device_chosen)
        layout.addWidget(self.device_list)
        device_row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.show_all_checkbox = QCheckBox("Show all keyboards, mice and remotes (not just Bluetooth)")
        self.show_all_checkbox.toggled.connect(lambda _: self.refresh_devices())
        device_row.addWidget(self.refresh_button)
        device_row.addWidget(self.show_all_checkbox, 1)
        layout.addLayout(device_row)

        layout.addWidget(QLabel("Buttons"))
        grid = QGridLayout()
        self._value_labels: dict[str, QLabel] = {}
        self._set_buttons: dict[str, QPushButton] = {}
        self._clear_buttons: dict[str, QPushButton] = {}
        for row, action in enumerate(ACTIONS):
            self._value_labels[action] = QLabel()
            self._set_buttons[action] = QPushButton("Set…")
            self._clear_buttons[action] = QPushButton("Clear")
            self._set_buttons[action].clicked.connect(lambda _=False, a=action: self._set_clicked(a))
            self._clear_buttons[action].clicked.connect(lambda _=False, a=action: self._clear_clicked(a))
            grid.addWidget(QLabel(ACTION_LABELS[action]), row, 0)
            grid.addWidget(self._value_labels[action], row, 1)
            grid.addWidget(self._set_buttons[action], row, 2)
            grid.addWidget(self._clear_buttons[action], row, 3)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.last_seen_label = QLabel()
        self.last_seen_label.setWordWrap(True)
        note = QLabel("A clicker button still reaches whatever program is in front (a Page Down keeps turning the slide), "
                      "so pick a button you don't otherwise need.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        for label in (self.status_label, self.last_seen_label, note):
            layout.addWidget(label)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        close_row.addWidget(self.close_button)
        layout.addLayout(close_row)
        self._update_rows()

    # ---- devices --------------------------------------------------------------------------------------------------
    def refresh_devices(self) -> None:
        self._devices = self._device_lister(self.show_all_checkbox.isChecked())
        chosen = self._config.device_key
        self.device_list.blockSignals(True)
        self.device_list.clear()
        for device in self._devices:
            item = QListWidgetItem(device.name + ("" if device.bluetooth else "   (not Bluetooth)"))
            item.setData(Qt.ItemDataRole.UserRole, device.key)
            self.device_list.addItem(item)
        if chosen and all(d.key != chosen for d in self._devices):
            item = QListWidgetItem(f"{self._config.device_name or 'Clicker'}   (not connected)")
            item.setData(Qt.ItemDataRole.UserRole, chosen)
            self.device_list.addItem(item)
        for index in range(self.device_list.count()):
            if self.device_list.item(index).data(Qt.ItemDataRole.UserRole) == chosen:
                self.device_list.setCurrentRow(index)
        self.device_list.blockSignals(False)
        if not self._devices and not chosen:
            self.status_label.setText("No Bluetooth clicker is connected. Pair it in Windows Bluetooth settings and turn it on; "
                                      "this list updates by itself.")
        self._update_rows()
        self._restart_listener()

    def _auto_refresh(self) -> None:
        if self._learning is None and self.isVisible():
            self.refresh_devices()

    def _selected_device(self) -> InputDevice | None:
        return next((d for d in self._devices if d.key == self._config.device_key), None)

    def _device_chosen(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None or self._learning is not None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        device = next((d for d in self._devices if d.key == key), None)
        if key != self._config.device_key:
            self._config.select_device(key, device.name if device else self._config.device_name)
            self._store.save(self._config)
            self.status_label.setText(f"Selected {self._config.device_name}. Set its buttons below.")
        self._update_rows()
        self._restart_listener()

    def _restart_listener(self) -> None:
        """Listen for the chosen device. Only re-register with Windows when what to listen for changes: doing it on
        every refresh would drop any press that lands in the gap."""
        if not self._config.device_key:
            self._listener.stop()
            self._listening_for = None
            return
        want_mouse = any(s.startswith("mouse:") for s in self._config.selected_bindings().values())
        usages = frozenset(usages_to_listen_on(self._selected_device())) | ({(0x01, 0x02)} if want_mouse else frozenset())
        if usages != self._listening_for or not self._listener.running:
            self._listening_for = usages
            self._listener.start(usages)

    # ---- the button rows ------------------------------------------------------------------------------------------
    def _update_rows(self) -> None:
        bindings = self._config.selected_bindings()
        have_device = bool(self._config.device_key)
        busy = self._learning is not None
        for action in ACTIONS:
            signature = bindings.get(action)
            label = self._value_labels[action]
            label.setText(describe_signature(signature) if signature else "Not set")
            label.setStyleSheet("" if signature else "color: gray;")
            learning_this = self._learning == action
            self._set_buttons[action].setText("Cancel" if learning_this else "Set…")
            self._set_buttons[action].setEnabled(have_device and (learning_this or not busy))
            self._clear_buttons[action].setEnabled(bool(signature) and not busy)
        self.device_list.setEnabled(not busy)
        self.show_all_checkbox.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)

    def _set_clicked(self, action: str) -> None:
        if self._learning == action:
            self._end_learning("Cancelled.")
        elif self._learning is None and self._config.device_key:
            self._begin_learning(action)

    def _clear_clicked(self, action: str) -> None:
        self._config.clear(action)
        self._store.save(self._config)
        self.status_label.setText(f"{ACTION_LABELS[action]} button cleared.")
        self._update_rows()

    # ---- learning -------------------------------------------------------------------------------------------------
    def _begin_learning(self, action: str) -> None:
        self._learning = action
        self._learner = ButtonLearner(self._config.device_key)
        self._swallow_keys = True  # the clicker's keys must not press this window's buttons
        self._settle.stop()
        self._learn_timeout.start()
        self.status_label.setText(f"Press the button on {self._config.device_name} that should {_WHAT_IT_DOES[action]}…")
        self._update_rows()

    def _end_learning(self, message: str) -> None:
        self._learning = None
        self._learner = None
        self._learn_timeout.stop()
        self._settle.start()
        self.status_label.setText(message)
        self._update_rows()

    def _learn_timed_out(self) -> None:
        if self._learning is not None:
            self._end_learning(f"No button press came from {self._config.device_name}. Check that it is on and connected, then try again.")

    def _stop_swallowing_keys(self) -> None:
        if self._learning is None:
            self._swallow_keys = False

    def _on_input(self, event: RawEvent) -> None:
        if event.device_key != self._config.device_key:
            return
        now = self._clock()
        if event.kind == "key":
            self._swallow_until = now + self.KEY_QUIET_SECONDS
        if self._learning is not None and self._learner is not None:
            self._learn_step(self._learner.feed(event, now))
        elif self._monitor.is_new(event, now):
            self._show_last_seen(event)

    def _learn_step(self, step) -> None:
        action = self._learning
        if step.state == "again":
            self._learn_timeout.start()
            self.status_label.setText("Got it. Press the same button once more to confirm.")
        elif step.state == "mismatch":
            self._learn_timeout.start()
            self.status_label.setText("Those looked like two different buttons. Press the same button twice.")
        elif step.state == "done" and action is not None:
            problem = self._config.bind(action, step.signature)
            if problem:
                self._end_learning(problem)
                return
            self._store.save(self._config)
            self._end_learning(f"{ACTION_LABELS[action]} is now: {describe_signature(step.signature)}.")

    def _show_last_seen(self, event: RawEvent) -> None:
        matched = ClickerMatcher(self._config.device_key, self._config.selected_bindings()).feed(event, 0.0)
        text = f"Last button seen from {self._config.device_name}: {describe_signature(event.signature)}"
        self.last_seen_label.setText(text + (f"  →  {ACTION_LABELS[matched]}" if matched else ""))

    # ---- keeping the clicker's own keys away from this window ------------------------------------------------------
    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.ShortcutOverride):
            if self._swallow_keys or self._clock() < self._swallow_until:
                window = watched.window() if hasattr(watched, "window") else None
                if window is self:
                    return True
        return super().eventFilter(watched, event)

    def done(self, result: int) -> None:  # noqa: N802 - Qt override
        self._refresh_timer.stop()
        self._learn_timeout.stop()
        self._settle.stop()
        self._swallow_keys = False
        self._listener.stop()
        QApplication.instance().removeEventFilter(self)
        super().done(result)
