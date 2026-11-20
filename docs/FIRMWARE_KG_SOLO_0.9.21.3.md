# 📍 Firmware KG/SOLO — launch the recovery helper from the code

**Game microfix 0.9.21.3. Approved firmware remains JanOS 1.7.0 + SOLO.**
Original game and JanOS by **LOCOSP**. KG integration by **KiloGramowy**.

## What the new evidence establishes

The earlier failed attempt has two separate errors:

- `stage=rom-chip-id`: esptool produced no output for 10 seconds; the game stopped before a firmware write.
- `stage=return-to-app`: Python could not open the requested file and exited with code 2. The recovery helper did not execute. This is not evidence that its HardReset code ran and failed.

The photographs do not reveal the entire file path or the full initial-tool output. Do not fill those gaps with guessed paths or diagnoses.

## Before / Now

❌ **Before:** a separately configured data/loot directory was also used as the base directory for `watchdogs/esp32_flash.py`. That location need not contain game code.

✅ **Now:** the helper is selected beside the module containing the running game, using its resolved source location. It does not depend on the data directory or process CWD. No helper is copied to loot, and the existing data-root setting is not changed globally.

The helper must be present and accessible before the installer closes its current connection, changes USB state or starts ROM preflight. If availability fails, read that launch/preparation error; it is not a ROM, flash or physical-reset result.

The software check uses the actual helper with `--help`, including a separate data folder, another CWD, spaces in paths and the normal installation layout. Expected result: exit code 0 and the `--reset-c5-usb-to-app` option. Help parsing does not open a device. The source archive and actual OTA installation must also retain the helper beside `app.py`.

*Code lives with code. Loot gets its own room.* 📍💾

## Update controls and retained safety

Update the game through **TAB → SYSTEM → Update Game → R → Enter → Y**. **N / Esc** cancels before installation. Updaters 0.9.19.1 onward restart after success; running 0.9.17, 0.9.18 or 0.9.19 needs one manual relaunch on its first upgrade.

**Game OTA does not flash an ESP32.** The existing deliberate **Firmware KG/SOLO → R → M → Y** migration to older JanOS 1.7.0 + SOLO stays separate. **N / Esc** cancels its confirmation; **C** can cancel before the write starts. The game-Python **P** dependency repair remains.

The supported XIAO ESP32-C5 revision 1.0 / 8 MiB / approved active ota_0 profile is unchanged. USB identity, port/process ownership, image/header/SHA, ROM revision, flash size, security, actual partition table, active slot and mandatory matching private recovery backups remain checked. Normal downgrade protection, application-only writes, fresh JanOS/SOLO confirmation, reset logic and process supervision are retained.

No erase, force, firmware rebuild or changes to KG/SOLO/Piglet image bytes, bootloader, partitions, OTA selection, NVS, PHY or other application slots are introduced. XP, levels, loot, history, logs, settings and keys are not moved or restored backwards.

Read the existing [migration and private-backup guide](FIRMWARE_KG_SOLO_0.9.21.1.md), [USB recovery guide](FIRMWARE_KG_SOLO_0.9.21.2.md) and [game update/recovery guide](UPDATING.md) for the preserved procedure.

## What remains unknown

The original `rom-chip-id` timeout remains a separate, unresolved physical-device issue. Correcting the path proves that recovery can launch from its intended location; it does not prove successful USB reset, firmware boot or migration on PapaSucio's C5. Do not blame esptool 5.6.0 or the chip based only on that timeout.

**Hardware migration/reset on PapaSucio's device: NOT VERIFIED.** Software tests and real `--help` subprocesses must not be described as physical USB tests.

## One useful record from PapaSucio

After updating, stop ESP32 tools and make one existing R → M → Y attempt only if the same supported device responds and offers that migration. Read the older-base warning. If the first timeout remains, share the complete redacted **[KG-FW]** block from **~/.watchdogs/last_run.log** (**previous_run.log** after relaunch).

That single block identifies the first failing phase and its real output separately from the recovery-launch result. Do not send flash/NVS backups or work through repeated reinstalls and reflashes.
