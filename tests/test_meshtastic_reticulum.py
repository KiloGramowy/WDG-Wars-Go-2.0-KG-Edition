"""Radio handoff readiness checks using fake Reticulum and RNode transports.

No network, radio, credentials, or live application storage is opened.
"""

from types import ModuleType, SimpleNamespace
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from watchdogs import reticulum_manager as reticulum


class TCPClientInterface:
    """Type name intentionally matches the production TCP lookup."""

    def __init__(self):
        self.detach = Mock()


class ReticulumLoRaReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tcp = TCPClientInterface()
        self.transport = SimpleNamespace(
            interfaces=[self.tcp], path_table={reticulum.HUB_HASH: "cached-tcp"},
            path_table_lock=threading.RLock(), request_path=Mock())
        self.rns_patch = patch.object(reticulum, "RNS", SimpleNamespace(Transport=self.transport))
        self.available_patch = patch.object(reticulum, "RNS_AVAILABLE", True)
        self.rns_patch.start()
        self.available_patch.start()
        self.addCleanup(self.rns_patch.stop)
        self.addCleanup(self.available_patch.stop)
        self.iface = SimpleNamespace(feed_rx=Mock(), activate=Mock(), detach=Mock())
        self.iface_type = Mock(return_value=self.iface)
        module = ModuleType("watchdogs.rns_lora_interface")
        module.RNodeCompatInterface = self.iface_type
        self.module_patch = patch.dict(sys.modules, {"watchdogs.rns_lora_interface": module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.manager = reticulum.ReticulumManager(configdir="unused-test-config")
        self.manager.running = True
        self.manager._restore_tcp = Mock()
        self.radio = SimpleNamespace(running=False, stop=Mock(return_value=True),
                                     start_rnode=Mock(return_value=True),
                                     send_rnode_frame=Mock(), _on_rnode_rx=None)

    def test_failed_start_never_activates_interface_or_requests_radio_path(self):
        self.radio.start_rnode.return_value = False
        with self.assertLogs("reticulum", level="ERROR"):
            result = self.manager.enable_lora(self.radio)
        self.assertFalse(result)
        self.assertFalse(self.manager.lora_active)
        self.iface.activate.assert_not_called()
        self.transport.request_path.assert_not_called()
        self.assertIsNone(self.radio._on_rnode_rx)
        self.radio.stop.assert_called_once_with()

    def test_previous_radio_owner_stop_failure_does_not_detach_tcp(self):
        self.radio.running = True
        self.radio.stop.return_value = False
        with self.assertLogs("reticulum", level="ERROR"):
            result = self.manager.enable_lora(self.radio)
        self.assertFalse(result)
        self.tcp.detach.assert_not_called()
        self.assertEqual(self.transport.interfaces, [self.tcp])
        self.iface_type.assert_not_called()
        self.radio.start_rnode.assert_not_called()
        self.transport.request_path.assert_not_called()
        self.assertFalse(self.manager.lora_active)

    def test_failure_after_tcp_detach_restores_tcp(self):
        self.radio.start_rnode.side_effect = RuntimeError("fake start failure")
        with self.assertLogs("reticulum", level="ERROR"):
            result = self.manager.enable_lora(self.radio)
        self.assertFalse(result)
        self.tcp.detach.assert_called_once_with()
        self.assertNotIn(self.tcp, self.transport.interfaces)
        self.manager._restore_tcp.assert_called_once_with()
        self.assertIsNone(self.radio._on_rnode_rx)
        self.transport.request_path.assert_not_called()

    def test_success_reports_ready_and_only_then_activates_and_requests_path(self):
        order = []
        self.radio.start_rnode.side_effect = lambda: order.append("ready") or True
        self.iface.activate.side_effect = lambda: order.append("activate")
        self.transport.request_path.side_effect = lambda target: order.append("path")
        self.assertTrue(self.manager.enable_lora(self.radio))
        self.assertEqual(order, ["ready", "activate", "path"])
        self.assertTrue(self.manager.lora_active)
        self.assertIs(self.manager._lora_iface, self.iface)
        self.assertIs(self.radio._on_rnode_rx, self.iface.feed_rx)
        self.transport.request_path.assert_called_once_with(reticulum.HUB_HASH)
        self.manager._restore_tcp.assert_not_called()
        self.assertNotIn(reticulum.HUB_HASH, self.transport.path_table)

    def test_ordinary_manual_disable_still_restores_tcp(self):
        self.assertTrue(self.manager.enable_lora(self.radio))
        self.assertTrue(self.manager.disable_lora())
        self.iface.detach.assert_called_once_with()
        self.radio.stop.assert_called_once_with()
        self.manager._restore_tcp.assert_called_once_with()
        self.assertFalse(self.manager.lora_active)
        self.assertIsNone(self.manager._lora_mgr)

    def test_shutdown_sets_stop_first_joins_worker_and_never_restores_tcp(self):
        self.assertTrue(self.manager.enable_lora(self.radio))
        completed = threading.Event()
        def worker():
            self.manager._stop_event.wait()
            completed.set()
        thread = threading.Thread(target=worker)
        self.manager._thread = thread
        thread.start()
        def stop_radio():
            self.assertTrue(self.manager._stop_event.is_set())
            return True
        self.radio.stop.side_effect = stop_radio
        self.assertTrue(self.manager.stop())
        self.assertTrue(completed.is_set())
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.manager._thread)
        self.assertFalse(self.manager.running)
        self.assertFalse(self.manager.connected)
        self.manager._restore_tcp.assert_not_called()

    def test_shutdown_is_idempotent_without_radio_or_tcp_recreation(self):
        self.assertTrue(self.manager.enable_lora(self.radio))
        self.assertTrue(self.manager.stop())
        self.assertTrue(self.manager.stop())
        self.radio.stop.assert_called_once_with()
        self.iface.detach.assert_called_once_with()
        self.manager._restore_tcp.assert_not_called()

    def test_failed_radio_stop_preserves_owned_references_for_retry(self):
        self.assertTrue(self.manager.enable_lora(self.radio))
        self.radio.stop.return_value = False
        self.assertFalse(self.manager.stop())
        self.assertIs(self.manager._lora_mgr, self.radio)
        self.assertIs(self.manager._lora_iface, self.iface)
        self.manager._restore_tcp.assert_not_called()
        self.radio.stop.return_value = True
        self.assertTrue(self.manager.stop())
        self.assertIsNone(self.manager._lora_mgr)
        self.assertIsNone(self.manager._lora_iface)

    def test_failed_detach_is_reported_without_losing_the_interface(self):
        self.assertTrue(self.manager.enable_lora(self.radio))
        self.iface.detach.side_effect = OSError("fake detach failure")
        self.assertFalse(self.manager.stop())
        self.assertIs(self.manager._lora_iface, self.iface)
        self.assertIs(self.manager._lora_mgr, self.radio)
        self.radio.stop.assert_called_once_with()
        self.iface.detach.side_effect = None
        self.assertTrue(self.manager.stop())

    def test_unfinished_worker_returns_false_and_retains_thread_reference(self):
        thread = SimpleNamespace(is_alive=Mock(return_value=True), join=Mock())
        self.manager._thread = thread
        self.assertFalse(self.manager.stop())
        thread.join.assert_called_once_with(timeout=2)
        self.assertIs(self.manager._thread, thread)
        self.assertTrue(self.manager.running)
        self.manager._restore_tcp.assert_not_called()

    def test_shutdown_discards_queued_messages_and_rejects_late_send(self):
        self.assertEqual(self.manager.send("synthetic pending"), 0)
        self.assertTrue(self.manager.stop())
        self.assertTrue(self.manager._tx_queue.empty())
        self.assertEqual(self.manager.send("synthetic late"), -1)
        self.assertTrue(self.manager._tx_queue.empty())

    def test_stopped_manager_does_not_restore_tcp_resolve_or_send(self):
        self.assertTrue(self.manager.stop())
        self.manager._find_tcp_iface = Mock(side_effect=AssertionError("must not inspect TCP"))
        reticulum.ReticulumManager._restore_tcp(self.manager)
        self.manager._find_tcp_iface.assert_not_called()
        self.assertIsNone(self.manager._resolve_dest(reticulum.HUB_HASH))
        self.manager._resolve_dest = Mock()
        self.manager._do_send(0, "synthetic late")
        self.manager._resolve_dest.assert_not_called()
        self.manager._group_out = object()
        self.manager._group_broadcast("synthetic late")  # No RNS.Packet exists in the fake.
        self.transport.request_path.assert_not_called()

    def test_stop_during_resolution_prevents_outbound_dispatch(self):
        self.manager._router = SimpleNamespace(handle_outbound=Mock())
        def resolve(target):
            self.manager._stop_event.set()
            return object()
        self.manager._resolve_dest = Mock(side_effect=resolve)
        self.manager._do_send(0, "synthetic late")
        self.manager._router.handle_outbound.assert_not_called()

    def test_stop_during_initialization_prevents_router_creation_and_announce(self):
        def initialize(**kwargs):
            self.manager._stop_event.set()
            return object()
        self.manager.running = False
        with patch.object(reticulum, "_ensure_configdir"), \
                patch.object(reticulum, "RNS", SimpleNamespace(Reticulum=initialize)), \
                patch.object(reticulum, "LXMF", SimpleNamespace(LXMRouter=Mock())) as lxmf:
            self.manager._run()
        lxmf.LXMRouter.assert_not_called()
        self.assertFalse(self.manager.running)

    def test_stopped_manager_ignores_late_network_callbacks(self):
        self.manager._on_message = Mock()
        self.manager._on_delivered = Mock()
        self.manager._on_failed = Mock()
        self.assertTrue(self.manager.stop())
        self.manager._handle_group(b"synthetic", None)
        self.manager._handle_inbound(object())
        self.manager._delivered(0)
        self.manager._failed(0)
        self.manager._on_message.assert_not_called()
        self.manager._on_delivered.assert_not_called()
        self.manager._on_failed.assert_not_called()
        self.assertFalse(self.manager.connected)


if __name__ == "__main__":
    unittest.main()
