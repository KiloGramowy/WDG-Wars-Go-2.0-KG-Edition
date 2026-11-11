# 🐷 Piglet KG Collection — pocket the pig, deliver the loot

**Small scanner. Real CSV. One delivery address.** 📡📦

Firmware supplied with **WDGWars GO 0.9.20** for the **separate Seeed XIAO ESP32-C5 Piglet**. It does not replace the embedded uConsole XIAO's JanOS/SOLO firmware. Updating the WDG game **does not flash Piglet**.

## 🔌 Start with the complete installation guide

**[📚 USB installation, file verification, pairing and rollback](../../docs/PIGLET_COLLECTION_INSTALL.md)**

**[💾 DOWNLOAD BIN](piglet-kg-uconsole-xiao-c5-app.bin?raw=1)** · **[🔐 SHA256SUMS](SHA256SUMS)** · **[📋 Manifest](manifest.json)** · **[🚀 Full 0.9.20 release notes](../../docs/RELEASE_0.9.20.md)**

The GitHub guide is self-contained: identify the physical board, preserve existing firmware and SD files, verify the image, write only the supported app slot, then pair using your own uConsole's values.

## 📦 Two modes, one clear destination

❌ **Before:** WDG had no paired Piglet Collection receiver, and connecting to home Wi-Fi used Piglet's configured external-upload workflow.

✅ **Now:** choose **Normal Piglet / UConsole Piglet** in the existing WebUI. **Normal Piglet retains its original behavior. UConsole Piglet sends CSV only to its paired uConsole.** Backend checks refuse direct external log uploads before connection/send, including manual WebUI routes. Saved external-service credentials remain available for Normal mode.

UConsole Piglet searches for the collector for **at most about 10 seconds at boot**. Transfer can take longer. It then enters the full original WebUI stage: home Wi-Fi or fallback **192.168.4.1**, **60 seconds**, and **Stay: five minutes**. Standalone UConsole operation disconnects home Wi-Fi and starts the existing scanning flow when that window ends, even with `autoStartAfterUpload=false`. Deliberate pauses and saved Core/Node boot behavior retain their existing rules. No new gestures or PAUSE controls are added.

Unacknowledged CSV in **`/logs` and `/uploaded`** stay protected across restarts. One-time migration covers the available historical archive too. Only verified Collection **ACK** permits the existing archive/retention handover; previous external-upload status does not count. Existing `autoDeleteUploadedLogs` and `uploadedLogsToKeep` values stay. A full SD reports a storage problem rather than deleting pending data.

> 🐷 The pig has a delivery address. It does not need a career in international logistics.

## 🔧 Supported image — the other ESP32, not JanOS

| Compatibility item | Supplied image |
| --- | --- |
| Board | **Seeed XIAO ESP32-C5**, validated revision **v1.0** |
| Memory | **8 MiB flash / 8 MiB PSRAM** |
| Existing layout | **`default_8MB`**, active **app0** |
| Image / offset / capacity | **Application only / `0x10000` / `0x330000` bytes** |
| BIN length | **1,970,496 bytes** |
| BIN SHA256 | **`bf7919af370c6bfbd2016683927d6cfa623864dc1e38eb7912087ecabda715ad`** |
| Base display version | **v1.0.1**, deliberately unchanged; identify the image by its hash and manifest |

This upgrades a compatible existing Piglet. It is **not a blank-board installer** or a reason to replace a partition table. The documented app-only procedure preserves bootloader, partitions, OTA data, NVS and the SD archive/configuration. The BIN is compiled firmware, **not a device dump**. No full erase or SD formatting is part of installation.

**One physical Piglet is validated. Multiple physical Piglets are unsupported pending correction and verification of the device identifier.** Full compatibility and validation limits are in the [installation guide](../../docs/PIGLET_COLLECTION_INSTALL.md#multiple-physical-piglets--unsupported-in-this-release).

## 🔗 Pair once, collect later

1. On uConsole open **WARDRIVING → Piglet Collection**. **P** shows/hides pairing details.
2. In Piglet's existing WebUI choose **UConsole Piglet**. Copy your own console's **hotspot SSID, hotspot password, collector IPv4 address, collector port and Collection pairing token** into the five matching fields.
3. **Save Config**. Start the uConsole receiver and wait for **Hotspot: ACTIVE**, then use **Save & Reboot** on Piglet.
4. **DOWNLOADING CSV** measures the current transfer; **IMPORT CSV** measures processing. Final **100%** waits for durable storage, completed-file accounting and required ACKs.

Imported exports enter uConsole's existing **manual upload** workflow. Collection ACK confirms local delivery, **not an external upload**. Return to ordinary Piglet behavior through **Normal Piglet → Save Config → Save & Reboot**; do not erase saved settings to change modes.

## 🛠️ Package contents and reproducibility

| File | Purpose |
| --- | --- |
| [`piglet-kg-uconsole-xiao-c5-app.bin`](piglet-kg-uconsole-xiao-c5-app.bin?raw=1) | Supported application image |
| [`SHA256SUMS`](SHA256SUMS) · [`manifest.json`](manifest.json) | Image identity, hardware/build profile and source hashes |
| [`source/Piglet/`](source/Piglet/Piglet.ino) | Complete application sketch |
| [`BUILD.md`](BUILD.md) · [`build.py`](build.py) | Pinned build configuration and source-verifying helper |
| [`device_backup.py`](device_backup.py) | Read-only hardware/layout/OTA validation, two full backups and rollback-slot extraction |
| [`tests/`](tests/test_collection_source_regression.py) · [`baseline/`](baseline/Piglet.ino) | Offline regressions and portable comparison sources |
| [`LICENSE`](LICENSE) | Preserved upstream license |

Download the BIN, manifest, checksums and sources from the **same v0.9.20 tag**. The OLED/CSV label remains **Piglet KG v1.0.1**; a version label alone cannot distinguish the image. See [the complete guide](../../docs/PIGLET_COLLECTION_INSTALL.md) for verification, the tested **460800-baud** app-only procedure and prepared rollback.
