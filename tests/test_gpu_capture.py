from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core import video_encoder
from app.recorder.core import ffmpeg_record, gpu_capture
from app.recorder.core.ffmpeg_record import (
    AudioCaptureConfig, CameraCaptureConfig, RecordConfig, RecordingController, RecordingSession, ScreenCaptureConfig, build_ffmpeg_argv,
    uses_graphics_chip,
)
from app.recorder.core.gpu_capture import Colors, DxgiOutput, GpuCapture

_app = QApplication.instance() or QApplication([])

DISPLAY = r"\\.\DISPLAY1"
FULL = DxgiOutput(0, DISPLAY, (0, 0, 2880, 1920))
BT601_TV = Colors("smpte170m", "tv")


class DxgiTests(unittest.TestCase):
    def test_a_guid_is_laid_out_the_way_com_wants_it(self) -> None:
        self.assertEqual(bytes(gpu_capture._guid("770aae78-f26f-4dba-a829-253c83d1b387")), bytes.fromhex("78ae0a776ff2ba4da829253c83d1b387"))

    def test_there_are_no_outputs_off_windows(self) -> None:
        with patch.object(gpu_capture.sys, "platform", "linux"):
            self.assertEqual(gpu_capture.dxgi_outputs(), [])

    def test_whatever_goes_wrong_asking_dxgi_means_no_outputs_not_an_error(self) -> None:
        with patch.object(gpu_capture.sys, "platform", "win32"), patch.object(gpu_capture, "_read_dxgi_outputs", side_effect=OSError("no dxgi")):
            self.assertEqual(gpu_capture.dxgi_outputs(), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_this_machines_displays_are_found_with_windows_names_and_real_sizes(self) -> None:
        outputs = gpu_capture.dxgi_outputs()
        if not outputs:
            self.skipTest("no graphics adapter with displays here (a headless machine)")
        for position, output in enumerate(outputs):
            self.assertEqual(output.index, position)
            self.assertTrue(output.device_name.startswith("\\\\.\\DISPLAY"), output.device_name)
            self.assertGreater(output.rect[2], 0)
            self.assertGreater(output.rect[3], 0)


def _planes(matrix: str, limited: bool, noise: int = 0, seed: int = 1) -> tuple[tuple[bytes, bytes, bytes], int]:
    """The YUV 4:2:0 planes a chip using *matrix* and range would make of the test colours (with a little lossy-encode noise)."""
    block, count = gpu_capture._BLOCK, len(gpu_capture._TEST_COLORS)
    width, height = block * count, block
    y_plane, u_plane, v_plane = bytearray(width * height), bytearray(width * height // 4), bytearray(width * height // 4)
    rng = random.Random(seed)
    jitter = lambda: rng.randint(-noise, noise) if noise else 0
    for index, color in enumerate(gpu_capture._TEST_COLORS):
        y, u, v = gpu_capture._expected(color, matrix, limited)
        for row in range(height):
            for column in range(index * block, (index + 1) * block):
                y_plane[row * width + column] = max(0, min(255, round(y + jitter())))
        for row in range(height // 2):
            for column in range(index * block // 2, (index + 1) * block // 2):
                u_plane[row * (width // 2) + column] = max(0, min(255, round(u + jitter())))
                v_plane[row * (width // 2) + column] = max(0, min(255, round(v + jitter())))
    return (bytes(y_plane), bytes(u_plane), bytes(v_plane)), width


class ColourCalibrationTests(unittest.TestCase):
    def test_every_matrix_and_range_is_told_apart(self) -> None:
        for matrix in ("bt709", "smpte170m"):
            for limited in (True, False):
                planes, width = _planes(matrix, limited)
                self.assertEqual(gpu_capture._best_fit(planes, width), Colors(matrix, "tv" if limited else "pc"), (matrix, limited))

    def test_the_noise_of_a_lossy_encode_does_not_fool_it(self) -> None:
        for seed in range(5):
            planes, width = _planes("smpte170m", True, noise=3, seed=seed)
            self.assertEqual(gpu_capture._best_fit(planes, width), BT601_TV, seed)

    def test_a_result_that_fits_no_standard_is_refused(self) -> None:
        width = gpu_capture._BLOCK * len(gpu_capture._TEST_COLORS)
        flat = (bytes([128]) * (width * gpu_capture._BLOCK), bytes([128]) * (width * gpu_capture._BLOCK // 4), bytes([128]) * (width * gpu_capture._BLOCK // 4))
        self.assertIsNone(gpu_capture._best_fit(flat, width))
        rng = random.Random(7)
        noisy = tuple(bytes(rng.randrange(256) for _ in range(len(plane))) for plane in flat)
        self.assertIsNone(gpu_capture._best_fit(noisy, width))

    def test_the_matrix_it_finds_becomes_the_frames_label(self) -> None:
        self.assertEqual(BT601_TV.filter, "setparams=range=tv:colorspace=smpte170m")
        self.assertEqual(Colors("bt709", "pc").filter, "setparams=range=pc:colorspace=bt709")

    def test_the_test_pattern_has_both_ends_of_the_range_and_saturated_colours(self) -> None:
        colors = gpu_capture._TEST_COLORS
        self.assertIn((255, 255, 255), colors)
        self.assertIn((0, 0, 0), colors)
        coloured = [color for color in colors if color not in ((255, 255, 255), (0, 0, 0))]
        self.assertGreaterEqual(len(coloured), 6)  # the primaries and secondaries: what tells one matrix from another
        self.assertTrue(all(min(color) == 0 and max(color) == 255 for color in coloured))


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hardware = video_encoder._MF_QUALITY
        self.outputs = [FULL]
        self.colors: Colors | None = BT601_TV
        self.probed = 0
        for patcher in (patch.object(gpu_capture.sys, "platform", "win32"), patch.object(gpu_capture, "find_ffmpeg", return_value="ffmpeg"),
                        patch.object(video_encoder, "available_encoders", return_value=frozenset({"libx264", "h264_mf"})),
                        patch.object(video_encoder, "hardware_encoder", side_effect=lambda encoders, allow_filters=True: self.hardware),
                        patch.object(gpu_capture, "_outputs", side_effect=lambda: self.outputs),
                        patch.object(gpu_capture, "_known_output_and_colors", side_effect=self._probe)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _probe(self, ffmpeg, hardware, output) -> Colors | None:
        self.probed += 1
        return self.colors

    def source(self, rect=(0, 0, 2880, 1920), **options) -> ScreenCaptureConfig:
        return ScreenCaptureConfig(rect, screen_name=options.pop("screen_name", DISPLAY), screen_rect=options.pop("screen_rect", (0, 0, 2880, 1920)), **options)

    def test_the_whole_screen_is_captured_on_the_chip_and_stays_there_until_it_is_encoded(self) -> None:
        plan = gpu_capture.plan_gpu_capture(self.source())
        self.assertEqual(plan.input_args, ["-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=30:draw_mouse=1"])
        self.assertEqual(plan.filter, "setparams=range=tv:colorspace=smpte170m")
        self.assertEqual(plan.encoder_args[plan.encoder_args.index("-c:v") + 1], "h264_mf")
        self.assertNotIn("-pix_fmt", plan.encoder_args)  # a format would pull the frames back to memory

    def test_an_area_is_an_offset_and_a_size_inside_the_display(self) -> None:
        plan = gpu_capture.plan_gpu_capture(self.source((400, 300, 1280, 720)))
        self.assertEqual(plan.input_args[-1], "ddagrab=output_idx=0:framerate=30:draw_mouse=1:video_size=1280x720:offset_x=400:offset_y=300")

    def test_on_a_second_display_only_the_position_within_it_counts(self) -> None:
        self.outputs = [FULL, DxgiOutput(1, r"\\.\DISPLAY2", (2880, 0, 1920, 1080))]
        plan = gpu_capture.plan_gpu_capture(self.source((2880 + 100, 50, 800, 600), screen_name=r"\\.\DISPLAY2", screen_rect=(2880, 0, 1920, 1080)))
        self.assertEqual(plan.input_args[-1], "ddagrab=output_idx=1:framerate=30:draw_mouse=1:video_size=800x600:offset_x=100:offset_y=50")

    def test_the_second_display_whole_needs_no_offset(self) -> None:
        self.outputs = [FULL, DxgiOutput(1, r"\\.\DISPLAY2", (2880, 0, 1920, 1080))]
        plan = gpu_capture.plan_gpu_capture(self.source((2880, 0, 1920, 1080), screen_name=r"\\.\DISPLAY2", screen_rect=(2880, 0, 1920, 1080)))
        self.assertEqual(plan.input_args[-1], "ddagrab=output_idx=1:framerate=30:draw_mouse=1")

    def test_the_cursor_option_and_the_frame_rate_are_passed_on(self) -> None:
        plan = gpu_capture.plan_gpu_capture(self.source(draw_cursor=False, fps=15))
        self.assertEqual(plan.input_args[-1], "ddagrab=output_idx=0:framerate=15:draw_mouse=0")

    def test_the_frame_size_sizes_the_encoders_bitrate_fallback(self) -> None:
        self.hardware = video_encoder._MF_BITRATE
        plan = gpu_capture.plan_gpu_capture(self.source((0, 0, 1920, 1080), screen_rect=(0, 0, 2880, 1920)))
        self.assertEqual(plan.encoder_args[plan.encoder_args.index("-b:v") + 1], "6M")

    def test_each_reason_to_use_the_ordinary_capture_instead(self) -> None:
        cases = {
            "no screen name": lambda: self.source(screen_name=None),
            "no screen rectangle": lambda: self.source(screen_rect=None),
            "wider than the encoders take": lambda: self.source((0, 0, 5120, 2880), screen_rect=(0, 0, 5120, 2880)),
            "a display the chip doesn't have": lambda: self.source(screen_name=r"\\.\DISPLAY9"),
            "an area past the display's edge": lambda: self.source((2000, 1500, 1280, 720)),
            "an area before the display's edge": lambda: self.source((-10, 0, 800, 600)),
        }
        for reason, make in cases.items():
            self.assertIsNone(gpu_capture.plan_gpu_capture(make()), reason)

    def test_no_graphics_chip_encoder_means_the_ordinary_capture(self) -> None:
        # Copying frames back to memory for x264 costs more than gdigrab does, so without a chip encoder there is nothing to gain.
        self.hardware = None
        self.assertIsNone(gpu_capture.plan_gpu_capture(self.source()))
        self.assertEqual(self.probed, 0)

    def test_a_capture_that_fails_its_check_means_the_ordinary_capture(self) -> None:
        self.colors = None
        self.assertIsNone(gpu_capture.plan_gpu_capture(self.source()))

    def test_it_is_only_for_windows(self) -> None:
        with patch.object(gpu_capture.sys, "platform", "darwin"):
            self.assertIsNone(gpu_capture.plan_gpu_capture(self.source()))

    def test_no_ffmpeg_means_the_ordinary_capture(self) -> None:
        with patch.object(gpu_capture, "find_ffmpeg", side_effect=FileNotFoundError("none")):
            self.assertIsNone(gpu_capture.plan_gpu_capture(self.source()))

    def test_planning_never_raises(self) -> None:
        with patch.object(gpu_capture, "_outputs", side_effect=RuntimeError("driver crashed")):
            self.assertIsNone(gpu_capture.plan_gpu_capture(self.source()))

    def test_a_display_that_cant_be_asked_about_is_not_probed_again_and_again(self) -> None:
        gpu_capture.plan_gpu_capture(self.source())
        gpu_capture.plan_gpu_capture(self.source((100, 100, 800, 600)))
        self.assertEqual(self.probed, 2)  # asked per plan; the answers are remembered by _known_output_and_colors, tested below


class CaptureCheckTests(unittest.TestCase):
    hardware = video_encoder._MF_QUALITY
    grabbed = b"  Stream #0:0: Video: wrapped_avframe, d3d11, 2880x1920 [SAR 1:1 DAR 3:2], 30 fps, 30 tbr, 1000k tbn\n"

    def check(self, answers):
        with patch.object(gpu_capture, "_run", side_effect=answers) as run:
            return gpu_capture._capture_works("ffmpeg", self.hardware, FULL), run

    def done(self, code: int = 0, text: bytes = b""):
        return SimpleNamespace(returncode=code, stderr=text)

    def test_it_works_when_the_frames_are_the_displays_size_and_the_encoder_takes_them(self) -> None:
        works, run = self.check([self.done(0, self.grabbed), self.done(0)])
        self.assertIs(works, True)
        grab, encode = (call.args[1] for call in run.call_args_list)
        self.assertIn("ddagrab=output_idx=0:framerate=30:draw_mouse=0", grab)
        self.assertIn("ddagrab=output_idx=0:framerate=30:draw_mouse=0:video_size=320x240:offset_x=0:offset_y=0", encode)
        self.assertEqual(encode[encode.index("-c:v") + 1], "h264_mf")
        self.assertNotIn("-pix_fmt", encode)

    def test_frames_of_another_size_mean_ffmpegs_display_is_not_ours(self) -> None:
        # e.g. a display ffmpeg numbers differently, or a scale Windows applies to one program and not the other
        works, run = self.check([self.done(0, b"Video: wrapped_avframe, d3d11, 1440x960 [SAR 1:1]")])
        self.assertIs(works, False)
        self.assertEqual(run.call_count, 1)  # the encoder is not even tried

    def test_it_does_not_work_if_the_grab_fails_or_says_nothing_about_a_size(self) -> None:
        self.assertIs(self.check([self.done(1, self.grabbed)])[0], False)
        self.assertIs(self.check([self.done(0, b"no stream line here")])[0], False)

    def test_it_does_not_work_if_the_encoder_refuses_the_frames(self) -> None:
        self.assertIs(self.check([self.done(0, self.grabbed), self.done(1)])[0], False)

    def test_it_could_not_be_asked_if_ffmpeg_will_not_run(self) -> None:
        self.assertIsNone(self.check([None])[0])
        self.assertIsNone(self.check([self.done(0, self.grabbed), None])[0])


class RememberedAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        gpu_capture._probe_results.clear()
        self.addCleanup(gpu_capture._probe_results.clear)

    def ask(self, hardware=video_encoder._MF_QUALITY, output=FULL, ffmpeg="ffmpeg"):
        return gpu_capture._known_output_and_colors(ffmpeg, hardware, output)

    def test_a_working_display_is_checked_and_measured_once(self) -> None:
        with patch.object(gpu_capture, "_capture_works", return_value=True) as works, patch.object(gpu_capture, "_measure_colors", return_value=BT601_TV) as measure:
            self.assertEqual(self.ask(), BT601_TV)
            self.assertEqual(self.ask(), BT601_TV)
        self.assertEqual((works.call_count, measure.call_count), (1, 1))

    def test_the_colours_belong_to_the_encoder_so_a_second_display_reuses_them(self) -> None:
        second = DxgiOutput(1, r"\\.\DISPLAY2", (2880, 0, 1920, 1080))
        with patch.object(gpu_capture, "_capture_works", return_value=True) as works, patch.object(gpu_capture, "_measure_colors", return_value=BT601_TV) as measure:
            self.ask()
            self.ask(output=second)
        self.assertEqual((works.call_count, measure.call_count), (2, 1))

    def test_another_encoder_is_measured_on_its_own(self) -> None:
        with patch.object(gpu_capture, "_capture_works", return_value=True), patch.object(gpu_capture, "_measure_colors", side_effect=[BT601_TV, Colors("bt709", "tv")]) as measure:
            self.assertEqual(self.ask(video_encoder._NVENC), BT601_TV)
            self.assertEqual(self.ask(video_encoder._AMF), Colors("bt709", "tv"))
        self.assertEqual(measure.call_count, 2)

    def test_a_display_that_fails_is_remembered_as_failing_and_never_measured(self) -> None:
        with patch.object(gpu_capture, "_capture_works", return_value=False) as works, patch.object(gpu_capture, "_measure_colors") as measure:
            self.assertIsNone(self.ask())
            self.assertIsNone(self.ask())
        self.assertEqual((works.call_count, measure.call_count), (1, 0))

    def test_colours_that_cannot_be_read_are_remembered_as_failing(self) -> None:
        with patch.object(gpu_capture, "_capture_works", return_value=True), patch.object(gpu_capture, "_measure_colors", return_value=None) as measure:
            self.assertIsNone(self.ask())
            self.assertIsNone(self.ask())
        self.assertEqual(measure.call_count, 1)

    def test_a_question_that_could_not_be_asked_is_asked_again_next_time(self) -> None:
        with patch.object(gpu_capture, "_capture_works", side_effect=[None, True]), patch.object(gpu_capture, "_measure_colors", return_value=BT601_TV):
            self.assertIsNone(self.ask())
            self.assertEqual(self.ask(), BT601_TV)

    def test_a_different_ffmpeg_is_asked_again(self) -> None:
        with patch.object(gpu_capture, "_capture_works", return_value=True) as works, patch.object(gpu_capture, "_measure_colors", return_value=BT601_TV):
            self.ask(ffmpeg="a")
            self.ask(ffmpeg="b")
        self.assertEqual(works.call_count, 2)


class PrefetchTests(unittest.TestCase):
    def test_every_screen_is_planned_in_the_background_and_a_failure_is_swallowed(self) -> None:
        screens = [SimpleNamespace(physical_rect=(0, 0, 2880, 1920), qt_name=DISPLAY), SimpleNamespace(physical_rect=(2880, 0, 1920, 1080), qt_name=r"\\.\DISPLAY2")]
        planned: list[ScreenCaptureConfig] = []

        class Now:
            def __init__(self, target, daemon=False) -> None:
                self.target = target

            def start(self) -> None:
                self.target()

        with patch.object(gpu_capture.threading, "Thread", Now), patch.object(gpu_capture, "plan_gpu_capture", side_effect=planned.append):
            gpu_capture.prefetch_gpu_capture(screens)
        self.assertEqual([(config.screen_name, config.capture_rect) for config in planned], [(DISPLAY, (0, 0, 2880, 1920)), (r"\\.\DISPLAY2", (2880, 0, 1920, 1080))])
        with patch.object(gpu_capture.threading, "Thread", Now), patch.object(gpu_capture, "plan_gpu_capture", side_effect=RuntimeError("boom")):
            gpu_capture.prefetch_gpu_capture(screens)


X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "60", "-pix_fmt", "yuv420p"]


class CommandTests(unittest.TestCase):
    """build_ffmpeg_argv with the plan stood in for."""

    plan = GpuCapture(["-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=30:draw_mouse=1"], "setparams=range=tv:colorspace=smpte170m",
                      ["-c:v", "h264_mf", "-hw_encoding", "1", "-rate_control", "quality", "-quality", "75", "-g", "60"])

    def argv(self, source, audio=None, plan="plan", allow_gpu=True) -> list[str]:
        with patch.object(ffmpeg_record, "find_ffmpeg", return_value="ffmpeg"), patch.object(ffmpeg_record, "plan_gpu_capture", return_value=self.plan if plan == "plan" else None) as planned, \
                patch("app.core.video_encoder.available_encoders", return_value=frozenset({"libx264", "aac"})), patch("app.core.video_encoder.find_ffmpeg", return_value="ffmpeg"):
            self.planned = planned
            return build_ffmpeg_argv(RecordConfig(source, audio or AudioCaptureConfig(None), Path("out.mp4")), allow_gpu=allow_gpu)

    def test_a_screen_on_the_chip_reads_from_ddagrab_and_labels_and_encodes_the_frames_there(self) -> None:
        self.assertEqual(self.argv(ScreenCaptureConfig((0, 0, 2880, 1920))), [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=30:draw_mouse=1", "-an", "-vf", "setparams=range=tv:colorspace=smpte170m",
            "-c:v", "h264_mf", "-hw_encoding", "1", "-rate_control", "quality", "-quality", "75", "-g", "60",
            "-video_track_timescale", "90000", "-movflags", "+faststart", "out.mp4",
        ])

    def test_the_microphone_still_joins_the_recording(self) -> None:
        argv = self.argv(ScreenCaptureConfig((0, 0, 2880, 1920)), AudioCaptureConfig("Mic", 1.25))
        self.assertEqual(argv[argv.index("-i") + 1], "ddagrab=output_idx=0:framerate=30:draw_mouse=1")
        self.assertIn("audio=Mic", argv)
        self.assertEqual(argv[argv.index("-map") + 1: argv.index("-map") + 4], ["0:v", "-map", "1:a"])
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")

    def test_without_a_plan_it_is_gdigrab_and_x264_exactly_as_before(self) -> None:
        argv = self.argv(ScreenCaptureConfig((10, 20, 640, 360)), plan=None)
        self.assertEqual(argv[:12], ["ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30", "-offset_x", "10", "-offset_y", "20", "-video_size", "640x360"])
        self.assertNotIn("-vf", argv)
        self.assertEqual(argv[argv.index("-c:v"): argv.index("-c:v") + len(X264)], X264)

    def test_with_the_chip_ruled_out_it_is_gdigrab_and_x264_and_the_plan_is_not_even_asked(self) -> None:
        argv = self.argv(ScreenCaptureConfig((0, 0, 640, 360)), allow_gpu=False)
        self.assertIn("gdigrab", argv)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.planned.assert_not_called()

    def test_a_camera_is_never_planned_as_a_screen(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (1920, 1080), input_format="mjpeg"))
        self.planned.assert_not_called()
        self.assertNotIn("ddagrab=output_idx=0:framerate=30:draw_mouse=1", argv)


class UsesGraphicsChipTests(unittest.TestCase):
    def test_it_recognises_a_chip_encoder_and_a_chip_capture(self) -> None:
        self.assertTrue(uses_graphics_chip(["ffmpeg", "-i", "x", "-c:v", "h264_nvenc", "out.mp4"]))
        self.assertTrue(uses_graphics_chip(["ffmpeg", "-f", "lavfi", "-i", "ddagrab=output_idx=0", "-c:v", "libx264", "out.mp4"]))

    def test_it_does_not_mistake_the_cpu_or_a_copy_for_the_chip(self) -> None:
        self.assertFalse(uses_graphics_chip(["ffmpeg", "-c:v", "libx264", "out.mp4"]))
        self.assertFalse(uses_graphics_chip(["ffmpeg", "-c:v", "copy", "out.mp4"]))
        self.assertFalse(uses_graphics_chip(["ffmpeg", "-f", "gdigrab", "-i", "desktop", "out.mp4"]))
        self.assertFalse(uses_graphics_chip(["ffmpeg", "-c:v"]))


class SessionFallbackTests(unittest.TestCase):
    """A segment on the chip that dies as it starts is thrown away and started again on the CPU; nothing later is."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.session = RecordingSession(RecordConfig(ScreenCaptureConfig((0, 0, 640, 360)), AudioCaptureConfig(None), Path(self.folder.name) / "out.mp4"), Path(self.folder.name))
        self.segment = Path(self.folder.name) / "segment_001.mp4"
        self.segment.write_bytes(b"not much")
        self.segment.with_suffix(".log").write_text("error")
        self.session.segment_paths = [self.segment]
        self.session.segment_used_gpu = True
        self.session._segment_started = 100.0
        self.session._log = MagicMock()
        self.clock = patch.object(ffmpeg_record.time, "monotonic", return_value=101.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_a_first_segment_on_the_chip_that_dies_at_once_is_dropped_and_the_cpu_takes_over(self) -> None:
        log = self.session._log
        self.assertTrue(self.session.fall_back_to_cpu())
        self.assertEqual(self.session.segment_paths, [])
        self.assertFalse(self.segment.exists())
        self.assertFalse(self.segment.with_suffix(".log").exists())
        log.close.assert_called_once()
        self.assertFalse(self.session._allow_gpu)
        self.assertTrue(self.session.fell_back_to_cpu)
        self.assertFalse(self.session.segment_used_gpu)

    def test_it_is_not_done_when_the_segment_was_not_on_the_chip(self) -> None:
        self.session.segment_used_gpu = False
        self.assertFalse(self.session.fall_back_to_cpu())
        self.assertTrue(self.segment.exists())

    def test_it_is_not_done_for_a_later_segment_because_encoders_cannot_be_joined(self) -> None:
        self.session.segment_paths = [Path(self.folder.name) / "segment_000.mp4", self.segment]
        self.assertFalse(self.session.fall_back_to_cpu())
        self.assertTrue(self.segment.exists())

    def test_it_is_not_done_once_the_recording_has_been_running_a_while(self) -> None:
        with patch.object(ffmpeg_record.time, "monotonic", return_value=100.0 + RecordingSession.START_UP_SECONDS + 1):
            self.assertFalse(self.session.fall_back_to_cpu())
        self.assertTrue(self.segment.exists())

    def test_it_is_only_done_once(self) -> None:
        self.assertTrue(self.session.fall_back_to_cpu())
        self.session.segment_paths = [self.segment]
        self.session.segment_used_gpu = True
        self.assertFalse(self.session.fall_back_to_cpu())

    def test_starting_a_segment_notes_whether_it_is_on_the_chip(self) -> None:
        for argv, on_chip, screen in ((["ffmpeg", "-f", "lavfi", "-i", "ddagrab=output_idx=0", "-c:v", "h264_mf", "o.mp4"], True, True),
                                      (["ffmpeg", "-f", "gdigrab", "-i", "desktop", "-c:v", "h264_mf", "o.mp4"], True, False),
                                      (["ffmpeg", "-f", "gdigrab", "-i", "desktop", "-c:v", "libx264", "o.mp4"], False, False)):
            self.session.segment_paths = []
            with patch.object(ffmpeg_record, "build_ffmpeg_argv", return_value=argv) as built, patch.object(ffmpeg_record.subprocess, "Popen"), \
                    patch("builtins.open", MagicMock()):
                self.session.start_segment()
            self.assertEqual((self.session.segment_used_gpu, self.session.captured_on_gpu), (on_chip, screen), argv)
            self.assertIs(built.call_args.kwargs["allow_gpu"], True)

    def test_after_a_fall_back_the_next_command_keeps_the_chip_out(self) -> None:
        self.session.fall_back_to_cpu()
        with patch.object(ffmpeg_record, "build_ffmpeg_argv", return_value=["ffmpeg", "-c:v", "libx264", "o.mp4"]) as built, patch.object(ffmpeg_record.subprocess, "Popen"), \
                patch("builtins.open", MagicMock()):
            self.session.start_segment()
        self.assertIs(built.call_args.kwargs["allow_gpu"], False)


class ControllerFallbackTests(unittest.TestCase):
    def controller(self, *, alive: bool = False, falls_back: bool = False, start_error: Exception | None = None):
        controller = RecordingController()
        controller._state = "recording"
        controller.session = MagicMock(poll_alive=MagicMock(return_value=alive), fall_back_to_cpu=MagicMock(return_value=falls_back),
                                       start_segment=MagicMock(side_effect=start_error), log_tail=MagicMock(return_value="encoder said no"))
        self.failures: list[str] = []
        controller.failed.connect(self.failures.append)
        return controller

    def test_a_start_up_failure_on_the_chip_starts_the_segment_again_on_the_cpu_and_the_recording_goes_on(self) -> None:
        controller = self.controller(falls_back=True)
        controller._tick()
        controller.session.start_segment.assert_called_once()
        self.assertEqual(self.failures, [])
        self.assertEqual(controller.state, "recording")
        self.assertTrue(controller.gpu_fell_back)

    def test_if_the_cpu_cannot_start_either_it_is_a_failure(self) -> None:
        controller = self.controller(falls_back=True, start_error=OSError("no ffmpeg"))
        controller._tick()
        self.assertEqual(self.failures, ["no ffmpeg"])

    def test_any_other_death_is_still_the_failure_it_always_was(self) -> None:
        controller = self.controller(falls_back=False)
        controller._tick()
        controller.session  # was reset by the failure; the message carries what ffmpeg said
        self.assertEqual(len(self.failures), 1)
        self.assertIn("ffmpeg stopped unexpectedly", self.failures[0])
        self.assertIn("encoder said no", self.failures[0])

    def test_a_running_ffmpeg_is_left_alone(self) -> None:
        controller = self.controller(alive=True)
        controller._tick()
        controller.session.fall_back_to_cpu.assert_not_called()
        self.assertEqual(self.failures, [])

    def test_what_happened_is_reported_when_the_recording_finishes(self) -> None:
        controller = self.controller()
        controller.session.dropped_frame_warnings = 0
        controller.session.captured_on_gpu = True
        controller.session.fell_back_to_cpu = False
        controller._finish(Path("out.mp4"))
        self.assertTrue(controller.captured_on_gpu)
        self.assertFalse(controller.gpu_fell_back)


if __name__ == "__main__":
    unittest.main()
