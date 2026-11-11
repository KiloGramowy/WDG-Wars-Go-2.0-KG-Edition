"""Private policy and exact-file proof ledger for uploaded-log retention.

This class neither decides that an upload succeeded nor removes logs. Callers
must record only durable, complete upload acknowledgements and independently
recheck the account, exact file content, active session and in-flight work before
unlinking. Missing, corrupt or unwritable ledger state never authorizes deletion.
"""

import copy
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time


_FILENAMES = frozenset({"wardriving.csv", "adsb_aircraft.csv",
                        "meshcore_nodes.csv", "meshtastic_nodes.csv"})
_PROOF_FIELDS = frozenset({"session", "filename", "digest", "scope",
                           "receipt_key", "receipt_digest", "confirmed_at"})
_HEX = re.compile(r"[a-fA-F0-9]{64}\Z")
_MAX_DAYS = 999999


def _days(value):
    return type(value) is int and 0 <= value <= _MAX_DAYS


def _epoch(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _session(value):
    return (isinstance(value, str) and bool(value) and value.strip() == value
            and value not in (".", "..") and not any(c in value for c in "/\\:")
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _file(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid retention file")
    if (not _session(value.get("session")) or value.get("filename") not in _FILENAMES
            or not _hex(value.get("digest"))):
        raise ValueError("Invalid retention file")
    return {"session": value["session"], "filename": value["filename"],
            "digest": value["digest"].lower()}


def _proof(value):
    if not isinstance(value, dict) or set(value) != _PROOF_FIELDS:
        raise ValueError("Invalid retention proof")
    item = _file(value)
    if (not _hex(value["scope"]) or not _hex(value["receipt_key"])
            or not _hex(value["receipt_digest"])
            or not _epoch(value["confirmed_at"])):
        raise ValueError("Invalid retention proof")
    item.update(scope=value["scope"].lower(), receipt_key=value["receipt_key"].lower(),
                receipt_digest=value["receipt_digest"].lower(),
                confirmed_at=value["confirmed_at"])
    return item


def _identity(proof):
    return proof["session"], proof["filename"]


class UploadedLogRetention:
    """Persistent opt-in retention policy, with first-confirmation timestamps."""

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._days = 0
        self._proofs = {}
        self._stamp = None
        self._loaded = False
        self._valid = True
        with self._lock:
            self._reload_if_changed()

    @property
    def days(self):
        with self._lock:
            return self._days if self._valid else 0

    def _signature(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Retention state is not a regular file")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)

    def _reload_if_changed(self):
        try:
            signature = self._signature()
            if self._loaded and signature == self._stamp:
                return
            if signature is None:
                days, proofs = 0, {}
            else:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if (not isinstance(raw, dict) or type(raw.get("version")) is not int
                        or raw["version"] != 1 or not isinstance(raw.get("policy"), dict)
                        or not _days(raw["policy"].get("days"))
                        or not isinstance(raw.get("proofs"), list)):
                    raise ValueError("Invalid retention state")
                days, proofs = raw["policy"]["days"], {}
                for value in raw["proofs"]:
                    item = _proof(value)
                    key = _identity(item)
                    if key in proofs:
                        raise ValueError("Ambiguous retention proof")
                    proofs[key] = item
                if self._signature() != signature:
                    raise OSError("Retention state changed while being read")
            self._days, self._proofs = days, proofs
            self._valid = True
            self._stamp = signature
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
            self._days, self._proofs = 0, {}
            self._valid = False
            # Retry a failed read on the next use, including transient failures.
            self._stamp = object()
        self._loaded = True

    def _writable(self):
        """Respect explicit read-only permissions even for a root-launched game."""
        try:
            if self.path.is_symlink():
                return False
            if self.path.exists():
                info = self.path.stat()
                if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o222:
                    return False
                if not os.access(self.path, os.W_OK):
                    return False
            parent = self.path.parent
            if not parent.is_dir() or not parent.stat().st_mode & 0o222:
                return False
            return os.access(parent, os.W_OK | os.X_OK)
        except OSError:
            return False

    def _persist(self, days, proofs):
        if not self._valid or not self._writable():
            return False
        temporary = None
        try:
            previous = self.path.stat() if self.path.exists() else None
            descriptor, temporary = tempfile.mkstemp(prefix=".uploaded-log-retention-",
                                                      dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                if previous is not None and hasattr(os, "fchown"):
                    os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
                json.dump({"version": 1, "policy": {"days": days},
                           "proofs": [proofs[key] for key in sorted(proofs)]},
                          stream, allow_nan=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
            os.replace(temporary, self.path)
            self._days, self._proofs = days, proofs
            try:
                current = self._signature()
            except OSError:
                current = None
            own = (written.st_dev, written.st_ino, written.st_size, written.st_mtime_ns,
                   written.st_mode, written.st_uid, written.st_gid)
            # Rename may change ctime. An intervening replacement/removal must
            # force a reload rather than pairing our cache with another file.
            self._stamp = (current if current is not None
                           and current[:4] + current[5:] == own else object())
            self._loaded = True
            return True
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
            return False
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def set_days(self, days):
        if not _days(days):
            return False
        with self._lock:
            self._reload_if_changed()
            if not self._valid or not self._writable():
                return False
            if days == self._days and self.path.exists():
                return True
            return self._persist(days, copy.deepcopy(self._proofs))

    def record_many(self, files, *, scope, receipt_key, receipt_digest):
        """Record files already covered by a complete, durable caller ACK."""
        if (not isinstance(files, (list, tuple)) or not _hex(scope)
                or not _hex(receipt_key) or not _hex(receipt_digest)):
            return False
        try:
            items = [_file(value) for value in files]
            if len({_identity(value) for value in items}) != len(items):
                return False
        except (OSError, ValueError, TypeError, OverflowError):
            return False
        try:
            now = self._clock()
            if not _epoch(now):
                return False
        except Exception:
            return False
        with self._lock:
            self._reload_if_changed()
            if not self._valid or not self._writable():
                return False
            updated = copy.deepcopy(self._proofs)
            scope, receipt_key = scope.lower(), receipt_key.lower()
            receipt_digest = receipt_digest.lower()
            for item in items:
                key = _identity(item)
                previous = updated.get(key)
                confirmed_at = (previous["confirmed_at"] if previous is not None
                    and previous["digest"] == item["digest"] and previous["scope"] == scope
                    else now)
                updated[key] = dict(item, scope=scope, receipt_key=receipt_key,
                                    receipt_digest=receipt_digest,
                                    confirmed_at=confirmed_at)
            if updated == self._proofs:
                return True
            return self._persist(self._days, updated)

    def due(self, now=None):
        with self._lock:
            self._reload_if_changed()
            if not self._valid or not self._days or not self._writable():
                return []
            try:
                now = self._clock() if now is None else now
            except Exception:
                return []
            try:
                if not _epoch(now):
                    return []
                age = self._days * 86400
                values = [item for item in self._proofs.values()
                          if now >= item["confirmed_at"]
                          and now - item["confirmed_at"] >= age]
                values.sort(key=lambda item: (item["confirmed_at"], item["session"], item["filename"]))
                return copy.deepcopy(values)
            except (OSError, ValueError, TypeError, OverflowError):
                return []

    def forget(self, proof):
        try:
            proof = _proof(proof)
        except (ValueError, TypeError, OverflowError):
            return False
        with self._lock:
            self._reload_if_changed()
            if not self._valid or not self._writable():
                return False
            key = _identity(proof)
            if self._proofs.get(key) != proof:
                return False
            updated = copy.deepcopy(self._proofs)
            del updated[key]
            return self._persist(self._days, updated)
