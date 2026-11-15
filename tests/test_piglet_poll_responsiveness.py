"""Whole Collection UI admission under real store/DB thread contention."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_piglet_collection_ui as fixtures


class TrackedLock:
    def __init__(self, name, events):
        self.name, self.events = name, events
        self.inner = threading.RLock()

    def acquire(self, blocking=True):
        self.events.append((self.name, "acquire", blocking))
        return self.inner.acquire(blocking=blocking)

    def release(self):
        self.events.append((self.name, "release"))
        self.inner.release()


class PigletPollResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.g = fixtures.game()
        self.order = []
        self.store_lock = TrackedLock("store", self.order)
        self.db_lock = TrackedLock("db", self.order)
        self.g.loot = SimpleNamespace(_db_lock=self.db_lock)
        self.g._piglet_collection.store._lock = self.store_lock
        self.g._piglet_status = {"state": "previous", "pending_receipts": 2}
        self.original_status = self.g._piglet_status

    def hold(self, lock):
        entered, release = threading.Event(), threading.Event()

        def worker():
            lock.acquire()
            try:
                entered.set()
                release.wait(3)
            finally:
                lock.release()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.addCleanup(thread.join, 1)
        self.addCleanup(release.set)
        self.order.clear()
        return release, thread

    def poll_fast(self):
        finished = threading.Event()
        failures = []

        def frame():
            try:
                self.g._poll_piglet_collection()
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=frame, daemon=True)
        thread.start()
        try:
            self.assertTrue(finished.wait(.25), "Collection poll waited for another thread's lock")
            self.assertEqual(failures, [])
        finally:
            thread.join(.1)

    def assert_no_callbacks(self):
        collection = self.g._piglet_collection
        collection.store.process_pending.assert_not_called()
        collection.store.pending_events.assert_not_called()
        collection.store.acknowledge_events.assert_not_called()
        collection.snapshot.assert_not_called()

    def assert_released_for_other_thread(self):
        acquired = []

        def worker():
            for lock in (self.store_lock, self.db_lock):
                result = lock.acquire(blocking=False)
                acquired.append(result)
                if result:
                    lock.release()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(1)
        self.assertEqual(acquired, [True, True])

    def test_busy_store_returns_before_database_or_any_presentation(self):
        release, worker = self.hold(self.store_lock)
        self.g._piglet_poll_at, self.g._piglet_error = 17.5, "prior error"
        self.poll_fast()
        self.assertEqual(self.order, [("store", "acquire", False)])
        self.assert_no_callbacks()
        self.assertEqual((self.g.xp, self.g._xp_dirty), (135, True))
        self.assertEqual((self.g._piglet_poll_at, self.g._piglet_error), (17.5, "prior error"))
        self.assertIs(self.g._piglet_status, self.original_status)
        release.set(); worker.join(1)

    def test_busy_database_releases_store_without_consuming_pending_receipts(self):
        release, worker = self.hold(self.db_lock)
        receipts = [{"event_id": "synthetic-1"}, {"event_id": "synthetic-2"}]
        self.g._piglet_collection.store.pending_events.side_effect = lambda **_kw: list(receipts)
        self.g._piglet_poll_at, self.g._piglet_error = 17.5, "prior error"
        self.poll_fast()
        self.assertEqual(self.order, [("store", "acquire", False), ("db", "acquire", False), ("store", "release")])
        self.assert_no_callbacks()
        self.assertEqual(len(receipts), 2)
        self.assertEqual((self.g.xp, self.g._xp_dirty), (135, True))
        self.assertEqual((self.g._piglet_poll_at, self.g._piglet_error), (17.5, "prior error"))
        # The failed DB admission must not leave the already-acquired store held.
        obtained = []
        probe = threading.Thread(target=lambda: self._probe_store(obtained), daemon=True)
        probe.start(); probe.join(1)
        self.assertEqual(obtained, [True])
        release.set(); worker.join(1)

    def _probe_store(self, results):
        acquired = self.store_lock.acquire(blocking=False)
        results.append(acquired)
        if acquired:self.store_lock.release()

    def test_release_then_retry_applies_and_acknowledges_each_receipt_once(self):
        g = self.g
        events = [dict(event_id="synthetic-1", record=fixtures.record(), new_identity=True)]
        collection = g._piglet_collection
        pending = list(events)
        collection.store.pending_events.side_effect = lambda **_kw: list(pending)
        collection.store.process_pending.return_value = {"committed": True, "xp_total": 160}

        def acknowledge(ids):
            self.assertEqual(ids, ["synthetic-1"])
            pending.clear()
            return True

        collection.store.acknowledge_events.side_effect = acknowledge
        original = g._apply_piglet_events
        g._apply_piglet_events = Mock(side_effect=original)
        release, worker = self.hold(self.db_lock)
        self.poll_fast()
        self.assertEqual(g.xp, 135)
        self.assertEqual(pending, events)
        g._apply_piglet_events.assert_not_called()
        release.set(); worker.join(1)
        g._poll_piglet_collection()
        self.assertEqual((g.xp, g._xp_dirty, pending), (160, False, []))
        g._piglet_poll_at = 0
        collection.store.process_pending.return_value = {"committed": False, "xp_total": 160}
        g._poll_piglet_collection()
        g._apply_piglet_events.assert_called_once_with(events)
        collection.store.acknowledge_events.assert_called_once_with(["synthetic-1"])
        g.gain_xp.assert_not_called()  # Durable import XP must not be awarded again.

    def test_whole_poll_has_store_then_database_and_reentrant_admission(self):
        collection = self.g._piglet_collection

        def checked(result):
            def callback(*_args, **_kwargs):
                self.assertTrue(self.store_lock.inner._is_owned())
                self.assertTrue(self.db_lock.inner._is_owned())
                # The actual nested Store/Loot APIs use these reentrant locks.
                self.assertTrue(self.store_lock.inner.acquire(blocking=False))
                self.assertTrue(self.db_lock.inner.acquire(blocking=False))
                self.db_lock.inner.release(); self.store_lock.inner.release()
                return result
            return callback

        collection.store.process_pending.side_effect = checked({"committed": False})
        collection.store.pending_events.side_effect = checked([{"event_id": "synthetic"}])
        self.g._apply_piglet_events = Mock(side_effect=checked(["synthetic"]))
        collection.store.acknowledge_events.side_effect = checked(True)
        collection.snapshot.side_effect = checked({"state": "active"})
        self.g._poll_piglet_collection()
        self.assertEqual(self.order, [("store", "acquire", False), ("db", "acquire", False),
                                      ("db", "release"), ("store", "release")])
        self.assert_released_for_other_thread()

    def test_every_failure_path_releases_both_locks_and_keeps_existing_backoff(self):
        for stage in ("process", "pending", "apply", "ack", "snapshot"):
            with self.subTest(stage=stage):
                self.setUp()
                collection = self.g._piglet_collection
                collection.store.pending_events.return_value = [{"event_id": "synthetic"}]
                self.g._apply_piglet_events = Mock(return_value=["synthetic"])
                callback = {"process": collection.store.process_pending,
                            "pending": collection.store.pending_events,
                            "apply": self.g._apply_piglet_events,
                            "ack": collection.store.acknowledge_events,
                            "snapshot": collection.snapshot}[stage]
                callback.side_effect = OSError("synthetic failure")
                with self.assertLogs(level="ERROR"):
                    self.g._poll_piglet_collection()
                self.assertIn("Import paused; data retained", self.g._piglet_error)
                self.assertGreater(self.g._piglet_poll_at, 0)
                self.assert_released_for_other_thread()

    def test_cancelled_snapshot_waits_for_admission_without_advancing_pacer(self):
        collection = self.g._piglet_collection
        collection.cancel_requested = True
        self.g._piglet_poll_at = 999999999999
        release, worker = self.hold(self.db_lock)
        self.poll_fast()
        self.assert_no_callbacks()
        self.assertIs(self.g._piglet_status, self.original_status)
        self.assertEqual(self.g._piglet_poll_at, 999999999999)
        release.set(); worker.join(1)
        self.g._poll_piglet_collection()
        collection.snapshot.assert_called_once_with()
        collection.store.process_pending.assert_not_called()
        self.assertEqual(self.g._piglet_poll_at, 999999999999)
        self.assert_released_for_other_thread()

    def test_cancelled_snapshot_exception_also_releases_both_locks(self):
        self.g._piglet_collection.cancel_requested = True
        self.g._piglet_collection.snapshot.side_effect = OSError("synthetic failure")
        with self.assertRaises(OSError):
            self.g._poll_piglet_collection()
        self.assert_released_for_other_thread()

    def test_old_fixtures_without_locks_and_existing_pacer_keep_their_contract(self):
        g = fixtures.game()
        g._poll_piglet_collection()
        g._piglet_collection.store.process_pending.assert_called_once()
        g._piglet_collection.snapshot.assert_called_once()
        g._poll_piglet_collection()
        g._piglet_collection.store.process_pending.assert_called_once()
        g._piglet_collection.snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
