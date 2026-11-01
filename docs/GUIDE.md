# 📖 Watch Dogs Go — Inherited Feature Guide

This guide retains LOCOSP’s feature reference, with the current menu names, keys and configuration paths corrected below. Companion hardware and inherited features are not all requalified by the fork’s validation. For current mesh, AUTO GPS, scanning, upload, shutdown and updater behavior, start with the [README](../README.md), [feature guide](FEATURE_GUIDE.md) and [recorded checks](VALIDATION.md).

Open-world hacking RPG for the ClockworkPi uConsole with real WiFi/BLE scanning, attacks, wardriving, mesh networking, and handshake capture. Frontend for ESP32-C5 running [projectZero](https://github.com/LOCOSP/projectZero) firmware.

---

## Installation

### Requirements

| Component | Description |
|-----------|-------------|
| **ClockworkPi uConsole** | Handheld Linux console (ARM) |
| **ESP32-C5 XIAO** | Seeed Studio XIAO ESP32-C5 with [projectZero](https://github.com/LOCOSP/projectZero) firmware |
| **Python 3.10+** | Runtime |
| **AIO v2** *(optional)* | [HackerGadgets](https://hackergadgets.com/products/uconsole-aio-v2) extension board (GPS, LoRa SX1262, SDR, USB hub) |

### Quick Start

This review repository is private and needs GitHub access. Use a separate checkout; preserve the current installation and its player/configuration data. The [README installation section](../README.md#-hardware-and-installation) is the maintained entry point.

```bash
git clone --branch codex/wdg-go-wars-release --single-branch https://github.com/KiloGramowy/esp32-watch-dogs.git
cd esp32-watch-dogs
bash setup.sh       # creates venv and installs dependencies in this separate checkout
./run.sh            # obtains sudo, auto-detects ESP32 and starts the game
```

### Manual Port

```bash
./run.sh /dev/ttyACM1   # explicit serial port; use the actual detected ESP32 port
```

### AIO v2

If AIO v2 is detected, the game automatically:
- Enables USB power (GPIO 23) on startup — powers ESP32 XIAO
- Waits six seconds after switching USB power on for boot/enumeration
- Detects the serial device, with up to three two-second retries when needed
- Probes the opened serial connection for firmware readiness

---

## Display Layout

```
+----------------------------------------------+
| HUD TOP: Level | XP bar | Badges | BAT | Z  |  16px
+----------------------------------------------+
|                                              |
|           MAP: Coastlines, OSM tiles,        |
|           markers, radar, player             |  218px
|                                              |
+----------------------------------------------+
| TERMINAL: ESP32 output, attack logs          |  110px
+----------------------------------------------+
| HUD BOT: Stats | Active ops | GPS | LoRa    |  16px
+----------------------------------------------+
```

---

## Main Controls

| Key | Action |
|-----|--------|
| **TAB** | Open/close menu |
| **Arrow keys** | Left/right changes menu category; up/down selects an item; pan the map when no overlay is open |
| **ENTER** | Select menu item / confirm |
| **ESC** | Back / close overlay / quit dialog |
| **\`** (backtick) | Toggle Loot Database screen |
| **+/-** | Zoom in/out on map |
| **PAGEUP/PAGEDOWN** | Scroll terminal |

---

## Menu Categories

The labels below match the current game. Navigate categories with left/right, select an item with up/down and activate it with Enter. Bracketed letters/numbers are the displayed menu prefixes; this build’s main menu does not dispatch them as direct shortcuts. **SYSTEM → Download Map** is implemented and needs a Stadia API key in `secrets.conf`. Entries labelled **WIP** are disabled menu placeholders. Inherited host attack implementations elsewhere in the source do not make a disabled entry active.

### SCAN

| Displayed prefix | Menu entry |
| --- | --- |
| 1 | WiFi Scan |
| 2 | BLE Scan |
| t | BT Tracker |
| a | AirTag Scan |

### SNIFF

| Displayed prefix | Menu entry |
| --- | --- |
| 1 | WiFi Wardrive |
| 2 | BT Wardrive |
| 3 | Pkt Sniffer |
| 4 | HS Capture |
| 5 | HS Capture no SD |

### ATTACK

| Displayed prefix | Menu entry |
| --- | --- |
| 1 | Deauth |
| 2 | Blackout |
| 3 | HS Capture |
| 4 | Evil Twin |
| 5 | SAE Flood |
| 6 | HS Capture no SD |
| 7 | Evil Portal |
| d | Dragon Drain |
| m | MITM |
| k | BlueDucky (WIP) |
| j | RACE Attack (WIP) |

### ADDONS

| Displayed prefix | Menu entry |
| --- | --- |
| h | BLE HID (WIP) |
| i | HID Type (WIP) |
| 9 | MeshCore Chat |
| 8 | Meshtastic Chat |
| 7 | Meshtastic Profile |
| r | MeshCore Region |
| w | WDGWars Chat |
| f | Flipper Zero |
| a | ADS-B Radar |
| 4 | 433 MHz Scanner |
| e | Emergency Radio |
| s | Monster RF SubGHz |
| m | Mute Sounds |
| p | PipBoy Watch |
| y | Proximity |
| z | Prox Radar |

### SYSTEM

| Displayed prefix | Menu entry |
| --- | --- |
| x | STOP ALL |
| r | Reboot ESP32 |
| m | Download Map |
| g | GPS |
| l | LoRa |
| d | SDR |
| b | USB |
| w | Whitelist |
| u | Upload WPA-SEC |
| p | Download WPA-SEC |
| f | Flash ESP32 |
| i | Update Game |

### CRISIS

| Displayed prefix | Menu entry |
| --- | --- |
| w | Wikipedia |
| c | ctOS Map |
| n | Navigation |
| s | Status |

A **PLUGINS** tab is added when plugins provide menu entries. See the [plugin guide](PLUGINS.md). Mesh chat, RF profiles and wardrive scheduling are explained in the [current feature guide](FEATURE_GUIDE.md).

---

## PipBoy Watch Integration

Connect the game to a [PipBoy Watch](https://github.com/LOCOSP/twatchultra) (LilyGo T-Watch Ultra with ESP32-S3) for NFC scanning, remote recon, GPS, and LoRa mesh — all controlled from the game UI.

### Requirements

- LilyGo T-Watch Ultra with PipBoy firmware
- uConsole Bluetooth enabled (`bluetoothctl power on`)

### Connection Flow

1. Enable BLE on watch: WiFi app > BLE ON/OFF
2. In game: select **ADDONS → PipBoy Watch** with arrows and Enter
3. Game scans for `PipBoy-*` devices (10 seconds)
4. Select device with arrows + ENTER
5. If previously connected, game auto-reconnects on startup

### Watch Overlay

The PipBoy Watch overlay has two panels:

**Left panel** — Command menu:

| Command | Description |
|---------|-------------|
| Request Status | Battery, GPS, time, heap |
| Haptic Buzz | Vibrate watch (connection test) |
| WiFi Recon | Scan WiFi networks from watch |
| BLE Recon | Scan BLE devices from watch |
| Stop Recon | Stop active scan |
| Get Recon Results | Retrieve scan results |
| NFC Scan | Start NFC tag polling |
| NFC Stop | Stop NFC polling |
| NFC Save Tag | Save detected tag to watch SD |
| NFC List Tags | List saved tags on watch |
| NFC Download All | Transfer all .nfc files to game loot |
| LoRa Start | Start LoRa radio on watch |
| LoRa Stop | Stop LoRa radio |
| LoRa Advertise | Broadcast MeshCore presence |
| GPS ON/OFF | Toggle watch GPS |
| Disconnect | Close BLE connection |
| Forget Watch | Remove BLE bonding (unpair) |

**Right panel** — Live log of all watch events (NFC tags, recon results, status, errors).

### NFC Tag Scanning

1. Select **NFC Scan** from watch menu
2. Hold NFC tag to watch back
3. Tag UID + NDEF data appears in watch log (+25 XP)
4. **NFC Save Tag** saves to watch SD card
5. **NFC Download All** transfers all tags to `loot/<session>/nfc/` (Flipper .nfc format)

### LoRa via Watch

The watch LoRa integrates with the **MeshCore Messenger**:

- Messages from watch LoRa appear in MeshCore chat with `W` tag (e.g., `14:32 2hop W`)
- Messages from AIO LoRa appear without `W` tag (e.g., `14:32 2hop`)
- When sending: if AIO LoRa active, uses AIO; if only watch connected, forwards via watch
- Opening MeshCore Messenger with only watch connected auto-starts watch LoRa

### Auto-Connect

- On game startup, checks if PipBoy is already connected to uConsole Bluetooth
- If yes — attaches silently in background (no scan needed)
- If watch disconnects (or game crashes), watch auto-restores after 20-60 seconds
- Game sends keepalive every 30 seconds to maintain connection

### Recon (Remote WiFi/BLE Scanning)

Use the watch as a remote scanner — useful when uConsole doesn't have ESP32 or for stealth recon:

1. **WiFi Recon** — watch scans nearby WiFi networks
2. **BLE Recon** — watch scans BLE devices
3. **Get Recon Results** — retrieves scan data, displayed in watch log

---

## MeshCore Messenger

This section describes the inherited MeshCore messenger. Meshtastic uses a separate public-text implementation and does not inherit MeshCore private-channel or DM support; see the [current feature guide](FEATURE_GUIDE.md).

Off-grid encrypted mesh chat over LoRa (SX1262). Compatible with [MeshCore](https://github.com/meshcore-dev/MeshCore) / MeshOS devices.

### Opening Messenger

**ADDONS → MeshCore Chat** (displayed prefix **[9]**, selected with arrows and Enter), or enable LoRa through **SYSTEM → LoRa** and choose MeshCore first. For Meshtastic, use **ADDONS → Meshtastic Chat**; the protocols share one radio owner. See the [feature guide](FEATURE_GUIDE.md) for the current selection flow.

### Messenger Controls

| Key | Action |
|-----|--------|
| **Type + ENTER** | Send message on current channel |
| **Ctrl+H** | Toggle Contacts panel (open/close) |
| **Ctrl+A** | Broadcast advertisement (announce presence) |
| **Ctrl+N** | Change node name |
| **Ctrl+C** | Channel picker |
| **Ctrl+X** | Clear chat log |
| **[ / ]** | Quick switch channel (left/right) |
| **PAGEUP/PAGEDOWN** | Scroll chat history |
| **ESC** | Exit DM mode / close messenger |

### Contacts Panel (Ctrl+H)

| Key | Action |
|-----|--------|
| **UP/DOWN** | Navigate contact list |
| **ENTER** | Open action menu |
| **ESC** | Close panel |

#### Contact Actions

| Action | Description |
|--------|-------------|
| **DM** | Start encrypted Direct Message (ECDH + AES-128, peer-to-peer) |
| **Note** | Add/edit note for contact (saved to loot_db.json) |
| **Info** | Show contact details in chat (ID, type, first seen, GPS) |
| **Delete** | Remove contact (will re-appear on next advertisement) |

### Direct Messages (DM)

- Select contact > DM from action menu
- Blue title bar and input bar indicate DM mode
- Messages encrypted with ECDH (Ed25519 to X25519 key agreement) + AES-128-ECB
- **ESC** exits DM mode, returns to channel chat
- Delivery confirmed with checkmark when recipient sends ACK

### Chat Display

- Incoming messages show **timestamp** and **hop count** on the right (e.g., `14:32 2hop`)
- Outgoing messages show **timestamp**, then **checkmark** when confirmed
- DMs shown in blue with `[DM]` prefix
- Long messages wrap to next line
- Character counter appears when typing (max 120 characters)

### Channels

- **public** — default channel, everyone can read
- **#hashtag** — PSK derived from hashtag name
- Custom private channels configurable in `~/.watchdogs_meshcore.json`

### Node Types

| Icon | Type | Description |
|------|------|-------------|
| C | Client | Chat companion device (can DM) |
| R | Repeater | Infrastructure relay node (highlighted green) |
| M | Room | Message storage server (BBS) |
| S | Sensor | Telemetry-only node |

### Identity

- Node name auto-generated as `WatchDogs_<hex>` (from Ed25519 public key)
- Change with **Ctrl+N** in messenger
- Ed25519 keypair stored in `~/.watchdogs_meshcore_key`

---

## XP System & Progression

Earn XP through real-world cybersecurity activities. XP persists across sessions in `loot_db.json`.

### XP Rewards

| Activity | XP |
|----------|-----|
| New WiFi network discovered | +15 |
| Duplicate WiFi network | +1 |
| New BLE device discovered | +10 |
| Duplicate BLE device | +1 |
| WPA handshake captured | +200 |
| Evil Twin credential captured | +150 |
| Evil Twin client connected | +25 |
| NFC tag saved | +25 |
| Flipper SubGHz TX | +25 |
| New persistent MeshCore or Meshtastic contact | +20 once per new discovery |
| Completed SPACE game interaction (PWN) | +50 |
| New aircraft tracked (ADS-B) | +10 |
| New 433 MHz sensor decoded | +5 |
| NFC tag scanned (PipBoy Watch) | +25 |
| NFC file downloaded from watch | +25 |

### Level Progression

| Level | XP Required | Title |
|-------|-------------|-------|
| 1 | 0 | NOOB |
| 2 | 100 | SCRIPT_KIDDIE |
| 3 | 500 | SKIDDIE+ |
| 4 | 1,500 | WANNABE |
| 5 | 3,000 | PACKET_MONKEY |
| 6 | 6,000 | WARDRIVER |
| 7 | 10,000 | HACKER |
| 8 | 20,000 | NETRUNNER |
| 9 | 35,000 | PHREAKER |
| 10 | 50,000 | EXPLOIT_DEV |
| 11 | 75,000 | ELITE |
| 12 | 100,000 | SHADOW_OPS |
| 13 | 150,000 | GHOST |
| 14 | 250,000 | ZERO_DAY |
| 15 | 400,000 | APT_AGENT |
| 16 | 600,000 | CYBER_DEMON |
| 17 | 1,000,000 | CYBER_GOD |
| 18 | 2,500,000 | DIGITAL_DEITY |
| 19 | 5,000,000 | MATRIX_BREAKER |
| 20 | 10,000,000 | FINAL_BOSS |

### Achievement Badges

| Badge | How to Earn |
|-------|-------------|
| WARDRIVER | Start WiFi or BLE wardriving scan |
| HANDSHAKE | Capture a WPA handshake |
| EVIL TWIN | Capture credentials via Evil Twin |
| MESHCORE | Receive a mesh message; the Meshtastic integration reuses this existing achievement |
| WPA-SEC | Upload handshakes to WPA-sec |
| FLIPPER | Use Flipper Zero features |
| SKYWATCH | Start ADS-B radar |
| IOT HUNTER | Start 433 MHz scanner |

---

## SDR — Software Defined Radio

Requires RTL-SDR (RTL2832U) on AIO v2 board + external tools.

### Prerequisites

```bash
sudo apt install dump1090-mutability rtl-433
```

### ADS-B Radar (Aircraft Tracking)

1. Enable SDR hardware: SYSTEM > SDR (toggle ON)
2. Start radar: ADDONS > ADS-B Radar

Aircraft appear on the map as arrow icons:
- **Green** — low altitude (<5,000 ft)
- **Yellow** — mid altitude (5,000–20,000 ft)
- **Cyan** — high altitude (>20,000 ft)

At zoom level 4+, callsign and altitude (in thousands of feet) are shown.
Aircraft disappear from map after 60 seconds without signal.

Data saved to `loot/<session>/adsb_aircraft.csv`.

### 433 MHz Scanner (IoT Sensors)

1. Enable SDR hardware: SYSTEM > SDR (toggle ON)
2. Start scanner: ADDONS > 433 MHz Scanner

Decoded sensors appear in the terminal with model, temperature, humidity, etc.
Sensors with GPS fix are shown as blue squares on the map.

Supported protocols (via rtl_433): weather stations, TPMS tire pressure,
door/window sensors, smoke detectors, remote controls, and 200+ more.

Data saved to `loot/<session>/sdr_sensors.csv`.

---

## Loot Database

Press **\`** (backtick) to view all-time statistics:

- **Sessions** — total game sessions
- **WiFi networks** — unique networks discovered
- **BT devices** — unique BLE devices found
- **Handshakes** — WPA captures (PCAP, HCCAPX, HC22000)
- **Passwords** — portal + Evil Twin credentials
- **MC Nodes** — MeshCore nodes heard (per session)
- **MC Messages** — MeshCore messages received
- **Contacts** — persistent MeshCore contacts
- **GPS points** — wardriving data points

### Loot Directory Structure

```
loot/
  loot_db.json                    # Aggregate database
  <session>/
    serial_full.log               # Raw ESP32 serial output
    wardriving.csv                # WiFi (lat, lon, ssid, bssid, rssi, ch, enc)
    bt_devices.csv                # BLE (mac, name, type, rssi, lat, lon)
    handshakes/
      *.pcap *.hccapx *.22000    # WPA captures in 3 formats
    meshcore_nodes.csv            # Discovered LoRa nodes
    meshcore_messages.log         # Chat messages received
    evil_twin_capture.log         # Captured credentials
    portal_passwords.log          # Portal form submissions
    mitm/mitm.pcap               # MITM packet capture
    nfc/*.txt                     # Flipper NFC tag dumps
    adsb_aircraft.csv             # ADS-B tracked aircraft
    sdr_sensors.csv               # 433 MHz decoded sensors
```

---

## HUD Elements

### Top Bar
- **Level + Title** — current progression rank
- **XP bar** — visual progress to next level
- **Badges** — earned achievement icons
- **BAT:XX%** — uConsole battery (green >25%, yellow >10%, red <=10%)
- **Z:level** — current map zoom

### Bottom Bar
- **BLE/WiFi/HS/PWD** — saved totals plus current-session additions; **PWN** — current-session game interactions
- **Active tools** — currently running operations (WiFi, BT, SNF, HS, EP, ET, ADS-B, 433)
- **GPS** — fix status + satellite count
- **LoRa** — actual active MeshCore/Meshtastic protocol and receive activity
- **SDR** — satellite dish icon above radar when active (AC:N aircraft, S:N sensors)

---

## Game Updates

Open **SYSTEM → Update Game** for the installed numeric version, full scrollable notes and the small avatar. The startup screen retains LOCOSP’s credit and adds **Modified by KiloGramowy** underneath. Startup checks run in the background; a release notice appears once for that release, and opening it never installs code. A separate confirmation is required.

This is still a private preview without a published release feed. Use the [update guide](UPDATING.md) for release-source checks, data preservation and code-only recovery. Do not use a blanket `git pull` on an installation maintained through a deployment manifest.

---

## ESP32 Flashing

### With AIO v2 (no disassembly needed)

1. **SYSTEM → Flash ESP32**
2. Select **XIAO ESP32-C5**
3. Game automatically:
   - Closes serial connection
   - USB power cycles ESP32 (GPIO 23)
   - Downloads latest [projectZero](https://github.com/LOCOSP/projectZero) firmware
   - Flashes via esptool with auto-bootloader (USB-JTAG DTR/RTS)
   - Reconnects after reboot

### Without AIO v2

1. Hold **BOOT** button on XIAO
2. Press **RESET** while holding BOOT
3. Release BOOT
4. Select Flash from menu

---

## Configuration Files

| File | Location | Description |
|------|----------|-------------|
| `~/.watchdogs_meshcore.json` | Home dir | MeshCore node name + channels |
| `~/.watchdogs_meshcore_key` | Home dir | Ed25519 keypair (identity) |
| `~/.watchdogs_meshtastic.json` | Home dir | Meshtastic identity, local nickname, RF/channel choice and RX journal |
| `~/.watchdogs_radio_profiles.json` | Home dir | Per-protocol MANUAL/AUTO preferences and explicitly approved local areas |
| `~/.watchdogs_game_updates.json` | Home dir | Releases whose update notice has already been displayed |
| `loot/loot_db.json` | Project dir | XP, badges, contacts, stats |
| `secrets.conf` | Project dir | WPA-sec API key |
| `loot/<session>/` | Project dir | Per-session loot data |

Legacy `.janos_meshcore.json` and `.janos_meshcore_key` names are migration inputs. New configuration uses the `.watchdogs_*` files above; preserve both names when backing up an older installation.

---

## Supported Hardware

### ESP32 Boards
- ESP32-C5 WROOM-1 (Dev Kit) — via CP210x/CH340 USB-UART
- Seeed Studio XIAO ESP32-C5 — via native USB-JTAG/Serial

### PipBoy Watch (Companion)
- LilyGo T-Watch Ultra (ESP32-S3)
- Connection: BLE (Nordic UART Service)
- Features: NFC (PN532), LoRa, GPS, WiFi/BLE recon, compass
- Firmware: [PipBoy-3000](https://github.com/LOCOSP/twatchultra)

### AIO v2 GPIO Map
| Module | GPIO |
| --- | --- |
| GPS | 27 |
| LoRa (SX1262) | 16 |
| SDR (RTL2832U) | 7 |
| USB Hub | 23 |

Initial rail states depend on the device. WDG tracks the power it switches on or acquires and releases that ownership on exit; see [Leaving the game](FEATURE_GUIDE.md#-leaving-the-game).

### Auto-detected Serial Ports
- `/dev/ttyUSB0` — CP210x, CH340, FTDI
- `/dev/ttyACM1` — ESP32-C5 native USB-JTAG/Serial
