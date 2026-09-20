from __future__ import annotations

import unittest

from PySide6.QtCore import QPoint

from app.recorder.ui.box_selector import action_button_rects, drag_rect, hit_zone, move_selection, resize_selection
from app.recorder.ui.region_picker import selection_to_physical

BOX = (100, 100, 200, 100)  # left 100, top 100, right 300, bottom 200
SCREEN = (1000, 800)


class HitZoneTests(unittest.TestCase):
    def test_corners(self) -> None:
        self.assertEqual(hit_zone(BOX, (100, 100)), "topleft")
        self.assertEqual(hit_zone(BOX, (300, 100)), "topright")
        self.assertEqual(hit_zone(BOX, (100, 200)), "bottomleft")
        self.assertEqual(hit_zone(BOX, (300, 200)), "bottomright")

    def test_edges(self) -> None:
        self.assertEqual(hit_zone(BOX, (100, 150)), "left")
        self.assertEqual(hit_zone(BOX, (300, 150)), "right")
        self.assertEqual(hit_zone(BOX, (200, 100)), "top")
        self.assertEqual(hit_zone(BOX, (200, 200)), "bottom")

    def test_grab_tolerance_works_from_just_outside_and_just_inside(self) -> None:
        self.assertEqual(hit_zone(BOX, (94, 150)), "left")
        self.assertEqual(hit_zone(BOX, (106, 150)), "left")
        self.assertEqual(hit_zone(BOX, (200, 194)), "bottom")
        self.assertEqual(hit_zone(BOX, (306, 206)), "bottomright")

    def test_inside_moves_and_outside_starts_a_new_box(self) -> None:
        self.assertEqual(hit_zone(BOX, (200, 150)), "move")
        self.assertEqual(hit_zone(BOX, (50, 50)), "new")
        self.assertEqual(hit_zone(BOX, (350, 150)), "new")
        self.assertEqual(hit_zone(BOX, (200, 260)), "new")

    def test_far_along_an_edge_line_is_not_that_edge(self) -> None:
        self.assertEqual(hit_zone(BOX, (100, 400)), "new")  # same x as the left edge, but far below the box


class ResizeSelectionTests(unittest.TestCase):
    def test_each_edge_moves_only_its_side(self) -> None:
        self.assertEqual(resize_selection(BOX, "left", 30, 99, SCREEN), (130, 100, 170, 100))
        self.assertEqual(resize_selection(BOX, "right", 30, 99, SCREEN), (100, 100, 230, 100))
        self.assertEqual(resize_selection(BOX, "top", 99, -20, SCREEN), (100, 80, 200, 120))
        self.assertEqual(resize_selection(BOX, "bottom", 99, 40, SCREEN), (100, 100, 200, 140))

    def test_corners_move_two_sides(self) -> None:
        self.assertEqual(resize_selection(BOX, "topleft", -10, -20, SCREEN), (90, 80, 210, 120))
        self.assertEqual(resize_selection(BOX, "bottomright", 50, 60, SCREEN), (100, 100, 250, 160))
        self.assertEqual(resize_selection(BOX, "topright", 50, -20, SCREEN), (100, 80, 250, 120))
        self.assertEqual(resize_selection(BOX, "bottomleft", -10, 60, SCREEN), (90, 100, 210, 160))

    def test_cannot_go_past_the_screen(self) -> None:
        self.assertEqual(resize_selection(BOX, "topleft", -500, -500, SCREEN), (0, 0, 300, 200))
        self.assertEqual(resize_selection(BOX, "bottomright", 5000, 5000, SCREEN), (100, 100, 900, 700))

    def test_cannot_shrink_below_the_minimum_or_flip_over(self) -> None:
        self.assertEqual(resize_selection(BOX, "right", -500, 0, SCREEN, min_size=16), (100, 100, 16, 100))
        self.assertEqual(resize_selection(BOX, "left", 500, 0, SCREEN, min_size=16), (284, 100, 16, 100))
        self.assertEqual(resize_selection(BOX, "bottom", 0, -500, SCREEN, min_size=16), (100, 100, 200, 16))
        self.assertEqual(resize_selection(BOX, "top", 0, 500, SCREEN, min_size=16), (100, 184, 200, 16))


class MoveSelectionTests(unittest.TestCase):
    def test_moves_by_the_drag_offset(self) -> None:
        self.assertEqual(move_selection(BOX, 50, -30, SCREEN), (150, 70, 200, 100))

    def test_stops_at_every_screen_edge_keeping_its_size(self) -> None:
        self.assertEqual(move_selection(BOX, -500, -500, SCREEN), (0, 0, 200, 100))
        self.assertEqual(move_selection(BOX, 5000, 5000, SCREEN), (800, 700, 200, 100))


class ActionButtonRectsTests(unittest.TestCase):
    def test_sit_below_the_lower_right_corner_with_the_check_rightmost(self) -> None:
        cancel, confirm = action_button_rects(BOX, SCREEN, size=30, gap=8, inset=12)
        self.assertEqual(confirm, (258, 208, 30, 30))   # 12px in from the box's right edge (300), 8px below it
        self.assertEqual(cancel, (220, 208, 30, 30))    # immediately left of the check

    def test_move_inside_the_box_when_there_is_no_room_below(self) -> None:
        cancel, confirm = action_button_rects((100, 700, 200, 100), SCREEN, size=30, gap=8, inset=12)
        self.assertEqual(confirm, (258, 762, 30, 30))
        self.assertEqual(cancel, (220, 762, 30, 30))

    def test_stay_on_screen_when_the_box_hugs_the_left_edge(self) -> None:
        cancel, confirm = action_button_rects((0, 100, 40, 50), SCREEN, size=30, gap=8, inset=12)
        self.assertEqual(cancel[0], 0)
        self.assertEqual(confirm[0], 38)

    def test_buttons_stay_clear_of_the_bottom_right_corner_grab_zone(self) -> None:
        _cancel, confirm = action_button_rects(BOX, SCREEN)
        corner_zone_left = 300 - 8  # hit_zone grabs the corner within 8px
        self.assertLess(confirm[0] + confirm[2], corner_zone_left)


class DragRectTests(unittest.TestCase):
    def test_rect_is_exactly_as_large_as_the_drag(self) -> None:
        self.assertEqual(drag_rect(QPoint(300, 200), QPoint(900, 600)).getRect(), (300, 200, 600, 400))

    def test_dragging_up_and_left_gives_the_same_rect(self) -> None:
        self.assertEqual(drag_rect(QPoint(900, 600), QPoint(300, 200)).getRect(), (300, 200, 600, 400))
        self.assertEqual(drag_rect(QPoint(900, 200), QPoint(300, 600)).getRect(), (300, 200, 600, 400))

    def test_a_click_without_moving_is_empty(self) -> None:
        self.assertEqual(drag_rect(QPoint(50, 60), QPoint(50, 60)).getRect(), (50, 60, 0, 0))


class SelectionToPhysicalTests(unittest.TestCase):
    def test_200_percent_display_doubles_the_selection(self) -> None:
        # 1440x960 on-screen picker over a 2880x1920 physical monitor
        self.assertEqual(selection_to_physical((100, 100, 400, 300), (1440, 960), (0, 0, 2880, 1920)), (200, 200, 800, 600))

    def test_unscaled_display_is_identity(self) -> None:
        self.assertEqual(selection_to_physical((10, 20, 300, 200), (1920, 1080), (0, 0, 1920, 1080)), (10, 20, 300, 200))

    def test_monitor_left_of_primary_keeps_its_negative_origin(self) -> None:
        self.assertEqual(selection_to_physical((10, 20, 100, 50), (1920, 1080), (-1920, 0, 1920, 1080)), (-1910, 20, 100, 50))

    def test_monitor_below_primary_with_scaling_offsets_from_its_own_origin(self) -> None:
        self.assertEqual(selection_to_physical((50, 50, 200, 100), (1280, 720), (500, 1080, 2560, 1440)), (600, 1180, 400, 200))

    def test_whole_view_selects_whole_monitor(self) -> None:
        self.assertEqual(selection_to_physical((0, 0, 1440, 960), (1440, 960), (0, 0, 2880, 1920)), (0, 0, 2880, 1920))

    def test_selection_reaching_the_far_edge_is_clamped_to_the_monitor(self) -> None:
        # rounding each of x and width separately could overshoot by a pixel; it must not
        x, y, w, h = selection_to_physical((1000, 600, 440, 360), (1441, 961), (0, 0, 2880, 1920))
        self.assertLessEqual(x + w, 2880)
        self.assertLessEqual(y + h, 1920)

    def test_fractional_scale_rounds_to_whole_pixels(self) -> None:
        # 125% scaling: 1536x864 view over a 1920x1080 monitor
        self.assertEqual(selection_to_physical((100, 100, 401, 301), (1536, 864), (0, 0, 1920, 1080)), (125, 125, 501, 376))

    def test_degenerate_selection_is_at_least_one_pixel(self) -> None:
        _x, _y, w, h = selection_to_physical((5, 5, 0, 0), (100, 100), (0, 0, 100, 100))
        self.assertGreaterEqual(w, 1)
        self.assertGreaterEqual(h, 1)


if __name__ == "__main__":
    unittest.main()
