"""Actual popup/form methods with simulated serial; never run RF operations."""
import ast
import copy
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_scanner_lifecycle as base
import test_wardrive_alternation as wardrive


class Keys:
    frame_count = 100

    def __init__(self):
        self.pressed = set()
        self.calls = []

    def __getattr__(self, name):
        if name.startswith('KEY_'):
            return name
        return lambda *args: self.calls.append((name, args))

    def btnp(self, key, *args):
        return key in self.pressed


PX = Keys()
ENV = dict(wardrive.ENV, Parent=wardrive.WardriveGame, pyxel=PX, re=re)
for node in base.TREE.body:
    if isinstance(node, ast.Assign):
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(base.SOURCE), 'exec'), ENV)
        except (NameError, AttributeError, TypeError):
            pass
METHODS = {'_activate_menu_item', '_start_input_dialog', '_start_wifi_input_picker',
           '_refresh_wifi_input_scan', '_complete_wifi_input_scan', '_close_wifi_input_picker',
           '_update_wifi_input_picker', '_update_picker_overlay', '_draw_net_picker',
           '_show_net_selection'}
cls = ast.ClassDef(name='PickerGame', bases=[ast.Name(id='Parent', ctx=ast.Load())],
                  keywords=[], decorator_list=[], body=[copy.deepcopy(n) for n in base.GAME.body
                    if isinstance(n, ast.FunctionDef) and n.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
             str(base.SOURCE), 'exec'), ENV)
PickerGame = ENV['PickerGame']
MENU = next((ci, ii) for ci, (_, items) in enumerate(ENV['MENU_CATS'])
            for ii, item in enumerate(items) if item[2] == 'start_deauth')


def network(bssid='02:00:00:00:00:01', channel='6', ssid='Synthetic test AP'):
    return SimpleNamespace(index='1', bssid=bssid, channel=channel, ssid=ssid,
                           rssi='-55', auth='WPA2', band='2.4 GHz')


class WifiInputPickerTests(unittest.TestCase):
    def setUp(self):
        PX.pressed.clear()
        PX.calls.clear()
        PX.frame_count = 100
        for namespace in (base.ENV, wardrive.ENV):
            p = patch.dict(namespace, pyxel=PX)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(sys.modules, pyxel=PX)
        p.start()
        self.addCleanup(p.stop)
        wardrive.AlternationTests.setUp(self)
        self.g.__class__ = PickerGame
        self.g._wifi_input_picker = None
        self.g._et_net_screen = False
        self.g._et_net_selected = set()
        self.g._portal_select_screen = False
        self.g._attack_mode = self.g._attack_step = ''
        self.g.state.sniffer_aps = []
        self.g.input_mode = False
        self.g._show_portal_selection = Mock()
        self.g._execute_item = Mock()

    def open(self, nets=()):
        self.g.state.networks = list(nets)
        self.g._activate_menu_item(*MENU)

    def press(self, key):
        PX.pressed = {key}
        self.g._update_picker_overlay()

    def sent(self):
        return [c.args[0] for c in self.g.serial.send_command.call_args_list]

    def test_menu_opens_same_popup_with_previous_results_and_no_commands(self):
        self.open([network()])
        self.assertTrue(self.g._et_net_screen)
        self.assertFalse(self.g.input_mode)
        self.g._draw_net_picker()
        labels = [args[2] for op, args in PX.calls if op == 'text']
        self.assertIn('DEAUTH — Select Network', labels)
        for label in ('SSID', 'BSSID', 'CH', 'RSSI', 'Synthetic test AP', '02:00:00:00:00:01'):
            self.assertIn(label, labels)
        self.assertTrue(any('R scan' in label and 'M manual' in label for label in labels))
        self.assertEqual(self.sent(), [])
        self.g._execute_item.assert_not_called()

    def test_enter_fills_only_selected_bssid_and_channel_with_same_ssid(self):
        self.open([network(), network('02:00:00:00:00:02', '11')])
        self.press(PX.KEY_DOWN)
        self.press(PX.KEY_RETURN)
        self.assertFalse(self.g._et_net_screen)
        self.assertTrue(self.g.input_mode)
        self.assertEqual(self.g.input_fields, [{'label': 'BSSID', 'value': '02:00:00:00:00:02'},
                                              {'label': 'CH', 'value': '11'}])
        self.assertEqual((self.g._input_pending_cat, self.g._input_pending_item), MENU)
        self.assertEqual(self.sent(), [])
        self.g._execute_item.assert_not_called()
        self.g._show_portal_selection.assert_not_called()

    def test_manual_opens_original_empty_form_without_commands(self):
        self.open([network()])
        self.press(PX.KEY_M)
        self.assertTrue(self.g.input_mode)
        self.assertEqual([f['value'] for f in self.g.input_fields], ['', ''])
        self.assertIsNone(self.g._wifi_input_picker)
        self.assertEqual(self.sent(), [])

    def test_empty_list_is_visible_and_navigation_never_selects_minus_one(self):
        self.open()
        for key in (PX.KEY_DOWN, PX.KEY_UP, PX.KEY_RETURN, PX.KEY_SPACE):
            self.press(key)
        self.assertEqual(self.g._et_net_sel, 0)
        self.assertFalse(self.g.input_mode)
        self.g._draw_net_picker()
        self.assertTrue(any(op == 'text' and 'No scan results' in args[2] for op, args in PX.calls))
        self.assertEqual(self.sent(), [])

    def test_selection_uses_stable_scan_snapshot(self):
        self.open([network()])
        self.g.state.networks[:] = [network('02:00:00:00:00:02')]
        self.press(PX.KEY_RETURN)
        self.assertEqual(self.g.input_fields[0]['value'], '02:00:00:00:00:01')

    def test_whitelisted_network_cannot_fill_form(self):
        self.open([network()])
        self.g._whitelist.is_blocked.return_value = True
        self.press(PX.KEY_RETURN)
        self.assertFalse(self.g.input_mode)
        self.assertTrue(self.g._et_net_screen)
        self.assertIn('whitelisted', self.g._wifi_input_picker['status'])
        self.assertEqual(self.sent(), [])

    def test_malformed_scan_bssid_or_channel_cannot_fill_form(self):
        for row in (network('not-a-mac'), network(channel='unknown'), network(channel='0')):
            with self.subTest(row=row):
                self.open([row])
                self.press(PX.KEY_RETURN)
                self.assertFalse(self.g.input_mode)
                self.assertIn('Invalid', self.g._wifi_input_picker['status'])
        self.assertEqual(self.sent(), [])

    def test_refresh_accepts_completed_empty_or_populated_scan(self):
        for rows in ([], [network('02:00:00:00:00:02', '11')]):
            with self.subTest(rows=rows):
                self.open([network()])
                self.press(PX.KEY_R)
                self.assertTrue(self.g._wifi_input_picker['pending'])
                self.assertEqual(self.g.state.networks, [])
                self.press(PX.KEY_RETURN)
                self.assertFalse(self.g.input_mode)
                self.g.state.networks.extend(rows)
                self.g._handle_serial_line('Scan results printed')
                self.assertFalse(self.g._wifi_input_picker['pending'])
                self.assertFalse(self.g._wifi_scan_only)
                self.assertEqual(self.g._attack_scan_results, rows)
        self.assertEqual(self.sent(), ['scan_networks', 'scan_networks'])
        self.g._execute_item.assert_not_called()

    def test_refresh_waits_for_confirmed_stop_and_ignores_old_completion(self):
        self.open([network()])
        self.g.ble_scanning = True
        self.press(PX.KEY_R)
        self.assertEqual(self.sent(), ['stop'])
        self.assertFalse(self.g._wifi_input_picker['started'])
        self.g._handle_serial_line('Scan results printed')
        self.assertTrue(self.g._wifi_input_picker['pending'])
        self.g._handle_serial_line('All operations stopped.')
        PX.frame_count += 15
        base.CLOCK.now += .5
        self.g._dispatch_pending_esp_command()
        self.assertEqual(self.sent(), ['stop', 'scan_networks'])
        self.assertTrue(self.g._wifi_input_picker['started'])
        self.assertEqual(self.g.state.networks, [])

    def test_failed_start_keeps_popup_manual_available(self):
        self.open()
        self.g.serial.send_command.return_value = False
        self.press(PX.KEY_R)
        self.assertFalse(self.g._wifi_input_picker['pending'])
        self.assertIn('could not start', self.g._wifi_input_picker['status'])
        self.press(PX.KEY_M)
        self.assertTrue(self.g.input_mode)
        self.g._execute_item.assert_not_called()

    def test_disconnected_scanner_does_not_block_manual_entry(self):
        self.open()
        self.g._esp32 = False
        self.press(PX.KEY_R)
        self.assertFalse(self.g._wifi_input_picker['pending'])
        self.assertIn('No scanner', self.g._wifi_input_picker['status'])
        self.press(PX.KEY_M)
        self.assertTrue(self.g.input_mode)
        self.assertEqual(self.sent(), [])

    def test_deferred_scan_write_failure_after_stop_allows_retry(self):
        self.open()
        self.g.ble_scanning = True
        self.press(PX.KEY_R)
        self.g._handle_serial_line('All operations stopped.')
        PX.frame_count += 15
        self.g.serial.send_command.return_value = False
        self.g._dispatch_pending_esp_command()
        self.assertFalse(self.g._wifi_input_picker['pending'])
        self.assertIn('could not start', self.g._wifi_input_picker['status'])
        self.g.serial.send_command.return_value = True
        self.press(PX.KEY_R)
        self.assertTrue(self.g._wifi_input_picker['pending'])
        self.assertTrue(self.g._wifi_input_picker['started'])
        self.g._execute_item.assert_not_called()

    def test_escape_cancels_queued_scan_and_late_results_cannot_reopen_popup(self):
        self.open()
        self.g.ble_scanning = True
        self.press(PX.KEY_R)
        self.press(PX.KEY_ESCAPE)
        self.assertIsNone(self.g._pending_cmd)
        self.g._handle_serial_line('All operations stopped.')
        PX.frame_count += 15
        self.g._dispatch_pending_esp_command()
        self.g._handle_serial_line('Scan results printed')
        self.assertFalse(self.g._et_net_screen)
        self.assertIsNone(self.g._wifi_input_picker)
        self.assertEqual(self.sent(), ['stop'])

    def test_manual_cancels_owned_active_scan_without_executing_form(self):
        self.open()
        self.press(PX.KEY_R)
        self.press(PX.KEY_M)
        self.assertTrue(self.g.input_mode)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])
        self.g._handle_serial_line('Scan results printed')
        self.assertFalse(self.g._et_net_screen)
        self.g._execute_item.assert_not_called()

    def test_scan_timeout_does_not_leave_pending_picker_or_reopen_it(self):
        self.open()
        self.press(PX.KEY_R)
        base.CLOCK.now += 21
        self.g._update_wardriving_loop()
        self.assertFalse(self.g._wifi_input_picker['pending'])
        old_rows = list(self.g._attack_scan_results)
        self.g.state.networks = [network()]
        self.g._handle_serial_line('Scan results printed')
        self.assertEqual(self.g._attack_scan_results, old_rows)
        self.assertEqual(self.sent(), ['scan_networks', 'stop'])

    def test_existing_evil_twin_popup_keeps_multi_select_and_portal_routing(self):
        self.g._attack_scan_results = [network(), network('02:00:00:00:00:02')]
        self.g._show_net_selection()
        self.press(PX.KEY_SPACE)
        self.press(PX.KEY_DOWN)
        self.press(PX.KEY_SPACE)
        self.assertEqual(self.g._et_net_selected, {0, 1})
        self.g._draw_net_picker()
        self.assertTrue(any(op == 'text' and args[2] == 'EVIL TWIN — Select Target Network'
                            for op, args in PX.calls))
        self.press(PX.KEY_RETURN)
        self.g._show_portal_selection.assert_called_once_with('ET')
        self.assertFalse(self.g.input_mode)
        self.assertEqual(self.sent(), [])


if __name__ == '__main__':
    unittest.main()
