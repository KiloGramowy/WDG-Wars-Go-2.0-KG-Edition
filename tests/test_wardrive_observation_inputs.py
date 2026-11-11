"""Preserve real observation time/position through existing temporary CSV stores.

No GPS policy, GUI, player files, device, network, upload or firmware is used.
Position-only corrections are independent durable operations for each file.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdogs import loot_manager as module
from watchdogs.loot_manager import LootManager


MAC = "02:00:00:00:00:01"
OTHER = "02:00:00:00:00:02"
OBSERVED = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
POSITION = dict(lat=12.345678901, lon=-45.678901234, altitude=42.125, accuracy=2.34567)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2030, 11, 7, 14, 15, 16, tzinfo=tz)


class WardriveObservationInputsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gps = SimpleNamespace(available=True, fix=SimpleNamespace(
            valid=True, latitude=50.1, longitude=20.2, altitude=11.25, hdop=1.1))
        with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
            self.loot = LootManager(directory.name, gps_manager=self.gps)
        self.loot.save_xp(12345)
        self.loot.save_badge("wardriver")
        def close():
            with patch.object(os, "sync", lambda: None, create=True):
                self.loot.close()
        self.addCleanup(close)

    def path(self, kind):
        return Path(self.loot.session_dir) / ("bt_devices.csv" if kind == "devices" else "wardriving.csv")

    def save(self, kind, mac=MAC, rssi=-70, name="synthetic", **kwargs):
        if kind == "wifi":
            return self.loot.save_wardriving_network(SimpleNamespace(
                bssid=mac, ssid=name, auth="WPA2_PSK", channel="6", rssi=str(rssi)), **kwargs)
        if kind == "ble":
            return self.loot.save_wardriving_bt(mac, rssi, name, **kwargs)
        return self.loot.save_bt_device(mac, rssi, name, False, True, **kwargs)

    def reset_csvs(self):
        self.path("wifi").unlink(missing_ok=True)
        self.path("devices").unlink(missing_ok=True)

    def record(self, kind):
        with self.path(kind).open(encoding="utf-8", newline="") as stream:
            rows = list(stream)
        return (self.loot._bt_history_record(rows[-1]) if kind == "devices"
                else self.loot._wardrive_fields(rows[-1]))

    def fill(self, kind, mac=MAC, observed_at=OBSERVED, position=POSITION):
        if kind == "devices":
            return self.loot.fill_bt_device_position(mac, observed_at=observed_at, position=position)
        return self.loot.fill_wardriving_position(kind, mac, observed_at=observed_at, position=position)

    def test_default_sniff_call_bytes_and_current_gps_semantics_are_unchanged(self):
        with patch.object(module, "datetime", FixedDatetime):
            self.assertTrue(self.save("wifi"))
            self.assertTrue(self.save("ble", OTHER))
            self.assertTrue(self.save("devices", OTHER))
        self.assertEqual(self.path("wifi").read_text(encoding="utf-8"),
            self.loot._WIGLE_PRE_HEADER + self.loot._WIGLE_HEADER +
            f"{MAC},synthetic,[WPA2_PSK][ESS],2030-11-07 14:15:16,6,-70,50.1,20.2,11.2,5.5,WIFI\n" +
            f"{OTHER},synthetic,[BLE],2030-11-07 14:15:16,,-70,50.1,20.2,11.2,5.5,BLE\n")
        self.assertEqual(self.path("devices").read_text(encoding="utf-8"),
            "timestamp,mac,rssi,name,airtag,smarttag,lat,lon\n" +
            f"2030-11-07T14:15:16,{OTHER},-70,synthetic,False,True,50.1,20.2\n")

    def test_supplied_utc_time_and_assigned_position_override_later_globals(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind, observed_at=OBSERVED, position=POSITION))
                row = self.record(kind)
                if kind == "devices":
                    self.assertEqual((float(row["lat"]), float(row["lon"])),
                                     (POSITION["lat"], POSITION["lon"]))
                    self.assertTrue(self.path(kind).read_text().splitlines()[-1].startswith("2020-01-02T03:04:05,"))
                else:
                    self.assertEqual(row[3], "2020-01-02 03:04:05")
                    self.assertEqual(tuple(map(float, row[6:10])), tuple(POSITION.values()))

    def test_explicit_none_never_borrows_a_fix_obtained_after_observation(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind, observed_at=OBSERVED, position=None))
                row = self.record(kind)
                coords = (row["lat"], row["lon"]) if kind == "devices" else row[6:8]
                self.assertEqual(tuple(map(float, coords)), (0.0, 0.0))

    def test_optional_time_does_not_change_omitted_position_default(self):
        self.assertTrue(self.save("wifi", observed_at=OBSERVED.timestamp()))
        row = self.record("wifi")
        self.assertEqual(row[3], "2020-01-02 03:04:05")
        self.assertEqual(tuple(map(float, row[6:8])), (50.1, 20.2))

    def test_aware_offset_and_epoch_are_normalized_to_utc_with_fraction_preserved(self):
        offset = timezone(timedelta(hours=3, minutes=30))
        supplied = datetime(2020, 1, 2, 6, 34, 5, 123456, tzinfo=offset)
        for stamp in (supplied, supplied.timestamp()):
            for kind in ("wifi", "ble", "devices"):
                with self.subTest(stamp=stamp, kind=kind):
                    self.reset_csvs()
                    self.assertTrue(self.save(kind, observed_at=stamp, position=None))
                    expected = "2020-01-02T03:04:05.123456" if kind == "devices" else "2020-01-02 03:04:05.123456"
                    actual = (self.path(kind).read_text().splitlines()[-1].partition(",")[0]
                              if kind == "devices" else self.record(kind)[3])
                    self.assertEqual(actual, expected)

    def test_raw_hdop_is_never_guessed_to_be_accuracy_metres(self):
        self.assertTrue(self.save("wifi", observed_at=OBSERVED,
            position=dict(lat=1.0, lon=2.0, altitude=3.12345, hdop=0.9)))
        self.assertEqual(self.record("wifi")[8:10], ["3.12345", "0.0"])

    def test_invalid_explicit_times_cannot_write_or_fall_back_to_now(self):
        for stamp in (datetime(2020, 1, 2), math.nan, math.inf, True, "2020-01-02", 10**1000):
            for kind in ("wifi", "ble", "devices"):
                with self.subTest(stamp=str(stamp)[:40], kind=kind):
                    self.reset_csvs()
                    with self.assertRaises(ValueError):
                        self.save(kind, observed_at=stamp, position=POSITION)
                    self.assertFalse(self.path(kind).exists())
        self.assertEqual(self.loot.wardrive_save_errors, 0)

    def test_invalid_assigned_positions_cannot_borrow_current_gps(self):
        invalid = ({}, {"lat": 1}, {"lat": math.nan, "lon": 2},
                   {"lat": 1, "lon": math.inf}, {"lat": 91, "lon": 2},
                   {"lat": 1, "lon": 181}, {"lat": True, "lon": 2},
                   {"lat": 1, "lon": 2, "accuracy": -1},
                   {"lat": 1, "lon": 2, "altitude": math.nan}, "bad")
        for position in invalid:
            for kind in ("wifi", "ble", "devices"):
                with self.subTest(position=position, kind=kind):
                    self.reset_csvs()
                    with self.assertRaises(ValueError):
                        self.save(kind, observed_at=OBSERVED, position=position)
                    self.assertFalse(self.path(kind).exists())

    def test_supplied_inputs_keep_existing_protocol_and_rssi_deduplication(self):
        self.assertTrue(self.save("wifi", observed_at=OBSERVED, position=None))
        self.assertTrue(self.save("ble", observed_at=OBSERVED, position=None))
        before = self.path("wifi").read_bytes()
        for kind in ("wifi", "ble"):
            self.assertFalse(self.save(kind, rssi=-80, observed_at=OBSERVED, position=POSITION))
        self.assertEqual(self.path("wifi").read_bytes(), before)
        self.assertTrue(self.save("wifi", rssi=-40, observed_at=OBSERVED, position=POSITION))
        self.assertEqual(len(self.path("wifi").read_text().splitlines()), 4)
        self.assertTrue(self.save("devices", observed_at=OBSERVED, position=None))
        before = self.path("devices").read_bytes()
        self.assertFalse(self.save("devices", rssi=-20, observed_at=OBSERVED, position=POSITION))
        self.assertEqual(self.path("devices").read_bytes(), before)

    def test_fill_only_changes_gps_for_exact_observation_and_preserves_saved_progress(self):
        self.assertTrue(self.save("wifi", name='"synthetic, kitchen', observed_at=OBSERVED, position=None))
        self.assertTrue(self.save("ble", observed_at=OBSERVED, position=None))
        original = self.path("wifi").read_text().splitlines(keepends=True)
        database = self.loot._db_path.read_bytes()
        with patch.object(self.loot, "save_xp", side_effect=AssertionError("unexpected XP save")), \
                patch.object(self.loot, "_save_db", side_effect=AssertionError("unexpected DB write")):
            result = self.fill("wifi")
        self.assertEqual(result, dict(status="updated", persisted=True, changed=True))
        current = self.path("wifi").read_text().splitlines(keepends=True)
        self.assertEqual(current[:2] + current[3:], original[:2] + original[3:])
        self.assertEqual(current[2].rsplit(",", 5)[0], original[2].rsplit(",", 5)[0])
        self.assertEqual(self.loot._db_path.read_bytes(), database)
        self.assertEqual(self.loot.load_xp(), 12345)
        self.assertEqual(self.loot.load_badges(), {"wardriver"})

    def test_replay_is_noop_without_changed_bytes_mtime_or_writes(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind, observed_at=OBSERVED, position=None))
                self.assertTrue(self.fill(kind)["persisted"])
                target = self.path(kind)
                before, mtime = target.read_bytes(), target.stat().st_mtime_ns
                with patch.object(self.loot, "_commit_wardrive_csv", side_effect=AssertionError("no-op wrote CSV")):
                    self.assertEqual(self.fill(kind), dict(status="positioned", persisted=True, changed=False))
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(target.stat().st_mtime_ns, mtime)

    def test_ble_partial_pair_failure_replay_finishes_only_missing_store(self):
        for failed in ("ble", "devices"):
            with self.subTest(failed=failed):
                self.reset_csvs()
                self.assertTrue(self.save("ble", name="BLE, kitchen", observed_at=OBSERVED, position=None))
                self.assertTrue(self.save("devices", name="BLE, kitchen", observed_at=OBSERVED, position=None))
                database = self.loot._db_path.read_bytes()
                results = {}
                for kind in ("ble", "devices"):
                    if kind == failed:
                        with patch.object(module.os, "fsync", side_effect=OSError("synthetic fsync failure")), \
                                self.assertLogs(module.log, level="ERROR"):
                            results[kind] = self.fill(kind)
                    else:
                        results[kind] = self.fill(kind)
                self.assertFalse(all(value["persisted"] for value in results.values()))
                succeeded = "devices" if failed == "ble" else "ble"
                unchanged = self.path(succeeded).read_bytes()
                unchanged_mtime = self.path(succeeded).stat().st_mtime_ns
                self.assertEqual(self.fill(succeeded)["status"], "positioned")
                self.assertEqual(self.path(succeeded).read_bytes(), unchanged)
                self.assertEqual(self.path(succeeded).stat().st_mtime_ns, unchanged_mtime)
                self.assertEqual(self.fill(failed)["status"], "updated")
                self.assertTrue(all(self.fill(kind)["persisted"] for kind in ("ble", "devices")))
                self.assertEqual(len(self.path("ble").read_text().splitlines()), 3)
                self.assertEqual(len(self.path("devices").read_text().splitlines()), 2)
                self.assertEqual(self.loot._db_path.read_bytes(), database)

    def test_wrong_time_mac_or_protocol_never_changes_existing_observation(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind, observed_at=OBSERVED, position=None))
                old = self.path(kind).read_bytes()
                self.assertEqual(self.fill(kind, observed_at=OBSERVED + timedelta(seconds=1))["status"], "observation_changed")
                self.assertEqual(self.fill(kind, mac=OTHER)["status"], "missing")
                if kind != "devices":
                    other_protocol = "ble" if kind == "wifi" else "wifi"
                    self.assertEqual(self.fill(other_protocol)["status"], "missing")
                self.assertEqual(self.path(kind).read_bytes(), old)

    def test_later_stronger_observation_cannot_receive_an_older_rows_position(self):
        self.assertTrue(self.save("wifi", observed_at=OBSERVED, position=None))
        self.assertTrue(self.save("wifi", rssi=-20, observed_at=OBSERVED + timedelta(seconds=5), position=None))
        old = self.path("wifi").read_bytes()
        self.assertEqual(self.fill("wifi")["status"], "observation_changed")
        self.assertEqual(self.path("wifi").read_bytes(), old)

    def test_verified_stronger_current_export_reports_superseded_without_rewriting_it(self):
        self.assertTrue(self.save("wifi", observed_at=OBSERVED, position=None))
        later = OBSERVED + timedelta(seconds=5)
        self.assertTrue(self.save("wifi", rssi=-20, observed_at=later, position=POSITION))
        path = self.path("wifi")
        old, mtime = path.read_bytes(), path.stat().st_mtime_ns
        result = self.loot.fill_wardriving_position("wifi", MAC, observed_at=OBSERVED,
            position=POSITION, source_rssi=-70)
        self.assertEqual(result, dict(status="superseded", persisted=False, changed=False,
            current_observed_at="2020-01-02 03:04:10", current_rssi=-20))
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_supersession_requires_valid_different_timestamp_and_stronger_signal(self):
        for stamp, strength in (("bad time", -20), ("2020-01-02 03:04:10", -80),
                                ("2020-02-31 03:04:10", -20), ("2020-01-02 03:04:05", -20)):
            with self.subTest(stamp=stamp, strength=strength):
                self.reset_csvs()
                self.assertTrue(self.save("wifi", observed_at=OBSERVED, position=None))
                path = self.path("wifi")
                data = path.read_text().replace("2020-01-02 03:04:05", stamp).replace(",-70,", f",{strength},")
                path.write_text(data)
                old = path.read_bytes()
                result = self.loot.fill_wardriving_position("wifi", MAC, observed_at=OBSERVED,
                    position=POSITION, source_rssi=-70)
                self.assertEqual(result["status"], "observation_changed")
                self.assertFalse(result["persisted"])
                self.assertEqual(path.read_bytes(), old)

    def test_ble_stronger_repeat_keeps_distinct_canonical_times_for_both_stores(self):
        later = OBSERVED + timedelta(seconds=5)
        self.assertTrue(self.save("ble", observed_at=OBSERVED, position=None))
        self.assertTrue(self.save("devices", observed_at=OBSERVED, position=None))
        self.assertTrue(self.save("ble", rssi=-20, observed_at=later, position=None))
        self.assertFalse(self.save("devices", rssi=-20, observed_at=later, position=None))
        database = self.loot._db_path.read_bytes()
        before = {kind: self.path(kind).read_bytes() for kind in ("ble", "devices")}
        # WiGLE keeps the strongest RX; BLE history retains the first RX.
        self.assertEqual(self.fill("ble", observed_at=OBSERVED)["status"], "observation_changed")
        self.assertEqual(self.fill("devices", observed_at=later)["status"], "observation_changed")
        for kind in before:
            self.assertEqual(self.path(kind).read_bytes(), before[kind])
        self.assertEqual(self.fill("ble", observed_at=later)["status"], "updated")
        self.assertEqual(self.fill("devices", observed_at=OBSERVED)["status"], "updated")
        self.assertEqual(self.record("ble")[3:6], ["2020-01-02 03:04:10", "", "-20"])
        self.assertTrue(self.path("devices").read_text().splitlines()[-1].startswith(
            f"2020-01-02T03:04:05,{MAC},-70,"))
        self.assertEqual(self.loot._db_path.read_bytes(), database)

    def test_positioned_rows_are_never_replaced_by_backfill(self):
        for kind in ("wifi", "ble", "devices"):
            with self.subTest(kind=kind):
                self.reset_csvs()
                self.assertTrue(self.save(kind, observed_at=OBSERVED, position=POSITION))
                old = self.path(kind).read_bytes()
                self.assertEqual(self.fill(kind, position=dict(lat=70, lon=80))["status"], "positioned")
                self.assertEqual(self.path(kind).read_bytes(), old)

    def test_helpers_are_current_session_only_and_do_not_create_missing_files(self):
        self.assertTrue(self.save("wifi", observed_at=OBSERVED, position=None))
        old_path, old = self.path("wifi"), self.path("wifi").read_bytes()
        self.loot._session = old_path.parent.parent / "other-session"
        self.loot._session.mkdir()
        for kind in ("wifi", "ble", "devices"):
            self.assertEqual(self.fill(kind)["status"], "missing")
            self.assertFalse(self.path(kind).exists())
        self.assertEqual(old_path.read_bytes(), old)

    def test_native_comma_names_crlf_and_fractional_time_survive_corrections(self):
        stamp = OBSERVED.replace(microsecond=123456)
        first = "2020-01-02 03:04:05.123456"
        wardrive = (self.loot._WIGLE_PRE_HEADER + self.loot._WIGLE_HEADER).replace("\n", "\r\n")
        wardrive += f'{MAC},"synthetic, kitchen,[BLE],{first},,-70,0,0,0,0,BLE\r\n'
        bt = ("timestamp,mac,rssi,name,airtag,smarttag,lat,lon\r\n" +
              f'2020-01-02T03:04:05.123456,{MAC},-70,"synthetic, kitchen,False,True,0,0\r\n')
        self.path("ble").write_bytes(wardrive.encode())
        self.path("devices").write_bytes(bt.encode())
        self.assertEqual(self.fill("ble", observed_at=stamp)["status"], "updated")
        self.assertEqual(self.fill("devices", observed_at=stamp)["status"], "updated")
        self.assertEqual(self.path("ble").read_bytes(), wardrive.replace(
            ",0,0,0,0,BLE\r\n", f',{POSITION["lat"]},{POSITION["lon"]},{POSITION["altitude"]},{POSITION["accuracy"]},BLE\r\n').encode())
        self.assertEqual(self.path("devices").read_bytes(), bt.replace(
            ",True,0,0\r\n", f',True,{POSITION["lat"]},{POSITION["lon"]}\r\n').encode())

    def test_ambiguous_ble_history_match_is_not_rewritten(self):
        self.assertTrue(self.save("devices", observed_at=OBSERVED, position=None))
        target = self.path("devices")
        with target.open("a", encoding="utf-8") as stream:
            stream.write(target.read_text().splitlines(keepends=True)[-1])
        old = target.read_bytes()
        self.assertEqual(self.fill("devices")["status"], "ambiguous")
        self.assertEqual(target.read_bytes(), old)

    def test_correction_and_existing_ble_writer_share_lock_without_lost_rows(self):
        original = [f"02:00:00:00:01:{number:02X}" for number in range(8)]
        appended = [f"02:00:00:00:02:{number:02X}" for number in range(8)]
        for mac in original:
            self.assertTrue(self.save("devices", mac, observed_at=OBSERVED, position=None))
        work = [("fill", mac) for mac in original] + [("save", mac) for mac in appended]
        def act(item):
            action, mac = item
            return (self.fill("devices", mac)["persisted"] if action == "fill" else
                    self.save("devices", mac, observed_at=OBSERVED, position=POSITION))
        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertTrue(all(executor.map(act, work)))
        with self.path("devices").open(encoding="utf-8") as stream:
            rows = [self.loot._bt_history_record(line) for line in stream]
        rows = [row for row in rows if row is not None]
        self.assertEqual({row["mac"] for row in rows}, set(original + appended))
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(float(row["lat"]) == POSITION["lat"] for row in rows))
        self.assertEqual(json.loads(self.loot._db_path.read_text())["xp"], 12345)


if __name__ == "__main__":
    unittest.main()
