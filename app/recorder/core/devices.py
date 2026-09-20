"""Windows capture-device discovery and display geometry helpers."""
from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass

from PySide6.QtGui import QGuiApplication

from app.core.ffmpeg_locator import find_ffmpeg


@dataclass(frozen=True)
class CameraDevice:
    name: str
    capture_id: str


@dataclass(frozen=True)
class MicDevice:
    name: str
    capture_id: str


@dataclass(frozen=True)
class ScreenSource:
    qt_name: str
    label: str
    physical_rect: tuple[int, int, int, int]
    is_primary: bool


_SECTION_RE = re.compile(r"DirectShow (video|audio) devices", re.IGNORECASE)
_NAME_RE = re.compile(r'\]\s+"(.+)"\s*$')
# ffmpeg 7+ drops the "DirectShow video devices" headers and tags each device instead:
#   [in#0 @ 000001] "Surface Camera Front" (video)
_TYPED_NAME_RE = re.compile(r'\]\s+"(.+)"\s+\((video|audio|none)\)\s*$', re.IGNORECASE)
_ALT_RE = re.compile(r'Alternative name\s+"(.+)"\s*$')
_SIZE_RE = re.compile(r"s=(\d+)x(\d+)")


def _parse_dshow_device_list(stderr_text: str | bytes) -> tuple[list[CameraDevice], list[MicDevice]]:
    """Parse ffmpeg's diagnostic-only ``-list_devices`` output."""
    text = stderr_text.decode(errors="replace") if isinstance(stderr_text, bytes) else stderr_text
    cameras: list[CameraDevice] = []
    mics: list[MicDevice] = []
    section: str | None = None
    pending: tuple[str, str] | None = None

    def commit() -> None:
        nonlocal pending
        if pending is None:
            return
        kind, name = pending
        device = CameraDevice(name, name) if kind == "video" else MicDevice(name, name)
        (cameras if kind == "video" else mics).append(device)
        pending = None

    for line in text.splitlines():
        section_match = _SECTION_RE.search(line)
        if section_match:
            commit()
            section = section_match.group(1).lower()
            continue
        alt_match = _ALT_RE.search(line)
        if alt_match and pending is not None:
            kind, name = pending
            device = CameraDevice(name, alt_match.group(1)) if kind == "video" else MicDevice(name, alt_match.group(1))
            (cameras if kind == "video" else mics).append(device)
            pending = None
            continue
        typed_match = _TYPED_NAME_RE.search(line)
        if typed_match:
            commit()
            kind = typed_match.group(2).lower()
            # "(none)" = ffmpeg couldn't tell the media type (e.g. virtual cameras), so it isn't offered.
            pending = (kind, typed_match.group(1)) if kind in ("video", "audio") else None
            continue
        name_match = _NAME_RE.search(line)
        if name_match and section:
            commit()
            pending = (section, name_match.group(1))
    commit()
    return cameras, mics


_FPS_RE = re.compile(r"fps=([0-9.]+)")
_FORMAT_RE = re.compile(r"\b(pixel_format|vcodec)=(\S+)")
TARGET_CAMERA_FPS = 30


@dataclass(frozen=True)
class CameraMode:
    """One line of a camera's option list: a size offered in one format, at up to *max_fps* (None if the line gives no rate)."""

    width: int
    height: int
    max_fps: float | None
    format: str | None  # "h264", "mjpeg", "nv12", "yuyv422", ...; None if the line names none


def _parse_dshow_modes(stderr_text: str | bytes) -> list[CameraMode]:
    """The modes a camera lists, in the order listed. A line looks like
    ``pixel_format=nv12  min s=1920x1080 fps=5 max s=1920x1080 fps=30`` (or ``vcodec=h264 ...`` for a camera that compresses in hardware)."""
    text = stderr_text.decode(errors="replace") if isinstance(stderr_text, bytes) else stderr_text
    modes: list[CameraMode] = []
    for line in text.splitlines():
        sizes = [(int(width), int(height)) for width, height in _SIZE_RE.findall(line)]
        fps = [float(value) for value in _FPS_RE.findall(line)]
        found = _FORMAT_RE.search(line)
        for width, height in dict.fromkeys(sizes):
            modes.append(CameraMode(width, height, max(fps) if fps else None, found.group(2).lower() if found else None))
    return modes


def _size_rates(modes: list[CameraMode]) -> list[tuple[int, int, float | None]]:
    """(width, height, fastest frame rate it is offered at in any format) for each size, in the order listed.
    The rate is None when no line for the size gives one."""
    rates: dict[tuple[int, int], float | None] = {}
    for mode in modes:
        size = (mode.width, mode.height)
        if size not in rates or rates[size] is None:
            rates[size] = mode.max_fps
        elif mode.max_fps is not None:
            rates[size] = max(rates[size], mode.max_fps)
    return [(width, height, rate) for (width, height), rate in rates.items()]


def _parse_dshow_size_rates(stderr_text: str | bytes) -> list[tuple[int, int, float | None]]:
    return _size_rates(_parse_dshow_modes(stderr_text))


HARDWARE_FORMATS = ("h264", "hevc")  # the camera itself compresses the picture; the computer only saves it
_COMPRESSED_FORMATS = ("h264", "hevc", "mjpeg")
FORMAT_NAMES = {"h264": "H.264", "hevc": "H.265", "mjpeg": "MJPEG", "raw": "Uncompressed"}


def _kind(mode: CameraMode) -> str | None:
    """What the person would call this mode's format: h264, hevc, mjpeg, or raw (any uncompressed pixel format)."""
    if mode.format in _COMPRESSED_FORMATS:
        return mode.format
    return "raw" if mode.format else None


def _modes_at_rate(modes: list[CameraMode], kind: str, fps: float) -> list[CameraMode]:
    """Modes in this format offered at *fps* or faster. (An unknown rate doesn't count: better to be sure.)"""
    return [m for m in modes if _kind(m) == kind and m.max_fps is not None and m.max_fps >= fps]


def _offers(modes: list[CameraMode], kind: str, size: tuple[int, int], fps: float) -> bool:
    return any((m.width, m.height) == tuple(size) for m in _modes_at_rate(modes, kind, fps))


def available_camera_formats(modes: list[CameraMode], fps: float = TARGET_CAMERA_FPS) -> list[str]:
    """The formats this camera can deliver at *fps* or faster, in the order to offer them."""
    return [kind for kind in ("h264", "hevc", "mjpeg", "raw") if _modes_at_rate(modes, kind, fps)]


def _parse_dshow_video_options(stderr_text: str | bytes) -> list[tuple[int, int]]:
    return [(width, height) for width, height, _ in _parse_dshow_size_rates(stderr_text)]


def list_dshow_devices(timeout: float = 8.0) -> tuple[list[CameraDevice], list[MicDevice]]:
    try:
        result = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], []
    return _parse_dshow_device_list(result.stderr)


_video_options_cache: dict[str, list[CameraMode]] = {}


def _sizes_that_reach(size_rates: list[tuple[int, int, float | None]], min_fps: float | None) -> list[tuple[int, int]]:
    """The sizes offered at *min_fps* or faster (a size whose rate isn't known is kept). If none qualify, all of them: better
    to try a slower mode than to offer nothing."""
    if min_fps is None:
        return [(width, height) for width, height, _ in size_rates]
    fast = [(width, height) for width, height, rate in size_rates if rate is None or rate >= min_fps]
    return fast or [(width, height) for width, height, _ in size_rates]


def list_dshow_camera_modes(capture_id: str, timeout: float = 8.0) -> list[CameraMode]:
    """Every size/format/rate a camera lists. Cached per camera: asking costs about half a second, and the answer doesn't change."""
    cached = _video_options_cache.get(capture_id)
    if cached is not None:
        return list(cached)
    try:
        result = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-f", "dshow", "-list_options", "true", "-i", _video_spec(capture_id)],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    modes = _parse_dshow_modes(result.stderr)
    if modes:  # never cache a failed or empty answer
        _video_options_cache[capture_id] = modes
    return list(modes)


def cached_camera_modes(capture_id: str) -> list[CameraMode] | None:
    """The camera's modes if they have been asked for already (no waiting), else None."""
    cached = _video_options_cache.get(capture_id)
    return list(cached) if cached is not None else None


def list_dshow_video_options(capture_id: str, timeout: float = 8.0, min_fps: float | None = TARGET_CAMERA_FPS) -> list[tuple[int, int]]:
    """The frame sizes a camera offers at *min_fps* or faster (4K is often only 30 fps in one format, and 5-15 fps in the rest)."""
    return _sizes_that_reach(_size_rates(list_dshow_camera_modes(capture_id, timeout)), min_fps)


def prefetch_dshow_video_options(capture_id: str) -> None:
    """Ask for a camera's sizes in the background so the answer is cached before it is needed."""
    if capture_id not in _video_options_cache:
        threading.Thread(target=list_dshow_video_options, args=(capture_id,), daemon=True).start()


def _video_spec(capture_id: str) -> str:
    # No quotes: passed to subprocess as a list element, so they would reach ffmpeg literally.
    return capture_id if capture_id.startswith("video=") else f"video={capture_id}"


UHD = (3840, 2160)


def pick_camera_native_size(available: list[tuple[int, int]], cap: tuple[int, int] = (1920, 1080), allow_uhd: bool = True) -> tuple[int, int]:
    """Choose the largest available size not exceeding *cap* (1080p), else the closest; except that a camera offering 4K (3840x2160)
    gets 4K. The cap stays at 1080p on purpose: a bigger cap would pick odd taller modes such as 1920x1440 that some cameras list."""
    if allow_uhd and UHD in available:
        return UHD
    if not available:
        return cap
    bounded = [size for size in available if size[0] <= cap[0] and size[1] <= cap[1]]
    return max(bounded or available, key=lambda size: (size[0] * size[1], size[0], size[1]))


@dataclass(frozen=True)
class CameraInput:
    """What to ask the camera for: a size, and a format ("h264", "hevc", "mjpeg", "pixel:nv12" for an uncompressed one, or None to
    let DirectShow choose)."""

    size: tuple[int, int]
    input_format: str | None


def plan_camera_input(modes: list[CameraMode], choice: str = "auto", fps: float = TARGET_CAMERA_FPS) -> CameraInput:
    """The size and format to record, for what the person chose ("auto", "h264", "hevc", "mjpeg" or "raw").

    A format the camera doesn't offer at *fps* falls back to automatic. A chosen format gets the biggest size it offers (4K if it can).
    Automatic prefers a format the camera compresses itself, H.264 before H.265, because then the computer only saves the stream. It
    allows 4K only that way: a computer can't decode 4K MJPEG and encode it again in real time (FFmpeg's MJPEG decoder is single-threaded),
    so without a hardware-compressed 4K mode it stays at 1080p and below.
    """
    if choice in available_camera_formats(modes, fps):
        chosen = _modes_at_rate(modes, choice, fps)
        size = pick_camera_native_size(list(dict.fromkeys((m.width, m.height) for m in chosen)))
        if choice == "raw":
            offered = [m.format for m in chosen if (m.width, m.height) == size]
            # NV12 is half the data of YUY2 and needs no chroma conversion on its way to H.264, so prefer it.
            return CameraInput(size, f"pixel:{next((f for f in ('nv12', 'yuyv422') if f in offered), offered[0])}")
        return CameraInput(size, choice)
    sizes = _sizes_that_reach(_size_rates(modes), fps)
    hardware_uhd = any(_offers(modes, kind, UHD, fps) for kind in HARDWARE_FORMATS)
    size = pick_camera_native_size(sizes, allow_uhd=hardware_uhd)
    for kind in HARDWARE_FORMATS:
        if _offers(modes, kind, size, fps):
            return CameraInput(size, kind)
    return CameraInput(size, None)


def camera_format_choices(modes: list[CameraMode], fps: float = TARGET_CAMERA_FPS) -> list[tuple[str, str]]:
    """(choice, label) for the "Camera format" list: Automatic, then each format this camera offers, each with the size it would record."""
    def where(plan: CameraInput) -> str:
        return f"{plan.size[0]}×{plan.size[1]}"

    auto = plan_camera_input(modes, "auto", fps)
    auto_how = f"{FORMAT_NAMES[auto.input_format]} from the camera" if auto.input_format in HARDWARE_FORMATS else "the computer encodes"
    choices = [("auto", f"Automatic: {auto_how}, {where(auto)}")]
    for kind in available_camera_formats(modes, fps):
        who = "the camera encodes" if kind in HARDWARE_FORMATS else "the computer encodes"
        choices.append((kind, f"{FORMAT_NAMES[kind]}: {who}, {where(plan_camera_input(modes, kind, fps))}"))
    return choices


def list_screens() -> list[ScreenSource]:
    app = QGuiApplication.instance()
    if app is None:
        return []
    primary = app.primaryScreen()
    sources: list[ScreenSource] = []
    for index, screen in enumerate(app.screens(), start=1):
        geometry = screen.geometry()
        ratio = screen.devicePixelRatio()
        rect = tuple(round(value * ratio) for value in (geometry.x(), geometry.y(), geometry.width(), geometry.height()))
        is_primary = screen is primary
        suffix = " (Primary)" if is_primary else ""
        sources.append(ScreenSource(screen.name(), f"Screen {index} — {rect[2]}x{rect[3]}{suffix}", rect, is_primary))
    return sources
