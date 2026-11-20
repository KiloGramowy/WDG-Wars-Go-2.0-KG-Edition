RELEASE 0.9.21.3 - RIGHT HELPER. RIGHT ADDRESS.

**A small recovery-launch fix: keep the helper with the game code, and keep the loot folder out of the address book.**

Original game and JanOS by **LOCOSP**. KG Edition changes by **KiloGramowy**.

## 📍 Recovery helper — right file, right address

❌ **Before:** with a separate data/loot directory, 0.9.21.2 could look for the recovery program inside that data directory. Python then stopped with “can't open file” and exit code **2**. **The helper never started; this was not a failure inside HardReset.**

✅ **Now:** the helper path is derived from the **running game code**, independently of the data directory or current working directory. The installer checks that the helper is available **before closing the game connection, resetting USB or entering ROM**. A missing recovery program therefore stops the attempt before it can change the device's state.

No scripts are copied into loot. The data directory stays where you chose it; XP, levels, loot, settings, history, logs and keys are not moved.

*The helper has stopped knocking on the treasure chest. Its office is next to the game code.* 📍🐇💾

## 🧪 A real launch check — without a USB adventure

The software regression launches the real helper with **`--help`**, using the production path selection, a separate data directory, another working directory and paths containing spaces. It checks exit code **0** and the **`--reset-c5-usb-to-app`** option. The prepared source/OTA artifact must contain the helper at the location selected by the corrected code.

**`--help` opens no device and proves no physical reset or migration.** Executed software results, explicit skips and artifact hashes are recorded in the release manifest and verification report. Historical totals are not substituted for this fix's checks.

## 📞 The first ROM timeout remains a separate case

The reported **`stage=rom-chip-id` / no output for 10 seconds / Stopped before write** is **not explained or proven fixed by this path correction**. The later **`stage=return-to-app` / Python can't open file / exit 2** identifies the helper-launch bug fixed here. We do not invent the hidden end of the photographed path or missing first-error output.

**No successful physical C5 migration, USB transition, HardReset execution on PapaSucio's board or repair of that device is claimed.** The complete redacted **`[KG-FW]`** block is still needed to investigate the original timeout.

## 🛡️ The safety checks keep their jobs

The existing **XIAO ESP32-C5 revision 1.0 / 8 MiB / approved active ota_0** scope remains. USB identity, exclusive port/process ownership, image/header/SHA, ROM revision, flash size, security, actual partition table, active slot and matching private recovery backups stay checked.

Ordinary downgrade protection stays. **R → M → Y** remains a separate, conscious migration to the approved **older JanOS 1.7.0 + SOLO** base; **N / Esc** cancels confirmation. **C** can cancel before the write starts. Fresh firmware and SOLO replies are still required after installation. The existing reset sequence and supervision are retained; this fix changes the helper's address and the early availability check.

KG/SOLO and Piglet images/manifests keep their **exact bytes**. Writes remain application-only, without full erase or changes to bootloader, partition table, OTA selection, NVS, PHY or other slots. The game-Python **P** repair, esptool **5.0.0** minimum, requirements, stub selection and flash speed are unchanged. No new JanOS or Dark3D firmware is included.

## 🔄 Update the game

Open **TAB → SYSTEM → Update Game → R → Enter → Y**. **N / Esc** cancels before installation. Updaters **0.9.19.1 onward restart after a successful game update**; **0.9.17 / 0.9.18 / 0.9.19** still need one manual relaunch on their first upgrade.

**Game OTA never flashes ESP32.** Firmware installation remains a separate operation you select and confirm.

## 🐇 PapaSucio — one useful follow-up

After updating, stop ESP32 tools and make **one** existing **Firmware KG/SOLO → R → M → Y** attempt only if the same supported device responds and offers that migration. Read the older-base warning. If the first ROM timeout remains, share the **complete redacted `[KG-FW]` block**, not only the last line, from **`~/.watchdogs/last_run.log`** (**`previous_run.log`** after relaunch). That separates the first failing operation and its real output from recovery. Do not send flash/NVS backups or try a series of reflashes.

📦 [Release 0.9.21.3](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases/tag/v0.9.21.3) · 📝 [All releases](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases) · 📚 [Recovery helper and diagnostic guide](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/blob/v0.9.21.3/docs/FIRMWARE_KG_SOLO_0.9.21.3.md)

**RIGHT HELPER. RIGHT ADDRESS. KEEP THE LOOT.** 📍🐇💾
