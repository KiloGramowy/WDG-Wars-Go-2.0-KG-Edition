"""Sync preparation, resource failure and wire-format regressions; synthetic I/O only."""
import base64
import builtins
import csv
import hashlib
import hmac
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from plugins.wardrive_upload import UploadReadError, WardriveUpload
from watchdogs.loot_manager import LootManager
from test_pending_log_ui import PyxelRecorder


class SyncPreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.session = self.base / "closed"
        self.session.mkdir()
        with patch.object(WardriveUpload, "_load_state"):
            self.plugin = WardriveUpload()
        p = self.plugin
        p._api_url, p._api_key = "https://example.invalid/api/upload/", "synthetic-key"
        p._state_file = lambda: self.base / "state.json"
        p.app = SimpleNamespace(level=6, loot=SimpleNamespace(
            _base=str(self.base), session_dir=str(self.base / "active"),
            prepare_upload_log_discard=lambda *_args: True), _term_add=Mock())
        p._badge_sync_worker = Mock()
        p._handle_upload_badges = Mock()
        self.releases = []
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        for event in self.releases:
            event.set()
        self.plugin.on_unload()
        thread = self.plugin._sync_worker_thread
        if thread is not None:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "Synthetic worker did not stop")

    def barrier(self, result=None):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def blocked():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release I/O barrier")
            return result
        return entered, release, blocked

    def wait_idle(self):
        self.assertTrue(self.plugin._sync_idle.wait(5), "Worker did not finish")

    def write_kind(self, kind, *, no_gps=False):
        lat, lon = (0, 0) if no_gps else (36.4, -6.2)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        if kind in ("WIFI", "BLE"):
            filename, parser = "wardriving.csv", "_parse_csv"
            stream.write(LootManager._WIGLE_PRE_HEADER)
            stream.write(LootManager._WIGLE_HEADER)
            for suffix in ("55", "66"):
                writer.writerow(["00:11:22:33:44:" + suffix, "synthetic", "[ESS]", "now",
                                 1, -50, lat, lon, 10, 1, kind])
        elif kind == "ADSB":
            filename, parser = "adsb_aircraft.csv", "_parse_adsb"
            writer.writerow(["timestamp", "icao", "callsign", "lat", "lon", "alt_ft", "speed_kt", "heading"])
            for identity in ("abc123", "def456"):
                writer.writerow(["now", identity, "synthetic", lat, lon, 10000, 200, 20])
        else:
            filename = "meshtastic_nodes.csv" if kind == "MT" else "meshcore_nodes.csv"
            parser = "_parse_meshtastic" if kind == "MT" else "_parse_meshcore"
            writer.writerow(["timestamp", "node_id", "type", "name", "lat", "lon", "rssi", "snr"])
            for identity in ("12345678", "87654321"):
                writer.writerow(["now", identity, "Client", "synthetic", lat, lon, -90, 0])
        path = self.session / filename
        path.write_text(stream.getvalue(), encoding="utf-8", newline="")
        return path, parser

    def failing_open(self, path, error):
        original = builtins.open
        content = path.read_text(encoding="utf-8")
        before_error = 3 if path.name == "wardriving.csv" else 2
        class Interrupted(io.StringIO):
            count = 0
            def readline(self, *args):
                if self.count >= before_error:
                    raise error("synthetic interrupted read")
                self.count += 1
                return super().readline(*args)
            def __next__(self):
                line = self.readline()
                if not line:
                    raise StopIteration
                return line
        def open_file(file, mode="r", *args, **kwargs):
            if Path(file) == path and mode == "r":
                return Interrupted(content)
            return original(file, mode, *args, **kwargs)
        return patch("builtins.open", side_effect=open_file)

    def test_sync_open_draw_and_update_never_do_catalog_io_on_ui_thread(self):
        p = self.plugin
        entered, release, blocked = self.barrier([self.session])
        threads = []
        p._pending_sessions = Mock(side_effect=lambda: (threads.append(threading.get_ident()), blocked())[1])
        p._retention_tick = Mock(side_effect=lambda: threads.append(threading.get_ident()))
        px = PyxelRecorder()
        with patch.dict(sys.modules, {"pyxel": px}):
            p.open_overlay()
            self.assertTrue(entered.wait(5))
            self.assertIn("preparing", p._overlay_items()[0][1])
            p.draw(0, 0, 640, 360)
            p._next_cleanup = float("inf")
            p.on_update()
            self.assertTrue(all(identity != threading.get_ident() for identity in threads))
            self.assertEqual(p._pending_sessions.call_count, 1)
            release.set()
            self.wait_idle()
            self.assertIn("1 pending", p._overlay_items()[0][1])

    def test_refresh_requests_and_reopen_coalesce_in_one_worker(self):
        p = self.plugin
        entered, release, blocked = self.barrier([])
        p._retention_tick = Mock()
        p._pending_sessions = Mock(side_effect=blocked)
        p.open_overlay()
        self.assertTrue(entered.wait(5))
        worker = p._sync_worker_thread
        p._overlay_active = False
        for _ in range(20):
            p._request_sync_refresh()
        p.open_overlay()
        self.assertIs(p._sync_worker_thread, worker)
        release.set()
        self.wait_idle()
        self.assertEqual(p._pending_sessions.call_count, 2)

    def test_stale_account_snapshot_is_hidden_and_result_is_not_published(self):
        for attribute, value in (("_api_key", "different-key"), ("_api_url", "https://other.invalid/api/upload/"),
                                 ("_receipt_generation", 7)):
            with self.subTest(attribute=attribute):
                p = self.plugin
                entered, release, blocked = self.barrier([self.session])
                p._retention_tick = Mock()
                p._pending_sessions = Mock(side_effect=blocked)
                p._publish_sync_snapshot(p._sync_identity(), sessions=[self.session])
                p._request_sync_refresh()
                self.assertTrue(entered.wait(5))
                setattr(p, attribute, value)
                self.assertEqual(p._sync_snapshot_view()["state"], "preparing")
                p._pending_sessions.side_effect = [[], []]
                release.set()
                self.wait_idle()
                self.assertEqual(p._sync_snapshot_view()["sessions"], ())
                self.assertEqual(p._sync_snapshot_view()["identity"], p._sync_identity())

    def test_upload_selection_is_reprepared_off_ui_and_latest_is_respected(self):
        p = self.plugin
        entered, release, blocked = self.barrier([self.base / "old", self.session])
        p._retention_tick = Mock()
        p._pending_sessions = Mock(side_effect=blocked)
        p._publish_sync_snapshot(p._sync_identity(), sessions=[self.base / "stale"])
        p._upload_worker = Mock()
        p._start_upload(all_sessions=False)
        self.assertTrue(entered.wait(5))
        p._upload_worker.assert_not_called()
        self.assertTrue(p._sync_upload_pending)
        release.set()
        self.wait_idle()
        p._upload_worker.assert_called_once_with([self.session])
        self.assertFalse(p._uploading)
        self.assertFalse(p._sync_upload_pending)

    def test_account_change_during_upload_preparation_prevents_send(self):
        p = self.plugin
        entered, release, blocked = self.barrier([self.session])
        p._retention_tick = Mock()
        p._pending_sessions = Mock(side_effect=blocked)
        p._upload_worker = Mock()
        p._start_upload()
        self.assertTrue(entered.wait(5))
        p._api_key = "another-account"
        release.set()
        self.wait_idle()
        p._upload_worker.assert_not_called()
        self.assertFalse(p._sync_upload_pending)

    def test_io_and_resource_preparation_failures_show_error_and_release_flags(self):
        p = self.plugin
        p._retention_tick = Mock()
        for error in (OSError, MemoryError):
            with self.subTest(error=error):
                p._pending_sessions = Mock(side_effect=error("synthetic"))
                p._start_upload()
                self.wait_idle()
                self.assertEqual(p._sync_snapshot_view()["state"], "error")
                self.assertIn("check failed", p._overlay_items()[0][1])
                self.assertFalse(p._uploading)
                self.assertFalse(p._sync_upload_pending)

    def test_unload_reports_busy_worker_and_stop_prevents_followup_upload(self):
        p = self.plugin
        entered, release, blocked = self.barrier([self.session])
        p._retention_tick = Mock()
        p._pending_sessions = Mock(side_effect=blocked)
        p._upload_worker = Mock()
        p._start_upload()
        self.assertTrue(entered.wait(5))
        thread = p._sync_worker_thread
        with patch.object(thread, "join") as join:
            self.assertFalse(p.on_unload())
            join.assert_called_once_with(timeout=1.0)
        self.assertFalse(p._sync_idle.is_set())
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(p.on_unload())
        p._upload_worker.assert_not_called()
        p._request_sync_refresh()
        self.assertIs(p._sync_worker_thread, thread)

    def test_no_gps_mesh_cache_reuses_unchanged_parse_and_invalidates_on_fix(self):
        p = self.plugin
        for kind in ("MC", "MT"):
            with self.subTest(kind=kind):
                for previous in self.session.glob("*.csv"):
                    previous.unlink()
                path, parser_name = self.write_kind(kind, no_gps=True)
                original = path.read_bytes()
                with patch.object(p, parser_name, wraps=getattr(p, parser_name)) as parser:
                    self.assertEqual(p._pending_sessions(), [])
                    initial_calls = parser.call_count
                    for _ in range(5):
                        self.assertEqual(p._pending_sessions(), [])
                        self.assertEqual(p._mesh_gps_wait, 1)
                    self.assertEqual(parser.call_count, initial_calls)
                    self.assertEqual(path.read_bytes(), original)
                    self.write_kind(kind)
                    self.assertEqual(p._pending_sessions(), [self.session])
                    self.assertGreater(parser.call_count, initial_calls)

    def test_interrupted_csv_never_returns_partial_data_or_authorizes_upload_cleanup(self):
        p = self.plugin
        for kind in ("WIFI", "BLE", "ADSB", "MC", "MT"):
            for error in (OSError, MemoryError):
                with self.subTest(kind=kind, error=error):
                    for previous in self.session.glob("*.csv"):
                        previous.unlink()
                    path, parser = self.write_kind(kind)
                    before = path.read_bytes()
                    self.assertEqual(len(getattr(p, parser)(path)), 2)
                    p._uploading = True
                    with self.failing_open(path, error), patch("plugins.wardrive_upload._open") as network:
                        with self.assertRaises(UploadReadError):
                            getattr(p, parser)(path)
                        inspected = p._inspect_pending_log(self.session, path.name)
                        self.assertEqual(inspected["status"], "unreadable")
                        self.assertFalse(inspected["can_delete"])
                        p._upload_worker([self.session])
                    network.assert_not_called()
                    self.assertFalse(p._uploading)
                    self.assertEqual(p._last_upload_summary["pending"], 1)
                    self.assertEqual(p._last_upload_summary["preparation_errors"], 1)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(p._uploaded_sessions, set())
                    self.assertEqual(p._uploaded_meshtastic_sessions, set())
                    self.assertEqual(p._upload_receipts, {})

    def test_receipt_persistence_failure_keeps_every_data_type_pending(self):
        p = self.plugin
        for kind in ("WIFI", "BLE", "ADSB", "MC", "MT"):
            for failure in (False, MemoryError):
                with self.subTest(kind=kind, failure=failure):
                    for previous in self.session.glob("*.csv"):
                        previous.unlink()
                    path, _ = self.write_kind(kind)
                    before = path.read_bytes()
                    response = dict(ok=True, imported=2, aircraft_imported=2, meshcore_imported=2)
                    remote = io.BytesIO(json.dumps(response).encode())
                    save = Mock(return_value=False) if failure is False else Mock(side_effect=MemoryError("synthetic"))
                    p._uploading = True
                    with patch("plugins.wardrive_upload._open", return_value=remote), patch.object(p, "_save_state", save):
                        p._upload_worker([self.session])
                    self.assertFalse(p._uploading)
                    self.assertEqual(p._last_upload_summary["pending"], 1)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(p._uploaded_sessions, set())
                    self.assertEqual(p._uploaded_meshtastic_sessions, set())
                    self.assertEqual(p._upload_receipts, {})

    def test_cleanup_memory_error_cannot_leave_uploading_stuck(self):
        p = self.plugin
        p._uploading = True
        with patch.object(p, "cleanup_no_gps_logs", side_effect=MemoryError), \
                patch.object(p, "cleanup_uploaded_logs", side_effect=MemoryError):
            p._upload_worker([])
        self.assertFalse(p._uploading)

    def test_thread_start_resource_failure_leaves_retryable_error_not_busy(self):
        p = self.plugin
        with patch("plugins.wardrive_upload.threading.Thread.start", side_effect=MemoryError):
            p._start_upload()
        self.assertFalse(p._sync_upload_pending)
        self.assertFalse(p._uploading)
        self.assertTrue(p._sync_idle.is_set())
        self.assertEqual(p._sync_snapshot_view()["state"], "error")
        self.assertIsNone(p._sync_worker_thread)

    def test_oversized_server_response_is_bounded_and_cannot_acknowledge(self):
        p = self.plugin
        path, _ = self.write_kind("MT")
        before = path.read_bytes()
        class OversizedBody:
            def __len__(self):
                return 16 * 1024 * 1024 + 1
            def decode(self):
                raise AssertionError("Oversized body must not be decoded")
        remote = MagicMock()
        remote.__enter__.return_value = remote
        remote.read.return_value = OversizedBody()
        with patch("plugins.wardrive_upload._open", return_value=remote):
            p._upload_worker([self.session])
        remote.read.assert_called_once_with(16 * 1024 * 1024 + 1)
        self.assertEqual(p._last_upload_summary["pending"], 1)
        self.assertEqual(p._upload_receipts, {})
        self.assertEqual(p._uploaded_sessions, set())
        self.assertEqual(p._uploaded_meshtastic_sessions, set())
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(any("response too large" in text for text, _ in p._log))

    def test_meshtastic_upload_has_no_redundant_initial_parse(self):
        p = self.plugin
        self.write_kind("MT")
        with patch.object(p, "_parse_meshtastic", wraps=p._parse_meshtastic) as parse, \
                patch.object(p, "_retention_capture", return_value=[]), \
                patch.object(p, "cleanup_no_gps_logs"), \
                patch("plugins.wardrive_upload._open", return_value=io.BytesIO(b'{"ok": false}')):
            p._upload_worker([self.session])
        # Retention proof parsing remains independently checked in production.
        parse.assert_called_once_with(self.session / "meshtastic_nodes.csv")

    def test_stop_before_invalid_prune_unlink_preserves_file(self):
        p = self.plugin
        path = self.session / "meshtastic_nodes.csv"
        path.write_text("timestamp,node_id,lat,lon\nnow,invalid,0,0\n")
        before = path.read_bytes()
        def preserve(*_args):
            p._sync_stop.set()
            return True
        p.app.loot.prepare_upload_log_discard = preserve
        self.assertEqual(p._prune_upload_logs(self.session), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_slow_closed_log_parse_does_not_hold_the_game_database_lock(self):
        p = self.plugin
        self.write_kind("MT")
        p.app.loot._db_lock = lock = threading.RLock()
        entered, release, blocked = self.barrier([])
        original = p._parse_meshtastic
        def slow_parse(path):
            blocked()
            return original(path)
        thread = threading.Thread(target=p._prune_upload_logs, args=(self.session,))
        with patch.object(p, "_parse_meshtastic", side_effect=slow_parse):
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                acquired = lock.acquire(blocking=False)
                if acquired:
                    lock.release()
                self.assertTrue(acquired, "Slow preparation blocks main-loop XP/GPS writers")
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_retention_hash_preparation_does_not_hold_game_database_lock(self):
        p = self.plugin
        self.write_kind("MT")
        p.app.loot._db_lock = lock = threading.RLock()
        data = p._session_upload_data(self.session)
        self.assertTrue(p._remember_receipt(data, dict(ok=True, meshcore_imported=2)))
        store = p._get_retention()
        now = [1_800_000_000]
        store._clock = lambda: now[0]
        self.assertTrue(store.set_days(1))
        snapshots = p._retention_capture(self.session, data)
        p._retention_confirm_files(snapshots, data)
        now[0] += 86400
        operations = (("capture", lambda: p._retention_capture(self.session, data)),
                      ("confirm", lambda: p._retention_confirm_files(snapshots, data)),
                      ("cleanup", p.cleanup_uploaded_logs))
        for name, operation in operations:
            with self.subTest(operation=name):
                entered, release, blocked = self.barrier([])
                original = p._upload_log_token
                def slow_token(path):
                    blocked()
                    return original(path)
                errors = []
                def run():
                    try:
                        operation()
                    except Exception as error:
                        errors.append(error)
                thread = threading.Thread(target=run)
                with patch.object(p, "_upload_log_token", side_effect=slow_token):
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        acquired = lock.acquire(blocking=False)
                        if acquired:
                            lock.release()
                        self.assertTrue(acquired, "Slow retention hash blocks gameplay writers")
                    finally:
                        release.set()
                        thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_changed_account_or_loot_source_aborts_entire_batch(self):
        p = self.plugin
        path, _ = self.write_kind("MT")
        other = self.base / "second"
        other.mkdir()
        second_path = other / path.name
        second_path.write_bytes(path.read_bytes())
        originals = {file: file.read_bytes() for file in (path, second_path)}
        original_loot = p.app.loot
        for change in ("key", "url", "base", "loot"):
            with self.subTest(change=change):
                p.app.loot = original_loot
                original_loot._base = str(self.base)
                p._api_url, p._api_key = "https://example.invalid/api/upload/", "synthetic-key"
                sent = []
                def response(request, **_kwargs):
                    sent.append((request.full_url, request.get_header("X-api-key")))
                    if change == "key":
                        p._api_key = "new-account"
                    elif change == "url":
                        p._api_url = "https://other.invalid/api/upload/"
                    elif change == "base":
                        p.app.loot._base = str(self.base / "changed")
                    else:
                        p.app.loot = SimpleNamespace(_base=str(self.base))
                    return io.BytesIO(b'{"ok": true, "meshcore_imported": 2}')
                with patch("plugins.wardrive_upload._open", side_effect=response):
                    p._upload_worker([self.session, other])
                self.assertEqual(sent, [("https://example.invalid/api/upload/", "synthetic-key")])
                self.assertEqual(p._last_upload_summary["pending"], 2)
                self.assertEqual(p._upload_receipts, {})
                self.assertEqual(p._uploaded_sessions, set())
                self.assertEqual(p._uploaded_meshtastic_sessions, set())
                self.assertEqual({file: file.read_bytes() for file in originals}, originals)
        p.app.loot = original_loot
        original_loot._base = str(self.base)

    def test_batch_receipt_generation_changes_do_not_abort_same_account(self):
        p = self.plugin
        path, _ = self.write_kind("MT")
        other = self.base / "second"
        other.mkdir()
        (other / path.name).write_bytes(path.read_bytes().replace(b"12345678", b"abcdef01"))
        def response(*_args, **_kwargs):
            return io.BytesIO(b'{"ok": true, "meshcore_imported": 2}')
        with patch("plugins.wardrive_upload._open", side_effect=response) as network, \
                patch("time.sleep"):
            p._upload_worker([self.session, other])
        self.assertEqual(network.call_count, 2)
        self.assertEqual(p._last_upload_summary["confirmed"], 2)
        self.assertEqual(p._last_upload_summary["pending"], 0)
        self.assertEqual(p._receipt_generation, 2)

    def test_signature_and_receipt_hmac_keep_exact_existing_bytes(self):
        p = self.plugin
        data = {"networks": [{"ssid": "Łódź café", "lat": 36.4}], "aircraft": [], "meshcore_nodes": []}
        nonce, key = "0123456789abcdef", "override-key"
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        encoded = base64.b64encode(raw).decode()
        signature = hmac.new(key.encode(), (nonce + encoded).encode(), hashlib.sha256).hexdigest()
        expected = json.dumps(dict(data=encoded, nonce=nonce, sig=signature)).encode()
        with patch("plugins.wardrive_upload.secrets.token_hex", return_value=nonce):
            self.assertEqual(p._sign_payload(data, api_key=key), expected)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        message = "wdg-upload-receipt-v1\0" + p._api_url + "\0" + canonical
        self.assertEqual(p._receipt_key(data), hmac.new(p._api_key.encode(), message.encode(), hashlib.sha256).hexdigest())


if __name__ == "__main__":
    unittest.main()
