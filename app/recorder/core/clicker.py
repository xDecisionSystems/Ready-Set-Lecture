"""BT clicker buttons: how a press is described, how the choices are remembered, and how a press becomes an action.

A Bluetooth clicker is, to Windows, an ordinary input device (keyboard, mouse or media-control). So a "button" is
identified by the device it came from plus what it sent: a key, a mouse button, or a raw HID report.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

ACTION_PAUSE = "pause"
ACTION_MARK = "mark"
ACTIONS = (ACTION_PAUSE, ACTION_MARK)
ACTION_LABELS = {ACTION_PAUSE: "Record / Pause", ACTION_MARK: "Mark"}

_SIGNATURE = re.compile(r"^(?:(?:key|mouse):[0-9a-z]{1,16}|hid:(?:[0-9a-f]{2}|\?\?){1,64})$")

_KEY_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause/Break", 0x1B: "Esc", 0x20: "Space",
    0x21: "Page Up", 0x22: "Page Down", 0x23: "End", 0x24: "Home", 0x25: "Left arrow", 0x26: "Up arrow",
    0x27: "Right arrow", 0x28: "Down arrow", 0x2C: "Print Screen", 0x2D: "Insert", 0x2E: "Delete",
    0x5B: "Left Windows", 0x5C: "Right Windows", 0x5D: "Menu",
    0xA6: "Browser Back", 0xA7: "Browser Forward", 0xAD: "Volume Mute", 0xAE: "Volume Down", 0xAF: "Volume Up",
    0xB0: "Next Track", 0xB1: "Previous Track", 0xB2: "Stop Media", 0xB3: "Play/Pause Media",
    0xBC: "Comma", 0xBE: "Period", 0xBD: "Minus", 0xBB: "Equals",
    0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt",
}
_MOUSE_NAMES = {"left": "Left mouse button", "right": "Right mouse button", "middle": "Middle mouse button",
                "x1": "Mouse back button", "x2": "Mouse forward button"}


def key_signature(vkey: int, extended: bool = False) -> str:
    return f"key:{vkey:02x}{'e' if extended else ''}"


def mouse_signature(button: str) -> str:
    return f"mouse:{button}"


def hid_signature(report: bytes) -> str:
    return f"hid:{report.hex()}"


def is_valid_signature(text: object) -> bool:
    return isinstance(text, str) and bool(_SIGNATURE.match(text))


def _pairs(code: str) -> list[str]:
    return [code[i:i + 2] for i in range(0, len(code), 2)]


def merge_hid_samples(first: str, second: str) -> str | None:
    """Two presses of one remote button, as hex reports -> one pattern, with '??' where they differ (counters, contact
    ids...). None if they can't be the same button: different lengths, or nothing in common."""
    a, b = _pairs(first), _pairs(second)
    if len(a) != len(b) or not a:
        return None
    merged = [x if x == y else "??" for x, y in zip(a, b)]
    return "".join(merged) if any(part != "??" for part in merged) else None


def hid_pattern_matches(pattern: str, report: str) -> bool:
    p, r = _pairs(pattern), _pairs(report)
    return len(p) == len(r) and all(x == "??" or x == y for x, y in zip(p, r))


def hid_patterns_overlap(a: str, b: str) -> bool:
    x, y = _pairs(a), _pairs(b)
    return len(x) == len(y) and all(p == "??" or q == "??" or p == q for p, q in zip(x, y))


def describe_signature(signature: str) -> str:
    """A name a person can recognise for a stored button, e.g. 'Key: Page Down'."""
    kind, _, code = signature.partition(":")
    if kind == "key":
        vkey = int(code.rstrip("e") or "0", 16)
        if 0x70 <= vkey <= 0x87:
            name = f"F{vkey - 0x6F}"
        elif 0x30 <= vkey <= 0x39 or 0x41 <= vkey <= 0x5A:
            name = chr(vkey)
        else:
            name = _KEY_NAMES.get(vkey, f"0x{vkey:02X}")
        return f"Key: {name}"
    if kind == "mouse":
        return _MOUSE_NAMES.get(code, f"Mouse button {code}")
    if kind == "hid":
        return f"Remote button ({' '.join(_pairs(code))})"
    return signature


@dataclass(frozen=True)
class RawEvent:
    """One button event from an input device, already decoded."""

    device_key: str | None  # which physical device sent it (None when Windows doesn't say)
    kind: str  # "key", "mouse" or "hid"
    code: str
    pressed: bool | None  # None for a HID report, which doesn't say press or release

    @property
    def signature(self) -> str:
        return f"{self.kind}:{self.code}"


class PressDetector:
    """Tells a fresh press from key repeat, and the start of a raw-report burst from the rest of it."""

    STALE_HOLD_SECONDS = 0.8  # a "held" button that has been silent this long has really been released
    BURST_SECONDS = 0.35  # raw reports closer together than this belong to one press (down, moves, up, repeats)

    def __init__(self) -> None:
        self._held: dict[str, float] = {}
        self._last_report: float | None = None

    def is_new(self, event: RawEvent, now: float) -> bool:
        """True for a genuine new press; False for release, repeat, and the tail of a burst."""
        if event.pressed is None:
            last, self._last_report = self._last_report, now
            return last is None or now - last >= self.BURST_SECONDS
        if not event.pressed:
            self._held.pop(event.signature, None)
            return False
        seen, self._held[event.signature] = self._held.get(event.signature), now
        return seen is None or now - seen >= self.STALE_HOLD_SECONDS


class ClickerMatcher:
    """Turns events into actions. Only the chosen device's bound buttons count, and one physical press counts once."""

    def __init__(self, device_key: str, bindings: dict[str, str]) -> None:
        self._device_key = device_key
        self._bindings = dict(bindings)
        self._presses = PressDetector()

    def feed(self, event: RawEvent, now: float) -> str | None:
        if event.device_key != self._device_key or not self._presses.is_new(event, now):
            return None
        for action, signature in self._bindings.items():
            kind, _, code = signature.partition(":")
            if kind == event.kind and (hid_pattern_matches(code, event.code) if kind == "hid" else code == event.code):
                return action
        return None


@dataclass(frozen=True)
class LearnStep:
    state: str  # "waiting", "again" (a remote button needs pressing once more), "done" or "mismatch"
    signature: str | None = None


class ButtonLearner:
    """Works out which button the user pressed on the chosen clicker: one press for a key or mouse button; two for a raw
    HID remote, so that anything that changes from press to press can be told apart from what identifies the button."""

    def __init__(self, device_key: str) -> None:
        self._device_key = device_key
        self._presses = PressDetector()
        self._first_report: str | None = None

    def feed(self, event: RawEvent, now: float) -> LearnStep:
        if event.device_key != self._device_key or not self._presses.is_new(event, now):
            return LearnStep("waiting")
        if event.kind != "hid":
            return LearnStep("done", event.signature)
        if self._first_report is None:
            self._first_report = event.code
            return LearnStep("again")
        merged = merge_hid_samples(self._first_report, event.code)
        if merged is None:
            self._first_report = event.code  # the newest press starts over
            return LearnStep("mismatch")
        return LearnStep("done", f"hid:{merged}")


@dataclass
class ClickerConfig:
    """The chosen clicker and, for each device that has been set up, which button does what."""

    device_key: str | None = None
    device_name: str = ""
    bindings: dict[str, dict[str, str]] = field(default_factory=dict)  # device key -> {action: signature}

    def selected_bindings(self) -> dict[str, str]:
        return dict(self.bindings.get(self.device_key or "", {}))

    def is_ready(self) -> bool:
        return bool(self.device_key and self.selected_bindings())

    def select_device(self, key: str | None, name: str = "") -> None:
        self.device_key, self.device_name = key, name

    def bind(self, action: str, signature: str) -> str | None:
        """Assign a button to an action. Returns why not, if it can't be (a button can only do one thing)."""
        if action not in ACTIONS or not self.device_key or not is_valid_signature(signature):
            return "Choose a clicker first."
        mine = self.bindings.setdefault(self.device_key, {})
        for other, existing in mine.items():
            if other != action and (existing == signature or (signature.startswith("hid:") and existing.startswith("hid:")
                                                              and hid_patterns_overlap(existing[4:], signature[4:]))):
                return f"That button is already used for {ACTION_LABELS[other]}. Clear that first."
        mine[action] = signature
        return None

    def clear(self, action: str) -> None:
        if self.device_key:
            self.bindings.get(self.device_key, {}).pop(action, None)

    def summary(self) -> str:
        if not self.device_key:
            return "Not set up"
        parts = [f"{ACTION_LABELS[a]}: {describe_signature(s).removeprefix('Key: ')}" for a, s in self.selected_bindings().items()]
        return f"{self.device_name or 'Clicker'}  ({', '.join(parts)})" if parts else f"{self.device_name or 'Clicker'}  (no buttons set yet)"

    def to_json(self) -> str:
        return json.dumps({"device": {"key": self.device_key, "name": self.device_name}, "bindings": self.bindings})

    @classmethod
    def from_json(cls, text: str) -> "ClickerConfig":
        """Tolerant: anything unreadable or out of shape is dropped rather than raised."""
        try:
            data = json.loads(text) if text else {}
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        device = data.get("device") if isinstance(data.get("device"), dict) else {}
        key = device.get("key") if isinstance(device.get("key"), str) and device.get("key") else None
        name = device.get("name") if isinstance(device.get("name"), str) else ""
        bindings: dict[str, dict[str, str]] = {}
        raw = data.get("bindings") if isinstance(data.get("bindings"), dict) else {}
        for device_key, actions in raw.items():
            if isinstance(device_key, str) and isinstance(actions, dict):
                clean = {a: s for a, s in actions.items() if a in ACTIONS and is_valid_signature(s)}
                if clean:
                    bindings[device_key] = clean
        return cls(key, name, bindings)
