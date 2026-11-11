"""Failed XP writes remain retryable; update cleanup never reports false success.

Real temporary loot files and native app methods only: no player data, GUI,
network, device access, update installation or process restart.
"""
import ast
from contextlib import contextmanager
import copy
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.loot_manager import LootDataError, LootManager


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs/app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = next(node for node in TREE.body
            if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
METHODS = {"_save_xp_if_dirty", "_cleanup", "_gameupd_do", "_gameupd_discard_plan"}
NODE = ast.ClassDef(name="SaveGame", bases=[], keywords=[], decorator_list=[],
    body=[copy.deepcopy(node) for node in GAME.body
          if isinstance(node, ast.FunctionDef) and node.name in METHODS])
ENV = dict(logging=logging, time=time, __name__="xp_durability_test",
           pyxel=SimpleNamespace(frame_count=1))
exec(compile(ast.fix_missing_locations(ast.Module(body=[NODE], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
SaveGame = ENV["SaveGame"]


def seed(root, *, saved=True):
    base = Path(root) / "loot"
    base.mkdir()
    value = dict(version=2, sessions={}, totals={}, badges=["wardriver"],
        mc_contacts={"1234abcd": {"id": "1234abcd", "note": "Keep this note"}},
        mt_contacts={"!1234abcd": {"id": "!1234abcd", "name": "Keep this contact"}},
        extension_metadata={"keep": [1, 2, 3]})
    if saved:
        value["xp"] = 9000
    raw = json.dumps(value, indent=3).encode("utf-8")
    (base / "loot_db.json").write_bytes(raw)
    return value, raw


@contextmanager
def loot_at(root):
    with patch.object(LootManager, "_periodic_backup_loop", lambda self: None):
        loot = LootManager(str(root))
    try:
        yield loot
    finally:
        with patch.object(os, "sync", lambda: None, create=True):
            loot.close()


def game(loot, xp=9025, *, dirty=True):
    value = SaveGame()
    value.loot, value.xp, value._xp_dirty = loot, xp, dirty
    value._cleanup_done = value._cleanup_running = value._shutting_down = False
    value._cleanup_errors = []
    value._gameupd_closed_for_install = True
    value._gameupd_plan = "isolated-update-plan"
    value._gameupd_ok = value._gameupd_done = False
    value._gameupd_running = True
    value._gameupd_restart_at = None
    value._gameupd_log = []
    value._gameupd_log_add = value._gameupd_log.append
    value._game_updates = SimpleNamespace(install=Mock(), discard=Mock())
    return value


def bootstrap(loot):
    initialize = next(node for node in GAME.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_initialize")
    start = next(i for i, statement in enumerate(initialize.body)
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Attribute)
            and target.attr == "xp" for target in statement.targets))
    end = next(i for i in range(start + 1, len(initialize.body))
        if isinstance(initialize.body[i], ast.AnnAssign)
        and isinstance(initialize.body[i].target, ast.Attribute)
        and initialize.body[i].target.attr == "ble_devices")
    value = game(loot)
    exec(compile(ast.Module(body=copy.deepcopy(initialize.body[start:end]),
                           type_ignores=[]), str(SOURCE), "exec"), dict(self=value))
    return value


class XpDurabilityTests(unittest.TestCase):
    def test_atomic_replace_failure_keeps_original_ram_disk_and_dirty_then_same_value_retries(self):
        with tempfile.TemporaryDirectory() as root:
            original, raw = seed(root)
            with loot_at(root) as loot:
                value = game(loot)
                with patch("os.replace", side_effect=OSError("synthetic storage failure")), \
                        self.assertLogs(level="WARNING"):
                    self.assertIs(value._save_xp_if_dirty(), False)
                self.assertTrue(value._xp_dirty)
                self.assertEqual(value.xp, 9025)
                self.assertEqual(loot.load_xp(), 9000)
                self.assertEqual(loot._db_path.read_bytes(), raw)
                self.assertIs(value._save_xp_if_dirty(), True)
                self.assertFalse(value._xp_dirty)
                expected = dict(original, xp=9025)
                self.assertEqual(json.loads(loot._db_path.read_bytes()), expected)
                self.assertEqual(loot._db, expected)
            with loot_at(root) as restarted:
                self.assertEqual(restarted.load_xp(), 9025)
                self.assertEqual(restarted.load_badges(), {"wardriver"})
                self.assertEqual(restarted.load_contacts(), original["mc_contacts"])
                self.assertEqual(restarted.load_meshtastic_contacts(), original["mt_contacts"])

    def test_file_fsync_failure_is_not_reported_saved(self):
        with tempfile.TemporaryDirectory() as root:
            _, raw = seed(root)
            with loot_at(root) as loot:
                value = game(loot)
                with patch("os.fsync", side_effect=OSError("synthetic fsync failure")), \
                        self.assertLogs(level="WARNING"):
                    self.assertFalse(value._save_xp_if_dirty())
                self.assertTrue(value._xp_dirty)
                self.assertEqual(loot._db_path.read_bytes(), raw)
                self.assertEqual(loot.load_xp(), 9000)
                self.assertTrue(value._save_xp_if_dirty())

    def test_memory_failure_cannot_escape_periodic_save_or_clear_pending_progress(self):
        with tempfile.TemporaryDirectory() as root:
            _, raw = seed(root)
            with loot_at(root) as loot:
                value = game(loot)
                with patch("json.dump", side_effect=MemoryError("synthetic memory failure")), \
                        self.assertLogs(level="WARNING"):
                    self.assertFalse(value._save_xp_if_dirty())
                self.assertTrue(value._xp_dirty)
                self.assertEqual(value.xp, 9025)
                self.assertEqual(loot.load_xp(), 9000)
                self.assertEqual(loot._db_path.read_bytes(), raw)
                self.assertTrue(value._save_xp_if_dirty())
                self.assertFalse(value._xp_dirty)

    def test_same_persisted_value_succeeds_without_another_write(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root)
            with loot_at(root) as loot, patch.object(loot, "_save_db") as save:
                self.assertIs(loot.save_xp(9000), True)
                save.assert_not_called()

    def test_explicit_zero_is_committed_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root, saved=False)
            with loot_at(root) as loot:
                self.assertTrue(loot.save_xp(0))
                self.assertTrue(loot.has_saved_xp())
            with loot_at(root) as restarted:
                self.assertEqual(restarted.load_xp(), 0)
                self.assertTrue(restarted.has_saved_xp())

    def test_failed_bootstrap_keeps_award_dirty_for_retry(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root, saved=False)
            with loot_at(root) as loot:
                raw = loot._db_path.read_bytes()
                with patch.object(loot, "calculate_xp_from_loot", return_value=75), \
                        patch.object(loot, "_save_db", return_value=False):
                    value = bootstrap(loot)
                self.assertEqual(value.xp, 75)
                self.assertTrue(value._xp_dirty)
                self.assertFalse(loot.has_saved_xp())
                self.assertEqual(loot._db_path.read_bytes(), raw)
                self.assertTrue(value._save_xp_if_dirty())
                self.assertEqual(loot.load_xp(), 75)

    def test_unavailable_store_cannot_clear_dirty_progress(self):
        value = game(None)
        self.assertIs(value._save_xp_if_dirty(), False)
        self.assertTrue(value._xp_dirty)

    def test_clean_progress_does_not_touch_storage(self):
        loot = SimpleNamespace(save_xp=Mock())
        value = game(loot, dirty=False)
        self.assertIs(value._save_xp_if_dirty(), True)
        loot.save_xp.assert_not_called()

    def test_backend_exception_preserves_dirty_progress(self):
        value = game(SimpleNamespace(save_xp=Mock(side_effect=OSError("synthetic failure"))))
        with self.assertLogs(level="WARNING"):
            self.assertFalse(value._save_xp_if_dirty())
        self.assertTrue(value._xp_dirty)

    def test_cleanup_save_failure_blocks_code_installation(self):
        with tempfile.TemporaryDirectory() as root:
            _, raw = seed(root)
            with loot_at(root) as loot:
                value = game(loot)
                with patch.object(loot, "_save_db", return_value=False), \
                        patch.object(os, "sync", lambda: None, create=True), \
                        self.assertLogs(level="WARNING"):
                    value._cleanup()
                    value._gameupd_do()
                self.assertTrue(value._xp_dirty)
                self.assertIn("XP save", value._cleanup_errors)
                self.assertIn("loot flush", value._cleanup_errors)
                value._game_updates.install.assert_not_called()
                self.assertFalse(value._gameupd_ok)
                self.assertIsNone(value._gameupd_restart_at)
                self.assertEqual(loot._db_path.read_bytes(), raw)

    def test_final_database_failure_blocks_install_even_when_xp_was_already_saved(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root)
            with loot_at(root) as loot:
                value = game(loot, xp=9000, dirty=False)
                with patch.object(loot, "_save_db", return_value=False), \
                        patch.object(os, "sync", lambda: None, create=True), \
                        self.assertLogs(level="WARNING"):
                    value._cleanup()
                    value._gameupd_do()
                self.assertIn("loot flush", value._cleanup_errors)
                self.assertNotIn("XP save", value._cleanup_errors)
                value._game_updates.install.assert_not_called()

    def test_successful_final_save_preserves_progress_and_allows_mocked_code_installation(self):
        with tempfile.TemporaryDirectory() as root:
            original, _ = seed(root)
            with loot_at(root) as loot:
                value = game(loot)
                value._game_updates.install.return_value = SimpleNamespace(version="isolated-test")
                with patch.object(os, "sync", lambda: None, create=True):
                    value._cleanup()
                    value._gameupd_do()
                self.assertEqual(value._cleanup_errors, [])
                self.assertFalse(value._xp_dirty)
                self.assertTrue(value._gameupd_ok)
                value._game_updates.install.assert_called_once_with("isolated-update-plan", quiescent=True)
                stored = json.loads(loot._db_path.read_bytes())
                self.assertEqual(stored["xp"], 9025)
                for name in ("mc_contacts", "mt_contacts", "badges", "extension_metadata"):
                    self.assertEqual(stored[name], original[name])

    def test_close_reports_failure_and_releases_open_log(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root)
            with loot_at(root) as loot:
                handle = loot._serial_fh
                with patch.object(loot, "_save_db", return_value=False), \
                        patch.object(os, "sync", lambda: None, create=True):
                    self.assertIs(loot.close(), False)
                self.assertTrue(handle.closed)
                self.assertIsNone(loot._serial_fh)

    def test_serial_fsync_error_is_not_hidden_by_final_close(self):
        with tempfile.TemporaryDirectory() as root:
            seed(root)
            with loot_at(root) as loot:
                handle = loot._serial_fh
                with patch("os.fsync", side_effect=OSError("synthetic fsync failure")), \
                        patch.object(os, "sync", lambda: None, create=True), \
                        self.assertLogs(level="ERROR"):
                    self.assertIs(loot.close(), False)
                self.assertTrue(handle.closed)

    def test_refused_database_constructor_closes_log_without_masking_original_error(self):
        with tempfile.TemporaryDirectory() as root:
            _, raw = seed(root)
            opened = []
            original_open = open

            def capture(*args, **kwargs):
                handle = original_open(*args, **kwargs)
                if Path(args[0]).name == "serial_full.log":
                    opened.append(handle)
                return handle

            error = LootDataError("synthetic invalid player save")
            with patch("builtins.open", side_effect=capture), \
                    patch.object(LootManager, "_load_or_build_db", side_effect=error):
                with self.assertRaises(LootDataError) as result:
                    LootManager(root)
            self.assertIs(result.exception, error)
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)
            self.assertEqual((Path(root) / "loot/loot_db.json").read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
