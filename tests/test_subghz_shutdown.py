"""Pending Monster connector/probe shutdown, using only fake serial devices."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs import subghz_manager

Thread = threading.Thread  # Reader factory is patched independently below.


class Port:
    def __init__(self, opening=None, resume=None):
        self.opening, self.resume = opening, resume
        self.opened = self.closed = False
        self.closes = 0
        self.writes = []

    def open(self):
        if self.opening:
            self.opening.set()
            if not self.resume.wait(2):
                raise RuntimeError("test open was not released")
        self.opened = True

    def close(self):
        self.closed = True
        self.closes += 1

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)

    def cancel_read(self):
        pass

    def reset_input_buffer(self):
        pass

    def read(self, size):
        return b"[SUBGHZ_STATUS] mode=idle\n"


class Reader:
    def __init__(self, *args, **kwargs):
        self.alive = False

    def start(self):
        self.alive = True

    def join(self, timeout=None):
        self.alive = False

    def is_alive(self):
        return self.alive


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.manager = subghz_manager.SubGhzManager()

    def join(self, thread):
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_pending_find_cannot_open_or_publish_after_shutdown(self):
        finding, resume = threading.Event(), threading.Event()
        results = []
        def find(*args, **kwargs):
            finding.set()
            resume.wait(2)
            return "/dev/fake-monster"
        worker = Thread(target=lambda: results.append(self.manager.connect()), daemon=True)
        with patch.object(self.manager, "find_port", side_effect=find):
            with patch.object(subghz_manager, "serial", SimpleNamespace(Serial=Mock())) as serial:
                worker.start()
                self.assertTrue(finding.wait(2))
                self.assertTrue(self.manager.shutdown())
                resume.set()
                self.join(worker)
                serial.Serial.assert_not_called()
        self.assertEqual(results, [False])
        self.assertIsNone(self.manager._ser)
        self.assertIsNone(self.manager._reader)

    def test_pending_open_closes_private_fd_and_never_starts_reader(self):
        opening, resume = threading.Event(), threading.Event()
        port = Port(opening, resume)
        results = []
        worker = Thread(target=lambda: results.append(self.manager.connect("/dev/fake-monster")), daemon=True)
        with patch.object(subghz_manager, "serial", SimpleNamespace(Serial=Mock(return_value=port))):
            with patch.object(subghz_manager.threading, "Thread", return_value=Reader()) as reader:
                worker.start()
                self.assertTrue(opening.wait(2))
                self.assertTrue(self.manager.shutdown())
                resume.set()
                self.join(worker)
        self.assertEqual(results, [False])
        self.assertTrue(port.closed)
        self.assertEqual(port.closes, 1)
        self.assertEqual(port.writes, [])
        reader.assert_not_called()
        self.assertFalse(self.manager.connected)
        self.assertIsNone(self.manager._ser)

    def test_pending_probe_open_closes_its_private_fd_without_status_write(self):
        opening, resume = threading.Event(), threading.Event()
        port = Port(opening, resume)
        results = []
        worker = Thread(target=lambda: results.append(self.manager.find_port()), daemon=True)
        with patch.object(subghz_manager, "serial", SimpleNamespace(Serial=Mock(return_value=port))):
            with patch.object(subghz_manager.glob, "glob", return_value=["/dev/fake-monster"]):
                worker.start()
                self.assertTrue(opening.wait(2))
                self.assertTrue(self.manager.shutdown())
                resume.set()
                self.join(worker)
        self.assertEqual(results, [None])
        self.assertTrue(port.closed)
        self.assertEqual(port.closes, 1)
        self.assertEqual(port.writes, [])

    def test_shutdown_before_connect_or_probe_permanently_rejects_both(self):
        self.assertTrue(self.manager.shutdown())
        with patch.object(subghz_manager, "serial", SimpleNamespace(Serial=Mock())) as serial:
            self.assertFalse(self.manager.connect("/dev/fake-monster"))
            self.assertIsNone(self.manager.find_port())
        serial.Serial.assert_not_called()

    def test_ordinary_stop_still_allows_explicit_reconnect(self):
        first, second = Port(), Port()
        with patch.object(subghz_manager, "serial", SimpleNamespace(Serial=Mock(side_effect=[first, second]))):
            with patch.object(subghz_manager.threading, "Thread", side_effect=Reader):
                with patch.object(self.manager._stop, "wait", return_value=False):
                    self.assertTrue(self.manager.connect("/dev/fake-one"))
                    self.assertTrue(self.manager.stop())
                    self.assertTrue(self.manager.connect("/dev/fake-two"))
                    self.assertFalse(self.manager._closing.is_set())
                    self.assertTrue(self.manager.shutdown())
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(first.writes, [b"subghz_status\r\n", b"subghz_stop\r\n"])
        self.assertEqual(second.writes, first.writes)

    def test_shutdown_after_publication_closes_registered_reader_and_port(self):
        port, reader = Port(), Reader()
        reader.start()
        self.manager._ser = port
        self.manager._reader = reader
        self.manager.connected = True
        self.assertTrue(self.manager.shutdown())
        self.assertTrue(port.closed)
        self.assertEqual(port.writes, [b"subghz_stop\r\n"])
        self.assertFalse(reader.is_alive())
        self.assertIsNone(self.manager._ser)
        self.assertIsNone(self.manager._reader)
        self.manager.send("subghz_rx")
        self.assertEqual(port.writes, [b"subghz_stop\r\n"])

    def test_reader_start_failure_closes_unpublished_port(self):
        port = Port()
        reader = Mock()
        reader.start.side_effect = RuntimeError("no thread available")
        with patch.object(subghz_manager, "serial", SimpleNamespace(Serial=Mock(return_value=port))):
            with patch.object(subghz_manager.threading, "Thread", return_value=reader):
                self.assertFalse(self.manager.connect("/dev/fake-monster"))
        self.assertTrue(port.closed)
        self.assertFalse(self.manager.connected)
        self.assertIsNone(self.manager._ser)
        self.assertIsNone(self.manager._reader)


if __name__ == "__main__":
    unittest.main()
