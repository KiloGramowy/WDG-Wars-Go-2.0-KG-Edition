"""Legacy LOCOSP save compatibility, using temporary player data only.

These checks exercise real JSON migration and filesystem writes. They do not
open the player's installed loot, radio, network or graphical game.
"""
from contextlib import contextmanager
import ast
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs import loot_manager


SESSION = "2026-10-01_12-34-56"


def legacy_data():
    return {
        "version": 1,
        "sessions": {SESSION: {"wardriving": 17, "bt_devices": 8, "old_counter": 42}},
        "totals": {"wardriving": 17, "bt_devices": 8, "extension_total": 91},
        # This is deliberately unrelated to the cache's bootstrap calculation:
        # duplicate scans, PWN and other awards cannot be reconstructed exactly.
        "xp": 12345,
        "badges": ["wardriver", "meshcore", "handshake_hunter"],
        "mc_contacts": {
            "abcd1234": {"id": "abcd1234", "name": "Legacy MC", "type": "Client",
                         "first_seen": 12, "note": "keep the player's note"}},
        "mt_contacts": {
            "!12345678": {"id": "!12345678", "name": "Legacy MT", "type": "Client",
                          "first_seen": 15, "note": "keep this too"}},
        "mesh_discovery_events": {
            "meshcore": {"abcd1234": {"time": 12, "historical": True}},
            "meshtastic": {
                "!12345678": {"time": 15, "historical": True},
                "!11112222": {"time": 18, "historical": False, "xp_awarded": 20}}},
        "extension_player_metadata": {"nested": [1, {"keep": True}]},
    }


def write_db(root, value):
    base = Path(root) / "loot"
    base.mkdir(parents=True, exist_ok=True)
    # Non-default whitespace ensures the backup preserves bytes, not just JSON.
    raw = (json.dumps(value, indent=3, ensure_ascii=False) + "\n\n").encode("utf-8")
    (base / "loot_db.json").write_bytes(raw)
    return base, raw


def reader(root):
    manager = loot_manager.LootManager.__new__(loot_manager.LootManager)
    manager._base = Path(root) / "loot"
    manager._db_path = manager._base / "loot_db.json"
    manager._db_lock = threading.RLock()
    return manager


def backup_path(base, raw):
    return base / ("loot_db.v1." + hashlib.sha256(raw).hexdigest() + ".bak")


@contextmanager
def live_loot(root):
    with patch.object(loot_manager.LootManager, "_periodic_backup_loop", lambda self: None):
        manager = loot_manager.LootManager(str(root))
    try:
        yield manager
    finally:
        with patch.object(os, "sync", lambda: None, create=True):
            manager.close()


def level_at(xp):
    # Read the native game's table without importing Pyxel or initializing UI.
    source = Path(loot_manager.__file__).with_name("app.py").read_text(encoding="utf-8")
    for statement in ast.parse(source).body:
        if (isinstance(statement, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "LEVEL_TABLE" for t in statement.targets)):
            table = ast.literal_eval(statement.value)
            return max(i for i, (threshold, _) in enumerate(table) if xp >= threshold)
    raise AssertionError("The native level table was not found")


def app_initializer():
    source = Path(loot_manager.__file__).with_name("app.py").read_text(encoding="utf-8")
    game = next(node for node in ast.parse(source).body
                if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
    return next(node for node in game.body
                if isinstance(node, ast.FunctionDef) and node.name == "_initialize")


def bootstrap_native_xp(manager):
    # Execute the real constructor's XP block without initializing any hardware,
    # window or other constructor side effects.
    statements = app_initializer().body
    start = next(i for i, statement in enumerate(statements)
                 if isinstance(statement, ast.Assign)
                 and any(isinstance(t, ast.Attribute) and t.attr == "xp" for t in statement.targets))
    end = next(i for i in range(start + 1, len(statements))
               if isinstance(statements[i], ast.AnnAssign)
               and isinstance(statements[i].target, ast.Attribute)
               and statements[i].target.attr == "ble_devices")
    game = SimpleNamespace(loot=manager)
    exec(compile(ast.Module(body=statements[start:end], type_ignores=[]),
                 "native-xp-constructor", "exec"), {"self": game})
    return game


class LegacyLootMigrationTests(unittest.TestCase):
    def test_schema_one_retains_exact_xp_level_badges_contacts_and_unknown_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            old = legacy_data()
            base, _ = write_db(folder, old)
            loaded = reader(folder)._load_or_build_db()
            stored = json.loads((base / "loot_db.json").read_bytes())
            self.assertEqual(loaded, stored)
            self.assertEqual(stored["version"], 2)
            for key in ("xp", "badges", "mc_contacts", "mt_contacts", "mesh_discovery_events",
                        "extension_player_metadata"):
                self.assertEqual(stored[key], old[key], key)
            self.assertEqual(level_at(stored["xp"]), level_at(old["xp"]))
            self.assertEqual(stored["xp"], 12345)

    def test_exact_source_backup_is_created_before_atomic_database_save(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            manager = reader(folder)
            original_save = manager._save_db
            observed = []

            def checked_save(db):
                observed.append(True)
                self.assertEqual(backup_path(base, raw).read_bytes(), raw)
                self.assertEqual((base / "loot_db.json").read_bytes(), raw)
                return original_save(db)

            with patch.object(manager, "_save_db", checked_save):
                manager._load_or_build_db()
            self.assertEqual(observed, [True])
            self.assertEqual(backup_path(base, raw).read_bytes(), raw)
            if os.name == "posix":
                self.assertEqual(backup_path(base, raw).stat().st_mode & 0o777, 0o600)

    def test_restart_never_readds_xp_or_remigrates_the_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            initial = reader(folder)._load_or_build_db()
            first_bytes = (base / "loot_db.json").read_bytes()
            with patch.object(loot_manager.LootManager, "_migrate_legacy_db",
                              side_effect=AssertionError("A v2 save must not migrate again")):
                restarted = reader(folder)._load_or_build_db()
            self.assertEqual(restarted, initial)
            self.assertEqual(restarted["xp"], 12345)
            self.assertEqual((base / "loot_db.json").read_bytes(), first_bytes)
            self.assertEqual(list(base.glob("loot_db.v1.*.bak")), [backup_path(base, raw)])

    def test_removed_raw_logs_do_not_erase_historical_sessions_or_counters(self):
        with tempfile.TemporaryDirectory() as folder:
            old = legacy_data()
            base, _ = write_db(folder, old)
            self.assertFalse((base / SESSION).exists())
            loaded = reader(folder)._load_or_build_db()
            self.assertEqual(loaded["sessions"][SESSION], old["sessions"][SESSION])
            self.assertEqual(loaded["totals"]["wardriving"], 17)
            self.assertEqual(loaded["totals"]["bt_devices"], 8)
            self.assertEqual(loaded["totals"]["extension_total"], 91)

    def test_new_counter_is_scanned_without_replacing_existing_session_history(self):
        with tempfile.TemporaryDirectory() as folder:
            base, _ = write_db(folder, legacy_data())
            session = base / SESSION
            session.mkdir()
            (session / "emergency_rf.csv").write_text("time,rssi\n1,-90\n2,-85\n", encoding="utf-8")
            loaded = reader(folder)._load_or_build_db()
            self.assertEqual(loaded["sessions"][SESSION]["emergency"], 2)
            self.assertEqual(loaded["totals"]["emergency"], 2)
            # There is no wardriving CSV anymore; its saved count must survive.
            self.assertEqual(loaded["sessions"][SESSION]["wardriving"], 17)
            self.assertEqual(loaded["sessions"][SESSION]["old_counter"], 42)
            self.assertEqual(loaded["xp"], 12345)

    def test_existing_schema_two_save_is_loaded_without_any_rewrite(self):
        with tempfile.TemporaryDirectory() as folder:
            current = legacy_data()
            current["version"] = 2
            base, raw = write_db(folder, current)
            with patch.object(loot_manager.LootManager, "_save_db",
                              side_effect=AssertionError("No rewrite of current schema")):
                loaded = reader(folder)._load_or_build_db()
            self.assertEqual(loaded, current)
            self.assertEqual((base / "loot_db.json").read_bytes(), raw)
            self.assertEqual(list(base.glob("loot_db.v1.*.bak")), [])

    def test_known_contacts_and_deleted_contact_ledger_do_not_earn_another_award(self):
        with tempfile.TemporaryDirectory() as folder:
            write_db(folder, legacy_data())
            with live_loot(folder) as manager:
                for protocol, identity in (("meshcore", "abcd1234"),
                                           ("meshtastic", "!12345678"),
                                           ("meshtastic", "!11112222")):
                    result = manager.register_mesh_contact(
                        protocol, identity, {"name": "Seen again", "type": "Client", "lat": 0, "lon": 0})
                    self.assertEqual(result["xp_award"], 0)
                    self.assertFalse(result["new_event"])
                    self.assertEqual(result["xp_total"], 12345)
                self.assertEqual(manager.load_badges(), set(legacy_data()["badges"]))
            with live_loot(folder) as restarted:
                self.assertEqual(restarted.load_xp(), 12345)

    def test_blocked_backup_aborts_before_replacing_the_original_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            backup_path(base, raw).mkdir()
            manager = reader(folder)
            with patch.object(manager, "_save_db") as save:
                with self.assertRaises(loot_manager.LootDataError):
                    manager._load_or_build_db()
            save.assert_not_called()
            self.assertEqual((base / "loot_db.json").read_bytes(), raw)

    def test_database_save_failure_retains_original_bytes_and_verified_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            manager = reader(folder)
            with patch.object(manager, "_save_db", return_value=False):
                with self.assertRaises(loot_manager.LootDataError):
                    manager._load_or_build_db()
            self.assertEqual((base / "loot_db.json").read_bytes(), raw)
            self.assertEqual(backup_path(base, raw).read_bytes(), raw)

    def test_mismatching_existing_backup_aborts_without_replacing_either_file(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            unexpected_backup = b"Existing backup with different bytes; preserve it"
            backup_path(base, raw).write_bytes(unexpected_backup)
            manager = reader(folder)
            with patch.object(manager, "_save_db") as save:
                with self.assertRaises(loot_manager.LootDataError):
                    manager._load_or_build_db()
            save.assert_not_called()
            self.assertEqual((base / "loot_db.json").read_bytes(), raw)
            self.assertEqual(backup_path(base, raw).read_bytes(), unexpected_backup)

    def test_backup_symlink_to_live_database_is_not_accepted_as_a_durable_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            manager = reader(folder)
            backup = backup_path(base, raw)
            try:
                backup.symlink_to(manager._db_path)
            except NotImplementedError:
                self.skipTest("This platform does not support symbolic links")
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows requires symbolic-link privileges for this filesystem case")
                raise
            self.assertTrue(backup.is_symlink())
            with patch.object(manager, "_save_db") as save:
                with self.assertRaises(loot_manager.LootDataError):
                    manager._load_or_build_db()
            save.assert_not_called()
            self.assertEqual(manager._db_path.read_bytes(), raw)
            self.assertTrue(backup.is_symlink())
            self.assertEqual(backup.read_bytes(), raw)

    def test_unknown_future_schema_is_not_downgraded_or_rebuilt(self):
        with tempfile.TemporaryDirectory() as folder:
            future = legacy_data()
            future["version"] = 3
            base, raw = write_db(folder, future)
            manager = reader(folder)
            with patch.object(manager, "_save_db") as save:
                with self.assertRaises(loot_manager.LootDataError):
                    manager._load_or_build_db()
            save.assert_not_called()
            self.assertEqual((base / "loot_db.json").read_bytes(), raw)
            self.assertEqual(list(base.glob("loot_db.v1.*.bak")), [])

    def test_malformed_json_or_invalid_encoding_is_never_rebuilt_as_zero_progress(self):
        for raw in (b'{"version":1,"xp":12345,"sessions":',
                    b'{"version":1,"sessions":{},"note":"\xff"}'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                base = Path(folder) / "loot"
                base.mkdir()
                (base / "loot_db.json").write_bytes(raw)
                manager = reader(folder)
                with patch.object(manager, "_rebuild_db") as rebuild, \
                        patch.object(manager, "_save_db") as save:
                    with self.assertRaises(loot_manager.LootDataError):
                        manager._load_or_build_db()
                rebuild.assert_not_called()
                save.assert_not_called()
                self.assertEqual((base / "loot_db.json").read_bytes(), raw)
                self.assertEqual(list(base.glob("loot_db.v1.*.bak")), [])

    def test_read_permission_error_is_not_treated_as_a_missing_save(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            manager = reader(folder)
            with patch.object(Path, "read_bytes", side_effect=PermissionError("Fixture read denied")), \
                    patch.object(manager, "_rebuild_db") as rebuild, \
                    patch.object(manager, "_save_db") as save:
                with self.assertRaises(loot_manager.LootDataError):
                    manager._load_or_build_db()
            rebuild.assert_not_called()
            save.assert_not_called()
            self.assertEqual((base / "loot_db.json").read_bytes(), raw)

    def test_genuinely_missing_database_still_creates_a_fresh_schema_two_save(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "loot"
            base.mkdir()
            manager = reader(folder)
            self.assertFalse(manager._db_path.exists())
            loaded = manager._load_or_build_db()
            self.assertEqual(loaded["version"], 2)
            self.assertEqual(loaded["sessions"], {})
            self.assertNotIn("xp", loaded)
            self.assertEqual(json.loads(manager._db_path.read_bytes()), loaded)
            self.assertEqual(list(base.glob("loot_db.v1.*.bak")), [])

    def test_schema_version_must_be_an_integer_not_a_lookalike_value(self):
        for version in (True, 1.0, 2.0, "1", None):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                invalid = legacy_data()
                invalid["version"] = version
                base, raw = write_db(folder, invalid)
                manager = reader(folder)
                with patch.object(manager, "_rebuild_db") as rebuild, \
                        patch.object(manager, "_save_db") as save:
                    with self.assertRaises(loot_manager.LootDataError):
                        manager._load_or_build_db()
                rebuild.assert_not_called()
                save.assert_not_called()
                self.assertEqual((base / "loot_db.json").read_bytes(), raw)

    def test_valid_zero_xp_is_retained_and_missing_xp_remains_missing(self):
        for saved in (0, None):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as folder:
                old = legacy_data()
                if saved is None:
                    old.pop("xp")
                else:
                    old["xp"] = saved
                write_db(folder, old)
                loaded = reader(folder)._load_or_build_db()
                if saved is None:
                    self.assertNotIn("xp", loaded)
                else:
                    self.assertEqual(loaded["xp"], 0)

    def test_native_game_bootstrap_uses_exact_saved_xp_including_zero(self):
        for saved in (0, 12345):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as folder:
                old = legacy_data()
                old["xp"] = saved
                base, _ = write_db(folder, old)
                manager = reader(folder)
                manager._db = manager._load_or_build_db()
                raw = (base / "loot_db.json").read_bytes()
                with patch.object(manager, "calculate_xp_from_loot",
                                  side_effect=AssertionError("Saved XP must never be recalculated")):
                    game = bootstrap_native_xp(manager)
                self.assertEqual(game.xp, saved)
                self.assertFalse(game._xp_dirty)
                self.assertEqual((base / "loot_db.json").read_bytes(), raw)

    def test_native_game_keeps_historical_bootstrap_only_when_xp_was_never_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            old = legacy_data()
            old.pop("xp")
            base, _ = write_db(folder, old)
            manager = reader(folder)
            manager._db = manager._load_or_build_db()
            game = bootstrap_native_xp(manager)
            self.assertEqual(game.xp, 17 * 15 + 8 * 10)
            self.assertEqual(json.loads((base / "loot_db.json").read_bytes())["xp"], game.xp)

    def test_native_game_does_not_swallow_a_player_data_error_and_start_at_zero(self):
        initializer = app_initializer()
        block = next(statement for statement in initializer.body
                     if isinstance(statement, ast.Try)
                     and any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id == "LootManager" for node in ast.walk(statement)))
        game = SimpleNamespace(gps=None)
        error = loot_manager.LootDataError("Migration stopped for the fixture")
        namespace = {"self": game, "_app_dir": "unused-fixture-path",
                     "LootManager": Mock(side_effect=error),
                     "LootDataError": loot_manager.LootDataError,
                     "logging": logging, "__name__": __name__}
        with self.assertLogs(__name__, level="ERROR"):
            with self.assertRaises(loot_manager.LootDataError) as caught:
                exec(compile(ast.Module(body=[block], type_ignores=[]),
                             "native-loot-constructor", "exec"), namespace)
        self.assertIs(caught.exception, error)
        self.assertFalse(hasattr(game, "loot"))

    def test_explicit_old_data_root_works_with_a_separate_new_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            old_root = Path(folder) / "old-install"
            new_root = Path(folder) / "new-checkout"
            new_root.mkdir()
            base, _ = write_db(old_root, legacy_data())
            previous_cwd = Path.cwd()
            try:
                os.chdir(new_root)
                with live_loot(old_root.resolve()) as manager:
                    self.assertEqual(manager.loot_root, base)
                    self.assertEqual(manager.load_xp(), 12345)
                    self.assertEqual(manager.load_badges(), set(legacy_data()["badges"]))
            finally:
                os.chdir(previous_cwd)
            self.assertFalse((new_root / "loot").exists())

    def test_a_concurrent_source_change_aborts_instead_of_overwriting_newer_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            base, raw = write_db(folder, legacy_data())
            (base / SESSION).mkdir()
            newer = copy.deepcopy(legacy_data())
            newer["xp"] = 13000
            newer_bytes = json.dumps(newer).encode("utf-8")
            manager = reader(folder)

            def changed_source(_):
                (base / "loot_db.json").write_bytes(newer_bytes)
                return {"emergency": 0}

            with patch.object(manager, "_scan_session_dir", changed_source), \
                    patch.object(manager, "_save_db") as save:
                with self.assertRaises(loot_manager.LootDataError):
                    manager._load_or_build_db()
            save.assert_not_called()
            self.assertEqual((base / "loot_db.json").read_bytes(), newer_bytes)
            self.assertEqual(backup_path(base, raw).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
