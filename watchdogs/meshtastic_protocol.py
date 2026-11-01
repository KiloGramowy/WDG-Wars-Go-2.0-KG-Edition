"""Public Meshtastic codec used by the WDG integration.

The RF/header/legacy CTR validation is shared with the hardware-tested probe.
No callback generated here constitutes authentication or proof of direct range.
Sources: Meshtastic mesh.proto/User/Position/Data and RadioInterface.h (255B MTU).
"""

import hashlib
import math
import struct

from meshtastic_rx_probe import HEADER, PUBLIC_KEY, decode_header, get_profile, parse_data


MAX_TEXT_BYTES = 233  # Current official mesh.options Data.payload bound.
MAX_FRAME_BYTES = 255
DEFAULT_HOPS = 3


def _varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def fields(data):
    """Read bounded protobuf fields, retaining the payload and skipping extensions.

    Unknown fields are decoded generically, never interpreted as GPS/name/text.
    Duplicate singular fields and malformed wire encodings are rejected.
    """
    result, offset = {}, 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not number or number in result:
            raise ValueError("invalid/duplicate protobuf field")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise ValueError("truncated protobuf fixed field")
            value, offset = data[offset:offset + size], offset + size
        elif wire == 2:
            size, offset = _read_varint(data, offset)
            if size > len(data) - offset:
                raise ValueError("truncated protobuf bytes")
            value, offset = data[offset:offset + size], offset + size
        else:
            raise ValueError("unsupported protobuf wire type")
        result[number] = (wire, value)
    return result


def _read_varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            if value > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("overflowing protobuf varint")
            return value, offset
    raise ValueError("overflowing protobuf varint")


def _field(parsed, number, wire, default=None):
    if number not in parsed:
        return default
    actual_wire, value = parsed[number]
    if actual_wire != wire:
        raise ValueError("invalid protobuf field type")
    return value


def _display_text(data, maximum=None):
    if maximum is not None and len(data) > maximum:
        raise ValueError("oversized text field")
    text = data.decode("utf-8", errors="strict")
    return "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in text)


def crypt_payload(payload, source, packet_id):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    nonce = struct.pack("<QII", packet_id, source, 0)
    cipher = Cipher(algorithms.AES(PUBLIC_KEY), modes.CTR(nonce)).encryptor()
    return cipher.update(payload) + cipher.finalize()


def packet_key(frame):
    destination, source, packet_id, _, channel, _, _ = HEADER.unpack_from(frame)
    return (source, destination, packet_id, channel, hashlib.sha256(frame[16:]).digest())


def decode_packet(frame, profile_id="sfnarrow"):
    profile = get_profile(profile_id)
    header = decode_header(frame)
    destination, source, packet_id, _, channel, _, _ = HEADER.unpack_from(frame)
    if channel != profile["channel_hash"]:
        raise ValueError("unsupported/private/AEAD channel")
    data = crypt_payload(frame[16:], source, packet_id)
    validated = parse_data(data)
    parsed = fields(data)
    payload = _field(parsed, 2, 2, b"")
    if len(payload) > MAX_TEXT_BYTES:
        raise ValueError("application payload exceeds Meshtastic Data bound")
    result = dict(header, key=packet_key(frame), portnum=validated["portnum"],
                  payload=payload, name="", short_name="", lat=None, lon=None,
                  node_type=None, public_key=None, hw_model=None, text=None,
                  channel_name=profile["channel_name"])
    if validated["portnum"] == 1:
        result["text"] = _display_text(payload, MAX_TEXT_BYTES)
        # This integration exposes one public room, never private/direct messages.
        if destination != 0xFFFFFFFF:
            result["text"] = None
    elif validated["portnum"] == 4:
        user = fields(payload)
        claimed = _display_text(_field(user, 1, 2, b""), 16)
        if claimed and claimed.lower() != header["source_node_id"]:
            raise ValueError("NodeInfo id differs from header source")
        result["name"] = _display_text(_field(user, 2, 2, b""), 40)
        result["short_name"] = _display_text(_field(user, 3, 2, b""), 5)
        result["name"] = result["name"] or result["short_name"]
        result["hw_model"] = _field(user, 5, 0)
        role = _field(user, 7, 0, 0)
        result["node_type"] = "Router" if role in (2, 3, 4, 11) else "Sensor" if role == 6 else "Client"
        public_key = _field(user, 8, 2)
        if public_key is not None:
            if len(public_key) != 32:
                raise ValueError("invalid NodeInfo public key length")
            result["public_key"] = public_key
    elif validated["portnum"] == 3:
        position = fields(payload)
        latitude, longitude = _field(position, 1, 5), _field(position, 2, 5)
        if latitude is not None and longitude is not None:
            lat = struct.unpack("<i", latitude)[0] / 10_000_000
            lon = struct.unpack("<i", longitude)[0] / 10_000_000
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError("invalid position bounds")
            result.update(lat=lat, lon=lon)
    return result


def encode_text(text, source, packet_id, profile_id="sfnarrow", hops=DEFAULT_HOPS):
    if not 3 < source < 0xFFFFFFFF or not 0 < packet_id <= 0xFFFFFFFF:
        raise ValueError("invalid local node or packet number")
    if not 0 <= hops <= 7:
        raise ValueError("invalid hop limit")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("message is empty")
    payload = text.encode("utf-8")
    if len(payload) > MAX_TEXT_BYTES:
        raise ValueError(f"message exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    data = b"\x08\x01\x12" + _varint(len(payload)) + payload
    profile = get_profile(profile_id)
    # hop_limit and hop_start; no WANT_ACK, no VIA_MQTT, no next-hop request.
    header = HEADER.pack(0xFFFFFFFF, source, packet_id, hops | (hops << 5),
                         profile["channel_hash"], 0, source & 255)
    frame = header + crypt_payload(data, source, packet_id)
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("serialized packet exceeds radio MTU")
    return frame


def airtime_seconds(length, profile_id="sfnarrow"):
    """Semtech LoRa packet airtime, explicit header, CRC, profile preamble."""
    profile = get_profile(profile_id)
    sf, bw, cr = profile["sf"], profile["bandwidth_hz"], profile["coding_rate_denominator"]
    de = int(profile["ldro"])
    payload_symbols = 8 + max(math.ceil((8 * length - 4 * sf + 28 + 16) /
                                      (4 * (sf - 2 * de))) * cr, 0)
    return (profile["preamble_symbols"] + 4.25 + payload_symbols) * 2 ** sf / bw
