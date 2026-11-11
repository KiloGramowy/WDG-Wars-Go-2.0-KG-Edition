"""One bounded background writer for SOLO observations and startup backfill.

CSV/XP/upload ownership stays with WDG's existing stores and UI. The only
temporary startup file is local to the current loot session; a new recorder
never adopts an earlier process's pending queue or GPS fix.
"""

from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time
import uuid

from .app_state import Network
from .wardrive_gps import WardriveGpsPolicy

log = logging.getLogger(__name__)
_MAX_RECORD = 16 * 1024


class WardriveRecorder:
    def __init__(self, loot, policy=None, *, queue_size=256, clock=time.monotonic):
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or not 1 <= queue_size <= 4096:
            raise ValueError("Wardrive queue size must be 1..4096")
        self.loot = loot
        self.policy = policy if policy is not None else WardriveGpsPolicy(clock=clock)
        self._clock = clock
        self._jobs = queue.Queue(maxsize=queue_size)
        self._events = queue.Queue(maxsize=max(32, queue_size * 2))
        self._condition = threading.Condition()
        self._accepting = True
        self._closing = False
        self._stopped = False
        self._finishing = False
        self._initialised = False
        self._seal_queued = False
        self._stage = "waiting"
        self._failed_job = None
        self._submit_error = False
        self._retry_requested = False
        self._credit = 0
        self._highwater = {}
        # Scalar canonical identities, not an in-memory collection of raw RX.
        # Existing CSV dedup keeps first BLE history and strongest WiGLE RX.
        self._canonical = {}
        self._pending_count = self._replayed = 0
        self._pending_writer = self._pending_reader = None
        self._pending_path = Path(loot.session_dir) / "wardrive_startup.pending.jsonl"
        self._pending_identity = None
        self._run_id = uuid.uuid4().hex
        self._thread = threading.Thread(target=self._worker, name="wardrive-recorder", daemon=True)
        self._thread.start()

    @staticmethod
    def _observation(raw):
        if not isinstance(raw, dict):
            raise ValueError("Wardrive observation must be a mapping")
        # A bounded JSON copy freezes transport metadata without retaining a
        # mutable controller buffer. Reject NaN and non-serialisable payloads.
        encoded = json.dumps(raw, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > _MAX_RECORD:
            raise ValueError("Wardrive observation exceeds its record limit")
        value = json.loads(encoded)
        identity = value.get("identity")
        if (not isinstance(identity, list) or len(identity) != 3
                or any(not isinstance(part, (str, int)) or isinstance(part, bool) for part in identity[:2])
                or any(len(str(part)) > 128 for part in identity[:2])
                or isinstance(identity[2], bool) or not isinstance(identity[2], int) or identity[2] < 0):
            raise ValueError("Wardrive identity requires boot, session and monotonic sequence")
        value["identity"] = tuple(identity)
        if value.get("protocol") not in ("wifi", "ble"):
            raise ValueError("Wardrive protocol must be wifi or ble")
        if not isinstance(value.get("mac"), str) or not re.fullmatch(
                r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value["mac"]):
            raise ValueError("Wardrive observation has invalid MAC")
        captured = value.get("observed_at")
        if (isinstance(captured, bool) or not isinstance(captured, (int, float))
                or not math.isfinite(captured)):
            raise ValueError("Wardrive capture requires monotonic time")
        if isinstance(value.get("rssi"), bool) or not isinstance(value.get("rssi"), int):
            raise ValueError("Wardrive observation requires integer RSSI")
        stamp = value.get("captured_at")
        if not isinstance(stamp, str):
            raise ValueError("Wardrive capture requires UTC timestamp")
        utc = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if utc.tzinfo is None or utc.utcoffset() is None:
            raise ValueError("Wardrive capture timestamp must be timezone-aware")
        return value, utc.astimezone(timezone.utc)

    @property
    def failed(self):
        with self._condition:
            return self._failed_job is not None or self._submit_error

    @property
    def pending_count(self):
        with self._condition:
            return max(0, self._pending_count - self._replayed)

    def _input_error(self):
        with self._condition:
            self._submit_error = True
        try:
            self._events.put_nowait(dict(event="error", message="Wardrive input could not be queued; STOP required."))
        except queue.Full:
            pass
        return False

    def submit(self, observation):
        """Accept a bounded RX for durable retry, without filesystem work."""
        try:
            value, utc = self._observation(observation)
            startup = not self.policy.startup_complete
            position = None if startup else self.policy.position_for(value["observed_at"], self._clock())
        except (TypeError, ValueError, OverflowError, MemoryError):
            return self._input_error()
        with self._condition:
            if (not self._accepting or self._failed_job is not None or self._submit_error
                    or (self._seal_queued and startup)):
                return False
            job = dict(kind="raw", observation=value, utc=utc, position=position,
                       startup=startup, stores={}, pending_saved=False)
            try:
                self._jobs.put_nowait(job)
            except queue.Full:
                return self._input_error()
            self._condition.notify_all()
            return True

    def seal_startup(self):
        """Queue a closeout barrier only AFTER the caller has a full STOP ACK."""
        with self._condition:
            if self._seal_queued or self.policy.startup_complete:
                return True
            if (self._stopped or self._finishing or self.policy.first_fix_snapshot is None
                    or self._failed_job is not None):
                return False
            try:
                self._jobs.put_nowait(dict(kind="seal"))
            except queue.Full:
                return False
            self._seal_queued = True
            self._condition.notify_all()
            return True

    def tick(self):
        """Grant at most twenty replay records; the UI never reads/writes files."""
        with self._condition:
            if self._stage != "replay" or self._failed_job is not None or self._credit:
                return False
            self._credit = 20
            self._condition.notify_all()
            return True

    def retry(self):
        with self._condition:
            if self._stopped:
                return False
            requested = self._failed_job is not None or self._submit_error
            self._submit_error = False
            self._retry_requested = self._failed_job is not None
            self._condition.notify_all()
            return requested

    def poll_events(self, limit=32):
        events = []
        for _ in range(min(1024, max(0, int(limit)))):
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def close(self, timeout=3.0):
        """Flush accepted RX and stop, or retain the failed job and return False.

        Before first fix the unpositioned pending file is left intact. Once
        sealed, shutdown can grant further bounded replay batches. It cannot
        invent a STOP ACK or seal a still-running startup capture.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            return False
        deadline = time.monotonic() + timeout
        with self._condition:
            self._accepting = False
            self._closing = True
            self._condition.notify_all()
        while self._thread.is_alive():
            with self._condition:
                if self._failed_job is not None:
                    return False
                if self._stage == "replay" and not self._credit:
                    self._credit = 20
                    self._condition.notify_all()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._thread.join(min(0.02, remaining))
        return self._stopped and self._jobs.unfinished_tasks == 0

    def _emit(self, value):
        # Bounded event backpressure retains the saved observation for the UI;
        # it never silently loses an XP event if the game pauses its polling.
        self._events.put(value)

    def _initialise(self):
        if not getattr(self.loot, "active", False):
            raise OSError("Loot session is inactive")
        self._pending_writer = self._open_pending(reset=True)
        self._pending_writer.write(json.dumps(dict(kind="session", run=self._run_id)) + "\n")
        self._pending_writer.flush()
        os.fsync(self._pending_writer.fileno())
        self._initialised = True

    def _open_pending(self, *, reset=False):
        """Reject links/devices before any truncation or queue replay."""
        try:
            before = self._pending_path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink > 1):
            raise OSError("Startup pending path is not a private regular file")
        flags = os.O_RDWR if reset else os.O_RDONLY
        if reset and before is None:
            flags |= os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(self._pending_path, flags, 0o600)
        try:
            opened, current = os.fstat(descriptor), self._pending_path.lstat()
            identity = opened.st_dev, opened.st_ino
            if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                    or opened.st_nlink > 1 or (current.st_dev, current.st_ino) != identity
                    or before is not None and (before.st_dev, before.st_ino) != identity
                    or self._pending_identity is not None and self._pending_identity != identity):
                raise OSError("Startup pending file changed before it was opened")
            if reset:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                os.ftruncate(descriptor, 0)
                self._pending_identity = identity
            return os.fdopen(descriptor, "w+" if reset else "r", encoding="utf-8", newline="")
        except BaseException:
            os.close(descriptor)
            raise

    def _store(self, job, store, save):
        before = self.loot.wardrive_save_errors
        written = save()
        if self.loot.wardrive_save_errors != before:
            raise OSError("CSV persistence failed")
        if written:
            job["stores"][store] = True
            obs = job["observation"]
            if job["startup"]:
                self._canonical[(store, obs["protocol"], obs["mac"].upper())] = obs["identity"]
        else:
            job["stores"].setdefault(store, False)

    def _append_pending(self, job):
        if job["pending_saved"]:
            return
        stream = self._pending_writer
        offset = job.setdefault("pending_offset", stream.tell())
        # Only the not-yet-acknowledged tail is replaced after a partial failure.
        stream.seek(offset)
        stream.truncate()
        stream.write(json.dumps(dict(kind="rx", observation=job["observation"],
            stores=job["stores"]), ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        job["pending_saved"] = True
        self._pending_count += 1

    def _raw(self, job):
        obs = job["observation"]
        boot_session, sequence = obs["identity"][:2], obs["identity"][2]
        if sequence <= self._highwater.get(boot_session, -1):
            return
        if not getattr(self.loot, "active", False):
            raise OSError("Loot session is inactive")
        args = dict(observed_at=job["utc"], position=job["position"])
        if obs["protocol"] == "wifi":
            network = Network(bssid=obs["mac"], ssid=str(obs.get("ssid", "")),
                auth=str(obs.get("auth", "UNKNOWN")), channel=str(obs.get("channel", "")), rssi=str(obs["rssi"]))
            self._store(job, "wardriving", lambda: self.loot.save_wardriving_network(network, **args))
        else:
            name = str(obs.get("name", ""))
            self._store(job, "wardriving", lambda: self.loot.save_wardriving_bt(obs["mac"], obs["rssi"], name, **args))
            self._store(job, "bt_devices", lambda: self.loot.save_bt_device(obs["mac"], obs["rssi"], name, False, False, **args))
        if job["startup"]:
            self._append_pending(job)
        if not self.loot.append_wardrive_provenance(dict(event="observation", observation=obs,
                position=job["position"], startup_pending=job["startup"], stores=job["stores"])):
            raise OSError("Observation provenance was not persisted")
        self._emit(dict(event="saved", observation=obs, position=job["position"], replay=False))
        self._highwater[boot_session] = sequence

    def _close_pending_writer(self):
        stream = self._pending_writer
        if stream is None:
            return
        try:
            if not stream.closed:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
        finally:
            if stream.closed:
                self._pending_writer = None

    def _seal(self):
        if self.policy.first_fix_snapshot is None:
            raise RuntimeError("Startup closeout requires an accepted first fix")
        self._close_pending_writer()
        self._pending_reader = self._open_pending()
        self._stage = "replay"

    def _replay(self, job):
        if job.get("finish"):
            self._finish_replay()
            return
        if "record" not in job:
            offset = job.setdefault("offset", self._pending_reader.tell())
            self._pending_reader.seek(offset)
            line = self._pending_reader.readline(_MAX_RECORD * 2 + 1)
            if not line:
                job["finish"] = True
                self._finish_replay()
                return
            if len(line) > _MAX_RECORD * 2:
                raise ValueError("Startup replay record exceeds its limit")
            job["record"] = json.loads(line)
        record = job["record"]
        if record.get("kind") == "session":
            if record.get("run") != self._run_id:
                raise ValueError("Startup queue belongs to another run")
            return
        if record.get("kind") != "rx":
            raise ValueError("Unknown startup replay record")
        obs, utc = self._observation(record["observation"])
        stores = record.get("stores")
        expected = {"wardriving", "bt_devices"} if obs["protocol"] == "ble" else {"wardriving"}
        if not isinstance(stores, dict) or set(stores) != expected or any(type(value) is not bool for value in stores.values()):
            raise ValueError("Startup record has incomplete store outcomes")
        position = self.policy.first_fix_snapshot
        results, proofs = {}, {}
        for store, accepted in stores.items():
            if not accepted:
                results[store] = "dedup"
                continue
            canonical = self._canonical.get((store, obs["protocol"], obs["mac"].upper()))
            if canonical is None:
                raise OSError("Startup canonical source proof is missing")
            if canonical != obs["identity"]:
                results[store] = "not_current_export"
                proofs[store] = dict(reason="later_saved_source", identity=canonical)
                continue
            result = (self.loot.fill_bt_device_position(obs["mac"], observed_at=utc, position=position)
                if store == "bt_devices" else self.loot.fill_wardriving_position(
                    obs["protocol"], obs["mac"], observed_at=utc, position=position,
                    source_rssi=obs["rssi"]))
            if result["status"] == "superseded":
                results[store] = "not_current_export"
                proofs[store] = dict(reason="stronger_current_export",
                    observed_at=result["current_observed_at"], rssi=result["current_rssi"])
                continue
            if not result["persisted"]:
                raise OSError("Startup position correction was not persisted: " + result["status"])
            results[store] = result["status"]
        if not self.loot.append_wardrive_provenance(dict(event="startup_backfill", observation=obs,
                position=position, stores=results, store_proofs=proofs)):
            raise OSError("Backfill provenance was not persisted")
        self._replayed += 1
        self._emit(dict(event="backfill", observation=obs, position=position, replay=True))

    def _finish_replay(self):
        if self._replayed != self._pending_count:
            raise OSError("Startup pending store is incomplete; replay cannot finish")
        if self._pending_reader is not None:
            self._pending_reader.close()
            self._pending_reader = None
        try:
            current = self._pending_path.lstat()
        except FileNotFoundError:
            current = None
        if current is not None:
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != self._pending_identity:
                raise OSError("Startup pending path changed before cleanup")
            self._pending_path.unlink()
        self.policy.complete_startup()
        self._stage = "normal"
        self._canonical.clear()
        self._credit = 0
        self._emit(dict(event="backfill_done", count=self._replayed))

    def _close_files(self):
        self._close_pending_writer()
        if self._pending_reader is not None:
            self._pending_reader.close()
            self._pending_reader = None

    def _worker(self):
        while True:
            with self._condition:
                if self._failed_job is not None:
                    if not self._retry_requested:
                        self._condition.wait(0.1)
                        continue
                    job = self._failed_job
                    self._retry_requested = False
                elif not self._initialised:
                    job = dict(kind="init")
                elif self._stage == "replay":
                    if not self._credit:
                        self._condition.wait(0.1)
                        continue
                    self._credit -= 1
                    job = dict(kind="replay")
                else:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        if self._closing and not self.policy.startup_closing:
                            self._finishing = True
                            job = dict(kind="close")
                        else:
                            self._condition.wait(0.1)
                            continue
            try:
                kind = job["kind"]
                if kind == "init":
                    # Close any descriptor left by a failed initial fsync.
                    if self._pending_writer is not None:
                        self._pending_writer.close()
                    self._initialise()
                elif kind == "raw":
                    self._raw(job)
                elif kind == "seal":
                    self._seal()
                elif kind == "replay":
                    self._replay(job)
                elif kind == "close":
                    self._close_files()
                    with self._condition:
                        self._stopped = True
                    return
                with self._condition:
                    self._failed_job = None
                    if kind in ("raw", "seal"):
                        self._jobs.task_done()
                    self._condition.notify_all()
            except Exception as exc:
                log.error("Wardrive writer retained a failed %s operation: %s", job["kind"], exc)
                with self._condition:
                    self._failed_job = job
                self._emit(dict(event="error", message="Wardrive persistence failed; data retained. Stop and retry."))
