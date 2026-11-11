"""Durable local Piglet transfer/import. No network setup, external upload or RF."""
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid

from .piglet_csv import NATIVE_HEADER, PREAMBLE, native_row, piglet_records

MAX_SIZE = 2 * 1024**3
MAX_CHUNK = 16384
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")


class CollectionError(RuntimeError):
    def __init__(self, message, status=400, *, offset=None):
        super().__init__(message)
        self.status = self.status_code = status
        self.offset = offset


def _fsync_directory(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise CollectionError("Collection directory must not be a link", 503)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise CollectionError("Collection directory is unavailable", 503)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CollectionError("Collection file is not an independent regular file", 503)
    return info


def _digest_file(path, cancelled=None):
    before = _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(256 * 1024), b""):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Collection stopped; source retained")
            digest.update(data)
    after = _regular(path)
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise CollectionError("Source changed during verification", 409)
    return digest.hexdigest()


class PigletCollectionStore:
    def __init__(self, loot, state_dir=None):
        self.loot = loot
        self.root = _private_directory(state_dir or Path(loot._base) / ".piglet_collection")
        self.raw = _private_directory(self.root / "raw")
        self.exports = _private_directory(self.root / "exports")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._closed = False
        self._cancelled = False
        self._overview_cache = dict(pending_files=0, active_devices=[], sessions=[], files=[],
                                    wifi=0, ble=0, duplicates=0, records=0, received_bytes=0, total_bytes=0)
        index = self.root / "index.sqlite3"
        if index.exists():
            _regular(index)
        self._sql = sqlite3.connect(index, check_same_thread=False, timeout=5)
        self._sql.row_factory = sqlite3.Row
        self._sql.execute("PRAGMA journal_mode=WAL")
        self._sql.execute("PRAGMA synchronous=FULL")
        if self._sql.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            self._sql.close()
            raise CollectionError("Collection index is damaged; source files were kept", 503)
        self._sql.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, device TEXT NOT NULL,
                expected INTEGER NOT NULL, finished INTEGER NOT NULL DEFAULT 0, touched REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS files(sha TEXT PRIMARY KEY,size INTEGER NOT NULL,name TEXT NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'receiving',
                prepared INTEGER NOT NULL DEFAULT 0,exported INTEGER NOT NULL DEFAULT 0,
                records INTEGER NOT NULL DEFAULT 0,wifi INTEGER NOT NULL DEFAULT 0,
                ble INTEGER NOT NULL DEFAULT 0,duplicates INTEGER NOT NULL DEFAULT 0,
                import_id TEXT NOT NULL,session_name TEXT,error TEXT,receipt TEXT,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS session_files(session TEXT NOT NULL,sha TEXT NOT NULL,name TEXT NOT NULL,
                PRIMARY KEY(session,sha,name));
            CREATE TABLE IF NOT EXISTS prepared(sha TEXT NOT NULL,ordinal INTEGER NOT NULL,
                record_id TEXT NOT NULL,protocol TEXT NOT NULL,mac TEXT NOT NULL,rssi INTEGER NOT NULL,
                record TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(sha,ordinal),UNIQUE(sha,record_id));
            CREATE INDEX IF NOT EXISTS prepared_pending ON prepared(sha,done,ordinal);
            CREATE TABLE IF NOT EXISTS records(record_id TEXT PRIMARY KEY,sha TEXT NOT NULL,ordinal INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS bulk_groups(sha TEXT NOT NULL,protocol TEXT NOT NULL,mac TEXT NOT NULL,
                count INTEGER NOT NULL,ordinal INTEGER NOT NULL,record_id TEXT NOT NULL,record TEXT NOT NULL,
                PRIMARY KEY(sha,protocol,mac));
            CREATE TABLE IF NOT EXISTS collection_jobs(id TEXT PRIMARY KEY,created REAL NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS job_devices(job TEXT NOT NULL,device TEXT NOT NULL,session TEXT NOT NULL,
                expected_floor INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(job,device));
            CREATE TABLE IF NOT EXISTS job_sources(job TEXT NOT NULL,device TEXT NOT NULL,
                source_id TEXT NOT NULL,sha TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(job,device,source_id));
            CREATE TABLE IF NOT EXISTS session_sources(session TEXT NOT NULL,source_id TEXT NOT NULL,
                sha TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(session,source_id));
            CREATE INDEX IF NOT EXISTS job_sources_sha ON job_sources(job,sha);
        """)
        # Additive transfer-index metadata; player data and receipt schema stay
        # unchanged. Old imports lack an exact new-identity reward breakdown.
        columns = {row[1] for row in self._sql.execute("PRAGMA table_info(files)")}
        for name, declaration in (("new_wifi", "INTEGER NOT NULL DEFAULT 0"),
                                  ("new_ble", "INTEGER NOT NULL DEFAULT 0"),
                                  ("base_xp", "INTEGER NOT NULL DEFAULT 0"),
                                  ("summary_complete", "INTEGER NOT NULL DEFAULT 0"),
                                  ("owner_job", "TEXT"),
                                  ("bulk_ready", "INTEGER NOT NULL DEFAULT 0"),
                                  ("bulk_processed", "INTEGER NOT NULL DEFAULT 0"),
                                  ("bulk_id", "TEXT")):
            if name not in columns:
                self._sql.execute(f"ALTER TABLE files ADD COLUMN {name} {declaration}")
        columns = {row[1] for row in self._sql.execute("PRAGMA table_info(sessions)")}
        for name, declaration in (("job", "TEXT"), ("source_items", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self._sql.execute(f"ALTER TABLE sessions ADD COLUMN {name} {declaration}")
        columns = {row[1] for row in self._sql.execute("PRAGMA table_info(job_devices)")}
        if "expected_floor" not in columns:
            self._sql.execute("ALTER TABLE job_devices ADD COLUMN expected_floor INTEGER NOT NULL DEFAULT 0")
        self._sql.execute("UPDATE job_devices SET expected_floor=MAX(expected_floor,"
                          "COALESCE((SELECT MAX(expected) FROM sessions WHERE job=job_devices.job "
                          "AND device=job_devices.device),0))")
        with self._sql:
            self._sql.execute("INSERT OR IGNORE INTO meta VALUES('collector_id',?)", (uuid.uuid4().hex,))
        self.collector_id = self._sql.execute("SELECT value FROM meta WHERE key='collector_id'").fetchone()[0]
        if not _ID.fullmatch(self.collector_id):
            raise CollectionError("Invalid stored collector identity", 503)
        self._recover_markers()
        self._recover_receipts()
        self._overview_cache["import"] = self._job_summary()
        self._worker = threading.Thread(target=self._worker_main, name="piglet-import", daemon=True)
        self._worker.start()
        self._wake.set()

    @property
    def cancelled(self):
        return self._cancelled

    @property
    def max_chunk(self):
        return MAX_CHUNK

    def _check_open(self):
        if self._closed:
            raise CollectionError("Collection store is closed", 503)

    def _session(self, session_id, *, writable=True):
        self._check_open()
        if writable and self._cancelled:
            raise CollectionError("Collection stopped; original CSV retained", 503)
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise CollectionError("Unknown Collection session", 404)
        row = self._sql.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise CollectionError("Unknown Collection session", 404)
        if writable and row["finished"]:
            raise CollectionError("Collection session is finished", 409)
        return row

    def _file(self, session_id, sha, *, writable=True):
        self._session(session_id, writable=writable)
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise CollectionError("Invalid file digest")
        row = self._sql.execute("SELECT files.* FROM files JOIN session_files ON files.sha=session_files.sha "
            "WHERE files.sha=? AND session_files.session=? LIMIT 1", (sha, session_id)).fetchone()
        if row is None:
            raise CollectionError("File was not offered in this session", 404)
        return row

    def _path(self, sha):
        return self.raw / (sha + ".csv")

    def begin(self, device_id, pending_files, *, source_items=0):
        if (not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", device_id)
                or type(pending_files) is not int or not 0 <= pending_files <= 1000000
                or type(source_items) is not int or source_items not in (0, 1)):
            raise CollectionError("Invalid device ID or pending file count")
        with self._lock, self._sql:
            self._check_open()
            if self._cancelled:
                raise CollectionError("Collection stopped; original CSV retained", 503)
            session = uuid.uuid4().hex
            job = self._sql.execute("SELECT id FROM collection_jobs WHERE completed=0 ORDER BY created DESC LIMIT 1").fetchone()
            job_id = job["id"] if job is not None else uuid.uuid4().hex
            previous = self._sql.execute("SELECT s.expected,(SELECT COUNT(*) FROM session_sources ss "
                "WHERE ss.session=s.id) offered FROM job_devices jd JOIN sessions s ON s.id=jd.session "
                "WHERE jd.job=? AND jd.device=?", (job_id, device_id)).fetchone()
            if previous is not None and pending_files < max(0, previous["expected"]-previous["offered"]):
                # A new connection cannot erase announced-but-unidentified
                # source obligations, even if a sender lost its RAM inventory.
                raise CollectionError("Previous Collection source inventory remains incomplete", 409)
            self._sql.execute("INSERT OR IGNORE INTO collection_jobs(id,created) VALUES(?,?)", (job_id, time.time()))
            self._sql.execute("INSERT INTO sessions(id,device,expected,touched,job,source_items) VALUES(?,?,?,?,?,?)",
                              (session, device_id, pending_files, time.time(), job_id, source_items))
            self._sql.execute("INSERT INTO job_devices(job,device,session,expected_floor) VALUES(?,?,?,?) "
                              "ON CONFLICT(job,device) DO UPDATE SET session=excluded.session,"
                              "expected_floor=MAX(job_devices.expected_floor,excluded.expected_floor)",
                              (job_id, device_id, session, pending_files))
            self._overview_cache["import"] = self._job_summary(job_id)
        result = dict(protocol=1, collector_id=self.collector_id, session_id=session)
        if source_items:
            result["source_items"] = 1
        return result

    def offer(self, session_id, name, size, sha256, *, source_id=None):
        if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 255
                or PurePosixPath(name).name != name or PureWindowsPath(name).name != name
                or any(ord(c) < 32 for c in name) or not name.lower().endswith(".csv")
                or type(size) is not int or not 0 <= size <= MAX_SIZE
                or not isinstance(sha256, str) or not _SHA.fullmatch(sha256)):
            raise CollectionError("Invalid closed CSV offer")
        with self._lock, self._sql:
            session = self._session(session_id)
            if session["source_items"]:
                if not isinstance(source_id, str) or not _SHA.fullmatch(source_id):
                    raise CollectionError("Negotiated Collection source identity is required")
            elif source_id is not None:
                raise CollectionError("Collection source identities were not negotiated")
            item_id = source_id or "legacy:" + hashlib.sha256((sha256 + "\n" + name).encode("utf-8")).hexdigest()
            previous = self._sql.execute("SELECT sha,name FROM session_sources WHERE session=? AND source_id=?",
                                         (session_id, item_id)).fetchone()
            if previous is not None and (previous["sha"], previous["name"]) != (sha256, name):
                raise CollectionError("Source identity changed within this Collection session", 409)
            if previous is None and session["source_items"]:
                offered = self._sql.execute("SELECT COUNT(*) FROM session_sources WHERE session=?", (session_id,)).fetchone()[0]
                if offered >= session["expected"]:
                    raise CollectionError("More sources offered than announced", 409)
            old_job_item = self._sql.execute("SELECT sha,name FROM job_sources WHERE job=? AND device=? AND source_id=?",
                                            (session["job"], session["device"], item_id)).fetchone()
            if old_job_item is not None and (old_job_item["sha"], old_job_item["name"]) != (sha256, name):
                raise CollectionError("Source identity changed within this Collection job", 409)
            row = self._sql.execute("SELECT * FROM files WHERE sha=?", (sha256,)).fetchone()
            if row is None:
                if shutil.disk_usage(self.root).free < size + 1024 * 1024:
                    raise CollectionError("Not enough storage for this CSV; source kept", 507)
                path = self._path(sha256)
                if path.exists():
                    # Creation/fsync may have succeeded just before the first
                    # index transaction failed. Only our own empty, independent
                    # regular file can be adopted; unknown bytes are never reset.
                    info = _regular(path)
                    owner = getattr(os, "geteuid", None)
                    if info.st_size != 0 or owner is not None and info.st_uid != owner():
                        raise CollectionError("Unindexed source file retained for review", 503)
                    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        opened = os.fstat(descriptor)
                        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size != 0
                                or owner is not None and opened.st_uid != owner()):
                            raise CollectionError("Unindexed source changed; retained for review", 503)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                else:
                    with path.open("xb") as fh:
                        fh.flush()
                        os.fsync(fh.fileno())
                if os.name != "nt":
                    path.chmod(0o600)
                _fsync_directory(self.raw)
                self._sql.execute("INSERT INTO files(sha,size,name,import_id,created,summary_complete,owner_job) VALUES(?,?,?,?,?,1,?)",
                                  (sha256, size, name, uuid.uuid4().hex, time.time(), session["job"]))
            elif row["size"] != size:
                raise CollectionError("Digest was offered with a different size", 409)
            self._sql.execute("INSERT OR IGNORE INTO session_files VALUES(?,?,?)", (session_id, sha256, name))
            self._sql.execute("INSERT OR IGNORE INTO session_sources VALUES(?,?,?,?)", (session_id, item_id, sha256, name))
            if source_id:
                # An old name/digest-only session may have left one ambiguous
                # placeholder. A negotiated real path replaces that placeholder.
                self._sql.execute("DELETE FROM job_sources WHERE job=? AND device=? AND sha=? AND name=? AND source_id LIKE 'legacy:%'",
                                  (session["job"], session["device"], sha256, name))
            self._sql.execute("INSERT OR IGNORE INTO job_sources VALUES(?,?,?,?,?)",
                              (session["job"], session["device"], item_id, sha256, name))
            self._sql.execute("UPDATE files SET owner_job=? WHERE sha=? AND owner_job IS NULL AND state!='committed'",
                              (session["job"], sha256))
            self._sql.execute("UPDATE sessions SET touched=? WHERE id=?", (time.time(), session_id))
            self._repair_tail(sha256)
            return self._status(self._sql.execute("SELECT * FROM files WHERE sha=?", (sha256,)).fetchone())

    def _repair_tail(self, sha):
        row = self._sql.execute("SELECT offset,state FROM files WHERE sha=?", (sha,)).fetchone()
        path = self._path(sha)
        info = _regular(path)
        if info.st_size < row["offset"]:
            raise CollectionError("Durable source prefix is missing; no acknowledgement", 503)
        if info.st_size > row["offset"] and row["state"] == "receiving":
            with path.open("r+b") as fh:
                fh.truncate(row["offset"])
                fh.flush()
                os.fsync(fh.fileno())

    def chunk(self, session_id, sha256, offset, data, chunk_sha256):
        if (type(offset) is not int or offset < 0 or not isinstance(data, bytes)
                or not 0 < len(data) <= MAX_CHUNK or not isinstance(chunk_sha256, str)
                or not _SHA.fullmatch(chunk_sha256)
                or hashlib.sha256(data).hexdigest() != chunk_sha256):
            raise CollectionError("Invalid chunk or digest")
        with self._lock, self._sql:
            row = self._file(session_id, sha256)
            expected = row["offset"]
            if row["state"] != "receiving":
                raise CollectionError("File is not receiving", 409, offset=expected)
            self._repair_tail(sha256)
            path = self._path(sha256)
            if offset < expected and offset + len(data) <= expected:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    if fh.read(len(data)) != data:
                        raise CollectionError("Conflicting retried chunk", 409, offset=expected)
                return self._status(row)
            if offset != expected or expected + len(data) > row["size"]:
                raise CollectionError("Unexpected chunk offset", 409, offset=expected)
            with path.open("r+b") as fh:
                fh.seek(expected)
                if fh.write(data) != len(data):
                    raise OSError("Short Collection source write")
                fh.flush()
                os.fsync(fh.fileno())
            self._sql.execute("UPDATE files SET offset=? WHERE sha=?", (expected + len(data), sha256))
            self._sql.execute("UPDATE sessions SET touched=? WHERE id=?", (time.time(), session_id))
            return self._status(self._sql.execute("SELECT * FROM files WHERE sha=?", (sha256,)).fetchone())

    def commit(self, session_id, sha256):
        with self._lock, self._sql:
            row = self._file(session_id, sha256)
            if row["state"] == "committed":
                return self._status(row)
            if row["offset"] != row["size"]:
                raise CollectionError("File is incomplete", 409, offset=row["offset"])
            self._repair_tail(sha256)
            self._sql.execute("UPDATE files SET state='processing',error=NULL WHERE sha=?", (sha256,))
            self._wake.set()
            return self._status(self._sql.execute("SELECT * FROM files WHERE sha=?", (sha256,)).fetchone())

    def status(self, session_id, sha256):
        with self._lock:
            return self._status(self._file(session_id, sha256, writable=False))

    def _status(self, row):
        result = {key: row[key] for key in ("size", "offset", "state", "records", "wifi", "ble", "duplicates",
                                            "new_wifi", "new_ble", "base_xp")}
        result["summary_complete"] = bool(row["summary_complete"])
        result.update(sha256=row["sha"], name=row["name"], collector_id=self.collector_id)
        processed = max(row["wifi"] + row["ble"] + row["duplicates"], row["bulk_processed"])
        result["import_percent"] = (100 if row["state"] == "committed" else
            min(99, processed * 100 // row["records"]) if row["prepared"] and row["records"] else 0)
        if row["error"]:
            result["error"] = row["error"]
        if row["state"] == "committed":
            result["receipt"] = json.loads(row["receipt"])
        return result

    def finish(self, session_id):
        with self._lock, self._sql:
            session = self._session(session_id, writable=False)
            table = "session_sources" if session["source_items"] else "session_files"
            offers = self._sql.execute(f"SELECT COUNT(*) FROM {table} WHERE session=?", (session_id,)).fetchone()[0]
            pending = self._sql.execute(f"SELECT COUNT(*) FROM {table} sf JOIN files ON files.sha=sf.sha "
                "WHERE session=? AND files.state!='committed'", (session_id,)).fetchone()[0]
            if session["job"]:
                pending = self._sql.execute("SELECT COUNT(*) FROM job_sources js JOIN files f ON f.sha=js.sha "
                    "WHERE js.job=? AND js.device=? AND f.state!='committed'",
                    (session["job"], session["device"])).fetchone()[0]
            source_pending = self._pending_event_files()
            if source_pending:
                placeholders = ",".join("?" for _ in source_pending)
                if session["job"]:
                    pending += self._sql.execute(f"SELECT COUNT(*) FROM job_sources WHERE job=? AND device=? AND sha IN ({placeholders})",
                                                 (session["job"], session["device"], *source_pending)).fetchone()[0]
                else:
                    pending += self._sql.execute(f"SELECT COUNT(*) FROM {table} WHERE session=? AND sha IN ({placeholders})",
                                                 (session_id, *source_pending)).fetchone()[0]
            obligation = self._sql.execute("SELECT MAX(0,jd.expected_floor-(SELECT COUNT(*) FROM job_sources js "
                "WHERE js.job=jd.job AND js.device=jd.device)) FROM job_devices jd WHERE jd.job=? AND jd.device=?",
                (session["job"], session["device"])).fetchone()
            if obligation is not None:
                pending += obligation[0]
            complete = not pending and (offers == session["expected"] if session["source_items"] else offers >= session["expected"])
            if complete:
                self._sql.execute("UPDATE sessions SET finished=1,touched=? WHERE id=?", (time.time(), session_id))
            unfinished = self._sql.execute("SELECT COUNT(*) FROM job_devices jd JOIN sessions s ON s.id=jd.session "
                                           "WHERE jd.job=? AND s.finished=0", (session["job"],)).fetchone()[0]
            job_done = (bool(session["job"]) and not unfinished
                        and self._job_summary(session["job"])["status"] == "complete")
            if complete and job_done:
                self._sql.execute("UPDATE collection_jobs SET completed=1 WHERE id=?", (session["job"],))
            active = self._sql.execute("SELECT COUNT(DISTINCT device) FROM sessions s WHERE device!=? AND finished=0 "
                "AND (touched>? OR EXISTS(SELECT 1 FROM session_files sf JOIN files f ON f.sha=sf.sha "
                "WHERE sf.session=s.id AND f.state!='committed'))", (session["device"], time.time()-30)).fetchone()[0]
            # A declared peer can hash a large file without making requests for
            # longer than the liveness window. Its missing offers/finish remain
            # durable work; elapsed touch time must never authorize shutdown.
            all_done = complete and job_done and not active
            return dict(finished=complete, all_done=all_done,
                        shutdown_allowed=all_done, pending_files=pending, other_active_devices=active)

    def job_file_shas(self, job_id, *, credited_only=True):
        """On-demand membership, excluding receipts earned in older jobs by default."""
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            return []
        with self._lock:
            self._check_open()
            query = "SELECT DISTINCT f.sha FROM job_sources js JOIN files f ON f.sha=js.sha WHERE js.job=?"
            parameters = [job_id]
            if credited_only:
                query += " AND f.owner_job=?"
                parameters.append(job_id)
            return [row[0] for row in self._sql.execute(query + " ORDER BY f.sha", parameters)]

    def _job_summary(self, job_id=None):
        """File-weighted durable processing, never transfer-byte progress.

        Unoffered source items each contribute zero. A reconnect can announce
        previously known items; until those offers arrive the conservative
        denominator includes both known items and not-yet-identified slots.
        """
        if job_id is None:
            latest = self._sql.execute("SELECT id FROM collection_jobs ORDER BY created DESC LIMIT 1").fetchone()
            job_id = latest[0] if latest is not None else None
        result = dict(job_id=job_id, percent=0, status="waiting", expected_files=0, offered_files=0,
                      completed_files=0, new_wifi=0, new_ble=0, duplicates=0, base_xp=0,
                      pending_events=0, summary_complete=True)
        if job_id is None:
            return result
        aggregate = self._sql.execute("SELECT COUNT(*) offered, COALESCE(SUM(f.state='committed'),0) completed, "
            "COALESCE(SUM(f.state='error'),0) errors, COALESCE(SUM(CASE WHEN f.state='committed' THEN 100 "
            "WHEN f.prepared=1 AND f.records>0 THEN MIN(99,MAX(f.wifi+f.ble+f.duplicates,f.bulk_processed)*100/f.records) ELSE 0 END),0) units "
            "FROM job_sources js JOIN files f ON f.sha=js.sha WHERE js.job=?", (job_id,)).fetchone()
        expected = self._sql.execute("SELECT COALESCE(SUM(MAX(jd.expected_floor,"
            "(SELECT COUNT(*) FROM job_sources js WHERE js.job=jd.job AND js.device=jd.device) "
            "+MAX(0,s.expected-(SELECT COUNT(*) FROM session_sources ss WHERE ss.session=s.id)))),0) "
            "FROM job_devices jd JOIN sessions s ON s.id=jd.session WHERE jd.job=?", (job_id,)).fetchone()[0]
        missing = expected - aggregate["offered"]
        credits = self._sql.execute("SELECT COALESCE(SUM(CASE WHEN f.owner_job=? THEN f.new_wifi ELSE 0 END),0) new_wifi, "
            "COALESCE(SUM(CASE WHEN f.owner_job=? THEN f.new_ble ELSE 0 END),0) new_ble, "
            "COALESCE(SUM(CASE WHEN f.owner_job=? THEN f.base_xp ELSE 0 END),0) base_xp, "
            "COALESCE(SUM(CASE WHEN f.owner_job=? THEN f.duplicates+(js.copies-1)*f.records ELSE js.copies*f.records END),0) duplicates, "
            "COALESCE(MIN(CASE WHEN f.owner_job=? THEN f.summary_complete ELSE 1 END),1) summary_complete "
            "FROM (SELECT sha,COUNT(*) copies FROM job_sources WHERE job=? GROUP BY sha) js JOIN files f ON f.sha=js.sha",
            (job_id, job_id, job_id, job_id, job_id, job_id)).fetchone()
        pending_events = self._pending_event_sources()
        pending_files = set(pending_events.values())
        pending = 0
        if pending_files:
            placeholders = ",".join("?" for _ in pending_files)
            matching = {row[0] for row in self._sql.execute(
                f"SELECT DISTINCT sha FROM job_sources WHERE job=? AND sha IN ({placeholders})", (job_id, *pending_files))}
            pending = sum(sha in matching for sha in pending_events.values())
        complete = not missing and aggregate["offered"] == aggregate["completed"] and not pending and not aggregate["errors"]
        result.update({key: credits[key] for key in ("new_wifi", "new_ble", "base_xp", "duplicates")})
        result.update(expected_files=expected, offered_files=aggregate["offered"], completed_files=aggregate["completed"],
                      summary_complete=bool(credits["summary_complete"]), pending_events=pending,
                      percent=100 if complete else min(99, aggregate["units"] // expected) if expected else 0,
                      status="error" if aggregate["errors"] else "complete" if complete else "importing")
        return result

    def status_overview(self):
        """Bounded read-only UI snapshot; a busy worker never blocks the game loop."""
        if not self._lock.acquire(blocking=False):
            return dict(self._overview_cache, busy=True)
        try:
            self._check_open()
            aggregate = self._sql.execute("SELECT COUNT(*) total_files, "
                "COALESCE(SUM(state!='committed'),0) pending_files, COALESCE(SUM(wifi),0) wifi, "
                "COALESCE(SUM(ble),0) ble, COALESCE(SUM(duplicates),0) duplicates, "
                "COALESCE(SUM(records),0) records, COALESCE(SUM(offset),0) received_bytes, "
                "COALESCE(SUM(size),0) total_bytes FROM files").fetchone()
            result = dict(aggregate)
            result["states"] = dict(self._sql.execute("SELECT state,COUNT(*) FROM files GROUP BY state"))
            result["files"] = [self._status(row) for row in self._sql.execute(
                "SELECT * FROM files ORDER BY CASE state WHEN 'processing' THEN 0 WHEN 'receiving' THEN 1 "
                "WHEN 'error' THEN 2 ELSE 3 END,created DESC LIMIT 8")]
            rows = self._sql.execute("SELECT id,device,expected,finished,touched, "
                "(SELECT COUNT(*) FROM session_files sf JOIN files f ON f.sha=sf.sha "
                "WHERE sf.session=s.id AND f.state!='committed') pending_files "
                "FROM sessions s ORDER BY touched DESC LIMIT 16").fetchall()
            result["sessions"] = [dict(session_id=row["id"], device_id=row["device"],
                expected_files=row["expected"], pending_files=row["pending_files"], finished=bool(row["finished"]),
                active=not row["finished"] and (row["pending_files"] > 0 or row["touched"] > time.time()-30)) for row in rows]
            result["active_devices"] = sorted({item["device_id"] for item in result["sessions"] if item["active"]})
            result["current_file"] = next((item for item in result["files"] if item["state"] != "committed"), None)
            result["import"] = self._job_summary()
            self._overview_cache = result
            return dict(result, busy=False)
        finally:
            self._lock.release()

    def _set_error(self, sha, message):
        with self._lock, self._sql:
            self._sql.execute("UPDATE files SET state='error',error=? WHERE sha=?", (message, sha))

    def _worker_main(self):
        while not self._stop.is_set():
            self._wake.wait(.2)
            self._wake.clear()
            with self._lock:
                row = self._sql.execute("SELECT * FROM files WHERE state='processing' "
                                        "ORDER BY created LIMIT 1").fetchone()
            # Do not prepare another file against a record index that is still
            # awaiting this file's canonical commit.
            if row is None or row["bulk_ready"] != 0:
                continue
            try:
                if not row["prepared"]:
                    self._prepare(row)
                    with self._lock:
                        row = self._sql.execute("SELECT * FROM files WHERE sha=?", (row["sha"],)).fetchone()
                self._prepare_bulk(row)
            except InterruptedError:
                break
            except Exception:
                self._set_error(row["sha"], "CSV validation or local import failed; original file retained")
            self._wake.set()

    def _prepare(self, file):
        sha, path = file["sha"], self._path(file["sha"])
        if _regular(path).st_size != file["size"] or _digest_file(path, self._stop) != sha:
            raise ValueError("Whole CSV size/digest mismatch")
        with self._lock, self._sql:
            # Paid rows from the previous row-at-a-time implementation remain
            # authoritative when an interrupted legacy file is reparsed.
            self._sql.execute("DELETE FROM prepared WHERE sha=? AND done!=1", (sha,))
        records, inserted, batch = 0, 0, []
        signature = _regular(path)
        for record in piglet_records(path):
            if self._stop.is_set():
                raise InterruptedError()
            records += 1
            batch.append((sha, records, record["record_id"], record["protocol"], record["mac"],
                          record["rssi"], json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
            if len(batch) >= 256:
                inserted += self._prepare_rows(batch)
                batch.clear()
        if batch:
            inserted += self._prepare_rows(batch)
        after = _regular(path)
        if (signature.st_ino, signature.st_size, signature.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("CSV changed during parsing")
        with self._lock, self._sql:
            unique = self._sql.execute("SELECT COUNT(*) FROM prepared WHERE sha=?", (sha,)).fetchone()[0]
            duplicate = records - unique + self._sql.execute(
                "SELECT COUNT(*) FROM prepared WHERE sha=? AND done=2", (sha,)).fetchone()[0]
            self._sql.execute("UPDATE files SET prepared=1,records=?,duplicates=? WHERE sha=?",
                              (records, duplicate, sha))

    def _prepare_rows(self, rows):
        with self._lock, self._sql:
            before = self._sql.total_changes
            self._sql.executemany("INSERT OR IGNORE INTO prepared(sha,ordinal,record_id,protocol,mac,rssi,record) VALUES(?,?,?,?,?,?,?)", rows)
            return self._sql.total_changes-before

    def _recover_batch(self, batch_id, marker):
        if marker.get("whole_file") is True:
            return self._recover_file_batch(batch_id, marker)
        sha = marker["sha256"]
        with self._sql:
            for item in marker["entries"]:
                row = self._sql.execute("SELECT * FROM prepared WHERE sha=? AND ordinal=?", (sha, item["ordinal"])).fetchone()
                if row is None or row["record_id"] != item["record_id"]:
                    raise CollectionError("Import recovery metadata is missing; originals retained", 503)
                if row["done"] == 0:
                    self._sql.execute("INSERT OR IGNORE INTO records VALUES(?,?,?)", (row["record_id"], sha, row["ordinal"]))
                    self._sql.execute("UPDATE prepared SET done=1 WHERE sha=? AND ordinal=?", (sha, row["ordinal"]))
                    column = "wifi" if item["protocol"] == "wifi" else "ble"
                    self._sql.execute(f"UPDATE files SET {column}={column}+1 WHERE sha=?", (sha,))
                    if type(item.get("new_identity")) is bool and type(item.get("base_xp")) is int and item["base_xp"] >= 0:
                        self._sql.execute(f"UPDATE files SET new_{column}=new_{column}+?,base_xp=base_xp+? WHERE sha=?",
                                          (int(item["new_identity"]), item["base_xp"], sha))
                    else:
                        # Legacy recovery markers cannot prove how much of a
                        # previously committed award belonged to this source.
                        self._sql.execute("UPDATE files SET summary_complete=0 WHERE sha=?", (sha,))
        if not self.loot.finalize_collection_batch(batch_id):
            raise OSError("Import recovery marker could not be released")

    def _recover_markers(self):
        with self._lock:
            for key, marker in self.loot.collection_pending_batches().items():
                self._recover_batch(key, marker)

    def _recover_receipts(self):
        with self._lock, self._sql:
            pending_files = self._pending_event_files()
            for sha in pending_files:
                # Upgrade recovery: older versions could issue a file receipt
                # while its durable presentation events were still outstanding.
                self._sql.execute("UPDATE files SET state='processing' WHERE sha=? AND state='committed'", (sha,))
            for row in self._sql.execute("SELECT sha FROM files WHERE state!='committed'").fetchall():
                if row["sha"] in pending_files:
                    continue
                saved = self.loot.collection_file_receipt(row["sha"])
                if saved is not None:
                    self._sql.execute("UPDATE files SET state='committed',receipt=? WHERE sha=?",
                                      (json.dumps(saved["receipt"]), row["sha"]))

    def _pending_event_files(self):
        """Resolve at most the existing bounded outbox, including old events."""
        return set(self._pending_event_sources().values())

    def _pending_event_sources(self):
        events = self.loot.collection_pending_events(256)
        sources = {event["event_id"]: event["source_file_sha256"] for event in events
                   if isinstance(event.get("source_file_sha256"), str)
                   and _SHA.fullmatch(event["source_file_sha256"])}
        legacy = [event["event_id"] for event in events if event["event_id"] not in sources]
        if legacy:
            placeholders = ",".join("?" for _ in legacy)
            sources.update(self._sql.execute(f"SELECT record_id,sha FROM records WHERE record_id IN ({placeholders})", legacy))
            missing = [event_id for event_id in legacy if event_id not in sources]
            for event_id in missing:
                row = self._sql.execute("SELECT sha FROM prepared WHERE record_id=? ORDER BY ordinal LIMIT 1", (event_id,)).fetchone()
                if row is None:
                    raise CollectionError("Observation acknowledgement source is unavailable; import remains pending", 503)
                sources[event_id] = row[0]
        return sources

    def _prepare_bulk(self, file):
        """Build exact unpaid groups and a full-source export on the worker."""
        sha = file["sha"]
        if self._stop.is_set():
            raise InterruptedError()
        with self._lock, self._sql:
            self._sql.execute("UPDATE prepared SET done=2 WHERE sha=? AND done=0 AND EXISTS "
                              "(SELECT 1 FROM records WHERE records.record_id=prepared.record_id)", (sha,))
            self._sql.execute("DELETE FROM bulk_groups WHERE sha=?", (sha,))
            self._sql.execute("INSERT INTO bulk_groups SELECT p.sha,p.protocol,p.mac,g.n,p.ordinal,p.record_id,p.record "
                "FROM prepared p JOIN (SELECT protocol,mac,COUNT(*) n,MIN(ordinal) ordinal FROM prepared "
                "WHERE sha=? AND done IN (0,3) GROUP BY protocol,mac) g ON p.sha=? AND p.ordinal=g.ordinal", (sha, sha))
            self._sql.execute("UPDATE prepared SET done=3 WHERE sha=? AND done=0", (sha,))
            self._sql.execute("UPDATE files SET bulk_id=COALESCE(bulk_id,?),bulk_processed=0 WHERE sha=?",
                              (uuid.uuid4().hex, sha))
        self._export(file)
        if self._stop.is_set():
            raise InterruptedError()
        with self._lock, self._sql:
            self._sql.execute("UPDATE files SET bulk_ready=1,exported=1,bulk_processed=records WHERE sha=?", (sha,))

    def _recover_file_batch(self, batch_id, marker):
        sha, counters = marker["sha256"], marker["counters"]
        row = self._sql.execute("SELECT * FROM files WHERE sha=?", (sha,)).fetchone()
        if row is None or row["bulk_id"] != batch_id:
            raise CollectionError("Whole-file recovery source is unavailable; originals retained", 503)
        actual = dict(self._sql.execute("SELECT protocol,COUNT(*) FROM prepared WHERE sha=? AND done IN (1,3) GROUP BY protocol", (sha,)))
        if (any(actual.get(protocol,0) != counters[protocol] for protocol in ("wifi","ble"))
                or counters["wifi"]+counters["ble"]+counters["duplicates"] != row["records"]):
            raise CollectionError("Whole-file recovery counters do not match source index", 503)
        with self._sql:
            self._sql.execute("INSERT OR IGNORE INTO records SELECT record_id,sha,ordinal FROM prepared "
                              "WHERE sha=? AND done IN (1,3)", (sha,))
            self._sql.execute("UPDATE prepared SET done=1 WHERE sha=? AND done=3", (sha,))
            self._sql.execute("UPDATE files SET wifi=?,ble=?,duplicates=?,new_wifi=?,new_ble=?,base_xp=?,bulk_ready=2 "
                "WHERE sha=?", tuple(counters[key] for key in ("wifi", "ble", "duplicates", "new_wifi", "new_ble", "base_xp")) + (sha,))
        self._publish_export(row)
        if not self.loot.finalize_collection_batch(batch_id):
            raise OSError("Whole-file recovery marker could not be released")

    def process_pending(self, current_xp, known_wifi, known_ble, limit=128):
        # limit remains accepted for existing callers; reward units are now whole
        # CSV files. No row is ever presented or awarded from the worker thread.
        result = dict(committed=False, xp_total=current_xp)
        if self._cancelled or not self._lock.acquire(blocking=False):
            return result
        try:
            self._check_open()
            if self._cancelled:
                return result
            self._recover_markers()
            self._recover_receipts()
            row = self._sql.execute("SELECT * FROM files WHERE state='processing' AND bulk_ready IN (1,2) "
                                    "ORDER BY created LIMIT 1").fetchone()
            if row is None or row["sha"] in self._pending_event_files():
                return result
            if row["bulk_ready"] == 1:
                groups = [dict(protocol=item["protocol"],mac=item["mac"],count=item["count"],ordinal=item["ordinal"],
                               record_id=item["record_id"],record=json.loads(item["record"]))
                          for item in self._sql.execute("SELECT * FROM bulk_groups WHERE sha=? ORDER BY ordinal", (row["sha"],))]
                result = self.loot.commit_collection_file(row["bulk_id"], row["sha"], groups, current_xp,
                    known_wifi, known_ble, source_job_id=row["owner_job"],
                    counters={key:row[key] for key in ("records","wifi","ble","new_wifi","new_ble","base_xp")})
                if result.get("committed"):
                    try:
                        self._recover_file_batch(row["bulk_id"], self.loot.collection_pending_batches()[row["bulk_id"]])
                    except Exception:
                        result["error"] = "Whole-file import recovery pending; committed XP retained"
                        return result
                row = self._sql.execute("SELECT * FROM files WHERE sha=?", (row["sha"],)).fetchone()
            if row["bulk_ready"] == 2:
                receipt = dict(protocol=1, collector_id=self.collector_id, sha256=row["sha"], size=row["size"],
                    import_id=row["import_id"], records=row["records"], wifi=row["wifi"], ble=row["ble"], duplicates=row["duplicates"])
                if receipt["wifi"]+receipt["ble"]+receipt["duplicates"] != receipt["records"]:
                    raise CollectionError("Import counters do not reconcile", 503)
                try:
                    if not self.loot.complete_collection_file(row["sha"], row["session_name"], receipt):
                        raise OSError("Import completion was not committed")
                    self._recover_receipts()
                except Exception:
                    # The caller must observe the already durable XP even when
                    # publication/history completion needs another attempt.
                    if not result.get("committed"):
                        raise
                    result["error"] = "Import completion pending; committed XP retained"
                    return result
                result["file_completed"] = row["sha"]
            self._wake.set()
            return result
        except Exception:
            if result.get("committed"):
                result["error"] = "Import finalization pending; committed XP retained"
                return result
            raise
        finally:
            self._lock.release()

    def _export(self, file):
        """Write every original observation in source order, including paid rows."""
        sha = file["sha"]
        session = file["session_name"] or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")+"_piglet_"+file["import_id"][:12]
        with self._lock, self._sql:
            self._sql.execute("UPDATE files SET session_name=? WHERE sha=?", (session, sha))
        stage = _private_directory(self.exports / file["import_id"])
        signature = _regular(self._path(sha))
        if _digest_file(self._path(sha), self._stop) != sha:
            raise ValueError("Whole-source export digest changed")
        with (stage / "wardriving.csv").open("w", encoding="utf-8", newline="") as out, \
                (stage / "bt_devices.csv").open("w", encoding="utf-8", newline="") as bt:
            out.write(PREAMBLE + "\n")
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(NATIVE_HEADER)
            bt_writer = csv.writer(bt, lineterminator="\n")
            bt_writer.writerow(("timestamp", "mac", "rssi", "name", "airtag", "smarttag", "lat", "lon"))
            count = 0
            for record in piglet_records(self._path(sha)):
                if self._stop.is_set():
                    raise InterruptedError()
                writer.writerow(native_row(record))
                if record["protocol"] == "ble":
                    fields = record["source_fields"]
                    bt_writer.writerow((fields[3],record["mac"],fields[6],fields[1],False,False,fields[7],fields[8]))
                count += 1
                if count % 256 == 0:
                    with self._lock, self._sql:
                        self._sql.execute("UPDATE files SET bulk_processed=? WHERE sha=?", (count,sha))
            after = _regular(self._path(sha))
            if (count != file["records"] or
                    (signature.st_ino,signature.st_size,signature.st_mtime_ns) !=
                    (after.st_ino,after.st_size,after.st_mtime_ns)):
                raise ValueError("Whole-source export changed")
            for stream in (out,bt):
                stream.flush()
                os.fsync(stream.fileno())
        with (stage / "collection_source.json").open("w", encoding="utf-8") as out:
            json.dump(dict(protocol=1,sha256=sha,size=file["size"],name=file["name"],
                raw_archive=str(self._path(sha)),import_id=file["import_id"],whole_source_rows=True,records=count),out)
            out.flush()
            os.fsync(out.fileno())
        _fsync_directory(stage)

    def _publish_export(self, file):
        """Complete a paid file's publication; retry safely after an interrupted rename."""
        target = Path(self.loot._base) / file["session_name"]
        stage = self.exports / file["import_id"]
        if target.exists():
            if target.is_symlink() or json.loads((target/"collection_source.json").read_text(encoding="utf-8"))["sha256"] != file["sha"]:
                raise ValueError("Existing import directory conflicts")
        else:
            _regular(stage/"collection_source.json")
            stage.rename(target)
            _fsync_directory(target.parent)
            _fsync_directory(self.exports)
            return
        for name in ("wardriving.csv","bt_devices.csv","collection_source.json"):
            source = stage/name
            if source.exists():
                _regular(source)
                os.replace(source,target/name)
            elif not (target/name).is_file():
                raise OSError("Paid import export is unavailable; originals retained")
        _fsync_directory(target)
        _fsync_directory(target.parent)

    def cancel_processing(self):
        """Cooperatively stop; only unpaid derived work is reset after quiescence."""
        self._cancelled = True
        self._stop.set()
        self._wake.set()

    def pending_events(self, limit=20):
        return self.loot.collection_pending_events(limit)

    def acknowledge_events(self, event_ids):
        return self.loot.acknowledge_collection_events(event_ids)

    def close(self):
        if self._closed:
            return True
        self._stop.set()
        self._wake.set()
        self._worker.join(timeout=5)
        if self._worker.is_alive():
            return False
        with self._lock:
            if self._cancelled:
                self._recover_markers()
                with self._sql:
                    self._sql.execute("UPDATE prepared SET done=0 WHERE done=3 AND sha IN "
                                      "(SELECT sha FROM files WHERE bulk_ready!=2 AND state!='committed')")
                    self._sql.execute("DELETE FROM bulk_groups WHERE sha IN "
                                      "(SELECT sha FROM files WHERE bulk_ready!=2 AND state!='committed')")
                    self._sql.execute("UPDATE files SET bulk_ready=0,bulk_processed=0,exported=0 "
                                      "WHERE bulk_ready!=2 AND state!='committed'")
            self._sql.close()
            self._closed = True
        return True
