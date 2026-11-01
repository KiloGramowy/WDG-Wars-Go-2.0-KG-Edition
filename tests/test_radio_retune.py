"""RF profile changes with fake workers/transports; no radio or live data."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import meshtastic_rx_probe as probe
import test_meshtastic_rx_probe as fixtures
from test_meshtastic_protocol import actual_radio, metadata
from watchdogs.lora_manager import LoRaManager, MESHCORE_PRESETS, make_private_channel
from watchdogs.meshtastic_manager import MeshtasticManager
from watchdogs.meshtastic_protocol import decode_packet
from watchdogs.radio_profiles import get_meshtastic_rf


class MeshCoreRetuneTests(unittest.TestCase):
    def manager(self):
        manager = LoRaManager()
        manager.running = manager.ready = True
        manager.mode = "meshcore"
        return manager

    def test_verified_presets_keep_saved_wdg_legacy_key(self):
        self.assertEqual(MESHCORE_PRESETS["eu_uk_narrow"][:4], (869618000, 8, 5, 62500))
        self.assertIn("legacy", MESHCORE_PRESETS["eu_uk_narrow"][4])
        self.assertEqual(MESHCORE_PRESETS["eu_uk_narrow_v2"][:4], (869618000, 8, 8, 62500))
        self.assertEqual(MESHCORE_PRESETS["us_recommended"][:4], (910525000, 7, 5, 62500))
        # Its required 3-byte path encoding is not supported by this manager.
        self.assertNotIn("us_socal", MESHCORE_PRESETS)

    def test_queued_and_inflight_tx_defer_without_stopping_or_dequeue(self):
        for active in (False, True):
            with self.subTest(active=active):
                manager = self.manager()
                manager._tx_active = active
                if not active:
                    manager._tx_queue.put(b"queued frame")
                with patch.object(manager, "stop") as stop:
                    self.assertFalse(manager.retune_profile("eu_uk_narrow_v2"))
                stop.assert_not_called()
                self.assertIn("deferred", manager.last_error)
                self.assertEqual(manager.profile_id, "eu_uk_narrow")
                self.assertEqual(manager._tx_queue.qsize(), 0 if active else 1)

    def test_real_stop_start_preserves_rx_chat_channel_key_and_callbacks(self):
        manager = LoRaManager()
        cleanup = []
        configurations = []
        def worker(*args):
            configurations.append(args[:4] + (args[6],))
            manager.ready = True
            manager._ready_event.set()
            manager._stop_event.wait(2)
            # A join while holding the guard would deadlock this cleanup.
            with manager._tx_guard:
                cleanup.append(True)
                manager.ready = manager.running = False
        with patch.object(manager, "_run_sniffer", side_effect=worker), \
                patch.object(manager, "send_meshcore_advert") as advert:
            try:
                self.assertTrue(manager.start_meshcore())
                manager._seen_packets[b"old RX"] = 123
                manager.packets_received = 17
                manager._tx_dedup_keys.add(b"our previous TX")
                keypair = object()
                manager._mc_keypair = keypair
                channel = make_private_channel("private", "11" * 16)
                manager.set_mc_channels([channel])
                callback = manager._on_message = Mock()
                self.assertTrue(manager.retune_profile("eu_uk_narrow_v2"))
                self.assertEqual(configurations, [(869618000, 8, 5, 62500, 16), (869618000, 8, 8, 62500, 32)])
                self.assertEqual(manager._seen_packets, {b"old RX": 123})
                self.assertEqual(manager.packets_received, 17)
                self.assertEqual(manager._tx_dedup_keys, {b"our previous TX"})
                self.assertIs(manager._mc_keypair, keypair)
                self.assertEqual(manager._mc_channels, [channel])
                self.assertIs(manager._on_message, callback)
                self.assertEqual(manager.profile_id, "eu_uk_narrow_v2")
                advert.assert_not_called()
            finally:
                self.assertTrue(manager.stop())
        self.assertEqual(len(cleanup), 2)

    def test_all_enqueue_paths_and_dequeue_are_closed_during_retune(self):
        manager = self.manager()
        def stopped():
            self.assertTrue(manager._quiescing)
            manager.send_meshcore_message("text", "name")
            manager.send_meshcore_dm("text", "name", bytes(32))
            manager.send_meshcore_advert("name")
            manager._send_ack(bytes(4))
            manager.send_rnode_frame(b"rnode")
            manager._do_tx(Mock())
            self.assertTrue(manager._tx_queue.empty())
            self.assertFalse(manager._tx_active)
            manager.running = manager.ready = False
            return True
        with patch.object(manager, "stop", side_effect=stopped), \
                patch.object(manager, "start_meshcore", return_value=True), \
                patch.object(manager, "_build_mc_group_text") as build:
            self.assertTrue(manager.retune_profile("eu_uk_narrow_v2"))
        build.assert_not_called()
        self.assertFalse(manager._quiescing)

    def test_actual_tx_marker_covers_dequeue_until_rx_restoration(self):
        manager = self.manager()
        manager._tx_queue.put(b"frame")
        entered, release = threading.Event(), threading.Event()
        radio = Mock(IRQ_TX_DONE=1, IRQ_TIMEOUT=512, IRQ_RX_DONE=2,
                     IRQ_HEADER_ERR=32, IRQ_CRC_ERR=64, RX_CONTINUOUS=0xFFFFFF)
        radio.getIrqStatus.return_value = 1
        radio.beginPacket.side_effect = lambda: (entered.set(), release.wait(2))
        worker = threading.Thread(target=manager._do_tx, args=(radio,))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(manager._tx_queue.empty())
            with patch.object(manager, "stop") as stop:
                self.assertFalse(manager.retune_profile("eu_uk_narrow_v2"))
            stop.assert_not_called()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(manager._tx_active)
        radio.setRx.assert_called_once_with(radio.RX_CONTINUOUS)

    def test_failed_stop_does_not_start_second_owner_and_reopens_admission(self):
        manager = self.manager()
        with patch.object(manager, "stop", return_value=False), \
                patch.object(manager, "start_meshcore") as start:
            self.assertFalse(manager.retune_profile("eu_uk_narrow_v2"))
        start.assert_not_called()
        self.assertFalse(manager._quiescing)

    def test_new_us_preset_passes_32_symbol_preamble_to_radio_worker(self):
        manager = LoRaManager()
        with patch.object(manager, "start_sniffer", return_value=True) as start:
            self.assertTrue(manager.start_meshcore("us_recommended"))
        self.assertEqual(start.call_args.args[:4], (910525000, 7, 5, 62500))
        self.assertEqual(start.call_args.kwargs["preamble"], 32)


class MeshtasticRetuneTests(unittest.TestCase):
    def test_legacy_state_migrates_its_channel_separately_from_rf(self):
        with tempfile.TemporaryDirectory() as folder:
            first = MeshtasticManager(home=folder)
            self.assertTrue(first.set_profile("longfast"))
            state = dict(first._state)
            del state["channel_id"]
            path = Path(folder) / ".watchdogs_meshtastic.json"
            path.write_text(json.dumps(state))
            manager = MeshtasticManager(home=folder)
            self.assertEqual(manager.channel_profile_id, "longfast")
            self.assertEqual(manager.channel_name, "LongFast")
            self.assertTrue(manager.retune_profile("us_longfast"))
            restored = MeshtasticManager(home=folder)
            self.assertEqual(restored.profile_id, "us_longfast")
            self.assertEqual(restored.channel_profile_id, "longfast")
            self.assertEqual(restored.node_id, first.node_id)

    def test_rf_only_change_preserves_channel_identity_nonce_and_packet_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            manager.set_node_name("Saved nickname")
            manager._on_node = Mock()
            self.assertTrue(manager._handle_frame(fixtures.FRAME, metadata()))
            before = dict(manager._state)
            self.assertTrue(manager.retune_profile("us_longturbo"))
            for field in ("node_num", "packet_id", "node_name", "next_tx_at", "seen_rx", "channel_id"):
                self.assertEqual(manager._state[field], before[field])
            self.assertEqual(manager.channel_name, "SFNarrow")
            self.assertFalse(manager._handle_frame(fixtures.FRAME, metadata()))
            self.assertEqual(manager._on_node.call_count, 1)
            manager.running = manager.ready = True
            self.assertIsNotNone(manager.send_message("public text"))
            _, frame = manager._tx_queue.get_nowait()
            self.assertEqual(decode_packet(frame, "sfnarrow")["text"], "public text")
            with self.assertRaises(ValueError):
                decode_packet(frame, "longfast")

    def test_queued_and_active_tx_defer_without_consuming_packet_or_stopping(self):
        with tempfile.TemporaryDirectory() as folder:
            for active in (False, True):
                manager = MeshtasticManager(home=folder)
                manager.running = manager.ready = True
                manager._tx_active = active
                if not active:
                    manager.send_message("queued")
                previous = dict(manager._state)
                with patch.object(manager, "stop") as stop:
                    self.assertFalse(manager.retune_profile("us_longfast"))
                stop.assert_not_called()
                self.assertEqual(manager._state, previous)
                self.assertIn("deferred", manager.last_error)
                self.assertEqual(manager._tx_queue.qsize(), 0 if active else 1)

    def test_dequeued_frame_stays_pending_and_blocks_retune_through_duty_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            manager.running = manager.ready = True
            token = manager.send_message("duty wait")
            manager._state["next_tx_at"] = time.time() + 3600
            self.assertIsNone(manager._take_pending_tx())
            self.assertTrue(manager._tx_queue.empty())
            self.assertEqual(manager._pending_tx[0], token)
            self.assertFalse(manager.retune_profile("us_longfast"))
            manager._state["next_tx_at"] = 0
            claimed = manager._take_pending_tx()
            self.assertEqual(claimed[0], token)
            self.assertTrue(manager._tx_active)
            self.assertFalse(manager.retune_profile("us_longfast"))

    def test_guard_closes_admission_and_dequeue_across_stop_start(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            manager.running = manager.ready = True
            nonce = manager._state["packet_id"]
            def stopped():
                self.assertTrue(manager._quiescing)
                self.assertIsNone(manager.send_message("during retune"))
                self.assertIsNone(manager._take_pending_tx())
                self.assertEqual(manager._state["packet_id"], nonce)
                manager.running = manager.ready = False
                return True
            with patch.object(manager, "stop", side_effect=stopped), \
                    patch.object(manager, "start", return_value=True) as start:
                self.assertTrue(manager.retune_profile("us_longfast"))
            start.assert_called_once_with("us_longfast")
            self.assertFalse(manager._quiescing)
            self.assertTrue(manager._tx_queue.empty())

    def test_real_worker_stop_start_does_not_deadlock_or_queue_advert(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            profiles, cleanup = [], []
            def worker():
                profiles.append(manager.profile_id)
                manager.ready = True
                manager._started.set()
                manager._stop.wait(2)
                with manager._guard:
                    cleanup.append(True)
                    manager.running = manager.ready = False
            with patch.object(manager, "_run", side_effect=worker), \
                    patch.object(manager, "_transmit_frame") as transmit:
                try:
                    self.assertTrue(manager.start())
                    identity = manager.node_id
                    self.assertTrue(manager.retune_profile("us_longfast"))
                    self.assertEqual(profiles, ["sfnarrow", "us_longfast"])
                    self.assertEqual(manager.node_id, identity)
                    self.assertEqual(manager.channel_name, "SFNarrow")
                    self.assertTrue(manager._tx_queue.empty())
                    transmit.assert_not_called()
                finally:
                    self.assertTrue(manager.stop())
            self.assertEqual(len(cleanup), 2)

    def test_busy_direct_transmit_marker_and_failure_release(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            entered, release = threading.Event(), threading.Event()
            def transmit(*args):
                entered.set()
                release.wait(2)
                raise OSError("synthetic radio failure")
            errors = []
            def run():
                try:
                    manager._transmit_frame(Mock(), b"frame")
                except OSError as exc:
                    errors.append(str(exc))
            with patch.object(manager, "_transmit_frame_owned", side_effect=transmit):
                worker = threading.Thread(target=run)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(manager.retune_profile("us_longfast"))
                finally:
                    release.set()
                    worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["synthetic radio failure"])
            self.assertFalse(manager._tx_active)

    def test_invalid_profile_and_failed_stop_preserve_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = MeshtasticManager(home=folder)
            self.assertFalse(manager.retune_profile("not-a-profile"))
            manager.running = manager.ready = True
            with patch.object(manager, "stop", return_value=False), patch.object(manager, "start") as start:
                self.assertFalse(manager.retune_profile("us_longfast"))
            self.assertEqual(manager.profile_id, "sfnarrow")
            self.assertFalse(manager._quiescing)
            start.assert_not_called()


class RealDriverRegionalConfigurationTests(unittest.TestCase):
    """Real LoRaRF source, fake GPIO and mutating SPI: no physical RF."""

    def check_profile(self, profile_id, expected_modulation):
        with actual_radio(self) as (radio, spi):
            profile = get_meshtastic_rf(profile_id)
            audit = probe.SpiAudit(spi)
            import sys
            module = sys.modules[radio.__module__]
            with patch.object(module, "spi", audit):
                readback = probe.configure_radio(radio, profile)
                report = audit.report(profile)
            self.assertEqual(readback["mode"], "RX")
            self.assertEqual(report["applied_rf"]["frequency_hz"], profile["frequency_hz"])
            self.assertFalse(report["rf_measured"])
            self.assertEqual(audit.commands[0x8B], expected_modulation)
            self.assertFalse(any(command[0] == 0x83 for command in spi.commands))

    def test_us_longfast_configuration_and_actual_driver_audit(self):
        self.check_profile("us_longfast", bytes([0x8B, 11, 5, 1, 0, 0, 0, 0, 0]))

    def test_us_longturbo_configuration_and_actual_driver_audit(self):
        self.check_profile("us_longturbo", bytes([0x8B, 11, 6, 4, 0, 0, 0, 0, 0]))


if __name__ == "__main__":
    unittest.main()
