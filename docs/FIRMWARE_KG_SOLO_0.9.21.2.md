# 📞 Firmware KG/SOLO — USB recovery and useful evidence

**Game microfix 0.9.21.2. The approved firmware remains JanOS 1.7.0 + SOLO.**
Original game and JanOS by **LOCOSP**. KG integration by **KiloGramowy**.

This follow-up repairs confirmed recovery and USB hand-off defects from the
0.9.21.1 installer. It does not establish why the first ROM connection timed
out on the affected physical C5, and it does not claim that PapaSucio's device
has been repaired.

## 🎮 Update the game first

Use **TAB → SYSTEM → Update Game → R → Enter → Y**. **N / Esc** cancels before
installation. Updaters **0.9.19.1 onward restart after success**. Updaters
**0.9.17 / 0.9.18 / 0.9.19** still need one manual relaunch on the first upgrade.

Game updates do not flash ESP32. Installing firmware is still a separate
choice. The existing **Firmware KG/SOLO → P** dependency repair stays in the
game's own Python. **esptool 5.0.0 remains supported**, the minimum and
`requirements.txt` are unchanged, and 5.6.0 has not been identified as the
cause of the reported failure.

## 🔌 What changed in the failed-attempt path

❌ **Before:** recovery after a failed initial ROM synchronization still used
a no-reset ROM command. That assumed the very connection that might not have
been established. Its output callback also replaced every real tool line with
the same generic restart-check text.

✅ **Now:** the installer can request a native USB application reset without
first synchronizing with ROM. It runs through the game's interpreter under
the existing process supervisor. The request has no flash read, write or erase
operation. It is a request to boot, not evidence of a successful boot.

The bound native USB connection sets DTR/RTS idle before opening. Stable
enumeration of the same physical device is awaited, and an uncertain close
retains the existing connection reference. If a tool process has not been
confirmed stopped, the installer retains the port and does not reconnect or
start another process on top of it.

The supported USB reset selection is explicit; esptool's default already has
native USB reset support. A flag change alone is not presented as a physical
fix. Timeouts have not simply been increased to conceal the failure.

## 🧾 Read the stage and keep the original error

Redacted **`[KG-FW]`** records use the existing game log:

- **`~/.watchdogs/last_run.log`** for the current run.
- **`~/.watchdogs/previous_run.log`** if the game was relaunched once afterwards.

The records retain the stage, first installation error, actual merged tool
stdout/stderr and a separate recovery result. Final output fragments are kept
on supervised failure too. Private device paths and USB identifiers are
redacted; flash/NVS backup contents are not logged.

Distinguish a stop **before writing**, a failure **during writing**, and missing
**startup confirmation**. An “esptool produced no output” message is a game
supervisor observation, not a complete diagnosis from esptool. Repeated generic
restart-check lines from 0.9.21.1 did not prove that multiple resets occurred.
OUTPUT alone was held in memory; do not assume that an earlier complete attempt
is recoverable from the old log.

## ✅ What confirms the application

A USB port appearing is not a successful boot. After a stopped pre-write
attempt, the same bound device must report a fresh, matching previous JanOS
version before the game calls that application responsive.

After an actual KG installation, the game still requires both a fresh firmware
response and a valid fresh **`wardrive_solo_caps`** response from that device.
Exit code zero, a reset request, a port name and a version banner alone do not
prove working SOLO. Capabilities confirm an interface response, not the
cryptographic identity of a binary. A panel showing **unknown** is not permission
to substitute an old cached version or mark the attempt successful.

## 🛡️ Existing migration and data boundaries

Read the [0.9.21.1 migration and recovery guide](FIRMWARE_KG_SOLO_0.9.21.1.md)
for the retained **R → M → Y** conscious migration, older **JanOS 1.7.0 + SOLO**
warning, **N / Esc** confirmation cancellation, and **C** cancellation before
writing. Ordinary downgrade protection and same-version reinstall remain.
Unknown, partial, timed-out, stale or disconnected SOLO evidence does not
authorize migration.

Only the approved **XIAO ESP32-C5 revision 1.0 / 8 MiB / active ota_0** profile
and layout remain supported. Same-device USB identity, ROM revision, image
header/SHA, flash size, security, actual partitions, slot and exclusive port
ownership must pass. The two independent, matching full **8 MiB** recovery reads
and the original application-sector copy remain required before migration.
Keep those recovery files private and local.

The write remains the approved application range only. No full erase, force,
partition/security bypass, bootloader/table/OTA-selection/NVS/PHY replacement,
or automatic retry flash is added. Stub selection and write speed are unchanged.
KG/SOLO and Piglet firmware images/manifests keep their exact bytes. No new
JanOS base or Dark3D firmware is supplied. Player progress, logs, settings and
keys are not firmware rollback data.

See the [firmware application-only scope](../firmware/wardriving-solo/README.md)
and [game update / code-only rollback guide](UPDATING.md).

## 🐇 One targeted follow-up if it still stops

After updating, stop ESP32 tools and make **one** existing
**Firmware KG/SOLO → R → M → Y** attempt only if the same supported device
responds and offers the migration. Read the older-base warning. If it still
stops, share **only the redacted `[KG-FW]` block** from `last_run.log` or, after
one relaunch, `previous_run.log`.

That record separates the first failing phase and real tool diagnostic from
the recovery result. Do not send private flash/NVS backups, a full personal log
or a series of repeated reflash attempts.

Software regression results and explicit skips are recorded separately in the
release verification materials. **No physical C5 USB transition, migration,
flash/recovery, or repair of PapaSucio's board has been verified for this
microfix.**
