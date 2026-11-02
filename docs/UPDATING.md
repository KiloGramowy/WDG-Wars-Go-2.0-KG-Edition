# 🔔 Game updates

The updater lives in **SYSTEM → Update Game**. Its menu row retains the original layout; the small GitHub avatar appears only inside the update window. Version numbers stay numeric. Original LOCOSP authorship remains on startup, with **Modified by KiloGramowy** directly below it.

## Player controls

- Each launch checks for an approved release in the background.
- A new release shows one notice on the main game screen, after other dialogs close.
- **Enter / Y — OK** opens its full notes. **N / Esc — No** dismisses the notice. Neither installs code.
- The notice is remembered per immutable repository ID, release channel and stable release ID across restarts. Editing notes, renaming the repository or pushing a development commit does not create a new notice.
- **Update Game** stays white while a confirmed update is available, including after dismissal.
- In the update window, use **Up / Down**, **Page Up / Page Down**, **Home / End** to read all notes, **R** to check again and **Esc** to return.
- **O** opens the verified update source on GitHub.
- When an approved update is available, **Enter** prepares it. A separate **Y / N** confirmation is required before applying it.

Failed or offline checks are explicit. They are never presented as evidence that the installed game is current. Scanning and reception continue while a player reads notes; installation stops the game's operations and saves final records before replacing files.

## Prepared stable release

The prepared release is **0.9.18**. It combines legacy progress preservation, bridge BLE completion, empty mesh contact panels and the WiFi form picker. Its public source is the formal fork [KiloGramowy/WDG-Wars-Go-2.0-KG-Edition](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition), based on [LOCOSP/WatchDogsGo](https://github.com/LOCOSP/WatchDogsGo). Its approved `updates/stable.json` feed identifies the release's code; committing that feed does not create a GitHub Release.

The code commit comes first. A second, feed-only commit records that first commit as `source_commit`. A fresh clone can therefore have a newer feed/documentation HEAD while running the same approved version. The updater reports it as current only when the approved commit and installed commit have identical runtime Git entries. In-game offline notes, including `docs/UPDATE_PREVIEW.txt`, are part of that runtime set. Unadvertised code changes cannot use this exception.

Release metadata uses schema 2 and includes the immutable repository ID, repository name, channel, stable release ID, numeric version, full source commit, title and full notes. Ordinary development commits do not advertise an update. Future upgrades require a newer approved numeric version and a feed pointing to that release's code.

## Edition name and repository rename

**WDG Wars Go 2.0 — KG Edition** is the presentation name; the prepared software version is **0.9.18**. The repository was renamed without changing its immutable ID `1442123109`, public LOCOSP parent or approved runtime source. Existing 0.9.17 builds retain their bootstrap name `KiloGramowy/WatchDogsGo-KG-Edition` and resolve the new canonical name through that ID. The feed label can follow the rename without creating a new release or another notice. No reinstall or player-data migration is required for this rename.

## Release source

The approved source is explicitly configured in `watchdogs/game_updates.py`; it does not follow an arbitrary installation origin:

| Build | Repository ID | Repository name | Branch | Channel |
| --- | --- | --- | --- | --- |
| Private development | `1437688988` | `KiloGramowy/esp32-watch-dogs` | `codex/wdg-go-wars-release` | `stable` |
| Formal public release fork | `1442123109` | `KiloGramowy/WDG-Wars-Go-2.0-KG-Edition` | `main` | `stable` |

The updater resolves `https://api.github.com/repositories/1442123109` for the release build, checks the pinned repository and owner IDs, then fetches the verified canonical name. GitHub identifies its parent and source as the public `LOCOSP/WatchDogsGo`, repository ID `1208909098`. The fork's current name retains its own identity and fork relationship; it does not inherit the previous standalone repository's ID.

Public installations use ordinary HTTPS and do not require `gh` or a token. Private development builds can use the owner's existing GitHub authentication, including when the game runs with sudo. Authentication material is never placed in request URLs, source files or game output. Without the required access, the check reports an error.

An installation's source is determined by its deployed updater constants, deployment manifest and file hashes. A legacy Git HEAD does not describe every deployed overlay. A development installation keeps its own source until a separately verified migration; renaming another repository does not switch that installation to a different repository ID. The private deployment report records the source and code-only rollback used on the maintained uConsole.

## Publication and existing installations

The selected route uses a **formal fork of the public LOCOSP repository**, with its public upstream history and the reviewed published snapshot. The private development repository and its five branches stay private; their operational history is not imported. Original LOCOSP attribution and the license remain intact. See [Release preparation](RELEASE_PREPARATION.md) for verification and cutover requirements.

No rename, repository deletion or visibility change is performed by an update check, game launch or ordinary push. The public fork's source, images, native movie and update feed must pass anonymous checks before cutover and again after the former standalone publication is removed. An authenticated browser preview alone is not proof of public access.

Earlier standalone builds from `KiloGramowy/WatchDogsGo` pinned repository ID `1441947361`. Those installations require a manual source migration to `KiloGramowy/WDG-Wars-Go-2.0-KG-Edition`, even though the displayed game version remains 0.9.17. There is no retained handoff repository or promise that the deleted ID will continue serving updates. Do not treat an error from that old source as proof that the game is current.

The maintained uConsole completed a controlled, one-time **0.9.17 → 0.9.17** code deployment with verified backups, a complete runtime manifest and repository ID `1442123109`. Other standalone installations need the same controlled migration, preserving current player data, settings, identities, keys, contacts, pairing and pending logs. Do not disable ancestry or local-edit checks, and do not import private commits to bypass them. Future releases on the approved formal-fork ancestry can use the normal in-game update flow.

## Preservation and recovery

Preparation verifies the pinned source, permitted runtime paths and current file hashes. Local edits, unexpected file collisions, symlinks and dependency changes fail safely rather than being overwritten or invoking an unattended installer. Player data and credentials are outside the update file set.

Installation requires stopped game-owned workers and saved player state. It keeps a verified code-only backup and recovery receipt; settings, loot, XP, achievements, RF/channel preferences, keys, contacts and pending logs are not restored from an older player snapshot during rollback. Relaunch the game after installation.

Keep deployment backup paths and recovery receipts private. A separate exact-file deployment also has its own code-only rollback, recorded in the private deployment report. These update checks do not establish new RF reception, approved AUTO behavior or chat delivery.
