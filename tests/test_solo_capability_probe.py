"""Fresh, device-context-bound read-only SOLO probes, with no hardware/I/O."""
import json
import math
import unittest
from unittest.mock import Mock

from watchdogs.wardrive_solo import ACK_TIMEOUT, KG_CHANNELS, WardriveSoloController

BOOT = "0123456789abcdef"
CONTEXT_A = ("synthetic-connection-A", "synthetic-USB-A")
CONTEXT_B = ("synthetic-connection-B", "synthetic-USB-B")


def caps(**changes):
    fields = dict(v=1, event="caps", boot_id=BOOT, emitted_monotonic_us=1_000_000,
                  clock_monotonic_us=1_000_000, supported=True, contract=1,
                  firmware="1.7.0", sdk="synthetic-sdk", max_line_bytes=4096,
                  channels=sorted(KG_CHANNELS), skipped=[],
                  country=dict(code="US", policy=0, first=1, count=11, mask5=0))
    fields.update(changes)
    return "@SOLO " + json.dumps(fields, separators=(",", ":"))


class ReadOnlyCapabilityProbeTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.start = Mock(return_value=True)
        self.stop = Mock(return_value=True)
        self.cancel = Mock()
        self.observation = Mock()
        self.notice = Mock()
        self.c = WardriveSoloController(self.start, self.stop, self.observation,
            cancel_pending=self.cancel, notice=self.notice,
            monotonic=lambda: self.now, utc_clock=lambda: 1_800_000_000.0)

    def probe(self, *, context=CONTEXT_A, negative_safe=True, transmitted=True):
        self.assertTrue(self.c.probe_capabilities())
        self.c.bind_capability_context(context, negative_safe=negative_safe)
        if transmitted:
            self.assertTrue(self.c.command_sent("wardrive_solo_caps"))
        return self.c

    def assert_no_rf(self):
        self.assertFalse(self.c.desired_wifi)
        self.assertFalse(self.c.desired_ble)
        self.assertFalse(self.c.confirmed_wifi)
        self.assertFalse(self.c.confirmed_ble)
        self.assertTrue(all(call.args[0] == "wardrive_solo_caps" for call in self.start.call_args_list))
        self.observation.assert_not_called()

    def assert_no_proof(self):
        self.assertNotIn(self.c.capability_status, ("supported", "unsupported"))
        self.assertIsNone(self.c.capability_received_at)
        self.assertIsNone(self.c.capability_context)
        self.assert_no_rf()

    def test_probe_only_queues_read_command_and_does_not_request_radio_modes(self):
        self.probe(transmitted=False)
        self.start.assert_called_once_with("wardrive_solo_caps", "solo_control", "Check SOLO interface")
        self.assertEqual(self.c.state, "queued_caps")
        self.assertEqual(self.c.capability_status, "checking")
        self.assert_no_proof()

    def test_existing_operation_cannot_be_replaced_by_an_idle_read_probe(self):
        self.assertTrue(self.c.request(wifi=True))
        count = self.start.call_count
        self.assertFalse(self.c.probe_capabilities())
        self.assertEqual(self.start.call_count, count)
        self.assertTrue(self.c.desired_wifi)  # Existing intentional scan request is not erased.

    def test_supported_fresh_reply_records_context_without_queuing_start(self):
        self.probe();self.now += 0.2
        self.assertTrue(self.c.handle_line(caps()))
        self.assertEqual(self.c.capability_status, "supported")
        self.assertEqual(self.c.capability_context, CONTEXT_A)
        self.assertEqual(self.c.capability_received_at, self.now)
        self.assertEqual(self.c.state, "idle")
        self.assertFalse(self.c.busy)
        self.assertEqual(self.start.call_count, 1)
        self.stop.assert_not_called()
        self.assert_no_rf()

    def test_only_complete_exact_legacy_unknown_lines_can_record_unsupported(self):
        for line in ["Unrecognized command", "Unknown command: wardrive_solo_caps",
                     '> Unrecognized command: "wardrive_solo_caps".',
                     "unknown command 'wardrive_solo_caps'", "  Unknown command.\r\n"]:
            with self.subTest(line=line):
                self.setUp();self.probe();self.now += 0.2
                self.assertFalse(self.c.handle_line(line))
                self.assertEqual(self.c.capability_status, "unsupported")
                self.assertEqual(self.c.capability_context, CONTEXT_A)
                self.assertEqual(self.c.capability_received_at, self.now)
                self.assertIn("does not recognize", self.c.last_error)
                self.assertFalse(self.c.busy)
                self.assert_no_rf()

    def test_no_unsupported_without_explicit_safe_stop_and_bound_context(self):
        for context, safe in [(CONTEXT_A, False), (None, True), (None, False)]:
            with self.subTest(context=context, safe=safe):
                self.setUp();self.probe(context=context, negative_safe=safe)
                self.now += 0.2;self.c.handle_line("Unrecognized command")
                self.assert_no_proof()

    def test_queued_without_actual_transmission_never_proves_missing_solo(self):
        self.probe(transmitted=False)
        self.now += 100.0;self.c.tick()
        self.c.handle_line("Unrecognized command")
        self.assertEqual(self.c.state, "queued_caps")
        self.assert_no_proof()

    def test_reply_timestamp_before_transmission_is_not_evidence(self):
        self.probe()
        self.c.handle_line("Unknown command", monotonic=9.999)
        self.assert_no_proof()

    def test_deadline_or_later_unknown_is_not_evidence_even_before_tick(self):
        for delay in [ACK_TIMEOUT, ACK_TIMEOUT + .001, 100.0]:
            with self.subTest(delay=delay):
                self.setUp();self.probe();self.now += delay
                self.c.handle_line("Unrecognized command")
                self.assert_no_proof()
                self.c.tick()
                self.assertEqual(self.c.capability_status, "unknown")
                self.assert_no_proof()

    def test_positive_caps_require_actual_transmission_and_predeadline_reply(self):
        for transmitted, received in [(False, 10.2), (True, 9.999), (True, 15.0), (True, 15.001)]:
            with self.subTest(transmitted=transmitted, received=received):
                self.setUp();self.probe(transmitted=transmitted)
                self.c.handle_line(caps(), monotonic=received)
                self.assert_no_proof()

    def test_partial_unrelated_and_embedded_unknown_text_are_never_evidence(self):
        for line in ["Unrecognized comman", "Unknown command:", "Unknown command: version",
                     "Unrecognized command: wardrive_solo", "Unrecognized command wardrive_solo_caps trailing",
                     "log: Unknown command", "SSID: Unrecognized command", "Unknown command\nother output",
                     "@SOLO {", "@SOLO {}", None, b"Unrecognized command"]:
            with self.subTest(line=line):
                self.setUp();self.probe();self.now += .2
                self.c.handle_line(line)
                self.assert_no_proof()

    def test_wrong_transmitted_command_cannot_authorize_an_unsent_probe(self):
        self.probe(transmitted=False)
        self.assertFalse(self.c.command_sent("version"))
        self.now += .2;self.c.handle_line("Unrecognized command")
        self.assert_no_proof()

    def test_connection_context_cannot_be_rebound_after_transmission(self):
        self.probe()
        self.c.bind_capability_context(CONTEXT_B, negative_safe=True)
        self.now += .2
        self.c.handle_line("Unrecognized command")
        self.c.handle_line(caps())
        self.assert_no_proof()
        self.assertFalse(self.c.busy)

    def test_unbound_transmitted_probe_cannot_gain_context_after_transmission(self):
        self.probe(context=None)
        self.c.bind_capability_context(CONTEXT_A, negative_safe=True)
        self.now += .2;self.c.handle_line("Unrecognized command")
        self.assert_no_proof()

    def test_unsafe_transmitted_probe_cannot_gain_negative_authority_later(self):
        self.probe(negative_safe=False)
        self.c.bind_capability_context(CONTEXT_A, negative_safe=True)
        self.now += .2;self.c.handle_line("Unrecognized command")
        self.assert_no_proof()

    def test_cancelled_read_probe_has_no_late_positive_or_negative_result(self):
        self.probe()
        self.assertTrue(self.c.cancel())
        self.now += .2;self.c.handle_line("Unrecognized command");self.c.handle_line(caps())
        self.assert_no_proof()
        self.assertFalse(self.c.busy)
        self.stop.assert_not_called()

    def test_disconnect_invalidates_proof_and_late_responses(self):
        self.probe();self.now += .2;self.c.handle_line(caps())
        self.assertEqual(self.c.capability_status, "supported")
        self.c.disconnected();self.c.handle_line("Unrecognized command");self.c.handle_line(caps())
        self.assertEqual(self.c.capability_status, "unknown")
        self.assertIsNone(self.c.capabilities)
        self.assert_no_proof()

    def test_late_unknown_does_not_replace_a_valid_positive_reply(self):
        self.probe();self.now += .2;self.c.handle_line(caps())
        before=(self.c.capability_status, self.c.capability_received_at, self.c.capability_context)
        self.now += .1;self.c.handle_line("Unknown command")
        self.assertEqual((self.c.capability_status, self.c.capability_received_at,
                          self.c.capability_context), before)
        self.assertEqual(self.c.capability_status, "supported")
        self.assert_no_rf()

    def test_malformed_nonfinite_and_boolean_clocks_never_grant_negative_proof(self):
        for clock in [float("nan"), float("inf"), -float("inf"), "bad clock", True, [], None]:
            with self.subTest(clock=repr(clock)):
                self.setUp();self.probe();self.now = clock
                self.c.handle_line("Unrecognized command")
                self.assert_no_proof()

    def test_invalid_clocks_never_grant_positive_proof(self):
        for clock in [float("nan"), float("inf"), "bad clock", True, [], None]:
            with self.subTest(clock=repr(clock)):
                self.setUp();self.probe();self.now = clock
                self.c.handle_line(caps())
                self.assert_no_proof()

    def test_new_probe_clears_old_proof_and_needs_a_new_transmission(self):
        self.probe();self.now += .2;self.c.handle_line("Unrecognized command")
        self.assertEqual(self.c.capability_status, "unsupported")
        self.probe(context=CONTEXT_B, transmitted=False)
        self.c.handle_line("Unrecognized command")
        self.assert_no_proof()
        self.assertTrue(self.c.command_sent("wardrive_solo_caps"))
        self.now += .2;self.c.handle_line(caps())
        self.assertEqual(self.c.capability_status, "supported")
        self.assertEqual(self.c.capability_context, CONTEXT_B)
        self.assert_no_rf()

    def test_regular_scan_request_does_not_gain_legacy_migration_authority(self):
        self.assertTrue(self.c.request(wifi=True))
        self.c.bind_capability_context(CONTEXT_A, negative_safe=True)
        self.assertTrue(self.c.command_sent("wardrive_solo_caps"))
        self.now += .2;self.c.handle_line("Unrecognized command")
        self.assertNotEqual(self.c.capability_status, "unsupported")
        self.now += ACK_TIMEOUT;self.c.tick()
        self.assertFalse(self.c.desired_wifi)
        self.assertFalse(self.c.confirmed_wifi)
        self.assertEqual(self.start.call_count, 1)

    def test_rejected_arbiter_queue_has_no_capability_proof_or_radio_start(self):
        self.start.return_value=False
        self.assertFalse(self.c.probe_capabilities())
        self.c.bind_capability_context(CONTEXT_A, negative_safe=True)
        self.c.handle_line("Unrecognized command")
        self.assertEqual(self.c.capability_status, "unknown")
        self.assert_no_proof()

    def test_malformed_caps_do_not_become_unsupported_or_start_scanning(self):
        for changes in [dict(supported=False),dict(contract=2),dict(channels=[]),dict(firmware=""),
                        dict(clock_monotonic_us="bad"),dict(max_line_bytes=0)]:
            with self.subTest(changes=changes):
                self.setUp();self.probe();self.now += .2
                self.c.handle_line(caps(**changes))
                self.assert_no_proof()

    def test_invalid_transmission_clock_cannot_authorize_a_later_unknown_reply(self):
        for clock in [float("nan"),float("inf"),"bad",True,None]:
            with self.subTest(clock=repr(clock)):
                self.setUp();self.probe(transmitted=False)
                if clock is None:
                    self.now = None
                    self.assertFalse(self.c.command_sent("wardrive_solo_caps"))
                else:
                    self.assertFalse(self.c.command_sent("wardrive_solo_caps", monotonic=clock))
                self.c.handle_line("Unrecognized command", monotonic=10.2)
                self.assert_no_proof()
