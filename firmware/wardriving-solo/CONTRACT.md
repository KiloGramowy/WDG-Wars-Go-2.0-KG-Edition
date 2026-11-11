# JanOS Wardriving SOLO — UART contract v1

This opt-in extension targets projectZero `ef35e9bbed28c441f53abdb6e634cf46428f912b`.
Legacy commands, Wardrive 2.0, SD exports and GPS remain unchanged. SOLO emits
real raw observations without requiring GPS or an SD card. WDG on the host owns
GPS quality/cache, the durable pending queue and startup backfill; firmware does
not assign GPS.

## Commands

Commands use the existing 115200-baud console and CRLF termination.

* `wardrive_solo_caps`
* `wardrive_solo <session> <full|kg> <wifi> <ble>`
* `stop` — the existing universal STOP command

`session` is exactly 16 lowercase hexadecimal characters chosen by the host.
`wifi` and `ble` are each `0` or `1`; at least one must be `1`.
Change profile or enabled protocols through confirmed STOP, then a new start.
Do not retry a start while a previous start/STOP is unresolved.

Capability probing is permitted only while other radio operations are idle.
It temporarily validates tuning with RX disabled, checks channel readback,
restores prior band/channel/promiscuous state, and never reports scanning ON.
Busy/failed probing returns an error; do not infer support from a version string.
The cold probe creates a default event loop only when necessary and removes only
its own loop after driver deinitialization. Existing loops and JanOS bookkeeping
are preserved. Associated STA, ARP helpers and other active radio owners reject
probing/start before tuning. `capability_failed` means the prior RF state was
restored; `restore_failed` means ownership is unresolved even without a session.
After `restore_failed`, send universal STOP and wait for its final ACK. Firmware
retains probe resources and legacy mode gates until STOP verifies their release;
it never deletes an owned loop while the driver remains alive.

## Framing and identity

Only a complete line beginning `@SOLO ` is part of this contract. The remainder
is one JSON object. Ignore other legacy console output. Maximum line length is
4096 bytes including the prefix and excluding CRLF.

All events contain `v: 1`, `event`, `boot_id` (16 lowercase hex characters),
and `emitted_monotonic_us` (nonnegative integer from `esp_timer_get_time()`).
All session events also contain `session`. A new boot creates a new `boot_id`.
An accepted start creates an empty, monotonically increasing sequence of
observations: `seq` starts at 1. Identity is `(boot_id, session, seq)`.
Observation `capture_monotonic_us` is recorded at radio reception, not at UART
flush. STOP drains the accepted queue before `stopped`; no later observation
from that session may follow it. Dropped observations are reported explicitly.

`clock_monotonic_us` in caps/state anchors the same clock used by observations.
The host records request/response timing and its UTC/monotonic clock pair, keeps
uncertainty metadata, and retains source capture time. This is not GPS UTC or
proof of an exact wall-clock measurement. Never label a cache position as a fix.

## Capability event

```json
{"v":1,"event":"caps","boot_id":"0123456789abcdef","emitted_monotonic_us":1000000,"clock_monotonic_us":1000000,"supported":true,"contract":1,"firmware":"1.7.0","sdk":"v6.0.1","max_line_bytes":4096,"channels":[1,6,11,36,40,44,48],"skipped":[{"channel":177,"reason":"driver_rejected"}],"country":{"code":"PH","policy":0,"first":1,"count":14,"mask5":0}}
```

`channels` contains candidates accepted by the running driver with matching
channel/band readback under its current configuration. This proves tuning, not
RF reception or permission to transmit. Empty/failed capability is unsupported.
Country mask zero means SDK regional defaults, not every 5-GHz channel.
The firmware does not modify the country configuration to broaden coverage.
An unavailable KG channel makes KG start fail; never silently shorten KG.
Start emits another caps event after the real JanOS Wi-Fi initialization, before
the confirmed state. This supersedes the idle probe if SDK initialization changed
country/channel availability. While a start is pending, update capabilities and
clock only; do not send another start command in response to this caps event.
This actual-start caps event is mandatory. An earlier idle/preflight caps event
does not authorize ON: the host must receive fresh actual-start caps before the
first state ACK. Country and accepted channels need not equal the idle snapshot.

## Confirmed state event

```json
{"v":1,"event":"state","boot_id":"0123456789abcdef","session":"fedcba9876543210","emitted_monotonic_us":1100000,"clock_monotonic_us":1100000,"profile":"kg","wifi":true,"ble":true,"phase":"wifi","cycles":0}
```

Emit only after actual first-phase initialization or a successful phase change.
`wifi`/`ble` are the accepted session's enabled protocols, not simultaneous RF
owners; `phase` is `wifi`, `ble`, or `pause` during the approved KG cycle gap.
`pause` keeps the confirmed session enabled while neither receiver is active.
No state event
may follow a failed initialization as though scanning succeeded.

## Raw observation events

```json
{"v":1,"event":"observation","boot_id":"0123456789abcdef","session":"fedcba9876543210","seq":1,"capture_monotonic_us":1200000,"emitted_monotonic_us":1201000,"protocol":"wifi","mac":"AA:BB:CC:DD:EE:FF","ssid":"example","auth":"WPA2_PSK","channel":6,"rssi":-70}
{"v":1,"event":"observation","boot_id":"0123456789abcdef","session":"fedcba9876543210","seq":2,"capture_monotonic_us":1202000,"emitted_monotonic_us":1203000,"protocol":"ble","mac":"11:22:33:44:55:66","name":"example","rssi":-80}
```

Display names are bounded to 32 UTF-8 bytes, may be empty, and are JSON escaped.
Optional `name_bytes_hex` preserves the original received name/SSID bytes, including
NUL or invalid UTF-8; invalid display sequences are replaced with `?`. RSSI
is an integer. MAC strings are uppercase, colon-separated six-byte addresses.
No GPS, XP, upload receipt, synthetic identity or invented coordinates exist in
these events. Deduplication and existing XP rules are owned by WDG. Firmware
coalesces unchanged identities within a radio phase to keep UART bounded. Name
enrichment/change and stronger RSSI are real new observations with a new sequence;
WDG applies its existing repeat rules. A bounded FIFO identity ring evicts oldest
coalescing entries when full, so continuous BLE never stops accepting new MACs.
Later phases may observe identities again. Queue overflow increments `dropped`.
`last_seq` is the last assigned observation sequence, so dropped queue entries may
leave gaps. The stopped `dropped` total is finalized by the writer after earlier
FIFO rows. A failed completion write retains ownership; no final legacy ACK is
authorized. A separate explicit STOP may retry completion after UART recovery.

## Completion and errors

```json
{"v":1,"event":"stopped","boot_id":"0123456789abcdef","session":"fedcba9876543210","emitted_monotonic_us":2000000,"last_seq":2,"dropped":0}
{"v":1,"event":"error","boot_id":"0123456789abcdef","session":"fedcba9876543210","emitted_monotonic_us":2000100,"code":"busy","message":"Stop the active radio operation first"}
```

Errors use stable codes and short English messages. Session can be absent for
capability/argument errors. `stopped` follows disabled RF and drained output.
The legacy final `All operations stopped.` remains the universal arbiter ACK;
WDG must not start a competing operation before that ACK. An error never grants
radio ownership. A stop timeout must not manufacture a successful ACK.

## Approved timing

* KG Wi-Fi: 1/6/11 at 110 ms; 36/40/44/48 at 100 ms; at least 1500 ms from
  completed Wi-Fi cycle to the next Wi-Fi cycle. With BLE enabled, a passive
  1000-ms BLE phase follows every five completed Wi-Fi cycles.
* Full Wi-Fi: sequential accepted-channel sweep, using JanOS tier dwells:
  500 ms for 1/6/11, 400 ms for remaining 2.4 GHz/non-DFS tier, 250 ms for the
  existing DFS tier. With BLE enabled, each sweep is followed by passive BLE
  for 1000 ms. No parallel BLE.
* BLE SOLO: preserve Wardrive 2.0 active scanning, interval 160 ms/window 40 ms,
  continuously until STOP. Combined bursts are passive instead.

The scheduler and parser tests are software evidence. The completed XIAO vendor
build and prior one-device hardware checks are described in README.md. Driver
acceptance and observed reception do not prove every channel or RF losslessness.
