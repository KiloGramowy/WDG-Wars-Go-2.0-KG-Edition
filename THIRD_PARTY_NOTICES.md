# Third-party notices and media attribution

## Watch Dogs Go by LOCOSP

This fork builds on [LOCOSP's Watch Dogs Go](https://github.com/LOCOSP). Retain the original project [LICENSE](LICENSE), including its **Copyright (c) 2025-2026 LOCOSP** and MIT permission/disclaimer text. The original game, menus and upstream functionality must remain credited to LOCOSP; fork/integration and demo-editing credits are separate.

The software's MIT notice does not automatically relicense third-party fonts, music, artwork or dependencies.

## Spleen 5x8 font — BSD 2-Clause

`assets/spleen-5x8.bdf` contains the Spleen font by Frederic Cambus and its copyright/SPDX header. Preserve that header. The following full notice was checked against the [primary Spleen license source](https://github.com/fcambus/spleen/blob/5116f8eac7f65da2cfa169626ec43b5d041665fe/LICENSE) on 2026-11-01:

```text
Copyright (c) 2018-2026, Frederic Cambus
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

  * Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

  * Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in the
    documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS
BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

## Other upstream assets

`assets/font_8x8.bdf`, the character/radio sprites, sound effects and existing landing-page assets were inherited from upstream. Do not describe them as newly authored by this fork. No separate license/provenance declaration for the 8x8 font was established in this preparation; no replacement license is invented here.

Upstream artwork attribution and any applicable third-party rights remain relevant. Its presence in the original repository is not a new claim that all depicted characters or external artwork are universally cleared. Record verified provenance before adding or redistributing additional artwork/media.

## Update-window profile image

`assets/kilogramowy-avatar-32.png` is a resized copy of [KiloGramowy's GitHub profile avatar](https://avatars.githubusercontent.com/u/315514197?v=4), bundled at the account owner's request for the private updater preview. It is separate from LOCOSP's original artwork; no third-party authorship or additional image license is invented here.

## Offline geographic data

The radio geography catalog is derived from [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/), whose data is public domain. Selected enclave policy centers are derived from [Wikidata's CC0 structured data](https://www.wikidata.org/wiki/Wikidata:Licensing). Retain the source URLs, pinned revisions, scope and hashes in `watchdogs/radio_countries.json`. These data sources are not measurements of the user's location or legal/survey boundary guarantees.

## Optional demo soundtrack — Infraction, Venom (Rave)

If a release includes the audible demo using this track, credit [Infraction — Venom (Rave)](https://inaudio.org/track/venom-rave/) and comply with the applicable [InAudio terms](https://inaudio.org/license-terms/). The primary pages were checked on 2026-11-01. Their free-track video route requires a track link in the video description; it is conditional attribution, not public domain or the repository's MIT license.

Include this credit in the video's description and beside a repository/release link to that audible demo:

```text
Music from #InAudio: https://inaudio.org/
Infraction - Venom (Rave)
https://inaudio.org/track/venom-rave/

Edited excerpt: duration, fades and volume adjusted for this video.
Free-use terms: https://inaudio.org/license-terms/
```

Keep the standalone audio, download archive and unused candidate tracks out of the source package. A synchronized-video permission is not a general standalone-audio redistribution right. No paid subscription certificate or guarantee of claim-free future uploads is claimed here; tracks labelled Content ID have separate subscription conditions. A silent GIF or still image contains no soundtrack.

## Dependencies

Installed third-party Python/system packages retain their own licenses and notices. Listing or installing a dependency does not place it under this project's MIT license. Keep applicable notices when building a bundled distribution rather than a source-only checkout.

## Firmware included with 0.9.20

The compatible JanOS SOLO and Piglet Collection applications preserve their
upstream MIT licenses, source provenance and pinned build instructions. Their
third-party SDK/runtime components retain separate licenses. See
[`firmware/THIRD_PARTY_NOTICES.md`](firmware/THIRD_PARTY_NOTICES.md) and the copied
original notices under `firmware/licenses/`; neither the firmware libraries nor
the compiler runtime are relicensed by this repository's MIT notice.
