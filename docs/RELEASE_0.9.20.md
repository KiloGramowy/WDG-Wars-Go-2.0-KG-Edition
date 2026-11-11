RELEASE 0.9.20 - GO SOLO. SEND THE PIG. KEEP THE LOOT.

**📡 New WARDRIVING SOLO. 🐷 Piglet KG Collection. Less repeated work for the cyberdeck.**

Take the uConsole out hunting, or let Piglet bring home the CSV. Your CPU has enough paperwork already. 😏

Original game and JanOS by **LOCOSP**. Modified by **KiloGramowy**.

## 📡 WARDRIVING SOLO — one tab, your call

❌ **Before:** the repeating SNIFF scans were WDG's Wi-Fi/BLE wardrive path; JanOS Wardrive 2.0 expected its own SD/GPS workflow.

✅ **Now:** the separate **WARDRIVING** tab offers **WiFi Wardrive** and **BLE Wardrive**: **FULL SPECTRUM**, **KG SETTINGS**, **BLE SOLO**, or alternating Wi-Fi + BLE. Existing **WiFi Sniff / BT Sniff** remain available in SNIFF.

- **KG SETTINGS:** channels **1/6/11 at 110 ms**, then **36/40/44/48 at 100 ms**, with at least **1500 ms** between completed Wi-Fi cycles. BLE ON adds **1000 ms passive BLE after every fifth full cycle**.
- **FULL SPECTRUM:** the channels actually accepted by the driver, using existing JanOS **500/400/250 ms** tiers; **1000 ms passive BLE** follows each full sweep when enabled.
- **BLE SOLO:** existing active BLE settings, **160 ms interval / 40 ms window**. Combined modes alternate radio use.
- **Confirmed START / STOP:** matching firmware capabilities and a real acknowledgement are required. Switching tools waits for STOP completion; a timeout does not start a competing radio operation.

**GPS can catch up:** collect before the first fix, then use Startup GPS Backfill. The first accepted fix must be younger than **2 seconds**, with **3+ satellites and HDOP <= 10**. After fix loss, the accepted-position cache lasts **720 minutes**, retaining its measurement time and provenance. Expired or unavailable GPS does not invent coordinates. The measured **GPS trail works with downloaded tiles or the existing grid**.

Existing CSV, history and upload paths stay. Basic rewards remain **15 XP for new Wi-Fi, 10 XP for new BLE and 1 XP for repeats**; backfill and source retransmissions do not award XP twice.

**Compatible JanOS SOLO firmware is required.** Updating the game does not flash the embedded XIAO. A familiar firmware version banner alone does not establish SOLO support, including on software bridges.

## 🐷 PIGLET KG COLLECTION — pocket the pig, park the cyberdeck

❌ **Before:** there was no paired Piglet receiver in WDG, and bringing its CSV archive into the game required a separate manual workflow.

✅ **Now:** **WARDRIVING → Piglet Collection** receives closed Wi-Fi/BLE CSV over a paired local hotspot, saves and imports whole files in the background, and places native exports in the existing **Pending Upload Logs** workflow for **manual upload**.

**Two modes, one clear destination:**

- **Normal Piglet** retains its original startup, configured uploads, WebUI, buttons, scan profiles, GPS and retention.
- **UConsole Piglet** delivers CSV **only to its paired uConsole**. Direct external CSV uploads are refused before connection/send, including manual WebUI requests. Saved service credentials remain available for Normal mode.
- At each boot, UConsole Piglet searches for the saved collector for **at most about 10 seconds**. Transfer may take longer. Then comes the original WebUI stage: fallback **192.168.4.1**, the full **60-second window** and **Stay: five minutes**.
- In standalone UConsole mode, that window ends by disconnecting home Wi-Fi and starting the existing scanning flow, even when `autoStartAfterUpload` is off. Deliberate pauses and saved Core/Node startup behavior retain their existing rules.

**Your archive gets a receipt:** Collection handles both **`/logs` and `/uploaded`**, including a one-time migration of the available historical archive. Final **ACK** requires complete receipt, SHA256 verification, durable storage and successful import or safe recognition of already imported data. Unacknowledged files remain protected for another boot; after ACK, Piglet's existing archive handover and retention settings apply.

**No parade of map dots:** imports produce **no per-record map markers, terminal replay or device animations**. Processing preserves source GPS, timestamps and supported radio fields; unknown values remain unknown. Each whole CSV settles its remaining XP and identities once. Repeated content, retransmission and restart cannot pay the same import twice.

**Progress tells you which job is happening:** **DOWNLOADING CSV** measures the current file's received bytes; **IMPORT CSV** tracks processing across the announced job. **100% waits for durable exports, completed accounting and required ACKs**. **ACTIVE PIGLET KG** means an authenticated request was seen in the last **30 seconds**; **WAITING FOR PIGLET KG** means the receiver is ready without recent qualifying activity.

**Pairing:** copy the **hotspot SSID, hotspot password, collector IPv4 address, collector port and Collection pairing token** shown by your own uConsole into Piglet's matching WebUI fields. Start the receiver **before** rebooting Piglet. **P** shows/hides pairing details; **Enter** starts/stops, **S** stops. During active or unfinished work, **Esc** asks **Yes / No**, defaulting to **No**. Cancelling retains completed imports, saved rewards and resumable raw data.

The hotspot uses a separate virtual AP on the connected router interface's **same channel**. Supported concurrent STA+AP hardware/driver/NetworkManager configuration is required; unsupported combinations fail preflight rather than taking over router Wi-Fi.

**This release is validated with one physical Piglet. Multiple physical Piglets remain unsupported pending correction and verification of the device identifier.** The supplied application-only BIN targets the documented **XIAO ESP32-C5, 8 MiB flash / 8 MiB PSRAM, existing `default_8MB`, app0 at `0x10000`** layout. It is not a blank-board installer and does not flash JanOS inside uConsole.

🐷 Five pairing fields. One delivery address. The pig is not a mind reader.

## 🗺️ Performance — remember the street, skip the paperwork

❌ **Before:** unchanged raster-map backgrounds were rescaled and copied every frame; even a weaker repeat could reread the entire session CSV.

✅ **Now:** the completed tile background is cached and rebuilt after pan, zoom, viewport or tile changes. Aircraft, mesh, player markers and animations still update separately. The CSV path reuses an **exact in-memory device index**, reloading after external file changes and rebuilding between sessions.

Formats, stronger-signal rules, scan parameters, reward rules and accepted-write durability stay unchanged. Collection workers get a conditional **1 ms interpreter hand-off per frame** while Collection has work; inactive Collection adds no wait to standalone SOLO.

Earlier isolated uConsole measurements reduced an unchanged-background draw from **43.4002 ms to 0.1385 ms CPU**, and a weaker repeat in a 10,000-row CSV from **59.7063 ms to 0.0231 ms CPU**. These are warmed function measurements, **not whole-game FPS, watts, temperature or battery-life claims**. Changed CSV still use atomic whole-file replacement; the index grows with the session.

## 🧭 Sync and large uploads — sensible envelopes, real receipts

❌ **Before:** local preparation could hold up the game thread, and large signed requests could hit server limits. Partial or ambiguous responses needed more careful accounting.

✅ **Now:** Sync opens with **Preparing local sessions...** while a worker builds its snapshot. Unchanged mesh parses are reused; read failures stay errors rather than becoming a fictional zero-pending result.

Manual upload targets **10,000 records per batch** and a maximum **6 MiB signed HTTP body**, whichever is reached first. **HTTP 413**, a **90-second timeout**, or an incomplete legacy duplicate-cache reply leads to bounded retry/smaller requests. Each real acknowledgement stays bound to its payload, file, account and endpoint. **All rows must be accounted for before a file is fully uploaded or eligible for retention.** Unconfirmed data stays pending; a manual discard is not a server receipt.

A fully confirmed duplicate-only Collection native export can be removed while its raw source, history and XP stay. Other successfully uploaded files use the configured Auto-clean policy.

There is **no new uploader whole-file cap**, but this is not a streaming rewrite: memory use grows with the file. The **1 MiB** synthetic capacity case ran in the main suite; **30 / 100 MiB** cases passed a separate opt-in run. They use fake HTTP with real parsing, signing, acknowledgement coverage and resume checks. The separate run reached approximately **1.06 GiB peak RSS**. Piglet Collection's separate **2 GiB receive limit** remains. Two actual 100,000-record exports completed with **10 acknowledged batches each**; that is not proof that every server/file combination succeeds.

📮 Piglet packed the archive. The server still chooses the size of its letterbox.

## 💾 Saves and shutdown — progress needs a real receipt too

❌ **Before:** a failed XP write could look saved in memory and suppress a retry. A Sync callback could also reach terminal state before initialization completed or after shutdown began.

✅ **Now:** a failed write retains pending progress for retry. Final-save/flush failure blocks update installation and restart. Terminal state is initialized before Sync starts; tracked workers and stop-aware callbacks keep late results out of a closing UI. Unfinished workers are reported rather than declared safely stopped.

**Previously earned XP is not recalculated, and reward rates stay unchanged.** Login requests also keep their existing **five-second pause** after dismissals and handled errors.

## 🔧 Flash tools and OTA — check the toolbox first

❌ **Before:** missing tooling could be detected after releasing Serial; competing reconnects, reopening the wizard or stale cache contents could mislead a flash attempt. Root-launched archive preparation could also fail with a misleading network message.

✅ **Now:** flash preflight uses **the game's Python**, requires **esptool >= 5.0.0**, and checks before downloads, serial closure or USB changes. One operation owns the port; reopening the window shows that operation. Selected XIAO/WROOM archives are verified in a fresh attempt directory. Completion distinguishes **write finished → USB connected → fresh firmware response**.

The flasher has a **300-second total / 60-second no-output limit**, then bounded terminate/kill waits of **up to five seconds each**. An unconfirmed process exit keeps its resources reserved. Existing profiles, **460800 baud**, reset modes and **`0x2000 / 0x8000 / 0x20000`** offsets remain. No automatic package installation or permission changes.

The updater now receives the checkout owner's Git archive through the game's own private output file, with clearer source/archive/access/storage errors. **`sudo -v` is not a repair for a different user's staging permissions.** A running older updater needs the corrected code before benefiting; ZIP installs without Git metadata require the documented clone migration.

## 🧹 Auto-clean — uploaded is not a lifetime lease

❌ **Before:** uploaded CSV had no configurable age-based cleanup.

✅ **Now:** **PLUGINS → Pending Upload Logs → A** offers **Keep all**, **1 / 5 / 15 / 31 days**, or **Custom: 1–999999 days**. **Enter** applies; **Esc** cancels. **Default: Keep all.** Existing choices survive restart.

Age starts at the first saved complete confirmation for that exact content. Only unchanged, closed, fully acknowledged **Wi-Fi/BLE, ADS-B, MeshCore and Meshtastic exports** qualify. Pending, partial, failed, active, changed and uncertain old files stay. Checks run every **60 seconds**, bounded to **8 historical sessions / 8 expired files**; browsing pauses deletion.

Only eligible exports are removed. **XP, levels, achievements, contacts, known identities and other session files stay.** Piglet's separate retention settings are not changed.

## ↔️ UI polish — give those counters some elbow room

❌ **Before:** startup credits did not share a clean center, and larger footer totals could squeeze their neighbours.

✅ **Now:** **by LOCOSP** and **Modified by KiloGramowy** use the active font's measured width. Footer labels and values do too, with consistent spacing and the existing dense-layout fallback. Original artwork, colours, controls and counter meanings remain.

## 🧪 Tested paths, honest limits

**Public-candidate host regressions:** Linux/uConsole ran **1,422 tests: 1,417 passed, 5 skipped**, with **zero failures/errors**, as root on Python **3.13.5**. This includes the root-only Git-permission cases, real-exec restart checks and the actual LoRaRF driver contract. Windows ran **1,422 tests: 1,396 passed, 26 skipped**, with **zero failures/errors**.

The Linux main-suite skips were **two private Meshtastic replays, one optional external Piglet archive and two opt-in large-file capacity cases**. The **30 / 100 MiB capacity cases subsequently passed 2/2 with no skips** in **261.04 seconds**, using fake HTTP with real parsing, signing, acknowledgement coverage and resumption; peak RSS was approximately **1.06 GiB**. The remaining **three private-fixture checks were not configured in this public-candidate run**. Windows also skips platform-dependent checks. Skipped methods are not counted as passes; earlier real-archive results are separately scoped evidence.

**Firmware software checks:** **29 native SOLO tests** and **51 Piglet source, WebUI, compiled-mock, receipt/lifecycle and backup-helper checks passed**, with **no skips**. These checks do not flash devices or establish new RF reception.

Earlier physical uConsole checks covered **all five SOLO modes**, real Wi-Fi/BLE observations through durable storage, final STOP/ACK ownership, a measured GPS fix, startup backfill/cache and map rendering with and without saved tiles at zooms **6 / 8 / 9**. Full walking-trail continuity, the physical **12-hour cache boundary**, independent RF dwell timing and lossless reception were not established. An intermittent USB-enumeration incident remains unexplained; this release does not claim it permanently fixed.

Piglet's compiled image passed an application-only installation with exact app readback and unchanged protected flash, paired delivery with ACK, absent-collector boots, the full WebUI handover and resumed Wi-Fi/BLE scans. Normal-mode external uploads were checked with offline mocks.

Software fakes, recorded replays and builds do not establish new RF reception on every setup. **No fresh valid GPS fix, forced power loss, real full-SD failure, sustained throughput or multiple-Piglet hardware result is claimed for the final Piglet correction.** The existing PSRAM framework warning remains a recorded limitation. App-only rollback is prepared from verified backups, not claimed physically performed. **AC1200 hardware and software-bridge SOLO compatibility are not established by these C5 checks.**

## 🔄 Install the game, flash devices separately

**In game:** open **TAB → SYSTEM → Update Game**, press **R** to check, then **Enter** to prepare. Read the confirmation: **Y installs; N / Esc cancels beforehand**. One confirmation installs the complete approved version, including skipped fixes. A new-release notice opens the notes; it does not install automatically.

Updaters **0.9.19.1 or later restart the game automatically after successful installation**. Preparation, cancellation and failure do not restart it. If the running updater is **0.9.17, 0.9.18 or 0.9.19**, the first upgrade still requires **one manual relaunch**: after installation, **Enter or Esc** closes the game, then start it again.

**Game updates do not flash either ESP32.** SOLO needs its compatible JanOS extension; Piglet needs its own supported firmware and pairing. Preserve existing device firmware, settings and logs before any deliberate firmware installation. ZIP source downloads do not contain Git history and cannot use the current in-game installer.

📦 [Release 0.9.20](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases/tag/v0.9.20) · 📝 [All releases](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases)

📚 [Game update and recovery guide](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/blob/v0.9.20/docs/UPDATING.md) · 📡 [SOLO firmware and build guide](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/blob/v0.9.20/firmware/wardriving-solo/README.md)

🐷 [Piglet BIN](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/raw/refs/tags/v0.9.20/firmware/piglet-collection/piglet-kg-uconsole-xiao-c5-app.bin) · 🔐 [Piglet SHA256SUMS](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/blob/v0.9.20/firmware/piglet-collection/SHA256SUMS) · 🔌 [Complete Piglet installation, pairing and rollback](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/blob/v0.9.20/docs/PIGLET_COLLECTION_INSTALL.md)

**KEEP THE HUNT. KEEP THE LOOT. LET THE PIG CARRY THE CSV.** 🐕📡🐷
