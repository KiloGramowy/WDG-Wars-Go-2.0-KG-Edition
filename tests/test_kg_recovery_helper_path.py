"""Actual recovery path selection and standalone helper processes, without USB.

The copied files form an isolated installation fixture, never a workaround in
the data directory. Every successful process executes the production helper's
--help path; no test selects a port or starts a reset/flash operation.
"""
import ast
import contextlib
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdogs.esp32_flash import FlashError


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'watchdogs' / 'app.py'
HELPER = APP.with_name('esp32_flash.py')


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def production_selector(app_file, data_dir):
    """Load just the real selector, avoiding game/hardware initialization."""
    tree = ast.parse(app_file.read_text(encoding='utf-8'))
    game = next(node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == 'WatchDogsGame')
    method = next(node for node in game.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == '_kg_recovery_helper_path')
    namespace = dict(__name__='watchdogs.recovery_path_test',
                     __package__='watchdogs', __file__=str(app_file),
                     Path=Path, os=os)
    executable = ast.fix_missing_locations(ast.Module(
        body=[copy.deepcopy(method)], type_ignores=[]))
    exec(compile(executable, str(app_file), 'exec'), namespace)
    return namespace['_kg_recovery_helper_path'], SimpleNamespace(_app_dir=str(data_dir))


class KGRecoveryHelperPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wdg helper path ')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def installation(self, *, include_helper=True):
        root = self.base / 'installed code with spaces'
        code = root / 'watchdogs'
        code.mkdir(parents=True)
        shutil.copyfile(APP, code / 'app.py')
        if include_helper:
            shutil.copyfile(HELPER, code / 'esp32_flash.py')
            self.assertEqual((code / 'esp32_flash.py').read_bytes(), HELPER.read_bytes())
        return code / 'app.py'

    def separate_directories(self):
        data = self.base / 'private data and loot'
        cwd = self.base / 'different working directory'
        data.mkdir()
        cwd.mkdir()
        sentinel = data / 'keep.txt'
        sentinel.write_bytes(b'SYNTHETIC player data; do not move or replace')
        return data, cwd, sentinel

    def select_and_run_help(self, app_file, data, cwd):
        selector, game = production_selector(app_file, data)
        with working_directory(cwd):
            selected = selector(game)
        self.assertEqual(selected, app_file.resolve().with_name('esp32_flash.py'))
        environment = os.environ.copy()
        environment.pop('PYTHONPATH', None)
        environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                           PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        # This is deliberately a real process, not the flash runner mock.
        result = subprocess.run([sys.executable, '-B', str(selected), '--help'],
            cwd=cwd, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--reset-c5-usb-to-app', result.stdout)
        self.assertEqual(game._app_dir, str(data))
        return selected

    def test_normal_installation_executes_the_actual_helper_help(self):
        selected = self.select_and_run_help(APP, ROOT, ROOT)
        self.assertEqual(selected, HELPER.resolve())

    def test_separate_data_and_different_cwd_do_not_select_the_loot_directory(self):
        data, cwd, sentinel = self.separate_directories()
        before = sentinel.read_bytes()
        selected = self.select_and_run_help(APP, data, cwd)
        self.assertEqual(selected, HELPER.resolve())
        self.assertFalse((data / 'watchdogs').exists())
        self.assertEqual(sentinel.read_bytes(), before)
        self.assertEqual(list(data.iterdir()), [sentinel])

    def test_installation_data_and_cwd_paths_with_spaces_execute_real_help(self):
        app_file = self.installation()
        data, cwd, sentinel = self.separate_directories()
        selected = self.select_and_run_help(app_file, data, cwd)
        self.assertIn(' ', str(selected))
        self.assertFalse(selected.is_relative_to(data))
        self.assertEqual(list(data.iterdir()), [sentinel])

    def test_changing_only_the_data_directory_cannot_change_the_selected_program(self):
        app_file = self.installation()
        first = self.base / 'first loot'
        second = self.base / 'second loot'
        first.mkdir()
        second.mkdir()
        selector, game = production_selector(app_file, first)
        with working_directory(second):
            original = selector(game)
            game._app_dir = str(second)
            self.assertEqual(selector(game), original)
        self.assertEqual(original, app_file.with_name('esp32_flash.py'))

    def test_missing_program_is_refused_by_the_actual_selector(self):
        app_file = self.installation(include_helper=False)
        data, cwd, _sentinel = self.separate_directories()
        selector, game = production_selector(app_file, data)
        with working_directory(cwd), self.assertRaises(FlashError):
            selector(game)

    def test_unreadable_program_is_refused_without_starting_a_process(self):
        selector, game = production_selector(APP, self.base)
        with patch.object(os, 'access', return_value=False), \
                patch.object(subprocess, 'run') as run, self.assertRaises(FlashError):
            selector(game)
        run.assert_not_called()

    def test_missing_helper_gate_preserves_connection_and_never_prepares_a_flash(self):
        from test_flash_preflight import FlashEnvironment
        env = FlashEnvironment(aio=True, allow_upstream=False)
        self.addCleanup(env.close)
        app_file = self.installation(include_helper=False)
        env.namespace['__file__'] = str(app_file)
        connection = env.game.serial
        env.flash()
        self.assertEqual([event[0] for event in env.events], ['probe'])
        self.assertEqual(env.kg_calls, [])
        self.assertEqual(env.flash_commands, [])
        self.assertEqual(env.recovery_commands, [])
        self.assertIs(env.game.serial, connection)
        self.assertTrue(connection.is_open)
        self.assertTrue(env.game._esp32 and env.game.state.connected)
        self.assertFalse(env.game._flash_device_acquired)
        self.assertFalse((env.root / 'firmware_cache').exists())

    def test_new_job_cannot_borrow_a_cached_helper_from_a_previous_attempt(self):
        from test_flash_preflight import FlashEnvironment
        env = FlashEnvironment(aio=False, allow_upstream=False)
        self.addCleanup(env.close)
        env.game._kg_fw_reset_helper = HELPER.resolve()
        env.namespace['__file__'] = str(self.installation(include_helper=False))
        env.flash()
        self.assertIsNone(env.game._kg_fw_reset_helper)
        self.assertEqual([event[0] for event in env.events], ['probe'])
        self.assertEqual(env.kg_calls, [])
        self.assertEqual(env.flash_commands, [])
        self.assertEqual(env.recovery_commands, [])


if __name__ == '__main__':
    unittest.main()
