"""Small, device-independent helpers for the existing ESP32 flash wizard.

Dependency repair is separate from device operations. The explicit native USB
reset helper opens only the already-bound local port to request an application
reset; it never synchronizes ROM, sends device commands or flashes firmware.
The caller owns the port and the lifetime of its attempt directory, including
the exceptional case of an unkillable child process.
"""
import codecs
import math
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import zipfile


MIN_ESPTOOL = (5, 0, 0)
MAX_FIRMWARE_FILE_BYTES = 16 * 1024 * 1024


class FlashError(RuntimeError):
    """A flash failure; process is retained only when exit is unconfirmed."""
    def __init__(self, message, process=None):
        super().__init__(message)
        self.process = process


def _text(value, limit=600):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value or "")
    return " ".join(value.replace("\x00", "").split())[:limit]


def _esptool_install_args(python):
    return [str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "esptool>=5.0.0"]


def esptool_repair_command(python):
    """A safely quoted command for the exact game Python (no system pip)."""
    args = _esptool_install_args(python)
    if os.name == "nt":
        # The desktop terminal is PowerShell; cmd-style quoting leaves >= as
        # redirection. Quote every argument and use PowerShell's call operator.
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
    return shlex.join(args)


def _esptool_failure(message, python):
    return FlashError(message + ". Repair with the game Python: " +
                      esptool_repair_command(python))


def check_esptool(python, *, run=None):
    """Probe the exact game interpreter before any download/device operation."""
    run = run or subprocess.run
    try:
        result = run([python, "-m", "esptool", "version"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired as exc:
        raise _esptool_failure(f"esptool check timed out (10 s). Python: {python}", python) from exc
    except PermissionError as exc:
        raise _esptool_failure(f"Permission denied running the game Python: {python}. "
                               "Check execute/access permissions; no device was changed", python) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise _esptool_failure(f"Cannot start the game Python: {python}. {_text(exc)}", python) from exc

    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    detail = _text(stderr or stdout, 400)
    if result.returncode != 0:
        if re.search(r"No module named [\"']?esptool[\"']?(?:\s|$)", str(stderr)):
            reason = "esptool is unavailable in the game Python"
        elif "permission denied" in detail.lower() or "permissionerror" in detail.lower():
            reason = "esptool cannot access its Python environment (permission denied)"
        else:
            reason = "esptool cannot run in the game Python"
        raise _esptool_failure(f"{reason}. Python: {python}. {detail}", python)

    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    versions = re.findall(
        r"(?im)^\s*(?:esptool(?:\.py)?\s+)?v?"
        r"(\d+)\.(\d+)(?:\.(\d+))?([a-z][\w.-]*|[-+][\w.-]+|\.[a-z][\w.-]*)?\s*$",
        stdout[:16384])
    if not versions:
        raise _esptool_failure(f"Cannot determine the esptool version; require >=5.0.0. "
                               f"Python: {python}. {_text(stdout, 400)}", python)
    major, minor, patch, suffix = versions[-1]
    numeric = (int(major), int(minor), int(patch or 0))
    prerelease = suffix and not suffix.startswith(("+", ".post", "post"))
    if numeric < MIN_ESPTOOL or numeric == MIN_ESPTOOL and prerelease:
        raise _esptool_failure(f"esptool {major}.{minor}.{patch or '0'}{suffix} is unsupported; "
                               f"require >=5.0.0. Python: {python}", python)
    return f"{major}.{minor}.{patch or '0'}{suffix}"


def repair_esptool(python, *, run=None, on_line=None):
    """Ensure esptool>=5 in this interpreter, never opening a device.

    Call only for an explicit repair or the existing launcher/setup dependency
    step. A successful pip exit is not enough: the same preflight runs again.
    Firmware installation remains a separate user-confirmed operation.
    """
    run = run or subprocess.run
    python = str(python)
    try:
        return check_esptool(python, run=run)
    except FlashError:
        pass
    if on_line:
        on_line("Repairing esptool>=5.0.0 in the game Python: " + python)
    command = _esptool_install_args(python)
    try:
        result = run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired as exc:
        raise _esptool_failure("esptool installation timed out (180 s); "
                               "no firmware operation was started", python) from exc
    except PermissionError as exc:
        raise _esptool_failure("Permission denied repairing the game Python environment: " +
                               _text(exc, 300), python) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise _esptool_failure("Cannot run pip with the game Python: " + _text(exc, 300), python) from exc
    if result.returncode:
        detail = getattr(result, "stderr", "") or getattr(result, "stdout", "") or "no diagnostic output"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise _esptool_failure("esptool installation failed (exit " + str(result.returncode) +
                               "): " + _text(str(detail)[-600:], 600), python)
    try:
        version = check_esptool(python, run=run)
    except FlashError as exc:
        raise FlashError("pip completed but esptool is still unusable. " + str(exc)) from exc
    if on_line:
        on_line("esptool " + version + " verified in the game Python; no firmware was flashed")
    return version


def select_firmware_asset(release, board):
    """Bind the archive to the selected board, never to asset-list ordering."""
    if board not in ("xiao", "wroom"):
        raise FlashError("Unknown ESP32 flash board profile")
    tag = release.get("tag_name", "") if isinstance(release, dict) else ""
    if not isinstance(tag, str) or not re.fullmatch(
            r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9][A-Za-z0-9.-]*)?", tag):
        raise FlashError("Firmware release has an invalid version tag")
    version = tag.lstrip("v")
    suffix = "-xiao" if board == "xiao" else ""
    filename = f"projectZerobyLOCOSP{suffix}-{version}.zip"
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise FlashError("Firmware release assets are invalid")
    matches = [asset for asset in assets if isinstance(asset, dict)
               and isinstance(asset.get("name"), str)
               and asset["name"].casefold() == filename.casefold()]
    if len(matches) > 1:
        raise FlashError(f"Firmware release has ambiguous {board} archives")
    if matches:
        url = matches[0].get("browser_download_url")
        if not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).netloc:
            raise FlashError("Firmware archive download URL is invalid")
        return url
    # Missing metadata cannot authorize a guessed stock firmware download.
    raise FlashError(f"Firmware release has no exact {board} archive; no fallback download is permitted")


def extract_firmware_archive(zip_path, destination, required_files):
    """Validate all current images, including CRC, before writing fresh files."""
    required = tuple(required_files)
    if not required or len(set(required)) != len(required) or any(
            not isinstance(name, str) or PurePosixPath(name).name != name
            or name in ("", ".", "..") or "\\" in name or ":" in name for name in required):
        raise FlashError("Invalid required firmware filenames")
    destination = Path(destination)
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            members = {}
            for info in archive.infolist():
                name = info.filename
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                    raise FlashError("Unsafe path in firmware archive")
                if name in members:
                    raise FlashError("Duplicate filename in firmware archive")
                members[name] = info
            missing = [name for name in required if name not in members]
            if missing:
                raise FlashError("Current firmware archive is missing: " + ", ".join(missing))
            for name in required:
                info = members[name]
                mode = (info.external_attr >> 16) & 0o170000
                if info.is_dir() or mode not in (0, stat.S_IFREG) or info.flag_bits & 1:
                    raise FlashError(f"Firmware image is not a regular readable file: {name}")
                if not 0 < info.file_size <= MAX_FIRMWARE_FILE_BYTES:
                    raise FlashError(f"Firmware image has an invalid size: {name}")
                # Reading to EOF verifies the current archive's CRC. Do this
                # for every required image before any destination file exists.
                count = 0
                with archive.open(info) as source:
                    for block in iter(lambda: source.read(65536), b""):
                        count += len(block)
                        if count > MAX_FIRMWARE_FILE_BYTES:
                            raise FlashError(f"Firmware image exceeds size limit: {name}")
                if count != info.file_size:
                    raise FlashError(f"Incomplete firmware image: {name}")
            if any((destination / name).exists() for name in required):
                raise FlashError("Firmware extraction needs a fresh attempt directory")
            destination.mkdir(parents=True, exist_ok=True)
            for name in required:
                with archive.open(members[name]) as source, (destination / name).open("xb") as target:
                    for block in iter(lambda: source.read(65536), b""):
                        target.write(block)
    except FlashError:
        raise
    except PermissionError as exc:
        raise FlashError(f"Permission denied preparing firmware in {destination}; "
                         "check directory access permissions.") from exc
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise FlashError(f"Cannot prepare current firmware archive: {_text(exc)}") from exc


def _stop_child(process, timeout):
    """Bound each stop attempt; return True only after observed process exit."""
    try:
        if process.poll() is not None:
            return True
    except Exception:
        pass
    for method in ("terminate", "kill"):
        try:
            getattr(process, method)()
        except Exception:
            pass
        try:
            if process.wait(timeout=timeout) is not None:
                return True
        except Exception:
            pass
        try:
            if process.poll() is not None:
                return True
        except Exception:
            pass
    return False


def run_esptool(command, on_line, *, on_process=None, popen=None,
                total_timeout=300, idle_timeout=60, stop_timeout=5,
                monotonic=None, poll_interval=.05, drain_timeout=1):
    """Supervise output and exit without allowing a blocked pipe to own time.

    A failure retaining ``error.process`` means exit could not be confirmed.
    The caller must continue reserving that child's serial port and files.
    """
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
           for value in (total_timeout, idle_timeout, stop_timeout, poll_interval, drain_timeout)):
        raise FlashError("Invalid esptool supervision timeout")
    popen = popen or subprocess.Popen
    monotonic = monotonic or time.monotonic
    process = None
    reader = None
    emit = decoder = None
    flushed = False
    stopped = threading.Event()
    reader_done = threading.Event()
    events = queue.Queue(maxsize=128)

    def publish(kind, value):
        while not stopped.is_set():
            try:
                events.put((kind, value), timeout=.05)
                return
            except queue.Full:
                continue

    def read_output():
        try:
            read = getattr(process.stdout, "read1", None) or process.stdout.read
            while not stopped.is_set():
                data = read(4096)
                if not data:
                    break
                publish("data", data)
        except Exception as exc:
            publish("error", exc)
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
            reader_done.set()

    try:
        process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=False, bufsize=0)
        if on_process is not None:
            on_process(process)
        if process.stdout is None:
            raise FlashError("esptool output pipe is unavailable")
        reader = threading.Thread(target=read_output, name="wdg-esptool-output", daemon=True)
        reader.start()
        started = last_output = monotonic()
        exited_at = None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        flushed = False

        def emit(text, *, final=False):
            nonlocal pending
            pending += text
            while True:
                separator = re.search(r"[\r\n]", pending)
                if separator is None:
                    break
                value, pending = pending[:separator.start()], pending[separator.end():]
                if value.strip():
                    on_line(_text(value, 4096))
            while len(pending) >= 4096:
                on_line(_text(pending[:4096], 4096))
                pending = pending[4096:]
            if final and pending.strip():
                on_line(_text(pending, 4096))
                pending = ""

        while True:
            # A noisy process cannot postpone timeout checks by filling the
            # queue forever; each drain has a fixed maximum batch size.
            for _ in range(64):
                try:
                    kind, value = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "error":
                    raise FlashError("Cannot read esptool output: " + _text(value))
                last_output = monotonic()
                emit(decoder.decode(value) if isinstance(value, bytes) else value)
            now = monotonic()
            if reader_done.is_set() and events.empty() and not flushed:
                emit(decoder.decode(b"", final=True), final=True)
                flushed = True
            returncode = process.poll()
            if returncode is not None:
                if reader_done.is_set() and events.empty():
                    if returncode:
                        raise FlashError(f"esptool failed with exit code {returncode}")
                    return
                if exited_at is None:
                    exited_at = now
                elif now - exited_at >= drain_timeout:
                    raise FlashError("esptool exited but its output pipe did not close")
            elif now - started >= total_timeout:
                raise FlashError(f"esptool exceeded the {total_timeout:g}-second time limit")
            elif now - last_output >= idle_timeout:
                raise FlashError(f"esptool produced no output for {idle_timeout:g} seconds")
            if events.empty():
                stopped.wait(poll_interval)
    except BaseException as exc:
        confirmed = process is None or _stop_child(process, stop_timeout)
        # A failed connect often ends in an unterminated "Connecting...".
        # Keep that tail and the child's final stdout/stderr after termination,
        # without trusting EOF from an inherited or stuck pipe. A retained
        # live child gets no extra waiting and still owns its serial port.
        if emit is not None:
            try:
                deadline = time.monotonic() + (drain_timeout if confirmed else 0)
                for _ in range(128):
                    remaining = max(0, deadline - time.monotonic())
                    try:
                        kind, value = events.get(timeout=min(.01, remaining))
                    except queue.Empty:
                        if reader_done.is_set() or remaining <= 0:
                            break
                        continue
                    if kind == "data":
                        emit(decoder.decode(value) if isinstance(value, bytes) else value)
                    elif kind == "error":
                        on_line("Cannot read final esptool output: " + _text(value))
                if not flushed:
                    emit(decoder.decode(b"", final=True), final=True)
            except BaseException:
                # Diagnostic callbacks/draining must not replace the actual
                # install failure or discard an unconfirmed process handle.
                pass
        if isinstance(exc, FlashError):
            message = str(exc)
        elif isinstance(exc, PermissionError):
            message = "Permission denied starting/accessing esptool: " + _text(exc)
        else:
            message = "Cannot run esptool: " + _text(exc)
        if not confirmed:
            message += "; process exit is unconfirmed: keep the serial port reserved"
        raise FlashError(message, process=None if confirmed else process) from exc
    finally:
        stopped.set()
        if reader is not None:
            reader.join(timeout=min(stop_timeout, .2))
        # TextIO.close can itself block behind another thread's readline.
        # Only close once the reader is out of the stream, never from under it.
        if process is not None and reader_done.is_set():
            try:
                process.stdout.close()
            except Exception:
                pass


def reset_c5_usb_to_app(port, *, serial_factory=None, hard_reset=None):
    """Request the official native USB application reset without ROM sync.

    The installer must bind the physical C5 identity and exclusively own its
    port before invoking this helper in its supervised child process. A reset
    request is not proof that the application booted or answered afterwards.
    """
    if (not isinstance(port, str) or not port.strip() or "://" in port
            or any(ord(char) < 32 or ord(char) == 127 for char in port)):
        raise FlashError("Native USB reset requires the bound local serial port")
    connection = None
    failure = None
    try:
        if serial_factory is None:
            from serial import Serial
            serial_factory = Serial
        if hard_reset is None:
            from esptool.reset import HardReset
            hard_reset = HardReset
        options = dict(port=None, baudrate=115200, timeout=1, write_timeout=2)
        if os.name == "posix":
            options["exclusive"] = True
        connection = serial_factory(**options)
        # Configure the modem-line state while closed: opening with both
        # asserted can request a different USB Serial/JTAG reset transition.
        connection.dtr = False
        connection.rts = False
        connection.port = port
        connection.open()
        hard_reset(connection, uses_usb=True)()
    except Exception as exc:
        failure = exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                if failure is None:
                    failure = exc
    if failure is not None:
        raise FlashError("Native USB application reset request failed: " +
                         _text(failure)) from failure


def _dependency_main(argv=None):
    """Existing helper entry point; no branch can flash or repair then flash."""
    import argparse
    parser = argparse.ArgumentParser(description="Game Python tools and supervised native USB reset")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-esptool", action="store_true")
    action.add_argument("--repair-esptool", action="store_true")
    action.add_argument("--reset-c5-usb-to-app", metavar="PORT")
    args = parser.parse_args(argv)
    try:
        if args.reset_c5_usb_to_app is not None:
            reset_c5_usb_to_app(args.reset_c5_usb_to_app)
            print("Native USB application reset requested; firmware boot is not confirmed")
            return 0
        version = (repair_esptool(sys.executable, on_line=print) if args.repair_esptool
                   else check_esptool(sys.executable))
        print("esptool " + version + " ready in " + sys.executable)
        return 0
    except FlashError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_dependency_main())
