"""ffmpeg argv construction and the recorder process lifecycle."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QImage

from app.core.ffmpeg_locator import find_ffmpeg
from app.core.ffmpeg_utils import concat_segments, probe_duration, probe_has_video
from app.core.video_encoder import h264_args, is_hardware_encoder
from app.recorder.core.gpu_capture import plan_gpu_capture


@dataclass(frozen=True)
class ScreenCaptureConfig:
    capture_rect: tuple[int, int, int, int]
    fps: int = 30
    draw_cursor: bool = True
    # Which screen the area is on (Windows' name for it, e.g. \\.\DISPLAY1) and that whole screen's rectangle, in the same physical desktop
    # pixels as capture_rect. Only the graphics-chip capture needs them: it captures one display and takes the area as an offset inside it.
    screen_name: str | None = None
    screen_rect: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class CameraCaptureConfig:
    capture_id: str
    native_size: tuple[int, int]
    crop_rect: tuple[int, int, int, int] | None = None
    fps: int = 30
    preview_size: tuple[int, int] | None = None  # (width, height): also stream small raw preview frames to stdout
    # Ask the camera for this format: "h264", "hevc" or "mjpeg" (-vcodec), "pixel:nv12" and the like for an uncompressed one
    # (-pixel_format), or None to let DirectShow choose.
    input_format: str | None = None

    @property
    def saves_stream_as_is(self) -> bool:
        """The camera compresses the picture itself (H.264 or H.265) and nothing has to change in it, so ffmpeg only saves the stream:
        no decoding, no encoding. (A crop needs the picture decoded and encoded again.)"""
        return self.input_format in ("h264", "hevc") and not self.crop_rect


@dataclass(frozen=True)
class AudioCaptureConfig:
    capture_id: str | None
    gain: float = 1.0


@dataclass(frozen=True)
class RecordConfig:
    source: ScreenCaptureConfig | CameraCaptureConfig
    audio: AudioCaptureConfig
    output_path: Path
    allow_gpu: bool = True


VIDEO_TIMESCALE = 90000
PREVIEW_WIDTH = 1280  # the preview fills the screen, so it needs to be sharper than a thumbnail
PREVIEW_FPS = 10


def preview_frame_size(source_size: tuple[int, int], width: int = PREVIEW_WIDTH) -> tuple[int, int]:
    """Size of the preview frames for a recording of *source_size*: up to *width* wide, same aspect, even numbers."""
    source_width, source_height = source_size
    preview_width = max(2, min(width, source_width))
    preview_width -= preview_width % 2
    return preview_width, max(2, round(preview_width * source_height / source_width / 2) * 2)


def _camera_crop(source: CameraCaptureConfig) -> str:
    if not source.crop_rect:
        return ""
    x, y, crop_width, crop_height = source.crop_rect
    return f"crop={crop_width}:{crop_height}:{x}:{y}"


def _capture_buffer(width: int, height: int, fps: int, seconds: float = 1.2, minimum_mb: int = 150) -> str:
    """dshow's real-time buffer (-rtbufsize): about *seconds* of raw frames, two bytes a pixel, and never less than *minimum_mb*.
    It is only a ceiling (memory is used as a backlog builds). 1080p30 stays at 150 MB; 4K30 gets about 600 MB, since a few
    slow moments at 4K would otherwise fill a fixed 150 MB after just nine frames."""
    return f"{max(minimum_mb, round(width * height * 2 * fps * seconds / 1_000_000))}M"


def _input_format_args(source: CameraCaptureConfig) -> list[str]:
    """The DirectShow input options that pick the camera's format."""
    fmt = source.input_format
    if not fmt:
        return []
    return ["-pixel_format", fmt[len("pixel:"):]] if fmt.startswith("pixel:") else ["-vcodec", fmt]


_CAMERA_FORMAT_NAMES = {"h264": "H.264", "hevc": "H.265", "mjpeg": "MJPEG"}


def describe_recording(source: ScreenCaptureConfig | CameraCaptureConfig) -> str:
    """One line about how the picture was recorded, for the "saved" message."""
    if isinstance(source, ScreenCaptureConfig):
        return ""
    width, height = source.crop_rect[2:] if source.crop_rect else source.native_size
    fmt = source.input_format or ""
    name = _CAMERA_FORMAT_NAMES.get(fmt)
    if source.saves_stream_as_is:
        how = f"the camera's own {name} stream, saved as it came (no re-encoding)"
    elif name:
        how = f"encoded by the computer from the camera's {name} stream"
    elif fmt.startswith("pixel:"):
        how = "encoded by the computer from the camera's uncompressed picture"
    else:
        how = "encoded by the computer"
    return f"Camera: {width}×{height}, {how}."


def _dshow_part(kind: str, capture_id: str) -> str:
    # No quotes: argv goes to Popen as a list, so ffmpeg would receive them literally
    # and fail with "Could not find video device with name ["..."]".
    return capture_id if capture_id.startswith(f"{kind}=") else f"{kind}={capture_id}"


def uses_graphics_chip(argv: list[str]) -> bool:
    """Whether this ffmpeg command captures the screen on the graphics chip, or encodes on it."""
    encoder = argv[argv.index("-c:v") + 1] if "-c:v" in argv and argv.index("-c:v") + 1 < len(argv) else ""
    return is_hardware_encoder(encoder) or any(part.startswith("ddagrab=") for part in argv)


def build_ffmpeg_argv(config: RecordConfig, output_path: Path | None = None, allow_gpu: bool = True) -> list[str]:
    """Build an ffmpeg command for one self-contained MP4 recording segment. *allow_gpu* False keeps the graphics chip out of it."""
    output = output_path or config.output_path
    source, audio = config.source, config.audio
    cmd = [find_ffmpeg(), "-y"]
    gpu = None
    if isinstance(source, ScreenCaptureConfig):
        x, y, width, height = source.capture_rect
        gpu = plan_gpu_capture(source) if allow_gpu else None
        if gpu is not None:
            cmd += gpu.input_args  # the screen is read on the chip and stays there until it is encoded
        else:
            cmd += ["-f", "gdigrab", "-framerate", str(source.fps)]
            if not source.draw_cursor:
                cmd += ["-draw_mouse", "0"]
            cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{width}x{height}", "-i", "desktop"]
        if audio.capture_id:
            cmd += ["-f", "dshow", "-i", _dshow_part("audio", audio.capture_id), "-map", "0:v", "-map", "1:a", "-af", f"volume={audio.gain:.3f}"]
        else:
            cmd += ["-an"]
    else:
        width, height = source.native_size
        capture = _dshow_part("video", source.capture_id)
        if audio.capture_id:
            capture += ":" + _dshow_part("audio", audio.capture_id)
        cmd += ["-rtbufsize", _capture_buffer(width, height, source.fps), "-f", "dshow", *_input_format_args(source),
                "-video_size", f"{width}x{height}", "-framerate", str(source.fps), "-i", capture]
        crop = _camera_crop(source)
        if source.preview_size:
            preview_width, preview_height = source.preview_size
            preview = f"fps={PREVIEW_FPS},scale={preview_width}:{preview_height},format=bgra[pvo]"
            if source.saves_stream_as_is:
                # The recording is the camera's own stream, copied. Only the small preview needs the picture decoded.
                cmd += ["-filter_complex", f"[0:v]{preview}", "-map", "0:v"]
            else:
                # One process, one camera open: the (cropped) picture is split into the recording and a small preview stream.
                cmd += ["-filter_complex", f"[0:v]{crop + ',' if crop else ''}split=2[rec][pv];[pv]{preview}", "-map", "[rec]"]
            cmd += ["-map", "0:a", "-af", f"volume={audio.gain:.3f}"] if audio.capture_id else ["-an"]
        else:
            if crop:
                cmd += ["-vf", crop]
            if audio.capture_id:
                cmd += ["-af", f"volume={audio.gain:.3f}"]
            else:
                cmd += ["-an"]
    # A fixed video time scale, so every segment has the same time base. Left to itself the encoder derives one from
    # the capture's irregular frame timing (1/15360, 1/30000, 1/11456, ...), and joining segments whose time bases
    # differ sometimes squashed or stretched everything after the join.
    if isinstance(source, CameraCaptureConfig) and source.saves_stream_as_is:
        # H.265 is tagged hvc1: ffmpeg's default (hev1) is refused by many players, including Apple's and some on Windows.
        video_options = ["-c:v", "copy", *(["-tag:v", "hvc1"] if source.input_format == "hevc" else [])]
    else:
        recorded_width, recorded_height = source.capture_rect[2:] if isinstance(source, ScreenCaptureConfig) else (source.crop_rect[2:] if source.crop_rect else source.native_size)
        if gpu is not None:
            cmd += ["-vf", gpu.filter]  # labels the frames with the colours the chip really made (see gpu_capture)
            video_options = gpu.encoder_args
        else:
            video_options = h264_args("record", pixels=recorded_width * recorded_height, allow_filters=False, gpu=allow_gpu)  # a camera command has a -vf or -filter_complex of its own
    cmd += [*video_options, "-video_track_timescale", str(VIDEO_TIMESCALE)]
    if audio.capture_id:
        cmd += ["-c:a", "aac"]
    cmd += ["-movflags", "+faststart", str(output)]
    if isinstance(source, CameraCaptureConfig) and source.preview_size:
        cmd += ["-map", "[pvo]", "-f", "rawvideo", "pipe:1"]  # a second output: raw BGRA preview frames on stdout
    return cmd


def _drain_frames(stream: BinaryIO | None, frame_bytes: int, callback: Callable[[bytes], None] | None) -> None:
    """Read fixed-size raw frames until the stream ends, handing each to *callback*.

    It must never stop reading early: a full pipe blocks ffmpeg, so a consumer that raises is ignored, not obeyed.
    A partial frame at the end (ffmpeg was stopped mid-frame) is dropped.
    """
    try:
        while stream is not None:
            frame = stream.read(frame_bytes)
            if len(frame) < frame_bytes:
                break
            if callback is not None:
                try:
                    callback(frame)
                except Exception:  # noqa: BLE001
                    pass
    except (OSError, ValueError):
        pass
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def build_preview_argv(source: CameraCaptureConfig) -> list[str]:
    """A camera-only ffmpeg command that streams just the preview frames (the same crop and size the recording will have)."""
    assert source.preview_size is not None
    width, height = source.native_size
    preview_width, preview_height = source.preview_size
    crop = _camera_crop(source)
    graph = f"[0:v]{crop + ',' if crop else ''}fps={PREVIEW_FPS},scale={preview_width}:{preview_height},format=bgra[pvo]"
    return [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-rtbufsize", _capture_buffer(width, height, source.fps, 0.5, 64), "-f", "dshow", *_input_format_args(source),
            "-video_size", f"{width}x{height}", "-framerate", str(source.fps), "-i", _dshow_part("video", source.capture_id), "-filter_complex", graph, "-map", "[pvo]", "-f", "rawvideo", "pipe:1"]


class PreviewFeed:
    """Live preview frames while nothing is being recorded: the 3-2-1 before a start or a resume, and while paused.

    The camera can only be open in one process, so this must be stopped (stop() waits until the camera is free)
    before the recorder starts.
    """

    def __init__(self, source: CameraCaptureConfig, on_frame: Callable[[bytes], None]) -> None:
        assert source.preview_size is not None
        self._source, self._on_frame = source, on_frame
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        width, height = self._source.preview_size
        self._proc = subprocess.Popen(build_preview_argv(self._source), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._reader = threading.Thread(target=_drain_frames, args=(self._proc.stdout, width * height * 4, self._on_frame), daemon=True)
        self._reader.start()

    def stop(self, timeout: float = 3.0) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        RecordingSession._request_stop(proc)
        RecordingSession._reap(proc, None, timeout)
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None


class RecordingSession:
    def __init__(self, config: RecordConfig, work_dir: Path, on_preview_frame: Callable[[bytes], None] | None = None) -> None:
        self.config = config
        self.work_dir = work_dir
        self._on_preview_frame = on_preview_frame  # called on a reader thread with each raw BGRA preview frame
        self.proc: subprocess.Popen[bytes] | None = None
        self.segment_paths: list[Path] = []
        self._log: BinaryIO | None = None
        self._finishing: list[tuple[subprocess.Popen[bytes], BinaryIO | None]] = []  # stopped segments still closing their files
        self.dropped_frame_warnings = 0  # set by finalize(): how often ffmpeg said the camera stream was backing up
        self._allow_gpu = config.allow_gpu  # false when disabled by the user, or after an automatic CPU fallback
        self._forced_stop = False
        self._bad_exit_codes: list[int] = []
        self.segment_used_gpu = False  # the newest segment captures or encodes on the graphics chip
        self.captured_on_gpu = False  # ... and it captures the screen there
        self.fell_back_to_cpu = False
        self._segment_started = 0.0

    def set_cursor_visible(self, visible: bool) -> bool:
        """Record the mouse cursor (or not) from the next segment on. Screen capture only; True if that changed anything."""
        source = self.config.source
        if not isinstance(source, ScreenCaptureConfig) or source.draw_cursor == visible:
            return False
        self.config = replace(self.config, source=replace(source, draw_cursor=visible))
        return True

    def switch_segment(self) -> Path:
        """Start the next segment right now, with the current config, while the previous one is still closing.

        ffmpeg stops capturing the moment it reads 'q' but takes a second or two to finish its file, so overlapping
        the two costs the recording only the new process's start-up time, not the previous one's shutdown time.
        """
        previous, self.proc = self.proc, None
        if previous is not None:
            self._request_stop(previous)
            self._finishing.append((previous, self._log))
            self._log = None
        return self.start_segment()

    def start_segment(self) -> Path:
        if self.poll_alive():
            raise RuntimeError("A recording segment is already active.")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        path = self.work_dir / f"segment_{len(self.segment_paths) + 1:03d}.mp4"
        # stderr goes to a file, not a PIPE nobody reads: once the pipe buffer fills, ffmpeg blocks on its
        # next log write, never sees the 'q' stop, and gets terminated, leaving an MP4 with no moov atom.
        log = open(path.with_suffix(".log"), "wb")
        try:
            preview = self.config.source.preview_size if isinstance(self.config.source, CameraCaptureConfig) else None
            argv = build_ffmpeg_argv(self.config, path, allow_gpu=self._allow_gpu)
            self.segment_used_gpu = uses_graphics_chip(argv)
            self.captured_on_gpu = any(part.startswith("ddagrab=") for part in argv)
            self._segment_started = time.monotonic()
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE if preview else subprocess.DEVNULL, stderr=log)
        except Exception:
            log.close()
            path.with_suffix(".log").unlink(missing_ok=True)
            self._remove_empty_work_dir()
            raise
        self._log = log
        self.proc = proc
        if preview:
            threading.Thread(target=_drain_frames, args=(self.proc.stdout, preview[0] * preview[1] * 4, self._on_preview_frame), daemon=True).start()
        self.segment_paths.append(path)
        return path

    def log_tail(self, chars: int = 600) -> str:
        """Tail of the newest segment's ffmpeg log, for failure messages."""
        if not self.segment_paths:
            return ""
        try:
            return self.segment_paths[-1].with_suffix(".log").read_text(errors="replace")[-chars:].strip()
        except OSError:
            return ""

    @staticmethod
    def _request_stop(proc: subprocess.Popen[bytes]) -> None:
        """Ask ffmpeg to finish its file cleanly (a hard kill leaves an MP4 with no moov atom)."""
        try:
            if proc.stdin:
                proc.stdin.write(b"q\n")
                proc.stdin.flush()
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass  # already exiting

    @staticmethod
    def _reap(proc: subprocess.Popen[bytes], log: BinaryIO | None, timeout: float) -> bool:
        forced = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            forced = True
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        finally:
            if log:
                log.close()
        return forced

    def _reap_finishing(self, timeout: float) -> None:
        finishing, self._finishing = self._finishing, []
        for proc, log in finishing:
            self._forced_stop |= self._reap(proc, log, timeout)
            if proc.returncode not in (None, 0):
                self._bad_exit_codes.append(proc.returncode)

    def stop_segment_blocking(self, timeout: float = 5.0) -> None:
        proc, self.proc = self.proc, None
        log, self._log = self._log, None
        if proc is not None:
            self._request_stop(proc)
            self._forced_stop |= self._reap(proc, log, timeout)
            if proc.returncode not in (None, 0):
                self._bad_exit_codes.append(proc.returncode)
        self._reap_finishing(timeout)

    def poll_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # A hardware encoder or a display the chip can't duplicate is refused right as ffmpeg starts. A segment that dies that soon on the chip is
    # a start-up failure, not a lost recording, so it is safe to throw away and start again on the CPU. Any later death is a real failure: a
    # second segment made by a different encoder can't be joined to the first, so nothing is switched over mid-recording.
    START_UP_SECONDS = 6.0

    def fall_back_to_cpu(self) -> bool:
        """Called when ffmpeg has died. If that was a start-up failure of a graphics-chip segment (the first of this recording), forget the
        segment, use the CPU from now on and return True: the caller then starts the segment again. Otherwise False."""
        if (not self._allow_gpu or not self.segment_used_gpu or len(self.segment_paths) != 1
                or time.monotonic() - self._segment_started > self.START_UP_SECONDS):
            return False
        self._allow_gpu = False
        self.fell_back_to_cpu = True
        self.segment_used_gpu = self.captured_on_gpu = False
        self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        path = self.segment_paths.pop()
        path.unlink(missing_ok=True)
        path.with_suffix(".log").unlink(missing_ok=True)
        return True

    def finalize(self, output_path: Path) -> Path:
        self._reap_finishing(5.0)
        completed = [path for path in self.segment_paths if path.exists() and path.stat().st_size > 0]
        # A killed MP4 can be non-empty but lack a readable movie header, including in the one-segment case.
        completed = [path for path in completed if self._has_video(path)]
        if not completed:
            raise RuntimeError(f"ffmpeg did not produce a usable recording segment. Recovery files are in {self.work_dir}")
        if self._forced_stop:
            raise RuntimeError(f"ffmpeg had to be terminated. Recovery files are in {self.work_dir}")
        if self._bad_exit_codes:
            codes = ", ".join(str(code) for code in self._bad_exit_codes)
            raise RuntimeError(f"ffmpeg exited with an error ({codes}). Recovery files are in {self.work_dir}")
        self.dropped_frame_warnings = sum(self._count_drop_warnings(path.with_suffix(".log")) for path in self.segment_paths)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        publish_path = output_path.with_name(f".{output_path.stem}.{uuid.uuid4().hex}.partial{output_path.suffix}")
        try:
            if len(completed) == 1:
                shutil.copy2(completed[0], publish_path)
            else:
                concat_segments(completed, publish_path)
            if not self._has_video(publish_path):
                raise RuntimeError(f"The assembled recording failed validation. Recovery files are in {self.work_dir}")
            try:
                # Atomic create-if-absent: never overwrite a name that appeared while recording.
                os.link(publish_path, output_path)
            except FileExistsError as exc:
                raise RuntimeError(f"A file now exists at {output_path}. Recovery files are in {self.work_dir}") from exc
        finally:
            publish_path.unlink(missing_ok=True)
        for path in self.segment_paths:
            path.unlink(missing_ok=True)
            path.with_suffix(".log").unlink(missing_ok=True)
        self._remove_empty_work_dir()
        return output_path

    def abort(self, preserve_files: bool = True, timeout: float = 5.0) -> None:
        """Idempotently stop every process and close every log owned by the session."""
        self.stop_segment_blocking(timeout)
        if not preserve_files:
            for path in self.segment_paths:
                path.unlink(missing_ok=True)
                path.with_suffix(".log").unlink(missing_ok=True)
            self._remove_empty_work_dir()

    def _remove_empty_work_dir(self) -> None:
        try:
            self.work_dir.rmdir()
        except OSError:
            pass

    @staticmethod
    def _count_drop_warnings(log: Path) -> int:
        """ffmpeg logs 'real-time buffer [camera] ... too full or near too full' when the capture is outrunning what can be encoded."""
        try:
            text = log.read_text(errors="replace")
        except OSError:
            return 0
        return sum(1 for line in text.splitlines() if "real-time buffer" in line and "too full" in line)

    @staticmethod
    def _has_video(path: Path) -> bool:
        try:
            return probe_has_video(path) and probe_duration(path) > 0.05
        except Exception:  # noqa: BLE001 - an unreadable segment is simply left out
            return False


class PauseStopWorker(QThread):
    done = Signal()
    finalized = Signal(Path)
    error = Signal(str)

    def __init__(self, session: RecordingSession, output_path: Path | None, parent=None) -> None:
        super().__init__(parent)
        self.session, self.output_path = session, output_path
        self.result_path: Path | None = None
        self.error_message: str | None = None

    def run(self) -> None:
        try:
            self.session.stop_segment_blocking()
            if self.output_path is None:
                self.done.emit()
            else:
                self.result_path = self.session.finalize(self.output_path)
                self.finalized.emit(self.result_path)
        except Exception as exc:  # noqa: BLE001
            self.error_message = str(exc)
            self.error.emit(self.error_message)


class RecordingController(QObject):
    state_changed = Signal(str)
    elapsed_changed = Signal(float)
    finished = Signal(Path)
    failed = Signal(str)
    preview_image = Signal(QImage)  # a small live frame of what is being recorded (camera only)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.session: RecordingSession | None = None
        self._preview_size: tuple[int, int] | None = None
        self._feed: PreviewFeed | None = None
        self.elapsed_seconds = 0.0
        self.dropped_frame_warnings = 0  # of the recording that just finished: how often the camera stream backed up
        self.gpu_fell_back = False  # the graphics chip could not start, so this recording is on the CPU
        self.captured_on_gpu = False  # of the recording that just finished: the screen was read on the graphics chip
        self._last_tick = 0.0
        self._state: str | None = None
        self._worker: PauseStopWorker | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    @property
    def state(self) -> str | None:
        return self._state

    @property
    def previewing(self) -> bool:
        """True while a preview feed (the camera picture outside a recording segment) is running."""
        return self._feed is not None

    def start_preview_feed(self, source: CameraCaptureConfig) -> None:
        """Show the live camera picture while nothing is being recorded: the countdowns, and while paused.
        start() and resume() stop it themselves, so the recorder can have the camera."""
        self.stop_preview_feed()
        if source.preview_size is None:
            return
        self._preview_size = source.preview_size
        feed = PreviewFeed(source, self._emit_preview)
        try:
            feed.start()
        except OSError:
            return  # no preview during the countdown; the recording itself is unaffected
        self._feed = feed

    def stop_preview_feed(self) -> None:
        """Release the camera (blocks until ffmpeg has exited) so the recorder can open it."""
        feed, self._feed = self._feed, None
        if feed is not None:
            feed.stop()

    def start(self, config: RecordConfig, work_dir: Path, final_output: Path) -> None:
        if self._state or self._worker:
            return
        self.stop_preview_feed()
        try:
            self._preview_size = config.source.preview_size if isinstance(config.source, CameraCaptureConfig) else None
            self.session = RecordingSession(config, work_dir, self._emit_preview)
            self.session.start_segment()
        except Exception as exc:  # noqa: BLE001
            if self.session is not None:
                self.session.abort(preserve_files=False)
                self.session = None
            self.failed.emit(str(exc))
            return
        self._final_output = final_output
        self.gpu_fell_back = False
        self.elapsed_seconds = 0.0
        self._last_tick = time.monotonic()
        self._state = "recording"
        self._timer.start()
        self.state_changed.emit(self._state)
        self.elapsed_changed.emit(self.elapsed_seconds)

    def pause(self) -> None:
        if self._state != "recording" or not self.session or self._worker:
            return
        self._accumulate_elapsed()
        self._timer.stop()
        self._start_worker(None)

    def resume(self) -> None:
        if self._state != "paused" or not self.session:
            return
        self.stop_preview_feed()
        try:
            self.session.start_segment()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return
        self._last_tick = time.monotonic()
        self._timer.start()
        self._state = "recording"
        self.state_changed.emit(self._state)

    def _emit_preview(self, frame: bytes) -> None:
        """Runs on the session's reader thread; Qt delivers the signal to the UI thread."""
        if self._preview_size is None:
            return
        width, height = self._preview_size
        self.preview_image.emit(QImage(frame, width, height, width * 4, QImage.Format.Format_RGB32).copy())

    def set_cursor_visible(self, visible: bool) -> None:
        """Show or hide the mouse cursor in a screen recording. While recording this starts a new segment at once
        (the previous one keeps closing in the background); while paused it applies from the next resume."""
        if self.session is None or not self.session.set_cursor_visible(visible):
            return
        if self._state == "recording" and not self._worker:
            try:
                self.session.switch_segment()
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)

    def stop(self) -> None:
        if self._worker or not self.session:
            return
        if self._state == "recording":
            self._accumulate_elapsed()
        self._timer.stop()
        if self._state == "paused":
            try:
                self._finish(self.session.finalize(self._final_output))
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
        else:
            self._start_worker(self._final_output)

    def _start_worker(self, output_path: Path | None) -> None:
        assert self.session is not None
        self._worker = PauseStopWorker(self.session, output_path, self)
        self._worker.done.connect(self._paused)
        self._worker.finalized.connect(self._finish)
        self._worker.error.connect(self._fail)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _paused(self) -> None:
        self._state = "paused"
        self.state_changed.emit(self._state)

    def _finish(self, output: Path) -> None:
        self.dropped_frame_warnings = self.session.dropped_frame_warnings if self.session else 0
        self.captured_on_gpu = bool(self.session and self.session.captured_on_gpu)
        self.gpu_fell_back = bool(self.session and self.session.fell_back_to_cpu)
        self._reset()
        self.finished.emit(output)

    def _fail(self, exc: Exception | str) -> None:
        session = self.session
        recovery = ""
        if session is not None:
            try:
                session.abort(preserve_files=True)
                if session.work_dir.exists() and any(session.work_dir.iterdir()):
                    recovery = f"\n\nRecovery files are in {session.work_dir}"
                else:
                    session._remove_empty_work_dir()
            except Exception as cleanup_exc:  # noqa: BLE001
                recovery = f"\n\nCleanup also failed: {cleanup_exc}"
        self._reset()
        self.failed.emit(str(exc) + recovery)

    def _worker_finished(self) -> None:
        if self._worker:
            self._worker.deleteLater()
            self._worker = None

    def _reset(self) -> None:
        self._timer.stop()
        self._state = None
        self.session = None

    def _tick(self) -> None:
        if self._state != "recording" or not self.session:
            return
        if not self.session.poll_alive():
            if self.session.fall_back_to_cpu():
                try:
                    self.session.start_segment()  # the same recording, from the start, on the CPU
                except Exception as exc:  # noqa: BLE001
                    self._fail(exc)
                    return
                self._last_tick = time.monotonic()
                self.gpu_fell_back = True
                return
            tail = self.session.log_tail()
            self._fail("ffmpeg stopped unexpectedly." + (f"\n\n{tail}" if tail else ""))
            return
        self._accumulate_elapsed()

    def _accumulate_elapsed(self) -> None:
        if self._state != "recording":
            return
        now = time.monotonic()
        self.elapsed_seconds += max(0.0, now - self._last_tick)
        self._last_tick = now
        self.elapsed_changed.emit(self.elapsed_seconds)

    def mark_time(self) -> float:
        """Return an up-to-date timestamp for a marker, independent of the UI timer cadence."""
        self._accumulate_elapsed()
        return self.elapsed_seconds

    def shutdown(self) -> Path | None:
        """Stop and save synchronously on application exit; preserve recovery files on failure."""
        self._timer.stop()
        self.stop_preview_feed()
        worker = self._worker
        if worker is not None:
            if not worker.wait(8000):
                raise RuntimeError("The recording worker did not finish during shutdown; recovery files were preserved.")
            if worker.error_message is not None:
                raise RuntimeError(worker.error_message)
            if worker.result_path is not None:
                self._reset()
                return worker.result_path
        session = self.session
        output = getattr(self, "_final_output", None)
        if session is None:
            return None
        try:
            session.stop_segment_blocking()
            if output is not None:
                return session.finalize(output)
            session.abort(preserve_files=True)
            return None
        finally:
            self._reset()
