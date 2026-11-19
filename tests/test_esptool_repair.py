"""Interpreter-bound dependency repair; no device, GPIO or real flash access."""
import contextlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watchdogs import esp32_flash as flash

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/player/Game Folder/.venv/bin/python3"


def native_bash():
    if os.name == "posix":
        return "/bin/bash" if Path("/bin/bash").is_file() else None
    git = shutil.which("git")
    choices = [Path(r"C:\Program Files\Git\bin\bash.exe")]
    if git:
        root = Path(git).resolve().parent.parent
        choices += [root / "usr/bin/sh.exe", root / "bin/bash.exe"]
    return next((str(p) for p in choices if p.is_file()), None)


NATIVE_BASH = native_bash()


def result(code=0, out="5.0.0\n", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class EsptoolRepairTests(unittest.TestCase):
    def test_supported_five_zero_is_not_reinstalled_or_upgraded(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return result()
        self.assertEqual(flash.repair_esptool(PYTHON, run=run), "5.0.0")
        self.assertEqual(calls, [[PYTHON, "-m", "esptool", "version"]])

    def test_missing_tool_repairs_the_same_interpreter_and_rechecks(self):
        answers = iter([result(1, "", "No module named esptool"), result(out="installed"), result()])
        calls = []; lines = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return next(answers)
        with patch.object(flash, "run_esptool", side_effect=AssertionError("Device access forbidden")):
            self.assertEqual(flash.repair_esptool(PYTHON, run=run, on_line=lines.append), "5.0.0")
        self.assertEqual(calls[1][0], [PYTHON, "-m", "pip", "install", "--disable-pip-version-check",
                                      "--no-input", "esptool>=5.0.0"])
        self.assertEqual(calls[0][0], calls[2][0])
        self.assertEqual(calls[1][1]["timeout"], 180)
        self.assertFalse(calls[1][1]["check"])
        self.assertIn("no firmware was flashed", lines[-1])

    def test_install_permission_failure_is_actionable_and_never_rechecks_or_flashes(self):
        answers = iter([result(1, "", "No module named esptool"),
                        result(1, "", "ERROR: Permission denied: site-packages")])
        calls = []
        def run(args, **kwargs):
            calls.append(args); return next(answers)
        with patch.object(flash, "run_esptool", side_effect=AssertionError("No flash")):
            with self.assertRaisesRegex(flash.FlashError, "Permission denied") as caught:
                flash.repair_esptool(PYTHON, run=run)
        self.assertEqual(len(calls), 2)
        self.assertIn(flash.esptool_repair_command(PYTHON), str(caught.exception))

    def test_install_timeout_launch_and_permission_errors_do_not_start_firmware(self):
        for error in [subprocess.TimeoutExpired("pip", 180), PermissionError("read-only venv"),
                      FileNotFoundError("interpreter missing")]:
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    return result(1, "", "No module named esptool")
                raise error
            with self.subTest(error=type(error).__name__), patch.object(
                    flash, "run_esptool", side_effect=AssertionError("No flash")):
                with self.assertRaises(flash.FlashError) as caught:
                    flash.repair_esptool(PYTHON, run=run)
                self.assertIn(flash.esptool_repair_command(PYTHON), str(caught.exception))
                self.assertEqual(len(calls), 2)

    def test_successful_pip_exit_without_working_tool_is_not_success(self):
        answers = iter([result(1, "", "No module named esptool"), result(out="installed"),
                        result(1, "", "ImportError: broken dependency")])
        with patch.object(flash, "run_esptool", side_effect=AssertionError("No flash")):
            with self.assertRaisesRegex(flash.FlashError, "pip completed but esptool is still unusable"):
                flash.repair_esptool(PYTHON, run=lambda *a, **k: next(answers))

    def test_old_tool_uses_the_existing_minimum_and_is_verified_after_install(self):
        answers = iter([result(out="4.8.0\n"), result(out="installed"), result(out="5.0.0\n")])
        self.assertEqual(flash.repair_esptool(PYTHON, run=lambda *a, **k: next(answers)), "5.0.0")
        self.assertEqual(flash.MIN_ESPTOOL, (5, 0, 0))

    def test_posix_command_survives_spaces_quotes_and_requirement_redirection(self):
        python = "/home/O'Neil/Game Folder/.venv/bin/python3"
        with patch.object(flash.os, "name", "posix"):
            self.assertEqual(shlex.split(flash.esptool_repair_command(python)),
                             flash._esptool_install_args(python))

    def test_powershell_command_quotes_every_argument_and_escapes_apostrophe(self):
        python = r"C:\O'Neil\Game Folder\.venv\Scripts\python.exe"
        with patch.object(flash.os, "name", "nt"):
            command = flash.esptool_repair_command(python)
        self.assertTrue(command.startswith("& 'C:\\O''Neil\\Game Folder"))
        self.assertIn("'esptool>=5.0.0'", command)

    def test_cli_repair_uses_its_actual_interpreter_and_never_the_flash_runner(self):
        with patch.object(flash.sys, "executable", PYTHON), patch.object(
                flash, "repair_esptool", return_value="5.0.0") as repair, patch.object(
                flash, "run_esptool", side_effect=AssertionError("No flash")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flash._dependency_main(["--repair-esptool"]), 0)
        self.assertEqual(repair.call_args.args, (PYTHON,))

    def test_cli_failed_dependency_is_nonzero_and_does_not_flash(self):
        with patch.object(flash, "repair_esptool", side_effect=flash.FlashError("network unavailable")), patch.object(
                flash, "run_esptool", side_effect=AssertionError("No flash")), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(flash._dependency_main(["--repair-esptool"]), 1)
        self.assertIn("network unavailable", err.getvalue())


class ExistingVenvLauncherTests(unittest.TestCase):
    @unittest.skipUnless(NATIVE_BASH, "A native POSIX/Git Bash is required for the launcher fixture")
    def test_existing_venv_checks_and_repairs_before_launch_without_setup_or_hardware(self):
        bash = NATIVE_BASH
        for repair_code in [0, 1]:
            with self.subTest(repair_code=repair_code), tempfile.TemporaryDirectory(prefix="wdg-launch-fixture-") as directory:
                root = Path(directory).resolve()
                self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
                (root / ".venv/bin").mkdir(parents=True)
                (root / "fake-bin").mkdir()
                (root / "run.sh").write_bytes((ROOT / "run.sh").read_bytes())
                (root / "setup.sh").write_bytes(b'#!/bin/bash\necho SETUP_MUST_NOT_RUN >> fixture.log\nexit 99\n')
                (root / ".venv/bin/python3").write_bytes(
                    b'#!/bin/bash\nprintf "dependency:%s\\n" "$*" >> fixture.log\nexit "${REPAIR_EXIT:-0}"\n')
                (root / "fake-bin/sudo").write_bytes(
                    b'#!/bin/bash\nif [ "$1" = "-E" ]; then printf "game:%s\\n" "$*" >> fixture.log; fi\nexit 0\n')
                (root / "fake-bin/id").write_bytes(b'#!/bin/bash\necho 0\n')
                (root / "fake-bin/dirname").write_bytes(
                    b'#!/bin/bash\ncase "$1" in */*) printf "%s\\n" "${1%/*}";; *) echo .;; esac\n')
                for name in ["run.sh", "setup.sh", ".venv/bin/python3", "fake-bin/sudo", "fake-bin/id", "fake-bin/dirname"]:
                    script = root / name
                    # The bundled Git Bash ships as /usr/bin/sh.exe rather
                    # than /bin/bash; fixture executables use that interpreter.
                    shebang = b"#!/usr/bin/sh" if os.name == "nt" else b"#!/bin/sh"
                    script.write_bytes(script.read_bytes().replace(b"#!/bin/bash", shebang, 1))
                    script.chmod(0o755)
                env = os.environ.copy();env["REPAIR_EXIT"] = str(repair_code)
                run = subprocess.run([bash, "--noprofile", "--norc", "-c",
                                      'PATH="$PWD/fake-bin:/usr/bin:/bin" "$BASH" run.sh --fixture'],
                                     cwd=root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stderr)
                lines = (root / "fixture.log").read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines, ["dependency:-m watchdogs.esp32_flash --repair-esptool",
                                         "game:-E .venv/bin/python3 -m watchdogs --fixture"])
                if repair_code:
                    self.assertIn("firmware installation remains unavailable", run.stderr)
