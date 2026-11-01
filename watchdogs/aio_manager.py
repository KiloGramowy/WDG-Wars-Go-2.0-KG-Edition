"""AIO v2 module control — direct GPIO via pinctrl.

Bypasses aiov2_ctl entirely to avoid subprocess chain issues
(pinctrl → sudo → systemctl → meshtasticd) that crash urwid.
Uses ``pinctrl set/get`` directly for instant GPIO toggling.

``aiov2_ctl`` is still used for:
- ``is_installed()`` — detect if AIO v2 hardware is present
- ``install()``      — first-time setup from GitHub
"""

import logging
import re
import shutil
import subprocess
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

FEATURES = ("gps", "lora", "sdr", "usb")

# GPIO pin mapping from AIO v2 board (matches aiov2_ctl GPIO_MAP)
GPIO_MAP = {
    "gps":  27,
    "lora": 16,
    "sdr":  7,
    "usb":  23,
}


def _pinctrl_set(pin: int, high: bool) -> bool:
    """Set a GPIO pin via ``pinctrl set <pin> op dh|dl``."""
    state = "dh" if high else "dl"
    try:
        result = subprocess.run(
            ["pinctrl", "set", str(pin), "op", state],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning("pinctrl set %s failed: %s", pin, exc)
        return False


def _pinctrl_get(pin: int) -> bool:
    """Read a GPIO pin state via ``pinctrl get <pin>``.

    Returns True if pin is HIGH (``"hi"`` in output).
    """
    try:
        result = subprocess.run(
            ["pinctrl", "get", str(pin)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return "hi" in (result.stdout or "")
    except Exception:
        return False


def _pinctrl_read_confirmed(pin: int) -> Optional[bool]:
    """Read the actual pin level; a failed/ambiguous read is not evidence of OFF."""
    try:
        result = subprocess.run(
            ["pinctrl", "get", str(pin)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            log.warning("Could not confirm GPIO%d state (exit %s)", pin, result.returncode)
            return None
        levels = re.findall(
            rf"^\s*{pin}:\s*[^|\r\n]*\|\s*(hi|lo)\b",
            result.stdout or "", re.MULTILINE,
        )
        if len(levels) == 1:
            return levels[0] == "hi"
        log.warning("Could not confirm GPIO%d state from pinctrl output", pin)
    except Exception as exc:
        log.warning("Could not confirm GPIO%d state: %s", pin, exc)
    return None


class AioManager:
    """Interface to HackerGadgets AIO v2 — direct GPIO control."""

    @staticmethod
    def is_installed() -> bool:
        """Check if AIO v2 is available (pinctrl present)."""
        return shutil.which("pinctrl") is not None

    @staticmethod
    def get_status() -> Optional[dict]:
        """Read all GPIO pin states directly.

        Returns dict like ``{"gps": True, "lora": False, ...}``
        or *None* if pinctrl is not available.
        """
        if not shutil.which("pinctrl"):
            return None
        try:
            status = {}
            for feat, pin in GPIO_MAP.items():
                status[feat] = _pinctrl_get(pin)
            return status
        except Exception as exc:
            log.warning("GPIO status read failed: %s", exc)
            return None

    @staticmethod
    def get_feature_state(feature: str) -> Optional[bool]:
        """Return a confirmed actual level, or None when it cannot be read."""
        if feature not in GPIO_MAP:
            return None
        return _pinctrl_read_confirmed(GPIO_MAP[feature])

    @staticmethod
    def toggle(feature: str, on: bool) -> bool:
        """Toggle an AIO feature via direct GPIO. Returns True on success."""
        if feature not in GPIO_MAP:
            return False
        pin = GPIO_MAP[feature]
        ok = _pinctrl_set(pin, on)
        if ok:
            log.info("GPIO%d (%s) → %s", pin, feature, "HIGH" if on else "LOW")
        else:
            log.warning("GPIO%d (%s) toggle failed", pin, feature)
        return ok

    @staticmethod
    def install(callback: Callable[[str, str], None]) -> None:
        """Install aiov2_ctl from GitHub in a background thread.

        Uses the official install method from
        https://github.com/hackergadgets/aiov2_ctl/ — git clone +
        ``sudo python3 ./aiov2_ctl.py --install`` (NOT pip).

        callback(line, attr) is called for each output line.
        """
        import os
        import tempfile

        def _run():
            callback("Installing aiov2_ctl from GitHub...", "attack_active")
            try:
                tmp = tempfile.mkdtemp(prefix="aiov2_ctl_")
                clone_dir = os.path.join(tmp, "aiov2_ctl")
                callback(f"  cloning into {clone_dir}", "dim")

                # Step 1: git clone
                proc = subprocess.Popen(
                    ["git", "clone", "--depth=1",
                     "https://github.com/hackergadgets/aiov2_ctl.git",
                     clone_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        callback(f"  {line}", "dim")
                proc.wait()
                if proc.returncode != 0:
                    callback(f"git clone failed (exit {proc.returncode})", "error")
                    return

                # Step 2: sudo python3 ./aiov2_ctl.py --install
                callback("  running installer (sudo required)...", "dim")
                proc = subprocess.Popen(
                    ["sudo", "python3", "./aiov2_ctl.py", "--install"],
                    cwd=clone_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        callback(f"  {line}", "dim")
                proc.wait()
                if proc.returncode == 0:
                    callback("aiov2_ctl installed successfully!", "success")
                else:
                    callback(f"Install failed (exit code {proc.returncode})", "error")
            except FileNotFoundError as exc:
                callback(f"Install error: {exc} (is git installed?)", "error")
            except Exception as exc:
                callback(f"Install error: {exc}", "error")

        t = threading.Thread(target=_run, daemon=True)
        t.start()


class AioPowerSession:
    """Track power enabled by one game session without claiming existing rails.

    Only a confirmed OFF -> requested ON transition is owned. Record that
    claim before writing hardware: a failed/interrupted write may have applied.
    A preexisting ON or unknown state never grants ownership through toggle().
    claim() lets the game explicitly adopt a confirmed ON rail after acquiring
    its device. Call
    begin_shutdown() before stopping workers, then shutdown() after they have
    released their devices. No services or other processes are stopped here.
    """

    _SHUTDOWN_ORDER = ("lora", "sdr", "gps", "usb")

    def __init__(self, controller=AioManager):
        self._controller = controller
        self._lock = threading.RLock()
        self._owned: set[str] = set()
        self._closing = False

    def _read_state(self, feature: str) -> Optional[bool]:
        try:
            state = self._controller.get_feature_state(feature)
        except Exception as exc:
            log.warning("Could not confirm %s power state: %s", feature, exc)
            return None
        return state if type(state) is bool else None

    def _write(self, feature: str, on: bool) -> bool:
        try:
            return self._controller.toggle(feature, on) is True
        except Exception as exc:
            log.warning("Could not set %s power %s: %s", feature, on, exc)
            return False

    def toggle(self, feature: str, on: bool) -> bool:
        """Write power, preserving ownership until OFF is actually confirmed.

        Once shutdown begins, new ON requests are rejected. Explicit OFF
        requests remain available, but drop a claim only after write success
        and a confirmed LOW read. This does not change AioManager.toggle().
        """
        if feature not in GPIO_MAP or type(on) is not bool:
            return False
        with self._lock:
            if on and self._closing:
                log.warning("Ignoring %s power ON during shutdown", feature)
                return False
            if on and self._read_state(feature) is False:
                self._owned.add(feature)
            if not self._write(feature, on):
                log.warning("%s power %s write failed; ownership retained", feature, on)
                return False
            if not on:
                if self._read_state(feature) is not False:
                    log.warning("%s power OFF could not be confirmed; ownership retained", feature)
                    return False
                self._owned.discard(feature)
            return True

    def claim(self, feature: str) -> bool:
        """Adopt a confirmed ON rail after explicit device acquisition, without writing it."""
        if feature not in GPIO_MAP:
            return False
        with self._lock:
            if self._closing or self._read_state(feature) is not True:
                return False
            self._owned.add(feature)
            return True

    def begin_shutdown(self) -> None:
        """Prevent new asynchronous ON requests before workers are stopped."""
        with self._lock:
            self._closing = True

    def shutdown(self, blocked_features=()) -> dict[str, bool]:
        """Turn off owned rails, USB last, returning {feature: confirmed_off}.

        Only owned features appear in the result. A blocked, failed, or
        unverified feature returns False and remains owned for a later retry.
        The caller supplies blocks for live workers, flashing, or other users;
        this helper cannot establish their device ownership itself.
        """
        with self._lock:
            self._closing = True
            blocked = set(blocked_features)
            result = {}
            for feature in self._SHUTDOWN_ORDER:
                if feature not in self._owned:
                    continue
                if feature in blocked:
                    log.warning("Leaving owned %s power ON: shutdown is blocked", feature)
                    result[feature] = False
                    continue
                result[feature] = self.toggle(feature, False)
            return result
