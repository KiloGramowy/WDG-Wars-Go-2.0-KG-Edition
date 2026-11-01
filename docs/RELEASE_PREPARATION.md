# 🔐 Private release preparation

This is the consolidated private review branch. Its updater preview has separately been tested and deployed on the owner's uConsole; see [Validation](VALIDATION.md). Preparing or changing this branch does **not** publish the repository or authorize a public release.

## Separate private development and review repositories

| Repository | Purpose | Branch policy |
| --- | --- | --- |
| Private development repository, `KiloGramowy/esp32-watch-dogs` | Preserve the integrated source, development history and recovery references | Keep all existing development branches; use `codex/wdg-go-wars-release` as the consolidated source |
| Private review target, `KiloGramowy/WatchDogsGo-1` | Review a clean snapshot of the current source, documentation and media before publication | One branch, `main`; do not import the private development history |

The current README, feature guides, reviewed media and integrated source belong together. Users should not assemble the candidate by merging prototype or development branches. The `-1` suffix marks the private review target; removing it and any public publication remain later, explicitly authorized steps.

Older development branches are history and recovery references, rather than additional release editions. Keep them in the development repository. Tidying the separate review repository does not require deleting or rewriting that history.

## Keep the existing work

Retain all prior branches and commits. Do not rewrite history, force-push or delete earlier work to make a release branch look clean. Keep the original LOCOSP attribution and [LICENSE](../LICENSE); document the fork's additions separately.

The recorded software baseline and its limits are in [Validation](VALIDATION.md). A candidate branch needs its own exact commit and fresh validation record; historical test totals are not a substitute.

## Protect player data

Test an isolated checkout first. Before any later deployment, back up the current installation's:

- `loot/` and its current XP, discoveries, notes, achievements and sessions;
- `secrets.conf` and `plugins/.wardrive_state.json`;
- home-directory `.watchdogs_meshcore.json`, `.watchdogs_meshtastic.json` and `.watchdogs_radio_profiles.json`, together with existing identity/key files.

Keep those backups private and verify them. Git ignore rules do not create a backup. The working installation's old Git HEAD may not describe all deployed sources; compare its deployment manifest and file hashes. A code rollback must preserve newer progress rather than restoring an old full data snapshot over it.

## Build a clean candidate tree

1. Package reviewed tracked files from the intended branch, not an unrestricted copy of a live device directory.
2. Exclude runtime loot, secrets, upload state, keys, packet captures, private screenshots/videos, backup archives and local tool output.
3. Replace device-specific operational reports with generic documentation. Keep their private originals outside the publication package.
4. Preserve protocol constants and explicitly synthetic tests. Do not blanket-redact public channel defaults or geographic datasets as if they were personal credentials or user GPS.
5. Review screenshots and video pixels for identities, coordinates, message bodies, contact lists, private channels/keys and authentication requests. Commit only the reviewed publication derivatives.
6. Check relative links, source attribution and [third-party notices](../THIRD_PARTY_NOTICES.md). A soundtrack does not inherit the software's MIT license.
7. Make install/update instructions select this fork and the reviewed branch. Upstream curl installers do not install the fork's extra work. While the fork is private, use an authenticated checkout workflow rather than advertising a nonexistent public installer.

Do not embed an API token in an installer URL, source file or screenshot. Verify a fresh checkout and archive against the intended file list before sharing it.

## History is a separate publication boundary

Removing private material from the newest tree does not remove it from older commits. A clean release artifact is not proof that making the entire existing fork public would reveal no prior operational data.

The development repository is a **private GitHub fork of a private upstream repository**. GitHub does not allow changing a fork's visibility independently. The separate private review repository is a clean snapshot, not a claim that GitHub has already created a formal public fork. The preferred public route remains a formal fork of [LOCOSP/WatchDogsGo](https://github.com/LOCOSP/WatchDogsGo) containing the approved current files, with original authorship and license retained. The private development history stays private. See [GitHub's fork visibility rules](https://docs.github.com/en/pull-requests/reference/forks).

The current instruction is **review and preparation only, without publication**. Create or update only the private review target. Keep the prior private development branches and commits unchanged. Do not merge, mirror-push or otherwise copy their history into the clean candidate or future public fork merely to preserve Git ancestry.

At publication, set the actual public repository/branch in the updater and installation instructions, attach the film in the public context, and verify links while signed out. The first approved numeric version and its release feed must describe that verified public snapshot. A private attachment URL or authenticated preview is not proof of anonymous access.

An existing private preview has different Git ancestry from a clean public snapshot. Its one-time source migration needs verified files and a controlled code deployment, preserving player data; see [Updating](UPDATING.md). Do not disable the ancestry guard or disclose private commits to bypass it.

## A useful handoff record

Record the candidate branch and full commit, changed files, software results and skips, reviewed media/attribution, unresolved hardware or backend limits, and a code-only rollback procedure. Include the archive hash when distributing an artifact. Avoid private machine paths, exact user locations and raw device evidence in the public-facing record.
