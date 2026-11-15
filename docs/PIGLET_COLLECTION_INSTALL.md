# 🐷 Piglet Collection — install, pair, deliver

> **0.9.21 DEV host navigation:** open **WARDRIVING → Wardriving Collection**.
> The package/version labels below retain the original 0.9.20 package checkpoint;
> its Piglet firmware, timing and recovery instructions are unchanged. Current
> Dark3D receiver support and firmware limits are described in
> [Wardriving Collection](ota-0.9.21/CHANGELOG_AND_UPDATE_GUIDE.md).

**🚧 WDGWars GO 0.9.20 DEV / UNRELEASED.** Public stable remains **0.9.19.2**.
This is the installation guide for the **separate Piglet KG device**. It does not
flash the embedded uConsole XIAO, replace JanOS or install a public OTA update.
Updating the WDG game and updating Piglet firmware are two separate operations.

## 📦 1. Choose the supported package

Use the files in [`firmware/piglet-collection/`](../firmware/piglet-collection/README.md)
on **`KiloGramowy/WDG-Wars-Go-DEV` → `codex/0.9.20-dev-performance`**. Keep the BIN,
manifest and checksums from the **same commit**, rather than mixing later branch
files with an older downloaded image.

| File | Purpose |
| --- | --- |
| [`piglet-kg-uconsole-xiao-c5-app.bin`](../firmware/piglet-collection/piglet-kg-uconsole-xiao-c5-app.bin) | Application image compiled from the included source candidate |
| [`SHA256SUMS`](../firmware/piglet-collection/SHA256SUMS) | Integrity checks for the package files |
| [`manifest.json`](../firmware/piglet-collection/manifest.json) | Image size/hash, hardware/build profile, source provenance and validation limits |
| [`source/Piglet/`](../firmware/piglet-collection/source/Piglet/) | Complete application sketch used for this candidate |
| [`BUILD.md`](../firmware/piglet-collection/BUILD.md) and [`build.py`](../firmware/piglet-collection/build.py) | Pinned build configuration and reproducible build procedure |
| [`device_backup.py`](../firmware/piglet-collection/device_backup.py) | Read-only, strict hardware/layout/OTA validation and two full backups |
| [`tests/`](../firmware/piglet-collection/tests/) | Software regressions supplied with the source candidate |

| Compatibility item | This candidate |
| --- | --- |
| Physical target | **Seeed XIAO ESP32-C5**, validated ESP32-C5 **revision v1.0** |
| Flash / PSRAM build profile | **8 MiB flash / 8 MiB PSRAM**, QIO flash at 80 MHz, USB CDC enabled |
| Existing partition scheme | **`default_8MB`**, active **app0** on the validated device |
| Image type | **Application only**; not a merged image or full-flash dump |
| Write offset / slot capacity | **`0x10000` / `0x330000` bytes** (3,342,336 bytes) |
| Firmware identifier | Piglet KG **v1.0.1** base display label; final DEV identity comes from the manifest, BIN hash and packaged source hash |
| BIN bytes | **1,970,496** |
| BIN SHA256 | **`bf7919af370c6bfbd2016683927d6cfa623864dc1e38eb7912087ecabda715ad`** |

The package supports only that verified board/profile. USB enumeration alone
cannot identify flash size, PSRAM, partition layout or firmware. If your device
has another board, memory profile, partition table or active application slot,
stop before writing and check compatibility. This candidate does not replace a
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
Download `SHA256SUMS` and `manifest.json` from the same branch commit.

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
- The existing OLED/CSV firmware label is **v1.0.1**. This candidate deliberately
  keeps that base label; it does **not** independently identify the DEV fix. The
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

Both compatible **WDG 0.9.20 DEV host code** and the Piglet image are required.
The game update does not flash Piglet, and the Piglet image does not install the
receiver on uConsole.

1. In WDG open **WARDRIVING → Wardriving Collection**. The screen shows pairing details
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
| **PIGLET KG CONNECTED** | A valid authenticated Collection request and recognized Piglet identity were seen within the last **30 seconds** |
| **WAITING FOR WARDRIVER** | Receiver is running without recent qualifying peer activity |
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

**Software, rerun from this packaged source:** **20 Python source regressions**,
**two Python tests compiling/executing the production upload, HTTP, STA/AP and SD
functions with offline mocks**, **14 real WebUI Node tests**, plus **two C++17
receipt/lifecycle programs** all passed. **No skips.** The tests cover refusal
before external log connection/send and upload-side SD changes, unchanged Normal
upload behavior, mode capture, WebUI labels/routes, STA/AP deadlines/Stay and
storage failures. They do not send real logs to external services.
The read-only device backup helper has **13 additional passing offline fixtures**,
including hardware, partition checksum/layout, OTA CRC/slot and a mocked complete
two-read flow. Its parser also accepted the verified physical flash backup and
its real OTA selection. Parser/mocked checks do not constitute a physical USB run
of the distributed helper.

**Build and physical flash, 11 November 2026:** the included XIAO-C5 sketch
compiled with the pinned Arduino core/libraries. Its compiled partition table
matched the existing device layout. Two independent **8 MiB** pre-update flash
reads matched. The application-only **0x10000 / 460800-baud** write was followed
by an exact complete app readback with the packaged SHA256. Flash outside the
written application's sectors remained unchanged: **`0x0–0x10000`** and
**`0x1f2000–0x800000`**. No bootloader, partition, OTA-data or NVS write was made.

**Application boot and paired Collection:** the corrected Piglet booted, mounted
its SD and delivered pending files through the real paired collector with valid
ACKs. The pending queue cleared and the already-completed migration state stayed
complete. Compared configuration fields were unchanged. All three tested manual
external-upload routes (**`/wigle/uploadAll`**, **`/wdgwars/uploadAll`** and
**`/wigle/upload`**) returned **HTTP 403 / Collection-only**; runtime status
reported **`externalLogUploadsAllowed: false`**. The connected-home-STA WebUI
started with the full 60-second budget and **closed automatically**. During the
**212-second physical capture**, real Wi-Fi scan output and KG BLE scan output
continued after that handover. No assertion, Guru Meditation or SD error was
observed in that capture. This is a short functional check, not a sustained RF
throughput or power benchmark.

**Collector unavailable, router available:** a separate **150-second boot capture**
reported the collector unavailable, issued **no Collection ACK**, retained the
pending file and preserved completed migration/configuration state. Manual
external-upload routes again returned **403**. The full WebUI window closed and
real Wi-Fi/KG BLE scanning resumed without a Guru Meditation or SD error in the
capture. This confirms that router access does not bypass UConsole-only delivery.

**Exact final CLI and repeated no-collector boot:** the documented
**`--before usb-reset --after hard-reset verify-flash`** command confirmed the
application digest and restarted the real application. A further **110-second**
no-collector capture retained the earlier unacknowledged CSV, kept the additional
pending item queued and preserved configuration/completed migration. Direct
upload requests remained **403**; the WebUI deadline closed and Wi-Fi/KG BLE
scan output resumed. No second firmware write was needed for these repeats.
The archive migration completed on **8 November 2026** is earlier evidence; it
is not used as a substitute for the fresh checks above.

**Limits:** Normal-mode external uploads were verified with offline mocks; no real
CSV was sent to external services for this validation. A real full-SD/FAT failure,
forced power loss, a fresh GPS fix, sustained throughput and several physical
Piglets are not confirmed. App-only rollback is prepared from the verified backup,
not physically performed during this successful update.
NMEA reception was observed, but the no-collector captures do not confirm a fresh
GPS fix or that every new scan result was committed as a geotagged CSV row.

A pre-existing framework warning about the **MSPI dummy-cacheline/PSRAM
configuration** also appeared; it was already present in the 8 November capture.
The application subsequently confirmed **8 MiB PSRAM** and active deduplication,
then scanned successfully. The warning is retained as a limitation rather than
treated as a newly established regression or proof of long-term memory stability.

The earlier single-Piglet archive migration does not validate several physical
Piglets. A separate device-ID construction issue remains an explicit
[multiple-device NO-GO](RELEASE_0.9.20.md)
until corrected and independently verified.

For wire/import behavior see [Task 02 implementation and validation](RELEASE_0.9.20.md)
and [protocol 1](PIGLET_COLLECTION_PROTOCOL.md). For the unreleased feature list see
[0.9.20 DEV preview](RELEASE_0.9.20.md).
