#!/usr/bin/env python3
"""Read-only Piglet ESP32-C5 backup using esptool 5.6.0 and its RAM stub.

Creates two independent 8 MiB reads in 64 KiB requests and verifies local files.
Does not program persistent device storage or boot the application afterward.
Output contains private device data: keep it outside repositories and packages.
The device must be exclusively available; close serial monitors before running.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import zlib


ESPTOOL_VERSION = "5.6.0"
FLASH_BYTES = 0x800000
CHUNK_BYTES = 0x10000
TABLE_OFFSET = 0x8000
TABLE_BYTES = 0x1000
OTA_OFFSET = 0xE000
OTA_BYTES = 0x2000
EXPECTED_PARTITIONS = (
    (1, 2, 0x9000, 0x5000, "nvs", 0),
    (1, 0, 0xE000, 0x2000, "otadata", 0),
    (0, 0x10, 0x10000, 0x330000, "app0", 0),
    (0, 0x11, 0x340000, 0x330000, "app1", 0),
    (1, 0x82, 0x670000, 0x180000, "spiffs", 0),
    (1, 3, 0x7F0000, 0x10000, "coredump", 0),
)


class BackupError(RuntimeError):
    """A safety or integrity check failed; no deployment is authorized."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def parse_partition_table(table):
    if len(table) != TABLE_BYTES:
        raise BackupError("Partition-table read has the wrong length")
    entries = []
    checksum_found = False
    for offset in range(0, TABLE_BYTES, 32):
        row = table[offset:offset + 32]
        if row[:2] == b"\xeb\xeb":
            if row[2:16] != b"\xff" * 14 or row[16:] != hashlib.md5(table[:offset]).digest():
                raise BackupError("Partition-table MD5 is invalid")
            if table[offset + 32:] != b"\xff" * (TABLE_BYTES - offset - 32):
                raise BackupError("Unexpected data after the partition-table checksum")
            checksum_found = True
            break
        if row[:2] != b"\xaa\x50":
            raise BackupError("Partition-table entry or checksum is missing/unsupported")
        _, kind, subtype, address, size, raw_label, flags = struct.unpack("<HBBII16sI", row)
        try:
            label = raw_label.rstrip(b"\x00").decode("ascii")
        except UnicodeError as error:
            raise BackupError("Partition label is not ASCII") from error
        if not size or address < 0x9000 or address + size > FLASH_BYTES or flags:
            raise BackupError("Unsupported partition bounds or security flags")
        entries.append((kind, subtype, address, size, label, flags))
    if not checksum_found or tuple(entries) != EXPECTED_PARTITIONS:
        raise BackupError("Partition layout differs from the tested default_8MB Piglet layout")
    return [dict(type=e[0], subtype=e[1], offset=e[2], size=e[3], label=e[4], flags=e[5])
            for e in entries]


def parse_ota_selection(ota):
    if len(ota) != OTA_BYTES:
        raise BackupError("OTA-data read has the wrong length")
    entries = []
    for index in (0, 1):
        row = ota[index * 0x1000:index * 0x1000 + 32]
        sequence, state, checksum = struct.unpack("<I20xII", row)
        if state not in (0, 1, 2, 3, 4, 0xFFFFFFFF):
            raise BackupError("Unsupported OTA image state")
        computed = zlib.crc32(row[:4], 0xFFFFFFFF) & 0xFFFFFFFF
        valid = sequence != 0xFFFFFFFF and state not in (3, 4) and checksum == computed
        entries.append(dict(copy=index, sequence=sequence, state=state,
                            crc=checksum, computed_crc=computed, valid=valid))
    valid = [entry for entry in entries if entry["valid"]]
    if not valid:
        raise BackupError("No CRC-valid OTA selection; active slot requires separate review")
    selected = max(valid, key=lambda entry: entry["sequence"])
    if selected["sequence"] == 0:
        raise BackupError("OTA sequence zero alone does not establish the tested boot slot")
    return entries, (selected["sequence"] - 1) % 2


def analyze_flash(data):
    if len(data) != FLASH_BYTES:
        raise BackupError("Full flash image is not exactly 8 MiB")
    partitions = parse_partition_table(data[TABLE_OFFSET:TABLE_OFFSET + TABLE_BYTES])
    ota, active_index = parse_ota_selection(data[OTA_OFFSET:OTA_OFFSET + OTA_BYTES])
    if active_index != 0:
        raise BackupError("Active slot is not app0; stop for an app-only deployment/rollback review")
    active = next(row for row in partitions if row["label"] == "app0")
    app = data[active["offset"]:active["offset"] + active["size"]]
    if not app or app[0] != 0xE9:
        raise BackupError("Active app0 has no ESP image header")
    return dict(partitions=partitions, ota_entries=ota, active_slot="app0",
                active_offset=active["offset"], active_slot_bytes=active["size"],
                active_basis="highest CRC-valid OTA sequence; (sequence-1)%2 = app0",
                app_slot_sha256=sha256(app))


def validate_hardware(esp):
    if esp.CHIP_NAME != "ESP32-C5":
        raise BackupError("Only a verified ESP32-C5 is supported")
    security = esp.get_security_info()
    if security.get("flags") != 0:
        raise BackupError("Device security flags differ from zero; stop for separate review")
    return dict(chip=esp.CHIP_NAME, revision=esp.get_chip_revision(), security_flags=0)


def validate_flash_id(flash_id):
    # JEDEC density byte 23 means 2**23 bytes (8 MiB).
    if (flash_id >> 16) & 0xFF != 23:
        raise BackupError("JEDEC flash density is not the verified 8 MiB")


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    if path.read_bytes() != data:
        raise BackupError("Local saved-file reread differs: " + path.name)


def save_json(path, value):
    write_private(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


def read_one_flash(esp, output, number):
    target = output / ("flash-read-%d.bin" % number)
    if target.exists():
        raise BackupError("Backup destination already exists")
    partial = target.with_suffix(".bin.partial")
    digest = hashlib.sha256()
    started = time.monotonic()
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        for offset in range(0, FLASH_BYTES, CHUNK_BYTES):
            block = esp.read_flash(offset, CHUNK_BYTES)
            if len(block) != CHUNK_BYTES:
                raise BackupError("Short flash read at " + hex(offset))
            stream.write(block)
            digest.update(block)
            if (offset + CHUNK_BYTES) % 0x100000 == 0:
                print("Read %d: %d/%d bytes" % (number, offset + CHUNK_BYTES, FLASH_BYTES), flush=True)
        stream.flush()
        os.fsync(stream.fileno())
    reread = partial.read_bytes()
    if len(reread) != FLASH_BYTES or sha256(reread) != digest.hexdigest():
        raise BackupError("Local full-backup reread failed")
    # The output directory is new and exclusive; do not reuse prior backup files.
    if target.exists():
        raise BackupError("Backup destination already exists")
    partial.rename(target)
    return dict(path=target.name, bytes=len(reread), sha256=digest.hexdigest(),
                seconds=round(time.monotonic() - started, 2))


def backup_device(port, output, before):
    # Import only for an explicitly requested physical backup. Parser tests never
    # import esptool or open a serial port; no guessed port or device is selected.
    import esptool
    from esptool.cmds import attach_flash, connect_esp, run_stub
    if esptool.__version__ != ESPTOOL_VERSION:
        raise BackupError("Use exactly esptool " + ESPTOOL_VERSION + "; nothing is installed automatically")
    package = Path(__file__).resolve().parent
    output = output.resolve()
    repository = next((parent for parent in (package, *package.parents)
                       if (parent / ".git").exists()), None)
    if (output == package or package in output.parents or
            (repository and (output == repository or repository in output.parents)) or
            any((parent / ".git").exists() for parent in (output, *output.parents))):
        raise BackupError("Private backup output must be outside this package/repository")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    esp = None
    try:
        esp = connect_esp(port, chip="esp32c5", before=before, connect_attempts=3)
        info = validate_hardware(esp)
        esp = run_stub(esp)  # official C5 stub is loaded into RAM only
        if not esp.IS_STUB:
            raise BackupError("Official RAM stub did not start")
        attach_flash(esp)
        flash_id = esp.flash_id(cache=False)
        validate_flash_id(flash_id)
        info.update(flash_id=hex(flash_id), flash_bytes=FLASH_BYTES,
                    esptool=ESPTOOL_VERSION, chunk_bytes=CHUNK_BYTES, before=before)
        save_json(output / "hardware.json", info)
        reads = [read_one_flash(esp, output, number) for number in (1, 2)]
        save_json(output / "flash-reads.json", reads)
        if reads[0]["sha256"] != reads[1]["sha256"]:
            raise BackupError("Two independent full flash reads differ")
        first = (output / reads[0]["path"]).read_bytes()
        second = (output / reads[1]["path"]).read_bytes()
        if first != second:
            raise BackupError("Local independent backup contents differ")
        result = analyze_flash(first)
        table = first[TABLE_OFFSET:TABLE_OFFSET + TABLE_BYTES]
        ota = first[OTA_OFFSET:OTA_OFFSET + OTA_BYTES]
        app = first[result["active_offset"]:result["active_offset"] + result["active_slot_bytes"]]
        write_private(output / "partitions.bin", table)
        write_private(output / "otadata.bin", ota)
        write_private(output / "previous-app0-slot.bin", app)
        save_json(output / "partitions.json", result["partitions"])
        result.update(two_reads_identical=True, full_flash_sha256=sha256(first),
                      partition_sha256=sha256(table), ota_sha256=sha256(ota),
                      rollback_file="previous-app0-slot.bin", rollback_offset="0x10000",
                      backup_only=True, application_started=False)
        save_json(output / "backup-verification.json", result)
        print(json.dumps(result, indent=2), flush=True)
        return result
    except Exception as error:
        save_json(output / "FAILED.json", dict(status="FAILED / NO-GO", error=str(error),
                                               deployment_authorized=False))
        raise
    finally:
        if esp is not None:
            esp._port.close()
        print("Port closed. No application boot requested; recover/reset separately as documented.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="explicit verified Piglet serial port")
    parser.add_argument("--output", required=True, type=Path, help="new private directory outside the repository")
    parser.add_argument("--before", choices=("usb-reset", "no-reset"), default="usb-reset",
                        help="no-reset is only for an already active ROM loader")
    args = parser.parse_args(argv)
    try:
        backup_device(args.port, args.output, args.before)
    except Exception as error:
        print("Backup FAILED / NO-GO: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
