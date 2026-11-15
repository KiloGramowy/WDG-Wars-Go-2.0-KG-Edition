"""Synthetic UART corruption, identity guards and honest RF loss diagnostics."""
import json
import unittest
from unittest.mock import Mock, patch

import test_wardrive_solo as fixtures


class SoloResynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WardriveSoloTests(methodName="runTest")
        self.f.setUp()

    def running(self, **choices):
        return self.f.running(**choices)

    def observation(self, seq=1, **changes):
        event = json.loads(self.f.observation(seq=seq)[6:])
        event.update(changes)
        return "@SOLO " + json.dumps(event, separators=(",", ":"))

    @staticmethod
    def damaged(suffix):
        return '@SOLO {"v":1,"event":"observation","ssid":"synthetic fragment ' + suffix

    def test_real_shape_truncated_52_then_complete_76_recovers_one_not_missing_24(self):
        c = self.running(wifi=True, ble=True, profile="full")
        for seq in range(1, 52):
            c.handle_line(self.observation(seq))
        self.f.now += 16
        later = dict(capture_monotonic_us=16_913_000, emitted_monotonic_us=16_963_000)
        suffix = self.observation(76, **later)
        c.handle_line(self.damaged(suffix))
        c.handle_line(suffix)  # A retransmission must not create another reward.
        for seq in range(77, 119):
            c.handle_line(self.observation(seq, **later))
        c.handle_line(fixtures.frame("stopped", emitted=17_000_000,
            session=fixtures.SESSION, last_seq=118, dropped=0))
        self.assertEqual(len(self.f.observations), 94)
        self.assertEqual([row["seq"] for row in self.f.observations],
                         list(range(1, 52)) + list(range(76, 119)))
        self.assertEqual((c.sequence_gaps, c.framing_errors, c.invalid_events,
                          c.recovered_observations, c.dropped_observations), (24, 1, 1, 1, 0))
        self.assertEqual(self.f.notice.call_count, 1)
        self.assertIn("host sequence gaps 24", self.f.notice.call_args.args[0])
        self.assertIn("recovered complete observations 1", self.f.notice.call_args.args[0])

    def test_valid_ssid_marker_is_data_and_never_invokes_recovery(self):
        c = self.running()
        for seq, name in enumerate(('@SOLO {"v":1}', '@SOLO {}', 'x @SOLO {"event":"caps"}'), 1):
            with self.subTest(name=name), patch.object(c, "_recover_observation", wraps=c._recover_observation) as recover:
                c.handle_line(self.observation(seq, ssid=name))
                recover.assert_not_called()
        self.assertEqual([row["ssid"] for row in self.f.observations],
                         ['@SOLO {"v":1}', '@SOLO {}', 'x @SOLO {"event":"caps"}'])
        self.assertEqual((c.invalid_events, c.recovered_observations), (0, 0))

    def test_decodable_but_invalid_canonical_object_does_not_fall_back(self):
        c = self.running()
        with patch.object(c, "_recover_observation", wraps=c._recover_observation) as recover:
            c.handle_line(self.observation(channel=0, ssid='@SOLO {"v":1}'))
            recover.assert_not_called()
        self.assertEqual(self.f.observations, [])
        self.assertEqual((c.invalid_events, c.framing_errors), (1, 0))

    def test_duplicate_keys_and_nan_do_not_create_a_recovery_fallback(self):
        for kind in ("duplicate", "nan"):
            with self.subTest(kind=kind):
                self.setUp()
                c = self.running()
                line = self.observation()
                line = line.replace('"v":1', '"v":1,"v":1', 1) if kind == "duplicate" else self.observation(rssi=float("nan"))
                with patch.object(c, "_recover_observation", wraps=c._recover_observation) as recover:
                    c.handle_line(line)
                    recover.assert_not_called()
                self.assertEqual((c.invalid_events, c.recovered_observations), (1, 0))
                self.assertEqual(self.f.observations, [])

    def test_wrong_boot_session_disabled_protocol_and_retransmission_are_not_salvaged(self):
        for changes in ({"boot_id": "1111111111111111"}, {"session": "1111111111111111"},
                        {"protocol": "ble", "name": "synthetic"}, {"seq": 1}):
            with self.subTest(changes=changes):
                self.setUp()
                c = self.running()
                c.handle_line(self.observation(1))
                values = dict(seq=2)
                values.update(changes)
                c.handle_line(self.damaged(self.observation(**values)))
                self.assertEqual([row["seq"] for row in self.f.observations], [1])
                self.assertEqual(c.recovered_observations, 0)
                self.assertEqual(c.boot_id, fixtures.BOOT)
                self.assertTrue(c.confirmed_wifi)
                self.f.stop.assert_not_called()

    def test_suffix_still_requires_all_normal_observation_checks(self):
        for changes in ({"rssi": "-70"}, {"mac": "aa:bb:cc:dd:ee:ff"},
                        {"channel": 0}, {"capture_monotonic_us": 1_400_000},
                        {"emitted_monotonic_us": 1_000_000}, {"seq": True}):
            with self.subTest(changes=changes):
                self.setUp()
                c = self.running()
                c.handle_line(self.damaged(self.observation(**changes)))
                self.assertEqual(self.f.observations, [])
                self.assertEqual((c.recovered_observations, c.invalid_events), (0, 1))

    def test_multiple_markers_duplicate_keys_nan_and_trailing_garbage_are_ambiguous_or_invalid(self):
        valid = self.observation()
        suffixes = (valid + self.observation(2), valid + "garbage",
                    valid.replace('"v":1', '"v":1,"v":1', 1), self.observation(rssi=float("nan")))
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                self.setUp()
                c = self.running()
                c.handle_line(self.damaged(suffix))
                self.assertEqual(self.f.observations, [])
                self.assertEqual((c.recovered_observations, c.invalid_events), (0, 1))

    def test_arbitrary_console_prefix_and_oversized_lines_never_resynchronize(self):
        for prefix in ("noise ", ">> ", "> > ", "x> ", " > "):
            with self.subTest(prefix=prefix):
                self.setUp()
                c = self.running()
                self.assertFalse(c.handle_line(prefix + self.damaged(self.observation())))
                self.assertEqual((c.invalid_events, c.recovered_observations), (0, 0))
        c.handle_line(self.damaged("x" * 4096 + self.observation()))
        self.assertEqual((c.invalid_events, c.recovered_observations), (1, 0))

    def test_complete_first_json_followed_by_another_frame_is_not_silently_discarded_as_fragment(self):
        c = self.running()
        c.handle_line(self.observation(1) + self.observation(2))
        self.assertEqual(self.f.observations, [])
        self.assertEqual((c.invalid_events, c.recovered_observations), (1, 0))

    def test_exact_cli_prompt_can_recover_one_observation(self):
        c = self.running()
        self.assertTrue(c.handle_line("> " + self.damaged(self.observation())))
        self.assertEqual([row["seq"] for row in self.f.observations], [1])
        self.assertEqual((c.recovered_observations, c.invalid_events), (1, 1))

    def test_corrupt_control_suffix_cannot_grant_or_release_radio_ownership(self):
        controls = (fixtures.caps(), fixtures.frame("stopped", session=fixtures.SESSION, last_seq=0, dropped=0),
                    fixtures.frame("state", session=fixtures.SESSION, profile="kg", wifi=True,
                                   ble=False, phase="wifi", cycles=0, clock_monotonic_us=1_200_000),
                    fixtures.frame("error", session=fixtures.SESSION, code="radio_start", message="synthetic"))
        for control in controls:
            with self.subTest(control=control):
                self.setUp()
                c = self.running()
                c.handle_line(self.damaged(control))
                self.assertTrue(c.confirmed_wifi)
                self.assertTrue(c.busy)
                self.assertEqual(c.state, "running")
                self.assertEqual(c.recovered_observations, 0)
                self.f.stop.assert_not_called()

    def test_rejected_recorder_does_not_count_recovery_or_create_diagnostics_rewards(self):
        c = self.running()
        c._on_observation = Mock(return_value=False)
        c.handle_line(self.damaged(self.observation()))
        self.assertEqual(c._on_observation.call_count, 1)
        self.assertEqual((c.recovered_observations, c.sequence_gaps, c._last_seq), (0, 0, 0))
        self.f.stop.assert_called_once()
        self.assertEqual(self.f.observations, [])

    def test_stop_tail_counts_once_separately_from_firmware_drops_and_never_calls_recorder(self):
        c = self.running()
        c.handle_line(self.observation(1))
        c.handle_line(self.observation(4))
        c.cancel()
        stopped = fixtures.frame("stopped", emitted=1_400_000,
                                 session=fixtures.SESSION, last_seq=7, dropped=3)
        c.handle_line(stopped)
        c.handle_line(stopped)
        c.stop_acknowledged()
        self.assertEqual((c.sequence_gaps, c.dropped_observations), (5, 3))
        self.assertEqual(len(self.f.observations), 2)
        self.assertEqual(self.f.notice.call_count, 1)
        self.assertIn("host sequence gaps 5", self.f.notice.call_args.args[0])
        self.assertIn("3 observation(s) dropped by firmware", self.f.notice.call_args.args[0])
        self.assertFalse(c.busy)

    def test_stop_with_no_observations_reports_tail_without_inventing_records(self):
        c = self.running()
        c.handle_line(fixtures.frame("stopped", emitted=1_400_000,
            session=fixtures.SESSION, last_seq=9, dropped=0))
        self.assertEqual(c.sequence_gaps, 9)
        self.assertEqual(self.f.observations, [])
        self.assertEqual(self.f.notice.call_count, 1)

    def test_malformed_stop_summary_does_not_clear_owner_or_add_tail(self):
        for changes in ({"last_seq": 0}, {"last_seq": True}, {"dropped": -1},
                        {"session": "1111111111111111"}):
            with self.subTest(changes=changes):
                self.setUp()
                c = self.running()
                c.handle_line(self.observation(1))
                values = dict(session=fixtures.SESSION, last_seq=3, dropped=0)
                values.update(changes)
                c.handle_line(fixtures.frame("stopped", emitted=1_400_000, **values))
                self.assertTrue(c.confirmed_wifi)
                self.assertEqual(c.sequence_gaps, 0)
                self.assertEqual(c.dropped_observations, 0)
                self.f.notice.assert_not_called()

    def test_transmitted_new_session_resets_sequence_and_summary_baseline_before_first_state(self):
        c = self.running()
        c.handle_line(self.observation(1))
        c.handle_line(fixtures.frame("stopped", emitted=1_400_000,
            session=fixtures.SESSION, last_seq=5, dropped=0))
        self.assertEqual(c.sequence_gaps, 4)
        self.f.notice.reset_mock()
        fresh = "1111111111111111"
        c._queue("synthetic-start", "start", "synthetic", session=fresh, target=("kg", True, False))
        c.command_sent("synthetic-start")
        c.cancel(stop=False)
        c.handle_line(fixtures.frame("stopped", emitted=1_500_000,
            session=fresh, last_seq=0, dropped=0))
        self.assertEqual(c.sequence_gaps, 4)
        self.f.notice.assert_not_called()
        self.assertFalse(c.busy)


if __name__ == "__main__":
    unittest.main()
