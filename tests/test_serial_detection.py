"""Serial selection and send results with synthetic USB/serial devices only."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs import serial_manager
from watchdogs.serial_manager import SerialManager, detect_esp32_port


def usb_port(device, vid, pid, description="synthetic device"):
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


KEYBOARD = (0x1EAF, 0x0024)
ESPRESSIF = (0x303A, 0x1001)
CH340 = (0x1A86, 0x7523)


class SerialDetectionTests(unittest.TestCase):
    def detect(self, ports):
        with patch.object(serial_manager.serial.tools.list_ports, "comports", return_value=ports):
            return detect_esp32_port()

    def test_cold_boot_waits_for_esp32_after_keyboard(self):
        keyboard = usb_port("/dev/ttyACM0", *KEYBOARD, "ClockworkPI controller")
        esp = usb_port("/dev/ttyACM1", *ESPRESSIF, "ESP32 native USB")
        with patch.object(serial_manager.serial.tools.list_ports, "comports",
                          side_effect=[[keyboard], [keyboard, esp]]):
            # Device nodes existing before VID/PID enumeration must not bypass
            # selection. The next detection chooses the newly enumerated ESP.
            with patch.object(serial_manager.os.path, "exists", return_value=True):
                self.assertIsNone(detect_esp32_port())
                self.assertEqual(detect_esp32_port(), "/dev/ttyACM1")

    def test_enumeration_order_does_not_select_keyboard(self):
        for esp_device, keyboard_device in (("/dev/ttyACM0", "/dev/ttyACM1"),
                                            ("/dev/ttyACM1", "/dev/ttyACM0")):
            esp = usb_port(esp_device, *ESPRESSIF)
            keyboard = usb_port(keyboard_device, *KEYBOARD)
            for ports in ([keyboard, esp], [esp, keyboard]):
                with self.subTest(esp=esp_device, ports=[p.device for p in ports]):
                    self.assertEqual(self.detect(ports), esp_device)

    def test_native_esp32_wins_over_monster_rf_bridge(self):
        monster = usb_port("/dev/ttyUSB0", *CH340, "Monster RF CH340")
        esp = usb_port("/dev/ttyACM1", *ESPRESSIF, "ESP32 native USB")
        for ports in ([monster, esp], [esp, monster]):
            with self.subTest(ports=[p.device for p in ports]):
                self.assertEqual(self.detect(ports), esp.device)

    def test_supported_uart_bridges_remain_candidates(self):
        for vid_pid in ((0x10C4, 0xEA60), CH340, (0x1A86, 0x55D4),
                        (0x0403, 0x6001), (0x0403, 0x6015)):
            with self.subTest(vid_pid=vid_pid):
                self.assertEqual(self.detect([usb_port("/dev/ttyUSB2", *vid_pid)]),
                                 "/dev/ttyUSB2")

    def test_supported_native_usb_ids_remain_candidates(self):
        for vid_pid in (ESPRESSIF, (0x303A, 0x4001)):
            with self.subTest(vid_pid=vid_pid):
                self.assertEqual(self.detect([usb_port("/dev/ttyACM2", *vid_pid)]),
                                 "/dev/ttyACM2")

    def test_unknown_and_missing_identifiers_never_fall_back_to_existing_paths(self):
        cases = ([usb_port("/dev/ttyACM0", *KEYBOARD)],
                 [usb_port("/dev/ttyUSB0", 0x1234, 0x5678)],
                 [usb_port("/dev/ttyACM0", None, None)], [])
        for ports in cases:
            with self.subTest(ports=ports):
                with patch.object(serial_manager.os.path, "exists", return_value=True):
                    self.assertIsNone(self.detect(ports))

    def test_port_listing_failure_does_not_select_existing_unknown_port(self):
        with patch.object(serial_manager.serial.tools.list_ports, "comports",
                          side_effect=OSError("USB enumeration unavailable")):
            with patch.object(serial_manager.os.path, "exists", return_value=True):
                self.assertIsNone(detect_esp32_port())

    def test_multiple_known_ports_have_deterministic_selection(self):
        first = usb_port("/dev/ttyACM1", *ESPRESSIF)
        second = usb_port("/dev/ttyACM2", *ESPRESSIF)
        self.assertEqual(self.detect([second, first]), first.device)

    def test_explicit_pty_setup_is_not_filtered_by_usb_detection(self):
        manager = SerialManager("/dev/pts/7")
        connection = Mock(is_open=True)
        with patch.object(serial_manager.os.path, "exists", return_value=True), \
                patch.object(serial_manager.os, "access", return_value=True), \
                patch.object(serial_manager.serial, "Serial", return_value=connection) as constructor, \
                patch.object(serial_manager, "detect_esp32_port") as autodetect:
            manager.setup()
        self.assertEqual(constructor.call_args.kwargs["port"], "/dev/pts/7")
        self.assertIs(manager.serial_conn, connection)
        autodetect.assert_not_called()
        connection.reset_input_buffer.assert_called_once_with()
        connection.reset_output_buffer.assert_called_once_with()


class SerialSendResultTests(unittest.TestCase):
    def setUp(self):
        self.manager = SerialManager("/dev/ttyACM1")
        self.connection = Mock(is_open=True)
        self.manager.serial_conn = self.connection

    def test_complete_command_returns_true_after_flush(self):
        payload = b"scan_networks\r\n"
        self.connection.write.return_value = len(payload)
        with patch.object(serial_manager.time, "sleep") as sleep:
            self.assertIs(self.manager.send_command("scan_networks"), True)
        self.connection.write.assert_called_once_with(payload)
        self.connection.flush.assert_called_once_with()
        sleep.assert_called_once_with(0.1)

    def test_write_length_counts_encoded_bytes(self):
        command = "name J\N{LATIN SMALL LETTER U WITH ACUTE}nior"
        payload = (command + "\r\n").encode("utf-8")
        self.connection.write.return_value = len(payload)
        with patch.object(serial_manager.time, "sleep"):
            self.assertIs(self.manager.send_command(command), True)
        self.connection.write.assert_called_once_with(payload)

    def test_missing_connection_returns_false(self):
        self.manager.serial_conn = None
        with self.assertLogs(serial_manager.log, level="ERROR"):
            self.assertIs(self.manager.send_command("scan_networks"), False)
        self.connection.write.assert_not_called()

    def test_closed_connection_returns_false_without_writing(self):
        self.connection.is_open = False
        with self.assertLogs(serial_manager.log, level="ERROR"):
            self.assertIs(self.manager.send_command("scan_bt"), False)
        self.connection.write.assert_not_called()
        self.connection.flush.assert_not_called()

    def test_write_exception_returns_false(self):
        self.connection.write.side_effect = OSError("USB disconnected")
        with self.assertLogs(serial_manager.log, level="ERROR"):
            self.assertIs(self.manager.send_command("scan_networks"), False)
        self.connection.flush.assert_not_called()

    def test_write_timeout_returns_false(self):
        self.connection.write.side_effect = serial_manager.serial.SerialTimeoutException("timeout")
        with self.assertLogs(serial_manager.log, level="ERROR"):
            self.assertIs(self.manager.send_command("start_sniffer"), False)
        self.connection.flush.assert_not_called()

    def test_short_or_missing_write_count_is_failure(self):
        for written in (0, 1, None):
            with self.subTest(written=written):
                self.connection.reset_mock()
                self.connection.write.return_value = written
                with self.assertLogs(serial_manager.log, level="ERROR"):
                    with patch.object(serial_manager.time, "sleep") as sleep:
                        self.assertIs(self.manager.send_command("scan_bt"), False)
                self.connection.flush.assert_not_called()
                sleep.assert_not_called()

    def test_flush_exception_is_not_reported_as_success(self):
        self.connection.write.return_value = len(b"scan_bt\r\n")
        self.connection.flush.side_effect = OSError("device disappeared")
        with self.assertLogs(serial_manager.log, level="ERROR"):
            with patch.object(serial_manager.time, "sleep") as sleep:
                self.assertIs(self.manager.send_command("scan_bt"), False)
        sleep.assert_not_called()


class SerialShutdownStopTests(unittest.TestCase):
    def setUp(self):
        self.manager = SerialManager("/dev/ttyACM1")
        self.connection = Mock(is_open=True, write_timeout=None)
        self.connection.write.return_value = len(b"stop\r\n")
        self.manager.serial_conn = self.connection

    def test_complete_stop_uses_bounded_write_without_flush_or_other_io(self):
        observed = []
        self.connection.write.side_effect = lambda data: observed.append(
            self.connection.write_timeout) or len(data)
        with patch.object(self.manager, "setup") as setup, \
                patch.object(self.manager, "probe") as probe, \
                patch.object(self.manager, "send_command") as send, \
                patch.object(serial_manager.time, "sleep") as sleep:
            self.assertTrue(self.manager.request_stop_for_shutdown())
        self.assertEqual(observed, [1.0])
        self.assertIsNone(self.connection.write_timeout)
        self.assertEqual(self.connection.mock_calls, [unittest.mock.call.write(b"stop\r\n")])
        setup.assert_not_called()
        probe.assert_not_called()
        send.assert_not_called()
        sleep.assert_not_called()

    def test_custom_timeout_and_previous_setting_are_preserved(self):
        self.connection.write_timeout = 2.5
        observed = []
        self.connection.write.side_effect = lambda data: observed.append(
            self.connection.write_timeout) or len(data)
        self.assertTrue(self.manager.request_stop_for_shutdown(timeout=0.25))
        self.assertEqual(observed, [0.25])
        self.assertEqual(self.connection.write_timeout, 2.5)

    def test_missing_or_closed_port_is_not_opened(self):
        with patch.object(self.manager, "setup") as setup:
            self.manager.serial_conn = None
            self.assertFalse(self.manager.request_stop_for_shutdown())
            self.manager.serial_conn = self.connection
            self.connection.is_open = False
            self.assertFalse(self.manager.request_stop_for_shutdown())
        setup.assert_not_called()
        self.connection.write.assert_not_called()
        self.connection.flush.assert_not_called()

    def test_short_write_is_failure_and_restores_timeout(self):
        for length in (0, 1, 5, None):
            with self.subTest(length=length):
                self.connection.write.return_value = length
                self.assertFalse(self.manager.request_stop_for_shutdown())
                self.assertIsNone(self.connection.write_timeout)
        self.connection.flush.assert_not_called()

    def test_write_exception_or_timeout_restores_setting_without_drain(self):
        for error in (OSError("disconnected"), serial_manager.serial.SerialTimeoutException("timeout")):
            with self.subTest(error=type(error).__name__):
                self.connection.write_timeout = 3.0
                self.connection.write.side_effect = error
                self.assertFalse(self.manager.request_stop_for_shutdown(timeout=0.1))
                self.assertEqual(self.connection.write_timeout, 3.0)
        self.connection.flush.assert_not_called()
        self.connection.reset_output_buffer.assert_not_called()
        self.connection.close.assert_not_called()

    def test_interrupted_write_still_restores_timeout(self):
        self.connection.write_timeout = 2.0
        self.connection.write.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.manager.request_stop_for_shutdown()
        self.assertEqual(self.connection.write_timeout, 2.0)
        self.connection.flush.assert_not_called()

    def test_timeout_configuration_failure_prevents_write_and_attempts_restore(self):
        class Connection:
            is_open = True
            write = Mock()
            changes = []
            @property
            def write_timeout(self):
                return 2.0
            @write_timeout.setter
            def write_timeout(self, value):
                self.changes.append(value)
                if value == 1.0:
                    raise OSError("cannot configure timeout")
        connection = Connection()
        self.manager.serial_conn = connection
        self.assertFalse(self.manager.request_stop_for_shutdown())
        self.assertEqual(connection.changes, [1.0, 2.0])
        connection.write.assert_not_called()

    def test_unbounded_or_invalid_timeout_never_writes(self):
        for timeout in (None, float("inf"), float("nan"), -1, True, "1"):
            with self.subTest(timeout=timeout):
                self.assertFalse(self.manager.request_stop_for_shutdown(timeout=timeout))
        self.connection.write.assert_not_called()


class SerialCloseTests(unittest.TestCase):
    def setUp(self):
        self.manager = SerialManager("/dev/ttyACM1")
        self.connection = Mock(is_open=True)
        self.manager.serial_conn = self.connection

    def test_discards_pending_output_before_close(self):
        self.manager.close()
        self.assertEqual([entry[0] for entry in self.connection.mock_calls],
                         ["reset_output_buffer", "close"])
        self.assertIsNone(self.manager.serial_conn)

    def test_discard_failure_still_closes_and_clears_reference(self):
        self.connection.reset_output_buffer.side_effect = OSError("USB removed")
        with self.assertLogs(serial_manager.log, level="WARNING"):
            self.manager.close()
        self.connection.close.assert_called_once_with()
        self.assertIsNone(self.manager.serial_conn)

    def test_close_failure_still_clears_reference(self):
        self.connection.close.side_effect = OSError("close failed")
        with self.assertRaisesRegex(OSError, "close failed"):
            self.manager.close()
        self.connection.reset_output_buffer.assert_called_once_with()
        self.assertIsNone(self.manager.serial_conn)

    def test_no_connection_is_a_noop(self):
        self.manager.serial_conn = None
        self.manager.close()
        self.connection.reset_output_buffer.assert_not_called()
        self.connection.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
