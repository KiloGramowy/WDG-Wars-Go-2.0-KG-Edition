"""OFF display policy with geographic ordering and a controlled monotonic clock."""
import unittest
from unittest.mock import patch

from watchdogs.map_visibility import OffScanDecay


class OffScanDecayTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.decay = OffScanDecay(clock=lambda: self.now)

    def visible(self, keys):
        return {key for key in keys if self.decay.visible(key)}

    def test_active_scanning_has_no_off_filter(self):
        self.assertFalse(self.decay.active)
        self.assertTrue(self.decay.visible("new-result"))

    def test_farther_first_then_nearer_across_exact_five_to_ten_minutes(self):
        keys = ("near", "far", "middle")
        self.decay.freeze([("near", 51.01, 0), ("far", 54, 0),
                           ("middle", 52, 0)], receiver=(51, 0))
        for elapsed, expected in ((0, set(keys)), (299.99, set(keys)),
                                  (300, {"near", "middle"}),
                                  (449.99, {"near", "middle"}),
                                  (450, {"near"}), (599.99, {"near"}),
                                  (600, set()), (100000, set())):
            with self.subTest(elapsed=elapsed):
                self.now = 100 + elapsed
                self.assertEqual(self.visible(keys), expected)

    def test_single_marker_stays_until_ten_minutes(self):
        self.decay.freeze([("only", 51, 1)], receiver=(51, 0))
        self.now += 599.99
        self.assertTrue(self.decay.visible("only"))
        self.now = 700
        self.assertFalse(self.decay.visible("only"))

    def test_equal_distances_use_stable_identity_not_input_order(self):
        self.decay.freeze([("z", 51, 1), ("a", 51, 1)], receiver=(51, 0))
        self.now = 400
        self.assertFalse(self.decay.visible("a"))
        self.assertTrue(self.decay.visible("z"))

    def test_geographic_order_handles_antimeridian(self):
        self.decay.freeze([("near", 1, -179.9), ("far", 1, 170)],
                          receiver=(1, 179.9))
        self.now = 400
        self.assertEqual(self.visible(("near", "far")), {"near"})

    def test_unknown_positions_are_not_assigned_a_fake_distance(self):
        self.decay.freeze([("near", 51.01, 0), ("far", 54, 0),
                           ("unknown", None, None)], receiver=(51, 0))
        self.now = 400
        self.assertEqual(self.visible(("near", "far", "unknown")), {"near", "far"})
        self.now = 550
        self.assertEqual(self.visible(("near", "far", "unknown")), {"near"})

    def test_invalid_or_missing_receiver_uses_deterministic_fallback(self):
        for receiver in (None, (None, None), (0, 0), (91, 1),
                         (51, float("nan")), (51, float("inf"))):
            with self.subTest(receiver=receiver):
                self.decay.clear()
                self.decay.freeze([("z", 51, 1), ("a", 89, 1)],
                                  receiver=receiver, now=100)
                self.assertFalse(self.decay.visible("a", now=400))
                self.assertTrue(self.decay.visible("z", now=400))

    def test_invalid_observations_use_fallback_without_raising(self):
        for lat, lon in ((None, 1), ("51", 1), (0, 0), (91, 1),
                         (51, 181), (float("nan"), 1), (51, float("inf"))):
            with self.subTest(lat=lat, lon=lon):
                self.decay.clear()
                self.decay.freeze([("unknown", lat, lon), ("known", 51.1, 1)],
                                  receiver=(51, 0), now=100)
                self.assertFalse(self.decay.visible("unknown", now=400))
                self.assertTrue(self.decay.visible("known", now=400))

    def test_repeat_off_does_not_extend_deadline_or_admit_new_keys(self):
        self.decay.freeze([("old", 51, 1)], receiver=(51, 0))
        self.now = 699
        self.decay.freeze([("new", 51, 1)], receiver=(51, 0))
        self.assertFalse(self.decay.visible("new"))
        self.assertTrue(self.decay.visible("old"))
        self.now = 700
        self.assertFalse(self.decay.visible("old"))

    def test_resume_clears_schedule_and_next_off_starts_a_new_one(self):
        self.decay.freeze([("old", 51, 1)])
        self.now = 700
        self.assertFalse(self.decay.visible("old"))
        self.decay.clear()
        self.assertFalse(self.decay.active)
        self.assertTrue(self.decay.visible("new"))
        self.decay.freeze([("new", 51, 1)])
        self.assertTrue(self.decay.visible("new", now=1299.99))
        self.assertFalse(self.decay.visible("new", now=1300))

    def test_wall_clock_changes_do_not_change_off_deadlines(self):
        self.decay.freeze([("one", 51, 1)])
        for wall_time in (-100000, 1000000000000):
            with patch("time.time", return_value=wall_time):
                self.assertTrue(self.decay.visible("one"))
        self.now = 700
        self.assertFalse(self.decay.visible("one"))

    def test_empty_snapshot_is_valid_and_cannot_admit_late_results(self):
        self.decay.freeze([])
        self.assertTrue(self.decay.active)
        self.assertFalse(self.decay.visible("late"))

    def test_duplicate_keys_and_invalid_policy_are_rejected(self):
        with self.assertRaises(ValueError):
            self.decay.freeze([("a", 51, 1), ("a", 52, 1)])
        self.assertFalse(self.decay.active)
        for first, last in ((-1, 600), (601, 600), (0, float("inf")),
                            (float("nan"), 600), (True, 600)):
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                OffScanDecay(first_after=first, last_after=last)


if __name__ == "__main__":
    unittest.main()
