# 🛠️ Firmware KG/SOLO — dependency repair and deliberate migration

**Game microfix 0.9.21.1. Firmware remains the approved JanOS 1.7.0 + SOLO.**
Original game and JanOS by **LOCOSP**. KG integration by **KiloGramowy**.

> A bigger JanOS number is not a SOLO membership card. The radio still has to
> answer the door. 🐇

## 🎮 Update the game first

Open **TAB → SYSTEM → Update Game → R → Enter**, read the confirmation, then
press **Y** to install. **N / Esc** cancels before installation.

Clients **0.9.19.1 or later restart after a successful game update**. If the
running updater is **0.9.17, 0.9.18 or 0.9.19**, the first upgrade still needs one
manual relaunch: after installation, **Enter or Esc** closes the game; start it
again. Preparation, cancellation and failure do not restart the game.

**Updating the game never flashes an ESP32.** Firmware installation is a
separate manual operation. Player XP, levels, loot, history, pending logs,
settings and keys are not firmware rollback data.

## 🐍 Hypnox: put esptool in the Python that actually runs WDG

❌ **Before:** `esptool>=5.0.0` was declared, but an existing incomplete `.venv`
could still reach the firmware panel without that module. Installing it in a
different Python did not help.

✅ **Now:** the existing setup/launch path checks the game environment too.
For an already running installation updated by OTA, open
**TAB → SYSTEM → Firmware KG/SOLO** and press **P** to repair esptool in the
**detected game interpreter**. This dependency action does not open or flash
the ESP32. It does not start a firmware installation when it finishes.

If repair fails, read its concrete error and the command shown for the detected
interpreter. Copy that exact quoted command if a terminal repair is needed;
do not substitute a different `python`, a system `pip`, or an unrelated venv.
**esptool 5.0.0 remains supported.** A missing, broken or inaccessible tool keeps
firmware operations blocked.

*One Python, one esptool. No more inter-venv hide-and-seek.* 🐍

## 📟 Read the three separate facts

The Firmware KG/SOLO panel separates:

- **Reported JanOS:** the version the connected device actually reports.
- **SOLO interface:** the current device's fresh response to
  `wardrive_solo_caps` — supported, explicitly unrecognized, or unknown.
- **Approved KG package:** the target version, release and own-fork source.

Press **R** to refresh the package and request a read-only SOLO probe. The
ESP32 must be idle; stop an active radio tool with its existing controls first.
A timeout, partial response or disconnection means **unknown**, not “no SOLO”.
A response cached for another USB device or connection is not permission to
migrate. A confirmed capabilities response proves the interface responded;
it is **not a cryptographic attestation of a particular firmware binary**.

## 🐇 PapaSucio: consciously choose the older SOLO base

❌ **Before:** an XIAO C5 reporting JanOS **1.7.5** but rejecting
`wardrive_solo_caps` was blocked because the approved package was **1.7.0**.
The display could also call that reported number “KG” without confirming SOLO.

✅ **Now:** an explicitly selected migration can install the unchanged approved
**JanOS 1.7.0 + SOLO** package after the required device, image and recovery
checks. It does **not** install new firmware based on JanOS 1.7.5.

1. Open **TAB → SYSTEM → Firmware KG/SOLO** after updating the game.
2. Repair esptool with **P** if needed. Stop active ESP32 operations, then press
   **R** for the current device's version/capabilities and the approved package.
3. **Enter** remains ordinary installation/reinstallation. Its normal downgrade
   block stays. Reinstalling the same supported version remains available.
4. For the explicitly unsupported-SOLO case, press **M** to read the migration
   details: reported JanOS, SOLO status, target package/release/source and the
   clear warning that **1.7.5 → 1.7.0 + SOLO lowers the JanOS base**.
5. Press **Y** only if that is the operation you intend. **N / Esc** cancels
   this confirmation without authorizing a flash. **C** can cancel the confirmed
   migration before its firmware write starts. Unknown or stale evidence
   cannot unlock the migration; request a fresh response instead.

This is a specific supported-device migration, not a general “ignore version
checks” switch. Once a confirmed installation has started, **Esc closes the
panel; the operation continues**. Do not unplug or terminate a running flash.

## 🛡️ The hardware and write boundaries stay narrow

The package is only for the supported **Seeed XIAO ESP32-C5 revision 1.0,
8 MiB flash** profile and its approved partition layout. The installer retains
checks for physical USB identity, exclusive port ownership, ROM chip/revision,
security flags, actual partition table, active **ota_0**, image header and
SHA-256. Secure Boot and Flash Encryption must meet the existing supported
state. WROOM, other revisions, a different layout or active ota_1 stay blocked.

The write is the approved **application only**. It does not replace the
bootloader, partition table, OTA selection, NVS, PHY or the other application
slot. There is no full erase, stock fallback, Piglet fallback or automatic
firmware flash. Existing transport, stub selection and write speed are not
changed by this microfix. Firmware and Piglet image bytes remain unchanged.

## 💾 Preflight is not a backup

A read-only ROM check and its small partition/OTA readbacks prove compatibility;
they are **not a complete recovery copy**.

Before the confirmed migration reaches its write, the installer obtains **two
independent complete 8 MiB reads** of that same bound device. Both complete
files must agree in SHA-256 and match the current partition/OTA preflight. It
also saves and verifies the original application sectors that the approved
image can overwrite. For this exact image that is **550 × 4 KiB sectors**,
**2,252,800 bytes**, in the verified ota_0 region.

These durable recovery files are stored separately from the disposable attempt
folder, under the private **`firmware_cache/kg-recovery/`** directory. Its
per-attempt `backup.json` binds the hashes and application recovery scope to the
same device. The full copies include private flash/NVS data and may contain
credentials. **Never commit, upload, publish or attach these backups to an
issue.** Keep the entire recovery directory until recovery is no longer needed.

Backup read, space, permission, cancellation or identity failures stop the
operation before a firmware write. Reading two complete copies can take time;
this is real recovery preparation, not a progress-bar costume.

## 🔄 Recovery is also application-only

If startup is not confirmed, keep the recovery directory and the error.
Do not repeatedly flash, erase the chip or select a different partition to
force success.

For a deliberate rollback, verify the **same physical USB device**, supported
ROM/security state, actual partition table and active ota_0 again. Verify the
backup manifest and original application-sector SHA-256, establish exclusive
ownership of the port, and check that no later firmware change would be
replaced. Restore **only the backed-up application sectors for that verified
layout**. An offset copied from another board, a full-flash restore, old NVS
restoration or a game-data rollback is not this procedure. There is deliberately
no universal write command for unknown layouts here.

The original sectors cover the exact write range of the current package;
other original application bytes were not changed by this app-only write.
Keep both complete snapshots as private recovery evidence. Do not restore old
XP, loot, settings or logs together with firmware code.

## ✅ What counts as success?

An esptool exit code **0** means the write completed. A version number **1.7.0**
means a version line was received. Neither alone proves working SOLO.

The migration remains unconfirmed until the same bound device returns a
**fresh firmware response and a valid `wardrive_solo_caps` response** after
startup. That confirms the SOLO interface, not the cryptographic identity of
the complete running binary. A timeout or missing device leaves a clear
unconfirmed/recovery state.

Software regression checks cover the guards and synthetic recovery files.
**No physical C5 migration or recovery was performed for this microfix, and no
repair of PapaSucio's actual device is claimed.** No new JanOS or Piglet image
was built. See the [existing firmware guide](../firmware/wardriving-solo/README.md)
and [game update/recovery guide](UPDATING.md) for the retained boundaries.
