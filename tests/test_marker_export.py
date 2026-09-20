from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.edit_model import EditProject, MarkerKind
from app.recorder.core.marker_export import write_marker_sidecar


class MarkerExportTests(unittest.TestCase):
    def test_writes_candidate_spans_and_clips_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "recording.mp4"
            write_marker_sidecar(output, [10.0, 45.0], 60.0)
            project = EditProject.load(EditProject.sidecar_path_for(output))
        self.assertEqual([(marker.kind, marker.source) for marker in project.markers], [(MarkerKind.CANDIDATE, "color"), (MarkerKind.CANDIDATE, "color")])
        self.assertEqual([(marker.range_start, marker.range_end) for marker in project.markers], [(10.0, 40.0), (45.0, 60.0)])

    def test_press_past_end_of_file_is_dropped_not_written_as_inverted_span(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "recording.mp4"
            write_marker_sidecar(output, [3.0, 6.25], 4.89)
            project = EditProject.load(EditProject.sidecar_path_for(output))
        self.assertEqual([(marker.range_start, marker.range_end) for marker in project.markers], [(3.0, 4.89)])

    def test_all_presses_past_end_writes_no_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "recording.mp4"
            write_marker_sidecar(output, [6.25], 4.89)
            self.assertFalse(EditProject.sidecar_path_for(output).exists())

    def test_empty_break_list_does_not_create_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "recording.mp4"
            write_marker_sidecar(output, [], 10.0)
            self.assertFalse(EditProject.sidecar_path_for(output).exists())


class SidecarNameTests(unittest.TestCase):
    def test_new_sidecars_use_the_new_program_name(self) -> None:
        self.assertEqual(EditProject.sidecar_path_for(Path("lecture.mp4")).name, "lecture.mp4.readysetlecture.json")

    def test_a_sidecar_written_under_the_old_name_is_still_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "lecture.mp4"
            self.assertIsNone(EditProject.existing_sidecar_for(video))
            old = Path(temporary) / "lecture.mp4.videotrim.json"
            EditProject(video_path=str(video)).save(old)
            self.assertEqual(EditProject.existing_sidecar_for(video), old)
            EditProject.load(EditProject.existing_sidecar_for(video))  # and it loads

    def test_the_new_sidecar_wins_when_both_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "lecture.mp4"
            EditProject(video_path=str(video)).save(Path(temporary) / "lecture.mp4.videotrim.json")
            EditProject(video_path=str(video)).save(EditProject.sidecar_path_for(video))
            self.assertEqual(EditProject.existing_sidecar_for(video), EditProject.sidecar_path_for(video))


if __name__ == "__main__":
    unittest.main()
