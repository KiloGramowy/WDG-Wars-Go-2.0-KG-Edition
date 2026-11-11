# 📡 JanOS WARDRIVING SOLO — XIAO ESP32-C5

This is the compatible **JanOS 1.7.0 + SOLO** extension for **WDGWars GO 0.9.20**.
Original JanOS/projectZero by **LOCOSP**. SOLO preserves the existing firmware
commands and adds the confirmed `wardrive_solo` UART contract.

> One radio, orderly turns. The cyberdeck has retired its tiny traffic policeman. 🚦

**Before:** the game had no firmware-owned FULL/KG SOLO schedule.
**Now:** choose FULL SPECTRUM, KG SETTINGS, BLE SOLO, or either Wi-Fi profile
with BLE from the game's WARDRIVING menu. The host owns GPS and Linux storage;
this extension requires no SD card inside the embedded XIAO.

## 📦 What is included

- [`janos-1.7.0-solo-xiao-c5-app.bin`](janos-1.7.0-solo-xiao-c5-app.bin): the
  **unchanged, previously tested compiled application**, 2,250,896 bytes.
- SHA256: **`e3dff6b534ff8d7712d37e181a3d2d1e30d3672877b6925be2a4bf27c8a989c5`**.
- [`manifest.json`](manifest.json), [`SHA256SUMS`](SHA256SUMS), the exact source
  patch, added sources, pinned build inputs and upstream MIT license.
- [`BUILD.md`](BUILD.md): public source reference, exact configuration and
  reproduction/validation procedure.
- [`CONTRACT.md`](CONTRACT.md): commands, ACKs, framing and approved schedule.
- [SOLO user and compatibility guide](../../docs/WARDRIVING_SOLO_0.9.20.md).

The firmware's CLI still reports **1.7.0**. Its inherited application descriptor
reports **1.6.9**; neither number proves SOLO support. The game requires a real,
fresh `wardrive_solo_caps` response and confirmed START/STOP events.

## 🎯 Compatibility is specific

The tested profile is **Seeed XIAO ESP32-C5 revision 1.0**, **8 MiB flash**, DIO
80 MHz, USB Serial/JTAG console, with the exact layout in
[`build-inputs/partitions.csv.example`](build-inputs/partitions.csv.example). The tested
application slot is **ota_0 at `0x20000`, capacity `0x3F0000`**.

**This is not Piglet firmware, a WROOM image, a full-flash dump or an installer.**
Do not write it to the Piglet app offset `0x10000`, substitute it for a
bootloader/partition image, change OTA selection to make it fit, or flash a
software bridge/PTY. WROOM and Linux/AC1200 SOLO are not validated by this image.

**Update Game updates game files; it does not flash the ESP32.** Stock JanOS
without this extension leaves SOLO unavailable with an explanatory message.
The existing ESP32 FLASH wizard downloads stock upstream JanOS; it does not
install this SOLO application and can replace the extension. Do not use its
stock three-image procedure for this app-only package.

## 🛠️ Installation and recovery boundaries

Before an application-only installation, close WDG normally, save current data,
and establish exclusive ownership of the **physical C5 port**. Identify the
chip/revision, security flags, real flash size, partition table and active boot
slot. Obtain and verify **two complete independent backups** of that same device
and preserve the original application sectors separately for rollback.

Proceed only when the image SHA, 8-MiB layout, slot and security state have been
verified. The tested write used **460800 baud** and an official ESP32-C5 RAM stub,
writing only the application at `0x20000`, with readback and unchanged-region
checks. The flash operation touches 550 4-KiB sectors, `[0x20000, 0x246000)`.
It must preserve the bootloader, partition table, OTA selection, NVS, PHY and
other application slot. A full erase or full-flash restore is not this procedure.

There is deliberately no universal write command here: changing a device with
another active OTA slot or layout requires its own verified installation plan.
The included BIN is not permission to replace a different device's partition
scheme. A successful write, USB re-enumeration and confirmed JanOS application
startup are **three separate checks**. A generic reset alone did not always
restore USB enumeration in prior testing; do not promise an automatic recovery
or repeatedly flash a device that has not restarted.

For rollback, restore only the backed-up application sectors on the **same
verified device**, after checking that no later firmware change would be
replaced. Do not restore old WDG loot, XP, history or settings together with code.
Never distribute device backups, NVS or OTA metadata in a public package.

## 🧪 Evidence and limits

**Software:** 10 native core + 19 adapter tests passed again for this release.
The existing complete **ESP-IDF 6.0.1 / GCC 15.2.0** XIAO build passed compilation,
linking and partition-size checks. Source, patch and compiled-image hashes were
reverified; a new vendor rebuild or physical flash was not performed for release.

**Hardware:** prior tests on one XIAO-C5/uConsole confirmed all five SOLO modes,
real Wi-Fi/BLE observations, final STOP/ownership ACKs, durable host records,
GPS fix/cache and startup backfill. This does not prove RF losslessness, every
channel's physical dwell, WROOM/AC1200 compatibility, a full 12-hour GPS-cache
boundary or a complete moving field track. An intermittent USB-enumeration
failure remains a documented limitation.
