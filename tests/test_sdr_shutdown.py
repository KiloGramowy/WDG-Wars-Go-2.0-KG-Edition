"""Owned SDR process/reader shutdown; no radio programs or hardware are opened."""
import io
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from watchdogs.sdr_manager import SDRManager


class Child:
    def __init__(self, pid=10001, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.stdout = self.stderr = None
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake owned child", timeout)
        return self.returncode


class StuckReader:
    def __init__(self):
        self.alive = True
        self.joins = []

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.joins.append(timeout)


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.manager = SDRManager()

    def release(self, proc, *, kill=False):
        proc.returncode = -9 if kill else -15

    def test_reaps_all_owned_children_and_is_idempotent(self):
        children = [Child(10001 + i) for i in range(3)]
        names = ("_dump1090_proc", "_rtl433_proc", "_emerg_proc")
        for name, proc in zip(names, children):
            setattr(self.manager, name, proc)
        unrelated = Child(20001)
        self.manager.running = True
        self.manager.mode = "adsb+433+emergency"
        log = self.manager._adsb_log = io.StringIO()
        with patch.object(self.manager, "_signal_owned_process", side_effect=self.release) as signal:
            self.assertIs(self.manager.stop(), True)
            self.assertEqual([call.args[0] for call in signal.call_args_list], children)
            self.assertIs(self.manager.stop(), True)
            self.assertEqual(signal.call_count, 3)
        self.assertIsNone(unrelated.returncode)
        self.assertTrue(all(proc.waits for proc in children))
        self.assertTrue(all(getattr(self.manager, name) is None for name in names))
        self.assertTrue(log.closed)
        self.assertFalse(self.manager.running)
        self.assertEqual(self.manager.mode, "")

    def test_already_exited_child_is_reaped_without_signal(self):
        proc = self.manager._rtl433_proc = Child(returncode=0)
        with patch.object(self.manager, "_signal_owned_process") as signal:
            self.assertTrue(self.manager.stop())
        signal.assert_not_called()
        self.assertEqual(proc.waits, [0])

    def test_clean_stop_allows_explicit_restart_without_changing_scan_command(self):
        self.assertTrue(self.manager.stop())
        proc = Child()
        with patch.object(self.manager, "has_rtl433", return_value=True):
            with patch("watchdogs.sdr_manager.subprocess.Popen", return_value=proc) as launch:
                self.assertTrue(self.manager.start_433())
        from watchdogs.sdr_manager import RTL433_CMD
        self.assertEqual(launch.call_args.args[0], RTL433_CMD)
        self.assertTrue(self.manager.running)
        self.assertEqual(self.manager.mode, "433")
        with patch.object(self.manager, "_signal_owned_process", side_effect=self.release):
            self.assertTrue(self.manager.stop())

    def test_log_close_cannot_admit_a_restart_mid_shutdown(self):
        attempted = []
        self.manager._adsb_log = Mock()
        self.manager._adsb_log.close.side_effect = lambda: attempted.append(self.manager.start_emergency())
        with patch.object(self.manager, "has_rtl_power", return_value=True):
            with patch("watchdogs.sdr_manager.subprocess.Popen") as launch:
                self.assertTrue(self.manager.stop())
        self.assertEqual(attempted, [False])
        launch.assert_not_called()

    def test_ignored_term_is_killed_and_waited_for(self):
        proc = self.manager._dump1090_proc = Child()
        def signal(child, *, kill=False):
            if kill:
                self.release(child, kill=True)
        with patch.object(self.manager, "_signal_owned_process", side_effect=signal) as send:
            self.assertTrue(self.manager.stop())
        self.assertEqual([call.kwargs for call in send.call_args_list], [{}, {"kill": True}])
        self.assertEqual(len(proc.waits), 2)
        self.assertLessEqual(proc.waits[0], 0.5)
        self.assertLessEqual(proc.waits[1], 5)
        self.assertIsNone(self.manager._dump1090_proc)

    def test_unreleased_process_keeps_ownership_and_open_logs_for_retry(self):
        proc = self.manager._rtl433_proc = Child()
        log = self.manager._sensor_log = io.StringIO()
        self.manager.running, self.manager.mode = True, "433"
        with patch.object(self.manager, "_signal_owned_process", side_effect=PermissionError("denied")):
            with self.assertLogs("watchdogs.sdr_manager", level="WARNING"):
                self.assertIs(self.manager.stop(), False)
        self.assertIs(self.manager._rtl433_proc, proc)
        self.assertFalse(log.closed)
        self.assertTrue(self.manager.running)
        self.assertEqual(self.manager.mode, "433")
        with patch.object(self.manager, "_signal_owned_process", side_effect=self.release):
            self.assertIs(self.manager.stop(), True)
        self.assertTrue(log.closed)

    def test_reader_join_finishes_before_logs_close_without_holding_lock(self):
        log = self.manager._sensor_log = io.StringIO()
        ready, finished = threading.Event(), threading.Event()
        observations = []
        def reader():
            ready.set()
            self.manager._stop_event.wait(2)
            with self.manager._lifecycle_lock:
                observations.append(log.closed)
                log.write("last observation\n")
            finished.set()
        thread = threading.Thread(target=reader, daemon=True)
        self.manager._threads.append(thread)
        thread.start()
        self.assertTrue(ready.wait(2))
        self.assertTrue(self.manager.stop())
        self.assertTrue(finished.is_set())
        self.assertEqual(observations, [False])
        self.assertTrue(log.closed)
        self.assertFalse(thread.is_alive())

    def test_stuck_reader_is_not_reported_stopped_and_prevents_restart(self):
        reader = StuckReader()
        self.manager._threads.append(reader)
        log = self.manager._adsb_log = io.StringIO()
        self.manager.running, self.manager.mode = True, "adsb"
        self.assertIs(self.manager.stop(), False)
        self.assertFalse(log.closed)
        self.assertTrue(reader.joins)
        self.assertTrue(all(0 <= timeout <= 5 for timeout in reader.joins))
        with patch.object(self.manager, "has_rtl_power", return_value=True):
            with patch("watchdogs.sdr_manager.subprocess.Popen") as launch:
                self.assertIs(self.manager.start_emergency(), False)
        launch.assert_not_called()
        self.assertTrue(self.manager._stop_event.is_set())
        reader.alive = False
        self.assertIs(self.manager.stop(), True)
        self.assertTrue(log.closed)

    def test_emergency_launch_after_stop_snapshot_is_cancelled(self):
        before_launch, continue_launch = threading.Event(), threading.Event()
        original = self.manager._spawn_owned
        result = []
        def paused_launch(*args, **kwargs):
            before_launch.set()
            continue_launch.wait(2)
            return original(*args, **kwargs)
        worker = threading.Thread(target=lambda: self.manager._rtl_power_peak(1, 2, 1), daemon=True)
        self.manager._threads.append(worker)
        with patch.object(self.manager, "_spawn_owned", side_effect=paused_launch):
            with patch("watchdogs.sdr_manager.subprocess.Popen") as launch:
                worker.start()
                self.assertTrue(before_launch.wait(2))
                stopper = threading.Thread(target=lambda: result.append(self.manager.stop()), daemon=True)
                stopper.start()
                self.assertTrue(self.manager._stop_event.wait(2))
                continue_launch.set()
                stopper.join(2)
                worker.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        launch.assert_not_called()
        self.assertIsNone(self.manager._emerg_proc)

    def test_new_start_cannot_clear_stop_event_before_stop_acquires_lock(self):
        real_lock = self.manager._lifecycle_lock
        attempted = []
        manager = self.manager
        class LockProbe:
            def acquire(self, *args, **kwargs):
                attempted.append(manager.start_emergency())
                return real_lock.acquire(*args, **kwargs)

            def release(self):
                real_lock.release()

            def __enter__(self):
                real_lock.acquire()
                return self

            def __exit__(self, *args):
                real_lock.release()
        self.manager._lifecycle_lock = LockProbe()
        with patch.object(self.manager, "has_rtl_power", return_value=True):
            with patch("watchdogs.sdr_manager.subprocess.Popen") as launch:
                self.assertTrue(self.manager.stop())
        self.assertEqual(attempted, [False])
        self.assertTrue(self.manager._stop_event.is_set())
        launch.assert_not_called()

    def test_timeout_kills_and_reaps_emergency_child_before_forgetting_it(self):
        proc = Child()
        with patch("watchdogs.sdr_manager.subprocess.Popen", return_value=proc) as launch:
            with patch.object(self.manager, "_signal_owned_process", side_effect=self.release):
                self.assertIsNone(self.manager._rtl_power_peak(1, 2, 1))
        self.assertEqual(proc.waits[0], 8)
        self.assertGreaterEqual(len(proc.waits), 2)
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(self.manager._emerg_proc)
        self.assertTrue(launch.call_args.kwargs["start_new_session"])

    def test_failed_timeout_cleanup_retains_child_for_later_stop(self):
        proc = Child()
        with patch("watchdogs.sdr_manager.subprocess.Popen", return_value=proc):
            with patch.object(self.manager, "_signal_owned_process", side_effect=PermissionError("denied")):
                with self.assertLogs("watchdogs.sdr_manager", level="WARNING"):
                    self.assertIsNone(self.manager._rtl_power_peak(1, 2, 1))
        self.assertIs(self.manager._emerg_proc, proc)
        with patch.object(self.manager, "_signal_owned_process", side_effect=self.release):
            self.assertTrue(self.manager.stop())

    def test_sbs_initial_wait_is_interruptible_without_opening_socket(self):
        thread = threading.Thread(target=self.manager._sbs_reader, daemon=True)
        self.manager._threads.append(thread)
        with patch("socket.socket") as socket:
            thread.start()
            self.assertTrue(self.manager.stop())
        self.assertFalse(thread.is_alive())
        socket.assert_not_called()

    def test_pending_adsb_start_cannot_spawn_after_stop_was_requested(self):
        in_service, release_service = threading.Event(), threading.Event()
        result = []
        def service(*args, **kwargs):
            in_service.set()
            release_service.wait(2)
        starter = threading.Thread(target=lambda: result.append(self.manager.start_adsb()), daemon=True)
        with patch.object(self.manager, "has_dump1090", return_value=True):
            with patch("watchdogs.sdr_manager.subprocess.run", side_effect=service):
                with patch("watchdogs.sdr_manager.subprocess.Popen") as launch:
                    starter.start()
                    self.assertTrue(in_service.wait(2))
                    self.assertIs(self.manager.stop(), False)
                    release_service.set()
                    starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertEqual(result, [False])
        launch.assert_not_called()
        self.assertTrue(self.manager.stop())

    def test_stop_reaps_a_real_harmless_owned_child(self):
        # No SDR executable, USB, network connection or RF is involved.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)
        self.addCleanup(cleanup)
        self.manager._dump1090_proc = proc
        self.assertTrue(self.manager.stop())
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(self.manager._dump1090_proc)


if __name__ == "__main__":
    unittest.main()
