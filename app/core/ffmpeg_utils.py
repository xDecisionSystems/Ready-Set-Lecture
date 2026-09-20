"""Small ffmpeg helpers shared by the editor and recorder."""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.core.ffmpeg_locator import find_ffmpeg, find_ffprobe


class FfmpegRunError(RuntimeError):
    """An ffmpeg/ffprobe operation could not be completed."""


def unique_path(path: Path) -> Path:
    """Return *path*, or the first numbered sibling that does not exist."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    number = 2
    while True:
        candidate = path.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def concat_segments(segment_paths: list[Path], output_path: Path) -> None:
    """Losslessly concatenate compatible MP4 segments with ffmpeg."""
    if not segment_paths:
        raise FfmpegRunError("Cannot concatenate an empty segment list.")
    list_path = output_path.with_suffix(".concat.txt")
    try:
        lines = ["file '" + str(path.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for path in segment_paths]
        list_path.write_text("\n".join(lines), encoding="utf-8")
        result = subprocess.run(
            [find_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)],
            capture_output=True,
        )
        if result.returncode != 0:
            raise FfmpegRunError(
                f"ffmpeg concat failed: {result.stderr.decode(errors='replace')[-2000:]}"
            )
    finally:
        list_path.unlink(missing_ok=True)


def probe_duration(path: Path) -> float:
    """Return a media file's container duration in seconds."""
    result = subprocess.run(
        [find_ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise FfmpegRunError(result.stderr.decode(errors="replace")[-2000:] or "ffprobe failed")
    try:
        return float(result.stdout.decode().strip())
    except ValueError as exc:
        raise FfmpegRunError(f"ffprobe returned no usable duration for {path}") from exc
