"""Extracts a downsampled amplitude envelope from a video's audio track, for
drawing a waveform under the seek bar.

Decodes the whole audio track in one ffmpeg pass to raw 16-bit mono PCM at a
low sample rate (audio decode is cheap compared to the video-frame scanning
elsewhere in this app — even an hour-plus lecture recording decodes in a few
seconds), then collapses it down to a fixed number of (min, max) peak pairs
so the widget has a small, constant-size array to paint regardless of the
video's length.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

from app.core.ffmpeg_locator import find_ffmpeg

DEFAULT_SAMPLE_RATE = 8000  # plenty for an amplitude envelope; keeps the decode+pipe small
DEFAULT_PEAK_COUNT = 2000  # far more than any realistic seek-bar pixel width, so it never looks blocky


def extract_waveform_peaks(
    video_path: str | Path,
    peak_count: int = DEFAULT_PEAK_COUNT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """Returns an (peak_count, 2) float32 array of (min, max) amplitude per
    time bucket, each in [-1, 1]. Buckets are equal-width slices of the
    decoded sample stream, so peaks[i] always corresponds to the same
    fraction of the video's duration regardless of peak_count.
    """
    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg, "-v", "error",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}: {proc.stderr.decode(errors='replace')}")

    samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return np.zeros((peak_count, 2), dtype=np.float32)

    # np.array_split handles sample counts that don't divide evenly into
    # peak_count buckets (true for almost every real video), unlike a plain
    # reshape which requires an exact multiple.
    buckets = np.array_split(samples, min(peak_count, samples.size))
    peaks = np.array([(b.min(), b.max()) for b in buckets], dtype=np.float32)

    # Normalize so the loudest excursion in the track reaches full height —
    # otherwise a quiet recording (or one just mixed with headroom) renders
    # as a barely-visible flat line instead of using the available bar height.
    peak_abs = float(np.abs(peaks).max())
    if peak_abs > 0:
        peaks /= peak_abs

    # Pad up to peak_count with silence if the audio was shorter than
    # peak_count samples (a near-empty/very short clip) — keeps the returned
    # array a consistent size for the widget to index into.
    if len(peaks) < peak_count:
        pad = np.zeros((peak_count - len(peaks), 2), dtype=np.float32)
        peaks = np.vstack([peaks, pad])
    return peaks


try:
    from PySide6.QtCore import QThread, Signal

    class WaveformWorker(QThread):
        """Runs extract_waveform_peaks off the UI thread."""

        finished_waveform = Signal(object)  # np.ndarray
        failed = Signal(str)

        def __init__(self, video_path: str | Path, parent=None) -> None:
            super().__init__(parent)
            self.video_path = video_path

        def run(self) -> None:
            try:
                peaks = extract_waveform_peaks(self.video_path)
            except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                self.failed.emit(str(exc))
                return
            self.finished_waveform.emit(peaks)

except ImportError:
    pass  # PySide6 not required for CLI/headless use of extract_waveform_peaks


def _main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m app.core.waveform <video_path>")
        raise SystemExit(1)
    peaks = extract_waveform_peaks(sys.argv[1])
    print(f"{len(peaks)} peaks, amplitude range [{peaks.min():.3f}, {peaks.max():.3f}]")


if __name__ == "__main__":
    _main()
