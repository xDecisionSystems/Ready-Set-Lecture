"""Builds a synthetic lecture-like video with a colored marker held in a
known screen region for known spans, and checks that the scanner recovers
timestamps close to what was actually rendered.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.color_scanner import calibrate_color_spec, scan_for_color_markers

WIDTH, HEIGHT, FPS = 960, 540, 30
# (start_second, end_second) spans where the colored marker is held up on
# screen. Kept further apart than DEFAULT_CLUSTER_GAP_SECONDS (10s) so the
# two spans are guaranteed to cluster as separate markers, matching real
# distinct marker-hold episodes. Also kept several seconds long each,
# matching how long a real hold actually lasts (tens of seconds to minutes)
# rather than a momentary flash — a span shorter than the check interval
# could otherwise be missed by sparse sampling entirely by bad luck.
COLOR_SPANS = [(2.0, 7.0), (20.0, 26.0)]
DURATION_SECONDS = 30
MARKER_COLOR_BGR = (40, 220, 40)  # bright green paper
MARKER_RECT = (700, 50, 180, 120)  # x, y, w, h — a fixed on-screen spot, like a wall-mounted sheet


def _build_video(path: Path) -> None:
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, FPS, (WIDTH, HEIGHT))
    x, y, w, h = MARKER_RECT

    total_frames = DURATION_SECONDS * FPS
    for frame_idx in range(total_frames):
        t = frame_idx / FPS
        frame = np.full((HEIGHT, WIDTH, 3), 200, dtype=np.uint8)  # plain "whiteboard" background
        if any(start <= t <= end for start, end in COLOR_SPANS):
            frame[y : y + h, x : x + w] = MARKER_COLOR_BGR
        writer.write(frame)
    writer.release()


def main() -> None:
    video_path = Path(__file__).resolve().parent / "_synthetic_color_test.mp4"
    print(f"Building synthetic test video at {video_path} ...")
    _build_video(video_path)

    # Calibrate exactly as the UI would: sample the color from a frame where
    # the marker is actually showing, inside the box the user would have
    # dragged around it.
    x, y, w, h = MARKER_RECT
    calibration_frame = np.full((HEIGHT, WIDTH, 3), 200, dtype=np.uint8)
    calibration_frame[y : y + h, x : x + w] = MARKER_COLOR_BGR
    spec = calibrate_color_spec(calibration_frame, MARKER_RECT)

    print("Scanning...")
    markers = scan_for_color_markers(video_path, spec)

    print(f"Expected spans: {COLOR_SPANS}")
    print(f"Detected markers: {[(round(m.start, 2), round(m.end, 2)) for m in markers]}")

    assert len(markers) == len(COLOR_SPANS), (
        f"Expected {len(COLOR_SPANS)} clustered markers, got {len(markers)}"
    )
    # Candidate markers are only accurate to ~one sample interval by design
    # (they're refined by the user afterward), so allow that much slack.
    tolerance = 1.1
    for marker, (expected_start, expected_end) in zip(markers, COLOR_SPANS):
        assert abs(marker.start - expected_start) < tolerance, marker
        assert abs(marker.end - expected_end) < tolerance, marker

    print("PASS: detected markers align with the known color-hold spans.")
    video_path.unlink()


if __name__ == "__main__":
    main()
