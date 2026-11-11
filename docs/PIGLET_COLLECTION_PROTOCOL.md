# Piglet Collection protocol 1

This is a local, authenticated, resumable transfer of closed Piglet KG CSV files.
An acknowledgement means a durable game import, not an upload to WDGWars or WiGLE.
After a verified acknowledgement, the Piglet hands that CSV to its existing
`/uploaded` archive and existing configured retention. Before acknowledgement,
the source must remain available for retry. Collection is a delivery destination,
not evidence that either external service received anything.

## Network and pairing

The collector listens only on its owned AP IPv4 address, default `10.72.0.1:8765`.
It must not bind to `0.0.0.0`, the router-facing STA address, or a public interface.
The AP uses WPA2-PSK. The pairing screen and Piglet WebUI use matching field names:
**UConsole hotspot SSID**, **UConsole hotspot password**, **Collector IPv4 address**,
**Collector port** and **Collection pairing token**. The local pairing token is
separate from both external upload-service credentials. Every request requires
`Authorization: Bearer <pairing-token>`; the token is a persistent random secret.
Credentials are configured locally, never embedded in firmware or source.

The target is concurrent STA and virtual AP on the existing uConsole Wi-Fi PHY.
The AP must use the STA channel. No fallback may disconnect STA or switch to a
different adapter without the owner's decision. Channel change or STA loss stops
the owned AP/receiver and preserves unfinished files. Creating the virtual AP and
NetworkManager connection occurs only on explicit receiver START after preflight.

The collector ID is a persistent random 32-character lowercase hexadecimal value.
A device ID is a stable 12–64-character identifier, restricted to letters, digits,
`_` and `-`. A file ID is its 64-character lowercase SHA256 over exact raw bytes.
Names are display-only basenames, not filesystem paths. File size is an unsigned
integer, at most 2 GiB in protocol 1. Default maximum chunk is 16,384 bytes.

### Receiver readiness and recent peer activity

**Hotspot: ACTIVE** identifies the running local AP/receiver. The separate peer
label **ACTIVE PIGLET KG** requires a successful authenticated Collection request
within the last **30 seconds**. With the receiver running but no recent qualifying
request, show **WAITING FOR PIGLET KG**; when the receiver is inactive, show
**NOT CONNECTED**. A lost peer ages out within 30 seconds. Pending CSV import work
must never refresh or preserve the ACTIVE peer label.

This is a runtime activity registry based on existing requests. It introduces no
new wire messages, heartbeat command or player-database write, and does not assert
that a TCP connection or radio association is still alive.

## Requests

All JSON uses UTF-8. JSON request bodies are bounded to 16 KiB. HTTP redirects are
not followed. Requests after begin carry `X-Piglet-Session: <session-id>`.
Unknown IDs, invalid state, bad digest or bad offset never acknowledge a file.

### GET /pc/v1/hello

Reply 200: `{"protocol":1,"collector_id":"<32hex>","max_chunk":16384,"source_items":1}`.
`source_items:1` advertises the optional source-path accounting extension below.
It does not change protocol 1 or the PC1 receipt signature.

### POST /pc/v1/begin

Body: `{"device_id":"<id>","pending_files":3}`.
Reply: `{"protocol":1,"collector_id":"<32hex>","session_id":"<32hex>"}`.
This is a connection/batch session, not a game loot session.

After a supporting hello, the sender adds `"source_items":1` to begin. The
collector echoes `"source_items":1`; without that echo the sender retains its
files and does not use the extension. `pending_files` then counts the announced
source paths, including content-identical aliases and cached-ACK items in that
inventory. Each must be offered in the new session.

### POST /pc/v1/file

Body: `{"sha256":"<64hex>","size":12345,"name":"scan.csv"}`.
Reply includes `sha256`, `size`, `offset`, and `state`:
`receiving`, `processing`, `committed` or `error`. A committed file additionally
returns its durable `receipt`. A previously committed digest must return that
receipt without importing or awarding XP again, even after either device restarts.

In negotiated source-item mode, add `"source_id":"<64hex>"`: SHA256 of the exact
UTF-8 canonical source path, such as `/logs/scan.csv` or `/uploaded/scan.csv`.
The path itself is not sent or interpreted as a collector filesystem path.
Bind this ID immutably to its name, digest and size; retries are idempotent.
Two paths with identical digest and basename count as two source items but
import the content only once. Offer every announced ID even when its receipt is
already cached; a valid cached receipt does not substitute for an offer.
Legacy sessions remain supported. An ambiguous legacy inventory cannot be
reported as complete when its expected path count exceeds its distinct offers.

### PUT /pc/v1/chunk?id=<sha256>&offset=<decimal>

Body: raw bytes; `Content-Length` must be at most `max_chunk`.
Required header: `X-Chunk-SHA256: <64hex>` over this chunk.
The offset returned in JSON is durable: bytes and metadata are flushed before it
is acknowledged. Retrying an already stored identical range is idempotent.
A conflicting/out-of-order range returns 409 with the expected durable `offset`.
After a crash, any tail beyond the last committed offset is discarded before
resumption. No whole-file retry is required for an interrupted transfer.

### POST /pc/v1/commit

Body: `{"sha256":"<64hex>"}`.
Verify exact size and whole-file SHA256, then validate the complete CSV before
starting import. Reply 202 with `state:"processing"`, or 200 with a committed
receipt if the import has already completed. A successful HTTP write alone is
never a final acknowledgement.

### GET /pc/v1/status?id=<sha256>

Reply includes `state`, `offset`, `size`, `sha256`, and progress counters
`records`, `wifi`, `ble`, `duplicates`. A committed response contains `receipt`.
A parsing/import/storage error returns a clear error without a committed receipt.
Partial data and import recovery state remain available for a later session.

### POST /pc/v1/finish

Body: `{}`. Declare that this device has finished its current batch. The reply is
flushed before the collector schedules AP/service shutdown. Do not stop a second
active transfer or discard unfinished staging. STOP COLLECTION releases only owned
network resources and preserves source or unconfirmed raw data.

When active or unfinished Collection work exists, **Esc** opens a **Yes / No**
cancellation question with **No selected by default**. No keeps the operation
running. Yes stops the receiver and import worker and cancels unfinished, unpaid
aggregation. Completed file imports and saved rewards remain durable; raw transfer
data is retained for a safe later resume. Cancellation is not a successful file
receipt. With no active work, Esc returns normally.

## Final receipt

Fields: `protocol` (1), `collector_id`, `sha256`, `size`, `import_id`, `records`,
`wifi`, `ble`, `duplicates`, and `signature` (lowercase HMAC-SHA256).
All counts are nonnegative integers. `records` is the number of source data rows;
`wifi + ble + duplicates == records`. `import_id` is lowercase hexadecimal.

The HMAC input is the following ASCII string, including the final newline:

```text
PC1\n{collector_id}\n{sha256}\n{size}\n{import_id}\n{records}\n{wifi}\n{ble}\n{duplicates}\n
```

Here `\n` denotes one LF byte. Integers use ordinary decimal without leading zeros.
The key is the exact UTF-8 pairing-token text, not its hex-decoded representation.
The Piglet verifies protocol, current collector ID, digest, size, count relation
and HMAC before persisting its separate Collection acknowledgement ledger.
Only after validating the receipt and persisting its local delivery ledger may
the Piglet call its existing `moveToUploaded()` path. If a restart interrupts this
handover, retry the move using the saved receipt without importing or scoring
again. Do not set an external WDGWars or WiGLE confirmation.

## CSV and import

Expected Piglet KG header (14 columns):

```text
MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type
```

The first line identifies WiGLE 1.6. Support quoted commas, quotes, CR and LF in
names with a real CSV parser. FirstSeen is source UTC, including a valid 1970
placeholder when the Piglet had no time. Never substitute the collector clock.
Historically well-shaped but impossible calendar dates remain exact source text
with an explicitly unknown capture timestamp. Non-UTF-8 radio names retain their
raw bytes and reversible display escapes; other fields remain strictly validated.
Coordinates `0,0` denote missing GPS in the source implementation. Never use the
uConsole GPS as fallback. Preserve the source value of AccuracyMeters even though
this firmware currently stores HDOP there; do not invent a conversion.

Retain the raw 14-column file unchanged and retain additional source metadata.
Generate compatible native 11-column exports for existing history/manual upload;
BLE needs native `[BLE]` AuthMode while its original source AuthMode remains in
the raw record. New import-generated exports must identify their CSV dialect so
legacy physical-line parsers can keep their existing fallback behavior. Newly
completed CSV exports contain the full source observations, including rows whose
rewards were already saved during a previous partial import. This does not rewrite
previously committed exports retroactively.

The exact file digest and canonical complete source-record identity are persistent.
Exact retransmissions/duplicate records award zero additional XP. Distinct real
observations use the existing game awards (Wi-Fi new 15/repeat 1; BLE new 10/repeat
1). The canonical game XP remains in loot_db.json; any SQLite index is only
transfer/import metadata. A background worker validates and aggregates a whole CSV
before its remaining XP and newly known identities are saved, with recovery for a
crash between metadata, exports and the game DB. An unfinished aggregation cannot
award new partial-file XP. Paid records from the earlier batch-based path remain
paid and are recognised when completing the remaining work.

Collection is a pure file import: **no per-record map markers, terminal output or
visual replay**. The game reads aggregate progress and completed-file summaries.
Persist whole-file accounting and recovery before issuing a receipt; the UI must
not be required to replay each observation to finish the import. Existing recovery
state from a previously partial import is retained without repeating its saved
rewards or its presentation effects. Withhold the final receipt until the complete
native export, remaining rewards and required durable accounting are confirmed.

The Collection screen displays **DOWNLOADING CSV: n%** for the current file's
received bytes and **IMPORT CSV: n%** for the full Collection job, without a
graphical bar. Import is file-weighted processing progress, not transfer-byte
percentage: each announced source is one unit, and its validated processing
determines its fraction. A progress fraction is not an XP commit. Unoffered items count
as zero. During reconnect, unknown overlap with previously offered sources is
counted conservatively until offers identify it. The figure can adjust when a
device joins or a resumed inventory identifies overlap. **100%** requires every
announced source, full exports, completed-file rewards and required durable
acknowledgements.
Display new Wi-Fi/BLE identities, skipped exact duplicates, awarded XP and status.
Distinct real observations of known identities retain existing repeat-XP rules.

Raw data and completed accounting survive STOP/restart/retry and group participating
devices. Confirmed cancellation ends unfinished, unpaid aggregation; resumption
revalidates retained data without paying completed files or prior paid records again.
After successful finish of all participating sessions, a later begin starts a
new job. A file's first-import credit remains with its original job; offering an
older committed digest cannot inflate the new job's new-device or XP summary.
Index metadata is additive; it does not rewrite player history or raw CSV.

Completed import sessions become ordinary pending manual uploads. Do not call an
external upload API, create upload receipts, or change uploaded-log retention.

## Piglet lifecycle

Normal Piglet follows its existing boot, scanning and WebUI paths unchanged and
does not incur Collection discovery delay. UConsole Piglet makes one at-most
10-second automatic association/collector discovery attempt at boot, before
opening the new scan log. Hashing/transmission starts only after a valid collector
is found; transfer itself can take longer. Missing hotspot, failed pairing or
unavailable receiver ends this attempt without waiting indefinitely.

After Collection (success or failure), release its STA connection, then execute
the original boot sequence: home Wi-Fi, fallback AP at `192.168.4.1`, a full
60-second AP budget, and the existing rolling five-minute **Stay** extension.
For standalone UConsole Piglet, this same full WebUI deadline also applies when
home STA connects. When the deadline ends, disconnect STA and enter the existing
scan flow, even with autoStartAfterUpload=false. Deliberate pauses, SD errors and
saved ESP-NOW Core/Node boot behavior retain their existing rules. Normal Piglet
retains its original startup, autoStartAfterUpload and upload behavior. No new
PAUSE gesture, WebUI activation method or concurrent Piglet AP/Collection is added.

UConsole mode refuses direct external CSV uploads before connection/send,
including startup and manual WebUI routes. Existing external-service credentials
remain stored for Normal mode. A missing collector does not permit an external
upload or an automatic mode switch.

Transfer closed CSV from both `/logs` and `/uploaded`, excluding the current log,
startup GPS pending data and non-observation files. The first UConsole-mode
migration includes the entire available historical CSV archive in both folders.
Persist its inventory/progress; do not mark migration complete with missing ACKs.
Later boots handle new or still-unconfirmed content, not repeated successful
imports. A previous external upload or presence in `/uploaded` is not a
Collection receipt.

In UConsole mode, pending files are protected from the existing automatic deletion
paths until their exact content has a valid Collection ACK. Once acknowledged,
`/logs` content uses the existing archive handover; content already in `/uploaded`
does not move again. Existing configured retention then applies, without a second
cleanup system or changing its settings. Normal-mode retention/upload behavior
remains unchanged. Hash and transmit with bounded buffers, regular GPS draining
and yield. Secure the available source archive and configuration before migration.
Individual WebUI CSV downloads are not a complete SD image; record anything the
running firmware does not expose rather than claiming a complete backup.
Preserve all original scan schedules, GPS/cache/backfill and deduplication rules.
Resume network failures from the receiver's durable offset with bounded retry;
on failure retain data and continue into the original boot sequence. Do not follow
redirects or turn a Collection ACK into a WDGWars/WiGLE upload marker. No new
WiGLE integration, automatic external uploader or retention policy is introduced
on uConsole by Collection. Imports remain in the existing manual upload queue.

## Compatibility and validation limits

This release is validated with **one physical Piglet**. The present device-ID
construction is not verified as collision-safe across several C5 Piglets;
multiple physical Piglets remain unsupported pending correction and independent
verification. The protocol's device/session fields do not themselves establish
multi-device firmware compatibility.

The [installation guide](PIGLET_COLLECTION_INSTALL.md#-12-what-was-actually-verified)
distinguishes compiled/mocked tests, physical delivery, no-collector handover and
unverified fresh GPS/full-SD/power-loss/throughput cases. Software peer-label tests
are not a fresh physical association/expiry measurement. See the
[canonical release notes](RELEASE_0.9.20.md) for the public candidate results.
