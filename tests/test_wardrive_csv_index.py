"""CSV indexing with real files, external edits and durable-write failures."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdogs import loot_manager as module
from watchdogs.loot_manager import LootManager


FIRST = "02:00:00:00:00:01"
SECOND = "02:00:00:00:00:02"


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2020, 1, 2, 3, 4, 5, tzinfo=tz)


class WardriveCsvIndexTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        clock = patch.object(module, "datetime", FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)
        self.loot = self.make_manager()
        self.path = Path(self.loot.session_dir) / "wardriving.csv"

    def make_manager(self):
        with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
            loot = LootManager(self.root)
        def close():
            with patch.object(module.os, "sync", lambda: None, create=True):
                loot.close()
        self.addCleanup(close)
        return loot

    def save(self, kind="wifi", address=FIRST, rssi=-70, name="synthetic", loot=None):
        loot = self.loot if loot is None else loot
        if kind == "wifi":
            return loot.save_wardriving_network(SimpleNamespace(
                bssid=address, ssid=name, auth="WPA2_PSK", channel="6", rssi=str(rssi)))
        return loot.save_wardriving_bt(address, rssi, name)

    def records(self):
        return [self.loot._wardrive_record(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()[2:]]

    def test_repeated_results_do_not_reopen_or_reparse_the_unchanged_csv(self):
        self.assertTrue(self.save())
        self.assertTrue(self.save("ble", FIRST))
        old = self.path.read_bytes()
        with patch("builtins.open", side_effect=AssertionError("unexpected CSV reread")), \
                patch.object(self.loot, "_wardrive_record", side_effect=AssertionError("unexpected parse")), \
                patch.object(self.loot, "_commit_wardrive_csv", side_effect=AssertionError("duplicate write")):
            for _ in range(100):
                self.assertFalse(self.save(rssi=-80))
                self.assertFalse(self.save("ble", FIRST, -80))
        self.assertEqual(self.path.read_bytes(), old)

    def test_accepted_records_and_stronger_updates_also_reuse_the_index(self):
        self.assertTrue(self.save())
        with patch("builtins.open", side_effect=AssertionError("unexpected CSV reread")), \
                patch.object(self.loot, "_wardrive_record", side_effect=AssertionError("unexpected parse")):
            self.assertTrue(self.save(address=SECOND))
            self.assertTrue(self.save(rssi=-40))
            self.assertTrue(self.save("ble", FIRST, -30))
        self.assertEqual(self.records(), [("WIFI", FIRST, -40), ("WIFI", SECOND, -70),
                                          ("BLE", FIRST, -30)])

    def test_restart_rebuilds_once_and_keeps_protocol_specific_deduplication(self):
        self.assertTrue(self.save())
        self.assertTrue(self.save("ble", FIRST, -20))
        restarted = self.make_manager()
        with patch.object(restarted, "_wardrive_record", wraps=restarted._wardrive_record) as parse:
            self.assertFalse(self.save(rssi=-80, loot=restarted))
            self.assertFalse(self.save("ble", FIRST, -30, loot=restarted))
            self.assertFalse(self.save(rssi=-80, loot=restarted))
            self.assertEqual(parse.call_count, 2)
        self.assertTrue(self.save(rssi=-40, loot=restarted))
        self.assertEqual(self.records(), [("WIFI", FIRST, -40), ("BLE", FIRST, -20)])

    def test_existing_crlf_malformed_lines_and_native_comma_names_keep_exact_bytes(self):
        header = (self.loot._WIGLE_PRE_HEADER + self.loot._WIGLE_HEADER).replace("\n", "\r\n")
        untouched = "legacy malformed row\r\n" + (
            f'{FIRST},"BLE, Kitchen,[BLE],2020-01-01 00:00:00,,-30,0,0,0,0,BLE\r\n')
        self.path.write_bytes((header + untouched).encode())
        self.assertTrue(self.save(name='"WiFi, Kitchen'))
        expected = (header + untouched +
                    f'{FIRST},"WiFi, Kitchen,[WPA2_PSK][ESS],2020-01-02 03:04:05,6,-70,0.0,0.0,0.0,0.0,WIFI\n')
        self.assertEqual(self.path.read_bytes(), expected.encode())
        self.assertFalse(self.save("ble", FIRST, -40))

    def test_external_in_place_edit_invalidates_index(self):
        self.assertTrue(self.save())
        self.path.write_bytes(self.path.read_bytes().replace(b",-70,", b",-20,"))
        # Ensure metadata changes even on filesystems with coarse timestamps.
        info = self.path.stat()
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        self.assertFalse(self.save(rssi=-40))
        self.assertEqual(self.records(), [("WIFI", FIRST, -20)])

    def test_external_replacement_is_detected_even_with_same_length_and_mtime(self):
        self.assertTrue(self.save())
        old = self.path.stat()
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(self.path.read_bytes().replace(b",-70,", b",-20,"))
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(self.path)
        self.assertFalse(self.save(rssi=-40))
        self.assertEqual(self.records(), [("WIFI", FIRST, -20)])

    def test_external_append_is_preserved_and_indexed(self):
        self.assertTrue(self.save())
        external = f"{SECOND},external,[BLE],2020-01-01 00:00:00,,-20,0,0,0,0,BLE\n"
        with self.path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(external)
        self.assertFalse(self.save("ble", SECOND, -40))
        self.assertTrue(self.save(rssi=-60))
        self.assertTrue(self.path.read_bytes().endswith(external.encode()))

    def test_deleted_csv_starts_empty_instead_of_resurrecting_cached_rows(self):
        self.assertTrue(self.save())
        self.assertTrue(self.save("ble", SECOND))
        self.path.unlink()
        self.assertTrue(self.save(rssi=-80))
        self.assertEqual(self.records(), [("WIFI", FIRST, -80)])

    def test_session_path_change_does_not_reuse_previous_rows(self):
        self.assertTrue(self.save())
        old = self.path.read_bytes()
        self.loot._session = Path(self.root) / "another-session"
        self.loot._session.mkdir()
        self.assertTrue(self.save("ble", SECOND))
        self.assertEqual(self.path.read_bytes(), old)
        target = self.loot._session / "wardriving.csv"
        self.assertNotIn(FIRST.encode(), target.read_bytes())

    def test_failed_commits_do_not_advance_cached_rssi_or_add_unsaved_identities(self):
        for kind in ("wifi", "ble"):
            for operation in ("fsync", "replace"):
                with self.subTest(kind=kind, operation=operation):
                    self.path.unlink(missing_ok=True)
                    self.assertTrue(self.save(kind))
                    original = self.path.read_bytes()
                    target, method = (module.os, "fsync") if operation == "fsync" else (Path, "replace")
                    with patch.object(target, method, side_effect=OSError("synthetic storage failure")), \
                            self.assertLogs(module.log, level="ERROR"):
                        self.assertFalse(self.save(kind, FIRST, -20))
                        self.assertFalse(self.save(kind, SECOND, -30))
                    self.assertEqual(self.path.read_bytes(), original)
                    self.assertTrue(self.save(kind, FIRST, -40))
                    self.assertTrue(self.save(kind, SECOND, -30))
                    self.assertFalse(list(self.path.parent.glob(".wardriving.csv.*.tmp")))

    def test_stat_permission_error_does_not_allow_cached_data_to_overwrite_disk(self):
        self.assertTrue(self.save())
        original = self.path.read_bytes()
        with patch.object(self.loot, "_wardrive_csv_signature", side_effect=PermissionError("denied")), \
                self.assertLogs(module.log, level="ERROR"):
            self.assertFalse(self.save(address=SECOND))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.save(address=SECOND))

    def test_invalid_utf8_external_edit_never_falls_back_to_stale_cache(self):
        self.assertTrue(self.save())
        original = self.path.read_bytes()
        self.path.write_bytes(b"\xffinvalid CSV\n")
        with self.assertLogs(module.log, level="ERROR"):
            self.assertFalse(self.save(address=SECOND))
        self.assertEqual(self.path.read_bytes(), b"\xffinvalid CSV\n")
        self.path.write_bytes(original)
        self.assertTrue(self.save(address=SECOND))
        self.assertEqual(len(self.records()), 2)

    def test_file_changed_during_reload_is_not_overwritten_with_partial_snapshot(self):
        self.assertTrue(self.save())
        self.loot._wardrive_csv_cache = None
        parse = self.loot._wardrive_record
        changed = False
        external = f"{SECOND},external,[BLE],2020-01-01 00:00:00,,-20,0,0,0,0,BLE\n"
        def edit_during_parse(line):
            nonlocal changed
            if not changed:
                changed = True
                with self.path.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(external)
            return parse(line)
        with patch.object(self.loot, "_wardrive_record", side_effect=edit_during_parse), \
                self.assertLogs(module.log, level="ERROR"):
            self.assertFalse(self.save(rssi=-20))
        self.assertTrue(self.path.read_bytes().endswith(external.encode()))
        self.assertTrue(self.save(rssi=-20))
        self.assertEqual(self.records(), [("WIFI", FIRST, -20), ("BLE", SECOND, -20)])

    def test_concurrent_wifi_and_ble_saves_do_not_lose_each_others_records(self):
        addresses = [f"02:00:00:00:01:{number:02X}" for number in range(12)]
        work = [(kind, address, strength) for kind in ("wifi", "ble")
                for address in addresses for strength in (-80, -30)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda item: self.save(*item), work))
        records = self.records()
        self.assertEqual(len(records), 24)
        self.assertEqual({(kind, address) for kind, address, _ in records},
                         {(kind.upper(), address) for kind in ("wifi", "ble") for address in addresses})
        self.assertEqual({strength for _, _, strength in records}, {-30})

    def test_missing_metadata_after_success_does_not_report_failed_save(self):
        signature = self.loot._wardrive_csv_signature
        calls = 0
        def unavailable_after_commit(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("synthetic post-commit stat failure")
            return signature(path)
        with patch.object(self.loot, "_wardrive_csv_signature", side_effect=unavailable_after_commit):
            self.assertTrue(self.save())
        self.assertEqual(self.loot.wardrive_save_errors, 0)
        self.assertFalse(self.save(rssi=-80))

    def test_external_deletion_immediately_after_commit_does_not_cache_missing_file(self):
        commit = self.loot._commit_wardrive_csv
        def delete_after_commit(path, rows):
            result = commit(path, rows)
            path.unlink()
            return result
        with patch.object(self.loot, "_commit_wardrive_csv", side_effect=delete_after_commit):
            self.assertTrue(self.save())
        self.assertTrue(self.save("ble", SECOND))
        self.assertEqual(self.records(), [("BLE", SECOND, -70)])

    def test_external_replacement_immediately_after_commit_is_not_mistaken_for_our_rows(self):
        self.assertTrue(self.save())
        original = self.path.read_bytes()
        commit = self.loot._commit_wardrive_csv
        def replace_after_commit(path, rows):
            result = commit(path, rows)
            external = path.with_suffix(".external")
            external.write_bytes(original.replace(FIRST.encode(), SECOND.encode()))
            external.replace(path)
            return result
        with patch.object(self.loot, "_commit_wardrive_csv", side_effect=replace_after_commit):
            self.assertTrue(self.save(rssi=-20))
        self.assertTrue(self.save(rssi=-80))
        self.assertEqual(self.records(), [("WIFI", SECOND, -70), ("WIFI", FIRST, -80)])


if __name__ == "__main__":
    unittest.main()
