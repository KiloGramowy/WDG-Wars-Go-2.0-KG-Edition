"""GPS receiver — NMEA parser for UART/USB GPS modules."""

import glob
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import serial

from .config import GPS_DEVICE, GPS_BAUD_RATE

log = logging.getLogger(__name__)

# AUTO radio selection needs fresh, measured positions, independent of the
# player's map position and the older display-oriented ``fix.valid`` flag.
RADIO_FIX_MAX_AGE = 10.0
RADIO_FIX_RMC_MAX_AGE = 2.0
RADIO_FIX_RMC_TIME_TOLERANCE = 2.0
RADIO_FIX_RMC_DISTANCE_METRES = 100.0


@dataclass
class GpsFix:
    """Snapshot of current GPS state."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    speed_knots: float = 0.0
    satellites: int = 0
    satellites_visible: int = 0
    fix_quality: int = 0       # 0=no fix, 1=GPS, 2=DGPS
    hdop: float = 99.9
    timestamp: str = ""        # UTC time from NMEA (hhmmss.ss)
    valid: bool = False


class _LineBuffer:
    """Accumulate raw bytes and yield complete NMEA sentences."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, raw: bytes) -> List[str]:
        self._buf += raw
        lines: List[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded.startswith("$"):
                lines.append(decoded)
        # Prevent unbounded growth if no newlines arrive
        if len(self._buf) > 1024:
            self._buf = self._buf[-512:]
        return lines


class GpsManager:
    """Manage a UART GPS receiver via pyserial + urwid watch_file."""

    # True when user explicitly set the GPS device via env var
    _user_configured = bool(
        os.environ.get("WDG_GPS_DEVICE")
        or os.environ.get("JANOS_GPS_DEVICE")
    )

    def __init__(self, device: str = GPS_DEVICE,
                 baud: int = GPS_BAUD_RATE) -> None:
        self.device = device
        self._baud = baud
        self._conn: Optional[serial.Serial] = None
        self._buf = _LineBuffer()
        self.fix = GpsFix()
        self._available = False
        self._gsv_visible: dict = {}  # constellation prefix → satellite count
        self._radio_position = None
        self._recent_rmc = None
        self._last_gga = None

    @property
    def available(self) -> bool:
        return self._available

    def radio_fix(self, now=None) -> Optional[dict]:
        """Return a fresh credible GGA position, or None, without refreshing it.

        Complete checksum-valid GGA is sufficient; RMC is optional. When a
        fresh RMC is present it must agree in UTC time and position. GSV, UI
        polling and RMC alone cannot create/refresh this radio-selection fix.
        ``observed_at`` is the monotonic receive time, never NMEA/wall time.
        """
        now = time.monotonic() if now is None else now
        position = self._radio_position
        if not self.available or position is None or not math.isfinite(now):
            return None
        age = now - position["observed_at"]
        if not 0 <= age <= RADIO_FIX_MAX_AGE:
            return None
        return {key: position[key] for key in
                ("lat", "lon", "observed_at", "quality", "sats", "hdop")}

    def _invalidate_position(self):
        self.fix.valid = False
        self._radio_position = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> bool:
        """Try to open GPS serial port. Returns True on success.
        If user set WDG_GPS_DEVICE (or legacy JANOS_GPS_DEVICE), use that directly.
        Otherwise probe default path for NMEA, then auto-detect USB GPS.
        Never raises — GPS is optional."""
        device = self.device

        if self._user_configured:
            # User explicitly set env var — trust it
            if self._try_open(device):
                return True
            log.info("GPS device %s (from env) not available — GPS disabled",
                     device)
            return False

        # No env var — probe default path, then auto-detect
        if os.path.exists(device) and self._probe_nmea(device, self._baud):
            if self._try_open(device):
                return True

        log.info("GPS not on %s, scanning for USB GPS...", device)
        detected = self._auto_detect(exclude={device})
        if detected:
            self.device = detected
            if self._try_open(detected):
                return True
        log.info("No GPS found — GPS disabled")
        return False

    @staticmethod
    def _probe_nmea(device: str, baud: int) -> bool:
        """Quick check if a serial port outputs NMEA sentences."""
        try:
            conn = serial.Serial(port=device, baudrate=baud, timeout=2)
            data = conn.read(512)
            conn.close()
            return b"$GP" in data or b"$GN" in data
        except Exception:
            return False

    def _try_open(self, device: str) -> bool:
        """Try to open a serial port as GPS. Returns True on success."""
        if not os.path.exists(device):
            return False
        if not os.access(device, os.R_OK):
            log.warning("No read access to %s", device)
            return False
        try:
            self._conn = serial.Serial(
                port=device,
                baudrate=self._baud,
                timeout=0,
            )
            self._conn.reset_input_buffer()
            self._invalidate_position()
            self._recent_rmc = None
            self._last_gga = None
            self._available = True
            log.info("GPS opened: %s @ %d baud", device, self._baud)
            return True
        except Exception as exc:
            log.debug("GPS open failed on %s: %s", device, exc)
            return False

    @staticmethod
    def _auto_detect(exclude: Optional[set] = None) -> Optional[str]:
        """Scan common serial paths for a GPS device (NMEA probe)."""
        skip = exclude or set()
        candidates = sorted(
            glob.glob("/dev/ttyUSB*")
            + glob.glob("/dev/ttyACM*")
            + glob.glob("/dev/ttyAMA*")
        )
        for path in candidates:
            if path in skip:
                continue
            if not os.access(path, os.R_OK):
                continue
            try:
                conn = serial.Serial(port=path, baudrate=9600, timeout=2)
                data = conn.read(512)
                conn.close()
                if b"$GP" in data or b"$GN" in data:
                    log.info("GPS auto-detected on %s", path)
                    return path
                log.debug("GPS probe %s — no NMEA data", path)
            except Exception:
                continue
        return None

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._available = False
        self._invalidate_position()
        self._recent_rmc = None
        self._last_gga = None

    @property
    def fd(self) -> int:
        """File descriptor for urwid watch_file."""
        if self._conn is None:
            raise RuntimeError("GPS port not open")
        return self._conn.fileno()

    # ------------------------------------------------------------------
    # Reading & parsing
    # ------------------------------------------------------------------

    def read_available(self) -> List[str]:
        """Non-blocking read — return complete NMEA sentences."""
        if not self._conn:
            return []
        try:
            waiting = self._conn.in_waiting
            if waiting <= 0:
                return []
            raw = self._conn.read(waiting)
            return self._buf.feed(raw)
        except Exception as exc:
            log.debug("GPS read error: %s", exc)
            self.close()
            return []

    def process_sentences(self, sentences: List[str]) -> None:
        """Parse NMEA sentences and update self.fix."""
        for s in sentences:
            try:
                self._parse(s)
            except Exception as exc:
                log.debug("GPS parse error: %s — %s", s.strip(), exc)

    def _parse(self, sentence: str) -> None:
        sentence = sentence.strip()
        body, marker, checksum = sentence.partition("*")
        kind = body.split(",", 1)[0]
        is_position = kind in ("$GPGGA", "$GNGGA", "$GPRMC", "$GNRMC")
        checksum_ok = False
        if marker and re.fullmatch(r"[0-9a-fA-F]{2}", checksum):
            expected = 0
            for character in body[1:]:
                expected ^= ord(character)
            checksum_ok = expected == int(checksum, 16)
        if not checksum_ok:
            if is_position:
                self._invalidate_position()
            return
        parts = body.split(",")
        if len(parts) < 3:
            if is_position:
                self._invalidate_position()
            return
        try:
            if kind in ("$GPGGA", "$GNGGA"):
                self._parse_gga(parts)
            elif kind in ("$GPRMC", "$GNRMC"):
                self._parse_rmc(parts)
            elif kind in ("$GPGSV", "$GLGSV", "$GNGSV", "$GBGSV", "$GAGSV"):
                self._parse_gsv(parts)
        except (ValueError, IndexError, OverflowError):
            if is_position:
                self._invalidate_position()
            raise

    @staticmethod
    def _utc_seconds(value: str) -> float:
        if not re.fullmatch(r"\d{6}(?:\.\d+)?", value):
            raise ValueError("invalid NMEA UTC time")
        hour, minute, second = int(value[:2]), int(value[2:4]), float(value[4:])
        if hour > 23 or minute > 59 or not 0 <= second < 60:
            raise ValueError("NMEA UTC time out of bounds")
        return hour * 3600 + minute * 60 + second

    @staticmethod
    def _position(parts, lat_index, lon_index):
        lat = GpsManager._coordinate(parts[lat_index], parts[lat_index + 1], True)
        lon = GpsManager._coordinate(parts[lon_index], parts[lon_index + 1], False)
        return lat, lon

    @staticmethod
    def _coordinate(value: str, direction: str, latitude: bool) -> float:
        width = 4 if latitude else 5
        if not re.fullmatch(r"\d{" + str(width) + r"}(?:\.\d+)?", value):
            raise ValueError("incomplete NMEA coordinate")
        if direction not in (("N", "S") if latitude else ("E", "W")):
            raise ValueError("invalid NMEA direction")
        degrees, minutes = int(value[:width - 2]), float(value[width - 2:])
        limit = 90 if latitude else 180
        if minutes >= 60 or degrees > limit or (degrees == limit and minutes != 0):
            raise ValueError("NMEA coordinate out of bounds")
        coordinate = degrees + minutes / 60
        return -coordinate if direction in ("S", "W") else coordinate

    @staticmethod
    def _consistent_positions(first, second):
        utc_delta = abs(first["utc_seconds"] - second["utc_seconds"])
        utc_delta = min(utc_delta, 86400 - utc_delta)  # midnight rollover
        if utc_delta > RADIO_FIX_RMC_TIME_TOLERANCE:
            return False
        lat1, lat2 = math.radians(first["lat"]), math.radians(second["lat"])
        dlat = lat2 - lat1
        dlon = math.radians(second["lon"] - first["lon"])
        hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        distance = 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, hav))))
        return distance <= RADIO_FIX_RMC_DISTANCE_METRES

    def _parse_gga(self, p: List[str]) -> None:
        """$GPGGA: time, lat, N/S, lon, E/W, quality, sats, hdop, alt, ..."""
        if len(p) < 10:
            raise ValueError("incomplete GGA")
        quality = int(p[6]) if p[6] else 0
        sats = int(p[7]) if p[7] else 0
        hdop = float(p[8]) if p[8] else 99.9
        utc_seconds = self._utc_seconds(p[1])
        if quality not in range(9) or not 0 <= sats <= 99 or not math.isfinite(hdop) or hdop < 0:
            raise ValueError("invalid GGA quality metrics")
        self.fix.fix_quality, self.fix.satellites, self.fix.hdop = quality, sats, hdop
        self.fix.timestamp = p[1]
        if quality == 0:
            self._invalidate_position()
            return
        lat, lon = self._position(p, 2, 4)
        altitude = float(p[9]) if p[9] else self.fix.altitude
        if not math.isfinite(altitude):
            raise ValueError("invalid GGA altitude")
        now = time.monotonic()
        position = dict(lat=lat, lon=lon, observed_at=now, quality=quality,
                        sats=sats, hdop=hdop, utc_seconds=utc_seconds)
        credible = quality in (1, 2, 4, 5) and sats >= 4 and hdop <= 3
        rmc = self._recent_rmc
        if (rmc is not None and 0 <= now - rmc["observed_at"] <= RADIO_FIX_RMC_MAX_AGE
                and not self._consistent_positions(position, rmc)):
            self._invalidate_position()
            return
        previous = self._last_gga
        self._last_gga = position
        repeated_epoch = previous is not None and previous["utc_seconds"] == utc_seconds
        if repeated_epoch and (previous["lat"] != lat or previous["lon"] != lon):
            self._invalidate_position()
            return
        self.fix.latitude, self.fix.longitude, self.fix.altitude = lat, lon, altitude
        self.fix.valid = True
        if repeated_epoch:
            # Repeated receiver output from the same epoch cannot make an old
            # observation fresh or count as several stable AUTO samples.
            if not credible:
                self._radio_position = None
            return
        self._radio_position = position if credible else None

    def _parse_rmc(self, p: List[str]) -> None:
        """$GPRMC: time, status, lat, N/S, lon, E/W, speed, ..."""
        if len(p) < 8:
            raise ValueError("incomplete RMC")
        utc_seconds = self._utc_seconds(p[1])
        if p[2] == "V":
            self.fix.timestamp = p[1]
            self._recent_rmc = None
            self._invalidate_position()
            return
        if p[2] != "A":
            raise ValueError("invalid RMC status")
        lat, lon = self._position(p, 3, 5)
        speed = float(p[7]) if p[7] else 0.0
        if not math.isfinite(speed) or speed < 0:
            raise ValueError("invalid RMC speed")
        now = time.monotonic()
        rmc = dict(lat=lat, lon=lon, utc_seconds=utc_seconds, observed_at=now)
        self._recent_rmc = rmc
        position = self._radio_position
        if (position is not None
                and 0 <= now - position["observed_at"] <= RADIO_FIX_MAX_AGE
                and not self._consistent_positions(position, rmc)):
            self._invalidate_position()
            return
        self.fix.valid = True
        self.fix.timestamp = p[1]
        self.fix.latitude, self.fix.longitude, self.fix.speed_knots = lat, lon, speed

    def _parse_gsv(self, p: List[str]) -> None:
        """$xxGSV: total_msgs, msg_num, sats_in_view, ..."""
        if len(p) < 4:
            return
        prefix = p[0][:3]  # $GP, $GL, $GN, $GB, $GA
        total_visible = int(p[3]) if p[3] else 0
        self._gsv_visible[prefix] = total_visible
        self.fix.satellites_visible = sum(self._gsv_visible.values())

    @staticmethod
    def _to_decimal(value: str, direction: str) -> float:
        """Convert NMEA ddmm.mmmm to decimal degrees."""
        return GpsManager._coordinate(value, direction, direction in ("N", "S"))
