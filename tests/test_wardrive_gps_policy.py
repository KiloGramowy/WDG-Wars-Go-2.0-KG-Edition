"""Piglet's GPS decisions with deterministic monotonic time and no hardware."""

import math
import unittest

from watchdogs.wardrive_gps import WardriveGpsPolicy


def sample(when=100.0, **changes):
    value = dict(lat=36.46, lon=-6.2, altitude=10.0, hdop=0.9,
                 sats=8, quality=1, observed_at=when)
    value.update(changes)
    return value


class WardriveGpsPolicyTests(unittest.TestCase):
    def test_startup_waits_for_quality_gated_fix_not_merely_valid_location(self):
        policy = WardriveGpsPolicy()
        self.assertTrue(policy.startup_waiting)
        for now, change in ((100, dict(sats=2)), (101, dict(hdop=10.01)),
                            (102, dict(hdop=math.nan))):
            self.assertFalse(policy.feed_sample(sample(now, **change), now))
            self.assertIsNone(policy.first_fix_snapshot)
            self.assertEqual(policy.position_for(now, now)["source"], "measured")
        self.assertTrue(policy.feed_sample(sample(103, sats=3, hdop=10), 103))
        self.assertFalse(policy.startup_waiting)
        self.assertTrue(policy.startup_closing)
        self.assertEqual(policy.first_fix_snapshot["source"], "startup_backfill")

    def test_first_fix_is_immutable_while_later_measurements_refresh_cache(self):
        policy = WardriveGpsPolicy()
        self.assertTrue(policy.feed_sample(sample(), 100))
        first = policy.first_fix_snapshot
        first["lat"] = 99
        self.assertFalse(policy.feed_sample(sample(101, lat=40), 101))
        self.assertEqual(policy.first_fix_snapshot["lat"], 36.46)
        self.assertEqual(policy.position_for(101, 101)["lat"], 40)
        policy.complete_startup()
        self.assertFalse(policy.startup_closing)
        self.assertTrue(policy.startup_complete)

    def test_cache_720_minutes_inclusive_expiry_preserves_measurement_age(self):
        policy = WardriveGpsPolicy(cache_minutes=720)
        policy.feed_sample(sample(), 100)
        # Piglet updates lastGpsValidMs on each loop while location age <2s.
        policy.feed_sample(sample(), 101.9)
        policy.feed_sample(None, 102)
        value = policy.position_for(102, 102)
        self.assertEqual(value["source"], "cache")
        self.assertEqual(value["observed_at"], 100)
        self.assertEqual(value["cache_refreshed_at"], 101.9)
        self.assertAlmostEqual(value["age"], 2)
        self.assertAlmostEqual(value["cache_age"], 0.1)
        boundary = 101.9 + 720 * 60
        self.assertEqual(policy.position_for(boundary, boundary)["source"], "cache")
        self.assertIsNone(policy.position_for(boundary + 0.001, boundary + 0.001))

    def test_polling_position_never_refreshes_cache_or_measured_timestamp(self):
        policy = WardriveGpsPolicy(cache_minutes=1)
        policy.feed_sample(sample(), 100)
        for when in range(102, 161):
            position = policy.position_for(when, when)
            self.assertEqual(position["observed_at"], 100)
            self.assertEqual(position["cache_refreshed_at"], 100)
        self.assertIsNone(policy.position_for(160.001, 160.001))

    def test_fresh_low_quality_location_is_preferred_but_cannot_poison_cache(self):
        policy = WardriveGpsPolicy()
        policy.feed_sample(sample(), 100)
        policy.complete_startup()
        policy.feed_sample(sample(101, lat=40, sats=2, hdop=12), 101)
        live = policy.position_for(101, 101)
        self.assertEqual((live["lat"], live["source"], live["hdop"]), (40, "measured", 12))
        policy.feed_sample(None, 102)
        cached = policy.position_for(102, 102)
        self.assertEqual((cached["lat"], cached["source"], cached["observed_at"]), (36.46, "cache", 100))

    def test_loss_expiry_recovery_does_not_arm_another_startup_backfill(self):
        policy = WardriveGpsPolicy(cache_minutes=1)
        policy.feed_sample(sample(), 100)
        policy.complete_startup()
        policy.feed_sample(None, 102)
        self.assertIsNone(policy.position_for(161, 161))
        self.assertFalse(policy.feed_sample(sample(162, lat=41), 162))
        self.assertEqual(policy.position_for(162, 162)["source"], "measured")
        self.assertTrue(policy.startup_complete)
        self.assertFalse(policy.startup_closing)
        self.assertEqual(policy.first_fix_snapshot["lat"], 36.46)

    def test_delayed_rx_does_not_receive_a_future_gps_measurement(self):
        policy = WardriveGpsPolicy()
        policy.feed_sample(sample(100, lat=10), 100)
        policy.feed_sample(sample(101, lat=20), 101)
        self.assertEqual(policy.position_for(100.5, 101.5)["lat"], 10)
        self.assertEqual(policy.position_for(101.5, 101.5)["lat"], 20)
        self.assertIsNone(policy.position_for(99, 101.5))
        self.assertIsNone(policy.position_for(102, 101.5))

    def test_bounded_state_history_does_not_guess_an_older_capture_position(self):
        policy = WardriveGpsPolicy(history_limit=2)
        for when in (100, 101, 102):
            policy.feed_sample(sample(when), when)
        self.assertEqual(len(policy._history), 2)
        self.assertIsNone(policy.position_for(100.5, 103))

    def test_new_instance_has_no_old_cache_or_first_fix(self):
        before = WardriveGpsPolicy()
        before.feed_sample(sample(), 100)
        after = WardriveGpsPolicy()
        after.feed_sample(None, 101)
        self.assertTrue(after.startup_waiting)
        self.assertIsNone(after.first_fix_snapshot)
        self.assertIsNone(after.position_for(101, 101))

    def test_stale_invalid_or_future_samples_cannot_qualify_a_first_fix(self):
        for value in (None, sample(98), sample(101), sample(lat=math.nan),
                      sample(lon=181), sample(valid=False), sample(observed_at=math.inf)):
            with self.subTest(sample=value):
                policy = WardriveGpsPolicy()
                self.assertFalse(policy.feed_sample(value, 100))
                self.assertTrue(policy.startup_waiting)
                self.assertIsNone(policy.position_for(100, 100))

    def test_raw_hdop_is_kept_separate_from_unknown_accuracy_metres(self):
        policy = WardriveGpsPolicy()
        policy.feed_sample(sample(hdop=2.345, altitude=12.345), 100)
        position = policy.position_for(100, 100)
        self.assertEqual((position["hdop"], position["altitude"]), (2.345, 12.345))
        self.assertNotIn("accuracy", position)

    def test_bad_configuration_or_monotonic_time_is_rejected(self):
        for minutes in (0, 10081, True, 3.5):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                WardriveGpsPolicy(cache_minutes=minutes)
        policy = WardriveGpsPolicy()
        policy.feed_sample(sample(), 100)
        with self.assertRaises(ValueError):
            policy.feed_sample(sample(99), 99)
        with self.assertRaises(ValueError):
            policy.position_for(math.nan)
        with self.assertRaises(RuntimeError):
            WardriveGpsPolicy().complete_startup()


if __name__ == "__main__":
    unittest.main()
