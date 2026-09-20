"""Keep recorder-owned windows (control overlay, region outline) out of the recording itself."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtWidgets import QWidget

_WDA_EXCLUDEFROMCAPTURE = 0x00000011


def exclude_from_capture(widget: QWidget) -> bool:
    """Hide *widget* from screen capture (gdigrab, Snipping Tool, ...). Windows 10 2004+; returns whether it took effect."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        return bool(user32.SetWindowDisplayAffinity(int(widget.winId()), _WDA_EXCLUDEFROMCAPTURE))
    except (AttributeError, OSError):
        return False
