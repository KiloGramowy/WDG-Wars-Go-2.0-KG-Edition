# 🐷 Piglet KG Collection — pocket the pig, deliver the loot

**Small scanner. Real CSV. One delivery address.** 📡📦

**WDG Wars Go 0.9.21 integration documentation.** This guide covers the existing
Piglet KG Collection package and game-side integration. Game and Piglet firmware
are released separately; follow the [game Releases](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases)
and package [manifest](manifest.json) for current availability.

This firmware is for the **separate Seeed XIAO ESP32-C5 Piglet**. It does not
replace the embedded uConsole XIAO's JanOS/SOLO firmware. Updating the WDG game
does **not** automatically update Piglet.

## 🔌 README → INSTALL

**[🐷 INSTALL — open the self-contained installation guide](../../docs/PIGLET_COLLECTION_INSTALL.md)**

The INSTALL link opens the self-contained installation guide. The complete,
self-contained instructions and firmware also remain directly available here:

**[📚 Full USB installation, verification, pairing and rollback guide](../../docs/PIGLET_COLLECTION_INSTALL.md)**

**[💾 DOWNLOAD BIN](piglet-kg-uconsole-xiao-c5-app.bin?raw=1)** · **[🔐 SHA256SUMS](SHA256SUMS)** · **[📋 Manifest](manifest.json)** · **[🚀 Full 0.9.20 release description](../../docs/RELEASE_0.9.20.md)**

> 🐷 Take Piglet for a walk. Bring home the CSV. Your uConsole can skip leg day.

## 📦 What changed?

❌ **Before:** UConsole Piglet could return from Collection to the original
external-upload paths after connecting to home Wi-Fi; WebUI upload requests could
also reach them.

✅ **Now:** **UConsole Piglet delivers CSV only to its paired uConsole**. Shared
backend checks reject direct external log upload **before connecting or
sending**, including manual WebUI routes. Existing service credentials remain
stored. **Normal Piglet keeps its original uploads, configuration and startup.**

UConsole mode still searches for the collector for **at most about 10 seconds**
at boot. Transfer can take longer. Collection then hands over to the full
**60-second WebUI window**, with **Stay: five minutes** retained. For standalone
UConsole operation, a connected home STA disconnects when that window ends and
the existing scan flow resumes, including when `autoStartAfterUpload` is off.
Deliberate user pauses and saved Core/Node boot behavior are preserved.

Unacknowledged CSV in **`/logs` and `/uploaded`** stay queued across restarts and
protected from automatic deletion. Only verified Collection **ACK** permits the
existing archive/retention handover. Missing the collector adds no continuous
search, retry schedule or automatic mode switch. A full SD reports a storage
problem rather than deleting pending files to make room.

> 📬 The pig has a delivery address. It no longer takes a scenic route through
> somebody else's upload button.

## 🔧 Install the correct image — not the other ESP32

**Read the [complete USB installation, verification, pairing and rollback
guide](../../docs/PIGLET_COLLECTION_INSTALL.md) before writing firmware.**

The supported profile is **XIAO ESP32-C5, 8 MiB flash / 8 MiB PSRAM**, existing
`default_8MB` partition layout, **app0 at `0x10000` with `0x330000` bytes capacity**.
This is an **application-only upgrade of a compatible existing Piglet**, not an
initial installation for a blank board. Identify your own chip, flash, partition
table and active slot first. Do not infer them from the USB port name.

The app-only procedure preserves bootloader, partition table, OTA metadata, NVS
and the SD archive/configuration. It does not format SD or perform a full erase.
The image is compiled from the included source, **not a device flash dump**.

| Download / source | Purpose |
| --- | --- |
| [`piglet-kg-uconsole-xiao-c5-app.bin`](piglet-kg-uconsole-xiao-c5-app.bin?raw=1) | Ready application image for the supported profile |
| [`SHA256SUMS`](SHA256SUMS) | Package integrity checks |
| [`manifest.json`](manifest.json) | Exact BIN size/SHA256, source hashes, build profile and validation record |
| [`source/Piglet/`](source/Piglet/) | Complete application sketch used to build the candidate |
| [`BUILD.md`](BUILD.md) · [`build.py`](build.py) | Pinned build instructions and source-verifying build helper |
| [`device_backup.py`](device_backup.py) | Strict read-only hardware/layout/OTA preflight, two independent backups and app-only rollback extraction |
| [`tests/`](tests/) · [`baseline/`](baseline/) | Offline regression tests and their portable comparison sources |
| [`LICENSE`](LICENSE) | Preserved upstream license |

Download these files from the **same DEV commit**. The manifest identifies the
candidate as **Piglet KG v1.0.1 + UConsole Collection upload isolation candidate
2026-11-11**. Its OLED/CSV base label intentionally remains **v1.0.1**; the BIN
hash, manifest and source identity distinguish the DEV image.

## 🔗 Pair, start, collect

On uConsole open **WARDRIVING → Piglet Collection** and copy **your own** SSID,
hotspot password, collector IPv4 address, port and pairing token into the five
matching Piglet WebUI fields. **Start the uConsole receiver first** and wait for
**Hotspot: ACTIVE**, then restart Piglet in **UConsole Piglet** mode.

**DOWNLOADING CSV** measures received bytes. **IMPORT CSV: 100%** requires the
completed job, durable exports, completed-file accounting and required ACKs.
Imported CSV enter the existing uConsole **manual upload** workflow. Collection
ACK confirms local delivery, not an upload to an external service.

To restore ordinary Piglet operation, select **Normal Piglet → Save Config →
Save & Reboot**. Do not reset settings or erase service credentials.

📚 [Full installation and pairing guide](../../docs/PIGLET_COLLECTION_INSTALL.md)
· [Host setup and validation](../../docs/RELEASE_0.9.20.md)
· [0.9.20 DEV preview — SOLO and Piglet first](../../docs/RELEASE_0.9.20.md)
