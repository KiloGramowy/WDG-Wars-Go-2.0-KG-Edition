"""Negotiated source-path accounting metadata; no sockets, devices or files."""
import http.client
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from watchdogs.piglet_http import _Handler


class SourceMetadataDispatchTests(unittest.TestCase):
    def dispatch(self, path, body):
        data = json.dumps(body).encode()
        handler = _Handler.__new__(_Handler)
        handler.headers = http.client.HTTPMessage()
        for name, value in (("Authorization", "Bearer fixture-token"), ("Content-Length", str(len(data))),
                            ("Content-Type", "application/json"), ("X-Piglet-Session", "a" * 32)):
            handler.headers.add_header(name, value)
        handler.rfile = io.BytesIO(data)
        handler.connection = object()
        handler.command, handler.path = "POST", path
        handler._reply = Mock(return_value=200)
        store = SimpleNamespace(begin=Mock(return_value={"session_id": "a" * 32}),
                                offer=Mock(return_value={"state": "receiving"}))
        handler.server = SimpleNamespace(owner=SimpleNamespace(store=store, token="fixture-token", on_finish=None),
                                         stopping=threading.Event(), expired=lambda _connection: False)
        handler._dispatch()
        return store, handler._reply

    def test_explicit_capability_is_forwarded_and_legacy_begin_signature_is_preserved(self):
        body = {"device_id": "piglet-123456789abc", "pending_files": 2}
        store, reply = self.dispatch("/pc/v1/begin", body)
        store.begin.assert_called_once_with(body["device_id"], 2)
        self.assertEqual(reply.call_args.args[0], 200)
        store, reply = self.dispatch("/pc/v1/begin", dict(body, source_items=1))
        store.begin.assert_called_once_with(body["device_id"], 2, source_items=1)
        self.assertEqual(reply.call_args.args[0], 200)

    def test_invalid_capability_and_unknown_fields_are_rejected_before_store_access(self):
        base = dict(device_id="piglet-123456789abc", pending_files=2)
        for extra in (dict(source_items=True), dict(source_items=2), dict(source_items=None), dict(typo=1)):
            with self.subTest(extra=extra):
                store, reply = self.dispatch("/pc/v1/begin", dict(base, **extra))
                store.begin.assert_not_called()
                self.assertEqual(reply.call_args.args[0], 400)

    def test_distinct_source_identity_is_forwarded_without_changing_file_digest(self):
        base = dict(name="closed.csv", size=2, sha256="b" * 64)
        for source_id in ("c" * 64, "d" * 64):
            with self.subTest(source_id=source_id):
                store, reply = self.dispatch("/pc/v1/file", dict(base, source_id=source_id))
                store.offer.assert_called_once_with("a" * 32, "closed.csv", 2, "b" * 64, source_id=source_id)
                self.assertEqual(reply.call_args.args[0], 200)
        store, _ = self.dispatch("/pc/v1/file", base)
        store.offer.assert_called_once_with("a" * 32, "closed.csv", 2, "b" * 64)

    def test_unexpected_or_malformed_source_metadata_never_reaches_the_store(self):
        base = dict(name="closed.csv", size=2, sha256="b" * 64)
        for extra in (dict(source_id="../logs/closed.csv"), dict(source_id=None),
                      dict(source_id="C" * 64), dict(source_id=1), dict(unknown="c" * 64)):
            with self.subTest(extra=extra):
                store, reply = self.dispatch("/pc/v1/file", dict(base, **extra))
                store.offer.assert_not_called()
                self.assertEqual(reply.call_args.args[0], 400)


if __name__ == "__main__":
    unittest.main()
