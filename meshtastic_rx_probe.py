#!/usr/bin/env python3
"""Isolated, passive Meshtastic RX probe. Without --listen, no radio is opened.

Public legacy AES-CTR Data validation is structural, not authentication.
No TX, chat UI, GPS, game state, points, upload or automatic profile switching.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import time


COMMON = {
    "region": "EU_868", "coding_rate_denominator": 5,
    "sync_word_sx126x": "0x24b4", "preamble_symbols": 16,
    "explicit_header": True, "crc": True, "inverted_iq": False, "ldro": False,
    "spi": "/dev/spidev1.0", "reset_bcm": 25, "busy_bcm": 24,
    "dio1_bcm": 26, "dio1_used": False, "rf_switch": "DIO2",
    "tcxo_volts": 1.8, "tcxo_delay_ms": 10, "tcxo_delay_ticks": 640,
    "public_psk_b64": "AQ==",
}
PROFILES = {
    "longfast": dict(COMMON, profile_id="longfast", profile="EU_868/LONG_FAST",
                     frequency_hz=869_525_000, sf=11, bandwidth_hz=250_000,
                     channel_name="LongFast", channel_hash=0x08),
    "sfnarrow": dict(COMMON, profile_id="sfnarrow", profile="EU_868/SFNarrow",
                     frequency_hz=869_618_000, sf=7, bandwidth_hz=62_500,
                     channel_name="SFNarrow", channel_hash=0x20),
}
PROFILE = PROFILES["longfast"]  # compatibility for callers of the first probe
PUBLIC_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")  # AQ== alias 01
HEADER = struct.Struct("<IIIBBBB")
RX_DONE, HEADER_ERR, CRC_ERR, TIMEOUT = 0x02, 0x20, 0x40, 0x200
EVENTS = RX_DONE | HEADER_ERR | CRC_ERR | TIMEOUT
PORTS = set(range(1, 14)) | set(range(32, 39)) | set(range(64, 80)) | {112} | set(range(256, 512))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def get_profile(name):
    return dict(PROFILES[name])


def node_id(value):
    if not re.fullmatch(r"![0-9a-fA-F]{8}", value):
        raise argparse.ArgumentTypeError("expected ! followed by exactly 8 hex digits")
    if int(value[1:], 16) in (0, 0xFFFFFFFF):
        raise argparse.ArgumentTypeError("zero/broadcast is not a source node ID")
    return value.lower()


def decode_header(frame):
    if not HEADER.size < len(frame) <= 255:
        raise ValueError("expected 16-byte header and nonempty payload, at most 255 bytes")
    destination, source, packet_id, flags, channel, next_hop, relay = HEADER.unpack_from(frame)
    if source in (0, 0xFFFFFFFF):
        raise ValueError("invalid source node number (zero or broadcast)")
    return {
        "source_node_id": f"!{source:08x}", "destination_node_id": f"!{destination:08x}",
        "packet_id": f"0x{packet_id:08x}", "hop_limit": flags & 7, "hop_start": flags >> 5,
        "channel_hash": channel, "next_hop_hint": next_hop if flags >> 5 else None,
        "relay_hint": relay if flags >> 5 else None, "payload_bytes": len(frame) - HEADER.size,
        "identity_authenticated": False, "direct_transmitter_node_id": None,
        "direct_range_to_source_confirmed": False,
    }


def read_varint(data, offset):
    start, value = offset, 0
    for shift in range(0, 35, 7):
        if offset >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            if value > 0xFFFFFFFF or (offset - start > 1 and byte == 0):
                raise ValueError("noncanonical or overflowing protobuf uint32")
            return value, offset
    raise ValueError("overflowing protobuf varint")


def parse_data(data):
    """Strict supported subset of meshtastic.Data; unknown extensions stay unverified."""
    fields, offset = {}, 0
    schema = {1: 0, 2: 2, 3: 0, 4: 5, 5: 5, 6: 5, 7: 5, 8: 5, 9: 0, 10: 2}
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if field not in schema or schema[field] != wire or field in fields:
            raise ValueError("unsupported, duplicate or invalid Data protobuf field")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire == 2:
            size, offset = read_varint(data, offset)
            if size > len(data) - offset:
                raise ValueError("truncated Data protobuf bytes")
            value, offset = data[offset:offset + size], offset + size
        else:
            if offset + 4 > len(data):
                raise ValueError("truncated Data protobuf fixed32")
            value, offset = data[offset:offset + 4], offset + 4
        fields[field] = value
    if fields.get(1) not in PORTS or fields.get(3, 0) not in (0, 1):
        raise ValueError("missing/unsupported portnum or invalid boolean")
    if len(fields.get(2, b"")) > 237 or len(fields.get(10, b"")) not in (0, 64):
        raise ValueError("unsupported payload/signature size")
    if fields[1] != 5 and not fields.get(2):
        raise ValueError("empty application payload outside supported Routing subset")
    return {"portnum": fields[1], "payload_bytes": len(fields.get(2, b"")),
            "signature_present": bool(fields.get(10))}


def decode_public_data(frame, profile):
    decode_header(frame)
    _, source, packet_id, _, channel, _, _ = HEADER.unpack_from(frame)
    if channel != profile["channel_hash"]:
        raise ValueError("unknown/private/AEAD channel hash; public CTR decoder not applicable")
    # The header remains plaintext. This decrypts Data, never MeshPacket.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    nonce = struct.pack("<QII", packet_id, source, 0)
    decryptor = Cipher(algorithms.AES(PUBLIC_KEY), modes.CTR(nonce)).decryptor()
    data = decryptor.update(frame[16:]) + decryptor.finalize()
    return parse_data(data)


class PacketTracker:
    """Session-only observations; neither persistence nor game discovery/credit."""
    def __init__(self, profile, references=()):
        self.profile, self.references = dict(profile), set(references)
        self.nodes, self.packets, self.packet_ids, self.reference_matches = set(), set(), {}, set()
        self.frames = self.accepted_frames = self.duplicate_packets = 0

    def observe(self, frame, hardware=False, metadata=None):
        metadata = dict(metadata or {})
        self.frames += 1
        record = dict(metadata, event="rejected_frame", mode="radio" if hardware else "offline",
                      received_at_utc=metadata.get("received_at_utc", utc_now()),
                      profile_id=self.profile["profile_id"], raw_frame_hex=frame.hex(),
                      crc_status=metadata.get("crc_status", "not_checked_offline"),
                      new_node=False, duplicate_packet=False, packet_id_conflict=False,
                      identity_authenticated=False, reference_id_match=False,
                      source_node_id=None, packet_kind=None)
        try:
            record.update(decode_header(frame))
        except ValueError as exc:
            return dict(record, validation="rejected_header", reason=str(exc))
        record["reference_id_match"] = record["source_node_id"] in self.references
        if hardware and (record["crc_status"] != "pass" or metadata.get("header_error")
                         or metadata.get("crc_present") is not True
                         or metadata.get("irq", 0) & (HEADER_ERR | CRC_ERR | TIMEOUT)):
            return dict(record, validation="rejected_crc", reason="CRC/header absent, failed or uncheckable")
        try:
            data = decode_public_data(frame, self.profile)
        except ValueError as exc:
            return dict(record, event="meshtastic_candidate", validation="unverified_payload",
                        reason=str(exc))
        source = record["source_node_id"]
        identity = (source, record["destination_node_id"], record["packet_id"])
        digest = hashlib.sha256(frame[16:]).digest()
        key = identity + (record["channel_hash"], digest)
        duplicate = key in self.packets
        conflict = identity in self.packet_ids and digest not in self.packet_ids[identity]
        new = source not in self.nodes
        if len(self.packets) >= 100_000 and not duplicate:
            raise RuntimeError("session packet limit reached; start a new bounded capture")
        self.accepted_frames += 1
        self.duplicate_packets += int(duplicate)
        self.nodes.add(source)
        self.packets.add(key)
        self.packet_ids.setdefault(identity, set()).add(digest)
        if record["reference_id_match"]:
            self.reference_matches.add(source)
        return dict(record, event="meshtastic_data", validation="public_ctr_data",
                    portnum=data["portnum"], application_payload_bytes=data["payload_bytes"],
                    signature_present=data["signature_present"], signature_verified=False,
                    new_node=new, duplicate_packet=duplicate, packet_id_conflict=conflict,
                    packet_kind="new_node" if new else "duplicate_packet" if duplicate else "new_packet")

    def summary(self):
        return {"event": "summary", "frames": self.frames, "accepted_frames": self.accepted_frames,
                "unique_nodes": len(self.nodes), "unique_packets": len(self.packets),
                "duplicate_packets": self.duplicate_packets,
                "reference_matches": sorted(self.reference_matches),
                "reference_match_count": len(self.reference_matches), "dedup_scope": "session",
                "identity_authenticated": False,
                "result": "observed_public_data" if self.accepted_frames else
                          "inconclusive_no_packets" if not self.frames else "inconclusive_unverified_frames"}


def observation(frame, expected=None, hardware=False):
    """Compatibility helper; the tracking API accepts multiple map reference IDs."""
    return PacketTracker(PROFILE, [expected] if expected else []).observe(frame, hardware)


class SpiAudit:
    """Record configuration writes actually sent by LoRaRF, without changing its API."""
    def __init__(self, spi):
        object.__setattr__(self, "_spi", spi)
        object.__setattr__(self, "commands", {})

    def __getattr__(self, name):
        return getattr(self._spi, name)

    def __setattr__(self, name, value):
        setattr(self._spi, name, value)

    def xfer2(self, data):
        command = bytes(data)
        result = self._spi.xfer2(data)
        if command[0] in (0x86, 0x8B, 0x8C, 0x97, 0x9D):
            self.commands[command[0]] = command
        return result

    def report(self, profile):
        pll = int(profile["frequency_hz"] * 33554432 / 32000000)
        # SX126x SetModulationParams encodings used by LoRaRF 1.4.0.
        # Verify the selected RF values, not the two original EU presets.
        bw_code = {62_500: 3, 125_000: 4, 250_000: 5, 500_000: 6}[profile["bandwidth_hz"]]
        cr_code = profile["coding_rate_denominator"] - 4
        if not 1 <= cr_code <= 4:
            raise ValueError("unsupported LoRa coding rate")
        preamble = profile["preamble_symbols"]
        expected = {
            0x86: bytes([0x86]) + pll.to_bytes(4, "big"),
            0x8B: bytes([0x8B, profile["sf"], bw_code, cr_code, int(profile["ldro"]), 0, 0, 0, 0]),
            0x8C: bytes([0x8C, preamble >> 8, preamble & 255, 0, 255, 1, 0, 0, 0, 0]),
            0x97: bytes([0x97, 2, 0, 2, 0x80]), 0x9D: bytes([0x9D, 1]),
        }
        if any(self.commands.get(op) != command for op, command in expected.items()):
            raise RuntimeError("observed SPI configuration commands do not match selected profile")
        return {"applied_rf": dict(profile), "spi_commands": {f"0x{op:02x}": cmd.hex()
                for op, cmd in expected.items()}, "pll_frequency_hz": pll * 32000000 / 33554432,
                "verification": "SPI writes plus listed register readbacks; no RF measurement",
                "rf_measured": False}


def configure_radio(radio, profile=None):
    profile = profile or PROFILE
    radio.setDio2RfSwitch(True)
    radio.setDio3TcxoCtrl(radio.DIO3_OUTPUT_1_8, profile["tcxo_delay_ticks"])
    radio.setRxGain(radio.RX_GAIN_BOOSTED)
    radio.setFrequency(profile["frequency_hz"])
    radio.setLoRaModulation(profile["sf"], profile["bandwidth_hz"], profile["coding_rate_denominator"], profile["ldro"])
    radio.setLoRaPacket(radio.HEADER_EXPLICIT, profile["preamble_symbols"], 255, True, False)
    radio.setSyncWord(0x24B4)
    # Semtech errata: standard IQ needs bit 2 SET; LoRaRF 1.4.0 clears it.
    radio.writeRegister(0x0736, (radio.readRegister(0x0736, 1)[0] | 0x04,), 1)
    sync = bytes(radio.readRegister(0x0740, 2))
    iq = radio.readRegister(0x0736, 1)[0]
    if sync != b"\x24\xb4" or not iq & 4:
        raise RuntimeError("sync word or standard IQ register readback failed")
    radio.setBufferBaseAddress(0, 0)
    radio.setDioIrqParams(EVENTS, 0, 0, 0)
    arm_rx(radio)
    return {"sync_hex": sync.hex(), "iq_register": iq, "mode": "RX"}


def arm_rx(radio):
    radio.clearIrqStatus(radio.IRQ_ALL)
    radio.setRx(radio.RX_CONTINUOUS)
    if radio.getMode() != radio.STATUS_MODE_RX:
        raise RuntimeError("radio did not enter RX mode")


def poll_frame(radio):
    irq = radio.getIrqStatus()
    if not irq & EVENTS:
        return None
    received_at = utc_now()
    radio.setStandby(radio.STANDBY_RC)
    try:
        irq |= radio.getIrqStatus()
        result = {"event": "radio_event", "received_at_utc": received_at,
                  "irq": irq, "header_error": bool(irq & HEADER_ERR),
                  "crc_status": "failed" if irq & CRC_ERR else "not_checkable",
                  "raw_frame_hex": None, "rssi_dbm": None, "snr_db": None}
        # Retain CRC-failed RX_DONE bytes as evidence, never as a recognized node.
        if not irq & RX_DONE or irq & (HEADER_ERR | TIMEOUT):
            return dict(result, reason="no complete frame or header/timeout error")
        length, offset = radio.getRxBufferStatus()
        if not 1 <= length <= 255:
            return dict(result, reason="invalid radio length", length=length)
        frame = bytes(radio.readBuffer(offset, length))
        if len(frame) != length:
            raise RuntimeError("incomplete SPI frame read")
        rssi, snr, _ = radio.getPacketStatus()
        crc_present = bool(radio.readRegister(0x076B, 1)[0] & 0x10)
        result.update(raw_frame_hex=frame.hex(), rssi_dbm=-rssi / 2,
                      snr_db=(snr if snr < 128 else snr - 256) / 4,
                      crc_present=crc_present,
                      crc_status="failed" if irq & CRC_ERR else "pass" if crc_present else "absent")
        return result
    finally:
        arm_rx(radio)


def preflight():
    """Refuse conflicts; never stop apps/services, change power or free foreign pins."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("live mode requires Linux/uConsole and root for owner checks")
    if not Path(COMMON["spi"]).exists():
        raise RuntimeError("expected AIO v2 SPI device is missing")
    for proc in Path("/proc").glob("[0-9]*"):
        if proc.name == str(os.getpid()):
            continue
        try:
            args = (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except FileNotFoundError:
            continue
        if any(Path(a).name in ("meshtasticd", "rnsd", "RNode.py", "watchdogs-launcher")
               or a == "watchdogs" or "/WatchDogsGo/watchdogs/" in a for a in args):
            raise RuntimeError(f"another radio application is running (PID {proc.name})")
    if not shutil.which("fuser") or not shutil.which("pinctrl"):
        raise RuntimeError("fuser and pinctrl are required for owner/power checks")
    owners = subprocess.run(["fuser", COMMON["spi"]], capture_output=True, timeout=5)
    if owners.returncode != 1 or owners.stdout.strip() or owners.stderr.strip():
        raise RuntimeError("SPI device is in use or its ownership cannot be checked")
    power = subprocess.run(["pinctrl", "get", "16"], capture_output=True, text=True, timeout=5)
    if power.returncode or not re.search(r"\bhi\b", power.stdout):
        raise RuntimeError("AIO LoRa power GPIO16 is off or unreadable; no power changes made")


@contextmanager
def radio_lock():
    import fcntl
    fd = os.open("/run/lock/wdg-meshtastic-spi1.0.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class JsonlSink:
    def __init__(self, path=None):
        self.path, self.file = path, None

    def __enter__(self):
        if self.path:
            target = Path(self.path).expanduser().absolute()
            if (target.resolve().is_relative_to(Path(__file__).resolve().parent)
                    or target.resolve().is_relative_to(Path("/home/x/python/WatchDogsGo").resolve())):
                raise ValueError("store raw captures outside the source repository and active WDG installation")
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self.file = os.fdopen(fd, "w", encoding="utf-8")
        return self

    def emit(self, record):
        record = dict(record)
        record.setdefault("logged_at_utc", utc_now())
        line = json.dumps(record, sort_keys=True)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()
        print(line, flush=True)

    def __exit__(self, *args):
        if self.file:
            self.file.close()


def listen(profile, references, seconds, sink):
    if sys.platform != "linux":
        raise RuntimeError("live mode requires Linux/uConsole")
    with radio_lock():
        preflight()
        if version("LoRaRF") != "1.4.0":
            raise RuntimeError("this isolated adapter requires LoRaRF 1.4.0")
        # Check crypto dependency before touching SPI/GPIO; do not install anything.
        importlib.import_module("cryptography.hazmat.primitives.ciphers")
        module = importlib.import_module("LoRaRF.SX126x")
        original_spi = module.spi
        audit = SpiAudit(original_spi)
        module.spi = audit
        radio, tracker = module.SX126x(), PacketTracker(profile, references)
        initialized = begin_returned = interrupted = False
        old_term = signal.getsignal(signal.SIGTERM)
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
        try:
            detected = radio.begin(bus=1, cs=0, reset=25, busy=24, irq=-1)
            begin_returned = True
            if not detected:
                raise RuntimeError("SX1262 initialization failed")
            initialized = True
            readbacks = configure_radio(radio, profile)
            sink.emit(dict(audit.report(profile), event="rx_started", profile_id=profile["profile_id"],
                           readbacks=readbacks, compare_node_ids=list(references)))
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                metadata = poll_frame(radio)
                if metadata is not None:
                    if metadata["raw_frame_hex"] is None:
                        sink.emit(dict(metadata, profile_id=profile["profile_id"]))
                    else:
                        sink.emit(tracker.observe(bytes.fromhex(metadata["raw_frame_hex"]), True, metadata))
                time.sleep(0.01)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            try:
                if initialized:
                    radio.setStandby(radio.STANDBY_RC)
            finally:
                try:
                    original_spi.close()
                    if begin_returned:
                        module.gpio.cleanup((25, 24))
                finally:
                    module.spi = original_spi
                    signal.signal(signal.SIGTERM, old_term)
        sink.emit(dict(tracker.summary(), mode="radio", interrupted=interrupted,
                       profile_id=profile["profile_id"]))
        return 130 if interrupted else 0 if tracker.accepted_frames else 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--listen", action="store_true", help="explicitly acquire physical radio, RX only")
    mode.add_argument("--frame-hex", help="offline decode; never imports SPI/GPIO")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="longfast")
    parser.add_argument("--compare-node-id", "--expected-node-id", action="append", default=[], type=node_id,
                        help="optional historical map reference; repeatable, never proof of direct range")
    parser.add_argument("--seconds", type=int, default=60, help="bounded live window, 1..3600 seconds")
    parser.add_argument("--output", help="new JSONL file outside the source repository; required for live")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    if args.listen and not args.output:
        parser.error("--listen requires --output to a new local JSONL capture")
    profile = get_profile(args.profile)
    try:
        with JsonlSink(args.output) as sink:
            sink.emit(dict(profile, event="configuration", rf_state="not_applied", hardware_test_performed=False))
            if args.frame_hex is not None:
                tracker = PacketTracker(profile, args.compare_node_id)
                sink.emit(tracker.observe(bytes.fromhex(args.frame_hex)))
                sink.emit(dict(tracker.summary(), mode="offline", profile_id=args.profile))
                return 0
            if args.listen:
                return listen(profile, args.compare_node_id, args.seconds, sink)
            return 0
    except (OSError, RuntimeError, ValueError, IndexError, ImportError, subprocess.SubprocessError) as exc:
        print(json.dumps({"event": "error", "reason": str(exc)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"event": "interrupted", "hardware_test_complete": False}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
