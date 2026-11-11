"""Watch Dogs Go Wars Sync — upload wardriving loot to community server.

Uploads WiFi/BLE networks, ADS-B aircraft, and MeshCore/Meshtastic nodes from
loot sessions to a wardrive server via HTTPS API with HMAC-SHA256
signed payloads. Supports auth push (game as 2FA authenticator).

Setup:
  1. Register on the wardrive server to get an API key
  2. Add to secrets.conf:
     WARDRIVE_API_URL=https://your-server.example/api/upload/
     WARDRIVE_API_KEY=<64-char-hex-key>
  3. Or set both from the plugin overlay (PLUGINS > Wars Sync > Set URL / Set API Key)
"""

import base64
from collections import Counter
import csv
from contextlib import nullcontext
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from plugins.plugin_base import PluginBase, PluginMenuItem

try:
    from watchdogs import __version__ as _GAME_VERSION
except Exception:
    _GAME_VERSION = "0.0.0"

# Cloudflare's Bot Fight Mode on wdgwars.pl drops requests that look like
# scraper traffic (default urllib/requests UA, empty UA) with HTTP 403 +
# error code 1010. Server-side the wdgwars admin asked for a UA that:
#   - starts with "WatchDogsGo/<version>" so access logs can identify us
#   - carries a platform hint so traffic from bench/dev can be told apart
# Anything except "python-requests/*" or empty passes Cloudflare; the
# structured format is purely for their telemetry.
import platform as _plat
import sys as _sys
_PLATFORM = "uConsole" if _plat.system() == "Linux" else _plat.system()
_PYVER = f"{_sys.version_info.major}.{_sys.version_info.minor}"
USER_AGENT = (f"WatchDogsGo/{_GAME_VERSION} "
              f"({_PLATFORM}; Python/{_PYVER})")


def _open(req: Request, timeout: float = 30):
    """Thin wrapper around urlopen that sets our required headers before
    every call. Centralises User-Agent + Accept so future endpoints can't
    forget them and get swallowed by Cloudflare's WAF."""
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    return urlopen(req, timeout=timeout)


log = logging.getLogger(__name__)


class UploadReadError(OSError):
    """A log was not read completely; no partial payload may be acknowledged."""

# Auth mode mapping from WiGLE CSV to simple format
# Badge ID mapping: game badge → server badge
BADGE_GAME_TO_SERVER = {
    "wardriver": "wardriver",
    "handshake_hunter": "handshake_hunter",
    "evil_twin": "evil_twin",
    "meshcore": "mesh_first",
    "wpasec_uploader": "wpa_sec_user",
    "flipper": "flipper_user",
    "skywatch": "skywatch",
    "iot_hunter": "iot_hunter",
}
# Reverse mapping: server badge → game badge
BADGE_SERVER_TO_GAME = {v: k for k, v in BADGE_GAME_TO_SERVER.items()}

AUTH_MAP = {
    "[WPA2-PSK]": "WPA2", "[WPA2]": "WPA2", "[WPA2-EAP]": "WPA2-EAP",
    "[WPA-PSK]": "WPA", "[WPA]": "WPA",
    "[WPA3-SAE]": "WPA3", "[SAE]": "WPA3",
    "[ESS]": "OPEN", "[OPEN]": "OPEN", "": "OPEN",
    "[WEP]": "WEP",
}


def _map_auth(wigle_auth: str) -> str:
    """Convert WiGLE auth string like '[WPA2-PSK]' to simple 'WPA2'."""
    if not wigle_auth:
        return "OPEN"
    result = AUTH_MAP.get(wigle_auth)
    if result:
        return result
    a = wigle_auth.upper()
    if "WPA3" in a or "SAE" in a:
        return "WPA3"
    if "WPA2" in a:
        return "WPA2"
    if "WPA" in a:
        return "WPA"
    if "WEP" in a:
        return "WEP"
    if "OPEN" in a or "ESS" in a:
        return "OPEN"
    return wigle_auth.strip("[]")


DEFAULT_API_URL = "https://wdgwars.pl/api/upload/"
UPLOAD_BATCH_RECORDS = 10000
UPLOAD_BATCH_BYTES = 6 * 1024 * 1024
UPLOAD_POST_INTERVAL = 6.5
_UPLOAD_GROUPS = ("networks", "aircraft", "meshcore_nodes")

# Verified in the authenticated official help, 2026-10-29:
# https://www.wdgwars.pl/help#mesh-upload
# Both protocols use meshcore_nodes; network is the authoritative namespace.


def _upload_receipt(result: dict, submitted: dict) -> tuple[bool, str, int]:
    """Check application acknowledgement, separately from HTTP success.

    Missing counters are unknown, not zero. A short acknowledgement or any
    rejected record keeps the session pending. Existing response field names
    are retained; no inferred per-protocol server credit is manufactured.
    """
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False, "server did not acknowledge upload", 0
    groups = (
        ("networks", "nets", ("imported",), ("updated", "duplicates"), "rejected"),
        ("aircraft", "ac", ("aircraft_imported",), ("aircraft_already_seen",), "aircraft_rejected"),
        ("meshcore_nodes", "mesh", ("meshcore_imported",), ("meshcore_already_seen", "meshcore_merged"), "meshcore_rejected"),
    )
    complete = True
    info = []
    imported = 0
    for key, label, accepted_keys, known_keys, rejected_key in groups:
        count = len(submitted.get(key, []))
        if not count:
            continue
        fields = accepted_keys + known_keys + (rejected_key,)
        if key == "networks":
            fields += ("cooldown",)
        for field in fields:
            if field in result and (type(result[field]) is not int or result[field] < 0):
                return False, f"invalid {field} counter", 0
        accepted = sum(result.get(k, 0) for k in accepted_keys)
        known = sum(result.get(k, 0) for k in known_keys)
        # The server handles these observations without awarding new credit.
        # They are separate from imported/updated/duplicate network records.
        cooldown = result.get("cooldown", 0) if key == "networks" else 0
        rejected = result.get(rejected_key)
        reasons = {}
        if key == "meshcore_nodes":
            for field in ("meshcore_yours_known", "meshcore_owned_by_others"):
                if field in result and (type(result[field]) is not int or result[field] < 0):
                    return False, f"invalid {field} counter", 0
            split_known = sum(result.get(field, 0) for field in
                              ("meshcore_yours_known", "meshcore_owned_by_others"))
            if "meshcore_already_seen" not in result:
                if result.get("meshcore_merged", 0) and split_known:
                    return False, "missing mesh aggregate counter", 0
                known += split_known
            # Ownership describes already-seen AND merged records. A merged
            # node may still belong to us or another player; it is not a
            # second counted record. Verified against the real 103-node reply.
            elif split_known > known:
                return False, "inconsistent mesh ownership counters", 0
            if "meshcore_reject_reasons" in result:
                reasons = result["meshcore_reject_reasons"]
                if not isinstance(reasons, dict) or any(type(v) is not int or v < 0 for v in reasons.values()):
                    return False, "invalid meshcore_reject_reasons counters", 0
                reason_total = sum(reasons.values())
                if rejected is None:
                    rejected = reason_total
                elif reason_total > rejected:
                    return False, "inconsistent mesh rejection counters", 0
        accounted = accepted + known + cooldown + (rejected or 0)
        unknown = max(0, count - accounted)
        # When all submitted records are handled, rejection is
        # necessarily zero. Otherwise an omitted rejection counter stays ?.
        rejected_text = str(rejected) if rejected is not None else ("0" if unknown == 0 else "?")
        cooldown_text = f", cooldown {cooldown}" if key == "networks" else ""
        info.append(f"{label}: +{accepted}, known {known}{cooldown_text}, rejected {rejected_text}, unknown {unknown}")
        if key == "meshcore_nodes":
            if "meshcore_yours_known" in result or "meshcore_owned_by_others" in result:
                info.append(f"mesh known: yours {result.get('meshcore_yours_known', '?')}, "
                            f"other players {result.get('meshcore_owned_by_others', '?')}")
            if any(reasons.values()):
                info.append("mesh rejection reasons: " + ", ".join(f"{k}={v}" for k, v in reasons.items() if v))
        complete &= accounted == count and not rejected and not unknown
        if key == "networks":
            imported = accepted
    return bool(complete), "; ".join(info), imported


def _default_endpoint() -> str:
    """Resolve default server endpoint. Override in secrets.conf with
    WARDRIVE_API_URL=... if you run your own wardrive server."""
    return DEFAULT_API_URL


def _load_secrets_conf() -> dict:
    """Load key=value pairs from secrets.conf."""
    result = {}
    path = Path(__file__).parent.parent / "secrets.conf"
    if path.is_file():
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        except Exception:
            pass
    return result


class WardriveUpload(PluginBase):
    NAME = "Watch Dogs Go Wars Sync"
    VERSION = "2.1"
    AUTHOR = "LOCOSP"

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._uploading = False
        self._log: list[tuple[str, int]] = []
        self._overlay_active = False
        self._menu_sel = 0
        self._uploaded_sessions: set[str] = set()
        self._uploaded_meshtastic_sessions: set[str] = set()
        self._upload_receipts: dict[str, dict] = {}
        self._upload_coverage: dict[str, dict] = {}
        self._upload_keep_exports: set[str] = set()
        self._next_upload_post = 0.0
        self._receipt_generation = 0
        self._upload_log_checks: dict[str, tuple] = {}
        self._pending_log_checks: dict[str, tuple] = {}
        self._upload_wait_checks: dict[str, tuple] = {}
        self._next_cleanup = 0.0
        self._cleanup_removed = 0
        self._mesh_gps_wait = 0
        self._mesh_parse_stats: dict[str, dict] = {}
        self._api_key = ""
        self._api_url = ""
        self._key_input = ""
        self._url_input = ""
        self._entering_key = False
        self._entering_url = False
        self._pending_browser = False
        self._pending_rows: list[dict] = []
        self._pending_sel = 0
        self._pending_scroll = 0
        self._pending_page_rows = 17
        self._pending_delete = None
        self._pending_message = ""
        self._pending_from_sync = False
        self._pending_open_frame = -1
        self._pending_confirm_frame = -1
        self._retention_settings_open = False
        self._retention_sel = 0
        self._retention_custom = False
        self._retention_input = ""
        self._retention_open_frame = -1
        self._retention_message = ""
        # Auth push — PIN display
        self._auth_pin = ""
        self._auth_pin_expiry = 0
        self._auth_dismissed_token = ""
        self._auth_current_token = ""
        self._auth_polling = False
        self._auth_poll_thread = None
        # Identity from /api/me
        self._username = ""
        self._user_gang = ""
        self._user_stats: dict = {}
        # One coalescing local worker owns expensive Sync preparation. UI
        # callbacks only publish requests/read immutable snapshot references.
        self._sync_lock = threading.RLock()
        self._sync_wakeup = threading.Event()
        self._sync_stop = threading.Event()
        self._sync_idle = threading.Event()
        self._sync_idle.set()
        self._sync_worker_thread = None
        self._sync_refresh_requested = False
        self._sync_notify_ready = False
        self._sync_upload_requested = None
        self._sync_upload_pending = False
        self._sync_snapshot = dict(state="idle", identity=None, sessions=(),
                                   waiting_for_gps=0, error="")
        self._background_lock = threading.Lock()
        self._background_threads = set()
        self._load_state()

    def menu_items(self) -> list[PluginMenuItem]:
        return [
            PluginMenuItem("w", "Watch Dogs Go Wars Sync", "open_overlay"),
            PluginMenuItem("l", "Pending Upload Logs", "open_pending_logs"),
        ]

    def on_load(self, app) -> None:
        super().on_load(app)
        self._start_auth_poll()
        # If a key is already configured, fetch identity in background
        # so badges sync and LoRa name updates without user interaction.
        if self._api_key and self._api_url:
            self._start_background_worker(self._validate_and_apply_user,
                                          name="wdg-sync-identity")

    def _callbacks_allowed(self):
        """A completed HTTP request cannot revive an unloaded game."""
        stop = getattr(self, "_sync_stop", None)
        return (not (stop is not None and stop.is_set())
                and not getattr(getattr(self, "app", None), "_shutting_down", False))

    def _start_background_worker(self, target, *, name):
        """Retain one-shot workers so shutdown can report real quiescence."""
        with self._background_lock:
            if not self._callbacks_allowed():
                return False

            def run():
                try:
                    if self._callbacks_allowed():
                        target()
                finally:
                    with self._background_lock:
                        self._background_threads.discard(threading.current_thread())

            thread = threading.Thread(target=run, name=name, daemon=True)
            self._background_threads.add(thread)
            try:
                thread.start()
            except BaseException:
                self._background_threads.discard(thread)
                raise
            return True

    def on_unload(self) -> bool:
        self._sync_stop.set()
        self._sync_wakeup.set()
        with self._background_lock:
            self._auth_polling = False
            threads = list(self._background_threads)
        threads.extend((self._sync_worker_thread, self._auth_poll_thread))
        threads = list(dict.fromkeys(thread for thread in threads if thread is not None))
        # A blocked filesystem/HTTP call cannot be cancelled safely. Report
        # incomplete shutdown to the existing update/exit guard, never wait
        # indefinitely or claim that an active worker is quiescent.
        deadline = time.monotonic() + 1.0
        first = True
        for thread in threads:
            if thread is not threading.current_thread() and thread.is_alive():
                timeout = 1.0 if first else max(0.0, deadline - time.monotonic())
                first = False
                thread.join(timeout=timeout)
        return not any(thread.is_alive() for thread in threads)

    def msg(self, text: str, color: int = 7) -> None:
        if self._callbacks_allowed():
            super().msg(text, color)

    def _check_level(self) -> bool:
        """Check if player meets level requirement. Returns True if OK."""
        if not self.app:
            return True
        if self.app.level < 6:
            lvl = self.app.level
            title = self.app.level_title
            self.msg(f"[Sync] Locked — reach Lv.6 WARDRIVER to unlock (Lv.{lvl} {title})", 10)
            self.msg("[Sync] Earn XP: scan WiFi/BLE, capture handshakes, wardrive!", 13)
            return False
        return True

    def open_overlay(self):
        if not self._check_level():
            return
        self._overlay_active = True
        self._menu_sel = 0
        self._entering_key = False
        self._entering_url = False
        self._pending_browser = False
        self._pending_delete = None
        if not self._api_key:
            self._log_add("Set your API key to start syncing", 10)
            self._log_add("  Register on the community server to get your key", 13)
        else:
            self._log_add("Connected to server", 11)
        self._log_add("Preparing local sessions...", 13)
        self._request_sync_refresh(notify=True)

    def _sync_identity(self):
        """Cheap identity only: no stat, directory scan, parsing or disk lock."""
        loot = getattr(self.app, "loot", None) if self.app else None
        return (id(loot), str(getattr(loot, "_base", "")), self._api_url,
                self._api_key, self._receipt_generation)

    def _sync_snapshot_view(self):
        snapshot = self._sync_snapshot
        if snapshot["identity"] != self._sync_identity():
            return dict(state="preparing", identity=None, sessions=(),
                        waiting_for_gps=0, error="")
        return snapshot

    def _ensure_sync_worker(self):
        """Called with the short state lock; the worker never holds it for I/O."""
        if self._sync_stop.is_set():
            return False
        thread = self._sync_worker_thread
        if thread is None or not thread.is_alive():
            thread = self._sync_worker_thread = threading.Thread(
                target=self._sync_worker_main, name="WDG Sync preparation", daemon=True)
            try:
                thread.start()
            except (RuntimeError, MemoryError):
                self._sync_worker_thread = None
                self._sync_refresh_requested = self._sync_notify_ready = False
                self._sync_upload_requested = None
                self._sync_upload_pending = False
                self._sync_idle.set()
                self._sync_snapshot = dict(state="error", identity=self._sync_identity(),
                    sessions=(), waiting_for_gps=0, error="Cannot start upload preparation; try again.")
                self._log_add("Cannot start upload preparation; logs kept. Try again.", 8)
                return False
        return True

    def _request_sync_refresh(self, *, notify=False):
        with self._sync_lock:
            if self._sync_stop.is_set():
                return
            self._sync_refresh_requested = True
            self._sync_idle.clear()
            self._sync_notify_ready = self._sync_notify_ready or notify
            if notify or self._sync_snapshot["identity"] != self._sync_identity():
                self._sync_snapshot = dict(state="preparing", identity=self._sync_identity(),
                                           sessions=(), waiting_for_gps=0, error="")
            if self._ensure_sync_worker():
                self._sync_wakeup.set()

    def _publish_sync_snapshot(self, identity, *, sessions=(), error=""):
        with self._sync_lock:
            if identity != self._sync_identity() or self._sync_stop.is_set():
                # A result for an earlier account/loot source must never be
                # displayed, or used as the next account's upload selection.
                if not self._sync_stop.is_set():
                    self._sync_refresh_requested = True
                    self._sync_wakeup.set()
                return False
            self._sync_snapshot = dict(state="error" if error else "ready",
                identity=identity, sessions=tuple(sessions),
                waiting_for_gps=self._mesh_gps_wait, error=error)
            return True

    def _prepare_sync_snapshot(self, *, notify=False):
        identity = self._sync_identity()
        try:
            if self._sync_stop.is_set():
                return None
            # Retention has the same durable receipt/token checks as before,
            # but neither it nor catalog preparation belongs in the UI loop.
            self._retention_tick()
            if self._sync_stop.is_set():
                return None
            sessions = self._pending_sessions()
        except (OSError, MemoryError, ValueError, TypeError, UnicodeError):
            message = "Cannot prepare local uploads; logs kept. Try again."
            if self._publish_sync_snapshot(identity, error=message):
                self._sync_log(message, 8)
            return None
        if not self._publish_sync_snapshot(identity, sessions=sessions):
            return None
        if notify and self._overlay_active:
            self._log_add(f"{len(sessions)} session(s) ready to upload" if sessions
                          else "No pending uploads", 11 if sessions else 13)
            self._show_cleanup_count()
            if self._mesh_gps_wait:
                self._log_add(f"{self._mesh_gps_wait} mesh log(s) waiting for receiver GPS", 10)
        return sessions

    def _sync_worker_main(self):
        while True:
            self._sync_wakeup.wait()
            with self._sync_lock:
                if self._sync_stop.is_set():
                    self._sync_upload_pending = False
                    self._sync_idle.set()
                    return
                upload = self._sync_upload_requested
                self._sync_upload_requested = None
                refresh = self._sync_refresh_requested
                notify = self._sync_notify_ready
                self._sync_refresh_requested = self._sync_notify_ready = False
                self._sync_wakeup.clear()
            try:
                if upload is not None:
                    self._prepare_and_upload(*upload)
                elif refresh:
                    self._prepare_sync_snapshot(notify=notify)
            except Exception:
                # Resource and I/O errors are handled above. An unexpected
                # worker failure must also leave a usable, explicit UI state.
                self._publish_sync_snapshot(self._sync_identity(),
                    error="Cannot prepare local uploads; logs kept. Try again.")
                self._sync_log("Local upload preparation failed; logs kept pending", 8)
            finally:
                with self._sync_lock:
                    if not self._sync_refresh_requested and self._sync_upload_requested is None:
                        self._sync_idle.set()

    def _prepare_and_upload(self, all_sessions, requested_identity):
        prepared = False
        try:
            if requested_identity != self._sync_identity():
                self._sync_log("Upload account/source changed; prepare again. Logs kept.", 10)
                return
            sessions = self._prepare_sync_snapshot()
            if sessions is None:
                return
            if requested_identity != self._sync_identity() or self._sync_stop.is_set():
                self._sync_log("Upload account/source changed; logs kept pending", 10)
                return
            if not all_sessions and sessions:
                sessions = sessions[-1:]
            if not sessions:
                self.cleanup_no_gps_logs()
                self._sync_log("Nothing to upload", 10)
                prepared = True
                return
            if not self._api_url or not self._api_key:
                self.cleanup_no_gps_logs()
                self._sync_log("Set server URL first!" if not self._api_url
                               else "Set API key first!", 8)
                prepared = True
                return
            self._uploading = True
            self._upload_worker(sessions)
            prepared = True
        except (OSError, MemoryError, ValueError, TypeError, UnicodeError):
            self._publish_sync_snapshot(self._sync_identity(),
                error="Cannot prepare local uploads; logs kept. Try again.")
            self._sync_log("Upload preparation failed; logs kept pending", 8)
        finally:
            self._uploading = False
            with self._sync_lock:
                self._sync_upload_pending = False
            if prepared and not self._sync_stop.is_set():
                self._request_sync_refresh()

    def on_update(self) -> None:
        if not self._pending_browser and self.app and time.monotonic() >= self._next_cleanup:
            self._next_cleanup = time.monotonic() + 2.0
            if not self._uploading and not self._sync_upload_pending:
                self._request_sync_refresh()
        if not self._overlay_active:
            return
        import pyxel

        if self._pending_browser:
            # The game dismisses a PIN before returning input to this browser.
            # In particular, a PIN's Y/Esc cannot confirm/cancel file deletion.
            if self._auth_pin_active():
                return
            self._update_pending_logs()
            return

        # Key input mode
        if self._entering_key:
            for c in "0123456789abcdef":
                if pyxel.btnp(getattr(pyxel, f"KEY_{c.upper()}")):
                    self._key_input += c
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._key_input:
                self._key_input = self._key_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and len(self._key_input) >= 16:
                with self._upload_queue_lock():
                    self._api_key = self._key_input
                    self._save_secret("WARDRIVE_API_KEY", self._api_key)
                self._log_add(f"API key saved ({len(self._api_key)} chars)", 11)
                self._entering_key = False
                self._key_input = ""
                self._start_auth_poll()
                # Validate the new key against /api/me and pull identity/badges
                self._log_add("Validating key with server...", 3)
                self._start_background_worker(self._validate_and_apply_user,
                                              name="wdg-sync-identity")
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._entering_key = False
                self._key_input = ""
            return

        # URL input mode
        if self._entering_url:
            for c in "abcdefghijklmnopqrstuvwxyz0123456789.-/:_":
                key = f"KEY_{c.upper()}" if c.isalnum() else {
                    ".": "KEY_PERIOD", "-": "KEY_MINUS", "/": "KEY_SLASH",
                    ":": "KEY_SEMICOLON", "_": "KEY_MINUS",
                }.get(c)
                if key and pyxel.btnp(getattr(pyxel, key, -1)):
                    self._url_input += c
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._url_input:
                self._url_input = self._url_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and self._url_input.startswith("http"):
                with self._upload_queue_lock():
                    self._api_url = self._url_input
                    if not self._api_url.endswith("/"):
                        self._api_url += "/"
                    self._save_secret("WARDRIVE_API_URL", self._api_url)
                self._log_add(f"Server URL saved", 11)
                self._entering_url = False
                self._url_input = ""
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._entering_url = False
                self._url_input = ""
            return

        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return

        items = self._overlay_items()
        if pyxel.btnp(pyxel.KEY_UP) and self._menu_sel > 0:
            self._menu_sel -= 1
        if pyxel.btnp(pyxel.KEY_DOWN):
            self._menu_sel = min(self._menu_sel + 1, len(items) - 1)
        if pyxel.btnp(pyxel.KEY_RETURN) and items:
            action = items[min(self._menu_sel, len(items) - 1)][0]
            self._exec(action)

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel

        if self._pending_browser:
            self._draw_pending_logs(w, h)
            if self._auth_pin_active():
                self._draw_auth_pin(w, h)
            return

        pyxel.rect(0, 0, w, h, 0)

        # Title bar
        pyxel.rect(0, 0, w, 12, 1)
        server_short = self._api_url.replace("https://", "").replace("http://", "").rstrip("/") if self._api_url else "no server"
        pyxel.text(4, 3, f"WARS SYNC — {server_short[:40]}", 3)
        key_txt = f"KEY:{self._api_key[:8]}..." if self._api_key else "NO KEY"
        key_c = 11 if self._api_key else 8
        pyxel.text(w - 80, 3, key_txt, key_c)

        # Key input overlay
        if self._entering_key:
            cy = h // 2
            pyxel.rect(w // 2 - 140, cy - 30, 280, 60, 1)
            pyxel.rectb(w // 2 - 140, cy - 30, 280, 60, 10)
            pyxel.text(w // 2 - 80, cy - 20, "ENTER API KEY (hex)", 10)
            display = self._key_input[-40:] + "_"
            pyxel.text(w // 2 - 80, cy, display, 7)
            pyxel.text(w // 2 - 90, cy + 16,
                       f"[0-9a-f] Type  [ENTER] Confirm  ({len(self._key_input)} chars)", 13)
            return

        # URL input overlay
        if self._entering_url:
            cy = h // 2
            pyxel.rect(w // 2 - 160, cy - 30, 320, 60, 1)
            pyxel.rectb(w // 2 - 160, cy - 30, 320, 60, 10)
            pyxel.text(w // 2 - 80, cy - 20, "ENTER SERVER URL", 10)
            display = self._url_input[-50:] + "_"
            pyxel.text(w // 2 - 100, cy, display, 7)
            pyxel.text(w // 2 - 110, cy + 16,
                       "https://your-server.example/api/upload/  [ENTER] Confirm", 13)
            return

        # Left: menu
        items = self._overlay_items()
        cy = 20
        for i, (action, label) in enumerate(items):
            sel = i == self._menu_sel
            c = 7 if sel else 13
            prefix = "\x10" if sel else " "
            pyxel.text(4, cy, f"{prefix} {label}", c)
            cy += 10

        # Right: log
        lx = 250
        pyxel.line(lx - 4, 14, lx - 4, h - 16, 1)
        pyxel.text(lx, 16, "-- Sync Log --", 3)
        ly = 28
        max_lines = (h - 44) // 8
        for text, color in self._log[-max_lines:]:
            pyxel.text(lx, ly, text[:48], color)
            ly += 8

        pyxel.text(4, h - 22,
                   f"Uploaded: {len(self._uploaded_sessions)} sessions  |  "
                   f"Requires: Lv.6 WARDRIVER (6000 XP)", 13)
        pyxel.text(4, h - 12, "[ENTER] Execute  [ESC] Back", 13)

        # Auth PIN popup (drawn on top of everything)
        if self._auth_pin_active():
            self._draw_auth_pin(w, h)

    def open_pending_logs(self):
        """Open local file management without an upload level or API key."""
        self._open_pending_logs(from_sync=False)

    def _open_pending_logs(self, *, from_sync):
        import pyxel
        self._overlay_active = self._pending_browser = True
        self._pending_from_sync = from_sync
        self._entering_key = self._entering_url = False
        self._pending_delete = None
        self._retention_settings_open = False
        self._retention_custom = False
        self._retention_message = ""
        self._pending_message = ""
        self._pending_open_frame = pyxel.frame_count
        self._refresh_pending_logs()

    def _refresh_pending_logs(self):
        """Take a read-only catalog snapshot; never prune while browsing."""
        previous = (self._pending_rows[self._pending_sel]
                    if 0 <= self._pending_sel < len(self._pending_rows) else {})
        key = previous.get("session"), previous.get("filename")
        try:
            self._pending_rows = self.list_pending_logs()
        except (OSError, ValueError):
            self._pending_rows = []
            self._pending_message = "Could not read pending logs. Press R to retry."
        self._pending_sel = next((index for index, item in enumerate(self._pending_rows)
            if (item.get("session"), item.get("filename")) == key),
            min(self._pending_sel, max(0, len(self._pending_rows) - 1)))
        self._pending_scroll = min(self._pending_scroll, self._pending_sel)

    def _consume_pending_escape(self):
        import pyxel
        if self.app:
            self.app._esc_consumed_frame = pyxel.frame_count

    def _pending_can_delete(self, item):
        return (not self._uploading and bool(item.get("can_delete"))
                and not item.get("active") and not item.get("confirmed")
                and item.get("status") in ("ready", "waiting_gps", "empty_invalid"))

    def _update_pending_logs(self):
        import pyxel
        if pyxel.frame_count == self._pending_open_frame:
            return
        if self._retention_settings_open:
            self._update_retention_settings()
            return
        if self._pending_delete is not None:
            if pyxel.frame_count == self._pending_confirm_frame:
                return
            if pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                self._pending_delete = None
                self._pending_message = "Deletion cancelled."
                if pyxel.btnp(pyxel.KEY_ESCAPE):
                    self._consume_pending_escape()
            elif pyxel.btnp(pyxel.KEY_Y):
                item, self._pending_delete = self._pending_delete, None
                if self._uploading:
                    self._pending_message = "Upload in progress. Nothing was deleted."
                else:
                    try:
                        result = self.delete_pending_log(item["session"], item["filename"], item["token"])
                        self._pending_message = ("Deleted " + item["filename"] + "." if result.get("deleted")
                                                 else result.get("reason") or "Log changed. Refresh and review it again.")
                    except (OSError, ValueError):
                        self._pending_message = "Could not delete this log. Refresh and try again."
                self._refresh_pending_logs()
            return
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._pending_browser = False
            self._overlay_active = bool(self._pending_from_sync)
            self._consume_pending_escape()
            return
        if pyxel.btnp(pyxel.KEY_A):
            self._open_retention_settings()
            return
        if pyxel.btnp(pyxel.KEY_R):
            self._pending_message = ""
            self._refresh_pending_logs()
            return
        size = len(self._pending_rows)
        if not size:
            return
        if pyxel.btnp(pyxel.KEY_HOME):
            self._pending_sel = 0
        elif pyxel.btnp(pyxel.KEY_END):
            self._pending_sel = size - 1
        elif pyxel.btnp(pyxel.KEY_PAGEUP):
            self._pending_sel = max(0, self._pending_sel - self._pending_page_rows)
        elif pyxel.btnp(pyxel.KEY_PAGEDOWN):
            self._pending_sel = min(size - 1, self._pending_sel + self._pending_page_rows)
        elif pyxel.btnp(pyxel.KEY_UP):
            self._pending_sel = max(0, self._pending_sel - 1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._pending_sel = min(size - 1, self._pending_sel + 1)
        if pyxel.btnp(pyxel.KEY_D):
            item = self._pending_rows[self._pending_sel]
            if not self._pending_can_delete(item):
                self._pending_message = ("Upload in progress. Wait before deleting logs." if self._uploading
                    else item.get("reason") or "This log is protected and cannot be deleted.")
                return
            # Keep the exact reviewed file/token even if the list later changes.
            self._pending_delete = dict(item)
            self._pending_confirm_frame = pyxel.frame_count

    @staticmethod
    def _retention_choices():
        return ((0, "Keep all"), (1, "After 1 day"), (5, "After 5 days"),
                (15, "After 15 days"), (31, "After 31 days"), (None, "Custom days..."))

    def _retention_label(self):
        days = self._get_retention().days
        return "Keep all" if not days else f"After {days} day" + ("s" if days != 1 else "")

    def _open_retention_settings(self):
        import pyxel
        if self._pending_delete is not None:
            return
        days = self._get_retention().days
        choices = self._retention_choices()
        self._retention_sel = next((i for i, (value, _label) in enumerate(choices)
                                    if value == days), len(choices) - 1)
        self._retention_settings_open = True
        self._retention_custom = False
        self._retention_input = str(days) if days else ""
        self._retention_message = ""
        self._retention_open_frame = pyxel.frame_count

    def _apply_retention_choice(self, days):
        try:
            saved = self._set_retention_days(days)
        except (OSError, ValueError):
            saved = False
        if not saved:
            self._retention_message = "Could not save setting. Previous policy stays."
            return
        self._retention_settings_open = False
        self._retention_custom = False
        self._retention_message = ""
        self._pending_message = "Auto-clean saved: " + self._retention_label() + ". Uploaded logs only."

    def _update_retention_settings(self):
        import pyxel
        if pyxel.frame_count == self._retention_open_frame:
            return
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._consume_pending_escape()
            self._retention_message = ""
            if self._retention_custom:
                self._retention_custom = False
                self._retention_open_frame = pyxel.frame_count
            else:
                self._retention_settings_open = False
                self._pending_message = "Auto-clean setting unchanged."
            return
        if self._retention_custom:
            for digit in "0123456789":
                if pyxel.btnp(getattr(pyxel, "KEY_" + digit)):
                    if len(self._retention_input) < 6:
                        self._retention_input += digit
                        self._retention_message = ""
                    else:
                        self._retention_message = "Use 1-999999 days (up to 6 digits)."
            if pyxel.btnp(pyxel.KEY_BACKSPACE):
                self._retention_input = self._retention_input[:-1]
                self._retention_message = ""
            if pyxel.btnp(pyxel.KEY_RETURN):
                days = int(self._retention_input) if self._retention_input else 0
                if not 1 <= days <= 999999:
                    self._retention_message = "Enter a number from 1 to 999999 days."
                else:
                    self._apply_retention_choice(days)
            return
        choices = self._retention_choices()
        if pyxel.btnp(pyxel.KEY_UP):
            self._retention_sel = max(0, self._retention_sel - 1)
            self._retention_message = ""
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._retention_sel = min(len(choices) - 1, self._retention_sel + 1)
            self._retention_message = ""
        if pyxel.btnp(pyxel.KEY_RETURN):
            days = choices[self._retention_sel][0]
            if days is None:
                self._retention_custom = True
                self._retention_message = ""
                self._retention_open_frame = pyxel.frame_count
            else:
                self._apply_retention_choice(days)

    def _draw_retention_settings(self, w, h):
        import pyxel
        import textwrap
        bx, by, bw, bh = (w - 520) // 2, (h - 228) // 2, 520, 228
        pyxel.rect(bx, by, bw, bh, 0)
        pyxel.rectb(bx, by, bw, bh, 10)
        pyxel.text(bx + 12, by + 10, "UPLOADED LOG AUTO-CLEAN", 10)
        pyxel.text(bx + 12, by + 29, "Only successfully uploaded logs.", 7)
        pyxel.text(bx + 12, by + 41, "Pending / failed / active logs stay.", 13)
        pyxel.text(bx + 12, by + 53, "Days since confirmed upload.", 13)
        if self._retention_custom:
            pyxel.text(bx + 18, by + 82, "Custom retention: 1-999999 days", 3)
            pyxel.rect(bx + 18, by + 99, 200, 18, 1)
            pyxel.text(bx + 24, by + 104, "Days: " + self._retention_input + "_", 7)
            pyxel.text(bx + 18, by + 132, "Keep all is available in the previous menu.", 13)
            controls = "[ENTER] Save    [BACKSPACE] Edit    [ESC] Choices"
        else:
            for index, (_days, label) in enumerate(self._retention_choices()):
                y = by + 75 + index * 14
                if index == self._retention_sel:
                    pyxel.rect(bx + 8, y - 2, bw - 16, 12, 1)
                pyxel.text(bx + 18, y, label, 7 if index == self._retention_sel else 13)
            controls = "[UP/DOWN] Select    [ENTER] Apply    [ESC] Cancel"
        for index, line in enumerate(textwrap.wrap(self._retention_message,
                width=(bw - 24) // 5)[:2]):
            pyxel.text(bx + 12, by + 174 + index * 9, line, 10)
        pyxel.text(bx + 12, by + 212, controls, 13)

    @staticmethod
    def _pending_count(value):
        value = max(0, int(value or 0))
        return str(value) if value <= 999999 else "999999+"

    @staticmethod
    def _pending_size(value):
        value = max(0, int(value or 0))
        if value < 1024:
            return f"{value} B"
        if value < 1024 * 1024:
            return f"{value / 1024:.1f} KB"
        return f"{value / (1024 * 1024):.1f} MB"

    def _draw_pending_logs(self, w, h):
        import pyxel
        import textwrap
        pyxel.rect(0, 0, w, h, 0)
        pyxel.rect(0, 0, w, 12, 1)
        pyxel.text(4, 3, "PENDING UPLOAD LOGS", 3)
        pyxel.text(4, 18, "Local file management - no API key required", 13)
        total = len(self._pending_rows)
        count = f"{self._pending_sel + 1 if total else 0}/{total}"
        pyxel.text(w - 4 - len(count) * 5, 18, count, 13)
        for x, label in ((6, "SESSION"), (112, "LOG FILE"), (226, "TYPE"),
                         (280, "STATUS"), (366, "ROWS"), (424, "READY / NO GPS"), (510, "SIZE")):
            pyxel.text(x, 33, label, 3)
        self._pending_page_rows = max(1, (h - 152) // 12)
        self._pending_scroll = min(self._pending_scroll, max(0, total - self._pending_page_rows))
        if self._pending_sel < self._pending_scroll:
            self._pending_scroll = self._pending_sel
        elif self._pending_sel >= self._pending_scroll + self._pending_page_rows:
            self._pending_scroll = self._pending_sel - self._pending_page_rows + 1
        statuses = {"ready": "READY", "waiting_gps": "WAIT GPS", "empty_invalid": "EMPTY/INVALID",
                    "confirmed": "CONFIRMED", "active": "ACTIVE", "unreadable": "UNREADABLE"}
        kinds = {"wardriving.csv": "WIFI/BLE", "adsb_aircraft.csv": "ADS-B",
                 "meshcore_nodes.csv": "MC", "meshtastic_nodes.csv": "MT"}
        for index, item in enumerate(self._pending_rows[self._pending_scroll:
                self._pending_scroll + self._pending_page_rows], self._pending_scroll):
            y = 46 + (index - self._pending_scroll) * 12
            selected = index == self._pending_sel
            if selected:
                pyxel.rect(3, y - 1, w - 6, 10, 1)
            color = 7 if selected else 13
            values = ((6, str(item.get("session", ""))[:19]),
                (112, str(item.get("filename", ""))[:21]),
                (226, kinds.get(item.get("filename"), str(item.get("kind", ""))[:8].upper())),
                (280, statuses.get(item.get("status"), "UNKNOWN")),
                (366, self._pending_count(item.get("records"))),
                (424, (self._pending_count(item.get("eligible")) + "/" +
                       self._pending_count(item.get("no_gps")))[:15]),
                (510, self._pending_size(item.get("bytes"))[:12]))
            for x, text in values:
                pyxel.text(x, y, text, color)
        if not total:
            pyxel.text(6, 54, "No pending upload logs.", 13)
        pyxel.text(4, h - 106, "Auto-clean: " + self._retention_label()
                   + "  |  A settings  |  Uploaded logs only", 3)
        pyxel.line(4, h - 94, w - 5, h - 94, 1)
        if total:
            item = self._pending_rows[self._pending_sel]
            pyxel.text(4, h - 82, (str(item.get("session", "")) + " / " +
                       str(item.get("filename", "")))[:124], 7)
            reason = item.get("reason") or ("D: review deletion of this one export. Saved progress stays."
                if self._pending_can_delete(item) else "This log is protected.")
            for index, line in enumerate(textwrap.wrap(reason, width=max(1, (w - 8) // 5))[:2]):
                pyxel.text(4, h - 70 + index * 9, line, 13)
        for index, line in enumerate(textwrap.wrap(self._pending_message, width=max(1, (w - 8) // 5))[:2]):
            pyxel.text(4, h - 42 + index * 9, line, 10)
        pyxel.text(4, h - 12, "UP/DOWN select  PgUp/PgDn page  D delete  A auto-clean  R refresh  Esc back", 13)
        if self._pending_delete is not None:
            item = self._pending_delete
            bx, by, bw, bh = (w - 520) // 2, (h - 118) // 2, 520, 118
            pyxel.rect(bx, by, bw, bh, 0)
            pyxel.rectb(bx, by, bw, bh, 10)
            pyxel.text(bx + 12, by + 8, "DELETE THIS UPLOAD LOG?", 10)
            pyxel.text(bx + 12, by + 25, str(item["session"])[:90], 7)
            pyxel.text(bx + 12, by + 37, str(item["filename"])[:90], 7)
            detail = (f"Records: {item.get('records', 0)}  Ready: {item.get('eligible', 0)}"
                      f"  No GPS: {item.get('no_gps', 0)}")
            pyxel.text(bx + 12, by + 52, detail[:98], 13)
            pyxel.text(bx + 12, by + 68, "Only this CSV export is deleted.", 7)
            pyxel.text(bx + 12, by + 80, "Saved XP, device history and contacts stay.", 7)
            pyxel.text(bx + 12, by + 102, "[Y] Delete    [N/ESC] Cancel", 10)
        if self._retention_settings_open:
            self._draw_retention_settings(w, h)

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    # ------------------------------------------------------------------
    def _overlay_items(self) -> list[tuple[str, str]]:
        snapshot = self._sync_snapshot_view()
        if self._sync_upload_pending:
            pending = "preparing upload..." if not self._uploading else "uploading..."
        elif snapshot["state"] == "ready":
            pending = f"{len(snapshot['sessions'])} pending"
        elif snapshot["state"] == "error":
            pending = "check failed; retry"
        else:
            pending = "preparing..."
        items = [
            ("upload_all", f"Upload All ({pending})"),
            ("upload_latest", "Upload Latest Session"),
            ("show_profile", "Show My Profile"),
            ("sync_badges", "Sync Badges"),
            ("set_key", "Set API Key"),
            ("test_api", "Test API Connection"),
            ("show_pending", "Manage Pending Logs"),
            ("reset", "Reset Upload History"),
        ]
        return items

    def _exec(self, action: str):
        if action == "upload_all":
            self._start_upload(all_sessions=True)
        elif action == "upload_latest":
            self._start_upload(all_sessions=False)
        elif action == "set_url":
            self._entering_url = True
            self._url_input = self._api_url
        elif action == "set_key":
            self._entering_key = True
            self._key_input = self._api_key
        elif action == "show_profile":
            self._show_profile()
        elif action == "sync_badges":
            self._start_badge_sync()
        elif action == "test_api":
            self._test_api()
        elif action == "show_pending":
            self._open_pending_logs(from_sync=True)
        elif action == "reset":
            previous = (self._uploaded_sessions.copy(), self._uploaded_meshtastic_sessions.copy(), self._upload_receipts.copy(), self._upload_coverage.copy(), self._upload_keep_exports.copy())
            self._uploaded_sessions.clear()
            self._uploaded_meshtastic_sessions.clear()
            self._upload_receipts.clear()
            self._upload_coverage.clear()
            self._upload_keep_exports.clear()
            if self._save_state() is False:
                self._uploaded_sessions, self._uploaded_meshtastic_sessions, self._upload_receipts, self._upload_coverage, self._upload_keep_exports = previous
                self._log_add("Cannot save history reset; history kept", 8)
            else:
                self._log_add("Upload history cleared", 10)

    # ------------------------------------------------------------------
    # Upload via API
    # ------------------------------------------------------------------
    @staticmethod
    def _upload_log_specs():
        return {"wardriving.csv": ("Wi-Fi/BLE", "_parse_csv"),
                "adsb_aircraft.csv": ("ADS-B", "_parse_adsb"),
                "meshcore_nodes.csv": ("MeshCore", "_parse_meshcore"),
                "meshtastic_nodes.csv": ("Meshtastic", "_parse_meshtastic")}

    def _get_retention(self):
        """Lazily load local policy/proofs; drawing never rereads the ledger."""
        from types import SimpleNamespace
        from watchdogs.upload_retention import UploadedLogRetention
        loot = getattr(self.app, "loot", None) if self.app else None
        base = getattr(loot, "_base", None)
        if base is None:
            return SimpleNamespace(days=0)
        path = Path(base) / ".uploaded_log_retention.json"
        cached = getattr(self, "_retention_store", None)
        if cached is None or cached[0] != path:
            cached = self._retention_store = (path, UploadedLogRetention(path))
        return cached[1]

    def _set_retention_days(self, days):
        with self._upload_queue_lock():
            store = self._get_retention()
            if not hasattr(store, "set_days") or not store.set_days(days):
                return False
            self._retention_next_check = 0.0
            self._retention_seed_scope = None
            return True

    def _retention_scope(self):
        if not self._api_key or not self._api_url:
            return ""
        message = "wdg-upload-retention-scope-v1\0" + self._api_url
        return hmac.new(self._api_key.encode(), message.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _retention_receipt_digest(receipt):
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _retention_source_count(filename, content):
        """Reject omitted/malformed rows instead of certifying a partial log."""
        import csv
        import io
        from watchdogs.loot_manager import LootManager
        text = content.decode("utf-8")
        if filename == "wardriving.csv":
            lines = text.splitlines()
            if len(lines) < 2:
                return None
            if not re.match(r"^WigleWifi-\d+(?:\.\d+)+,", lines[0]):
                return None
            if next(csv.reader([lines[1]]), []) != LootManager._WIGLE_HEADER.strip().split(","):
                return None
            from watchdogs.piglet_csv import collection_preamble
            if collection_preamble(lines[0]):
                try:
                    return sum(1 for _ in LootManager.iter_wardrive_fields(io.StringIO(text, newline="")))
                except (ValueError, csv.Error):
                    return None
            rows = [line for line in lines[2:] if line.strip()]
            if any(LootManager._wardrive_fields(line) is None for line in rows):
                return None
            return len(rows)
        rows = list(csv.reader(io.StringIO(text), strict=True))
        if not rows:
            return None
        header, body = rows[0], [row for row in rows[1:] if row]
        if len(header) != len(set(header)) or any(len(row) != len(header) for row in body):
            return None
        if filename == "adsb_aircraft.csv":
            expected = ["timestamp", "icao", "callsign", "lat", "lon", "alt_ft", "speed_kt", "heading"]
            if [name.strip().lower() for name in header] != expected or any(not row[1] for row in body):
                return None
        elif (not ({"node_id", "id"} & set(header))
              or not {"lat", "lon"}.issubset(header)):
            return None
        return len(body)

    def _retention_capture(self, session_dir, data):
        """Snapshot only complete files represented in this exact payload."""
        loot = getattr(self.app, "loot", None) if self.app else None
        if loot is None or not self._retention_scope():
            return []
        base = Path(loot._base).resolve()
        directory = Path(session_dir)
        if directory.is_symlink() or directory.resolve().parent != base:
            return []
        snapshots = []
        groups = {"wardriving.csv": "networks", "adsb_aircraft.csv": "aircraft",
                  "meshcore_nodes.csv": "meshcore_nodes", "meshtastic_nodes.csv": "meshcore_nodes"}
        for filename, (_kind, parser) in self._upload_log_specs().items():
            path = directory / filename
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                token, digest, content = self._upload_log_token(path)
                count = self._retention_source_count(filename, content)
                parsed = getattr(self, parser)(path)
                if not count or count != len(parsed):
                    continue
                canonical = lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                required = Counter(canonical(row) for row in parsed)
                submitted = Counter(canonical(row) for row in data.get(groups[filename], []))
                if required - submitted or self._upload_log_token(path)[0] != token:
                    continue
                snapshots.append(dict(session=directory.name, filename=filename,
                                      digest=digest, token=token))
            except (OSError, ValueError, TypeError, UnicodeError, csv.Error):
                continue
        return snapshots

    def _retention_confirm_files(self, snapshots, data, *, account=None):
        """Prepare proof reads outside the DB lock, revalidate before recording."""
        if not snapshots or self._sync_stop.is_set():
            return
        loot = self.app.loot
        current_account = (self._api_url, self._api_key)
        if account is not None and account != current_account:
            return
        receipt = self._cached_receipt(data) or self._cached_coverage(data)
        if receipt is None:
            return
        receipt_key = self._receipt_key(data)
        receipt_digest = self._retention_receipt_digest(receipt)
        prepared = []
        for entry in snapshots:
            path = Path(loot._base) / entry["session"] / entry["filename"]
            try:
                version = self._upload_log_version(path)
                if (not path.is_symlink() and not path.parent.is_symlink()
                        and self._upload_log_token(path)[0] == entry["token"]
                        and self._upload_log_version(path) == version):
                    prepared.append((entry, path, version))
            except OSError:
                continue
        lock = getattr(loot, "_db_lock", None)
        with self._upload_queue_lock(), (lock if lock is not None else nullcontext()):
            if (self._sync_stop.is_set() or self.app.loot is not loot
                    or current_account != (self._api_url, self._api_key)
                    or self._retention_receipt_digest(self._stored_upload_proof(receipt_key)) != receipt_digest):
                return
            files = []
            for entry, path, version in prepared:
                try:
                    if (not path.is_symlink() and not path.parent.is_symlink()
                            and self._upload_log_version(path) == version):
                        files.append({key: entry[key] for key in ("session", "filename", "digest")})
                except OSError:
                    continue
            if files and not self._get_retention().record_many(files,
                    scope=self._retention_scope(), receipt_key=receipt_key,
                    receipt_digest=receipt_digest):
                self._sync_log("Upload confirmed; auto-clean proof could not be saved. Logs kept.", 10)

    def _retention_tick(self):
        """No per-frame catalog scans, and never remove a browsed/active log."""
        if (not self.app or self._uploading or self._pending_browser or self._sync_stop.is_set()
                or getattr(self.app, "_shutting_down", False)):
            return
        now = time.monotonic()
        if now < getattr(self, "_retention_next_check", 0.0):
            return
        self._retention_next_check = now + 60.0
        try:
            self._retention_seed_existing()
            self.cleanup_uploaded_logs()
        except (OSError, ValueError, TypeError):
            self._sync_log("Uploaded-log auto-clean could not check local files; remaining logs kept", 10)

    def _retention_seed_existing(self):
        """Age old exact receipts from first verification, never file mtime.

        Only eight closed sessions per minute are examined on enabling/restart.
        Legacy uploaded flags and registry-presence checks are not receipts.
        """
        if not self.app or self._uploading or self._pending_browser or self._sync_stop.is_set():
            return
        store, scope = self._get_retention(), self._retention_scope()
        if not store.days or not scope:
            return
        context = (str(self.app.loot._base), scope)
        if getattr(self, "_retention_seed_scope", None) != context:
            base = Path(self.app.loot._base).resolve()
            sessions = [path for path in sorted(base.iterdir())
                        if path.is_dir() and not path.is_symlink()
                        and path.resolve().parent == base]
            self._retention_seed_scope = context
            self._retention_seed_remaining = iter(sessions)
        pending = getattr(self, "_retention_seed_remaining", iter(()))
        for _ in range(8):
            if self._sync_stop.is_set():
                return
            directory = next(pending, None)
            if directory is None:
                return
            if directory.name == self._active_session_name():
                continue
            data = {"networks": self._parse_csv(directory / "wardriving.csv"),
                    "aircraft": self._parse_adsb(directory / "adsb_aircraft.csv"),
                    "meshcore_nodes": self._merge_mesh_nodes(
                        self._parse_meshcore(directory / "meshcore_nodes.csv"),
                        self._parse_meshtastic(directory / "meshtastic_nodes.csv"))}
            candidates = [data]
            # Earlier versions could acknowledge legacy and Meshtastic groups
            # separately. Try only the exact reconstructed submitted groups.
            legacy = dict(data, meshcore_nodes=[row for row in data["meshcore_nodes"]
                                               if row.get("network") != "meshtastic"])
            mt_only = dict(networks=[], aircraft=[], meshcore_nodes=[
                row for row in data["meshcore_nodes"] if row.get("network") == "meshtastic"])
            candidates.extend([legacy, mt_only])
            seen = set()
            for payload in candidates:
                key = self._receipt_key(payload)
                if key in seen or not any(payload.values()):
                    continue
                seen.add(key)
                if self._cached_receipt(payload) is not None or self._cached_coverage(payload) is not None:
                    self._retention_confirm_files(self._retention_capture(directory, payload), payload)

    def cleanup_uploaded_logs(self):
        """Remove only expired, unchanged exports with their own exact ACK."""
        result = dict(deleted=0, skipped=0, errors=0)
        loot = getattr(self.app, "loot", None) if self.app else None
        if loot is None or self._uploading or self._pending_browser or self._sync_stop.is_set():
            return result
        store = self._get_retention()
        if not store.days or not self._retention_scope():
            return result
        base = Path(loot._base).resolve()
        lock = getattr(loot, "_db_lock", None)
        # Catalog/proof reads and full source hashes must not hold the lock
        # used by main-loop XP/GPS writers. Only a verified discard needs it.
        due = store.due()
        order = lambda item: (item["confirmed_at"], item["session"], item["filename"])
        cursor = getattr(self, "_retention_cleanup_cursor", None)
        start = next((i for i, item in enumerate(due) if cursor is None or order(item) > cursor), 0)
        batch = due[start:start + 8]
        self._retention_cleanup_cursor = (order(batch[-1]) if batch and start + 8 < len(due) else None)
        for proof in batch:
            if self._sync_stop.is_set():
                break
            directory = base / proof["session"]
            path = directory / proof["filename"]
            try:
                receipt = self._stored_upload_proof(proof["receipt_key"])
                if (self._uploading or self._pending_browser or not store.days
                        or proof["scope"] != self._retention_scope()
                        or proof["filename"] not in self._upload_log_specs()
                        or directory.is_symlink() or directory.resolve().parent != base
                        or path.is_symlink() or not path.is_file()
                        or proof["session"] == self._active_session_name()
                        or not isinstance(receipt, dict)
                        or self._retention_receipt_digest(receipt) != proof["receipt_digest"]):
                    result["skipped"] += 1
                    continue
                version = self._upload_log_version(path)
                _token, digest, content = self._upload_log_token(path)
                del content
                if digest != proof["digest"] or self._upload_log_version(path) != version:
                    result["skipped"] += 1
                    continue
                with self._upload_queue_lock(), (lock if lock is not None else nullcontext()):
                    def still_current():
                        return (not self._sync_stop.is_set() and self.app.loot is loot
                            and proof in store.due() and not self._uploading and not self._pending_browser
                            and proof["session"] != self._active_session_name()
                            and proof["scope"] == self._retention_scope()
                            and self._retention_receipt_digest(self._stored_upload_proof(proof["receipt_key"]))
                                == proof["receipt_digest"]
                            and not path.is_symlink() and not directory.is_symlink()
                            and directory.resolve().parent == base
                            and self._upload_log_version(path) == version)
                    if not still_current():
                        result["skipped"] += 1
                        continue
                    preserve = getattr(loot, "prepare_upload_log_discard", None)
                    if preserve is None or not preserve(directory, proof["filename"], digest):
                        result["skipped"] += 1
                        continue
                    # prepare_upload_log_discard checked the exact digest under
                    # this same writer lock. Its version must still be unchanged.
                    if not still_current():
                        result["skipped"] += 1
                        continue
                    path.unlink()
                    store.forget(proof)
                    self._upload_log_checks.pop(str(path), None)
                    self._upload_wait_checks.pop(str(path), None)
                    self._pending_log_checks.pop(str(directory), None)
                    loot._gps_points_cache = None
                    result["deleted"] += 1
            except (OSError, ValueError, TypeError, KeyError, UnicodeError):
                result["errors"] += 1
        if result["deleted"]:
            self._sync_log(f"Auto-clean removed {result['deleted']} confirmed uploaded log(s); progress kept", 13)
        return result

    def _upload_queue_lock(self):
        return self.__dict__.setdefault("_local_upload_queue_lock", threading.RLock())

    @staticmethod
    def _upload_log_token(path):
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        stamp = (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
        if stamp != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            raise OSError("Log changed while reading")
        digest = hashlib.sha256(content).hexdigest()
        token = hashlib.sha256(json.dumps([*stamp, digest]).encode()).hexdigest()
        return token, digest, content

    def _inspect_pending_log(self, session_dir, filename, *, receipts=True):
        import csv
        import io
        from watchdogs.loot_manager import LootManager
        kind, parser = self._upload_log_specs()[filename]
        path = session_dir / filename
        entry = dict(session=session_dir.name, filename=filename, kind=kind, status="unreadable",
                     records=0, eligible=0, no_gps=0, bytes=0, active=False, confirmed=False,
                     can_delete=False, token="", reason="Cannot safely read this log.")
        try:
            if path.is_symlink() or not path.is_file():
                return entry
            token, digest, content = self._upload_log_token(path)
            text = content.decode("utf-8")
            ready = set()
            no_gps = 0
            records = 0
            if filename == "wardriving.csv":
                rows = list(LootManager.iter_wardrive_fields(io.StringIO(text, newline="")))
                samples = [(fields[0], fields[6], fields[7], fields[8] or "0") for fields in rows]
            elif filename == "adsb_aircraft.csv":
                rows = list(csv.reader(io.StringIO(text)))[1:]
                samples = [(row[1], row[3], row[4], "0") for row in rows if len(row) >= 8 and row[1]]
            else:
                rows = list(csv.DictReader(io.StringIO(text)))
                samples = [(str(row.get("node_id") or row.get("id") or "").strip().lower().lstrip("!"),
                            row.get("lat") or "0", row.get("lon") or "0", "0") for row in rows]
                samples = [sample for sample in samples if re.fullmatch(r"[a-f0-9]{8,16}", sample[0])]
            records = len(rows)
            for identity, raw_lat, raw_lon, raw_alt in samples:
                try:
                    lat, lon, alt = float(raw_lat), float(raw_lon), float(raw_alt)
                except (ValueError, TypeError):
                    continue
                if not (all(math.isfinite(value) for value in (lat, lon, alt))
                        and -90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                if lat == 0 and lon == 0:
                    no_gps += 1
                else:
                    ready.add(identity)
            # Independent stable-field checks above protect positioned records
            # even if an older upload parser cannot represent that row.
            eligible = max(len(ready), len(getattr(self, parser)(path)))
            confirmed = session_dir.name in (self._uploaded_meshtastic_sessions
                if filename == "meshtastic_nodes.csv" else self._uploaded_sessions)
            if receipts and eligible and not confirmed:
                payload = {"networks": self._parse_csv(session_dir / "wardriving.csv"),
                    "aircraft": self._parse_adsb(session_dir / "adsb_aircraft.csv"),
                    "meshcore_nodes": self._merge_mesh_nodes(
                        self._parse_meshcore(session_dir / "meshcore_nodes.csv"),
                        self._parse_meshtastic(session_dir / "meshtastic_nodes.csv"))}
                confirmed = (self._cached_receipt(payload) is not None or self._cached_coverage(payload) is not None
                             or self._cached_mesh_verification(payload))
            if self._upload_log_token(path)[0] != token:
                raise OSError("Log changed while inspecting")
            active = session_dir.name == self._active_session_name()
            status = "ready" if eligible else "waiting_gps" if no_gps else "empty_invalid"
            reason = "Delete this local export only; saved progress and contacts stay."
            can_delete = True
            if eligible and confirmed:
                status, can_delete, reason = "confirmed", False, "Confirmed upload data is protected."
            if active:
                status, can_delete, reason = "active", False, "Active session: close/restart before deleting its export."
            elif self._uploading:
                can_delete, reason = False, "Upload in progress; wait until it finishes."
            entry.update(status=status, records=records, eligible=eligible, no_gps=no_gps,
                         bytes=len(content), token=token, active=active,
                         confirmed=bool(confirmed and eligible), can_delete=can_delete, reason=reason)
        except (OSError, ValueError, TypeError, UnicodeError, csv.Error):
            pass
        return entry

    def list_pending_logs(self) -> list[dict]:
        """Read-only per-export catalog, including logs waiting for GPS."""
        if not self.app or not self.app.loot:
            return []
        base = Path(self.app.loot._base).resolve()
        if not base.is_dir():
            return []
        entries = []
        for session in sorted(base.iterdir()):
            if not session.is_dir() or session.is_symlink() or session.resolve().parent != base:
                continue
            for filename in self._upload_log_specs():
                path = session / filename
                if path.exists() or path.is_symlink():
                    entries.append(self._inspect_pending_log(session, filename))
        return entries

    def _delete_pending_log(self, session, filename, token, *, automatic=False):
        result = dict(deleted=False, reason="No log was deleted; refresh the list and try again.",
                      session=session, filename=filename)
        if not self.app or not self.app.loot or self._uploading or self._sync_stop.is_set():
            result["reason"] = "Upload in progress or local loot is unavailable."
            return result
        if (filename not in self._upload_log_specs() or not isinstance(session, str)
                or not session or Path(session).name != session or session in (".", "..")):
            return result
        loot = self.app.loot
        base = Path(loot._base).resolve()
        directory = base / session
        path = directory / filename
        if (directory.is_symlink() or directory.resolve().parent != base or path.is_symlink()
                or not path.is_file() or session == self._active_session_name()):
            result["reason"] = "Active, linked or unavailable logs cannot be deleted."
            return result
        lock = getattr(loot, "_db_lock", None)
        with self._upload_queue_lock(), (lock if lock is not None else nullcontext()):
            try:
                if self._sync_stop.is_set() or self._uploading or directory.is_symlink() or path.is_symlink():
                    return result
                entry = self._inspect_pending_log(directory, filename)
                if not entry["can_delete"] or not token or entry["token"] != token:
                    result["reason"] = entry["reason"] if not entry["can_delete"] else "Log changed; refresh and confirm it again."
                    return result
                if automatic and (entry["eligible"] or not entry["no_gps"]):
                    result["reason"] = "This log contains positioned records or is not a no-GPS log."
                    return result
                current_token, digest, _ = self._upload_log_token(path)
                if current_token != token:
                    return result
                preserve = getattr(loot, "prepare_upload_log_discard", None)
                if preserve is None or not preserve(directory, filename, digest):
                    result["reason"] = "Cannot preserve local discovery history; log kept."
                    return result
                if (self._sync_stop.is_set() or self._upload_log_token(path)[0] != token
                        or path.is_symlink() or directory.is_symlink()):
                    result["reason"] = "Log changed; refresh and confirm it again."
                    return result
                path.unlink()
                self._upload_log_checks.pop(str(path), None)
                self._upload_wait_checks.pop(str(path), None)
                self._pending_log_checks.pop(str(directory), None)
                loot._gps_points_cache = None
                result.update(deleted=True, reason="Local log deleted. Saved progress and contacts kept.")
            except (OSError, ValueError, TypeError, UnicodeError):
                result["reason"] = "Cannot safely delete this log; no deletion was confirmed."
        return result

    def delete_pending_log(self, session: str, filename: str, token: str) -> dict:
        return self._delete_pending_log(session, filename, token)

    def cleanup_no_gps_logs(self) -> dict:
        """After an upload attempt, discard only closed, wholly unlocated logs."""
        result = dict(deleted=0, skipped=0, errors=0)
        if self._uploading or self._sync_stop.is_set():
            return result
        for entry in self.list_pending_logs():
            if self._sync_stop.is_set():
                break
            if entry["eligible"] or not entry["no_gps"] or not entry["can_delete"]:
                result["skipped"] += 1
                continue
            outcome = self._delete_pending_log(entry["session"], entry["filename"], entry["token"], automatic=True)
            result["deleted" if outcome["deleted"] else "errors"] += 1
        if result["deleted"]:
            self._sync_log(f"Removed {result['deleted']} inactive no-GPS upload log(s); progress and contacts kept", 13)
        return result

    def _pending_sessions(self) -> list[Path]:
        if not self.app or not self.app.loot:
            return []
        identity = self._sync_identity()
        base = Path(self.app.loot._base)
        pending = []
        self._mesh_gps_wait = 0
        for d in sorted(base.iterdir()):
            if self._sync_stop.is_set():
                break
            if not d.is_dir() or d.is_symlink():
                continue
            self._prune_upload_logs(d)
            # Session has data if any of these exist
            has_data = any((d / f).is_file() for f in
                          ["wardriving.csv", "adsb_aircraft.csv", "meshcore_nodes.csv"])
            has_mt = self._has_meshtastic(d)
            if ((has_data and d.name not in self._uploaded_sessions)
                    or (has_mt and d.name not in self._uploaded_meshtastic_sessions)):
                if self._session_has_pending_records(d): pending.append(d)
        self._publish_sync_snapshot(identity, sessions=pending)
        return pending

    def _session_upload_data(self, session_dir: Path) -> dict:
        legacy_done = session_dir.name in self._uploaded_sessions
        mt_done = session_dir.name in self._uploaded_meshtastic_sessions
        if legacy_done: self._mesh_parse_stats["meshcore"] = {}
        if mt_done: self._mesh_parse_stats["meshtastic"] = {}
        return {
            "networks": [] if legacy_done else self._parse_csv(session_dir / "wardriving.csv"),
            "aircraft": [] if legacy_done else self._parse_adsb(session_dir / "adsb_aircraft.csv"),
            "meshcore_nodes": self._merge_mesh_nodes(
                [] if legacy_done else self._parse_meshcore(session_dir / "meshcore_nodes.csv"),
                [] if mt_done else self._parse_meshtastic(session_dir / "meshtastic_nodes.csv")),
        }

    def _session_has_pending_records(self, session_dir: Path) -> bool:
        versions = []
        for name in ("wardriving.csv", "adsb_aircraft.csv", "meshcore_nodes.csv", "meshtastic_nodes.csv"):
            try:
                st = (session_dir/name).stat()
                versions.append((name, st.st_ino, st.st_mtime_ns, st.st_size))
            except FileNotFoundError: pass
        signature = (tuple(versions), session_dir.name in self._uploaded_sessions,
                     session_dir.name in self._uploaded_meshtastic_sessions, len(self._upload_receipts),
                     self._receipt_generation, self._api_url, self._api_key)
        cached = self._pending_log_checks.get(str(session_dir))
        if cached is not None and cached[0] == signature: return cached[1]
        data = self._session_upload_data(session_dir)
        ready = (any(data.values()) and self._cached_receipt(data) is None
                 and not self._cached_mesh_verification(data))
        self._pending_log_checks[str(session_dir)] = (signature, bool(ready))
        return bool(ready)

    def _show_cleanup_count(self):
        if self._cleanup_removed:
            self._log_add(f"Removed {self._cleanup_removed} empty/invalid upload log(s)", 13)
            self._cleanup_removed = 0

    def _prune_upload_logs(self, session_dir: Path) -> int:
        """Inspect closed exports without blocking gameplay; lock only discard.

        Complete parsing/inspection precedes a guarded mutation. Both the file
        version and all path/session/group protections are rechecked after
        acquiring the writer's lock, and again before unlinking.
        """
        if not self.app or not self.app.loot or self._uploading or self._sync_stop.is_set():
            return 0
        loot = self.app.loot
        session_dir = Path(session_dir)
        base = Path(loot._base).resolve()
        if (session_dir.is_symlink() or session_dir.resolve().parent != base
                or session_dir.name == self._active_session_name()):
            return 0
        removed = 0
        parsers = (("wardriving.csv", self._parse_csv, False),
                   ("adsb_aircraft.csv", self._parse_adsb, False),
                   ("meshcore_nodes.csv", self._parse_meshcore, False),
                   ("meshtastic_nodes.csv", self._parse_meshtastic, True))
        lock = getattr(loot, "_db_lock", None)
        for filename, parse, is_mt in parsers:
            if self._sync_stop.is_set() or self._uploading:
                break
            done = self._uploaded_meshtastic_sessions if is_mt else self._uploaded_sessions
            if session_dir.name in done:
                continue
            path = session_dir / filename
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                version = self._upload_log_version(path)
                stamp = (version[1], version[2], version[4])
                if self._upload_log_checks.get(str(path)) == stamp:
                    continue
                waiting = self._upload_wait_checks.get(str(path))
                if waiting is not None and waiting[0] == stamp:
                    protocol = "meshtastic" if is_mt else "meshcore"
                    self._mesh_parse_stats[protocol] = dict(waiting[1])
                    self._mesh_gps_wait += 1
                    continue
                # Expensive read-only work never holds the game's DB lock.
                # An interrupted read cannot authorize a discard.
                with path.open("rb") as stream:
                    stream.read(1)
                valid = parse(path)
                inspection = self._inspect_pending_log(session_dir, filename, receipts=False)
                if inspection["status"] == "unreadable":
                    continue
                valid = valid or inspection["eligible"] > 0
                if version != self._upload_log_version(path):
                    continue
                if valid:
                    self._upload_log_checks[str(path)] = stamp
                    self._upload_wait_checks.pop(str(path), None)
                elif (filename in ("meshcore_nodes.csv", "meshtastic_nodes.csv")
                        and self._mesh_parse_stats.get("meshtastic" if is_mt else "meshcore", {}).get("no_gps", 0)):
                    self._mesh_gps_wait += 1
                    protocol = "meshtastic" if is_mt else "meshcore"
                    self._upload_wait_checks[str(path)] = (stamp, dict(self._mesh_parse_stats[protocol]))
                else:
                    _token, digest, _content = self._upload_log_token(path)
                    del _content
                    if version != self._upload_log_version(path):
                        continue
                    with self._upload_queue_lock(), (lock if lock is not None else nullcontext()):
                        done = self._uploaded_meshtastic_sessions if is_mt else self._uploaded_sessions
                        if (self._sync_stop.is_set() or self._uploading or self.app.loot is not loot
                                or session_dir.name in done or session_dir.name == self._active_session_name()
                                or session_dir.is_symlink() or session_dir.resolve().parent != base
                                or path.is_symlink() or version != self._upload_log_version(path)):
                            continue
                        preserve = getattr(loot, "prepare_upload_log_discard", None)
                        if preserve is None or not preserve(session_dir, filename, digest):
                            continue
                        if (self._sync_stop.is_set() or self._uploading or self.app.loot is not loot
                                or session_dir.name in done or session_dir.name == self._active_session_name()
                                or session_dir.is_symlink() or session_dir.resolve().parent != base
                                or path.is_symlink() or version != self._upload_log_version(path)):
                            continue
                        path.unlink()
                        self._upload_log_checks.pop(str(path), None)
                        self._upload_wait_checks.pop(str(path), None)
                        self._pending_log_checks.pop(str(session_dir), None)
                        loot._gps_points_cache = None
                        removed += 1
                        log.info("Sync: removed empty/invalid upload log %s/%s", session_dir.name, filename)
            except OSError:
                log.warning("Sync: upload log cleanup failed for %s/%s", session_dir.name, filename)
        self._cleanup_removed += removed
        return removed

    @staticmethod
    def _upload_log_version(path):
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)

    @staticmethod
    def _has_meshtastic(session_dir: Path) -> bool:
        path = session_dir / "meshtastic_nodes.csv"
        if not path.is_file():
            return False
        with path.open(encoding="utf-8") as fh:
            next(fh, None)
            return any(line.strip() for line in fh)

    def _active_session_name(self) -> str:
        """Directory name of the currently-recording loot session, or ''
        if loot is not initialised. The active session keeps accumulating
        data while the game runs (ADS-B sniffer, GPS, MeshCore), so we
        never permanently mark it as uploaded — otherwise rows appended
        after an upload silently disappear because the session is not
        re-queued. Reported by a US user who ran ADS-B overnight: 578
        new aircraft ICAOs were captured but never offered for upload
        because the session got flagged 'uploaded' earlier in the day."""
        try:
            loot = self.app.loot
            for attribute in ("session_dir", "session_path", "_session"):
                path = getattr(loot, attribute, None)
                if path:
                    return Path(path).name
        except Exception:
            pass
        return ""

    def _mark_uploaded(self, session_dir: Path, *, meshtastic: bool = False) -> bool:
        """Mark a session as fully uploaded so it stops appearing in the
        pending queue. Returns True if marked, False if skipped because
        the session is still active (will be re-queued on next upload).
        Always persists state when something changed."""
        if session_dir.name == self._active_session_name():
            return False
        before = self._uploaded_sessions.copy(), self._uploaded_meshtastic_sessions.copy()
        try:
            self._uploaded_sessions.add(session_dir.name)
            if meshtastic:
                self._uploaded_meshtastic_sessions.add(session_dir.name)
            saved = self._save_state()
        except Exception:
            saved = False
        if saved is False:
            self._uploaded_sessions, self._uploaded_meshtastic_sessions = before
            self._log_add("  Cannot save upload state; left pending", 8)
            return False
        return True

    def _receipt_key(self, data: dict) -> str:
        """Bind exact supported content to the configured account and endpoint.

        No credentials or node data are stored in the ledger. A changed row,
        account or URL requires its own acknowledgement. Never infer a receipt
        from a filename, account-wide counter or server duplicate-cache notice.
        """
        signature = hmac.new(self._api_key.encode(), digestmod=hashlib.sha256)
        signature.update(b"wdg-upload-receipt-v1\0")
        signature.update(self._api_url.encode())
        signature.update(b"\0")
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
        for piece in encoder.iterencode(data):
            signature.update(piece.encode())
        return signature.hexdigest()

    def _cached_receipt(self, data: dict) -> dict | None:
        result = self._upload_receipts.get(self._receipt_key(data))
        if result is not None and _upload_receipt(result, data)[0]:
            return json.loads(json.dumps(result))
        return None

    def _remember_receipt(self, data: dict, result: dict) -> bool:
        if not _upload_receipt(result, data)[0]:
            return False
        return self._store_receipt(data, result)

    def _store_receipt(self, data: dict, result: dict) -> bool:
        key = self._receipt_key(data)
        previous = self._upload_receipts.copy()
        self._upload_receipts[key] = json.loads(json.dumps(result))
        try:
            saved = self._save_state()
        except Exception:
            saved = False
        if saved is False:
            self._upload_receipts = previous
            return False
        self._receipt_generation += 1
        self._pending_log_checks.clear()
        return True

    def _mesh_registry_url(self) -> str:
        return self._api_url.rsplit("/api/", 1)[0] + "/api/meshcore"

    def _cached_mesh_verification(self, data: dict) -> bool:
        proof = self._upload_receipts.get(self._receipt_key(data), {})
        return (not data.get("networks") and not data.get("aircraft")
                and bool(data.get("meshcore_nodes"))
                and proof.get("verification") == "mesh_registry"
                and proof.get("source") == self._mesh_registry_url()
                and type(proof.get("matched")) is int
                and proof["matched"] == len(data["meshcore_nodes"]))

    def _verify_duplicate_mesh(self, data: dict, result: dict, *, source_identity=None) -> bool:
        """Recover presence, not ownership/credit, after the server's cache reply.

        The documented authenticated registry exposes both protocols. Require
        every submitted mesh node to match ID, protocol, name, role, position
        and RSSI exactly. This is a separate kind of evidence, never a made-up
        import/known counter. A mixed batch or incomplete/mismatched registry
        remains pending. The registry does not prove timestamp or ownership.
        """
        source_identity = self._sync_identity()[:4] if source_identity is None else source_identity
        if self._sync_stop.is_set() or source_identity != self._sync_identity()[:4]:
            return False
        if not isinstance(result, dict): return False
        reasons = result.get("meshcore_reject_reasons", {})
        if (not isinstance(reasons, dict) or any(type(v) is not int or v != 0 for v in reasons.values())
                or type(result.get("meshcore_imported")) is not int
                or ("meshcore_rejected" in result and type(result["meshcore_rejected"]) is not int)
                or result.get("ok") is not True
                or result.get("info") != "This payload was already uploaded recently — no new processing."
                or result.get("meshcore_imported") != 0
                or result.get("meshcore_rejected", 0) != 0
                or data.get("networks") or data.get("aircraft")
                or not data.get("meshcore_nodes")):
            return False
        try:
            registry_url = source_identity[2].rsplit("/api/", 1)[0] + "/api/meshcore"
            req = Request(registry_url)
            req.add_header("X-API-Key", source_identity[3])
            with _open(req, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if (len(raw) > 16 * 1024 * 1024 or self._sync_stop.is_set()
                    or source_identity != self._sync_identity()[:4]):
                return False
            rows = json.loads(raw)
            if not isinstance(rows, list): return False
            by_id = {}
            for row in rows:
                if not isinstance(row, dict): return False
                node_id = row.get("node_id")
                if not isinstance(node_id, str): continue
                node_id = node_id.lower().removeprefix("!")
                if not re.fullmatch(r"[a-f0-9]{8,16}", node_id): continue
                key = row.get("network"), node_id
                by_id.setdefault(key, []).append(row)
            for node in data["meshcore_nodes"]:
                matches = by_id.get((node["network"], node["node_id"]), [])
                if len(matches) != 1: return False
                match = matches[0]
                for server_key, sent_key in (("latitude", "lat"), ("longitude", "lon"), ("rssi", "rssi")):
                    value = match.get(server_key)
                    if type(value) not in (int, float) or not math.isfinite(value) or value != node[sent_key]:
                        return False
                if any(match.get(key) != node[key] for key in ("node_type", "name")):
                    return False
            with self._upload_queue_lock():
                if self._sync_stop.is_set() or source_identity != self._sync_identity()[:4]:
                    return False
                return self._store_receipt(data, dict(verification="mesh_registry",
                    source=registry_url, matched=len(data["meshcore_nodes"])))
        except (URLError, TimeoutError, OSError, ValueError, TypeError, KeyError):
            return False

    def _sync_log(self, text: str, color: int = 13):
        """Persist upload diagnostics only; auth PINs never pass this helper."""
        self._log_add(text, color)
        log.info("Sync: %s", text)

    def _error_detail(self, body: bytes) -> str:
        """Read a short API explanation, never dump HTML or credentials."""
        try:
            result = json.loads(body)
            if not isinstance(result, dict): return ""
            text = next((result[k] for k in ("error", "message", "info")
                         if isinstance(result.get(k), str)), "")
            if self._api_key: text = text.replace(self._api_key, "[redacted]")
            return " ".join(text.split())[:160]
        except (ValueError, UnicodeError):
            return ""

    def _parse_csv(self, csv_path: Path) -> list[dict]:
        """Parse WiGLE CSV to list of network dicts."""
        if not csv_path.is_file():
            return []
        networks = []
        try:
            import csv as _csv
            from watchdogs.loot_manager import LootManager
            from watchdogs.piglet_csv import native_rows
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                def legacy_row(line):
                    parts = next(_csv.reader([line]))
                    if len(parts) != 11:
                        parts = LootManager._wardrive_fields(line)
                    return parts
                for parts in native_rows(f, legacy_row):
                    if parts is None or len(parts) < 11:
                        continue
                    try:
                        lat = float(parts[6]) if parts[6] else 0.0
                        lon = float(parts[7]) if parts[7] else 0.0
                        alt = float(parts[8]) if parts[8] else 0.0
                    except ValueError:
                        continue
                    if lat == 0.0 and lon == 0.0:
                        continue
                    if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(alt)) or lat < -90 or lat > 90 or lon < -180 or lon > 180:
                        continue
                    networks.append({
                        "bssid": parts[0],
                        "ssid": parts[1],
                        "auth": _map_auth(parts[2]),
                        "first_seen": parts[3],
                        "channel": int(parts[4]) if parts[4].isdigit() else 0,
                        "rssi": int(parts[5]) if parts[5].lstrip("-").isdigit() else -100,
                        "lat": lat,
                        "lon": lon,
                        "alt": alt,
                        "type": parts[10] if len(parts) > 10 else "WIFI",
                    })
        except Exception as e:
            self._log_add(f"CSV read failed ({type(e).__name__}); log kept pending", 8)
            raise UploadReadError("Upload CSV was not read completely") from e
        return networks

    def _parse_adsb(self, csv_path: Path) -> list[dict]:
        """Parse adsb_aircraft.csv to list of aircraft dicts."""
        if not csv_path.is_file():
            return []
        aircraft = {}
        try:
            import csv as _csv
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                reader = _csv.reader(f)
                header = next(reader, None)
                if not header:
                    return []
                for parts in reader:
                    if len(parts) < 8:
                        continue
                    try:
                        lat = float(parts[3]) if parts[3] else 0.0
                        lon = float(parts[4]) if parts[4] else 0.0
                    except ValueError:
                        continue
                    if lat == 0.0 and lon == 0.0:
                        continue
                    if not (math.isfinite(lat) and math.isfinite(lon)) or lat < -90 or lat > 90 or lon < -180 or lon > 180:
                        continue
                    icao = parts[1]
                    if not icao or icao in aircraft:
                        continue
                    try:
                        alt = int(float(parts[5])) if parts[5] else 0
                        spd = int(float(parts[6])) if parts[6] else 0
                        hdg = int(float(parts[7])) if parts[7] else 0
                    except ValueError:
                        alt = spd = hdg = 0
                    aircraft[icao] = {
                        "icao": icao,
                        "callsign": parts[2],
                        "lat": lat,
                        "lon": lon,
                        "alt_ft": alt,
                        "speed_kt": spd,
                        "heading": hdg,
                        "first_seen": parts[0],
                        "type": "ADSB",
                    }
        except Exception as e:
            self._log_add(f"ADS-B read failed ({type(e).__name__}); log kept pending", 8)
            raise UploadReadError("ADS-B CSV was not read completely") from e
        return list(aircraft.values())

    def _parse_meshcore(self, csv_path: Path) -> list[dict]:
        return self._parse_mesh(csv_path, "meshcore")

    def _parse_meshtastic(self, csv_path: Path) -> list[dict]:
        return self._parse_mesh(csv_path, "meshtastic")

    def _parse_mesh(self, csv_path: Path, network: str) -> list[dict]:
        """Read the real mesh CSV schema; never fabricate position or identity."""
        import csv as _csv
        stats = dict(rows=0, eligible=0, no_gps=0, invalid=0, duplicates=0, receiver_gps=0)
        self._mesh_parse_stats[network] = stats
        if not csv_path.is_file():
            return []
        nodes = {}
        try:
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    stats["rows"] += 1
                    node_id = str(row.get("node_id") or row.get("id") or "").strip().lower()
                    if node_id.startswith("!"):
                        node_id = node_id[1:]
                    if not re.fullmatch(r"[a-f0-9]{8,16}", node_id):
                        stats["invalid"] += 1
                        continue
                    try:
                        lat, lon = float(row.get("lat") or 0), float(row.get("lon") or 0)
                    except (ValueError, TypeError):
                        stats["invalid"] += 1
                        continue
                    if lat == 0.0 and lon == 0.0:
                        stats["no_gps"] += 1
                        continue
                    if not (math.isfinite(lat) and math.isfinite(lon)) or lat < -90 or lat > 90 or lon < -180 or lon > 180:
                        stats["invalid"] += 1
                        continue
                    try:
                        rssi = float(row.get("rssi") or 0)
                    except (ValueError, TypeError):
                        rssi = 0
                    if not math.isfinite(rssi):
                        rssi = 0
                    key = (network, node_id)
                    if key in nodes:
                        stats["duplicates"] += 1
                        continue
                    record = {
                        "node_id": node_id,
                        "network": network,
                        "node_type": row.get("type") or "Client",
                        "name": row.get("name") or "",
                        "lat": lat,
                        "lon": lon,
                        "rssi": rssi,
                        "first_seen": row.get("timestamp") or row.get("time") or "",
                    }
                    if network == "meshcore":
                        record["type"] = "MESHCORE"  # preserve existing MC envelope marker
                        public_key = row.get("public_key") or ""
                        if re.fullmatch(r"[a-fA-F0-9]{64}", public_key):
                            record["public_key"] = public_key.lower()
                    nodes[key] = record
                    if row.get("position_source") in ("receiver_gps", "receiver_assignment"):
                        stats["receiver_gps"] += 1
        except Exception as e:
            self._log_add(f"{network} read failed ({type(e).__name__}); log kept pending", 8)
            raise UploadReadError("Mesh CSV was not read completely") from e
        stats["eligible"] = len(nodes)
        return list(nodes.values())

    @staticmethod
    def _merge_mesh_nodes(*groups: list[dict]) -> list[dict]:
        nodes = {}
        for group in groups:
            for node in group:
                nodes.setdefault((node["network"], node["node_id"]), node)
        return list(nodes.values())

    @staticmethod
    def _payload_counts(data):
        return {key:len(data.get(key,[])) for key in _UPLOAD_GROUPS}

    @staticmethod
    def _payload_slice(data, start, end):
        result, offset = {}, 0
        for key in _UPLOAD_GROUPS:
            rows = data.get(key,[])
            result[key] = rows[max(0,start-offset):max(0,min(len(rows),end-offset))]
            offset += len(rows)
        return result

    @staticmethod
    def _envelope_size(raw_size):
        overhead = len(b'{"data": "", "nonce": "'+b'0'*16+b'", "sig": "'+b'0'*64+b'"}')
        return overhead + 4 * ((raw_size+2)//3)

    def _plan_upload_batches(self, data):
        """Deterministic lossless spans, bounded by the complete signed body."""
        empty = {key:[] for key in _UPLOAD_GROUPS}
        base = len(json.dumps(empty,separators=(",",":")).encode("utf-8"))
        start = total = count = 0
        raw_size, group_counts = base, {key:0 for key in _UPLOAD_GROUPS}
        for key in _UPLOAD_GROUPS:
            for record in data.get(key,[]):
                size = len(json.dumps(record,separators=(",",":"),allow_nan=False).encode("utf-8"))
                extra = size + int(group_counts[key]>0)
                if count and (count >= UPLOAD_BATCH_RECORDS or self._envelope_size(raw_size+extra)>UPLOAD_BATCH_BYTES):
                    yield (start,total)
                    start,count,raw_size = total,0,base
                    group_counts = {name:0 for name in _UPLOAD_GROUPS}
                    extra = size
                if self._envelope_size(raw_size+extra)>UPLOAD_BATCH_BYTES:
                    raise UploadReadError("A single record exceeds the upload body limit; source kept")
                raw_size += extra
                group_counts[key] += 1
                count += 1
                total += 1
        if count:
            yield (start,total)

    def _valid_coverage(self, proof):
        if not isinstance(proof,dict) or proof.get("kind")!="batch-coverage-v1":
            return False
        counts, batches = proof.get("counts"), proof.get("batches")
        if (not isinstance(counts,dict) or set(counts)!=set(_UPLOAD_GROUPS)
                or any(type(n) is not int or n<0 for n in counts.values())
                or not isinstance(batches,list) or not len(batches)>=1):
            return False
        cursor=0
        for batch in batches:
            if (not isinstance(batch,dict) or type(batch.get("start")) is not int or type(batch.get("end")) is not int
                    or batch["start"]!=cursor or batch["end"]<=cursor or not isinstance(batch.get("counts"),dict)
                    or set(batch["counts"])!=set(_UPLOAD_GROUPS)
                    or any(type(n) is not int or n<0 for n in batch["counts"].values())
                    or sum(batch["counts"].values())!=batch["end"]-cursor
                    or not isinstance(batch.get("key"),str) or not re.fullmatch(r"[0-9a-f]{64}",batch["key"])):
                return False
            submitted={key:range(batch["counts"][key]) for key in _UPLOAD_GROUPS}
            if not _upload_receipt(self._upload_receipts.get(batch["key"]),submitted)[0]:
                return False
            cursor=batch["end"]
        return cursor==sum(counts.values()) and all(
            sum(batch["counts"][key] for batch in batches)==counts[key] for key in _UPLOAD_GROUPS)

    def _cached_coverage(self, data):
        proof=getattr(self,"_upload_coverage",{}).get(self._receipt_key(data))
        if not self._valid_coverage(proof) or proof["counts"]!=self._payload_counts(data):
            return None
        for batch in proof["batches"]:
            part=self._payload_slice(data,batch["start"],batch["end"])
            if self._payload_counts(part)!=batch["counts"] or self._receipt_key(part)!=batch["key"]:
                return None
        return json.loads(json.dumps(proof))

    def _stored_upload_proof(self, key):
        receipt=self._upload_receipts.get(key)
        if isinstance(receipt,dict) and receipt.get("ok") is True:
            return receipt
        proof=getattr(self,"_upload_coverage",{}).get(key)
        return proof if self._valid_coverage(proof) else None

    def _remember_coverage(self, data, batches):
        proof=dict(kind="batch-coverage-v1",counts=self._payload_counts(data),batches=batches)
        if not self._valid_coverage(proof):
            return False
        for batch in batches:
            part=self._payload_slice(data,batch["start"],batch["end"])
            if self._payload_counts(part)!=batch["counts"] or self._receipt_key(part)!=batch["key"]:
                return False
        key=self._receipt_key(data)
        previous=dict(getattr(self,"_upload_coverage",{}))
        self._upload_coverage=dict(previous,**{key:proof})
        if self._save_state() is False:
            self._upload_coverage=previous
            return False
        self._receipt_generation+=1
        self._pending_log_checks.clear()
        return True

    def _remember_upload_keep(self, data):
        """Retain an export when an actual response reported new/updated data."""
        key=self._receipt_key(data)
        previous=set(getattr(self,"_upload_keep_exports",set()))
        if key in previous:return True
        self._upload_keep_exports=previous|{key}
        if self._save_state() is False:
            # Retain conservatively in this process too; never delete on an
            # inability to persist the observed accepted/updated outcome.
            return False
        return True

    @staticmethod
    def _reported_new_or_updated(result):
        return isinstance(result,dict) and any(type(result.get(key)) is int and result[key]>0
            for key in ("imported","updated","aircraft_imported","meshcore_imported","meshcore_merged"))

    def _wait_upload(self, delay, current):
        deadline=time.monotonic()+max(0,delay)
        while current():
            remaining=deadline-time.monotonic()
            if remaining<=0:return True
            if self._sync_stop.wait(min(.2,remaining)):return False
        return False

    def _wait_upload_slot(self, current):
        if not self._wait_upload(max(0,getattr(self,"_next_upload_post",0)-time.monotonic()),current):
            return False
        if not current():return False
        self._next_upload_post=time.monotonic()+UPLOAD_POST_INTERVAL
        return True

    def _post_upload_batch(self, data, account, current):
        rate_retried=False
        while self._wait_upload_slot(current):
            try:
                req=Request(account[0],data=self._sign_payload(data,api_key=account[1]),method="POST")
                req.add_header("Content-Type","application/json")
                req.add_header("X-API-Key",account[1])
                with _open(req,timeout=90) as response:raw=response.read(16*1024*1024+1)
                if len(raw)>16*1024*1024:
                    self._sync_log("  Server response too large; left pending",8);return None,None
                result=json.loads(raw.decode())
                if not isinstance(result,dict):raise ValueError("Invalid response")
                return (result,None) if current() else (None,None)
            except HTTPError as exc:
                detail=self._error_detail(exc.read(65536))
                if detail:self._sync_log("  Server: "+detail,10)
                if exc.code==429 and not rate_retried:
                    retry=exc.headers.get("Retry-After","5") if exc.headers else "5"
                    if retry.isdigit() and int(retry)<=60:
                        rate_retried=True
                        self._sync_log(f"  Rate limit; retry in {int(retry)}s",10)
                        if self._wait_upload(int(retry),current):continue
                self._sync_log(f"  HTTP {exc.code}; left pending",8)
                return None,exc.code
            except (URLError,TimeoutError,OSError) as exc:
                timed_out=isinstance(exc,TimeoutError) or (isinstance(exc,URLError) and isinstance(exc.reason,TimeoutError))
                self._sync_log("  Request timed out; no confirmed receipt" if timed_out else "  No confirmed response; left pending",10)
                return None,408 if timed_out else None
            except (ValueError,TypeError):
                self._sync_log("  Invalid server response; left pending",8);return None,None
        return None,None

    @staticmethod
    def _duplicate_cache_notice(result):
        return (isinstance(result,dict) and result.get("ok") is True
                and "already uploaded" in str(result.get("info","")).lower())

    @staticmethod
    def _retry_incomplete_ack(result, data):
        """One retry may reveal the server cache; it never certifies missing rows."""
        if (not isinstance(result,dict) or result.get("ok") is not True
                or "error" in result or "job_id" in result
                or WardriveUpload._duplicate_cache_notice(result) or _upload_receipt(result,data)[0]):
            return False
        fields=("imported","updated","duplicates","cooldown","rejected","captured","cap_hits","skipped",
                "aircraft_imported","aircraft_already_seen","aircraft_rejected","meshcore_imported",
                "meshcore_already_seen","meshcore_merged","meshcore_rejected","meshcore_yours_known","meshcore_owned_by_others")
        if any(key in result and (type(result[key]) is not int or result[key]<0) for key in fields):return False
        if any(result.get(key,0)>0 for key in ("rejected","aircraft_rejected","meshcore_rejected")):return False
        reasons=result.get("meshcore_reject_reasons",{})
        if not isinstance(reasons,dict) or any(type(value) is not int or value!=0 for value in reasons.values()):return False
        confirmed,info,_=_upload_receipt(result,data)
        if info.startswith(("invalid ","inconsistent ","missing ")):return False
        unknown=False
        for key,counters in (("networks",("imported","updated","duplicates","cooldown")),
                ("aircraft",("aircraft_imported","aircraft_already_seen")),
                ("meshcore_nodes",("meshcore_imported","meshcore_already_seen","meshcore_merged"))):
            count=len(data.get(key,[]))
            accounted=sum(result.get(field,0) for field in counters)
            if key=="meshcore_nodes" and "meshcore_already_seen" not in result:
                accounted+=result.get("meshcore_yours_known",0)+result.get("meshcore_owned_by_others",0)
            if accounted>count:return False
            unknown |= accounted<count
        return unknown

    @staticmethod
    def _duplicate_receipt(result, data):
        return (_upload_receipt(result,data)[0] and not any(result.get(key,0) for key in
            ("imported","updated","aircraft_imported","meshcore_imported","meshcore_merged")))

    def _upload_batched(self, data, account, current, *, force_split=False, spans=None, initial_result=None):
        """Confirm actual child payloads; a cache notice is never a receipt."""
        leaves=[]
        imported=sent=0
        duplicate_only=True
        def visit(start,end,split=False,seed=None):
            nonlocal imported,sent,duplicate_only
            if not current():return False
            part=self._payload_slice(data,start,end)
            cached=self._cached_receipt(part)
            result,code=(cached,None) if cached is not None else (seed,None)
            if result is None and not split:
                result,code=self._post_upload_batch(part,account,current)
                sent+=1
            retried_incomplete=False
            while result is not None:
                if data.get("networks") and self._reported_new_or_updated(result) and not self._remember_upload_keep(data):
                    self._sync_log("  Cannot save upload outcome; source export kept pending",8);return False
                confirmed,info,credit=_upload_receipt(result,part)
                if confirmed:
                    if not current():return False
                    if cached is None:
                        with self._upload_queue_lock():
                            if not current() or not self._remember_receipt(part,result):
                                self._sync_log("  Server confirmed; cannot save receipt; left pending",8);return False
                        imported+=credit
                        self._handle_upload_badges(result)
                    duplicate_only &= self._duplicate_receipt(result,part)
                    leaves.append(dict(start=start,end=end,key=self._receipt_key(part),counts=self._payload_counts(part)))
                    self._sync_log(f"  Confirmed batch records {start+1}-{end}",11)
                    return True
                self._sync_log("  "+info,10)
                if not retried_incomplete and self._retry_incomplete_ack(result,part):
                    retried_incomplete=True
                    self._sync_log("  Checking incomplete acknowledgement once before recovery",10)
                    result,code=self._post_upload_batch(part,account,current)
                    sent+=1
                    continue
                break
            if (split or code in (408,413) or self._duplicate_cache_notice(result)) and end-start>1:
                middle=start+(end-start)//2
                return visit(start,middle) and visit(middle,end)
            if result is not None and result.get("job_id") is not None:
                self._sync_log("  Server queued a job; no final receipt; left pending",10)
            self._sync_log("  Incomplete acknowledgement; left pending",10)
            return False
        spans=list(self._plan_upload_batches(data)) if spans is None else spans
        if force_split and len(spans)==1:
            complete=visit(*spans[0],split=True)
        else:
            complete=all(visit(*span,seed=initial_result if len(spans)==1 else None) for span in spans)
        if not complete:return dict(confirmed=False,imported=imported,duplicate_only=False,reused=False)
        if not current() or not self._remember_coverage(data,leaves):
            self._sync_log("  Cannot save complete batch coverage; logs kept pending",8)
            return dict(confirmed=False,imported=imported,duplicate_only=False,reused=False)
        return dict(confirmed=True,imported=imported,duplicate_only=duplicate_only,reused=sent==0)

    def _source_upload_tokens(self, session_dir):
        result={}
        for name in self._upload_log_specs():
            path=session_dir/name
            if path.is_file() and not path.is_symlink():result[name]=self._upload_log_token(path)[0]
        return result

    def _source_upload_unchanged(self, session_dir, tokens):
        try:return tokens==self._source_upload_tokens(session_dir)
        except (OSError,ValueError,UnicodeError):return False

    def _remove_duplicate_exports(self, snapshots, data, *, account):
        """Delete only fully covered duplicate Collection exports; raw/history stays."""
        if account!=(self._api_url,self._api_key) or self._sync_stop.is_set():return 0
        key=self._receipt_key(data)
        if key in getattr(self,"_upload_keep_exports",set()):return 0
        proof=self._cached_receipt(data) or self._cached_coverage(data)
        if proof is None:return 0
        if proof.get("kind")=="batch-coverage-v1":
            if not all(self._duplicate_receipt(self._upload_receipts[b["key"]],
                {name:range(b["counts"][name]) for name in _UPLOAD_GROUPS}) for b in proof["batches"]):return 0
        elif not self._duplicate_receipt(proof,data):return 0
        digest=self._retention_receipt_digest(proof)
        loot=self.app.loot
        base=Path(loot._base).resolve()
        lock=getattr(loot,"_db_lock",None)
        removed=0
        for entry in snapshots:
            if entry["filename"]!="wardriving.csv":continue
            path=base/entry["session"]/entry["filename"]
            metadata=path.parent/"collection_source.json"
            try:
                version=self._upload_log_version(path)
                def still_current():
                    return (not self._sync_stop.is_set() and not self._pending_browser
                        and self.app.loot is loot and account==(self._api_url,self._api_key)
                        and key not in getattr(self,"_upload_keep_exports",set())
                        and entry["session"]!=self._active_session_name()
                        and metadata.is_file() and not metadata.is_symlink()
                        and not path.is_symlink() and not path.parent.is_symlink()
                        and path.parent.resolve().parent==base
                        and self._retention_receipt_digest(self._stored_upload_proof(key))==digest
                        and self._upload_log_version(path)==version)
                with self._upload_queue_lock(),(lock if lock is not None else nullcontext()):
                    if not still_current() or self._upload_log_token(path)[0]!=entry["token"]:continue
                    preserve=getattr(loot,"prepare_upload_log_discard",None)
                    if preserve is None or not preserve(path.parent,path.name,entry["digest"]):continue
                    if not still_current() or self._upload_log_token(path)[0]!=entry["token"]:continue
                    path.unlink()
                    self._pending_log_checks.pop(str(path.parent),None)
                    self._upload_log_checks.pop(str(path),None)
                    loot._gps_points_cache=None
                    removed+=1
            except (OSError,ValueError,TypeError,UnicodeError):continue
        if removed:self._sync_log(f"  Removed {removed} fully confirmed duplicate CSV export(s); saved progress kept",13)
        return removed

    def cleanup_collection_duplicate_export(self, session_dir):
        """Explicitly clean one unchanged, fully server-confirmed Collection CSV.

        Ignores the legacy session flag only while reconstructing the original
        exact payload. It never treats that flag or a cache notice as proof.
        """
        if self._uploading or not self.app or not self.app.loot or self._sync_stop.is_set():
            return 0
        directory=Path(session_dir)
        base=Path(self.app.loot._base).resolve()
        if (directory.is_symlink() or directory.resolve().parent!=base
                or directory.name==self._active_session_name()
                or not (directory/"collection_source.json").is_file()):return 0
        data={"networks":self._parse_csv(directory/"wardriving.csv"),
              "aircraft":self._parse_adsb(directory/"adsb_aircraft.csv"),
              "meshcore_nodes":self._merge_mesh_nodes(self._parse_meshcore(directory/"meshcore_nodes.csv"),
                                                      self._parse_meshtastic(directory/"meshtastic_nodes.csv"))}
        return self._remove_duplicate_exports(self._retention_capture(directory,data),data,
                                              account=(self._api_url,self._api_key))

    def _start_upload(self, all_sessions: bool = True):
        # Never acquire the disk/upload lock from an input callback. Selection
        # is rechecked by the same worker that prepares the UI snapshot.
        with self._sync_lock:
            if self._uploading or self._sync_upload_pending:
                self._log_add("Upload in progress...", 10)
                return
            if self._sync_stop.is_set():
                return
            self._sync_upload_pending = True
            self._sync_idle.clear()
            self._sync_upload_requested = (bool(all_sessions), self._sync_identity())
            self._log_add("Preparing upload...", 13)
            if self._ensure_sync_worker():
                self._sync_wakeup.set()

    def _upload_worker(self, sessions: list[Path]):
        import time as _time
        uploaded = total_nets = ready = local_only = reused = verified = waiting_for_gps = 0
        preparation_errors = 0
        unprocessed = 0
        preparing_session = False
        aborted = False
        total = len(sessions)
        report = self._sync_log
        batch_identity = self._sync_identity()[:4]
        upload_account = (self._api_url, self._api_key)
        def batch_current():
            return not self._sync_stop.is_set() and self._sync_identity()[:4] == batch_identity
        try:
            for i, session_dir in enumerate(sessions):
                # Release the previous session/envelope before constructing
                # the next one. Retries within this session retain their data.
                data = req = raw_response = result = retention_files = None
                if not batch_current():
                    aborted = True
                    unprocessed = total - i
                    report("  Upload account/source changed or stopped; batch left pending", 10)
                    break
                preparing_session = True
                has_mt = self._has_meshtastic(session_dir)
                legacy_done = session_dir.name in self._uploaded_sessions
                mt_done = session_dir.name in self._uploaded_meshtastic_sessions
                if legacy_done and (not has_mt or mt_done):
                    continue
                source_tokens = self._source_upload_tokens(session_dir)
                data = self._session_upload_data(session_dir)
                if not self._source_upload_unchanged(session_dir,source_tokens):
                    raise UploadReadError("Source changed while preparing upload")
                mt_count = sum(row.get("network") == "meshtastic"
                               for row in data["meshcore_nodes"])
                if has_mt:
                    stats = self._mesh_parse_stats.get("meshtastic", {})
                    report(f"[{i+1}/{total}] {session_dir.name}: MT eligible {mt_count}, "
                                  f"local no GPS {stats.get('no_gps', 0)}, invalid {stats.get('invalid', 0)}", 13)
                retention_files = self._retention_capture(session_dir, data)
                receiver_count = sum(self._mesh_parse_stats.get(protocol, {}).get("receiver_gps", 0)
                                     for protocol in ("meshcore", "meshtastic"))
                if receiver_count:
                    report(f"  Receiver GPS used for {receiver_count} mesh node(s)", 13)
                if not any(data.values()):
                    removed = self._prune_upload_logs(session_dir)
                    if any(self._mesh_parse_stats.get(protocol, {}).get("no_gps", 0)
                           for protocol in ("meshcore", "meshtastic")):
                        waiting_for_gps += 1
                        report(f"[{i+1}/{total}] {session_dir.name}: no positioned records; local cleanup checked after upload", 10)
                    else:
                        local_only += 1
                        report(f"[{i+1}/{total}] {session_dir.name}: removed {removed} empty/invalid upload log(s)", 13)
                    continue
                ready += 1
                preparing_session = False
                if not batch_current():
                    report("  Upload account/source changed; logs kept pending", 10)
                    aborted = True
                    unprocessed = total - i - 1
                    break
                cached = self._cached_receipt(data)
                if cached is not None:
                    uploaded += 1
                    reused += 1
                    report(f"[{i+1}/{total}] {session_dir.name}: previously confirmed; no resend or new credit", 11)
                    if self._source_upload_unchanged(session_dir,source_tokens):
                        self._retention_confirm_files(retention_files, data, account=upload_account)
                        self._mark_uploaded(session_dir, meshtastic=bool(mt_count))
                        self._remove_duplicate_exports(retention_files,data,account=upload_account)
                    continue
                if self._cached_coverage(data) is not None:
                    if not self._source_upload_unchanged(session_dir,source_tokens):
                        report("  Source changed; complete confirmation kept, current log pending",10)
                        continue
                    uploaded+=1
                    reused+=1
                    report(f"[{i+1}/{total}] {session_dir.name}: previously confirmed batches; no resend or new credit",11)
                    self._retention_confirm_files(retention_files,data,account=upload_account)
                    self._mark_uploaded(session_dir,meshtastic=bool(mt_count))
                    self._remove_duplicate_exports(retention_files,data,account=upload_account)
                    continue
                planned_batches=list(self._plan_upload_batches(data))
                if len(planned_batches)>1:
                    report(f"[{i+1}/{total}] {session_dir.name}: sending {len(planned_batches)} bounded batches",3)
                    outcome=self._upload_batched(data,upload_account,batch_current,spans=planned_batches)
                    if outcome["confirmed"] and batch_current() and self._source_upload_unchanged(session_dir,source_tokens):
                        uploaded+=1
                        reused+=int(outcome["reused"])
                        total_nets+=outcome["imported"]
                        self._retention_confirm_files(retention_files,data,account=upload_account)
                        self._mark_uploaded(session_dir,meshtastic=bool(mt_count))
                        self._remove_duplicate_exports(retention_files,data,account=upload_account)
                    if not batch_current():
                        aborted=True
                        unprocessed=total-i-1
                        break
                    continue
                if self._cached_mesh_verification(data):
                    verified += 1
                    report(f"[{i+1}/{total}] {session_dir.name}: previously verified on server; no resend or new credit", 11)
                    self._mark_uploaded(session_dir, meshtastic=bool(mt_count))
                    continue
                report(f"[{i+1}/{total}] {session_dir.name}: sending supported records", 3)
                rate_retried = False
                while True:
                    try:
                        if not batch_current():
                            aborted = True
                            break
                        if not self._wait_upload_slot(batch_current):
                            aborted=True
                            break
                        req = Request(upload_account[0], data=self._sign_payload(data, api_key=upload_account[1]), method="POST")
                        req.add_header("Content-Type", "application/json")
                        req.add_header("X-API-Key", upload_account[1])
                        with _open(req, timeout=90) as resp:
                            raw_response = resp.read(16 * 1024 * 1024 + 1)
                        if len(raw_response) > 16 * 1024 * 1024:
                            report("  Server response too large; left pending", 8)
                            break
                        result = json.loads(raw_response.decode())
                        del raw_response
                        if not batch_current():
                            aborted = True
                            report("  Upload account/source changed during request; logs kept pending", 10)
                            break
                        if data.get("networks") and self._reported_new_or_updated(result) and not self._remember_upload_keep(data):
                            report("  Cannot save upload outcome; source export kept pending",8)
                            break
                        confirmed, info, imported = _upload_receipt(result, data)
                        report("  " + info, 11 if confirmed else 10)
                        if confirmed:
                            # Store the complete receipt before closing a session.
                            # Active sessions remain open, but unchanged content
                            # can now be recognised safely after a restart too.
                            with self._upload_queue_lock():
                                if not batch_current():
                                    aborted = True
                                    report("  Upload account/source changed; logs kept pending", 10)
                                    break
                                if not self._remember_receipt(data, result):
                                    report("  Server confirmed; cannot save receipt; left pending", 8)
                                    break
                                self._retention_confirm_files(retention_files, data, account=upload_account)
                            self._handle_upload_badges(result)
                            total_nets += imported
                            uploaded += 1
                            if self._source_upload_unchanged(session_dir,source_tokens):
                                if not self._mark_uploaded(session_dir, meshtastic=bool(mt_count)):
                                    if session_dir.name == self._active_session_name():
                                        report("  Active session stays open for new records", 13)
                                self._remove_duplicate_exports(retention_files,data,account=upload_account)
                            else:
                                report("  Source changed; confirmed snapshot saved, current log stays pending",10)
                        else:
                            detail = self._error_detail(json.dumps(result).encode())
                            recovered = self._verify_duplicate_mesh(data, result, source_identity=batch_identity)
                            if recovered:
                                verified += 1
                                report("  Verified present in server mesh registry", 11)
                                report("  Import/ownership credit unknown; no new credit claimed", 13)
                                self._mark_uploaded(session_dir, meshtastic=bool(mt_count))
                            if not recovered and detail:
                                report("  Server: " + detail, 10)
                            if not recovered and self._retry_incomplete_ack(result,data):
                                outcome=self._upload_batched(data,upload_account,batch_current,initial_result=result)
                                if (outcome["confirmed"] and batch_current()
                                        and self._source_upload_unchanged(session_dir,source_tokens)):
                                    uploaded+=1
                                    total_nets+=outcome["imported"]
                                    reused+=int(outcome["reused"])
                                    self._retention_confirm_files(retention_files,data,account=upload_account)
                                    self._mark_uploaded(session_dir,meshtastic=bool(mt_count))
                                    self._remove_duplicate_exports(retention_files,data,account=upload_account)
                                    recovered=True
                            if not recovered and self._duplicate_cache_notice(result) and sum(self._payload_counts(data).values())>1:
                                outcome=self._upload_batched(data,upload_account,batch_current,force_split=True)
                                if (outcome["confirmed"] and batch_current()
                                        and self._source_upload_unchanged(session_dir,source_tokens)):
                                    uploaded+=1
                                    total_nets+=outcome["imported"]
                                    reused+=int(outcome["reused"])
                                    self._retention_confirm_files(retention_files,data,account=upload_account)
                                    self._mark_uploaded(session_dir,meshtastic=bool(mt_count))
                                    self._remove_duplicate_exports(retention_files,data,account=upload_account)
                                    recovered=True
                            if not recovered and self._duplicate_cache_notice(result):
                                report("  Server duplicate cache has no full receipt; awaiting confirmation", 10)
                            elif not recovered and isinstance(result, dict) and result.get("job_id") is not None:
                                report("  Server queued a job; no final receipt; left pending", 10)
                            elif not recovered:
                                report("  Incomplete acknowledgement; left pending", 10)
                        break
                    except HTTPError as exc:
                        detail = self._error_detail(exc.read(65536))
                        if detail:
                            report("  Server: " + detail, 10)
                        if exc.code == 429 and not rate_retried:
                            retry_after = exc.headers.get("Retry-After", "5") if exc.headers else "5"
                            if retry_after.isdigit() and int(retry_after) <= 60:
                                rate_retried = True
                                delay = int(retry_after)
                                report(f"  Rate limit; retry in {delay}s", 10)
                                if not self._wait_upload(delay,batch_current):
                                    aborted=True
                                    break
                                continue
                            report("  Rate limit; retry later as requested by server", 10)
                        if exc.code==413 and sum(self._payload_counts(data).values())>1:
                            outcome=self._upload_batched(data,upload_account,batch_current,force_split=True)
                            if (outcome["confirmed"] and batch_current()
                                    and self._source_upload_unchanged(session_dir,source_tokens)):
                                uploaded+=1
                                total_nets+=outcome["imported"]
                                self._retention_confirm_files(retention_files,data,account=upload_account)
                                self._mark_uploaded(session_dir,meshtastic=bool(mt_count))
                                self._remove_duplicate_exports(retention_files,data,account=upload_account)
                        report(f"  HTTP {exc.code}; " + ("smaller batches confirmed" if self._cached_coverage(data) is not None else "left pending"),8)
                        break
                    except (URLError, TimeoutError, OSError) as exc:
                        timed_out=isinstance(exc,TimeoutError) or (isinstance(exc,URLError) and isinstance(exc.reason,TimeoutError))
                        if timed_out and sum(self._payload_counts(data).values())>1:
                            report("  Request timed out; checking smaller batches",10)
                            outcome=self._upload_batched(data,upload_account,batch_current,force_split=True)
                            if (outcome["confirmed"] and batch_current()
                                    and self._source_upload_unchanged(session_dir,source_tokens)):
                                uploaded+=1
                                total_nets+=outcome["imported"]
                                self._retention_confirm_files(retention_files,data,account=upload_account)
                                self._mark_uploaded(session_dir,meshtastic=bool(mt_count))
                                self._remove_duplicate_exports(retention_files,data,account=upload_account)
                        else:
                            report("  No confirmed response; left pending",10)
                        break
                    except (ValueError, TypeError):
                        report("  Invalid server response; left pending", 8)
                        break
                if not batch_current():
                    aborted = True
                    unprocessed = total - i - 1
                    break
                if i < total - 1:
                    if not self._wait_upload(1,batch_current):
                        aborted=True
                        break
        except Exception as exc:
            aborted = True
            preparation_errors += int(preparing_session)
            unprocessed = max(0, total - i - 1) if total else 0
            report(f"  Upload error: {type(exc).__name__}; left pending", 8)
        finally:
            # Always release the running state, even if a resource failure also
            # prevents cleanup. Failed preparation never authorizes deletion.
            self._uploading = False
            if not aborted and not self._sync_stop.is_set():
                try:
                    self.cleanup_no_gps_logs()
                except (OSError, MemoryError, ValueError, TypeError):
                    report("Local no-GPS cleanup could not finish; remaining logs kept", 10)
                try:
                    self.cleanup_uploaded_logs()
                except (OSError, MemoryError, ValueError, TypeError):
                    report("Uploaded-log auto-clean could not finish; remaining logs kept", 10)
        self._last_upload_summary = dict(ready=ready, confirmed=uploaded, reused=reused,
                                         verified=verified, local_only=local_only,
                                         pending=ready-uploaded-verified+preparation_errors+unprocessed,
                                         unprocessed=unprocessed,
                                         preparation_errors=preparation_errors,
                                         waiting_for_gps=waiting_for_gps, new_networks=total_nets)
        report(f"Done: {uploaded} receipts, {verified} server-verified, {local_only} empty/invalid", 11 if not aborted and uploaded+verified == ready else 10)
        report(f"  {ready-uploaded-verified+preparation_errors+unprocessed} pending; {ready} uploadable session(s)", 13)
        if preparation_errors:
            report(f"  {preparation_errors} session(s) could not be read completely; logs kept", 10)
        report(f"  Reused receipts {reused}; +{total_nets} new WiFi/BLE networks", 13)
        if uploaded > reused:
            self._badge_sync_worker()

    def _sign_payload(self, data: dict, *, api_key=None) -> bytes:
        """Sign payload with HMAC-SHA256. Server verifies before processing."""
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_hex(8)
        data_b64 = base64.b64encode(raw)
        del raw
        signature = hmac.new(
            (self._api_key if api_key is None else api_key).encode("utf-8"),
            digestmod=hashlib.sha256)
        signature.update(nonce.encode("ascii"))
        signature.update(data_b64)
        # All three values are ASCII without JSON escapes. Join once instead
        # of retaining a Base64 str plus concatenated/encoded HMAC copies and
        # a second full JSON str. Bytes match the existing json.dumps envelope.
        return b"".join((b'{"data": "', data_b64, b'", "nonce": "',
                         nonce.encode("ascii"), b'", "sig": "',
                         signature.hexdigest().encode("ascii"), b'"}'))

    # ------------------------------------------------------------------
    # Badge sync
    # ------------------------------------------------------------------
    def _get_game_badges(self) -> list[str]:
        """Get current game badges mapped to server IDs."""
        if not self.app or not self._callbacks_allowed():
            return []
        server_badges = []
        for game_id in self.app._badges:
            srv = BADGE_GAME_TO_SERVER.get(game_id)
            if srv:
                server_badges.append(srv)
        return server_badges

    def _apply_server_badges(self, server_badges: list[str]) -> list[str]:
        """Import server badges into game. Returns list of newly earned game badges."""
        if not self.app or not self._callbacks_allowed():
            return []
        new = []
        for srv_id in server_badges:
            if not self._callbacks_allowed():
                break
            game_id = BADGE_SERVER_TO_GAME.get(srv_id)
            if game_id and game_id not in self.app._badges:
                self.app._earn_badge(game_id)
                new.append(game_id)
        return new

    def _start_badge_sync(self):
        if not self._api_url or not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        self._log_add("Syncing badges...", 3)
        self._start_background_worker(self._badge_sync_worker,
                                      name="wdg-sync-badges")

    def _badge_sync_worker(self):
        if not self._callbacks_allowed():
            return
        try:
            api_base = self._api_url.rsplit("/api/", 1)[0]
            url = f"{api_base}/api/badges/"
            game_badges = self._get_game_badges()
            payload = json.dumps({"badges": game_badges}).encode()
            req = Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-API-Key", self._api_key)
            with _open(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            if not self._callbacks_allowed():
                return
            all_badges = result.get("badges", [])
            game_imported = result.get("game_imported", 0)
            server_awarded = result.get("server_awarded", [])
            new_game = self._apply_server_badges(all_badges)
            parts = []
            if game_imported:
                parts.append(f"{game_imported} pushed")
            if server_awarded:
                parts.append(f"{len(server_awarded)} from server")
            if new_game:
                parts.append(f"{len(new_game)} new in game")
                for b in new_game:
                    self.msg(f"[BADGE] Unlocked: {b.upper().replace('_', ' ')}", 11)
            total = len(all_badges)
            info = ", ".join(parts) if parts else "in sync"
            self._log_add(f"Badges: {total} total — {info}", 11)
        except HTTPError as e:
            self._log_add(f"Badge sync: HTTP {e.code}", 8)
        except Exception as e:
            self._log_add(f"Badge sync: {e}", 8)

    def _handle_upload_badges(self, result: dict):
        """Process new_badges from upload response."""
        new_badges = result.get("new_badges", [])
        if not new_badges:
            return
        new_game = self._apply_server_badges(new_badges)
        for b in new_game:
            self.msg(f"[BADGE] Unlocked: {b.upper().replace('_', ' ')}", 11)
        if new_game:
            self._log_add(f"  +{len(new_game)} badge(s) from server!", 11)

    # ------------------------------------------------------------------
    # /api/me — fetch user identity, stats and badges from server
    # ------------------------------------------------------------------
    def _api_me_url(self) -> str:
        if not self._api_url:
            return ""
        api_base = self._api_url.rsplit("/api/", 1)[0]
        return f"{api_base}/api/me"

    def _validate_and_apply_user(self):
        """Background worker — fetch /api/me after key save and apply identity."""
        if not self._callbacks_allowed():
            return
        info = self.fetch_user_info()
        if info and self._callbacks_allowed():
            self._on_user_info(info, sync_lora_name=True)

    def _show_profile(self):
        """Menu action — refresh /api/me on demand and show stats in log."""
        if not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        self._log_add("Fetching profile from server...", 3)
        self._start_background_worker(
            lambda: self._on_user_info(
                self.fetch_user_info() or {}, sync_lora_name=False),
            name="wdg-sync-profile")

    def fetch_user_info(self) -> dict | None:
        """GET /api/me — returns dict with username, stats, badges, gang.
        Returns None on any error. Logs status to plugin log."""
        if not self._api_url or not self._api_key or not self._callbacks_allowed():
            return None
        try:
            req = Request(self._api_me_url())
            req.add_header("X-API-Key", self._api_key)
            with _open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if not self._callbacks_allowed():
                return None
            if not data.get("ok"):
                err = data.get("error", "unknown error")
                self._log_add(f"  /api/me: {err}", 8)
                return None
            return data
        except HTTPError as e:
            if e.code == 401:
                self._log_add("  Invalid API key (401)", 8)
            else:
                self._log_add(f"  /api/me: HTTP {e.code}", 8)
            return None
        except URLError as e:
            self._log_add(f"  /api/me: {e.reason}", 8)
            return None
        except Exception as e:
            self._log_add(f"  /api/me: {e}", 8)
            return None

    def _on_user_info(self, info: dict, *, sync_lora_name: bool = True):
        """Apply user info from /api/me — show stats, sync badges, update LoRa name."""
        if not self._callbacks_allowed() or not info or not info.get("username"):
            return  # fetch failed; error already logged
        username = info.get("username", "")
        gang = info.get("gang") or "(no gang)"
        wifi = info.get("wifi", 0)
        ble = info.get("ble", 0)
        ac = info.get("aircraft", 0)
        mesh = info.get("mesh", 0)
        cracked = info.get("cracked", 0)
        total = info.get("total", 0)
        badges = info.get("badges", [])

        self._username = username
        self._user_gang = gang
        self._user_stats = {
            "wifi": wifi, "ble": ble, "aircraft": ac, "mesh": mesh,
            "cracked": cracked, "total": total,
        }

        self._log_add(f"Welcome, {username}!", 11)
        self._log_add(f"  Gang: {gang}", 13)
        self._log_add(f"  Stats: {wifi} wifi, {ble} ble, {ac} ac, {mesh} mesh", 13)
        if cracked:
            self._log_add(f"  Cracked passwords: {cracked}", 13)
        self._log_add(f"  Total records on server: {total}", 13)
        self._log_add(f"  Badges on server: {len(badges)}", 13)

        # Sync badges from /api/me payload
        if badges and self.app and self._callbacks_allowed():
            new_game = self._apply_server_badges(badges)
            if new_game:
                self._log_add(f"  +{len(new_game)} badge(s) imported from server", 11)
                for b in new_game:
                    self.msg(
                        f"[BADGE] Unlocked: {b.upper().replace('_', ' ')}", 11)

        # Sync LoRa node name to portal username (with WDG_ prefix for clarity)
        if sync_lora_name and username and self.app and self._callbacks_allowed():
            target_name = f"WDG_{username}"
            current = getattr(self.app, "_mc_node_name", "")
            # Only auto-update if user still has the auto-generated name
            # (starts with "WatchDogs_") or matches an old WDG_ prefix
            if current.startswith("WatchDogs_") or current.startswith("WDG_"):
                if current != target_name:
                    self.app._mc_node_name = target_name
                    try:
                        from watchdogs.lora_manager import save_meshcore_config
                        save_meshcore_config(
                            target_name,
                            getattr(self.app, "_mc_channels_list", []))
                        self._log_add(
                            f"  LoRa node name -> {target_name}", 11)
                        self.msg(
                            f"[LoRa] Identity synced: {target_name}", 11)
                    except Exception as e:
                        self._log_add(f"  LoRa name save error: {e}", 8)
            else:
                # User picked a custom name — don't override silently
                self._log_add(
                    f"  LoRa node kept as '{current}' (custom)", 13)

    # ------------------------------------------------------------------
    # API test
    # ------------------------------------------------------------------
    def _test_api(self):
        if not self._api_url:
            self._log_add("Set server URL first!", 8)
            return
        if not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        self._log_add("Testing API...", 3)
        self._start_background_worker(self._test_worker,
                                      name="wdg-sync-api-test")

    def _test_worker(self):
        if not self._callbacks_allowed():
            return
        try:
            payload = self._sign_payload({"networks": []})
            req = Request(self._api_url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-API-Key", self._api_key)
            with _open(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                total = result.get("total", "?")
                self._log_add(f"API OK — {total} records on server", 11)
        except HTTPError as e:
            if e.code == 401:
                self._log_add("API: Invalid key (401)", 8)
            elif e.code == 403:
                self._log_add("API: Invalid signature (403)", 8)
            else:
                self._log_add(f"API: HTTP {e.code}", 8)
        except URLError as e:
            self._log_add(f"API: {e.reason}", 8)
        except Exception as e:
            self._log_add(f"API: {e}", 8)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _state_file(self) -> Path:
        return Path(__file__).parent / ".wardrive_state.json"

    def _load_state(self):
        # Load uploaded sessions
        p = self._state_file()
        if p.is_file():
            try:
                data = json.loads(p.read_text())
                self._uploaded_sessions = set(data.get("uploaded", []))
                self._uploaded_meshtastic_sessions = set(data.get("uploaded_meshtastic", []))
                receipts = data.get("receipts", {})
                if isinstance(receipts, dict):
                    self._upload_receipts = {k:v for k,v in receipts.items()
                        if isinstance(k, str) and re.fullmatch(r"[a-f0-9]{64}", k) and isinstance(v, dict)}
                keep=data.get("keep_exports",[])
                if isinstance(keep,list):
                    self._upload_keep_exports={key for key in keep if isinstance(key,str) and re.fullmatch(r"[0-9a-f]{64}",key)}
                coverage=data.get("coverage",{})
                if isinstance(coverage,dict):
                    self._upload_coverage={k:v for k,v in coverage.items()
                        if isinstance(k,str) and re.fullmatch(r"[a-f0-9]{64}",k) and self._valid_coverage(v)}
            except Exception:
                pass
        # Load config from secrets.conf
        conf = _load_secrets_conf()
        self._api_key = conf.get("WARDRIVE_API_KEY", "")
        self._api_url = conf.get("WARDRIVE_API_URL", "") or _default_endpoint()

    def _save_state(self):
        temporary = None
        try:
            path = self._state_file()
            previous = path.stat() if path.exists() else None
            descriptor, temporary = tempfile.mkstemp(prefix=".wardrive-state-", dir=path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({
                "uploaded": sorted(self._uploaded_sessions),
                "uploaded_meshtastic": sorted(self._uploaded_meshtastic_sessions),
                "receipts": self._upload_receipts,
                "coverage": getattr(self,"_upload_coverage",{}),
                "keep_exports": sorted(getattr(self,"_upload_keep_exports",set())),
                }, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if previous is not None and hasattr(os, "chown"):
                os.chown(temporary, previous.st_uid, previous.st_gid)
            os.replace(temporary, path)
            if os.name != "nt":
                directory=os.open(path.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
                try:os.fsync(directory)
                finally:os.close(directory)
            return True
        except Exception:
            log.warning("Sync: upload state could not be saved")
            return False
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except (OSError, MemoryError):
                    log.warning("Sync: unused temporary state file could not be removed")

    def _save_secret(self, key: str, value: str):
        """Save a key=value to secrets.conf."""
        secrets_path = Path(__file__).parent.parent / "secrets.conf"
        lines = []
        found = False
        if secrets_path.is_file():
            lines = secrets_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        secrets_path.write_text("\n".join(lines) + "\n")

    def _log_add(self, text: str, color: int = 13):
        self._log.append((text, color))
        if len(self._log) > 100:
            self._log = self._log[-100:]
        if self.app and self._callbacks_allowed():
            self.app._term_add(f"[Sync] {text}", raw=True)

    # ------------------------------------------------------------------
    # Auth push — game as authenticator
    # ------------------------------------------------------------------
    def _start_auth_poll(self):
        """Start background polling for auth requests from web."""
        with self._background_lock:
            if (self._auth_polling or not self._api_key or not self._api_url
                    or not self._callbacks_allowed()):
                return
            self._auth_polling = True
            self._auth_poll_thread = threading.Thread(
                target=self._auth_poll_worker, name="wdg-sync-auth", daemon=True)
            try:
                self._auth_poll_thread.start()
            except BaseException:
                self._auth_polling = False
                self._auth_poll_thread = None
                raise

    def _auth_poll_worker(self):
        """Poll server for pending auth requests every 5 seconds."""
        import time as _time
        api_base = self._api_url.rsplit("/api/", 1)[0]
        poll_url = f"{api_base}/api/auth/pending/"

        while self._auth_polling and WardriveUpload._callbacks_allowed(self):
            try:
                req = Request(poll_url)
                req.add_header("X-API-Key", self._api_key)
                with _open(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    if not self._auth_polling or not WardriveUpload._callbacks_allowed(self):
                        break
                    if data.get("pending"):
                        token = data.get("token", "")
                        if token == self._auth_dismissed_token:
                            continue
                        pin = data["pin"]
                        expires = data.get("expires", 60)
                        self._auth_pin = str(pin)
                        self._auth_pin_expiry = _time.time() + min(expires, 20)
                        self._auth_current_token = token
                        self._log_add(f"LOGIN PIN: {pin}", 10)
                        if self.app:
                            self.app.msg(f"[AUTH] Login PIN: {pin}", 10)
            except Exception:
                pass
            finally:
                # Ignoring an already dismissed PIN must still respect the
                # polling interval instead of immediately querying again.
                stop = getattr(self, "_sync_stop", None)
                if stop is None:
                    _time.sleep(5)
                else:
                    stop.wait(5)

    def _auth_pin_active(self) -> bool:
        """Check if there's an active PIN to display."""
        import time as _time
        if self._auth_pin and _time.time() < self._auth_pin_expiry:
            return True
        if self._auth_pin:
            self._auth_pin = ""
        return False

    def _draw_auth_pin(self, w: int = 640, h: int = 360):
        """Draw large PIN popup centered on screen."""
        import pyxel
        import time as _time

        remaining = int(self._auth_pin_expiry - _time.time())
        cx, cy = w // 2, h // 2
        bw, bh = 280, 90
        x0 = cx - bw // 2
        y0 = cy - bh // 2
        bf = self.app._big_font if self.app else None
        # Background
        pyxel.rect(x0, y0, bw, bh, 0)
        pyxel.rectb(x0, y0, bw, bh, 10)
        pyxel.rectb(x0 + 2, y0 + 2, bw - 4, bh - 4, 3)
        # Title
        pyxel.text(cx - 40, y0 + 6, "WEB LOGIN REQUEST", 10)
        pyxel.line(x0 + 8, y0 + 16, x0 + bw - 8, y0 + 16, 3)
        # PIN digits in big font
        pin_str = self._auth_pin
        if bf:
            char_w = 12
            total = len(pin_str) * char_w
            px = cx - total // 2
            for ch in pin_str:
                pyxel.text(px, y0 + 26, ch, 7, bf)
                px += char_w
        else:
            px = cx - len(pin_str) * 6
            for ch in pin_str:
                pyxel.text(px, y0 + 28, ch, 7)
                px += 12
        # Progress bar
        bar_w = bw - 40
        bar_x = x0 + 20
        bar_y = y0 + bh - 24
        pyxel.rect(bar_x, bar_y, bar_w, 4, 1)
        fill = max(0, int(bar_w * remaining / 20))
        tc = 11 if remaining > 10 else (10 if remaining > 5 else 8)
        pyxel.rect(bar_x, bar_y, fill, 4, tc)
        # Hint
        pyxel.text(cx - 55, y0 + bh - 14, f"Expires in {remaining}s  [ESC] dismiss", 13)
