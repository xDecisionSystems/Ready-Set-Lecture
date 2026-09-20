"""Finding the Bluetooth clicker among Windows' input devices, and giving it a name a person will recognise.

Windows exposes a paired, connected Bluetooth clicker as one physical device with several HID "collections" (a
keyboard, a media-control part, ...), each with its own raw-input path. Paths carry the Bluetooth service UUID, the
vendor/product ids, and an instance id shared by all of one device's collections; that shared part is what identifies it.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

# Bluetooth HID (classic) and HID over GATT (Bluetooth LE)
_BLUETOOTH_HID_SERVICES = ("00001124-0000-1000-8000-00805f9b34fb", "00001812-0000-1000-8000-00805f9b34fb")
_DEFAULT_USAGES = frozenset({(0x01, 0x06), (0x0C, 0x01)})  # keyboard, consumer control: what almost every clicker sends
_MOUSE_USAGE, _KEYBOARD_USAGE = (0x01, 0x02), (0x01, 0x06)


@dataclass(frozen=True)
class ParsedPath:
    vid: int | None
    pid: int | None
    bluetooth: bool
    address: str | None  # the Bluetooth address, when the path carries it (LE devices)
    instance: str | None  # e.g. "9&13243a44&0": shared by every collection of one device
    collection: int | None


def parse_device_path(path: str) -> ParsedPath:
    lower = path.lower()
    vid = re.search(r"vid[_&]([0-9a-f]{4,8})", lower)
    pid = re.search(r"pid[_&]([0-9a-f]{4})", lower)
    address = re.search(r"_([0-9a-f]{12})(?=&col|#)", lower)
    collection = re.search(r"&col(\d+)", lower)
    parts = path.split("#")
    instance = None
    if len(parts) > 2 and "&" in parts[2]:
        instance = parts[2].rsplit("&", 1)[0].lower()  # drop the trailing per-collection index
    return ParsedPath(
        vid=int(vid.group(1)[-4:], 16) if vid else None,
        pid=int(pid.group(1), 16) if pid else None,
        bluetooth=any(uuid in lower for uuid in _BLUETOOTH_HID_SERVICES),
        address=address.group(1) if address else None,
        instance=instance,
        collection=int(collection.group(1)) if collection else None,
    )


def device_key(path: str) -> str:
    """A stable identity for the physical device: the same for all its collections, and across reconnects."""
    parsed = parse_device_path(path)
    ident = parsed.address or parsed.instance or path.lower()
    vid = f"{parsed.vid:04x}" if parsed.vid is not None else "----"
    pid = f"{parsed.pid:04x}" if parsed.pid is not None else "----"
    return f"{vid}:{pid}:{ident}"


class _WinRegistry:
    """Just enough of HKLM\\SYSTEM\\CurrentControlSet\\Enum to read; replaceable in tests."""

    def subkeys(self, path: str) -> list[str]:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Enum\{path}") as key:
                names, index = [], 0
                while True:
                    try:
                        names.append(winreg.EnumKey(key, index))
                    except OSError:
                        return names
                    index += 1
        except OSError:
            return []

    def value(self, path: str, name: str) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Enum\{path}") as key:
                data, _ = winreg.QueryValueEx(key, name)
                return data if isinstance(data, str) else None
        except OSError:
            return None


def _friendly_name_for_address(address: str, registry) -> str | None:
    for enumerator in ("BTHLE", "BTHENUM"):
        for instance in registry.subkeys(rf"{enumerator}\Dev_{address}"):
            path = rf"{enumerator}\Dev_{address}\{instance}"
            name = registry.value(path, "FriendlyName")
            if name:
                return name
            described = registry.value(path, "DeviceDesc")
            if described and ";" in described:
                return described.rsplit(";", 1)[1]
    return None


def _address_for_instance(instance: str, pid: int | None, registry) -> str | None:
    """A classic Bluetooth HID path has no address, only an id derived from its parent; find the parent that has it."""
    for service in registry.subkeys("BTHENUM"):
        lowered = service.lower()
        if not any(f"{{{uuid}}}" in lowered for uuid in _BLUETOOTH_HID_SERVICES):
            continue
        if pid is not None and f"pid&{pid:04x}" not in lowered:
            continue
        for node in registry.subkeys(rf"BTHENUM\{service}"):
            if (registry.value(rf"BTHENUM\{service}\{node}", "ParentIdPrefix") or "").lower() == instance:
                found = re.search(r"&([0-9a-f]{12})_c", node.lower())
                if found:
                    return found.group(1)
    return None


def bluetooth_name(path: str, registry=None) -> str | None:
    """The name the device has in Windows' Bluetooth list (e.g. 'Surface Slim Pen 2'), if it can be found."""
    if sys.platform != "win32" and registry is None:
        return None
    registry = registry or _WinRegistry()
    parsed = parse_device_path(path)
    address = parsed.address
    if address is None and parsed.bluetooth and parsed.instance:
        address = _address_for_instance(parsed.instance, parsed.pid, registry)
    return _friendly_name_for_address(address, registry) if address else None


@dataclass(frozen=True)
class InputDevice:
    key: str
    name: str
    bluetooth: bool
    kinds: frozenset[str]  # "keyboard", "mouse", "hid"
    usages: frozenset[tuple[int, int]]  # (usage page, usage) pairs to listen on to hear this device


def usages_to_listen_on(device: InputDevice | None, want_mouse: bool = False) -> frozenset[tuple[int, int]]:
    """What to register with Windows to hear the device. Unknown (e.g. not connected yet): the usual clicker kinds."""
    if device is None:
        return _DEFAULT_USAGES | ({_MOUSE_USAGE} if want_mouse else frozenset())
    return device.usages


def list_input_devices(include_all: bool = False) -> list[InputDevice]:
    """The connected input devices, one entry per physical device. Bluetooth ones only, unless include_all."""
    if sys.platform != "win32":
        return []
    from app.recorder.core import raw_input_win32 as win

    groups: dict[str, dict] = {}
    for handle, kind in win.raw_input_device_list():
        path = win.device_path(handle)
        if not path:
            continue
        parsed = parse_device_path(path)
        if not (parsed.bluetooth or include_all):
            continue
        entry = groups.setdefault(device_key(path), {"paths": [], "kinds": set(), "usages": set(), "bluetooth": parsed.bluetooth})
        entry["paths"].append(path)
        entry["kinds"].add(kind)
        usage = {"keyboard": _KEYBOARD_USAGE, "mouse": _MOUSE_USAGE}.get(kind) or win.hid_usage(handle)
        if usage:
            entry["usages"].add(usage)
    devices = []
    registry = _WinRegistry()
    for key, entry in groups.items():
        name = next((n for n in (bluetooth_name(p, registry) for p in entry["paths"]) if n), None)
        name = name or next((n for n in (win.product_string(p) for p in entry["paths"]) if n), None)
        if not name:
            parsed = parse_device_path(entry["paths"][0])
            name = "Bluetooth input device" if entry["bluetooth"] else "Input device"
            if parsed.vid is not None:
                name += f" (VID {parsed.vid:04X}, PID {parsed.pid or 0:04X})"
        devices.append(InputDevice(key, name, entry["bluetooth"], frozenset(entry["kinds"]), frozenset(entry["usages"])))
    return sorted(devices, key=lambda d: (not d.bluetooth, d.name.lower()))
