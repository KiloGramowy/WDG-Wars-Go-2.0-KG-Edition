# 🚙 WDG WARS GO 2.0 — 0.9.20 DEV: WARDRIVING SOLO

📡 Pick your profile. 🛰️ Let GPS catch up. 💰 Keep the loot.

Original game and JanOS by **LOCOSP**. Modified by **KiloGramowy**, with the scan
profile and GPS policy from **Piglet KG Edition**.

> 🚗 The cyberdeck has a road-trip plan. GPS is still looking for its shoes. 😏

**🚧 PRIVATE DEV CANDIDATE — prepared for review, not released.** SOLO has not
been installed on uConsole or flashed to XIAO. **No public Release, tag or OTA
has been activated.**

---

## 🎮 1. WARDRIVING — ITS OWN LANE AT LAST

❌ **Before:** WiFi Wardrive and BT Wardrive lived inside SNIFF and used its
existing repeating-scan mechanism.

✅ **Now:** **WARDRIVING** gets its own category beside SNIFF, powered by the
JanOS Wardrive 2.0 reception machinery and its new SOLO scheduling:

- 📶 **WiFi Wardrive** opens **FULL SPECTRUM / KG SETTINGS**.
- 🔵 **BLE Wardrive** has its own ON/OFF toggle. No extra profile popup.
- 🎮 **Left/Right → choose · Enter → apply · Esc → cancel.**

🖥️ Same WDG frame, colours, artwork and **640 × 360** layout. The chooser follows
the familiar MeshCore/Meshtastic window style; the full tab names fit alongside
PLUGINS.

📋 SNIFF stays available as **WiFi Sniff / BT Sniff**. Its keys, commands and
existing behaviour stay. SOLO gets the new lane without bulldozing the old one.

---

## 📡 2. FULL SPECTRUM OR KG SETTINGS — PICK THE ROUTE

❌ **Before:** WDG had no SOLO chooser for a full supported-channel sweep or the
Piglet KG scan profile.

✅ **Now:** choose the profile before WiFi starts:

🌐 **FULL SPECTRUM** sweeps the 2.4/5-GHz channels actually accepted by the
configured C5 driver. Firmware checks the country settings, tuning result and
channel/band readback, and reports skipped channels. **A channel written on a
list does not count as a successful tune.**

🎯 **KG SETTINGS** follows the Piglet KG profile:

- **2.4 GHz:** 1 / 6 / 11 — **110 ms each**.
- **5 GHz:** 36 / 40 / 44 / 48 — **100 ms each**.
- **Cycle gap:** at least **1500 ms** after a completed WiFi cycle.

📍 Full Spectrum uses the JanOS dwell tiers: **500 ms** for 1/6/11, **400 ms** for
the remaining basic tier and **250 ms** for its existing DFS tier. The effective
RF country remains the configured JanOS country; GPS does not silently change
it. **KG refuses to start if a required channel is unavailable.**

Actual accepted channels and physical reception still need the approved hardware
test. The software checks are ready; the antenna has not signed the paperwork.

---

## 🔁 3. WIFI + BLE — TAKE TURNS, KEEP COLLECTING

❌ **Before:** the existing SNIFF scheduler controlled its repeating scans from
the host. It did not provide these firmware-owned SOLO profiles.

✅ **Now:** enable **WiFi only**, **BLE only**, or **both**. JanOS handles the
approved schedule while WDG receives and saves observations:

- 🎯 **KG + BLE:** five complete WiFi cycles, then **1000 ms passive BLE**.
- 🌐 **FULL + BLE:** one full supported-channel sweep, then **1000 ms passive BLE**.
- 🔵 **BLE solo:** the existing Wardrive 2.0 **active** scan, with a **160 ms
  interval / 40 ms window**, continuously until STOP.

📻 Combined scans **alternate WiFi and BLE**. Their scan phases do not run in
parallel. Short radio windows are scheduled in firmware; slow log output stays
out of the radio scheduler.

---

## 🛰️ 4. GPS RUNNING LATE? THE SCAN STILL CLOCKS IN.

❌ **Before:** the stock JanOS Wardrive 2.0 workflow waited for GPS and used its
own SD logging. That was not the uConsole-owned Piglet workflow requested here.

✅ **Now:** **WDG is the single owner of SOLO GPS, cache and startup backfill**.
JanOS sends real observations; WDG uses its receiver and existing Linux storage.
**No separate SD card at XIAO is required by SOLO.**

🚦 **No fix at startup?** Scanning continues. Observations keep their original
data and capture time while the current-run startup queue waits.

📍 **First accepted fix:** location younger than **2 seconds**, **3+ satellites**
and **HDOP ≤ 10**. WDG freezes that position once, stops and drains active SOLO,
then fills the waiting records in background batches of at most **20 per UI
grant**. SOLO resumes after completion. **Backfill awards no extra XP.**

☁️ **Fix lost later?** Continue using the accepted-position cache for **720
minutes**, with its original measurement time and provenance. When it expires,
scanning continues without invented coordinates. A fresh fix brings current
positions back; it does not start another startup backfill.

🔄 **Restarted before the first fix?** The next run does not adopt the previous
run's queue or cache. A new trip does not get to rewrite the old trip's location.

🧭 **LoRa, MeshCore and Meshtastic AUTO GPS rules remain unchanged.** Cached and
backfilled positions are recorded as such; they never pretend to be a new fix.

---

## 🧵 5. FOLLOW YOUR ROUTE — NOT THE SCAN PROGRESS BAR

❌ **Before:** there was no SOLO GPS trail, and the older roughly **30-metre**
player-position filter could hide short movements.

✅ **Now:** fresh measured GPS samples update the player and trail **without
waiting for a WiFi/BLE scan to finish**. Short real movements remain visible;
the existing camera still smooths the view.

🗺️ Downloaded tiles or the familiar grid/coastline fallback — **the same trail
works with either**. Existing markers and effects keep their own layers.

⏸️ **No fresh GPS = no invented movement.** Cache and backfill do not draw a
route through an outage. The **4096-point visual budget** thins older trail
points and may remove oldest complete segments, preserving the newest half
and gaps. **Source observations and exports are not rewritten by that visual
reduction.**

---

## 💰 6. SAME LOOT. ZERO XP CLONING.

❌ **Before:** your WiFi/BLE history, identities, XP and uploads already belonged
to WDG's existing storage. SOLO needed to join that system.

✅ **Now:** SOLO feeds the **same CSV, history, deduplication, counters and later
WDGWars upload path**. Your saved progress stays in the existing system.

💰 **New WiFi: 15 XP · New BLE: 10 XP · Valid repeats: 1 XP.**

🔄 A repeated source identity or a backfill correction earns **no second XP**.
Repeated receptions update one current target; WiFi and BLE identities remain
separate. A delayed backfill cannot overwrite a newer valid SNIFF export or move
its newer map marker backwards.

💾 Failed writes stay pending for retry. Saved discovery events reach the UI
after persistence, and shutdown drains them before the final XP save and log
close. **An error never becomes an imaginary successful save.**

> 🧺 GPS backfill fills coordinates. It does not operate an XP laundromat. 😉

---

## 🛑 7. ONE C5. NO RADIO TUG-OF-WAR.

❌ **Before:** the new SOLO engine had no WDG handover or confirmed-status path.

✅ **Now:** **ON requires compatible firmware, current capabilities and a
matching start confirmation**. A successful command write alone cannot light
the ON indicator.

🔀 Switching operations sends STOP and waits for the final **All operations
stopped** acknowledgement. A timeout cancels the pending start. The next scanner
does not get to jump the queue.

🔧 **SYSTEM → STOP ALL** or quick **S** can explicitly retry a timed-out STOP.
Background polling does not keep retrying. Disconnect or firmware reboot clears
the confirmed state and requires a deliberate new selection. Failed radio
restoration keeps the ownership barrier until STOP succeeds.

📦 SOLO needs the compatible JanOS extension. Stock firmware or a bridge without
it stays OFF with an explanation. Queue losses are reported, and bounded-buffer
errors are not advertised as complete capture.

---

## 🧪 8. TESTED — NO RADIO FAIRY TALES

✅ **1106 software methods: 1094 PASS, zero failures/errors, 12 Windows
platform/privilege skips. No new SOLO test was skipped.**

🧩 **29 native C tests passed:** 10 scheduler tests and 19 adapter tests using
real cJSON with simulated SDK/radio/task boundaries.

🛠️ **Full XIAO ESP32-C5 build passed** with **ESP-IDF 6.0.1 / GCC 15.2.0**,
including linking and the partition-size check. The app is **2,250,896 bytes**,
with **45% of the smallest app partition free**.

📶 **Physical RF, live channel acceptance, hardware timing and the uConsole
display test are still pending.** Recorded replay, simulated transports and a
successful build do not prove those hardware results. The earlier **965 Linux
repair tests** belong to earlier work, not a physical test of SOLO.

---

## 🚧 9. IN THE WORKSHOP — NOT IN YOUR UPDATER YET

🔒 This remains part of the **private 0.9.20 candidate** on the existing DEV
branch. **No SOLO installation, XIAO flash, public Release, tag or OTA activation
has been performed.** Hardware testing and deployment require separate approval.

💾 Existing settings, saved progress and earlier 0.9.20 repairs remain protected.
Piglet Collection, MASTER/NODES, hotspot, local API, file synchronization, other
wardriver integrations and WiFi Spam are outside this stage.

> 🐕 Same hunt. New road trip. Somebody tell GPS the engine is running. 😎

---

<details>
<summary>🔬 Open the technical notes — exact rules, source references and build receipts</summary>

### WARDRIVING SOLO — technical validation record

Original game and JanOS by **LOCOSP**. Modified by **KiloGramowy**, with the
approved scan profile and GPS policy from **Piglet KG Edition**.

**Private preparation. No uConsole installation, firmware flash, public Release,
tag or OTA activation is part of this task.** Earlier 0.9.20 repair-preview
results do not validate this new scanner.

## 📌 Sources and approved boundaries

| Component | Reference |
| --- | --- |
| WDG working branch | `codex/0.9.20-dev-performance` |
| WDG starting HEAD | `890bc51398bffe5bd79b7056257df9ff0cda5877` |
| Piglet KG | `fd8c95c5386cb116fcca7c1baa5b2936af9be880` |
| JanOS/projectZero | `ef35e9bbed28c441f53abdb6e634cf46428f912b` |

The starting HEAD was newer than the prompt's `7bbb930`; none of the prepared
0.9.20 repairs were reverted. No new project branch or merge was created.
Firmware modifications live as a reproducible patch, small source files and
tests in [`firmware/wardriving-solo`](../firmware/wardriving-solo/README.md).
Reference sources and LOCOSP's upstream are unchanged.

**One GPS owner: WDG on uConsole.** This was explicitly approved after comparing
it with firmware-owned policy and host-side durable storage. The latter would
require additional cross-device GPS state, durable acknowledgements and recovery
messages. The selected design keeps the receiver, policy and durable files
together; JanOS supplies real observations and owns radio scheduling only.

The system card used by Linux is not JanOS's `/sdcard`. Stock JanOS uses a
separate SPI SD interface on MISO 2 / MOSI 7 / CLK 6 / CS 10. Available device
logs did not establish that a card is connected there. The occupied ESP32 port
was not opened by a second reader. **No physical XIAO SD card is assumed or
required by SOLO**, and no new hardware is required.

## 🎮 Familiar menu, separate engine

**Before:** SNIFF contained WiFi Wardrive and BT Wardrive, using the existing
host-side repeating scans.

**Now:** a separate **WARDRIVING** category sits beside SNIFF:

- **WiFi Wardrive** opens **FULL SPECTRUM / KG SETTINGS**.
- **BLE Wardrive** toggles independently, without a profile dialog.
- WiFi solo, BLE solo and combined WiFi/BLE are supported by the new contract.
- **Left/Right**, **Enter** and **Esc** use the existing protocol-picker behavior.

The picker retains the LoRa chooser's frame, palette, coordinates and row sizes.
All category labels, including PLUGINS, fit the original 310-pixel panel at
**640 × 360**. Existing category artwork remains attached by name.

SNIFF's labels become **WiFi Sniff** and **BT Sniff**. Their commands, keys and
existing scheduler remain unchanged. SOLO does not use that scheduler.

## 📡 Radio schedules

| Mode | Firmware schedule |
| --- | --- |
| KG WiFi | 1 / 6 / 11: **110 ms** each; 36 / 40 / 44 / 48: **100 ms** each |
| KG cycle gap | At least **1500 ms** after a completed WiFi cycle |
| KG + BLE | **1000 ms passive BLE after every fifth completed WiFi cycle** |
| Full WiFi | Sequential sweep of channels actually accepted by the driver |
| Full tier dwells | 1 / 6 / 11: **500 ms**; remaining basic tier: **400 ms**; existing DFS tier: **250 ms** |
| Full + BLE | One full sweep, then **1000 ms passive BLE**, repeated |
| BLE solo | Existing Wardrive 2.0 **active** scan, interval **160 ms**, window **40 ms**, until STOP |

Combined modes do not run WiFi and BLE scans in parallel. Scheduler waits account
for the pinned **100 Hz FreeRTOS tick**, and UART output runs in a separate
writer rather than blocking short radio dwells. These are programmed and
software-tested schedules; physical dwell timing still needs measurement.

Full Spectrum considers 1–14 in 2.4 GHz and 28 SDK candidates in 5 GHz. A list
entry alone is not support: firmware checks the effective country settings,
each tuning result, and channel/band readback. It reports accepted channels and
reasons for skipped channels. KG fails if any required channel is unavailable.

Capabilities are checked again after actual JanOS initialization, before the
start confirmation. The effective country comes from the existing JanOS
configuration, including its existing country initialization; **it is not
inferred from GPS**. No country-broadening logic was added.

**Driver acceptance, country configuration and actual RF reception are different
facts.** No list of physically received channels is claimed here.

## 🛑 One C5, one owner

The existing ESP32 arbiter sends STOP and waits for the final **All operations
stopped** acknowledgement before handing the radio to another operation.
Receipt of the word `stop` is insufficient. A timeout cancels the pending start;
it does not manufacture a successful handover.

SOLO **ON** comes only from a matching firmware state confirmation after actual
phase initialization and actual-start capabilities. It never means merely
"a command was written." Stock JanOS or a software bridge without this extension
remains OFF with an explanatory message; a version string is not a capability.

STOP disables producers and drains accepted output before completion. Explicit
**SYSTEM → STOP ALL** or quick **S** can retry a timed-out STOP; background polling
does not keep retrying or automatically start a competing scanner. Disconnect or
firmware reboot clears confirmed states and requires a deliberate new selection.
Firmware queue loss is counted and reported, not disguised as complete capture.

## 🛰️ GPS takes the Piglet route

Only this new SOLO path uses the Piglet policy. **LoRa/MeshCore/Meshtastic AUTO
criteria and regional assignments are unchanged.**

1. **Start without fix:** scan normally and save real observations without
   invented coordinates. One current-run startup queue awaits the first fix.
2. **First accepted fix:** location age must be **less than 2 seconds**, with
   **at least 3 satellites** and **HDOP ≤ 10**. Freeze that snapshot once. Stop
   and drain active SOLO before sealing its queue; late in-flight observations
   still belong to the closing batch.
3. **Backfill:** the background writer corrects the existing records using the
   frozen snapshot, preserving their observation time and source data. Replay
   receives at most **20 records per UI grant**. SOLO resumes only after the
   replay finishes. Backfill from an already stopped SOLO does not stop a
   different tool the player has since started.
4. **Loss after a good fix:** continue with the last accepted position cache for
   **720 minutes**, retaining the original measurement and cache-refresh times.
   As in Piglet, polling can refresh the cache while a qualified measurement is
   still fresh; it never changes that measurement's receive time.
5. **Expiry:** continue scanning without coordinates. Do not generate a location.
6. **Recovery:** use current measured positions again. Cache refresh retains the
   quality gate. After startup, a fresh valid position is preferred according to
   Piglet's policy; it does not start another startup backfill.
7. **Restart before first fix:** do not adopt the previous run's pending queue
   or GPS cache. Original logs and observations are not reinterpreted using a
   new trip's fix.

Position decisions record **measured**, **cache** or **startup_backfill** provenance
in the existing `serial_full.log`. HDOP remains HDOP; it is not relabelled as
measured accuracy in metres. Unknown CSV accuracy stays unknown.

Source UART timestamps retain firmware boot/session/sequence identity and the
capture monotonic clock. UTC is derived from a recorded host clock exchange with
an uncertainty value; it is not claimed as exact GPS UTC or a flush-time sample.

## 💾 Same loot, same XP

SOLO uses the existing WiFi/BLE CSV formats, protocol-specific deduplication,
history, saved identities and later WDGWars upload path. There is no second score
database or upload client.

- **15 XP** for a new WiFi identity; **10 XP** for a new BLE identity.
- Existing valid-repeat rules remain: **1 XP** for a repeat observation.
- A retransmitted source identity or a backfill correction earns **no second XP**.
- Accepted discovery events reach the UI after the background persistence step.
- Repeated WiFi observations update one current scan target rather than growing
  an unlimited target list from every repeated packet.

BLE's first history record and strongest WiGLE record can have different source
times. Each correction matches its own record. A newer valid SNIFF export or map
position is preserved instead of being overwritten by an older SOLO backfill.

The two CSV files retain their existing independent atomic replacements; this is
not a new multi-file transaction system. Partial failure is explicit and retry
is idempotent. Failed writes, malformed pending data or uncertain conflicts keep
the job pending and do not produce a false completion. Scan selection can retry
retained saves before a new RF start. Shutdown drains saved events before the
existing final XP save and loot close; an unfinished writer is reported.

Bounded queues keep the UI responsive. Overflow stops the source with an error;
the software does not promise lossless RF/UART capture under arbitrary overload.

## 🧵 Follow the route, not the scan progress bar

The GPS trail and player movement use fresh measured samples independently of
WiFi/BLE scan completion. SOLO bypasses the older approximately 30-metre player
movement filter, so short real movements are visible. The existing camera still
provides visual smoothing.

The trail uses the same map projection with cached tiles or the grid/coastline
fallback. Cache positions and backfill do not move the player or draw a route
through a GPS outage. Existing markers and effects retain their own layers.

The trail is current-session visual state, with a **4096-point rendering budget**:
the newest half is retained and older interior points are thinned. Separate
segments are never joined across an outage; if necessary, oldest complete visual
segments are removed. This visual reduction does not rewrite source GPS/scan
records or exports. Cached geometry is reused until the view or trail changes.

## 🧪 Validation and hardware gate

| Software check | PASS | FAIL / ERROR | SKIP |
| --- | ---: | ---: | ---: |
| Complete final Windows CPython 3.12 suite, 1106 methods | 1094 | 0 / 0 | 12 |
| Native C scheduler + SDK-boundary adapter | 10 + 19 | 0 / 0 | 0 |
| Complete ESP-IDF 6.0.1 XIAO vendor build | 1 | 0 / 0 | 0 |

The complete run used the existing private Meshtastic recording and the actual
LoRaRF 1.4.0 driver source, both outside Git. All seven runtime file hashes were
unchanged during the complete final run. It includes the `restore_failed`
unknown-owner STOP barrier and the four tests of the actual app GPS-polling
method, as well as the earlier CSV, tile, updater and Auto-clean regressions.

The 12 skips concern POSIX process replacement, root/owner/archive integration,
POSIX permissions/xattrs and symlink privileges unavailable in this Windows
session. **No new SOLO test was skipped.**

Native C uses real cJSON, a strict C11 compiler and fake SDK/radio/task boundaries,
including the pinned 100 Hz tick conversion. It checks schedules, tuning/restore,
source identities/bytes/clocks, bounded queues and STOP failure/retry. It is not
a real FreeRTOS scheduling or physical-radio test.

Native SDK stubs and recorded Meshtastic replay are software evidence, not fresh RF.
The prior 965-test uConsole repair run is historical evidence for earlier work,
not a hardware test of SOLO.

**Complete vendor build: PASS.** The final patch was applied to an isolated copy
of the pinned JanOS source and built with **ESP-IDF 6.0.1 / GCC 15.2.0**, target
**ESP32-C5**, the upstream XIAO overlay and `idf.py --preview build`. Compilation,
linking and the partition-size check passed. The app is **2,250,896 bytes**, with
**45% of the smallest app partition free**. The built main/core/adapter source
bytes were checked against the final patch. Build scheduling/path workarounds
were confined to the isolated Windows toolchain, not runtime firmware settings.

- Final patch SHA256: `16b2b910dd18cee0301dc76add39ce8e898322b2ccb9e56b16e29b727b5638ae`.
- This build's app BIN SHA256: `e3dff6b534ff8d7712d37e181a3d2d1e30d3672877b6925be2a4bf27c8a989c5`.

The binary, ELF, SDK, toolchain, private recording and full local logs remain
outside Git and OTA. A successful build proves vendor API/type/link compatibility;
it does not prove RF reception, physical timing or console behavior.

**Hardware test pending.** After a separately approved firmware flash and WDG
deployment, test each solo/combined profile, inspect live accepted/skipped
channels, measure the phases, exercise GPS start/loss/cache/recovery and startup
backfill, compare source times/coordinates in both CSVs, and inspect the trail
with tiles and without tiles. Exercise STOP, missing ACK and reconnect using one
serial owner. Back up firmware, changed code and current player data before
that separately approved operation; restore only the changed code/firmware when
rolling back, retaining newer player progress.

No Piglet Collection, MASTER/NODES, hotspot, local API, file synchronization,
other wardriver integration or WiFi Spam is included.

> ☕ GPS may take a coffee break. The scanner keeps its shift — the coordinates
> just have to wait for an honest receipt.

</details>
