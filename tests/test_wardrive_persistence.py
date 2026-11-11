"""Real temporary CSVs, synthetic discoveries and injected storage failures."""

import csv
from contextlib import ExitStack
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


class FailingStream:
    """Keep a real file/descriptor, failing after a partial write or at flush."""
    def __init__(self, stream, operation):
        self.stream, self.operation = stream, operation

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def writelines(self, lines):
        if self.operation == "write":
            self.stream.write("partial bytes")
            raise OSError("synthetic write failure")
        return self.stream.writelines(lines)

    def flush(self):
        if self.operation == "flush":
            raise OSError("synthetic flush failure")
        return self.stream.flush()


class WardrivePersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gps = SimpleNamespace(available=True, fix=SimpleNamespace(
            valid=True, latitude=12.5, longitude=34.5, altitude=20.0, hdop=1.0))
        with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
            self.loot = LootManager(directory.name, gps_manager=self.gps)
        def close():
            with patch.object(os, "sync", lambda: None, create=True):
                self.loot.close()
        self.addCleanup(close)

    def save(self, kind, mac=FIRST, rssi=-70):
        if kind == "wifi":
            network = SimpleNamespace(bssid=mac, ssid="synthetic", auth="WPA2_PSK",
                                      channel="6", rssi=str(rssi))
            return self.loot.save_wardriving_network(network)
        if kind == "ble":
            return self.loot.save_wardriving_bt(mac, rssi, "synthetic BLE")
        return self.loot.save_bt_device(mac, rssi, "synthetic BLE", False, False)

    def path(self, kind):
        name = "bt_devices.csv" if kind == "devices" else "wardriving.csv"
        return Path(self.loot.session_dir) / name

    def rows(self, kind):
        with self.path(kind).open(encoding="utf-8", newline="") as stream:
            if kind != "devices":
                next(stream)  # WiGLE pre-header is preserved.
            return list(csv.DictReader(stream))

    def reset_csvs(self):
        for name in ("wardriving.csv", "bt_devices.csv"):
            (Path(self.loot.session_dir) / name).unlink(missing_ok=True)

    def failing_commit(self, kind, operation, mac=SECOND, rssi=-60):
        with ExitStack() as stack:
            if operation in ("write", "flush"):
                fdopen = module.os.fdopen
                stack.enter_context(patch.object(module.os, "fdopen", side_effect=
                    lambda *a, **k: FailingStream(fdopen(*a, **k), operation)))
            elif operation == "fsync":
                stack.enter_context(patch.object(module.os, "fsync",
                    side_effect=OSError("synthetic fsync failure")))
            else:
                stack.enter_context(patch.object(Path, "replace",
                    side_effect=OSError("synthetic replace failure")))
            with self.assertLogs(module.log, level="ERROR"):
                return self.save(kind, mac, rssi)

    def test_counter_starts_at_zero_and_normal_wifi_ble_formats_are_unchanged(self):
        self.assertEqual(self.loot.wardrive_save_errors, 0)
        self.assertTrue(self.save("wifi"))
        self.assertTrue(self.save("ble", SECOND))
        self.assertTrue(self.save("devices", SECOND))
        rows = self.rows("wifi")
        self.assertEqual([r["Type"] for r in rows], ["WIFI", "BLE"])
        self.assertEqual([r["MAC"] for r in rows], [FIRST, SECOND])
        self.assertEqual(rows[0]["CurrentLatitude"], "12.5")
        self.assertEqual(rows[1]["CurrentLongitude"], "34.5")
        self.assertEqual(self.rows("devices")[0]["mac"], SECOND)
        self.assertEqual(self.loot.wardrive_save_errors, 0)

    def test_duplicates_are_skips_not_errors_and_do_not_write_again(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind))
                original = self.path(kind).read_bytes()
                with patch.object(module.os, "fsync") as fsync:
                    self.assertIs(self.save(kind, rssi=-80), False)
                    fsync.assert_not_called()
                self.assertEqual(self.path(kind).read_bytes(), original)
        self.assertEqual(self.loot.wardrive_save_errors, 0)

    def test_missing_or_invalid_gps_uses_existing_zero_coordinate_semantics(self):
        for gps in (None, SimpleNamespace(available=False),
                    SimpleNamespace(available=True, fix=SimpleNamespace(valid=False))):
            self.loot._gps = gps
            for kind in ("wifi", "ble", "devices"):
                with self.subTest(gps=gps, kind=kind):
                    self.reset_csvs()
                    self.assertTrue(self.save(kind))
                    row = self.rows(kind)[0]
                    self.assertEqual(float(row["lat" if kind == "devices" else "CurrentLatitude"]), 0)
                    self.assertEqual(float(row["lon" if kind == "devices" else "CurrentLongitude"]), 0)
        self.assertEqual(self.loot.wardrive_save_errors, 0)

    def test_inactive_session_and_empty_ids_are_skips_not_errors(self):
        for kind in ("wifi", "ble", "devices"):
            self.assertIs(self.save(kind, mac=""), False)
        self.loot._session_active = False
        for kind in ("wifi", "ble", "devices"):
            self.assertIs(self.save(kind), False)
        self.assertEqual(self.loot.wardrive_save_errors, 0)

    def test_write_flush_fsync_and_replace_failures_keep_old_file_and_retry_recovers(self):
        for kind in ("wifi", "ble", "devices"):
            for operation in ("write", "flush", "fsync", "replace"):
                with self.subTest(kind=kind, operation=operation):
                    self.reset_csvs()
                    self.assertTrue(self.save(kind))
                    original = self.path(kind).read_bytes()
                    before = self.loot.wardrive_save_errors
                    self.assertIs(self.failing_commit(kind, operation), False)
                    self.assertEqual(self.loot.wardrive_save_errors, before + 1)
                    self.assertEqual(self.path(kind).read_bytes(), original)
                    self.assertFalse(list(self.path(kind).parent.glob(f".{self.path(kind).name}.*.tmp")))
                    self.assertTrue(self.save(kind, SECOND, -60))
                    self.assertEqual(len(self.rows(kind)), 2)
                    self.assertEqual(self.loot.wardrive_save_errors, before + 1)

    def test_failed_first_save_does_not_leave_dedup_record_and_retry_is_new(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                before = self.loot.wardrive_save_errors
                self.assertIs(self.failing_commit(kind, "fsync", FIRST), False)
                self.assertFalse(self.path(kind).exists())
                self.assertTrue(self.save(kind))
                self.assertEqual(len(self.rows(kind)), 1)
                self.assertEqual(self.loot.wardrive_save_errors, before + 1)

    def test_stronger_rssi_update_fsync_failure_keeps_previous_record(self):
        for kind in ("wifi", "ble"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind, rssi=-80))
                original = self.path(kind).read_bytes()
                self.assertIs(self.failing_commit(kind, "fsync", FIRST, -40), False)
                self.assertEqual(self.path(kind).read_bytes(), original)
                self.assertTrue(self.save(kind, FIRST, -40))
                self.assertEqual(len(self.rows(kind)), 1)
                self.assertEqual(self.rows(kind)[0]["RSSI"], "-40")

    def test_read_failure_preserves_existing_file_and_counts_once(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind))
                target = self.path(kind)
                original = target.read_bytes()
                # An external replacement requires a real reload even when
                # the previous successful save populated the in-memory index.
                replacement = target.with_suffix(".external")
                replacement.write_bytes(original)
                replacement.replace(target)
                before = self.loot.wardrive_save_errors
                real_open, real_read = open, Path.read_text
                def fail_open(file, mode="r", *a, **k):
                    if Path(file) == target and mode == "r":
                        raise OSError("synthetic read failure")
                    return real_open(file, mode, *a, **k)
                def fail_read(path, *a, **k):
                    if path == target:
                        raise OSError("synthetic read failure")
                    return real_read(path, *a, **k)
                with patch("builtins.open", side_effect=fail_open), \
                        patch.object(Path, "read_text", fail_read), \
                        self.assertLogs(module.log, level="ERROR"):
                    self.assertIs(self.save(kind, SECOND), False)
                self.assertEqual(self.loot.wardrive_save_errors, before + 1)
                self.assertEqual(target.read_bytes(), original)
                self.assertTrue(self.save(kind, SECOND))
                self.assertEqual(len(self.rows(kind)), 2)

    def test_new_files_are_readable_and_existing_mode_and_owner_are_preserved(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind))
                target = self.path(kind)
                if os.name == "posix":
                    self.assertEqual(target.stat().st_mode & 0o777, 0o644)
                target.chmod(0o640)
                previous = target.stat()
                self.assertTrue(self.save(kind, SECOND))
                current = target.stat()
                self.assertEqual(current.st_mode & 0o7777, previous.st_mode & 0o7777)
                self.assertEqual((current.st_uid, current.st_gid), (previous.st_uid, previous.st_gid))

    def test_invalid_utf8_read_counts_once_and_preserves_bytes_for_all_save_paths(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                target = self.path(kind)
                original = b"\xffinvalid UTF-8 CSV\n"
                target.write_bytes(original)
                before = self.loot.wardrive_save_errors
                try:
                    with self.assertLogs(module.log, level="ERROR"):
                        self.assertIs(self.save(kind), False)
                    self.assertEqual(self.loot.wardrive_save_errors, before + 1)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))
                finally:
                    # The unrelated session-summary reader is not part of this
                    # change and still expects valid UTF-8 at fixture cleanup.
                    target.unlink(missing_ok=True)

    def test_encoding_failure_keeps_existing_csv_and_counts_once(self):
        self.assertTrue(self.save("wifi"))
        target = self.path("wifi")
        original = target.read_bytes()
        before = self.loot.wardrive_save_errors
        with self.assertLogs(module.log, level="ERROR"):
            self.assertIs(self.loot._commit_wardrive_csv(target, ["\ud800invalid text"]), False)
        self.assertEqual(self.loot.wardrive_save_errors, before + 1)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))


if __name__ == "__main__":
    unittest.main()
