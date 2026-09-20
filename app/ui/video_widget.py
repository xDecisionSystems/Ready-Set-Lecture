"""Embedded mpv playback widget.

Wraps libmpv (via python-mpv) inside a Qt native window. mpv owns the actual
decoding/rendering; this widget just hands it a window handle and forwards
playback commands. python-mpv's property-observer callbacks fire on mpv's own
thread, so state changes are relayed to Qt via signals (Qt auto-queues
cross-thread signal/slot delivery, keeping this safe without manual locking).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget


def _register_libmpv_search_path() -> None:
    """Make sure libmpv can be found before python-mpv tries to load it.

    Windows has no system package manager entry for libmpv, so it isn't on
    %PATH% by default. We look next to the interpreter (covers our conda env
    during development) and in the project's packaging/vendor directory
    (covers running from source, and mirrors where the PyInstaller build will
    pull it from), and add whichever exists to the DLL search path.
    """
    if sys.platform != "win32":
        return

    candidates = [
        Path(sys.prefix),
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "packaging" / "vendor" / "win-64",
        Path(__file__).resolve().parents[2] / "packaging" / "vendor" / "win-64",
    ]
    for directory in candidates:
        if (directory / "libmpv-2.dll").exists():
            os.add_dll_directory(str(directory))
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            return


_register_libmpv_search_path()

import mpv  # noqa: E402  (must come after the DLL search path is registered)


class VideoWidget(QWidget):
    position_changed = Signal(float)
    duration_changed = Signal(float)
    pause_changed = Signal(bool)
    end_of_file = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Force a real native window handle before mpv attaches to it.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen)
        self.setAutoFillBackground(False)

        self.player = mpv.MPV(
            wid=str(int(self.winId())),
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            keep_open=True,
            hwdec="auto",
        )

        self.player.observe_property(
            "time-pos", lambda name, value: self.position_changed.emit(value or 0.0)
        )
        self.player.observe_property(
            "duration", lambda name, value: self.duration_changed.emit(value or 0.0)
        )
        self.player.observe_property(
            "pause", lambda name, value: self.pause_changed.emit(bool(value))
        )
        self.player.observe_property("eof-reached", self._on_eof)

    def _on_eof(self, name: str, value: object) -> None:
        if value:
            self.end_of_file.emit()

    def load(self, path: str) -> None:
        self.player.pause = True
        self.player.play(path)

    def toggle_pause(self) -> None:
        self.player.pause = not self.player.pause

    def set_paused(self, paused: bool) -> None:
        self.player.pause = paused

    def set_speed(self, factor: float) -> None:
        self.player.speed = factor

    def seek_relative(self, seconds: float) -> None:
        self.player.seek(seconds, reference="relative")

    def seek_absolute(self, seconds: float) -> None:
        self.player.seek(seconds, reference="absolute", precision="exact")

    def seek_preview(self, seconds: float) -> None:
        """Fast, keyframe-snapped seek for responsive scrubbing while dragging."""
        self.player.seek(seconds, reference="absolute", precision="keyframes")

    def shutdown(self) -> None:
        try:
            self.player.terminate()
        except Exception:
            pass
