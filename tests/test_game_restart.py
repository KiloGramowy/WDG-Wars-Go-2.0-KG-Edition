"""Restart argument/environment preservation; no real game or radio is run."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from watchdogs.game_restart import restart_game


class GameRestartTests(unittest.TestCase):
    def entry(self, root):
        package = root / "watchdogs"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text("# synthetic entry point\n", encoding="utf-8")
        return package

    def test_exec_uses_install_root_same_interpreter_and_literal_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package = self.entry(root)
            previous = Path.cwd()
            os.chdir(package)
            calls = []

            def exec_failure(executable, argv):
                calls.append((Path.cwd(), executable, argv))
                raise OSError("synthetic exec failure")

            arguments = ["/dev/ttyUSB TEST", "player data", "$(literal)"]
            try:
                with patch("os.execv", side_effect=exec_failure), self.assertRaises(OSError):
                    restart_game(root, arguments=arguments)
                self.assertEqual(Path.cwd(), package)
            finally:
                os.chdir(previous)
            self.assertEqual(calls, [(root, sys.executable,
                [sys.executable, "-B", "-m", "watchdogs", *arguments])])

    def test_default_arguments_and_environment_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.entry(root)
            environment = dict(os.environ)
            with patch.object(sys, "argv", ["module.py", "/dev/ttyTEST", "loot"]), \
                    patch("os.execv", side_effect=OSError("test")) as execute:
                with self.assertRaises(OSError):
                    restart_game(root)
            self.assertEqual(execute.call_args.args,
                (sys.executable, [sys.executable, "-B", "-m", "watchdogs", "/dev/ttyTEST", "loot"]))
            self.assertEqual(dict(os.environ), environment)

    def test_missing_entry_point_never_executes_or_changes_directory(self):
        with tempfile.TemporaryDirectory() as temporary, patch("os.execv") as execute:
            previous = Path.cwd()
            with self.assertRaisesRegex(RuntimeError, "entry point"):
                restart_game(temporary)
            self.assertEqual(Path.cwd(), previous)
            execute.assert_not_called()

    def test_unexpected_exec_return_is_reported_and_restores_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package = self.entry(root)
            previous = Path.cwd()
            os.chdir(package)
            try:
                with patch("os.execv", return_value=None), self.assertRaisesRegex(RuntimeError, "not restarted"):
                    restart_game(root)
                self.assertEqual(Path.cwd(), package)
            finally:
                os.chdir(previous)

    @unittest.skipUnless(os.name == "posix", "Real process replacement is verified on uConsole Linux")
    def test_real_exec_in_isolated_child_preserves_args_environment_and_player_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package = self.entry(root)
            result = root / "restart-result.json"
            data = root / "player data" / "loot_db.json"
            data.parent.mkdir()
            before = b'{"xp":1597146,"contacts":["saved"]}\n'
            data.write_bytes(before)
            # The replacement module only reports startup context. It never
            # imports WatchDogsGame, initializes a renderer or touches hardware.
            (package / "__main__.py").write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                "Path(os.environ['WDG_RESTART_TEST_RESULT']).write_text(json.dumps({"
                "'argv':sys.argv[1:],'cwd':os.getcwd(),'pid':os.getpid(),"
                "'executable':sys.executable,'sentinel':os.environ['WDG_RESTART_TEST_SENTINEL']}))\n",
                encoding="utf-8")
            helper_root = str(Path(__file__).resolve().parents[1])
            args = ["/dev/ttyUSB TEST", "player data", "$(literal)"]
            runner = ("import os,sys\n"
                      f"sys.path.insert(0,{helper_root!r})\n"
                      "from watchdogs.game_restart import restart_game\n"
                      f"os.chdir({str(package)!r})\n"
                      f"restart_game({str(root)!r},arguments={args!r})\n")
            environment = dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1",
                WDG_RESTART_TEST_RESULT=str(result), WDG_RESTART_TEST_SENTINEL="preserved")
            process = subprocess.Popen([sys.executable, "-B", "-c", runner], env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = process.communicate(timeout=20)
            except BaseException:
                process.kill()
                process.communicate()
                raise
            self.assertEqual(process.returncode, 0, stderr or stdout)
            observed = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(observed["argv"], args)
            self.assertEqual(Path(observed["cwd"]).resolve(), root)
            self.assertEqual(Path(observed["executable"]).resolve(), Path(sys.executable).resolve())
            self.assertEqual(observed["sentinel"], "preserved")
            if os.name == "posix":
                self.assertEqual(observed["pid"], process.pid)
            self.assertEqual(data.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
