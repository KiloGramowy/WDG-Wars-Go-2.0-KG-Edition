# 0.9.21 change and update guide

**0.9.21 release and update documentation.** The [canonical Release/Discord
description](RELEASE_AND_DISCORD_EN.md) is the public-facing source. This file
records release scope, update controls and validation limits.

## What players actually get — the short version

**🗺️ The map follows. 🟩 The gangs get borders. 🐇 The rabbits get markers. 🏅 The badges get a shelf. 📡 Wi-Fi/BLE gets back to hunting instead of waiting on paperwork.**

❌ **Before:** manual map extension, no visible portal rabbits or complete trophy shelf, and the occasional GUI wait behind a busy save.

✅ **Now:** opt-in GPS-following road tiles and WDGWars turf; cached streets, visible White Rabbit targets, server-owned Portal Badges, correct KG/SOLO firmware routing and shared scan/Collection responsiveness improvements with **no new performance toggle**.

*Your CPU didn't join a filing department. It joined a wardriving crew.* 😏

The table below preserves the technical Before/Now contract. The [full Release/Discord copy](RELEASE_AND_DISCORD_EN.md) is deliberately more readable for players; this guide remains the technical detail.

## New in 0.9.21

| Area | Before | Now |
| --- | --- | --- |
| Automatic map extension | Download Map was manual. | Opt-in **TAB → SYSTEM → Auto Map + Turf → Enter**, remembered and **OFF by default**; existing manual command retained. |
| Download position | Manual downloading used its existing GPS/view-center behavior; no automatic map feature in public 0.9.20. | New automatic requests require a **measured GPS sample younger than two seconds**, while GPS is enabled. Panning and SOLO's accepted-position cache cannot request a new area. |
| Tile coverage | Manual downloads populated the local tile cache. | Automatic **10 km radius**, considering a new center after **3 km** from the last fully downloaded center, skipping existing tiles and using the existing Stadia key/source. |
| WDGWars territory | No automatic cached sector overlay. | Nearby authenticated sector fetches, local disk snapshots, **five-minute** freshness, gang-coloured outlines at zoom **9+**, measured-position **OWN / TURF** hint and stale `*`. |
| Offline view | Already downloaded tiles worked offline. | Automatic downloads join the same local tile path; saved sectors can render offline while the switch is ON. Unknown/uncached areas still need a connection. |
| Failed downloads | Manual Download Map retains its behavior. | Automatic tile batches stop after their first failed request and keep partial successful work. Failed automatic requests schedule a **two-minute** retry with fresh GPS/key requirements retained; failed territory reads do not replace good saved snapshots. |
| No-fix initial view after restart | The game could open its original default area despite a completed automatic cache. | 0.9.21 restores the last completed validated center as the initial **view** when Auto Map is ON. It cannot become a fresh GPS input or trigger area downloads/RF/SOLO. |
| White Rabbit UI | API data existed without map glyphs. | 0.9.21 draws white bunny markers for open, unclaimed, unexpired server targets; automatic reads at startup and at most every **60 seconds**, independent of GPS/Auto Map. |
| Portal Badges | Only IDs matching native achievements had a local representation. | 0.9.21 adds the full valid returned account-owned list in Wars Sync → **Portal Badges**, separately from native achievement/XP semantics. |
| Firmware source/routing | Stock upstream alerts and the old stock wizard could replace SOLO. | Automatic checks use the KG Edition release manifest, independently of game tags; **R** refreshes, **Enter** starts a compatible verified manual KG install/reinstall, **Esc** closes without cancelling an active installation. No automatic flash or stock/Piglet fallback. |
| Firmware display | The host showed the bare firmware number. | The host labels the reported version **KG 1.7.0** in its terminal/panel. This changes display text only; raw serial logs, numeric version and actual SOLO capability checks retain their meaning. No firmware flash. |
| Shared Wi-Fi/BLE GUI | Incoming scan work or idle Collection checks could wait behind another write and stall the screen. | Applies automatically to all 0.9.21 users, no toggle: small work batches and nonblocking presentation/whole-Collection gates defer busy work. |
| Large progress saves | Repeated serialization/write work could cause avoidable waiting on large collections. | Prepare a compact save once and write in one pass; values, receipt/recovery rules and rewards remain. |
| Serial input/commands | Bursts incurred repeated split work and commands added an artificial 100 ms wait. | Split bursts once; remove that avoidable wait, keeping real START/STOP acknowledgement requirements and scan timings. |
| Damaged observation line | A truncated prefix could hide a complete following observation. | Strict recovery of one complete valid follow-up OBS; never invent truncated/absent records. STOP sequence diagnostics remain honest. |
| Upload-age retention | Full supported uploads with duplicate/no-GPS leftovers could miss eligibility; legacy flags/log dates did not prove upload age. | Persist the first complete real upload confirmation for the unchanged content/account; batch completion waits for all ACKs. **After 5 days** uses that clock, never session date/mtime/reseed time. Keep all / 1 / 5 / 15 / 31 / custom and manual D controls stay. Invalid/unreadable and no-GPS-only unconfirmed files gain no automatic expiry proof. |
| Wardriving Collection | Piglet KG fourteen-column WiGLE CSV receiver. | Supports Dark3D eleven-column WiGLE `.log` too; typed authenticated peer status, original bytes/metadata, PC1/durable ACK and deduplication retained. Host compatibility only; no new physical sender validation or Dark3D image. |
| Registry duplicate notice | A mojibake-only literal blocked the documented Unicode duplicate response. | Accept the exact Unicode notice and legacy spelling; still require exact authenticated mesh registry proof. No invented ACK time, server credit or XP. |
| Manual closed CSV deletion | Confirmed-upload status could block D and sent/not-sent status was unclear. | Visible **SENT / NOT SENT**. For one closed CSV, **SENT + D** deletes immediately; **NOT SENT + D** warns not uploaded, **Y** confirms and **N / Esc** cancels, including no-GPS unuploadable files. Progress/receipts stay; active/upload locks and Auto-clean rules are unchanged. |
| Refused firmware preflight | A refused new-client check could leave the board waiting for its application. | Before any write, request the old application's restart on the same bound board; require a fresh matching version response to confirm return. The refused installation is never reported as success. |
| KG reconnect metadata | A previous connection's version/pending response could remain visible. | Ask for a fresh version and recheck KG after reconnect; stale earlier-connection replies cannot replace current information. |

The cache/settings guards fixed during private DEV preparation belong to the
new feature: non-object settings and invalid numeric/cache fields no longer
interrupt its startup/refresh. These are **not advertised as a defect in public
0.9.20**, which had no Auto Map feature. Invalid source cache files are not
deleted just by reading them.

**Default street-basemap credits:** © [Stadia Maps](https://stadiamaps.com/attribution/) · © [OpenMapTiles](https://openmaptiles.org/) · © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).

## WDGWars API methods versus visible UI

**Private-candidate corrections:** Before, opening/R refresh could wait behind
upload-file work, a malformed ownership reply could replace good cached
ownership and a corrupt timestamp could interrupt startup. Now refresh
scheduling is separate from upload-file work, invalid ownership is rejected
as a whole and the old cache kept, and invalid timestamps are handled safely.
These are corrections found before publication, not defects claimed in 0.9.20.

| Capability in private candidate | User-visible scope |
| --- | --- |
| Bounded member-territories reads | Used by the opt-in local map overlay. Ownership comes from the server; movement alone captures nothing. |
| White Rabbit API + automatic controller | White bunny map glyphs, existing pan/zoom, no new screen/key. Account-bound cached targets expire offline; last-known availability can lag a caught event until a good refresh. No capture request, hacking target or local XP award. |
| Game State API client | On-demand client only; no new Game State screen; no GPS is sent unless explicitly supplied by a caller. |
| Device Pairing API client | On-demand client only; no new interactive pairing screen; polling does not adopt/save a key automatically. |
| Approved device User-Agent and actual `game_xp` metadata | Existing signed nonempty observation uploads carry the agreed metadata; no new XP source or automatic observation uploader. |
| Portal-owned badges | Existing Sync → **Portal Badges → Enter**; Up/Down, Page Up/Down, Home/End, **R** GET refresh, **Esc** back. Opening also refreshes. Full valid returned IDs/labels are account-bound and persisted separately; panel does not POST or award native badges/XP. |

The technical API methods are described in the [portal parity guide](../WDGWARS_PORTAL_PARITY_DEV.md).
No private owner-only integration or implementation is part of the public copy.

## Preserved from 0.9.20

- **WARDRIVING SOLO:** all existing profiles, independent Wi-Fi/BLE choices,
  confirmed START/STOP and existing scan parameters; SNIFF remains available.
- **GPS:** Startup GPS Backfill and the **720-minute** accepted-position cache
  keep their policy. Fresh automatic-map GPS is separate from that cache.
- **Piglet Collection:** paired delivery, durable whole-CSV import, raw source
  retention, account-bound upload receipts and deduplication remain.
- **Rewards and saves:** **15 / 10 / 1 XP** for new Wi-Fi/new BLE/repeats,
  existing XP/level/achievements/badges/loot/history, final-save and shutdown
  protections. No recalculation of previously earned rewards.
- **Uploads/settings:** existing manual upload controls, signatures, retry,
  acknowledgement coverage, pending data and chosen Auto-clean policy.
- **Radio:** MeshCore, Meshtastic, AUTO GPS and manual regional RF profiles;
  this host update neither flashes firmware nor changes ESP32 partitions.

These are source/preservation boundaries. Preservation on a particular device
needs a real before/after comparison and must not be inferred from this list.

## Manual deletion is separate from automatic cleanup

**PLUGINS → Pending Upload Logs** shows **SENT / NOT SENT**. Select one
**closed** CSV. **SENT + D** removes it immediately, without confirmation.
**NOT SENT + D** warns that it has not been uploaded; **Y** confirms deletion,
**N / Esc** cancels. A closed no-GPS export that cannot upload gets this same
unsent warning and choice.

Only the chosen CSV export is removed; XP, progress, contacts and saved server
receipts remain. This is not an upload or server credit. Current writing sessions
and active-upload locks stay protected. Automatic cleanup retains its existing
confirmation/eligibility and configured retention rules.

## Upload-age retention — first complete confirmation

In Pending Upload Logs, **A** opens the existing **Keep all / 1 / 5 / 15 /
31 days / custom** choices. **Up / Down → Enter** applies, **Esc** cancels.
Choosing **After 5 days** means five elapsed days from the first recorded
complete acknowledgement of the exact supported upload for the unchanged file
and account. A batched upload is complete only when every required part is
acknowledged; the completion time follows the last required ACK. Rechecking,
restarting or reseeding the same matching proof does not reset the clock.

Legitimate deduplication and no-GPS residual source lines do not imply new
server credit and do not strand a fully acknowledged supported representation.
No-GPS-only NOT SENT data is not certified as uploaded by this policy; manual
D remains available. Structurally invalid, unreadable, changed or unverified
files stay protected. Existing active-session/upload locks remain.

A legacy uploaded flag alone is not a timestamp. Where exact older batch
coverage exists but required ACK times are missing, use the conservatively
established full-completion time, never an invented past upload date. Session
names, capture dates and filesystem timestamps cannot backdate expiry.
The separate existing no-GPS-after-upload-attempt behavior is not redesigned
by this retention fix; do not confuse it with upload-age certification.

## Open-source distribution and the existing updater

The complete game, including the KG changes, remains open source. Keep LOCOSP's
MIT license, original authorship and the applicable third-party notices. Use the
existing verified Git/source updater and normal source plugin loader. The former
closed-binary/Nuitka/APT proposal is historical and imposes no current release
architecture gate. Python/system dependencies retain their own licenses and are
installed normally; this candidate does not bundle a closed game executable.

Only the separate Dark3D firmware project may later supply an approved binary.
Its private sources, licensing decisions and build environment are outside this
game release. No modified Dark3D Collection sender image is publicly supplied.
Firmware documentation and future `.bin` downloads: [Dark3D C5 UConsole](https://github.com/KiloGramowy/Dark3D-C5-Wardriver-UConsole) (**currently private**, separate release approval required).
The public update feed identifies the approved release; it is bound to a public source commit, not a private snapshot.

## Player update instructions

1. Open **TAB → SYSTEM → Update Game** and press **R**.
2. Read the full notes; **Up / Down**, **Page Up / Page Down**, **Home / End**
   scroll. **O** opens the verified source. **Esc** returns.
3. When the approved update is offered, press **Enter** to prepare it.
4. In the separate confirmation, **Y installs; N / Esc cancels beforehand**.
   Opening the new-release notice does not install anything.
5. Updaters **0.9.19.1+** restart after a successful installation. Preparing,
   cancelling or failing does not restart. A running **0.9.17 / 0.9.18 /
   0.9.19** needs one manual relaunch for its first upgrade: after installation,
   **Enter or Esc** closes the game; then start it again.
6. An unsuccessful automatic restart offers retry or manual relaunch without
   rolling back installed code or saved player data. **No game OTA flashes
   JanOS or Piglet.** ZIP source archives lack Git history and cannot use the
   current in-game installer; use the documented clone migration.

The game offers the release approved by its public update feed. Opening these
notes alone does not install an update.

## Existing installation and rollback boundary

Inspect the actual running installation, local edits, code manifest, launcher,
data locations and private local functions first. Compare only intended changed
code files, keep verified minimal private backups of those files and a recovery
receipt, save progress and stop game-owned workers cleanly before replacement.
Do not use a blind pull, hard reset, clean, full reinstall or old data snapshot.

A rollback restores **code**, then relaunches through the normal graphical
launcher. It must preserve XP/history/settings/queues created after the code
backup. Reconcile local code conflicts rather than overwriting local functions.
No credentials, raw private logs, device backup or location-bearing screenshot
belongs in the publication archive.

## Known limits and open evidence

The earlier shared scan/Collection baseline passed complete Linux and Windows software gates:
**1679 Linux PASS / zero SKIP**, **1665 Windows PASS / 14 platform-root SKIP**;
all 14 passed on Linux. **164 tracked Python files** compiled on both platforms.
A bounded physical **120-second / 480-sample** combined-scan check recorded
no second-long gap in sampled rolling frame timings (maximum **151.93 ms**).
Three closed captures accounted for **1150 raw/durable/STOP observations** with
zero observed gaps or malformed messages. Status publication sometimes lagged.
Normal shutdown/save passed; final post-relaunch/source/data/notes checks are
reported separately. Earlier missing/truncated rows remain historical evidence,
without a blanket lossless, universal FPS or zero-lag claim.


The owner-approved own-fork firmware client supersedes the interim read-only
policy; the KG display remains. These are later checkpoints after A3/B. Exact current software results and physical-panel observations are
reported separately in the validation/device records; old totals are not a
full run of later code. The approved historical KG/SOLO package exists; absence
of an automatic channel does not mean no package exists.

- Fresh GPS, network access and the relevant configured key are required for
  **new** automatic downloads. No fresh GPS means no new automatic area requests.
- Existing cached sectors can be stale. The hint's `*` shows age over five
  minutes; offline use is not live ownership validation.
- Colours approximate the game's 16-colour palette. Sector outlines appear at
  local zoom 9+; no alternate map UI or capture rules are introduced.
- New automatic neighborhoods exclude the polar limits and dateline crossings.
- White Rabbit now supplies map glyphs, and Portal Badges has a list inside
  existing Sync. Game State and interactive Pairing still have no new screen.
- Actual rabbit/badge/cached-view GUI checks passed on A3/B. Current complete
  Linux/Windows software results, fixture/root conditions and any platform
  skips are scoped in the canonical description and validation record.
- Multiple physical Piglets remain unsupported pending identifier correction
  and hardware validation. Firmware and existing identities stay unchanged.
- Current private installation, visible GUI, saved switch and **61 manual
  tiles / zero errors** have been checked on the physical uConsole. Manual
  downloading used the default map view **without a fresh GPS fix**.
- The first live authenticated read-only territory audit returned **67 cells** whose
  names/colours were accepted by the current parser, using an authentic
  **historical receiver area**. White Rabbit and Game State reads also passed;
  no pairing, fictitious observations or test XP were submitted. This does not
  establish physical turf rendering or fresh-GPS movement. Later a genuine
  brief fix triggered **nine regions / 57 cells** and real coloured outlines.
- ON persisted across a second normal restart; test settings and the owner's
  modes were restored. Final enable choice follows the player. Data/settings/source comparisons and
  SQLite integrity passed. Local tiles rendered again after restart with the
  network still available, and the game remained visibly running in its own
  detached session with normal hardware connections.
- Physical firmware installation/ROM recovery through the new client, **3 km relocation**, the current-cell hint with a captured fresh fix,
  and full internet-loss recovery remain unverified. Host fakes,
  manual default-view downloads and historical 0.9.20 evidence cannot close
  those items. The short warm frame sample had p95 about 34.5 ms and transient
  cache/refresh spikes around 167–199 ms, not a zero-lag/sustained-FPS guarantee.
- The maintained private DEV installation's Update Game window reports
  **“Cannot verify update history; manual check required.”** No verification
  guard was bypassed. This is not a public 0.9.21 offer, and a public 0.9.21
  installation transaction has not been activated or tested.

See [the software validation record](../RELEASE_0.9.20.md). Final copy
must match the actual final report, with **PASS / FAIL / SKIP / NOT VERIFIED**
kept distinct.
