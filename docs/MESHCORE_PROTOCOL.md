# MeshCore Protocol — Legacy WDG Implementation Reference

> This is an inherited developer reference, not the current regional preset catalog. Its SF8 / CR4/5 / preamble-16 example is the preserved **WDG legacy** profile. For current selectable profiles and local GPS policy, see [RF profiles](RADIO_PROFILES.md).

Reference implementation: `watchdogs/lora_manager.py` from ESP32 Watch Dogs.
This document describes every critical detail needed to build a compatible MeshCore client.

---

## 1. Radio Configuration (CRITICAL)

All values must match EXACTLY or packets won't be heard/decoded by other nodes:

```
Frequency:    869_618_000 Hz  (869.618 MHz — WDG legacy CR5)
SF:           8
CR:           5               (coding rate 4/5)
BW:           62_500 Hz       (62.5 kHz)
Sync Word:    0x1424          (private LoRa sync word — NOT default 0x3444!)
Preamble:     16 symbols
Header:       Explicit (0x00)
Payload len:  255             (max, variable)
CRC:          True
```

**Common mistake #1**: Sync word `0x1424` is two bytes. The LoRa chip register format splits this into two registers. Verify with a logic analyzer or by reading back registers that it's actually set. If sync word is wrong, you'll never see any packets.

**Common mistake #2**: BW must be exactly 62500 — not 125000. This is a narrow-band preset.

---

## 2. Packet Structure (Wire Format)

Every MeshCore packet on the wire:

```
[header (1 byte)] [path_byte (1 byte)] [payload (variable)]
```

### Header byte encoding:

```
Bits 7-6: version (always 0)
Bits 5-2: payload_type (4 bits)
Bits 1-0: route_type (2 bits)
```

So: `header = (version << 6) | (payload_type << 2) | route_type`

### Payload types:

| Type | Value | Header byte (Flood) |
|------|-------|---------------------|
| Plain (peer) | 0x00 | 0x01 |
| Encrypted (peer) | 0x01 | 0x05 |
| Direct Message | 0x02 | 0x09 |
| ACK | 0x03 | 0x0D |
| Advertisement | 0x04 | 0x11 |
| Group Text | 0x05 | 0x15 |
| PathReturn | 0x08 | 0x21 |

### Route types:

| Type | Value | Has transport codes |
|------|-------|---------------------|
| Zero | 0 | Yes (4 bytes) |
| Flood | 1 | No |
| Direct | 2 | No |
| Addressed | 3 | Yes (4 bytes) |

**For TX, always use Flood (route_type=1)**. Simple and works.

### Path byte:

```
Bits 7-6: hash_size = (value >> 6) + 1  (typically 1, so bits = 0b00)
Bits 5-0: hash_count = number of hop hashes in path

For NEW packets from your node: path_byte = 0x00 (no hops yet)
```

**CRITICAL**: When building packets for TX, path_byte is always `0x00`. Repeaters add their hop hashes as they forward.

---

## 3. RX Parsing

When you receive a packet:

```python
data = raw_lora_bytes
header = data[0]
route_type = header & 0x03
payload_type = (header >> 2) & 0x0F

# Skip transport codes for route 0 or 3
offset = 1
if route_type in (0, 3):
    offset += 4

# Parse path
path_byte = data[offset]
hash_size = ((path_byte >> 6) & 0x03) + 1
hash_count = path_byte & 0x3F
offset += 1
offset += hash_count * hash_size  # skip path hashes

hops = hash_count
payload = data[offset:]
```

### Deduplication

Hash `data[0:1] + payload` (header + payload, skip path). Cache for 30 seconds. Drop duplicates.

---

## 4. Channel System & PSK

### Public channel (hardcoded):

```
PSK:          bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")  # 16 bytes
Channel hash: 0x11
```

### Hashtag channels (derived):

```python
digest = sha256("#tagname".encode("utf-8")).digest()
psk = digest[:16]          # AES key
channel_hash = digest[0]   # 1 byte
```

### Private channels:

```python
psk = bytes.fromhex(user_provided_hex)[:16]
channel_hash = sha256(psk).digest()[0]
```

---

## 5. Group Text (Type 0x05) — Channel Messages

### TX (sending):

```python
import struct, hmac, hashlib

# 1. Build plaintext
timestamp = int(time.time())  # Unix epoch, 4 bytes LE
flags = 0x00                  # text type=0
msg = f"{node_name}: {text}\x00".encode("utf-8")  # NULL terminated!
plaintext = struct.pack("<I", timestamp) + bytes([flags]) + msg

# 2. Pad to 16-byte boundary (zero padding)
pad_len = (16 - len(plaintext) % 16) % 16
plaintext_padded = plaintext + b"\x00" * pad_len

# 3. AES-128-ECB encrypt with channel PSK
ciphertext = aes_ecb_encrypt(channel.psk, plaintext_padded)

# 4. MAC: HMAC-SHA256(key=PSK, data=ciphertext), truncated to 2 bytes
mac = hmac.new(channel.psk, ciphertext, hashlib.sha256).digest()[:2]

# 5. Assemble payload
payload = bytes([channel.ch_hash]) + mac + ciphertext

# 6. Assemble packet
header = 0x15  # Flood + GroupText: (0<<6)|(0x05<<2)|0x01
packet = bytes([header]) + b"\x00" + payload
```

**CRITICAL: `msg` must be NULL-terminated!** (`\x00` at end). Without this, receivers may see garbage after the text.

**CRITICAL: The message format is `"{node_name}: {text}\x00"`** — colon+space between name and text. Other MeshCore clients parse the sender name from this.

### RX (receiving):

```python
channel_hash = payload[0]
mac = payload[1:3]        # 2 bytes
ciphertext = payload[3:]

# Find channel with matching hash
# Decrypt: AES-128-ECB with channel PSK
# Verify MAC: hmac.new(psk, ciphertext, sha256).digest()[:2] == mac
# Parse: timestamp(4 LE) + flags(1) + text (until \x00)
```

---

## 6. Advertisement (Type 0x04)

### TX (sending):

```python
import struct
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

privkey, pubkey = get_ed25519_keypair()  # 32-byte raw keys

timestamp = int(time.time())
ts_bytes = struct.pack("<I", timestamp)

# Appdata: flags + optional GPS + name
flags = 0x01      # type=1 (chat/client)
flags |= 0x80     # bit 7: has name

appdata = bytes([flags])

# Optional GPS (bit 4):
if has_gps:
    flags |= 0x10
    appdata = bytes([flags])
    appdata += struct.pack("<ii",
                           int(lat * 1_000_000),
                           int(lon * 1_000_000))

appdata += node_name.encode("utf-8") + b"\x00"

# Sign: Ed25519 signature over (pubkey + timestamp + appdata)
sign_data = pubkey + ts_bytes + appdata
signature = privkey.sign(sign_data)  # 64 bytes

# Payload: pubkey(32) + timestamp(4) + signature(64) + appdata
payload = pubkey + ts_bytes + signature + appdata

# Packet
header = 0x11  # (0<<6)|(0x04<<2)|0x01 = Flood Advert
packet = bytes([header]) + b"\x00" + payload
```

### Advert structure (100+ bytes):

```
Offset   Length   Field
0        32       Ed25519 public key
32       4        Timestamp (uint32 LE)
36       64       Ed25519 signature
100      1        Flags byte
101      0 or 8   GPS (if flag bit 4 set): lat(int32 LE) + lon(int32 LE), microdegrees
101/109  N+1      Name (UTF-8, null-terminated)
```

### Flags byte:

```
Bits 0-3: node type (0=none, 1=chat/client, 2=repeater, 3=room, 4=sensor)
Bit 4:    has GPS
Bit 5:    feature 1
Bit 6:    feature 2
Bit 7:    has name
```

**CRITICAL for Advert not landing in contacts**: The advert must be at least 100 bytes (32 pubkey + 4 timestamp + 64 signature). If payload is shorter, receivers will reject it. Verify that:
1. `pubkey` is exactly 32 bytes (raw Ed25519, NOT PEM/DER)
2. `signature` is exactly 64 bytes
3. `sign_data` is `pubkey + ts_bytes + appdata` (NOT just appdata!)
4. Name is null-terminated
5. Flags byte has bit 7 set (0x80) if name is present

**Why advert appears in logs but not contacts**: Other nodes see the packet and log it, but signature verification fails — so they don't trust it as a valid advertisement and don't add to contacts. Most likely cause: **wrong sign_data** (signing just `appdata` instead of `pubkey + timestamp + appdata`).

---

## 7. Identity (Ed25519 Keypair)

### Generation:

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

# Generate once, save to file
privkey = Ed25519PrivateKey.generate()
raw_private = privkey.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)
# Save raw_private (32 bytes) to file

# Load
privkey = Ed25519PrivateKey.from_private_bytes(raw_private)

# Get raw public key (32 bytes)
pubkey = privkey.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
```

**Key must be 32 bytes raw**, not PEM, not DER. Just raw bytes.

### Node name:

```python
node_name = f"WatchDogs_{pubkey[:4].hex()}"
# Example: WatchDogs_a3f1b2c0
```

Each device gets a unique name derived from its unique public key.

---

## 8. Direct Messages (Type 0x02, Encrypted)

### Key exchange: ECDH (Ed25519 → X25519)

```python
import nacl.bindings

# Convert Ed25519 keys to X25519 (Curve25519)
x_pub = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(dest_pubkey)
x_priv = nacl.bindings.crypto_sign_ed25519_sk_to_curve25519(
    priv_raw + our_pubkey)  # nacl expects 64-byte secret key!

shared = nacl.bindings.crypto_scalarmult(x_priv, x_pub)
# shared = raw 32 bytes, NOT hashed
```

**CRITICAL**: `pynacl` expects a 64-byte "secret key" = `private_raw(32) + public_raw(32)` concatenated.

### DM wire format:

```
[dest_hash(1)] [src_hash(1)] [MAC(2)] [ciphertext(N*16)]
```

Where:
- `dest_hash = dest_pubkey[0]` — first byte of raw Ed25519 public key (NOT sha256!)
- `src_hash = our_pubkey[0]` — same, first byte
- AES key = `shared_secret[:16]`
- HMAC key = full `shared_secret` (32 bytes)

### DM TX:

```python
dest_hash = dest_pubkey[0]
src_hash = our_pubkey[0]

# Plaintext: timestamp(4 LE) + flags(1) + "name: text"
# NOTE: no null terminator in DM text (unlike group text)
plaintext = struct.pack("<I", timestamp) + b"\x00" + msg_bytes

# AES-128-ECB encrypt
aes_key = shared[:16]
ciphertext = aes_ecb_encrypt(aes_key, zero_pad_16(plaintext))

# MAC
mac = hmac.new(shared, ciphertext, hashlib.sha256).digest()[:2]

payload = bytes([dest_hash, src_hash]) + mac + ciphertext
header = 0x09  # (0<<6)|(0x02<<2)|0x01
packet = bytes([header]) + b"\x00" + payload
```

### ACK (Type 0x03):

```python
# ACK hash = SHA256(plaintext[:5+text_len] + sender_pubkey)[:4]
frag = plaintext[:5 + text_len]
ack_hash = hashlib.sha256(frag + sender_pubkey).digest()[:4]

header = 0x0D  # (0<<6)|(0x03<<2)|0x01
packet = bytes([header]) + b"\x00" + ack_hash
```

---

## 9. TX Procedure (Radio)

### The problem with TX on SX1262/SX1276

After `beginPacket()`/`endPacket()`, the radio driver's internal state gets corrupted. You MUST do a nuclear reset after each TX:

```python
# 1. Build and send
lora.beginPacket()
lora.write(list(packet), len(packet))
lora.endPacket(timeout_ms)

# 2. Wait for TX_DONE (poll IRQ register)
while time.time() < deadline:
    irq = lora.getIrqStatus()
    if irq & IRQ_TX_DONE:
        break
    if irq & IRQ_TIMEOUT:
        break
    time.sleep(0.01)

# 3. NUCLEAR RESET — reconfigure EVERYTHING
lora.setStandby(STANDBY_RC)
time.sleep(0.01)
lora.clearIrqStatus(0x03FF)

# Reset internal state
lora._bufferIndex = 0
lora._payloadTxRx = 0
lora.setBufferBaseAddress(0x00, 0x00)

# Full reconfigure
lora.setFrequency(869_618_000)
lora.setLoRaModulation(8, 62_500, 5, False)
lora.setSyncWord(0x1424)
lora.setLoRaPacket(0x00, 16, 255, True)

# Re-arm RX
lora._irqSetup(IRQ_RX_DONE | IRQ_TIMEOUT | IRQ_HEADER_ERR | IRQ_CRC_ERR)
lora.setRx(RX_CONTINUOUS)
```

**This is the #1 reason TX doesn't work**: after transmission, the radio doesn't properly return to RX mode, or worse, frequency/sync word/BW gets corrupted. Other nodes never see your packet because the radio was misconfigured during TX.

### For T-Watch Ultra specifically

The T-Watch Ultra likely uses a different LoRa library than LoRaRF (maybe RadioLib, Arduino-LoRa, or SX126x driver directly). The key principles:

1. **After every TX, reconfigure ALL radio parameters** — don't assume they survived
2. **Verify TX_DONE via IRQ before proceeding** — don't just sleep
3. **Return to RX_CONTINUOUS after TX** — not RX_SINGLE
4. **Verify sync word is still 0x1424 after TX** — some drivers reset it

---

## 10. Debugging Checklist

### "I receive packets but can't send"

1. **Is TX_DONE IRQ firing?** If not, the packet never left the radio.
2. **After TX, is the radio back in RX?** Read the LoRa operating mode register.
3. **After TX, is sync word still 0x1424?** Read it back from registers.
4. **After TX, is frequency still 869.618 MHz?** Some drivers reset it.
5. **Is packet structure correct?** Compare TX hex dump with RX hex dump of a known-good packet from another node:
   - Group text: `15 00 11 XX XX [ciphertext...]` (header=0x15, path=0x00, ch_hash=0x11 for public)
   - Advert: `11 00 [32B pubkey] [4B ts] [64B sig] [flags] [name\x00]`
6. **Log your raw TX bytes** and compare with what other nodes see on RX.

### "Advert appears in logs but not in contacts"

1. **Signature verification fails on receiver side**. Sign data MUST be `pubkey(32) + timestamp(4) + appdata` — not just appdata, not including signature.
2. **Pubkey format**: must be raw 32 bytes, not wrapped in ASN.1/DER.
3. **Timestamp**: must be valid Unix epoch (uint32 LE). Don't use milliseconds.
4. **Flags byte**: bit 7 (0x80) must be set if name follows.
5. **Appdata byte order**: flags first, then optional GPS, then name.

### "Messages sent from watch don't reach other nodes"

1. **Channel hash mismatch**: public channel hash is exactly `0x11`. Verify `payload[0]` after header+path.
2. **MAC mismatch**: the receiver computes `hmac.new(PSK, ciphertext, sha256).digest()[:2]` and compares with your MAC bytes. If PSK is wrong, or you HMAC over `plaintext` instead of `ciphertext`, MAC fails silently.
3. **Encryption mismatch**: AES-128-ECB, key = channel PSK (16 bytes). Not CBC, not CTR, not GCM. ECB.
4. **Null terminator in message**: `"name: text\x00"` — the `\x00` is INSIDE the plaintext before encryption.
5. **Padding**: zero-pad plaintext to 16-byte multiple BEFORE encryption.

### Quick TX verification test

Send a known message and capture it on another node. Compare byte-by-byte:

```python
# On watch: send "test" on public channel
# Expected packet structure:
# [0x15] [0x00] [0x11] [mac0] [mac1] [ciphertext...]
#  ^hdr   ^path  ^ch_hash ^2B MAC    ^AES-ECB encrypted

# Verify: decrypt ciphertext with PSK, you should get:
# [4B timestamp LE] [0x00] [b"NodeName: test\x00"] [zero padding]
```

---

## 11. Summary of Critical Constants

```python
FREQ          = 869_618_000    # Hz
SF            = 8
CR            = 5
BW            = 62_500         # Hz
SYNC_WORD     = 0x1424
PREAMBLE      = 16
PUBLIC_PSK    = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
PUBLIC_HASH   = 0x11
HEADER_GRPTXT = 0x15           # Flood + GroupText
HEADER_ADVERT = 0x11           # Flood + Advertisement
HEADER_DM     = 0x09           # Flood + DM
HEADER_ACK    = 0x0D           # Flood + ACK
```

---

## 12. Dependencies

```
cryptography    — Ed25519 keygen, AES-128-ECB
pynacl          — Ed25519↔X25519 conversion for DM ECDH (only needed for DMs)
```

---

## 13. Typical Bugs on T-Watch Ultra (ESP32-based)

1. **RadioLib vs Arduino-LoRa**: sync word register format differs between libraries. RadioLib uses raw 2-byte value, Arduino-LoRa may expect single byte. For SX1262, sync word 0x1424 means register 0x0740=0x14, 0x0741=0x24.

2. **ESP32 endianness**: ESP32 is little-endian, same as protocol. `struct.pack("<I", ...)` in Python = direct `uint32_t` write in C on ESP32. No byte swapping needed.

3. **LoRa TX power**: set to +14 dBm or higher. If TX power is too low, nearby nodes may not hear you even though you hear them (asymmetric link).

4. **DIO1 IRQ handling on ESP32**: make sure DIO1 is properly connected and IRQ handler fires for TX_DONE. Without TX_DONE confirmation, you don't know if the packet was actually transmitted.

5. **SPI bus contention**: if display and LoRa share SPI bus, ensure proper mutex/CS handling during TX. A corrupted TX frame will be silently dropped by all receivers.
