"""Strict wardriver inputs and scoped, quoted exports; no GPS substitution."""
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
# This exact eleven-column header is present in the reference Dark3D image.
# It is an input schema as well as the shape of our normalized native export;
# the versioned preamble still distinguishes the two kinds of source.
DARK3D_HEADER = NATIVE_HEADER
DARK3D_FORMAT = "dark3d-wigle-1.4"
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


def parse_dark3d_record(fields):
    """Retain the eleven source fields without padding absent Piglet columns.

    A naive Dark3D FirstSeen has no verified timezone. Preserve it verbatim,
    but withhold an absolute capture timestamp rather than assuming host/UTC.
    """
    if (len(fields) != 11 or any(not isinstance(value, str) or "\x00" in value for value in fields)
            or sum(len(value.encode("utf-8", errors="surrogateescape")) for value in fields) > MAX_PHYSICAL_LINE):
        raise ValueError("Dark3D row must contain eleven bounded non-NUL fields")
    try:
        for index, value in enumerate(fields):
            if index != 1:
                value.encode("utf-8")
        name_bytes = fields[1].encode("utf-8", errors="surrogateescape")
    except UnicodeError:
        raise ValueError("Invalid Dark3D source bytes outside a radio name") from None
    mac, kind = fields[0].strip().upper(), fields[10].strip().upper()
    if not _MAC.fullmatch(mac):
        raise ValueError("Invalid Dark3D MAC")
    if kind not in ("WIFI", "BLE"):
        raise ValueError("Unsupported Dark3D record type")
    source_time = fields[3].strip()
    timestamp = re.fullmatch(
        r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?",
        source_time)
    if source_time and timestamp is None:
        raise ValueError("Invalid Dark3D FirstSeen format")
    try:
        # Historical wardrivers concatenated GPS components without padding.
        # Normalize only this parsing temporary, never the retained source value.
        if timestamp is None:
            stamp = None
        else:
            year, month, day, hour, minute, second = (int(value) for value in timestamp.groups()[:6])
            fraction, offset = timestamp.groups()[6:]
            normalized = (f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
                          + (fraction or "") + (offset or ""))
            stamp = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        stamp = None
    timezone_known = stamp is not None and stamp.tzinfo is not None
    channel = _number(fields[4], integer=True)
    rssi = _number(fields[5], integer=True)
    lat, lon = (_number(value, optional=True) for value in fields[6:8])
    alt, accuracy = (_number(value, optional=True) for value in fields[8:10])
    if not 0 <= channel <= 196 or not -128 <= rssi <= 127:
        raise ValueError("Dark3D radio values outside range")
    if (lat is None) != (lon is None):
        raise ValueError("Partial Dark3D position")
    if lat is not None and (not -90 <= lat <= 90 or not -180 <= lon <= 180):
        raise ValueError("Dark3D coordinates outside range")
    if accuracy is not None and accuracy < 0:
        raise ValueError("Negative Dark3D source accuracy")
    source, canonical = list(fields), list(fields)
    canonical[0], canonical[10] = mac, kind
    # The format namespace keeps existing Piglet identities untouched. File
    # name, preamble and transfer metadata never participate in observation ID.
    identity = [DARK3D_FORMAT, canonical]
    record_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="surrogateescape")).hexdigest()
    name_metadata = {}
    try:
        name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        source[1] = "".join("\\x%02x" % byte for byte in name_bytes)
        name_metadata = dict(source_name_bytes_hex=name_bytes.hex(), source_name_encoding="raw-bytes")
    position = None if lat is None or lat == lon == 0 else dict(
        lat=lat, lon=lon, altitude=alt, accuracy=accuracy, source="dark3d", source_time=fields[3])
    record = dict(record_id=record_id, protocol=kind.lower(), mac=mac,
        ssid=source[1] if kind == "WIFI" else "", name=source[1] if kind == "BLE" else "",
        auth=fields[2] if kind == "WIFI" else "[BLE]", channel=channel, rssi=rssi,
        captured_at=stamp.isoformat().replace("+00:00", "Z") if timezone_known else None,
        capture_time_known=timezone_known, capture_timezone_known=timezone_known,
        source_time=fields[3], position=position, source_fields=source, source_format=DARK3D_FORMAT)
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


def dark3d_records(path):
    """Read the reference Dark3D WiGLE 1.4 schema; source bytes stay on disk."""
    with Path(path).open("r", encoding="utf-8-sig", errors="surrogateescape", newline="") as stream:
        lines = _physical_lines(stream)
        preamble = next(lines, "")
        try:
            preamble.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid Dark3D source bytes in CSV preamble") from None
        if "\x00" in preamble or not preamble.startswith("WigleWifi-1.4,"):
            raise ValueError("Expected Dark3D WiGLE 1.4 preamble")
        reader = csv.reader(lines, strict=True)
        if tuple(next(reader, ())) != DARK3D_HEADER:
            raise ValueError("Unexpected Dark3D eleven-column header")
        for fields in reader:
            if fields:
                record = parse_dark3d_record(fields)
                record["source_preamble"] = preamble.rstrip("\r\n")
                yield record


def collection_records(path):
    """Dispatch by versioned content, except a genuinely empty Dark3D .log."""
    path = Path(path)
    # Legacy Dark3D can close a log before writing its preamble. There are no
    # observations to parse, and no source schema to invent. This exception is
    # deliberately limited to zero bytes and the original .log archive suffix.
    if path.suffix.lower() == ".log" and path.stat().st_size == 0:
        return
    with Path(path).open("r", encoding="utf-8-sig", errors="surrogateescape", newline="") as stream:
        preamble = next(_physical_lines(stream), "")
    if preamble.startswith("WigleWifi-1.6,"):
        yield from piglet_records(path)
    elif preamble.startswith("WigleWifi-1.4,"):
        yield from dark3d_records(path)
    else:
        raise ValueError("Unsupported Collection WiGLE source format")


def native_row(record):
    f = record["source_fields"]
    if record.get("source_format") == DARK3D_FORMAT:
        return [record["mac"], f[1], f[2] if record["protocol"] == "wifi" else "[BLE]",
                *f[3:10], record["protocol"].upper()]
    return [record["mac"], f[1], f[2] if record["protocol"] == "wifi" else "[BLE]",
            f[3], f[4], f[6], *f[7:11], record["protocol"].upper()]
