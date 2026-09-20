"""Which H.264 encoder ffmpeg is told to use, for recording and for export.

x264 is preferred. On real footage it made files 35% (screen slides) to 5x (camera) smaller than OpenH264 at 20 Mbps with the same
measured quality, and, unlike OpenH264, it keeps up with live 1080p camera capture (OpenH264 dropped more than half the frames).
It is GPL, which Ready, Set, Lecture! now is too (see LICENSE). When the ffmpeg on a machine has no libx264, for example a build made LGPL-only,
the previous OpenH264 settings are used instead, so nothing stops working.
"""
from __future__ import annotations

import re
import subprocess
import threading

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


def prefetch_encoders() -> None:
    """Ask in the background so the first recording doesn't wait for it."""
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return
    if ffmpeg not in _encoders_by_ffmpeg:
        threading.Thread(target=available_encoders, args=(ffmpeg,), daemon=True).start()


def h264_args(purpose: str, encoders: frozenset[str] | None = None, pixels: int | None = None) -> list[str]:
    """The ffmpeg output options that choose and configure the video encoder. *purpose* is "record" or "export"; *pixels* is the
    size of the recorded frame (width x height), which picks a lighter preset for frames bigger than 1080p."""
    if encoders is None:
        encoders = available_encoders(find_ffmpeg())
    if "libx264" in encoders:
        if purpose == "record":
            preset, crf = RECORD_QUALITY_LARGE if pixels is not None and pixels > LARGE_FRAME_PIXELS else RECORD_QUALITY
        else:
            preset, crf = EXPORT_QUALITY
        # yuv420p stated outright: from a screen grab (RGB) ffmpeg would otherwise pick 4:4:4, which many players can't show.
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-g", str(KEYFRAME_INTERVAL_FRAMES), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libopenh264", "-b:v", "20M"]
