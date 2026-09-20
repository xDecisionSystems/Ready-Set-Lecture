"""Capturing the screen on the graphics chip (Windows only: Desktop Duplication, through ffmpeg's ddagrab).

The usual capture (gdigrab) copies every frame through the CPU. At a full 2880x1920 screen that was over a core and a half on the
development laptop, and it could not keep up: it recorded about 19 frames a second of the 30 asked for. ddagrab hands the picture to
the encoder as a texture that never leaves the chip, which measured about a sixth of the CPU and the full frame rate.

That only pays when the encoder is on the chip too: copying the frames back to memory for an x264 encode cost more than gdigrab. So this
is used only with a graphics-chip encoder, and the plan is dropped (the caller uses gdigrab) unless everything below checks out:

* the screen being recorded is found among the graphics chip's outputs (by its Windows display name),
* a test capture of that output through the chosen encoder works, and the frames ffmpeg gets are the size the chip reports for that
  display (so ffmpeg's output number is our display, and its offsets are in the pixels our rectangles are in),
* the colours are known. The chip's video processor turns the screen's RGB into YUV inside the encoder, with a matrix and range that
  differ by vendor, while ffmpeg labels the result "RGB, full range". Believing that label washes the picture out. So a synthetic
  colour test is encoded the same way, the matrix and range that came out are measured, and the recording is labelled with those.
"""
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core import video_encoder
from app.core.ffmpeg_locator import find_ffmpeg

if TYPE_CHECKING:
    from app.recorder.core.ffmpeg_record import ScreenCaptureConfig

# The largest picture the graphics chips' H.264 encoders take, on the ones that do least: level 5.1 is 4096x2304.
MAX_WIDTH, MAX_HEIGHT = 4096, 2304
_PROBE_SIZE = (320, 240)
_VIDEO_SIZE = re.compile(r"Video: [^\n]*?, (\d+)x(\d+)")  # ffmpeg's line for an input stream: "Video: wrapped_avframe, d3d11, 2880x1920 [SAR ..."


@dataclass(frozen=True)
class DxgiOutput:
    index: int  # what ddagrab's output_idx counts
    device_name: str  # Windows' name for the display, which is also Qt's QScreen.name(), e.g. \\.\DISPLAY1
    rect: tuple[int, int, int, int]  # x, y, width, height on the desktop, in physical pixels


def _guid(text: str) -> "ctypes.Array":
    parts = text.split("-")
    data = bytes.fromhex(parts[0])[::-1] + bytes.fromhex(parts[1])[::-1] + bytes.fromhex(parts[2])[::-1] + bytes.fromhex(parts[3] + parts[4])
    return (ctypes.c_ubyte * 16).from_buffer_copy(data)


def dxgi_outputs() -> list[DxgiOutput]:
    """The displays attached to the default graphics adapter (the one ddagrab opens), in the order ddagrab numbers them. Empty when this
    is not Windows or DXGI can't be asked."""
    if sys.platform != "win32":
        return []
    try:
        return _read_dxgi_outputs()
    except Exception:  # noqa: BLE001 - never let an odd driver take the recorder down: no outputs just means gdigrab
        return []


def _read_dxgi_outputs() -> list[DxgiOutput]:
    from ctypes import POINTER, WINFUNCTYPE, byref, c_int, c_uint, c_void_p, c_wchar, windll, wintypes

    def call(pointer: c_void_p, slot: int, restype, *argtypes):
        """Call slot *slot* of a COM object's method table."""
        table = ctypes.cast(ctypes.cast(pointer, POINTER(c_void_p))[0], POINTER(c_void_p))
        return WINFUNCTYPE(restype, c_void_p, *argtypes)(table[slot])

    class OutputDesc(ctypes.Structure):
        _fields_ = [("DeviceName", c_wchar * 32), ("Left", c_int), ("Top", c_int), ("Right", c_int), ("Bottom", c_int),
                    ("AttachedToDesktop", c_int), ("Rotation", c_int), ("Monitor", c_void_p)]

    factory = c_void_p()
    if windll.dxgi.CreateDXGIFactory1(byref(_guid("770aae78-f26f-4dba-a829-253c83d1b387")), byref(factory)) != 0:  # IDXGIFactory1
        return []
    release_factory = call(factory, 2, c_uint)
    try:
        adapter = c_void_p()
        if call(factory, 12, c_int, c_uint, POINTER(c_void_p))(factory, 0, byref(adapter)) != 0:  # IDXGIFactory1::EnumAdapters1
            return []
        release_adapter = call(adapter, 2, c_uint)
        try:
            outputs: list[DxgiOutput] = []
            index = 0
            while True:
                output = c_void_p()
                if call(adapter, 7, c_int, c_uint, POINTER(c_void_p))(adapter, index, byref(output)) != 0:  # IDXGIAdapter::EnumOutputs
                    break
                try:
                    desc = OutputDesc()
                    if call(output, 7, c_int, POINTER(OutputDesc))(output, byref(desc)) == 0:  # IDXGIOutput::GetDesc
                        outputs.append(DxgiOutput(index, desc.DeviceName, (desc.Left, desc.Top, desc.Right - desc.Left, desc.Bottom - desc.Top)))
                finally:
                    call(output, 2, c_uint)(output)
                index += 1
            return outputs
        finally:
            release_adapter(adapter)
    finally:
        release_factory(factory)


@dataclass(frozen=True)
class Colors:
    """What the chip's conversion turned the screen's RGB into. *matrix* is the ffmpeg name of the YUV matrix, *range* "tv" or "pc"."""
    matrix: str
    range: str

    @property
    def filter(self) -> str:
        """A filter that puts these on the frames, so the encoder writes them into the file."""
        return f"setparams=range={self.range}:colorspace={self.matrix}"


@dataclass(frozen=True)
class GpuCapture:
    input_args: list[str]  # ffmpeg options that read the screen
    filter: str  # a -vf that labels the frames with the colours the chip really made
    encoder_args: list[str]  # the chip's encoder, taking the frames as they are


_probe_lock = threading.Lock()
_outputs_cache: list[DxgiOutput] | None = None
_probe_results: dict[tuple, object] = {}  # (ffmpeg, encoder key, ...) -> answer; a failed probe is remembered as None


def _outputs() -> list[DxgiOutput]:
    global _outputs_cache
    if _outputs_cache is None:
        _outputs_cache = dxgi_outputs()
    return _outputs_cache


def _ddagrab(output: int, rect: tuple[int, int, int, int] | None, fps: int, draw_mouse: bool) -> str:
    """The ddagrab source; rect is x, y, width, height inside the output, or None for all of it."""
    options = [f"output_idx={output}", f"framerate={fps}", f"draw_mouse={int(draw_mouse)}"]
    if rect is not None:
        options += [f"video_size={rect[2]}x{rect[3]}", f"offset_x={rect[0]}", f"offset_y={rect[1]}"]
    return "ddagrab=" + ":".join(options)


def _run(ffmpeg: str, args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run([ffmpeg, "-hide_banner", "-y", *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _capture_works(ffmpeg: str, hardware: video_encoder.HardwareEncoder, output: DxgiOutput) -> bool | None:
    """Whether ddagrab's output N is the display DXGI calls N, and whether *hardware* can encode its frames as they come.

    First the whole output is grabbed, and the size of the frames (which ffmpeg prints for its input) must be the display's physical size:
    that shows both that the number means the same display to ffmpeg, and that its offsets are in the pixels our rectangles are in. (The
    "dimensions" in ffmpeg's log are not used: Windows shows a program that is not scale-aware a scaled-down display size.) Then a small
    area is grabbed and encoded, so the encoder is tried on frames that are on the chip.
    """
    sized = _run(ffmpeg, ["-f", "lavfi", "-i", _ddagrab(output.index, None, 30, False), "-frames:v", "2", "-f", "null", "-"])
    if sized is None:
        return None
    frame = _VIDEO_SIZE.search(sized.stderr.decode("utf-8", "replace"))
    if sized.returncode != 0 or not frame or (int(frame.group(1)), int(frame.group(2))) != tuple(output.rect[2:]):
        return False
    encoder = video_encoder.without_pixel_format(hardware.args("record", _PROBE_SIZE[0] * _PROBE_SIZE[1]))
    rect = (0, 0, min(_PROBE_SIZE[0], output.rect[2]), min(_PROBE_SIZE[1], output.rect[3]))
    encoded = _run(ffmpeg, ["-loglevel", "error", "-f", "lavfi", "-i", _ddagrab(output.index, rect, 30, False), *encoder, "-frames:v", "8", "-f", "null", "-"])
    return None if encoded is None else encoded.returncode == 0


# Solid colours that tell the matrices and ranges apart, as (R, G, B). The white and black ones show the range; the saturated ones the matrix.
_TEST_COLORS = [(255, 0, 255), (0, 255, 255), (255, 255, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0)]
_BLOCK = 128
_MATRICES = {"bt709": (0.2126, 0.0722), "smpte170m": (0.299, 0.114)}  # (Kr, Kb) of BT.709, and of BT.601 (which ffmpeg calls smpte170m)


def _expected(color: tuple[int, int, int], matrix: str, limited: bool) -> tuple[float, float, float]:
    kr, kb = _MATRICES[matrix]
    r, g, b = color
    y = kr * r + (1 - kr - kb) * g + kb * b
    u, v = (b - y) / (2 * (1 - kb)), (r - y) / (2 * (1 - kr))
    return (16 + y * 219 / 255, 128 + u * 224 / 255, 128 + v * 224 / 255) if limited else (y, 128 + u, 128 + v)


def _best_fit(planes: tuple[bytes, bytes, bytes], width: int) -> Colors | None:
    """Which matrix and range best explain the YUV the chip produced for the test colours; None if none explains it well enough."""
    y_plane, u_plane, v_plane = planes
    totals: dict[tuple[str, bool], float] = {}
    for index, color in enumerate(_TEST_COLORS):
        x, y = index * _BLOCK + _BLOCK // 2, _BLOCK // 2
        got = (y_plane[y * width + x], u_plane[(y // 2) * (width // 2) + x // 2], v_plane[(y // 2) * (width // 2) + x // 2])
        for matrix in _MATRICES:
            for limited in (True, False):
                totals[(matrix, limited)] = totals.get((matrix, limited), 0.0) + sum(abs(a - b) for a, b in zip(got, _expected(color, matrix, limited)))
    ranked = sorted(totals.items(), key=lambda item: item[1])
    (matrix, limited), best = ranked[0]
    # A lossy encode moves each value by a few levels, so "well enough" is a small error per value, and a clear winner over the runner-up.
    if best > len(_TEST_COLORS) * 3 * 4 or ranked[1][1] < best * 2:
        return None
    return Colors(matrix, "tv" if limited else "pc")


def _measure_colors(ffmpeg: str, hardware: video_encoder.HardwareEncoder) -> Colors | None:
    """Encode the test colours as GPU frames through this encoder, and read back what its conversion did to them."""
    width = _BLOCK * len(_TEST_COLORS)
    blocks = ";".join(f"color=c=0x{r:02X}{g:02X}{b:02X}:s={_BLOCK}x{_BLOCK}:r=30:d=1[c{i}]" for i, (r, g, b) in enumerate(_TEST_COLORS))
    graph = blocks + ";" + "".join(f"[c{i}]" for i in range(len(_TEST_COLORS))) + f"hstack=inputs={len(_TEST_COLORS)},format=bgra,hwupload[v]"
    encoder = video_encoder.without_pixel_format(hardware.args("record", width * _BLOCK))
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix="readysetlecture_colors_") as folder:
        clip, raw = os.path.join(folder, "colors.mp4"), os.path.join(folder, "colors.yuv")
        made = _run(ffmpeg, ["-loglevel", "error", "-init_hw_device", "d3d11va=hw", "-filter_hw_device", "hw", "-filter_complex", graph, "-map", "[v]", *encoder, "-frames:v", "8", clip])
        if made is None or made.returncode != 0 or not os.path.exists(clip):
            return None
        # As yuv420p, the file's own layout, so nothing is converted between the encoder's numbers and ours.
        read = _run(ffmpeg, ["-loglevel", "error", "-i", clip, "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", raw])
        if read is None or read.returncode != 0 or not os.path.exists(raw):
            return None
        with open(raw, "rb") as handle:
            data = handle.read()
    size = width * _BLOCK
    if len(data) < size * 3 // 2:
        return None
    return _best_fit((data[:size], data[size:size * 5 // 4], data[size * 5 // 4:size * 3 // 2]), width)


def _known_output_and_colors(ffmpeg: str, hardware: video_encoder.HardwareEncoder, output: DxgiOutput) -> Colors | None:
    """Whether this encoder can take this output's frames straight from the chip, and if so, its colours. Asked once each."""
    key = (ffmpeg, hardware.key, output.index, output.rect[2:])
    if key not in _probe_results:
        works = _capture_works(ffmpeg, hardware, output)
        if works is None:
            return None  # could not ask: not remembered
        colors = None
        if works:
            colors_key = (ffmpeg, hardware.key, "colors")
            if colors_key not in _probe_results:
                _probe_results[colors_key] = _measure_colors(ffmpeg, hardware)
            colors = _probe_results[colors_key]
        _probe_results[key] = colors
    return _probe_results[key]


def plan_gpu_capture(source: "ScreenCaptureConfig") -> GpuCapture | None:
    """How to record *source* without the frames leaving the graphics chip, or None to use gdigrab. Never raises."""
    try:
        return _plan(source)
    except Exception:  # noqa: BLE001 - whatever goes wrong here, the ordinary capture still works
        return None


def _plan(source: "ScreenCaptureConfig") -> GpuCapture | None:
    if sys.platform != "win32" or not source.screen_name or not source.screen_rect:
        return None
    x, y, width, height = source.capture_rect
    if width > MAX_WIDTH or height > MAX_HEIGHT:
        return None
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    hardware = video_encoder.hardware_encoder(video_encoder.available_encoders(ffmpeg), allow_filters=False)
    if hardware is None:
        return None
    output = next((candidate for candidate in _outputs() if candidate.device_name == source.screen_name), None)
    if output is None:
        return None
    # The area, as an offset inside the output. Qt and DXGI can disagree about where a display starts on a mixed-scale desktop, so only the
    # position within the screen is used, and it must fit inside the output.
    offset_x, offset_y = x - source.screen_rect[0], y - source.screen_rect[1]
    if offset_x < 0 or offset_y < 0 or offset_x + width > output.rect[2] or offset_y + height > output.rect[3]:
        return None
    with _probe_lock:
        colors = _known_output_and_colors(ffmpeg, hardware, output)
    if colors is None:
        return None
    rect = None if (width, height) == output.rect[2:] and (offset_x, offset_y) == (0, 0) else (offset_x, offset_y, width, height)
    return GpuCapture(["-f", "lavfi", "-i", _ddagrab(output.index, rect, source.fps, source.draw_cursor)], colors.filter,
                      video_encoder.without_pixel_format(hardware.args("record", width * height)))


def prefetch_gpu_capture(screens: list) -> None:
    """In the background, work out (and remember) whether each of *screens* (ScreenSource) can be captured on the chip, so that the first
    recording doesn't wait for it."""
    def work() -> None:
        try:
            from app.recorder.core.ffmpeg_record import ScreenCaptureConfig
            for screen in screens:
                plan_gpu_capture(ScreenCaptureConfig(screen.physical_rect, screen_name=screen.qt_name, screen_rect=screen.physical_rect))
        except Exception:  # noqa: BLE001 - a background nicety must never surface as an error
            pass
    threading.Thread(target=work, daemon=True).start()
