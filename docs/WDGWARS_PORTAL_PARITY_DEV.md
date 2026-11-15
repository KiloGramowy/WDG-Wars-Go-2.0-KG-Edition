# 📡 WDGWars Portal API parity — private DEV

**Source reviewed:** [LOCOSP/WatchDogsGo @ 7427fbfc](https://github.com/LOCOSP/WatchDogsGo/commit/7427fbfc9b0d62241cb3b03d7845f3328ce14da7) (12 November 2026).

The existing KG fork already supports signed wardrive upload, badge sync, `GET /api/me`, web login PIN and the familiar Wars Sync user interface. No source-file replacement from upstream was performed: our fork's batch uploads, HMAC, account snapshot, deduplication, retention, shutdown and receipt logic are preserved.

## Added API client methods

The existing `plugins/wardrive_upload.py` now exposes these **headless**, on-demand methods:

The transport methods remain headless, but **candidate A now connects White
Rabbit to a real map layer and adds a read-only Portal Badges view**, following
the owner's later explicit instruction. The API-only boundary below continues
to apply to Game State and interactive Device Pairing, not to the new rabbit UI.

| Method | Portal route | Behavior |
| --- | --- | --- |
| `fetch_rabbits()` | `GET /api/rabbits` | Authenticated White Rabbit target list; `[]` on failure |
| `fetch_rabbit_snapshot()` | `GET /api/rabbits` | Bounded structured snapshot including server time; used by the automatic map marker controller |
| `fetch_game_state(lat=None, lon=None)` | `GET /api/game-state` | Authenticated rank/team/territory/contracts; optional coordinates are sent **only when passed explicitly** |
| `fetch_member_territories(bbox, zoom=8)` | `GET /api/member-territories?compact=1&bbox=...&zoom=8` | Bounded authenticated territory grid for the opt-in GPS-following map |
| `request_pair(label=...)` | `POST /api/device-pair` | Initiates a code-pairing request; does not require an existing API key |
| `poll_pair(code, poll_secret)` | `POST /api/device-pair` | Polls a pairing request and returns pending/key/error; **does not save the key** |
| `adopt_api_key(api_key)` | Existing `secrets.conf` storage | Explicitly validates and saves a 64-hex key, then starts the existing auth poll |

All network operations use the existing `_open` wrapper with `WatchDogsGo/<version>` User-Agent. The transport methods themselves are on-demand. **White Rabbit** now has its own automatic caller independent of GPS/Auto Map; **Auto Map + Turf** separately fetches territory while ON with fresh measured GPS. Fake-HTTP coverage and later actual device/API evidence are separated in [the validation record](ota-0.9.21/CHANGELOG_AND_UPDATE_GUIDE.md).

## User-visible portal completion — candidate A

Private source **`30858f105ed4811b94a3e6925030b6a7d2e0476d`** implements:

- **White Rabbit map glyphs:** white bunny markers on the existing map, no new
  key/screen, normal pan/zoom and viewport clipping. First configured frame
  starts a bounded read; interval/retry is at most once per **60 seconds**.
  This global target GET is independent of GPS and Auto Map, and sends no player
  coordinates. Only open, unclaimed, unexpired targets are retained. Account-
  bound ignored local snapshots survive errors; deadlines continue offline.
  A good empty response clears old targets. Caught/claimed targets disappear
  after the next good server response; offline data is last-known availability.
- **Portal Badges:** **PLUGINS → Watch Dogs Go Wars Sync → Portal Badges → Enter**.
  Up/Down select, Page Up/Down page, Home/End first/last, **R** read-only refresh,
  **Esc** back. Opening also starts a GET. Full valid returned IDs and labels
  are cached separately from native achievements, scoped to server/account.
  Key/URL changes hide old ownership; late responses cannot cross accounts.
  Supplied labels/descriptions are used, otherwise IDs get a readable label.
- The panel sends **no badge POST** and awards **no local XP or native badge**.
  Existing **eight native mappings** keep their old import behavior outside the
  panel. Unknown legacy state is retained. No capture action/rule, hacking
  target, Game State screen, pairing screen or local API server is added.

Initial source checks: White Rabbit **28 run / 27 PASS / one Windows-platform
SKIP**, **21 portal badge tests PASS**, and **25 portal parity tests PASS**.
The saved real rabbit response had 37 records; the isolated parser kept **36
open** and excluded one caught record. Later final code A3 passed its full
Linux suite; Windows A plus A2/A3 follow-ups are scoped in the validation record.
**Physical GUI PASS:** real WORLD-map rabbit glyphs, Portal Badges first/last
pages for all 48 owned entries, and Home/End/R GET refresh. Read-only checks
left native achievements and XP unchanged. Normalized original plugin state
matched its protected pre-UI digest after excluding only the new badge cache;
secrets content was unchanged. No profile badge IDs or private values are
committed. This does not establish capture rules or create local awards.

### Full LOCOSP upload metadata parity — approved by the owner

LOCOSP's two additional **outgoing** fields are now implemented in the DEV plugin:

- **User-Agent:** on Linux it is `WatchDogsGo/<version> (uConsole-<6-char-hardware-fingerprint>; Python/<x.y>)`. The six-character suffix is calculated locally from hardware serial / machine ID (with an existing MAC-based fallback). Raw serials and machine IDs are not transmitted. This is a stable, pseudonymous identifier visible to the WDGWars portal; the `WatchDogsGo/` product prefix is retained. Non-Linux builds retain their platform hint.
- **`game_xp`:** a positive total read from `app.xp` is included only in the signed JSON for nonempty real Wi-Fi/BLE, aircraft or mesh upload batches. It is the **actual in-game total** including any locally earned points; the game does not disclose the code or event history responsible for those points. The upstream portal displays the declared total on the profile, independently of server-side ranked credit. Empty API pings carry no `game_xp`.

The existing HMAC-SHA256 envelope, account/key selection, deduplication and upload receipts are unchanged. The signed-body batch-size planner accounts for the additional field within its existing 6 MiB limit, and the original input record dictionaries are not mutated. This is *not* an XP award or a new uploader.

**No GPS is sent by the game-state client unless a caller supplies coordinates explicitly.** LOCOSP's original two-file API commit did not implement rabbit/game-state/pairing UI. The owner's later explicit instruction now authorizes only the rabbit map glyphs and full portal-badge view described above. Game State and interactive pairing screens remain absent. Transport methods do not initiate background requests themselves; the new White Rabbit controller is their explicit automatic caller.

## Scope and release boundaries

- Integration target: the private `KiloGramowy/WDG-Wars-Go-DEV` `develop` branch only.
- Published WDGWars GO 0.9.20, public firmware, Release/tag and OTA feed are unchanged. A later approved private 0.9.21 installation on uConsole is recorded separately; it is not public OTA activation.
- No private CSV, secrets, GPS tracks, Piglet archives, AUTO-PWN or player data are committed.
- The owner approved the device User-Agent and total-game-XP metadata. The later
  rabbit map layer and Portal Badges view are also explicitly approved. This
  does not approve new capture/XP policy, unrelated screens or a local server.

**Software validation, 13 November 2026:** all **22 portal API tests passed**, using synthetic HTTP responses and real local signing/batch planning. The complete host suite also passed with its explicit skips; see [ota-0.9.21/CHANGELOG_AND_UPDATE_GUIDE.md](ota-0.9.21/CHANGELOG_AND_UPDATE_GUIDE.md). That synthetic result alone does not establish authenticated live portal compatibility, physical deployment or firmware installation; the later read-only/device evidence follows separately.

**Later actual read-only checks on the privately updated uConsole:** White Rabbit returned HTTP 200 with **37 entries**; Game State returned HTTP 200 without coordinates; member-territories returned HTTP 200 with **67 cells**, grid **0.02**, accepted by the current parser including names/colour fields. The bbox came from an authentic **historical saved receiver area**, not a fresh fix or movement. No observation/XP POST, pairing request, key adoption or live-game territory-cache write was performed by that audit. Device Pairing remains live **NOT VERIFIED**; accepted data is not proof of a physical overlay or new portal screens. See [the execution report](ota-0.9.21/CHANGELOG_AND_UPDATE_GUIDE.md).

**Earlier badge/rabbit UI gap audit, before candidate A:** `/api/me` returned
**48 badge string IDs**; the existing native-game mapping recognizes **five**
and ignores **43 unmapped IDs**. No callback, badge write, XP write or POST was
performed. Existing supported badge sync is not full portal badge-UI parity.
The 37 rabbit records include latitude/longitude fields, but that response is
not a rabbit-marker screen and does not validate capture rules. That audit did
not add UI; the subsequent approved source change now retains the returned
portal IDs and implements map glyphs separately. Capture rules remain unchanged.

> 🐇 Follow the White Rabbit later. First make sure the rabbit has the right API endpoint — and isn't carrying your credentials in its pockets. 😏
