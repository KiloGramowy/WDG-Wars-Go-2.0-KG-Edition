# 🔔 Game updates

The updater lives in **SYSTEM → Update Game**. Its menu row retains the original layout; the small GitHub avatar appears only inside the update window. Version numbers stay numeric. Original LOCOSP authorship remains on startup, with **Modified by KiloGramowy** directly below it.

## Player controls

- Each launch checks for an approved release in the background.
- A new release shows one notice on the main game screen, after other dialogs close.
- **Enter / Y — OK** opens its full notes. **N / Esc — No** dismisses the notice. Neither installs code.
- The notice is remembered per immutable repository ID, release channel and stable release ID across restarts. Editing notes, renaming the repository or pushing a development commit does not create a new notice.
- **Update Game** stays white while a confirmed update is available, including after dismissal.
- In the update window, use **Up / Down**, **Page Up / Page Down**, **Home / End** to read all notes, **R** to check again and **Esc** to return.
- When an approved update is available, **Enter** prepares it. A separate **Y / N** confirmation is required before applying it.

Failed or offline checks are explicit. They are never presented as evidence that the installed game is current. Scanning and reception continue while a player reads notes; installation stops the game's operations and saves final records before replacing files.

## Prepared stable release

The prepared release is **0.9.17**. The review repository remains private and keeps its `-1` suffix until a separate publication instruction. Its approved `updates/stable.json` feed identifies the release's code; committing that feed does not change repository visibility or create a GitHub Release.

The code commit comes first. A second, feed-only commit records that first commit as `source_commit`. A fresh clone can therefore have a newer feed/documentation HEAD while running the same approved version. The updater reports it as current only when the approved commit and installed commit have identical runtime Git entries. In-game offline notes, including `docs/UPDATE_PREVIEW.txt`, are part of that runtime set. Unadvertised code changes cannot use this exception.

Release metadata uses schema 2 and includes the immutable repository ID, repository name, channel, stable release ID, numeric version, full source commit, title and full notes. Ordinary development commits do not advertise an update. Future upgrades require a newer approved numeric version and a feed pointing to that release's code.

## Release source

The approved source is explicitly configured in `watchdogs/game_updates.py`; it does not follow an arbitrary installation origin:

| Build | Repository ID | Repository name | Branch | Channel |
| --- | --- | --- | --- | --- |
| Private development | `1437688988` | `KiloGramowy/esp32-watch-dogs` | `codex/wdg-go-wars-release` | `stable` |
| Clean review and future public release | `1441947361` | `KiloGramowy/WatchDogsGo-1`, then `KiloGramowy/WatchDogsGo` | `main` | `stable` |

The updater resolves `https://api.github.com/repositories/1441947361` for the release build, checks the pinned repository and owner IDs, then fetches the verified canonical name. Removing `-1` keeps the same repository ID, history, release source and notification identity. It does not require another code change. Reusing the old repository name must be avoided because it breaks GitHub's rename redirects; see [GitHub's rename documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository).

Public installations use ordinary HTTPS and do not require `gh` or a token. While the source is private, the owner's existing GitHub authentication can provide access, including when the game runs with sudo. Authentication material is never placed in request URLs, source files or game output. Without access, the check reports an error.

An installation's source is determined by its deployed updater constants, deployment manifest and file hashes. A legacy Git HEAD does not describe every deployed overlay. A development installation keeps its own source until a separately verified migration; renaming another repository does not switch that installation to a different repository ID. The validated uConsole deployment was migrated once to the clean release source with its full runtime manifest, preserving player data and the older recovery reference. The private deployment report records its actual source and rollback.

## Publication and existing installations

The selected publication route uses the **same clean review repository**: rename `WatchDogsGo-1` to `WatchDogsGo`, then change its visibility to public after the owner's explicit instruction. It is a substantial code fork with LOCOSP attribution and the original license retained; GitHub stores this clean repository as a standalone repository, rather than a formal fork in the private upstream's fork network. The private development repository and its branches stay private. See [Release preparation](RELEASE_PREPARATION.md) for the read-only preflight and guarded activation commands.

No rename or visibility change is performed by an update check, game launch or ordinary push. Anonymous access to source, images, the native movie and the update feed can only be confirmed after the privacy change. The prepared publication helper runs those checks automatically after activation and reports a failure explicitly; an authenticated private preview is not proof of public access.

An existing development installation has different Git ancestry from the clean release snapshot. The updater deliberately rejects unrelated histories. Moving that installation to the release repository requires a controlled, one-time code deployment: back up current changed files and the deployment manifest, verify installed file hashes against the approved source, then record that verified source commit and repository ID. Preserve current player data, settings, identities, keys, contacts, pairing and pending logs. Do not disable the ancestry check or import private commits to bypass it. Future releases on the same approved release ancestry can use the normal in-game update flow.

## Preservation and recovery

Preparation verifies the pinned source, permitted runtime paths and current file hashes. Local edits, unexpected file collisions, symlinks and dependency changes fail safely rather than being overwritten or invoking an unattended installer. Player data and credentials are outside the update file set.

Installation requires stopped game-owned workers and saved player state. It keeps a verified code-only backup and recovery receipt; settings, loot, XP, achievements, RF/channel preferences, keys, contacts and pending logs are not restored from an older player snapshot during rollback. Relaunch the game after installation.

Keep deployment backup paths and recovery receipts private. A separate exact-file deployment also has its own code-only rollback, recorded in the private deployment report. These update checks do not establish new RF reception, approved AUTO behavior or chat delivery.
