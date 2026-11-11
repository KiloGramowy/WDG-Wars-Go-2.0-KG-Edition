"""Local closed-file transfer, exact import replay and crash boundaries; no RF/network."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import WardriveUpload
from watchdogs.loot_manager import LootManager
from watchdogs.piglet_collection import CollectionError, MAX_CHUNK, MAX_SIZE, PigletCollectionStore
from watchdogs.piglet_csv import (NATIVE_HEADER, PIGLET_HEADER, PREAMBLE, native_row,
                                 MAX_PHYSICAL_LINE, _physical_lines, native_rows, parse_record, piglet_records)
from watchdogs.scan_rules import observation_xp


WIFI, BLE = "02:00:00:00:00:01", "02:00:00:00:00:02"


def row(mac=WIFI, kind="WIFI", name="Synthetic", stamp="2026-11-07 12:00:01", rssi="-61", lat="12.5", lon="34.5"):
    return [mac, name, "[WPA2-PSK-CCMP][ESS]" if kind == "WIFI" else "[LE Random]",
            stamp, "6" if kind == "WIFI" else "0", "2437" if kind == "WIFI" else "0",
            rssi, lat, lon, "100.25", "0.8", "abcd" if kind == "BLE" else "", "76", kind]


def contents(rows, extra=""):
    out = io.StringIO(newline="")
    out.write("WigleWifi-1.6,appRelease=PigletKG1.0.1" + extra + "\n")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(PIGLET_HEADER)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PigletCSVTests(unittest.TestCase):
    def test_exact_source_time_gps_hdop_and_radio_metadata_preserved(self):
        source = row(stamp="1970-01-01 00:09:12")
        record = parse_record(source)
        self.assertEqual(record["source_fields"], source)
        self.assertEqual(record["source_time"], source[3])
        self.assertEqual(record["captured_at"], "1970-01-01T00:09:12Z")
        self.assertEqual(record["position"]["accuracy"], .8)
        self.assertEqual(record["frequency"], 2437)
        self.assertEqual(native_row(record), [source[0], source[1], source[2], source[3], source[4], source[6], *source[7:11], source[13]])

    def test_no_source_position_never_substitutes_host_gps(self):
        for lat, lon in (("0", "0"), ("", "")):
            self.assertIsNone(parse_record(row(lat=lat, lon=lon))["position"])

    def test_ble_native_auth_changes_only_export_not_original_record(self):
        record = parse_record(row(BLE, "BLE"))
        self.assertEqual(native_row(record)[2], "[BLE]")
        self.assertEqual(record["source_fields"][2], "[LE Random]")
        self.assertEqual(record["frequency"], 0)

    def test_marked_multiline_comma_quote_names_roundtrip(self):
        name = 'Lab, "quoted"\r\nsecond\nthird'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.csv"
            path.write_bytes(contents([row(name=name), row(BLE, "BLE", name)]))
            records = list(piglet_records(path))
        out = io.StringIO(newline="")
        out.write(PREAMBLE + "\n")
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(NATIVE_HEADER)
        writer.writerows(native_row(record) for record in records)
        parsed = list(native_rows(io.StringIO(out.getvalue(), newline=""), LootManager._wardrive_fields))
        self.assertEqual([record[1] for record in parsed], [name, name])
        self.assertEqual([record[10] for record in parsed], ["WIFI", "BLE"])

    def test_invalid_rows_are_rejected_without_fabricating_fields(self):
        invalid = []
        for index, value in ((0, "not-a-mac"), (3, "2026-11-07"), (3, "2026-11-07 10:00:00+01:00"),
                             (4, "300"), (6, "nan"), (7, "91"), (8, "181"), (10, "-1"), (13, "LORA"), (1, "NUL\0")):
            source = row()
            source[index] = value
            invalid.append(source)
        invalid.extend([row()[:-1], row(lat="", lon="12")])
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_record(source)

    def test_legacy_unquoted_names_and_optional_headerless_identities_retained(self):
        old = LootManager._WIGLE_PRE_HEADER + LootManager._WIGLE_HEADER + f'{WIFI},"bad, name,[ESS],2020-01-01 12:00:00,6,-60,12,34,0,1,WIFI\n'
        self.assertEqual(list(LootManager.iter_wardrive_fields(io.StringIO(old)))[0][0], WIFI)
        headerless = f"{WIFI},old\n{BLE},older\n"
        self.assertEqual([r[0] for r in LootManager.iter_wardrive_fields(io.StringIO(headerless), legacy_prefix=True)], [WIFI, BLE])

    def test_shared_existing_reward_rules(self):
        self.assertEqual([observation_xp(p, n) for p, n in (("wifi", True), ("wifi", False), ("ble", True), ("ble", False))], [15, 1, 10, 1])
        with self.assertRaises(ValueError):
            observation_xp("mesh", True)

    def test_physical_preamble_and_header_reads_are_bounded_before_rejection(self):
        for data in ("WigleWifi-1.6," + "x" * (MAX_PHYSICAL_LINE * 2),
                     "WigleWifi-1.6,fixture\n" + "MAC," + "x" * (MAX_PHYSICAL_LINE * 2)):
            stream = io.StringIO(data, newline="")
            with self.assertRaises(ValueError):
                list(_physical_lines(stream))
            self.assertLessEqual(stream.tell(), MAX_PHYSICAL_LINE + 1 + len("WigleWifi-1.6,fixture\n"))

    def test_multibyte_physical_line_limit_counts_utf8_bytes(self):
        stream = io.StringIO("é" * (MAX_PHYSICAL_LINE // 2 + 1), newline="")
        with self.assertRaises(ValueError):
            list(_physical_lines(stream))


class PigletCollectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        with patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
            self.loot = LootManager(str(self.root), SimpleNamespace(fix=SimpleNamespace(valid=True, latitude=80, longitude=80)))
        self.addCleanup(self.close_loot)
        self.store = PigletCollectionStore(self.loot)
        self.addCleanup(lambda: self.store.close())
        self.wifi, self.ble, self.xp = set(), set(), 100
        self.delivered_events = []
        self.loot.save_xp(self.xp)

    def close_loot(self):
        with patch.object(os, "sync", lambda: None, create=True):
            self.loot.close()

    def overview(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = self.store.status_overview()
            if not value.get("busy"):
                return value
            time.sleep(.002)
        self.fail("Overview did not become available")

    def session(self, files=1, device="piglet-123456789abc", *, source_items=0):
        return self.store.begin(device, files, source_items=source_items)["session_id"]

    def transfer(self, data, session=None, name="closed.csv", sha=None, source_id=None):
        session, sha = session or self.session(), sha or digest(data)
        state = self.store.offer(session, name, len(data), sha, source_id=source_id)
        if state["state"] == "receiving":
            for offset in range(state["offset"], len(data), MAX_CHUNK):
                chunk = data[offset:offset + MAX_CHUNK]
                self.store.chunk(session, sha, offset, chunk, digest(chunk))
        return session, sha

    def poll(self, session, sha, *, events=True, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.store.process_pending(self.xp, self.wifi, self.ble)
            if result["committed"]:
                self.xp = result["xp_total"]
            if events:
                pending = self.store.pending_events(20)
                self.assertTrue(self.store.acknowledge_events([e["event_id"] for e in pending]))
                self.delivered_events.extend(pending)
            state = self.store.status(session, sha)
            if state["state"] in ("committed", "error"):
                return state
            time.sleep(.005)
        self.fail("Import did not settle: " + repr(self.store.status(session, sha)))

    def wait_exported_without_presentation(self, sha):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.store._lock:
                ready = self.store._sql.execute("SELECT exported FROM files WHERE sha=?", (sha,)).fetchone()[0]
            if ready:
                return
            time.sleep(.005)
        self.fail("Export did not finish")

    def test_transfer_is_not_import_progress_and_full_file_waits_for_atomic_settlement(self):
        session, sha = self.transfer(contents([row(), row(BLE, "BLE")]))
        self.assertEqual(self.store.status(session, sha)["import_percent"], 0)
        self.store.commit(session, sha)
        self.wait_exported_without_presentation(sha)
        before = self.store.status(session, sha)
        self.assertEqual((before["state"], before["import_percent"]), ("processing", 99))
        self.assertNotIn("receipt", before)
        self.assertEqual((before["new_wifi"], before["new_ble"], before["base_xp"]), (0, 0, 0))
        self.assertEqual(self.xp, 100)
        self.assertEqual(self.store.pending_events(), [])
        self.assertFalse(self.store.finish(session)["finished"])
        self.store.close()
        self.store = PigletCollectionStore(self.loot)
        self.assertNotIn("receipt", self.store.status(session, sha))
        after = self.poll(session, sha)
        self.assertEqual((after["state"], after["import_percent"]), ("committed", 100))
        self.assertEqual((after["new_wifi"], after["new_ble"], after["base_xp"]), (1, 1, 25))
        self.assertEqual(self.xp, 125)
        self.assertTrue(self.store.finish(session)["finished"])

    def test_file_summary_counts_new_identities_exact_duplicates_and_existing_repeat_rules(self):
        data = contents([row(), row(), row(rssi="-60"), row(BLE, "BLE"), row(BLE, "BLE")])
        session, sha = self.transfer(data)
        self.store.commit(session, sha)
        state = self.poll(session, sha)
        self.assertEqual((state["records"], state["new_wifi"], state["new_ble"], state["duplicates"], state["base_xp"]),
                         (5, 1, 1, 2, 26))
        self.assertTrue(state["summary_complete"])
        self.assertEqual(self.xp, 126)
        self.assertEqual((len(self.wifi), len(self.ble)), (1, 1))
        self.store.close()
        self.store = PigletCollectionStore(self.loot)
        restored = self.store.status(session, sha)
        for field in ("new_wifi", "new_ble", "base_xp", "duplicates", "import_percent"):
            self.assertEqual(restored[field], state[field])

    def test_source_export_advances_progress_without_awarding_individual_rows(self):
        rows = [row(mac=f"02:00:00:00:00:{n:02X}") for n in (1, 2, 3)]
        session, sha = self.transfer(contents(rows))
        self.store.commit(session, sha)
        self.wait_exported_without_presentation(sha)
        self.assertEqual(self.store.status(session, sha)["import_percent"], 99)
        self.assertEqual(self.loot.load_xp(), 100)
        self.assertEqual(self.store.pending_events(), [])
        self.assertEqual(self.poll(session, sha)["import_percent"], 100)
        self.assertEqual(self.xp, 145)

    def test_negotiated_source_paths_count_aliases_but_import_shared_digest_once(self):
        reply = self.store.begin("piglet-123456789abc", 2, source_items=1)
        self.assertEqual(reply["source_items"], 1)
        session = reply["session_id"]
        data = contents([row()])
        source_a, source_b = digest(b"/logs/same.csv"), digest(b"/uploaded/same.csv")
        session, sha = self.transfer(data, session, name="same.csv", source_id=source_a)
        self.assertEqual(self.overview()["import"]["percent"], 0)
        self.store.commit(session, sha)
        self.poll(session, sha)
        first = self.overview()["import"]
        self.assertEqual((first["percent"], first["expected_files"], first["completed_files"]), (50, 2, 1))
        self.assertFalse(self.store.finish(session)["finished"])
        # Even a cached-ACK alias must be offered, without retransmitting bytes.
        alias = self.store.offer(session, "same.csv", len(data), sha, source_id=source_b)
        self.assertEqual(alias["state"], "committed")
        self.assertEqual(self.xp, 115)
        summary = self.overview()["import"]
        self.assertEqual((summary["percent"], summary["new_wifi"], summary["base_xp"], summary["duplicates"]),
                         (100, 1, 15, 1))
        self.assertEqual(summary["offered_files"], 2)
        self.assertTrue(self.store.finish(session)["finished"])

    def test_source_identity_is_negotiated_bounded_and_immutable(self):
        session = self.session(source_items=1)
        data, source_id = contents([row()]), digest(b"/logs/one.csv")
        for bad in (None, "x", "A" * 64):
            with self.assertRaises(CollectionError):
                self.store.offer(session, "one.csv", len(data), digest(data), source_id=bad)
        self.transfer(data, session, name="one.csv", source_id=source_id)
        for name, sha in (("other.csv", digest(data)), ("one.csv", "a" * 64)):
            with self.assertRaises(CollectionError) as error:
                self.store.offer(session, name, len(data), sha, source_id=source_id)
            self.assertEqual(error.exception.status, 409)
        with self.assertRaises(CollectionError):
            self.store.offer(session, "one.csv", len(data), digest(data), source_id=digest(b"/logs/extra.csv"))
        with self.assertRaises(CollectionError):
            self.store.offer(self.session(), "one.csv", len(data), digest(data), source_id=source_id)

    def test_job_resume_and_completed_file_reoffer_keep_rewards_with_original_job(self):
        first = self.session(2, source_items=1)
        first, sha_a = self.transfer(contents([row()]), first, source_id=digest(b"/logs/a.csv"))
        self.store.commit(first, sha_a)
        self.poll(first, sha_a)
        job_id = self.overview()["import"]["job_id"]
        self.store.close()
        self.store = PigletCollectionStore(self.loot)
        retry = self.session(1, source_items=1)
        self.assertEqual(self.overview()["import"]["job_id"], job_id)
        retry, sha_b = self.transfer(contents([row(BLE, "BLE")]), retry, source_id=digest(b"/logs/b.csv"))
        self.store.commit(retry, sha_b)
        self.poll(retry, sha_b)
        summary = self.overview()["import"]
        self.assertEqual((summary["percent"], summary["expected_files"], summary["new_wifi"], summary["new_ble"], summary["base_xp"]),
                         (100, 2, 1, 1, 25))
        self.assertEqual(set(self.store.job_file_shas(job_id)), {sha_a, sha_b})
        self.assertTrue(self.store.finish(retry)["finished"])
        replay = self.session(source_items=1)
        replay_job = self.overview()["import"]["job_id"]
        self.assertNotEqual(replay_job, job_id)
        data = contents([row()])
        self.store.offer(replay, "closed.csv", len(data), sha_a, source_id=digest(b"/logs/a.csv"))
        summary = self.overview()["import"]
        self.assertEqual((summary["percent"], summary["new_wifi"], summary["base_xp"], summary["duplicates"]), (100, 0, 0, 1))
        self.assertEqual(self.store.job_file_shas(replay_job), [])
        self.assertEqual(self.store.job_file_shas(replay_job, credited_only=False), [sha_a])
        self.assertEqual(self.xp, 125)

    def test_concurrent_devices_share_one_job_percentage_and_finish_independently(self):
        first = self.session(source_items=1)
        second = self.session(device="piglet-abcdef123456", source_items=1)
        first, sha_a = self.transfer(contents([row()]), first, source_id=digest(b"/logs/a.csv"))
        self.store.commit(first, sha_a)
        self.poll(first, sha_a)
        summary = self.overview()["import"]
        self.assertEqual((summary["percent"], summary["expected_files"]), (50, 2))
        self.assertFalse(self.store.finish(first)["shutdown_allowed"])
        second, sha_b = self.transfer(contents([row(BLE, "BLE")]), second, source_id=digest(b"/logs/b.csv"))
        self.store.commit(second, sha_b)
        self.poll(second, sha_b)
        summary = self.overview()["import"]
        self.assertEqual((summary["percent"], summary["base_xp"], summary["new_wifi"], summary["new_ble"]), (100, 25, 1, 1))
        self.assertTrue(self.store.finish(second)["shutdown_allowed"])

    def test_ambiguous_legacy_alias_cannot_finish_but_negotiated_retry_resolves_it(self):
        data, legacy = contents([row()]), self.session(2)
        legacy, sha = self.transfer(data, legacy, name="same.csv")
        self.store.commit(legacy, sha)
        self.poll(legacy, sha)
        self.store.offer(legacy, "same.csv", len(data), sha)
        self.assertEqual(self.overview()["import"]["percent"], 50)
        self.assertFalse(self.store.finish(legacy)["finished"])
        retry = self.session(2, source_items=1)
        for path in (b"/logs/same.csv", b"/uploaded/same.csv"):
            self.store.offer(retry, "same.csv", len(data), sha, source_id=digest(path))
        summary = self.overview()["import"]
        self.assertEqual((summary["percent"], summary["expected_files"], summary["new_wifi"], summary["base_xp"]), (100, 2, 1, 15))
        self.assertTrue(self.store.finish(retry)["finished"])
        self.assertEqual(self.xp, 115)

    def test_stale_announced_peer_with_no_offer_prevents_job_shutdown_until_finished(self):
        first = self.session(source_items=1)
        second = self.session(device="piglet-abcdef123456", source_items=1)
        first, sha_a = self.transfer(contents([row()]), first, source_id=digest(b"/logs/a.csv"))
        self.store.commit(first, sha_a)
        self.poll(first, sha_a)
        with self.store._lock, self.store._sql:
            self.store._sql.execute("UPDATE sessions SET touched=? WHERE id=?", (time.time()-120, second))
        state = self.store.finish(first)
        self.assertTrue(state["finished"])
        self.assertFalse(state["all_done"])
        self.assertFalse(state["shutdown_allowed"])
        self.assertEqual(self.overview()["import"]["percent"], 50)
        second, sha_b = self.transfer(contents([row(BLE, "BLE")]), second, source_id=digest(b"/logs/b.csv"))
        self.store.commit(second, sha_b)
        self.poll(second, sha_b)
        self.assertEqual(self.overview()["import"]["percent"], 100)
        self.assertFalse(self.store.finish(first)["shutdown_allowed"], "The peer still owes its finish declaration")
        state = self.store.finish(second)
        self.assertTrue(state["finished"])
        self.assertTrue(state["all_done"])
        self.assertTrue(state["shutdown_allowed"])

    def test_reconnect_cannot_shrink_unoffered_source_obligations_into_false_completion(self):
        first = self.session(2, source_items=1)
        first, sha_a = self.transfer(contents([row()]), first, source_id=digest(b"/logs/a.csv"))
        self.store.commit(first, sha_a)
        self.poll(first, sha_a)
        before = self.overview()["import"]
        self.assertEqual(before["percent"], 50)
        with self.assertRaises(CollectionError) as error:
            self.session(0, source_items=1)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.overview()["import"], before)
        self.assertFalse(self.store.finish(first)["finished"])
        # A truthful reconnect announcing its one remaining item is accepted.
        retry = self.session(1, source_items=1)
        retry, sha_b = self.transfer(contents([row(BLE, "BLE")]), retry, source_id=digest(b"/logs/b.csv"))
        self.store.commit(retry, sha_b)
        self.poll(retry, sha_b)
        self.assertEqual(self.overview()["import"]["percent"], 100)
        self.assertTrue(self.store.finish(retry)["shutdown_allowed"])
        self.assertEqual(self.xp, 125)

    def test_known_only_reoffer_cannot_erase_earlier_unidentified_source_after_restart(self):
        data = contents([row()])
        first = self.session(2, source_items=1)
        source_a = digest(b"/logs/a.csv")
        first, sha_a = self.transfer(data, first, source_id=source_a)
        self.store.commit(first, sha_a)
        self.poll(first, sha_a)
        # The sender announces one item, but mistakenly reoffers the old one.
        retry = self.session(1, source_items=1)
        self.store.offer(retry, "closed.csv", len(data), sha_a, source_id=source_a)
        self.assertEqual(self.overview()["import"]["percent"], 50)
        self.assertFalse(self.store.finish(retry)["finished"])
        self.store.close()
        self.store = PigletCollectionStore(self.loot)
        self.assertEqual(self.overview()["import"]["expected_files"], 2)
        self.assertEqual(self.overview()["import"]["percent"], 50)
        remaining = self.session(1, source_items=1)
        remaining, sha_b = self.transfer(contents([row(BLE, "BLE")]), remaining, source_id=digest(b"/uploaded/b.csv"))
        self.store.commit(remaining, sha_b)
        self.poll(remaining, sha_b)
        self.assertEqual(self.overview()["import"]["percent"], 100)
        self.assertTrue(self.store.finish(remaining)["shutdown_allowed"])
        self.assertEqual(self.xp, 125)

    def test_device_finish_checks_older_sources_not_only_latest_session_offers(self):
        first = self.session(2, source_items=1)
        source_a, source_b = digest(b"/logs/a.csv"), digest(b"/logs/b.csv")
        first, sha_a = self.transfer(contents([row()]), first, source_id=source_a)
        empty = contents([])
        first, sha_b = self.transfer(empty, first, source_id=source_b)
        self.store.commit(first, sha_b)
        self.poll(first, sha_b)
        # The earlier source A was received but never validated/imported.
        retry = self.session(1, source_items=1)
        self.store.offer(retry, "closed.csv", len(empty), sha_b, source_id=source_b)
        self.assertFalse(self.store.finish(retry)["finished"])
        self.assertNotEqual(self.overview()["import"]["percent"], 100)
        self.store.commit(first, sha_a)
        self.poll(first, sha_a)
        self.assertTrue(self.store.finish(retry)["finished"])
        self.assertEqual(self.overview()["import"]["percent"], 100)
        self.assertEqual(self.xp, 115)

    def test_expected_floor_migration_preserves_maximum_historical_session_declaration(self):
        first = self.session(2, source_items=1)
        first, sha = self.transfer(contents([row()]), first, source_id=digest(b"/logs/a.csv"))
        self.store.commit(first, sha)
        self.poll(first, sha)
        self.session(1, source_items=1)
        # Emulate an older job-metadata generation without the durable floor.
        with self.store._lock, self.store._sql:
            self.store._sql.execute("UPDATE job_devices SET expected_floor=0")
        self.store.close()
        self.store = PigletCollectionStore(self.loot)
        summary = self.overview()["import"]
        self.assertEqual((summary["expected_files"], summary["percent"]), (2, 50))

    def test_wifi_ble_import_ack_history_raw_source_and_manual_upload_only(self):
        name = 'Synthetic, "name"\r\ncontinued'
        data = contents([row(name=name), row(BLE, "BLE", name)])
        session, sha = self.transfer(data)
        self.assertNotIn("receipt", self.store.commit(session, sha))
        state = self.poll(session, sha)
        self.assertEqual(state["state"], "committed")
        receipt = state["receipt"]
        self.assertEqual((receipt["records"], receipt["wifi"], receipt["ble"], receipt["duplicates"]), (2, 1, 1, 0))
        self.assertEqual(self.xp, 125)
        self.assertEqual(self.loot.load_xp(), self.xp)
        self.assertEqual(self.store._path(sha).read_bytes(), data)
        self.assertEqual(self.loot.get_known_devices(), ({BLE}, {WIFI}))
        record = self.loot.collection_file_receipt(sha)
        exported = self.loot._base / record["session"]
        self.assertEqual(self.loot._scan_session_dir(exported)["wardriving"], 2)
        self.assertEqual(self.loot._scan_session_dir(exported)["bt_devices"], 1)
        self.assertEqual([r["label"] for r in self.loot.get_gps_points()], [name, name, name])
        plugin = WardriveUpload.__new__(WardriveUpload)
        plugin._log_add = Mock()
        parsed = plugin._parse_csv(exported / "wardriving.csv")
        self.assertEqual({r["ssid"] for r in parsed}, {name})
        self.assertEqual({r["lat"] for r in parsed}, {12.5})
        self.assertEqual(WardriveUpload._retention_source_count("wardriving.csv", (exported / "wardriving.csv").read_bytes()), 2)
        self.assertFalse((exported / ".uploaded").exists())
        self.assertEqual(self.delivered_events, [])
        self.assertEqual(self.store.pending_events(), [])
        self.assertTrue(self.store.finish(session)["shutdown_allowed"])

    def test_exact_records_across_files_and_file_reoffer_have_zero_replay_reward(self):
        data = contents([row(), row(), row(BLE, "BLE")])
        session, sha = self.transfer(data)
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha)["receipt"]["duplicates"], 1)
        self.assertEqual(self.xp, 125)
        session2, sha2 = self.transfer(contents([row(), row(BLE, "BLE")], ",different=1"))
        self.store.commit(session2, sha2)
        state = self.poll(session2, sha2)
        self.assertEqual((state["wifi"], state["ble"], state["duplicates"]), (0, 0, 2))
        # Whole source files remain available for manual upload even when all
        # their exact records have already been imported.
        exported = self.loot._base / self.loot.collection_file_receipt(sha2)["session"]
        self.assertEqual(self.loot._scan_session_dir(exported)["wardriving"], 2)
        self.assertEqual(self.xp, 125)
        again = self.store.offer(self.session(), "renamed.csv", len(data), sha)
        self.assertEqual(again["state"], "committed")
        self.assertEqual(again["receipt"]["import_id"], self.store.status(session, sha)["receipt"]["import_id"])

    def test_distinct_observations_known_identity_repeat_one_and_protocol_separate(self):
        session, sha = self.transfer(contents([row(), row(rssi="-60"), row(WIFI, "BLE")]))
        self.store.commit(session, sha)
        self.poll(session, sha)
        self.assertEqual(self.xp, 126)
        self.assertEqual(self.delivered_events, [])
        state = self.store.status(session, sha)
        self.assertEqual((state["new_wifi"], state["new_ble"], state["base_xp"]), (1, 1, 26))

    def test_entire_csv_validates_before_any_reward_or_event(self):
        invalid = row(BLE, "BLE")
        invalid[7] = "nan"
        session, sha = self.transfer(contents([row(), invalid]))
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha)["state"], "error")
        self.assertEqual(self.loot.load_xp(), 100)
        self.assertEqual(self.store.pending_events(), [])
        self.assertEqual((self.wifi, self.ble), (set(), set()))

    def test_whole_file_digest_mismatch_never_acknowledges(self):
        data = contents([row()])
        session, sha = self.transfer(data, sha="a" * 64)
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha)["state"], "error")
        self.assertEqual(self.xp, 100)

    def test_chunk_retry_offset_conflict_and_durable_restart_prefix(self):
        data = contents([row()])
        sha, session = digest(data), self.session()
        self.store.offer(session, "resume.csv", len(data), sha)
        first = data[:100]
        self.assertEqual(self.store.chunk(session, sha, 0, first, digest(first))["offset"], 100)
        self.assertEqual(self.store.chunk(session, sha, 0, first, digest(first))["offset"], 100)
        for offset, chunk in ((0, b"bad"), (101, b"next")):
            with self.assertRaises(CollectionError) as caught:
                self.store.chunk(session, sha, offset, chunk, digest(chunk))
            self.assertEqual((caught.exception.status, caught.exception.offset), (409, 100))
        self.store.close()
        with self.store._path(sha).open("ab") as target:
            target.write(b"uncommitted tail")
        self.store = PigletCollectionStore(self.loot)
        session = self.session()
        self.assertEqual(self.store.offer(session, "resume.csv", len(data), sha)["offset"], 100)
        self.assertEqual(self.store._path(sha).stat().st_size, 100)
        self.store.chunk(session, sha, 100, data[100:], digest(data[100:]))
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha)["state"], "committed")

    def test_invalid_offers_and_chunks_never_create_source_paths(self):
        session = self.session()
        for name, size in (("../bad.csv", 1), ("C:\\bad.csv", 1), ("bad.txt", 1), ("ok.csv", MAX_SIZE + 1)):
            with self.assertRaises(CollectionError):
                self.store.offer(session, name, size, "a" * 64)
        data = contents([row()])
        sha = digest(data)
        self.store.offer(session, "ok.csv", len(data), sha)
        with self.assertRaises(CollectionError):
            self.store.chunk(session, sha, 0, data, "b" * 64)
        with self.assertRaises(CollectionError):
            self.store.commit(session, sha)
        self.assertEqual(self.store.status(session, sha)["offset"], 0)

    def test_initial_file_fsync_failure_can_retry_own_empty_orphan(self):
        data, session = contents([row()]), self.session()
        sha = digest(data)
        with patch("watchdogs.piglet_collection.os.fsync", side_effect=OSError("synthetic fsync failure")), self.assertRaises(OSError):
            self.store.offer(session, "closed.csv", len(data), sha)
        self.assertEqual(self.store._path(sha).stat().st_size, 0)
        self.assertEqual(self.store.offer(session, "closed.csv", len(data), sha)["offset"], 0)
        self.store.chunk(session, sha, 0, data, digest(data))
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha)["state"], "committed")
        self.assertEqual(self.xp, 115)

    def test_initial_index_failure_can_retry_own_empty_orphan(self):
        data, session = contents([row()]), self.session()
        sha = digest(data)
        with self.store._lock, self.store._sql:
            self.store._sql.execute("CREATE TRIGGER synthetic_file_failure BEFORE INSERT ON files "
                                    "BEGIN SELECT RAISE(ABORT,'synthetic index failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.offer(session, "closed.csv", len(data), sha)
        with self.store._lock, self.store._sql:
            self.store._sql.execute("DROP TRIGGER synthetic_file_failure")
        self.assertEqual(self.store._path(sha).stat().st_size, 0)
        self.assertEqual(self.store.offer(session, "closed.csv", len(data), sha)["offset"], 0)

    def test_nonempty_linked_or_other_owner_orphans_are_kept_without_adoption(self):
        sha, session = "a" * 64, self.session()
        path = self.store._path(sha)
        path.write_bytes(b"unindexed bytes")
        with self.assertRaises(CollectionError):
            self.store.offer(session, "closed.csv", 100, sha)
        self.assertEqual(path.read_bytes(), b"unindexed bytes")
        empty_sha = "b" * 64
        empty = self.store._path(empty_sha)
        empty.touch()
        with patch.object(os, "geteuid", return_value=999999, create=True), self.assertRaises(CollectionError):
            self.store.offer(session, "closed.csv", 100, empty_sha)
        linked_sha = "c" * 64
        linked = self.store._path(linked_sha)
        os.link(empty, linked)
        with self.assertRaises(CollectionError):
            self.store.offer(session, "closed.csv", 100, linked_sha)
        self.assertEqual(empty.stat().st_nlink, 2)

    def test_oversized_preamble_header_and_data_line_never_import_or_ack(self):
        samples = [b"WigleWifi-1.6," + b"x" * (MAX_PHYSICAL_LINE + 1),
                   b"WigleWifi-1.6,fixture\nMAC," + b"x" * (MAX_PHYSICAL_LINE + 1),
                   contents([row(name="x" * (MAX_PHYSICAL_LINE + 1))])]
        for data in samples:
            with self.subTest(size=len(data)):
                session, sha = self.transfer(data)
                self.store.commit(session, sha)
                state = self.poll(session, sha)
                self.assertEqual(state["state"], "error")
                self.assertNotIn("receipt", state)
        self.assertEqual(self.xp, 100)
        self.assertEqual(self.store.pending_events(), [])

    def test_symlink_orphan_is_never_adopted_or_followed(self):
        target = self.root / "kept.csv"
        target.write_bytes(b"")
        sha, session = "d" * 64, self.session()
        path = self.store._path(sha)
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("Creating symlinks requires platform privileges")
        with self.assertRaises(CollectionError):
            self.store.offer(session, "closed.csv", 100, sha)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), b"")

    def test_failed_loot_transaction_keeps_xp_knowns_outbox_and_retries(self):
        session, sha = self.transfer(contents([row()]))
        self.store.commit(session, sha)
        deadline = time.monotonic() + 5
        while not self.store._sql.execute("SELECT prepared FROM files WHERE sha=?", (sha,)).fetchone()[0]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)
        before = copy.deepcopy(self.loot._db)
        with patch.object(self.loot, "_save_db", return_value=False), self.assertRaises(OSError):
            while time.monotonic() < deadline:
                # The deliberate nonblocking UI API can legitimately yield
                # while the validation worker is releasing its short lock.
                self.store.process_pending(self.xp, self.wifi, self.ble)
                time.sleep(.005)
            self.fail("Failed DB transaction was not reached")
        self.assertEqual(self.loot._db, before)
        self.assertEqual((self.wifi, self.ble), (set(), set()))
        self.assertNotIn("receipt", self.store.status(session, sha))
        self.assertEqual(self.poll(session, sha)["state"], "committed")
        self.assertEqual(self.xp, 115)

    def test_crash_after_db_reward_before_sql_recovery_does_not_award_again(self):
        session, sha = self.transfer(contents([row(), row(BLE, "BLE")]))
        self.store.commit(session, sha)
        deadline = time.monotonic() + 5
        result = {"committed": False}
        with patch.object(self.store, "_recover_file_batch", side_effect=OSError("simulated metadata interruption")):
            while not result["committed"]:
                self.assertLess(time.monotonic(), deadline)
                result = self.store.process_pending(self.xp, self.wifi, self.ble)
                time.sleep(.005)
        self.xp = result["xp_total"]
        self.assertEqual(self.xp, 125)
        event_ids = [event["event_id"] for event in self.store.pending_events()]
        self.assertTrue(self.loot.collection_pending_batches())
        self.store.close()
        self.store = PigletCollectionStore(self.loot)
        self.assertFalse(self.loot.collection_pending_batches())
        self.assertEqual([event["event_id"] for event in self.store.pending_events()], event_ids)
        self.assertNotIn("receipt", self.store.status(session, sha))
        self.assertEqual(self.poll(session, sha)["state"], "committed")
        self.assertEqual(self.xp, 125)
        state = self.store.status(session, sha)
        self.assertEqual((state["new_wifi"], state["new_ble"], state["base_xp"]), (1, 1, 25))
        self.assertTrue(self.store.acknowledge_events(event_ids))
        self.assertEqual(self.store.pending_events(), [])

    def test_export_failure_keeps_source_unacknowledged_and_retry_does_not_rescore(self):
        data = contents([row()])
        session, sha = self.transfer(data)
        with patch.object(self.store, "_export", side_effect=OSError("simulated storage failure")):
            self.store.commit(session, sha)
            state = self.poll(session, sha)
        self.assertEqual(state["state"], "error")
        self.assertNotIn("receipt", state)
        self.assertEqual(self.xp, 100)
        self.assertEqual(self.store._path(sha).read_bytes(), data)
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha)["state"], "committed")
        self.assertEqual(self.xp, 115)

    def test_completion_failure_never_acknowledges_until_durable_db_save(self):
        session, sha = self.transfer(contents([row()]))
        self.store.commit(session, sha)
        deadline = time.monotonic() + 5
        with patch.object(self.loot, "complete_collection_file", return_value=False):
            with self.assertRaises(OSError):
                while time.monotonic() < deadline:
                    result = self.store.process_pending(self.xp, self.wifi, self.ble)
                    if result["committed"]:
                        self.xp = result["xp_total"]
                    self.store.acknowledge_events([e["event_id"] for e in self.store.pending_events(20)])
                    time.sleep(.005)
                self.fail("Completion failure was not reached")
        self.assertNotIn("receipt", self.store.status(session, sha))
        self.assertEqual(self.xp, 115)
        self.assertEqual(self.poll(session, sha)["state"], "committed")
        self.assertEqual(self.xp, 115)

    def test_process_and_game_restart_preserve_receipt_outbox_and_player_data(self):
        self.loot._db.update(badges=["kept"], mc_contacts={"12345678": {"note": "kept"}})
        session, sha = self.transfer(contents([row()]))
        self.store.commit(session, sha)
        receipt = self.poll(session, sha)["receipt"]
        events = self.store.pending_events()
        collector = self.store.collector_id
        self.store.close()
        self.close_loot()
        with patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
            self.loot = LootManager(str(self.root))
        self.store = PigletCollectionStore(self.loot)
        self.assertEqual(self.loot.load_xp(), 115)
        self.assertEqual(self.loot._db["badges"], ["kept"])
        self.assertEqual(self.loot._db["mc_contacts"]["12345678"]["note"], "kept")
        self.assertEqual(self.store.collector_id, collector)
        self.assertEqual(self.store.pending_events(), events)
        self.assertEqual(self.store.status(session, sha)["receipt"], receipt)
        self.assertEqual(self.loot.get_known_devices(), (set(), {WIFI}))

    def test_committing_import_uses_current_unsaved_live_xp_and_noop_preserves_it(self):
        self.xp = 999
        no_op = self.store.process_pending(self.xp, self.wifi, self.ble)
        self.assertEqual(no_op, {"committed": False, "xp_total": 999})
        self.assertEqual(self.loot.load_xp(), 100)
        session, sha = self.transfer(contents([row()]))
        self.store.commit(session, sha)
        self.poll(session, sha)
        self.assertEqual(self.xp, 1014)

    def test_persisted_known_identity_is_not_new_with_stale_ui_known_set(self):
        self.loot._db["known_device_identities"] = {"wifi": [WIFI], "ble": []}
        self.assertTrue(self.loot._save_db(self.loot._db))
        session, sha = self.transfer(contents([row()]))
        self.store.commit(session, sha)
        self.poll(session, sha)
        self.assertEqual(self.xp, 101)
        self.assertEqual(self.store.status(session, sha)["new_wifi"], 0)
        self.assertEqual(self.delivered_events, [])

    def test_missing_gps_records_still_import_without_using_live_host_position(self):
        data = contents([row(lat="0", lon="0"), row(BLE, "BLE", lat="", lon="")])
        session, sha = self.transfer(data)
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha)["state"], "committed")
        self.assertEqual(self.xp, 125)
        self.assertTrue(all(e["record"]["position"] is None for e in self.delivered_events))
        self.assertEqual(self.loot.get_gps_points(), [])
        exported = self.loot._base / self.loot.collection_file_receipt(sha)["session"]
        plugin = WardriveUpload.__new__(WardriveUpload)
        plugin._log_add = Mock()
        self.assertEqual(plugin._parse_csv(exported / "wardriving.csv"), [])

    def test_whole_file_settlement_has_no_per_observation_outbox(self):
        rows = [row(mac=f"02:01:00:{n >> 16:02X}:{n >> 8 & 255:02X}:{n & 255:02X}") for n in range(300)]
        session, sha = self.transfer(contents(rows))
        self.store.commit(session, sha)
        self.assertEqual(self.poll(session, sha, events=False)["state"], "committed")
        self.assertEqual(self.store.pending_events(256), [])
        self.assertEqual(self.xp, 100 + 300 * 15)

    def test_second_active_device_blocks_finish_shutdown_until_done(self):
        first, second = self.session(0), self.session(0, "piglet-abcdef123456")
        self.assertFalse(self.store.finish(first)["shutdown_allowed"])
        self.assertTrue(self.store.finish(second)["shutdown_allowed"])


if __name__ == "__main__":
    unittest.main()
