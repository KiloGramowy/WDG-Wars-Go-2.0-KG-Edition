"""Strict Piglet input and scoped, quoted native exports; no GPS substitution."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re

PIGLET_HEADER = ("MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "Frequency", "RSSI",
                 "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters",
                 "RCOIs", "MfgrId", "Type")
NATIVE_HEADER = ("MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
                 "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters", "Type")
PREAMBLE = "WigleWifi-1.6,appRelease=PigletCollection1,source=PigletCollection1"
MAX_PHYSICAL_LINE = 16384
_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


def collection_preamble(value):
    return value.rstrip("\r\n").startswith("WigleWifi-") and "source=PigletCollection1" in value.rstrip("\r\n").split(",")


def native_rows(stream, legacy_parser, *, legacy_prefix=False):
    """Use strict RFC CSV only for our marked exports; preserve old physical rows."""
    first = stream.readline()
    second = stream.readline()
    if collection_preamble(first):
        if tuple(next(csv.reader([second]), [])) != NATIVE_HEADER:
            raise ValueError("Invalid Collection export header")
        for row in csv.reader(stream, strict=True):
            if not row:
                continue
            if len(row) != 11 or not _MAC.fullmatch(row[0]) or row[10] not in ("WIFI", "BLE"):
                raise ValueError("Invalid Collection export row")
            yield row
    else:
        if legacy_prefix:
            for line in (first, second):
                row = legacy_parser(line)
                if row is not None:
                    yield row
        for line in stream:
            row = legacy_parser(line)
            if row is not None:
                yield row


def native_rows_text(content, legacy_parser):
    return native_rows(io.StringIO(content, newline=""), legacy_parser)


def _number(value, *, integer=False, optional=False):
    if value == "" and optional:
        return None
    try:
        result = int(value) if integer else float(value)
    except (ValueError, OverflowError):
        raise ValueError("Invalid numeric Piglet field") from None
    if not math.isfinite(result):
        raise ValueError("Non-finite Piglet field")
    return result


def parse_record(fields):
    if len(fields) != 14 or any("\x00" in f for f in fields):
        raise ValueError("Piglet row must contain exactly fourteen non-NUL fields")
    # Radio names are byte strings. Historical firmware could write a BLE name
    # that was not UTF-8; every other field must still decode strictly.
    try:
        for index, value in enumerate(fields):
            if index != 1:
                value.encode("utf-8")
        name_bytes = fields[1].encode("utf-8", errors="surrogateescape")
    except UnicodeError:
        raise ValueError("Invalid source bytes outside a radio name") from None
    mac = fields[0].strip().upper()
    if not _MAC.fullmatch(mac):
        raise ValueError("Invalid Piglet MAC")
    kind = fields[13].strip().upper()
    if kind not in ("WIFI", "BLE"):
        raise ValueError("Unsupported Piglet record type")
    # This firmware's naive FirstSeen is explicitly UTC, including its 1970 fallback.
    source_time = fields[3].strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)?", source_time):
        raise ValueError("Invalid source FirstSeen format")
    try:
        stamp = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
    except ValueError:
        # Historical GPS startup rows can contain e.g. 2000-00-00. Keep that
        # exact source value and explicitly withhold a capture timestamp.
        stamp = None
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    elif stamp is not None and stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("Piglet FirstSeen must be UTC")
    channel = _number(fields[4], integer=True)
    frequency = _number(fields[5])
    rssi = _number(fields[6], integer=True)
    lat, lon = (_number(f, optional=True) for f in fields[7:9])
    alt, accuracy = (_number(f, optional=True) for f in fields[9:11])
    if not 0 <= channel <= 196 or not 0 <= frequency <= 100000 or not -128 <= rssi <= 127:
        raise ValueError("Piglet radio values outside range")
    if (lat is None) != (lon is None):
        raise ValueError("Partial Piglet position")
    if lat is not None and (not -90 <= lat <= 90 or not -180 <= lon <= 180):
        raise ValueError("Piglet coordinates outside range")
    if accuracy is not None and accuracy < 0:
        raise ValueError("Negative Piglet source accuracy")
    source = list(fields)
    canonical = list(fields)
    canonical[0], canonical[13] = mac, kind
    # Preserve every existing valid-record identity. For invalid names hash the
    # original byte string, never its display escape (which could be a real name).
    record_id = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="surrogateescape")).hexdigest()
    name_metadata = {}
    try:
        name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        source[1] = "".join("\\x%02x" % byte for byte in name_bytes)
        name_metadata = dict(source_name_bytes_hex=name_bytes.hex(), source_name_encoding="raw-bytes")
    position = None if lat is None or lat == lon == 0 else dict(
        lat=lat, lon=lon, altitude=alt if alt is not None else 0.0,
        accuracy=accuracy if accuracy is not None else 0.0, source="piglet", source_time=fields[3])
    record = dict(record_id=record_id, protocol=kind.lower(), mac=mac,
        ssid=source[1] if kind == "WIFI" else "", name=source[1] if kind == "BLE" else "",
        auth=fields[2] if kind == "WIFI" else "[BLE]", channel=channel,
        frequency=frequency, rssi=rssi, captured_at=stamp.isoformat().replace("+00:00", "Z") if stamp is not None else None,
        source_time=fields[3], position=position, source_fields=source)
    if stamp is None:
        record["capture_time_known"] = False
    record.update(name_metadata)
    return record


def _physical_lines(stream):
    """Bound a physical line before csv.reader can accumulate a quoted field."""
    while True:
        line = stream.readline(MAX_PHYSICAL_LINE + 1)
        if not line:
            return
        if len(line) > MAX_PHYSICAL_LINE or len(line.encode("utf-8", errors="surrogateescape")) > MAX_PHYSICAL_LINE:
            raise ValueError("Piglet physical CSV line exceeds 16 KiB")
        yield line


def piglet_records(path):
    with Path(path).open("r", encoding="utf-8-sig", errors="surrogateescape", newline="") as stream:
        lines = _physical_lines(stream)
        preamble = next(lines, "")
        try:
            preamble.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid source bytes in CSV preamble") from None
        if not preamble.startswith("WigleWifi-1.6,"):
            raise ValueError("Expected Piglet WiGLE 1.6 preamble")
        reader = csv.reader(lines, strict=True)
        if tuple(next(reader, ())) != PIGLET_HEADER:
            raise ValueError("Unexpected Piglet fourteen-column header")
        for fields in reader:
            if fields:
                yield parse_record(fields)


def native_row(record):
    f = record["source_fields"]
    return [record["mac"], f[1], f[2] if record["protocol"] == "wifi" else "[BLE]",
            f[3], f[4], f[6], *f[7:11], record["protocol"].upper()]
