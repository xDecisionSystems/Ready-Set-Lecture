# Recorder code review and fix plan

## Scope and approach

This review covers the recorder entry point, setup and overlay UI, device discovery, camera preview, microphone tooling, clicker support, FFmpeg command construction, recording/pause/finalization lifecycle, marker export, settings, and the recorder-focused tests.

The implementation is well decomposed and unusually well covered at the pure-logic level. In particular, FFmpeg argument construction, DirectShow parsing, GPU capture planning, region geometry, clicker decoding, filenames, marker export, and audio gain logic have focused tests. The most important remaining risks are at process/session boundaries, where failures can turn into a corrupt “successful” recording or leave recorder resources behind.

## Findings

### P0 — A single corrupt segment can be reported as a saved recording

`RecordingSession.finalize()` only calls `_has_video()` when there is more than one non-empty segment (`app/recorder/core/ffmpeg_record.py:395-415`). A single segment is considered complete solely because it exists and has a non-zero size, then is moved to the requested output path. This matters because `_reap()` terminates and eventually kills FFmpeg after its timeout (`ffmpeg_record.py:341-354`); an MP4 interrupted before its `moov` atom is written can be non-empty but unplayable.

Impact: the UI can show “Recording saved” for an invalid file. This is a data-loss issue because the user may dismiss the success message believing the lecture is safe.

Fix: validate every candidate segment, including the one-segment case, with `ffprobe`. Validation should check for a video stream and a positive duration, not just container duration. Check the FFmpeg process return code and preserve the log and failed segment when validation fails. After concatenation, probe the final output before deleting source segments.

### P0 — Session failures abandon cleanup and may leave processes/resources active

`RecordingController._fail()` immediately drops its reference to the session via `_reset()` (`ffmpeg_record.py:596-608`). It does not stop/reap an active process, close the current log, reap processes in `_finishing`, or remove temporary artifacts. Failure paths include start/resume errors, segment switching errors, worker errors, and unexpected FFmpeg exit. `start_segment()` also opens the log before `Popen`; if command construction or `Popen` raises, that log is not closed (`ffmpeg_record.py:302-318`).

Impact: leaked file handles, orphaned FFmpeg processes in some partial-failure sequences, locked camera/microphone devices, and accumulated recordings/logs under the system temp directory.

Fix: give `RecordingSession` one idempotent `abort()`/`close()` operation that requests graceful stop, reaps all processes, closes every stream/log, and deliberately either removes or preserves diagnostic artifacts. Call it before controller state is discarded. Make `start_segment()` exception-safe around log creation and `Popen`. Retain the session until asynchronous cleanup has finished.

### P1 — Turning off “Use the graphics chip” does not disable GPU screen capture

The checkbox only writes the global encoder setting (`app/recorder/ui/setup_window.py:144-150,188`). A new `RecordingSession` always initializes `_allow_gpu = True`, and `build_ffmpeg_argv(... allow_gpu=True)` calls `plan_gpu_capture()` for a screen (`app/recorder/core/ffmpeg_record.py:131-140,275,311`). The encoder helper consults the setting, but the `ddagrab` screen-capture path does not. Therefore the checkbox can disable hardware encoding while screen frames are still captured on the graphics chip, contradicting both the label and tooltip.

Impact: user intent is not honored; machines with problematic GPU capture can still take the path the user explicitly disabled.

Fix: make GPU policy explicit session configuration, preferably `RecordConfig.allow_gpu`, populated from the checkbox at Start. Use that one value for both `plan_gpu_capture()` and `h264_args()`. Reserve the mutable session flag for automatic fallback after a startup failure. Add an argv test proving the disabled setting produces neither `ddagrab` nor a hardware encoder.

### P1 — Temporary session directories are never removed

Every recording creates `tempfile.mkdtemp(prefix="readysetlecturerecorder_")` (`app/recorder/ui/setup_window.py:570`), while finalization deletes segment and log files but never the directory (`ffmpeg_record.py:395-415`). Failure cleanup is weaker and can leave both the directory and its contents.

Impact: permanent accumulation in the user's temp directory; failed recordings may consume significant disk space.

Fix: centralize ownership of the work directory in `RecordingSession`. Remove it after verified success. On failure, either remove it after logging useful diagnostics or intentionally retain it and tell the user its location. A practical policy is to retain artifacts only for integrity/finalization failures and clean them after ordinary startup failures.

### P1 — Closing the application is not defined safely during an active recording

`RecorderMainWindow.closeEvent()` stops UI helpers but does not stop or finalize the recording controller (`app/recorder/ui/setup_window.py:341-349`). The setup window is hidden during recording, so ordinary UI flow makes this uncommon, but application quit, session shutdown, or closing via taskbar/system controls can still destroy the object graph while a worker or FFmpeg process is active.

Impact: an unfinished/corrupt output, QThread destruction warnings, and orphaned capture processes.

Fix: add an application-level shutdown protocol. If recording, prompt to either continue recording or stop and save; during countdown, cancel cleanly. Do not let Qt destroy the controller while `PauseStopWorker` is running. Add a blocking shutdown method with bounded waits specifically for application exit.

### P2 — Elapsed time loses the fraction since the last timer tick at pause/stop

Elapsed time is advanced only by a 100 ms timer (`ffmpeg_record.py:610-629`). `pause()` and `stop()` stop that timer without first accumulating `time.monotonic() - _last_tick` (`ffmpeg_record.py:526-530,564-574`). Each pause and the final stop can therefore undercount by nearly one tick. Marker timestamps use this value directly (`app/recorder/ui/setup_window.py:707-708`).

Impact: displayed duration and marker breaks drift slightly early, with error accumulating over repeated pauses.

Fix: add `_accumulate_elapsed()` and call it from the timer, immediately before pause/stop, and before accepting a marker. Unit-test with a fake monotonic clock.

### P2 — Final output naming has a time-of-check/time-of-use race

`unique_path()` selects an unused destination before the three-second countdown and potentially a long recording (`setup_window.py:488-496`). `concat_segments()` passes `-y`, which overwrites a file that appears at that path; the one-segment `shutil.move` behavior is platform-dependent.

Impact: another file created with the same name during recording can be overwritten or cause finalization to fail.

Fix: choose/reserve the destination at finalization, or create an exclusive placeholder at Start and replace only that known reservation. Never use unconditional overwrite for an unreserved user destination. Add a test that creates the chosen name during the simulated recording and verifies neither file is lost.

### P2 — Process success is inferred from artifacts rather than exit status

The normal stop/finalize flow does not inspect FFmpeg return codes. A command can exit non-zero after writing a superficially probeable partial file, and the log is then deleted after success.

Impact: degraded or truncated recordings can silently pass; useful diagnostics are erased.

Fix: have `_reap()` return structured termination information (return code, forced termination, log path). Finalization should require acceptable return status plus media validation. Treat a forced terminate/kill as a warning or failure unless the resulting file passes stronger validation and the user is explicitly informed.

## Test and environment observations

I ran the full `unittest` discovery command with the repository's `videotrim` environment. It discovered 316 tests but did not produce a trustworthy product result in this sandbox:

- QtMultimedia could not load in that environment (`ImportError: DLL load failed while importing QtMultimedia`). The recorder batch file intentionally uses a different `readysetlecture-qtmm` environment, so the failure is an environment mismatch, but it demonstrates that the documented/default development test command is not self-contained.
- Many tests that use `tempfile.TemporaryDirectory()` failed because the sandbox denied writes under the OS temp directory. Those failures then contaminated later settings tests, so the reported 14 failures and 34 errors should not be interpreted as confirmed application regressions.

Fix the test harness by directing `TMP`/`TEMP` to a repository-local scratch directory for constrained runs and document/use the QtMultimedia-capable environment. Then rerun the entire suite before implementation and after each phase below.

## Recommended implementation plan

### Phase 1 — Protect recording integrity

1. Introduce structured process results for every segment: exit code, whether graceful stop timed out, log path, and output path.
2. Make `start_segment()` exception-safe and implement idempotent session `abort()`/`close()` cleanup.
3. Validate every segment with ffprobe, including single-segment sessions. Extend probing to assert at least one video stream and positive duration.
4. Write concatenation to a temporary file in the destination directory, validate it, then atomically replace only the recorder's reserved destination.
5. Delete source segments/logs/work directory only after final output validation succeeds. On failure, preserve recoverable segments and include their folder in the error dialog.
6. Add lifecycle tests using fake `Popen` objects for: spawn failure after log open, non-zero exit, graceful timeout, forced kill, corrupt non-empty single segment, corrupt segment in a multi-segment session, concat failure, final probe failure, and repeated `abort()`.

### Phase 2 — Make shutdown and controller state deterministic

1. Model transitional states explicitly (`starting`, `recording`, `pausing`, `paused`, `resuming`, `stopping`, `failed`) instead of combining `_state` and `_worker` checks.
2. Keep the session owned until its worker and all FFmpeg processes are fully reaped.
3. Add `RecordingController.shutdown()` for application exit and wire it to `closeEvent`/`aboutToQuit` with an appropriate stop-and-save confirmation.
4. Ensure stop during resume countdown and repeated clicker/UI actions are idempotent.
5. Test state transitions and shutdown while starting, recording, pausing, paused, resuming, and stopping.

### Phase 3 — Honor settings and improve timing

1. Add explicit `allow_gpu` policy to the record configuration and apply it consistently to screen capture and encoding.
2. Separate “user disabled GPU” from “automatic GPU fallback” in status reporting.
3. Accumulate monotonic elapsed time synchronously on pause, stop, and marker creation.
4. Add focused tests for GPU-off screen argv and exact elapsed/marker behavior across repeated pauses.

### Phase 4 — Make verification reproducible

1. Provide one documented test command/environment that can import QtMultimedia.
2. Make tests use a repository-local or explicitly configurable scratch root in restricted environments.
3. Add an integration test with a generated FFmpeg source (`lavfi`) so start, graceful stop, pause/resume concatenation, output probing, cleanup, and failure preservation can run without physical hardware.
4. Perform the existing manual Windows matrix: screen and camera, mic on/off, crop/full frame, GPU on/off/fallback, cursor changes, multiple pauses, preview shown/hidden, marker while recording/paused, multi-monitor/HiDPI, and application exit mid-recording.

## Suggested acceptance criteria

- No code path reports success unless the final output contains a video stream and has positive duration.
- Failed or cancelled sessions leave no FFmpeg process, open log, or unexplained temp directory.
- Recoverable segments are not deleted when final assembly fails, and the UI tells the user where they are.
- GPU-off recordings use neither GPU capture nor a hardware encoder.
- Marker timestamps and elapsed time do not lose timer fractions at pause/stop.
- Existing destination files cannot be overwritten by a name collision arising after Start.
- Closing the application during every controller state has a deterministic, tested result.
- The complete automated suite passes in the documented recorder environment, followed by the hardware/manual matrix.

## Implementation handoff for review

Implemented on 2026-09-20. The following is intended for the next agent reviewing this change.

### What changed

- `RecordConfig` now carries an explicit `allow_gpu` policy captured from the setup checkbox at recording start. `RecordingSession` uses it for both GPU capture planning and encoder selection, while retaining the existing automatic CPU fallback behavior.
- `RecordingSession.start_segment()` is exception-safe: if argv construction or process creation fails, its log handle/file is closed and removed, and an empty work directory is removed.
- Process reaping now records forced termination and non-zero exit codes. Finalization rejects either condition and preserves the recovery directory rather than announcing success.
- Every candidate segment is validated, including a single segment. Validation now requires both a video stream (`probe_has_video`) and a positive duration (`probe_duration`).
- Final output is assembled/copied to a hidden temporary MP4 in the destination directory, validated again, and published using an atomic create-if-absent hard link. A file that appears at the chosen destination during recording is never overwritten. Source segments and logs are removed only after publication succeeds.
- Successful finalization removes the recorder work directory. Failure paths preserve useful recovery artifacts and include their directory in the error message. `RecordingSession.abort()` is idempotent and handles all active/finishing processes and logs.
- Controller startup, resume, runtime failure, and application shutdown paths now invoke session cleanup instead of dropping the session reference. Application shutdown synchronously stops and finalizes an active recording; if that cannot complete, recovery files are retained and the user receives a recovery warning.
- Elapsed time is accumulated immediately before pause and stop, and marker creation calls `mark_time()` so it includes time since the most recent 100 ms UI tick.

### Tests added or adjusted

- Added `tests/test_recorder_lifecycle.py`, covering:
  - rejection and preservation of a non-empty invalid single segment;
  - validation, collision-safe publication, and successful work-directory cleanup;
  - protection against a destination file appearing during recording;
  - forced-stop rejection;
  - idempotent abort/discard cleanup;
  - cleanup after process-spawn failure;
  - explicit GPU-off session policy;
  - exact marker and pause timing between timer ticks.
- Updated the screen-wash marker fixture to expose the controller's new `mark_time()` API.

### Verification performed

Using `C:\Users\Adan Ernesto Vela\anaconda3\envs\readysetlecture-qtmm\python.exe`:

```text
python -m unittest discover -s tests -p "test_*.py"
Ran 436 tests in 4.429s — OK

python -m compileall -q app tests
completed successfully
```

The earlier failures described in “Test and environment observations” were confirmed to be sandbox/environment artifacts: the QtMultimedia-capable environment plus normal temporary-directory access passes the complete suite.

### Reviewer focus and remaining manual checks

- Review the Windows semantics of the final `os.link(publish_path, output_path)` publication step. Both paths are deliberately in the same destination directory, so they are on the same volume; it provides atomic no-overwrite behavior. The hidden temporary link source is removed immediately afterward.
- Review shutdown while a pause/stop worker is active. Shutdown waits up to eight seconds and refuses concurrent cleanup if the worker does not finish, preserving recovery files.
- Hardware checks remain necessary because CI/unit tests cannot validate DirectShow, desktop duplication, device exclusivity, or compositor behavior. Exercise screen and camera capture, mic on/off, pause/resume, cursor changes, GPU on/off and fallback, marker timing, multi-monitor/HiDPI selection, output-name collision, and application close during recording.
