from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.recorder.core import devices
from app.recorder.core.devices import _parse_dshow_device_list, _parse_dshow_video_options, pick_camera_native_size


class DshowParsingTests(unittest.TestCase):
    def test_devices_use_alternative_name_when_present(self) -> None:
        stderr = '''[dshow @ 1] DirectShow video devices
[dshow @ 1]  "Camera One"
[dshow @ 1]     Alternative name "@device_pnp_camera"
[dshow @ 1] DirectShow audio devices
[dshow @ 1]  "Microphone One"
'''
        cameras, mics = _parse_dshow_device_list(stderr)
        self.assertEqual(cameras[0].name, "Camera One")
        self.assertEqual(cameras[0].capture_id, "@device_pnp_camera")
        self.assertEqual(mics[0].capture_id, "Microphone One")

    def test_devices_parse_typed_lines_from_modern_ffmpeg(self) -> None:
        stderr = '''[in#0 @ 000001] "Camera One" (video)
[in#0 @ 000001]   Alternative name "@device_pnp_camera_one"
[in#0 @ 000001] "Camera Two" (video)
[in#0 @ 000001]   Alternative name "@device_pnp_camera_two"
[in#0 @ 000001] "Virtual Camera" (none)
[in#0 @ 000001]   Alternative name "@device_sw_virtual"
[in#0 @ 000001] "Microphone One" (audio)
[in#0 @ 000001]   Alternative name "@device_cm_mic_one"
Error opening input file dummy.
'''
        cameras, mics = _parse_dshow_device_list(stderr)
        self.assertEqual([(c.name, c.capture_id) for c in cameras],
                         [("Camera One", "@device_pnp_camera_one"), ("Camera Two", "@device_pnp_camera_two")])
        self.assertEqual([(m.name, m.capture_id) for m in mics], [("Microphone One", "@device_cm_mic_one")])

    def test_typed_device_without_alternative_name_falls_back_to_name(self) -> None:
        cameras, mics = _parse_dshow_device_list('[in#0 @ 1] "Camera One" (video)\n[in#0 @ 1] "Mic One" (audio)\n')
        self.assertEqual(cameras[0].capture_id, "Camera One")
        self.assertEqual(mics[0].capture_id, "Mic One")

    def test_options_are_deduplicated_in_report_order(self) -> None:
        result = _parse_dshow_video_options("min s=1280x720 max s=1280x720\n s=1920x1080\n s=1280x720")
        self.assertEqual(result, [(1280, 720), (1920, 1080)])

    def test_camera_sizes_are_asked_for_once_and_then_remembered(self) -> None:
        devices._video_options_cache.clear()
        answer = SimpleNamespace(stderr=b"pixel_format=nv12  min s=1280x720 fps=5 max s=1920x1080 fps=30")
        with patch.object(devices, "find_ffmpeg", return_value="ffmpeg"), patch.object(devices.subprocess, "run", return_value=answer) as run:
            first = devices.list_dshow_video_options("cam-1")
            second = devices.list_dshow_video_options("cam-1")
        self.assertEqual((first, second), ([(1280, 720), (1920, 1080)],) * 2)
        self.assertEqual(run.call_count, 1)
        devices._video_options_cache.clear()

    def test_a_failed_or_empty_answer_is_not_remembered(self) -> None:
        devices._video_options_cache.clear()
        with patch.object(devices, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(devices.subprocess, "run", return_value=SimpleNamespace(stderr=b"Could not find video device")) as run:
            self.assertEqual(devices.list_dshow_video_options("cam-2"), [])
            self.assertEqual(devices.list_dshow_video_options("cam-2"), [])
        self.assertEqual(run.call_count, 2)  # asked again: the camera may simply have been busy

    def test_a_caller_cannot_corrupt_the_remembered_answer(self) -> None:
        devices._video_options_cache.clear()
        answer = SimpleNamespace(stderr=b"s=640x480 s=1280x720")
        with patch.object(devices, "find_ffmpeg", return_value="ffmpeg"), patch.object(devices.subprocess, "run", return_value=answer):
            devices.list_dshow_video_options("cam-3").append((1, 1))
            self.assertEqual(devices.list_dshow_video_options("cam-3"), [(640, 480), (1280, 720)])
        devices._video_options_cache.clear()

    def test_native_size_prefers_largest_size_within_the_1080p_cap(self) -> None:
        self.assertEqual(pick_camera_native_size([(640, 480), (1280, 720), (1920, 1080)]), (1920, 1080))
        self.assertEqual(pick_camera_native_size([(640, 480), (1280, 720)]), (1280, 720))
        self.assertEqual(pick_camera_native_size([(7680, 4320)]), (7680, 4320))  # nothing within the cap: the closest available
        self.assertEqual(pick_camera_native_size([]), (1920, 1080))

    def test_a_camera_that_offers_4k_gets_4k(self) -> None:
        sizes = [(640, 480), (1280, 720), (1920, 1080), (3840, 2160)]
        self.assertEqual(pick_camera_native_size(sizes), (3840, 2160))
        self.assertEqual(pick_camera_native_size(sizes, allow_uhd=False), (1920, 1080))

    def test_odd_taller_modes_are_not_mistaken_for_an_upgrade(self) -> None:
        # The Surface camera lists 1920x1440 (4:3, more pixels than 1080p): its recordings must stay 16:9 1080p.
        self.assertEqual(pick_camera_native_size([(640, 480), (1280, 720), (1920, 1080), (1920, 1280), (1920, 1440)]), (1920, 1080))
        self.assertEqual(pick_camera_native_size([(1920, 1080), (2560, 1440)]), (1920, 1080))  # 1440p is not opted into either


# What a 4K USB camera can report: uncompressed formats only reach 4K at a few frames a second, MJPEG reaches 30.
USB_4K_CAMERA = b"""[dshow @ 0000] DirectShow video device options (from video devices)
[dshow @ 0000]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000]   pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=30
[dshow @ 0000]   pixel_format=yuyv422  min s=1920x1080 fps=5 max s=1920x1080 fps=5
[dshow @ 0000]   pixel_format=yuyv422  min s=3840x2160 fps=5 max s=3840x2160 fps=5
[dshow @ 0000]   vcodec=mjpeg  min s=640x480 fps=5 max s=640x480 fps=30
[dshow @ 0000]   vcodec=mjpeg  min s=1920x1080 fps=5 max s=1920x1080 fps=30
[dshow @ 0000]   vcodec=mjpeg  min s=3840x2160 fps=5 max s=3840x2160 fps=30
"""


# A 4K USB camera with its own H.264 encoder (the ELP family advertises H.264, MJPEG and YUY2): 4K is H.264 at 30 fps, MJPEG only at 15.
H264_4K_CAMERA = b"""[dshow @ 0000] DirectShow video device options (from video devices)
[dshow @ 0000]  Pin "Capture" (alternative pin name "0")
[dshow @ 0000]   pixel_format=yuyv422  min s=640x480 fps=30 max s=640x480 fps=30
[dshow @ 0000]   vcodec=mjpeg  min s=640x480 fps=30 max s=640x480 fps=30
[dshow @ 0000]   vcodec=mjpeg  min s=1920x1080 fps=30 max s=1920x1080 fps=30
[dshow @ 0000]   vcodec=mjpeg  min s=3840x2160 fps=15 max s=3840x2160 fps=15
[dshow @ 0000]   vcodec=h264  min s=1920x1080 fps=30 max s=1920x1080 fps=30
[dshow @ 0000]   vcodec=h264  min s=3840x2160 fps=30 max s=3840x2160 fps=30
"""


# An ELP-style 4K camera that compresses in hardware in H.264 AND H.265: both do 4K at 30 fps; MJPEG only manages 4K at 15.
ELP_STYLE_CAMERA = b"""[dshow @ 0000] DirectShow video device options (from video devices)
[dshow @ 0000]   pixel_format=yuyv422  min s=640x480 fps=30 max s=640x480 fps=30
[dshow @ 0000]   vcodec=mjpeg  min s=1920x1080 fps=30 max s=1920x1080 fps=30
[dshow @ 0000]   vcodec=mjpeg  min s=3840x2160 fps=15 max s=3840x2160 fps=15
[dshow @ 0000]   vcodec=h264  min s=1920x1080 fps=30 max s=1920x1080 fps=30
[dshow @ 0000]   vcodec=h264  min s=3840x2160 fps=30 max s=3840x2160 fps=30
[dshow @ 0000]   vcodec=hevc  min s=1920x1080 fps=30 max s=1920x1080 fps=30
[dshow @ 0000]   vcodec=hevc  min s=3840x2160 fps=30 max s=3840x2160 fps=30
"""

# A laptop camera (this Surface): uncompressed video only, with an odd taller 1920x1440 mode.
SURFACE_STYLE_CAMERA = b"""[dshow @ 0000]   pixel_format=nv12  min s=640x360 fps=15 max s=640x360 fps=30
[dshow @ 0000]   pixel_format=yuyv422  min s=640x360 fps=15 max s=640x360 fps=30
[dshow @ 0000]   pixel_format=nv12  min s=1920x1080 fps=30 max s=1920x1080 fps=30
[dshow @ 0000]   pixel_format=yuyv422  min s=1920x1080 fps=30 max s=1920x1080 fps=30
[dshow @ 0000]   pixel_format=nv12  min s=1920x1440 fps=30 max s=1920x1440 fps=30
"""

class CameraFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        devices._video_options_cache.clear()
        self.addCleanup(devices._video_options_cache.clear)

    def test_each_line_keeps_its_format_size_and_rate(self) -> None:
        modes = devices._parse_dshow_modes(H264_4K_CAMERA)
        self.assertIn(devices.CameraMode(3840, 2160, 30.0, "h264"), modes)
        self.assertIn(devices.CameraMode(3840, 2160, 15.0, "mjpeg"), modes)
        self.assertIn(devices.CameraMode(640, 480, 30.0, "yuyv422"), modes)
        self.assertEqual(len(modes), 6)

    def test_lines_that_name_no_format_or_rate_still_give_sizes(self) -> None:
        self.assertEqual(devices._parse_dshow_modes("s=640x480 s=1280x720"), [devices.CameraMode(640, 480, None, None), devices.CameraMode(1280, 720, None, None)])

    def test_the_formats_a_camera_offers_are_listed_in_order_and_only_if_fast_enough(self) -> None:
        self.assertEqual(devices.available_camera_formats(devices._parse_dshow_modes(H264_4K_CAMERA)), ["h264", "mjpeg", "raw"])
        self.assertEqual(devices.available_camera_formats(devices._parse_dshow_modes(ELP_STYLE_CAMERA)), ["h264", "hevc", "mjpeg", "raw"])
        self.assertEqual(devices.available_camera_formats(devices._parse_dshow_modes(SURFACE_STYLE_CAMERA)), ["raw"])
        self.assertEqual(devices.available_camera_formats(devices._parse_dshow_modes(H264_4K_CAMERA), fps=60), [])  # nothing reaches 60 fps
        self.assertEqual(devices.available_camera_formats(devices._parse_dshow_modes("vcodec=h264 s=3840x2160")), [])  # no rate given: not relied on
        self.assertEqual(devices.available_camera_formats([]), [])

    def test_automatic_takes_the_cameras_own_h264_at_its_best_size(self) -> None:
        plan = devices.plan_camera_input(devices._parse_dshow_modes(H264_4K_CAMERA))
        self.assertEqual((plan.size, plan.input_format), ((3840, 2160), "h264"))

    def test_automatic_prefers_h264_over_h265_and_takes_h265_when_that_is_all_there_is(self) -> None:
        both = devices.plan_camera_input(devices._parse_dshow_modes(ELP_STYLE_CAMERA))
        self.assertEqual((both.size, both.input_format), ((3840, 2160), "h264"))
        only_h265 = devices._parse_dshow_modes(b"vcodec=hevc  min s=3840x2160 fps=30 max s=3840x2160 fps=30\nvcodec=mjpeg  min s=1920x1080 fps=30 max s=1920x1080 fps=30\n")
        plan = devices.plan_camera_input(only_h265)
        self.assertEqual((plan.size, plan.input_format), ((3840, 2160), "hevc"))

    def test_automatic_uses_the_cameras_encoder_at_1080p_too(self) -> None:
        modes = devices._parse_dshow_modes(b"vcodec=h264  min s=1920x1080 fps=30 max s=1920x1080 fps=30\nvcodec=mjpeg  min s=1920x1080 fps=30 max s=1920x1080 fps=30\n")
        plan = devices.plan_camera_input(modes)
        self.assertEqual((plan.size, plan.input_format), ((1920, 1080), "h264"))

    def test_automatic_does_not_pick_4k_that_the_computer_would_have_to_encode(self) -> None:
        # MJPEG-only 4K can't be decoded and encoded in real time (single-threaded MJPEG decoder): stay at 1080p.
        plan = devices.plan_camera_input(devices._parse_dshow_modes(USB_4K_CAMERA))
        self.assertEqual((plan.size, plan.input_format), ((1920, 1080), None))
        plan = devices.plan_camera_input(devices._parse_dshow_modes(b"vcodec=h264  min s=1920x1080 fps=30 max s=1920x1080 fps=30\nvcodec=mjpeg  min s=3840x2160 fps=30 max s=3840x2160 fps=30\n"))
        self.assertEqual((plan.size, plan.input_format), ((1920, 1080), "h264"))  # 4K exists, but not from the camera's encoder

    def test_a_camera_with_only_uncompressed_video_is_left_to_directshow(self) -> None:
        plan = devices.plan_camera_input(devices._parse_dshow_modes(SURFACE_STYLE_CAMERA))
        self.assertEqual((plan.size, plan.input_format), ((1920, 1080), None))  # and never the odd 1920x1440 mode
        self.assertEqual(devices.plan_camera_input([]).input_format, None)

    def test_choosing_a_format_gives_the_biggest_size_that_format_offers(self) -> None:
        modes = devices._parse_dshow_modes(ELP_STYLE_CAMERA)
        self.assertEqual(devices.plan_camera_input(modes, "hevc"), devices.CameraInput((3840, 2160), "hevc"))
        self.assertEqual(devices.plan_camera_input(modes, "h264"), devices.CameraInput((3840, 2160), "h264"))
        self.assertEqual(devices.plan_camera_input(modes, "mjpeg"), devices.CameraInput((1920, 1080), "mjpeg"))  # its 4K is only 15 fps

    def test_the_person_can_choose_mjpeg_4k_knowingly(self) -> None:
        plan = devices.plan_camera_input(devices._parse_dshow_modes(USB_4K_CAMERA), "mjpeg")
        self.assertEqual((plan.size, plan.input_format), ((3840, 2160), "mjpeg"))

    def test_an_uncompressed_choice_names_the_pixel_format_to_ask_for(self) -> None:
        plan = devices.plan_camera_input(devices._parse_dshow_modes(SURFACE_STYLE_CAMERA), "raw")
        self.assertEqual((plan.size, plan.input_format), ((1920, 1080), "pixel:nv12"))

    def test_nv12_is_preferred_even_when_the_camera_lists_yuy2_first(self) -> None:
        yuy2_first = b"pixel_format=yuyv422  min s=1920x1080 fps=30 max s=1920x1080 fps=30\npixel_format=nv12  min s=1920x1080 fps=30 max s=1920x1080 fps=30\n"
        self.assertEqual(devices.plan_camera_input(devices._parse_dshow_modes(yuy2_first), "raw").input_format, "pixel:nv12")

    def test_yuy2_is_used_when_that_is_all_the_camera_has(self) -> None:
        only_yuy2 = b"pixel_format=yuyv422  min s=1280x720 fps=30 max s=1280x720 fps=30\n"
        self.assertEqual(devices.plan_camera_input(devices._parse_dshow_modes(only_yuy2), "raw").input_format, "pixel:yuyv422")

    def test_an_unusual_uncompressed_format_is_still_requested_by_name(self) -> None:
        odd = b"pixel_format=uyvy422  min s=1280x720 fps=30 max s=1280x720 fps=30\n"
        self.assertEqual(devices.plan_camera_input(devices._parse_dshow_modes(odd), "raw").input_format, "pixel:uyvy422")

    def test_a_format_the_camera_does_not_offer_falls_back_to_automatic(self) -> None:
        modes = devices._parse_dshow_modes(SURFACE_STYLE_CAMERA)
        for choice in ("h264", "hevc", "mjpeg", "nonsense"):
            self.assertEqual(devices.plan_camera_input(modes, choice), devices.plan_camera_input(modes, "auto"))

    def test_the_list_offers_automatic_then_each_format_with_the_size_it_records(self) -> None:
        choices = devices.camera_format_choices(devices._parse_dshow_modes(ELP_STYLE_CAMERA))
        self.assertEqual([key for key, _ in choices], ["auto", "h264", "hevc", "mjpeg", "raw"])
        labels = dict(choices)
        self.assertEqual(labels["auto"], "Automatic: H.264 from the camera, 3840×2160")
        self.assertEqual(labels["h264"], "H.264: the camera encodes, 3840×2160")
        self.assertEqual(labels["hevc"], "H.265: the camera encodes, 3840×2160")
        self.assertEqual(labels["mjpeg"], "MJPEG: the computer encodes, 1920×1080")
        self.assertEqual(labels["raw"], "Uncompressed: the computer encodes, 640×480")

    def test_a_camera_with_nothing_but_uncompressed_video_still_gets_a_sensible_list(self) -> None:
        choices = devices.camera_format_choices(devices._parse_dshow_modes(SURFACE_STYLE_CAMERA))
        self.assertEqual([key for key, _ in choices], ["auto", "raw"])
        self.assertEqual(choices[0][1], "Automatic: the computer encodes, 1920×1080")
        self.assertEqual(devices.camera_format_choices([]), [("auto", "Automatic: the computer encodes, 1920×1080")])

    def test_modes_are_only_read_from_the_camera_when_not_already_known(self) -> None:
        self.assertIsNone(devices.cached_camera_modes("cam"))
        with patch.object(devices, "find_ffmpeg", return_value="ffmpeg"), patch.object(devices.subprocess, "run", return_value=SimpleNamespace(stderr=H264_4K_CAMERA)):
            devices.list_dshow_camera_modes("cam")
        self.assertEqual(len(devices.cached_camera_modes("cam")), 6)
    def test_the_cameras_modes_are_asked_for_once(self) -> None:
        with patch.object(devices, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(devices.subprocess, "run", return_value=SimpleNamespace(stderr=H264_4K_CAMERA)) as run:
            first = devices.list_dshow_camera_modes("cam")
            sizes = devices.list_dshow_video_options("cam")
            second = devices.list_dshow_camera_modes("cam")
        self.assertEqual(first, second)
        self.assertIn((3840, 2160), sizes)
        self.assertEqual(run.call_count, 1)


class CameraFrameRateTests(unittest.TestCase):
    def setUp(self) -> None:
        devices._video_options_cache.clear()
        self.addCleanup(devices._video_options_cache.clear)

    def options(self, stderr: bytes, **kwargs):
        with patch.object(devices, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(devices.subprocess, "run", return_value=SimpleNamespace(stderr=stderr)):
            return devices.list_dshow_video_options("cam", **kwargs)

    def test_the_fastest_rate_each_size_is_offered_at_is_read(self) -> None:
        rates = devices._parse_dshow_size_rates(USB_4K_CAMERA)
        self.assertEqual(rates, [(640, 480, 30.0), (1920, 1080, 30.0), (3840, 2160, 30.0)])  # the MJPEG rate wins over the 5 fps one

    def test_a_4k_camera_that_does_30_fps_offers_4k_and_it_is_the_one_picked(self) -> None:
        sizes = self.options(USB_4K_CAMERA)
        self.assertIn((3840, 2160), sizes)
        self.assertEqual(pick_camera_native_size(sizes), (3840, 2160))

    def test_a_size_that_is_only_slow_is_not_offered_when_faster_ones_exist(self) -> None:
        slow_4k = b"""pixel_format=yuyv422  min s=1280x720 fps=5 max s=1280x720 fps=30
pixel_format=yuyv422  min s=1920x1080 fps=5 max s=1920x1080 fps=30
pixel_format=yuyv422  min s=3840x2160 fps=5 max s=3840x2160 fps=15
"""
        sizes = self.options(slow_4k)
        self.assertEqual(sizes, [(1280, 720), (1920, 1080)])  # 4K only at 15 fps would fail at the requested 30
        self.assertEqual(pick_camera_native_size(sizes), (1920, 1080))

    def test_asking_for_no_rate_returns_every_size(self) -> None:
        self.assertEqual(self.options(USB_4K_CAMERA, min_fps=None), [(640, 480), (1920, 1080), (3840, 2160)])

    def test_if_no_size_reaches_the_rate_all_are_offered_rather_than_none(self) -> None:
        slow = b"pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=15\npixel_format=yuyv422  min s=1280x720 fps=5 max s=1280x720 fps=10\n"
        self.assertEqual(self.options(slow), [(640, 480), (1280, 720)])

    def test_sizes_without_any_rate_information_are_kept(self) -> None:
        self.assertEqual(self.options(b"s=640x480 s=1280x720"), [(640, 480), (1280, 720)])

    def test_the_answer_is_remembered_whatever_rate_was_asked_for(self) -> None:
        with patch.object(devices, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(devices.subprocess, "run", return_value=SimpleNamespace(stderr=USB_4K_CAMERA)) as run:
            devices.list_dshow_video_options("cam", min_fps=None)
            self.assertIn((3840, 2160), devices.list_dshow_video_options("cam"))
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
