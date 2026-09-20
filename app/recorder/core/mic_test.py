"""Record from the selected microphone so the user can play it back and judge the level.

Uses the same ffmpeg dshow input and gain filter as a real recording, so the test exercises the actual
capture chain (device selection, dshow access, gain), not just the Qt level meter.
"""
from __future__ import annotations

import math
import re
import subprocess
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QThread, Signal

from app.core.ffmpeg_locator import find_ffmpeg
from app.recorder.core.ffmpeg_record import _dshow_part

# Safety cap: the user normally ends the test with Play; this only bounds a forgotten one.
MIC_TEST_MAX_SECONDS = 30

_SILENCE_DBFS = -60.0
_QUIET_DBFS = -24.0
_CLIPPING_DBFS = -1.0
_WAV_HEADER_BYTES = 44


def build_mic_test_argv(capture_id: str, gain: float, output_path: Path, seconds: int = MIC_TEST_MAX_SECONDS) -> list[str]:
    return [
        find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostats", "-y",
        "-f", "dshow", "-i", _dshow_part("audio", capture_id),
        "-af", f"volume={gain:.3f}", "-t", str(seconds), "-c:a", "pcm_s16le", str(output_path),
    ]


def peak_dbfs(wav_path: Path) -> float | None:
    """Peak level of a 16-bit PCM WAV in dBFS, or None if it holds no samples or only digital silence."""
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getsampwidth() != 2:
            return None
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    if not samples.size:
        return None
    peak = int(np.abs(samples.astype(np.int32)).max())
    return 20 * math.log10(peak / 32768) if peak else None


def describe_level(peak_db: float | None) -> str:
    if peak_db is None or peak_db < _SILENCE_DBFS:
        return "No sound picked up. Check the microphone selection and gain."
    text = f"Peak {peak_db:.0f} dBFS: "
    if peak_db >= _CLIPPING_DBFS:
        return text + "clipping, lower the gain."
    if peak_db < _QUIET_DBFS:
        return text + "quiet, raise the gain or the mic level in Windows sound settings."
    return text + "good level."


def first_error_line(log_text: str) -> str:
    """The first line of an ffmpeg error log, without its '[in#0 @ 0000...]' context prefix."""
    for line in log_text.splitlines():
        line = re.sub(r"^\[[^\]]*\]\s*", "", line.strip())
        if line:
            return line
    return ""


class MicTestWorker(QThread):
    """One-shot: records from the mic off the UI thread until stop() is called, or the safety cap."""

    done = Signal(Path)
    error = Signal(str)

    def __init__(self, capture_id: str, gain: float, output_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._capture_id, self._gain, self._output_path = capture_id, gain, output_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop_requested = False
        self._cancelled = False

    def stop(self) -> None:
        """Finish the recording cleanly (so the WAV header is valid); run() then emits done."""
        self._stop_requested = True
        self._send_quit()

    def cancel(self) -> None:
        """Abort and discard (window closing); run() emits nothing."""
        self._cancelled = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def _send_quit(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError):
            pass  # already exiting

    def run(self) -> None:
        log_path = self._output_path.with_suffix(".log")
        try:
            argv = build_mic_test_argv(self._capture_id, self._gain, self._output_path)
            # stderr goes to a file, not an unread PIPE: a full pipe would block ffmpeg and it would never see 'q'.
            with open(log_path, "wb") as log:
                self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
                if self._stop_requested:  # Play was pressed before the process existed
                    self._send_quit()
                try:
                    self._proc.wait(timeout=MIC_TEST_MAX_SECONDS + 10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
                    self.error.emit("The microphone did not respond in time.")
                    return
            if self._cancelled:
                return
            if self._proc.returncode != 0 or not self._output_path.exists() or self._output_path.stat().st_size <= _WAV_HEADER_BYTES:
                detail = first_error_line(log_path.read_text(errors="replace"))
                self.error.emit(detail or ("The recording was too short." if self._stop_requested else "ffmpeg could not record from this microphone."))
                return
            self.done.emit(self._output_path)
        except OSError as exc:
            self.error.emit(str(exc))
        finally:
            log_path.unlink(missing_ok=True)
