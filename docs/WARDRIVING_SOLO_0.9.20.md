# 🚙 WDG WARS GO 0.9.20 — WARDRIVING SOLO

Original game and JanOS by **LOCOSP**. Modified by **KiloGramowy**, with the
approved scan profiles and GPS policy from **Piglet KG Edition**.

> Pick a profile. Let the radio take turns. GPS can finish tying its shoes. 🛰️😏

## 🎮 Its own lane in WARDRIVING

**Before:** WiFi/BT Wardrive used the existing repeating scans in SNIFF.
**Now:** open **TAB → WARDRIVING** for dedicated **WiFi Wardrive** and
**BLE Wardrive**. SNIFF remains available as WiFi Sniff / BT Sniff with its
existing behavior. The other modules keep their established controls.

WiFi opens **FULL SPECTRUM / KG SETTINGS**. Use **Left/Right** to choose,
**Enter** to apply and **Esc** to cancel. BLE has its own ON/OFF selection.
Enable either protocol alone or Wi-Fi with BLE. **S** uses the existing STOP.

**A compatible JanOS SOLO extension is required on the embedded XIAO-C5.**
[The application, manifest, checksums, source and build guide are included](../firmware/wardriving-solo/README.md).
Updating game files alone does not flash the ESP32. Without a valid capability
reply the game reports the missing extension and leaves the scan OFF; a JanOS
version number alone is not treated as support.

## 📡 FULL SPECTRUM / KG SETTINGS

**Before:** there was no firmware-owned SOLO profile chooser.
**Now:** the C5 runs the selected schedule while WDG receives real observations.

| Profile | Wi-Fi schedule | With BLE enabled |
| --- | --- | --- |
| **KG SETTINGS** | 2.4 GHz **1/6/11, 110 ms each**; 5 GHz **36/40/44/48, 100 ms each**; at least **1500 ms** between completed Wi-Fi cycles | **1000 ms passive BLE after every five completed Wi-Fi cycles** |
| **FULL SPECTRUM** | Sequential sweep of channels accepted by the running C5 driver; inherited JanOS dwells **500 ms** on 1/6/11, **400 ms** on the remaining basic tier and **250 ms** on the existing DFS tier | **1000 ms passive BLE after each complete sweep** |
| **BLE SOLO** | No Wi-Fi sweep | Existing Wardrive 2.0 **active BLE**, interval **160 ms**, window **40 ms**, continuously until STOP |

Combined sessions **alternate** Wi-Fi and BLE; they do not scan both radios at
once. Firmware reports accepted and skipped channels under its current country
settings. GPS does not silently change that country. KG refuses START if any
required tuning channel is missing. An AP's advertised primary channel remains
its real metadata, including a frame received from an overlapping channel.

The scheduler honors the SDK's **100-Hz** task tick. The schedules above are
specified and software-tested targets; prior hardware phase events were also
observed. This is not an independent measurement of every physical RF dwell or
permission to transmit on every listed channel.

## 🛰️ No fix at startup? Keep the real observations

**Before:** stock JanOS Wardrive 2.0 owned its GPS/SD logging workflow.
**Now:** SOLO sends raw Wi-Fi/BLE observations and WDG owns GPS and Linux
storage. **No separate SD card inside the embedded XIAO is required.**

- A qualified first fix requires a location **younger than 2 seconds**,
  **at least 3 satellites**, and **HDOP ≤ 10**.
- Until that first fix, the current game's startup queue preserves observations,
  source timing and metadata. It does not invent coordinates.
- The first qualified fix is frozen once for **Startup GPS Backfill**. WDG
  confirms STOP and drains the active SOLO session, replays the queue in
  background batches of at most **20 per UI grant**, then resumes the prior
  requested SOLO session.
- Backfill does not create another scan, observation identity or extra XP.
- Once a real fix has populated the cache, temporary GPS loss can use the last
  known cached location for **720 minutes**. The measurement timestamp and
  cached provenance stay distinct from a fresh measured fix.
- The GPS policy and cache belong to this running game process. A new game does
  not adopt a previous run's pending queue or treat its position as a fresh fix.
  Expired or unavailable positioning is kept explicit rather than fabricated.

The ordinary GPS/display position and other radio modules retain their own
existing rules. This policy does not alter LoRa AUTO, GPS firmware or Piglet CSV
coordinates.

## 🧵 A GPS trail with an honest source

**Before:** this SOLO workflow had no dedicated current-session GPS trail.
**Now:** qualified measured movement can draw the existing-style trail on the
map, with tiles or without them. Cache positions and startup-backfill coordinates
do not fabricate a movement track. STOP/missing fixes break the trail segment.
Projection caching follows map pan/zoom changes; the trail is visual session
data, not an alternative GPS journal or an upload source.

## 💾 Same loot, real records

**Before:** there was no durable host pipeline for the SOLO event stream.
**Now:** each accepted observation retains its protocol, real MAC/BSSID, name,
RSSI, channel and capture metadata. WDG uses its existing stores, CSV, history,
deduplication and ordinary Wi-Fi/BLE XP rules.

The recorder runs in a bounded background queue. Failed writes are visible and
retryable; completion is not invented to hide an error. The exact CSV index
avoids repeatedly parsing unchanged whole CSVs, while real metadata/stronger
observations retain the established storage behavior. A Bloom filter is not
part of this release.

Existing manual Sync upload and Auto-clean settings remain. SOLO does not add
a new automatic external upload or change already-earned XP. Historical contact
and player data are preserved. Piglet Collection has its separate
[installation and use guide](PIGLET_COLLECTION_INSTALL.md).

## 🚦 One C5, one confirmed owner

**Before:** SNIFF's host scheduler managed its own repeat operations.
**Now:** SOLO uses capabilities, session identities and real START/STOP ACKs.
A profile/protocol change first stops the previous confirmed session. Another
tool cannot be granted the C5 while SOLO still owns it.

STOP disables capture, drains accepted output and waits for the existing final
`All operations stopped.` ACK. A timeout/failure stays unresolved and visible;
it does not pretend the radio is free. The flasher has exclusive serial access
while active. Legacy commands remain on their original paths when SOLO is OFF.

## 🛠️ Firmware compatibility and updates

The supplied **application-only** image is for the tested **XIAO ESP32-C5
revision 1.0, 8-MiB flash, USB Serial/JTAG** configuration and verified JanOS
partition layout. It is separate from Piglet firmware and its different offsets.
Do not flash a WROOM, Linux adapter, software bridge/PTY or another partition
layout with this image.

**Update Game installs game code; it does not install this firmware.** The
existing ESP32 FLASH wizard downloads stock upstream JanOS and can replace SOLO.
Use the [package compatibility and verified-backup requirements](../firmware/wardriving-solo/README.md)
before planning an application-only firmware installation. No stock flash
profile, baud or reset mode was silently changed to add this extension.

## 🧪 What was actually checked

**Software:** the existing complete **ESP-IDF 6.0.1 / GCC 15.2.0** XIAO build
passed compilation, linking and partition-size checks. The exact source/patch
and unchanged application SHA were reverified for release. **10 native core +
19 adapter tests** passed again using fake SDK/task/radio boundaries and real
cJSON. Host regression results are recorded with the release's validation.

**Physical tests completed earlier on one XIAO-C5/uConsole:** FULL Wi-Fi, KG
Wi-Fi, BLE SOLO, FULL+BLE and KG+BLE; real Wi-Fi/BLE reception and durable host
records; final STOP/ownership ACKs; GPS first fix/cache/startup backfill; tile and
non-tile views plus tested zoom changes. The tested image was retained unchanged;
no new physical flash was done just to prepare the release.

**Still limited:** no claim of RF losslessness or independent per-channel dwell
measurement; no WROOM/AC1200 SOLO validation, full physical 12-hour cache-boundary
run, complete moving field-track validation or universal USB cold-start recovery.
An intermittent USB-enumeration failure occurred during earlier testing; its
cause was not established. Program tests do not replace those hardware checks.
