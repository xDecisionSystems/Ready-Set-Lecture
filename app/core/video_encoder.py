"""Which H.264 encoder ffmpeg is told to use, for recording and for export.

The graphics chip comes first, so the CPU is left free: NVIDIA NVENC, Intel Quick Sync (8th generation and newer), AMD (through AMF on
Windows, VA-API on Linux), Apple silicon (VideoToolbox), or on Windows any other chip through Media Foundation. Which of these exist
differs by operating system and by machine, and an ffmpeg that lists an encoder is not proof the hardware is there, so each one is
tried on a few blank frames before it is trusted (once per ffmpeg, in the background at start-up). The first that works is used. When
none does, or the user has switched GPU encoding off, x264 is used.

x264 is the fallback, not the first choice, because of the CPU it costs. On real footage it made files 35% (screen slides) to 5x (camera)
smaller than OpenH264 at 20 Mbps with the same measured quality, and, unlike OpenH264, it keeps up with live 1080p camera capture
(OpenH264 dropped more than half the frames). It is GPL, which Ready, Set, Lecture! now is too (see LICENSE). When the ffmpeg on a machine
has no libx264, for example a build made LGPL-only, the previous OpenH264 settings are used instead, so nothing stops working.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.ffmpeg_locator import find_ffmpeg

_ENCODER_LINE = re.compile(r"^\s*[VAS][.FSXBD]{5}\s+(\S+)\s")  # e.g. " V....D libx264   libx264 H.264 / AVC ..."

# (x264 preset, CRF). CRF asks for a quality instead of a bitrate: lower is better and bigger. "veryfast" was the sweet spot in the
# live tests: "superfast" lacks the tools that shrink static scenes (a camera file 6x bigger), "faster" cost far more CPU.
RECORD_QUALITY = ("veryfast", 20)
EXPORT_QUALITY = ("veryfast", 18)
# Bigger than 1080p (2.07 megapixels), live encoding is at its limit: on the development laptop x264 veryfast managed about 26 fps
# on 4K and superfast about 35, so a 30 fps 4K recording needs the lighter preset. The files are bigger, and it keeps up.
LARGE_FRAME_PIXELS = 2_600_000
RECORD_QUALITY_LARGE = ("superfast", 20)
# A keyframe at least every two seconds at 30 fps. Without it a still scene had a single keyframe in six seconds, and the editor's
# fast (stream-copy) cuts can only start on a keyframe, so they landed far from where you asked.
KEYFRAME_INTERVAL_FRAMES = 60

_encoders_by_ffmpeg: dict[str, frozenset[str]] = {}


def available_encoders(ffmpeg: str) -> frozenset[str]:
    """The names of the encoders this ffmpeg has. Asked once per ffmpeg; a failed answer is not remembered."""
    cached = _encoders_by_ffmpeg.get(ffmpeg)
    if cached is not None:
        return cached
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    names = frozenset(m.group(1) for m in map(_ENCODER_LINE.match, result.stdout.splitlines()) if m and m.group(1) != "=")  # "=" is the legend
    if names:
        _encoders_by_ffmpeg[ffmpeg] = names
    return names


# --- the graphics chip ---------------------------------------------------------------------------------------------------------------
#
# Each encoder is driven by a function from (purpose, frame size) to ffmpeg's output options. They ask for a quality rather than a
# bitrate wherever the encoder can, like x264's CRF, because a bitrate wastes space on still slides and starves a busy camera picture.
# The values aim near x264's CRF 20 (recording) and 18 (export). Only Media Foundation was measured, on an Intel chip: on a desktop
# capture it came within 0.002 SSIM of x264 at about 15% more bytes, and on a busy synthetic picture it needed about 1.7x the bytes. The
# rest are the usual starting points for those encoders. Whatever an ffmpeg or driver refuses fails the test encode and is skipped.
# All of them take nv12, the chip's own layout (the video is still ordinary 4:2:0 that every player shows).

def _fallback_bitrate(pixels: int | None) -> str:
    """For encoders that can't be asked for a quality: about 6 Mbps at 1080p30, scaling with the frame size."""
    return f"{max(2, round((pixels or 1920 * 1080) * 30 * 0.1 / 1_000_000))}M"


def _pick(purpose: str, record, export):
    return export if purpose == "export" else record


def _nvenc_args(purpose: str, pixels: int | None) -> list[str]:
    preset, cq = _pick(purpose, ("p4", 24), ("p5", 22))
    return ["-c:v", "h264_nvenc", "-preset", preset, "-tune", "hq", "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
            "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "nv12"]


def _qsv_args(purpose: str, pixels: int | None) -> list[str]:
    preset, quality = _pick(purpose, ("veryfast", 23), ("medium", 21))
    return ["-c:v", "h264_qsv", "-preset", preset, "-global_quality", str(quality), "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "nv12"]


def _amf_args(purpose: str, pixels: int | None) -> list[str]:
    speed, qps = _pick(purpose, ("balanced", (22, 24, 26)), ("quality", (20, 22, 24)))
    return ["-c:v", "h264_amf", "-quality", speed, "-rc", "cqp", "-qp_i", str(qps[0]), "-qp_p", str(qps[1]), "-qp_b", str(qps[2]),
            "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "nv12"]


def _mf_quality_args(purpose: str, pixels: int | None) -> list[str]:
    # -hw_encoding 1: without it Media Foundation can hand the work to Microsoft's software encoder, which is slower and worse than x264.
    return ["-c:v", "h264_mf", "-hw_encoding", "1", "-rate_control", "quality", "-quality", str(_pick(purpose, 75, 80)),
            "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "nv12"]


def _mf_bitrate_args(purpose: str, pixels: int | None) -> list[str]:
    return ["-c:v", "h264_mf", "-hw_encoding", "1", "-rate_control", "u_vbr", "-b:v", _fallback_bitrate(pixels),
            "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "nv12"]


def _videotoolbox_quality_args(purpose: str, pixels: int | None) -> list[str]:
    # -allow_sw 0: never Apple's software encoder. Constant quality (-q:v) is offered on Apple silicon only.
    return ["-c:v", "h264_videotoolbox", "-allow_sw", "0", "-profile:v", "high", "-q:v", str(_pick(purpose, 65, 72)),
            "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "nv12"]


def _videotoolbox_bitrate_args(purpose: str, pixels: int | None) -> list[str]:
    return ["-c:v", "h264_videotoolbox", "-allow_sw", "0", "-profile:v", "high", "-b:v", _fallback_bitrate(pixels),
            "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "nv12"]


def _vaapi_device() -> str | None:
    """The first render node of a graphics chip on Linux (Intel and AMD, through Mesa or the Intel media driver)."""
    try:
        nodes = sorted(Path("/dev/dri").glob("renderD*"))
    except OSError:
        return None
    return str(nodes[0]) if nodes else None


def _vaapi_options(device: str | None) -> list[str]:
    # The frames must be uploaded to the chip, which is a video filter: see HardwareEncoder.needs_filter.
    return ["-vaapi_device", device or "", "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"]


def _vaapi_quality_args(purpose: str, pixels: int | None) -> list[str]:
    return [*_vaapi_options(_vaapi_device()), "-rc_mode", "CQP", "-qp", str(_pick(purpose, 23, 21)), "-g", str(KEYFRAME_INTERVAL_FRAMES)]


def _vaapi_bitrate_args(purpose: str, pixels: int | None) -> list[str]:
    return [*_vaapi_options(_vaapi_device()), "-b:v", _fallback_bitrate(pixels), "-g", str(KEYFRAME_INTERVAL_FRAMES)]


@dataclass(frozen=True)
class HardwareEncoder:
    key: str  # tells apart the ways one encoder can be driven (by quality, or if that is refused, by bitrate)
    encoder: str  # ffmpeg's name for it
    label: str  # what to call it in front of the user
    args: Callable[[str, int | None], list[str]]  # (purpose, frame pixels) -> ffmpeg output options
    needs_filter: bool = False  # its options include a -vf, so it can't be used by a caller that builds a -vf or -filter_complex of its own
    usable: Callable[[], bool] | None = None  # a cheap look at whether this machine could have the hardware at all


_NVENC = HardwareEncoder("nvenc", "h264_nvenc", "NVIDIA NVENC", _nvenc_args)
_QSV = HardwareEncoder("qsv", "h264_qsv", "Intel Quick Sync", _qsv_args)
_AMF = HardwareEncoder("amf", "h264_amf", "AMD AMF", _amf_args)
_MF_QUALITY = HardwareEncoder("mf-quality", "h264_mf", "Windows Media Foundation", _mf_quality_args)
_MF_BITRATE = HardwareEncoder("mf-bitrate", "h264_mf", "Windows Media Foundation", _mf_bitrate_args)
_VT_QUALITY = HardwareEncoder("vt-quality", "h264_videotoolbox", "Apple VideoToolbox", _videotoolbox_quality_args)
_VT_BITRATE = HardwareEncoder("vt-bitrate", "h264_videotoolbox", "Apple VideoToolbox", _videotoolbox_bitrate_args)
_VAAPI_QUALITY = HardwareEncoder("vaapi-quality", "h264_vaapi", "VA-API", _vaapi_quality_args, needs_filter=True, usable=lambda: _vaapi_device() is not None)
_VAAPI_BITRATE = HardwareEncoder("vaapi-bitrate", "h264_vaapi", "VA-API", _vaapi_bitrate_args, needs_filter=True, usable=lambda: _vaapi_device() is not None)

# In the order tried. A dedicated NVIDIA chip goes before the one built into the processor. Media Foundation is the way in to Intel, AMD and
# NVIDIA alike on Windows when the ffmpeg has no encoder of its own for the chip.
_HARDWARE_BY_PLATFORM: dict[str, tuple[HardwareEncoder, ...]] = {
    "win32": (_NVENC, _QSV, _AMF, _MF_QUALITY, _MF_BITRATE),
    "darwin": (_VT_QUALITY, _VT_BITRATE),
    "linux": (_NVENC, _QSV, _VAAPI_QUALITY, _VAAPI_BITRATE),
}

_PROBE_SIZE = (1920, 1080)
_probe_results: dict[tuple[str, str], bool] = {}  # (ffmpeg, encoder key) -> whether the test encode worked
_probe_lock = threading.Lock()


def _platform() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def _gpu_encoding_enabled() -> bool:
    """The user's switch (on unless turned off). Imported here so that export stays usable where Qt, and so the saved switch, is not."""
    try:
        from app.core import settings
        return settings.get_gpu_encoding()
    except Exception:  # noqa: BLE001
        return True


def _test_encode(ffmpeg: str, candidate: HardwareEncoder) -> bool | None:
    """Whether this encoder can encode a few blank frames: True if it did, False if it can't (no such hardware, no driver, options
    refused), None if we could not find out (ffmpeg would not run or hung), which is not remembered."""
    width, height = _PROBE_SIZE
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=30",
           "-frames:v", "8", *candidate.args("record", width * height), "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _hardware_encoder(encoders: frozenset[str], allow_filters: bool = True) -> HardwareEncoder | None:
    """The first graphics-chip encoder for this operating system that this ffmpeg has, this machine can run, and the user has not
    switched off. Each is test-encoded once per ffmpeg; the answer is remembered."""
    candidates = [candidate for candidate in _HARDWARE_BY_PLATFORM.get(_platform(), ())
                  if candidate.encoder in encoders and (allow_filters or not candidate.needs_filter)
                  and (candidate.usable is None or candidate.usable())]
    if not candidates or not _gpu_encoding_enabled():
        return None
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    with _probe_lock:
        for candidate in candidates:
            key = (ffmpeg, candidate.key)
            if key not in _probe_results:
                worked = _test_encode(ffmpeg, candidate)
                if worked is None:
                    return None  # could not ask: use the CPU this time and ask again next time, rather than remember a guess
                _probe_results[key] = worked
            if _probe_results[key]:
                return candidate
    return None


def hardware_encoder(encoders: frozenset[str], allow_filters: bool = True) -> HardwareEncoder | None:
    """The graphics-chip encoder that would be used (see _hardware_encoder), for callers outside this module."""
    return _hardware_encoder(encoders, allow_filters)


def is_hardware_encoder(name: str) -> bool:
    """Whether *name* (an ffmpeg encoder name) is one of the graphics chips' encoders."""
    return any(candidate.encoder == name for candidates in _HARDWARE_BY_PLATFORM.values() for candidate in candidates)


def without_pixel_format(args: list[str]) -> list[str]:
    """*args* without their -pix_fmt: for frames that are already on the chip, where asking for a format would pull them back to memory."""
    cleaned = list(args)
    if "-pix_fmt" in cleaned:
        position = cleaned.index("-pix_fmt")
        del cleaned[position:position + 2]
    return cleaned


def prefetch_encoders() -> None:
    """Ask in the background so the first recording doesn't wait: for ffmpeg's list of encoders, and for the test encodes that show
    whether the graphics chip works."""
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return
    threading.Thread(target=_prefetch, args=(ffmpeg,), daemon=True).start()


def _prefetch(ffmpeg: str) -> None:
    try:
        encoders = available_encoders(ffmpeg)
        if encoders:
            _hardware_encoder(encoders)  # every way of driving an encoder is remembered, so this also answers callers that pass allow_filters=False
    except Exception:  # noqa: BLE001 - a background nicety must never surface as an error
        pass


def describe_encoder(allow_filters: bool = True) -> str:
    """In words, what the next recording or export encodes with (for the 'saved' message); empty if ffmpeg can't be asked."""
    try:
        encoders = available_encoders(find_ffmpeg())
    except FileNotFoundError:
        return ""
    if not encoders:
        return ""
    hardware = _hardware_encoder(encoders, allow_filters)
    if hardware is not None:
        return f"the graphics chip ({hardware.label})"
    return "the CPU (x264)" if "libx264" in encoders else "the CPU (OpenH264)"


def h264_args(purpose: str, encoders: frozenset[str] | None = None, pixels: int | None = None, *, allow_filters: bool = True,
              gpu: bool = True) -> list[str]:
    """The ffmpeg output options that choose and configure the video encoder. *purpose* is "record" or "export"; *pixels* is the
    size of the recorded frame (width x height), which picks a lighter x264 preset for frames bigger than 1080p (and sizes the bitrate
    where an encoder has to be given one). Pass *allow_filters* False when the command has a -vf or -filter_complex of its own: an
    encoder that must add one (VA-API) is then left out. Pass *gpu* False to skip the graphics chip and use the CPU."""
    if encoders is None:
        encoders = available_encoders(find_ffmpeg())
    hardware = _hardware_encoder(encoders, allow_filters) if gpu else None
    if hardware is not None:
        return hardware.args(purpose, pixels)
    if "libx264" in encoders:
        if purpose == "record":
            preset, crf = RECORD_QUALITY_LARGE if pixels is not None and pixels > LARGE_FRAME_PIXELS else RECORD_QUALITY
        else:
            preset, crf = EXPORT_QUALITY
        # yuv420p stated outright: from a screen grab (RGB) ffmpeg would otherwise pick 4:4:4, which many players can't show.
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libopenh264", "-b:v", "20M"]
