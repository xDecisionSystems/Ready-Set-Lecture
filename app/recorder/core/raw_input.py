"""Listening for the BT clicker: a Raw Input listener, and the service that turns its presses into recorder actions.

Privacy: Raw Input reports keys from every keyboard, so this must only ever be looked at, never kept. Events from any
device other than the chosen clicker are dropped in ClickerMatcher.feed, and nothing here logs or stores them.
"""
from __future__ import annotations

import sys
import time
from ctypes import wintypes
from typing import Callable, Iterable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Qt, Signal
from PySide6.QtWidgets import QWidget

from app.recorder.core import raw_input_win32 as win
from app.recorder.core.clicker import ClickerConfig, ClickerMatcher, RawEvent
from app.recorder.core.input_devices import device_key, list_input_devices, usages_to_listen_on


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, handler: Callable[[int], None]) -> None:
        super().__init__()
        self._handler = handler

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt override
        if bytes(event_type) == b"windows_generic_MSG":
            self._handler(int(message))
        return False, 0  # never consume: Windows still needs to finish with the message


class RawInputListener(QObject):
    """Emits input_event(RawEvent) for button presses from the devices Windows is asked to report."""

    input_event = Signal(object)

    def __init__(self, parent=None, resolver: Callable[[int], str | None] | None = None) -> None:
        super().__init__(parent)
        self._resolver = resolver or self._resolve
        self._window: QWidget | None = None
        self._filter: _NativeFilter | None = None
        self._usages: frozenset[tuple[int, int]] = frozenset()
        self._handle_keys: dict[int, str | None] = {}

    @property
    def running(self) -> bool:
        return self._window is not None

    def start(self, usages: Iterable[tuple[int, int]]) -> bool:
        """Begin (or change what is) listened to. Returns whether Windows accepted the registration."""
        self.stop()
        if sys.platform != "win32":
            return False
        self._usages = frozenset(usages)
        self._window = QWidget()  # never shown: it only gives Windows somewhere to send the messages
        self._window.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self._hwnd = int(self._window.winId())
        accepted = win.register(self._hwnd, self._usages)
        self._filter = _NativeFilter(self._on_message)
        QCoreApplication.instance().installNativeEventFilter(self._filter)
        return accepted

    def stop(self) -> None:
        if self._filter is not None:
            QCoreApplication.instance().removeNativeEventFilter(self._filter)
            self._filter = None
        if self._window is not None:
            win.unregister(self._usages)
            self._window.deleteLater()
            self._window = None
        self._usages = frozenset()
        self._handle_keys.clear()

    def _resolve(self, handle: int) -> str | None:
        path = win.device_path(handle)
        return device_key(path) if path else None

    def _key_for(self, handle: int) -> str | None:
        if handle not in self._handle_keys:
            self._handle_keys[handle] = self._resolver(handle)
        return self._handle_keys[handle]

    def _on_message(self, message_pointer: int) -> None:
        msg = wintypes.MSG.from_address(message_pointer)
        if msg.message != win.WM_INPUT or int(msg.hWnd or 0) != self._hwnd:
            return
        for decoded in win.decode_raw_input(win.read_raw_input(msg.lParam)):
            self.input_event.emit(RawEvent(self._key_for(decoded.handle), decoded.kind, decoded.code, decoded.pressed))


class ClickerService(QObject):
    """While a recording runs: listens for the chosen clicker and says which action a press asked for."""

    action_triggered = Signal(str)  # ACTION_PAUSE or ACTION_MARK

    def __init__(self, parent=None, listener: RawInputListener | None = None, clock: Callable[[], float] = time.monotonic,
                 device_lister=list_input_devices) -> None:
        super().__init__(parent)
        self._listener = listener or RawInputListener(self)
        self._listener.input_event.connect(self._on_event)
        self._clock = clock
        self._device_lister = device_lister
        self._matcher: ClickerMatcher | None = None

    @property
    def running(self) -> bool:
        return self._matcher is not None

    def start(self, config: ClickerConfig) -> bool:
        """Listen for the config's clicker. Does nothing (and returns False) if no buttons have been set up."""
        self.stop()
        if not config.is_ready():
            return False
        bindings = config.selected_bindings()
        self._matcher = ClickerMatcher(config.device_key, bindings)
        device = next((d for d in self._device_lister(True) if d.key == config.device_key), None)
        usages = set(usages_to_listen_on(device))
        if any(signature.startswith("mouse:") for signature in bindings.values()):
            usages.add((0x01, 0x02))
        return self._listener.start(usages)

    def stop(self) -> None:
        self._listener.stop()
        self._matcher = None

    def _on_event(self, event: RawEvent) -> None:
        action = self._matcher.feed(event, self._clock()) if self._matcher else None
        if action:
            self.action_triggered.emit(action)
