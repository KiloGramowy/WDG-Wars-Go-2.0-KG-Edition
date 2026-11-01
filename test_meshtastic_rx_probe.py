"""Software-only protocol/capture tests; synthetic fixtures and SPI are not RF evidence.

Set LORARF_SOURCE to the exact LoRaRF 1.4.0 LoRaRF/SX126x.py source file for
real-adapter byte-contract tests. An installed 1.4.0 distribution also works.
"""

import argparse
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
import importlib.util
from importlib.metadata import PackageNotFoundError, distribution
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import meshtastic_rx_probe as probe


# Independently encrypted static fixtures. Data protobuf = 08 01 12 03 61 62 63.
# Public default AES key, nonce <QII(packet id, source, 0)>; never captured RF.
SOURCE = "!9f90f6a0"
FRAME = bytes.fromhex("ffffffffa0f6909f44332211632099ee58eab110883953")
NEXT_PACKET = bytes.fromhex("ffffffffa0f6909f45332211632099ee293989c2b5d3a9")
LONGFAST_FRAME = bytes.fromhex("ffffffffa0f6909f44332211630899ee58eab110883953")
OPAQUE_FRAME = bytes.fromhex("ddccbbaa78563412efcdab90634299ee01020304")
UTC = "2026-10-28T22:24:00.123456+00:00"


def metadata(**changes):
    value = {"raw_frame_hex": FRAME.hex(), "received_at_utc": UTC,
             "rssi_dbm": -90.0, "snr_db": -2.0, "irq": 2,
             "crc_status": "pass", "crc_present": True, "header_error": False}
    value.update(changes)
    return value


def encrypted_frame(plaintext, source=0x9F90F6A0, packet_id=0x11223344, channel=0x20):
    """Test fixture encoder independent of production decoding/parser functions."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES(bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")),
                    modes.CTR(struct.pack("<QII", packet_id, source, 0))).encryptor()
    return struct.pack("<IIIBBBB", 0xFFFFFFFF, source, packet_id, 0x63, channel, 0x99, 0xEE) + cipher.update(plaintext) + cipher.finalize()


class ProfileAndHeaderTests(unittest.TestCase):
    def test_profiles_have_independent_verified_meshtastic_phy(self):
        for name, freq, sf, bw in (("longfast", 869525000, 11, 250000),
                                   ("sfnarrow", 869618000, 7, 62500)):
            with self.subTest(profile=name):
                p = probe.get_profile(name)
                self.assertEqual((p["frequency_hz"], p["sf"], p["bandwidth_hz"]), (freq, sf, bw))
                self.assertEqual(p["coding_rate_denominator"], 5)
                self.assertEqual(p["sync_word_sx126x"].lower(), "0x24b4")
                self.assertEqual(p["preamble_symbols"], 16)
                self.assertTrue(p["explicit_header"])
                self.assertTrue(p["crc"])
                self.assertFalse(p["inverted_iq"])
                self.assertFalse(p["ldro"])

    def test_profile_lookup_returns_fresh_mapping(self):
        p = probe.get_profile("sfnarrow")
        p["sf"] = 12
        self.assertEqual(probe.get_profile("sfnarrow")["sf"], 7)
        self.assertEqual(probe.get_profile("longfast")["sf"], 11)

    def test_source_is_from_little_endian_not_destination_packet_or_relay(self):
        result = probe.decode_header(FRAME)
        self.assertEqual(result["source_node_id"], SOURCE)
        self.assertEqual(result["destination_node_id"], "!ffffffff")
        self.assertEqual(result["packet_id"], "0x11223344")
        self.assertEqual(result["relay_hint"], 0xEE)
        self.assertEqual(result["hop_limit"], 3)
        self.assertEqual(result["hop_start"], 3)
        self.assertFalse(result["identity_authenticated"])

    def test_source_preserves_leading_zeroes(self):
        frame = FRAME[:4] + bytes.fromhex("01000000") + FRAME[8:]
        self.assertEqual(probe.decode_header(frame)["source_node_id"], "!00000001")

    def test_old_header_does_not_interpret_relay_hints(self):
        frame = FRAME[:12] + b"\x03" + FRAME[13:]
        result = probe.decode_header(frame)
        self.assertIsNone(result["relay_hint"])
        self.assertIsNone(result["next_hop_hint"])

    def test_truncated_header_empty_payload_and_oversize_rejected(self):
        for frame in (b"", FRAME[:15], FRAME[:16], FRAME + b"\0" * 256):
            with self.subTest(length=len(frame)), self.assertRaises(ValueError):
                probe.decode_header(frame)

    def test_zero_and_broadcast_source_rejected(self):
        for source in (b"\0" * 4, b"\xff" * 4):
            with self.subTest(source=source), self.assertRaises(ValueError):
                probe.decode_header(FRAME[:4] + source + FRAME[8:])

    def test_reference_id_requires_all_eight_hex_digits(self):
        self.assertEqual(probe.node_id("!ABCDEF12"), "!abcdef12")
        for invalid in ("12345678", "!1234", "!123456789", "!00000000", "!ffffffff", "JA f6a0"):
            with self.subTest(value=invalid), self.assertRaises(argparse.ArgumentTypeError):
                probe.node_id(invalid)


class PublicProtocolValidationTests(unittest.TestCase):
    def test_independent_cipher_fixture_decodes_public_data(self):
        for name, frame in (("sfnarrow", FRAME), ("longfast", LONGFAST_FRAME)):
            with self.subTest(profile=name):
                result = probe.decode_public_data(frame, probe.get_profile(name))
                self.assertEqual(result["portnum"], 1)
                self.assertEqual(result["payload_bytes"], 3)
                self.assertFalse(result["signature_present"])
        self.assertEqual(encrypted_frame(bytes.fromhex("08011203616263")), FRAME)

    def test_channel_name_or_reference_id_cannot_validate_arbitrary_bytes(self):
        tracker = probe.PacketTracker(probe.get_profile("sfnarrow"), references=(SOURCE,))
        random_payload = FRAME[:16] + bytes.fromhex("00000000000000")
        result = tracker.observe(random_payload, hardware=True, metadata=metadata(raw_frame_hex=random_payload.hex()))
        self.assertNotEqual(result["event"], "meshtastic_data")
        self.assertFalse(result["new_node"])
        self.assertEqual(tracker.summary()["unique_nodes"], 0)

    def test_wrong_channel_hash_stays_unverified(self):
        wrong_channel = FRAME[:13] + b"\xff" + FRAME[14:]
        with self.assertRaises(ValueError):
            probe.decode_public_data(wrong_channel, probe.get_profile("sfnarrow"))
        result = probe.PacketTracker(probe.get_profile("sfnarrow")).observe(wrong_channel)
        self.assertEqual(result["validation"], "unverified_payload")
        self.assertFalse(result["new_node"])

    def test_nonce_source_and_packet_id_are_both_required(self):
        for bad in (FRAME[:4] + bytes.fromhex("a1f6909f") + FRAME[8:],
                    FRAME[:8] + bytes.fromhex("45332211") + FRAME[12:]):
            with self.subTest(frame=bad.hex()), self.assertRaises(ValueError):
                probe.decode_public_data(bad, probe.get_profile("sfnarrow"))

    def test_malformed_data_never_promotes_node(self):
        malformed = (b"", b"\0", bytes.fromhex("0800"), bytes.fromhex("0801"), bytes.fromhex("0880"),
                     bytes.fromhex("080112056162"), bytes.fromhex("08010801"),
                     bytes.fromhex("0a0101"), bytes.fromhex("0801120161120162"),
                     bytes.fromhex("08011802"), bytes.fromhex("0801ff"))
        for plain in malformed:
            with self.subTest(plain=plain.hex()):
                tracker = probe.PacketTracker(probe.get_profile("sfnarrow"))
                result = tracker.observe(encrypted_frame(plain))
                self.assertNotEqual(result["event"], "meshtastic_data")
                self.assertEqual(tracker.summary()["unique_nodes"], 0)

    def test_registered_routing_port_allows_empty_payload(self):
        result = probe.decode_public_data(encrypted_frame(bytes.fromhex("0805")),
                                          probe.get_profile("sfnarrow"))
        self.assertEqual(result["portnum"], 5)
        self.assertEqual(result["payload_bytes"], 0)

    def test_fixed32_data_fields_and_private_port_range_are_supported(self):
        plain = bytes.fromhex("08800212016125010000002d0200000035030000003d0400000045050000004801")
        result = probe.decode_public_data(encrypted_frame(plain), probe.get_profile("sfnarrow"))
        self.assertEqual(result["portnum"], 256)
        self.assertEqual(result["payload_bytes"], 1)

    def test_unknown_future_fields_or_formats_remain_unverified(self):
        for plain in (bytes.fromhex("08011201615801"), bytes.fromhex("088f4e120161")):
            with self.subTest(plain=plain.hex()):
                result = probe.PacketTracker(probe.get_profile("sfnarrow")).observe(encrypted_frame(plain))
                self.assertEqual(result["validation"], "unverified_payload")
                self.assertFalse(result["new_node"])

    def test_signature_presence_is_not_signature_verification(self):
        plain = bytes.fromhex("08011201615240") + b"\x55" * 64
        frame = encrypted_frame(plain)
        result = probe.PacketTracker(probe.get_profile("sfnarrow")).observe(frame)
        self.assertEqual(result["event"], "meshtastic_data")
        self.assertTrue(result["signature_present"])
        self.assertFalse(result["signature_verified"])
        self.assertFalse(result["identity_authenticated"])


class DedupAndCaptureTests(unittest.TestCase):
    def tracker(self, references=()):
        return probe.PacketTracker(probe.get_profile("sfnarrow"), references=references)

    def test_new_node_new_packet_and_duplicate_are_separate(self):
        tracker = self.tracker((SOURCE,))
        first = tracker.observe(FRAME, hardware=True, metadata=metadata())
        next_packet = tracker.observe(NEXT_PACKET, hardware=True,
                                      metadata=metadata(raw_frame_hex=NEXT_PACKET.hex()))
        duplicate = tracker.observe(FRAME, hardware=True, metadata=metadata())
        self.assertEqual(first["packet_kind"], "new_node")
        self.assertEqual(next_packet["packet_kind"], "new_packet")
        self.assertEqual(duplicate["packet_kind"], "duplicate_packet")
        self.assertTrue(first["new_node"])
        self.assertFalse(next_packet["new_node"])
        self.assertTrue(duplicate["duplicate_packet"])
        summary = tracker.summary()
        self.assertEqual(summary["accepted_frames"], 3)
        self.assertEqual(summary["unique_nodes"], 1)
        self.assertEqual(summary["unique_packets"], 2)
        self.assertEqual(summary["duplicate_packets"], 1)
        self.assertEqual(summary["reference_matches"], [SOURCE])
        self.assertEqual(summary["reference_match_count"], 1)

    def test_relay_and_hop_changes_do_not_create_new_packet_or_prove_direct_range(self):
        tracker = self.tracker()
        tracker.observe(FRAME)
        relayed = FRAME[:12] + bytes([0x62, 0x20, 0x33, 0x44]) + FRAME[16:]
        result = tracker.observe(relayed)
        self.assertTrue(result["duplicate_packet"])
        self.assertEqual(result["source_node_id"], SOURCE)
        self.assertFalse(result["identity_authenticated"])
        self.assertIsNone(result["direct_transmitter_node_id"])
        self.assertFalse(result["direct_range_to_source_confirmed"])

    def test_packet_id_reuse_with_changed_payload_is_not_silently_discarded(self):
        tracker = self.tracker()
        tracker.observe(FRAME)
        other = encrypted_frame(bytes.fromhex("08011203616264"))
        result = tracker.observe(other)
        self.assertFalse(result["duplicate_packet"])
        self.assertTrue(result["packet_id_conflict"])
        self.assertEqual(tracker.summary()["unique_nodes"], 1)
        self.assertEqual(tracker.summary()["unique_packets"], 2)

    def test_same_packet_id_from_another_author_is_a_new_node(self):
        tracker = self.tracker()
        tracker.observe(FRAME)
        other = encrypted_frame(bytes.fromhex("08011203616263"), source=0x06CEC900)
        result = tracker.observe(other)
        self.assertTrue(result["new_node"])
        self.assertFalse(result["duplicate_packet"])
        self.assertFalse(result["packet_id_conflict"])
        self.assertEqual(result["source_node_id"], "!06cec900")
        self.assertEqual(tracker.summary()["unique_nodes"], 2)

    def test_malformed_header_cannot_create_any_node_or_packet(self):
        tracker = self.tracker()
        result = tracker.observe(b"random", hardware=True, metadata=metadata())
        self.assertEqual(result["validation"], "rejected_header")
        self.assertIsNone(result["source_node_id"])
        self.assertFalse(result["new_node"])
        self.assertFalse(result["duplicate_packet"])
        self.assertEqual(tracker.summary()["unique_packets"], 0)

    def test_crc_failure_absence_and_header_errors_never_add_node(self):
        for values in ({"crc_status": "failed"}, {"crc_status": "absent"},
                       {"crc_status": "not_checkable"}, {"header_error": True},
                       {"crc_present": False}, {"irq": probe.CRC_ERR | probe.RX_DONE},
                       {"irq": probe.HEADER_ERR | probe.RX_DONE},
                       {"irq": probe.TIMEOUT | probe.RX_DONE}):
            with self.subTest(values=values):
                tracker = self.tracker((SOURCE,))
                result = tracker.observe(FRAME, hardware=True, metadata=metadata(**values))
                self.assertEqual(result["validation"], "rejected_crc")
                self.assertFalse(result["new_node"])
                self.assertEqual(tracker.summary()["unique_nodes"], 0)
                self.assertEqual(result["raw_frame_hex"], FRAME.hex())

    def test_capture_contains_utc_raw_signal_and_from_without_auth_claim(self):
        result = self.tracker().observe(FRAME, hardware=True, metadata=metadata())
        self.assertEqual(result["raw_frame_hex"], FRAME.hex())
        self.assertEqual(result["received_at_utc"], UTC)
        self.assertEqual(result["source_node_id"], SOURCE)
        self.assertEqual((result["rssi_dbm"], result["snr_db"]), (-90.0, -2.0))
        self.assertEqual(result["crc_status"], "pass")
        self.assertFalse(result["identity_authenticated"])

    def test_missing_crc_presence_cannot_promote_a_radio_frame(self):
        reported = metadata()
        del reported["crc_present"]
        tracker = self.tracker()
        result = tracker.observe(FRAME, hardware=True, metadata=reported)
        self.assertEqual(result["validation"], "rejected_crc")
        self.assertFalse(result["new_node"])
        self.assertEqual(tracker.summary()["unique_nodes"], 0)

    def test_empty_session_is_inconclusive_not_radio_failure(self):
        summary = self.tracker().summary()
        self.assertEqual(summary["result"], "inconclusive_no_packets")
        self.assertEqual(summary["accepted_frames"], 0)
        self.assertEqual(summary["unique_nodes"], 0)

    def test_jsonl_sink_exclusive_create_flush_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.jsonl"
            output = io.StringIO()
            with redirect_stdout(output), probe.JsonlSink(path) as sink:
                sink.emit({"event": "test", "received_at_utc": UTC})
                self.assertEqual(json.loads(path.read_text())["event"], "test")
            self.assertEqual(json.loads(output.getvalue())["received_at_utc"], UTC)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError), probe.JsonlSink(path):
                pass
            self.assertEqual(path.read_bytes(), original)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_raw_capture_must_stay_outside_source_repository(self):
        target = Path(__file__).resolve().parent / "never-created-capture.jsonl"
        with self.assertRaisesRegex(ValueError, "outside"), probe.JsonlSink(target):
            pass
        self.assertFalse(target.exists())


class ReceiveTests(unittest.TestCase):
    def radio(self, irq=probe.RX_DONE, crc_present=True):
        radio = Mock()
        radio.STANDBY_RC = 0
        radio.IRQ_ALL = 0x03FF
        radio.RX_CONTINUOUS = 0xFFFFFF
        radio.STATUS_MODE_RX = 0x50
        radio.getMode.return_value = 0x50
        radio.getIrqStatus.return_value = irq
        radio.getRxBufferStatus.return_value = (len(FRAME), 37)
        radio.readBuffer.return_value = tuple(FRAME)
        radio.readRegister.return_value = (0x10 if crc_present else 0,)
        radio.getPacketStatus.return_value = (180, 248, 190)
        return radio

    def assert_rearmed(self, radio):
        self.assertEqual(radio.mock_calls[-3:], [unittest.mock.call.clearIrqStatus(0x03FF),
                                               unittest.mock.call.setRx(0xFFFFFF),
                                               unittest.mock.call.getMode()])

    def test_good_frame_copies_raw_checks_present_crc_and_decodes_signed_snr(self):
        radio = self.radio()
        record = probe.poll_frame(radio)
        self.assertEqual(record["raw_frame_hex"], FRAME.hex())
        self.assertEqual(record["crc_status"], "pass")
        self.assertEqual((record["rssi_dbm"], record["snr_db"]), (-90.0, -2.0))
        self.assertIn("+00:00", record["received_at_utc"])
        radio.readRegister.assert_called_with(0x076B, 1)
        self.assertLess(radio.mock_calls.index(unittest.mock.call.setStandby(0)),
                        radio.mock_calls.index(unittest.mock.call.readBuffer(37, len(FRAME))))
        self.assert_rearmed(radio)

    def test_crc_failed_complete_frame_is_logged_raw_but_not_promoted(self):
        radio = self.radio(probe.RX_DONE | probe.CRC_ERR)
        record = probe.poll_frame(radio)
        self.assertEqual(record["raw_frame_hex"], FRAME.hex())
        self.assertEqual(record["crc_status"], "failed")
        result = probe.PacketTracker(probe.get_profile("sfnarrow")).observe(
            FRAME, hardware=True, metadata=record)
        self.assertEqual(result["validation"], "rejected_crc")
        self.assertFalse(result["new_node"])
        self.assert_rearmed(radio)

    def test_absent_crc_is_not_presented_as_pass(self):
        record = probe.poll_frame(self.radio(crc_present=False))
        self.assertEqual(record["crc_status"], "absent")

    def test_header_error_or_timeout_is_not_a_good_frame(self):
        for error in (probe.HEADER_ERR, probe.TIMEOUT):
            with self.subTest(error=error):
                radio = self.radio(probe.RX_DONE | error)
                record = probe.poll_frame(radio)
                self.assertNotEqual(record["crc_status"], "pass")
                if error == probe.HEADER_ERR:
                    self.assertTrue(record["header_error"])
                self.assert_rearmed(radio)

    def test_error_appearing_after_standby_is_not_lost(self):
        radio = self.radio()
        radio.getIrqStatus.side_effect = [probe.RX_DONE, probe.RX_DONE | probe.CRC_ERR]
        record = probe.poll_frame(radio)
        self.assertEqual(record["crc_status"], "failed")
        self.assert_rearmed(radio)

    def test_io_error_still_rearms_rx(self):
        radio = self.radio()
        radio.readBuffer.side_effect = OSError("synthetic SPI read failure")
        with self.assertRaises(OSError):
            probe.poll_frame(radio)
        self.assert_rearmed(radio)

    def test_short_spi_read_never_produces_valid_record(self):
        radio = self.radio()
        radio.readBuffer.return_value = tuple(FRAME[:-1])
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            probe.poll_frame(radio)
        self.assert_rearmed(radio)

    def test_invalid_radio_length_is_not_copied_from_fifo(self):
        for length in (0, 256):
            with self.subTest(length=length):
                radio = self.radio()
                radio.getRxBufferStatus.return_value = (length, 0)
                record = probe.poll_frame(radio)
                self.assertIsNone(record["raw_frame_hex"])
                self.assertNotEqual(record["crc_status"], "pass")
                radio.readBuffer.assert_not_called()
                self.assert_rearmed(radio)

    def test_idle_does_not_touch_fifo_or_restart_radio(self):
        radio = self.radio(0)
        self.assertIsNone(probe.poll_frame(radio))
        radio.setStandby.assert_not_called()
        radio.readBuffer.assert_not_called()
        radio.setRx.assert_not_called()

    def test_rearm_failure_reports_error_not_silent_no_packets(self):
        radio = self.radio(probe.RX_DONE | probe.CRC_ERR)
        radio.getMode.return_value = 0x20
        with self.assertRaisesRegex(RuntimeError, "did not enter RX"):
            probe.poll_frame(radio)


class CliTests(unittest.TestCase):
    def test_default_and_offline_never_import_hardware(self):
        for args in ([], ["--profile", "sfnarrow"],
                     ["--profile", "sfnarrow", "--frame-hex", FRAME.hex()]):
            output = io.StringIO()
            with self.subTest(args=args), redirect_stdout(output), patch.object(
                    probe.importlib, "import_module", side_effect=AssertionError("hardware import")):
                self.assertEqual(probe.main(args), 0)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(records)
            if "sfnarrow" in args:
                self.assertIn("869618000", output.getvalue())

    def test_offline_in_fresh_interpreter_has_no_gpio_spi_modules(self):
        snippet = ("import sys,meshtastic_rx_probe as p; "
                   f"assert p.main(['--profile','sfnarrow','--frame-hex','{FRAME.hex()}'])==0; "
                   "assert not any(k.startswith(('LoRaRF','RPi','spidev')) for k in sys.modules)")
        result = subprocess.run([sys.executable, "-B", "-c", snippet],
                                cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_live_requires_output_but_no_second_own_device(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            probe.main(["--listen"])
        self.assertEqual(raised.exception.code, 2)
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()), patch.object(
                probe, "listen", return_value=0) as live:
            self.assertEqual(probe.main(["--listen", "--profile", "sfnarrow", "--output",
                                         str(Path(folder) / "capture.jsonl")]), 0)
            live.assert_called_once()

    def test_invalid_profile_or_window_is_rejected_before_radio_work(self):
        for args in (["--profile", "meshcore"], ["--seconds", "0"], ["--seconds", "3601"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe.main(args)


class ListenAssemblyTests(unittest.TestCase):
    """Exercise CLI -> live loop -> JSONL -> cleanup without a Linux device."""
    def exercise(self, *, interrupt=False, begin=True, begin_error=None):
        original_spi = Mock()
        gpio = Mock()
        radio = Mock()
        radio.STANDBY_RC = 0
        radio.begin.return_value = begin
        radio.begin.side_effect = begin_error
        module = types.SimpleNamespace(spi=original_spi, gpio=gpio,
                                       SX126x=Mock(return_value=radio))
        old_handler = object()
        output, errors = io.StringIO(), io.StringIO()
        profile = probe.get_profile("sfnarrow")

        def imported(name):
            if name == "LoRaRF.SX126x":
                return module
            if name == "cryptography.hazmat.primitives.ciphers":
                return object()  # dependency check only; fixtures already imported crypto.
            raise AssertionError(f"unexpected import {name}")

        def received(active_radio):
            self.assertIs(active_radio, radio)
            self.assertIsInstance(module.spi, probe.SpiAudit)
            if interrupt:
                raise KeyboardInterrupt
            return metadata()

        # Import crypto before mocking import_module: payload validation still uses
        # the real library, and the transport assembly alone is substituted.
        from cryptography.hazmat.primitives import ciphers  # noqa: F401
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            capture = Path(folder) / "passive.jsonl"
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            stack.enter_context(patch.object(probe.sys, "platform", "linux"))
            stack.enter_context(patch.object(probe, "radio_lock", return_value=nullcontext()))
            checked = stack.enter_context(patch.object(probe, "preflight"))
            stack.enter_context(patch.object(probe, "version", return_value="1.4.0"))
            stack.enter_context(patch.object(probe.importlib, "import_module", side_effect=imported))
            configured = stack.enter_context(patch.object(probe, "configure_radio", return_value={
                "sync_hex": "24b4", "iq_register": 0xAC, "mode": "RX"}))
            stack.enter_context(patch.object(probe.SpiAudit, "report", return_value={
                "applied_rf": profile, "rf_measured": False, "spi_commands": {}}))
            stack.enter_context(patch.object(probe, "poll_frame", side_effect=received))
            stack.enter_context(patch.object(probe.time, "monotonic", side_effect=[0, 0, 2]))
            stack.enter_context(patch.object(probe.time, "sleep"))
            stack.enter_context(patch.object(probe.signal, "getsignal", return_value=old_handler))
            changed_signal = stack.enter_context(patch.object(probe.signal, "signal"))
            code = probe.main(["--listen", "--profile", "sfnarrow", "--seconds", "1",
                               "--compare-node-id", SOURCE, "--output", str(capture)])
            records = [json.loads(line) for line in capture.read_text().splitlines()]
            checked.assert_called_once()
            self.assertIs(module.spi, original_spi)
            original_spi.close.assert_called_once_with()
            self.assertEqual(changed_signal.call_args,
                             unittest.mock.call(probe.signal.SIGTERM, old_handler))
            radio.begin.assert_called_once_with(bus=1, cs=0, reset=25, busy=24, irq=-1)
        return code, records, json.loads(errors.getvalue()) if errors.getvalue() else None, radio, gpio, configured

    def test_single_passive_frame_reaches_capture_summary_and_targeted_cleanup(self):
        code, records, error, radio, gpio, configured = self.exercise()
        self.assertEqual(code, 0)
        self.assertIsNone(error)
        self.assertEqual([r["event"] for r in records],
                         ["configuration", "rx_started", "meshtastic_data", "summary"])
        self.assertEqual(records[0]["rf_state"], "not_applied")
        self.assertFalse(records[1]["rf_measured"])
        self.assertEqual(records[2]["source_node_id"], SOURCE)
        self.assertEqual(records[2]["crc_status"], "pass")
        self.assertEqual(records[2]["raw_frame_hex"], FRAME.hex())
        self.assertEqual(records[-1]["unique_nodes"], 1)
        self.assertEqual(records[-1]["unique_packets"], 1)
        self.assertFalse(records[-1]["interrupted"])
        configured.assert_called_once()
        radio.setStandby.assert_called_once_with(0)
        gpio.cleanup.assert_called_once_with((25, 24))

    def test_keyboard_interrupt_still_records_summary_cleans_up_and_returns_130(self):
        code, records, error, radio, gpio, _ = self.exercise(interrupt=True)
        self.assertEqual(code, 130)
        self.assertIsNone(error)
        self.assertEqual([r["event"] for r in records], ["configuration", "rx_started", "summary"])
        self.assertTrue(records[-1]["interrupted"])
        self.assertEqual(records[-1]["unique_nodes"], 0)
        radio.setStandby.assert_called_once_with(0)
        gpio.cleanup.assert_called_once_with((25, 24))

    def test_unsuccessful_begin_never_issues_standby_and_only_frees_known_pins(self):
        for begin, failure in ((False, None), (True, OSError("synthetic SPI begin failure"))):
            with self.subTest(begin=begin, raises=failure is not None):
                code, records, error, radio, gpio, configured = self.exercise(
                    begin=begin, begin_error=failure)
                self.assertEqual(code, 2)
                self.assertEqual(error["event"], "error")
                self.assertEqual([r["event"] for r in records], ["configuration"])
                configured.assert_not_called()
                radio.setStandby.assert_not_called()
                if failure is None:
                    gpio.cleanup.assert_called_once_with((25, 24))
                else:
                    gpio.cleanup.assert_not_called()


class SpiModel:
    """Minimal register/command transport model, not a physical radio simulator."""
    def __init__(self):
        self.commands = []
        self.registers = {0x0736: 0xA8, 0x076B: 0x10}
        self.mode = 0x20

    def open(self, bus, cs):
        self.bus_cs = (bus, cs)

    def xfer2(self, data):
        if any(not isinstance(byte, int) or not 0 <= byte <= 255 for byte in data):
            raise ValueError("SPI command contains a non-byte")
        self.commands.append(tuple(data))
        response = [0] * len(data)
        opcode = data[0]
        if opcode == 0x0D:
            address = data[1] * 256 + data[2]
            self.registers.update({address + i: byte for i, byte in enumerate(data[3:])})
        elif opcode == 0x1D:
            address = data[1] * 256 + data[2]
            response[4:] = [self.registers.get(address + i, 0) for i in range(len(data) - 4)]
        elif opcode == 0xC0:
            response[1] = self.mode
        elif opcode == 0x80:
            self.mode = 0x20
        elif opcode == 0x82:
            self.mode = 0x50
        return response


def lorarf_source():
    specified = os.environ.get("LORARF_SOURCE")
    if specified:
        return Path(specified)
    try:
        dist = distribution("LoRaRF")
    except PackageNotFoundError:
        return None
    if dist.version != "1.4.0":
        return None
    return Path(dist.locate_file("LoRaRF/SX126x.py"))


class ActualLoRaRF140ContractTests(unittest.TestCase):
    def test_real_adapter_sends_both_verified_rx_only_profiles(self):
        source = lorarf_source()
        if source is None or not source.is_file():
            self.skipTest("install LoRaRF 1.4.0 or set LORARF_SOURCE for real-adapter tests")
        for name, rf_bytes, modulation in (
            ("longfast", (0x36, 0x58, 0x66, 0x66), (11, 0x05, 1, 0)),
            ("sfnarrow", (0x36, 0x59, 0xE3, 0x53), (7, 0x03, 1, 0)),
        ):
            with self.subTest(profile=name):
                spi = SpiModel()
                spidev = types.ModuleType("spidev")
                audit = probe.SpiAudit(spi)
                spidev.SpiDev = lambda: audit
                gpio = types.ModuleType("RPi.GPIO")
                gpio.BCM, gpio.IN, gpio.OUT, gpio.LOW, gpio.HIGH = 11, 1, 0, 0, 1
                gpio.setmode = gpio.setwarnings = gpio.setup = gpio.output = lambda *args: None
                gpio.input = lambda pin: 0
                rpi = types.ModuleType("RPi")
                rpi.GPIO = gpio
                package_name = "_probe_lorarf140"
                package = types.ModuleType(package_name)
                package.__path__ = [str(source.parent)]
                spec = importlib.util.spec_from_file_location(package_name + ".SX126x", source)
                module = importlib.util.module_from_spec(spec)
                modules = {"spidev": spidev, "RPi": rpi, "RPi.GPIO": gpio,
                           package_name: package, spec.name: module}
                with patch.dict(sys.modules, modules):
                    spec.loader.exec_module(module)
                    radio = module.SX126x()
                    self.assertTrue(radio.begin(bus=1, cs=0, reset=25, busy=24, irq=-1))
                    readback = probe.configure_radio(radio, probe.get_profile(name))
                self.assertEqual(spi.bus_cs, (1, 0))
                self.assertIn((0x86,) + rf_bytes, spi.commands)
                self.assertIn((0x8B,) + modulation + (0, 0, 0, 0), spi.commands)
                self.assertIn((0x8C, 0, 16, 0, 255, 1, 0, 0, 0, 0), spi.commands)
                self.assertIn((0x97, 2, 0, 2, 0x80), spi.commands)
                self.assertIn((0x9D, 1), spi.commands)
                self.assertIn((0x0D, 0x07, 0x40, 0x24, 0xB4), spi.commands)
                self.assertEqual(spi.registers[0x0736], 0xAC)
                self.assertIn((0x82, 0xFF, 0xFF, 0xFF), spi.commands)
                self.assertIn((0x08, 0x02, 0x62, 0, 0, 0, 0, 0, 0), spi.commands)
                self.assertEqual(spi.mode, 0x50)
                self.assertEqual(readback["mode"], "RX")
                self.assertEqual(readback["sync_hex"], "24b4")
                self.assertEqual(readback["iq_register"], 0xAC)
                report = audit.report(probe.get_profile(name))
                self.assertEqual(report["applied_rf"]["profile_id"], name)
                self.assertEqual(report["spi_commands"]["0x86"], bytes((0x86,) + rf_bytes).hex())
                self.assertLess(abs(report["pll_frequency_hz"] - probe.get_profile(name)["frequency_hz"]), 1)
                self.assertFalse(report["rf_measured"])
                self.assertFalse(any(command[0] in (0x83, 0x0E) for command in spi.commands),
                                 "RX-only setup must never set TX or write TX payload")
                # A later incorrect SPI write must not be reported as the chosen profile.
                audit.xfer2([0x86, 0, 0, 0, 0])
                with self.assertRaisesRegex(RuntimeError, "do not match"):
                    audit.report(probe.get_profile(name))

    def test_no_observed_spi_configuration_is_not_reported_as_applied(self):
        audit = probe.SpiAudit(SpiModel())
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            audit.report(probe.get_profile("sfnarrow"))


if __name__ == "__main__":
    unittest.main()
