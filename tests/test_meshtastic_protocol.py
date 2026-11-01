"""Software integration tests. No RF is transmitted; captures are external/private."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from watchdogs import meshtastic_manager as manager_module
from watchdogs.meshtastic_manager import MeshtasticManager
from watchdogs.meshtastic_protocol import (
    MAX_TEXT_BYTES, airtime_seconds, decode_packet, encode_text,
)
import test_meshtastic_rx_probe as probe_tests


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def blob(number, value):
    return varint(number * 8 + 2) + varint(len(value)) + value


def frame(port, payload, packet_id=0x11223344, source=0x9F90F6A0):
    # This test encoder does not call the production codec.
    plaintext = b"\x08" + varint(port) + blob(2, payload)
    return probe_tests.encrypted_frame(plaintext, source=source, packet_id=packet_id)


def metadata(**updates):
    result = {"crc_status": "pass", "crc_present": True, "header_error": False,
              "irq": 2, "rssi_dbm": -115.0, "snr_db": -7.5}
    result.update(updates)
    return result


class ProtocolTests(unittest.TestCase):
    def test_independent_known_ciphertext_public_text(self):
        packet = decode_packet(probe_tests.FRAME)
        self.assertEqual(packet["source_node_id"], "!9f90f6a0")
        self.assertEqual(packet["text"], "abc")
        self.assertFalse(packet["identity_authenticated"])
        self.assertFalse(packet["direct_range_to_source_confirmed"])

    def test_text_encoder_uses_meshtastic_header_and_ctr_nonce(self):
        encoded = encode_text("abc", 0x9F90F6A0, 0x11223344, hops=3)
        self.assertEqual(encoded[16:], probe_tests.FRAME[16:])
        to, source, packet_id, flags, channel, next_hop, relay = struct.unpack("<IIIBBBB", encoded[:16])
        self.assertEqual((to, source, packet_id, flags, channel, next_hop, relay),
                         (0xFFFFFFFF, 0x9F90F6A0, 0x11223344, 0x63, 0x20, 0, 0xA0))
        self.assertEqual(decode_packet(encoded)["text"], "abc")

    def test_utf8_byte_limit_and_packet_mtu_are_enforced(self):
        self.assertEqual(MAX_TEXT_BYTES, 233)
        good = encode_text("a" * 233, 0x12345678, 1)
        self.assertLessEqual(len(good), 255)
        self.assertEqual(len(decode_packet(good)["payload"]), 233)
        for text in ("", " ", "a" * 234, "ż" * 117):
            with self.subTest(text_length=len(text)), self.assertRaises(ValueError):
                encode_text(text, 0x12345678, 1)

    def test_longfast_manual_profile_has_its_own_public_hash(self):
        encoded = encode_text("abc", 0x12345678, 5, "longfast")
        self.assertEqual(encoded[13], 0x08)
        self.assertEqual(decode_packet(encoded, "longfast")["channel_name"], "LongFast")
        with self.assertRaises(ValueError):
            decode_packet(encoded, "sfnarrow")

    def test_no_public_chat_from_unicast_packet(self):
        encoded = frame(1, b"abc")
        encoded = struct.pack("<I", 0x98765432) + encoded[4:]
        self.assertIsNone(decode_packet(encoded)["text"])

    def test_nodeinfo_and_later_position_decode_optional_fields(self):
        user = blob(1, b"!9f90f6a0") + blob(2, b"Penny c900") + b"\x38\x02" + blob(8, bytes(range(32)))
        packet = decode_packet(frame(4, user))
        self.assertEqual(packet["name"], "Penny c900")
        self.assertEqual(packet["node_type"], "Router")
        self.assertEqual(packet["public_key"], bytes(range(32)))
        self.assertIsNone(packet["lat"])
        position = b"\x0d" + struct.pack("<i", 364700000) + b"\x15" + struct.pack("<i", -62000000)
        packet = decode_packet(frame(3, position))
        self.assertAlmostEqual(packet["lat"], 36.47)
        self.assertAlmostEqual(packet["lon"], -6.2)
        self.assertIsNone(packet["node_type"])

    def test_incomplete_position_never_invents_coordinates(self):
        packet = decode_packet(frame(3, b"\x0d" + struct.pack("<i", 364700000)))
        self.assertIsNone(packet["lat"])
        self.assertIsNone(packet["lon"])

    def test_bad_nodeinfo_id_utf8_and_position_bounds_rejected(self):
        bad_payloads = [(4, blob(1, b"!00000001")), (4, blob(2, b"\xff")),
                        (3, b"\x0d" + struct.pack("<i", 910000000) + b"\x15" + struct.pack("<i", 1)),
                        (1, b"\xff")]
        for port, payload in bad_payloads:
            with self.subTest(port=port), self.assertRaises(ValueError):
                decode_packet(frame(port, payload))

    def test_airtime_is_positive_and_profile_dependent(self):
        narrow = airtime_seconds(32, "sfnarrow")
        longfast = airtime_seconds(32, "longfast")
        self.assertGreater(narrow, 0)
        self.assertGreater(longfast, narrow)


class ManagerTests(unittest.TestCase):
    def test_nickname_persists_while_listening_without_tx_or_identity_change(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            identity = manager.node_id
            manager.running = manager.ready = True
            self.assertTrue(manager.set_node_name("  Żółty WDG  "))
            self.assertEqual(manager.node_name, "Żółty WDG")
            again = MeshtasticManager(home=folder)
            self.assertEqual(again.node_name, "Żółty WDG")
            self.assertEqual(again.node_id, identity)
            self.assertEqual(manager._tx_queue.qsize(), 0)

    def test_nickname_rejects_empty_controls_and_overlong_utf8(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            self.assertTrue(manager.set_node_name("a" * 40))
            for name in ("", "   ", "a\nb", "a\x00b", "a\x7fb", "ż" * 21, None):
                with self.subTest(name=name):
                    self.assertFalse(manager.set_node_name(name))
                    self.assertEqual(manager.node_name, "a" * 40)
            self.assertEqual(MeshtasticManager(home=folder).node_name, "a" * 40)

    def test_nickname_write_failure_preserves_previous_name(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            self.assertTrue(manager.set_node_name("Saved"))
            with patch.object(manager, "_save_state", side_effect=OSError("synthetic write failure")):
                self.assertFalse(manager.set_node_name("Not saved"))
            self.assertEqual(manager.node_name, "Saved")
            self.assertEqual(MeshtasticManager(home=folder).node_name, "Saved")

    def test_identity_and_profile_persist_without_opening_radio(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            identity = manager.node_id
            self.assertRegex(identity, r"^![0-9a-f]{8}$")
            self.assertTrue(manager.set_profile("longfast"))
            again = MeshtasticManager(home=folder)
            self.assertEqual(again.node_id, identity)
            self.assertEqual(again.profile_id, "longfast")
            self.assertFalse(again.running)
            self.assertFalse(again.ready)
            self.assertEqual(again.channel_name, "LongFast")

    def test_same_frame_relay_and_restart_do_not_repeat_callbacks(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            nodes, texts = [], []
            manager._on_node = lambda *args: nodes.append(args)
            manager._on_text = lambda *args: texts.append(args)
            self.assertTrue(manager._handle_frame(probe_tests.FRAME, metadata()))
            self.assertFalse(manager._handle_frame(probe_tests.FRAME, metadata()))
            relay = bytearray(probe_tests.FRAME)
            relay[12], relay[14], relay[15] = 0x61, 0x22, 0x33
            self.assertFalse(manager._handle_frame(bytes(relay), metadata()))
            restarted = MeshtasticManager(home=folder)
            restarted._on_node = lambda *args: nodes.append(args)
            restarted._on_text = lambda *args: texts.append(args)
            self.assertFalse(restarted._handle_frame(probe_tests.FRAME, metadata()))
            self.assertTrue(restarted._handle_frame(probe_tests.NEXT_PACKET, metadata()))
            self.assertEqual(len(nodes), 2)
            self.assertEqual(len(texts), 2)

    def test_known_node_metadata_merges_name_position_and_router_type(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            seen = []
            manager._on_node = lambda *args: seen.append(args)
            user = blob(1, b"!9f90f6a0") + blob(2, b"Test Router") + b"\x38\x02"
            manager._handle_frame(frame(4, user), metadata())
            position = b"\x0d" + struct.pack("<i", 364700000) + b"\x15" + struct.pack("<i", -62000000)
            manager._handle_frame(frame(3, position, packet_id=2), metadata())
            self.assertEqual(seen[-1][1:5], ("Router", "Test Router", 36.47, -6.2))

    def test_corrupt_config_is_preserved_and_does_not_reuse_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".watchdogs_meshtastic.json"
            path.write_text("[]")
            manager = MeshtasticManager(home=folder)
            self.assertFalse(manager.start())
            self.assertEqual(path.read_text(), "[]")
            manager.running = manager.ready = True
            self.assertIsNone(manager.send_message("test"))

    def test_crc_and_unknown_channel_do_not_emit_nodes_or_text(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            manager._on_node = Mock()
            manager._on_text = Mock()
            for details in (metadata(crc_status="failed"), metadata(crc_present=False),
                            metadata(header_error=True), metadata(irq=0x42)):
                self.assertFalse(manager._handle_frame(probe_tests.FRAME, details))
            unknown = bytearray(probe_tests.FRAME)
            unknown[13] = 0x8E
            self.assertFalse(manager._handle_frame(bytes(unknown), metadata()))
            manager._on_node.assert_not_called()
            manager._on_text.assert_not_called()

    def test_send_only_queues_and_reserves_nonce_no_delivery_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            self.assertIsNone(manager.send_message("test"))
            manager.running = manager.ready = True
            before = manager._state["packet_id"]
            token = manager.send_message("test")
            self.assertIsNotNone(token)
            queued_token, encoded = manager._tx_queue.get_nowait()
            self.assertEqual(token, queued_token)
            self.assertEqual(decode_packet(encoded)["text"], "test")
            self.assertEqual(MeshtasticManager(home=folder)._state["packet_id"], before + 1)


class TxSpiModel(probe_tests.SpiModel):
    """Transport model with real py-spidev input mutation and explicit TX IRQ."""
    def __init__(self, timeout=False):
        super().__init__()
        self.pending_tx = False
        self.timeout = timeout
        self.closed = False

    def close(self):
        self.closed = True

    def xfer2(self, data):
        opcode = data[0]
        response = super().xfer2(data)
        if opcode == 0x83:
            self.pending_tx = True
            self.mode = 0x60
        elif opcode == 0x82:
            self.pending_tx = False
        elif opcode == 0x12 and self.pending_tx:
            irq = 0x0200 if self.timeout else 1
            response[-2:] = [irq >> 8, irq & 255]
        data[:] = response
        return response


@contextmanager
def actual_radio(testcase, timeout=False):
    source = probe_tests.lorarf_source()
    if source is None or not source.is_file():
        testcase.skipTest("LoRaRF 1.4.0 source required for real-driver contract")
    spi = TxSpiModel(timeout)
    spidev = types.ModuleType("spidev")
    spidev.SpiDev = lambda: spi
    gpio = types.ModuleType("RPi.GPIO")
    gpio.BCM, gpio.IN, gpio.OUT, gpio.LOW, gpio.HIGH = 11, 1, 0, 0, 1
    gpio.setmode = gpio.setwarnings = gpio.setup = gpio.output = lambda *args: None
    gpio.cleanup = Mock()
    gpio.input = lambda pin: 0
    rpi = types.ModuleType("RPi")
    rpi.GPIO = gpio
    package = types.ModuleType("_mt_real_driver")
    package.__path__ = [str(source.parent)]
    spec = importlib.util.spec_from_file_location("_mt_real_driver.SX126x", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"spidev": spidev, "RPi": rpi, "RPi.GPIO": gpio,
                                 "_mt_real_driver": package, spec.name: module}):
        spec.loader.exec_module(module)
        radio = module.SX126x()
        testcase.assertTrue(radio.begin(bus=1, cs=0, reset=25, busy=24, irq=-1))
        yield radio, spi


class RealDriverTxTests(unittest.TestCase):
    def test_tx_done_both_profiles_and_full_rx_resume(self):
        for profile in ("sfnarrow", "longfast"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as folder, actual_radio(self) as (radio, spi):
                manager = MeshtasticManager(home=folder)
                manager.profile_id = profile
                packet = encode_text("test", 0x12345678, 9, profile)
                success, error = manager._transmit_frame(radio, packet)
                self.assertTrue(success)
                self.assertEqual(error, "")
                self.assertIn((0x0E, 0, *packet), spi.commands)
                self.assertTrue(any(command[0] == 0x83 for command in spi.commands))
                self.assertEqual(spi.mode, 0x50)
                self.assertTrue(spi.registers[0x0736] & 4)
                self.assertIn((0x82, 255, 255, 255), spi.commands)
                self.assertGreater(manager._state["next_tx_at"], manager_module.time.time())
                again = MeshtasticManager(home=folder)
                self.assertEqual(again._state["next_tx_at"], manager._state["next_tx_at"])

    def test_timeout_returns_false_and_still_resumes_rx(self):
        with tempfile.TemporaryDirectory() as folder, actual_radio(self, timeout=True) as (radio, spi):
            manager = MeshtasticManager(home=folder)
            success, error = manager._transmit_frame(radio, encode_text("test", 0x12345678, 8))
            self.assertFalse(success)
            self.assertIn("timed out", error)
            self.assertEqual(spi.mode, 0x50)

    def test_write_failure_resumes_rx(self):
        with tempfile.TemporaryDirectory() as folder, actual_radio(self) as (radio, spi):
            manager = MeshtasticManager(home=folder)
            with patch.object(radio, "writeBuffer", side_effect=OSError("synthetic SPI write failure")):
                success, error = manager._transmit_frame(radio, encode_text("test", 0x12345678, 8))
            self.assertFalse(success)
            self.assertIn("synthetic", error)
            self.assertEqual(spi.mode, 0x50)

    def test_worker_logs_config_and_bad_crc_without_node_callback_or_tx(self):
        with tempfile.TemporaryDirectory() as folder, actual_radio(self) as (radio, spi):
            manager = MeshtasticManager(home=folder)
            manager.running = True
            manager._on_node = Mock()
            events = []
            manager._on_radio_event = events.append
            module = sys.modules[radio.__module__]
            os_open = os.open
            def opened(path, flags, mode=0o777):
                if str(path) == "/run/lock/wdg-meshtastic-spi1.0.lock":
                    path = str(Path(folder) / "radio.lock")
                return os_open(path, flags, mode)
            def poll(_radio):
                manager._stop.set()
                return dict(metadata(crc_status="failed", irq=0x42),
                            event="rx_done", raw_frame_hex=probe_tests.FRAME.hex())
            fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=4, flock=lambda *args: None)
            with patch.dict(sys.modules, {"fcntl": fcntl}), \
                 patch.object(manager_module.os, "open", side_effect=opened), \
                 patch.object(manager_module.importlib, "import_module", return_value=module), \
                 patch.object(manager, "_radio_guard"), \
                 patch.object(manager_module, "poll_frame", side_effect=poll):
                manager._run()
            self.assertFalse(manager.running)
            self.assertFalse(manager.ready)
            self.assertTrue(manager.stop())
            manager._on_node.assert_not_called()
            module.gpio.cleanup.assert_called_once_with((25, 24))
            self.assertEqual(events[0]["event"], "rx_started")
            self.assertEqual(events[0]["rf_parameters"]["frequency_hz"], 869618000)
            self.assertEqual(events[1]["crc_status"], "failed")
            self.assertEqual(events[1]["raw_frame_hex"], probe_tests.FRAME.hex())
            self.assertTrue(all(e["profile_id"] == "sfnarrow" and e["logged_at_utc"] for e in events))
            self.assertFalse(any(command[0] == 0x83 for command in spi.commands))

    def test_failed_rx_restore_fails_pending_tx_and_releases_gpio(self):
        with tempfile.TemporaryDirectory() as folder, actual_radio(self) as (radio, spi):
            manager = MeshtasticManager(home=folder)
            manager.running = True
            manager._on_tx = Mock()
            packet = encode_text("test", 0x12345678, 8)
            manager._tx_queue.put(("local-token", packet))
            module = sys.modules[radio.__module__]
            os_open = os.open
            def opened(path, flags, mode=0o777):
                if str(path) == "/run/lock/wdg-meshtastic-spi1.0.lock":
                    path = str(Path(folder) / "radio.lock")
                return os_open(path, flags, mode)
            fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=4, flock=lambda *args: None)
            configure = manager_module.configure_radio
            calls = [0]
            def configure_then_fail(*args):
                calls[0] += 1
                if calls[0] > 1:
                    raise OSError("synthetic RX restore failed")
                return configure(*args)
            with patch.dict(sys.modules, {"fcntl": fcntl}), \
                 patch.object(manager_module.os, "open", side_effect=opened), \
                 patch.object(manager_module.importlib, "import_module", return_value=module), \
                 patch.object(manager, "_radio_guard"), \
                 patch.object(manager_module, "poll_frame", return_value=None), \
                 patch.object(manager_module, "configure_radio", side_effect=configure_then_fail):
                manager._run()
            self.assertFalse(manager.running)
            self.assertFalse(manager.ready)
            manager._on_tx.assert_called_once()
            self.assertEqual(manager._on_tx.call_args.args[:2], ("local-token", False))
            self.assertIn("RX restore failed", manager._on_tx.call_args.args[2])
            module.gpio.cleanup.assert_called_once_with((25, 24))


class PrivateCaptureReplayTests(unittest.TestCase):
    def test_real_capture_repeats_relay_new_node_and_process_restart(self):
        capture = os.environ.get("WDG_MESHTASTIC_REPLAY_LOG")
        if not capture:
            self.skipTest("private recorded capture supplied only via WDG_MESHTASTIC_REPLAY_LOG")
        rows = [json.loads(line) for line in Path(capture).read_text().splitlines()]
        valid = [r for r in rows if r.get("validation") == "public_ctr_data"]
        self.assertTrue(valid)
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            nodes = []
            manager._on_node = lambda *args: nodes.append(args)
            for row in valid:
                self.assertTrue(manager._handle_frame(bytes.fromhex(row["raw_frame_hex"]), row))
            count = len(nodes)
            self.assertEqual(count, len(valid))
            for row in valid:
                frame_bytes = bytes.fromhex(row["raw_frame_hex"])
                self.assertFalse(manager._handle_frame(frame_bytes, row))
                relay = bytearray(frame_bytes)
                relay[12] = (relay[12] & 0xF8) | max(0, (relay[12] & 7) - 1)
                relay[15] ^= 0x55
                self.assertFalse(manager._handle_frame(bytes(relay), row))
            restarted = MeshtasticManager(home=folder)
            restarted._on_node = lambda *args: nodes.append(args)
            for row in valid:
                self.assertFalse(restarted._handle_frame(bytes.fromhex(row["raw_frame_hex"]), row))
            self.assertEqual(len(nodes), count)
            self.assertEqual(manager._tx_queue.qsize(), 0)  # replay never transmits


if __name__ == "__main__":
    unittest.main()
