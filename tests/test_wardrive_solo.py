"""UART v1 controller with a deterministic sole-arbiter fake, no hardware/I/O."""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch

from watchdogs.wardrive_gps import WardriveGpsPolicy
from watchdogs.wardrive_solo import ACK_TIMEOUT, KG_CHANNELS, MAX_LINE_BYTES, WardriveSoloController


BOOT, SESSION = "0123456789abcdef", "fedcba9876543210"


def frame(kind, emitted=1_000_000, **extra):
    return "@SOLO " + json.dumps(dict(v=1, event=kind, boot_id=BOOT,
        emitted_monotonic_us=emitted, **extra), separators=(",", ":"))


def caps(emitted=1_000_000, **extra):
    values = dict(clock_monotonic_us=1_000_000, supported=True, contract=1,
                  firmware="1.7.0", sdk="v6.0.1", max_line_bytes=4096,
                  channels=sorted(KG_CHANNELS | {149}), skipped=[],
                  country=dict(code="PH", policy=0, first=1, count=14, mask5=0))
    values.update(extra)
    return frame("caps", emitted=emitted, **values)


class WardriveSoloTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 10.0, 1_800_000_000.0
        self.start, self.stop, self.cancel, self.notice = Mock(return_value=True), Mock(return_value=True), Mock(), Mock()
        self.observations = []
        self.controller = WardriveSoloController(self.start, self.stop, self.observations.append,
            cancel_pending=self.cancel, notice=self.notice,
            monotonic=lambda: self.now, utc_clock=lambda: self.wall + (self.now - 10.0))

    def sent(self):
        command = self.start.call_args.args[0]
        self.assertTrue(self.controller.command_sent(command))
        return command

    def running(self, wifi=True, ble=False, profile="kg"):
        c = self.controller
        c.request(wifi=wifi, ble=ble, profile=profile)
        self.sent()
        self.now += 0.2
        with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
            self.assertTrue(c.handle_line(caps()))
        command = self.sent()
        self.assertEqual(command, f"wardrive_solo {SESSION} {profile} {int(wifi)} {int(ble)}")
        self.now += 0.1
        c.handle_line(caps(emitted=1_150_000, clock_monotonic_us=1_150_000))
        self.now += 0.1
        c.handle_line(frame("state", emitted=1_200_000, clock_monotonic_us=1_200_000,
            session=SESSION, profile=profile, wifi=wifi, ble=ble,
            phase="wifi" if wifi else "ble", cycles=0))
        self.assertEqual((c.confirmed_wifi, c.confirmed_ble), (wifi, ble))
        return c

    def observation(self, seq=1, protocol="wifi", **extra):
        values = dict(session=SESSION, seq=seq, capture_monotonic_us=1_250_000,
                      protocol=protocol, mac="AA:BB:CC:DD:EE:FF", rssi=-70)
        values.update(dict(ssid="synthetic", auth="WPA2_PSK", channel=6) if protocol == "wifi"
                      else dict(name="synthetic"))
        values.update(extra)
        return frame("observation", emitted=1_300_000, **values)

    def test_each_wifi_profile_and_ble_combination_requires_real_state_ack(self):
        for profile, wifi, ble in (("full", True, False), ("kg", True, False),
                                   ("full", False, True), ("kg", True, True), ("full", True, True)):
            with self.subTest(profile=profile, wifi=wifi, ble=ble):
                self.setUp()
                c = self.running(wifi, ble, profile)
                self.assertEqual(c.phase, "wifi" if wifi else "ble")
                self.assertEqual(c.state, "running")
                self.assertTrue(c.busy)

    def test_queue_delay_does_not_consume_firmware_ack_timeout_or_show_on(self):
        c = self.controller
        c.request(wifi=True)
        self.assertEqual(self.start.call_args.args, ("wardrive_solo_caps", "solo_control", "Check SOLO firmware"))
        self.now += 100
        c.tick()
        self.assertTrue(c.desired_wifi)
        self.assertFalse(c.confirmed_wifi)
        self.sent()
        self.now += ACK_TIMEOUT
        c.tick()
        self.assertFalse(c.desired_wifi)
        self.assertFalse(c.confirmed_wifi)
        self.assertIn("compatible firmware required", c.last_error)
        self.assertEqual(self.start.call_count, 1)

    def test_stock_firmware_unknown_reply_never_grants_scanning(self):
        c = self.controller
        c.request(wifi=True)
        self.sent()
        self.assertFalse(c.handle_line("Unknown command: wardrive_solo_caps"))
        c.tick(self.now + ACK_TIMEOUT)
        self.assertFalse(c.confirmed_wifi)
        self.assertEqual(self.start.call_count, 1)

    def test_failed_probe_restore_keeps_unknown_rf_owner_until_stop_ack_or_disconnect(self):
        for finish in ("ack", "disconnect"):
            with self.subTest(finish=finish):
                self.setUp()
                c = self.controller
                c.request(wifi=True)
                self.sent()
                c.handle_line(frame("error", code="restore_failed",
                    message="Cannot verify and restore radio channels"))
                self.stop.assert_called_once_with()
                self.assertTrue(c.busy)
                self.assertFalse(c.confirmed_wifi)
                self.assertFalse(c.desired_wifi)
                self.assertIsNone(c._active_session)
                self.now += ACK_TIMEOUT
                c.tick()
                self.assertTrue(c.busy)
                self.assertEqual(c.state, "error")
                c.cancel(stop=False)
                self.assertTrue(c.busy)
                self.assertEqual(self.start.call_count, 1)
                c.stop_acknowledged() if finish == "ack" else c.disconnected()
                self.assertFalse(c.busy)

    def test_failed_probe_with_confirmed_restoration_remains_normal_off(self):
        c = self.controller
        c.request(wifi=True)
        self.sent()
        c.handle_line(frame("error", code="capability_failed",
            message="Cannot verify and restore radio channels"))
        self.stop.assert_not_called()
        self.assertFalse(c.busy)
        self.assertFalse(c.confirmed_wifi)
        self.assertFalse(c.desired_wifi)
        self.assertEqual(c.state, "error")

    def test_probe_toggles_coalesce_and_start_uses_latest_requested_modes(self):
        c = self.controller
        c.request(wifi=True, profile="full")
        self.sent()
        c.request(ble=True, profile="kg")
        c.request(wifi=False)
        self.assertEqual(self.start.call_count, 1)
        self.now += 0.2
        with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
            c.handle_line(caps())
        self.assertEqual(self.start.call_args.args[0], f"wardrive_solo {SESSION} kg 0 1")
        self.assertFalse(c.confirmed_ble)

    def test_unsupported_and_incomplete_kg_capabilities_remain_off(self):
        for response in (caps(supported=False), caps(contract=2), caps(channels=[1, 6, 11])):
            with self.subTest(response=response):
                self.setUp()
                c = self.controller
                c.request(wifi=True, profile="kg")
                self.sent()
                self.now += 0.2
                c.handle_line(response)
                self.assertEqual(self.start.call_count, 1)
                self.assertFalse(c.desired_wifi)
                self.assertFalse(c.confirmed_wifi)
                self.assertTrue(c.last_error)

    def test_start_ack_must_match_session_profile_modes_and_actual_phase(self):
        c = self.controller
        c.request(wifi=True, profile="kg")
        self.sent()
        self.now += 0.2
        with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
            c.handle_line(caps())
        self.sent()
        self.now += 0.1
        c.handle_line(caps(emitted=1_150_000, clock_monotonic_us=1_150_000))
        for changes in (dict(session="1111111111111111"), dict(profile="full"),
                        dict(ble=True), dict(phase="ble"), dict(wifi=1)):
            values = dict(session=SESSION, profile="kg", wifi=True, ble=False,
                          phase="wifi", cycles=0, clock_monotonic_us=1_200_000)
            values.update(changes)
            c.handle_line(frame("state", emitted=1_200_000, **values))
            self.assertFalse(c.confirmed_wifi)
        self.now += ACK_TIMEOUT
        c.tick()
        self.stop.assert_called_once_with()
        self.assertFalse(c.confirmed_wifi)
        self.assertTrue(c.busy)  # Sent start may own RF until full STOP.

    def test_cancel_before_ack_preserves_unknown_rf_ownership_for_app_stop_barrier(self):
        c = self.controller
        c.request(wifi=True)
        self.sent()
        self.now += 0.2
        with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
            c.handle_line(caps())
        self.sent()
        c.cancel(stop=False)
        self.assertTrue(c.busy)
        self.assertFalse(c.desired_wifi)
        self.assertFalse(c.confirmed_wifi)
        self.stop.assert_not_called()
        c.stop_acknowledged()
        self.assertFalse(c.busy)

    def test_cancel_and_stop_timeout_do_not_fake_confirmed_off_or_restart(self):
        c = self.running()
        c.cancel()
        self.assertTrue(c.confirmed_wifi)
        self.stop.assert_called_once_with()
        calls = self.start.call_count
        self.now += ACK_TIMEOUT
        c.tick()
        self.assertTrue(c.confirmed_wifi)
        self.assertFalse(c.desired_wifi)
        self.assertEqual(c.state, "error")
        self.assertEqual(self.start.call_count, calls)
        c.stop_acknowledged()
        self.assertFalse(c.confirmed_wifi)
        self.assertFalse(c.busy)

    def test_late_phase_state_during_stop_cannot_restore_running(self):
        c = self.running(wifi=True, ble=True)
        c.cancel()
        c.handle_line(frame("state", emitted=1_300_000, clock_monotonic_us=1_300_000,
            session=SESSION, profile="kg", wifi=True, ble=True, phase="ble", cycles=1))
        self.assertEqual(c.state, "stopping")
        self.assertFalse(c.desired_wifi)
        self.assertTrue(c.confirmed_wifi)
        c.stop_acknowledged()
        c.handle_line(frame("state", emitted=1_400_000, clock_monotonic_us=1_400_000,
            session=SESSION, profile="kg", wifi=True, ble=True, phase="wifi", cycles=2))
        self.assertFalse(c.confirmed_wifi)
        self.assertFalse(c.confirmed_ble)

    def test_mode_change_queues_only_probe_and_waits_for_arbiter_stop_then_start_ack(self):
        c = self.running()
        c.request(ble=True)
        self.assertTrue(c.confirmed_wifi)
        self.assertFalse(c.confirmed_ble)
        self.assertEqual(self.start.call_args.args[0], "wardrive_solo_caps")
        c.stop_acknowledged()
        self.assertFalse(c.confirmed_wifi)
        self.assertTrue(c.desired_wifi)
        self.assertTrue(c.desired_ble)
        self.assertEqual(c.state, "queued_caps")

    def test_confirmed_native_pause_keeps_enabled_modes_without_claiming_radio_phase(self):
        c = self.running(wifi=True, ble=True)
        c.handle_line(frame("state", emitted=1_300_000, clock_monotonic_us=1_300_000,
            session=SESSION, profile="kg", wifi=True, ble=True, phase="pause", cycles=1))
        self.assertEqual(c.phase, "pause")
        self.assertTrue(c.confirmed_wifi)
        self.assertTrue(c.confirmed_ble)

    def test_observation_ids_are_unique_but_repeated_device_in_later_sequence_is_forwarded(self):
        c = self.running(wifi=True, ble=True)
        c.handle_line(self.observation())
        c.handle_line(self.observation())
        c.handle_line(self.observation(seq=2))
        c.handle_line(self.observation(seq=4, protocol="ble"))
        self.assertEqual([row["identity"] for row in self.observations],
                         [(BOOT, SESSION, 1), (BOOT, SESSION, 2), (BOOT, SESSION, 4)])
        self.assertEqual(c.sequence_gaps, 1)

    def test_capture_clock_uses_fixed_midpoint_anchor_and_preserves_raw_time(self):
        c = self.running()
        self.wall += 3600  # Wall-clock correction must not move an existing session.
        c.handle_line(self.observation())
        record = self.observations[0]
        self.assertEqual(record["capture_monotonic_us"], 1_250_000)
        self.assertAlmostEqual(record["observed_at"], 10.35)
        expected = datetime.fromtimestamp(1_800_000_000.35, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.assertEqual(record["captured_at"], expected)
        self.assertAlmostEqual(record["clock_uncertainty_s"], 0.05)
        self.assertEqual(record["clock_receive_correction_s"], 0.0)
        self.assertEqual(record["clock_source"], "host_anchor")
        self.assertNotIn("lat", record)
        self.assertNotIn("xp", record)

    def test_small_future_estimate_is_bounded_to_receive_with_consistent_utc(self):
        for correction in (0.005, 0.012):
            with self.subTest(correction=correction):
                self.setUp()
                c = self.running()
                self.now = 10.35 - correction
                source = self.observation()
                raw = json.loads(source[6:])
                c.handle_line(source)
                self.assertEqual(len(self.observations), 1)
                record = self.observations[0]
                self.assertEqual(record["observed_at"], self.now)
                self.assertAlmostEqual(record["clock_receive_correction_s"], correction)
                self.assertAlmostEqual(record["clock_uncertainty_s"], 0.05)
                expected = datetime.fromtimestamp(1_800_000_000.35 - correction,
                    timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                self.assertEqual(record["captured_at"], expected)
                for key, value in raw.items():
                    self.assertEqual(record[key], value)

    def test_future_estimate_outside_uncertainty_still_rejected_before_receive_bound(self):
        c = self.running()
        self.now = 10.29  # Estimate10.35 exceeds the50ms uncertainty.
        c.handle_line(self.observation())
        self.assertEqual(self.observations, [])
        self.assertEqual(c.invalid_events, 1)
        self.assertEqual(c._last_seq, 0)

    def test_receive_bound_uses_existing_gps_cache_not_a_later_queued_fix(self):
        c = self.running()
        self.now = 10.338
        policy = WardriveGpsPolicy(clock=lambda: self.now)
        sample = dict(lat=12.0, lon=34.0, observed_at=9.0, hdop=1.0, sats=5)
        self.assertTrue(policy.feed_sample(sample, now=9.0))
        policy.complete_startup()
        policy.feed_sample(None, now=10.0)
        self.assertIsNone(policy.position_for(10.35, now=self.now))
        c.handle_line(self.observation())
        record = self.observations[0]
        # A worker can see a GPS update queued after RX. Its newer position
        # must not replace the cache that was available at capture.
        policy.feed_sample(dict(sample, lat=56.0, lon=78.0, observed_at=10.339), now=10.339)
        position = policy.position_for(record["observed_at"], now=10.34)
        self.assertEqual(position["source"], "cache")
        self.assertEqual((position["lat"], position["lon"]), (12.0, 34.0))
        self.assertEqual(position["observed_at"], 9.0)
        self.assertEqual(position["cache_refreshed_at"], 9.0)
        self.assertAlmostEqual(position["cache_age"], self.now - 9.0)
        self.assertAlmostEqual(record["clock_receive_correction_s"], 0.012)
        self.assertEqual(policy.cache_minutes, 720)

    def test_receive_bound_does_not_invent_gps_or_extend_expired_cache(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                self.setUp()
                c = self.running()
                self.now = 10.338
                policy = WardriveGpsPolicy(clock=lambda: self.now)
                if expired:
                    old = self.now - 720 * 60 - 1
                    policy.feed_sample(dict(lat=12.0, lon=34.0, observed_at=old,
                        hdop=1.0, sats=5), now=old)
                    policy.complete_startup()
                    policy.feed_sample(None, now=self.now - 0.001)
                c.handle_line(self.observation())
                self.assertIsNone(policy.position_for(self.observations[0]["observed_at"], now=self.now))
                self.assertEqual(policy.cache_minutes, 720)
                if expired:
                    self.assertEqual(policy._cache_refreshed_at, old)

    def test_receive_bound_and_cli_prompt_do_not_duplicate_retransmission(self):
        c = self.running()
        self.now = 10.338
        delivered = Mock(side_effect=self.observations.append)
        c._on_observation = delivered
        source = self.observation(seq=1)
        self.assertTrue(c.handle_line("> " + source))
        self.now += 0.1
        self.assertTrue(c.handle_line(source))
        self.assertTrue(c.handle_line("> " + source))
        self.assertEqual(delivered.call_count, 1)
        self.assertEqual([row["identity"] for row in self.observations], [(BOOT, SESSION, 1)])
        self.assertAlmostEqual(self.observations[0]["clock_receive_correction_s"], 0.012)
        self.assertEqual(c._last_seq, 1)

    def test_exact_cli_prompt_first_observation_is_forwarded_only_once(self):
        c = self.running()
        first = self.observation(seq=1)
        self.assertTrue(c.handle_line("> " + first))
        self.assertTrue(c.handle_line(first))
        self.assertEqual([row["identity"] for row in self.observations],
                         [(BOOT, SESSION, 1)])
        self.assertEqual(c.invalid_events, 0)
        self.assertEqual(c._last_seq, 1)

    def test_arbitrary_console_prefixes_are_not_treated_as_solo_frames(self):
        c = self.running()
        for prefix in ("noise ", ">> ", "> > ", "x> ", " > "):
            with self.subTest(prefix=prefix):
                self.assertFalse(c.handle_line(prefix + self.observation()))
        self.assertEqual(self.observations, [])
        self.assertEqual(c.invalid_events, 0)

    def test_malformed_solo_after_exact_cli_prompt_is_consumed_and_rejected(self):
        c = self.running()
        self.assertTrue(c.handle_line("> @SOLO {"))
        self.assertEqual(self.observations, [])
        self.assertEqual(c.invalid_events, 1)

    def test_malformed_oversized_wrong_session_disabled_protocol_and_future_capture_are_not_forwarded(self):
        c = self.running()
        lines = ["@SOLO {", "@SOLO " + "x" * MAX_LINE_BYTES,
                 "@SOLO " + "[" * 1500 + "]" * 1500,
                 '@SOLO {"v":1,"v":1}', self.observation(session="1111111111111111"),
                 self.observation(protocol="ble"), self.observation(mac="aa:bb:cc:dd:ee:ff"),
                 self.observation(seq=True), self.observation(ssid="ż" * 17),
                 self.observation(channel=0), self.observation(rssi="-70"),
                 self.observation(capture_monotonic_us=1_400_000)]
        for line in lines:
            self.assertTrue(c.handle_line(line))
        self.assertEqual(self.observations, [])
        self.assertGreater(c.invalid_events, 0)

    def test_kg_keeps_adjacent_ap_channel_separate_from_receiver_tuning(self):
        c = self.running(profile="kg")
        self.assertNotIn(10, KG_CHANNELS)
        self.assertNotIn(10, c.capabilities["channels"])
        c.handle_line(self.observation(channel=10))
        self.assertEqual([row["channel"] for row in self.observations], [10])
        self.assertEqual(c.invalid_events, 0)
        self.assertEqual(c._active_target, ("kg", True, False))

    def test_full_keeps_valid_advertised_channel_outside_tuning_capabilities(self):
        c = self.running(profile="full")
        self.assertNotIn(153, c.capabilities["channels"])
        c.handle_line(self.observation(channel=153))
        self.assertEqual([row["channel"] for row in self.observations], [153])
        self.assertEqual(c.invalid_events, 0)

    def test_invalid_observed_channel_range_and_type_are_still_rejected(self):
        for channel in (0, 197, -1, True, "6", 6.0, None):
            with self.subTest(channel=channel):
                self.setUp()
                c = self.running()
                c.handle_line(self.observation(channel=channel))
                self.assertEqual(self.observations, [])
                self.assertEqual(c.invalid_events, 1)
                self.assertEqual(c._last_seq, 0)

    def test_missing_stop_session_cannot_release_unconfirmed_rf_owner(self):
        c = self.running()
        c.cancel(stop=False)
        c.handle_line(frame("stopped", emitted=1_400_000, last_seq=0, dropped=0))
        self.assertTrue(c.busy)
        self.assertTrue(c.confirmed_wifi)

    def test_stop_drains_valid_observations_and_rejects_any_after_stopped(self):
        c = self.running()
        c.cancel()
        c.handle_line(self.observation())
        self.assertEqual(len(self.observations), 1)
        c.handle_line(frame("stopped", emitted=1_400_000, session=SESSION, last_seq=1, dropped=3))
        self.assertFalse(c.confirmed_wifi)
        self.assertEqual(c.dropped_observations, 3)
        self.assertTrue(any("3 observation(s) dropped" in call.args[0] for call in self.notice.call_args_list))
        c.handle_line(self.observation(seq=2))
        self.assertEqual(len(self.observations), 1)

    def test_disconnect_or_new_boot_clears_modes_without_automatic_restart(self):
        for new_boot in (False, True):
            with self.subTest(new_boot=new_boot):
                self.setUp()
                c = self.running()
                count = self.start.call_count
                if new_boot:
                    event = json.loads(self.observation()[6:])
                    event["boot_id"] = "1111111111111111"
                    c.handle_line("@SOLO " + json.dumps(event))
                else:
                    c.disconnected()
                c.tick(self.now + 100)
                self.assertFalse(c.busy)
                self.assertFalse(c.confirmed_wifi)
                self.assertFalse(c.desired_wifi)
                self.assertIsNone(c.capabilities)
                self.assertEqual(self.start.call_count, count)

    def test_save_failure_stops_source_instead_of_claiming_progress(self):
        c = self.running()
        c._on_observation = Mock(side_effect=OSError("synthetic storage failure"))
        c.handle_line(self.observation())
        self.stop.assert_called_once_with()
        self.assertIn("Cannot save", c.last_error)
        self.assertFalse(c.desired_wifi)

    def test_unsolicited_old_frame_cannot_poison_new_boot_clock_before_probe(self):
        c = self.controller
        c.handle_line(self.observation(seq=9).replace('1300000', '999999999999'))
        self.assertEqual(c._last_emitted, 0)
        c.request(wifi=True, profile="full")
        self.sent()
        self.now += 0.1
        incoming = json.loads(caps()[6:])
        incoming["boot_id"] = "1111111111111111"
        c.handle_line("@SOLO " + json.dumps(incoming))
        self.assertEqual(c.boot_id, "1111111111111111")
        self.assertTrue(self.start.call_args.args[0].startswith("wardrive_solo "))

    def test_actual_start_capabilities_replace_idle_probe_without_second_start(self):
        c = self.controller
        c.request(wifi=True, profile="full")
        self.sent()
        self.now += 0.2
        with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
            c.handle_line(caps())  # Idle driver did not accept channel 14.
        self.sent()
        self.now += 0.1
        c.handle_line(caps(emitted=1_150_000, clock_monotonic_us=1_150_000,
                           channels=sorted(KG_CHANNELS | {14})))
        self.assertEqual(self.start.call_count, 2)
        self.now += 0.1
        c.handle_line(frame("state", emitted=1_200_000, clock_monotonic_us=1_200_000,
            session=SESSION, profile="full", wifi=True, ble=False, phase="wifi", cycles=0))
        self.assertTrue(c.confirmed_wifi)
        c.handle_line(self.observation(channel=14))
        self.assertEqual(self.observations[0]["channel"], 14)

    def test_actual_caps_missing_or_lost_kg_channel_cannot_grant_on(self):
        for response in (None, caps(emitted=1_150_000, clock_monotonic_us=1_150_000, channels=[1, 6, 11])):
            with self.subTest(response=response):
                self.setUp()
                c = self.controller
                c.request(wifi=True, profile="kg")
                self.sent()
                self.now += 0.2
                with patch("watchdogs.wardrive_solo.secrets.token_hex", return_value=SESSION):
                    c.handle_line(caps())
                self.sent()
                self.now += 0.1
                if response is not None:
                    c.handle_line(response)
                c.handle_line(frame("state", emitted=1_200_000, clock_monotonic_us=1_200_000,
                    session=SESSION, profile="kg", wifi=True, ble=False, phase="wifi", cycles=0))
                self.assertFalse(c.confirmed_wifi)
                self.now += ACK_TIMEOUT
                c.tick()
                self.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
