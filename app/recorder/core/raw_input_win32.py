"""The Windows Raw Input API, in small pieces: list devices, register to hear them, and decode what arrives.

Raw Input tells us which device a press came from, even when another program has the focus. Nothing here keeps or
logs what it sees; the listener above it drops everything that isn't from the chosen clicker straight away.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Iterable, NamedTuple

WM_INPUT = 0x00FF
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD, RIM_TYPEHID = 0, 1, 2
_KIND_NAMES = {RIM_TYPEMOUSE: "mouse", RIM_TYPEKEYBOARD: "keyboard", RIM_TYPEHID: "hid"}
RI_KEY_BREAK, RI_KEY_E0 = 0x01, 0x02
_MOUSE_DOWN = ((0x0001, "left"), (0x0004, "right"), (0x0010, "middle"), (0x0040, "x1"), (0x0100, "x2"))
_MOUSE_UP = ((0x0002, "left"), (0x0008, "right"), (0x0020, "middle"), (0x0080, "x1"), (0x0200, "x2"))
_RID_INPUT, _RIDI_DEVICENAME, _RIDI_DEVICEINFO = 0x10000003, 0x20000007, 0x2000000B
_RIDEV_REMOVE, _RIDEV_INPUTSINK = 0x00000001, 0x00000100


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wintypes.USHORT), ("ulButtons", wintypes.ULONG), ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [("hDevice", wintypes.HANDLE), ("dwType", wintypes.DWORD)]


class _InfoHid(ctypes.Structure):
    _fields_ = [("dwVendorId", wintypes.DWORD), ("dwProductId", wintypes.DWORD), ("dwVersionNumber", wintypes.DWORD),
                ("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT)]


class _InfoUnion(ctypes.Union):
    _fields_ = [("pad", ctypes.c_ubyte * 24), ("hid", _InfoHid)]


class RID_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwType", wintypes.DWORD), ("info", _InfoUnion)]


class Decoded(NamedTuple):
    handle: int  # the sending device's Raw Input handle (0 for injected input)
    kind: str  # "key", "mouse" or "hid"
    code: str
    pressed: bool | None  # None for a HID report


def key_code(vkey: int, extended: bool) -> str:
    return f"{vkey:02x}{'e' if extended else ''}"


def decode_raw_input(data: bytes) -> list[Decoded]:
    """What a WM_INPUT packet says, as zero or more button events. Mouse movement, wheels, and idle (all-zero)
    HID reports say nothing worth acting on and give an empty list."""
    header_size = ctypes.sizeof(RAWINPUTHEADER)
    if len(data) < header_size:
        return []
    header = RAWINPUTHEADER.from_buffer_copy(data)
    handle = int(header.hDevice or 0)
    body = data[header_size:]
    if header.dwType == RIM_TYPEKEYBOARD and len(body) >= ctypes.sizeof(RAWKEYBOARD):
        key = RAWKEYBOARD.from_buffer_copy(body)
        if key.VKey in (0, 0xFF):  # the "fake" keys Windows adds around Shift / NumLock
            return []
        return [Decoded(handle, "key", key_code(key.VKey, bool(key.Flags & RI_KEY_E0)), not (key.Flags & RI_KEY_BREAK))]
    if header.dwType == RIM_TYPEMOUSE and len(body) >= ctypes.sizeof(RAWMOUSE):
        buttons = RAWMOUSE.from_buffer_copy(body).ulButtons & 0xFFFF
        return ([Decoded(handle, "mouse", name, True) for bit, name in _MOUSE_DOWN if buttons & bit]
                + [Decoded(handle, "mouse", name, False) for bit, name in _MOUSE_UP if buttons & bit])
    if header.dwType == RIM_TYPEHID and len(body) >= 8:
        size, count = int.from_bytes(body[0:4], "little"), int.from_bytes(body[4:8], "little")
        if size == 0 or count == 0 or len(body) < 8 + size * count:
            return []
        reports = [bytes(body[8 + i * size: 8 + (i + 1) * size]) for i in range(count)]
        return [Decoded(handle, "hid", report.hex(), None) for report in reports if any(report)]
    return []


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetRawInputDeviceList.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]
    user32.GetRawInputDeviceList.restype = wintypes.UINT
    user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]
    user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    user32.GetRawInputData.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
    user32.RegisterRawInputDevices.restype = wintypes.BOOL
    return user32


def raw_input_device_list() -> list[tuple[int, str]]:
    """(handle, kind) for every input device, kind being "keyboard", "mouse" or "hid"."""
    if sys.platform != "win32":
        return []
    user32 = _user32()
    count = wintypes.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    if not count.value:
        return []
    devices = (RAWINPUTDEVICELIST * count.value)()
    filled = user32.GetRawInputDeviceList(devices, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    if filled == 0xFFFFFFFF:
        return []
    return [(int(d.hDevice or 0), _KIND_NAMES[d.dwType]) for d in devices[:filled] if d.dwType in _KIND_NAMES]


def device_path(handle: int) -> str:
    if sys.platform != "win32" or not handle:
        return ""
    user32 = _user32()
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, _RIDI_DEVICENAME, None, ctypes.byref(size))
    if not size.value:
        return ""
    buffer = ctypes.create_unicode_buffer(size.value)
    if user32.GetRawInputDeviceInfoW(handle, _RIDI_DEVICENAME, buffer, ctypes.byref(size)) == 0xFFFFFFFF:
        return ""
    return buffer.value


def hid_usage(handle: int) -> tuple[int, int] | None:
    """(usage page, usage) of a HID collection, e.g. (0x0C, 0x01) for a media-control part."""
    user32 = _user32()
    info = RID_DEVICE_INFO()
    info.cbSize = ctypes.sizeof(info)
    size = wintypes.UINT(ctypes.sizeof(info))
    if user32.GetRawInputDeviceInfoW(handle, _RIDI_DEVICEINFO, ctypes.byref(info), ctypes.byref(size)) == 0xFFFFFFFF:
        return None
    return (int(info.info.hid.usUsagePage), int(info.info.hid.usUsage)) if info.dwType == RIM_TYPEHID else None


def product_string(path: str) -> str:
    """The name the device reports about itself (often empty for Bluetooth devices)."""
    if sys.platform != "win32":
        return ""
    kernel32, hid = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("hid", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
    hid.HidD_GetProductString.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(126)
        return buffer.value.strip() if hid.HidD_GetProductString(handle, buffer, ctypes.sizeof(buffer)) else ""
    finally:
        kernel32.CloseHandle(handle)


def read_raw_input(lparam: int) -> bytes:
    """The packet behind a WM_INPUT message's lParam."""
    user32 = _user32()
    header = ctypes.sizeof(RAWINPUTHEADER)
    size = wintypes.UINT(0)
    user32.GetRawInputData(ctypes.c_void_p(lparam), _RID_INPUT, None, ctypes.byref(size), header)
    if not size.value:
        return b""
    buffer = ctypes.create_string_buffer(size.value)
    if user32.GetRawInputData(ctypes.c_void_p(lparam), _RID_INPUT, buffer, ctypes.byref(size), header) == 0xFFFFFFFF:
        return b""
    return buffer.raw[:size.value]


def register(hwnd: int, usages: Iterable[tuple[int, int]]) -> bool:
    """Ask Windows to send this window WM_INPUT for these (usage page, usage) pairs, even when it is not in front."""
    devices = [RAWINPUTDEVICE(page, usage, _RIDEV_INPUTSINK, hwnd) for page, usage in sorted(set(usages))]
    if not devices:
        return True
    array = (RAWINPUTDEVICE * len(devices))(*devices)
    return bool(_user32().RegisterRawInputDevices(array, len(devices), ctypes.sizeof(RAWINPUTDEVICE)))


def unregister(usages: Iterable[tuple[int, int]]) -> None:
    devices = [RAWINPUTDEVICE(page, usage, _RIDEV_REMOVE, None) for page, usage in sorted(set(usages))]
    if devices:
        array = (RAWINPUTDEVICE * len(devices))(*devices)
        _user32().RegisterRawInputDevices(array, len(devices), ctypes.sizeof(RAWINPUTDEVICE))
