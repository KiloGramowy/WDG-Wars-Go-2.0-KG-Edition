"""Actual installer/reconnect methods, fake USB/processes and real local logs.

The synthetic 1.7.5 no-SOLO authorization is not a physical migration test.
No test starts esptool, opens a device, or reads a device backup.
"""
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from watchdogs import kg_firmware as kg
from watchdogs.esp32_flash import FlashError
from test_flash_preflight import FlashEnvironment, GAME_PYTHON


class KGC5RecoveryTests(unittest.TestCase):
    def environment(self):
        env = FlashEnvironment(aio=False, allow_upstream=False)
        self.addCleanup(env.close)
        g = env.game
        g._fw_version = '1.7.5'
        g._fw_generation = 7
        evidence = kg.SoloMigrationEvidence(env.kg_identity, '1.7.5', 7, env.now, 'unrecognized')
        g._kg_fw_migration_authorization = kg.authorize_install(env.kg_package, '1.7.5',
            migration=evidence, identity=env.kg_identity, connection_generation=7,
            confirmed=True, now=env.now)
        g._kg_fw_migration_serial_token = g._kg_serial_token()
        return env

    def attach_log(self, env):
        path = env.root / 'last_run.log'
        logger = logging.getLogger('flash_harness')
        old_level, old_propagate = logger.level, logger.propagate
        handler = logging.FileHandler(path, encoding='utf-8')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        def close():
            logger.removeHandler(handler)
            handler.close()
            logger.setLevel(old_level)
            logger.propagate = old_propagate
        self.addCleanup(close)
        return path, close

    def fail_first(self, env, message='Synthetic first ROM synchronization failed'):
        env.kg_preflight_error = FlashError(message)
        env.flash()
        self.assertEqual(env.flash_commands, [])
        self.assertFalse(env.game._flash_write_started)
        self.assertFalse(env.game._flash_write_complete)
        self.assertEqual(env.game._kg_fw_primary_error, message)
        self.assertEqual(env.game._kg_fw_failed_stage, 'rom-chip-id')
        self.assertFalse(env.game._flash_running)
        self.assertFalse(env.game._serial_busy)
        self.assertFalse(env.game._flash_job_lock.locked())

    def test_first_preflight_failure_uses_bound_direct_reset_without_rom_sync(self):
        env = self.environment()
        env.kg_bound_port = '/dev/ttyACM7'
        env.detect_port = '/dev/ttyACM99'  # A broad detector must not select this.
        self.fail_first(env)
        self.assertEqual(len(env.recovery_commands), 1)
        command, options = env.recovery_commands[0]
        self.assertEqual(command[:2], [GAME_PYTHON, '-u'])
        self.assertTrue(Path(command[2]).is_absolute())
        self.assertEqual(Path(command[2]), env.root / 'watchdogs/esp32_flash.py')
        self.assertEqual(command[3:], ['--reset-c5-usb-to-app', '/dev/ttyACM7'])
        self.assertEqual(options['total_timeout'], 15)
        self.assertEqual(options['idle_timeout'], 5)
        for forbidden in ('get-security-info', '--before', 'chip-id', 'write-flash', 'erase-flash'):
            self.assertNotIn(forbidden, command)
        self.assertIn(('preflight', '/dev/ttyACM7', 'xiao'), env.kg_calls)
        self.assertFalse(any(event[0] == 'detect-port' for event in env.events))
        self.assertTrue(env.game.serial.native_usb)
        self.assertTrue(env.game._kg_fw_recovery_needed)
        self.assertEqual(env.game._fw_version, '')

    def test_recovery_failure_keeps_first_error_stage_and_persisted_actual_output(self):
        env = self.environment()
        path, close_log = self.attach_log(env)
        original = env.helpers.run_esptool
        def process(command, on_line, **kwargs):
            def actual_output(_ignored):
                on_line('Synthetic native USB reset: opening the bound controller')
                on_line('Synthetic control request failed before reset completion')
            return original(command, actual_output if '--reset-c5-usb-to-app' in command else on_line,
                            **kwargs)
        env.helpers.run_esptool = process
        env.recovery_error = FlashError('Synthetic secondary recovery failure')
        self.fail_first(env, 'Synthetic primary ROM control timeout')
        close_log()  # Read the durable file after its logging handler is closed.
        saved = path.read_text(encoding='utf-8')
        self.assertIn('stage=rom-chip-id [FLASH] First failure: Synthetic primary ROM control timeout', saved)
        self.assertIn('stage=return-to-app [FW] Synthetic native USB reset:', saved)
        self.assertIn('Synthetic control request failed before reset completion', saved)
        self.assertIn('Synthetic secondary recovery failure', saved)
        self.assertLess(saved.index('Synthetic primary ROM control timeout'),
                        saved.index('Synthetic secondary recovery failure'))
        self.assertIn('Stopped before write (rom-chip-id)', env.game._flash_status)
        self.assertIn('previous boot unconfirmed', env.game._flash_status)
        self.assertNotIn('KG application restart check', saved)

    def test_actual_recovery_lines_are_redacted_in_output_and_durable_log(self):
        env = self.environment()
        path, close_log = self.attach_log(env)
        original = env.helpers.run_esptool
        mac, owner, secret = '02:11:22:33:44:55', 'synthetic-private-owner', 'SYNTHETIC-PRIVATE-TOKEN'
        dash_mac, windows_owner = 'A2-B3-C4-D5-E6-F7', 'synthetic-windows-owner'
        windows_home = 'C:\\Users\\' + windows_owner
        def process(command, on_line, **kwargs):
            def actual_output(_ignored):
                for line in ('Synthetic real recovery stdout', 'MAC: ' + mac,
                             'Controller MAC address ' + dash_mac,
                             'USB unit ' + env.kg_identity.serial_number,
                             'USB bus ' + env.kg_identity.location,
                             'Serial port /dev/ttyACM7',
                             'Cache /home/' + owner + '/firmware-cache',
                             'Game Python ' + windows_home + '\\.venv\\Scripts\\python.exe',
                             'token: ' + secret):
                    on_line(line)
            return original(command, actual_output if '--reset-c5-usb-to-app' in command else on_line,
                            **kwargs)
        env.helpers.run_esptool = process
        self.fail_first(env)
        close_log()
        saved = path.read_text(encoding='utf-8')
        terminal = '\n'.join(env.output)
        for content in (saved, terminal):
            self.assertIn('Synthetic real recovery stdout', content)
            self.assertIn('[MAC hidden]', content)
            self.assertIn('[USB identity hidden]', content)
            self.assertIn('[bound USB port]', content)
            self.assertIn('/[home]/firmware-cache', content)
            self.assertIn('[home]\\.venv\\Scripts\\python.exe', content)
            self.assertGreaterEqual(content.count('[MAC hidden]'), 2)
            for private in (mac, dash_mac, owner, windows_owner, windows_home, secret, env.kg_identity.serial_number,
                            env.kg_identity.location, '/dev/ttyACM7'):
                self.assertNotIn(private, content)

    def test_other_device_reusing_the_old_port_cannot_receive_recovery_reset(self):
        env = self.environment()
        identity = env.kg_identity
        def port(serial, location):
            return SimpleNamespace(device='/dev/ttyACM1', vid=0x303A, pid=0x1001,
                                   serial_number=serial, location=location)
        ports = [port(identity.serial_number, identity.location)]
        env.kg_helpers.find_bound_port = lambda bound: kg.find_bound_port(bound, ports=ports)
        env.kg_helpers.wait_bound_port = lambda bound, **kwargs: kg.wait_bound_port(
            bound, find_port=env.kg_helpers.find_bound_port, **kwargs)
        def replace_device(game):
            ports[:] = [port('SYNTHETIC-OTHER-C5', '1-99')]
        env.during_kg_preflight = replace_device
        self.fail_first(env)
        self.assertEqual(env.recovery_commands, [])
        self.assertFalse(any(event[0] == 'serial-open' for event in env.events))
        self.assertFalse(any(event[0] == 'detect-port' for event in env.events))
        self.assertFalse(env.game.state.connected)
        self.assertTrue(env.game._kg_fw_recovery_needed)

    def test_usb_and_stop_write_are_not_previous_firmware_confirmation(self):
        env = self.environment()
        self.fail_first(env)
        g = env.game
        self.assertTrue(g.serial.is_open)
        self.assertIn(('serial-command', 'stop'), env.events)
        self.assertIn(('serial-command', 'version'), env.events)
        self.assertEqual(g._fw_version, '')
        self.assertTrue(g._kg_fw_recovery_needed)
        self.assertNotIn('responsive', g._flash_status)
        env.now = g._kg_fw_recovery_deadline + .1
        g._poll_serial()
        self.assertIsNone(g._kg_fw_recovery_deadline)
        self.assertTrue(g._kg_fw_recovery_needed)
        self.assertIn('previous boot unconfirmed', g._flash_status)
        self.assertTrue(any('retain the [KG-FW] block' in line for line in env.output))

    def test_only_a_fresh_matching_previous_janos_response_confirms_recovery(self):
        env = self.environment()
        self.fail_first(env)
        g = env.game
        g.loot = None
        g._solo = SimpleNamespace(confirmed_wifi=False, confirmed_ble=False,
                                  handle_line=lambda line: True)
        g._handle_serial_line('JanOS version: 1.7.0')
        self.assertEqual(g._fw_version, '')
        self.assertTrue(g._kg_fw_recovery_needed)
        g._handle_serial_line('JanOS version: 1.7.5')
        self.assertEqual(g._fw_version, '1.7.5')
        self.assertFalse(g._kg_fw_recovery_needed)
        self.assertIsNone(g._kg_fw_recovery_deadline)
        self.assertIn('previous JanOS 1.7.5 responsive', g._flash_status)
        self.assertIn('Stopped before write (rom-chip-id)', g._flash_status)
        self.assertEqual(env.flash_commands, [])

    def test_live_or_uninspectable_child_blocks_reconnect_even_for_owner_with_flags_clear(self):
        for result in (None, OSError('Synthetic child state unavailable')):
            with self.subTest(result=type(result).__name__):
                env = self.environment()
                g = env.game
                g._flash_running = g._serial_busy = False
                g.serial = None
                g._esp32 = g.state.connected = False
                g._flash_reconnect_thread = threading.get_ident()
                poll = Mock(side_effect=result) if isinstance(result, BaseException) else Mock(return_value=None)
                g._flash_process = SimpleNamespace(poll=poll)
                before = list(env.events)
                self.assertFalse(g._try_reconnect_esp32(_flash_owner=True))
                self.assertEqual(env.events, before)
                self.assertIsNone(g.serial)
                poll.assert_called_once_with()

    def test_failed_close_keeps_owned_serial_and_cannot_be_replaced_by_reconnect(self):
        env = self.environment()
        previous = env.game.serial
        env.close_error = OSError('Synthetic existing serial close is unconfirmed')
        env.flash()
        self.assertIs(env.game.serial, previous)
        self.assertTrue(previous.is_open)
        self.assertEqual(env.flash_commands, [])
        self.assertEqual(env.recovery_commands, [])
        self.assertFalse(env.game._flash_device_acquired)
        self.assertFalse(env.game._try_reconnect_esp32())
        self.assertIs(env.game.serial, previous)
        self.assertFalse(any(event[0] == 'serial-open' for event in env.events))

    def test_poll_read_and_close_failure_retains_bound_handle_without_retries(self):
        env = self.environment()
        g = env.game
        connection = SimpleNamespace(is_open=True)
        manager = SimpleNamespace(serial_conn=connection, is_open=True, native_usb=True,
            read_available=Mock(side_effect=OSError('Synthetic native USB read failed')),
            close=Mock(side_effect=OSError('Synthetic native USB close is unconfirmed')))
        g.serial = manager
        g._kg_fw_usb_identity = env.kg_identity
        g._flash_running = g._serial_busy = False
        g._kg_probe = None
        g._kg_probe_requested = False
        g._solo = None
        factory = Mock(side_effect=AssertionError('A retained handle cannot be replaced'))
        env.namespace['SerialManager'] = factory
        g._poll_serial()
        self.assertIs(g.serial, manager)
        self.assertIs(g.serial.serial_conn, connection)
        self.assertFalse(g._esp32)
        self.assertFalse(g.state.connected)
        manager.read_available.assert_called_once_with()
        manager.close.assert_called_once_with()
        self.assertTrue(any('Serial close unconfirmed; port reserved' in line for line in env.output))
        output_after_failure = list(env.output)
        g._poll_serial()
        g._poll_serial()
        self.assertEqual(env.output, output_after_failure)
        manager.read_available.assert_called_once_with()
        manager.close.assert_called_once_with()
        self.assertFalse(g._try_reconnect_esp32())
        factory.assert_not_called()
        self.assertIs(g.serial, manager)
        self.assertIs(g.serial.serial_conn, connection)
        self.assertFalse(env.flash_commands or env.recovery_commands)


if __name__ == '__main__':
    unittest.main()
