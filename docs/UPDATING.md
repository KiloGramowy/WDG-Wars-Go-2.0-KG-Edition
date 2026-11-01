# 🔔 Game updates

The updater lives in **SYSTEM → Update Game**. Its menu row retains the original layout; the small GitHub avatar appears only inside the update window. Version numbers stay numeric. Original LOCOSP authorship remains on startup, with **Modified by KiloGramowy** directly below it.

## Player controls

- Each launch checks for an approved release in the background.
- A new release shows one notice on the main game screen, after other dialogs close.
- **Enter / Y — OK** opens its full notes. **N / Esc — No** dismisses the notice. Neither installs code.
- The notice is remembered per repository, release channel and stable release ID across restarts. Editing notes or pushing a development commit does not create a new notice.
- **Update Game** stays white while a confirmed update is available, including after dismissal.
- In the update window, use **Up / Down**, **Page Up / Page Down**, **Home / End** to read all notes, **R** to check again and **Esc** to return.
- When an approved update is available, **Enter** prepares it. A separate **Y / N** confirmation is required before applying it.

Failed or offline checks are explicit. They are never presented as evidence that the installed game is current. Scanning and reception continue while a player reads notes; installation stops the game's operations and saves final records before replacing files.

## Private preview

This branch is still private. There is **no active `updates/stable.json` release feed** in this preview, so no real update is advertised or applied merely by launching the game. `docs/UPDATE_PREVIEW.txt` supplies clearly labelled local preview text. Notification and installation scenarios are tested with isolated software fixtures, separately from the physical screen check.

The preview does not bump the version, publish a release, change repository visibility, create AUTO approvals, or qualify RF reception or chat delivery.

## Release source

The approved source is explicitly configured in `watchdogs/game_updates.py`; it does not follow an arbitrary installation origin:

| Build | Repository checked | Branch | Channel |
| --- | --- | --- | --- |
| Development source and the existing deployed preview | `KiloGramowy/esp32-watch-dogs` | `codex/wdg-go-wars-release` | `stable` |
| Clean private review snapshot | `KiloGramowy/WatchDogsGo-1` | `main` | `stable` |

The clean snapshot changes only the updater's repository/branch binding when exporting the current integrated code. This prevents a legacy installation's upstream remote from redirecting KG updates to another project. Publishing or renaming later requires deliberately updating the source binding and instructions; no uncreated public destination is assumed here. Neither private build has an active release feed in this review.

Git supplies authenticated transport for the private fork using the checkout owner's existing credentials. The game never asks for, records or ships a GitHub token. An installation without access reports a failed check. Its legacy Git HEAD is not substituted for the deployed source manifest.

Only an explicitly approved feed advertises a release. Its stable identity and pinned source commit distinguish a release from ordinary branch commits. Release metadata includes repository, channel, release ID, numeric version, source commit, title and full notes. Before the first release, use the validator and isolated update tests; do not publish the preview notes as a real update.

## Moving the preview to a public fork

The preferred formal public fork would use LOCOSP's public ancestry, without importing private development history. Such a clean snapshot is not a descendant of the development preview's deployed source commit. The updater deliberately rejects unrelated histories; changing the repository address alone does not migrate that installation. Renaming the same review repository keeps its history, but still changes its approved repository identity and source binding.

For an existing preview installation, use a controlled, one-time code deployment: back up current files and the deployment manifest, verify the installed source against the approved public snapshot, then record the verified public source commit. Preserve current player data, settings, identities, keys, contacts and pending logs throughout. Do not clear progress, disable the ancestry check or copy private history into the public fork to make the check pass.

The final public repository address, removal of the review name's `-1` suffix, and first approved version/feed are later publication steps. This review does not perform them. Future releases on the same approved ancestry and source binding can use the regular in-game update flow.

## Preservation and recovery

Preparation verifies the pinned source, permitted runtime paths and current file hashes. Local edits, unexpected file collisions, symlinks and dependency changes fail safely rather than being overwritten or invoking an unattended installer. Player data and credentials are outside the update file set.

Installation requires stopped game-owned workers and saved player state. It keeps a verified code-only backup and recovery receipt; settings, loot, XP, achievements, RF/channel preferences, keys, contacts and pending logs are not restored from an older player snapshot during rollback. Relaunch the game after installation.

Keep deployment backup paths and recovery receipts private. The separate exact-file deployment used for this preview also has its own code-only rollback, recorded in the private deployment report.
