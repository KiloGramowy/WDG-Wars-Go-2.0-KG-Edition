# 🐷 Piglet Collection — install, pair, deliver

**WDGWars GO 0.9.20 — separate Piglet firmware installation.**
This guide is for the **separate Piglet KG device**. It does not flash the
embedded uConsole XIAO or replace JanOS. **Updating the WDG game does not flash
Piglet; updating Piglet does not install the WDG game.**

Original Piglet projects and dependencies retain their credits and licenses in
the firmware package. Piglet KG modifications by **KiloGramowy**.

## 📦 1. Choose the supported package

Use the files in [`firmware/piglet-collection/`](../firmware/piglet-collection/README.md)
from the public **[v0.9.20 release](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases/tag/v0.9.20)**.
Keep the BIN, manifest and checksums from that **same tag**, rather than mixing
later branch files with an older downloaded image.

| File | Purpose |
| --- | --- |
| [`piglet-kg-uconsole-xiao-c5-app.bin`](../firmware/piglet-collection/piglet-kg-uconsole-xiao-c5-app.bin) | Application image compiled from the included source |
| [`SHA256SUMS`](../firmware/piglet-collection/SHA256SUMS) | Integrity checks for the package files |
| [`manifest.json`](../firmware/piglet-collection/manifest.json) | Image size/hash, hardware/build profile, source provenance and validation limits |
| [`source/Piglet/`](../firmware/piglet-collection/source/Piglet/Piglet.ino) | Complete application sketch used for this package |
| [`BUILD.md`](../firmware/piglet-collection/BUILD.md) and [`build.py`](../firmware/piglet-collection/build.py) | Pinned build configuration and reproducible build procedure |
| [`device_backup.py`](../firmware/piglet-collection/device_backup.py) | Read-only, strict hardware/layout/OTA validation and two full backups |
| [`tests/`](../firmware/piglet-collection/tests/test_collection_source_regression.py) | Software regressions supplied with the packaged source |

| Compatibility item | This package |
| --- | --- |
| Physical target | **Seeed XIAO ESP32-C5**, validated ESP32-C5 **revision v1.0** |
| Flash / PSRAM build profile | **8 MiB flash / 8 MiB PSRAM**, QIO flash at 80 MHz, USB CDC enabled |
| Existing partition scheme | **`default_8MB`**, active **app0** on the validated device |
| Image type | **Application only**; not a merged image or full-flash dump |
| Write offset / slot capacity | **`0x10000` / `0x330000` bytes** (3,342,336 bytes) |
| Firmware identifier | Piglet KG **v1.0.1** base display label; exact image identity comes from the manifest, BIN hash and packaged source hash |
| BIN bytes | **1,970,496** |
| BIN SHA256 | **`bf7919af370c6bfbd2016683927d6cfa623864dc1e38eb7912087ecabda715ad`** |

The package supports only that verified board/profile. USB enumeration alone
cannot identify flash size, PSRAM, partition layout or firmware. If your device
has another board, memory profile, partition table or active application slot,
stop before writing and check compatibility. This package does not replace a
partition table to make another device match. It upgrades an **existing compatible
Piglet installation**; an application-only BIN is not a first-flash package for a
blank board.

## 🔌 2. Find your Piglet's USB port

Connect **Piglet itself** to the computer with a USB data cable. Close Serial
Monitor and any program using that device. On Windows, compare Device Manager →
Ports before and after connecting it, or list the available ports:

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID, Name, PNPDeviceID
```

The tested device enumerated as **COM6 / Espressif USB, VID `303A`, PID `1001`**. Your COM
number can differ and can change when entering the ROM bootloader. Match the
physical device after each re-enumeration; do not copy another person's COM
number or select the uConsole's embedded ESP32 port. Listing ports does not open a
second serial session.

On Linux, compare `/dev/serial/by-id/` before/after connection and use the matching
Piglet entry. The tested device preparation was performed on **Windows**;
Linux port naming is not a claim that the full procedure was tested on Linux.

## 🔎 3. Verify the files before flashing

Download the BIN with GitHub's **Download raw file** action, not the HTML page.
Download `SHA256SUMS` and `manifest.json` from the same release tag.

```powershell
Get-FileHash -LiteralPath .\piglet-kg-uconsole-xiao-c5-app.bin -Algorithm SHA256
(Get-Item -LiteralPath .\piglet-kg-uconsole-xiao-c5-app.bin).Length
```

Both values must match the manifest and SHA256 entry **exactly**. With a complete
package on Linux, `sha256sum -c SHA256SUMS` checks its listed files. A matching
checksum verifies file identity; it does not prove hardware compatibility. The
manifest records the source/build identity needed for that second check.
Upper/lowercase hexadecimal letters represent the same checksum value.

## 💾 4. Preserve firmware, SD and settings

Before updating your device, retain:

- A verified backup of the current flash, plus the actual partition table and
  active-slot information. Two independent reads with equal sizes and SHA256 are
  the recovery baseline. Keep a separate copy of the **previous app-slot bytes**
  for an application-only rollback.
- The available original CSV from **both `/logs` and `/uploaded`**, including
  filenames and directory locations. The original WebUI can list files through
  `/files.json` and download an individual CSV through `/download?name=...`.
- Your current configuration, including `/wardriver.cfg` if your running firmware
  exposes a safe download. Document any configuration which cannot be exported;
  do not claim a complete SD/configuration backup when only CSV was readable.

Use the existing pause/scan controls to stop current scanning and let any SD write
finish before resetting into the bootloader. Back up closed CSV, and do not run
Collection or a serial monitor concurrently with the backup/flasher.

The SD stays in Piglet. **Do not format it, delete CSV or replace its configuration.**
`/downloadAll` produces a combined export; it is **not** a faithful copy of every
original source file or a complete SD image. Do not start external upload or
cleanup as part of backup. Reading WebUI files does not disable the older
firmware's automatic upload/retention rules; do not reboot it merely to obtain a
backup without accounting for those rules. Once a serial tool needs the port,
close other serial users as required by the tested procedure.

The new image is compiled firmware, **not a dump of the maintainer's device**.
The package contains no personal CSV, credentials, NVS or flash backups.

## 🛠️ 5. Write only the verified application slot

The tested tool is **esptool 5.6.0**. Confirm your executable's
version before using these commands. Keep a stable USB power/data connection.

Run these Windows commands **from `firmware/piglet-collection/`**, using the Python
interpreter in which you deliberately installed **esptool==5.6.0**. The helpers do
not install it or change your environment. Replace **COM6** with the actual port
you identified, and use your own **new, nonexistent** private backup directory
outside the repository:

```powershell
python -m esptool version
python -m esptool image-info .\piglet-kg-uconsole-xiao-c5-app.bin
python .\device_backup.py --port COM6 --output C:\PigletBackups\before-collection --before usb-reset
```

`device_backup.py` loads the official C5 stub **into RAM only**. It verifies
**ESP32-C5, 8 MiB flash, security flags zero, the exact supported partition table
and OTA selection**. It makes two independent **8 MiB** reads in **64 KiB**
requests, rereads the local files and checks equal SHA256. It does not assume
erased OTA metadata: the highest valid selection must resolve to **app0**.
Another slot, security state or layout returns **NO-GO**, without attempting to
change the device to fit this package.

A successful new backup directory contains:

- `flash-read-1.bin`, `flash-read-2.bin`, `flash-reads.json` and `hardware.json`;
- `partitions.bin`, `partitions.json` and `otadata.bin`;
- **`previous-app0-slot.bin`**, the **0x330000-byte** application-only rollback
  image from your device;
- **`backup-verification.json`**, confirming the matched full reads and active
  app0 layout.

These files contain private device data. Keep them out of Git and firmware
packages. **`FAILED.json`, a partial read, unequal hashes or a helper error means
stop; do not proceed to write.** A failed directory is not a successful backup.
`--before no-reset` is only for a device already confirmed in its ROM loader.
The helper always closes the port and **does not boot the application afterward**.

Once the backup reports success, the verified device is still in its loader.
Reconfirm its port if USB re-enumerated, then write **only** the application:

```powershell
python -m esptool --chip esp32c5 --port COM6 --baud 460800 --before no-reset --after no-reset write-flash 0x10000 .\piglet-kg-uconsole-xiao-c5-app.bin
```

This exact write offset and baud were used for the maintained-device installation.
Do not add `erase-flash`, bootloader, partition, OTA or NVS files/offsets, and do
not substitute a merged image. The board's **921600 build/upload-speed option**
does not change this procedure's validated **460800** flashing baud.

Verify the application from a fresh USB-reset loader session, then let the tool
reset into the application:

```powershell
python -m esptool --chip esp32c5 --port COM6 --baud 460800 --before usb-reset --after hard-reset verify-flash 0x10000 .\piglet-kg-uconsole-xiao-c5-app.bin
```

Do not continue after a verification error. The maintained-device validation additionally
performed an exact whole-app readback in 64 KiB requests and compared all flash
outside the app's written sectors with the pre-update backup; those checks are
separate from a successful esptool process exit. The validation section distinguishes
completed physical checks from any remaining command-level checks.

## ✅ 6. Check the new application started

After the documented reset/reconnect, confirm:

- The real Piglet application starts, mounts its existing SD and finds its existing
  settings and files.
- The existing OLED/CSV firmware label is **v1.0.1**. This package deliberately
  keeps that base label; it does **not** independently identify this image. The
  verified BIN/readback SHA256 and manifest/source identity establish which image
  was installed.
- The original OLED and WebUI work, and **Normal Piglet / UConsole Piglet** is
  available in the existing configuration page. In an active UConsole boot,
  WebUI reports Collection-only delivery and disables direct-upload actions;
  `/status.json` reports `externalLogUploadsAllowed: false`. The backend rejects
  direct-upload requests too; the disabled button is not the only enforcement.

A successful esptool write, USB reappearance and a confirmed application boot are
three separate checks. If the application does not start, retain the error/output
and use the rollback section; do not reset SD, NVS or the migration ledger.

## 🔗 7. Pair once using your own uConsole

Both compatible **WDG 0.9.20 host code** and the Piglet image are required.
The game update does not flash Piglet, and the Piglet image does not install the
receiver on uConsole.

1. In WDG open **WARDRIVING → Piglet Collection**. The screen shows pairing details
   before START. **P** hides/shows them. Copy **your own console's** values.
2. Open Piglet's original WebUI at the home-Wi-Fi IP shown on its OLED. If it uses
   its fallback AP, join that AP and open **http://192.168.4.1/**. Preserve the
   original **60-second** AP window; **Stay** extends it to **five minutes**.
3. Open **Configuration → Operating mode / UConsole Collection**. Choose
   **UConsole Piglet** under **Boot mode (reboot required)** and copy the fields:

| Piglet WebUI field | Value shown on your uConsole |
| --- | --- |
| **UConsole hotspot SSID** | **UConsole hotspot SSID** |
| **UConsole hotspot password** | **UConsole hotspot password** (PSK) |
| **Collector IPv4 address** | **Collector IPv4 address**; default **10.72.0.1** |
| **Collector port** | **Collector port**, normally **8765** |
| **Collection pairing token** | **Collection pairing token** |

4. Select **Save Config**. Keep your home-Wi-Fi, GPS, scan profiles and existing
   service keys. Do not reboot yet.
5. On uConsole press **Enter** to START and wait for **Hotspot: ACTIVE**. The
   receiver must be ready **before** Piglet boots its collection search. START
   hides pairing details; **P** shows them if needed.
6. Now use **Save & Reboot** on Piglet. It searches for the configured collector
   for **at most about 10 seconds**. Once connected, transferring/importing the
   queued files can take longer.

The **Collection pairing token** is not a WiGLE Basic Token or WDGoWars API key.
The hotspot password and token are also different fields. This local transfer
does not need external-service credentials or internet access.

> 🐷 Five pairing fields. One delivery address. The pig is not a mind reader.

## 🧾 8. Read progress and wait for the receipt

| uConsole label | Meaning |
| --- | --- |
| **Hotspot: ACTIVE** | Local AP/receiver is ready |
| **ACTIVE PIGLET KG** | A valid authenticated Collection request occurred within the last **30 seconds** |
| **WAITING FOR PIGLET KG** | Receiver is running without recent qualifying peer activity |
| **NOT CONNECTED** | Receiver is inactive |
| **DOWNLOADING CSV: n%** | Received bytes for the current CSV |
| **IMPORT CSV: n%** | File-weighted processing across the whole announced job |

The peer label reports recent application activity, not a guaranteed persistent
radio/TCP connection. A full download is not yet a completed import.

**IMPORT CSV: 100%** requires all announced sources, verified raw contents,
durable exports, completed-file accounting and the required acknowledgements.
The summary lists new Wi-Fi/BLE, skipped duplicates, awarded XP and completion
status. Import runs in the background without showing each record on the live
map or terminal; existing rules settle XP after each complete CSV.

Only a valid final **Collection ACK** confirms delivery to uConsole. It follows
complete receipt, SHA256 validation, durable storage and a successful import or
safe recognition of previously imported data. Files from `/logs` then enter the
existing `/uploaded` archive handover. Already archived files are not moved twice.
Existing `autoDeleteUploadedLogs` and `uploadedLogsToKeep` settings apply after
ACK; no new cleanup schedule is added.

Original Piglet GPS, source time and supported fields stay attached to the data.
The collector never substitutes its own GPS. Exact retransmissions and repeated
imports do not award XP twice. Imported exports remain in the uConsole's existing
**Pending Upload Logs** workflow for **manual** upload. Collection ACK does not
mean WDGWars or WiGLE received anything.

## 📡 9. If the collector is absent or transfer stops

UConsole mode remains selected. Piglet retains unacknowledged files from **both
`/logs` and `/uploaded`** across restart and protects them from automatic deletion.
Old external-upload status never substitutes for a Collection ACK. The next boot
tries Collection again; there is **no continuous search or new retry schedule**.
An interrupted transfer or missing ACK cannot authorize deletion or repeat XP.
Before opening a new UConsole-mode log, a full SD or fewer than **1,024 bytes**
free causes a reported storage error and recording stops. Failed/short Wi-Fi/BLE
row writes also close the failed log and report an error. Piglet does not delete
unacknowledged CSV or overwrite an existing fallback filename to make room.
This cannot provide unlimited recording when the card is full.

After the search/transfer, Piglet follows its home-Wi-Fi/WebUI startup:
fallback **192.168.4.1**, full **60 seconds**, **Stay: five minutes**, then its
existing scan flow and controls. **Only in UConsole mode**, that WebUI deadline
also applies when home STA connected successfully. At its end, Piglet disconnects
STA and starts scanning **even if `autoStartAfterUpload` is off**. This approved
handover prevents home Wi-Fi leaving UConsole mode paused indefinitely. It does
not change Normal Piglet's startup rules, scan profiles, GPS or button behavior.
The timed STA handover is for standalone Piglet operation; saved ESP-NOW
Core/Node boot behavior is retained rather than converted into a new scan mode.
A deliberate button/UI scan pause and a real SD failure still prevent scanning;
the timeout does not override the user or invent a successful write.
Existing GPS/cache/backfill rules still determine when new observations can be
committed to CSV; finding radio results is not proof of a fresh GPS fix.

**UConsole mode sends logs only to its paired uConsole.** Direct WDGWars/WiGLE
uploads are blocked before a connection/data send, including startup, every
Collection outcome and manual WebUI upload requests. Reaching the WebUI through
home Wi-Fi does not grant permission to upload externally. Saved service keys
remain stored for Normal mode.

On uConsole, **Enter** starts/stops; **S** stops. During active or unfinished
Collection **Esc → Yes / No** defaults to **No**. **No** continues; **Yes** stops
the receiver/worker and cancels unfinished unpaid work while retaining completed
imports, saved XP and resumable raw data. With no work active, **Esc** returns.

## 🔄 10. Return to Normal Piglet

Use the original WebUI, choose **Normal Piglet**, **Save Config**, then
**Save & Reboot**. Normal mode uses its existing startup, external uploads,
profiles, GPS, buttons and WebUI. Collection pairing and saved external-service
keys are not erased by this change. Switching mode is separate from restoring an
older firmware. Its original automatic-upload and retention rules become active
again with the saved settings. Do not reset the SD/configuration just to change
the mode.

## 🛟 11. Roll back the application if needed

Use the verified backup from **your own Piglet**, not a maintainer's full-flash
image. Reconfirm the same board, partition table and application slot. Restore
only **`previous-app0-slot.bin`** from your successful backup, at **0x10000**.
Its size must remain **0x330000 bytes** and its SHA256 must match
`app_slot_sha256` in your `backup-verification.json`. If the device is not already
in its loader, the read-only backup helper above can re-enter it and reconfirm
the supported board/layout in a separate new output directory.

From the confirmed loader, with your actual port and previous backup path:

```powershell
python -m esptool --chip esp32c5 --port COM6 --baud 460800 --before no-reset --after no-reset write-flash 0x10000 C:\PigletBackups\before-collection\previous-app0-slot.bin
python -m esptool --chip esp32c5 --port COM6 --baud 460800 --before usb-reset --after hard-reset verify-flash 0x10000 C:\PigletBackups\before-collection\previous-app0-slot.bin
```

Check the previous Piglet application boots and its existing SD/settings remain.
This is an application-only recovery procedure; a physical rollback write is not
claimed merely because a backup was validated.

Do not restore a full flash dump over current NVS/OTA data and do not replace SD
files or player data on uConsole. App-only rollback preserves the partition
table, bootloader and NVS as written during this update; it does not roll back new
CSV or erase Collector receipts. A firmware rollback does not require replaying
an already completed historical migration. If the existing partition layout or
security state is incompatible, stop instead of substituting an erase/repartition.

## 🧪 12. What was actually verified

**Build and software:** the included XIAO-C5 sketch compiled with the pinned
Arduino core and libraries. The compiled partition table matched the supported
layout. Source regressions, offline production-function mocks, WebUI tests,
portable receipt/lifecycle programs and read-only backup-helper fixtures cover
mode isolation, ACK/retention, deadlines, storage failures and hardware checks.
The public release notes record the final candidate's test results separately.

**Earlier physical validation of this exact BIN:** two independent 8 MiB backups
matched before the application-only **0x10000 / 460800-baud** installation. Exact
app readback matched the packaged SHA256. Flash outside written app sectors,
including bootloader, partition table, OTA data and NVS, remained unchanged.

The application mounted its existing SD, retained compared configuration and
completed paired delivery with real ACKs. Repeated no-collector boots retained
pending CSV. Direct external-upload routes returned **403 / Collection-only**.
The full home-Wi-Fi WebUI window closed and real Wi-Fi/KG BLE output resumed,
including with `autoStartAfterUpload=false`. These are short functional checks,
not sustained throughput or power measurements.

Normal-mode external uploads were checked with offline mocks. A real full-SD/FAT
failure, forced power loss, a fresh valid GPS fix and sustained throughput remain
unconfirmed for this firmware correction. NMEA input alone does not prove a fresh
fix or a newly geotagged CSV. App-only rollback is prepared from verified backups;
a successful backup does not establish that a physical rollback was performed.

The pre-existing **MSPI dummy-cacheline/PSRAM configuration** warning remains a
limitation. The application subsequently confirmed 8 MiB PSRAM and scanned, but
that does not prove long-term memory stability.

### Multiple physical Piglets — unsupported in this release

Validation used **one physical Piglet**. The current device-ID construction does
not establish a collision-safe identity for several C5 Piglets. Do not use
several physical Piglets with this receiver until that identifier is corrected
and independently verified. No identifier migration is included in this image.

See the [Collection protocol](PIGLET_COLLECTION_PROTOCOL.md),
[firmware package](../firmware/piglet-collection/README.md) and
[full 0.9.20 release notes](UPDATE_PREVIEW.txt) for the public contracts and limits.
