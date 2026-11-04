# 🔔 Game updates

The updater lives in **SYSTEM → Update Game**. Its menu row retains the original layout; the small GitHub avatar appears only inside the update window. Version numbers stay numeric. Original LOCOSP authorship remains on startup, with **Modified by KiloGramowy** directly below it.

## Player controls

- Each launch checks for an approved release in the background.
- A new release shows one notice on the main game screen, after other dialogs close.
- **Enter / Y — OK** opens its full notes. **N / Esc — No** dismisses the notice. Neither installs code.
- The notice is remembered per immutable repository ID, release channel and stable release ID across restarts. Editing notes, renaming the repository or pushing a development commit does not create a new notice.
- **Update Game** stays white while a confirmed update is available, including after dismissal.
- In the update window, use **Up / Down**, **Page Up / Page Down**, **Home / End** to read all notes, **R** to check again and **Esc** to return.
- Version 0.9.18 shows numbered uppercase change titles in yellow and descriptions in white. The older 0.9.17 window retains plain text, including the numbered sections and blank lines, until its renderer is updated.
- **O** opens the verified update source on GitHub.
- When an approved update is available, **Enter** prepares it. A separate **Y / N** confirmation is required before applying it.

Failed or offline checks are explicit. They are never presented as evidence that the installed game is current. Scanning and reception continue while a player reads notes; installation stops the game's operations and saves final records before replacing files.

A successfully verified current installation displays **UP TO DATE**. In 0.9.19.2, that result survives reopening the window and restarting, and is bound to the installed version and code. A retained earlier result is labelled as last verified during a new check or network error; a newer release replaces it. **R**, the startup check and once-per-release notices are retained. The 0.9.19.1 updater and later versions automatically restart the game after a completed installation, using the same Python interpreter, launch arguments and environment. Preparation, cancellation and failures do not restart it. An unsuccessful restart keeps the installed code and saved data and offers retry or a manual relaunch.

**First upgrade from 0.9.17, 0.9.18 or 0.9.19:** that already running older updater still requires one manual relaunch. After installation, press Enter or Esc to close the game, then start it again. Automatic restart applies to subsequent updates after the 0.9.19.1 updater is running.

## 🧭 0.9.19.2 — clean map, same loot

**Before:** Wi-Fi/BLE points from earlier sessions could return through map history. **Now:** those map layers show only observations received in the current game session, including known devices. STOP and scheduled switches keep the points until exit. No valid position means no map point. Starting a fresh map does not delete saved history, upload queues or progress. Your cyberdeck remembers the loot; the map is allowed to forget.

Mesh retains the existing **5-to-10-minute OFF rule, farther first**. Its OFF snapshot includes waiting observations, and queue draining is limited per frame; constructing and ranking the snapshot still scales with the queued observations and contacts. Repeated STOP does not extend deadlines, and the other active protocol keeps its markers. **MC:** and **MT:** both remain visible and count unique successfully registered nodes in this session, including unlocated nodes. Protocol switches and OFF retain those counts; restarting resets them.

The canonical English description is [UPDATE_PREVIEW.txt](UPDATE_PREVIEW.txt), shared by the GitHub Release and Discord announcement. Update Game derives its bitmap-font presentation automatically from that source after version filtering: Markdown markers and unsupported emoji are removed only for display, with native numbered yellow headings, the original font and colours, the existing window geometry and **nine visible rows**.

**BLE history — Before:** some saved BLE addresses were missed after restart; commas or unmatched quotation marks could confuse device types, consume following entries or shift names and positions in the history view. Some CSV readers were also left open. **Now:** supported current and older entries retain their correct Wi-Fi/BLE identity, names, signal strength and coordinates, and following entries are read separately. Readers close their files. Loading history does not rewrite old logs.

**Separate Wi-Fi/BLE exports — Before:** the same address used by Wi-Fi and BLE could make one protocol's repeat update the other's saved entry. **Now:** the WiGLE export keeps separate records for the two protocols. A stronger reading updates only its own protocol's row; weaker or equal readings leave its saved row intact, and the other protocol's row is not overwritten. These are saved-export rules; fresh session-map observations still follow new receptions. XP remains **15 for new Wi-Fi, 10 for new BLE and 1 for repeats**. Previously earned XP is neither recalculated nor taken away.

**Pending logs — Before:** closed-session exports with no usable GPS could remain as queue clutter. **Now:** after an upload attempt, wholly unlocated closed-session logs are removed automatically without a popup. A file containing positioned records is retained, including a mixed file with both positioned and unpositioned records. Active sessions, uploads in flight and confirmed positioned exports are protected.

Use **PLUGINS → Pending Upload Logs** for a read-only list of individual exports, including logs waiting for GPS. This local browser does not require an API key or the network-sync level unlock; it is also available through **Manage Pending Logs** in Sync. Navigate with arrows, **Page Up / Page Down / Home / End** and refresh with **R**. Press **D**, then **Y** to delete the selected export, or **N / Esc** to cancel. Only `wardriving.csv`, `adsb_aircraft.csv`, `meshcore_nodes.csv` and `meshtastic_nodes.csv` are covered. Deletion keeps XP, levels, achievements, contacts and known Wi-Fi/BLE identities. A known device's next reception still receives the normal repeat XP. Removing a local log does not create an upload receipt or server credit.

**Terminal spacing — Before:** the scroll indicator could overlap the line count after **Fn+U / Page Up**. **Now:** each label has its own space in the existing terminal header. Font, colours, terminal geometry and controls stay the same.

Software verification covers history reopening, the existing XP rules, saved-data preservation, note rendering and version filtering, session maps, mesh queues, counters, persistent update status and pending-log deletion. Recorded Meshtastic replays and driver-code tests with simulated transports exercise software paths. Native Pyxel scenes exercise the original renderer. **Natural Wi-Fi/BLE reception with GPS and Meshtastic reception were observed in the running 0.9.19.2 game on uConsole. MeshCore reception remains unconfirmed by that device run.** These checks do not prove physical RF reception for every profile or hardware setup; replays and simulated transports do not establish new reception.

The code commit comes first. A second, feed-only commit records that first commit as `source_commit`. A fresh clone can therefore have a newer feed/documentation HEAD while running the same approved version. The updater reports it as current only when the approved commit and installed commit have identical runtime Git entries. In-game offline notes, including `docs/UPDATE_PREVIEW.txt`, are part of that runtime set. Unadvertised code changes cannot use this exception.

Release metadata uses schema 2 and includes the immutable repository ID, repository name, channel, stable release ID, numeric version, full source commit, title and full notes. Ordinary development commits do not advertise an update. Future upgrades require a newer approved numeric version and a feed pointing to that release's code.

## Cumulative installation and versioned notes

One confirmation installs the latest complete approved code, including missing changes from skipped releases. A 0.9.17, 0.9.18 or 0.9.19 installation can update directly to **0.9.19.2**, subject to the first-upgrade manual relaunch described above. Current player data and settings stay outside the code replacement set.

Published notes use `RELEASE 0.9.19.2 - CLEAN MAP. SAME LOOT.` style headings within the existing notes string. Four-part numeric versions are supported by the existing updater. Readers from 0.9.19 onward select sections newer than the installed version; an already current installation can still read its latest section. The shared introduction is retained, and unrecognized formats are displayed in full.

The existing 0.9.17 and 0.9.18 readers cannot select sections before their first upgrade. Both show the cumulative description with 0.9.19.2 first, followed by historical 0.9.19.1, 0.9.19 and 0.9.18 sections. The 0.9.19 and later readers select the sections missing from the installed version. The feed's font-compatible presentation is derived from the canonical English description; GitHub and Discord retain its emoji and Markdown. The feed schema, approval source and installation confirmation are unchanged.

ZIP source archives do not contain Git history and cannot use the current in-game installer. Use a genuine clone and preserve the existing data/configuration as described in [the migration guide](UPGRADING_FROM_LOCOSP.md); do not create artificial Git metadata or bypass the updater's source checks.

## Edition name and repository rename

**WDG Wars Go 2.0 — KG Edition** is the presentation name; the software version is **0.9.19.2**. The repository was renamed without changing its immutable ID `1442123109`, public LOCOSP parent or approved runtime source. Existing 0.9.17 builds retain their bootstrap name `KiloGramowy/WatchDogsGo-KG-Edition` and resolve the new canonical name through that ID. The feed label can follow the rename without creating a new release or another notice. No reinstall or player-data migration is required for this rename.

## Release source

The approved source is explicitly configured in `watchdogs/game_updates.py`; it does not follow an arbitrary installation origin:

| Build | Repository ID | Repository name | Branch | Channel |
| --- | --- | --- | --- | --- |
| Formal public release fork | `1442123109` | `KiloGramowy/WDG-Wars-Go-2.0-KG-Edition` | `main` | `stable` |

The updater resolves `https://api.github.com/repositories/1442123109` for the release build, checks the pinned repository and owner IDs, then fetches the verified canonical name. GitHub identifies its parent and source as the public `LOCOSP/WatchDogsGo`, repository ID `1208909098`. The fork's current name retains its own identity and fork relationship; it does not inherit the previous standalone repository's ID.

Public installations use ordinary HTTPS and do not require `gh` or a token. Authentication material is never placed in request URLs, source files or game output. An unavailable or unverified source is reported as an error.

An installation's source is determined by its deployed updater constants, deployment manifest and file hashes. A legacy Git HEAD does not describe every deployed overlay. Renaming another repository does not switch an installation to a different repository ID.

## Publication and existing installations

The release source is a **formal fork of the public LOCOSP repository**, with its public upstream history and the reviewed published snapshot. Original LOCOSP attribution and the license remain intact.

No rename, repository deletion or visibility change is performed by an update check or game launch. Public source, release assets and the update feed are checked through anonymous access; an authenticated preview alone is not proof of public availability.

Earlier standalone builds from `KiloGramowy/WatchDogsGo` pinned repository ID `1441947361`. Those installations require a manual source migration to `KiloGramowy/WDG-Wars-Go-2.0-KG-Edition`, even though the displayed game version remains 0.9.17. There is no retained handoff repository or promise that the deleted ID will continue serving updates. Do not treat an error from that old source as proof that the game is current.

Standalone installations need a controlled source migration that preserves current player data, settings, identities, keys, contacts, pairing and pending logs. Do not disable ancestry or local-edit checks to bypass that migration. Releases on the approved formal-fork ancestry can use the normal in-game update flow.

## Preservation and recovery

Preparation verifies the pinned source, permitted runtime paths and current file hashes. Local edits, unexpected file collisions, symlinks and dependency changes fail safely rather than being overwritten or invoking an unattended installer. Player data and credentials are outside the update file set.

Installation requires stopped game-owned workers and saved player state. It keeps a verified code-only backup and recovery receipt; settings, loot, XP, achievements, RF/channel preferences, keys, contacts and pending logs are not restored from an older player snapshot during rollback. Relaunch the game after installation.

Keep deployment backup paths and recovery receipts private. Update checks do not establish new RF reception, approved AUTO behavior or chat delivery.
