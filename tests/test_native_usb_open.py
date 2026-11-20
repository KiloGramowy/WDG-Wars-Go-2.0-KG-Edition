"""Model CDC-ACM open and C5 control-line actions, without USB/device I/O."""
import unittest
from unittest.mock import patch

from watchdogs import esp32_flash as flash


class NativeUSBModel:
    """Public Serial properties plus Linux's pre-userspace assertion.

    States use (RTS, DTR), matching C5 TRM table 37.3-2. Applying 10 requests
    reset; 01 sets the download flag; 00 clears it; 11 leaves it alone.
    """
    def __init__(self, *, linux=True, fail_at=None):
        self.linux = linux
        self.fail_at = fail_at
        self.failure = OSError("synthetic native USB failure")
        self.is_open = False
        self._dtr = self._rts = True
        self.state = (False, False)
        self.download = False
        self.resets = 0
        self.events = []
        self.states = []

    def record(self, event):
        self.events.append(event)
        if event == self.fail_at:
            raise self.failure

    def apply(self, state):
        self.state = state
        if state == (True, False):
            self.resets += 1
        elif state == (False, True):
            self.download = True
        elif state == (False, False):
            self.download = False
        self.states.append((state, self.download, self.resets))

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self.record(("dtr", value, self.is_open))
        self._dtr = value
        if self.is_open:
            self.apply((self.state[0], value))

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self.record(("rts", value, self.is_open))
        self._rts = value
        if self.is_open:
            self.apply((value, self.state[1]))

    def open(self):
        self.record(("open",))
        self.is_open = True
        if self.linux:
            # tty_port_open raises both lines before os.open returns.
            self.apply((True, True))
        # pyserial POSIX applies stored DTR, then stored RTS, after os.open.
        self.apply((self.state[0], self._dtr))
        self.apply((self._rts, self.state[1]))

    def close(self):
        raise AssertionError("Opening helper must leave handle ownership to caller")

    def write(self, *args):
        raise AssertionError("Opening helper must not send commands")

    def read(self, *args):
        raise AssertionError("Opening helper must not probe firmware")


class NativeUSBOpenTests(unittest.TestCase):
    def test_previous_linux_sequence_reproduces_unintended_c5_reset(self):
        connection = NativeUSBModel()
        connection.dtr = False
        connection.rts = False
        connection.open()
        self.assertEqual(connection.resets, 1)
        self.assertIn(((True, False), False, 1), connection.states)
        self.assertEqual(connection.state, (False, False))

    def test_linux_open_clears_rts_first_without_reset_or_sticky_download(self):
        connection = NativeUSBModel()
        with patch.object(flash.sys, "platform", "linux"):
            self.assertIsNone(flash.open_native_usb_serial(connection))
        self.assertEqual(connection.events, [
            ("dtr", True, False), ("rts", True, False), ("open",),
            ("rts", False, True), ("dtr", False, True)])
        self.assertEqual(connection.states, [
            ((True, True), False, 0), ((True, True), False, 0),
            ((True, True), False, 0), ((False, True), True, 0),
            ((False, False), False, 0)])
        self.assertTrue(connection.is_open)
        self.assertFalse(connection.dtr)
        self.assertFalse(connection.rts)
        self.assertFalse(connection.download)
        self.assertEqual(connection.resets, 0)

    def test_nonlinux_preserves_deasserted_before_open_sequence(self):
        for platform in ("win32", "darwin", "freebsd14"):
            with self.subTest(platform=platform):
                connection = NativeUSBModel(linux=False)
                with patch.object(flash.sys, "platform", platform):
                    flash.open_native_usb_serial(connection)
                self.assertEqual(connection.events, [
                    ("dtr", False, False), ("rts", False, False), ("open",)])
                self.assertEqual(connection.resets, 0)
                self.assertFalse(connection.download)
                self.assertTrue(connection.is_open)

    def test_linux_failures_propagate_without_fallback_close_or_later_actions(self):
        expected = [("dtr", True, False), ("rts", True, False), ("open",),
                    ("rts", False, True), ("dtr", False, True)]
        for index, event in enumerate(expected):
            with self.subTest(fail_at=event):
                connection = NativeUSBModel(fail_at=event)
                with patch.object(flash.sys, "platform", "linux"):
                    with self.assertRaises(OSError) as caught:
                        flash.open_native_usb_serial(connection)
                self.assertIs(caught.exception, connection.failure)
                self.assertEqual(connection.events, expected[:index + 1])
                self.assertEqual(connection.is_open, index > 2)
                self.assertEqual(connection.resets, 0)

    def test_windows_failures_propagate_without_postopen_line_changes(self):
        expected = [("dtr", False, False), ("rts", False, False), ("open",)]
        for index, event in enumerate(expected):
            with self.subTest(fail_at=event):
                connection = NativeUSBModel(linux=False, fail_at=event)
                with patch.object(flash.sys, "platform", "win32"):
                    with self.assertRaises(OSError) as caught:
                        flash.open_native_usb_serial(connection)
                self.assertIs(caught.exception, connection.failure)
                self.assertEqual(connection.events, expected[:index + 1])
                self.assertFalse(connection.is_open)


if __name__ == "__main__":
    unittest.main()
