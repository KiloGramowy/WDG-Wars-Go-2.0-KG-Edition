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
        self._receipt_generation = 0
        self._upload_log_checks: dict[str, tuple] = {}
        self._pending_log_checks: dict[str, tuple] = {}
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
        self._load_state()

    def menu_items(self) -> list[PluginMenuItem]:
        return [
            PluginMenuItem("w", "Watch Dogs Go Wars Sync", "open_overlay"),
        ]

    def on_load(self, app) -> None:
        super().on_load(app)
        self._start_auth_poll()
        # If a key is already configured, fetch identity in background
        # so badges sync and LoRa name updates without user interaction.
        if self._api_key and self._api_url:
            threading.Thread(
                target=self._validate_and_apply_user,
                daemon=True).start()

    def on_unload(self) -> None:
        self._auth_polling = False

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
        if not self._api_key:
            self._log_add("Set your API key to start syncing", 10)
            self._log_add("  Register on the community server to get your key", 13)
        else:
            self._log_add("Connected to server", 11)
        n = len(self._pending_sessions())
        if n:
            self._log_add(f"{n} session(s) ready to upload", 11)
        else:
            self._log_add("No pending uploads", 13)
        self._show_cleanup_count()
        if getattr(self, "_mesh_gps_wait", 0):
            self._log_add(f"{self._mesh_gps_wait} mesh log(s) waiting for receiver GPS", 10)

    def on_update(self) -> None:
        if self.app and time.monotonic() >= self._next_cleanup:
            self._next_cleanup = time.monotonic() + 2.0
            self._pending_sessions()
        if not self._overlay_active:
            return
        import pyxel

        # Key input mode
        if self._entering_key:
            for c in "0123456789abcdef":
                if pyxel.btnp(getattr(pyxel, f"KEY_{c.upper()}")):
                    self._key_input += c
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._key_input:
                self._key_input = self._key_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and len(self._key_input) >= 16:
                self._api_key = self._key_input
                self._save_secret("WARDRIVE_API_KEY", self._api_key)
                self._log_add(f"API key saved ({len(self._api_key)} chars)", 11)
                self._entering_key = False
                self._key_input = ""
                self._start_auth_poll()
                # Validate the new key against /api/me and pull identity/badges
                self._log_add("Validating key with server...", 3)
                threading.Thread(
                    target=self._validate_and_apply_user,
                    daemon=True).start()
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

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    # ------------------------------------------------------------------
    def _overlay_items(self) -> list[tuple[str, str]]:
        n = len(self._pending_sessions())
        items = [
            ("upload_all", f"Upload All ({n} pending)"),
            ("upload_latest", "Upload Latest Session"),
            ("show_profile", "Show My Profile"),
            ("sync_badges", "Sync Badges"),
            ("set_key", "Set API Key"),
            ("test_api", "Test API Connection"),
            ("show_pending", "Show Pending Sessions"),
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
            pending = self._pending_sessions()
            for s in pending:
                self._log_add(f"  {s.name}", 13)
            if not pending: self._log_add("No pending uploads", 13)
            self._show_cleanup_count()
        elif action == "reset":
            previous = (self._uploaded_sessions.copy(), self._uploaded_meshtastic_sessions.copy(), self._upload_receipts.copy())
            self._uploaded_sessions.clear()
            self._uploaded_meshtastic_sessions.clear()
            self._upload_receipts.clear()
            if self._save_state() is False:
                self._uploaded_sessions, self._uploaded_meshtastic_sessions, self._upload_receipts = previous
                self._log_add("Cannot save history reset; history kept", 8)
            else:
                self._log_add("Upload history cleared", 10)

    # ------------------------------------------------------------------
    # Upload via API
    # ------------------------------------------------------------------
    def _pending_sessions(self) -> list[Path]:
        if not self.app or not self.app.loot:
            return []
        base = Path(self.app.loot._base)
        pending = []
        self._mesh_gps_wait = 0
        for d in sorted(base.iterdir()):
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
        """Delete unuploadable queue exports, as explicitly requested by user.

        Only four exact CSV names are eligible, only in the loot directory and
        only for unconfirmed groups. Never delete a session directory, contacts,
        XP, messages or other loot. The mesh writer uses the same DB lock;
        revalidate after acquiring it so a newly received position is retained.
        Valid exports are checked once per file version, not once per frame.
        """
        if not self.app or not self.app.loot: return 0
        session_dir = Path(session_dir)
        base = Path(self.app.loot._base).resolve()
        if session_dir.is_symlink() or not session_dir.resolve().is_relative_to(base): return 0
        removed = 0
        parsers = (("wardriving.csv", self._parse_csv, False),
                   ("adsb_aircraft.csv", self._parse_adsb, False),
                   ("meshcore_nodes.csv", self._parse_meshcore, False),
                   ("meshtastic_nodes.csv", self._parse_meshtastic, True))
        lock = getattr(self.app.loot, "_db_lock", None)
        with lock if lock is not None else nullcontext():
            for filename, parse, is_mt in parsers:
                done = self._uploaded_meshtastic_sessions if is_mt else self._uploaded_sessions
                if session_dir.name in done: continue
                # WiFi/BLE writes run on the main event queue without the mesh
                # DB lock. Their active export is only pruned on that same
                # thread; a background upload worker leaves it for the next
                # main-loop cleanup tick.
                if (filename in ("wardriving.csv", "adsb_aircraft.csv")
                        and session_dir.name == self._active_session_name()
                        and threading.current_thread() is not threading.main_thread()):
                    continue
                path = session_dir / filename
                if path.is_symlink() or not path.is_file(): continue
                try:
                    before = path.stat()
                    stamp = (before.st_ino, before.st_mtime_ns, before.st_size)
                    if self._upload_log_checks.get(str(path)) == stamp: continue
                    # Readability failures are not evidence of invalid data.
                    with path.open('rb') as stream: stream.read(1)
                    valid = parse(path)
                    after = path.stat()
                    if stamp != (after.st_ino, after.st_mtime_ns, after.st_size): continue
                    if valid:
                        self._upload_log_checks[str(path)] = stamp
                    elif filename in ("meshcore_nodes.csv", "meshtastic_nodes.csv") and self._mesh_parse_stats.get("meshtastic" if is_mt else "meshcore", {}).get("no_gps", 0):
                        # A real node awaiting the receiver's fix is recoverable,
                        # not a corrupt log. The GPS assignment path regenerates
                        # its export when a fresh measured fix becomes available.
                        self._mesh_gps_wait = getattr(self, "_mesh_gps_wait", 0) + 1
                    else:
                        path.unlink()
                        self._upload_log_checks.pop(str(path), None)
                        removed += 1
                        log.info("Sync: removed empty/invalid upload log %s/%s", session_dir.name, filename)
                except OSError:
                    log.warning("Sync: upload log cleanup failed for %s/%s", session_dir.name, filename)
        self._cleanup_removed += removed
        return removed

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
        self._uploaded_sessions.add(session_dir.name)
        if meshtastic:
            self._uploaded_meshtastic_sessions.add(session_dir.name)
        if self._save_state() is False:
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
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        message = "wdg-upload-receipt-v1\0" + self._api_url + "\0" + canonical
        return hmac.new(self._api_key.encode(), message.encode(), hashlib.sha256).hexdigest()

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
        if self._save_state() is False:
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

    def _verify_duplicate_mesh(self, data: dict, result: dict) -> bool:
        """Recover presence, not ownership/credit, after the server's cache reply.

        The documented authenticated registry exposes both protocols. Require
        every submitted mesh node to match ID, protocol, name, role, position
        and RSSI exactly. This is a separate kind of evidence, never a made-up
        import/known counter. A mixed batch or incomplete/mismatched registry
        remains pending. The registry does not prove timestamp or ownership.
        """
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
            req = Request(self._mesh_registry_url())
            req.add_header("X-API-Key", self._api_key)
            with _open(req, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
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
            return self._store_receipt(data, dict(verification="mesh_registry",
                source=self._mesh_registry_url(), matched=len(data["meshcore_nodes"])))
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
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                # Skip WiGLE pre-header line
                first = f.readline()
                if not first:
                    return []
                reader = _csv.reader(f)
                header = next(reader, None)
                if not header:
                    return []
                for parts in reader:
                    if len(parts) < 11:
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
            self._log_add(f"CSV parse error: {e}", 8)
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
            self._log_add(f"ADS-B parse error: {e}", 8)
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
            self._log_add(f"{network} parse error: {e}", 8)
        stats["eligible"] = len(nodes)
        return list(nodes.values())

    @staticmethod
    def _merge_mesh_nodes(*groups: list[dict]) -> list[dict]:
        nodes = {}
        for group in groups:
            for node in group:
                nodes.setdefault((node["network"], node["node_id"]), node)
        return list(nodes.values())

    def _start_upload(self, all_sessions: bool = True):
        if not self._api_url:
            self._log_add("Set server URL first!", 8)
            return
        if not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        if self._uploading:
            self._log_add("Upload in progress...", 10)
            return
        sessions = self._pending_sessions()
        self._show_cleanup_count()
        if not all_sessions and sessions:
            sessions = sessions[-1:]
        if not sessions:
            self._log_add("Nothing to upload", 10)
            return
        self._uploading = True
        threading.Thread(
            target=self._upload_worker, args=(sessions,),
            daemon=True).start()

    def _upload_worker(self, sessions: list[Path]):
        import time as _time
        uploaded = total_nets = ready = local_only = reused = verified = waiting_for_gps = 0
        total = len(sessions)
        report = self._sync_log
        try:
            for i, session_dir in enumerate(sessions):
                has_mt = self._has_meshtastic(session_dir)
                legacy_done = session_dir.name in self._uploaded_sessions
                mt_done = session_dir.name in self._uploaded_meshtastic_sessions
                if legacy_done and (not has_mt or mt_done):
                    continue
                mt_nodes = self._parse_meshtastic(session_dir / "meshtastic_nodes.csv") if not mt_done else []
                if has_mt:
                    stats = self._mesh_parse_stats.get("meshtastic", {})
                    report(f"[{i+1}/{total}] {session_dir.name}: MT eligible {len(mt_nodes)}, "
                                  f"local no GPS {stats.get('no_gps', 0)}, invalid {stats.get('invalid', 0)}", 13)
                data = self._session_upload_data(session_dir)
                receiver_count = sum(self._mesh_parse_stats.get(protocol, {}).get("receiver_gps", 0)
                                     for protocol in ("meshcore", "meshtastic"))
                if receiver_count:
                    report(f"  Receiver GPS used for {receiver_count} mesh node(s)", 13)
                if not any(data.values()):
                    removed = self._prune_upload_logs(session_dir)
                    if any(self._mesh_parse_stats.get(protocol, {}).get("no_gps", 0)
                           for protocol in ("meshcore", "meshtastic")):
                        waiting_for_gps += 1
                        report(f"[{i+1}/{total}] {session_dir.name}: waiting for receiver GPS; mesh data retained", 10)
                    else:
                        local_only += 1
                        report(f"[{i+1}/{total}] {session_dir.name}: removed {removed} empty/invalid upload log(s)", 13)
                    continue
                ready += 1
                cached = self._cached_receipt(data)
                if cached is not None:
                    uploaded += 1
                    reused += 1
                    report(f"[{i+1}/{total}] {session_dir.name}: previously confirmed; no resend or new credit", 11)
                    self._mark_uploaded(session_dir, meshtastic=bool(mt_nodes))
                    continue
                if self._cached_mesh_verification(data):
                    verified += 1
                    report(f"[{i+1}/{total}] {session_dir.name}: previously verified on server; no resend or new credit", 11)
                    self._mark_uploaded(session_dir, meshtastic=bool(mt_nodes))
                    continue
                report(f"[{i+1}/{total}] {session_dir.name}: sending supported records", 3)
                rate_retried = False
                while True:
                    try:
                        req = Request(self._api_url, data=self._sign_payload(data), method="POST")
                        req.add_header("Content-Type", "application/json")
                        req.add_header("X-API-Key", self._api_key)
                        with _open(req, timeout=90) as resp:
                            result = json.loads(resp.read().decode())
                        confirmed, info, imported = _upload_receipt(result, data)
                        report("  " + info, 11 if confirmed else 10)
                        if confirmed:
                            # Store the complete receipt before closing a session.
                            # Active sessions remain open, but unchanged content
                            # can now be recognised safely after a restart too.
                            if not self._remember_receipt(data, result):
                                report("  Server confirmed; cannot save receipt; left pending", 8)
                                break
                            self._handle_upload_badges(result)
                            total_nets += imported
                            uploaded += 1
                            if not self._mark_uploaded(session_dir, meshtastic=bool(mt_nodes)):
                                if session_dir.name == self._active_session_name():
                                    report("  Active session stays open for new records", 13)
                        else:
                            detail = self._error_detail(json.dumps(result).encode())
                            recovered = self._verify_duplicate_mesh(data, result)
                            if recovered:
                                verified += 1
                                report("  Verified present in server mesh registry", 11)
                                report("  Import/ownership credit unknown; no new credit claimed", 13)
                                self._mark_uploaded(session_dir, meshtastic=bool(mt_nodes))
                            if not recovered and detail:
                                report("  Server: " + detail, 10)
                            if not recovered and isinstance(result, dict) and result.get("ok") is True and "already uploaded" in str(result.get("info", "")).lower():
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
                                _time.sleep(delay)
                                continue
                            report("  Rate limit; retry later as requested by server", 10)
                        # Resending an unchanged oversized payload cannot fix 413.
                        report(f"  HTTP {exc.code}; left pending", 8)
                        break
                    except (URLError, TimeoutError, OSError):
                        report("  No confirmed response; left pending", 10)
                        break
                    except (ValueError, TypeError):
                        report("  Invalid server response; left pending", 8)
                        break
                if i < total - 1:
                    _time.sleep(1)
        except Exception as exc:
            report(f"  Upload error: {type(exc).__name__}; left pending", 8)
        finally:
            self._uploading = False
        self._last_upload_summary = dict(ready=ready, confirmed=uploaded, reused=reused,
                                         verified=verified, local_only=local_only, pending=ready-uploaded-verified,
                                         waiting_for_gps=waiting_for_gps, new_networks=total_nets)
        report(f"Done: {uploaded} receipts, {verified} server-verified, {local_only} empty/invalid", 11 if uploaded+verified == ready else 10)
        report(f"  {ready-uploaded-verified} pending; {ready} uploadable session(s)", 13)
        report(f"  Reused receipts {reused}; +{total_nets} new WiFi/BLE networks", 13)
        if uploaded > reused:
            self._badge_sync_worker()

    def _sign_payload(self, data: dict) -> bytes:
        """Sign payload with HMAC-SHA256. Server verifies before processing."""
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_hex(8)
        data_b64 = base64.b64encode(raw).decode("ascii")
        sig = hmac.new(
            self._api_key.encode("utf-8"),
            (nonce + data_b64).encode("utf-8"),
            hashlib.sha256).hexdigest()
        return json.dumps({
            "data": data_b64, "nonce": nonce, "sig": sig,
        }).encode("utf-8")

    # ------------------------------------------------------------------
    # Badge sync
    # ------------------------------------------------------------------
    def _get_game_badges(self) -> list[str]:
        """Get current game badges mapped to server IDs."""
        if not self.app:
            return []
        server_badges = []
        for game_id in self.app._badges:
            srv = BADGE_GAME_TO_SERVER.get(game_id)
            if srv:
                server_badges.append(srv)
        return server_badges

    def _apply_server_badges(self, server_badges: list[str]) -> list[str]:
        """Import server badges into game. Returns list of newly earned game badges."""
        if not self.app:
            return []
        new = []
        for srv_id in server_badges:
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
        threading.Thread(target=self._badge_sync_worker, daemon=True).start()

    def _badge_sync_worker(self):
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
        info = self.fetch_user_info()
        if info:
            self._on_user_info(info, sync_lora_name=True)

    def _show_profile(self):
        """Menu action — refresh /api/me on demand and show stats in log."""
        if not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        self._log_add("Fetching profile from server...", 3)
        threading.Thread(
            target=lambda: self._on_user_info(
                self.fetch_user_info() or {}, sync_lora_name=False),
            daemon=True).start()

    def fetch_user_info(self) -> dict | None:
        """GET /api/me — returns dict with username, stats, badges, gang.
        Returns None on any error. Logs status to plugin log."""
        if not self._api_url or not self._api_key:
            return None
        try:
            req = Request(self._api_me_url())
            req.add_header("X-API-Key", self._api_key)
            with _open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
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
        if not info or not info.get("username"):
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
        if badges and self.app:
            new_game = self._apply_server_badges(badges)
            if new_game:
                self._log_add(f"  +{len(new_game)} badge(s) imported from server", 11)
                for b in new_game:
                    self.msg(
                        f"[BADGE] Unlocked: {b.upper().replace('_', ' ')}", 11)

        # Sync LoRa node name to portal username (with WDG_ prefix for clarity)
        if sync_lora_name and username and self.app:
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
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
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
                }, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if previous is not None and hasattr(os, "chown"):
                os.chown(temporary, previous.st_uid, previous.st_gid)
            os.replace(temporary, path)
            return True
        except Exception:
            log.warning("Sync: upload state could not be saved")
            return False
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

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
        if self.app:
            self.app._term_add(f"[Sync] {text}", raw=True)

    # ------------------------------------------------------------------
    # Auth push — game as authenticator
    # ------------------------------------------------------------------
    def _start_auth_poll(self):
        """Start background polling for auth requests from web."""
        if self._auth_polling or not self._api_key or not self._api_url:
            return
        self._auth_polling = True
        self._auth_poll_thread = threading.Thread(
            target=self._auth_poll_worker, daemon=True)
        self._auth_poll_thread.start()

    def _auth_poll_worker(self):
        """Poll server for pending auth requests every 5 seconds."""
        import time as _time
        api_base = self._api_url.rsplit("/api/", 1)[0]
        poll_url = f"{api_base}/api/auth/pending/"

        while self._auth_polling:
            try:
                req = Request(poll_url)
                req.add_header("X-API-Key", self._api_key)
                with _open(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
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
            _time.sleep(5)

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
