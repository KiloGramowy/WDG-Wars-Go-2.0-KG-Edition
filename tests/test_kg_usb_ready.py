"""Bound-identity USB enumeration and ROM stages; no USB/serial processes."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs import kg_firmware as kg
from watchdogs.esp32_flash import FlashError
from test_kg_firmware import HTTPFixture, partition_table, ota_data


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class KGUSBReadinessTests(unittest.TestCase):
    def setUp(self):
        self.identity = kg.USBIdentity(0x303A, 0x1001, 'SYNTHETIC-USB-UNIT', '1-2')
        self.clock = Clock()

    def wait(self, finder, **kwargs):
        return kg.wait_bound_port(self.identity, timeout=1, stable_for=.2,
            poll_interval=.05, monotonic=self.clock.monotonic,
            sleep=self.clock.sleep, find_port=finder, **kwargs)

    def timeline(self, rows):
        def find(identity):
            self.assertIs(identity, self.identity)
            return next(value for at, value in reversed(rows) if at <= self.clock.now)
        return find

    def test_existing_bound_port_requires_repeated_stable_observations(self):
        finder = Mock(side_effect=self.timeline([(0, '/dev/ttyACM1')]))
        self.assertEqual(self.wait(finder), '/dev/ttyACM1')
        self.assertGreaterEqual(self.clock.now, .2)
        self.assertGreaterEqual(finder.call_count, 2)
        self.assertTrue(self.clock.sleeps)

    def test_disappearance_and_renumbering_restart_the_stability_window(self):
        finder = self.timeline([(0, '/dev/ttyACM1'), (.1, None), (.15, '/dev/ttyACM4')])
        self.assertEqual(self.wait(finder), '/dev/ttyACM4')
        self.assertGreaterEqual(self.clock.now, .35)

    def test_a_port_that_returns_after_another_number_needs_a_new_window(self):
        finder = self.timeline([(0, '/dev/ttyACM1'), (.1, '/dev/ttyACM4'), (.25, '/dev/ttyACM1')])
        self.assertEqual(self.wait(finder), '/dev/ttyACM1')
        self.assertGreaterEqual(self.clock.now, .45)

    def test_missing_bound_device_stops_at_the_deadline(self):
        with self.assertRaisesRegex(kg.KGFirmwareError, 'readiness deadline'):
            self.wait(lambda identity: None)
        self.assertAlmostEqual(self.clock.now, 1)
        self.assertTrue(all(0 < value <= .05 for value in self.clock.sleeps))

    def test_discovery_returning_a_port_after_deadline_is_not_accepted(self):
        def late(identity):
            self.clock.now += 2
            return '/dev/ttyACM1'
        with self.assertRaisesRegex(kg.KGFirmwareError, 'readiness deadline'):
            self.wait(late)
        self.assertEqual(self.clock.sleeps, [])

    def test_cancel_before_or_during_discovery_never_returns_a_port(self):
        finder = Mock(return_value='/dev/ttyACM1')
        with self.assertRaisesRegex(kg.KGFirmwareError, 'cancelled'):
            self.wait(finder, cancelled=lambda: True)
        finder.assert_not_called()
        cancelled = [False]
        def discover(identity):
            cancelled[0] = True
            return '/dev/ttyACM1'
        with self.assertRaisesRegex(kg.KGFirmwareError, 'cancelled'):
            self.wait(discover, cancelled=lambda: cancelled[0])
        self.assertEqual(self.clock.sleeps, [])

    def test_ambiguous_identity_fails_immediately_without_a_retry(self):
        error = kg.KGFirmwareError('The returning KG USB identity is ambiguous')
        finder = Mock(side_effect=error)
        with self.assertRaises(kg.KGFirmwareError) as caught:
            self.wait(finder)
        self.assertIs(caught.exception, error)
        finder.assert_called_once_with(self.identity)
        self.assertEqual(self.clock.sleeps, [])

    def test_reused_old_tty_belongs_to_other_c5_and_is_not_selected(self):
        def port(device, serial, location):
            return SimpleNamespace(device=device, serial_number=serial, location=location,
                                   vid=0x303A, pid=0x1001)
        ports = [port('/dev/ttyACM1', 'SYNTHETIC-OTHER-DEVICE', '1-3'),
                 port('/dev/ttyACM4', self.identity.serial_number, self.identity.location)]
        self.assertEqual(self.wait(lambda identity: kg.find_bound_port(identity, ports=ports)),
                         '/dev/ttyACM4')

    def test_invalid_timing_identity_or_port_is_rejected(self):
        for options in ({'timeout': 0}, {'timeout': True}, {'timeout': float('inf')},
                        {'stable_for': -1}, {'stable_for': float('nan')},
                        {'stable_for': 2, 'timeout': 1}, {'poll_interval': 0}):
            with self.subTest(options=options), self.assertRaises(kg.KGFirmwareError):
                kg.wait_bound_port(self.identity, **options)
        with self.assertRaisesRegex(kg.KGFirmwareError, 'No bound'):
            kg.wait_bound_port(None)
        for value in ('', 42, True):
            with self.subTest(port=value), self.assertRaisesRegex(kg.KGFirmwareError, 'Invalid bound'):
                self.wait(lambda identity: value)


class KGPreflightStagesTests(unittest.TestCase):
    def prepare(self, root):
        fixture = HTTPFixture()
        package = fixture.package()
        image = root / package.image_name
        image.write_bytes(fixture.image)
        return package, image

    def test_stages_precede_each_bounded_read_only_operation(self):
        events = []
        def tool(command, on_line, **kwargs):
            operation = command[command.index('--no-stub') + 1]
            events.append(('tool', operation))
            self.assertEqual(kwargs['total_timeout'], 30)
            self.assertEqual(kwargs['idle_timeout'], 10)
            self.assertEqual(command[command.index('--before') + 1],
                             'usb-reset' if operation == 'chip-id' else 'no-reset')
            self.assertEqual(command[command.index('--after') + 1], 'no-reset')
            if operation == 'chip-id': on_line('ESP32-C5 (revision v1.0)')
            elif operation == 'flash-id': on_line('Detected flash size: 8MB')
            elif operation == 'get-security-info':
                on_line('Secure Boot: Disabled'); on_line('Flash Encryption: Disabled')
            elif operation == 'read-flash':
                Path(command[-1]).write_bytes(partition_table() if command[-3] == '0x8000' else ota_data())
            else: self.fail('Preflight issued a non-read-only operation')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, image = self.prepare(root)
            kg.preflight_device(package, image, '/synthetic/game-python', '/dev/ttyACM4', root,
                run_tool=tool, on_stage=lambda value: events.append(('stage', value)))
        self.assertEqual(events, [('stage', 'rom-chip-id'), ('tool', 'chip-id'),
            ('stage', 'rom-flash-id'), ('tool', 'flash-id'),
            ('stage', 'rom-security'), ('tool', 'get-security-info'),
            ('stage', 'partition-table'), ('tool', 'read-flash'),
            ('stage', 'active-slot'), ('tool', 'read-flash')])

    def test_first_preflight_failure_keeps_error_and_never_advances_or_writes(self):
        clock = Clock()
        identity = kg.USBIdentity(0x303A, 0x1001, 'SYNTHETIC-USB-UNIT', '1-2')
        observations = iter(['/dev/ttyACM1', None, '/dev/ttyACM4'])
        def find(bound):
            self.assertIs(bound, identity)
            return next(observations, '/dev/ttyACM4')
        port = kg.wait_bound_port(identity, timeout=1, stable_for=.1,
            monotonic=clock.monotonic, sleep=clock.sleep, find_port=find)
        failure = FlashError('Synthetic first ROM synchronization failed')
        calls, stages, visible = [], [], []
        def fail_first(command, on_line, **kwargs):
            calls.append(command)
            on_line('Synthetic ROM sync diagnostic')
            raise failure
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, image = self.prepare(root)
            with self.assertRaises(FlashError) as caught:
                kg.preflight_device(package, image, '/synthetic/game-python', port, root,
                    run_tool=fail_first, on_stage=stages.append, on_line=visible.append)
            self.assertIs(caught.exception, failure)
            self.assertEqual(sorted(path.name for path in root.iterdir()), [image.name])
        self.assertEqual(stages, ['rom-chip-id'])
        self.assertEqual(visible, ['Synthetic ROM sync diagnostic'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], 'chip-id')
        self.assertEqual(calls[0][calls[0].index('-p') + 1], '/dev/ttyACM4')
        self.assertNotIn('/dev/ttyACM1', calls[0])

    def test_stage_callback_failure_stops_before_any_tool_operation(self):
        tool = Mock(side_effect=AssertionError('Stage failure must not start ROM operation'))
        error = RuntimeError('Synthetic diagnostic destination failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package, image = self.prepare(root)
            with self.assertRaises(RuntimeError) as caught:
                kg.preflight_device(package, image, '/synthetic/game-python', '/dev/ttyACM4', root,
                    run_tool=tool, on_stage=Mock(side_effect=error))
        self.assertIs(caught.exception, error)
        tool.assert_not_called()


if __name__ == '__main__':
    unittest.main()
