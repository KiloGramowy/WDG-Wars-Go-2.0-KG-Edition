"""Real temporary stores, asynchronous SOLO writes, and injected I/O failures."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdogs import loot_manager as loot_module
from watchdogs.loot_manager import LootManager
from watchdogs.wardrive_gps import WardriveGpsPolicy
from watchdogs.wardrive_recorder import WardriveRecorder


MAC = "02:00:00:00:00:01"


class WardriveRecorderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = 100.0
        self.events = []
        self.recorders = []
        # A later global fix must never be borrowed for delayed observations.
        gps = SimpleNamespace(available=True, fix=SimpleNamespace(
            valid=True, latitude=70.0, longitude=80.0, altitude=90.0, hdop=1.0))
        with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
            self.loot = LootManager(directory.name, gps_manager=gps)
        self.loot.save_xp(12345)
        self.loot.save_badge("wardriver")
        self.addCleanup(self.finish)

    def finish(self):
        for recorder in self.recorders:
            deadline = time.monotonic() + 4
            while not recorder.close(0.03):
                self.events.extend(recorder.poll_events(1024))
                if recorder.failed:
                    recorder.retry()
                if recorder.policy.startup_closing:
                    recorder.seal_startup()
                recorder.tick()
                if time.monotonic() >= deadline:
                    self.fail("Recorder did not stop after fixture recovery")
            self.events.extend(recorder.poll_events(1024))
        with patch.object(os, "sync", lambda: None, create=True):
            self.loot.close()

    def make(self, cache_minutes=720, queue_size=256):
        policy = WardriveGpsPolicy(cache_minutes=cache_minutes, clock=lambda: self.now)
        recorder = WardriveRecorder(self.loot, policy, queue_size=queue_size, clock=lambda: self.now)
        self.recorders.append(recorder)
        return recorder

    def observation(self, seq=1, protocol="wifi", mac=MAC, rssi=-70):
        return dict(identity=("boot-test", "session-test", seq), protocol=protocol,
            mac=mac, ssid="synthetic", name="synthetic BLE", auth="WPA2_PSK", channel=6,
            rssi=rssi, observed_at=self.now,
            captured_at=(datetime(2020, 1, 2, tzinfo=timezone.utc) + timedelta(microseconds=seq)).isoformat(),
            rawcapture_monotonic_us=seq * 1000, clock_source="test_fixed_anchor", clock_uncertainty_s=0.05)

    def feed_fix(self, recorder, lat=36.46, **changes):
        value = dict(lat=lat, lon=-6.2, altitude=10.125, hdop=0.9,
                     sats=8, quality=1, observed_at=self.now)
        value.update(changes)
        return recorder.policy.feed_sample(value, self.now)

    def wait(self, recorder, predicate, *, replay=False, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.events.extend(recorder.poll_events(1024))
            if predicate():
                self.events.extend(recorder.poll_events(1024))
                return
            if replay:
                recorder.tick()
            time.sleep(0.002)
        self.fail("Timed out waiting for asynchronous recorder state")

    def count(self, name):
        return sum(event["event"] == name for event in self.events)

    def fields(self):
        path = Path(self.loot.session_dir) / "wardriving.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            return [self.loot._wardrive_fields(line) for index, line in enumerate(stream) if index > 1]

    def ready(self, recorder):
        self.assertTrue(self.feed_fix(recorder))
        self.assertTrue(recorder.seal_startup())
        self.wait(recorder, lambda: self.count("backfill_done") >= 1, replay=True)

    def test_before_fix_rx_are_saved_unpositioned_and_reported_once_without_backend_xp(self):
        recorder = self.make()
        original = self.observation()
        self.assertTrue(recorder.submit(original))
        original["ssid"] = "mutated controller buffer"
        self.assertTrue(recorder.submit(self.observation(2, "ble")))
        self.wait(recorder, lambda: self.count("saved") == 2)
        self.assertTrue(all(row[6:8] == ["0.0", "0.0"] for row in self.fields()))
        saved = [event for event in self.events if event["event"] == "saved"]
        self.assertEqual(saved[0]["observation"]["ssid"], "synthetic")
        self.assertTrue(all(event["position"] is None and event["replay"] is False for event in saved))
        self.assertEqual(self.loot.load_xp(), 12345)
        self.assertEqual(self.loot.load_badges(), {"wardriver"})
        self.assertTrue(recorder._pending_path.exists())
        log = (Path(self.loot.session_dir) / "serial_full.log").read_text()
        self.assertIn('"startup_pending":true', log)
        self.assertIn('"clock_uncertainty_s":0.05', log)

    def test_first_fix_accepts_late_inflight_rx_then_seal_replays_frozen_position(self):
        recorder = self.make()
        self.assertTrue(recorder.submit(self.observation(1)))
        self.now = 101
        self.assertTrue(self.feed_fix(recorder))
        self.assertTrue(recorder.submit(self.observation(2, "ble")))
        self.now = 102
        self.assertFalse(self.feed_fix(recorder, lat=40))
        self.wait(recorder, lambda: self.count("saved") == 2)
        recorder.tick()
        time.sleep(0.02)
        self.events.extend(recorder.poll_events(1024))
        self.assertEqual(self.count("backfill"), 0)
        self.assertTrue(recorder.seal_startup())
        self.assertFalse(recorder.submit(self.observation(3)))
        self.wait(recorder, lambda: self.count("backfill_done") == 1, replay=True)
        self.assertEqual(self.count("saved"), 2)
        self.assertEqual(self.count("backfill"), 2)
        self.assertTrue(all(event["replay"] and event["position"]["lat"] == 36.46
            and event["position"]["source"] == "startup_backfill"
            for event in self.events if event["event"] == "backfill"))
        self.assertTrue(all(float(row[6]) == 36.46 for row in self.fields()))
        self.assertFalse(recorder._pending_path.exists())

    def test_each_tick_grants_at_most_twenty_background_replay_records(self):
        recorder = self.make(queue_size=64)
        for number in range(25):
            self.assertTrue(recorder.submit(self.observation(number + 1,
                mac=f"02:00:00:00:01:{number:02X}")))
        self.wait(recorder, lambda: self.count("saved") == 25)
        self.now = 101
        self.feed_fix(recorder)
        recorder.seal_startup()
        self.wait(recorder, lambda: recorder._stage == "replay")
        self.assertTrue(recorder.tick())
        self.wait(recorder, lambda: recorder._credit == 0)
        self.assertLessEqual(self.count("backfill"), 20)
        self.assertGreater(self.count("backfill"), 0)
        self.assertEqual(self.count("backfill_done"), 0)
        self.wait(recorder, lambda: self.count("backfill_done") == 1, replay=True)
        self.assertEqual(self.count("backfill"), 25)

    def test_cache_loss_expiry_and_recovery_do_not_start_a_second_backfill(self):
        recorder = self.make(cache_minutes=1)
        self.ready(recorder)
        self.now = 102
        recorder.policy.feed_sample(None, self.now)
        self.assertTrue(recorder.submit(self.observation(1)))
        self.wait(recorder, lambda: self.count("saved") == 1)
        self.now = 161
        recorder.policy.feed_sample(None, self.now)
        self.assertTrue(recorder.submit(self.observation(2, mac="02:00:00:00:00:02")))
        self.wait(recorder, lambda: self.count("saved") == 2)
        self.now = 162
        self.assertFalse(self.feed_fix(recorder, lat=40))
        self.assertTrue(recorder.submit(self.observation(3, mac="02:00:00:00:00:03")))
        self.wait(recorder, lambda: self.count("saved") == 3)
        positions = [event["position"] for event in self.events if event["event"] == "saved"]
        self.assertEqual(positions[0]["source"], "cache")
        self.assertEqual(positions[0]["observed_at"], 100)
        self.assertIsNone(positions[1])
        self.assertEqual(positions[2]["source"], "measured")
        self.assertEqual(self.count("backfill_done"), 1)
        self.assertFalse(recorder._pending_path.exists())

    def test_retransmitted_source_identity_never_emits_another_xp_event(self):
        recorder = self.make()
        value = self.observation()
        self.assertTrue(recorder.submit(value))
        self.assertTrue(recorder.submit(value))
        self.wait(recorder, lambda: recorder._jobs.unfinished_tasks == 0)
        self.assertEqual(self.count("saved"), 1)
        with recorder._pending_path.open(encoding="utf-8") as stream:
            self.assertEqual(sum(json.loads(line).get("kind") == "rx" for line in stream), 1)

    def test_csv_partial_failure_retains_job_and_same_source_retry_emits_once(self):
        recorder = self.make()
        self.wait(recorder, lambda: recorder._initialised)
        original = self.loot._commit_wardrive_csv
        failing = True
        def commit(path, rows):
            if path.name == "bt_devices.csv" and failing:
                with patch.object(loot_module.os, "fsync", side_effect=OSError("synthetic disk failure")):
                    return original(path, rows)
            return original(path, rows)
        with patch.object(self.loot, "_commit_wardrive_csv", side_effect=commit):
            self.assertTrue(recorder.submit(self.observation(1, "ble")))
            self.wait(recorder, lambda: recorder.failed)
            self.assertEqual(self.count("saved"), 0)
            self.assertEqual(recorder._highwater, {})
            self.assertFalse(recorder.close(0.02))
            failing = False
            self.assertTrue(recorder.retry())
            self.wait(recorder, lambda: self.count("saved") == 1)
        self.assertTrue(recorder.close())
        self.assertEqual(len(self.fields()), 1)
        self.assertEqual(self.count("saved"), 1)
        self.assertEqual(self.count("backfill"), 0)
        self.now = 101
        self.feed_fix(recorder)
        self.assertFalse(recorder.seal_startup())
        self.assertEqual(recorder._jobs.unfinished_tasks, 0)
        self.assertEqual(self.loot.load_xp(), 12345)

    def test_failed_strict_provenance_does_not_advance_source_or_duplicate_pending(self):
        recorder = self.make()
        self.wait(recorder, lambda: recorder._initialised)
        with patch.object(self.loot, "append_wardrive_provenance", return_value=False):
            self.assertTrue(recorder.submit(self.observation()))
            self.wait(recorder, lambda: recorder.failed)
        self.assertEqual(recorder._highwater, {})
        self.assertEqual(self.count("saved"), 0)
        self.assertTrue(recorder.retry())
        self.wait(recorder, lambda: self.count("saved") == 1)
        with recorder._pending_path.open(encoding="utf-8") as stream:
            self.assertEqual(sum(json.loads(line).get("kind") == "rx" for line in stream), 1)

    def test_pending_fsync_failure_replaces_only_unacknowledged_tail_on_retry(self):
        recorder = self.make()
        self.wait(recorder, lambda: recorder._initialised)
        descriptor = recorder._pending_writer.fileno()
        original = os.fsync
        failing = True
        def fsync(fd):
            if fd == descriptor and failing:
                raise OSError("synthetic pending fsync failure")
            return original(fd)
        with patch.object(os, "fsync", side_effect=fsync):
            self.assertTrue(recorder.submit(self.observation()))
            self.wait(recorder, lambda: recorder.failed)
            failing = False
            recorder.retry()
            self.wait(recorder, lambda: self.count("saved") == 1)
        with recorder._pending_path.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        self.assertEqual([row["kind"] for row in rows], ["session", "rx"])

    def test_backfill_partial_two_file_failure_retries_without_duplicate_or_early_done(self):
        recorder = self.make()
        self.assertTrue(recorder.submit(self.observation(1, "ble")))
        self.wait(recorder, lambda: self.count("saved") == 1)
        self.now = 101
        self.feed_fix(recorder)
        recorder.seal_startup()
        with patch.object(self.loot, "fill_bt_device_position", side_effect=OSError("synthetic BLE correction failure")):
            self.wait(recorder, lambda: recorder.failed, replay=True)
        path = Path(self.loot.session_dir) / "wardriving.csv"
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(self.count("backfill_done"), 0)
        self.assertTrue(recorder.retry())
        self.wait(recorder, lambda: self.count("backfill_done") == 1, replay=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.count("saved"), 1)
        self.assertEqual(self.count("backfill"), 1)

    def test_stronger_ble_repeat_uses_correct_canonical_timestamp_in_each_store(self):
        recorder = self.make()
        self.assertTrue(recorder.submit(self.observation(1, "ble", rssi=-70)))
        self.assertTrue(recorder.submit(self.observation(2, "ble", rssi=-20)))
        self.wait(recorder, lambda: self.count("saved") == 2)
        self.now = 101
        self.feed_fix(recorder)
        recorder.seal_startup()
        self.wait(recorder, lambda: self.count("backfill_done") == 1, replay=True)
        self.assertEqual(self.fields()[0][5], "-20")
        with (Path(self.loot.session_dir) / "bt_devices.csv").open(encoding="utf-8") as stream:
            rows = list(stream)
        record = self.loot._bt_history_record(rows[-1])
        self.assertEqual(record["rssi"], "-70")
        self.assertEqual(float(record["lat"]), 36.46)
        self.assertEqual(self.count("saved"), 2)
        self.assertEqual(self.count("backfill"), 2)
        log = (Path(self.loot.session_dir) / "serial_full.log").read_text()
        self.assertIn('"wardriving":"not_current_export"', log)

    def test_later_real_sniff_export_is_preserved_while_older_solo_rx_backfills(self):
        recorder = self.make()
        self.assertTrue(recorder.submit(self.observation()))
        self.wait(recorder, lambda: self.count("saved") == 1)
        # Existing SNIFF API, outside the recorder, writes a newer stronger RX.
        from watchdogs.app_state import Network
        self.assertTrue(self.loot.save_wardriving_network(Network(
            bssid=MAC, ssid="newer SNIFF", channel="11", auth="WPA2_PSK", rssi="-20")))
        target = Path(self.loot.session_dir) / "wardriving.csv"
        before, mtime = target.read_bytes(), target.stat().st_mtime_ns
        self.now = 101
        self.feed_fix(recorder)
        recorder.seal_startup()
        self.wait(recorder, lambda: self.count("backfill_done") == 1, replay=True)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, mtime)
        self.assertEqual(self.count("saved"), 1)
        self.assertEqual(self.count("backfill"), 1)
        self.assertEqual(self.loot.load_xp(), 12345)
        log = (Path(self.loot.session_dir) / "serial_full.log").read_text()
        self.assertIn('"wardriving":"not_current_export"', log)
        self.assertIn('"reason":"stronger_current_export"', log)

    def test_failed_pending_cleanup_holds_resume_and_can_be_retried_after_reader_closed(self):
        recorder = self.make()
        self.assertTrue(recorder.submit(self.observation()))
        self.wait(recorder, lambda: self.count("saved") == 1)
        self.now = 101
        self.feed_fix(recorder)
        recorder.seal_startup()
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == recorder._pending_path:
                raise OSError("synthetic pending removal failure")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            self.wait(recorder, lambda: recorder.failed, replay=True)
        self.assertEqual(self.count("backfill_done"), 0)
        self.assertTrue(recorder.policy.startup_closing)
        recorder.retry()
        self.wait(recorder, lambda: self.count("backfill_done") == 1, replay=True)
        self.assertEqual(self.count("backfill"), 1)

    def test_restart_before_first_fix_never_adopts_earlier_pending_observations(self):
        before = self.make()
        self.assertTrue(before.submit(self.observation()))
        self.wait(before, lambda: self.count("saved") == 1)
        self.assertTrue(before.close())
        self.assertTrue(before._pending_path.exists())
        after = self.make()
        self.wait(after, lambda: after._initialised)
        self.now = 101
        self.feed_fix(after)
        after.seal_startup()
        self.wait(after, lambda: self.count("backfill_done") == 1, replay=True)
        self.assertEqual(self.count("backfill"), 0)
        self.assertEqual(self.fields()[0][6:8], ["0.0", "0.0"])

    def test_submit_and_tick_remain_nonblocking_while_writer_is_busy_and_queue_is_bounded(self):
        recorder = self.make(queue_size=1)
        self.wait(recorder, lambda: recorder._initialised)
        entered, release = threading.Event(), threading.Event()
        original = recorder._raw
        def blocked(job):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test writer gate was not released")
            return original(job)
        try:
            with patch.object(recorder, "_raw", side_effect=blocked):
                self.assertTrue(recorder.submit(self.observation(1)))
                self.assertTrue(entered.wait(1))
                self.assertTrue(recorder.submit(self.observation(2)))
                self.assertFalse(recorder.submit(self.observation(3)))
                self.assertEqual(recorder._jobs.qsize(), 1)
                self.assertFalse(recorder.tick())
                recorder.retry()  # Clear the rejected, unaccepted input alarm.
                release.set()
                self.wait(recorder, lambda: self.count("saved") == 2)
        finally:
            release.set()

    def test_csv_and_strict_log_io_run_only_on_background_writer(self):
        recorder = self.make()
        calls = []
        store, append = self.loot.save_wardriving_network, self.loot.append_wardrive_provenance
        def save(*args, **kwargs):
            calls.append(threading.current_thread().name)
            return store(*args, **kwargs)
        def provenance(*args, **kwargs):
            calls.append(threading.current_thread().name)
            return append(*args, **kwargs)
        with patch.object(self.loot, "save_wardriving_network", side_effect=save), \
                patch.object(self.loot, "append_wardrive_provenance", side_effect=provenance):
            self.assertTrue(recorder.submit(self.observation()))
            self.wait(recorder, lambda: self.count("saved") == 1)
        self.assertEqual(calls, ["wardrive-recorder", "wardrive-recorder"])

    def test_close_cannot_invent_stop_ack_when_first_fix_is_still_unsealed(self):
        recorder = self.make()
        self.feed_fix(recorder)
        self.assertFalse(recorder.close(0.02))
        self.assertEqual(self.count("backfill_done"), 0)
        self.assertTrue(recorder.seal_startup())
        self.assertTrue(recorder.close())
        self.events.extend(recorder.poll_events(1024))
        self.assertEqual(self.count("backfill_done"), 1)

    def test_close_finishes_sealed_replay_in_bounded_batches_without_ui_ticks(self):
        recorder = self.make(queue_size=32)
        for number in range(22):
            self.assertTrue(recorder.submit(self.observation(number + 1,
                mac=f"02:00:00:00:01:{number:02X}")))
        self.wait(recorder, lambda: self.count("saved") == 22)
        self.now = 101
        self.feed_fix(recorder)
        self.assertTrue(recorder.seal_startup())
        self.assertTrue(recorder.close())
        self.events.extend(recorder.poll_events(1024))
        self.assertEqual(self.count("saved"), 22)
        self.assertEqual(self.count("backfill"), 22)
        self.assertEqual(self.count("backfill_done"), 1)

    def test_raw_input_error_is_explicit_and_can_be_cleared_without_writing_it(self):
        recorder = self.make()
        bad = self.observation()
        bad["captured_at"] = "2020-01-02T00:00:00"  # missing UTC offset
        self.assertFalse(recorder.submit(bad))
        self.assertTrue(recorder.failed)
        recorder.retry()
        self.assertTrue(recorder.submit(self.observation()))
        self.wait(recorder, lambda: self.count("saved") == 1)
        self.assertEqual(self.count("error"), 1)

    def test_strict_provenance_uses_one_append_write_and_rejects_oversized_record(self):
        self.loot.log_serial("raw legacy line")
        original = os.write
        with patch.object(os, "write", wraps=original) as write:
            self.assertTrue(self.loot.append_wardrive_provenance(dict(event="test", source="cache", observed_at=100)))
            self.assertEqual(write.call_count, 1)
        target = Path(self.loot.session_dir) / "serial_full.log"
        before = target.read_bytes()
        with self.assertLogs(loot_module.log, level="ERROR"):
            self.assertFalse(self.loot.append_wardrive_provenance(dict(value="x" * 40000)))
        self.assertEqual(target.read_bytes(), before)
        self.assertIn(b"raw legacy line", before)

    def test_nonregular_pending_path_fails_closed_and_retry_does_not_adopt_it(self):
        target = Path(self.loot.session_dir) / "wardrive_startup.pending.jsonl"
        target.mkdir()
        recorder = self.make()
        self.wait(recorder, lambda: recorder.failed)
        self.assertTrue(target.is_dir())
        self.assertFalse(recorder._initialised)
        target.rmdir()
        recorder.retry()
        self.wait(recorder, lambda: recorder._initialised)
        self.assertTrue(recorder.submit(self.observation()))
        self.wait(recorder, lambda: self.count("saved") == 1)

    def test_reported_symlink_pending_is_never_opened_or_truncated(self):
        target = Path(self.loot.session_dir) / "wardrive_startup.pending.jsonl"
        original = Path.lstat
        def lstat(path):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return original(path)
        with patch.object(Path, "lstat", lstat):
            recorder = self.make()
            self.wait(recorder, lambda: recorder.failed)
        self.assertFalse(target.exists())
        recorder.retry()
        self.wait(recorder, lambda: recorder._initialised)

    def test_strict_log_refuses_symlink_or_nonregular_path_before_any_write(self):
        target = Path(self.loot.session_dir) / "serial_full.log"
        before = target.read_bytes()
        original = Path.lstat
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFDIR):
            with self.subTest(mode=mode):
                def lstat(path):
                    return SimpleNamespace(st_mode=mode) if path == target else original(path)
                with patch.object(Path, "lstat", lstat), patch.object(os, "write") as write, \
                        self.assertLogs(loot_module.log, level="ERROR"):
                    self.assertFalse(self.loot.append_wardrive_provenance(dict(event="test")))
                    write.assert_not_called()
        self.assertEqual(target.read_bytes(), before)

    def test_short_provenance_write_is_not_acknowledged_and_retry_has_a_complete_record(self):
        original = os.write
        def partial(fd, data):
            return original(fd, data[:5])
        record = dict(event="test", identity=["boot", "session", 1])
        with patch.object(os, "write", side_effect=partial), self.assertLogs(loot_module.log, level="ERROR"):
            self.assertFalse(self.loot.append_wardrive_provenance(record))
        self.assertTrue(self.loot.append_wardrive_provenance(record))
        target = Path(self.loot.session_dir) / "serial_full.log"
        lines = target.read_text().splitlines()
        complete = [json.loads(line[len("[WARDGPS] "):]) for line in lines if line.startswith("[WARDGPS] ")]
        self.assertEqual(complete, [record])


if __name__ == "__main__":
    unittest.main()
