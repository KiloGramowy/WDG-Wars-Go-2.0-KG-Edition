"""Loopback-only protocol tests; no hotspot, device, external upload or firmware."""
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_piglet_collection import BLE, WIFI, contents, digest, row
from watchdogs.loot_manager import LootManager
from watchdogs.piglet_collection import PigletCollectionStore
from watchdogs.piglet_http import MAX_PEER_SESSIONS, MAX_REPLY, PigletHTTPService, _Handler, signed_receipt

TOKEN = "0123456789abcdef0123456789abcdef"  # Public test fixture, never a pairing credential.


class ReceiptTests(unittest.TestCase):
    def test_shared_c5_hmac_vector_uses_token_text_and_trailing_lf(self):
        receipt = dict(protocol=1, collector_id="a" * 32, sha256="b" * 64, size=123,
                       import_id="c" * 32, records=3, wifi=1, ble=1, duplicates=1)
        signed = signed_receipt(receipt, TOKEN)
        self.assertEqual(signed["signature"], "93859ae089db45f8b87a1994f2b27c25eccb77133069ac1f3786cd4c6a551e0b")
        self.assertNotIn("signature", receipt)

    def test_invalid_receipt_counts_never_get_signed(self):
        receipt = dict(protocol=1, collector_id="a" * 32, sha256="b" * 64, size=123,
                       import_id="c" * 32, records=2, wifi=1, ble=1, duplicates=1)
        with self.assertRaises(ValueError):
            signed_receipt(receipt, TOKEN)

    def test_bind_rejects_wildcard_hostname_public_and_implicit_loopback(self):
        for address in ("0.0.0.0", "localhost", "8.8.8.8", "169.254.1.1", "100.64.0.1", "127.0.0.1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                PigletHTTPService(Mock(), TOKEN, address)

    def test_finish_notifies_only_after_successful_response_flush(self):
        for flush_error in (None, BrokenPipeError("disconnected")):
            with self.subTest(flush_error=flush_error):
                handler = _Handler.__new__(_Handler)
                handler.headers = http.client.HTTPMessage()
                for name, value in (("Authorization", "Bearer " + TOKEN), ("Content-Length", "2"),
                                    ("X-Piglet-Session", "a" * 32)):
                    handler.headers.add_header(name, value)
                handler.rfile = io.BytesIO(b"{}")
                handler.wfile = Mock()
                handler.wfile.flush.side_effect = flush_error
                callback = Mock(side_effect=lambda _payload: self.assertTrue(handler.wfile.flush.called))
                store = SimpleNamespace(finish=Mock(return_value={"shutdown_allowed": True}))
                activity = Mock(side_effect=lambda *_args,**_kwargs:self.assertTrue(handler.wfile.flush.called))
                handler.server = SimpleNamespace(owner=SimpleNamespace(store=store, token=TOKEN, on_finish=callback,
                                                                       _record_activity=activity),
                                                 stopping=threading.Event(), expired=lambda _connection: False)
                handler.connection = object()
                handler.command, handler.path = "POST", "/pc/v1/finish"
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler._dispatch()
                self.assertEqual(callback.call_count, 0 if flush_error else 1)
                self.assertEqual(activity.call_count, 0 if flush_error else 1)

    def test_absent_or_throwing_activity_callback_never_sends_second_reply(self):
        for activity in (None,Mock(side_effect=RuntimeError("fixture telemetry failure"))):
            with self.subTest(activity=activity):
                handler=_Handler.__new__(_Handler)
                handler.headers=http.client.HTTPMessage()
                for name,value in (("Authorization","Bearer "+TOKEN),("Content-Length","2"),
                                   ("X-Piglet-Session","a"*32)):
                    handler.headers.add_header(name,value)
                handler.rfile=io.BytesIO(b"{}")
                handler.wfile=Mock()
                callback=Mock()
                owner=SimpleNamespace(store=SimpleNamespace(finish=Mock(return_value={"finished":True,"shutdown_allowed":True})),
                                      token=TOKEN,on_finish=callback)
                if activity is not None:owner._record_activity=activity
                handler.server=SimpleNamespace(owner=owner,stopping=threading.Event(),expired=lambda connection:False)
                handler.connection=object()
                handler.command,handler.path="POST","/pc/v1/finish"
                handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
                handler._dispatch()
                handler.send_response.assert_called_once_with(200)
                self.assertEqual(handler.wfile.write.call_count,1)
                callback.assert_called_once_with({"finished":True,"shutdown_allowed":True})


class PigletHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        with patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
            self.loot = LootManager(self.folder.name)
        self.addCleanup(self.close_loot)
        self.store = PigletCollectionStore(self.loot)
        self.addCleanup(self.store.close)
        self.notifications = []
        self.service = PigletHTTPService(self.store, TOKEN, "127.0.0.1", 0,
            on_finish=self.notifications.append, allow_loopback=True, max_clients=2, request_timeout=1.0)
        self.service.start()
        self.addCleanup(self.service.stop)
        self.xp, self.wifi, self.ble = 100, set(), set()
        self.loot.save_xp(self.xp)

    def close_loot(self):
        with patch.object(os, "sync", lambda: None, create=True):
            self.loot.close()

    def call(self, method, path, body=None, *, session=None, headers=None, token=TOKEN):
        request_headers = {"Authorization": "Bearer " + token}
        if session:
            request_headers["X-Piglet-Session"] = session
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection(*self.service.address, timeout=2)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        self.assertEqual(int(response.getheader("Content-Length")), len(data))
        self.assertLessEqual(len(data), MAX_REPLY)
        self.assertEqual(response.getheader("Connection"), "close")
        self.assertIsNone(response.getheader("Transfer-Encoding"))
        self.assertIsNone(response.getheader("Location"))
        return response.status, json.loads(data)

    def begin(self, pending=1, device="piglet-123456789abc"):
        status, body = self.call("POST", "/pc/v1/begin", {"device_id": device, "pending_files": pending})
        self.assertEqual(status, 200)
        return body["session_id"]

    def transfer(self, data):
        session, sha = self.begin(), digest(data)
        status, _ = self.call("POST", "/pc/v1/file", {"name": "closed.csv", "size": len(data), "sha256": sha}, session=session)
        self.assertEqual(status, 200)
        for offset in range(0, len(data), 16384):
            part = data[offset:offset + 16384]
            status, state = self.call("PUT", f"/pc/v1/chunk?id={sha}&offset={offset}", part,
                session=session, headers={"X-Chunk-SHA256": digest(part)})
            self.assertEqual(status, 200)
            self.assertEqual(state["offset"], offset + len(part))
        return session, sha

    def test_hello_and_authentication_are_required_for_every_endpoint(self):
        status, body = self.call("GET", "/pc/v1/hello")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"protocol": 1, "collector_id": self.store.collector_id,
                                "max_chunk": 16384, "source_items": 1})
        for method, path, data in (("GET", "/pc/v1/hello", None), ("POST", "/pc/v1/begin", {}),
                                   ("PUT", "/pc/v1/chunk", b"abc"), ("GET", "/not-an-endpoint", None)):
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, data, token="wrong-token")[0], 401)

    def test_full_import_pending_main_loop_signed_ack_and_same_file_retry(self):
        data = contents([row(), row(BLE, "BLE")])
        session, sha = self.transfer(data)
        status, processing = self.call("POST", "/pc/v1/commit", {"sha256": sha}, session=session)
        self.assertEqual(status, 202)
        self.assertNotIn("receipt", processing)
        self.assertEqual(self.loot.load_xp(), 100, "HTTP worker must never commit XP")
        deadline = time.monotonic() + 5
        while True:
            self.assertLess(time.monotonic(), deadline)
            result = self.store.process_pending(self.xp, self.wifi, self.ble)
            if result["committed"]:
                self.xp = result["xp_total"]
            self.assertTrue(self.store.acknowledge_events(
                [event["event_id"] for event in self.store.pending_events(20)]))
            status, body = self.call("GET", f"/pc/v1/status?id={sha}", session=session)
            if body["state"] == "committed":
                break
            time.sleep(.005)
        self.assertEqual(self.xp, 125)
        unsigned = dict(body["receipt"])
        signature = unsigned.pop("signature")
        self.assertEqual(signature, signed_receipt(unsigned, TOKEN)["signature"])
        status, retry = self.call("POST", "/pc/v1/file", {"name": "another-name.csv", "size": len(data), "sha256": sha}, session=session)
        self.assertEqual(retry["receipt"], body["receipt"])
        self.assertEqual(self.call("POST", "/pc/v1/commit", {"sha256": sha}, session=session)[0], 200)
        status, finished = self.call("POST", "/pc/v1/finish", {}, session=session)
        self.assertEqual(status, 200)
        self.assertTrue(finished["shutdown_allowed"])
        deadline = time.monotonic() + 1
        while not self.notifications and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.notifications, [finished])
        self.assertTrue(self.service.running, "HTTP callback only schedules shutdown")

    def test_duplicate_chunk_and_conflict_return_durable_resume_offset(self):
        data = contents([row()])
        session, sha = self.begin(), digest(data)
        self.call("POST", "/pc/v1/file", {"name": "closed.csv", "size": len(data), "sha256": sha}, session=session)
        part = data[:100]
        headers = {"X-Chunk-SHA256": digest(part)}
        for _ in range(2):
            status, state = self.call("PUT", f"/pc/v1/chunk?id={sha}&offset=0", part, session=session, headers=headers)
            self.assertEqual((status, state["offset"]), (200, 100))
        status, state = self.call("PUT", f"/pc/v1/chunk?id={sha}&offset=101", part, session=session, headers=headers)
        self.assertEqual((status, state["offset"]), (409, 100))

    def test_invalid_json_query_unknown_session_and_no_redirect(self):
        for method, path, body in (("POST", "/pc/v1/begin", b"{"), ("POST", "/pc/v1/begin", []),
                ("GET", "/pc/v1/hello?unexpected=1", None), ("GET", "/pc/v1/hello?a=1&a=2", None),
                ("GET", "https://example.invalid/pc/v1/hello", None)):
            if body == []:
                body = b"[]"
            with self.subTest(path=path, body=body):
                self.assertEqual(self.call(method, path, body)[0], 400)
        self.assertEqual(self.call("GET", "/not-found")[0], 404)
        self.assertEqual(self.call("GET", "/pc/v1/status?id=" + "a" * 64, session="b" * 32)[0], 404)

    def test_body_bounds_no_chunked_and_duplicate_lengths(self):
        self.assertEqual(self.call("POST", "/pc/v1/begin", b"{}", headers={"Content-Length": "16385"})[0], 413)
        self.assertEqual(self.call("PUT", "/pc/v1/chunk?id=" + "a" * 64 + "&offset=0", b"x",
                                  headers={"Content-Length": "16385"})[0], 413)
        self.assertEqual(self.call("POST", "/pc/v1/begin", b"{}", headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.call("POST", "/pc/v1/begin", b"{}", headers={"Content-Encoding": "gzip"})[0], 415)
        self.assertEqual(self.call("GET", "/pc/v1/hello", headers={"X-Large": "x" * 17000})[0], 431)
        with socket.create_connection(self.service.address, timeout=2) as connection:
            connection.sendall(("POST /pc/v1/begin HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN
                + "\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n").encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            data = response.read()
            self.assertEqual(response.status, 400)
            self.assertEqual(int(response.getheader("Content-Length")), len(data))

    def test_store_failure_does_not_expose_token_paths_or_exception_text(self):
        with patch.object(self.store, "begin", side_effect=OSError("secret path /home/pi/ " + TOKEN)):
            status, body = self.call("POST", "/pc/v1/begin", {"device_id": "piglet-123456789abc", "pending_files": 1})
        self.assertEqual(status, 503)
        self.assertNotIn(TOKEN, str(body))
        self.assertNotIn("/home/pi", str(body))

    def test_bounded_clients_and_stop_interrupt_incomplete_headers_without_store_close(self):
        clients = []
        try:
            for _ in range(2):
                connection = socket.create_connection(self.service.address, timeout=2)
                clients.append(connection)
                connection.sendall(b"GET /pc/v1/hello HTTP/1.1\r\nHost: localhost\r\n")
            deadline = time.monotonic() + .5
            while len(self.service._server._connections) < 2 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(self.call("GET", "/pc/v1/hello")[0], 503)
            started = time.monotonic()
            self.assertTrue(self.service.stop())
            self.assertLess(time.monotonic() - started, 1.8)
            self.assertFalse(self.service.running)
            self.assertFalse(self.store._closed)
            self.assertTrue(self.service.stop())
        finally:
            for connection in clients:
                connection.close()

    def test_absolute_request_timeout_closes_trickled_partial_body(self):
        with socket.create_connection(self.service.address, timeout=2) as connection:
            connection.sendall(("POST /pc/v1/begin HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN
                + "\r\nContent-Length: 100\r\n\r\n{").encode("ascii"))
            started = time.monotonic()
            chunks = []
            while True:
                data = connection.recv(8192)
                if not data:
                    break
                chunks.append(data)
            self.assertLess(time.monotonic() - started, 1.8)
            if chunks:
                response = b"".join(chunks)
                self.assertTrue(response.startswith(b"HTTP/1.1 408 "))
                header, body = response.split(b"\r\n\r\n", 1)
                length = next(line.split(b":", 1)[1] for line in header.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                self.assertEqual(int(length), len(body))
        self.assertEqual(self.store.status_overview()["sessions"], [])

    def test_unfinished_second_device_prevents_finish_shutdown_even_after_idle_timeout(self):
        first = self.begin(0)
        second = self.begin(1, "piglet-abcdef123456")
        data = contents([row()])
        sha = digest(data)
        self.call("POST", "/pc/v1/file", {"name": "pending.csv", "size": len(data), "sha256": sha}, session=second)
        with self.store._lock, self.store._sql:
            self.store._sql.execute("UPDATE sessions SET touched=0 WHERE id=?", (second,))
        status, state = self.call("POST", "/pc/v1/finish", {}, session=first)
        self.assertEqual(status, 200)
        self.assertFalse(state["shutdown_allowed"])
        overview = self.store.status_overview()
        self.assertEqual(overview["pending_files"], 1)
        self.assertIn("piglet-abcdef123456", overview["active_devices"])
        self.assertEqual(overview["current_file"]["offset"], 0)
        self.assertEqual(overview["current_file"]["size"], len(data))

    def test_start_stop_restart_are_idempotent_and_keep_collector_identity(self):
        self.assertEqual(self.service.start(), self.service.address)
        before = self.call("GET", "/pc/v1/hello")[1]["collector_id"]
        self.assertTrue(self.service.stop())
        self.assertTrue(self.service.stop())
        self.service.start()
        self.assertEqual(self.call("GET", "/pc/v1/hello")[1]["collector_id"], before)


class PeerActivityTests(unittest.TestCase):
    close_loot = PigletHTTPTests.close_loot
    call = PigletHTTPTests.call
    begin = PigletHTTPTests.begin
    transfer = PigletHTTPTests.transfer

    def setUp(self):
        PigletHTTPTests.setUp(self)
        self.now = 100.0
        self.service._activity_clock = lambda:self.now

    def wait_activity(self, session=None, *, devices=None):
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            actual=self.service.connected_devices()
            with self.service._peer_lock:
                current=self.service._peers.get(session) if session is not None else None
            if (devices is None or actual==devices) and (session is None or current and current[1]==self.now):
                return
            time.sleep(.002)
        self.fail("Authenticated activity did not reach expected state")

    def test_hello_unauthorized_invalid_and_error_requests_do_not_create_peer(self):
        self.assertEqual(self.call("GET","/pc/v1/hello")[0],200)
        self.assertEqual(self.call("POST","/pc/v1/begin",{},token="wrong")[0],401)
        self.assertEqual(self.call("POST","/pc/v1/begin",{})[0],400)
        self.assertEqual(self.call("GET","/pc/v1/status?id="+"a"*64,session="b"*32)[0],404)
        with patch.object(self.store,"begin",side_effect=OSError("fixture storage failure")):
            self.assertEqual(self.call("POST","/pc/v1/begin",dict(device_id="piglet-123456789abc",pending_files=1))[0],503)
        self.assertEqual(self.service.connected_devices(),[])

    def test_begin_activity_expires_even_when_durable_pending_work_remains(self):
        session=self.begin()
        data=contents([row()]);sha=digest(data)
        self.call("POST","/pc/v1/file",dict(name="pending.csv",size=len(data),sha256=sha),session=session)
        self.wait_activity(session,devices=["piglet-123456789abc"])
        with self.store._lock,self.store._sql:self.store._sql.execute("UPDATE sessions SET touched=0")
        self.now=130.0
        self.assertEqual(self.service.connected_devices(),[])
        self.assertIn("piglet-123456789abc",self.store.status_overview()["active_devices"])

    def test_successful_status_polls_refresh_ram_activity_without_db_writes(self):
        session=self.begin();data=contents([row()]);sha=digest(data)
        self.call("POST","/pc/v1/file",dict(name="pending.csv",size=len(data),sha256=sha),session=session)
        self.wait_activity(session)
        before=self.store._sql.total_changes
        touched=self.store._sql.execute("SELECT touched FROM sessions WHERE id=?",(session,)).fetchone()[0]
        self.now=129.0
        self.assertEqual(self.call("GET","/pc/v1/status?id="+sha,session=session)[0],200)
        self.wait_activity(session)
        self.now=158.0
        self.assertEqual(self.service.connected_devices(),["piglet-123456789abc"])
        self.assertEqual(self.store._sql.total_changes,before)
        self.assertEqual(self.store._sql.execute("SELECT touched FROM sessions WHERE id=?",(session,)).fetchone()[0],touched)
        self.now=159.0
        self.assertEqual(self.service.connected_devices(),[])

    def test_failed_rpc_and_unidentified_hello_do_not_extend_existing_activity(self):
        session=self.begin();self.wait_activity(session)
        self.now=129.0
        self.assertEqual(self.call("GET","/pc/v1/status?id="+"f"*64,session=session)[0],404)
        self.assertEqual(self.call("GET","/pc/v1/hello")[0],200)
        self.now=130.0
        self.assertEqual(self.service.connected_devices(),[])

    def test_service_restart_can_verify_existing_session_without_changing_database(self):
        session=self.begin();data=contents([row()]);sha=digest(data)
        self.call("POST","/pc/v1/file",dict(name="pending.csv",size=len(data),sha256=sha),session=session)
        self.wait_activity(session)
        before=self.store._sql.total_changes
        self.assertTrue(self.service.stop())
        self.assertEqual(self.service.connected_devices(),[])
        self.service.start()
        self.assertEqual(self.service.connected_devices(),[])
        self.now=150.0
        self.assertEqual(self.call("GET","/pc/v1/status?id="+sha,session=session)[0],200)
        self.wait_activity(session,devices=["piglet-123456789abc"])
        self.assertEqual(self.store._sql.total_changes,before)

    def test_successful_finish_removes_only_finished_device_while_pending_finish_refreshes(self):
        pending=self.begin(1)
        finished=self.begin(0,"piglet-abcdef123456")
        self.wait_activity(finished,devices=["piglet-123456789abc","piglet-abcdef123456"])
        self.now=129.0
        status,result=self.call("POST","/pc/v1/finish",{},session=pending)
        self.assertEqual(status,200);self.assertFalse(result["finished"])
        self.wait_activity(pending)
        status,result=self.call("POST","/pc/v1/finish",{},session=finished)
        self.assertEqual(status,200);self.assertTrue(result["finished"])
        self.wait_activity(devices=["piglet-123456789abc"])
        self.now=158.0
        self.assertEqual(self.service.connected_devices(),["piglet-123456789abc"])

    def test_registry_is_bounded_and_device_identifiers_are_verified(self):
        for number in range(MAX_PEER_SESSIONS+2):
            self.now+=1
            session=self.begin(0,f"piglet-{number:012x}")
            self.wait_activity(session)
        with self.service._peer_lock:
            self.assertEqual(len(self.service._peers),MAX_PEER_SESSIONS)
        peers=self.service.connected_devices()
        self.assertNotIn("piglet-000000000000",peers)
        self.assertIn(f"piglet-{MAX_PEER_SESSIONS+1:012x}",peers)
        self.assertTrue(self.service.stop())
        self.assertEqual(self.service.connected_devices(),[])

    def test_error_status_payload_does_not_refresh_activity(self):
        session=self.begin();data=contents([row()]);sha=digest(data)
        self.call("POST","/pc/v1/file",dict(name="pending.csv",size=len(data),sha256=sha),session=session)
        self.wait_activity(session)
        self.store._set_error(sha,"fixture validation error")
        self.now=129.0
        status,body=self.call("GET","/pc/v1/status?id="+sha,session=session)
        self.assertEqual(status,200);self.assertIn("error",body)
        self.now=130.0
        self.assertEqual(self.service.connected_devices(),[])

    def test_receiver_not_running_never_reports_cached_peer(self):
        session=self.begin();self.wait_activity(session)
        with patch.object(self.service._thread,"is_alive",return_value=False):
            self.assertEqual(self.service.connected_devices(),[])
        self.assertEqual(self.service.connected_devices(),["piglet-123456789abc"])


if __name__ == "__main__":
    unittest.main()
