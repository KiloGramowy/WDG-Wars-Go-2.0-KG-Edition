"""Deterministic startup/Sync/update lifecycle regressions; no user data or I/O.

The constructor, terminal, cleanup and updater methods execute from production
source. The startup projection retains the actual relevant initialization and
plugin-hook order while omitting unrelated hardware/rendering initialization.
"""
import ast
import atexit
import copy
import logging
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.plugin_base import PluginBase
from plugins.wardrive_upload import BADGE_GAME_TO_SERVER, WardriveUpload
from test_game_shutdown import GameHarness as CleanupHarness, game as shutdown_game
from test_game_update_ui import Game as UpdateHarness, PX, ENV as UPDATE_ENV


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = next(node for node in TREE.body
            if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
METHODS = {node.name: node for node in GAME.body if isinstance(node, ast.FunctionDef)}
ENV = dict(atexit=atexit, threading=threading, signal=signal, time=time,
           logging=logging, __name__="startup_shutdown_ui_test", C_TEXT=7,
           _color_for_terminal_line=lambda _line: 7)


class NoHardwarePower:
    def begin_shutdown(self):
        return True

    def shutdown(self, **_kwargs):
        return {}


ENV["AioPowerSession"] = NoHardwarePower
constructor_class = ast.ClassDef(name="ConstructorGame", bases=[], keywords=[],
    decorator_list=[], body=[copy.deepcopy(METHODS[name]) for name in
        ("__init__", "_term_add", "msg", "_earn_badge", "_cleanup", "_restore_exit_handlers")])
exec(compile(ast.fix_missing_locations(ast.Module(body=[constructor_class], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
ConstructorGame = ENV["ConstructorGame"]


def assigned_attributes(statement):
    targets = (statement.targets if isinstance(statement, ast.Assign) else
               [statement.target] if isinstance(statement, ast.AnnAssign) else [])
    return {target.attr for target in targets if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name) and target.value.id == "self"}


def projected_initializer():
    """Preserve the real ordering around fields consumed by on_load callbacks."""
    needed = {"loot", "terminal_lines", "_terminal_colors", "term_scroll", "_term_lock",
              "_plugins", "_plugin_overlay", "_badges", "_mc_node_name", "_mc_channels_list",
              "xp", "_xp_dirty", "msgs", "_boot_phase"}
    result = copy.deepcopy(METHODS["_initialize"])
    result.body = []
    for statement in METHODS["_initialize"].body:
        if assigned_attributes(statement) & needed:
            # Only the initial None loot declaration is needed; constructing a
            # real data store would defeat this hardware-free callback test.
            if "loot" in assigned_attributes(statement) and not (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.value, ast.Constant)
                    and statement.value.value is None):
                continue
            result.body.append(copy.deepcopy(statement))
        elif isinstance(statement, ast.For) and isinstance(statement.iter, ast.Attribute) \
                and statement.iter.attr == "_plugins":
            result.body.append(copy.deepcopy(statement))
    return result


projection_class = ast.ClassDef(name="ProjectedStartupGame",
    bases=[ast.Name(id="ConstructorGame", ctx=ast.Load())], keywords=[], decorator_list=[],
    body=[projected_initializer()])
ENV.update(node_name="WatchDogs_synthetic", _mc_cfg={"_channels": []})
exec(compile(ast.fix_missing_locations(ast.Module(body=[projection_class], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
ProjectedStartupGame = ENV["ProjectedStartupGame"]


def new_plugin():
    with patch.object(WardriveUpload, "_load_state"):
        return WardriveUpload()


class ImmediateProfilePlugin(WardriveUpload):
    """Force a completed response at the first on_load instruction, without HTTP."""
    def on_load(self, app):
        PluginBase.on_load(self, app)
        self.fields_ready = all(hasattr(app, name) for name in (
            "_term_lock", "terminal_lines", "_terminal_colors", "_badges",
            "_mc_node_name", "_mc_channels_list", "msgs", "xp", "_boot_phase"))
        self._on_user_info(dict(username="synthetic", badges=[BADGE_GAME_TO_SERVER["wardriver"]],
                                wifi=1, ble=2, total=3))


class StartupTerminalTests(unittest.TestCase):
    def setUp(self):
        self.register = patch.object(atexit, "register").start()
        self.addCleanup(patch.stopall)
        patch.object(atexit, "unregister").start()

    def test_terminal_available_before_first_device_or_plugin_initialization(self):
        def initialize(app, *_args):
            app._term_add("[ERR] synthetic early serial diagnostic", raw=True)
        with patch.object(ConstructorGame, "_initialize", initialize, create=True):
            app = ConstructorGame()
        self.assertEqual(app.terminal_lines, ["[ERR] synthetic early serial diagnostic"])
        self.assertEqual(app._terminal_colors, [7])
        self.assertEqual(app.term_scroll, 0)
        self.assertTrue(app._cleanup_done)

    def test_early_startup_failure_keeps_original_error_and_can_log_reason(self):
        seen = []
        def initialize(app, *_args):
            seen.append(app)
            app._term_add("[ERR] synthetic startup failed", raw=True)
            raise RuntimeError("synthetic original startup error")
        with patch.object(ConstructorGame, "_initialize", initialize, create=True):
            with self.assertRaisesRegex(RuntimeError, "synthetic original startup error"):
                ConstructorGame()
        self.assertEqual(seen[0].terminal_lines, ["[ERR] synthetic startup failed"])
        self.assertTrue(seen[0]._cleanup_done)
        self.assertEqual(seen[0]._cleanup_errors, [])
        self.assertEqual(seen[0]._plugins, [])

    def test_immediate_plugin_response_sees_ready_state_and_is_not_overwritten(self):
        with patch.object(WardriveUpload, "_load_state"):
            plugin = ImmediateProfilePlugin()
        with patch.dict(ENV, discover_plugins=lambda: [plugin]), \
                patch("watchdogs.lora_manager.save_meshcore_config") as save_name:
            app = ProjectedStartupGame()
        self.assertTrue(plugin.fields_ready)
        self.assertIn("wardriver", app._badges)
        self.assertEqual(app._mc_node_name, "WDG_synthetic")
        self.assertEqual(app.xp, 0)
        self.assertTrue(any("Welcome, synthetic!" in line for line in app.terminal_lines))
        self.assertTrue(any("[BADGE]" in message[0] for message in app.msgs))
        save_name.assert_called_once_with("WDG_synthetic", [])
        self.assertTrue(app._cleanup_done)

    def test_partial_plugin_load_is_unloaded_without_masking_startup_error(self):
        plugin = new_plugin()
        seen = []
        def fail(app):
            PluginBase.on_load(plugin, app)
            seen.append(app)
            app._term_add("[Sync] synthetic load diagnostic", raw=True)
            raise RuntimeError("synthetic plugin startup error")
        plugin.on_load = fail
        with patch.dict(ENV, discover_plugins=lambda: [plugin]):
            with self.assertRaisesRegex(RuntimeError, "synthetic plugin startup error"):
                ProjectedStartupGame()
        self.assertTrue(plugin._sync_stop.is_set())
        self.assertEqual(seen[0].terminal_lines, ["[Sync] synthetic load diagnostic"])
        self.assertTrue(seen[0]._cleanup_done)


class UpdateShutdownGame(CleanupHarness, UpdateHarness):
    pass


def update_game_with_real_cleanup():
    app = shutdown_game()
    app.__class__ = UpdateShutdownGame
    app._init_game_updates()
    app._gameupd_open_frame = -1
    app._gameupd_confirm = True
    app._gameupd_plan = "synthetic-verified-plan"
    app._game_update_available = True
    app._term_add = Mock()
    app._mc_node_name = "custom synthetic radio"
    app._badges = set()
    PX.pressed.clear()
    PX.frame_count += 1
    UPDATE_ENV["restart_game"].reset_mock()
    return app


class DelayedSyncShutdownTests(unittest.TestCase):
    def start_identity(self, app):
        plugin = new_plugin()
        plugin.app = app
        app._plugins = [plugin]
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        failures = []
        def response():
            started.set()
            if not release.wait(3):
                raise AssertionError("Synthetic HTTP response was not released")
            return dict(username="late-synthetic", badges=[], total=1)
        plugin.fetch_user_info = response
        def worker():
            try:
                plugin._validate_and_apply_user()
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()
        self.assertTrue(plugin._start_background_worker(worker, name="synthetic-delayed-identity"))
        self.assertTrue(started.wait(1))
        def retire():
            release.set()
            finished.wait(3)
            plugin.on_unload()
        self.addCleanup(retire)
        return plugin, release, finished, failures

    def test_normal_shutdown_retires_response_without_touching_closed_ui(self):
        app = shutdown_game()
        app._term_add = Mock()
        plugin, release, finished, failures = self.start_identity(app)
        app.gps.close.side_effect = lambda: release.set()
        app._cleanup()
        self.assertTrue(finished.wait(1))
        self.assertEqual(failures, [])
        self.assertEqual(app._cleanup_errors, [])
        self.assertEqual(plugin._username, "")
        app._term_add.assert_not_called()
        self.assertTrue(plugin.on_unload())
        self.assertIn("loot-close", app.events)

    def test_update_cancels_if_identity_worker_did_not_release(self):
        app = update_game_with_real_cleanup()
        plugin, release, finished, failures = self.start_identity(app)
        PX.pressed = {PX.KEY_Y}
        try:
            with self.assertLogs(level="WARNING"):
                app._update_gameupd_screen()
        finally:
            PX.pressed.clear()
        self.assertIn("plugin unload", app._cleanup_errors)
        app._game_updates.install.assert_not_called()
        self.assertTrue(app._gameupd_done)
        self.assertFalse(app._gameupd_ok)
        self.assertIsNone(app._gameupd_restart_at)
        shutdown_log_count = app._term_add.call_count
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(failures, [])
        self.assertEqual(plugin._username, "")
        self.assertEqual(app._term_add.call_count, shutdown_log_count)
        self.assertFalse(any("[Sync]" in str(call) for call in app._term_add.call_args_list))

    def test_update_only_installs_after_real_cleanup_retired_identity(self):
        app = update_game_with_real_cleanup()
        plugin, release, finished, failures = self.start_identity(app)
        app.gps.close.side_effect = lambda: release.set()
        def install(_plan, quiescent):
            self.assertTrue(quiescent and app._cleanup_done)
            self.assertEqual(app._cleanup_errors, [])
            self.assertTrue(finished.is_set())
            self.assertTrue(plugin.on_unload())
            return SimpleNamespace(version="synthetic-next")
        app._game_updates.install.side_effect = install
        PX.pressed = {PX.KEY_Y}
        try:
            app._update_gameupd_screen()
        finally:
            PX.pressed.clear()
        app._game_updates.install.assert_not_called()
        self.assertFalse(app._cleanup_errors)
        PX.frame_count += 1
        app._update_gameupd_screen()
        app._game_updates.install.assert_called_once()
        self.assertTrue(app._gameupd_ok)
        self.assertEqual(failures, [])
        self.assertEqual(plugin._username, "")
        UPDATE_ENV["restart_game"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
