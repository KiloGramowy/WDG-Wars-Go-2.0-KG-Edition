"""RNode-compatible RNS Interface for the uConsole SX1262 (WDGWars Phase 2).

Lets Reticulum reach the wdgwars LoRa gateway (an RNode, Heltec V3) directly
over the air from our raw SPI SX1262 — no RNode firmware, no extra board. The
gateway runs stock RNode firmware, so the on-air format is NOT a raw RNS packet:
RNode prepends a 1-byte header and splits payloads >254 B across two physical
frames. This module reproduces that framing byte-for-byte (spec verified against
the live gateway, fw 1.86 — see reference_rnode_phy_spec).

Design: the RNS contract + framing here are radio-agnostic and unit-testable.
The actual SX1262 I/O is injected via a `radio_send` callable and fed back in
via `feed_rx`, so the fragile hardware layer (LoRaManager) stays separate and
the framing — the part the gateway's split echo test exercises — is provable
without a radio.
"""

from __future__ import annotations

import logging
import math
import threading

log = logging.getLogger("rns_lora")

try:
    import RNS  # type: ignore
    from RNS.Interfaces.Interface import Interface as _BaseInterface  # type: ignore
    _RNS_OK = True
except Exception:  # pragma: no cover - import guard
    RNS = None  # type: ignore
    _BaseInterface = object  # type: ignore
    _RNS_OK = False

# ── RNode PHY constants (verified vs wdgwars gateway, fw 1.86) ──
FREQUENCY = 867_200_000
BANDWIDTH = 125_000
SPREADING = 8
CODING_RATE = 5            # 4/5
TX_POWER = 14              # dBm, EU868 1% duty
PREAMBLE = 18
SYNC_WORD = 0x1424         # on-air bytes 0x14 (MSB), 0x24 (LSB) — private
REG_IQ_POLARITY = 0x0736   # errata: standard IQ -> set bit2 (RNode/Semtech)

# ── RNode over-air framing (Config.h MTU=508 / SINGLE_MTU=255 / HEADER_L=1) ──
HW_MTU = 508
SINGLE_MTU = 255
HEADER_L = 1
FLAG_SPLIT = 0x01
MAX_FRAME_PAYLOAD = SINGLE_MTU - HEADER_L  # 254 B per physical LoRa frame


def rnode_bitrate() -> float:
    """On-air bitrate (matches RNodeInterface.updateBitrate)."""
    return SPREADING * ((4.0 / CODING_RATE) /
                        (math.pow(2, SPREADING) / (BANDWIDTH / 1000.0))) * 1000


def frame_outgoing(payload: bytes, seq: int) -> list[bytes]:
    """RNode over-air framing.

    Returns the list of physical LoRa frames (each <=255 B) for one RNS packet:
    - payload <=254 B  -> one frame  [seq<<4][payload]
    - payload 255-508  -> two frames, BOTH with the same header (same seq +
      FLAG_SPLIT): [hdr][254 B] then [hdr][rest]
    """
    header = (seq << 4) & 0xF0
    if len(payload) <= MAX_FRAME_PAYLOAD:
        return [bytes([header]) + payload]
    header |= FLAG_SPLIT
    return [
        bytes([header]) + payload[:MAX_FRAME_PAYLOAD],
        bytes([header]) + payload[MAX_FRAME_PAYLOAD:],
    ]


class SplitReassembler:
    """Reassemble RNode split frames by the sequence in the header's top nibble.

    Mirrors the firmware RX path: strip 1-byte header; non-split -> deliver as-is;
    split with a new seq -> buffer; split with the buffered seq -> concat+deliver.
    """

    def __init__(self) -> None:
        self._buf: bytes | None = None
        self._seq: int | None = None

    def reset(self) -> None:
        self._buf = None
        self._seq = None

    def feed(self, frame: bytes) -> bytes | None:
        """Return a complete RNS payload, or None while awaiting the 2nd frame."""
        if not frame:
            return None
        header = frame[0]
        seq = header >> 4
        split = header & FLAG_SPLIT
        payload = frame[1:]
        if not split:
            self.reset()
            return payload
        if self._buf is not None and self._seq == seq:
            full = self._buf + payload
            self.reset()
            return full
        # New split sequence — start buffering the first frame.
        self._buf = payload
        self._seq = seq
        return None


class RNodeCompatInterface(_BaseInterface):
    """Thin RNS interface: frames/splits outgoing packets and reassembles RX.

    Radio I/O is injected — `radio_send(frame_bytes)` transmits one physical
    LoRa frame; the radio layer calls `feed_rx(frame_bytes)` for each received
    physical frame.
    """

    def __init__(self, radio_send, owner=None, name="WDGWarsLoRa") -> None:
        if _RNS_OK:
            super().__init__()
        self._radio_send = radio_send
        self.owner = owner
        self.name = name
        self.IN = True
        self.OUT = True
        self.HW_MTU = HW_MTU
        self.bitrate = rnode_bitrate()
        # RNS normally sets these when loading an interface from config; a
        # programmatically-created interface must set them itself or RNS crashes
        # while processing inbound announces (e.g. 'no attribute
        # announce_rate_target'). No IFAC on the WDGWars LoRa segment.
        if _RNS_OK:
            self.mode = self.MODE_FULL
            self.announce_cap = RNS.Reticulum.ANNOUNCE_CAP / 100.0
            self.announce_rate_target = None
            self.announce_rate_grace = None
            self.announce_rate_penalty = None
            self.ifac_size = 0
            self.ifac_netname = None
            self.ifac_netkey = None
            self.ifac_key = None
            self.ifac_identity = None
            self.ifac_signature = None
        self.online = False
        self._seq = 0
        self._reasm = SplitReassembler()
        self._tx_lock = threading.Lock()
        self.rxb = getattr(self, "rxb", 0)
        self.txb = getattr(self, "txb", 0)

    # --- lifecycle ---
    def activate(self) -> None:
        """Mark online and register with RNS Transport."""
        self.online = True
        if _RNS_OK and self not in RNS.Transport.interfaces:
            RNS.Transport.interfaces.append(self)
        log.info("%s activated (%.0f bps, MTU %d)", self, self.bitrate, self.HW_MTU)

    def detach(self) -> None:
        self.online = False
        try:
            if _RNS_OK and self in RNS.Transport.interfaces:
                RNS.Transport.interfaces.remove(self)
        except Exception:
            pass

    # --- TX (called by RNS Transport) ---
    def process_outgoing(self, data: bytes) -> None:
        if not self.online:
            return
        with self._tx_lock:
            seq = self._seq & 0x0F
            self._seq += 1
            for frame in frame_outgoing(data, seq):
                self._radio_send(frame)
            self.txb += len(data)

    # --- RX (called by the radio layer per physical frame) ---
    def feed_rx(self, frame: bytes) -> None:
        payload = self._reasm.feed(frame)
        if payload:
            self.process_incoming(payload)

    def process_incoming(self, data: bytes) -> None:
        self.rxb += len(data)
        if len(data) >= 18:
            log.info("RXHASH f=%02x dest=%s", data[0], data[2:18].hex())
        if self.owner is not None and hasattr(self.owner, "inbound"):
            self.owner.inbound(data, self)
        elif _RNS_OK:
            RNS.Transport.inbound(data, self)

    def __str__(self) -> str:
        return f"RNodeCompatInterface[{self.name}]"
