from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from app.recorder.core.ffmpeg_record import (
    AudioCaptureConfig, CameraCaptureConfig, RecordConfig, RecordingSession, ScreenCaptureConfig,
    _capture_buffer, _drain_frames, build_ffmpeg_argv, build_preview_argv, describe_recording, preview_frame_size,
)

X264_AND_OPENH264 = frozenset({"libx264", "libopenh264", "aac"})
OPENH264_ONLY = frozenset({"libopenh264", "aac"})  # e.g. an LGPL-only ffmpeg build
X264_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "60", "-pix_fmt", "yuv420p"]
OPENH264_ARGS = ["-c:v", "libopenh264", "-b:v", "20M"]


class EncoderCase(unittest.TestCase):
    """x264 is available unless a test says otherwise, and the real ffmpeg is never asked what it has."""

    encoders = X264_AND_OPENH264

    def setUp(self) -> None:
        for target, options in (("app.core.video_encoder.available_encoders", {"return_value": self.encoders}),
                                ("app.core.video_encoder.find_ffmpeg", {"return_value": "ffmpeg"})):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)


class PreviewFrameSizeTests(unittest.TestCase):
    def test_keeps_the_aspect_ratio_and_caps_the_width_at_1280(self) -> None:
        self.assertEqual(preview_frame_size((1920, 1080)), (1280, 720))
        self.assertEqual(preview_frame_size((4000, 3000)), (1280, 960))
        self.assertEqual(preview_frame_size((1920, 1080), width=320), (320, 180))

    def test_a_small_source_is_not_enlarged(self) -> None:
        self.assertEqual(preview_frame_size((800, 600)), (800, 600))
        self.assertEqual(preview_frame_size((200, 100)), (200, 100))

    def test_dimensions_are_always_even(self) -> None:
        for size in ((1279, 720), (333, 111), (641, 361), (17, 9), (3, 3)):
            width, height = preview_frame_size(size)
            self.assertEqual((width % 2, height % 2), (0, 0), size)
            self.assertGreaterEqual(min(width, height), 2)


class PreviewFeedTests(unittest.TestCase):
    def test_preview_only_command_streams_just_the_cropped_preview(self) -> None:
        source = CameraCaptureConfig("Camera", (1920, 1080), (240, 135, 800, 600), preview_size=(800, 600))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_preview_argv(source)
        self.assertEqual(argv, [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-rtbufsize", "64M", "-f", "dshow", "-video_size", "1920x1080", "-framerate", "30",
            "-i", "video=Camera", "-filter_complex", "[0:v]crop=800:600:240:135,fps=10,scale=800:600,format=bgra[pvo]",
            "-map", "[pvo]", "-f", "rawvideo", "pipe:1",
        ])

    def test_preview_only_command_has_no_recording_output_and_no_audio(self) -> None:
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_preview_argv(CameraCaptureConfig("Camera", (1280, 720), preview_size=(1280, 720)))
        self.assertNotIn("libopenh264", argv)
        self.assertNotIn("audio=", " ".join(argv))
        self.assertIn("[0:v]fps=10,scale=1280:720,format=bgra[pvo]", argv)

    def test_frames_are_handed_over_whole_and_a_partial_last_frame_is_dropped(self) -> None:
        received: list[bytes] = []
        stream = io.BytesIO(b"AAAA" + b"BBBB" + b"CC")
        _drain_frames(stream, 4, received.append)
        self.assertEqual(received, [b"AAAA", b"BBBB"])
        self.assertTrue(stream.closed)

    def test_a_consumer_that_raises_never_stops_the_reading(self) -> None:
        # If reading stopped, the pipe would fill and ffmpeg (and so the recording) would stall.
        seen: list[bytes] = []

        def flaky(frame: bytes) -> None:
            seen.append(frame)
            raise RuntimeError("consumer is broken")

        _drain_frames(io.BytesIO(b"1111" * 5), 4, flaky)
        self.assertEqual(len(seen), 5)

    def test_reading_without_a_consumer_still_drains_the_stream(self) -> None:
        class Counting(io.BytesIO):
            consumed = 0

            def read(self, size=-1):
                data = super().read(size)
                Counting.consumed += len(data)
                return data

        stream = Counting(b"1234" * 3)
        _drain_frames(stream, 4, None)
        self.assertEqual(Counting.consumed, 12)  # everything was read, so a real pipe would never fill
        self.assertTrue(stream.closed)


class CaptureBufferTests(unittest.TestCase):
    def test_1080p_and_smaller_keep_the_150_mb_buffer_they_always_had(self) -> None:
        self.assertEqual(_capture_buffer(1920, 1080, 30), "150M")
        self.assertEqual(_capture_buffer(1280, 720, 30), "150M")
        self.assertEqual(_capture_buffer(640, 480, 30), "150M")

    def test_4k_gets_a_buffer_big_enough_for_over_a_second_of_raw_frames(self) -> None:
        self.assertEqual(_capture_buffer(3840, 2160, 30), "597M")  # 3840 x 2160 x 2 bytes x 30 fps x 1.2 s
        self.assertGreater(int(_capture_buffer(3840, 2160, 30)[:-1]), 3 * 150)  # far more than the old fixed 150 MB

    def test_the_preview_only_feed_uses_about_half_a_second_and_is_never_below_64_mb(self) -> None:
        self.assertEqual(_capture_buffer(1920, 1080, 30, 0.5, 64), "64M")
        self.assertEqual(_capture_buffer(3840, 2160, 30, 0.5, 64), "249M")

    def test_the_commands_use_it(self) -> None:
        camera = CameraCaptureConfig("Camera", (3840, 2160), preview_size=(1280, 720))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"), \
                patch("app.core.video_encoder.available_encoders", return_value=X264_AND_OPENH264), patch("app.core.video_encoder.find_ffmpeg", return_value="ffmpeg"):
            recording = build_ffmpeg_argv(RecordConfig(camera, AudioCaptureConfig(None), Path("out.mp4")))
            preview = build_preview_argv(camera)
        self.assertEqual(recording[recording.index("-rtbufsize") + 1], "597M")
        self.assertEqual(preview[preview.index("-rtbufsize") + 1], "249M")


class CameraH264PassThroughTests(EncoderCase):
    """A camera that compresses to H.264 itself: the recorder only saves what the camera sends."""

    def argv(self, camera: CameraCaptureConfig, audio: AudioCaptureConfig | None = None) -> list[str]:
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            return build_ffmpeg_argv(RecordConfig(camera, audio or AudioCaptureConfig("Mic"), Path("out.mp4")))

    def test_4k_h264_with_the_live_preview(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (3840, 2160), preview_size=(1280, 720), input_format="h264"))
        self.assertEqual(argv, [
            "ffmpeg", "-y", "-rtbufsize", "597M", "-f", "dshow", "-vcodec", "h264", "-video_size", "3840x2160", "-framerate", "30", "-i", "video=Camera:audio=Mic",
            "-filter_complex", "[0:v]fps=10,scale=1280:720,format=bgra[pvo]", "-map", "0:v", "-map", "0:a", "-af", "volume=1.000",
            "-c:v", "copy", "-video_track_timescale", "90000", "-c:a", "aac", "-movflags", "+faststart", "out.mp4",
            "-map", "[pvo]", "-f", "rawvideo", "pipe:1",
        ])

    def test_nothing_is_decoded_for_the_recording_or_encoded(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (3840, 2160), preview_size=(1280, 720), input_format="h264"))
        for word in ("libx264", "libopenh264", "-crf", "-preset", "-pix_fmt", "split=2", "[rec]"):
            self.assertNotIn(word, argv)

    def test_without_a_preview_it_is_just_a_copy(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (3840, 2160), input_format="h264"), AudioCaptureConfig(None))
        self.assertNotIn("-filter_complex", argv)
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertIn("-an", argv)

    def test_the_video_time_scale_is_still_fixed_so_segments_join(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (3840, 2160), input_format="h264"))
        self.assertEqual(argv[argv.index("-video_track_timescale") + 1], "90000")

    def test_a_crop_means_the_picture_is_changed_so_it_is_encoded_again(self) -> None:
        camera = CameraCaptureConfig("Camera", (3840, 2160), (100, 100, 1280, 720), preview_size=(1280, 720), input_format="h264")
        self.assertFalse(camera.saves_stream_as_is)
        argv = self.argv(camera)
        self.assertEqual(argv[argv.index("-vcodec") + 1], "h264")  # the camera's cheap-to-decode stream is still the input
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertIn("[0:v]crop=1280:720:100:100,split=2[rec][pv];[pv]fps=10,scale=1280:720,format=bgra[pvo]", argv)

    def test_no_input_format_means_no_vcodec_and_the_normal_encoding(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720)))
        self.assertNotIn("-vcodec", argv)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")

    def test_the_countdown_preview_uses_the_same_input_format(self) -> None:
        camera = CameraCaptureConfig("Camera", (3840, 2160), preview_size=(1280, 720), input_format="h264")
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_preview_argv(camera)
        self.assertEqual(argv[argv.index("-vcodec") + 1], "h264")
        self.assertLess(argv.index("-vcodec"), argv.index("-i"))  # an input option: before -i

    def test_saves_as_is_only_for_h264_without_a_crop(self) -> None:
        self.assertTrue(CameraCaptureConfig("C", (3840, 2160), input_format="h264").saves_stream_as_is)
        self.assertFalse(CameraCaptureConfig("C", (3840, 2160), input_format="mjpeg").saves_stream_as_is)
        self.assertFalse(CameraCaptureConfig("C", (3840, 2160)).saves_stream_as_is)

    def test_h265_is_saved_as_is_and_tagged_hvc1_so_players_accept_it(self) -> None:
        camera = CameraCaptureConfig("Camera", (3840, 2160), preview_size=(1280, 720), input_format="hevc")
        self.assertTrue(camera.saves_stream_as_is)
        argv = self.argv(camera)
        self.assertEqual(argv[argv.index("-vcodec") + 1], "hevc")
        i = argv.index("-c:v")
        self.assertEqual(argv[i:i + 4], ["-c:v", "copy", "-tag:v", "hvc1"])
        for word in ("libx264", "libx265", "-crf", "-preset", "split=2", "[rec]"):
            self.assertNotIn(word, argv)

    def test_h264_is_not_given_the_hevc_tag(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (3840, 2160), input_format="h264"))
        self.assertNotIn("-tag:v", argv)

    def test_a_cropped_h265_stream_is_decoded_and_encoded_as_h264(self) -> None:
        argv = self.argv(CameraCaptureConfig("Camera", (3840, 2160), (0, 0, 1920, 1080), preview_size=(1280, 720), input_format="hevc"))
        self.assertEqual(argv[argv.index("-vcodec") + 1], "hevc")  # the camera's cheaper-to-decode stream is still the input
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertNotIn("-tag:v", argv)

    def test_choosing_mjpeg_asks_for_it_and_the_computer_encodes(self) -> None:
        camera = CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720), input_format="mjpeg")
        self.assertFalse(camera.saves_stream_as_is)
        argv = self.argv(camera)
        self.assertEqual(argv[argv.index("-vcodec") + 1], "mjpeg")
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")

    def test_choosing_an_uncompressed_format_asks_for_that_pixel_format(self) -> None:
        camera = CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720), input_format="pixel:nv12")
        argv = self.argv(camera)
        self.assertEqual(argv[argv.index("-pixel_format") + 1], "nv12")
        self.assertNotIn("-vcodec", argv)
        self.assertLess(argv.index("-pixel_format"), argv.index("-i"))  # an input option
        self.assertFalse(camera.saves_stream_as_is)

    def test_the_countdown_preview_asks_for_the_same_pixel_format(self) -> None:
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_preview_argv(CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720), input_format="pixel:nv12"))
        self.assertEqual(argv[argv.index("-pixel_format") + 1], "nv12")

    def test_the_saved_message_names_h265_uncompressed_and_mjpeg_correctly(self) -> None:
        self.assertEqual(describe_recording(CameraCaptureConfig("C", (3840, 2160), input_format="hevc")),
                         "Camera: 3840×2160, the camera's own H.265 stream, saved as it came (no re-encoding).")
        self.assertEqual(describe_recording(CameraCaptureConfig("C", (1920, 1080), input_format="mjpeg")),
                         "Camera: 1920×1080, encoded by the computer from the camera's MJPEG stream.")
        self.assertEqual(describe_recording(CameraCaptureConfig("C", (1920, 1080), input_format="pixel:nv12")),
                         "Camera: 1920×1080, encoded by the computer from the camera's uncompressed picture.")
    def test_the_saved_message_says_how_it_was_recorded(self) -> None:
        self.assertEqual(describe_recording(ScreenCaptureConfig((0, 0, 640, 360))), "")
        self.assertEqual(describe_recording(CameraCaptureConfig("C", (3840, 2160), input_format="h264")),
                         "Camera: 3840×2160, the camera's own H.264 stream, saved as it came (no re-encoding).")
        self.assertEqual(describe_recording(CameraCaptureConfig("C", (3840, 2160), (0, 0, 1920, 1080), input_format="h264")),
                         "Camera: 1920×1080, encoded by the computer from the camera's H.264 stream.")
        self.assertEqual(describe_recording(CameraCaptureConfig("C", (1920, 1080))), "Camera: 1920×1080, encoded by the computer.")


class DropWarningTests(unittest.TestCase):
    def test_ffmpegs_real_time_buffer_warnings_are_counted(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "segment_001.log"
            log.write_text(
                "Input #0, dshow, from 'video=Camera':\n"
                "[dshow @ 000001] real-time buffer [Camera] [video input] too full or near too full (77% of size: 150000000 [rtbufsize parameter])! frame dropped!\n"
                "frame=   10 fps=9.0 q=-1.0 size=       0kB time=00:00:00.33\n"
                "[dshow @ 000001] real-time buffer [Camera] [video input] too full or near too full (81% of size: 150000000 [rtbufsize parameter])! frame dropped!\n",
                encoding="utf-8")
            self.assertEqual(RecordingSession._count_drop_warnings(log), 2)

    def test_a_clean_log_or_a_missing_one_counts_nothing(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            clean = Path(folder) / "clean.log"
            clean.write_text("frame=  240 fps= 30 q=-1.0 size=1024kB time=00:00:08.00 speed=1.0x\n", encoding="utf-8")
            self.assertEqual(RecordingSession._count_drop_warnings(clean), 0)
            self.assertEqual(RecordingSession._count_drop_warnings(Path(folder) / "missing.log"), 0)


class BuildFfmpegArgvTests(EncoderCase):
    def test_screen_with_microphone_maps_two_inputs(self) -> None:
        config = RecordConfig(ScreenCaptureConfig((0, 0, 1920, 1080)), AudioCaptureConfig("Mic", 1.5), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv, [
            "ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30", "-offset_x", "0", "-offset_y", "0", "-video_size", "1920x1080", "-i", "desktop",
            "-f", "dshow", "-i", "audio=Mic", "-map", "0:v", "-map", "1:a", "-af", "volume=1.500",
            *X264_ARGS, "-video_track_timescale", "90000", "-c:a", "aac", "-movflags", "+faststart", "out.mp4",
        ])

    def test_x264_is_used_at_a_quality_setting_and_states_its_pixel_format(self) -> None:
        config = RecordConfig(ScreenCaptureConfig((0, 0, 1920, 1080)), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertIn("-crf", argv)
        self.assertNotIn("-b:v", argv)  # quality-based, not a fixed 20 Mbps
        self.assertEqual(argv[argv.index("-pix_fmt") + 1], "yuv420p")  # a screen grab is RGB: without this ffmpeg may pick 4:4:4
        self.assertEqual(argv[argv.index("-g") + 1], "60")  # a keyframe at least every two seconds, for the editor's fast cuts

    def test_a_4k_camera_recording_uses_the_lighter_preset_and_a_big_buffer(self) -> None:
        config = RecordConfig(CameraCaptureConfig("Camera", (3840, 2160), preview_size=(1280, 720)), AudioCaptureConfig("Mic"), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv[argv.index("-preset") + 1], "superfast")
        self.assertEqual(argv[argv.index("-rtbufsize") + 1], "597M")
        self.assertEqual(argv[argv.index("-video_size") + 1], "3840x2160")

    def test_what_counts_is_the_recorded_size_so_a_small_crop_of_a_4k_camera_is_light_work(self) -> None:
        config = RecordConfig(CameraCaptureConfig("Camera", (3840, 2160), (100, 100, 1280, 720)), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv[argv.index("-preset") + 1], "veryfast")

    def test_a_full_hidpi_screen_uses_the_lighter_preset(self) -> None:
        config = RecordConfig(ScreenCaptureConfig((0, 0, 2880, 1920)), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv[argv.index("-preset") + 1], "superfast")

    def test_every_segment_uses_the_same_encoder_settings(self) -> None:
        # Segments are joined without re-encoding, so they must be encoded identically.
        config = RecordConfig(CameraCaptureConfig("Camera", (1280, 720)), AudioCaptureConfig("Mic"), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            first = build_ffmpeg_argv(config, Path("segment_001.mp4"))
            second = build_ffmpeg_argv(config, Path("segment_002.mp4"))
        self.assertEqual(first[:-1], second[:-1])

    def test_every_segment_uses_the_same_fixed_video_time_scale(self) -> None:
        # Segments with different time bases sometimes joined into a wrong-length file (pause/resume).
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            for source in (ScreenCaptureConfig((0, 0, 640, 360)), CameraCaptureConfig("Camera", (1280, 720))):
                argv = build_ffmpeg_argv(RecordConfig(source, AudioCaptureConfig(None), Path("out.mp4")))
                self.assertEqual(argv[argv.index("-video_track_timescale") + 1], "90000")
                self.assertLess(argv.index("-video_track_timescale"), argv.index("-movflags"))  # an output option, before the file

    def test_screen_cursor_is_recorded_by_default_and_the_argv_is_unchanged(self) -> None:
        config = RecordConfig(ScreenCaptureConfig((0, 0, 640, 360)), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            self.assertNotIn("-draw_mouse", build_ffmpeg_argv(config))

    def test_hiding_the_cursor_adds_draw_mouse_0_as_an_option_of_the_screen_input(self) -> None:
        config = RecordConfig(ScreenCaptureConfig((10, 20, 640, 360), draw_cursor=False), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv[:8], ["ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "0"])
        self.assertLess(argv.index("-draw_mouse"), argv.index("desktop"))  # an input option must come before its -i

    def test_camera_recordings_have_no_cursor_option(self) -> None:
        config = RecordConfig(CameraCaptureConfig("Camera", (1280, 720)), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            self.assertNotIn("-draw_mouse", build_ffmpeg_argv(config))

    def test_camera_preview_adds_a_second_raw_output_from_the_same_capture(self) -> None:
        config = RecordConfig(CameraCaptureConfig("Camera", (1920, 1080), preview_size=(1280, 720)), AudioCaptureConfig("Mic"), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv, [
            "ffmpeg", "-y", "-rtbufsize", "150M", "-f", "dshow", "-video_size", "1920x1080", "-framerate", "30", "-i", "video=Camera:audio=Mic",
            "-filter_complex", "[0:v]split=2[rec][pv];[pv]fps=10,scale=1280:720,format=bgra[pvo]", "-map", "[rec]", "-map", "0:a", "-af", "volume=1.000",
            *X264_ARGS, "-video_track_timescale", "90000", "-c:a", "aac", "-movflags", "+faststart", "out.mp4",
            "-map", "[pvo]", "-f", "rawvideo", "pipe:1",
        ])

    def test_camera_preview_shows_the_cropped_picture_that_is_recorded(self) -> None:
        config = RecordConfig(CameraCaptureConfig("Camera", (1280, 720), (100, 50, 800, 600), preview_size=(800, 600)), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertIn("[0:v]crop=800:600:100:50,split=2[rec][pv];[pv]fps=10,scale=800:600,format=bgra[pvo]", argv)  # crop first, then split
        self.assertNotIn("-vf", argv)
        self.assertIn("-an", argv)
        self.assertNotIn("-c:a", argv)
        self.assertEqual(argv[-5:], ["-map", "[pvo]", "-f", "rawvideo", "pipe:1"])

    def test_no_preview_output_unless_asked_for(self) -> None:
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            camera = build_ffmpeg_argv(RecordConfig(CameraCaptureConfig("Camera", (1280, 720)), AudioCaptureConfig("Mic"), Path("out.mp4")))
            screen = build_ffmpeg_argv(RecordConfig(ScreenCaptureConfig((0, 0, 640, 360)), AudioCaptureConfig(None), Path("out.mp4")))
        for argv in (camera, screen):
            self.assertNotIn("pipe:1", argv)
            self.assertNotIn("-filter_complex", argv)

    def test_camera_without_microphone(self) -> None:
        config = RecordConfig(CameraCaptureConfig("Camera", (1280, 720)), AudioCaptureConfig(None), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertIn('-i', argv)
        self.assertIn("video=Camera", argv)
        self.assertIn("-an", argv)
        self.assertNotIn("-c:a", argv)

    def test_device_ids_are_never_wrapped_in_literal_quotes(self) -> None:
        # argv is passed to Popen as a list, so quote characters would reach ffmpeg verbatim.
        config = RecordConfig(CameraCaptureConfig("Surface Camera Front", (1280, 720)), AudioCaptureConfig("Mic Array"), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertIn("video=Surface Camera Front:audio=Mic Array", argv)
        self.assertFalse(any('"' in arg for arg in argv))

    def test_camera_combines_av_and_applies_crop(self) -> None:
        config = RecordConfig(CameraCaptureConfig("Camera", (1280, 720), (100, 50, 800, 600)), AudioCaptureConfig("Mic"), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertIn("video=Camera:audio=Mic", argv)
        self.assertIn("crop=800:600:100:50", argv)
        self.assertIn("volume=1.000", argv)


class FallbackToOpenH264Tests(EncoderCase):
    encoders = OPENH264_ONLY

    def test_without_x264_the_previous_settings_are_used(self) -> None:
        config = RecordConfig(ScreenCaptureConfig((0, 0, 1920, 1080)), AudioCaptureConfig("Mic", 1.5), Path("out.mp4"))
        with patch("app.recorder.core.ffmpeg_record.find_ffmpeg", return_value="ffmpeg"):
            argv = build_ffmpeg_argv(config)
        self.assertEqual(argv, [
            "ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30", "-offset_x", "0", "-offset_y", "0", "-video_size", "1920x1080", "-i", "desktop",
            "-f", "dshow", "-i", "audio=Mic", "-map", "0:v", "-map", "1:a", "-af", "volume=1.500",
            *OPENH264_ARGS, "-video_track_timescale", "90000", "-c:a", "aac", "-movflags", "+faststart", "out.mp4",
        ])
        self.assertNotIn("libx264", argv)


if __name__ == "__main__":
    unittest.main()
