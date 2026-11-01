"""Exercise actual app methods without importing Pyxel or touching ESP32/RF."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / 'watchdogs' / 'app.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
GAME = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == 'WatchDogsGame')
METHODS = {'_send', '_anything_running_on_esp', '_start_scan_cmd', '_clear_esp_scan_state',
           '_begin_esp_scan', '_dispatch_pending_esp_command', '_set_running',
           '_handle_esp_operation_state', '_update_wardriving_loop', '_request_esp_stop',
           '_execute_item', '_try_reconnect_esp32'}
METHODS.update(n.name for n in GAME.body if isinstance(n, ast.FunctionDef)
               and (n.name.startswith('_wardrive_') or n.name in ('_toggle_wardrive', '_cancel_wardrive_plan')))
CLOCK = SimpleNamespace(now=100.0)
PX = SimpleNamespace(frame_count=100)
ENV = dict(time=SimpleNamespace(time=lambda: CLOCK.now, monotonic=lambda: CLOCK.now),
           pyxel=PX, C_ERROR=8, C_WARNING=9, C_DIM=13, C_HACK_CYAN=12, C_SUCCESS=11)
HARNESS = ast.ClassDef(name='GameHarness', bases=[], keywords=[], decorator_list=[],
                      body=[copy.deepcopy(n) for n in GAME.body
                            if isinstance(n, ast.FunctionDef) and n.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[HARNESS], type_ignores=[])),
             str(SOURCE), 'exec'), ENV)
GameHarness = ENV['GameHarness']


def game():
    CLOCK.now, PX.frame_count = 100.0, 100
    result = GameHarness()
    result.serial = SimpleNamespace(is_open=True, send_command=Mock(return_value=True))
    result._try_reconnect_esp32 = Mock(return_value=False)
    result._term_add = Mock()
    result.msg = Mock()
    result._earn_badge = Mock()
    result._pending_cmd = None
    result._pending_cmd_frame = 0
    result._pending_cmd_state = result._pending_cmd_name = ''
    result._esp_stop_ack = False
    result._esp_stop_requested_at = 0.0
    result._esp_stop_waiting = False
    result._esp_stop_timed_out = False
    result._SCAN_INTERVAL = 2.0
    result._ESP_SCAN_TIMEOUT = 20.0
    result._gps_wait = False
    result.state = SimpleNamespace(portal_running=False, evil_twin_running=False)
    for key in ('_dragon_drain', '_mitm', '_blueducky', '_race'):
        setattr(result, key, SimpleNamespace(running=False))
    result._clear_esp_scan_state(cancel_pending=True)
    return result


def messages(g):
    return [call.args[0] for call in g.msg.call_args_list]


class SendTests(unittest.TestCase):
    def test_failed_write_has_no_fake_tx(self):
        g = game()
        g.serial.send_command.return_value = False
        self.assertFalse(g._send('scan_networks'))
        self.assertFalse(any('[TX]' in call.args[0] for call in g._term_add.call_args_list))

    def test_closed_port_and_failed_reconnect_has_no_write(self):
        g = game()
        g.serial.is_open = False
        self.assertFalse(g._send('scan_networks'))
        g.serial.send_command.assert_not_called()

    def test_exception_has_no_fake_tx(self):
        g = game()
        g.serial.send_command.side_effect = OSError('disconnected')
        self.assertFalse(g._send('scan_networks'))
        self.assertFalse(any('[TX]' in call.args[0] for call in g._term_add.call_args_list))

    def test_successful_reconnect_returns_success_and_logs_once(self):
        g = game()
        g.serial.is_open = False
        def reconnect():
            g.serial.is_open = True
            return True
        g._try_reconnect_esp32.side_effect = reconnect
        self.assertTrue(g._send('scan_networks'))
        g.serial.send_command.assert_called_once_with('scan_networks')
        g._term_add.assert_called_once_with('[TX] scan_networks (after reconnect)', raw=True)


class ReconnectBarrierTests(unittest.TestCase):
    def reconnect_game(self, success=True):
        g = game()
        del g._try_reconnect_esp32  # Exercise the actual reconnect method, not the default stub.
        g.serial.is_open = False
        g._esp32 = False
        g.state.connected = False
        g._fw_version = '1.7.0'
        connection = SimpleNamespace(is_open=True, setup=Mock(), close=Mock(),
                                     send_command=Mock(return_value=success))
        ENV['detect_esp32_port'] = lambda: '/dev/ttyACM1'
        ENV['SerialManager'] = lambda port: connection
        return g, connection

    def test_replug_replaces_timed_out_barrier_with_fresh_stop_confirmation(self):
        g, connection = self.reconnect_game()
        g._esp_stop_waiting = g._esp_stop_timed_out = True
        g._pending_cmd = 'obsolete_scan'
        self.assertTrue(g._try_reconnect_esp32())
        self.assertTrue(g._esp_stop_waiting)
        self.assertFalse(g._esp_stop_timed_out)
        self.assertIsNone(g._pending_cmd)
        self.assertTrue(g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE'))
        connection.send_command.assert_called_once_with('stop')
        PX.frame_count = 115
        g._dispatch_pending_esp_command()
        self.assertFalse(g._ble_scan_only)
        g._handle_esp_operation_state('all operations stopped.')
        g._dispatch_pending_esp_command()
        self.assertTrue(g._ble_scan_only)
        self.assertFalse(g._esp_stop_waiting)
        self.assertEqual([c.args[0] for c in connection.send_command.call_args_list], ['stop', 'scan_bt'])

    def test_direct_start_cannot_bypass_new_reconnect_stop_barrier(self):
        g, connection = self.reconnect_game()
        self.assertFalse(g._start_scan_cmd('scan_networks', 'wifi_scan', 'WiFi'))
        connection.send_command.assert_called_once_with('stop')
        self.assertFalse(g._anything_running_on_esp())
        self.assertFalse(any(m.startswith('[START]') for m in messages(g)))
        g._handle_esp_operation_state('all operations stopped.')
        PX.frame_count = 115
        g._dispatch_pending_esp_command()
        self.assertTrue(g._start_scan_cmd('scan_networks', 'wifi_scan', 'WiFi'))
        self.assertEqual([c.args[0] for c in connection.send_command.call_args_list], ['stop', 'scan_networks'])

    def test_reconnect_stop_is_not_written_twice_by_outer_stop_request(self):
        g, connection = self.reconnect_game()
        self.assertTrue(g._request_esp_stop())
        self.assertTrue(g._esp_stop_waiting)
        connection.send_command.assert_called_once_with('stop')

    def test_failed_reconnect_handshake_closes_port_and_reports_no_connection(self):
        g, connection = self.reconnect_game(success=False)
        self.assertFalse(g._try_reconnect_esp32())
        self.assertIsNone(g.serial)
        self.assertFalse(g._esp32)
        self.assertFalse(g.state.connected)
        connection.close.assert_called_once_with()


class LifecycleTests(unittest.TestCase):
    def test_flags_start_only_after_successful_send(self):
        for success in (False, True):
            with self.subTest(success=success):
                g = game()
                g.serial.send_command.return_value = success
                self.assertEqual(g._start_scan_cmd('scan_networks', 'wifi_scan', 'WiFi'), success)
                self.assertEqual(g._wifi_scan_only, success)
                self.assertEqual(any(m.startswith('[START]') for m in messages(g)), success)

    def test_switch_clears_old_owner_and_rescan_before_delayed_send(self):
        g = game()
        g.wifi_scanning = True
        g._wifi_scan_done_time = 90.0
        g._wifi_scan_start_time = 80.0
        self.assertTrue(g._start_scan_cmd('scan_bt', 'bt_scanning', 'BT'))
        self.assertFalse(g._anything_running_on_esp())
        self.assertEqual(g._wifi_scan_done_time, 0)
        self.assertEqual(g._wifi_scan_start_time, 0)
        self.assertEqual(g._pending_cmd_state, 'bt_scanning')
        g._update_wardriving_loop()
        g.serial.send_command.assert_called_once_with('stop')
        g._handle_esp_operation_state('stop command received - stopping all operations...')
        g._handle_esp_operation_state('all operations stopped.')
        PX.frame_count = 115
        g._dispatch_pending_esp_command()
        self.assertTrue(g.ble_scanning)
        self.assertFalse(g.wifi_scanning)
        self.assertEqual(g._bt_scan_start_time, 100)
        CLOCK.now = 103
        g._update_wardriving_loop()
        self.assertEqual([c.args[0] for c in g.serial.send_command.call_args_list], ['stop', 'scan_bt'])

    def test_failed_stop_keeps_current_owner_and_does_not_queue_new_command(self):
        g = game()
        g.wifi_scanning = True
        g.serial.send_command.return_value = False
        self.assertFalse(g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE'))
        self.assertTrue(g.wifi_scanning)
        self.assertIsNone(g._pending_cmd)
        self.assertFalse(g._ble_scan_only)

    def test_failed_delayed_write_does_not_claim_scan_started(self):
        g = game()
        g.wifi_scanning = True
        g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE')
        g.serial.send_command.return_value = False
        g._handle_esp_operation_state('all operations stopped.')
        PX.frame_count = 115
        g._dispatch_pending_esp_command()
        self.assertIsNone(g._pending_cmd)
        self.assertFalse(g._anything_running_on_esp())
        self.assertFalse(any(m.startswith('[START]') for m in messages(g)))

    def test_last_pending_selection_replaces_previous_without_double_start(self):
        g = game()
        g.wifi_scanning = True
        g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE')
        g._start_scan_cmd('sniff', 'sniffer', 'Sniffer')
        g._handle_esp_operation_state('all operations stopped.')
        PX.frame_count = 115
        g._dispatch_pending_esp_command()
        self.assertTrue(g.sniffing)
        self.assertFalse(g._ble_scan_only)
        self.assertEqual([c.args[0] for c in g.serial.send_command.call_args_list], ['stop', 'sniff'])

    def test_stop_reply_while_waiting_does_not_cancel_pending(self):
        g = game()
        g.wifi_scanning = True
        g._start_scan_cmd('sniff', 'sniffer', 'Sniffer')
        g._handle_esp_operation_state('all stopped')
        self.assertEqual(g._pending_cmd, 'sniff')
        self.assertFalse(g._anything_running_on_esp())

    def test_final_stop_ack_clears_delayed_old_sniffer_state_without_losing_pending(self):
        g = game()
        g._request_esp_stop()
        g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE')
        g._handle_esp_operation_state('packet sniffer started')
        self.assertTrue(g.sniffing)
        g._handle_esp_operation_state('all operations stopped.')
        self.assertFalse(g.sniffing)
        self.assertEqual(g._pending_cmd, 'scan_bt')

    def test_actual_paired_stop_replies_and_minimum_gap_gate_new_operation(self):
        for command, key in [('sniff', 'sniffer'), ('scan_bt', 'ble_scan')]:
            with self.subTest(key=key):
                g = game()
                g.wifi_scanning = True
                g._start_scan_cmd(command, key, 'New scan')
                g._handle_esp_operation_state('stop command received - stopping all operations...')
                PX.frame_count = 115
                g._dispatch_pending_esp_command()
                self.assertFalse(g._anything_running_on_esp())
                g.serial.send_command.assert_called_once_with('stop')
                g._handle_esp_operation_state('all operations stopped.')
                PX.frame_count = 114
                g._dispatch_pending_esp_command()
                self.assertFalse(g._anything_running_on_esp())
                PX.frame_count = 115
                g._dispatch_pending_esp_command()
                self.assertTrue(g._anything_running_on_esp())
                self.assertEqual([c.args[0] for c in g.serial.send_command.call_args_list], ['stop', command])

    def test_absent_final_stop_ack_cancels_without_start_after_five_seconds(self):
        for receipt in (False, True):
            with self.subTest(receipt=receipt):
                g = game()
                g.wifi_scanning = True
                g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE')
                if receipt:
                    g._handle_esp_operation_state('stop command received - stopping all operations...')
                PX.frame_count = 200
                CLOCK.now = 104.9
                g._dispatch_pending_esp_command()
                self.assertIsNotNone(g._pending_cmd)
                CLOCK.now = 105
                g._dispatch_pending_esp_command()
                self.assertIsNone(g._pending_cmd)
                self.assertFalse(g._anything_running_on_esp())
                self.assertTrue(g._esp_stop_waiting)
                self.assertTrue(g._esp_stop_timed_out)
                self.assertFalse(any(m.startswith('[START]') for m in messages(g)))
                self.assertTrue(any('not confirmed' in m for m in messages(g)))
                g.serial.send_command.assert_called_once_with('stop')

    def test_explicit_stop_cancels_pending_without_duplicate_stop_request(self):
        g = game()
        g.wifi_scanning = True
        g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE')
        g._request_esp_stop()
        PX.frame_count = 200
        g._dispatch_pending_esp_command()
        self.assertIsNone(g._pending_cmd)
        self.assertFalse(g._anything_running_on_esp())
        self.assertTrue(g._esp_stop_waiting)
        self.assertFalse(g._esp_stop_ack)
        g.serial.send_command.assert_called_once_with('stop')
        g._handle_esp_operation_state('all operations stopped.')
        g._dispatch_pending_esp_command()
        self.assertFalse(g._esp_stop_waiting)
        g.serial.send_command.assert_called_once_with('stop')

    def test_stop_all_then_immediate_ble_waits_for_original_final_ack(self):
        g = game()
        g.wifi_scanning = True
        g._execute_item('', '_stop_all', 'Stop All', [])
        self.assertTrue(g._esp_stop_waiting)
        self.assertIsNone(g._pending_cmd)
        g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE')
        self.assertFalse(g._anything_running_on_esp())
        PX.frame_count = 115
        g._dispatch_pending_esp_command()
        g.serial.send_command.assert_called_once_with('stop')
        g._handle_esp_operation_state('stop command received - stopping all operations...')
        g._dispatch_pending_esp_command()
        g.serial.send_command.assert_called_once_with('stop')
        g._handle_esp_operation_state('all operations stopped.')
        g._dispatch_pending_esp_command()
        self.assertTrue(g._ble_scan_only)
        self.assertEqual([c.args[0] for c in g.serial.send_command.call_args_list], ['stop', 'scan_bt'])

    def test_timeout_barrier_without_pending_reports_once_and_rejects_quick_retry(self):
        g = game()
        g._request_esp_stop()
        CLOCK.now, PX.frame_count = 105, 250
        g._dispatch_pending_esp_command()
        self.assertTrue(g._esp_stop_waiting)
        self.assertTrue(g._esp_stop_timed_out)
        self.assertIsNone(g._pending_cmd)
        count = len(messages(g))
        CLOCK.now = 106
        g._dispatch_pending_esp_command()
        self.assertEqual(len(messages(g)), count)
        self.assertFalse(g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE'))
        self.assertFalse(g._anything_running_on_esp())
        g.serial.send_command.assert_called_once_with('stop')
        g._handle_esp_operation_state('all operations stopped.')
        g._dispatch_pending_esp_command()
        self.assertFalse(g._esp_stop_waiting)
        self.assertFalse(g._anything_running_on_esp())
        g.serial.send_command.assert_called_once_with('stop')

    def test_final_stop_reply_without_pending_clears_owner(self):
        g = game()
        g._begin_esp_scan('sniff', 'sniffer', 'Sniffer')
        g._handle_esp_operation_state('all operations stopped.')
        self.assertFalse(g._anything_running_on_esp())


class ScanTimeoutTests(unittest.TestCase):
    def test_scan_timeout_then_immediate_restart_waits_for_final_stop_ack(self):
        g = game()
        g._start_scan_cmd('scan_networks', 'wifi_scan', 'WiFi')
        CLOCK.now, PX.frame_count = 121, 200
        g._update_wardriving_loop()
        self.assertTrue(g._esp_stop_waiting)
        self.assertFalse(g._anything_running_on_esp())
        g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE')
        PX.frame_count = 215
        g._dispatch_pending_esp_command()
        self.assertFalse(g._ble_scan_only)
        g._handle_esp_operation_state('all operations stopped.')
        g._dispatch_pending_esp_command()
        self.assertTrue(g._ble_scan_only)
        self.assertEqual([c.args[0] for c in g.serial.send_command.call_args_list],
                         ['scan_networks', 'stop', 'scan_bt'])

    def test_missing_first_completion_stops_all_finite_scan_variants(self):
        for command, key in [('scan_networks', 'wifi_scan'), ('scan_networks', 'wardriving'),
                             ('scan_bt', 'ble_scan'), ('scan_bt', 'bt_scanning')]:
            with self.subTest(key=key):
                g = game()
                g._start_scan_cmd(command, key, key)
                CLOCK.now = 120
                g._update_wardriving_loop()
                self.assertTrue(g._anything_running_on_esp())
                CLOCK.now = 120.1
                g._update_wardriving_loop()
                self.assertFalse(g._anything_running_on_esp())
                self.assertEqual(g._wifi_scan_done_time, 0)
                self.assertEqual(g._bt_scan_done_time, 0)
                self.assertTrue(any('timed out' in m for m in messages(g)))
                count = g.serial.send_command.call_count
                CLOCK.now = 150
                g._update_wardriving_loop()
                self.assertEqual(g.serial.send_command.call_count, count)

    def test_continuous_scan_repeats_only_when_previous_scan_completed(self):
        for key, command, done_name in [('wardriving', 'scan_networks', '_wifi_scan_done_time'),
                                        ('bt_scanning', 'scan_bt', '_bt_scan_done_time')]:
            with self.subTest(key=key):
                g = game()
                g._start_scan_cmd(command, key, key)
                CLOCK.now = 105
                g._update_wardriving_loop()
                self.assertEqual(g.serial.send_command.call_count, 1)
                setattr(g, done_name, 105)
                CLOCK.now = 107
                g._update_wardriving_loop()
                self.assertEqual(g.serial.send_command.call_count, 2)
                self.assertEqual(getattr(g, done_name), 0)
                start_name = '_wifi_scan_start_time' if key == 'wardriving' else '_bt_scan_start_time'
                self.assertEqual(getattr(g, start_name), 107)

    def test_failed_rescan_clears_busy_state_instead_of_repeating_forever(self):
        g = game()
        g._start_scan_cmd('scan_networks', 'wardriving', 'WiFi')
        g._wifi_scan_done_time = 101
        CLOCK.now = 103
        g.serial.send_command.return_value = False
        g._update_wardriving_loop()
        self.assertFalse(g.wifi_scanning)
        self.assertEqual(g._wifi_scan_done_time, 0)
        self.assertTrue(any('rescan failed' in m for m in messages(g)))


if __name__ == '__main__':
    unittest.main()
