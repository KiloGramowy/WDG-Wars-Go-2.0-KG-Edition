# 0.9.21 source release and preparation record

WDG Wars Go 2.0 KG Edition remains fully open source. Preserve the original
[MIT license](../../LICENSE), LOCOSP authorship, KG credits and
[third-party notices](../../THIRD_PARTY_NOTICES.md). The existing Git/source
updater and source plugin discovery remain unchanged. Normal Python/system
packages retain their licenses; there is no bundled closed game executable.
Scapy GPL source obligations are not evaded by this release model.

## Reviewed source and preparation record

The release was prepared as a reviewed source-only snapshot containing the
complete game runtime, plugins, assets, firmware source/build inputs, tests and
notices. The source exporter checked unsafe paths, secrets, local links and
media hashes without rewriting Git blob bytes. Device data, credentials,
private operational reports and private development history are not release inputs.

Use the public `v0.9.21` tag and public repository history for the corresponding
source checkout. A downloaded source ZIP is not a Git checkout; the existing
in-game updater requires a real clone. See [Updating](../UPDATING.md).
The approved game feed binds the release to a **public source commit**, never
a private source identifier or a source-ZIP URL. No updater redesign is involved.

## Firmware assets for the existing KG client

The client checks the own-fork **latest public Release** and requires exactly:

| Release asset | Bytes | SHA256 |
| --- | ---: | --- |
| `janos-solo-manifest.json` (copy of `firmware/wardriving-solo/manifest.json`) | 4678 | `7a2332f0bb8325e4984e458dcd78e8443d4d39fdfb2b6b1db24223679f3f9741` |
| `janos-1.7.0-solo-xiao-c5-app.bin` | 2250896 | `e3dff6b534ff8d7712d37e181a3d2d1e30d3672877b6925be2a4bf27c8a989c5` |

These are exact already-published v0.9.20 bytes, restored from its code commit
`e78179a1e316e838f67149c40acf5f2b5f174cea`; **no new firmware build or flash**.
The legacy manifest hash is explicitly accepted by `watchdogs/kg_firmware.py`.
Do not rename the manifest asset, use game version 0.9.21 as firmware version,
or omit assets from a later game Release: the client fails closed rather than
falling back to stock firmware. The private schema-2 draft is optional future
metadata, not an activated channel. The approved target stays XIAO ESP32-C5
revision 1.0 / 8 MiB / ota_0 at 0x20000 with capacity 0x3F0000, app-only.

The existing Piglet BIN/manifest and all open firmware sources/build guides also
remain. Their own target/layout and [component notices](../../firmware/THIRD_PARTY_NOTICES.md)
apply. They are unrelated to the private Dark3D firmware. The game-side
Wardriving Collection parser does not supply a public modified Dark3D sender.

## Evidence and limits

Pre-release software checks and the checksum-only repair are summarized in the
[preparation record](PREPUBLICATION_CHECKLIST.md). Historical totals and synthetic
Pyxel previews retain their scope. Physical ARM64/device runtime, root-owned
staging and platform-specific omissions are not established by source availability.
Retain inherited asset provenance and component notices; source availability
alone does not relicense third-party artwork, fonts or media.
