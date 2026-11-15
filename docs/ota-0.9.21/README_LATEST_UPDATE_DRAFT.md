<!-- 0.9.21 banner source: replace the previous Latest update above the retained feature tour. -->
<!-- latest-public-update:start -->
## 🚀 Latest update — 0.9.21

**🗺️ LET THE MAP FOLLOW. 🐇 SPOT THE RABBIT. 💾 KEEP THE LOOT.**

*The 0.9.20 Piglet brought home the CSV. Now the map wants to join the road trip. Your CPU has already done enough paperwork.* 😏

❌ **Before:** new streets needed manual map downloads, WDGWars territory stayed off the map, and busy Wi-Fi/BLE work could make the screen wait.

✅ **Now:**

- 🗺️ **Auto Map + Turf:** **TAB → SYSTEM → Auto Map + Turf → Enter** makes both maps follow **fresh measured GPS**. Your choice is remembered (**default OFF**). Download **10 km** around a center, consider the next area after **3 km**, refresh nearby gang sectors after **five minutes**. Manual **Download Map** stays. *The map finally packed its own suitcase.*
- 🟩 **Gang turf on the map:** saved, gang-colored sector **outlines** at zoom **9+**, with **OWN / TURF** for an occupied measured-GPS sector and **an asterisk (*)** for a snapshot over five minutes old. No new capture rule or free XP for walking into somebody else's square. *Your shoes are not a capture API.*
- 🐇 **White Rabbit:** open, unclaimed, unexpired server targets appear as **white bunny markers** on the existing map. The existing Wars Sync key powers reads at startup and **at most every 60 seconds**, independent of Auto Map/GPS. Account-bound cache is last-known data, not a new capture feature. *The bunny brought its coordinates.*
- 🏅 **Portal Badges:** **PLUGINS → Watch Dogs Go Wars Sync → Portal Badges → Enter** opens the complete returned server-owned collection, even badges without native icons. Scroll with arrows/Page/Home/End; **R** refreshes by read-only GET, **Esc** returns. Native achievements and XP remain separate. *A bigger trophy cabinet, not imaginary trophies.*
- ⚡ **Smoother Wi-Fi + BLE for everyone:** scan presentation, idle Collection checks, compact saves and serial bursts cause less GUI waiting — **no extra switch**. Real **START/STOP ACK**, scan timing, data formats and XP rules stay. A complete valid message after damaged input may be recovered, but missing packets are never invented. *The map no longer queues behind a CSV.*
- 🗑️ **Pending Upload Logs, your call:** visible **SENT / NOT SENT**. Select one closed CSV: **SENT + D** deletes immediately; **NOT SENT + D** warns it was not uploaded, then **Y** deletes or **N / Esc** keeps it — including no-GPS exports that cannot upload. XP, progress and receipts stay; active/upload locks and **Auto-clean rules stay unchanged**. *Delete the paperwork, keep the loot.*
- 🧹 **Auto-clean follows the receipt:** **Pending Upload Logs → A** keeps **Keep all / 1 / 5 / 15 / 31 days / custom**. **After 5 days** counts from the first complete confirmed upload, not the session date, restart or file timestamp. Rechecking the same unchanged file/account does not reset it; all batches need ACKs. Legitimate duplicates/no-GPS leftovers no longer strand a fully confirmed supported upload. Legacy flags cannot invent age; unsent/no-GPS-only and invalid/unreadable data are not aged into removal by this policy. *The receipt owns the calendar.*
- 🛡️ **Firmware KG/SOLO:** **SYSTEM → Firmware KG/SOLO** checks **our KG Edition fork** and shows detected/latest KG. **R** refreshes; **Enter** begins a compatible, verified **manual** install/reinstall. **No automatic flash, no stock/Piglet fallback.** Firmware **KG 1.7.0** is separate from game version **0.9.21**.
- 📦 **Wardriving Collection:** keeps Piglet KG CSV and adds supported Dark3D eleven-column WiGLE `.log` imports, typed peer status, unchanged PC1/durable ACK and deduplication. This is host compatibility. Modified Dark3D Collection firmware awaits its own approved public release; no such image is supplied here. **Firmware documentation and future `.bin`: [Dark3D C5 UConsole](https://github.com/KiloGramowy/Dark3D-C5-Wardriver-UConsole) (currently private; separate Release approval required).** *Two dialects, one receipt desk.*
- 💾 **Your streets and loot stay:** saved tiles/turf remain available for cached viewing; turning Auto Map OFF does not delete them. A no-fix restart can restore the last saved automatic **view**, never fake GPS. Existing SOLO, Piglet Collection, **720-minute GPS cache**, uploads, settings and **15 / 10 / 1 XP** rules stay. Tile downloads need the existing **Stadia key**; sectors need the existing **Wars Sync key**.

**Default street-basemap credits:** © [Stadia Maps](https://stadiamaps.com/attribution/) · © [OpenMapTiles](https://openmaptiles.org/) · © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).

📸 **0.9.21 map previews:** [Auto Map + Turf](../media/0.9.21-automap-sectors.png) · [White Rabbit](../media/0.9.21-rabbit-world.png). *Illustrative images, not a physical device test.*

*Piglet keeps the CSV. SOLO keeps the hunt. The map keeps up. Your loot stays yours.* 🐷🐕

**Fresh RC results are recorded separately by exact commit; the counts below are historical and precede the final combined candidate.**

**Receipts, not fairy tales:** the earlier shared scan/Collection baseline passed **1,679 Linux tests (0 skipped)** and **1,665 Windows tests (14 platform/root skips; all passed on Linux)**. A bounded real uConsole run had no sampled second-long GUI gap (**151.93 ms maximum**) and three closed sessions reconciled **1,150 received = saved = STOP-confirmed** observations with no observed gaps in those sessions. Real map/turf, Rabbit and Portal Badges UI checks passed. **Physical 3 km movement, full network-loss recovery, a new Piglet transfer and firmware flashing remain unverified.** These are finite checks, not a universal FPS or lossless-RF guarantee.

**[📦 Release 0.9.21](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases/tag/v0.9.21)** · **[📝 All releases](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases)** · **[📚 Update guide](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/blob/main/docs/UPDATING.md)**

🎮 **Fully open source, with the existing Git/source updater:** **TAB → SYSTEM → Update Game → R → Enter**, then **Y** to install or **N / Esc** to cancel. Updaters **0.9.19.1+** restart after success; running **0.9.17 / 0.9.18 / 0.9.19** needs one manual relaunch after its first upgrade. **Game OTA never flashes JanOS or Piglet.**

---
<!-- latest-public-update:end -->
