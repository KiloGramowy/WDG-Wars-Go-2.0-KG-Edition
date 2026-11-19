"""Explicit no-SOLO migration and private read-only recovery snapshots.

Only synthetic flash/USB fixtures and the unchanged public application image
are read. No serial device, subprocess esptool or firmware write is used.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from watchdogs import kg_firmware as kg
from watchdogs.esp32_flash import FlashError
from test_kg_firmware import partition_table, ota_data


ROOT = Path(__file__).resolve().parents[1]
IMAGE_NAME = 'janos-1.7.0-solo-xiao-c5-app.bin'
IMAGE = ROOT / 'firmware/wardriving-solo' / IMAGE_NAME
PACKAGE = kg.KGPackage('1.7.0', 'v0.9.21', IMAGE_NAME, 2250896,
                       kg.LEGACY_IMAGE_SHA256,
                       'https://github.com/' + kg.REPOSITORY + '/releases/download/v0.9.21/' + IMAGE_NAME,
                       kg.LEGACY_MANIFEST_SHA256)
IDENTITY = kg.USBIdentity(0x303A, 0x1001, 'SYNTHETIC-NATIVE-C5', 'SYNTHETIC-USB-1')


def evidence(**changes):
    return replace(kg.SoloMigrationEvidence(IDENTITY, '1.7.5', 3, 90, 'unrecognized'), **changes)


def authorize(**changes):
    values = dict(migration=evidence(), identity=IDENTITY, connection_generation=3,
                  confirmed=True, now=100)
    values.update(changes)
    return kg.authorize_install(PACKAGE, '1.7.5', **values)


class ExplicitMigrationTests(unittest.TestCase):
    def test_ordinary_downgrade_stays_blocked(self):
        with self.assertRaisesRegex(kg.KGFirmwareError, "published KG package is older"):
            kg.authorize_install(PACKAGE, '1.7.5')

    def test_same_version_reinstall_and_ordinary_upgrade_remain_available(self):
        for version in ('1.7.0', '1.6.9'):
            with self.subTest(version=version):
                self.assertIsNone(kg.authorize_install(PACKAGE, version))

    def test_unknown_reported_version_cannot_silently_authorize_installation(self):
        for version in ('', None, 'incomplete', '1.7'):
            with self.subTest(version=version):
                with self.assertRaises(kg.KGFirmwareError):
                    kg.authorize_install(PACKAGE, version)

    def test_fresh_explicit_175_no_solo_migration_gets_a_bound_transaction(self):
        token = authorize()
        self.assertEqual(token.reported_version, '1.7.5')
        self.assertEqual(token.package_version, '1.7.0')
        self.assertEqual(token.identity, IDENTITY)
        self.assertEqual(token.confirmed_at, 100)
        # Recovery reads can take minutes in ROM. This does not fake another
        # caps response or invalidate a still exclusively owned transaction.
        self.assertIs(kg.validate_authorization(token, PACKAGE, IDENTITY, 3), token)

    def test_confirmation_is_required_even_for_fresh_unsupported_response(self):
        for confirmed in (False, None, 1, 'yes'):
            with self.subTest(confirmed=confirmed):
                with self.assertRaisesRegex(kg.KGFirmwareError, 'older'):
                    authorize(confirmed=confirmed)

    def test_timeout_partial_unknown_and_supported_responses_cannot_migrate(self):
        for response in ('', 'timeout', 'partial', 'supported', None):
            with self.subTest(response=response):
                with self.assertRaisesRegex(kg.KGFirmwareError, 'unrecognized'):
                    authorize(migration=evidence(response=response))

    def test_stale_future_or_invalid_clock_is_not_fresh_evidence(self):
        cases = [dict(now=121), dict(now=89), dict(now=float('nan')),
                 dict(now=True), dict(now=-1),
                 dict(migration=evidence(observed_at=float('inf'))),
                 dict(migration=evidence(observed_at=-1))]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(kg.KGFirmwareError, 'expired'):
                    authorize(**changes)

    def test_other_usb_generation_or_reported_version_cannot_reuse_evidence(self):
        for changed in (evidence(identity=replace(IDENTITY, serial_number='OTHER-DEVICE')),
                        evidence(connection_generation=2), evidence(connection_generation=True),
                        evidence(reported_version='1.7.4')):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(kg.KGFirmwareError, 'USB session'):
                    authorize(migration=changed)

    def test_exception_is_only_for_exact_approved_170_solo_bits(self):
        packages = [replace(PACKAGE, image_sha256='a' * 64),
                    replace(PACKAGE, manifest_sha256='a' * 64),
                    replace(PACKAGE, image_bytes=2250895),
                    replace(PACKAGE, version='1.7.1', image_name=IMAGE_NAME.replace('1.7.0', '1.7.1'),
                            image_url=PACKAGE.image_url.replace('1.7.0', '1.7.1'))]
        for package in packages:
            with self.subTest(package=package):
                with self.assertRaisesRegex(kg.KGFirmwareError, 'approved JanOS 1.7.0'):
                    kg.authorize_install(package, '1.7.5', migration=evidence(), identity=IDENTITY,
                                         connection_generation=3, confirmed=True, now=100)

    def test_confirmed_transaction_rejects_package_usb_or_session_change(self):
        token = authorize()
        for package, identity, generation in [
                (replace(PACKAGE, image_sha256='b' * 64), IDENTITY, 3),
                (PACKAGE, replace(IDENTITY, location='OTHER-PORT'), 3),
                (PACKAGE, IDENTITY, 4)]:
            with self.subTest(generation=generation):
                with self.assertRaises(kg.KGFirmwareError):
                    kg.validate_authorization(token, package, identity, generation)


class MigrationRecoverySnapshotTests(unittest.TestCase):
    def prepare(self, temporary):
        root = Path(temporary)
        attempt = root / 'attempt'; attempt.mkdir()
        image = attempt / IMAGE_NAME; image.write_bytes(IMAGE.read_bytes())
        table, ota = partition_table(), ota_data()
        (attempt / 'kg-partition-table.bin').write_bytes(table)
        (attempt / 'kg-otadata.bin').write_bytes(ota)
        raw = bytearray(b'\xff' * kg.FLASH_BYTES)
        raw[0x8000:0x9000] = table
        raw[0xF000:0x11000] = ota
        private_fixture = b'SYNTHETIC_PRIVATE_NVS_NO_REAL_KEY!'
        raw[0x9000:0x9000 + len(private_fixture)] = private_fixture
        raw[kg.APP_OFFSET:kg.APP_OFFSET + 2252800] = b'\xa5' * 2252800
        self.assertEqual(len(raw), kg.FLASH_BYTES)
        return root, attempt, image, bytes(raw)

    def backup(self, root, attempt, image, raw, *, guard=None, run=None):
        commands, messages, callbacks = [], [], []
        def read_tool(command, on_line, **options):
            commands.append(command)
            callbacks.append(options)
            self.assertEqual(command[-4:-1], ['read-flash', '0x0', '0x800000'])
            self.assertIn('--no-stub', command)
            self.assertEqual(command[command.index('--before') + 1], 'no-reset')
            self.assertEqual(command[command.index('--after') + 1], 'no-reset')
            self.assertNotIn('-b', command)
            self.assertNotIn('write-flash', command)
            self.assertNotIn('erase-flash', command)
            on_line('MAC: SYNTHETIC-PRIVATE-IDENTIFIER')
            on_line('Reading 50%')
            Path(command[-1]).write_bytes(raw)
        return (kg.backup_migration_device(
                    PACKAGE, image, '/synthetic/game python', '/dev/ttyACM1', attempt,
                    root / 'private-recovery', IDENTITY, authorize(),
                    run_tool=run or read_tool, check_current_device=guard or (lambda: True),
                    on_process=Mock(), on_line=messages.append), commands, messages, callbacks)

    def test_two_independent_complete_reads_and_app_only_recovery_are_retained_privately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            result, commands, visible, options = self.backup(root, attempt, image, raw)
            self.assertEqual(len(commands), 2)
            self.assertNotEqual(commands[0][-1], commands[1][-1])
            self.assertFalse(result.directory.is_relative_to(attempt))
            self.assertEqual(result.full_sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(result.rollback_bytes, 2252800)
            original = raw[kg.APP_OFFSET:kg.APP_OFFSET + result.rollback_bytes]
            self.assertEqual((result.directory / 'original-application-sectors.bin').read_bytes(), original)
            self.assertEqual(result.rollback_sha256, hashlib.sha256(original).hexdigest())
            saved = json.loads((result.directory / 'backup.json').read_bytes())
            self.assertEqual(saved['status'], 'complete')
            self.assertEqual(len(saved['independent_full_flash_reads']), 2)
            self.assertEqual(saved['application_rollback']['offset'], 0x20000)
            self.assertEqual(saved['usb_binding']['serial_number'], IDENTITY.serial_number)
            self.assertTrue(saved['device_backup_private_never_upload'])
            self.assertNotIn('SYNTHETIC-PRIVATE-IDENTIFIER', '\n'.join(visible))
            self.assertNotIn('SYNTHETIC_PRIVATE_NVS', '\n'.join(visible))
            self.assertTrue(all(x['total_timeout'] == 600 and x['idle_timeout'] == 60 for x in options))
            if kg.os.name == 'posix':
                self.assertEqual(stat.S_IMODE(result.directory.stat().st_mode), 0o700)
                for p in result.directory.iterdir():
                    self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_missing_preflight_stops_before_any_recovery_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            (attempt / 'kg-partition-table.bin').unlink()
            run = Mock(side_effect=AssertionError('No read/write without preflight'))
            with self.assertRaises(kg.KGFirmwareError):
                self.backup(root, attempt, image, raw, run=run)
            run.assert_not_called()

    def test_cancelled_before_start_never_starts_a_device_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            run = Mock(side_effect=AssertionError('No device command after cancellation'))
            with self.assertRaisesRegex(kg.KGFirmwareError, 'cancelled'):
                self.backup(root, attempt, image, raw, guard=lambda: False, run=run)
            run.assert_not_called()

    def test_disconnect_after_first_read_cannot_start_second_read_or_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            guard = Mock(side_effect=[True, True, False])
            run = Mock(side_effect=lambda command, *_args, **_kw: Path(command[-1]).write_bytes(raw))
            with self.assertRaisesRegex(kg.KGFirmwareError, 'USB identity changed'):
                self.backup(root, attempt, image, raw, guard=guard, run=run)
            self.assertEqual(run.call_count, 1)

    def test_partial_readback_cannot_authorize_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            run = Mock(side_effect=lambda command, *_args, **_kw: Path(command[-1]).write_bytes(raw[:-1]))
            with self.assertRaisesRegex(kg.KGFirmwareError, 'complete private 8MiB'):
                self.backup(root, attempt, image, raw, run=run)
            self.assertEqual(run.call_count, 1)

    def test_independent_full_reads_must_match_before_rollback_manifest_is_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            reads = [raw, raw[:-1] + b'\x00']
            run = Mock(side_effect=lambda command, *_args, **_kw: Path(command[-1]).write_bytes(reads.pop(0)))
            with self.assertRaisesRegex(kg.KGFirmwareError, 'do not match'):
                self.backup(root, attempt, image, raw, run=run)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(list((root / 'private-recovery').rglob('backup.json')), [])

    def test_full_read_metadata_must_still_match_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            changed = bytearray(raw); changed[0x8000] ^= 1
            with self.assertRaisesRegex(kg.KGFirmwareError, 'preflight'):
                self.backup(root, attempt, image, bytes(changed))

    def test_supervisor_timeout_preserves_child_for_existing_flash_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            child = object(); error = FlashError('Synthetic timeout', process=child)
            run = Mock(side_effect=error)
            with self.assertRaises(FlashError) as caught:
                self.backup(root, attempt, image, raw, run=run)
            self.assertIs(caught.exception, error)
            self.assertIs(caught.exception.process, child)
            self.assertEqual(run.call_count, 1)

    def test_backup_directory_must_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            saved = root / 'private-recovery'; saved.write_bytes(b'preserved unrelated file')
            run = Mock(side_effect=AssertionError('No read into invalid directory'))
            with self.assertRaisesRegex(kg.KGFirmwareError, 'owner-only'):
                self.backup(root, attempt, image, raw, run=run)
            run.assert_not_called()
            self.assertEqual(saved.read_bytes(), b'preserved unrelated file')


    def test_permission_error_stops_before_read_or_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            run = Mock(side_effect=AssertionError('No device operation without private storage'))
            with patch.object(kg.os, 'open', side_effect=PermissionError('Synthetic access denied')):
                with self.assertRaisesRegex(kg.KGFirmwareError, 'PermissionError'):
                    self.backup(root, attempt, image, raw, run=run)
            run.assert_not_called()

    def test_disk_failure_cannot_return_a_ready_backup_or_start_a_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, attempt, image, raw = self.prepare(temporary)
            run = Mock(side_effect=lambda command, *_args, **_kw: Path(command[-1]).write_bytes(raw))
            with patch.object(kg, '_private_backup_file', side_effect=OSError('Synthetic disk full')):
                with self.assertRaisesRegex(kg.KGFirmwareError, 'no firmware write'):
                    self.backup(root, attempt, image, raw, run=run)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(list((root / 'private-recovery').rglob('backup.json')), [])


if __name__ == '__main__':
    unittest.main()
