from __future__ import annotations

import json
import unittest

from app.recorder.core.clicker import (
    ACTION_MARK, ACTION_PAUSE, ButtonLearner, ClickerConfig, ClickerMatcher, RawEvent, describe_signature, hid_pattern_matches,
    hid_patterns_overlap, hid_signature, is_valid_signature, key_signature, merge_hid_samples, mouse_signature,
)

DEVICE = "05ac:022c:8&243a709b&0"
OTHER = "045e:0c0f:aabbccddeeff"


def key(code: str, pressed: bool, device: str | None = DEVICE) -> RawEvent:
    return RawEvent(device, "key", code, pressed)


def report(hex_bytes: str, device: str | None = DEVICE) -> RawEvent:
    return RawEvent(device, "hid", hex_bytes, None)


class SignatureTests(unittest.TestCase):
    def test_keys_are_named_for_a_person(self) -> None:
        self.assertEqual(describe_signature(key_signature(0x22)), "Key: Page Down")
        self.assertEqual(describe_signature(key_signature(0x21)), "Key: Page Up")
        self.assertEqual(describe_signature(key_signature(0xAF)), "Key: Volume Up")
        self.assertEqual(describe_signature(key_signature(0x87)), "Key: F24")
        self.assertEqual(describe_signature(key_signature(0x70)), "Key: F1")
        self.assertEqual(describe_signature(key_signature(0x41)), "Key: A")
        self.assertEqual(describe_signature(key_signature(0x35)), "Key: 5")
        self.assertEqual(describe_signature(key_signature(0xE8)), "Key: 0xE8")  # unknown keys still say something
        self.assertEqual(describe_signature(key_signature(0x22, extended=True)), "Key: Page Down")

    def test_mouse_and_remote_buttons_are_described(self) -> None:
        self.assertEqual(describe_signature(mouse_signature("left")), "Left mouse button")
        self.assertEqual(describe_signature(mouse_signature("x2")), "Mouse forward button")
        self.assertEqual(describe_signature(hid_signature(bytes([0x01, 0xE9, 0x00]))), "Remote button (01 e9 00)")
        self.assertEqual(describe_signature("hid:01????"), "Remote button (01 ?? ??)")

    def test_only_well_formed_signatures_are_accepted(self) -> None:
        for good in ("key:22", "key:22e", "mouse:left", "hid:01e900", "hid:01????", "hid:ff"):
            self.assertTrue(is_valid_signature(good), good)
        for bad in ("", "key:", "key", "hid:abc", "hid:zz", "hid:", "mouse:../x", "kbd:22", "key:22 ", None, 5, "hid:" + "00" * 65):
            self.assertFalse(is_valid_signature(bad), repr(bad))


class HidPatternTests(unittest.TestCase):
    def test_identical_presses_give_an_exact_pattern(self) -> None:
        self.assertEqual(merge_hid_samples("01e900", "01e900"), "01e900")

    def test_bytes_that_change_between_presses_become_wildcards(self) -> None:
        self.assertEqual(merge_hid_samples("0101a000", "0102a000"), "01??a000")

    def test_two_presses_that_cannot_be_the_same_button_give_nothing(self) -> None:
        self.assertIsNone(merge_hid_samples("01e900", "01e9"))  # different lengths
        self.assertIsNone(merge_hid_samples("0102", "0304"))  # nothing in common: would match everything
        self.assertIsNone(merge_hid_samples("", ""))

    def test_matching_and_overlap(self) -> None:
        self.assertTrue(hid_pattern_matches("01??a0", "0155a0"))
        self.assertFalse(hid_pattern_matches("01??a0", "0255a0"))
        self.assertFalse(hid_pattern_matches("01??a0", "0155a0ff"))
        self.assertTrue(hid_patterns_overlap("01??a0", "0155??"))
        self.assertFalse(hid_patterns_overlap("01e900", "01ea00"))


class MatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = ClickerMatcher(DEVICE, {ACTION_PAUSE: key_signature(0x22), ACTION_MARK: key_signature(0x21)})

    def test_a_bound_button_triggers_its_action(self) -> None:
        self.assertEqual(self.matcher.feed(key("22", True), 0.0), ACTION_PAUSE)
        self.assertEqual(self.matcher.feed(key("22", False), 0.1), None)
        self.assertEqual(self.matcher.feed(key("21", True), 1.0), ACTION_MARK)

    def test_unbound_buttons_do_nothing(self) -> None:
        self.assertIsNone(self.matcher.feed(key("23", True), 0.0))

    def test_the_same_button_on_another_device_does_nothing(self) -> None:
        # e.g. Page Down on the ordinary keyboard must not pause the recording
        self.assertIsNone(self.matcher.feed(key("22", True, device=OTHER), 0.0))
        self.assertIsNone(self.matcher.feed(key("22", True, device=None), 0.0))

    def test_holding_a_button_counts_once(self) -> None:
        self.assertEqual(self.matcher.feed(key("22", True), 0.0), ACTION_PAUSE)
        for step in range(1, 30):  # key repeat: many "presses" with no release
            self.assertIsNone(self.matcher.feed(key("22", True), step * 0.05))

    def test_press_release_press_is_two_presses(self) -> None:
        self.assertEqual(self.matcher.feed(key("22", True), 0.0), ACTION_PAUSE)
        self.matcher.feed(key("22", False), 0.1)
        self.assertEqual(self.matcher.feed(key("22", True), 0.2), ACTION_PAUSE)

    def test_a_lost_release_does_not_jam_the_button(self) -> None:
        # e.g. the clicker dropped its connection while a key was down
        self.assertEqual(self.matcher.feed(key("22", True), 0.0), ACTION_PAUSE)
        self.assertEqual(self.matcher.feed(key("22", True), 5.0), ACTION_PAUSE)

    def test_mouse_buttons_can_be_bound(self) -> None:
        matcher = ClickerMatcher(DEVICE, {ACTION_MARK: mouse_signature("middle")})
        self.assertEqual(matcher.feed(RawEvent(DEVICE, "mouse", "middle", True), 0.0), ACTION_MARK)
        self.assertIsNone(matcher.feed(RawEvent(DEVICE, "mouse", "left", True), 1.0))

    def test_a_key_and_a_raw_report_with_the_same_text_are_not_confused(self) -> None:
        matcher = ClickerMatcher(DEVICE, {ACTION_PAUSE: "hid:22"})
        self.assertIsNone(matcher.feed(key("22", True), 0.0))


class HidMatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = ClickerMatcher(DEVICE, {ACTION_PAUSE: "hid:01??00"})

    def test_a_press_is_the_first_report_of_a_burst(self) -> None:
        self.assertEqual(self.matcher.feed(report("010500"), 0.0), ACTION_PAUSE)
        self.assertIsNone(self.matcher.feed(report("010500"), 0.05))  # the rest of the same press
        self.assertIsNone(self.matcher.feed(report("010900"), 0.10))
        self.assertEqual(self.matcher.feed(report("010700"), 1.0), ACTION_PAUSE)  # the next press

    def test_a_release_report_that_arrives_alone_does_not_trigger(self) -> None:
        self.assertEqual(self.matcher.feed(report("010500"), 0.0), ACTION_PAUSE)
        self.assertIsNone(self.matcher.feed(report("02ff00"), 0.8))  # button held, then released: a different report

    def test_a_held_button_that_keeps_reporting_counts_once(self) -> None:
        self.assertEqual(self.matcher.feed(report("010500"), 0.0), ACTION_PAUSE)
        for step in range(1, 40):
            self.assertIsNone(self.matcher.feed(report("010500"), step * 0.1))

    def test_reports_from_other_devices_do_not_disturb_the_burst_timing(self) -> None:
        self.assertEqual(self.matcher.feed(report("010500"), 0.0), ACTION_PAUSE)
        self.assertIsNone(self.matcher.feed(report("010500", device=OTHER), 0.5))
        self.assertEqual(self.matcher.feed(report("010500"), 0.6), ACTION_PAUSE)


class LearnerTests(unittest.TestCase):
    def test_a_key_needs_one_press(self) -> None:
        learner = ButtonLearner(DEVICE)
        self.assertEqual(learner.feed(key("22", False), 0.0).state, "waiting")  # a release is not a press
        step = learner.feed(key("22", True), 0.1)
        self.assertEqual((step.state, step.signature), ("done", "key:22"))

    def test_other_devices_are_ignored_while_learning(self) -> None:
        learner = ButtonLearner(DEVICE)
        self.assertEqual(learner.feed(key("22", True, device=OTHER), 0.0).state, "waiting")
        self.assertEqual(learner.feed(RawEvent(None, "key", "22", True), 0.1).state, "waiting")

    def test_a_remote_button_needs_two_presses_and_finds_what_changes(self) -> None:
        learner = ButtonLearner(DEVICE)
        first = learner.feed(report("010500"), 0.0)
        self.assertEqual((first.state, first.signature), ("again", None))
        self.assertEqual(learner.feed(report("000000"), 0.1).state, "waiting")  # the tail of the first press
        second = learner.feed(report("010900"), 1.0)
        self.assertEqual((second.state, second.signature), ("done", "hid:01??00"))

    def test_the_tail_of_a_press_is_not_mistaken_for_the_second_press(self) -> None:
        learner = ButtonLearner(DEVICE)
        self.assertEqual(learner.feed(report("010500"), 0.0).state, "again")
        self.assertEqual(learner.feed(report("010000"), 0.05).state, "waiting")
        self.assertEqual(learner.feed(report("010500"), 0.30).state, "waiting")

    def test_two_different_buttons_start_again(self) -> None:
        learner = ButtonLearner(DEVICE)
        self.assertEqual(learner.feed(report("0105"), 0.0).state, "again")
        self.assertEqual(learner.feed(report("010500"), 1.0).state, "mismatch")  # different length
        step = learner.feed(report("010500"), 2.0)  # the newest press became the first sample
        self.assertEqual((step.state, step.signature), ("done", "hid:010500"))


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ClickerConfig()
        self.config.select_device(DEVICE, "T01")

    def test_nothing_is_ready_until_a_button_is_set(self) -> None:
        self.assertFalse(ClickerConfig().is_ready())
        self.assertFalse(self.config.is_ready())
        self.assertIsNone(self.config.bind(ACTION_PAUSE, "key:22"))
        self.assertTrue(self.config.is_ready())

    def test_binding_needs_a_device_and_a_valid_button(self) -> None:
        self.assertIsNotNone(ClickerConfig().bind(ACTION_PAUSE, "key:22"))
        self.assertIsNotNone(self.config.bind(ACTION_PAUSE, "nonsense"))
        self.assertIsNotNone(self.config.bind("explode", "key:22"))
        self.assertEqual(self.config.selected_bindings(), {})

    def test_one_button_cannot_do_two_things(self) -> None:
        self.config.bind(ACTION_PAUSE, "key:22")
        problem = self.config.bind(ACTION_MARK, "key:22")
        self.assertIn("already used for Record / Pause", problem)
        self.assertEqual(self.config.selected_bindings(), {ACTION_PAUSE: "key:22"})
        self.assertIsNone(self.config.bind(ACTION_PAUSE, "key:22"))  # re-learning the same action's own button is fine

    def test_overlapping_remote_buttons_count_as_the_same_button(self) -> None:
        self.config.bind(ACTION_PAUSE, "hid:01??00")
        self.assertIsNotNone(self.config.bind(ACTION_MARK, "hid:010500"))
        self.assertIsNone(self.config.bind(ACTION_MARK, "hid:02ff00"))

    def test_rebinding_replaces_and_clearing_removes(self) -> None:
        self.config.bind(ACTION_PAUSE, "key:22")
        self.config.bind(ACTION_PAUSE, "key:21")
        self.assertEqual(self.config.selected_bindings(), {ACTION_PAUSE: "key:21"})
        self.config.clear(ACTION_PAUSE)
        self.config.clear(ACTION_PAUSE)  # harmless twice
        self.assertEqual(self.config.selected_bindings(), {})

    def test_each_device_keeps_its_own_buttons(self) -> None:
        self.config.bind(ACTION_PAUSE, "key:22")
        self.config.select_device(OTHER, "Pen")
        self.assertEqual(self.config.selected_bindings(), {})
        self.config.bind(ACTION_PAUSE, "key:87")
        self.config.select_device(DEVICE, "T01")
        self.assertEqual(self.config.selected_bindings(), {ACTION_PAUSE: "key:22"})

    def test_it_survives_being_saved_and_loaded(self) -> None:
        self.config.bind(ACTION_PAUSE, "key:22")
        self.config.bind(ACTION_MARK, "hid:01??00")
        loaded = ClickerConfig.from_json(self.config.to_json())
        self.assertEqual((loaded.device_key, loaded.device_name, loaded.selected_bindings()),
                         (DEVICE, "T01", {ACTION_PAUSE: "key:22", ACTION_MARK: "hid:01??00"}))

    def test_unreadable_saved_data_gives_an_empty_setup_not_a_crash(self) -> None:
        for text in ("", "not json", "[]", "5", '{"device": 5, "bindings": []}', '{"bindings": {"x": 5}}', None):
            loaded = ClickerConfig.from_json(text)
            self.assertEqual((loaded.device_key, loaded.selected_bindings()), (None, {}), repr(text))

    def test_bad_entries_are_dropped_but_good_ones_kept(self) -> None:
        text = json.dumps({"device": {"key": DEVICE, "name": "T01"},
                           "bindings": {DEVICE: {"pause": "key:22", "mark": "junk", "explode": "key:21"}, 5: {"pause": "key:1"}}})
        loaded = ClickerConfig.from_json(text)
        self.assertEqual(loaded.selected_bindings(), {"pause": "key:22"})

    def test_the_summary_says_what_is_set(self) -> None:
        self.assertEqual(ClickerConfig().summary(), "Not set up")
        self.assertIn("no buttons set yet", self.config.summary())
        self.config.bind(ACTION_PAUSE, "key:22")
        self.config.bind(ACTION_MARK, "key:21")
        self.assertEqual(self.config.summary(), "T01  (Record / Pause: Page Down, Mark: Page Up)")


if __name__ == "__main__":
    unittest.main()
