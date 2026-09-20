# VideoTrim Recorder — companion recording app

> **For the implementing agent (Claude Code, Codex, or otherwise):** this document is meant to be self-contained — everything needed to execute is below (exact file paths, function signatures, ffmpeg argv shapes, settings keys, risks, and a concrete build order). No other conversation context should be required.

## Context

VideoTrim currently only *edits* pre-existing lecture recordings. The user wants a companion app, in the same repo, that *creates* the recordings in the first place: pick a camera or a screen region, pick a mic, watch levels, get a 3-2-1 countdown, record with a small non-recorded floating control frame, and save to a remembered/MRU folder. It should feel like a natural sibling to VideoTrim — same repo, same conventions (ffmpeg subprocess style, `QSettings`-based prefs, shared-base-class custom-painted widgets) — but is a distinct, standalone entry point, not a mode bolted onto `MainWindow`.

It should also close the loop with VideoTrim's editing side: while recording, the user can press a **Marker Break** button on the floating overlay at any moment they'd want to trim later (a mistake, a pause, a section break), and those presses land in the exact same `<video>.videotrim.json` sidecar VideoTrim already auto-loads on open. This reuses VideoTrim's own existing mechanism rather than inventing a new one — `app/ui/marker_panel.py` already has a "+ Marker Break" button (`_add_candidate_break`) that adds a 30-second CANDIDATE span "as if the colored paper had been held up there"; the recorder's live button does the same thing at record time instead of edit time.

Architecture decision already made with the user: **capture is ffmpeg-driven** (Windows `gdigrab` for screen, `dshow` for camera+mic), not Qt's native `QCamera`/`QScreenCapture`/`QMediaRecorder`, because Qt's native capture can't crop to an arbitrary sub-region and this app's headline feature is exactly that region-select. Qt (`QtMultimedia`, already available via the existing `PySide6` dependency) is used only for *monitoring*: the live mic-level meter and the live camera-preview used while picking a crop region — never for the actual recorded stream.

Scope is **Windows-first**, matching the rest of the app's actual deployment reality today (`packaging/vendor/win-64` only, `ffmpeg_locator`'s conda-layout branch is win32-specific). Platform-specific pieces (dshow/gdigrab argv, `SetWindowDisplayAffinity`) are isolated so Mac/Linux support is additive later, not a rewrite.

## New file layout

New top-level package, sibling to `app/core`/`app/ui` (keeps camera/dshow/overlay concerns out of the trim editor's UI package; only needs `ffmpeg_locator.find_ffmpeg()`, the `settings.py` pattern, and a small extracted ffmpeg helper from the trim app):

```
app/recorder/
  main.py                        # entry point: python -m app.recorder.main
  core/
    devices.py                   # camera/mic/screen enumeration
    ffmpeg_record.py             # argv builder + RecordingSession + RecordingController
    audio_meter.py                # QAudioSource-based live level meter
    marker_export.py              # writes marker-break presses into a VideoTrim-compatible sidecar JSON
  ui/
    setup_window.py               # RecorderMainWindow
    region_picker.py              # ScreenRegionPicker / CameraRegionPicker
    countdown_overlay.py          # CountdownOverlay
    floating_overlay.py           # FloatingOverlay (Record/Pause, Marker Break, Stop)
    save_location_dialog.py       # SaveLocationDialog
    level_bar.py                  # AudioLevelBar (custom-painted QWidget)

app/core/ffmpeg_utils.py          # NEW — extracted from exporter.py, shared; also gains probe_duration()
app/core/edit_model.py            # MODIFIED — CANDIDATE_BREAK_WINDOW_SECONDS promoted to a module-level constant
app/core/settings.py              # MODIFIED — add record/* keys+functions
app/core/exporter.py              # MODIFIED — delegate to ffmpeg_utils.py
app/ui/marker_panel.py            # MODIFIED — imports CANDIDATE_BREAK_WINDOW_SECONDS from edit_model.py instead of defining it locally

run_videotrim_recorder.bat        # NEW launcher, mirrors run_videotrim.bat

tests/test_ffmpeg_record_argv.py  # NEW
tests/test_dshow_parsing.py       # NEW
tests/test_settings_recorder.py   # NEW
tests/test_marker_export.py       # NEW
```

`pyproject.toml`'s `include = ["app*"]` fnmatch-globs dotted package names too, so `app.recorder.ui` is already covered — no `pyproject.toml` change needed. `QtMultimedia`/`QtMultimediaWidgets` (for `QCamera`, `QMediaDevices`, `QAudioSource`, `QVideoWidget`) ship inside the already-declared `PySide6>=6.6` dependency — no new dependency to add.

## Flagged risks (build-order accounts for these — validate each in isolation before depending on it)

1. **dshow name resolution for cameras/mics.** Resolved by making ffmpeg's own `-list_devices`/`-list_options` output the *sole* source of truth for dropdown contents and the exact capture argv — never Qt's `QMediaDevices` for that purpose. Qt is used only for the independent audio meter and the camera-preview picker, matched to the ffmpeg list by friendly-name string, degrading gracefully (not crashing) on a mismatch.
2. **HiDPI coordinate mismatch for screen capture.** `QScreen.geometry()` is *logical* (DPI-scaled) pixels; gdigrab's `-offset_x/-offset_y/-video_size` need *physical* pixels. Mitigation: multiply by `screen.devicePixelRatio()` in `devices.list_screens()`. Verify empirically on a scaled display — most likely place for a silent mis-cropped recording.
3. **Camera resolution mismatch** between Qt's `QCameraDevice.videoFormats()` and ffmpeg's `-list_options`. Mitigation: ffmpeg's list is authoritative for the actual `-video_size` used in recording; the same value is also fed into the Qt preview so the crop-rect scaling math never disagrees with what ffmpeg will actually capture.
4. **`SetWindowDisplayAffinity` must actually exclude the overlay from the recording** — this is the single point of failure for "not recorded." Validate standalone (a throwaway always-on-top test window, screen-record over it, confirm it's invisible) *before* building the rest of the floating overlay.
5. **Concurrent mic access**: Qt's `QAudioSource` (for the meter) and ffmpeg's `dshow` (for the actual recording) open the same mic at once during recording. WASAPI shared mode generally tolerates this; wrap the meter's `start()` in try/except and disable just the bar on failure, never the recording.

## Shared-code refactor (do first, low risk, unblocks the rest)

`app/core/ffmpeg_utils.py` (new): extract `_unique_path`/`_concat_segments` out of `exporter.py` verbatim as `unique_path()`/`concat_segments()` (raises a new `FfmpegRunError`). `exporter.py`'s private names become thin delegating wrappers (catch `FfmpegRunError`, re-raise `ExportError` to preserve current behavior exactly) — nothing else in `exporter.py` changes. This is what lets the recorder's pause/resume segment-stitching reuse the exact same concat logic instead of duplicating it. Verify by re-running an existing multi-split trim export after the change (the only path that currently exercises `_concat_segments`).

## Device enumeration — `app/recorder/core/devices.py`

```python
@dataclass(frozen=True)
class CameraDevice:
    name: str            # dshow friendly name, shown in dropdown
    capture_id: str      # exact video="..." string (alt "@device_pnp_..." path if ffmpeg reported one)

@dataclass(frozen=True)
class MicDevice:
    name: str
    capture_id: str

@dataclass(frozen=True)
class ScreenSource:
    qt_name: str
    label: str                                # "Screen 1 — 2560x1440 (Primary)"
    physical_rect: tuple[int, int, int, int]  # x, y, w, h — PHYSICAL pixels
    is_primary: bool

def list_dshow_devices(timeout: float = 8.0) -> tuple[list[CameraDevice], list[MicDevice]]
def list_dshow_video_options(capture_id: str, timeout: float = 8.0) -> list[tuple[int, int]]
def pick_camera_native_size(available: list[tuple[int, int]], cap=(1920, 1080)) -> tuple[int, int]
def list_screens() -> list[ScreenSource]
def _parse_dshow_device_list(stderr_text: str) -> tuple[list[CameraDevice], list[MicDevice]]   # pure, unit-testable
def _parse_dshow_video_options(stderr_text: str) -> list[tuple[int, int]]                        # pure, unit-testable
```

- `list_dshow_devices` runs `ffmpeg -hide_banner -list_devices true -f dshow -i dummy` (always exits non-zero; only stderr matters) via `find_ffmpeg()`. Parse with:
  ```python
  _SECTION_RE = re.compile(rb"DirectShow (video|audio) devices")
  _NAME_RE = re.compile(rb'\]\s+"(.+)"\r?$')
  _ALT_RE = re.compile(rb'Alternative name\s+"(.+)"\r?$')
  ```
  Track current section per line; a `_NAME_RE` match opens a device in that section; if the *next* line matches `_ALT_RE`, that becomes `capture_id`, else `capture_id` defaults to `name`.
- `list_dshow_video_options` runs `ffmpeg -f dshow -list_options true -i video="{capture_id}"`, parses `re.findall(rb"s=(\d+)x(\d+)", stderr)` into a deduped set.
- `list_screens()` uses `QGuiApplication.screens()`, converting `geometry()` to physical pixels via `devicePixelRatio()` (risk #2).
- Fail-soft: if parsing ever produces nothing (format drift, localization), return `([], [])` rather than raising — setup window shows "No cameras/microphones detected" and still allows screen-only, no-audio recording.

## ffmpeg command building + subprocess lifecycle — `app/recorder/core/ffmpeg_record.py`

```python
@dataclass(frozen=True)
class ScreenCaptureConfig:
    capture_rect: tuple[int, int, int, int]   # absolute PHYSICAL virtual-desktop coords
    fps: int = 30

@dataclass(frozen=True)
class CameraCaptureConfig:
    capture_id: str
    native_size: tuple[int, int]
    crop_rect: tuple[int, int, int, int] | None   # within native_size; None = full frame
    fps: int = 30

@dataclass(frozen=True)
class AudioCaptureConfig:
    capture_id: str | None
    gain: float = 1.0        # -> -af volume=<gain>

@dataclass(frozen=True)
class RecordConfig:
    source: ScreenCaptureConfig | CameraCaptureConfig
    audio: AudioCaptureConfig
    output_path: Path

def build_ffmpeg_argv(config: RecordConfig) -> list[str]
```

Screen mode captures the sub-rectangle *natively* via gdigrab's own `offset_x/offset_y/video_size` (region picker already folds monitor-origin + sub-rect into one absolute `capture_rect`) — no crop filter needed. Camera mode can't crop natively in dshow, so `-vf crop=w:h:x:y` is applied post-decode.

Exact argv shapes:

Screen, mic present, gain 1.5x:
```
ffmpeg -y -f gdigrab -framerate 30 -offset_x 0 -offset_y 0 -video_size 1920x1080 -i desktop
  -f dshow -i audio="Microphone (Realtek High Definition Audio)"
  -map 0:v -map 1:a -af volume=1.500
  -c:v libopenh264 -b:v 20M -c:a aac -movflags +faststart segment_001.mp4
```
Camera, mic present (single combined dshow `-i` for A/V sync), crop applied:
```
ffmpeg -y -rtbufsize 150M -f dshow -video_size 1280x720 -framerate 30
  -i video="Logitech BRIO":audio="Microphone (Realtek High Definition Audio)"
  -vf crop=800:600:100:50
  -c:v libopenh264 -b:v 20M -c:a aac -movflags +faststart segment_001.mp4
```
Camera, no mic:
```
ffmpeg -y -rtbufsize 150M -f dshow -video_size 1280x720 -framerate 30 -i video="Logitech BRIO"
  -c:v libopenh264 -b:v 20M -an -movflags +faststart segment_001.mp4
```
Note when implementing: camera+mic uses **one** combined `-i 'video="X":audio="Y"'`, not two `-i` blocks — build the video-part/audio-part strings first and join with `:` only in the camera case; screen+mic stays two separate `-i` inputs with `-map`.

Video encode always `-c:v libopenh264 -b:v 20M` (same LGPL-only encoder `exporter.py` already uses — this ffmpeg build has no libx264) and `-c:a aac` / `-an` depending on whether audio is present.

**Process lifecycle** — `Popen()` itself is non-blocking, no thread needed for start/resume. The graceful-stop handshake (write `b"q"` to stdin, wait, escalate to terminate/kill) can block for seconds, so it runs on a short-lived one-shot `QThread` per pause/stop click — same "worker per operation" idiom as the existing `ExportWorker`/`WaveformWorker`, not a persistent queue thread.

```python
class RecordingSession:
    def __init__(self, config: RecordConfig, work_dir: Path) -> None
    def start_segment(self) -> Path                                  # Popen(), non-blocking, new segment_NNN.mp4 in work_dir
    def stop_segment_blocking(self, timeout: float = 5.0) -> None    # stdin b"q" -> wait(timeout) -> terminate() -> wait(2) -> kill()
    def poll_alive(self) -> bool                                     # proc.poll() is None
    def finalize(self, output_path: Path) -> Path                    # 1 segment: move(); N segments: ffmpeg_utils.concat_segments() + cleanup

class PauseStopWorker(QThread):       # one-shot: runs stop_segment_blocking()/finalize() off the UI thread
    done = Signal(); finalized = Signal(Path); error = Signal(str)

class RecordingController(QObject):    # UI-facing: start/pause/resume/stop, elapsed timer, crash detection via poll_alive()
    state_changed = Signal(str)        # "recording" | "paused"
    elapsed_changed = Signal(float)
    finished = Signal(Path)
    failed = Signal(str)
    elapsed_seconds: float             # plain attribute, updated on every elapsed_changed tick — lets a
                                        # marker-break button press read "how far in are we" synchronously
                                        # without waiting on the next signal emission
    def start(self, config, work_dir, final_output) -> None
    def pause(self) -> None            # spawns PauseStopWorker(output_path=None)
    def resume(self) -> None           # session.start_segment() directly
    def stop(self) -> None             # spawns PauseStopWorker(output_path=final_output)
```

Each `pause()` cleanly ends one segment (proper trailer via graceful stop, not a hard kill); `resume()`/`start()` begin a new one; `stop()` concatenates all segments via the shared `concat_segments()`. Expect a sub-second gap at each pause point — accepted limitation, not a bug. `work_dir = Path(tempfile.mkdtemp(prefix="videotrimrecorder_"))`, segments cleaned up individually in `finalize()`; the empty temp dir itself is left behind (acceptable for v1).

`elapsed_seconds` only accumulates while a segment is actively recording (a per-tick `QTimer` adds the tick interval to it, paused while no segment is running) — since concatenation stitches active segments back-to-back with no gap inserted for paused wall-clock time, this cumulative value *is* already the correct timestamp basis in the final concatenated file. A marker-break press at any point (recording or paused — the button stays enabled through a pause, see below) can just read `controller.elapsed_seconds` directly with no further adjustment.

## Live audio level meter — `app/recorder/core/audio_meter.py`

One long-lived `AudioLevelMeter` QObject, created once in `RecorderMainWindow.__init__` and passed by reference into `FloatingOverlay` — both level bars subscribe to the same signal, only one `QAudioSource` is ever open.

```python
class AudioLevelMeter(QObject):
    level_changed = Signal(float)   # 0.0-1.0 RMS, already gain-scaled
    error = Signal(str)
    def start(self, device: QAudioDevice) -> None   # QAudioSource + 50ms QTimer polling the pull-mode QIODevice; try/except, emits error rather than raising
    def stop(self) -> None
    def set_gain(self, gain: float) -> None
    def _poll(self) -> None   # readAll() -> np.frombuffer(int16)/32768 -> rms -> emit min(1.0, rms*gain*_METER_SCALE)
```
`_METER_SCALE` (~4.0) is an empirical constant tuned by ear/eye during manual testing — typical speech RMS sits well under full scale. Owner re-`start()`s on mic-dropdown change (including the pre-seeded startup device), `stop()`s for "None (no audio)". Runs unmodified through setup → countdown → floating-overlay → pause/resume (only the *recording* pauses, not the monitor).

## Region-selection overlay — `app/recorder/ui/region_picker.py`

Shared base class (same shared-base/subclass convention as `TimelineWidget`+`SeekBar`/`WaveformBar`):

```python
class _RegionPickerBase(QDialog):
    def __init__(self, content_widget: QWidget, native_size: tuple[int, int], parent=None) -> None
    def picked_rect(self) -> tuple[int, int, int, int] | None   # None = whole frame; else native_size-space (x,y,w,h)
```
QRubberBand drag-to-select over `content_widget`, base class scales dragged widget-local coords into `native_size` space.

- `ScreenRegionPicker(screen_source, qscreen)`: content is a static `qscreen.grabWindow(0)` screenshot (fine — the screen isn't moving during selection). `picked_absolute_rect()` translates `picked_rect()` by the monitor's own origin (or returns the whole `physical_rect` if never dragged) — this is exactly `ScreenCaptureConfig.capture_rect`, no further combining needed by the caller.
- `CameraRegionPicker(qt_camera_device, native_size)`: opens a live **Qt-native** `QCamera`+`QMediaCaptureSession`+`QVideoWidget` preview (from `PySide6.QtMultimedia`/`PySide6.QtMultimediaWidgets`) purely so the user has a live feed to drag over — explicitly `.stop()`s the camera in `done()` before the dialog closes, so it's never open at the same time as ffmpeg's dshow capture (unlike the mic meter, which genuinely does run concurrently). Match `QCameraDevice` to the selected `devices.CameraDevice` by `description() == name` (best-effort; on no match, skip live preview and fall back to whole-frame/no-crop rather than crashing).
- Sequencing: before opening `CameraRegionPicker`, resolve the authoritative resolution via `list_dshow_video_options`/`pick_camera_native_size` and pass that same value into both the picker (correct crop scaling) and later `CameraCaptureConfig.native_size` (actual `-video_size`), so Qt and ffmpeg never disagree (risk #3).

## Countdown overlay — `app/recorder/ui/countdown_overlay.py`

Frameless/translucent/always-on-top (`FramelessWindowHint | WindowStaysOnTopHint | Tool`, `WA_TranslucentBackground`), positioned over `capture_rect` for screen mode (presenter can see what's about to record) or centered on the primary screen for camera mode.
```python
class CountdownOverlay(QWidget):
    countdown_finished = Signal()
    def start(self, seconds: int = 3) -> None   # shows at 3, QTimer 1s ticks down, hides + emits at 0
```
Sequencing: Start button → `SaveLocationDialog` → `CountdownOverlay.start(3)` (setup window still visible during countdown) → on `countdown_finished`: hide setup window, show `FloatingOverlay`, call `RecordingController.start(...)` — all at once, so the overlay's green dot lights up exactly when ffmpeg actually starts.

## Floating overlay — `app/recorder/ui/floating_overlay.py`

```python
class FloatingOverlay(QWidget):
    record_pause_clicked = Signal()   # toggles; caller decides pause vs resume from current state
    marker_break_clicked = Signal()   # "mark a trim point here" — enabled through both recording and paused states
    stop_clicked = Signal()
    def __init__(self, audio_meter: AudioLevelMeter, parent=None) -> None
    def showEvent(self, event) -> None   # calls _apply_capture_exclusion()
    def set_recording_state(self, state: str) -> None   # "recording": solid green dot + "Pause"; "paused": hollow dot + "Resume"
    def set_elapsed(self, seconds: float) -> None
```
Layout: green dot, elapsed mm:ss, Pause/Resume button, **Marker Break button**, Stop button, `AudioLevelBar` (subscribed to the shared `audio_meter.level_changed`). Frameless/translucent/always-on-top, standard mouse-drag-to-move. The Marker Break button stays enabled in both the "recording" and "paused" states (only the setup/countdown phase, before the overlay exists, has nothing to mark).

`_apply_capture_exclusion()`:
```python
if sys.platform == "win32":
    import ctypes
    WDA_EXCLUDEFROMCAPTURE = 0x00000011
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), WDA_EXCLUDEFROMCAPTURE)
    except OSError:
        pass  # pre-2004 Windows — best-effort, not fatal
```
Applied once in `showEvent` — sticks to the HWND through subsequent drag-repositioning, no need to reapply.

Wiring: `record_pause_clicked` → `controller.pause()`/`.resume()` per current state; `marker_break_clicked` → `RecorderMainWindow._on_marker_break_clicked` (appends `controller.elapsed_seconds` to a session-local list — see next section); `stop_clicked` → `controller.stop()`; `controller.state_changed`/`elapsed_changed` → overlay setters; `controller.finished` → hide overlay, re-show setup window, write the marker sidecar, confirmation dialog with output path (mirror `MainWindow`'s export-finished dialog style); `controller.failed` → hide overlay, re-show setup window, `QMessageBox.warning`.

## Marker-break → VideoTrim sidecar JSON — `app/recorder/core/marker_export.py`

This is what makes the floating overlay's Marker Break button actually useful: every press becomes a classifiable candidate span in the exact JSON file VideoTrim already auto-loads when the recorded video is later opened there (`MainWindow._open_file` → `EditProject.sidecar_path_for(path)` → `EditProject.load(sidecar)`, see `app/ui/main_window.py`). No new file format, no changes needed on VideoTrim's read side — this reuses `app.core.edit_model.EditProject` directly (it's a plain-dataclasses-plus-`json` module with no Qt dependency, already safe to import from the recorder).

**Shared constant relocation:** move `CANDIDATE_BREAK_WINDOW_SECONDS = 30.0` out of `app/ui/marker_panel.py` (where it's currently a private module constant backing that panel's own "+ Marker Break" button) into `app/core/edit_model.py` as a public module-level constant; update `marker_panel.py` to `from app.core.edit_model import CANDIDATE_BREAK_WINDOW_SECONDS` instead of defining it locally. This way "what one Marker Break press means" (a 30-second candidate span) has exactly one definition, shared by the live-recording button and the existing post-hoc editing button, and future changes to that window size only need to happen once.

**`app/core/ffmpeg_utils.py` addition** — first real caller of the already-defined-but-unused `find_ffprobe()`:
```python
def probe_duration(path: Path) -> float:
    """ffprobe -v error -show_entries format=duration -of csv=p=0 <path>,
    parsed as float. Used to clip the last marker-break span to the actual
    recorded duration, the same way marker_panel.py's _add_candidate_break
    clips to get_duration()."""
```

**`app/recorder/core/marker_export.py`:**
```python
from app.core.edit_model import EditProject, CANDIDATE_BREAK_WINDOW_SECONDS

def write_marker_sidecar(output_path: Path, marker_breaks: list[float], duration: float | None) -> None:
    """No-op if marker_breaks is empty — a recording with no button presses
    gets no sidecar, exactly like opening any video with no prior edits
    today. Otherwise builds one CANDIDATE span per press via the same
    add_color_candidates() path the trim app's own Marker Break button uses
    (source='color' — deliberately indistinguishable from a real detected
    marker, matching that existing button's own behavior/semantics), clipped
    to `duration` if known, and saves to EditProject.sidecar_path_for(output_path)."""
    if not marker_breaks:
        return
    project = EditProject(video_path=str(output_path))
    spans = [
        (t, min(t + CANDIDATE_BREAK_WINDOW_SECONDS, duration) if duration else t + CANDIDATE_BREAK_WINDOW_SECONDS)
        for t in marker_breaks
    ]
    project.add_color_candidates(spans)
    project.save(EditProject.sidecar_path_for(output_path))
```

**Wiring in `RecorderMainWindow`:**
- `self._marker_breaks: list[float] = []`, reset at the start of each new recording (in `_on_start_clicked`, alongside building `RecordConfig`).
- `overlay.marker_break_clicked` → `self._marker_breaks.append(self._controller.elapsed_seconds)` (optionally flash the overlay/label briefly for feedback — no separate signal needed for that, just a local `QTimer.singleShot` style blink in the overlay itself).
- `controller.finished(output_path)` handler: `duration = ffmpeg_utils.probe_duration(output_path)` then `marker_export.write_marker_sidecar(output_path, self._marker_breaks, duration)`, before showing the confirmation dialog.

This is pure, unit-testable without touching ffmpeg or hardware: `write_marker_sidecar` takes `duration` as a plain argument, so a test can call it directly with canned timestamps and assert the written JSON round-trips through `EditProject.load()` into the expected CANDIDATE markers.

## Save-location dialog — `app/recorder/ui/save_location_dialog.py`

`app/core/settings.py` additions (same `QSettings(_ORG, _APP)` get/set pattern, new `record/` key namespace):
```python
_KEY_RECORD_LAST_DIR = "record/last_output_dir"
_KEY_RECORD_MRU_DIRS = "record/mru_output_dirs"    # JSON list[str], most-recent-first, max 5
_KEY_RECORD_LAST_MIC = "record/last_mic_device"
_KEY_RECORD_LAST_SOURCE = "record/last_source"      # e.g. "camera:Logitech BRIO" or "screen:\\.\DISPLAY1"
_KEY_RECORD_GAIN = "record/gain"                    # default 1.0

def get_record_output_dirs() -> list[Path]
def add_record_output_dir(path: str | Path) -> None   # de-dupe, insert front, trim to 5, also sets last_dir
def get_last_record_output_dir() -> Path | None
def get_last_mic_device() -> str | None
def set_last_mic_device(name: str) -> None
def get_record_gain() -> float
def set_record_gain(gain: float) -> None
def get_last_record_source() -> str | None
def set_last_record_source(source_key: str) -> None
```

```python
class SaveLocationDialog(QDialog):
    """Shown every time, right before a recording starts. One button per
    get_record_output_dirs() entry (most-recent-first), plus 'Other…'
    (QFileDialog.getExistingDirectory) and Cancel."""
    def chosen_dir(self) -> Path | None
```
Caller (`RecorderMainWindow._on_start_clicked`):
```python
dialog = SaveLocationDialog(self)
if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.chosen_dir():
    return
output_dir = dialog.chosen_dir()
app_settings.add_record_output_dir(output_dir)
output_path = ffmpeg_utils.unique_path(output_dir / f"recording_{datetime.now():%Y%m%d_%H%M%S}.mp4")
```

## Setup window — `app/recorder/ui/setup_window.py`

```python
class RecorderMainWindow(QMainWindow):
    def __init__(self) -> None:
        self._audio_meter = AudioLevelMeter(self)   # created once, lives whole session
        self._controller = RecordingController(self)
        self._floating_overlay: FloatingOverlay | None = None
        self._selected_region: tuple[int,int,int,int] | None = None
        self._build_ui()       # source_combo -> Select-Area button/label -> mic_combo -> gain_slider -> AudioLevelBar -> Start button (exact requirement order)
        self._wire_signals()
        self._populate_devices()
```
- `_populate_devices()`: `source_combo` = `list_screens()` entries + camera half of `list_dshow_devices()`, pre-selected from `get_last_record_source()` else index 0; `mic_combo` = "None (no audio)" + mics, pre-selected from `get_last_mic_device()` else first real mic; `gain_slider` from `get_record_gain()`.
- `mic_combo.currentIndexChanged` → resolve `QAudioDevice` via `QMediaDevices.audioInputs()` matched on `.description() == mic.name` → `audio_meter.start()`/`.stop()`.
- `gain_slider.valueChanged` → `audio_meter.set_gain(value/100.0)`.
- `select_area_button.clicked` → branch on source kind → `ScreenRegionPicker`/`CameraRegionPicker` → store into `self._selected_region`, update label.
- `start_button.clicked` (`_on_start_clicked`): `SaveLocationDialog` → persist settings (`add_record_output_dir`, `set_last_mic_device`, `set_record_gain`, `set_last_record_source`) → build `RecordConfig` → `CountdownOverlay.start(3)` → on finished: `self.hide()`, create/show `FloatingOverlay(self._audio_meter)` (top-right of primary screen), `controller.start(config, work_dir, output_path)`.
- `controller.finished`/`failed` → restore setup window, write the marker sidecar via `marker_export.write_marker_sidecar` on success (see "Marker-break → VideoTrim sidecar JSON" above), show result/error (mirror `MainWindow`'s export-finished dialog pattern).

`app/recorder/main.py` mirrors `app/main.py` exactly (`QApplication`, build `RecorderMainWindow`, `.show()`, `app.exec()`), run via `python -m app.recorder.main`. `run_videotrim_recorder.bat` mirrors `run_videotrim.bat` (same conda python path) targeting that module.

## Testability

**Unit-testable now** (pure logic, no hardware/event loop):
- `build_ffmpeg_argv()` — exact argv for screen/camera × with/without mic × with/without crop × with/without gain → `tests/test_ffmpeg_record_argv.py`
- `_parse_dshow_device_list()` / `_parse_dshow_video_options()` — canned stderr fixture text (alt-name lines, empty output, duplicates) → `tests/test_dshow_parsing.py`
- `add_record_output_dir()` MRU ordering/de-dupe/truncate-to-5 and the other new `settings.py` getters/setters → `tests/test_settings_recorder.py`, pointing `QSettings` at a scratch `IniFormat` file so tests don't touch the real registry hive. (Note: nothing in this repo is currently pytest-collected — `tests/test_color_scanner*.py` are standalone scripts — this would be the first real pytest-style suite.)
- `write_marker_sidecar()` — pass canned timestamps + a fixed `duration` (no ffprobe/hardware involved), assert the written sidecar round-trips through `EditProject.load()` into the expected CANDIDATE spans (including last-span clipping to `duration`) and that an empty `marker_breaks` list writes nothing → `tests/test_marker_export.py`.

**Manual-only** (needs real hardware/OS compositor): actual device-list parsing against this machine's real devices; gdigrab/dshow capture correctness; `SetWindowDisplayAffinity` actually excluding the overlay from a real recording; concurrent mic access (meter + ffmpeg); HiDPI coordinate correctness; pause/resume gap and end-to-end concat; countdown/drag visual polish.

## Build order

1. `app/core/ffmpeg_utils.py` extraction (`unique_path`/`concat_segments`) + `probe_duration()` + `exporter.py` delegation — smoke-test existing trim app's multi-split export still works.
2. `app/core/settings.py` additions + `tests/test_settings_recorder.py` (fast, pure, lock in early).
3. `app/core/edit_model.py`: promote `CANDIDATE_BREAK_WINDOW_SECONDS` to a module constant, update `app/ui/marker_panel.py`'s import — smoke-test the trim app's existing "+ Marker Break" button still works unchanged. Then `app/recorder/core/marker_export.py` + `tests/test_marker_export.py` (pure, no hardware, quick to lock in).
4. `app/recorder/core/devices.py` — write & unit-test the two `_parse_*` functions against hand-written fixtures first, then wire real `ffmpeg -list_devices`/`-list_options` calls and manually verify against this machine's actual webcam/mic. Highest-uncertainty module — validate before anything depends on it.
5. `app/recorder/core/ffmpeg_record.py` — `build_ffmpeg_argv` unit tests first (no hardware); then, before any UI exists, drive `RecordingSession` from a throwaway scratch script against a real camera and real screen: confirm playback, confirm graceful `q`-stdin stop produces a clean file (not corrupt from a hard kill), confirm pause→resume→stop concat produces one valid file, and empirically verify the HiDPI physical-pixel math (risk #2) with a known test region.
6. `app/recorder/core/audio_meter.py` — manual verification against real speech, tune `_METER_SCALE`.
7. `app/recorder/ui/level_bar.py`, `countdown_overlay.py` — simple, quick to build and eyeball.
8. `app/recorder/ui/region_picker.py` — screen variant first (static screenshot, simpler), camera variant second (`QCamera` preview plumbing, verify native_size agreement, risk #3).
9. `app/recorder/ui/floating_overlay.py` — **validate `SetWindowDisplayAffinity` standalone first** (throwaway always-on-top test window, screen-record over it, confirm invisible in output) before building the rest of the overlay UI. Highest-consequence unknown in the whole feature (risk #4). Include the Marker Break button in this step.
10. `app/recorder/ui/save_location_dialog.py` — straightforward.
11. `app/recorder/ui/setup_window.py` + `app/recorder/main.py` + `run_videotrim_recorder.bat` — wire everything together last, once each piece is independently verified.
12. End-to-end manual pass: record → pause → resume → stop → concat → playback, for both screen and camera sources, with a sub-region selected in each, mic on/off, at least two gain settings, and (if available) a non-primary-monitor screen source to confirm negative-offset multi-monitor math. Also: press Marker Break a few times (including once while paused) during one of these recordings, then open the resulting file in VideoTrim itself and confirm the sidecar JSON loaded automatically with the expected candidate spans at the right timestamps, classifiable exactly like a color-detected marker.

## Critical files

- `app/recorder/core/devices.py` — device enumeration, highest-risk module
- `app/recorder/core/ffmpeg_record.py` — argv building + recording lifecycle
- `app/recorder/core/marker_export.py` — writes the VideoTrim-compatible sidecar from marker-break presses
- `app/recorder/ui/floating_overlay.py` — capture-exclusion, highest-consequence unknown
- `app/core/ffmpeg_utils.py` — shared extraction from `exporter.py`
- `app/core/settings.py` — new `record/*` keys
- `app/recorder/ui/setup_window.py` — ties everything together
