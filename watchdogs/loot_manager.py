"""Loot manager — persists all captured data to disk.

Session directory layout:
    <app_dir>/loot/<YYYY-MM-DD_HH-MM-SS>/
        serial_full.log          – every line from ESP32 (timestamped)
        scan_results.csv         – networks found during scan
        sniffer_aps.csv          – access points from sniffer
        sniffer_probes.csv       – probe requests captured
        handshakes/
            <ssid>_<bssid>.txt   – handshake metadata from serial
            <ssid>_<bssid>.pcap  – real pcap (from start_handshake_serial)
            <ssid>_<bssid>.hccapx – hashcat format (from start_handshake_serial)
            <ssid>_<bssid>.22000 – hc22000 hash (auto-generated from complete hccapx)
        portal_passwords.log     – portal form submissions
        evil_twin_capture.log    – evil twin captured data
        attacks.log              – attack start/stop events
"""

import base64
import csv
import copy
import hashlib
import io
import json
import logging
import os
import math
import re
import threading
import time
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .app_state import AppState, Network, SnifferAP, ProbeEntry
from .mesh_rules import mesh_contact_xp
from .mesh_position import mesh_position, receiver_observation

log = logging.getLogger(__name__)


class LootDataError(RuntimeError):
    """Player data could not be opened safely; do not start with zero progress."""


def _fsync_file(fh) -> None:
    """Flush Python buffer + OS page cache to physical disk.

    Without this, `with open(...).write(...)` only pushes bytes down to the
    OS cache. If the uConsole battery dies before Linux lazily writes that
    cache back to the eMMC/SD, the data is gone. fsync makes the write
    survive a hard power cut. Costs ~5–20 ms on SD — cheap per event,
    expensive per frame, so call it only from discrete save-on-event paths.
    """
    try:
        fh.flush()
        os.fsync(fh.fileno())
    except OSError:
        pass


def _sync_append(path: Path, text: str, encoding: str = "utf-8",
                 newline: Optional[str] = None) -> None:
    """Append text to path and fsync. Silently ignores I/O errors."""
    try:
        kwargs = {"encoding": encoding}
        if newline is not None:
            kwargs["newline"] = newline
        with open(path, "a", **kwargs) as fh:
            fh.write(text)
            _fsync_file(fh)
    except OSError:
        pass


def _sync_write(path: Path, text: str, encoding: str = "utf-8",
                newline: Optional[str] = None) -> None:
    """Write text to path and fsync. Silently ignores I/O errors."""
    try:
        kwargs = {"encoding": encoding}
        if newline is not None:
            kwargs["newline"] = newline
        with open(path, "w", **kwargs) as fh:
            fh.write(text)
            _fsync_file(fh)
    except OSError:
        pass


class LootManager:
    """Manages a per-session loot directory and auto-saves captured data."""

    @property
    def session_dir(self) -> str:
        return str(self._session)

    def __init__(self, app_dir: str, gps_manager=None) -> None:
        self._db_lock = threading.RLock()
        self._gps = gps_manager  # Optional GpsManager for geo-tagging
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._base = Path(app_dir) / "loot"
        self._session = self._base / ts
        self._serial_fh: Optional[io.TextIOWrapper] = None
        self._handshake_dir: Optional[Path] = None
        self._session_active = False
        # Monotonic CSV persistence failures, distinct from ordinary dedup skips.
        self.wardrive_save_errors = 0

        # Handshake metadata parser state (from serial log keywords)
        self._hs_buffer: List[str] = []
        self._hs_collecting = False

        # PCAP base64 parser state (from start_handshake_serial command)
        # Firmware outputs:
        #   --- PCAP BEGIN ---
        #   <base64 lines>
        #   --- PCAP END ---
        #   PCAP_SIZE: <N>
        #   --- HCCAPX BEGIN ---
        #   <base64 lines>
        #   --- HCCAPX END ---
        #   SSID: <ssid>  AP: <bssid>
        self._pcap_collecting = False
        self._pcap_b64_lines: List[str] = []
        self._hccapx_collecting = False
        self._hccapx_b64_lines: List[str] = []
        self._pcap_meta_ssid = "unknown"
        self._pcap_meta_bssid = "unknown"

        # Aggregate loot database
        self._db_path = self._base / "loot_db.json"
        self._db: dict = {}
        self._session_key = ts

        try:
            self._session.mkdir(parents=True, exist_ok=True)
            (self._session / "handshakes").mkdir(exist_ok=True)
            self._handshake_dir = self._session / "handshakes"
            self._serial_fh = open(
                self._session / "serial_full.log", "a", encoding="utf-8"
            )
            self._session_active = True
            log.info("Loot session: %s", self._session)
        except OSError as exc:
            log.error("Cannot create loot directory: %s", exc)

        # Load or build aggregate loot database
        self._db = self._load_or_build_db()

        # Periodic background sync — protects against power loss between
        # event-driven fsyncs. Every BACKUP_INTERVAL seconds it fsyncs the
        # open serial log handle and calls os.sync() so everything still
        # sitting in the OS page cache gets pushed to the SD card.
        self._backup_stop = False
        self._backup_thread = threading.Thread(
            target=self._periodic_backup_loop,
            name="loot-backup",
            daemon=True,
        )
        self._backup_thread.start()

    BACKUP_INTERVAL = 30  # seconds between sync passes

    def _periodic_backup_loop(self) -> None:
        """Run on a daemon thread; fsync the serial log and os.sync the FS.

        os.sync() is a syscall that schedules all dirty pages across every
        mounted filesystem to be written out. It can take ~100 ms–2 s on a
        busy SD card, which is why it runs off the game loop thread.
        """
        while not self._backup_stop:
            # Use short sleeps so shutdown is responsive
            for _ in range(self.BACKUP_INTERVAL):
                if self._backup_stop:
                    return
                time.sleep(1)
            self.flush_all()

    def flush_all(self) -> None:
        """Force every buffered write to reach the SD card.

        Safe to call from any thread. Called automatically every
        BACKUP_INTERVAL seconds; also hookable for pre-shutdown flushes.
        """
        fh = self._serial_fh
        if fh is not None:
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass  # Handle may be closed mid-call; ignore.
        try:
            os.sync()
        except AttributeError:
            pass  # Non-POSIX — nothing else we can do.

    @property
    def session_path(self) -> str:
        return str(self._session)

    @property
    def active(self) -> bool:
        return self._session_active

    @property
    def loot_root(self) -> Path:
        """Root loot directory (contains all session dirs)."""
        return self._base

    # ------------------------------------------------------------------
    # Aggregate loot database
    # ------------------------------------------------------------------

    def _load_or_build_db(self) -> dict:
        """Load existing progress safely; rebuild only when no database exists."""
        try:
            original = self._db_path.read_bytes()
        except FileNotFoundError:
            return self._rebuild_db()
        except OSError as exc:
            raise LootDataError("Cannot read the loot database. Player data was not reset.") from exc
        try:
            data = json.loads(original)
        except (json.JSONDecodeError, UnicodeError) as exc:
            # Keep even a damaged save intact for recovery, never replace it
            # with a new player or an incomplete estimate from session logs.
            raise LootDataError("Cannot read the loot database. Player data was not reset.") from exc
        if isinstance(data, dict) and type(data.get("version")) is int:
            if data["version"] > 2:
                raise LootDataError("This loot database needs a newer game version. Player data was not changed.")
            if isinstance(data.get("sessions"), dict):
                if data["version"] == 2:
                    log.info("Loot DB loaded: %d sessions", len(data["sessions"]))
                    return data
                if data["version"] == 1:
                    return self._migrate_legacy_db(data, original)
        raise LootDataError("Unsupported loot database format. Player data was not reset.")

    def _migrate_legacy_db(self, previous: dict, original: bytes) -> dict:
        """Upgrade LOCOSP's v1 cache without rebuilding earned progress.

        Old session files may have been removed after upload. Their existing
        counters and every top-level player field remain authoritative. Only
        missing counters and newly found sessions are read from disk.
        """
        if ("xp" in previous and
                (type(previous["xp"]) is not int or previous["xp"] < 0)):
            raise LootDataError("Invalid saved XP. Player data was not reset.")
        if not all(isinstance(value, dict) for value in previous["sessions"].values()):
            raise LootDataError("Invalid saved session data. Player data was not reset.")
        migrated = copy.deepcopy(previous)
        migrated["version"] = 2
        try:
            for entry in sorted(self._base.iterdir()):
                name = entry.name
                if (not entry.is_dir() or len(name) < 19 or
                        name[4] != "-" or name[7] != "-" or name[10] != "_"):
                    continue
                counts = migrated["sessions"].setdefault(name, {})
                for key, value in self._scan_session_dir(entry).items():
                    counts.setdefault(key, value)
            previous_totals = copy.deepcopy(previous.get("totals", {}))
            self._recalc_totals(migrated)
            migrated["totals"] = {**previous_totals, **migrated["totals"]}
            # Keep the exact source bytes before any database replacement.
            backup = self._base / ("loot_db.v1." + hashlib.sha256(original).hexdigest() + ".bak")
            created = False
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(fd, "wb") as fh:
                    fh.write(original)
                    fh.flush()
                    os.fsync(fh.fileno())
            except FileExistsError:
                if backup.is_symlink() or backup.read_bytes() != original:
                    raise LootDataError("The player-data backup could not be verified. Migration was cancelled.")
                os.chmod(backup, 0o600)
            except OSError:
                if created:
                    backup.unlink(missing_ok=True)
                raise
            try:
                directory = os.open(self._base, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass  # Some filesystems do not support directory fsync.
            if self._db_path.read_bytes() != original:
                raise LootDataError("Player data changed during migration. Close the other game before retrying.")
            if not self._save_db(migrated):
                raise LootDataError("Cannot save the migrated loot database. Original progress and backup were kept.")
        except (OSError, TypeError, ValueError) as exc:
            raise LootDataError("Cannot migrate the loot database safely. Original progress was not reset.") from exc
        log.info("Loot DB upgraded from v1 to v2; saved XP and player metadata preserved")
        return migrated

    def _rebuild_db(self) -> dict:
        """Scan all loot session directories and build the DB from scratch.

        Also generates .22000 files retroactively for any .hccapx that
        doesn't already have a corresponding .22000 file.
        """
        db: dict = {"version": 2, "sessions": {}, "totals": {}}
        if not self._base.is_dir():
            return db
        for entry in sorted(self._base.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if len(name) >= 19 and name[4] == "-" and name[7] == "-" and name[10] == "_":
                self._retroactive_22000(entry)
                db["sessions"][name] = self._scan_session_dir(entry)
        self._recalc_totals(db)
        self._save_db(db)
        log.info("Loot DB rebuilt: %d sessions", len(db["sessions"]))
        return db

    def _retroactive_22000(self, session_path: Path) -> None:
        """Generate .22000 for any .hccapx without a matching .22000."""
        hs_dir = session_path / "handshakes"
        if not hs_dir.is_dir():
            return
        try:
            for f in hs_dir.iterdir():
                if f.suffix == ".hccapx" and not f.with_suffix(".22000").exists():
                    self._try_generate_22000(f)
        except OSError:
            pass

    def _scan_session_dir(self, session_path: Path) -> dict:
        """Count loot items in a single session directory."""
        counts = {"pcap": 0, "hccapx": 0, "hc22000": 0, "passwords": 0, "et_captures": 0,
                  "mc_nodes": 0, "mc_messages": 0, "mt_nodes": 0, "mt_messages": 0,
                  "bt_devices": 0, "bt_airtags": 0,
                  "bt_smarttags": 0, "bt_devices_gps": 0, "wardriving": 0, "adsb": 0,
                  "mitm_pcaps": 0, "emergency": 0}
        hs_dir = session_path / "handshakes"
        if hs_dir.is_dir():
            try:
                for f in hs_dir.iterdir():
                    if f.suffix == ".pcap":
                        counts["pcap"] += 1
                    elif f.suffix == ".hccapx":
                        counts["hccapx"] += 1
                    elif f.suffix == ".22000":
                        counts["hc22000"] += 1
            except OSError:
                pass
        pw_file = session_path / "portal_passwords.log"
        if pw_file.is_file():
            try:
                # Only count lines that contain captured POST form data —
                # the log file also stores diagnostic events (client connects,
                # channel switches) which shouldn't inflate the password
                # count on the loot screen.
                with open(pw_file, encoding="utf-8") as f:
                    counts["passwords"] = sum(
                        1 for line in f
                        if "received post" in line.lower()
                        or "form submission" in line.lower())
            except OSError:
                pass
        et_file = session_path / "evil_twin_capture.log"
        if et_file.is_file():
            try:
                # Same filter as portal_passwords.log above — keep raw events
                # in the file for forensics but only count actual captures.
                with open(et_file, encoding="utf-8") as f:
                    counts["et_captures"] = sum(
                        1 for line in f
                        if "received post" in line.lower()
                        or "form submission" in line.lower())
            except OSError:
                pass
        mc_nodes_file = session_path / "meshcore_nodes.csv"
        if mc_nodes_file.is_file():
            try:
                with open(mc_nodes_file, encoding="utf-8") as fh:
                    lines = sum(1 for _ in fh)
                counts["mc_nodes"] = max(0, lines - 1)  # minus header
            except OSError:
                pass
        mc_msgs_file = session_path / "meshcore_messages.log"
        if mc_msgs_file.is_file():
            try:
                with open(mc_msgs_file, encoding="utf-8") as fh:
                    counts["mc_messages"] = sum(1 for _ in fh)
            except OSError:
                pass
        mt_nodes_file = session_path / "meshtastic_nodes.csv"
        if mt_nodes_file.is_file():
            try:
                with mt_nodes_file.open(encoding="utf-8", newline="") as fh:
                    counts["mt_nodes"] = len({row.get("node_id") for row in csv.DictReader(fh)
                                              if row.get("node_id")})
            except OSError:
                pass
        mt_msgs_file = session_path / "meshtastic_chat.jsonl"
        if mt_msgs_file.is_file():
            try:
                with mt_msgs_file.open(encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            item = json.loads(line)
                            if item.get("direction", "rx") == "rx":
                                counts["mt_messages"] += 1
                        except (ValueError, AttributeError):
                            continue
            except OSError:
                pass
        # Emergency-vehicle RF alerts (one row per keyup detection; skip header)
        emerg_file = session_path / "emergency_rf.csv"
        if emerg_file.is_file():
            try:
                lines = sum(1 for _ in open(emerg_file, encoding="utf-8"))
                counts["emergency"] = max(0, lines - 1)
            except OSError:
                pass
        bt_dev_file = session_path / "bt_devices.csv"
        if bt_dev_file.is_file():
            try:
                gps_count = 0
                total = 0
                for i, line in enumerate(open(bt_dev_file, encoding="utf-8")):
                    if i == 0:
                        continue  # skip header
                    total += 1
                    parts = line.strip().split(",")
                    # lat,lon are last two columns
                    if len(parts) >= 8:
                        try:
                            lat = float(parts[-2])
                            lon = float(parts[-1])
                            if lat != 0.0 or lon != 0.0:
                                gps_count += 1
                        except ValueError:
                            pass
                counts["bt_devices"] = total
                counts["bt_devices_gps"] = gps_count
            except OSError:
                pass
        bt_at_file = session_path / "bt_airtag.log"
        if bt_at_file.is_file():
            try:
                total_at = 0
                total_st = 0
                for line in open(bt_at_file, encoding="utf-8"):
                    if "AirTags:" in line:
                        try:
                            part = line.split("AirTags:")[1].split("|")[0].strip()
                            total_at = max(total_at, int(part))
                        except (ValueError, IndexError):
                            pass
                    if "SmartTags:" in line:
                        try:
                            part = line.split("SmartTags:")[1].strip()
                            total_st = max(total_st, int(part))
                        except (ValueError, IndexError):
                            pass
                counts["bt_airtags"] = total_at
                counts["bt_smarttags"] = total_st
            except OSError:
                pass
        wd_file = session_path / "wardriving.csv"
        if wd_file.is_file():
            try:
                lines = sum(1 for _ in open(wd_file, encoding="utf-8"))
                counts["wardriving"] = max(0, lines - 2)  # minus pre-header + header
            except OSError:
                pass
        adsb_file = session_path / "adsb_aircraft.csv"
        if adsb_file.is_file():
            try:
                counts["adsb"] = max(0, sum(1 for _ in open(adsb_file, encoding="utf-8")) - 1)  # minus header
            except OSError:
                pass
        mitm_dir = session_path / "mitm"
        if mitm_dir.is_dir():
            try:
                counts["mitm_pcaps"] = sum(
                    1 for f in mitm_dir.iterdir() if f.suffix == ".pcap"
                )
            except OSError:
                pass
        return counts

    def _recalc_totals(self, db: dict) -> None:
        """Recalculate totals from all session entries."""
        keys = ("pcap", "hccapx", "hc22000", "passwords", "et_captures",
                "mc_nodes", "mc_messages", "mt_nodes", "mt_messages",
                "bt_devices", "bt_airtags", "bt_smarttags",
                "bt_devices_gps", "wardriving", "adsb", "emergency")
        totals: dict = {k: 0 for k in keys}
        totals["sessions"] = len(db["sessions"])
        for session_counts in db["sessions"].values():
            for k in keys:
                totals[k] += session_counts.get(k, 0)
        db["totals"] = totals

    def _save_db(self, db: dict) -> bool:
        """Write the DB to loot_db.json atomically. fsync'd before rename so
        a power cut between tmp write and rename can't leave a zero-byte
        loot_db.json (the rename is atomic on the same filesystem)."""
        with self._db_lock:
            try:
                tmp = self._db_path.with_suffix(".tmp")
                mode = self._db_path.stat().st_mode & 0o777 if self._db_path.exists() else 0o600
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(db, fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, mode)
                tmp.replace(self._db_path)
                # Persist the directory entry as well as the file contents.
                try:
                    directory = os.open(self._db_path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError:
                    pass
                return True
            except OSError as exc:
                log.error("Cannot save loot DB: %s", exc)
                return False

    def update_session_loot(self) -> None:
        """Rescan current session and update aggregate DB."""
        if not self._session_active:
            return
        with self._db_lock:
            self._db["sessions"][self._session_key] = self._scan_session_dir(self._session)
            self._recalc_totals(self._db)
            self._save_db(self._db)

    @property
    def loot_totals(self) -> dict:
        """Aggregate totals across all sessions, including cracked passwords."""
        totals = self._db.get("totals", {})
        if "cracked" not in totals:
            totals["cracked"] = self.cracked_count
        return totals

    @property
    def cracked_count(self) -> int:
        """Count of cracked passwords from WPA-sec potfile JSON."""
        if not hasattr(self, "_cracked_cache"):
            self._cracked_cache: int | None = None
        if self._cracked_cache is not None:
            return self._cracked_cache
        json_path = self._base / "passwords" / "wpasec_cracked.json"
        if json_path.is_file():
            try:
                with open(json_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._cracked_cache = data.get("count", 0)
                return self._cracked_cache
            except (json.JSONDecodeError, OSError):
                pass
        self._cracked_cache = 0
        return 0

    def invalidate_cracked_cache(self) -> None:
        """Force reload of cracked count on next access."""
        self._cracked_cache = None

    # ------------------------------------------------------------------
    # Persistent XP
    # ------------------------------------------------------------------

    def load_xp(self) -> int:
        """Load persisted XP from loot_db.json."""
        return self._db.get("xp", 0)

    def has_saved_xp(self) -> bool:
        """An explicitly saved zero is progress too, not a bootstrap request."""
        return "xp" in self._db

    def save_xp(self, xp: int) -> None:
        """Persist current XP to loot_db.json."""
        with self._db_lock:
            if self._db.get("xp") != xp:
                self._db["xp"] = xp
                self._save_db(self._db)

    # ------------------------------------------------------------------
    # Persistent badges
    # ------------------------------------------------------------------

    def load_badges(self) -> set[str]:
        """Load earned badges from loot_db.json."""
        return set(self._db.get("badges", []))

    def save_badge(self, badge: str) -> None:
        """Add a badge and persist to loot_db.json."""
        with self._db_lock:
            badges = set(self._db.get("badges", []))
            if badge not in badges:
                badges.add(badge)
                self._db["badges"] = sorted(badges)
                self._save_db(self._db)

    # ------------------------------------------------------------------
    # Persistent MeshCore contacts
    # ------------------------------------------------------------------

    def load_contacts(self) -> dict[str, dict]:
        """Load MeshCore contacts from loot_db.json.

        Returns dict keyed by node_id with fields:
        id, type, name, lat, lon, rssi, snr, last_seen, first_seen, note
        """
        with self._db_lock:
            return copy.deepcopy(self._db.get("mc_contacts", {}))

    def save_contact(self, node_id: str, data: dict) -> bool:
        """Save or update a MeshCore contact. Returns True if new contact."""
        return self._upsert_mesh_contact("meshcore", node_id, data)

    def save_contact_note(self, node_id: str, note: str) -> None:
        """Update note for a MeshCore contact."""
        self._save_mesh_note("meshcore", node_id, note)

    def delete_contact(self, node_id: str) -> None:
        """Remove a MeshCore contact from loot_db."""
        self._delete_mesh_contact("meshcore", node_id)

    # ------------------------------------------------------------------
    # Shared persistent mesh discovery transaction
    # ------------------------------------------------------------------

    @staticmethod
    def _mesh_namespace(protocol: str) -> tuple[str, str]:
        if protocol == "meshcore":
            return "mc_contacts", "meshcore_nodes.csv"
        if protocol == "meshtastic":
            return "mt_contacts", "meshtastic_nodes.csv"
        raise ValueError("unknown mesh protocol")

    @staticmethod
    def _mesh_node_id(node_id: str) -> str:
        node_id = str(node_id).strip()
        if not node_id or len(node_id) > 128 or any(c in node_id for c in "\r\n\0"):
            raise ValueError("invalid mesh node identifier")
        if re.fullmatch(r"!?[0-9a-fA-F]{8}", node_id):
            node_id = node_id.lower()
        return node_id

    @staticmethod
    def _valid_mesh_position(data: dict) -> bool:
        try:
            lat, lon = float(data.get("lat")), float(data.get("lon"))
            return (math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180
                    and (lat != 0 or lon != 0))
        except (TypeError, ValueError):
            return False

    def _merge_mesh_contact(self, protocol: str, node_id: str,
                            previous: dict, incoming: dict) -> dict:
        now = time.time()
        merged = copy.deepcopy(previous)
        for key, value in incoming.items():
            if key in ("id", "protocol", "first_seen", "note", "lat", "lon", "has_position"):
                continue
            if value is None:
                continue
            if key == "name" and str(value).strip() in ("", "?", node_id):
                if previous.get("name") not in (None, "", "?", node_id):
                    continue
            merged[key] = value
        if self._valid_mesh_position(incoming):
            merged.update(lat=float(incoming["lat"]), lon=float(incoming["lon"]), has_position=True)
        elif not self._valid_mesh_position(previous):
            merged.update(lat=0.0, lon=0.0, has_position=False)
        else:
            merged["has_position"] = True
        merged.update(id=node_id, protocol=protocol)
        merged["first_seen"] = previous.get("first_seen", incoming.get("first_seen", now))
        merged["note"] = previous.get("note", incoming.get("note", ""))
        merged.setdefault("name", "?")
        merged.setdefault("type", "Client")
        merged.setdefault("rssi", 0.0)
        merged.setdefault("snr", 0.0)
        merged["last_seen"] = max(float(previous.get("last_seen", 0) or 0),
                                  float(incoming.get("last_seen", now) or now))
        return merged

    def assign_receiver_gps_to_unlocated_contacts(self) -> dict:
        """Apply the user's receiver-position fallback, without inventing RX.

        Missing historical positions are explicitly current user assignments.
        Canonical node coordinates, discovery dates, rewards and notes remain
        intact. A later genuine node GPS position always has priority.
        """
        observation = receiver_observation(self._gps, kind="user_assignment")
        if observation is None:
            return {"waiting_for_fix": True, "contacts": {"meshcore": [], "meshtastic": []}}
        changed = {"meshcore": [], "meshtastic": []}
        with self._db_lock:
            next_db = copy.deepcopy(self._db)
            for protocol in changed:
                key, _ = self._mesh_namespace(protocol)
                for node_id, contact in next_db.get(key, {}).items():
                    if mesh_position(contact) is not None: continue
                    contact.setdefault("id", node_id)
                    contact["receiver_position"] = dict(observation)
                    changed[protocol].append(copy.deepcopy(contact))
            if not any(changed.values()):
                return {"waiting_for_fix": False, "contacts": changed}
            # Exports share the discovery transaction lock and are reconstructible
            # from authoritative contacts. No discovery/XP API is invoked here.
            for protocol, contacts in changed.items():
                for contact in contacts: self._upsert_mesh_node_csv(protocol, contact)
            if self._session_active:
                next_db.setdefault("sessions", {})[self._session_key] = self._scan_session_dir(self._session)
                self._recalc_totals(next_db)
            if not self._save_db(next_db):
                raise OSError("receiver GPS assignment could not be saved")
            self._db = next_db
        return {"waiting_for_fix": False, "contacts": changed}

    def _upsert_mesh_contact(self, protocol: str, node_id: str, data: dict) -> bool:
        """Metadata persistence only; callers needing XP use register_mesh_contact."""
        key, _ = self._mesh_namespace(protocol)
        node_id = self._mesh_node_id(node_id)
        with self._db_lock:
            next_db = copy.deepcopy(self._db)
            contacts = next_db.setdefault(key, {})
            is_new = node_id not in contacts
            contacts[node_id] = self._merge_mesh_contact(protocol, node_id, contacts.get(node_id, {}), data)
            if not self._save_db(next_db):
                raise OSError("mesh contact persistence failed")
            self._db = next_db
            return is_new

    def register_mesh_contact(self, protocol: str, node_id: str, data: dict,
                              current_xp: int | None = None) -> dict:
        """Commit contact and its one-time discovery XP together.

        Invoke on the main event queue with the UI's current XP. The returned
        xp_total replaces the UI value; do not call gain_xp for the same event.
        Existing contacts are historical discoveries, never retroactive awards.
        A durable ledger is independent of packet retransmissions or deletion
        from the visible contact list. MeshCore and Meshtastic share one rule.
        """
        key, _ = self._mesh_namespace(protocol)
        node_id = self._mesh_node_id(node_id)
        with self._db_lock:
            next_db = copy.deepcopy(self._db)
            contacts = next_db.setdefault(key, {})
            ledger = next_db.setdefault("mesh_discovery_events", {}).setdefault(protocol, {})
            # Seed pre-integration contacts without modifying prior XP/history.
            for known_id, known in contacts.items():
                ledger.setdefault(known_id, {"time": known.get("first_seen", 0), "historical": True})
            is_new = node_id not in contacts
            new_event = node_id not in ledger
            contact = self._merge_mesh_contact(protocol, node_id, contacts.get(node_id, {}), data)
            award = mesh_contact_xp(new_event=new_event)
            if new_event:
                ledger[node_id] = {"time": time.time(), "historical": False, "xp_awarded": award}
            contacts[node_id] = contact
            base_xp = next_db.get("xp", 0) if current_xp is None else int(current_xp)
            next_db["xp"] = int(base_xp) + award
            # CSV is an export; loot_db is authoritative for contacts and XP.
            # Write export first so a failed DB commit never reports paid XP.
            self._upsert_mesh_node_csv(protocol, contact)
            if self._session_active:
                next_db.setdefault("sessions", {})[self._session_key] = self._scan_session_dir(self._session)
                self._recalc_totals(next_db)
            if not self._save_db(next_db):
                raise OSError("mesh discovery transaction failed; XP was not committed")
            self._db = next_db
            return {"new_contact": is_new, "new_event": new_event, "xp_award": award,
                    "xp_total": next_db["xp"], "contact": copy.deepcopy(contact)}

    def load_meshtastic_contacts(self) -> dict[str, dict]:
        with self._db_lock:
            return copy.deepcopy(self._db.get("mt_contacts", {}))

    def upsert_meshtastic_contact(self, node_id: str, data: dict) -> bool:
        return self._upsert_mesh_contact("meshtastic", node_id, data)

    def _save_mesh_note(self, protocol: str, node_id: str, note: str) -> None:
        key, _ = self._mesh_namespace(protocol)
        with self._db_lock:
            contacts = self._db.setdefault(key, {})
            if node_id in contacts:
                contacts[node_id]["note"] = str(note)
                self._save_db(self._db)

    def save_meshtastic_contact_note(self, node_id: str, note: str) -> None:
        self._save_mesh_note("meshtastic", node_id, note)

    def _delete_mesh_contact(self, protocol: str, node_id: str) -> None:
        key, _ = self._mesh_namespace(protocol)
        with self._db_lock:
            contacts = self._db.get(key, {})
            if node_id in contacts:
                # Preserve award evidence so a rediscovery cannot pay twice.
                ledger = self._db.setdefault("mesh_discovery_events", {}).setdefault(protocol, {})
                ledger.setdefault(node_id, {"time": contacts[node_id].get("first_seen", 0), "historical": True})
                del contacts[node_id]
                self._save_db(self._db)

    def delete_meshtastic_contact(self, node_id: str) -> None:
        self._delete_mesh_contact("meshtastic", node_id)

    def _upsert_mesh_node_csv(self, protocol: str, contact: dict) -> None:
        if not self._session_active:
            return
        _, filename = self._mesh_namespace(protocol)
        path = self._session / filename
        fields = ["timestamp", "node_id", "type", "name", "lat", "lon", "rssi", "snr",
                  "position_source", "node_lat", "node_lon", "position_observed_at", "public_key"]
        rows = []
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                if not reader.fieldnames or "node_id" not in reader.fieldnames:
                    raise ValueError("invalid existing mesh CSV; refusing to overwrite")
                fields = list(reader.fieldnames) + [f for f in fields if f not in reader.fieldnames]
                rows = list(reader)
        existing = next((row for row in rows if row.get("node_id") == contact["id"]), None)
        row = dict(existing or {})
        row.update(node_id=contact["id"], type=contact.get("type", "Client"),
                   rssi=contact.get("rssi", 0), snr=contact.get("snr", 0))
        if contact.get("name") not in (None, "", "?", contact["id"]) or row.get("name") in (None, "", "?"):
            row["name"] = contact.get("name", "?")
        position = mesh_position(contact)
        if position is not None:
            row.update(lat=position["lat"], lon=position["lon"], position_source=position["source"],
                       node_lat=contact.get("lat", 0), node_lon=contact.get("lon", 0),
                       position_observed_at=position.get("observed_at", ""))
            if position["source"] != "node":
                observed = position["assigned_at"] if position["source"] == "receiver_assignment" else position["observed_at"]
                row["timestamp"] = datetime.fromtimestamp(observed).strftime("%Y-%m-%dT%H:%M:%S")
        elif not self._valid_mesh_position(existing or {}):
            row.update(lat=0.0, lon=0.0, position_source="waiting_for_receiver_gps",
                       node_lat=contact.get("lat", 0), node_lon=contact.get("lon", 0), position_observed_at="")
        if contact.get("pubkey"):
            row["public_key"] = contact["pubkey"]
        row.setdefault("timestamp", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        if existing is None:
            rows.append(row)
        else:
            rows[rows.index(existing)] = row
        tmp = path.with_suffix(".tmp")
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fields)
            writer.writeheader()
            writer.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        tmp.replace(path)

    def calculate_xp_from_loot(self) -> int:
        """Calculate XP from all historical loot data.

        Used for initial XP bootstrap when no XP has been saved yet.
        """
        t = self._db.get("totals", {})
        xp = 0
        xp += t.get("wardriving", 0) * 15       # WiFi networks
        xp += t.get("bt_devices", 0) * 10        # BLE devices
        xp += t.get("pcap", 0) * 200             # Handshake captures
        xp += t.get("et_captures", 0) * 150      # Evil Twin credentials
        xp += t.get("passwords", 0) * 150         # Portal passwords
        xp += self.cracked_count * 150             # Cracked passwords
        return xp

    def get_known_devices(self) -> tuple[set, set]:
        """Load all known BLE MACs and WiFi BSSIDs from all loot sessions.

        Returns (known_ble_macs, known_wifi_bssids).
        """
        import csv as _csv

        ble_macs: set[str] = set()
        wifi_bssids: set[str] = set()

        if not self._base.is_dir():
            return ble_macs, wifi_bssids

        for entry in self._base.iterdir():
            if not entry.is_dir() or not entry.name[0].isdigit():
                continue

            # WiFi BSSIDs from wardriving.csv
            wd = entry / "wardriving.csv"
            if wd.is_file():
                try:
                    with open(wd, "r", encoding="utf-8", errors="replace") as f:
                        reader = _csv.reader(f)
                        for row in reader:
                            if len(row) >= 1 and ":" in row[0]:
                                wifi_bssids.add(row[0].strip().upper())
                except Exception:
                    pass

            # BLE MACs from bt_devices.csv
            bt = entry / "bt_devices.csv"
            if bt.is_file():
                try:
                    with open(bt, "r", encoding="utf-8", errors="replace") as f:
                        reader = _csv.reader(f)
                        for row in reader:
                            if len(row) >= 1 and ":" in row[0] and row[0] != "mac":
                                ble_macs.add(row[0].strip().upper())
                except Exception:
                    pass

        return ble_macs, wifi_bssids

    # ------------------------------------------------------------------
    # Full serial log
    # ------------------------------------------------------------------

    def log_serial(self, line: str) -> None:
        """Append a timestamped serial line to the full log."""
        if not self._serial_fh:
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        try:
            self._serial_fh.write(f"[{ts}] {line}\n")
            self._serial_fh.flush()
        except OSError:
            pass

        # Check for handshake metadata in serial stream
        self._detect_handshake(line)
        # Check for pcap base64 data from start_handshake_serial
        self._detect_pcap_stream(line)

    # ------------------------------------------------------------------
    # Handshake detection from serial stream
    # ------------------------------------------------------------------

    # Keywords that START handshake collection
    _HS_START_KW = ("message pair:", "message_pair:", "ANonce present")

    # Keywords that belong to a handshake block (keep collecting)
    _HS_RELATED_KW = (
        "message pair:", "message_pair:", "ANonce present", "SNonce present",
        "Key MIC", "AP MAC:", "STA MAC:", "EAPOL data:", "SSID:",
        "HANDSHAKE IS COMPLETE", "HANDSHAKE IS VALID",
        "Handshake #", "captured!", "Created ", "Failed ",
        "Cycle:", "networks captured", "attack cleanup",
        "cleanup complete", "task finished",
        "=====",  # separator lines
    )

    # Keywords that END handshake collection (save after this line)
    _HS_END_KW = ("task finished", "cleanup complete")

    def _detect_handshake(self, line: str) -> None:
        """Collect handshake metadata from serial and save when complete.

        Collects from 'message pair:' through 'task finished', saves ONCE.
        """
        stripped = line.strip()

        # Start collecting on handshake start indicators
        if not self._hs_collecting:
            if any(kw in stripped for kw in self._HS_START_KW):
                self._hs_collecting = True
                self._hs_buffer = [stripped]
            return

        # We are collecting — check if this line belongs to the block
        if any(kw in stripped for kw in self._HS_RELATED_KW) or not stripped:
            self._hs_buffer.append(stripped)
            # Check for end marker
            if any(kw in stripped for kw in self._HS_END_KW):
                self._save_handshake_buffer()
        else:
            # Unrelated line — save what we have and stop
            self._save_handshake_buffer()

    def _save_handshake_buffer(self) -> None:
        """Write collected handshake metadata to a file."""
        if not self._hs_buffer or not self._handshake_dir:
            self._hs_collecting = False
            self._hs_buffer = []
            return

        # Extract SSID and BSSID for filename
        ssid = "unknown"
        bssid = "unknown"
        for line in self._hs_buffer:
            if "SSID:" in line:
                parts = line.split("SSID:")
                if len(parts) > 1:
                    ssid = parts[1].strip().split()[0].strip(",")
            if "AP MAC:" in line:
                parts = line.split("AP MAC:")
                if len(parts) > 1:
                    bssid = parts[1].strip().replace(":", "")[:12]

        ts = datetime.now().strftime("%H%M%S")
        safe_ssid = "".join(c if c.isalnum() or c in "-_" else "_" for c in ssid)
        filename = f"{safe_ssid}_{bssid}_{ts}.txt"
        filepath = self._handshake_dir / filename

        try:
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(f"# Handshake captured at {datetime.now().isoformat()}\n")
                fh.write(f"# SSID: {ssid}\n")
                fh.write(f"# BSSID: {bssid}\n")
                fh.write(self._gps_header_lines())
                fh.write("\n")
                for line in self._hs_buffer:
                    fh.write(line + "\n")
                _fsync_file(fh)
            log.info("Handshake saved: %s", filepath)
            self._save_gps_sidecar(filepath)
            self.update_session_loot()
        except OSError as exc:
            log.error("Cannot save handshake: %s", exc)

        self._hs_collecting = False
        self._hs_buffer = []

    # ------------------------------------------------------------------
    # PCAP base64 stream parser (start_handshake_serial output)
    # ------------------------------------------------------------------

    def _detect_pcap_stream(self, line: str) -> None:
        """Parse base64-encoded pcap/hccapx blocks from serial stream.

        Triggered by start_handshake_serial firmware command which outputs:
            --- PCAP BEGIN ---
            <base64 lines>
            --- PCAP END ---
            PCAP_SIZE: <N>
            --- HCCAPX BEGIN ---
            <base64 lines>
            --- HCCAPX END ---
            SSID: <ssid>  AP: <bssid>
        """
        stripped = line.strip()

        # PCAP block
        if "--- PCAP BEGIN ---" in stripped:
            self._pcap_collecting = True
            self._pcap_b64_lines = []
            return

        if self._pcap_collecting:
            if "--- PCAP END ---" in stripped:
                self._pcap_collecting = False
            else:
                # Collect only valid base64 chars
                clean = stripped.replace(" ", "")
                if clean:
                    self._pcap_b64_lines.append(clean)
            return

        # HCCAPX block
        if "--- HCCAPX BEGIN ---" in stripped:
            self._hccapx_collecting = True
            self._hccapx_b64_lines = []
            return

        if self._hccapx_collecting:
            if "--- HCCAPX END ---" in stripped:
                self._hccapx_collecting = False
            else:
                clean = stripped.replace(" ", "")
                if clean:
                    self._hccapx_b64_lines.append(clean)
            return

        # Metadata line: firmware prints SSID and AP MAC after the blocks
        if "SSID:" in stripped and "AP:" in stripped:
            try:
                parts = stripped.split("SSID:")
                if len(parts) > 1:
                    rest = parts[1].strip()
                    if "AP:" in rest:
                        ssid_part, ap_part = rest.split("AP:", 1)
                        self._pcap_meta_ssid = ssid_part.strip()
                        self._pcap_meta_bssid = ap_part.strip().replace(":", "")[:12]
                    else:
                        self._pcap_meta_ssid = rest.strip().split()[0]
            except Exception:
                pass
            # Save when we have both pcap and hccapx (or at least pcap)
            if self._pcap_b64_lines:
                self._save_pcap_from_b64()
            return

        # Fallback: if we got hccapx end and pcap is ready, save after short wait
        # (in case firmware doesn't print SSID/AP line)
        if self._pcap_b64_lines and self._hccapx_b64_lines and not self._hccapx_collecting:
            # Check if this is an unrelated line that signals end of block
            if not any(c in stripped for c in ("BEGIN", "END", "SIZE:", "PCAP", "HCCAPX")):
                self._save_pcap_from_b64()

    def _save_pcap_from_b64(self) -> None:
        """Decode and save accumulated base64 pcap/hccapx data as binary files."""
        if not self._handshake_dir or not self._pcap_b64_lines:
            self._pcap_b64_lines = []
            self._hccapx_b64_lines = []
            self._pcap_meta_ssid = "unknown"
            self._pcap_meta_bssid = "unknown"
            return

        ts = datetime.now().strftime("%H%M%S")
        safe_ssid = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in self._pcap_meta_ssid
        )
        base_name = f"{safe_ssid}_{self._pcap_meta_bssid}_{ts}"

        # Save .pcap — fsync'd, this is unrecoverable capture data
        try:
            pcap_data = base64.b64decode("".join(self._pcap_b64_lines))
            pcap_path = self._handshake_dir / f"{base_name}.pcap"
            with open(pcap_path, "wb") as fh:
                fh.write(pcap_data)
                _fsync_file(fh)
            log.info("PCAP saved: %s (%d bytes)", pcap_path, len(pcap_data))
            self._save_gps_sidecar(pcap_path)
        except Exception as exc:
            log.error("Cannot save PCAP: %s", exc)

        # Save .hccapx (if present) — fsync'd
        if self._hccapx_b64_lines:
            try:
                hccapx_data = base64.b64decode("".join(self._hccapx_b64_lines))
                hccapx_path = self._handshake_dir / f"{base_name}.hccapx"
                with open(hccapx_path, "wb") as fh:
                    fh.write(hccapx_data)
                    _fsync_file(fh)
                log.info("HCCAPX saved: %s (%d bytes)",
                         hccapx_path, len(hccapx_data))
                self._save_gps_sidecar(hccapx_path)
                # Generate .22000 from complete handshakes
                self._try_generate_22000(hccapx_path)
            except Exception as exc:
                log.error("Cannot save HCCAPX: %s", exc)

        # Reset state and update DB
        self._pcap_b64_lines = []
        self._hccapx_b64_lines = []
        self._pcap_meta_ssid = "unknown"
        self._pcap_meta_bssid = "unknown"
        self.update_session_loot()

    # ------------------------------------------------------------------
    # GPS sidecar (Pwnagotchi-compatible .gps.json)
    # ------------------------------------------------------------------

    def _save_gps_sidecar(self, base_path: Path) -> None:
        """Write a .gps.json sidecar alongside a capture file.

        Creates e.g. MyWiFi_AABB_143022.pcap.gps.json with raw GPS fix.
        Loot always contains full (unmasked) data.
        """
        if not self._gps or not self._gps.available:
            return
        fix = self._gps.fix
        if not fix.valid:
            return
        geo_path = base_path.parent / (base_path.name + ".gps.json")
        try:
            data = {
                "Latitude": round(fix.latitude, 7),
                "Longitude": round(fix.longitude, 7),
                "Altitude": round(fix.altitude, 1),
                "Date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "Satellites": fix.satellites,
                "HDOP": round(fix.hdop, 1),
            }
            with open(geo_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            log.info("GPS sidecar: %s", geo_path)
        except Exception as exc:
            log.error("Cannot save GPS sidecar: %s", exc)

    def _gps_header_lines(self) -> str:
        """Return GPS header lines for .txt handshake files, or empty string."""
        if not self._gps or not self._gps.available:
            return ""
        fix = self._gps.fix
        if not fix.valid:
            return ""
        return (
            f"# GPS: {fix.latitude:.7f}, {fix.longitude:.7f}\n"
            f"# Alt: {fix.altitude:.1f}m  Sat: {fix.satellites}  HDOP: {fix.hdop:.1f}\n"
        )

    # ------------------------------------------------------------------
    # HC22000 generation
    # ------------------------------------------------------------------

    def _try_generate_22000(self, hccapx_path: Path) -> None:
        """Generate a .22000 file from an HCCAPX if the handshake is complete."""
        from .hc22000 import convert_hccapx_to_22000

        gps_fix = None
        if self._gps and self._gps.available:
            gps_fix = self._gps.fix
        convert_hccapx_to_22000(hccapx_path, gps_fix=gps_fix)

    # ------------------------------------------------------------------
    # Scan results
    # ------------------------------------------------------------------

    # WiGLE AuthMode mapping
    _AUTH_MAP: dict[str, str] = {
        "OPEN": "[ESS]",
        "WEP": "[WEP][ESS]",
        "WPA": "[WPA-PSK-CCMP+TKIP][ESS]",
        "WPA2": "[WPA2-PSK-CCMP][ESS]",
        "WPA3": "[WPA3-SAE-CCMP][ESS]",
        "WPA/WPA2": "[WPA-PSK-CCMP+TKIP][WPA2-PSK-CCMP][ESS]",
        "WPA2/WPA3": "[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]",
    }

    _WIGLE_PRE_HEADER = (
        "WigleWifi-1.4,appRelease=WatchDogsGo,model=uConsole,"
        "release=1.0,device=WatchDogsGo,display=Pyxel,board=ESP32,"
        "brand=LOCOSP,star=Sol,body=3,subBody=0\n"
    )
    _WIGLE_HEADER = (
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,"
        "CurrentLatitude,CurrentLongitude,AltitudeMeters,"
        "AccuracyMeters,Type\n"
    )

    def _wigle_auth(self, auth: str) -> str:
        """Convert ESP32 auth string to WiGLE AuthMode format."""
        return self._AUTH_MAP.get(auth.strip(), f"[{auth}][ESS]")

    def _wardrive_save_failed(self, path: Path, exc: OSError | UnicodeError) -> bool:
        self.wardrive_save_errors += 1
        log.error("Cannot persist wardrive CSV %s: %s", path, exc)
        return False

    def _commit_wardrive_csv(self, path: Path, lines) -> bool:
        """Commit only wardrive/BLE CSVs after strict flush and file fsync.

        A failed write/fsync leaves the previous file (and its dedup evidence)
        intact, so the same discovery can be saved on a later retry.
        """
        temporary = None
        try:
            previous = path.stat() if path.exists() else None
            descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                                dir=path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
                # Preserve existing access; normal new CSVs were readable 0644.
                if previous is not None and hasattr(os, "chown"):
                    os.chown(temporary, previous.st_uid, previous.st_gid)
                os.chmod(temporary, previous.st_mode & 0o7777 if previous else 0o644)
                fh.flush()
                os.fsync(fh.fileno())
            temporary.replace(path)
            return True
        except (OSError, UnicodeError) as exc:
            return self._wardrive_save_failed(path, exc)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("Cannot remove failed wardrive temporary file %s: %s",
                                temporary, exc)

    def save_wardriving_network(self, network: Network) -> bool:
        """Append a geo-tagged network to wardriving.csv (WiGLE format, dedup by BSSID).

        Returns True if the network was new or updated (stronger RSSI).
        """
        if not self._session_active or not network.bssid:
            return False
        path = self._session / "wardriving.csv"
        # Get GPS coords + accuracy
        lat, lon, alt, accuracy = 0.0, 0.0, 0.0, 0.0
        if self._gps and self._gps.available:
            fix = self._gps.fix
            if fix.valid:
                lat = round(fix.latitude, 7)
                lon = round(fix.longitude, 7)
                alt = round(fix.altitude, 1)
                accuracy = round(fix.hdop * 5.0, 1) if fix.hdop < 99 else 0.0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            rssi_val = int(network.rssi)
        except (ValueError, TypeError):
            rssi_val = -100
        auth_mode = self._wigle_auth(network.auth)
        # WiGLE row: MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,Lat,Lon,Alt,Accuracy,Type
        new_row = (
            f"{network.bssid},{network.ssid},{auth_mode},{ts},"
            f"{network.channel},{network.rssi},{lat},{lon},{alt},"
            f"{accuracy},WIFI\n"
        )
        # Read existing to dedup by BSSID (MAC = column 0, RSSI = column 5)
        existing: dict[str, tuple[int, int]] = {}  # bssid -> (line_index, rssi)
        lines: list[str] = []
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        lines.append(line)
                        if i <= 1:
                            continue  # pre-header + header
                        parts = line.strip().split(",")
                        if len(parts) >= 6:
                            bssid = parts[0]  # MAC column
                            try:
                                existing[bssid] = (i, int(parts[5]))
                            except (ValueError, IndexError):
                                existing[bssid] = (i, -100)
            except (OSError, UnicodeError) as exc:
                return self._wardrive_save_failed(path, exc)
        bssid = network.bssid
        if bssid in existing:
            old_idx, old_rssi = existing[bssid]
            if rssi_val <= old_rssi:
                return False  # existing is stronger or equal
            lines[old_idx] = new_row
            return self._commit_wardrive_csv(path, lines)
        else:
            if not lines:
                lines = [self._WIGLE_PRE_HEADER, self._WIGLE_HEADER]
            lines.append(new_row)
            return self._commit_wardrive_csv(path, lines)

    def save_wardriving_bt(self, mac: str, rssi: int, name: str) -> bool:
        """Append a geo-tagged BLE device to wardriving.csv (WiGLE format, dedup by MAC).

        Uses the same CSV file as WiFi wardriving with Type=BLE.
        Returns True if the device was new or updated (stronger RSSI).
        """
        if not self._session_active or not mac:
            return False
        path = self._session / "wardriving.csv"
        # Get GPS coords + accuracy
        lat, lon, alt, accuracy = 0.0, 0.0, 0.0, 0.0
        if self._gps and self._gps.available:
            fix = self._gps.fix
            if fix.valid:
                lat = round(fix.latitude, 7)
                lon = round(fix.longitude, 7)
                alt = round(fix.altitude, 1)
                accuracy = round(fix.hdop * 5.0, 1) if fix.hdop < 99 else 0.0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # WiGLE row: MAC,SSID(=name),AuthMode,FirstSeen,Channel,RSSI,Lat,Lon,Alt,Accuracy,Type
        new_row = (
            f"{mac},{name},[BLE],{ts},"
            f",{rssi},{lat},{lon},{alt},"
            f"{accuracy},BLE\n"
        )
        # Read existing to dedup by MAC (column 0, RSSI = column 5)
        existing: dict[str, tuple[int, int]] = {}
        lines: list[str] = []
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        lines.append(line)
                        if i <= 1:
                            continue  # pre-header + header
                        parts = line.strip().split(",")
                        if len(parts) >= 6:
                            try:
                                existing[parts[0]] = (i, int(parts[5]))
                            except (ValueError, IndexError):
                                existing[parts[0]] = (i, -100)
            except (OSError, UnicodeError) as exc:
                return self._wardrive_save_failed(path, exc)
        if mac in existing:
            old_idx, old_rssi = existing[mac]
            if rssi <= old_rssi:
                return False  # existing is stronger or equal
            lines[old_idx] = new_row
            return self._commit_wardrive_csv(path, lines)
        else:
            if not lines:
                lines = [self._WIGLE_PRE_HEADER, self._WIGLE_HEADER]
            lines.append(new_row)
            return self._commit_wardrive_csv(path, lines)

    def save_scan_results(self, networks: List[Network]) -> None:
        """Save scan results as CSV. fsync'd."""
        if not self._session_active or not networks:
            return
        filepath = self._session / "scan_results.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["#", "SSID", "BSSID", "Channel", "Auth",
                                 "RSSI", "Band", "Vendor"])
                for n in networks:
                    writer.writerow([
                        n.index, n.ssid, n.bssid, n.channel,
                        n.auth, n.rssi, n.band, n.vendor,
                    ])
                _fsync_file(fh)
            log.info("Scan results saved: %d networks", len(networks))
        except OSError as exc:
            log.error("Cannot save scan results: %s", exc)

    # ------------------------------------------------------------------
    # Sniffer results
    # ------------------------------------------------------------------

    def save_sniffer_aps(self, aps: List[SnifferAP]) -> None:
        """Save sniffer AP results as CSV. fsync'd."""
        if not self._session_active or not aps:
            return
        filepath = self._session / "sniffer_aps.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["SSID", "Channel", "Clients", "Client_MACs"])
                for ap in aps:
                    writer.writerow([
                        ap.ssid, ap.channel, ap.client_count,
                        ";".join(ap.clients),
                    ])
                _fsync_file(fh)
            log.info("Sniffer APs saved: %d", len(aps))
        except OSError as exc:
            log.error("Cannot save sniffer APs: %s", exc)

    def save_sniffer_probes(self, probes: List[ProbeEntry]) -> None:
        """Save probe requests as CSV. fsync'd."""
        if not self._session_active or not probes:
            return
        filepath = self._session / "sniffer_probes.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["SSID", "MAC"])
                for p in probes:
                    writer.writerow([p.ssid, p.mac])
                _fsync_file(fh)
            log.info("Sniffer probes saved: %d", len(probes))
        except OSError as exc:
            log.error("Cannot save sniffer probes: %s", exc)

    # ------------------------------------------------------------------
    # Portal
    # ------------------------------------------------------------------

    def save_portal_event(self, line: str) -> None:
        """Append a portal password/form submission line. fsync'd — this is
        captured-credential data we can't afford to lose on power failure."""
        if not self._session_active:
            return
        filepath = self._session / "portal_passwords.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {line}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # Evil Twin
    # ------------------------------------------------------------------

    def save_evil_twin_event(self, line: str) -> None:
        """Append an evil twin capture line. fsync'd — captured credentials."""
        if not self._session_active:
            return
        filepath = self._session / "evil_twin_capture.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {line}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # Attack events
    # ------------------------------------------------------------------

    def log_attack_event(self, event: str) -> None:
        """Log attack start/stop/result events. fsync'd."""
        if not self._session_active:
            return
        filepath = self._session / "attacks.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {event}\n")

    # ------------------------------------------------------------------
    # MeshCore
    # ------------------------------------------------------------------

    def save_meshcore_node(self, node_id: str, node_type: str, name: str,
                           lat: float, lon: float, rssi: float, snr: float) -> None:
        """Upsert one session node, including later GPS/name updates."""
        if not self._session_active:
            return
        try:
            with self._db_lock:
                contact = self._merge_mesh_contact("meshcore", node_id,
                    self._db.get("mc_contacts", {}).get(node_id, {}),
                    {"type": node_type, "name": name, "lat": lat, "lon": lon,
                     "rssi": rssi, "snr": snr})
                self._upsert_mesh_node_csv("meshcore", contact)
                self.update_session_loot()
        except OSError:
            pass

    def save_meshtastic_node(self, node_id: str, node_type: str, name: str,
                             lat: float, lon: float, rssi: float, snr: float) -> None:
        """Export validated node metadata without independently granting XP."""
        if not self._session_active:
            return
        node_id = self._mesh_node_id(node_id)
        with self._db_lock:
            contact = self._merge_mesh_contact("meshtastic", node_id,
                self._db.get("mt_contacts", {}).get(node_id, {}),
                {"type": node_type, "name": name, "lat": lat, "lon": lon,
                 "rssi": rssi, "snr": snr})
            self._upsert_mesh_node_csv("meshtastic", contact)
            self.update_session_loot()

    def save_meshcore_message(self, channel: str, message: str, rssi: float) -> None:
        """Append message to meshcore_messages.log. fsync'd."""
        if not self._session_active:
            return
        path = self._session / "meshcore_messages.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] [{channel}] {message} (RSSI:{rssi})\n")
        self.update_session_loot()

    def save_meshtastic_message(self, channel: str, message: str, rssi: float,
                                direction: str = "rx") -> None:
        """Save public chat history independently from MeshCore, without XP."""
        if not self._session_active:
            return
        if direction not in ("rx", "tx"):
            raise ValueError("invalid mesh chat direction")
        row = {"timestamp": datetime.now().isoformat(timespec="seconds"),
               "channel": str(channel), "message": str(message), "rssi": float(rssi),
               "direction": direction}
        with self._db_lock:
            with (self._session / "meshtastic_chat.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            readable = str(message).replace("\r", "\\r").replace("\n", "\\n")
            _sync_append(self._session / "meshtastic_messages.log",
                         f"[{row['timestamp']}] [{channel}] [{direction}] {readable} (RSSI:{rssi})\n")
            self.update_session_loot()

    def load_meshtastic_messages(self, limit: int = 200) -> list[tuple]:
        """Return (channel, message, rssi, timestamp), oldest first, bounded tail."""
        limit = max(0, min(int(limit), 2000))
        if not limit:
            return []
        rows = []
        with self._db_lock:
            for path in sorted(self._base.glob("*/meshtastic_chat.jsonl")):
                try:
                    with path.open(encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                item = json.loads(line)
                                if not isinstance(item.get("message"), str):
                                    continue
                                rows.append((str(item.get("channel", "SFNarrow")), item["message"],
                                             float(item.get("rssi", 0)), str(item.get("timestamp", ""))))
                                if len(rows) > limit:
                                    rows = rows[-limit:]
                            except (ValueError, TypeError, AttributeError):
                                continue
                except OSError:
                    continue
        return rows

    def save_wdgwars_message(self, nick: str, message: str) -> None:
        """Append a WDGWars #general chat message to wdgwars_chat.log."""
        if not self._session_active:
            return
        path = self._session / "wdgwars_chat.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] {nick}: {message}\n")

    # ------------------------------------------------------------------
    # Bluetooth
    # ------------------------------------------------------------------

    def save_bt_device(self, mac: str, rssi: int, name: str,
                       is_airtag: bool, is_smarttag: bool) -> bool:
        """Append BLE device to bt_devices.csv (dedup by MAC). fsync'd."""
        if not self._session_active or not mac:
            return False
        path = self._session / "bt_devices.csv"
        # Get GPS coords if available
        lat, lon = 0.0, 0.0
        if self._gps and self._gps.available:
            fix = self._gps.fix
            if fix.valid:
                lat = round(fix.latitude, 7)
                lon = round(fix.longitude, 7)
        if path.is_file():
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                return self._wardrive_save_failed(path, exc)
            if f",{mac}," in existing or existing.startswith(f"{mac},"):
                return False  # already known, not a persistence failure
        else:
            existing = ""
        if not existing:
            existing = "timestamp,mac,rssi,name,airtag,smarttag,lat,lon\n"
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if not self._commit_wardrive_csv(path, [existing,
            f"{ts},{mac},{rssi},{name},{is_airtag},{is_smarttag},"
            f"{lat},{lon}\n"]):
            return False
        self.update_session_loot()
        return True

    def save_bt_airtag_event(self, airtags: int, smarttags: int) -> None:
        """Log an AirTag scanner detection event. fsync'd."""
        if not self._session_active:
            return
        path = self._session / "bt_airtag.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] AirTags:{airtags} | SmartTags:{smarttags}\n")
        self.update_session_loot()

    def save_adsb_aircraft(self, icao: str, callsign: str = "",
                           lat: float = 0.0, lon: float = 0.0,
                           alt_ft: int = 0, speed_kt: int = 0,
                           heading: int = 0) -> None:
        """Append an ADS-B aircraft to adsb_aircraft.csv (dedup by ICAO). fsync'd."""
        if not self._session_active or not icao:
            return
        path = self._session / "adsb_aircraft.csv"
        if path.is_file():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if f",{icao}," in existing or f"\n{icao}," in existing:
                return  # already recorded this session
        else:
            _sync_write(path, "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading\n")
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _sync_append(
            path,
            f"{ts},{icao},{callsign or ''},"
            f"{round(lat, 7)},{round(lon, 7)},"
            f"{alt_ft},{speed_kt},{heading}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # GPS point collection (for Map tab)
    # ------------------------------------------------------------------

    _gps_points_cache: list[dict] | None = None
    _gps_points_ts: float = 0.0

    def get_gps_points(self) -> list[dict]:
        """Collect all GPS-tagged loot across all sessions.

        Returns list of dicts: {lat, lon, type, label}.
        Cached for 30 seconds to avoid excessive FS scans.
        """
        now = time.monotonic()
        if self._gps_points_cache is not None and now - self._gps_points_ts < 30.0:
            return self._gps_points_cache

        points: list[dict] = []
        if not self._base.is_dir():
            self._gps_points_cache = points
            self._gps_points_ts = now
            return points

        for session_dir in self._base.iterdir():
            if not session_dir.is_dir():
                continue

            # Wardriving CSV (WiGLE format)
            wd_file = session_dir / "wardriving.csv"
            if wd_file.is_file():
                try:
                    with open(wd_file, "r", encoding="utf-8") as fh:
                        for i, line in enumerate(fh):
                            if i <= 1:
                                continue  # skip pre-header + header
                            parts = line.strip().split(",")
                            if len(parts) >= 8:
                                try:
                                    lat = float(parts[6])
                                    lon = float(parts[7])
                                    if lat != 0.0 or lon != 0.0:
                                        # Detect Type column (11th col = index 10)
                                        ptype = "wifi"
                                        if len(parts) >= 11 and parts[10].strip().upper() == "BLE":
                                            ptype = "bt"
                                        points.append({
                                            "lat": lat, "lon": lon,
                                            "type": ptype,
                                            "label": parts[1],  # SSID/Name
                                            "bssid": parts[0],
                                            "auth": parts[2] if len(parts) > 2 else "",
                                            "rssi": parts[5] if len(parts) > 5 else "",
                                            "channel": parts[4] if len(parts) > 4 else "",
                                        })
                                except (ValueError, IndexError):
                                    pass
                except OSError:
                    pass

            # BT devices CSV (only WiFi + BT on map, no handshake/meshcore)
            bt_file = session_dir / "bt_devices.csv"
            if bt_file.is_file():
                try:
                    with open(bt_file, "r", encoding="utf-8") as fh:
                        reader = csv.DictReader(fh)
                        for row in reader:
                            try:
                                lat = float(row.get("lat", 0))
                                lon = float(row.get("lon", 0))
                                if lat != 0.0 or lon != 0.0:
                                    points.append({
                                        "lat": lat, "lon": lon,
                                        "type": "bt",
                                        "label": row.get("name", row.get("mac", "?")),
                                        "bssid": row.get("mac", ""),
                                        "auth": "[BLE]",
                                        "rssi": row.get("rssi", ""),
                                        "channel": "",
                                    })
                            except (ValueError, TypeError):
                                pass
                except OSError:
                    pass

        self._gps_points_cache = points
        self._gps_points_ts = now
        return points

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close file handles, write session summary, and update loot DB."""
        # Stop background sync thread before closing the handle it touches
        self._backup_stop = True
        self.flush_all()

        if self._serial_fh:
            try:
                self._serial_fh.close()
            except OSError:
                pass
            self._serial_fh = None

        # Write session summary
        if self._session_active:
            try:
                summary = self._session / "session_info.txt"
                with open(summary, "w", encoding="utf-8") as fh:
                    fh.write(f"Session ended: {datetime.now().isoformat()}\n")
                    # List files in session
                    for f in sorted(self._session.rglob("*")):
                        if f.is_file() and f.name != "session_info.txt":
                            size = f.stat().st_size
                            fh.write(f"  {f.relative_to(self._session)} "
                                     f"({size} bytes)\n")
                    _fsync_file(fh)
            except OSError:
                pass

            # Final DB update + one last OS sync so every file on the card
            # reflects our final state.
            self.update_session_loot()
            try:
                os.sync()
            except AttributeError:
                pass
