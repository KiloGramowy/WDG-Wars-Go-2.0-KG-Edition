"""Optional authenticated peer types; loopback/mocked networking only."""
import http.client
import io
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_piglet_http as legacy_http
import test_piglet_hotspot as legacy_hotspot
from watchdogs.piglet_collection import CollectionError
from watchdogs.piglet_http import MAX_PEER_SESSIONS, _Handler


class PeerTypeDispatchTests(unittest.TestCase):
    def dispatch(self, body, *, token="fixture-token", begin_error=None, reply_status=200):
        data = json.dumps(body).encode()
        handler = _Handler.__new__(_Handler)
        handler.headers = http.client.HTTPMessage()
        handler.headers.add_header("Authorization", "Bearer " + token)
        handler.headers.add_header("Content-Length", str(len(data)))
        handler.rfile = io.BytesIO(data)
        handler.connection = object()
        handler.command, handler.path = "POST", "/pc/v1/begin"
        handler._reply = Mock(return_value=reply_status)
        store = SimpleNamespace(begin=Mock(return_value={"protocol": 1, "session_id": "a" * 32},
                                            side_effect=begin_error))
        activity = Mock()
        handler.server = SimpleNamespace(owner=SimpleNamespace(store=store, token="fixture-token", on_finish=None,
                                                                 _record_activity=activity),
                                         stopping=threading.Event(), expired=lambda _connection: False)
        handler._dispatch()
        return store.begin, handler._reply, activity

    def test_legacy_signature_and_callback_are_unchanged_when_type_absent(self):
        begin, reply, activity = self.dispatch(dict(device_id="piglet-123456789abc", pending_files=0))
        begin.assert_called_once_with("piglet-123456789abc", 0)
        reply.assert_called_once_with(200, {"protocol": 1, "session_id": "a" * 32})
        activity.assert_called_once_with("a" * 32, "piglet-123456789abc", finished=False)

    def test_explicit_types_only_extend_activity_callback_not_store_or_begin_reply(self):
        for device_type in ("piglet-kg", "dark3d-c5"):
            with self.subTest(device_type=device_type):
                begin, reply, activity = self.dispatch(dict(device_id="sender-123456789abc", pending_files=2,
                                                           source_items=1, device_type=device_type))
                begin.assert_called_once_with("sender-123456789abc", 2, source_items=1)
                reply.assert_called_once_with(200, {"protocol": 1, "session_id": "a" * 32})
                activity.assert_called_once_with("a" * 32, "sender-123456789abc", finished=False, device_type=device_type)

    def test_invalid_types_and_other_invalid_metadata_never_mutate_store(self):
        invalid = (None, True, 1, [], {}, "", "Dark3D-C5", "c5", "piglet", "<script>")
        for value in invalid:
            with self.subTest(value=value):
                begin, reply, activity = self.dispatch(dict(device_id="sender-123456789abc", pending_files=0,
                                                           device_type=value))
                begin.assert_not_called()
                activity.assert_not_called()
                self.assertEqual(reply.call_args.args[0], 400)
        begin, reply, activity = self.dispatch(dict(device_id="sender-123456789abc", pending_files=0,
                                                   device_type="dark3d-c5", source_items=True))
        begin.assert_not_called()
        activity.assert_not_called()
        self.assertEqual(reply.call_args.args[0], 400)

    def test_unauthenticated_failed_begin_and_unsuccessful_reply_never_record_type(self):
        body = dict(device_id="sender-123456789abc", pending_files=0, device_type="dark3d-c5")
        begin, reply, activity = self.dispatch(body, token="wrong-token")
        begin.assert_not_called()
        activity.assert_not_called()
        self.assertEqual(reply.call_args.args[0], 401)
        for error in (CollectionError("invalid fixture", 400), OSError("fixture storage unavailable")):
            begin, reply, activity = self.dispatch(body, begin_error=error)
            begin.assert_called_once()
            activity.assert_not_called()
            self.assertIn(reply.call_args.args[0], (400, 503))
        _, _, activity = self.dispatch(body, reply_status=500)
        activity.assert_not_called()


class PeerTypeActivityTests(unittest.TestCase):
    call = legacy_http.PigletHTTPTests.call
    close_loot = legacy_http.PigletHTTPTests.close_loot

    def setUp(self):
        legacy_http.PigletHTTPTests.setUp(self)
        self.now = 100.0
        self.service._activity_clock = lambda: self.now

    def wait_peers(self, expected):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if self.service.connected_peers() == expected:
                return
            time.sleep(.002)
        self.fail("Unexpected live peers: " + repr(self.service.connected_peers()))

    def begin_peer(self, device, device_type=None, pending=0):
        body = dict(device_id=device, pending_files=pending)
        if device_type is not None:
            body["device_type"] = device_type
        status, reply = self.call("POST", "/pc/v1/begin", body)
        self.assertEqual(status, 200)
        session = reply["session_id"]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with self.service._peer_lock:
                if session in self.service._peers:
                    return session
            time.sleep(.002)
        self.fail("Begin activity was not recorded")

    def test_hello_advertises_optional_types_without_creating_connected_peer(self):
        status, hello = self.call("GET", "/pc/v1/hello")
        self.assertEqual(status, 200)
        self.assertEqual((hello["protocol"], hello["source_items"], hello["max_chunk"]), (1, 1, 16384))
        self.assertEqual(hello["device_types"], ["piglet-kg", "dark3d-c5"])
        self.assertEqual(self.service.connected_peers(), [])
        self.assertEqual(self.service._peer_types, {})

    def test_legacy_namespace_is_typed_only_after_valid_authenticated_begin(self):
        device = "piglet-123456789abc"
        body = dict(device_id=device, pending_files=0)
        self.assertEqual(self.call("POST", "/pc/v1/begin", body, token="wrong")[0], 401)
        self.assertEqual(self.service.connected_peers(), [])
        with patch.object(self.store, "begin", side_effect=CollectionError("invalid fixture")):
            self.assertEqual(self.call("POST", "/pc/v1/begin", body)[0], 400)
        self.assertEqual(self.service.connected_peers(), [])
        self.begin_peer(device)
        self.wait_peers([dict(device_id=device, device_type="piglet-kg")])
        self.assertEqual(self.service.connected_devices(), [device])

    def test_c5_requires_explicit_type_arbitrary_prefixes_stay_unknown(self):
        devices = ["dark3d-123456789abc", "c5-123456789abc", "piglet-nothexabcdef", "piglet-123456789ABC"]
        for device in devices:
            self.begin_peer(device)
        self.wait_peers([dict(device_id=device, device_type=None) for device in sorted(devices)])
        self.assertEqual(self.service.connected_devices(), sorted(devices))
        self.now += 1
        self.begin_peer(devices[0], "dark3d-c5")
        self.wait_peers([dict(device_id=device, device_type="dark3d-c5" if device == devices[0] else None)
                         for device in sorted(devices)])

    def test_live_session_binding_cannot_change_device_or_type_via_duplicate_callback(self):
        device = "sender-123456789abc"
        session = self.begin_peer(device, "dark3d-c5")
        self.service._record_activity(session, "other-123456789abc", device_type="piglet-kg")
        self.wait_peers([dict(device_id=device, device_type="dark3d-c5")])

    def test_begin_declaration_can_complete_a_cold_poll_binding_after_reply_flush(self):
        device = "sender-123456789abc"
        session = self.store.begin(device, 0)["session_id"]
        self.service._record_activity(session)
        self.wait_peers([dict(device_id=device, device_type=None)])
        self.service._record_activity(session, device, device_type="dark3d-c5")
        self.wait_peers([dict(device_id=device, device_type="dark3d-c5")])

    def test_slow_cold_session_lookup_cannot_overwrite_concurrent_begin_declaration(self):
        device = "sender-123456789abc"
        session = self.store.begin(device, 0)["session_id"]
        lookup_started, allow_lookup = threading.Event(), threading.Event()
        original = self.store._session

        def delayed_lookup(*args, **kwargs):
            lookup_started.set()
            allow_lookup.wait(1)
            return original(*args, **kwargs)

        with patch.object(self.store, "_session", side_effect=delayed_lookup):
            worker = threading.Thread(target=self.service._record_activity, args=(session,))
            worker.start()
            try:
                self.assertTrue(lookup_started.wait(1))
                self.service._record_activity(session, device, device_type="dark3d-c5")
            finally:
                allow_lookup.set()
                worker.join(1)
            self.assertFalse(worker.is_alive())
        self.wait_peers([dict(device_id=device, device_type="dark3d-c5")])

    def test_successful_poll_refreshes_type_without_database_writes_and_timeout_clears_it(self):
        device = "sender-123456789abc"
        session = self.begin_peer(device, "dark3d-c5", pending=1)
        sha = "b" * 64
        self.assertEqual(self.call("POST", "/pc/v1/file", dict(name="source.log", size=1, sha256=sha), session=session)[0], 200)
        before = self.store._sql.total_changes
        self.now = 129.0
        self.assertEqual(self.call("GET", "/pc/v1/status?id=" + sha, session=session)[0], 200)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with self.service._peer_lock:
                if self.service._peers[session][1] == 129.0:
                    break
            time.sleep(.002)
        self.assertEqual(self.store._sql.total_changes, before)
        self.now = 158.0
        self.wait_peers([dict(device_id=device, device_type="dark3d-c5")])
        self.now = 159.0
        self.assertEqual(self.service.connected_peers(), [])
        self.assertEqual(self.service.connected_devices(), [])
        self.assertEqual(self.service._peer_types, {})

    def test_successful_finish_removes_all_types_for_finished_device_only(self):
        pending = self.begin_peer("sender-123456789abc", "dark3d-c5", pending=1)
        finished = self.begin_peer("piglet-abcdef123456")
        self.assertFalse(self.call("POST", "/pc/v1/finish", {}, session=pending)[1]["finished"])
        self.assertTrue(self.call("POST", "/pc/v1/finish", {}, session=finished)[1]["finished"])
        self.wait_peers([dict(device_id="sender-123456789abc", device_type="dark3d-c5")])
        with self.service._peer_lock:
            self.assertEqual(set(self.service._peer_types), {pending})

    def test_type_registry_shares_existing_bound_eviction_and_stop_cleanup(self):
        for number in range(MAX_PEER_SESSIONS + 2):
            self.now += 1
            self.begin_peer(f"sender-{number:012x}", "dark3d-c5")
        with self.service._peer_lock:
            self.assertEqual(len(self.service._peer_types), MAX_PEER_SESSIONS)
            self.assertEqual(set(self.service._peer_types), set(self.service._peers))
        devices = self.service.connected_devices()
        self.assertNotIn("sender-000000000000", devices)
        self.assertTrue(all(peer["device_type"] == "dark3d-c5" for peer in self.service.connected_peers()))
        self.assertTrue(self.service.stop())
        self.assertEqual(self.service.connected_peers(), [])
        self.assertEqual(self.service._peer_types, {})

    def test_service_restart_does_not_claim_historical_c5_type_from_stored_device_id(self):
        device = "dark3d-123456789abc"
        session = self.begin_peer(device, "dark3d-c5", pending=1)
        sha = "b" * 64
        self.assertEqual(self.call("POST", "/pc/v1/file", dict(name="source.log", size=1, sha256=sha), session=session)[0], 200)
        self.assertTrue(self.service.stop())
        self.assertEqual(self.service._peer_types, {})
        self.service.start()
        before = self.store._sql.total_changes
        self.assertEqual(self.call("GET", "/pc/v1/status?id=" + sha, session=session)[0], 200)
        self.wait_peers([dict(device_id=device, device_type=None)])
        self.assertEqual(self.store._sql.total_changes, before)
        self.now += 1
        self.begin_peer(device, "dark3d-c5", pending=1)
        self.wait_peers([dict(device_id=device, device_type="dark3d-c5")])


class ControllerPeerTypeTests(unittest.TestCase):
    setUp = legacy_hotspot.ControllerTests.setUp
    _release = legacy_hotspot.ControllerTests._release
    wait_for = legacy_hotspot.ControllerTests.wait_for
    active = legacy_hotspot.ControllerTests.active

    def test_snapshot_exposes_live_types_and_clears_them_on_stop(self):
        self.active()
        peers = [dict(device_id="sender-123456789abc", device_type="dark3d-c5")]
        self.services[0].connected_devices = lambda: [peers[0]["device_id"]]
        self.services[0].connected_peers = lambda: peers
        progress = self.controller.snapshot()["progress"]
        self.assertEqual(progress["connected_peers"], peers)
        self.assertEqual(progress["connected_devices"], [peers[0]["device_id"]])
        self.assertTrue(self.controller.stop())
        self.assertEqual(self.controller.snapshot()["progress"]["connected_peers"], [])

    def test_legacy_service_and_telemetry_failure_do_not_guess_or_break_device_list(self):
        self.active()
        self.services[0].connected_devices = lambda: ["dark3d-123456789abc"]
        progress = self.controller.snapshot()["progress"]
        self.assertEqual(progress["connected_devices"], ["dark3d-123456789abc"])
        self.assertEqual(progress["connected_peers"], [])
        self.services[0].connected_peers = Mock(side_effect=OSError("optional telemetry unavailable"))
        self.assertEqual(self.controller.snapshot()["progress"]["connected_devices"], ["dark3d-123456789abc"])
        self.assertEqual(self.controller.snapshot()["progress"]["connected_peers"], [])


if __name__ == "__main__":
    unittest.main()
