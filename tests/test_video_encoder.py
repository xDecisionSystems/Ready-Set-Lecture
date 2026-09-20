from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core import exporter, video_encoder
from app.core.video_encoder import available_encoders, describe_encoder, h264_args, prefetch_encoders

X264_RECORD = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "60", "-pix_fmt", "yuv420p"]

ENCODERS_OUTPUT = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D libopenh264          OpenH264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_mf              H264 via MediaFoundation (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
 S..... srt                  SubRip subtitle (codec subrip)
"""


class AvailableEncodersTests(unittest.TestCase):
    def setUp(self) -> None:
        video_encoder._encoders_by_ffmpeg.clear()
        self.addCleanup(video_encoder._encoders_by_ffmpeg.clear)

    def test_the_encoders_are_read_from_ffmpegs_list_and_the_legend_is_not(self) -> None:
        with patch.object(video_encoder.subprocess, "run", return_value=SimpleNamespace(stdout=ENCODERS_OUTPUT)):
            names = available_encoders("ffmpeg")
        self.assertEqual(names, frozenset({"libx264", "libopenh264", "h264_mf", "aac", "srt"}))

    def test_it_is_asked_once_per_ffmpeg(self) -> None:
        with patch.object(video_encoder.subprocess, "run", return_value=SimpleNamespace(stdout=ENCODERS_OUTPUT)) as run:
            available_encoders("ffmpeg-a")
            available_encoders("ffmpeg-a")
            self.assertEqual(run.call_count, 1)
            available_encoders("ffmpeg-b")  # a different ffmpeg can have different encoders
            self.assertEqual(run.call_count, 2)

    def test_a_failed_answer_means_none_and_is_not_remembered(self) -> None:
        with patch.object(video_encoder.subprocess, "run", side_effect=OSError("no such file")):
            self.assertEqual(available_encoders("ffmpeg"), frozenset())
        with patch.object(video_encoder.subprocess, "run", return_value=SimpleNamespace(stdout="")):
            self.assertEqual(available_encoders("ffmpeg"), frozenset())
        with patch.object(video_encoder.subprocess, "run", return_value=SimpleNamespace(stdout=ENCODERS_OUTPUT)):
            self.assertIn("libx264", available_encoders("ffmpeg"))  # it recovers once ffmpeg answers

    def test_prefetching_never_raises_even_with_no_ffmpeg(self) -> None:
        with patch.object(video_encoder, "find_ffmpeg", side_effect=FileNotFoundError("no ffmpeg")):
            prefetch_encoders()


class H264ArgsTests(unittest.TestCase):
    with_x264 = frozenset({"libx264", "libopenh264"})
    without_x264 = frozenset({"libopenh264"})

    def test_recording_uses_x264_at_crf_20_veryfast(self) -> None:
        self.assertEqual(h264_args("record", self.with_x264),
                         ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "60", "-pix_fmt", "yuv420p"])

    def test_export_uses_x264_at_a_slightly_higher_quality(self) -> None:
        args = h264_args("export", self.with_x264)
        self.assertEqual((args[args.index("-preset") + 1], args[args.index("-crf") + 1]), ("veryfast", "18"))

    def test_without_x264_it_is_openh264_exactly_as_before(self) -> None:
        for purpose in ("record", "export"):
            self.assertEqual(h264_args(purpose, self.without_x264), ["-c:v", "libopenh264", "-b:v", "20M"])

    def test_with_no_encoder_information_at_all_it_is_openh264(self) -> None:
        self.assertEqual(h264_args("record", frozenset()), ["-c:v", "libopenh264", "-b:v", "20M"])

    def test_the_real_ffmpeg_is_asked_when_the_encoders_are_not_given(self) -> None:
        with patch.object(video_encoder, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(video_encoder, "available_encoders", return_value=self.with_x264) as asked:
            self.assertEqual(h264_args("record")[1], "libx264")
        asked.assert_called_once_with("ffmpeg")

    def test_frames_bigger_than_1080p_get_the_lighter_superfast_preset(self) -> None:
        for name, pixels in (("1440p", 2560 * 1440), ("4K", 3840 * 2160), ("a HiDPI full screen", 2880 * 1920)):
            args = h264_args("record", self.with_x264, pixels=pixels)
            self.assertEqual((args[args.index("-preset") + 1], args[args.index("-crf") + 1]), ("superfast", "20"), name)

    def test_1080p_and_smaller_keep_veryfast(self) -> None:
        for name, pixels in (("1080p", 1920 * 1080), ("720p", 1280 * 720), ("unknown size", None)):
            args = h264_args("record", self.with_x264, pixels=pixels)
            self.assertEqual(args[args.index("-preset") + 1], "veryfast", name)

    def test_export_is_not_real_time_so_the_frame_size_does_not_change_it(self) -> None:
        args = h264_args("export", self.with_x264, pixels=3840 * 2160)
        self.assertEqual(args[args.index("-preset") + 1], "veryfast")

    def test_the_pixel_format_is_always_stated_so_players_can_show_it(self) -> None:
        for purpose in ("record", "export"):
            args = h264_args(purpose, self.with_x264)
            self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")


class ExporterUsesItTests(unittest.TestCase):
    def command(self, precise: bool, encoders: frozenset[str]) -> list[str]:
        seen: list[list[str]] = []
        with patch.object(exporter, "find_ffmpeg", return_value="ffmpeg"), patch.object(video_encoder, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(video_encoder, "available_encoders", return_value=encoders), \
                patch.object(exporter, "_run_with_progress", side_effect=lambda cmd, duration, report: seen.append(cmd)):
            exporter._extract_segment("in.mp4", 10.0, 20.0, Path("out.mp4"), precise, lambda seconds: None)
        return seen[0]

    def test_a_precise_cut_re_encodes_with_x264(self) -> None:
        cmd = self.command(True, frozenset({"libx264", "libopenh264"}))
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-crf") + 1], "18")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_a_precise_cut_falls_back_to_openh264(self) -> None:
        cmd = self.command(True, frozenset({"libopenh264"}))
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libopenh264")
        self.assertNotIn("libx264", cmd)

    def test_a_fast_cut_copies_and_never_touches_the_encoder(self) -> None:
        cmd = self.command(False, frozenset({"libx264"}))
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("libx264", cmd)


class GraphicsChipTests(unittest.TestCase):
    """Choosing the graphics chip's encoder. No real ffmpeg runs: the test encode is stood in for by a set of the ways that "work"."""

    everything = frozenset({"libx264", "libopenh264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "h264_videotoolbox", "h264_vaapi"})

    def setUp(self) -> None:
        video_encoder._probe_results.clear()
        self.addCleanup(video_encoder._probe_results.clear)
        self.tried: list[str] = []
        self.working: set[str] = set()
        self.gpu_on = True
        for name, options in (("find_ffmpeg", {"return_value": "ffmpeg"}), ("_test_encode", {"side_effect": self._test_encode}),
                              ("_gpu_encoding_enabled", {"side_effect": lambda: self.gpu_on}), ("_vaapi_device", {"return_value": "/dev/dri/renderD128"})):
            patcher = patch.object(video_encoder, name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _test_encode(self, ffmpeg: str, candidate) -> bool:
        self.tried.append(candidate.key)
        return candidate.key in self.working

    def args(self, platform: str, *, purpose: str = "record", encoders: frozenset[str] | None = None, **options) -> list[str]:
        with patch.object(video_encoder, "_platform", return_value=platform):
            return h264_args(purpose, self.everything if encoders is None else encoders, **options)

    def encoder(self, platform: str, **options) -> str:
        args = self.args(platform, **options)
        return args[args.index("-c:v") + 1]

    def test_windows_tries_nvidia_then_intel_then_amd_then_media_foundation_then_the_cpu(self) -> None:
        for working, expected in (({"nvenc", "qsv", "amf", "mf-quality"}, "h264_nvenc"), ({"qsv", "amf", "mf-quality"}, "h264_qsv"),
                                  ({"amf", "mf-quality"}, "h264_amf"), ({"mf-quality", "mf-bitrate"}, "h264_mf"), (set(), "libx264")):
            self.working = working
            video_encoder._probe_results.clear()
            self.assertEqual(self.encoder("win32"), expected, working)

    def test_media_foundation_falls_back_to_a_bitrate_when_it_refuses_a_quality(self) -> None:
        self.working = {"mf-bitrate"}
        args = self.args("win32")
        self.assertEqual(args[args.index("-rate_control") + 1], "u_vbr")
        self.assertIn("-b:v", args)
        self.assertNotIn("-quality", args)
        self.assertEqual(self.tried, ["nvenc", "qsv", "amf", "mf-quality", "mf-bitrate"])

    def test_media_foundation_is_told_never_to_use_microsofts_software_encoder(self) -> None:
        self.working = {"mf-quality"}
        args = self.args("win32")
        self.assertEqual(args[args.index("-hw_encoding") + 1], "1")

    def test_macs_use_videotoolbox_only_and_never_the_windows_or_linux_encoders(self) -> None:
        self.working = {"nvenc", "qsv", "amf", "mf-quality", "vaapi-quality", "vt-quality"}
        args = self.args("darwin")
        self.assertEqual(args[args.index("-c:v") + 1], "h264_videotoolbox")
        self.assertEqual(args[args.index("-allow_sw") + 1], "0")  # never Apple's software encoder
        self.assertEqual(self.tried, ["vt-quality"])

    def test_videotoolbox_falls_back_to_a_bitrate_where_it_has_no_constant_quality(self) -> None:
        self.working = {"vt-bitrate"}  # an Intel Mac
        args = self.args("darwin")
        self.assertIn("-b:v", args)
        self.assertNotIn("-q:v", args)

    def test_linux_tries_nvidia_then_intel_then_va_api_which_serves_intel_and_amd(self) -> None:
        for working, expected in (({"nvenc", "vaapi-quality"}, "h264_nvenc"), ({"qsv", "vaapi-quality"}, "h264_qsv"), ({"vaapi-quality"}, "h264_vaapi"),
                                  ({"vaapi-bitrate"}, "h264_vaapi"), (set(), "libx264")):
            self.working, self.tried = working, []
            video_encoder._probe_results.clear()
            self.assertEqual(self.encoder("linux"), expected, working)
            self.assertNotIn("mf-quality", self.tried)  # Windows-only
            self.assertNotIn("amf", self.tried)

    def test_va_api_uploads_the_frames_with_a_filter_and_names_the_render_device(self) -> None:
        self.working = {"vaapi-quality"}
        args = self.args("linux")
        self.assertEqual(args[args.index("-vaapi_device") + 1], "/dev/dri/renderD128")
        self.assertEqual(args[args.index("-vf") + 1], "format=nv12,hwupload")

    def test_va_api_is_left_out_for_a_command_that_has_filters_of_its_own(self) -> None:
        self.working = {"vaapi-quality", "vaapi-bitrate"}
        self.assertEqual(self.encoder("linux", allow_filters=False), "libx264")
        self.assertNotIn("vaapi-quality", self.tried)  # not even test-encoded

    def test_va_api_is_left_out_when_there_is_no_render_device(self) -> None:
        self.working = {"vaapi-quality"}
        with patch.object(video_encoder, "_vaapi_device", return_value=None):
            self.assertEqual(self.encoder("linux"), "libx264")
        self.assertNotIn("vaapi-quality", self.tried)

    def test_an_encoder_this_ffmpeg_does_not_have_is_never_test_encoded(self) -> None:
        self.working = {"nvenc", "qsv", "amf", "mf-quality"}
        self.assertEqual(self.encoder("win32", encoders=frozenset({"libx264", "h264_mf"})), "h264_mf")
        self.assertEqual(self.tried, ["mf-quality"])

    def test_an_ffmpeg_with_no_graphics_encoders_uses_x264_without_test_encoding_anything(self) -> None:
        self.assertEqual(self.encoder("win32", encoders=frozenset({"libx264", "libopenh264"})), "libx264")
        self.assertEqual(self.tried, [])

    def test_switching_gpu_encoding_off_uses_x264_and_does_not_test_encode(self) -> None:
        self.working, self.gpu_on = {"nvenc", "mf-quality"}, False
        self.assertEqual(self.args("win32"), X264_RECORD)
        self.assertEqual(self.tried, [])

    def test_without_x264_the_graphics_chip_still_wins_and_otherwise_it_is_openh264(self) -> None:
        no_x264 = frozenset({"libopenh264", "h264_nvenc"})
        self.working = {"nvenc"}
        self.assertEqual(self.encoder("win32", encoders=no_x264), "h264_nvenc")
        self.working = set()
        video_encoder._probe_results.clear()
        self.assertEqual(self.encoder("win32", encoders=no_x264), "libopenh264")

    def test_an_unknown_operating_system_uses_x264(self) -> None:
        self.working = {"nvenc", "mf-quality", "vt-quality"}
        self.assertEqual(self.encoder("freebsd14"), "libx264")

    def test_the_answer_is_remembered_so_each_way_is_test_encoded_once(self) -> None:
        self.working = {"amf"}
        for _ in range(3):
            self.args("win32")
        self.assertEqual(self.tried, ["nvenc", "qsv", "amf"])

    def test_a_test_encode_that_could_not_be_run_is_not_remembered(self) -> None:
        answers = iter([None, True])
        with patch.object(video_encoder, "_test_encode", side_effect=lambda ffmpeg, candidate: next(answers)):
            self.assertEqual(self.encoder("win32"), "libx264")  # could not find out: the CPU this time
            self.assertEqual(self.encoder("win32"), "h264_nvenc")  # asked again, and it works

    def test_a_different_ffmpeg_is_asked_again(self) -> None:
        self.working = {"nvenc"}
        self.args("win32")
        with patch.object(video_encoder, "find_ffmpeg", return_value="other-ffmpeg"):
            self.args("win32")
        self.assertEqual(self.tried, ["nvenc", "nvenc"])

    def test_no_ffmpeg_at_all_means_the_cpu_and_no_error(self) -> None:
        with patch.object(video_encoder, "find_ffmpeg", side_effect=FileNotFoundError("no ffmpeg")):
            self.assertEqual(self.encoder("win32"), "libx264")

    def test_export_asks_for_a_little_more_quality_than_recording(self) -> None:
        for platform, working, option, record, export in (("win32", {"nvenc"}, "-cq", "24", "22"), ("win32", {"mf-quality"}, "-quality", "75", "80"),
                                                          ("darwin", {"vt-quality"}, "-q:v", "65", "72"), ("win32", {"qsv"}, "-global_quality", "23", "21")):
            self.working = working
            video_encoder._probe_results.clear()
            recorded, exported = self.args(platform), self.args(platform, purpose="export")
            self.assertEqual((recorded[recorded.index(option) + 1], exported[exported.index(option) + 1]), (record, export), (platform, working))

    def test_every_graphics_encoder_keeps_the_keyframe_interval_the_editors_fast_cuts_rely_on(self) -> None:
        for candidates in video_encoder._HARDWARE_BY_PLATFORM.values():
            for candidate in candidates:
                args = candidate.args("record", 1920 * 1080)
                self.assertEqual(args[args.index("-g") + 1], "60", candidate.key)
                self.assertEqual(args[args.index("-c:v") + 1], candidate.encoder, candidate.key)

    def test_the_bitrate_fallback_scales_with_the_frame(self) -> None:
        self.assertEqual(video_encoder._fallback_bitrate(1920 * 1080), "6M")
        self.assertEqual(video_encoder._fallback_bitrate(3840 * 2160), "25M")
        self.assertEqual(video_encoder._fallback_bitrate(None), "6M")
        self.assertEqual(video_encoder._fallback_bitrate(640 * 360), "2M")

    def test_it_says_in_words_what_it_encodes_with(self) -> None:
        self.working = {"mf-quality"}
        with patch.object(video_encoder, "available_encoders", return_value=self.everything), patch.object(video_encoder, "_platform", return_value="win32"):
            self.assertEqual(describe_encoder(), "the graphics chip (Windows Media Foundation)")
            self.gpu_on = False
            self.assertEqual(describe_encoder(), "the CPU (x264)")
        with patch.object(video_encoder, "available_encoders", return_value=frozenset({"libopenh264"})):
            self.assertEqual(describe_encoder(), "the CPU (OpenH264)")
        with patch.object(video_encoder, "available_encoders", return_value=frozenset()):
            self.assertEqual(describe_encoder(), "")

    def test_prefetching_runs_the_test_encodes_in_the_background_and_never_raises(self) -> None:
        self.working = {"mf-quality"}
        with patch.object(video_encoder, "available_encoders", return_value=self.everything), patch.object(video_encoder, "_platform", return_value="win32"):
            video_encoder._prefetch("ffmpeg")
        self.assertEqual(self.tried, ["nvenc", "qsv", "amf", "mf-quality"])
        with patch.object(video_encoder, "available_encoders", side_effect=RuntimeError("boom")):
            video_encoder._prefetch("ffmpeg")


class TestEncodeTests(unittest.TestCase):
    """The real _test_encode, with only the ffmpeg process stood in for."""

    candidate = video_encoder._MF_QUALITY

    def run_it(self, **run_options):
        with patch.object(video_encoder.subprocess, "run", **run_options) as run:
            return video_encoder._test_encode("ffmpeg", self.candidate), run

    def test_it_works_when_ffmpeg_encodes_the_frames(self) -> None:
        worked, run = self.run_it(return_value=SimpleNamespace(returncode=0))
        self.assertIs(worked, True)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-3:], ["-f", "null", "-"])
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_mf")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "8")

    def test_it_does_not_work_when_ffmpeg_refuses(self) -> None:
        self.assertIs(self.run_it(return_value=SimpleNamespace(returncode=1))[0], False)

    def test_it_could_not_be_asked_when_ffmpeg_will_not_run_or_hangs(self) -> None:
        self.assertIsNone(self.run_it(side_effect=OSError("no such file"))[0])
        self.assertIsNone(self.run_it(side_effect=subprocess.TimeoutExpired("ffmpeg", 20))[0])


class GpuSwitchTests(unittest.TestCase):
    def test_it_follows_the_saved_setting(self) -> None:
        for saved in (True, False):
            with patch("app.core.settings.get_gpu_encoding", return_value=saved):
                self.assertIs(video_encoder._gpu_encoding_enabled(), saved)

    def test_it_is_on_when_the_setting_cannot_be_read(self) -> None:
        with patch("app.core.settings.get_gpu_encoding", side_effect=RuntimeError("no settings")):
            self.assertIs(video_encoder._gpu_encoding_enabled(), True)


class ExporterAndRecorderUseTheGraphicsChipTests(unittest.TestCase):
    def setUp(self) -> None:
        video_encoder._probe_results.clear()
        self.addCleanup(video_encoder._probe_results.clear)
        for name, options in (("find_ffmpeg", {"return_value": "ffmpeg"}), ("_test_encode", {"return_value": True}), ("_platform", {"return_value": "win32"}),
                              ("_gpu_encoding_enabled", {"return_value": True}),
                              ("available_encoders", {"return_value": frozenset({"libx264", "h264_nvenc", "aac"})})):
            patcher = patch.object(video_encoder, name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def extract(self, precise: bool) -> list[str]:
        seen: list[list[str]] = []
        with patch.object(exporter, "find_ffmpeg", return_value="ffmpeg"), patch.object(exporter, "_run_with_progress", side_effect=lambda cmd, d, r: seen.append(cmd)):
            exporter._extract_segment("in.mp4", 10.0, 20.0, Path("out.mp4"), precise, lambda seconds: None)
        return seen[0]

    def test_a_precise_cut_re_encodes_on_the_graphics_chip_and_keeps_the_audio_settings(self) -> None:
        cmd = self.extract(True)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(cmd[cmd.index("-cq") + 1], "22")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_a_fast_cut_still_only_copies(self) -> None:
        cmd = self.extract(False)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("h264_nvenc", cmd)

    def test_a_screen_recording_encodes_on_the_graphics_chip(self) -> None:
        from app.recorder.core.ffmpeg_record import AudioCaptureConfig, RecordConfig, ScreenCaptureConfig, build_ffmpeg_argv
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(RecordConfig(ScreenCaptureConfig((0, 0, 1920, 1080)), AudioCaptureConfig(None), Path("out.mp4")))
        self.assertEqual(argv[argv.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(argv[argv.index("-pix_fmt") + 1], "nv12")

    def test_a_recording_never_gets_an_encoder_that_would_add_a_filter_of_its_own(self) -> None:
        from app.recorder.core.ffmpeg_record import AudioCaptureConfig, CameraCaptureConfig, RecordConfig, build_ffmpeg_argv
        camera = CameraCaptureConfig("Camera", (1920, 1080), (0, 0, 800, 600), input_format="mjpeg")
        seen: list[bool] = []
        real = video_encoder._hardware_encoder
        with patch.object(video_encoder, "_hardware_encoder", side_effect=lambda encoders, allow_filters=True: seen.append(allow_filters) or real(encoders, allow_filters)), \
                patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            build_ffmpeg_argv(RecordConfig(camera, AudioCaptureConfig(None), Path("out.mp4")))
        self.assertEqual(seen, [False])


if __name__ == "__main__":
    unittest.main()
