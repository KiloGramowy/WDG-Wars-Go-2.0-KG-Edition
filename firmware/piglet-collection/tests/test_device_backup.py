"""Offline fixtures for the packaged read-only backup helper; never opens USB."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock
import zlib


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("piglet_device_backup", ROOT / "device_backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def table_fixture(entries=backup.EXPECTED_PARTITIONS):
    rows = b"".join(struct.pack("<HBBII16sI", 0x50AA, kind, subtype, address, size,
                                label.encode("ascii"), flags)
                    for kind, subtype, address, size, label, flags in entries)
    rows += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(rows).digest()
    return rows.ljust(backup.TABLE_BYTES, b"\xff")


def ota_fixture(first=(1, 0xFFFFFFFF), second=(0, 0xFFFFFFFF)):
    def row(values):
        seq, state = values
        crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF
        return struct.pack("<I20xII", seq, state, crc).ljust(0x1000, b"\xff")
    return row(first) + row(second)


def flash_fixture(ota=None):
    result = bytearray(b"\xff" * backup.FLASH_BYTES)
    result[backup.TABLE_OFFSET:backup.TABLE_OFFSET + backup.TABLE_BYTES] = table_fixture()
    result[backup.OTA_OFFSET:backup.OTA_OFFSET + backup.OTA_BYTES] = ota or ota_fixture()
    result[0x10000] = 0xE9
    return bytes(result)


class DeviceBackupFixtures(unittest.TestCase):
    def test_physical_ota_layout_with_nonempty_valid_sequence_zero(self):
        entries, slot = backup.parse_ota_selection(ota_fixture())
        self.assertEqual(slot, 0)
        self.assertEqual([entry["sequence"] for entry in entries], [1, 0])
        self.assertTrue(all(entry["valid"] for entry in entries))
        self.assertEqual([entry["crc"] for entry in entries], [0x4743989A, 0xFFFFFFFF])

    def test_md5_and_all_expected_partition_boundaries(self):
        rows = backup.parse_partition_table(table_fixture())
        self.assertEqual(len(rows), 6)
        app = next(row for row in rows if row["label"] == "app0")
        self.assertEqual((app["offset"], app["size"]), (0x10000, 0x330000))
        damaged = bytearray(table_fixture()); damaged[12] ^= 1
        with self.assertRaisesRegex(backup.BackupError, "MD5"):
            backup.parse_partition_table(bytes(damaged))

    def test_unusual_partition_layout_and_encrypted_flags_are_rejected(self):
        for entries in (backup.EXPECTED_PARTITIONS[:-1],
                        ((1, 2, 0x9000, 0x5000, "nvs", 1),) + backup.EXPECTED_PARTITIONS[1:]):
            with self.assertRaises(backup.BackupError):
                backup.parse_partition_table(table_fixture(entries))
        with self.assertRaises(backup.BackupError):
            backup.parse_partition_table(b"\xff" * backup.TABLE_BYTES)

    def test_bad_crc_invalid_aborted_and_erased_ota_do_not_establish_a_slot(self):
        for data in (b"\xff" * backup.OTA_BYTES, ota_fixture((1, 3), (0, 4)),
                     b"\x00" * backup.OTA_BYTES):
            with self.assertRaises(backup.BackupError):
                backup.parse_ota_selection(data)
        with self.assertRaises(backup.BackupError):
            backup.parse_ota_selection(ota_fixture((1, 17)))
        with self.assertRaises(backup.BackupError):
            backup.parse_ota_selection(ota_fixture((0, 0xFFFFFFFF)))

    def test_maximum_crc_valid_sequence_selects_correct_ota_slot(self):
        entries, slot = backup.parse_ota_selection(ota_fixture((1, 2), (2, 2)))
        self.assertTrue(all(row["valid"] for row in entries))
        self.assertEqual(slot, 1)
        with self.assertRaisesRegex(backup.BackupError, "not app0"):
            backup.analyze_flash(flash_fixture(ota_fixture((1, 2), (2, 2))))

    def test_analysis_extracts_verified_app0_and_rejects_short_or_bad_header(self):
        image = flash_fixture()
        result = backup.analyze_flash(image)
        self.assertEqual(result["active_slot"], "app0")
        self.assertEqual(result["active_slot_bytes"], 0x330000)
        self.assertEqual(result["app_slot_sha256"], backup.sha256(image[0x10000:0x340000]))
        with self.assertRaises(backup.BackupError):
            backup.analyze_flash(image[:-1])
        damaged = bytearray(image); damaged[0x10000] = 0xFF
        with self.assertRaisesRegex(backup.BackupError, "header"):
            backup.analyze_flash(damaged)

    def test_security_model_and_density_gate(self):
        esp = types.SimpleNamespace(CHIP_NAME="ESP32-C5", get_security_info=lambda: {"flags": 0},
                                    get_chip_revision=lambda: 100)
        self.assertEqual(backup.validate_hardware(esp)["revision"], 100)
        backup.validate_flash_id(0x172085)
        with self.assertRaises(backup.BackupError):
            backup.validate_flash_id(0x162085)
        esp.CHIP_NAME = "ESP32-S3"
        with self.assertRaises(backup.BackupError):
            backup.validate_hardware(esp)
        esp.CHIP_NAME = "ESP32-C5"; esp.get_security_info = lambda: {"flags": 1}
        with self.assertRaises(backup.BackupError):
            backup.validate_hardware(esp)

    def test_two_independent_64k_reads_and_local_reread(self):
        image = flash_fixture()
        offsets = []
        esp = types.SimpleNamespace(read_flash=lambda offset, size:
            offsets.append((offset, size)) or image[offset:offset + size])
        with tempfile.TemporaryDirectory() as temp, mock.patch("builtins.print"):
            first = backup.read_one_flash(esp, Path(temp), 1)
            second = backup.read_one_flash(esp, Path(temp), 2)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["sha256"], backup.sha256(image))
            self.assertEqual(len(offsets), 256)
            self.assertEqual(offsets[:128], offsets[128:])
            self.assertTrue(all(size == 0x10000 for _, size in offsets))
            self.assertEqual((Path(temp) / first["path"]).read_bytes(), image)
            with self.assertRaisesRegex(backup.BackupError, "already exists"):
                backup.read_one_flash(esp, Path(temp), 1)

    def test_short_read_keeps_partial_and_never_claims_a_complete_backup(self):
        esp = types.SimpleNamespace(read_flash=lambda offset, size: b"\x00" * (size - 1))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(backup.BackupError, "Short flash read"):
                backup.read_one_flash(esp, Path(temp), 1)
            self.assertFalse((Path(temp) / "flash-read-1.bin").exists())
            self.assertTrue((Path(temp) / "flash-read-1.bin.partial").exists())

    def test_cli_requires_explicit_port_and_output_without_loading_esptool(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            backup.main([])
        with mock.patch.object(backup, "backup_device", return_value={}) as called:
            self.assertEqual(backup.main(["--port", "SYNTHETIC", "--output", "synthetic-output",
                                          "--before", "no-reset"]), 0)
            self.assertEqual(called.call_args.args, ("SYNTHETIC", Path("synthetic-output"), "no-reset"))

    def test_no_persistent_programming_api_or_machine_specific_default(self):
        source = (ROOT / "device_backup.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed = {"get_security_info", "get_chip_revision", "flash_id", "read_flash"}
        actual = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "esp"}
        self.assertEqual(actual, allowed)
        for forbidden in ("write_flash", "erase_flash", "erase_region", "write_efuse", "COM6", "C:/", "C:\\"):
            self.assertNotIn(forbidden, source)

    def test_full_backup_flow_through_fake_esptool_and_private_local_files(self):
        image = flash_fixture()
        port = types.SimpleNamespace(close=mock.Mock())
        esp = types.SimpleNamespace(CHIP_NAME="ESP32-C5", IS_STUB=True, _port=port,
            get_security_info=lambda: {"flags": 0}, get_chip_revision=lambda: 100,
            flash_id=lambda cache=False: 0x172085,
            read_flash=lambda offset, size: image[offset:offset + size])
        api = types.ModuleType("esptool"); api.__version__ = "5.6.0"
        cmds = types.ModuleType("esptool.cmds")
        cmds.connect_esp = mock.Mock(return_value=esp)
        cmds.run_stub = mock.Mock(return_value=esp)
        cmds.attach_flash = mock.Mock()
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(sys.modules,
                {"esptool": api, "esptool.cmds": cmds}), mock.patch("builtins.print"):
            output = Path(temp) / "new-backup"
            result = backup.backup_device("SYNTHETIC", output, "no-reset")
            self.assertTrue(result["two_reads_identical"])
            self.assertFalse(result["application_started"])
            self.assertEqual((output / "previous-app0-slot.bin").read_bytes(), image[0x10000:0x340000])
            self.assertEqual(json.loads((output / "backup-verification.json").read_text())["rollback_offset"], "0x10000")
            self.assertFalse((output / "FAILED.json").exists())
        port.close.assert_called_once_with()
        cmds.run_stub.assert_called_once_with(esp)
        cmds.connect_esp.assert_called_once_with("SYNTHETIC", chip="esp32c5", before="no-reset", connect_attempts=3)

    def test_security_failure_closes_port_and_writes_only_failure_diagnostic(self):
        port = types.SimpleNamespace(close=mock.Mock())
        esp = types.SimpleNamespace(CHIP_NAME="ESP32-C5", _port=port,
                                    get_security_info=lambda: {"flags": 4})
        api = types.ModuleType("esptool"); api.__version__ = "5.6.0"
        cmds = types.ModuleType("esptool.cmds")
        cmds.connect_esp = mock.Mock(return_value=esp)
        cmds.run_stub = mock.Mock()
        cmds.attach_flash = mock.Mock()
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(sys.modules,
                {"esptool": api, "esptool.cmds": cmds}), mock.patch("builtins.print"):
            output = Path(temp) / "new-backup"
            with self.assertRaisesRegex(backup.BackupError, "security"):
                backup.backup_device("SYNTHETIC", output, "usb-reset")
            self.assertEqual([p.name for p in output.iterdir()], ["FAILED.json"])
        port.close.assert_called_once_with()
        cmds.run_stub.assert_not_called()
        cmds.attach_flash.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
