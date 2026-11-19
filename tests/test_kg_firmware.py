"""Own-fork firmware metadata/download and ROM-read preflight, no real devices."""
import binascii
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs import kg_firmware as kg
from watchdogs.esp32_flash import FlashError


ROOT = Path(__file__).resolve().parents[1]
LEGACY = (ROOT / 'tests/fixtures/kg_janos_manifest_v1.json').read_bytes()


def synthetic_image(*, chip=23, minimum=100, maximum=199, application=True):
    header = bytearray(24)
    header[:4] = bytes((0xE9, 1, 2, 0x3F))
    struct.pack_into('<H', header, 12, chip)
    struct.pack_into('<HH', header, 15, minimum, maximum)
    header[23] = 1
    descriptor = bytearray(256)
    struct.pack_into('<I', descriptor, 0, 0xABCD5432 if application else 0)
    descriptor[16:21] = b'1.6.9'
    data = bytes(descriptor) + b'Synthetic KG SOLO application; never a device image.' * 3
    raw = bytes(header) + struct.pack('<II', 0x42000000, len(data)) + data
    checksum = 0xEF
    for value in data:
        checksum ^= value
    position = ((len(raw) + 16) // 16) * 16 - 1
    raw += b'\0' * (position - len(raw)) + bytes((checksum,))
    return raw + hashlib.sha256(raw).digest()


def manifest_v2(image=None):
    image = synthetic_image() if image is None else image
    data = json.loads(LEGACY)
    data.update(schema=2, firmware_version='1.7.1', solo_contract=1)
    data['target']['write_scope'] = 'application_only'
    data['image'] = {'path': 'janos-1.7.1-solo-xiao-c5-app.bin',
                     'bytes': len(image), 'sha256': hashlib.sha256(image).hexdigest()}
    return data


def partition_table(*, changed=None):
    # Independent tuples from the published v0.9.20 source partition CSV.
    entries = [('nvs', 1, 2, 0x9000, 0x6000),
               ('otadata', 1, 0, 0xF000, 0x2000),
               ('phy_init', 1, 1, 0x11000, 0x1000),
               ('ota_0', 0, 0x10, 0x20000, 0x3F0000),
               ('ota_1', 0, 0x11, 0x410000, 0x3F0000)]
    raw = b''
    for name, kind, subtype, start, size in entries:
        values = (kind, subtype, start, size, 0)
        if changed and name in changed:
            values = changed[name]
        kind, subtype, start, size, flags = values
        raw += struct.pack('<HBBII16sI', 0x50AA, kind, subtype, start, size,
                           name.encode().ljust(16, b'\0'), flags)
    raw += b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(raw).digest()
    return raw.ljust(4096, b'\xff')


def ota_data(sequence=None, *, state=2, second=None, second_state=2):
    def sector(seq, image_state):
        if seq is None:
            return b'\xff' * 4096
        crc = binascii.crc32(struct.pack('<I', seq), 0xFFFFFFFF) & 0xFFFFFFFF
        return struct.pack('<I20sII', seq, b'\xff' * 20, image_state, crc).ljust(4096, b'\xff')
    return sector(sequence, state) + sector(second, second_state)


class HTTPFixture:
    def __init__(self, manifest=None, *, raw=None, image=None):
        self.image = synthetic_image() if image is None else image
        self.raw = json.dumps(manifest_v2(self.image) if manifest is None else manifest,
                              sort_keys=True).encode() if raw is None else raw
        data = json.loads(self.raw)
        self.repo = {'id': 1442123109, 'full_name': kg.REPOSITORY}
        self.release = {'id': 123456, 'tag_name': 'v0.9.20', 'draft': False,
                        'prerelease': False, 'url': kg.API_PREFIX + '/releases/123456',
                        'html_url': 'https://github.com/' + kg.REPOSITORY + '/releases/tag/v0.9.20'}
        self.manifest_asset = self.asset(11, kg.MANIFEST_NAME, len(self.raw), hashlib.sha256(self.raw).hexdigest())
        self.image_asset = self.asset(12, data['image']['path'], data['image']['bytes'], data['image']['sha256'])
        self.release['assets'] = [self.manifest_asset, self.image_asset]
        self.calls = []
        self.final_url = None
        self.override_image = None

    def asset(self, identifier, name, size, digest):
        return {'id': identifier, 'name': name, 'size': size, 'state': 'uploaded',
                'digest': 'sha256:' + digest,
                'url': kg.API_PREFIX + '/releases/assets/' + str(identifier),
                'browser_download_url': f'https://github.com/{kg.REPOSITORY}/releases/download/v0.9.20/{name}'}

    def open(self, request, **kwargs):
        self.calls.append((request.full_url, kwargs))
        if request.full_url == kg.REPO_URL:
            raw = json.dumps(self.repo).encode()
        elif request.full_url == kg.LATEST_URL:
            raw = json.dumps(self.release).encode()
        elif request.full_url == self.manifest_asset['browser_download_url']:
            raw = self.raw
        elif request.full_url == self.image_asset['browser_download_url']:
            raw = self.image if self.override_image is None else self.override_image
        else:
            raise AssertionError('Unexpected request outside exact fixture scope')
        response = io.BytesIO(raw)
        response.geturl = lambda: self.final_url or request.full_url
        return response

    def package(self):
        return kg.fetch_latest_package(open_url=self.open)


class KGFirmwareMetadataTests(unittest.TestCase):
    def test_exact_public_legacy_manifest_uses_firmware_cli_not_game_tag_or_descriptor(self):
        self.assertEqual(hashlib.sha256(LEGACY).hexdigest(), kg.LEGACY_MANIFEST_SHA256)
        fixture = HTTPFixture(raw=LEGACY)
        package = fixture.package()
        self.assertEqual(package.version, '1.7.0')
        self.assertEqual(package.release_tag, 'v0.9.20')
        self.assertEqual(package.image_bytes, 2250896)
        self.assertEqual(package.image_sha256, kg.LEGACY_IMAGE_SHA256)
        self.assertEqual(len(fixture.calls), 3)

    def test_schema_two_positive_package_download_uses_own_asset_and_preserves_exact_bytes(self):
        fixture = HTTPFixture()
        with tempfile.TemporaryDirectory() as temporary, patch.object(kg, '_ports', side_effect=AssertionError('No USB discovery')):
            package = fixture.package()
            self.assertEqual(package.version, '1.7.1')
            path = kg.download_image(package, Path(temporary) / package.image_name, open_url=fixture.open)
            self.assertEqual(path.read_bytes(), fixture.image)
            self.assertEqual(kg.verify_image(package, path).chip_id, 23)
        self.assertTrue(all('LOCOSP' not in url for url, _ in fixture.calls))
        self.assertTrue(all(options['timeout'] == 10 for _, options in fixture.calls))

    def test_wrong_repository_identity_stops_before_reading_any_release(self):
        for value in (1, '1442123109', True, None):
            with self.subTest(value=value):
                fixture = HTTPFixture()
                fixture.repo['id'] = value
                with self.assertRaises(kg.KGFirmwareError): fixture.package()
                self.assertEqual(len(fixture.calls), 1)
        fixture = HTTPFixture()
        fixture.repo['full_name'] = 'LOCOSP/projectZero'
        with self.assertRaises(kg.KGFirmwareError): fixture.package()

    def test_release_identity_and_public_stable_scope_are_mandatory(self):
        changes = ({'draft': True}, {'prerelease': True}, {'id': True},
                   {'url': 'https://api.github.com/repos/LOCOSP/projectZero/releases/123456'},
                   {'html_url': 'https://github.com/other/repository/releases/tag/v0.9.20'},
                   {'tag_name': '../other'})
        for change in changes:
            with self.subTest(change=change):
                fixture = HTTPFixture(); fixture.release.update(change)
                with self.assertRaises(kg.KGFirmwareError): fixture.package()
                self.assertEqual(len(fixture.calls), 2)

    def test_latest_game_release_without_kg_manifest_never_guesses_or_falls_back(self):
        fixture = HTTPFixture()
        fixture.release['assets'] = [fixture.asset(33, 'piglet-kg-uconsole-xiao-c5-app.bin', 100, 'a' * 64)]
        with self.assertRaisesRegex(kg.KGFirmwareError, 'No compatible KG/SOLO package'): fixture.package()
        self.assertEqual(len(fixture.calls), 2)

    def test_missing_duplicate_or_cross_repository_assets_are_rejected(self):
        changes = ({'browser_download_url': 'https://github.com/LOCOSP/projectZero/releases/download/v1.7.3/firmware.zip'},
                   {'browser_download_url': 'https://github.com/' + kg.REPOSITORY + '/releases/download/v0.9.20/janos-solo-manifest.json?other=1'},
                   {'url': 'https://api.github.com/repos/other/repo/releases/assets/11'},
                   {'size': True}, {'digest': None}, {'state': 'new'})
        for change in changes:
            with self.subTest(change=change):
                fixture = HTTPFixture(); fixture.manifest_asset.update(change)
                with self.assertRaises(kg.KGFirmwareError): fixture.package()
        fixture = HTTPFixture()
        fixture.release['assets'].append(dict(fixture.manifest_asset, name='JANOS-SOLO-MANIFEST.JSON'))
        with self.assertRaisesRegex(kg.KGFirmwareError, 'ambiguous'): fixture.package()
        fixture = HTTPFixture(); fixture.release['assets'].append(dict(fixture.image_asset))
        with self.assertRaisesRegex(kg.KGFirmwareError, 'ambiguous'): fixture.package()

    def test_unapproved_schema_one_bytes_cannot_claim_legacy_approval(self):
        data = json.loads(LEGACY); data['package'] += ' changed'
        fixture = HTTPFixture(data)
        with self.assertRaisesRegex(kg.KGFirmwareError, 'Unapproved legacy'): fixture.package()

    def test_schema_contract_version_and_exact_target_types_are_required(self):
        changes = ({'schema': 3}, {'schema': True}, {'solo_contract': 2},
                   {'solo_contract': True}, {'firmware_version': '0.9.20-beta'},
                   {'firmware_version': '01.7.1'}, {'firmware_version': None})
        for change in changes:
            with self.subTest(change=change):
                data = manifest_v2(); data.update(change)
                with self.assertRaises(kg.KGFirmwareError): HTTPFixture(data).package()
        for key, value in [('board', 'ESP32 WROOM'), ('tested_write_offset', '0x10000'),
                           ('tested_partition', 'app0'), ('flash_bytes', True),
                           ('revision_tested', 'v2.0'), ('write_scope', 'full_flash')]:
            with self.subTest(key=key):
                data = manifest_v2(); data['target'][key] = value
                with self.assertRaises(kg.KGFirmwareError): HTTPFixture(data).package()

    def test_manifest_digest_and_duplicate_json_keys_are_verified(self):
        fixture = HTTPFixture(); fixture.raw += b' '
        with self.assertRaises(kg.KGFirmwareError): fixture.package()
        fixture = HTTPFixture()
        fixture.raw = fixture.raw.replace(b'"schema": 2', b'"schema": 1, "schema": 2')
        fixture.manifest_asset.update(size=len(fixture.raw), digest='sha256:' + hashlib.sha256(fixture.raw).hexdigest())
        with self.assertRaisesRegex(kg.KGFirmwareError, 'duplicate'): fixture.package()

    def test_download_rejects_wrong_bytes_before_creating_any_file(self):
        for mutation in (lambda value: value[:-1], lambda value: value + b'excess',
                         lambda value: value[:-1] + bytes((value[-1] ^ 1,))):
            with self.subTest(mutation=mutation):
                fixture = HTTPFixture(); package = fixture.package()
                fixture.override_image = mutation(fixture.image)
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / 'image.bin'
                    with self.assertRaises(kg.KGFirmwareError): kg.download_image(package, path, open_url=fixture.open)
                    self.assertFalse(path.exists())

    def test_forged_package_cannot_send_a_request_to_stock_or_unbounded_image(self):
        package = HTTPFixture().package()
        for changed in (replace(package, image_url='https://github.com/LOCOSP/projectZero/firmware.bin'),
                        replace(package, image_bytes=20 * 1024 * 1024),
                        replace(package, image_sha256='not-a-digest')):
            with self.subTest(changed=changed):
                opener = Mock(side_effect=AssertionError('No network for invalid package'))
                with self.assertRaises(kg.KGFirmwareError): kg.download_image(changed, 'unused.bin', open_url=opener)
                opener.assert_not_called()

    def test_wrong_final_response_or_cross_repo_redirect_is_rejected(self):
        fixture = HTTPFixture(); fixture.final_url = 'https://github.com/LOCOSP/projectZero'
        with self.assertRaises(kg.KGFirmwareError): fixture.package()
        allowed = f'https://release-assets.githubusercontent.com/github-production-release-asset-2e65be/{kg.REPOSITORY_ID}/asset?signature=value'
        self.assertTrue(kg._trusted_redirect(allowed, 'https://github.com/example'))
        self.assertTrue(kg._trusted_redirect(allowed.replace('-2e65be', ''), 'https://github.com/example'))
        self.assertFalse(kg._trusted_redirect(allowed.replace(str(kg.REPOSITORY_ID), '123'), 'https://github.com/example'))
        self.assertFalse(kg._trusted_redirect(allowed.replace('https:', 'http:'), 'https://github.com/example'))
        self.assertFalse(kg._trusted_redirect(allowed.replace('/asset?', '/../asset?'), 'https://github.com/example'))

    def test_image_header_segment_checksum_and_internal_hash_fail_closed(self):
        self.assertEqual(kg.validate_image_bytes(synthetic_image()).min_revision, 100)
        for raw in (synthetic_image(chip=5), synthetic_image(minimum=200, maximum=299),
                    synthetic_image(application=False),
                    synthetic_image()[:-1], b'not firmware'):
            with self.subTest(bytes=len(raw)):
                with self.assertRaises(kg.KGFirmwareError): kg.validate_image_bytes(raw)
        for position in (0, 2, 3, 23, 40, -1):
            raw = bytearray(synthetic_image()); raw[position] ^= 1
            with self.subTest(position=position):
                with self.assertRaises(kg.KGFirmwareError): kg.validate_image_bytes(bytes(raw))

    def test_existing_or_modified_image_is_not_overwritten_or_used(self):
        fixture = HTTPFixture(); package = fixture.package()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.bin'; path.write_bytes(b'preserve existing')
            with self.assertRaises(kg.KGFirmwareError): kg.download_image(package, path, open_url=fixture.open)
            self.assertEqual(path.read_bytes(), b'preserve existing')
            with self.assertRaises(kg.KGFirmwareError): kg.verify_image(package, path)
            with self.assertRaises(kg.KGFirmwareError): kg.verify_image(package, Path(temporary))

    def test_schema_two_fixture_retains_the_already_approved_image_and_version(self):
        # Public source tests must not require a private release-planning file.
        # This is synthetic metadata around the unchanged public legacy image,
        # not an activated or newly published firmware manifest.
        draft = json.loads(LEGACY)
        draft.update(schema=2, firmware_version='1.7.0', solo_contract=1,
                     draft={'publication_authorized': False})
        draft['target']['write_scope'] = 'application_only'
        package = HTTPFixture(draft).package()
        self.assertEqual(package.version, '1.7.0')
        self.assertEqual(package.image_bytes, 2250896)
        self.assertEqual(package.image_sha256, kg.LEGACY_IMAGE_SHA256)
        self.assertIs(draft['draft']['publication_authorized'], False)

    def test_response_size_deadline_and_non_binary_transport_are_bounded(self):
        with self.assertRaisesRegex(kg.KGFirmwareError, 'limit'):
            kg._read_url(kg.REPO_URL, 4, open_url=lambda *_args, **_kw: io.BytesIO(b'12345'))
        response = io.BytesIO(b'abc')
        with patch.object(kg.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(kg.KGFirmwareError, '30-second'):
                kg._read_url(kg.REPO_URL, 20, open_url=lambda *_args, **_kw: response)
        self.assertTrue(response.closed)
        with self.assertRaises(kg.KGFirmwareError):
            kg._json(b'{"firmware_version":NaN}')

    def test_binary_asset_digest_size_and_name_are_bound_to_manifest(self):
        for changes in ({'digest': 'sha256:' + 'a' * 64}, {'size': 1},
                        {'name': 'piglet-kg-uconsole-xiao-c5-app.bin'}):
            with self.subTest(changes=changes):
                fixture = HTTPFixture(); fixture.image_asset.update(changes)
                with self.assertRaises(kg.KGFirmwareError): fixture.package()
                self.assertEqual(len(fixture.calls), 3)


class KGDevicePreflightTests(unittest.TestCase):
    def port(self, device='/dev/ttyACM1', serial='SYNTHETIC-USB-123', location='1-1.2', **kwargs):
        return SimpleNamespace(device=device, vid=0x303A, pid=0x1001,
                               serial_number=serial, location=location, **kwargs)

    def test_usb_binding_survives_renumbering_and_does_not_choose_another_c5(self):
        original = self.port(); identity = kg.bind_usb_identity(original.device, ports=[original])
        returned = self.port(device='/dev/ttyACM4')
        piglet = self.port(device='/dev/ttyACM1', serial='SYNTHETIC-PIGLET-OTHER', location='1-1.3')
        self.assertEqual(kg.find_bound_port(identity, ports=[piglet, returned]), '/dev/ttyACM4')
        self.assertIsNone(kg.find_bound_port(identity, ports=[piglet]))
        self.assertNotIn(original.serial_number, repr(identity))
        self.assertNotIn(original.location, repr(identity))

    def test_unknown_or_ambiguous_usb_identity_stops_before_installation(self):
        for serial, location in ((None, '1-1'), ('000000', '1-1'), ('FFFFFF', '1-1'),
                                 ('SYNTHETIC-USB', None), ('SYNTHETIC-USB', 'unknown')):
            with self.subTest(serial=serial, location=location):
                with self.assertRaises(kg.KGFirmwareError): kg.bind_usb_identity('/dev/ttyACM1', ports=[self.port(serial=serial, location=location)])
        first = self.port(); duplicate = self.port(device='/dev/ttyACM2', location='1-2')
        with self.assertRaises(kg.KGFirmwareError): kg.bind_usb_identity(first.device, ports=[first, duplicate])
        identity = kg.bind_usb_identity(first.device, ports=[first])
        with self.assertRaises(kg.KGFirmwareError): kg.find_bound_port(identity, ports=[first, duplicate])

    def test_exact_partition_layout_requires_valid_md5_and_rejects_piglet_offsets(self):
        kg.validate_partition_table(partition_table())
        for raw in (partition_table(changed={'ota_0': (0, 0x10, 0x10000, 0x3F0000, 0)}),
                    partition_table(changed={'ota_0': (0, 0x10, 0x20000, 0x3F0000, 1)}),
                    partition_table()[:192], b'\xff' * 4096):
            with self.subTest(length=len(raw)):
                with self.assertRaises(kg.KGFirmwareError): kg.validate_partition_table(raw)
        raw = bytearray(partition_table()); raw[8] ^= 1
        with self.assertRaisesRegex(kg.KGFirmwareError, 'MD5'): kg.validate_partition_table(bytes(raw))

    def test_active_ota_zero_or_erased_default_is_required_without_changing_selection(self):
        for raw in (ota_data(), ota_data(1), ota_data(3), ota_data(1, second=3)):
            kg.validate_active_ota(raw)
        for raw in (ota_data(2), ota_data(1, second=4), ota_data(0), ota_data(0xFFFFFFFF)):
            with self.assertRaises(kg.KGFirmwareError): kg.validate_active_ota(raw)

    def test_pending_aborted_or_corrupt_ota_selection_is_rejected(self):
        for state in (0, 1, 3, 4, 5):
            with self.subTest(state=state):
                with self.assertRaises(kg.KGFirmwareError): kg.validate_active_ota(ota_data(1, state=state))
        raw = bytearray(ota_data(1)); raw[28] ^= 1
        with self.assertRaisesRegex(kg.KGFirmwareError, 'CRC'): kg.validate_active_ota(bytes(raw))
        raw = bytearray(ota_data()); raw[128] = 0
        with self.assertRaises(kg.KGFirmwareError): kg.validate_active_ota(bytes(raw))

    def preflight(self, temporary, *, table=None, ota=None, chip='ESP32-C5 (revision v1.0)', flash='Detected flash size: 8MB', board='xiao',
                  security='Security Information:\nSecure Boot: Disabled\nFlash Encryption: Disabled'):
        fixture = HTTPFixture(); package = fixture.package()
        root = Path(temporary); image = root / package.image_name
        image.write_bytes(fixture.image)
        commands, callbacks, visible = [], [], []
        def run_tool(command, on_line, **kwargs):
            commands.append(command)
            self.assertEqual(kwargs['total_timeout'], 30)
            self.assertEqual(kwargs['idle_timeout'], 10)
            if kwargs.get('on_process'): kwargs['on_process'](SimpleNamespace(returncode=0))
            if command[-1] == 'chip-id':
                on_line(chip); on_line('MAC: SYNTHETIC-PRIVATE-IDENTIFIER')
            elif command[-1] == 'flash-id': on_line(flash)
            elif command[-1] == 'get-security-info':
                for line in security.splitlines(): on_line(line)
            elif 'read-flash' in command:
                index = command.index('read-flash')
                self.assertIn(command[index + 1:index + 3], [['0x8000', '0x1000'], ['0xf000', '0x2000']])
                content = partition_table() if command[index + 1] == '0x8000' else ota_data()
                if command[index + 1] == '0x8000' and table is not None: content = table
                if command[index + 1] == '0xf000' and ota is not None: content = ota
                Path(command[-1]).write_bytes(content)
            else: self.fail('ROM preflight must never issue a write/erase command')
        kg.preflight_device(package, image, '/synthetic/game-python', '/dev/ttyACM1', root,
                            run_tool=run_tool, on_process=callbacks.append, on_line=visible.append, board=board)
        return commands, callbacks, visible

    def test_rom_preflight_uses_existing_supervisor_for_only_five_bounded_read_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            commands, callbacks, visible = self.preflight(temporary)
        self.assertEqual(len(commands), 5)
        self.assertEqual(len(callbacks), 5)
        self.assertTrue(all('--after' in command and 'no-reset' in command for command in commands))
        self.assertTrue(all('write-flash' not in command and '--force' not in command for command in commands))
        self.assertNotIn('SYNTHETIC-PRIVATE-IDENTIFIER', '\n'.join(visible))

    def test_wrong_chip_flash_layout_or_active_slot_cannot_reach_a_write(self):
        variants = ({'chip': 'ESP32-C3 (revision v1.0)'}, {'chip': 'ESP32-C5 (revision v2.0)'},
                    {'chip': 'ESP32-C5 (revision v1.1)'},
                    {'flash': 'Detected flash size: 4MB'}, {'table': b'\xff' * 4096},
                    {'ota': ota_data(2)}, {'board': 'wroom'})
        for variant in variants:
            with self.subTest(variant=tuple(variant)):
                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaises(kg.KGFirmwareError): self.preflight(temporary, **variant)

    def test_secure_boot_encryption_unknown_or_contradictory_security_refuses_unsigned_write(self):
        for security in ('Secure Boot: Enabled\nFlash Encryption: Disabled',
                         'Secure Boot: Disabled\nFlash Encryption: Enabled',
                         'Secure Boot: Disabled',
                         'Secure Boot: Disabled\nSecure Boot: Enabled\nFlash Encryption: Disabled',
                         'Secure Boot: unknown\nFlash Encryption: Disabled'):
            with self.subTest(security=security):
                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaises(kg.KGFirmwareError): self.preflight(temporary, security=security)

    def test_supervisor_unconfirmed_child_error_is_preserved_for_existing_cleanup(self):
        fixture = HTTPFixture(); package = fixture.package(); child = object()
        error = FlashError('Synthetic child did not stop', process=child)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / package.image_name; image.write_bytes(fixture.image)
            run = Mock(side_effect=error)
            with self.assertRaises(FlashError) as caught:
                kg.preflight_device(package, image, '/synthetic/python', '/dev/ttyACM1', root, run_tool=run)
            self.assertIs(caught.exception.process, child)
            self.assertEqual(run.call_count, 1)

    def test_stale_readback_or_invalid_image_stops_before_any_rom_command(self):
        fixture = HTTPFixture(); package = fixture.package()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / package.image_name; image.write_bytes(fixture.image)
            table = root / 'kg-partition-table.bin'; table.write_bytes(b'preserve preexisting file')
            run = Mock(side_effect=AssertionError('No device command expected'))
            with self.assertRaisesRegex(kg.KGFirmwareError, 'fresh'):
                kg.preflight_device(package, image, '/synthetic/python', '/dev/ttyACM1', root, run_tool=run)
            self.assertEqual(table.read_bytes(), b'preserve preexisting file')
            table.unlink(); image.write_bytes(b'invalid image')
            with self.assertRaises(kg.KGFirmwareError):
                kg.preflight_device(package, image, '/synthetic/python', '/dev/ttyACM1', root, run_tool=run)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
