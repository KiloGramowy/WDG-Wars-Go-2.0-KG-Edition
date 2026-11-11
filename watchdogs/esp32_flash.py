"""Small, device-independent helpers for the existing ESP32 flash wizard.

These helpers do not open a serial port, switch GPIO, install packages or retry
a firmware write. The caller owns the port and the lifetime of its attempt
directory, including the exceptional case of an unkillable child process.
"""
import codecs
import math
from pathlib import Path, PurePosixPath
import queue
import re
import stat
import subprocess
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


def check_esptool(python, *, run=None):
    """Probe the exact game interpreter before any download/device operation."""
    run = run or subprocess.run
    try:
        result = run([python, "-m", "esptool", "version"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FlashError(f"esptool check timed out (10 s). Python: {python}") from exc
    except PermissionError as exc:
        raise FlashError(f"Permission denied running the game Python: {python}. "
                         "Check execute/access permissions; no device was changed.") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise FlashError(f"Cannot start the game Python: {python}. {_text(exc)}") from exc

    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    detail = _text(stderr or stdout)
    if result.returncode != 0:
        if re.search(r"No module named [\"']?esptool[\"']?(?:\s|$)", str(stderr)):
            reason = "esptool is unavailable in the game Python"
        elif "permission denied" in detail.lower() or "permissionerror" in detail.lower():
            reason = "esptool cannot access its Python environment (permission denied)"
        else:
            reason = "esptool cannot run in the game Python"
        raise FlashError(f"{reason}. Python: {python}. {detail}")

    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    versions = re.findall(
        r"(?im)^\s*(?:esptool(?:\.py)?\s+)?v?"
        r"(\d+)\.(\d+)(?:\.(\d+))?([a-z][\w.-]*|[-+][\w.-]+|\.[a-z][\w.-]*)?\s*$",
        stdout[:16384])
    if not versions:
        raise FlashError(f"Cannot determine the esptool version; require >=5.0.0. "
                         f"Python: {python}. {_text(stdout)}")
    major, minor, patch, suffix = versions[-1]
    numeric = (int(major), int(minor), int(patch or 0))
    prerelease = suffix and not suffix.startswith(("+", ".post", "post"))
    if numeric < MIN_ESPTOOL or numeric == MIN_ESPTOOL and prerelease:
        raise FlashError(f"esptool {major}.{minor}.{patch or '0'}{suffix} is unsupported; "
                         f"require >=5.0.0. Python: {python}")
    return f"{major}.{minor}.{patch or '0'}{suffix}"


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
    # Preserve the official release-name fallback; a missing asset/download is
    # still a visible preparation error and never authorizes using old cache.
    return (f"https://github.com/LOCOSP/projectZero/releases/download/{tag}/"
            f"{filename}")


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
