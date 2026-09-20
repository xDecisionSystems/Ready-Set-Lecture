"""Export live Marker Break presses as native Ready, Set, Lecture! sidecars."""
from __future__ import annotations

from pathlib import Path

from app.core.edit_model import CANDIDATE_BREAK_WINDOW_SECONDS, EditProject


def write_marker_sidecar(output_path: Path, marker_breaks: list[float], duration: float | None) -> None:
    if not marker_breaks:
        return
    spans: list[tuple[float, float]] = []
    for timestamp in marker_breaks:
        end = timestamp + CANDIDATE_BREAK_WINDOW_SECONDS
        if duration is not None:
            if timestamp >= duration:
                continue  # pressed past the end of the file (the elapsed clock can run ahead of it); nothing left to cut
            end = min(end, duration)
        spans.append((timestamp, end))
    if not spans:
        return
    project = EditProject(video_path=str(output_path))
    project.add_color_candidates(spans)
    project.save(EditProject.sidecar_path_for(output_path))
