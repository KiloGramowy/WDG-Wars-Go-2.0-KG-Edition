# 🎮 Feature guide

This fork extends [LOCOSP's Watch Dogs Go](https://github.com/LOCOSP). It keeps the original game, keyboard controls and visual style. See [third-party notices](../THIRD_PARTY_NOTICES.md) for attribution and asset terms.

## 📡 Choose your LoRa protocol

Open **SYSTEM → LoRa** and choose **Meshtastic** or **MeshCore**. One SX1262 can serve one protocol at a time. The protocol is always chosen manually; GPS never switches it for you.

**ADDONS → MeshCore Chat** and **ADDONS → Meshtastic Chat** open their respective communicators. Opening the other chat switches the shared radio. Closing a chat leaves its receiver running in the background; **LoRa OFF** stops the active radio worker. The status indicator represents the active protocol, not whichever chat was opened last.

Keep one owner of the SX1262. Another radio application, including `meshtasticd`, can prevent startup. The existing launcher attempts to stop competing `meshtasticd` processes; account for that before launching it alongside another setup.

## 💬 Meshtastic messages and contacts

The integration receives supported public text, NodeInfo and position packets and can send public text. Its chat follows the existing WDG interface. The local nickname is saved, but it is not automatically advertised to the mesh as a NodeInfo packet. Private Meshtastic channels and direct messages are not implemented.

A queued message or local radio transmit completion does not prove delivery to another device. RF profile selection and chat channel/key selection are separate settings; the channel name alone does not identify another node's RF configuration.

Contact recognition checks radio CRC, packet structure, channel/decryption and supported packet content. Arbitrary bytes or a CRC pass alone are not a new Meshtastic contact. The packet's full `from` NodeID is its **declared author**, which may differ from the repeater that transmitted the received frame. Decoding a public channel does not authenticate that identity or establish direct range to its author.

## 🛰️ Choose and remember RF settings

Open **ADDONS → Meshtastic Profile** or **ADDONS → MeshCore Region**. Use **↑/↓** to highlight a profile, then **M or Enter** for MANUAL. MANUAL works without GPS and is not overwritten by later fixes.

**A** selects AUTO GPS. AUTO recalls only a profile you explicitly approved for that protocol and the displayed **2 km circle**. It does not discover local network settings or assign a preset to an entire country.

After a successful manual selection and a stable fix, **S** opens **Save for this area**. Check the profile and circle, then press **Y** to approve; **N/Esc** cancels. **F**, then **Y**, removes a local assignment. Enter does not substitute for Y in a confirmation dialog.

See [RF profiles and AUTO GPS](RADIO_PROFILES.md) for the full catalog and GPS rules.

## 📍 What receiver GPS means

When a MeshCore or Meshtastic contact has no valid coordinates of its own, a fresh measured uConsole fix can supply an observation position. Existing valid node coordinates take precedence. The local contact data keeps receiver provenance separate from the node's coordinates, and the UI labels **Node GPS** or **Receiver GPS**.

Receiver GPS means **where the contact was heard**, not the author's true position. A relayed packet also does not prove that the author is directly reachable from there. Backfilling a historical contact with a current fix records the assignment time; it does not invent a historical reception position.

Without a credible fix, recoverable contacts wait for receiver GPS. The game does not fabricate coordinates. The current upload API receives the supported position fields; local provenance does not imply that the server displays a separate provenance field.

## 🔁 WiFi and BLE wardriving

In **SNIFF**, **WiFi Wardrive** and **BT Wardrive** can both be ON:

- WiFi alone repeats full WiFi cycles; BT alone repeats full BLE cycles.
- With both selected, the ESP32 completes a WiFi cycle, saves its results, confirms STOP, completes a BLE cycle, saves, confirms STOP, and repeats.
- The two ON labels represent the selected schedule. Only one scan phase actually owns the ESP32 at a time.
- Turning one mode off leaves the other available. **STOP ALL** cancels the schedule and queued starts.

Initial wardrive startup follows the existing GPS wait/cancel dialog. A missing scan-completion reply, failed save or unconfirmed STOP pauses the plan with an error. It is not silently treated as an empty successful scan. Selecting a different ESP32 operation cancels the wardrive plan before handing over the device.

LoRa uses separate hardware and can receive while the ESP32 alternates. Packet sniffer and HS capture are separate operations, not additional parallel phases.

## 📤 Uploads, XP and ownership

The WDGWars pending counter counts **sessions**, not nodes. An active session stays open for further observations. New records or changed supported fields can create a new pending payload even when the identifiers are already known.

The uploader stores receipts bound to the exact content, account and endpoint. A complete application-level receipt distinguishes imported, known, cooldown-handled, rejected and unresolved records. **Cooldown is handled without new import credit.** HTTP 200 or a generic duplicate-cache reply alone is not a complete receipt. A separate registry verification can establish mesh presence without establishing new credit or ownership.

Empty or invalid queue CSV exports are deleted automatically. Raw session logs, authoritative contacts, XP and other player progress are retained. Valid mesh records waiting for receiver GPS are recoverable data and are retained too.

Both mesh protocols use the existing **20 local XP per new persistent discovery** rule. Repeat packets, retransmissions and GPS repairs do not create another discovery award. Local XP is separate from server acceptance, server score and first-finder ownership; an uploaded known node does not become a new server discovery.

## 🔋 Leaving the game

Confirm the normal quit dialog or close the game window. WDG cancels queued scanning, stops its own workers, reads the final ESP32 STOP acknowledgement, saves the received records and XP, closes device handles, then releases the AIO power it acquired. USB is switched off last. Both mesh protocols use the same shutdown path.

Power is tracked per session, including hardware already ON when WDG explicitly acquires its device. Untouched powered hardware is preserved. A worker that fails to release its device is reported and its affected power is retained; firmware flashing also protects power. A forced kill or loss of system power cannot run software cleanup.

The shutdown validation uses isolated test data and real uConsole hardware; it does not establish new RF coverage, remote message delivery or AUTO approval.

## 🔔 Game updates

Open **SYSTEM → Update Game** for the numeric installed version, full scrollable notes and a small avatar inside the window. Startup retains **by LOCOSP**, with **Modified by KiloGramowy** underneath.

Startup checks happen in the background. A new approved release gets one main-screen notice, remembered across restarts. **OK** opens its notes; **No / Esc** dismisses it. A dismissed release remains accessible through the white **Update Game** menu entry. Installation needs a separate confirmation and saves player state before replacing code.

The current private preview does not publish a release or invent an available update. See [Updating](UPDATING.md) for controls, privacy and recovery details.

## 💡 PWN and handshake counters

**PWN** counts the current session's devices marked by the game's SPACE interaction. It is not the number of passive scans or saved handshakes, and its session count starts again after restarting the game. **HS** and saved capture files are separate evidence.

Inherited HS capture tools may transmit deauthentication frames in the ESP32 firmware. A position in the SNIFF menu does not make every tool receive-only; use active tools only with equipment and networks you are authorized to test. This fork does not automatically add HS capture to the WiFi/BLE schedule.

For the recorded test results and their limits, see [Validation](VALIDATION.md).
