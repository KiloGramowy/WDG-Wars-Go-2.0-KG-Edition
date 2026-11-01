"""Real menu, parser and scheduler methods; simulated serial/storage only."""
import ast
import copy
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_scanner_lifecycle as base

ENV = dict(base.ENV, Base=base.GameHarness, re=re)
for statement in base.TREE.body:
    if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in ('_BLE_RE', '_AIRTAG_COUNT_RE')
            for target in statement.targets):
        exec(compile(ast.Module(body=[statement], type_ignores=[]), str(base.SOURCE), 'exec'), ENV)
extra = {'_is_running', '_handle_serial_line', '_poll_serial'}
update = next(n for n in base.GAME.body if isinstance(n, ast.FunctionDef) and n.name == 'update')
stop_key = next(n for n in ast.walk(update) if isinstance(n, ast.If)
                and 'pyxel.KEY_S' in ast.unparse(n.test) and 'self._attack_mode' in ast.unparse(n.test))
gps_cancel = next(n for n in ast.walk(update) if isinstance(n, ast.If)
                 and 'pyxel.KEY_N' in ast.unparse(n.test) and 'pyxel.KEY_ESCAPE' in ast.unparse(n.test)
                 and '_gps_wait_dialog = False' in ast.unparse(ast.Module(body=n.body, type_ignores=[])))
gps_wait_cancel = next(n for n in ast.walk(update) if isinstance(n, ast.If)
                      and isinstance(n.test, ast.Attribute) and n.test.attr == '_gps_wait')
branches = [ast.FunctionDef(name=name, args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='self')],
    kwonlyargs=[], kw_defaults=[], defaults=[]), body=copy.deepcopy(node.body), decorator_list=[])
    for name, node in [('_press_stop_key', stop_key), ('_cancel_gps_dialog', gps_cancel),
                       ('_cancel_gps_wait', gps_wait_cancel)]]
harness = ast.ClassDef(name='WardriveGame', bases=[ast.Name(id='Base', ctx=ast.Load())],
    keywords=[], decorator_list=[], body=[copy.deepcopy(n) for n in base.GAME.body
    if isinstance(n, ast.FunctionDef) and n.name in extra] + branches)
exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])),
             str(base.SOURCE), 'exec'), ENV)
WardriveGame = ENV['WardriveGame']


class AlternationTests(unittest.TestCase):
    def setUp(self):
        self.g = base.game()
        self.g.__class__ = WardriveGame
        self.g._wardrive_selected = set()
        self.g._wardrive_phase = self.g._wardrive_last_phase = ''
        self.g._wardrive_phase_done = 0.0
        self.g._wardrive_paused = self.g._wardrive_save_failed = False
        self.g._wardrive_save_errors_at_start = None
        self.g._gps_wait_dialog = False
        self.g._gps_wait_cmd = self.g._gps_wait_state = self.g._gps_wait_name = ''
        self.g.gps_fix = True
        self.g.gps_sats = 8
        self.g.gps_sats_vis = 10
        self.g._esp32 = True
        self.g._fw_version = '1.7.0'
        self.g._et_scan_pending = False
        self.g._known_wifi = {'AA:BB:CC:DD:EE:FF'}
        self.g._known_ble = {'11:22:33:44:55:66'}
        self.g._whitelist = SimpleNamespace(is_blocked=Mock(return_value=False))
        self.g.net_mgr = SimpleNamespace(parse_network_line=Mock(return_value=None))
        self.g.state.networks = []
        self.g.gain_xp = Mock()
        self.g.loot = SimpleNamespace(active=True, wardrive_save_errors=0, log_serial=Mock(),
            save_wardriving_network=Mock(return_value=False),
            save_bt_device=Mock(return_value=False), save_wardriving_bt=Mock(return_value=False))

    def toggle(self, key):
        command = 'scan_networks' if key == 'wardriving' else 'scan_bt'
        self.g._execute_item(command, key, key, [])
        self.assertLessEqual(int(self.g.wifi_scanning) + int(self.g.ble_scanning), 1)

    def sent(self):
        return [c.args[0] for c in self.g.serial.send_command.call_args_list]

    def complete(self, key):
        self.g._handle_serial_line('Scan results printed' if key == 'wardriving' else 'Summary: 0 devices')

    def advance(self, seconds=2):
        base.CLOCK.now += seconds
        base.PX.frame_count += int(seconds * 30)
        self.g._update_wardriving_loop()

    def ack(self):
        self.g._handle_serial_line('Stop command received - stopping all operations...')
        self.g._handle_serial_line('All operations stopped.')
        base.CLOCK.now += 0.5
        base.PX.frame_count += 15
        self.g._dispatch_pending_esp_command()
        self.assertLessEqual(int(self.g.wifi_scanning) + int(self.g.ble_scanning), 1)

    def both(self):
        self.toggle('wardriving')
        self.toggle('bt_scanning')

    def test_single_wifi_or_ble_repeats_only_that_mode_after_complete_and_pause(self):
        for key, command in [('wardriving', 'scan_networks'), ('bt_scanning', 'scan_bt')]:
            with self.subTest(key=key):
                self.setUp()
                self.toggle(key)
                self.complete(key)
                self.advance(1.9)
                self.assertEqual(self.sent(), [command])
                self.advance(.1)
                self.assertEqual(self.sent(), [command, command])

    def test_both_menu_toggles_on_but_physical_scans_alternate_one_to_one(self):
        self.both()
        self.assertTrue(self.g._is_running('wardriving'))
        self.assertTrue(self.g._is_running('bt_scanning'))
        self.assertEqual(self.sent(), ['scan_networks'])
        for key, next_key in [('wardriving', 'bt_scanning'), ('bt_scanning', 'wardriving')] * 2:
            self.assertEqual(self.g._wardrive_phase, key)
            self.complete(key)
            self.assertFalse(self.g.wifi_scanning or self.g.ble_scanning)
            self.advance()
            self.assertEqual(self.sent()[-1], 'stop')
            self.assertEqual(self.g._pending_cmd_state, next_key)
            self.ack()
        self.assertEqual(self.sent(), ['scan_networks', 'stop', 'scan_bt', 'stop',
            'scan_networks', 'stop', 'scan_bt', 'stop', 'scan_networks'])

    def test_enabling_second_midcycle_does_not_interrupt_current(self):
        self.toggle('wardriving')
        self.toggle('bt_scanning')
        self.assertEqual(self.sent(), ['scan_networks'])
        self.assertTrue(self.g.wifi_scanning)
        self.assertFalse(self.g.ble_scanning)
        self.assertTrue(any('queued' in c.args[0] for c in self.g._term_add.call_args_list))

    def test_disabling_waiting_mode_keeps_current_and_single_repeat(self):
        self.both()
        self.toggle('bt_scanning')
        self.assertEqual(self.g._wardrive_selected, {'wardriving'})
        self.assertTrue(self.g.wifi_scanning)
        self.assertEqual(self.sent(), ['scan_networks'])
        self.complete('wardriving')
        self.advance()
        self.assertEqual(self.sent(), ['scan_networks', 'scan_networks'])

    def test_disabling_active_mode_stops_then_runs_remaining_after_final_ack(self):
        self.both()
        self.toggle('wardriving')
        self.assertEqual(self.g._wardrive_selected, {'bt_scanning'})
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])
        self.assertFalse(self.g.wifi_scanning or self.g.ble_scanning)
        self.ack()
        self.assertEqual(self.sent(), ['scan_networks', 'stop', 'scan_bt'])

    def test_failed_active_mode_stop_requires_fresh_confirmed_stop_after_recovery(self):
        self.both()
        self.g.serial.send_command.return_value = False
        self.toggle('wardriving')
        self.assertTrue(self.g._wardrive_paused)
        self.assertTrue(self.g._esp_stop_required)
        self.assertFalse(self.g.wifi_scanning or self.g.ble_scanning)
        self.toggle('bt_scanning')  # Clear the remaining requested mode.
        self.g.serial.send_command.return_value = True
        self.toggle('bt_scanning')
        self.assertEqual(self.sent(), ['scan_networks', 'stop', 'stop'])
        self.assertTrue(self.g._esp_stop_waiting)
        self.assertFalse(self.g.ble_scanning)
        self.ack()
        self.assertEqual(self.sent(), ['scan_networks', 'stop', 'stop', 'scan_bt'])

    def test_failed_both_off_stop_does_not_lose_unknown_firmware_owner(self):
        self.toggle('wardriving')
        self.g.serial.send_command.return_value = False
        self.toggle('wardriving')
        self.assertEqual(self.g._wardrive_selected, set())
        self.assertTrue(self.g._esp_stop_required)
        self.g.serial.send_command.return_value = True
        self.toggle('bt_scanning')
        self.assertEqual(self.sent(), ['scan_networks', 'stop', 'stop'])
        self.assertFalse(self.g.ble_scanning)
        self.ack()
        self.assertEqual(self.sent(), ['scan_networks', 'stop', 'stop', 'scan_bt'])

    def test_disabling_queued_mode_during_stop_changes_next_without_second_stop(self):
        self.both()
        self.complete('wardriving')
        self.advance()
        self.toggle('bt_scanning')
        self.assertEqual(self.g._pending_cmd_state, 'wardriving')
        self.ack()
        self.assertEqual(self.sent(), ['scan_networks', 'stop', 'scan_networks'])

    def test_both_off_during_stop_barrier_cannot_restart_after_late_ack(self):
        self.both()
        self.complete('wardriving')
        self.advance()
        self.toggle('bt_scanning')
        self.toggle('wardriving')
        self.assertEqual(self.g._wardrive_selected, set())
        self.assertIsNone(self.g._pending_cmd)
        self.ack()
        self.advance(30)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])

    def test_both_selected_waiting_for_gps_start_wifi_from_latest_set(self):
        self.g.gps_fix = False
        self.toggle('bt_scanning')
        self.g._gps_wait_dialog, self.g._gps_wait = False, True
        self.toggle('wardriving')
        self.assertEqual(self.sent(), [])
        self.g.gps_fix = True
        self.g._update_wardriving_loop()
        self.assertEqual(self.sent(), ['scan_networks'])
        self.assertEqual(self.g._wardrive_selected, {'wardriving', 'bt_scanning'})

    def test_disabling_gps_wait_mode_does_not_resurrect_old_command(self):
        self.g.gps_fix = False
        self.toggle('wardriving')
        self.g._gps_wait_dialog, self.g._gps_wait = False, True
        self.toggle('bt_scanning')
        self.toggle('wardriving')
        self.g.gps_fix = True
        self.g._update_wardriving_loop()
        self.assertEqual(self.sent(), ['scan_bt'])

    def test_stop_all_cancels_both_gps_wait_and_pending_start(self):
        self.g.gps_fix = False
        self.toggle('wardriving')
        self.g._gps_wait_dialog, self.g._gps_wait = False, True
        self.toggle('bt_scanning')
        self.g._execute_item('', '_stop_all', 'Stop All', [])
        self.assertFalse(self.g._gps_wait or self.g._gps_wait_dialog)
        self.assertEqual(self.g._wardrive_selected, set())
        self.g.gps_fix = True
        self.ack()
        self.advance(30)
        self.assertEqual(self.sent(), ['stop'])

    def test_actual_s_key_branch_cancels_both_and_queued_restart(self):
        self.both()
        self.complete('wardriving')
        self.advance()
        self.g._press_stop_key()
        self.assertEqual(self.g._wardrive_selected, set())
        self.assertIsNone(self.g._pending_cmd)
        self.ack()
        self.advance(30)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])

    def test_cancel_gps_dialog_cancels_latest_selected_set(self):
        self.g.gps_fix = False
        self.toggle('wardriving')
        self.toggle('bt_scanning')
        self.g._cancel_gps_dialog()
        self.assertEqual(self.g._wardrive_selected, set())
        self.assertFalse(self.g._gps_wait or self.g._gps_wait_dialog)
        self.g.gps_fix = True
        self.advance()
        self.assertEqual(self.sent(), [])

    def test_escape_during_accepted_gps_wait_cannot_bypass_fix_gate(self):
        self.g.gps_fix = False
        self.toggle('wardriving')
        self.g._gps_wait_dialog, self.g._gps_wait = False, True
        self.toggle('bt_scanning')
        self.g._cancel_gps_wait()
        self.assertEqual(self.g._wardrive_selected, set())
        self.assertFalse(self.g._gps_wait or self.g._gps_wait_dialog)
        self.advance()
        self.assertEqual(self.sent(), [])
        self.g.gps_fix = True
        self.advance()
        self.assertEqual(self.sent(), [])

    def test_scan_and_packet_sniffer_takeover_cancel_plan_without_auto_resume(self):
        for command, key in [('scan_networks', 'wifi_scan'), ('packet_sniffer', 'sniffer')]:
            with self.subTest(key=key):
                self.setUp()
                self.both()
                self.g._start_scan_cmd(command, key, key)
                self.assertEqual(self.g._wardrive_selected, set())
                self.ack()
                count = len(self.sent())
                self.g._handle_serial_line('Scan results printed')
                self.advance(30)
                self.assertEqual(len(self.sent()), count)

    def test_wrong_completion_and_generic_scan_complete_do_not_end_current_phase(self):
        self.both()
        self.g._handle_serial_line('Summary: 0 devices')
        self.assertEqual(self.g._wardrive_phase_done, 0)
        self.complete('wardriving')
        self.advance()
        self.ack()
        self.g._handle_serial_line('Scan complete')
        self.g._handle_serial_line('Scan results printed')
        self.assertEqual(self.g._wardrive_phase_done, 0)
        self.assertTrue(self.g.ble_scanning)

    def test_empty_scan_and_duplicate_false_are_valid_completion(self):
        self.both()
        self.assertFalse(self.g.loot.save_wardriving_network(None))
        self.complete('wardriving')
        self.assertFalse(self.g._wardrive_paused)
        self.advance()
        self.ack()
        self.assertTrue(self.g.ble_scanning)

    def test_storage_error_counter_pauses_and_never_starts_next_phase(self):
        self.both()
        self.g.loot.wardrive_save_errors += 1
        self.complete('wardriving')
        self.assertTrue(self.g._wardrive_paused)
        self.assertTrue(self.g._is_running('wardriving'))
        self.assertTrue(self.g._is_running('bt_scanning'))
        self.ack()
        self.advance(30)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])

    def test_inactive_loot_session_cannot_start_or_claim_a_saved_phase(self):
        self.g.loot.active = False
        self.toggle('wardriving')
        self.assertTrue(self.g._wardrive_paused)
        self.assertEqual(self.sent(), [])
        self.setUp()
        self.both()
        self.g.loot.active = False
        self.complete('wardriving')
        self.assertTrue(self.g._wardrive_paused)
        self.ack()
        self.advance(30)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])
        self.assertFalse(any('complete and saved' in c.args[0] for c in self.g._term_add.call_args_list))

    def test_inactive_storage_takeover_stops_existing_manual_owner_before_clearing_flags(self):
        self.g._start_scan_cmd('packet_sniffer', 'sniffer', 'Packet Sniffer')
        self.g.loot.active = False
        self.toggle('wardriving')
        self.assertTrue(self.g._wardrive_paused)
        self.assertTrue(self.g._esp_stop_waiting)
        self.assertEqual(self.sent(), ['packet_sniffer', 'stop'])
        self.g._start_scan_cmd('scan_bt', 'ble_scan', 'BLE Scan')
        self.assertEqual(self.sent(), ['packet_sniffer', 'stop'])
        self.ack()
        self.assertEqual(self.sent(), ['packet_sniffer', 'stop', 'scan_bt'])

    def test_callback_write_exception_marks_current_phase_failed(self):
        self.both()
        network = SimpleNamespace(bssid='AA:BB:CC:DD:EE:FF', ssid='fixture', channel='1', rssi='-60', auth='WPA2')
        self.g.net_mgr.parse_network_line.side_effect = lambda line: network if line == 'fixture row' else None
        self.g.loot.save_wardriving_network.side_effect = OSError('disk full')
        self.g._handle_serial_line('fixture row')
        self.assertTrue(self.g._wardrive_save_failed)
        self.complete('wardriving')
        self.assertTrue(self.g._wardrive_paused)
        self.g.gain_xp.assert_called_once_with(1)  # Existing duplicate XP rule is unchanged.

    def test_ble_callback_write_exception_pauses_after_complete(self):
        self.toggle('bt_scanning')
        self.g.loot.save_wardriving_bt.side_effect = OSError('disk full')
        self.g._handle_serial_line('1. 11:22:33:44:55:66 RSSI: -60 dBm Name: fixture')
        self.assertTrue(self.g._wardrive_save_failed)
        self.complete('bt_scanning')
        self.assertTrue(self.g._wardrive_paused)
        self.g.gain_xp.assert_called_once_with(1)

    def test_save_occurs_before_stop_and_next_radio_command(self):
        trace = []
        self.g.serial.send_command.side_effect = lambda command: trace.append(command) or True
        self.both()
        network = SimpleNamespace(bssid='AA:BB:CC:DD:EE:FF', ssid='fixture', channel='1', rssi='-60', auth='WPA2')
        self.g.net_mgr.parse_network_line.side_effect = lambda line: network if line == 'fixture row' else None
        self.g.loot.save_wardriving_network.side_effect = lambda net: trace.append('saved') or True
        self.g._handle_serial_line('fixture row')
        self.complete('wardriving')
        self.advance()
        self.ack()
        self.assertEqual(trace, ['scan_networks', 'saved', 'stop', 'scan_bt'])

    def test_twenty_second_scan_timeout_pauses_instead_of_changing_phase(self):
        self.both()
        self.advance(20.1)
        self.assertTrue(self.g._wardrive_paused)
        self.ack()
        self.advance(30)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])

    def test_five_second_stop_timeout_pauses_and_late_ack_cannot_restart(self):
        self.both()
        self.complete('wardriving')
        self.advance()
        base.CLOCK.now += 5
        base.PX.frame_count += 150
        self.g._dispatch_pending_esp_command()
        self.assertTrue(self.g._wardrive_paused)
        self.assertIsNone(self.g._pending_cmd)
        self.ack()
        self.advance(30)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])

    def test_gps_loss_after_start_does_not_reapply_initial_fix_gate(self):
        self.both()
        self.g.gps_fix = False
        self.complete('wardriving')
        self.advance()
        self.ack()
        self.assertTrue(self.g.ble_scanning)
        self.assertFalse(self.g._gps_wait_dialog)

    def test_disconnect_pauses_without_recursive_reconnect_and_keeps_requests(self):
        self.both()
        self.g.serial.read_available = Mock(side_effect=OSError('USB unplugged'))
        self.g.serial.close = Mock()
        self.g._poll_serial()
        self.assertTrue(self.g._wardrive_paused)
        self.assertEqual(self.g._wardrive_selected, {'wardriving', 'bt_scanning'})
        self.assertFalse(self.g.wifi_scanning or self.g.ble_scanning)
        self.g._try_reconnect_esp32.assert_not_called()

    def test_verified_reconnect_keeps_selected_but_does_not_resume_plan(self):
        self.both()
        del self.g._try_reconnect_esp32
        connection = SimpleNamespace(is_open=True, setup=Mock(), close=Mock(),
                                     send_command=Mock(return_value=True))
        with patch.dict(base.ENV, detect_esp32_port=lambda: '/dev/ttyACM1',
                        SerialManager=lambda port: connection):
            self.assertTrue(self.g._try_reconnect_esp32())
        self.assertTrue(self.g._wardrive_paused)
        self.assertEqual(self.g._wardrive_selected, {'wardriving', 'bt_scanning'})
        self.ack()
        self.advance(30)
        connection.send_command.assert_called_once_with('stop')

    def test_scan_pause_uses_monotonic_clock_despite_wall_clock_jump(self):
        self.both()
        self.complete('wardriving')
        with patch.object(base.ENV['time'], 'time', return_value=1_000_000_000):
            self.advance(1)
            self.assertEqual(self.sent(), ['scan_networks'])
            self.advance(1)
            self.assertEqual(self.sent(), ['scan_networks', 'stop'])


if __name__ == '__main__':
    unittest.main()
