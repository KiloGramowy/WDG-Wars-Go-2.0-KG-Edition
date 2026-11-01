# 📦 Formal fork release preparation

This release is a formal public fork of [LOCOSP/WatchDogsGo](https://github.com/LOCOSP/WatchDogsGo), not a standalone repository carrying only an attribution link. GitHub must report `fork: true` and the public LOCOSP repository as both parent and source. Software and device evidence remain separate; see [Validation](VALIDATION.md).

## Separate private development and public release repositories

| Repository | Purpose | Branch policy |
| --- | --- | --- |
| Private development repository, `KiloGramowy/esp32-watch-dogs` (ID `1437688988`) | Preserve the integrated source, development history and recovery references | Keep the five existing development branches; use `codex/wdg-go-wars-release` as the consolidated source |
| Formal public fork, `KiloGramowy/WatchDogsGo-fork`, then `KiloGramowy/WatchDogsGo` (ID `1442123109`) | Release the reviewed source, documentation and media with GitHub's upstream fork relationship | One branch, `main`; retain public LOCOSP ancestry and the reviewed published snapshot, not private operational history |

The current README, feature guides, reviewed media and integrated source belong together. Users should not assemble the release by merging prototype or development branches. The temporary `WatchDogsGo-fork` name is used for verification before the formal fork takes the final `WatchDogsGo` name. A fork of a public repository is public during preparation; see [GitHub's fork visibility rules](https://docs.github.com/en/pull-requests/reference/forks).

Older development branches are history and recovery references, rather than additional release editions. Keep them in the development repository. Tidying the separate review repository does not require deleting or rewriting that history.

## Keep the existing work

Retain the private development branches, commits and recovery references. Do not rewrite or force-push that history to make a release branch look clean. The public release retains LOCOSP's public ancestry and the reviewed published snapshot. Keep the original LOCOSP attribution and [LICENSE](../LICENSE); document the fork's additions separately.

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
7. Make install/update instructions select this fork and the reviewed branch. Upstream curl installers do not install the fork's extra work. Check the public clone and updater without credentials.

Do not embed an API token in an installer URL, source file or screenshot. Verify a fresh checkout and archive against the intended file list before sharing it.

## History is a separate publication boundary

Removing private material from the newest tree does not remove it from older commits. A clean release artifact is not proof that making the entire existing fork public would reveal no prior operational data.

The development repository is a **private GitHub fork of a private upstream repository**. GitHub does not allow changing a fork's visibility independently; see [GitHub's fork visibility rules](https://docs.github.com/en/pull-requests/reference/forks). Its private history and branches remain in that repository.

The release fork's parent and source are **public** `LOCOSP/WatchDogsGo`, ID `1208909098`. The private `LOCOSP/esp32-watch-dogs` network belongs to the separate development workflow and does not prevent creating this public fork. GitHub's metadata, not a README badge or a Git remote alone, establishes the formal relationship.

After the required tests, the formal fork becomes `KiloGramowy/WatchDogsGo`, retaining its own immutable ID `1442123109`, owner ID `315514197`, and `main` branch. The previous standalone publication used ID `1441947361`; it is removed after verified backups and cutover as authorized, not retained as a legacy update or handoff repository. Reusing its name does not transfer that old ID to the formal fork. Do not merge or mirror-push private development history into either public network.

## Prepared release and publication checks

The numeric release remains **0.9.17**. Commit its reviewed formal-fork code first, then commit the schema-2 `updates/stable.json` feed pointing `source_commit` to that exact code commit. The feed also pins repository ID `1442123109`, channel `stable`, release ID, version, title and full notes. A feed-only descendant with identical runtime Git entries is treated as the same installed release. In-game offline notes are runtime content; changing them requires an updated approved code source. This workflow does not require publishing GitHub tags, a GitHub Release or a release asset.

During preparation, clone using the temporary fork's URL shown by its **Code** button. The final installation URL is `https://github.com/KiloGramowy/WatchDogsGo.git` after cutover. Neither public URL requires GitHub credentials. Do not put GitHub tokens or API secrets in URLs or the repository.

In a clean checkout of the reviewed `main`, replace `FULL_APPROVED_MAIN_SHA` below with the full 40-character commit at the tip of the prepared branch:

```sh
python -B tools/public_release.py --check --expected-head FULL_APPROVED_MAIN_SHA
```

The default invocation and `--dry-run` are also read-only. Preflight must verify the fixed fork and owner IDs, public LOCOSP parent/source IDs, administration access, local/remote commit equality, the reviewed public ancestry, exactly one `main` branch, source/media consistency, updater binding and baseline feed. It must also account for the previous standalone publication occupying the final name. It does not rename, remove or upload anything.

Guarded activation is a maintainer operation after tests and the owner's cutover instruction:

```sh
python -B tools/public_release.py --activate --confirm-public --expected-head FULL_APPROVED_MAIN_SHA
```

Activation repeats preflight and verifies the formal fork at the final name. The fork is already public; its identity and parent/source relationship must remain unchanged by the rename. The separately controlled removal of the old standalone requires verified backups and the authorized final checks. Partial failures must report which repository currently occupies each name; do not announce success from a rename alone.

GitHub documents redirects for repository web/Git URLs and warns that reusing a name breaks its earlier redirects: [Renaming a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository). Do not promise redirects to the deleted standalone ID. After cutover and its removal, repeat anonymous repository identity, README, feed, linked images and native movie checks. The public README can supply an expiring signed video URL even when the direct attachment route returns 404; verify the rendered source without credentials. Do not save those signed URLs or tokens in reports. Media access must be rechecked after deleting the repository that originally hosted the attachment; an earlier successful preview is insufficient.

Existing standalone installations pinned to ID `1441947361` require manual source migration. The maintained uConsole requires a verified code-only **0.9.17 → 0.9.17** deployment, with current source/manifest backups and preserved player data, contacts, keys, settings, pairing and pending logs; see [Updating](UPDATING.md). Until that deployment is verified, its old manifest does not establish use of the new fork. Do not disable the ancestry guard or disclose private commits to bypass it. There is no promise of updates through the removed old ID.

## Verification record

Record the candidate branch and full commit, changed files, software results and skips, reviewed media/attribution, unresolved hardware or backend limits, and a code-only rollback procedure. Include the archive hash when distributing an artifact. Avoid private machine paths, exact user locations and raw device evidence in the public-facing record.
