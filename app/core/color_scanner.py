"""Scans a video for held-up colored-paper markers and reports them as
candidate cut-point markers.

The marker carries no meaning of its own (same as the QR-code approach this
replaces) — a detection is just a timestamp the user classifies later in the
timeline UI. Detection is calibrated once by the user dragging a box around
the colored paper on a single frame (see ColorRegionDialog): that fixes both
the region of the frame to search and the color to match, since the paper
sits in approximately the same spot on camera every time.

Scanning walks keyframes only, via `ffmpeg -skip_frame nokey`, instead of a
fixed time interval: decoding every frame (or seeking to arbitrary points
with OpenCV, which turned out to carry ~1s of fixed overhead per seek
regardless of position on real lecture footage) is far slower than letting
the decoder skip everything but keyframes outright. Camera encoders typically
place a keyframe roughly once a second anyway, which already lands in the
right ballpark for candidate markers, and candidates don't need to be evenly
spaced; they just need to be close to wherever the paper was actually held up.
Hardware decode (when available) cuts the remaining keyframe-decode cost by
another ~4-5x.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from app.core.ffmpeg_locator import find_ffmpeg

ProgressCallback = Callable[[int], None]

DEFAULT_CLUSTER_GAP_SECONDS = 10.0
# Camera encoders typically place a keyframe roughly once a second (confirmed
# empirically: median 0.99s gap on real lecture footage), and the detector
# call itself is a real fraction of total scan cost alongside decode. Only
# actually running the detector once every few keyframes, rather than on
# every single one, checks less often but scans faster — reasonable since a
# real marker hold lasts many seconds, not a single frame.
#
# A coarse-only scan trades away boundary precision though (a hold's true
# start/end could be missed by up to one whole interval), so a coarse hit
# triggers a second, full-resolution pass over just that neighborhood — fast
# because it only touches the handful of small windows that actually matter,
# not the whole video.
DEFAULT_CHECK_INTERVAL_SECONDS = 10.0
_ASSUMED_KEYFRAME_INTERVAL_SECONDS = 1.0
# Much lower than the 1920px the old QR scanner needed: a QR code depends on
# resolving fine finder-pattern geometry that heavy downscaling destroys,
# but a solid color block doesn't — color is a low-frequency property that
# survives it easily. Empirically confirmed on real 4K lecture footage:
# 480px reproduced the exact same detected spans as 1920px (~35% faster to
# scan), while 640px and 320px each dropped one genuine borderline-length
# hold that 480px still caught — scaling-interpolation noise near the match
# threshold rather than a clean trend, but reason enough not to push lower.
DEFAULT_DOWNSCALE_WIDTH = 480
DEFAULT_MATCH_FRACTION = 0.35
# HSV margin applied around the sampled color on each side (S/V get double
# the margin since lighting swings brightness/saturation far more than hue).
DEFAULT_TOLERANCE = 25
# How far the paper can drift from where it was calibrated: the search zone
# is grown by this fraction of the calibrated box's own width/height on each
# side, so e.g. 0.5 turns a WxH box into a search zone up to 2W x 2H. Sized
# so a paper that's drifted until only half of it still overlaps the
# originally-drawn box is still fully inside the (larger) search zone.
DEFAULT_ROI_PADDING_FRACTION = 0.5

_HWACCEL_BY_PLATFORM = {
    "win32": "d3d11va",
    "darwin": "videotoolbox",
    "linux": "vaapi",
}
_PTS_TIME_RE = re.compile(rb"pts_time:([\d.]+)")


@dataclass
class ColorMarker:
    start: float
    end: float

    @property
    def timestamp(self) -> float:
        return (self.start + self.end) / 2


HsvBound = tuple[int, int, int]
HsvRange = tuple[HsvBound, HsvBound]


@dataclass
class ColorSpec:
    """A calibrated search region + target color, normalized to the frame
    size so it applies unchanged at whatever resolution a frame is decoded
    or downscaled to.

    roi_x/y/w/h are fractions (0..1) of the full frame — the search zone
    scanned for the color, padded out from the box the user actually drew
    (see calibrate_color_spec) so the paper can drift a bit without leaving
    it. hsv_ranges is a list of one or two (lower, upper) HSV bound pairs
    (H: 0-179, S/V: 0-255) — a pixel matches if it falls in ANY of them. Two
    ranges show up for a hue near the 0/179 wrap-around (true red): OpenCV's
    hue axis is circular but inRange() isn't, so a band that straddles the
    seam has to be split into a piece on each end instead of clipped to one
    side.

    min_match_area_fraction is how much matching-color area must appear
    somewhere in the (padded) search zone, as a fraction of the FULL frame's
    area — deliberately not a fraction of the search zone itself, since that
    zone is bigger than the paper on purpose. Anchoring the threshold to the
    frame instead means padding the search zone for drift tolerance doesn't
    also quietly make detection more trigger-happy (a bigger search zone
    would otherwise need less absolute color area to hit the same fraction).
    """

    roi_x: float
    roi_y: float
    roi_w: float
    roi_h: float
    hsv_ranges: list[HsvRange]
    min_match_area_fraction: float

    def to_dict(self) -> dict:
        return {
            "roi_x": self.roi_x,
            "roi_y": self.roi_y,
            "roi_w": self.roi_w,
            "roi_h": self.roi_h,
            "hsv_ranges": [[list(lower), list(upper)] for lower, upper in self.hsv_ranges],
            "min_match_area_fraction": self.min_match_area_fraction,
        }

    @staticmethod
    def from_dict(d: dict) -> "ColorSpec":
        return ColorSpec(
            roi_x=d["roi_x"],
            roi_y=d["roi_y"],
            roi_w=d["roi_w"],
            roi_h=d["roi_h"],
            hsv_ranges=[(tuple(lower), tuple(upper)) for lower, upper in d["hsv_ranges"]],
            min_match_area_fraction=d["min_match_area_fraction"],
        )


def grab_frame(video_path: str | Path, seconds: float) -> np.ndarray:
    """Reads a single BGR frame at the given timestamp, for color calibration.

    A plain seek-and-read (not the keyframe-walking approach used for
    scanning) — it only runs once per calibration, so the ~1s fixed seek
    overhead that makes OpenCV seeking too slow for a full scan doesn't
    matter here.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds) * 1000.0)
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError(f"Could not read a frame at {seconds:.2f}s")
        return frame
    finally:
        cap.release()


def calibrate_color_spec(
    frame_bgr: np.ndarray,
    roi_px: tuple[int, int, int, int],
    tolerance: int = DEFAULT_TOLERANCE,
    match_fraction: float = DEFAULT_MATCH_FRACTION,
    roi_padding_fraction: float = DEFAULT_ROI_PADDING_FRACTION,
) -> ColorSpec:
    """Derives a ColorSpec from the pixels inside roi_px = (x, y, w, h) of
    frame_bgr — the box the user actually drew around the paper. Uses the
    median HSV of that region (robust to a stray pixel or two of background
    creeping into the box) plus a fixed tolerance band.

    A hue near the 0/179 wrap-around (i.e. true red — a very likely marker
    color, since it reads as strongly "not background" against a whiteboard)
    is split into two ranges, one at each end of the hue axis, rather than
    clipped to one side.

    The color is sampled from roi_px exactly as drawn, but the ColorSpec's
    actual search zone is grown by roi_padding_fraction beyond it (see
    ColorSpec's docstring for why the match threshold is anchored to the
    original box's area rather than this larger zone).
    """
    frame_h, frame_w = frame_bgr.shape[:2]
    x, y, w, h = roi_px
    crop = frame_bgr[y : y + h, x : x + w]
    if crop.size == 0:
        raise ValueError("Selected region is empty")

    hsv_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    h_med, s_med, v_med = (float(c) for c in np.median(hsv_crop, axis=0))

    s_bounds = (max(0, int(s_med - tolerance * 2)), min(255, int(s_med + tolerance * 2)))
    v_bounds = (max(0, int(v_med - tolerance * 2)), min(255, int(v_med + tolerance * 2)))
    h_low = h_med - tolerance
    h_high = h_med + tolerance

    if h_low < 0:
        hsv_ranges = [
            ((0, s_bounds[0], v_bounds[0]), (int(h_high), s_bounds[1], v_bounds[1])),
            ((int(h_low) + 180, s_bounds[0], v_bounds[0]), (179, s_bounds[1], v_bounds[1])),
        ]
    elif h_high > 179:
        hsv_ranges = [
            ((int(h_low), s_bounds[0], v_bounds[0]), (179, s_bounds[1], v_bounds[1])),
            ((0, s_bounds[0], v_bounds[0]), (int(h_high) - 180, s_bounds[1], v_bounds[1])),
        ]
    else:
        hsv_ranges = [((int(h_low), s_bounds[0], v_bounds[0]), (int(h_high), s_bounds[1], v_bounds[1]))]

    pad_x, pad_y = w * roi_padding_fraction, h * roi_padding_fraction
    search_x = max(0.0, x - pad_x)
    search_y = max(0.0, y - pad_y)
    search_w = min(frame_w - search_x, w + 2 * pad_x)
    search_h = min(frame_h - search_y, h + 2 * pad_y)

    return ColorSpec(
        roi_x=search_x / frame_w,
        roi_y=search_y / frame_h,
        roi_w=search_w / frame_w,
        roi_h=search_h / frame_h,
        hsv_ranges=hsv_ranges,
        min_match_area_fraction=match_fraction * (w * h) / (frame_w * frame_h),
    )


def _make_color_detector(
    spec: ColorSpec, frame_width: int, frame_height: int
) -> Callable[[np.ndarray], bool]:
    """Returns a fn(frame) -> bool: True if enough of spec's color appears
    anywhere in its (padded) search zone in frame. frame_width/frame_height
    must match the actual frame this will be called on, so the normalized
    ROI and area threshold map to the right pixels."""
    x = max(0, int(spec.roi_x * frame_width))
    y = max(0, int(spec.roi_y * frame_height))
    w = max(1, min(int(spec.roi_w * frame_width), frame_width - x))
    h = max(1, min(int(spec.roi_h * frame_height), frame_height - y))
    bounds = [
        (np.array(lower, dtype=np.uint8), np.array(upper, dtype=np.uint8))
        for lower, upper in spec.hsv_ranges
    ]
    required_pixels = spec.min_match_area_fraction * frame_width * frame_height

    def detect(frame: np.ndarray) -> bool:
        crop = frame[y : y + h, x : x + w]
        if crop.size == 0:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, bounds[0][0], bounds[0][1])
        for lower, upper in bounds[1:]:
            mask |= cv2.inRange(hsv, lower, upper)
        return cv2.countNonZero(mask) >= required_pixels

    return detect


def scan_for_color_markers(
    video_path: str | Path,
    color_spec: ColorSpec,
    progress_callback: ProgressCallback | None = None,
    cluster_gap_seconds: float = DEFAULT_CLUSTER_GAP_SECONDS,
    downscale_width: int = DEFAULT_DOWNSCALE_WIDTH,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
) -> list[ColorMarker]:
    video_path = Path(video_path)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    cap.release()
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Could not read video dimensions: {video_path}")
    duration_estimate = total_frames / fps if fps else 0.0

    def coarse_progress(pct: int) -> None:
        if progress_callback:
            progress_callback(min(90, pct))  # leave room at the top for the refine pass

    coarse_timestamps = _scan_range(
        video_path, width, height, downscale_width, duration_estimate,
        coarse_progress, color_spec, check_interval_seconds, time_range=None,
    )

    if not coarse_timestamps:
        if progress_callback:
            progress_callback(100)
        return []

    # Coarse hits only say "somewhere around here" — a hold's true start/end
    # could be missed by up to one whole check_interval_seconds. Refine each
    # rough area with a full-resolution (every-keyframe) local pass, which is
    # fast because it's confined to just the handful of small windows that
    # actually had a hit, not the entire video.
    rough_windows = _cluster(coarse_timestamps, max(cluster_gap_seconds, check_interval_seconds * 2))
    padding = check_interval_seconds
    fine_timestamps: list[float] = []
    for i, rough in enumerate(rough_windows):
        window = (max(0.0, rough.start - padding), rough.end + padding)
        fine_timestamps.extend(
            _scan_range(
                video_path, width, height, downscale_width, 0.0,
                None, color_spec, _ASSUMED_KEYFRAME_INTERVAL_SECONDS, time_range=window,
            )
        )
        if progress_callback:
            progress_callback(90 + int((i + 1) / len(rough_windows) * 10))

    if progress_callback:
        progress_callback(100)

    return _cluster(fine_timestamps, cluster_gap_seconds)


def _scan_range(
    video_path: Path,
    width: int,
    height: int,
    downscale_width: int,
    duration_estimate: float,
    progress_callback: ProgressCallback | None,
    color_spec: ColorSpec,
    check_interval_seconds: float,
    time_range: tuple[float, float] | None,
) -> list[float]:
    try:
        return _scan_via_ffmpeg(
            video_path, width, height, downscale_width, duration_estimate,
            progress_callback, hwaccel=_HWACCEL_BY_PLATFORM.get(sys.platform), color_spec=color_spec,
            check_interval_seconds=check_interval_seconds, time_range=time_range,
        )
    except (FileNotFoundError, RuntimeError):
        # Hardware decode isn't available/supported on this machine (missing
        # drivers, unsupported codec/hwaccel combo, etc). Fall back to a pure
        # software decode of keyframes only — slower, but still far cheaper
        # than decoding every frame.
        return _scan_via_ffmpeg(
            video_path, width, height, downscale_width, duration_estimate,
            progress_callback, hwaccel=None, color_spec=color_spec,
            check_interval_seconds=check_interval_seconds, time_range=time_range,
        )


def _scan_via_ffmpeg(
    video_path: Path,
    width: int,
    height: int,
    downscale_width: int,
    duration_estimate: float,
    progress_callback: ProgressCallback | None,
    hwaccel: str | None,
    color_spec: ColorSpec,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    time_range: tuple[float, float] | None = None,
) -> list[float]:
    stride = max(1, round(check_interval_seconds / _ASSUMED_KEYFRAME_INTERVAL_SECONDS))
    scale_width = min(downscale_width, width)
    scale_height = max(2, round(height * scale_width / width))
    frame_bytes = scale_width * scale_height * 3

    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-v", "info"]
    if time_range:
        # Keep absolute timestamps even though we're seeking into the middle
        # of the file, so showinfo's pts_time still lines up with the real
        # timeline instead of resetting to 0 at the seek point.
        cmd += ["-copyts"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    if time_range:
        cmd += ["-ss", f"{time_range[0]:.3f}"]
    cmd += ["-skip_frame", "nokey", "-i", str(video_path)]
    if time_range:
        # -to (an absolute end time) rather than -t (a duration): -copyts
        # keeps output timestamps absolute (e.g. ~370s, not reset near 0),
        # and -t's duration cutoff is computed against that same absolute
        # clock — so a short -t immediately looked "already expired" against
        # a seek far into the file and silently produced zero output bytes.
        cmd += ["-to", f"{time_range[1]:.3f}"]
    cmd += [
        "-an", "-map", "0:v:0",
        "-vf", f"scale={scale_width}:{scale_height},showinfo",
        # rawvideo carries no per-frame timestamps, so ffmpeg's default vsync
        # behavior pads the gaps left by -skip_frame with *duplicated*
        # frames to hold a constant rate — silently breaking the 1:1
        # correspondence between frames on stdout and showinfo's timestamps.
        # passthrough disables that padding.
        "-fps_mode", "passthrough",
        "-pix_fmt", "bgr24",
        "-f", "rawvideo",
        "-",
    ]

    detect = _make_color_detector(color_spec, scale_width, scale_height)
    found_flags: list[bool] = []

    with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as stderr_file:
        stderr_path = Path(stderr_file.name)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file)
        try:
            assert proc.stdout is not None
            frame_index = 0
            while True:
                chunk = proc.stdout.read(frame_bytes)
                if len(chunk) < frame_bytes:
                    break
                if frame_index % stride == 0:
                    frame = np.frombuffer(chunk, dtype=np.uint8).reshape((scale_height, scale_width, 3))
                    found_flags.append(detect(frame))
                else:
                    found_flags.append(False)
                frame_index += 1
                if progress_callback and duration_estimate:
                    pct = min(99, int(len(found_flags) / duration_estimate * 100))
                    progress_callback(pct)
        finally:
            proc.stdout.close()
            proc.wait()

    if proc.returncode != 0 and not found_flags:
        stderr_tail = stderr_path.read_text(errors="replace")[-2000:]
        stderr_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}: {stderr_tail}")

    timestamps = [float(m.group(1)) for m in _PTS_TIME_RE.finditer(stderr_path.read_bytes())]
    stderr_path.unlink(missing_ok=True)

    n = min(len(timestamps), len(found_flags))
    return [timestamps[i] for i in range(n) if found_flags[i]]


def _cluster(timestamps: list[float], gap_seconds: float) -> list[ColorMarker]:
    if not timestamps:
        return []
    timestamps = sorted(timestamps)
    markers: list[ColorMarker] = []
    start = prev = timestamps[0]
    for t in timestamps[1:]:
        if t - prev > gap_seconds:
            markers.append(ColorMarker(start, prev))
            start = t
        prev = t
    markers.append(ColorMarker(start, prev))
    return markers


try:
    from PySide6.QtCore import QThread, Signal

    class ColorScanWorker(QThread):
        """Runs scan_for_color_markers off the UI thread, relaying progress via signals."""

        progress = Signal(int)
        finished_scan = Signal(list)
        failed = Signal(str)

        def __init__(self, video_path: str | Path, color_spec: ColorSpec, parent=None) -> None:
            super().__init__(parent)
            self.video_path = video_path
            self.color_spec = color_spec

        def run(self) -> None:
            try:
                markers = scan_for_color_markers(
                    self.video_path, self.color_spec, progress_callback=self.progress.emit
                )
            except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                self.failed.emit(str(exc))
                return
            self.finished_scan.emit(markers)

except ImportError:
    pass  # PySide6 not required for CLI/headless use of scan_for_color_markers


def _main() -> None:
    if len(sys.argv) != 7:
        print(
            "Usage: python -m app.core.color_scanner <video_path> "
            "<roi_x> <roi_y> <roi_w> <roi_h> <hex_color>"
        )
        print("  roi_x/y/w/h are fractions of the frame (0-1); hex_color like ff8800")
        raise SystemExit(1)
    video_path, rx, ry, rw, rh, hex_color = sys.argv[1:]
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    # A solid-color fake frame lets calibrate_color_spec do its normal job
    # (wraparound handling, padding, frame-relative area threshold) from
    # roi fractions instead of real pixels — the absolute fake size cancels
    # out since everything it derives is expressed as a fraction of it.
    fake_size = 1000
    x, y, w, h = (round(float(v) * fake_size) for v in (rx, ry, rw, rh))
    fake_frame = np.full((fake_size, fake_size, 3), (b, g, r), dtype=np.uint8)
    spec = calibrate_color_spec(fake_frame, (x, y, max(1, w), max(1, h)))

    def _print_progress(pct: int) -> None:
        print(f"\rScanning... {pct}%", end="", flush=True)

    markers = scan_for_color_markers(video_path, spec, progress_callback=_print_progress)
    print()
    if not markers:
        print("No color markers detected.")
    for i, marker in enumerate(markers, start=1):
        print(f"{i}: {marker.start:.2f}s - {marker.end:.2f}s (mid {marker.timestamp:.2f}s)")


if __name__ == "__main__":
    _main()
