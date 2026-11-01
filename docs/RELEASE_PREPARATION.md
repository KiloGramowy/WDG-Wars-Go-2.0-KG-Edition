# 🔐 Private release preparation

This is the consolidated private review repository. The integrated game and updater have separate software and device validation records; see [Validation](VALIDATION.md). Preparing this candidate does **not** publish the repository. It keeps the `-1` suffix and private visibility until an explicit publication instruction.

## Separate private development and review repositories

| Repository | Purpose | Branch policy |
| --- | --- | --- |
| Private development repository, `KiloGramowy/esp32-watch-dogs` (ID `1437688988`) | Preserve the integrated source, development history and recovery references | Keep the five existing development branches; use `codex/wdg-go-wars-release` as the consolidated source |
| Private review target, `KiloGramowy/WatchDogsGo-1` (ID `1441947361`) | Review a clean snapshot of the current source, documentation and media before publication | One branch, `main`; do not import the private development history |

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
5. Review screenshots and video pixels for private coordinates, keys, authentication requests and other confidential material. The approved media policy retains public conversations and public nodes while masking GPS and keys. Keep only the reviewed publication derivatives.
6. Check relative links, source attribution and [third-party notices](../THIRD_PARTY_NOTICES.md). A soundtrack does not inherit the software's MIT license.
7. Make install/update instructions select this fork and the reviewed branch. Upstream curl installers do not install the fork's extra work. While the fork is private, use an authenticated checkout workflow rather than advertising a nonexistent public installer.

Do not embed an API token in an installer URL, source file or screenshot. Verify a fresh checkout and archive against the intended file list before sharing it.

## History is a separate publication boundary

Removing private material from the newest tree does not remove it from older commits. A clean release artifact is not proof that making the entire existing fork public would reveal no prior operational data.

The development repository is a **private GitHub fork of a private upstream repository**. GitHub does not allow changing a fork's visibility independently; see [GitHub's fork visibility rules](https://docs.github.com/en/pull-requests/reference/forks). Its private history and branches remain in that repository.

The selected route uses the existing clean review repository, not another newly created repository. It is a substantial code fork of [LOCOSP/WatchDogsGo](https://github.com/LOCOSP/WatchDogsGo) with original authorship and license retained. GitHub's metadata identifies the clean repository as standalone, rather than a formal fork in the private upstream's network. This distinction does not change its code attribution.

Only after the owner's explicit publication instruction, rename `KiloGramowy/WatchDogsGo-1` to `KiloGramowy/WatchDogsGo` and change the **same repository** to public. Its immutable ID remains `1441947361`, its owner ID remains `315514197`, and the branch remains `main`. The updater already resolves that identity, so publication needs no new code or source-binding edit. Do not merge or mirror-push the private development history into it.

## Prepared release and publication checks

The first approved numeric release is **0.9.17**. Commit its reviewed code first, then commit the schema-2 `updates/stable.json` feed pointing `source_commit` to that exact code commit. The feed also pins repository ID `1441947361`, channel `stable`, release ID, version, title and full notes. A feed-only descendant with identical runtime Git entries is treated as the same installed release. In-game offline notes are runtime content; changing them requires an updated approved code source. This workflow does not require publishing GitHub tags, a GitHub Release or a release asset.

While the repository is private, clone using the authenticated URL shown by its **Code** button. The final installation URL is `https://github.com/KiloGramowy/WatchDogsGo.git` after the rename. Do not put GitHub tokens or API secrets in either URL or the repository.

In a clean checkout of the reviewed `main`, replace `FULL_APPROVED_MAIN_SHA` below with the full 40-character commit at the tip of the prepared branch:

```sh
python -B tools/public_release.py --check --expected-head FULL_APPROVED_MAIN_SHA
```

The default invocation and `--dry-run` are also read-only. Preflight checks the fixed repository and owner IDs, administration access, target-name availability, local/remote commit equality, the approved clean history, exactly one `main` branch, absence of unreviewed tags or private Actions history, source/media consistency, updater binding and baseline feed. It does not rename, publish or upload anything.

The following command is reserved for the later, explicit publication instruction:

```sh
python -B tools/public_release.py --activate --confirm-public --expected-head FULL_APPROVED_MAIN_SHA
```

Activation repeats preflight, renames the same repository, changes its visibility and verifies the resulting state. It then checks anonymous repository identity, approved README, update feed, linked images, native movie and old-name redirect. If only renaming succeeds, the repository remains private and the failure is reported. If visibility becomes public but anonymous verification fails, the helper reports that public state explicitly and does not silently claim success or roll visibility back.

GitHub documents redirects for repository web/Git URLs and warns that reusing the old name breaks them: [Renaming a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository). The reviewed native movie is attached to the same repository context. Its access follows repository permissions: [Attaching files](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files). Actual anonymous playback and public link access remain pending until the visibility change; private authenticated access cannot prove them in advance. The activation helper performs those checks after the switch.

An existing private development installation has different ancestry and a different source ID from this clean snapshot. Its one-time migration needs verified files and a controlled code deployment preserving player data, contacts, keys, settings, pairing and pending logs; see [Updating](UPDATING.md). The actual deployed manifest and file hashes determine its current source. Do not disable the ancestry guard or disclose private commits to bypass it.

## A useful handoff record

Record the candidate branch and full commit, changed files, software results and skips, reviewed media/attribution, unresolved hardware or backend limits, and a code-only rollback procedure. Include the archive hash when distributing an artifact. Avoid private machine paths, exact user locations and raw device evidence in the public-facing record.
