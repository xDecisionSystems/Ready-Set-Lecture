"""Checks that the scanner tolerates the paper drifting away from where it
was calibrated (down to ~50% overlap with the original box, per the
documented DEFAULT_ROI_PADDING_FRACTION contract), while a same-colored
object sitting elsewhere in frame the whole time — outside the padded
search zone — does NOT get picked up as a false marker hold.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.color_scanner import calibrate_color_spec, scan_for_color_markers

WIDTH, HEIGHT, FPS = 960, 540, 30
DURATION_SECONDS = 20
MARKER_COLOR_BGR = (40, 220, 40)  # bright green paper
HOME_RECT = (700, 50, 180, 120)  # x, y, w, h — where the user calibrated
HOLD_SPAN = (5.0, 12.0)
# Shifted right by half its own width during the hold: exactly the boundary
# case the padding is sized for (~50% of HOME_RECT's area still overlapped).
DRIFTED_RECT = (700 + 90, 50, 180, 120)
# A same-colored, same-sized patch sitting in the opposite corner the whole
# video — far outside the padded search zone around HOME_RECT — must never
# be mistaken for the marker.
DECOY_RECT = (20, 350, 180, 120)


def _build_video(path: Path) -> None:
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, FPS, (WIDTH, HEIGHT))
    dx, dy, dw, dh = DECOY_RECT
    x, y, w, h = DRIFTED_RECT

    total_frames = DURATION_SECONDS * FPS
    for frame_idx in range(total_frames):
        t = frame_idx / FPS
        frame = np.full((HEIGHT, WIDTH, 3), 200, dtype=np.uint8)  # plain "whiteboard" background
        frame[dy : dy + dh, dx : dx + dw] = MARKER_COLOR_BGR  # decoy, present throughout
        if HOLD_SPAN[0] <= t <= HOLD_SPAN[1]:
            frame[y : y + h, x : x + w] = MARKER_COLOR_BGR
        writer.write(frame)
    writer.release()


def main() -> None:
    video_path = Path(__file__).resolve().parent / "_synthetic_drift_test.mp4"
    print(f"Building synthetic drift-test video at {video_path} ...")
    _build_video(video_path)

    # Calibrate at the HOME position, as the user would via the box-drawing UI.
    x, y, w, h = HOME_RECT
    calibration_frame = np.full((HEIGHT, WIDTH, 3), 200, dtype=np.uint8)
    calibration_frame[y : y + h, x : x + w] = MARKER_COLOR_BGR
    spec = calibrate_color_spec(calibration_frame, HOME_RECT)

    print("Scanning...")
    markers = scan_for_color_markers(video_path, spec)
    print(f"Detected markers: {[(round(m.start, 2), round(m.end, 2)) for m in markers]}")

    assert len(markers) == 1, f"Expected exactly 1 marker (the drifted hold), got {markers}"
    marker = markers[0]
    tolerance = 1.1
    assert abs(marker.start - HOLD_SPAN[0]) < tolerance, marker
    assert abs(marker.end - HOLD_SPAN[1]) < tolerance, marker

    print("PASS: drifted hold detected, decoy elsewhere in frame ignored.")
    video_path.unlink()


if __name__ == "__main__":
    main()
