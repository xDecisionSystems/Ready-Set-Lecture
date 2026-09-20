"""Renders an EditProject to one or more output files.

Cut regions are removed, split points divide the result into multiple files,
and the original source is never modified — every export writes new file(s)
to the configured output directory.

Two modes:
  * fast (stream copy): near-instant even on 4K, but each cut snaps to the
    nearest keyframe, so a cut point can drift by up to a couple of seconds.
  * precise (re-encode): exact frame placement, much slower on 4K.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.edit_model import EditProject
from app.core.ffmpeg_locator import find_ffmpeg
from app.core.ffmpeg_utils import FfmpegRunError, concat_segments, unique_path
from app.core.video_encoder import h264_args

ProgressCallback = Callable[[int], None]

_TIME_RE = re.compile(rb"out_time_ms=(\d+)")


class ExportError(RuntimeError):
    pass


@dataclass
class ExportSettings:
    output_dir: Path
    precise: bool = False
    base_name: str = "output"


def build_export_groups(project: EditProject, duration: float) -> list[list[tuple[float, float]]]:
    """The list of output files to produce, each as a list of (start, end)
    ranges (in the *original* timeline) to concatenate together."""
    kept = project.kept_segments(duration)
    splits = sorted(t for t in project.split_points() if 0 < t < duration)

    groups: list[list[tuple[float, float]]] = [[]]
    for seg_start, seg_end in kept:
        cursor = seg_start
        for sp in splits:
            if seg_start < sp < seg_end:
                groups[-1].append((cursor, sp))
                groups.append([])
                cursor = sp
        groups[-1].append((cursor, seg_end))
    return [g for g in groups if g]


def export_project(
    project: EditProject,
    duration: float,
    settings: ExportSettings,
    progress_callback: ProgressCallback | None = None,
) -> list[Path]:
    _, unmatched = project.cut_regions()
    if unmatched:
        raise ExportError(
            f"{len(unmatched)} cut marker(s) aren't paired into a start/end region "
            "(an extra Cut-start or Cut-end with no match). Fix the markers before exporting."
        )

    groups = build_export_groups(project, duration)
    if not groups:
        raise ExportError("Nothing to export: the whole video is cut out.")

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    total_seconds = sum(end - start for group in groups for start, end in group)
    done_seconds = 0.0

    def report(delta_done: float) -> None:
        nonlocal done_seconds
        done_seconds += delta_done
        if progress_callback and total_seconds:
            progress_callback(min(99, int(done_seconds / total_seconds * 100)))

    outputs: list[Path] = []
    multi = len(groups) > 1
    with tempfile.TemporaryDirectory(prefix="readysetlecture_export_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for i, group in enumerate(groups, start=1):
            suffix = f"_part{i}" if multi else "_edited"
            out_path = _unique_path(settings.output_dir / f"{settings.base_name}{suffix}.mp4")

            if len(group) == 1:
                start, end = group[0]
                _extract_segment(project.video_path, start, end, out_path, settings.precise, report)
            else:
                segment_paths = []
                for j, (start, end) in enumerate(group):
                    seg_path = tmp_path / f"group{i}_seg{j}.mp4"
                    _extract_segment(project.video_path, start, end, seg_path, settings.precise, report)
                    segment_paths.append(seg_path)
                _concat_segments(segment_paths, out_path)

            outputs.append(out_path)

    if progress_callback:
        progress_callback(100)
    return outputs


def _unique_path(path: Path) -> Path:
    return unique_path(path)


def _extract_segment(
    source: str | Path,
    start: float,
    end: float,
    out_path: Path,
    precise: bool,
    report: Callable[[float], None],
) -> None:
    duration = max(0.0, end - start)
    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}"]
    if precise:
        # x264 when this ffmpeg has it (Ready, Set, Lecture! is GPL, so that is fine); otherwise OpenH264. See app.core.video_encoder.
        cmd += [*h264_args("export"), "-c:a", "aac"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-progress", "pipe:1", "-nostats", str(out_path)]

    _run_with_progress(cmd, duration, report)


def _concat_segments(segment_paths: list[Path], out_path: Path) -> None:
    try:
        concat_segments(segment_paths, out_path)
    except FfmpegRunError as exc:
        raise ExportError(str(exc)) from exc


def _run_with_progress(cmd: list[str], expected_duration: float, report: Callable[[float], None]) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    last_out_time = 0.0
    try:
        for line in proc.stdout:
            match = _TIME_RE.search(line)
            if match:
                out_time = int(match.group(1)) / 1_000_000
                report(max(0.0, out_time - last_out_time))
                last_out_time = out_time
    finally:
        proc.stdout.close()
        proc.wait()

    if proc.returncode != 0:
        raise ExportError(f"ffmpeg failed (exit {proc.returncode}) running: {' '.join(cmd)}")

    if last_out_time < expected_duration:
        report(expected_duration - last_out_time)


try:
    from PySide6.QtCore import QThread, Signal

    class ExportWorker(QThread):
        """Runs export_project off the UI thread, relaying progress via signals."""

        progress = Signal(int)
        finished_export = Signal(list)
        failed = Signal(str)

        def __init__(self, project: EditProject, duration: float, settings: ExportSettings, parent=None) -> None:
            super().__init__(parent)
            self.project = project
            self.duration = duration
            self.settings = settings

        def run(self) -> None:
            try:
                outputs = export_project(
                    self.project, self.duration, self.settings, progress_callback=self.progress.emit
                )
            except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                self.failed.emit(str(exc))
                return
            self.finished_export.emit(outputs)

except ImportError:
    pass  # PySide6 not required for headless use of export_project
