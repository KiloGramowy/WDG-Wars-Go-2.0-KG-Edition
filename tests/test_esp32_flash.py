"""Flash helpers with fake I/O and isolated Python children; no device/RF I/O."""
import io
import contextlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile

from watchdogs.esp32_flash import (
    FlashError, check_esptool, extract_firmware_archive,
    run_esptool, select_firmware_asset, reset_c5_usb_to_app,
)
from watchdogs import esp32_flash as flash


IMAGES = ("bootloader.bin", "partition-table.bin", "projectZerobyLOCOSP-xiao.bin")
PYTHON = "/game/.venv/bin/python3"


class ToolPreflightTests(unittest.TestCase):
    def result(self, output="5.4.0\n", code=0, error=""):
        return SimpleNamespace(returncode=code, stdout=output, stderr=error)

    def test_uses_exact_game_python_version_probe_with_ten_second_limit(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs)); return self.result()
        self.assertEqual(check_esptool(PYTHON, run=run), "5.4.0")
        command, kwargs = calls[0]
        self.assertEqual(command, [PYTHON, "-m", "esptool", "version"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)

    def test_accepts_supported_versions_and_actual_tool_banner(self):
        for value in ("5.0.0", "5.0", "esptool v5.4.0\n5.4.0\n",
                      "esptool.py v5.1.0", "v6.0.0", "5.0.0.post1", "5.0.0+local"):
            with self.subTest(value=value):
                self.assertTrue(check_esptool(PYTHON, run=lambda *a, **k: self.result(value)))

    def test_rejects_old_or_prerelease_minimum(self):
        for value in ("4.8.1", "esptool.py v4.9", "5.0.0rc1", "5.0.0.dev1", "5.0.0-beta.1"):
            with self.subTest(value=value), self.assertRaisesRegex(FlashError, ">=5.0.0"):
                check_esptool(PYTHON, run=lambda *a, **k: self.result(value))

    def test_unknown_version_never_passes(self):
        with self.assertRaisesRegex(FlashError, "Cannot determine"):
            check_esptool(PYTHON, run=lambda *a, **k: self.result("unexpected output"))

    def test_missing_module_and_environment_permission_are_distinct(self):
        for error, expected in [("No module named esptool\n", "unavailable"),
                                ("No module named 'esptool'\n", "unavailable"),
                                ("PermissionError: [Errno 13] Permission denied", "permission denied"),
                                ("ImportError: broken dependency", "cannot run")]:
            with self.subTest(error=error), self.assertRaisesRegex(FlashError, expected) as caught:
                check_esptool(PYTHON, run=lambda *a, **k: self.result(code=1, error=error))
            self.assertIn(PYTHON, str(caught.exception))

    def test_launch_permission_timeout_and_invalid_interpreter_are_actionable(self):
        for error, expected in [(PermissionError("denied"), "Permission denied"),
                                (FileNotFoundError("gone"), "Cannot start"),
                                (subprocess.TimeoutExpired("fake", 10), "timed out"),
                                (ValueError("invalid"), "Cannot start")]:
            def run(*args, **kwargs): raise error
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(FlashError, expected) as caught:
                check_esptool(PYTHON, run=run)
            self.assertIn(PYTHON, str(caught.exception))

    def test_error_diagnostic_is_bounded(self):
        with self.assertRaises(FlashError) as caught:
            check_esptool(PYTHON, run=lambda *a, **k: self.result(code=1, error="X" * 10000))
        self.assertLess(len(str(caught.exception)), 800)


class FirmwareArchiveTests(unittest.TestCase):
    def release(self, order=("xiao", "wroom")):
        return {"tag_name": "v1.7.0", "assets": [
            {"name": "projectZerobyLOCOSP" + ("-xiao" if b == "xiao" else "") + "-1.7.0.zip",
             "browser_download_url": "https://example.invalid/" + b + ".zip"}
            for b in order]}

    def test_asset_order_cannot_cross_board_variants(self):
        for order in (("xiao", "wroom"), ("wroom", "xiao")):
            for board in ("xiao", "wroom"):
                with self.subTest(order=order, board=board):
                    self.assertEqual(select_firmware_asset(self.release(order), board),
                                     "https://example.invalid/" + board + ".zip")

    def test_exact_name_does_not_accept_with_fap_or_another_version(self):
        release = self.release(())
        release["assets"] = [{"name": name, "browser_download_url": "https://example.invalid/wrong.zip"}
                             for name in ("projectZerobyLOCOSP-xiao-1.7.0.zip",
                                          "projectZerobyLOCOSP-with-fap-1.7.0.zip",
                                          "projectZerobyLOCOSP-1.6.5.zip")]
        with self.assertRaises(FlashError):
            select_firmware_asset(release, "wroom")

    def test_missing_asset_is_refused_without_guessing_a_stock_download(self):
        with self.assertRaisesRegex(FlashError, "no fallback"):
            select_firmware_asset(self.release(()), "xiao")

    def test_ambiguous_archive_invalid_tag_board_or_url_is_refused(self):
        duplicated = self.release(); duplicated["assets"].append(duplicated["assets"][0].copy())
        invalid_url = self.release(); invalid_url["assets"][0]["browser_download_url"] = "file:///tmp/firmware.zip"
        for release, board in ((duplicated, "xiao"), ({"tag_name": "../unsafe"}, "xiao"),
                               (self.release(), "other"), (invalid_url, "xiao")):
            with self.subTest(release=release, board=board), self.assertRaises(FlashError):
                select_firmware_asset(release, board)

    def archive(self, directory, entries=None):
        path = directory / "current.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, value in entries or [(name, b"current-" + name.encode()) for name in IMAGES]:
                    archive.writestr(name, value)
        return path

    def test_extracts_only_current_required_images_into_fresh_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); dest = root / "attempt"
            entries = [(name, name.encode()) for name in IMAGES] + [("README.md", b"extra")]
            archive = self.archive(root, entries)
            extract_firmware_archive(archive, dest, IMAGES)
            self.assertEqual(sorted(p.name for p in dest.iterdir()), sorted(IMAGES))
            for name in IMAGES:
                self.assertEqual((dest / name).read_bytes(), name.encode())

    def test_stale_cache_cannot_supply_missing_current_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); dest = root / "old-cache"; dest.mkdir()
            old = dest / IMAGES[2]; old.write_bytes(b"old")
            archive = self.archive(root, [(name, b"current") for name in IMAGES[:2]])
            with self.assertRaisesRegex(FlashError, "Current firmware archive is missing"):
                extract_firmware_archive(archive, dest, IMAGES)
            self.assertEqual(old.read_bytes(), b"old")
            self.assertEqual(len(list(dest.iterdir())), 1)

    def test_wrong_board_archive_cannot_be_completed_by_old_wroom_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); dest = root / "old-cache"; dest.mkdir()
            wroom = "projectZerobyLOCOSP.bin"; (dest / wroom).write_bytes(b"old-wroom")
            with self.assertRaisesRegex(FlashError, "missing"):
                extract_firmware_archive(self.archive(root), dest, (*IMAGES[:2], wroom))
            self.assertEqual((dest / wroom).read_bytes(), b"old-wroom")

    def test_duplicate_traversal_symlink_empty_and_oversized_images_are_refused_before_write(self):
        symlink = zipfile.ZipInfo(IMAGES[0]); symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [([(name, b"x") for name in IMAGES] + [(IMAGES[0], b"again")], None),
                 ([(name, b"x") for name in IMAGES] + [("../escape", b"x")], None),
                 ([(symlink, b"target"), *[(name, b"x") for name in IMAGES[1:]]], None),
                 ([(IMAGES[0], b""), *[(name, b"x") for name in IMAGES[1:]]], None),
                 ([(name, b"xx") for name in IMAGES], 1)]
        for entries, limit in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); dest = root / "attempt"; archive = self.archive(root, entries)
                with patch("watchdogs.esp32_flash.MAX_FIRMWARE_FILE_BYTES", limit or 16 * 1024 * 1024):
                    with self.assertRaises(FlashError):
                        extract_firmware_archive(archive, dest, IMAGES)
                self.assertFalse(dest.exists())

    def test_crc_corruption_is_detected_before_destination_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = self.archive(root); dest = root / "attempt"
            with zipfile.ZipFile(archive) as source:
                info = source.getinfo(IMAGES[0])
                offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            raw = bytearray(archive.read_bytes()); raw[offset] ^= 1; archive.write_bytes(raw)
            with self.assertRaisesRegex(FlashError, "Cannot prepare"):
                extract_firmware_archive(archive, dest, IMAGES)
            self.assertFalse(dest.exists())

    def test_preexisting_required_destination_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); dest = root / "attempt"; dest.mkdir()
            (dest / IMAGES[0]).write_bytes(b"previous")
            with self.assertRaisesRegex(FlashError, "fresh"):
                extract_firmware_archive(self.archive(root), dest, IMAGES)
            self.assertEqual((dest / IMAGES[0]).read_bytes(), b"previous")

    def test_destination_permission_error_has_clear_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = self.archive(root)
            with patch("watchdogs.esp32_flash.Path.mkdir", side_effect=PermissionError("read only")):
                with self.assertRaisesRegex(FlashError, "Permission denied preparing firmware"):
                    extract_firmware_archive(archive, root / "attempt", IMAGES)


class NativeUSBResetTests(unittest.TestCase):
    def connection(self, events, *, open_error=None, close_error=None):
        class Connection:
            def __setattr__(self, name, value):
                events.append(("set", name, value))
                object.__setattr__(self, name, value)
            def open(self):
                events.append(("open", self.port, self.dtr, self.rts))
                if open_error:
                    raise open_error
            def close(self):
                events.append(("close",))
                if close_error:
                    raise close_error
            def write(self, *args):
                raise AssertionError("Reset helper must not send serial/ROM commands")
            def read(self, *args):
                raise AssertionError("Reset helper must not synchronize or read ROM")
        return Connection()

    def test_native_reset_sets_closed_lines_before_open_and_uses_official_usb_reset(self):
        for platform in ("posix", "nt"):
            with self.subTest(platform=platform):
                events = []
                connection = self.connection(events)
                def factory(**options):
                    events.append(("factory", options))
                    return connection
                def reset(port, *, uses_usb):
                    self.assertIs(port, connection)
                    self.assertTrue(uses_usb)
                    events.append(("reset-created",))
                    return lambda: events.append(("reset-called",))
                with patch.object(flash.os, "name", platform):
                    reset_c5_usb_to_app("/dev/ttyACM-synthetic", serial_factory=factory,
                                        hard_reset=reset)
                self.assertEqual(events, [
                    ("factory", dict(port=None, baudrate=115200, timeout=1, write_timeout=2,
                                     **({"exclusive": True} if platform == "posix" else {}))),
                    ("set", "dtr", False), ("set", "rts", False),
                    ("set", "port", "/dev/ttyACM-synthetic"),
                    ("open", "/dev/ttyACM-synthetic", False, False),
                    ("reset-created",), ("reset-called",), ("close",)])

    def test_failed_open_closes_owned_connection_without_reset(self):
        events = []
        connection = self.connection(events, open_error=OSError("endpoint not ready"))
        with self.assertRaisesRegex(FlashError, "endpoint not ready"):
            reset_c5_usb_to_app("/dev/ttyACM-synthetic",
                serial_factory=lambda **kwargs: connection,
                hard_reset=lambda *args, **kwargs: self.fail("Failed open cannot reset"))
        self.assertEqual(events[-1], ("close",))

    def test_failed_reset_keeps_primary_error_even_if_close_also_fails(self):
        events = []
        connection = self.connection(events, close_error=OSError("secondary close error"))
        def reset(*args, **kwargs):
            raise OSError("primary control line error")
        with self.assertRaisesRegex(FlashError, "primary control line error") as caught:
            reset_c5_usb_to_app("/dev/ttyACM-synthetic",
                serial_factory=lambda **kwargs: connection, hard_reset=reset)
        self.assertNotIn("secondary", str(caught.exception))
        self.assertEqual(events[-1], ("close",))

    def test_unconfirmed_close_is_not_a_successful_reset_request(self):
        events = []
        connection = self.connection(events, close_error=OSError("close not confirmed"))
        with self.assertRaisesRegex(FlashError, "close not confirmed"):
            reset_c5_usb_to_app("/dev/ttyACM-synthetic",
                serial_factory=lambda **kwargs: connection,
                hard_reset=lambda *args, **kwargs: lambda: None)

    def test_url_empty_and_control_character_ports_are_refused_before_factory(self):
        for port in (None, "", " ", "socket://example.invalid:23", "rfc2217://example.invalid", "COM1\n"):
            with self.subTest(port=port), self.assertRaises(FlashError):
                reset_c5_usb_to_app(port,
                    serial_factory=lambda **kwargs: self.fail("Do not open invalid ports"))

    def test_reset_cli_never_repairs_checks_or_flashes_and_does_not_claim_boot(self):
        with patch.object(flash, "reset_c5_usb_to_app") as reset, patch.object(
                flash, "repair_esptool", side_effect=AssertionError("No repair")), patch.object(
                flash, "check_esptool", side_effect=AssertionError("No tool check")), patch.object(
                flash, "run_esptool", side_effect=AssertionError("No recursive process/flash")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(flash._dependency_main(["--reset-c5-usb-to-app", "/dev/ttyACM-synthetic"]), 0)
        reset.assert_called_once_with("/dev/ttyACM-synthetic")
        self.assertIn("requested", output.getvalue())
        self.assertIn("not confirmed", output.getvalue())

    def test_reset_cli_failure_is_nonzero_and_mutually_exclusive_with_repair(self):
        with patch.object(flash, "reset_c5_usb_to_app", side_effect=FlashError("reset failed")), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(flash._dependency_main(["--reset-c5-usb-to-app", "/dev/ttyACM-synthetic"]), 1)
        self.assertIn("reset failed", error.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            flash._dependency_main(["--reset-c5-usb-to-app", "COM1", "--repair-esptool"])
        self.assertEqual(caught.exception.code, 2)


class FakeProcess:
    def __init__(self, data=b"Writing 10%\rWriting 100%\nVerified\n", code=0,
                 terminate_works=True, kill_works=True, stream=None):
        self.stdout = stream if stream is not None else io.BytesIO(data)
        self.code = code
        self.terminate_works, self.kill_works = terminate_works, kill_works
        self.actions = []

    def poll(self): return self.code
    def terminate(self):
        self.actions.append("terminate")
        if self.terminate_works: self.code = -15
    def kill(self):
        self.actions.append("kill")
        if self.kill_works: self.code = -9
    def wait(self, timeout):
        self.actions.append(("wait", timeout))
        if self.code is None: raise subprocess.TimeoutExpired("fake", timeout)
        return self.code


class ProcessSupervisionTests(unittest.TestCase):
    def supervise(self, process, **kwargs):
        output = []; created = []
        options = dict(total_timeout=.3, idle_timeout=.1, stop_timeout=.01,
                       drain_timeout=.04, poll_interval=.001)
        options.update(kwargs)
        run_esptool([PYTHON, "-m", "esptool", "fake-no-device"], output.append,
                    popen=lambda *a, **k: process, on_process=created.append, **options)
        return output, created

    def test_success_tracks_child_and_delivers_cr_lf_and_final_partial_output(self):
        process = FakeProcess(data=b"Writing 10%\rWriting 100%\nVerified")
        output, created = self.supervise(process)
        self.assertEqual(output, ["Writing 10%", "Writing 100%", "Verified"])
        self.assertEqual(created, [process])
        self.assertEqual(process.actions, [])

    def test_nonzero_exit_never_reports_success(self):
        process = FakeProcess(data=b"USB write timeout\n", code=2)
        with self.assertRaisesRegex(FlashError, "exit code 2") as caught:
            self.supervise(process)
        self.assertIsNone(caught.exception.process)

    def test_launch_permission_error_is_reported_without_live_handle(self):
        def denied(*a, **k): raise PermissionError("not executable")
        with self.assertRaisesRegex(FlashError, "Permission denied") as caught:
            run_esptool(["fake"], lambda line: None, popen=denied)
        self.assertIsNone(caught.exception.process)

    def test_idle_process_is_terminated_and_exit_observed(self):
        process = FakeProcess(data=b"", code=None)
        with self.assertRaisesRegex(FlashError, "no output") as caught:
            self.supervise(process, idle_timeout=.02)
        self.assertIsNone(caught.exception.process)
        self.assertIn("terminate", process.actions)
        self.assertEqual(process.code, -15)

    def test_idle_failure_preserves_partial_connect_and_late_stderr(self):
        release = threading.Event()
        self.addCleanup(release.set)
        class FinalOutputPipe:
            index = 0
            def read(self, count):
                self.index += 1
                if self.index == 1:
                    return b"Connecting..."
                if self.index == 2:
                    release.wait(2)
                    return b"\nUSB endpoint unavailable\n"
                return b""
            def close(self): pass
        process = FakeProcess(code=None, stream=FinalOutputPipe())
        original_terminate = process.terminate
        def terminate():
            original_terminate()
            release.set()
        process.terminate = terminate
        output = []
        with self.assertRaisesRegex(FlashError, "produced no output") as caught:
            run_esptool(["fake"], output.append, popen=lambda *a, **k: process,
                        total_timeout=1, idle_timeout=.03, stop_timeout=.01,
                        drain_timeout=.1, poll_interval=.001)
        self.assertEqual(output, ["Connecting...", "USB endpoint unavailable"])
        self.assertIsNone(caught.exception.process)
        self.assertEqual(process.code, -15)

    def test_failure_tail_callback_cannot_mask_original_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)
        class PartialPipe:
            first = True
            def read(self, count):
                if self.first:
                    self.first = False
                    return b"Connecting..."
                release.wait(2)
                return b""
            def close(self): pass
        process = FakeProcess(code=None, stream=PartialPipe())
        original_terminate = process.terminate
        def terminate():
            original_terminate()
            release.set()
        process.terminate = terminate
        def broken(line):
            raise RuntimeError("late diagnostic callback failed")
        with self.assertRaisesRegex(FlashError, "produced no output") as caught:
            run_esptool(["fake"], broken, popen=lambda *a, **k: process,
                        total_timeout=1, idle_timeout=.03, stop_timeout=.01,
                        drain_timeout=.1, poll_interval=.001)
        self.assertNotIn("callback", str(caught.exception))
        self.assertIsNone(caught.exception.process)

    def test_unconfirmed_child_keeps_tail_handle_and_bounded_drain(self):
        release = threading.Event()
        self.addCleanup(release.set)
        class PartialPipe:
            first = True
            def read(self, count):
                if self.first:
                    self.first = False
                    return b"Connecting..."
                release.wait(2)
                return b""
            def close(self): pass
        process = FakeProcess(code=None, stream=PartialPipe(),
                              terminate_works=False, kill_works=False)
        output = []
        started = time.monotonic()
        with self.assertRaisesRegex(FlashError, "keep the serial port reserved") as caught:
            run_esptool(["fake"], output.append, popen=lambda *a, **k: process,
                        total_timeout=1, idle_timeout=.03, stop_timeout=.01,
                        drain_timeout=10, poll_interval=.001)
        self.assertLess(time.monotonic() - started, .6)
        self.assertEqual(output, ["Connecting..."])
        self.assertIs(caught.exception.process, process)
        self.assertIsNone(process.poll())

    def test_real_child_partial_output_survives_timeout_without_newline(self):
        # This is a Python pipe/process test, never an esptool/device call.
        output, children = [], []
        with self.assertRaisesRegex(FlashError, "produced no output") as caught:
            run_esptool([sys.executable, "-c",
                         "import os,time; os.write(1,b'Connecting...'); time.sleep(10)"],
                        output.append, on_process=children.append,
                        total_timeout=3, idle_timeout=.2, stop_timeout=1,
                        drain_timeout=.2, poll_interval=.005)
        self.assertEqual(output, ["Connecting..."])
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(caught.exception.process)

    def test_terminate_timeout_escalates_to_kill_and_wait(self):
        process = FakeProcess(data=b"", code=None, terminate_works=False)
        with self.assertRaises(FlashError) as caught:
            self.supervise(process, idle_timeout=.02)
        self.assertIsNone(caught.exception.process)
        self.assertEqual(process.actions, ["terminate", ("wait", .01), "kill", ("wait", .01)])

    def test_unkillable_process_retains_handle_and_explicit_port_reservation(self):
        process = FakeProcess(data=b"", code=None, terminate_works=False, kill_works=False)
        with self.assertRaisesRegex(FlashError, "keep the serial port reserved") as caught:
            self.supervise(process, idle_timeout=.02)
        self.assertIs(caught.exception.process, process)
        self.assertIsNone(process.poll())

    def test_callback_failure_stops_live_child(self):
        process = FakeProcess(code=None)
        def broken(line): raise RuntimeError("UI output failed")
        with self.assertRaisesRegex(FlashError, "UI output failed"):
            run_esptool(["fake"], broken, popen=lambda *a, **k: process,
                        total_timeout=.3, idle_timeout=.1, stop_timeout=.01, poll_interval=.001)
        self.assertEqual(process.code, -15)

    def test_reader_error_stops_child(self):
        class BrokenPipe:
            def read(self, count): raise OSError("fake pipe error")
            def close(self): pass
        process = FakeProcess(code=None, stream=BrokenPipe())
        with self.assertRaisesRegex(FlashError, "Cannot read esptool output"):
            self.supervise(process)
        self.assertEqual(process.code, -15)

    def test_blocked_pipe_does_not_block_process_deadline_or_foreign_close(self):
        release = threading.Event()
        class BlockedPipe:
            closed = False
            def read(self, count): release.wait(2); return b""
            def close(self): self.closed = True
        pipe = BlockedPipe(); process = FakeProcess(code=None, stream=pipe)
        try:
            started = time.monotonic()
            with self.assertRaises(FlashError): self.supervise(process, idle_timeout=.02)
            self.assertLess(time.monotonic() - started, .6)
            self.assertFalse(pipe.closed)
            self.assertEqual(process.code, -15)
        finally:
            release.set()

    def test_partial_output_without_newline_resets_idle_deadline(self):
        class SlowChunks:
            def __init__(self): self.index = 0
            def read(self, count):
                time.sleep(.012); self.index += 1
                if self.index <= 30: return b"."
                process.code = 0; return b""
            def close(self): pass
        stream = SlowChunks(); process = FakeProcess(code=None, stream=stream)
        output, _ = self.supervise(process, idle_timeout=.15, total_timeout=1.5)
        self.assertEqual(output, ["." * 30])
        self.assertEqual(process.actions, [])

    def test_continuous_output_does_not_disable_total_deadline(self):
        class EndlessChunks:
            def read(self, count): time.sleep(.001); return b"progress\r"
            def close(self): pass
        process = FakeProcess(code=None, stream=EndlessChunks())
        with self.assertRaisesRegex(FlashError, "time limit"):
            self.supervise(process, total_timeout=.03)
        self.assertEqual(process.code, -15)

    def test_exited_child_with_inherited_blocked_pipe_has_bounded_drain(self):
        release = threading.Event()
        class InheritedPipe:
            def read(self, count): release.wait(2); return b""
            def close(self): pass
        process = FakeProcess(code=0, stream=InheritedPipe())
        try:
            with self.assertRaisesRegex(FlashError, "output pipe did not close") as caught:
                self.supervise(process, drain_timeout=.02)
            self.assertIsNone(caught.exception.process)
        finally:
            release.set()

    def test_binary_pipe_configuration_and_admission_callback_precede_monitoring(self):
        process = FakeProcess(); calls = []
        def popen(command, **kwargs): calls.append(kwargs); return process
        tracked = []
        run_esptool(["fake"], lambda line: self.assertEqual(tracked, [process]),
                    popen=popen, on_process=tracked.append, poll_interval=.001)
        self.assertFalse(calls[0]["text"])
        self.assertEqual(calls[0]["bufsize"], 0)

    def test_invalid_timeouts_are_refused_without_starting_process(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(FlashError):
                run_esptool(["fake"], lambda line: None, total_timeout=value,
                            popen=lambda *a, **k: self.fail("must not start"))


if __name__ == "__main__":
    unittest.main()
