# Preparing a source release

The complete WDG Wars Go KG game remains **open source**, with the original
LOCOSP [MIT license](../LICENSE), attribution and [third-party notices](../THIRD_PARTY_NOTICES.md).
The existing verified Git/source updater and source plugin loader remain.
Closed game executables, Nuitka, binary OTA and a new installer are outside the
current plan. The separate Dark3D firmware is a different project.

## 0.9.21 preparation record

Read the [source package guide](ota-0.9.21/SOURCE_RELEASE.md),
[canonical Release/Discord text](ota-0.9.21/RELEASE_AND_DISCORD_EN.md),
[changelog/update guide](ota-0.9.21/CHANGELOG_AND_UPDATE_GUIDE.md) and
[one owner acceptance checklist](ota-0.9.21/PREPUBLICATION_CHECKLIST.md).
This records the preparation of 0.9.21. Current availability is shown by the
[public Releases](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases)
and the approved public update feed; this document is not a live publication receipt.

## Preserve public ancestry and private boundaries

Prepare reviewed source/media, never a copy of a live device directory or a
push of private DEV history. The formal public fork remains
`KiloGramowy/WDG-Wars-Go-2.0-KG-Edition`, repository ID `1442123109`, with public
LOCOSP ancestry. Retain existing private branches and reports in DEV. Review the
latest public main before applying a candidate so concurrent work survives.
The complete source snapshot contains all approved game changes, firmware
patches/sketches, tests, assets and notices. It contains no private Dark3D source.

Exclude loot, secrets, identity keys, state, GPS/radio captures, private media,
backups and intermediate builds. Keep synthetic tests and public protocol/data
constants intact. Check media pixels and existing external attachment access;
package hashes do not certify privacy in an image or future anonymous links.

## Validate and bind the exact version

Use one immutable candidate commit with fresh software results, per-case skips,
syntax/import checks, native firmware contracts and the actual Update Game
formatter/font/scroll/version-filter checks. Historical test totals and physical
screenshots retain their source/date. Record ZIP SHA256/size and per-file hashes.
The existing source exporter reads verified Git blobs without history or EOL
conversion and rejects unsafe paths, secrets, broken links or stale media.

Only after publication approval, apply the reviewed tree on current public
main, commit its exact approved code and notes, then bind the existing schema-2
stable feed to that **public source commit**, repository identity, version,
release identity and canonical notes. A private RC SHA or source ZIP URL is not a
valid update source. A feed-only descendant may represent the same version only
under the existing identical-runtime check. No updater redesign is required.

Reattach the exact KG/SOLO manifest/image expected by the firmware client to a
later latest Release; game 0.9.21 is not firmware 1.7.0. Review all asset/component
rights, source/build inputs and notices. Do not claim that a game MIT notice
relicenses third-party dependencies, music, artwork or firmware components.

## Player data and rollback

Before a separately authorized deployment, verify private backups of current
loot/XP/history, settings, keys, contacts, pending exports and receipts. Compare
actual deployment manifests/file hashes rather than an old device Git HEAD.
Use the existing code-only rollback to preserve newer progress. Source ZIPs
lack Git history and cannot use the in-game installer; follow the genuine-clone
[migration guide](UPGRADING_FROM_LOCOSP.md). No device was installed, restarted or
flashed by the source-release preparation. See [Updating](UPDATING.md) for existing controls.
