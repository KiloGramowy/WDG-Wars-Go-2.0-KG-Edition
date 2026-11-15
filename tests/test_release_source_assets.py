"""Distribution checks of the actual published SOLO/Piglet bytes, no device IO."""
import hashlib
import json
from pathlib import Path
import unittest

from watchdogs import kg_firmware

ROOT = Path(__file__).resolve().parents[1]


class ReleaseSourceAssetsTests(unittest.TestCase):
    def test_supplied_solo_image_and_manifest_are_the_exact_accepted_legacy_package(self):
        directory = ROOT / 'firmware/wardriving-solo'
        raw = (directory / 'manifest.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), kg_firmware.LEGACY_MANIFEST_SHA256)
        self.assertEqual(raw, (ROOT / 'tests/fixtures/kg_janos_manifest_v1.json').read_bytes())
        manifest = json.loads(raw)
        image = (directory / manifest['image']['path']).read_bytes()
        self.assertEqual(len(image), manifest['image']['bytes'])
        self.assertEqual(hashlib.sha256(image).hexdigest(), kg_firmware.LEGACY_IMAGE_SHA256)
        self.assertEqual(hashlib.sha256(image).hexdigest(), manifest['image']['sha256'])
        kg_firmware.validate_image_bytes(image)
        for entry in manifest['build_inputs']:
            content = (directory / entry['path']).read_bytes()
            self.assertEqual(len(content), entry['bytes'], entry['path'])
            self.assertEqual(hashlib.sha256(content).hexdigest(), entry['sha256'], entry['path'])

    def test_all_solo_and_piglet_checksum_entries_exist_and_match_packaged_bytes(self):
        for name in ('wardriving-solo', 'piglet-collection'):
            directory = ROOT / 'firmware' / name
            rows = (directory / 'SHA256SUMS').read_text(encoding='utf-8').splitlines()
            self.assertTrue(rows)
            seen = set()
            for row in rows:
                expected, relative = row.split('  ', 1)
                with self.subTest(package=name, path=relative):
                    self.assertNotIn(relative, seen)
                    seen.add(relative)
                    path = directory / relative
                    self.assertTrue(path.resolve().is_relative_to(directory.resolve()))
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
