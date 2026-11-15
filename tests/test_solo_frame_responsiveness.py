"""Real queue/lock contention around game callbacks, without RF or uploads."""

import ast
import copy
from datetime import datetime, timezone
from pathlib import Path
import queue
import random
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.app_state import AppState, Network
from watchdogs.wardrive_recorder import WardriveRecorder


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
APP = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
           and node.name == "WatchDogsGame")
NAMES = {"_poll_solo", "_finish_solo_recorder", "_yield_piglet_threads",
         "_apply_solo_observation", "_record_scan_map_observation"}
ENV = dict(time=time, random=random, Network=Network,
           pyxel=SimpleNamespace(frame_count=1))
for node in TREE.body:
    if isinstance(node, ast.ClassDef) and node.name in ("WifiNetwork", "BleDevice"):
        exec(compile(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]), str(SOURCE), "exec"), ENV)
HARNESS = ast.ClassDef(name="Game", bases=[], keywords=[], decorator_list=[],
    body=[copy.deepcopy(node) for node in APP.body
          if isinstance(node, ast.FunctionDef) and node.name in NAMES])
exec(compile(ast.fix_missing_locations(ast.Module(body=[HARNESS], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
Game = ENV["Game"]


def observation(seq=1, protocol="wifi"):
    return dict(identity=("synthetic-boot", "synthetic-session", seq),
                protocol=protocol, mac="02:00:00:00:00:01", ssid="Synthetic WiFi",
                name="Synthetic BLE", channel=6, auth="WPA2", rssi=-70,
                observed_at=float(seq), captured_at=datetime.now(timezone.utc).isoformat())


def saved(seq=1, protocol="wifi", **changes):
    event = dict(event="saved", observation=observation(seq, protocol), position=None)
    event.update(changes)
    return event


class EventRecorder:
    """The real consuming queue contract, with storage already acknowledged."""

    def __init__(self, events=()):
        self.events = queue.Queue()
        for event in events:
            self.events.put(event)
        self.tick = Mock()
        self.seal_startup = Mock(return_value=True)
        self.close = Mock(return_value=True)
        self.failed = False

    def poll_events(self, limit=32):
        events = []
        for _ in range(limit):
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                break
        return events


def game(events=()):
    value = Game()
    value.loot = SimpleNamespace(_db_lock=threading.RLock())
    value._solo = SimpleNamespace(tick=Mock(), request=Mock(), cancel=Mock(), busy=False)
    value._solo_recorder = EventRecorder(events)
    value._solo_backfill_waiting = value._solo_backfill_needs_stop = False
    value._solo_backfill_resume = None
    value._shutting_down = False
    value._solo_notice = Mock()
    value._known_wifi, value._known_ble = set(), set()
    value.wifi_networks, value.ble_devices = [], []
    value._session_scan_points = {}
    value.state = AppState()
    value.gps = SimpleNamespace(available=False)
    value.xp = 0
    value._term_add = Mock()

    def gain_xp(amount):
        value.xp += amount

    value.gain_xp = Mock(side_effect=gain_xp)
    return value


class SoloFrameResponsivenessTests(unittest.TestCase):
    def hold_from_worker(self, lock):
        entered, release = threading.Event(), threading.Event()

        def worker():
            with lock:
                entered.set()
                release.wait(3)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(1), "Fixture worker did not acquire the DB lock")
        self.addCleanup(thread.join, 1)
        self.addCleanup(release.set)
        return release, thread

    def test_busy_database_keeps_events_and_ticks_then_retries_exactly_once(self):
        g = game([saved(1), saved(2), saved(3, "ble")])
        g._solo_backfill_waiting = True
        release, holder = self.hold_from_worker(g.loot._db_lock)
        finished = threading.Event()
        result = []

        def frame():
            result.append(g._poll_solo())
            finished.set()

        ui = threading.Thread(target=frame, daemon=True)
        ui.start()
        try:
            self.assertTrue(finished.wait(.25), "A busy writer blocked the frame")
            self.assertEqual(result, [0])
            self.assertEqual(g._solo_recorder.events.qsize(), 3)
            g._solo.tick.assert_called_once_with()
            g._solo_recorder.tick.assert_called_once_with()
            g._solo_recorder.seal_startup.assert_called_once_with()
            g.gain_xp.assert_not_called()
        finally:
            release.set()
            ui.join(1)
            holder.join(1)
        self.assertFalse(holder.is_alive())
        while not g._solo_recorder.events.empty():
            self.assertGreater(g._poll_solo(), 0)
        self.assertEqual(g._poll_solo(), 0)
        self.assertEqual([call.args[0] for call in g.gain_xp.call_args_list], [15, 1, 10])
        self.assertEqual(g.xp, 26)

    def test_callbacks_can_reenter_db_lock_and_replay_does_not_repeat_xp(self):
        g = game([saved(1), saved(1, replay=True),
                  dict(event="backfill", observation=observation(1), position=None)])
        original = g._apply_solo_observation

        def callback(*args, **kwargs):
            self.assertTrue(g.loot._db_lock.acquire(blocking=False))
            try:
                return original(*args, **kwargs)
            finally:
                g.loot._db_lock.release()

        g._apply_solo_observation = callback
        with patch.object(time, "monotonic", return_value=100.0):
            self.assertEqual(g._poll_solo(), 3)
        g.gain_xp.assert_called_once_with(15)
        self.assertEqual(g.xp, 15)

    def test_one_frame_limits_event_work_and_leaves_fifo_tail_for_later(self):
        g = game([saved(i) for i in range(1, 10)])
        with patch.object(time, "monotonic", return_value=100.0):
            self.assertEqual(g._poll_solo(), 4)
            self.assertEqual(g._solo_recorder.events.qsize(), 5)
            self.assertEqual(g._poll_solo(), 4)
            self.assertEqual(g._poll_solo(), 1)
            self.assertEqual(g._poll_solo(), 0)
        self.assertEqual(g.xp, 23)
        self.assertEqual(g.gain_xp.call_count, 9)

    def test_elapsed_budget_does_not_dequeue_next_event(self):
        g = game([saved(1), saved(2)])
        original = g._apply_solo_observation
        now = [100.0]

        def callback(*args, **kwargs):
            original(*args, **kwargs)
            now[0] += .010

        g._apply_solo_observation = callback
        with patch.object(time, "monotonic", side_effect=lambda: now[0]):
            self.assertEqual(g._poll_solo(), 1)
        self.assertEqual(g._solo_recorder.events.qsize(), 1)
        self.assertEqual(g.xp, 15)

    def test_callback_exception_releases_lock_and_keeps_later_event_queued(self):
        g = game([saved(1), saved(2)])
        g._apply_solo_observation = Mock(side_effect=RuntimeError("synthetic callback failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic callback failure"):
            g._poll_solo()
        self.assertEqual(g._solo_recorder.events.qsize(), 1)
        acquired = []

        def worker():
            okay = g.loot._db_lock.acquire(timeout=.2)
            acquired.append(okay)
            if okay:
                g.loot._db_lock.release()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(1)
        self.assertEqual(acquired, [True])

    def test_final_poll_waits_for_writer_and_drains_without_frame_budget(self):
        g = game([saved(i) for i in range(1, 9)])
        release, holder = self.hold_from_worker(g.loot._db_lock)
        finished = threading.Event()
        result = []

        def final_poll():
            result.append(g._poll_solo(final=True))
            finished.set()

        ui = threading.Thread(target=final_poll, daemon=True)
        ui.start()
        try:
            self.assertFalse(finished.wait(.03))
            self.assertEqual(g._solo_recorder.events.qsize(), 8)
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        ui.join(1)
        holder.join(1)
        self.assertEqual(result, [8])
        self.assertEqual(g.xp, 22)
        g._solo.tick.assert_not_called()
        g._solo_recorder.tick.assert_not_called()

    def test_shutdown_drains_every_saved_event_and_never_resumes_rf(self):
        g = game([saved(i) for i in range(1, 35)] + [dict(event="backfill_done")])
        g._shutting_down = True
        g._solo_backfill_resume = ("full", True, True)
        self.assertTrue(g._finish_solo_recorder())
        self.assertTrue(g._solo_recorder.events.empty())
        self.assertEqual(g.gain_xp.call_count, 34)
        self.assertEqual(g.xp, 48)
        g._solo.request.assert_not_called()
        g._solo.tick.assert_not_called()

    def test_yield_is_once_for_active_or_pending_solo_and_absent_when_idle(self):
        g = game()
        g._piglet_collection = None
        for busy, pending, expected in ((False, False, 0), (True, False, 1),
                                        (False, True, 1), (True, True, 1)):
            with self.subTest(busy=busy, pending=pending), patch.object(time, "sleep") as pause:
                g._solo.busy = busy
                g._solo_recorder.work_pending = pending
                g._yield_piglet_threads()
                self.assertEqual(pause.call_count, expected)
                if expected:
                    pause.assert_called_once_with(.001)
        g._piglet_collection = object()
        g._piglet_status = dict(state="active")
        with patch.object(time, "sleep") as pause:
            g._yield_piglet_threads()
            pause.assert_called_once_with(.001)


class RecorderWorkPendingTests(unittest.TestCase):
    def test_real_worker_reports_initialising_queued_inflight_and_unpresented(self):
        with tempfile.TemporaryDirectory() as directory:
            initialising, initialise = threading.Event(), threading.Event()
            writing, finish_write = threading.Event(), threading.Event()

            def init(recorder):
                initialising.set()
                initialise.wait(2)
                recorder._initialised = True

            def raw(recorder, job):
                writing.set()
                finish_write.wait(2)
                recorder._emit(dict(event="saved", observation=job["observation"]))

            with patch.object(WardriveRecorder, "_initialise", init), \
                    patch.object(WardriveRecorder, "_raw", raw), \
                    patch.object(WardriveRecorder, "_close_files", lambda _self: None):
                recorder = WardriveRecorder(SimpleNamespace(session_dir=directory, active=True))
                try:
                    self.assertTrue(initialising.wait(1))
                    self.assertTrue(recorder.work_pending)
                    with self.assertRaises(AttributeError):
                        recorder.work_pending = False
                    initialise.set()
                    with recorder._condition:
                        self.assertTrue(recorder._condition.wait_for(lambda: recorder._initialised, 1))
                    self.assertFalse(recorder.work_pending)
                    self.assertTrue(recorder.submit(observation(1)))
                    self.assertTrue(writing.wait(1))
                    self.assertTrue(recorder._jobs.empty())
                    self.assertTrue(recorder.work_pending, "In-flight work is not an empty queue")
                    self.assertTrue(recorder.submit(observation(2)))
                    self.assertEqual(recorder._jobs.qsize(), 1)
                    self.assertTrue(recorder.work_pending)
                    finish_write.set()
                    with recorder._condition:
                        self.assertTrue(recorder._condition.wait_for(
                            lambda: recorder._jobs.unfinished_tasks == 0, 1))
                    self.assertTrue(recorder.work_pending, "Saved presentation is still pending")
                    self.assertEqual(len(recorder.poll_events()), 2)
                    self.assertFalse(recorder.work_pending)
                    self.assertTrue(recorder.close(1))
                    self.assertFalse(recorder.work_pending)
                finally:
                    initialise.set()
                    finish_write.set()
                    recorder.poll_events(1024)
                    recorder.close(1)


if __name__ == "__main__":
    unittest.main()
