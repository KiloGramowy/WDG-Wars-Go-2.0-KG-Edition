"""Synthetic Dark3D closed-file import regression; no hardware, RF or uploads."""
import csv
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdogs.loot_manager import LootManager
from watchdogs.piglet_collection import CollectionError, MAX_CHUNK, PigletCollectionStore
from watchdogs.piglet_csv import NATIVE_HEADER, native_row
from watchdogs.piglet_http import PigletHTTPService


WIFI, BLE, SECOND = "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"
PREAMBLE = "WigleWifi-1.4,appRelease=v2.3.0+dark3d.12,board=ESP32-C5-DevKit,brand=Dark3D"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def row(mac=WIFI, kind="WIFI", name="Synthetic", stamp="2026-11-13 12:34:56", rssi="-61"):
    return [mac, name, "[WPA2-PSK-CCMP][ESS]" if kind == "WIFI" else "[LE Random]",
            stamp, "36" if kind == "WIFI" else "0", rssi, "12.500001", "34.500002", "100.25", "0.8", kind]


def contents(rows, *, preamble=PREAMBLE, newline="\r\n", bom=False):
    stream = io.StringIO(newline="")
    stream.write(preamble + newline)
    writer = csv.writer(stream, lineterminator=newline)
    writer.writerow(NATIVE_HEADER)
    writer.writerows(rows)
    return (b"\xef\xbb\xbf" if bom else b"") + stream.getvalue().encode("utf-8")


class Dark3DCollectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self._open_loot()
        self.addCleanup(self._close_loot)
        self.store = PigletCollectionStore(self.loot)
        self.addCleanup(lambda: self.store.close())
        self.xp, self.wifi, self.ble = 100, set(), set()
        self.assertTrue(self.loot.save_xp(self.xp))

    def _open_loot(self):
        with patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
            self.loot = LootManager(str(self.root), SimpleNamespace(
                fix=SimpleNamespace(valid=True, latitude=80, longitude=80)))

    def _close_loot(self):
        with patch.object(os, "sync", lambda: None, create=True):
            self.loot.close()

    def _session(self):
        return self.store.begin("dark3d-123456789abc", 1, source_items=1)["session_id"]

    def _transfer(self, data, *, name="wigle-2026-11-13T123456+0000.log", session=None, sha=None):
        session, sha = session or self._session(), sha or digest(data)
        self.store.offer(session, name, len(data), sha, source_id=digest(name.encode()))
        for offset in range(0, len(data), MAX_CHUNK):
            chunk = data[offset:offset+MAX_CHUNK]
            self.store.chunk(session, sha, offset, chunk, digest(chunk))
        return session, sha

    def _process_once(self):
        result = self.store.process_pending(self.xp, self.wifi, self.ble)
        if result["committed"]:
            self.xp = result["xp_total"]
        return result

    def _settle(self, session, sha):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self._process_once()
            state = self.store.status(session, sha)
            if state["state"] in ("committed", "error"):
                return state
            time.sleep(.005)
        self.fail("Dark3D import did not settle: " + repr(self.store.status(session, sha)))

    def test_log_bytes_name_preamble_fields_time_gps_and_ble_export_are_preserved(self):
        name = "wigle-2026-11-13T123456+0000.LOG"
        rows = [row(name='Lab, "quoted"\r\ncontinued'), row(BLE, "BLE", "Błękit")]
        data = contents(rows, bom=True)
        session, sha = self._transfer(data, name=name)
        self.assertEqual(self.store._path(sha).suffix, ".log")
        self.assertEqual(self.store._path(sha).read_bytes(), data)
        self.assertNotIn("receipt", self.store.status(session, sha))
        self.store.commit(session, sha)
        state = self._settle(session, sha)
        self.assertEqual(state["state"], "committed")
        self.assertEqual((state["wifi"], state["ble"], state["duplicates"], self.xp), (1, 1, 0, 125))
        self.assertEqual(set(state["receipt"]), {"protocol", "collector_id", "sha256", "size", "import_id",
                                                 "records", "wifi", "ble", "duplicates"})
        with self.store._lock:
            records = [json.loads(item[0]) for item in self.store._sql.execute(
                "SELECT record FROM prepared WHERE sha=? ORDER BY ordinal", (sha,))]
        for record, original in zip(records, rows):
            self.assertEqual(record["source_fields"], original)
            self.assertEqual(record["source_time"], original[3])
            self.assertIsNone(record["captured_at"])
            self.assertFalse(record["capture_timezone_known"])
            self.assertEqual((record["position"]["lat"], record["position"]["lon"]), (12.500001, 34.500002))
            self.assertEqual((record["position"]["altitude"], record["position"]["accuracy"]), (100.25, .8))
            self.assertNotIn("frequency", record)
        exported = self.loot._base / self.loot.collection_file_receipt(sha)["session"]
        with (exported / "wardriving.csv").open(encoding="utf-8", newline="") as stream:
            stream.readline()
            native = list(csv.reader(stream))
        self.assertEqual(native, [list(NATIVE_HEADER)] + [native_row(record) for record in records])
        with (exported / "bt_devices.csv").open(encoding="utf-8", newline="") as stream:
            ble = list(csv.DictReader(stream))
        self.assertEqual((ble[0]["rssi"], ble[0]["lat"], ble[0]["lon"], ble[0]["timestamp"]),
                         (rows[1][5], rows[1][6], rows[1][7], rows[1][3]))
        provenance = json.loads((exported / "collection_source.json").read_text())
        self.assertEqual((provenance["name"], provenance["source_preamble"], provenance["source_header"]),
                         (name, PREAMBLE, list(NATIVE_HEADER)))
        self.assertEqual(provenance["source_format"], "dark3d-wigle-1.4")
        self.assertEqual(Path(provenance["raw_archive"]).read_bytes(), data)
        self.assertEqual(provenance["source_offers_at_import"], [dict(
            device_id="dark3d-123456789abc", session_id=session, name=name, source_id=digest(name.encode()))])
        self.assertFalse((exported / ".uploaded").exists())
        self.assertTrue(self.store.finish(session)["shutdown_allowed"])

    def test_log_resume_retry_and_restart_keep_durable_prefix_and_raw_suffix(self):
        data, name = contents([row()]), "resume.log"
        sha, session = digest(data), self._session()
        self.store.offer(session, name, len(data), sha, source_id=digest(name.encode()))
        first = data[:100]
        for _ in range(2):
            self.assertEqual(self.store.chunk(session, sha, 0, first, digest(first))["offset"], 100)
        with self.assertRaises(CollectionError) as caught:
            self.store.chunk(session, sha, 101, b"next", digest(b"next"))
        self.assertEqual((caught.exception.status, caught.exception.offset), (409, 100))
        self.assertTrue(self.store.close())
        with self.store._path(sha).open("ab") as stream:
            stream.write(b"uncommitted tail")
        self.store = PigletCollectionStore(self.loot)
        self.assertEqual(self.store._path(sha).suffix, ".log")
        resumed = self._session()
        state = self.store.offer(resumed, name, len(data), sha, source_id=digest(name.encode()))
        self.assertEqual(state["offset"], 100)
        self.assertEqual(self.store._path(sha).read_bytes(), first)
        self.assertNotIn("receipt", state)
        rest = data[100:]
        self.store.chunk(resumed, sha, 100, rest, digest(rest))
        self.store.commit(resumed, sha)
        self.assertEqual(self._settle(resumed, sha)["state"], "committed")
        self.assertEqual(self.store._path(sha).read_bytes(), data)
        self.assertEqual(self.xp, 115)

    def test_raw_non_utf8_radio_name_retains_bytes_and_does_not_block_manifest_or_ack(self):
        raw_name = b"bad\xffname"
        data = contents([row(name="raw_name_here")]).replace(b"raw_name_here", raw_name)
        session, sha = self._transfer(data, name="raw-name.log")
        self.store.commit(session, sha)
        state = self._settle(session, sha)
        self.assertEqual(state["state"], "committed")
        self.assertIn("receipt", state)
        self.assertEqual(self.xp, 115)
        self.assertEqual(self.store._path(sha).read_bytes(), data)
        with self.store._lock:
            record = json.loads(self.store._sql.execute(
                "SELECT record FROM prepared WHERE sha=?", (sha,)).fetchone()[0])
        self.assertEqual(record["source_name_bytes_hex"], raw_name.hex())
        exported = self.loot._base / self.loot.collection_file_receipt(sha)["session"]
        provenance = json.loads((exported / "collection_source.json").read_text(encoding="utf-8"))
        self.assertEqual(provenance["source_preamble"], PREAMBLE)
        self.assertEqual(provenance["source_header"], list(NATIVE_HEADER))
        self.assertEqual(Path(provenance["raw_archive"]).read_bytes(), data)

    def test_renamed_repacked_and_overlapping_log_observations_never_reward_twice(self):
        original = [row(), row(), row(BLE, "BLE")]
        data = contents(original)
        session, sha = self._transfer(data)
        self.store.commit(session, sha)
        receipt = self._settle(session, sha)["receipt"]
        self.assertEqual((receipt["duplicates"], self.xp), (1, 125))
        again = self._session()
        replay = self.store.offer(again, "renamed.log", len(data), sha, source_id=digest(b"renamed-path"))
        self.assertEqual(replay["receipt"], receipt)
        self.assertEqual(self.xp, 125)
        repacked = contents([original[2], original[0]], preamble=PREAMBLE + ",repacked=1", newline="\n")
        session2, sha2 = self._transfer(repacked, name="repacked.log")
        self.store.commit(session2, sha2)
        state = self._settle(session2, sha2)
        self.assertEqual((state["wifi"], state["ble"], state["duplicates"], self.xp), (0, 0, 2, 125))
        overlap = contents([original[0], row(SECOND), original[2], original[0]])
        session3, sha3 = self._transfer(overlap, name="overlap.log")
        self.store.commit(session3, sha3)
        state = self._settle(session3, sha3)
        self.assertEqual((state["wifi"], state["ble"], state["duplicates"], self.xp), (1, 0, 3, 140))
        with self.store._lock:
            names = {item[0] for item in self.store._sql.execute("SELECT name FROM session_sources WHERE sha=?", (sha,))}
        self.assertEqual(names, {"wigle-2026-11-13T123456+0000.log", "renamed.log"})
        self.assertEqual(self.store._path(sha).read_bytes(), data)
        self.assertEqual(self.store._path(sha2).read_bytes(), repacked)
        self.assertEqual(self.store._path(sha3).read_bytes(), overlap)

    def test_ack_waits_for_durable_completion_and_survives_store_and_loot_restart(self):
        data = contents([row()])
        session, sha = self._transfer(data)
        self.store.commit(session, sha)
        deadline = time.monotonic() + 5
        with patch.object(self.loot, "complete_collection_file", return_value=False):
            while self.xp == 100 and time.monotonic() < deadline:
                self._process_once()
                time.sleep(.005)
            self.assertEqual(self.xp, 115)
            self.assertNotIn("receipt", self.store.status(session, sha))
        receipt = self._settle(session, sha)["receipt"]
        collector = self.store.collector_id
        self.assertTrue(self.store.close())
        self._close_loot()
        self._open_loot()
        self.store = PigletCollectionStore(self.loot)
        self.assertEqual(self.loot.load_xp(), 115)
        self.assertEqual(self.store.collector_id, collector)
        self.assertEqual(self.store.status(session, sha)["receipt"], receipt)
        self.assertEqual(self.store._path(sha).read_bytes(), data)
        retry = self._session()
        self.assertEqual(self.store.offer(retry, "replayed.log", len(data), sha,
            source_id=digest(b"replayed"))["receipt"], receipt)
        self.assertEqual(self._process_once(), {"committed": False, "xp_total": 115})

    def test_interrupted_post_reward_index_recovery_cannot_pay_log_again(self):
        data = contents([row(), row(BLE, "BLE")])
        session, sha = self._transfer(data)
        self.store.commit(session, sha)
        deadline = time.monotonic() + 5
        result = {"committed": False}
        with patch.object(self.store, "_recover_file_batch", side_effect=OSError("synthetic interruption")):
            while not result["committed"] and time.monotonic() < deadline:
                result = self._process_once()
                time.sleep(.005)
        self.assertTrue(result["committed"])
        self.assertEqual(self.xp, 125)
        self.assertTrue(self.loot.collection_pending_batches())
        self.assertNotIn("receipt", self.store.status(session, sha))
        self.assertTrue(self.store.close())
        self.store = PigletCollectionStore(self.loot)
        self.assertFalse(self.loot.collection_pending_batches())
        state = self._settle(session, sha)
        self.assertEqual(state["state"], "committed")
        self.assertEqual((state["wifi"], state["ble"], self.xp, self.loot.load_xp()), (1, 1, 125, 125))
        self.assertEqual(self.store._path(sha).read_bytes(), data)

    def test_bad_digest_or_invalid_log_never_acknowledges_or_awards(self):
        invalid = row(BLE, "BLE")
        invalid[6] = "nan"
        for index, (data, sha) in enumerate(((contents([row()]), "a" * 64), (contents([row(), invalid]), None))):
            with self.subTest(sha=sha):
                session, actual_sha = self._transfer(data, sha=sha, name=f"invalid-{index}.log")
                self.store.commit(session, actual_sha)
                state = self._settle(session, actual_sha)
                self.assertEqual(state["state"], "error")
                self.assertNotIn("receipt", state)
                self.assertEqual(self.store._path(actual_sha).read_bytes(), data)
        self.assertEqual(self.xp, 100)
        self.assertEqual((self.wifi, self.ble), (set(), set()))

    def test_empty_log_and_missing_gps_do_not_invent_position_or_timestamp(self):
        missing = row()
        missing[6:10] = ["", "", "", ""]
        for index, data in enumerate((contents([]), contents([missing]))):
            session, sha = self._transfer(data, name=f"empty-gps-{index}.log")
            self.store.commit(session, sha)
            self.assertEqual(self._settle(session, sha)["state"], "committed")
        self.assertEqual(self.loot.get_gps_points(), [])
        self.assertEqual(self.xp, 115)

    def test_zero_byte_log_has_durable_original_zero_record_ack_and_no_xp(self):
        name = "zero.LOG"
        session, sha = self._transfer(b"", name=name)
        self.assertNotIn("receipt", self.store.status(session, sha))
        self.store.commit(session, sha)
        with patch.object(self.loot, "complete_collection_file", return_value=False) as complete:
            deadline = time.monotonic() + 5
            while not complete.called and time.monotonic() < deadline:
                self._process_once()
                time.sleep(.005)
            self.assertTrue(complete.called)
            self.assertNotIn("receipt", self.store.status(session, sha))
        state = self._settle(session, sha)
        self.assertEqual(state["state"], "committed")
        self.assertEqual((state["name"], state["size"], state["offset"]), (name, 0, 0))
        for field in ("records", "wifi", "ble", "duplicates", "new_wifi", "new_ble", "base_xp"):
            self.assertEqual(state[field], 0, field)
        self.assertEqual((self.xp, self.loot.load_xp(), self.wifi, self.ble), (100, 100, set(), set()))
        self.assertEqual(self.store.pending_events(), [])
        self.assertFalse(self.loot.collection_pending_batches())
        self.assertEqual(self.loot.get_gps_points(), [])
        raw = self.store._path(sha)
        self.assertEqual((raw.suffix, raw.read_bytes()), (".log", b""))
        saved = self.loot.collection_file_receipt(sha)
        self.assertEqual(saved["receipt"], state["receipt"])
        exported = self.loot._base / saved["session"]
        provenance = json.loads((exported / "collection_source.json").read_text())
        self.assertEqual((provenance["name"], provenance["size"], provenance["records"]), (name, 0, 0))
        self.assertEqual((provenance["source_preamble"], provenance["source_header"],
                          provenance["source_format"]), ("", [], "dark3d-empty-log"))
        self.assertEqual(Path(provenance["raw_archive"]).read_bytes(), b"")
        with (exported / "wardriving.csv").open(newline="") as stream:
            stream.readline()
            self.assertEqual(list(csv.reader(stream)), [list(NATIVE_HEADER)])
        self.assertTrue(self.store.finish(session)["shutdown_allowed"])

        # Reopen both durable stores, then exercise the real HTTP response signer.
        self.assertTrue(self.store.close())
        self._close_loot()
        self._open_loot()
        self.store = PigletCollectionStore(self.loot)
        retry = self._session()
        offered = self.store.offer(retry, "renamed-zero.log", 0, sha, source_id=digest(b"renamed-zero"))
        self.assertEqual(offered["receipt"], state["receipt"])
        self.assertEqual(self.store.commit(retry, sha)["receipt"], state["receipt"])
        token = "synthetic-empty-log-token-0123456789abcdef"
        service = PigletHTTPService(self.store, token, "127.0.0.1", 0, allow_loopback=True)
        service.start()
        try:
            connection = http.client.HTTPConnection(*service.address, timeout=2)
            try:
                connection.request("GET", "/pc/v1/status?id=" + sha, headers={
                    "Authorization": "Bearer " + token, "X-Piglet-Session": retry})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                signed = json.loads(response.read())["receipt"]
            finally:
                connection.close()
        finally:
            service.stop()
        fields = ("collector_id", "sha256", "size", "import_id", "records", "wifi", "ble", "duplicates")
        canonical = "PC1\n" + "\n".join(str(signed[field]) for field in fields) + "\n"
        self.assertEqual(signed.pop("signature"), hmac.digest(token.encode(), canonical.encode("ascii"), "sha256").hex())
        self.assertEqual(signed, state["receipt"])
        self.assertEqual(raw.read_bytes(), b"")
        self.assertEqual(self.loot.load_xp(), 100)
        self.assertTrue(self.store.finish(retry)["shutdown_allowed"])

    def test_empty_piglet_cannot_alias_empty_log_digest_before_or_after_import(self):
        session, sha = self._session(), digest(b"")
        for name in ("empty.csv", "empty.CSV"):
            with self.subTest(stage="before", name=name), self.assertRaises(CollectionError):
                self.store.offer(session, name, 0, sha, source_id=digest(name.encode()))
        self._transfer(b"", name="accepted.log", session=session)
        self.store.commit(session, sha)
        self.assertEqual(self._settle(session, sha)["state"], "committed")
        self.assertTrue(self.store.finish(session)["shutdown_allowed"])
        retry = self._session()
        with self.assertRaises(CollectionError):
            self.store.offer(retry, "empty.csv", 0, sha, source_id=digest(b"empty.csv"))
        with self.assertRaises(CollectionError) as missing:
            self.store.status(retry, sha)
        self.assertEqual(missing.exception.status, 404)
        self.assertFalse(self.store._path(sha, name="empty.csv").exists())
        self.assertEqual(self.loot.load_xp(), 100)

    def test_previous_receiver_empty_log_error_retries_after_restart_and_unblocks_next_batch(self):
        session, sha = self._transfer(b"", name="pending.log")
        # Durable state left by the previous receiver's rejected zero-byte log.
        self.store._set_error(sha, "CSV validation or local import failed; original file retained")
        self.assertNotIn("receipt", self.store.status(session, sha))
        self.assertTrue(self.store.close())
        self.store = PigletCollectionStore(self.loot)
        retry = self._session()
        offered = self.store.offer(retry, "pending.log", 0, sha, source_id=digest(b"pending.log"))
        self.assertEqual(offered["state"], "error")
        self.assertNotIn("receipt", offered)
        self.store.commit(retry, sha)
        self.assertEqual(self._settle(retry, sha)["state"], "committed")
        self.assertEqual((self.store._path(sha).read_bytes(), self.xp), (b"", 100))
        self.assertTrue(self.store.finish(retry)["shutdown_allowed"])
        following, next_sha = self._transfer(contents([row()]), name="following.log")
        self.store.commit(following, next_sha)
        self.assertEqual((self._settle(following, next_sha)["state"], self.xp), ("committed", 115))
        self.store.acknowledge_events([event["event_id"] for event in self.store.pending_events()])
        self.assertTrue(self.store.finish(following)["shutdown_allowed"])

    def test_nonzero_source_changed_to_empty_gets_new_receipt_only_in_next_batch(self):
        name, data = "same.log", contents([row()])
        session, original_sha = self._transfer(data, name=name)
        self.store.commit(session, original_sha)
        original = self._settle(session, original_sha)
        self.assertEqual((original["state"], original["records"], self.xp), ("committed", 1, 115))
        self.store.acknowledge_events([event["event_id"] for event in self.store.pending_events()])
        # A protected source cannot change identity within its existing job.
        with self.assertRaises(CollectionError) as changed:
            self.store.offer(session, name, 0, digest(b""), source_id=digest(name.encode()))
        self.assertEqual(changed.exception.status, 409)
        self.assertTrue(self.store.finish(session)["shutdown_allowed"])

        empty_session, empty_sha = self._transfer(b"", name=name)
        self.assertNotEqual(empty_sha, original_sha)
        self.assertNotIn("receipt", self.store.status(empty_session, empty_sha))
        self.store.commit(empty_session, empty_sha)
        empty = self._settle(empty_session, empty_sha)
        self.assertEqual((empty["state"], empty["records"], empty["size"]), ("committed", 0, 0))
        self.assertNotEqual(empty["receipt"]["import_id"], original["receipt"]["import_id"])
        self.assertEqual(empty["receipt"]["sha256"], empty_sha)
        self.assertEqual(self.store._path(original_sha).read_bytes(), data)
        self.assertEqual(self.store._path(empty_sha).read_bytes(), b"")
        self.assertEqual((self.xp, self.loot.load_xp()), (115, 115))
        self.assertTrue(self.store.finish(empty_session)["shutdown_allowed"])

        next_session, next_sha = self._transfer(contents([row(SECOND)]), name="next.log")
        self.store.commit(next_session, next_sha)
        following = self._settle(next_session, next_sha)
        self.assertEqual((following["state"], following["records"], self.xp), ("committed", 1, 130))
        self.store.acknowledge_events([event["event_id"] for event in self.store.pending_events()])
        self.assertTrue(self.store.finish(next_session)["shutdown_allowed"])

    def test_log_index_creation_failure_retries_same_empty_orphan_safely(self):
        data, session, name = contents([row()]), self._session(), "retry.log"
        sha = digest(data)
        with self.store._lock, self.store._sql:
            self.store._sql.execute("CREATE TRIGGER synthetic_log_failure BEFORE INSERT ON files "
                                    "BEGIN SELECT RAISE(ABORT,'synthetic index failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.offer(session, name, len(data), sha, source_id=digest(name.encode()))
        self.assertEqual(self.store._path(sha, name=name).stat().st_size, 0)
        with self.store._lock, self.store._sql:
            self.store._sql.execute("DROP TRIGGER synthetic_log_failure")
        self._transfer(data, name=name, session=session)
        self.store.commit(session, sha)
        self.assertEqual(self._settle(session, sha)["state"], "committed")
        self.assertEqual(self.store._path(sha).suffix, ".log")


if __name__ == "__main__":
    unittest.main()
