# 🛰️ RF profiles and local AUTO GPS

Choose the LoRa **protocol manually**. AUTO GPS only recalls an explicitly approved RF profile for the selected protocol and local area. It never discovers nearby radio settings or turns a local observation into a country-wide default.

## Controls

Open **ADDONS → Meshtastic Profile** or **ADDONS → MeshCore Region**.

| Key | Action |
| --- | --- |
| ↑ / ↓ | Highlight a profile |
| M / Enter | Select MANUAL and apply or save the highlighted RF profile |
| A | Select AUTO GPS for this protocol |
| S | Preview **Save for this area**, after a successful manual selection and a stable fix |
| F | Preview removal of a local assignment |
| Y | Confirm the displayed save/remove operation |
| N / Esc | Cancel a confirmation |
| Esc | Return from the profile menu |

MANUAL works without GPS and is not overwritten by it. The highlighted row is a choice, not proof that RF was applied. Check **Active RF** and the radio status. Retuning waits for safe radio ownership and pending transmission handling; a failed start does not become an active-radio indicator.

## Remembering an area

The save dialog shows the protocol, profile, center and **2 km radius**. Only **Y** creates the approval. Merely selecting a profile, entering a country or pressing S does not approve anything. Saving an assignment also does not enable AUTO by itself.

MeshCore and Meshtastic assignments are independent. An approval never silently moves, expands or becomes an approval for the whole country. The displayed center remains fixed during confirmation; an invalid fix or a move outside its safe interior requires another preview.

AUTO applies a matching assignment only inside its conservative inner area, below **1.8 km** from the center. The outer 200 m is an uncertainty margin. Missing, conflicting or ambiguous assignments keep the current RF settings and require a manual choice. Removing an assignment does not erase contacts, channel keys or player progress, and does not immediately retune the radio.

## What counts as a GPS fix?

AUTO uses measured GPS data, not the player's manually moved map position. Its checks include a valid GGA checksum, accepted fix quality, at least four satellites, HDOP no greater than 3, and a measurement no older than **10 seconds**. Re-reading the same measurement does not make it fresh.

Selection also needs at least **five distinct measurements over 20 seconds** of stability. Conflicting measurements and implausible jumps do not trigger a retune. Losing the fix keeps the active RF profile and displays that GPS confirmation was lost.

There are conservative guards near mapped polygon/coast edges and international borders. These are application safeguards, not survey accuracy or a radio-coverage prediction. Unknown or unsupported geography remains unknown; the game does not guess from language, city names, IP address or time zone.

## RF catalog

These are the profiles currently defined in the code, with documentation review dated **2026-10-29**. Frequencies are MHz and bandwidths are kHz. Availability in the catalog is not a claim that a nearby network uses that profile or that every preset has been physically tested.

| Protocol | Profile ID / label | Region | MHz | BW | SF | CR |
| --- | --- | --- | ---: | ---: | ---: | --- |
| Meshtastic | `sfnarrow` — ES SFNarrow | EU_868 | 869.618 | 62.5 | 7 | 4/5 |
| Meshtastic | `longfast` — EU/UK LongFast | EU_868 | 869.525 | 250 | 11 | 4/5 |
| Meshtastic | `us_longfast` — USA LongFast slot 20 | US | 906.875 | 250 | 11 | 4/5 |
| Meshtastic | `us_longturbo` — USA LongTurbo slot 14 | US | 908.750 | 500 | 11 | 4/8 |
| MeshCore | `eu_uk_narrow` — WDG legacy CR5 | EU_868 | 869.618 | 62.5 | 8 | 4/5 |
| MeshCore | `eu_uk_narrow_v2` — Narrow CR8 | EU_868 | 869.618 | 62.5 | 8 | 4/8 |
| MeshCore | `eu_uk_default` — Deprecated Wide | EU_868 | 869.525 | 250 | 11 | 4/5 |
| MeshCore | `us_recommended` — USA Recommended | US | 910.525 | 62.5 | 7 | 4/5 |
| MeshCore | `us_ca_narrow` — WDG legacy Narrow | US | 910.525 | 62.5 | 7 | 4/5 |
| MeshCore | `us_ca_default` — WDG legacy Wide | US | 910.525 | 250 | 11 | 4/5 |
| MeshCore | `anz_narrow` — WDG legacy | ANZ | 915.525 | 62.5 | 7 | 4/5 |
| MeshCore | `in_narrow` — WDG legacy | IN | 865.525 | 62.5 | 7 | 4/5 |

The AU/NZ and India legacy entries do not extend the verified AUTO geography to those countries. No country has an implicit approved RF assignment.

📌 A Spanish MeshCore operator documents a local **SF7 / CR4/6** variant. That is a candidate for separate implementation and testing, **not a profile currently included here**. It is also different from Meshtastic SFNarrow's SF7 / CR4/5. A map label, a Spanish GPS fix, or silence on EU/UK settings does not establish the correct local MeshCore preset.

## Channels and hardware are separate

Changing RF preserves the existing channel/key configuration. Meshtastic framing and sync settings are handled by its own protocol implementation; MeshCore settings are not interchangeable. A channel name alone is not evidence of matching frequency, bandwidth, spreading factor, coding rate or framing.

Real SFNarrow reception has been recorded on a uConsole setup. This does not qualify all presets, every antenna or a coverage area. US profiles remain unqualified on physical RF/antenna performance in this validation; the SX1262 chip's tuning range alone does not establish the complete module, filters and antenna capability. Do not test a profile outside the applicable regional conditions merely because it appears in the menu.

## Geography and sources

The finite offline catalog covers ES, GB, DE, FR, PL, NL, BE, PT, IT, CZ, AT, DK, SE, NO, FI, IE, CH and US, with documented territorial exclusions and conservative handling of disputed/boundary areas and enclaves. It is not worldwide geocoding or a map of local mesh presets.

Sources retained with the implementation:

- [Meshtastic Spanish community setup](https://meshtastic.es/docs/guias-basicas/configuracion-inicial/).
- [Pinned Meshtastic firmware RF implementation](https://github.com/meshtastic/firmware/blob/332b5b0a2c4fec2896de3c5fa63f46d5cd05ea98/src/mesh/RadioInterface.cpp).
- [MeshCore configuration catalog](https://api.meshcore.nz/api/v1/config) and [preset documentation](https://github.com/meshcore-dev/MeshCore/blob/main/docs/radio_presets.md).
- [Local MeshCore operator parameters](https://www.radioclubcapitol.es/capitolmesh/parametros-radio), used as documentary context rather than a deployed or verified local profile.
- [Pinned Natural Earth source](https://github.com/nvkelso/natural-earth-vector/tree/ca96624a56bd078437bca8184e78163e5039ad19) and [public-domain terms](https://www.naturalearthdata.com/about/terms-of-use/). Source hashes and scope metadata remain in `watchdogs/radio_countries.json`.

Source listings may change after the review date. Check a network's documented settings before making a new local approval. See [Validation](VALIDATION.md) for what has actually been checked.
