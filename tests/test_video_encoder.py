from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core import exporter, video_encoder
from app.core.video_encoder import available_encoders, h264_args, prefetch_encoders

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


if __name__ == "__main__":
    unittest.main()
