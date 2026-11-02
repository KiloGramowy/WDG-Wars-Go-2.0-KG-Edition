"""SDR Manager — ADS-B aircraft tracking + 433 MHz sensor decoding.

Uses RTL-SDR (RTL2832U) on AIO v2 board via dump1090 and rtl_433.
Subprocess + thread + queue pattern (same as serial_manager).
"""

import json
import logging
import math
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import wraps
from queue import Queue
from typing import Callable, Optional

from .map_visibility import OffScanDecay

log = logging.getLogger(__name__)


def _serialized_start(method):
    @wraps(method)
    def start(self, *args, **kwargs):
        with self._lifecycle_lock:
            if (self._stop_requested or self._stopping
                    or (self._stop_event.is_set() and self._has_owners())):
                self._events.put(("error", "SDR shutdown is still pending"))
                return False
            self._stop_event.clear()
            return method(self, *args, **kwargs)
    return start


# --- ADS-B Constants ---
# dump1090-fa/mutability: --net enables SBS on 30003 by default
# --quiet suppresses interactive output
DUMP1090_CMD = ["dump1090", "--net", "--quiet"]
# Fallback without --quiet (some builds don't support it)
DUMP1090_CMD_FALLBACK = ["dump1090", "--net"]

# --- rtl_433 Constants ---
RTL433_CMD = [
    "rtl_433",
    "-f", "433920000",            # 433.92 MHz
    "-F", "json",                 # JSON output to stdout
    "-M", "time:utc",            # UTC timestamps
    "-M", "level",               # include signal level
]

# Device index: dump1090 uses device 0, rtl_433 uses device 1
# (or run them sequentially, not simultaneously)
RTL433_CMD_DEV1 = RTL433_CMD + ["-d", "1"]

ADSB_TIMEOUT = 60  # active reception: remove aircraft not heard for 60s
SENSOR_TIMEOUT = 300  # remove sensors not seen for 5min


@dataclass
class Aircraft:
    """Tracked aircraft from ADS-B."""
    icao: str            # ICAO 24-bit hex address
    callsign: str = ""
    lat: float = 0.0
    lon: float = 0.0
    altitude: int = 0    # feet
    speed: int = 0       # knots
    heading: int = 0     # degrees
    squawk: str = ""
    last_seen: float = 0.0
    rssi: float = 0.0
    position_seen: float = 0.0

    @property
    def has_position(self) -> bool:
        return self.lat != 0.0 or self.lon != 0.0


@dataclass
class Sensor433:
    """Decoded 433 MHz sensor from rtl_433."""
    model: str
    sid: str              # unique sensor ID (model + id combo)
    data: dict = field(default_factory=dict)
    lat: float = 0.0     # GPS position when first seen
    lon: float = 0.0
    last_seen: float = 0.0
    count: int = 0        # times seen


# --- Emergency-vehicle RF presence scanner (RTL-SDR / rtl_power) ---
# Presence/activity detection only (NO content decode). Polish emergency-service
# sub-bands: fire/EMS/police analog FM VHF + police/EMS TETRA. A "vehicle"
# channel firing = a signal jumping above its OWN rolling baseline (a keyup);
# the TETRA downlink is kept as a passive "network present" reference (base
# carrier is always on). Verified live on-device: strong TETRA @~392 MHz,
# fire @~148.63. Analog VHF plans sourced from czestotliwosci.pl.tl (Opolskie):
#   fire PSP/OSP 148.66-149.34 | EMS/karetki 168.53-169.15 | police 172.575 +
#   national 172.9/173.0 | municipal (SM) Opole 147.825 (147.74-148.95 range).
# NOTE: the SM/analog-police plans may be dated -- calibrate against live logs.
EMERGENCY_BANDS = [
    # name,        lo_hz,       hi_hz,       bin_hz, role,      thr_db
    ("SM-VHF",     147_700_000, 148_450_000, 12_500, "vehicle", 7.0),  # straż miejska (Opole 147.825)
    ("STRAZ-VHF",  148_500_000, 149_400_000, 12_500, "vehicle", 7.0),  # straż pożarna PSP/OSP
    ("EMS-VHF",    168_500_000, 169_150_000, 12_500, "vehicle", 7.0),  # pogotowie / karetki
    ("POL-VHF",    172_500_000, 173_100_000, 12_500, "vehicle", 7.0),  # policja analog (Opolskie 172.575)
    ("TETRA-UL",   381_000_000, 383_000_000, 25_000, "vehicle", 7.0),  # wóz TETRA (uplink)
    ("TETRA-DL",   391_000_000, 393_000_000, 25_000, "network", 10.0), # sieć TETRA (downlink ref)
]
EMERGENCY_GAIN = "45"
EMERGENCY_INTEG = "0.5"     # rtl_power integration s/band (faster revisit)


@dataclass
class EmergencySignal:
    """A detected emergency-service RF signal (presence, not content)."""
    band: str
    freq: float = 0.0        # MHz of the peak bin
    rssi: float = 0.0        # peak power (dB, relative)
    baseline: float = 0.0    # channel's own rolling norm
    jump: float = 0.0        # rssi - baseline (keyup magnitude)
    trend: str = ""          # "up" (approaching) / "dn" / ""
    active: bool = False     # vehicle keyup event in progress
    role: str = "vehicle"    # "vehicle" or "network"
    last_seen: float = 0.0


class SDRManager:
    """Manages dump1090 (ADS-B) and rtl_433 (ISM sensors) subprocesses."""

    def __init__(self):
        self.running = False
        self.mode: str = ""  # "adsb", "433", "adsb+433"

        # ADS-B state
        self.aircraft: dict[str, Aircraft] = {}  # icao -> Aircraft
        self.total_aircraft_seen: int = 0
        # OFF retains the last view, with a distance-ordered 5–10 minute
        # display timeout. Resume allows a fresh receiver-range window.
        self._adsb_resumed_at: float = 0.0
        self._adsb_off_decay = OffScanDecay()

        # 433 MHz state
        self.sensors: dict[str, Sensor433] = {}  # sid -> Sensor433
        self.total_sensors_seen: int = 0

        # Subprocess handles
        self._dump1090_proc: Optional[subprocess.Popen] = None
        self._rtl433_proc: Optional[subprocess.Popen] = None

        # Event queue for app.py
        self._events: Queue = Queue()
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._stopping = False
        self._stop_requested = False

        # Callbacks
        self.on_new_aircraft: Optional[Callable] = None
        self.on_new_sensor: Optional[Callable] = None

        # Loot file handles
        self._adsb_log: Optional[object] = None
        self._sensor_log: Optional[object] = None

        # Emergency-vehicle RF scanner state
        self.emergency: dict[str, EmergencySignal] = {}  # band -> signal
        self.emergency_active = False   # any vehicle channel firing right now
        self.network_present = False    # TETRA base carrier seen (services present)
        self.total_emergency_hits = 0
        self._emerg_proc: Optional[subprocess.Popen] = None
        self._emerg_log: Optional[object] = None
        self._scan_log: Optional[object] = None   # full per-sweep calibration log

    @staticmethod
    def has_dump1090() -> bool:
        return shutil.which("dump1090") is not None

    @staticmethod
    def has_rtl433() -> bool:
        return shutil.which("rtl_433") is not None

    @_serialized_start
    def start_adsb(self, loot_dir: str = "") -> bool:
        """Start ADS-B tracking via dump1090."""
        if not self.has_dump1090():
            self._events.put(("error", "dump1090 not installed"))
            return False
        if self._dump1090_proc:
            return True  # already running

        # Stop system dump1090 service if running (holds RTL-SDR device)
        try:
            subprocess.run(
                ["systemctl", "stop", "dump1090-mutability"],
                capture_output=True, timeout=5)
        except Exception:
            pass

        try:
            proc = self._spawn_owned("_dump1090_proc",
                DUMP1090_CMD,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc is None:
                return False
            # Check if process died immediately (bad flags)
            time.sleep(0.5)
            if proc.poll() is not None:
                # Retry without --quiet
                proc = self._spawn_owned("_dump1090_proc",
                    DUMP1090_CMD_FALLBACK,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if proc is None:
                    return False
        except Exception as e:
            self._events.put(("error", f"dump1090 failed: {e}"))
            return False

        if self._stop_event.is_set():
            return False

        # Open loot file
        if loot_dir:
            try:
                self._adsb_log = open(
                    os.path.join(loot_dir, "adsb_aircraft.csv"), "a")
                if self._adsb_log.tell() == 0:
                    self._adsb_log.write(
                        "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,"
                        "heading,squawk\n")
            except Exception:
                pass

        # Remove expired OFF markers before resetting their display schedule;
        # a new scan must not resurrect observations already faded away. Do
        # this before a reader can deliver its first fresh observation.
        self._prune_aircraft(time.time())
        self._adsb_off_decay.clear()
        self.running = True
        self.mode = "adsb"
        self._adsb_resumed_at = time.time()

        # Start SBS parser thread (connects to port 30003)
        t = threading.Thread(target=self._sbs_reader, daemon=True)
        t.start()
        self._threads.append(t)

        self._events.put(("status", "ADS-B started (dump1090)"))
        return True

    @_serialized_start
    def start_433(self, loot_dir: str = "", gps_lat: float = 0.0,
                  gps_lon: float = 0.0) -> bool:
        """Start 433 MHz sensor decoding via rtl_433."""
        if not self.has_rtl433():
            self._events.put(("error", "rtl_433 not installed"))
            return False
        if self._rtl433_proc:
            return True

        self._gps_lat = gps_lat
        self._gps_lon = gps_lon

        # Use device 1 if dump1090 is already using device 0
        cmd = RTL433_CMD_DEV1 if self._dump1090_proc else RTL433_CMD
        try:
            proc = self._spawn_owned("_rtl433_proc",
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if proc is None:
                return False
        except Exception as e:
            self._events.put(("error", f"rtl_433 failed: {e}"))
            return False

        if self._stop_event.is_set():
            return False

        # Open loot file
        if loot_dir:
            try:
                self._sensor_log = open(
                    os.path.join(loot_dir, "sdr_sensors.csv"), "a")
                if self._sensor_log.tell() == 0:
                    self._sensor_log.write(
                        "timestamp,model,id,data,lat,lon\n")
            except Exception:
                pass

        t = threading.Thread(target=self._rtl433_reader, daemon=True)
        t.start()
        self._threads.append(t)

        self.running = True
        if self.mode == "adsb":
            self.mode = "adsb+433"
        else:
            self.mode = "433"
        self._events.put(("status", "433 MHz scanner started (rtl_433)"))
        return True

    # ------------------------------------------------------------------
    # Emergency-vehicle RF presence scanner (rtl_power sweep loop)
    # ------------------------------------------------------------------
    @staticmethod
    def has_rtl_power() -> bool:
        return shutil.which("rtl_power") is not None

    @_serialized_start
    def start_emergency(self, loot_dir: str = "", gps_lat: float = 0.0,
                        gps_lon: float = 0.0) -> bool:
        """Start emergency-vehicle RF presence scan (needs the RTL-SDR alone)."""
        if not self.has_rtl_power():
            self._events.put(("error", "rtl_power not installed (apt install rtl-sdr)"))
            return False
        if "emerg" in self.mode:
            return True
        # Coerce to float — the game may pass player_lat/lon as None before a GPS
        # fix, and "{None:.6f}" would raise in the loggers (silently swallowed,
        # leaving 0-byte logs). Fall back to 0.0 until a fix arrives.
        self._gps_lat = gps_lat if isinstance(gps_lat, (int, float)) else 0.0
        self._gps_lon = gps_lon if isinstance(gps_lon, (int, float)) else 0.0
        if loot_dir:
            try:
                self._emerg_log = open(
                    os.path.join(loot_dir, "emergency_rf.csv"), "a")
                if self._emerg_log.tell() == 0:
                    self._emerg_log.write(
                        "timestamp,band,freq_mhz,rssi_db,jump_db,trend,lat,lon\n")
            except Exception:
                self._emerg_log = None
            try:
                # full per-sweep log (every band, every pass) for calibration
                self._scan_log = open(
                    os.path.join(loot_dir, "emergency_scan.csv"), "a")
                if self._scan_log.tell() == 0:
                    self._scan_log.write(
                        "timestamp,band,role,freq_mhz,rssi_db,baseline_db,"
                        "floor_db,jump_db,thr_db,trend,active,net_present,"
                        "lat,lon\n")
            except Exception:
                self._scan_log = None
        if self._stop_event.is_set():
            return False
        t = threading.Thread(target=self._emergency_reader, daemon=True)
        t.start()
        self._threads.append(t)
        self.running = True
        self.mode = "emergency" if not self.mode else self.mode + "+emergency"
        self._events.put(("status", "Emergency radio scanner started (rtl_power)"))
        return True

    def _rtl_power_peak(self, lo: int, hi: int, binw: int):
        """One rtl_power single-shot -> (peak_freq_hz, peak_db, noise_floor_db)."""
        import statistics
        # Per-process temp file: a stale file owned by a different user (e.g. a
        # test run under another account) would make rtl_power's write fail and
        # every band read back empty. PID-scoping avoids that collision entirely.
        tmp = f"/tmp/_wdg_emerg_{os.getpid()}.csv"
        proc = None
        try:
            proc = self._spawn_owned("_emerg_proc",
                ["rtl_power", "-f", f"{lo}:{hi}:{binw}", "-1",
                 "-i", EMERGENCY_INTEG, "-g", EMERGENCY_GAIN, tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if proc is None:
                return None
            proc.wait(timeout=8)
        except Exception:
            return None
        finally:
            if proc is not None:
                released = self._stop_owned_process(proc, time.monotonic() + 2)
                with self._lifecycle_lock:
                    if released and self._emerg_proc is proc:
                        self._emerg_proc = None
        pts = []
        try:
            for line in open(tmp):
                p = [x.strip() for x in line.split(",")]
                if len(p) < 7:
                    continue
                try:
                    l = float(p[2]); step = float(p[4])
                except ValueError:
                    continue
                for i, x in enumerate(p[6:]):
                    try:
                        d = float(x)
                    except ValueError:
                        continue
                    if d != d:  # nan
                        continue
                    pts.append((l + step * (i + 0.5), d))
        except OSError:
            return None
        if not pts:
            return None
        # Reject the RTL-SDR DC spike / centre spur (a fixed strong bin near the
        # tuning centre that otherwise dominates the peak on every sweep -> the
        # 382.086 MHz false alerts, and masks real vehicle signals) and the noisy
        # band edges. Verified: real service carriers sit >130 kHz off-centre,
        # the DC spur ~86 kHz, so a 110 kHz centre guard separates them cleanly.
        center = (lo + hi) / 2.0
        guard = max(4 * binw, 110_000)
        edge = binw
        usable = [(fq, dd) for (fq, dd) in pts
                  if abs(fq - center) > guard and (lo + edge) < fq < (hi - edge)]
        if not usable:
            usable = pts
        floor = statistics.median(dd for _, dd in usable)
        f, d = max(usable, key=lambda t: t[1])
        return f, d, floor

    def _emergency_reader(self):
        """Sweep emergency sub-bands; keyup event vs steady infra + RSSI trend."""
        import statistics
        from collections import deque
        hist = {b[0]: deque(maxlen=8) for b in EMERGENCY_BANDS}
        cycle = 0
        while not self._stop_event.is_set():
            any_active = False
            cycle += 1
            for name, lo, hi, binw, role, thr in EMERGENCY_BANDS:
                if self._stop_event.is_set():
                    break
                # Prioritise the vehicle bands: the TETRA downlink is always-on
                # (network reference, slow-changing) and SM is low-priority, so
                # sample them less often -> tighter revisit on fire/EMS/police/
                # TETRA-uplink where a passing keyup must be caught.
                if role == "network" and (cycle % 4) != 0:
                    continue
                if name == "SM-VHF" and (cycle % 2) != 0:
                    continue
                r = self._rtl_power_peak(lo, hi, binw)
                if r is None:
                    continue
                freq, db, floor = r
                h = list(hist[name])
                base = statistics.median(h) if len(h) >= 4 else db
                hist[name].append(db)
                hh = list(hist[name])
                trend = ""
                if len(hh) >= 3:
                    if hh[-1] - min(hh[-3:]) > 3:
                        trend = "up"
                    elif max(hh[-3:]) - hh[-1] > 3:
                        trend = "dn"
                sig = self.emergency.setdefault(
                    name, EmergencySignal(band=name, role=role))
                sig.freq = freq / 1e6
                sig.rssi = db
                sig.baseline = base
                sig.trend = trend
                sig.last_seen = time.time()
                if role == "network":
                    sig.jump = db - floor
                    self.network_present = sig.jump > thr
                    sig.active = False
                else:
                    sig.jump = db - base
                    was = sig.active
                    sig.active = (sig.jump > thr) and ((db - floor) > thr)
                    if sig.active:
                        any_active = True
                    if sig.active and not was:
                        self.total_emergency_hits += 1
                        self._events.put(
                            ("emergency_alert", EmergencySignal(**sig.__dict__)))
                        self._log_emergency(sig)
                    elif was and not sig.active:
                        self._events.put(("emergency_clear", name))
                self._log_scan(name, role, freq, db, base, floor, thr, sig)
            self.emergency_active = any_active
            if self._stop_event.wait(0.15):
                break

    def _log_emergency(self, sig: "EmergencySignal"):
        if not self._emerg_log:
            return
        try:
            self._emerg_log.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{sig.band},"
                f"{sig.freq:.4f},{sig.rssi:.1f},{sig.jump:.1f},{sig.trend},"
                f"{getattr(self, '_gps_lat', 0.0):.6f},"
                f"{getattr(self, '_gps_lon', 0.0):.6f}\n")
            self._emerg_log.flush()
        except Exception:
            pass

    def _log_scan(self, name, role, freq_hz, db, base, floor, thr, sig):
        """Log EVERY band sweep (not just alerts) for later threshold calibration."""
        if not self._scan_log:
            return
        try:
            self._scan_log.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{name},{role},"
                f"{freq_hz/1e6:.4f},{db:.1f},{base:.1f},{floor:.1f},"
                f"{sig.jump:.1f},{thr:.1f},{sig.trend},"
                f"{int(sig.active)},{int(self.network_present)},"
                f"{getattr(self, '_gps_lat', 0.0):.6f},"
                f"{getattr(self, '_gps_lon', 0.0):.6f}\n")
            self._scan_log.flush()
        except Exception:
            pass

    def _has_owners(self):
        return (any(getattr(self, name) is not None for name in
                    ("_dump1090_proc", "_rtl433_proc", "_emerg_proc"))
                or any(thread.is_alive() for thread in self._threads))

    def _spawn_owned(self, name, command, **kwargs):
        # Register under the same lock used by stop's snapshot. A worker
        # cannot launch an untracked child just after shutdown began.
        with self._lifecycle_lock:
            if self._stop_requested or self._stopping or self._stop_event.is_set():
                return None
            proc = subprocess.Popen(command, start_new_session=True, **kwargs)
            setattr(self, name, proc)
            return proc

    @staticmethod
    def _signal_owned_process(proc, *, kill=False):
        if os.name == "posix":
            # Each owned child has its own session/process group.
            os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif kill:
            proc.kill()
        else:
            proc.terminate()

    def _stop_owned_process(self, proc, deadline):
        try:
            if proc.poll() is None:
                try:
                    self._signal_owned_process(proc)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=min(0.5, max(0, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    try:
                        self._signal_owned_process(proc, kill=True)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=max(0, deadline - time.monotonic()))
            else:
                proc.wait(timeout=0)  # Reap an already-exited child as well.
            return proc.poll() is not None
        except Exception as exc:
            log.warning("SDR child %s has not been released: %s", proc.pid, exc)
            return False

    def stop(self) -> bool:
        """Stop/reap owned children and join readers before closing logs.

        False retains ownership and open logs; callers must not power off
        the SDR on that result. The finite wait budget also permits retry.
        """
        deadline = time.monotonic() + 5
        # Freeze only the visible observations, without resurrecting already
        # expired entries between the app's periodic prune calls. The OFF
        # intent starts display expiry even if reader shutdown needs retry.
        self._prune_aircraft(time.time())
        if self._adsb_receiving():
            self._adsb_off_decay.freeze(
                [(ac.icao, ac.lat, ac.lon) for ac in self.visible_aircraft()
                 if self._owns_aircraft(ac)],
                receiver=(getattr(self, "_gps_lat", None),
                          getattr(self, "_gps_lon", None)))
        self._stop_requested = True
        self._stop_event.set()
        if not self._lifecycle_lock.acquire(timeout=0.5):
            self._events.put(("error", "SDR startup has not released ownership"))
            return False
        try:
            if self._stopping:
                return False
            self._stopping = True
            processes = [(name, getattr(self, name)) for name in
                         ("_dump1090_proc", "_rtl433_proc", "_emerg_proc")]
            threads = list(self._threads)
        finally:
            self._lifecycle_lock.release()

        complete = False
        try:
            for name, proc in processes:
                if proc is not None and self._stop_owned_process(proc, deadline):
                    with self._lifecycle_lock:
                        if getattr(self, name) is proc:
                            setattr(self, name, None)
            # Never hold the lifecycle lock during join: the emergency
            # reader needs it to finish releasing its child handle.
            for thread in threads:
                if thread is not threading.current_thread() and thread.is_alive():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
            with self._lifecycle_lock:
                self._threads = [thread for thread in self._threads if thread.is_alive()]
                if self._has_owners():
                    self._events.put(("error", "SDR shutdown pending; ownership retained"))
                    return False
            for _, proc in processes:
                for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
            for f in (self._adsb_log, self._sensor_log, self._emerg_log,
                      self._scan_log):
                if f:
                    try:
                        f.close()
                    except Exception:
                        pass
            self._adsb_log = None
            self._sensor_log = None
            self._emerg_log = None
            self._scan_log = None

            self.emergency_active = False
            self.network_present = False
            # The last scan gradually fades while OFF; nothing is replayed
            # from saved history when the game restarts.
            self.running = False
            self.mode = ""
            complete = True
            return True
        finally:
            with self._lifecycle_lock:
                self._stopping = False
                if complete:
                    self._stop_requested = False

    def poll_events(self) -> list:
        """Drain event queue. Returns list of (type, data) tuples."""
        events = []
        while not self._events.empty():
            try:
                events.append(self._events.get_nowait())
            except Exception:
                break
        return events

    def prune_stale(self):
        """Remove aircraft/sensors not seen recently."""
        now = time.time()
        self._prune_aircraft(now)

        stale_s = [k for k, v in self.sensors.items()
                   if now - v.last_seen > SENSOR_TIMEOUT]
        for k in stale_s:
            del self.sensors[k]

    def _adsb_receiving(self) -> bool:
        return (self.running and "adsb" in self.mode and
                not self._stop_requested and not self._stop_event.is_set())

    @staticmethod
    def _owns_aircraft(ac: Aircraft) -> bool:
        # Source adapters own their explicitly tagged observations, including
        # their removal policy. The receiver only expires its own results.
        return getattr(ac, "acquisition_source", None) in (None, "local_rf")

    def _aircraft_in_range(self, ac: Aircraft, now: float) -> bool:
        # As in LOCOSP 0.9.16, any received message is evidence of range;
        # position_seen describes the last coordinates, not receiver activity.
        seen = ac.last_seen
        return (type(seen) in (int, float) and math.isfinite(seen) and
                0 < seen <= now and
                now - max(seen, self._adsb_resumed_at) <= ADSB_TIMEOUT)

    def _prune_aircraft(self, now: float):
        receiving = self._adsb_receiving()
        for key, aircraft in list(self.aircraft.items()):
            if (self._owns_aircraft(aircraft) and
                    not (self._aircraft_in_range(aircraft, now) if receiving
                         else self._adsb_off_decay.visible(key)) and
                    self.aircraft.get(key) is aircraft):
                self.aircraft.pop(key, None)

    def visible_aircraft(self, now: float | None = None) -> list[Aircraft]:
        """Return active reception, or the distance-decaying last scan while OFF.

        Map and small radar use the same view. Receiving any message keeps
        the last valid coordinates visible; new coordinates move the marker.
        Saved observations and XP are independent of display retention.
        """
        now = time.time() if now is None else now
        receiving = self._adsb_receiving()
        return [ac for ac in list(self.aircraft.values())
                if (self._owns_aircraft(ac) and
                    (self._aircraft_in_range(ac, now) if receiving
                     else self._adsb_off_decay.visible(ac.icao)) and
                    math.isfinite(ac.lat) and math.isfinite(ac.lon) and
                    -90 <= ac.lat <= 90 and -180 <= ac.lon <= 180 and
                    ac.has_position)]

    def update_gps(self, lat: float, lon: float):
        """Update GPS position for tagging new sensors / logs. Ignores None so a
        pre-fix position never reaches the loggers (would raise on "{None:.6f}"
        and silently leave 0-byte logs)."""
        if isinstance(lat, (int, float)):
            self._gps_lat = lat
        if isinstance(lon, (int, float)):
            self._gps_lon = lon

    # ------------------------------------------------------------------
    # SBS BaseStation format parser (ADS-B via dump1090 port 30003)
    # ------------------------------------------------------------------
    def _sbs_reader(self):
        """Connect to dump1090 SBS output and parse aircraft data."""
        import socket
        if self._stop_event.wait(2):  # wait for dump1090, interruptibly
            return

        while not self._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(("127.0.0.1", 30003))
                sock.settimeout(2)
                buf = ""
                while not self._stop_event.is_set():
                    try:
                        chunk = sock.recv(4096).decode("ascii", errors="ignore")
                        if not chunk:
                            break
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            self._parse_sbs_line(line.strip())
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                sock.close()
            except Exception:
                if self._stop_event.is_set():
                    return
                if self._stop_event.wait(3):  # retry connection, interruptibly
                    return

    def _parse_sbs_line(self, line: str):
        """Parse SBS BaseStation format line.

        Format: MSG,type,ses,id,icao,id,date,time,date,time,
                callsign,alt,speed,heading,lat,lon,vrate,squawk,...
        """
        parts = line.split(",")
        if len(parts) < 11 or parts[0] != "MSG":
            return

        msg_type = parts[1].strip()
        icao = parts[4].strip().upper()
        if not icao:
            return

        now = time.time()
        ac = self.aircraft.get(icao)
        is_new = ac is None or not self._owns_aircraft(ac)
        if is_new:
            # A real RF observation owns this identity, but must not inherit
            # coordinates supplied by an independently managed source.
            ac = Aircraft(icao=icao)
            self.aircraft[icao] = ac
        ac.last_seen = now

        try:
            if msg_type == "1" and len(parts) > 10:
                cs = parts[10].strip()
                if cs:
                    ac.callsign = cs
            elif msg_type == "3" and len(parts) > 15:
                alt = parts[11].strip()
                spd = parts[12].strip()
                hdg = parts[13].strip()
                lat = parts[14].strip()
                lon = parts[15].strip()
                if alt:
                    ac.altitude = int(float(alt))
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
                if lat and lon:
                    new_lat, new_lon = float(lat), float(lon)
                    if (math.isfinite(new_lat) and math.isfinite(new_lon) and
                            -90 <= new_lat <= 90 and -180 <= new_lon <= 180 and
                            (new_lat != 0.0 or new_lon != 0.0)):
                        ac.lat, ac.lon = new_lat, new_lon
                        ac.position_seen = now
            elif msg_type == "4" and len(parts) > 13:
                spd = parts[12].strip()
                hdg = parts[13].strip()
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
            elif msg_type == "6" and len(parts) > 17:
                sq = parts[17].strip()
                if sq:
                    ac.squawk = sq
        except (ValueError, IndexError):
            pass

        had_pos_before = not is_new and ac.has_position and msg_type != "3"

        if is_new:
            self.total_aircraft_seen += 1
            label = ac.callsign or icao
            self._events.put(("aircraft_new", ac))
            pos = f"pos:{ac.lat:.3f},{ac.lon:.3f}" if ac.has_position else "waiting for position..."
            self._events.put(("log",
                              f"[ADS-B] New: {label} "
                              f"alt:{ac.altitude}ft {pos}"))
            ac._logged_pos = ac.has_position
        elif msg_type == "3" and ac.has_position and not getattr(ac, '_logged_pos', False):
            # First position fix for this aircraft
            label = ac.callsign or icao
            self._events.put(("log",
                              f"[ADS-B] {label} located: "
                              f"{ac.lat:.3f},{ac.lon:.3f} "
                              f"alt:{ac.altitude}ft spd:{ac.speed}kt"))
            ac._logged_pos = True

        # Log to CSV
        if ac.has_position and self._adsb_log:
            try:
                self._adsb_log.write(
                    f"{int(now)},{icao},{ac.callsign},{ac.lat:.6f},"
                    f"{ac.lon:.6f},{ac.altitude},{ac.speed},"
                    f"{ac.heading},{ac.squawk}\n")
                self._adsb_log.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # rtl_433 JSON parser (433 MHz ISM sensors)
    # ------------------------------------------------------------------
    def _rtl433_reader(self):
        """Read rtl_433 JSON output line by line."""
        proc = self._rtl433_proc
        if not proc or not proc.stdout:
            return

        while not self._stop_event.is_set():
            try:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                self._parse_rtl433_line(
                    line.decode("utf-8", errors="replace").strip())
            except Exception:
                if self._stop_event.is_set():
                    return
                continue

    def _parse_rtl433_line(self, line: str):
        """Parse a JSON line from rtl_433."""
        if not line.startswith("{"):
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        model = data.get("model", "Unknown")
        sid_num = data.get("id", data.get("channel", ""))
        sid = f"{model}_{sid_num}"
        now = time.time()

        is_new = sid not in self.sensors
        sensor = self.sensors.setdefault(sid, Sensor433(
            model=model, sid=sid,
            lat=getattr(self, "_gps_lat", 0.0),
            lon=getattr(self, "_gps_lon", 0.0),
        ))
        sensor.data = data
        sensor.last_seen = now
        sensor.count += 1

        if is_new:
            self.total_sensors_seen += 1
            self._events.put(("sensor_new", sensor))

        # Build display string
        display_parts = [f"[433] {model}"]
        for key in ("temperature_C", "humidity", "pressure_hPa",
                     "battery_ok", "wind_avg_km_h", "rain_mm"):
            if key in data:
                short = key.split("_")[0]
                display_parts.append(f"{short}:{data[key]}")
        rssi = data.get("rssi")
        if rssi is not None:
            display_parts.append(f"RSSI:{rssi}")
        self._events.put(("log", " ".join(display_parts)))

        # Log to CSV
        if self._sensor_log:
            try:
                safe_data = json.dumps(data, ensure_ascii=True)
                self._sensor_log.write(
                    f"{int(now)},{model},{sid_num},"
                    f"\"{safe_data}\","
                    f"{sensor.lat:.6f},{sensor.lon:.6f}\n")
                self._sensor_log.flush()
            except Exception:
                pass
