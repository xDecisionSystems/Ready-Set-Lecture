"""App-level preferences persisted across runs via QSettings (registry on
Windows, plist on macOS, ini on Linux)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QSettings

_ORG = "readysetlecture"
_APP = "readysetlecture"
# The program used to be called VideoTrim and saved its settings under that name. They are copied across the first time the
# new store is empty, so nobody starts from scratch; the old copy is left where it is.
_LEGACY_ORG = "VideoTrim"
_LEGACY_APP = "VideoTrim"
_stores_checked: set[str] = set()
_KEY_OUTPUT_DIR = "export/output_dir"
_KEY_COLOR_SPEC = "detect/color_spec"
_KEY_RECORD_FAVORITE_DIRS = "record/favorite_output_dirs"
_KEY_RECORD_LAST_MIC = "record/last_mic_device"
_KEY_RECORD_LAST_SOURCE = "record/last_source"
_KEY_RECORD_GAIN = "record/gain"
_KEY_RECORD_HIDE_CURSOR = "record/hide_cursor"
_KEY_RECORD_SHOW_PREVIEW = "record/show_camera_preview"
_KEY_RECORD_RED_WHEN_PAUSED = "record/red_screen_when_paused"
_KEY_RECORD_CLICKER = "record/bt_clicker"
_KEY_RECORD_CAMERA_FORMAT = "record/camera_format"
_KEY_USE_GPU_ENCODING = "encode/use_gpu"
_CAMERA_FORMATS = ("auto", "h264", "hevc", "mjpeg", "raw")


def _import_legacy_settings(current: QSettings, legacy: QSettings) -> int:
    """Copy every value from the legacy store into an empty current one. Returns how many were copied."""
    if current.allKeys():
        return 0
    keys = legacy.allKeys()
    for key in keys:
        current.setValue(key, legacy.value(key))
    if keys:
        current.sync()
    return len(keys)


def _settings() -> QSettings:
    # Explicit format on purpose: QSettings(org, app) always uses the native store (the Windows registry) and
    # ignores QSettings.setDefaultFormat(), which is how tests redirect settings to a scratch file.
    current = QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, _ORG, _APP)
    if current.fileName() not in _stores_checked:
        _stores_checked.add(current.fileName())
        legacy = QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, _LEGACY_ORG, _LEGACY_APP)
        _import_legacy_settings(current, legacy)
    return current


def get_output_dir() -> Path | None:
    settings = _settings()
    value = settings.value(_KEY_OUTPUT_DIR, "")
    return Path(value) if value else None


def set_output_dir(path: str | Path) -> None:
    settings = _settings()
    settings.setValue(_KEY_OUTPUT_DIR, str(path))


def get_last_color_spec() -> dict | None:
    """The most recently calibrated color-marker region/color, as a dict
    (see ColorSpec.to_dict). The camera rig and paper location are typically
    reused across many recordings, so a newly opened video is pre-seeded
    with this instead of making the user redraw the box every time."""
    settings = _settings()
    value = settings.value(_KEY_COLOR_SPEC, "")
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def set_last_color_spec(spec_dict: dict) -> None:
    settings = _settings()
    settings.setValue(_KEY_COLOR_SPEC, json.dumps(spec_dict))


def _dir_key(path: Path) -> str:
    """Two spellings of the same folder (case, slashes, .. segments) compare equal."""
    return os.path.normcase(str(path.resolve()))


def same_folder(first: str | Path, second: str | Path) -> bool:
    return _dir_key(Path(first)) == _dir_key(Path(second))


def get_favorite_output_dirs() -> list[Path]:
    """Starred recording folders, in the order they were starred."""
    value = _settings().value(_KEY_RECORD_FAVORITE_DIRS, "[]")
    try:
        values = json.loads(value) if isinstance(value, str) else []
    except (TypeError, ValueError):
        return []
    return [Path(item) for item in values if isinstance(item, str) and item]


def _save_favorite_output_dirs(dirs: list[Path]) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_FAVORITE_DIRS, json.dumps([str(item) for item in dirs]))
    settings.sync()


def is_favorite_output_dir(path: str | Path) -> bool:
    key = _dir_key(Path(path))
    return any(_dir_key(item) == key for item in get_favorite_output_dirs())


def add_favorite_output_dir(path: str | Path) -> None:
    if not is_favorite_output_dir(path):
        _save_favorite_output_dirs(get_favorite_output_dirs() + [Path(path)])


def remove_favorite_output_dir(path: str | Path) -> None:
    key = _dir_key(Path(path))
    _save_favorite_output_dirs([item for item in get_favorite_output_dirs() if _dir_key(item) != key])


def _get_record_string(key: str) -> str | None:
    value = _settings().value(key, "")
    return str(value) if value else None


def get_last_mic_device() -> str | None:
    return _get_record_string(_KEY_RECORD_LAST_MIC)


def set_last_mic_device(name: str) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_LAST_MIC, name)
    settings.sync()


def get_record_gain() -> float:
    try:
        return max(0.0, float(_settings().value(_KEY_RECORD_GAIN, 1.0)))
    except (TypeError, ValueError):
        return 1.0


def set_record_gain(gain: float) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_GAIN, float(gain))
    settings.sync()


def get_hide_cursor() -> bool:
    """Whether screen recordings leave out the mouse cursor (the floating controls' checkbox remembers its state)."""
    return str(_settings().value(_KEY_RECORD_HIDE_CURSOR, "false")).lower() in ("true", "1")


def set_hide_cursor(hidden: bool) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_HIDE_CURSOR, "true" if hidden else "false")
    settings.sync()


def get_show_camera_preview() -> bool:
    """Whether the full-screen camera preview opens when a camera recording starts (the controls' checkbox remembers it)."""
    return str(_settings().value(_KEY_RECORD_SHOW_PREVIEW, "true")).lower() in ("true", "1")


def set_show_camera_preview(shown: bool) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_SHOW_PREVIEW, "true" if shown else "false")
    settings.sync()


def get_red_when_paused() -> bool:
    """Whether the screen turns red while a recording is paused (the controls' red square remembers its state)."""
    return str(_settings().value(_KEY_RECORD_RED_WHEN_PAUSED, "false")).lower() in ("true", "1")


def set_red_when_paused(on: bool) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_RED_WHEN_PAUSED, "true" if on else "false")
    settings.sync()


def get_gpu_encoding() -> bool:
    """Whether recording and precise export encode on the graphics chip when this machine has a working one (on unless turned off)."""
    return str(_settings().value(_KEY_USE_GPU_ENCODING, "true")).lower() in ("true", "1")


def set_gpu_encoding(on: bool) -> None:
    settings = _settings()
    settings.setValue(_KEY_USE_GPU_ENCODING, "true" if on else "false")
    settings.sync()


def get_camera_format() -> str:
    """Which format to take from a camera: "auto" (the camera's own H.264/H.265 when it has one), or "h264", "hevc", "mjpeg", "raw"."""
    value = str(_settings().value(_KEY_RECORD_CAMERA_FORMAT, "auto") or "auto")
    return value if value in _CAMERA_FORMATS else "auto"


def set_camera_format(choice: str) -> None:
    if choice not in _CAMERA_FORMATS:
        return
    settings = _settings()
    settings.setValue(_KEY_RECORD_CAMERA_FORMAT, choice)
    settings.sync()


def get_clicker_config() -> str:
    """The BT clicker setup (chosen device and its buttons) as the JSON text app.recorder.core.clicker reads and writes."""
    return str(_settings().value(_KEY_RECORD_CLICKER, "") or "")


def set_clicker_config(text: str) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_CLICKER, text)
    settings.sync()


def get_last_record_source() -> str | None:
    return _get_record_string(_KEY_RECORD_LAST_SOURCE)


def set_last_record_source(source_key: str) -> None:
    settings = _settings()
    settings.setValue(_KEY_RECORD_LAST_SOURCE, source_key)
    settings.sync()
