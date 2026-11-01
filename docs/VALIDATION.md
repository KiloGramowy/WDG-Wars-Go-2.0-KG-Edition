# ✅ Validation and known limits

This page separates recorded software checks from real hardware evidence. Preparing documentation or a release branch does not itself repeat any radio, GPS, chat-delivery or server test.

## Formal public fork source transition

The formal release fork is repository ID `1442123109`, prepared as `KiloGramowy/WatchDogsGo-fork` and renamed to `KiloGramowy/WatchDogsGo` at verified cutover. Its public parent and source are `LOCOSP/WatchDogsGo`, ID `1208909098`. The private development repository and its five branches remain separate.

Preparation merge `be199c2ced736192399949bbf2b68098a4b5ae57` joins public LOCOSP commit `6412603d6fa7c8f2e6fe76873531d83c61adc63d` with the reviewed published snapshot `29412dccbfe9aff4c798252a7510d4b0314d7168`. That merge retains the reviewed snapshot while establishing public ancestry; it does not import the private development branches. Subsequent source-binding and feed changes require their own recorded verification.

The game version remains **0.9.17**. Existing standalone builds pin the former repository ID `1441947361`, so the maintained uConsole requires a controlled code-only 0.9.17 → 0.9.17 migration with a full verified source manifest. Reusing the final repository name does not retarget those installations. The old standalone is removed after the authorized checks; no legacy update or handoff service is retained.

| Final verification area | Required record |
| --- | --- |
| GitHub fork relationship | Verify ID `1442123109`, `fork: true`, public LOCOSP parent/source ID `1208909098`, one `main` branch and the final canonical name. |
| Source and feed | Record the approved formal-fork code commit, schema-2 feed, version 0.9.17 and matching repository ID. |
| Software checks | Record fresh commands, totals and any skips for the final candidate; the earlier standalone totals below are not new results for this source transition. |
| Maintained uConsole | Verify the code-only migration, complete runtime manifest, current player/configuration fingerprint, Proximity pairing and code-only rollback. |
| Public access after cutover | Recheck anonymous source, feed, images and native film after removal of the old standalone, without relying on the removed ID or prior attachment permissions. |

These are verification requirements, not additional passing test totals. No new RF, approved AUTO, chat-delivery or server-credit result is established by changing repository ancestry or update identity.

## Earlier standalone 0.9.17 validation

The following records concern the earlier standalone publication, repository ID `1441947361`, and runtime source `aef9e2ef9792249049fba2d30e7d105d64073e61`. Its preparation began as a private review with a `-1` suffix. These results remain evidence for that source and do not replace final checks of the formal fork's new identity, feed or deployment.

| Check | Result |
| --- | --- |
| uConsole full software suite as root | **566/566 PASS, no skips**, with LoRaRF 1.4.0 and the private recorded-frame fixture supplied outside Git. |
| Windows full software suite | 566 cases; OK with 14 reported platform/private-fixture skips, no failures/errors. |
| Update UI and publication guards | 35 UI cases and 27 publication-helper cases are included in that full suite. Rename, identity, network failure and lost-response cases use isolated fixtures; those cases did not perform a real publication. |
| Real private update transport | Numeric-ID lookup and authenticated fetch passed. A fresh clone reports 0.9.17 current; an isolated 0.9.16 checkout prepares and installs it, then reports current. All 87 runtime files match the approved code, including executable bits and offline notes. |
| Code-only rollback | The isolated installation returns to 0.9.16 and its earlier notes while preserving newer synthetic progress, settings, pairing and pending logs. Eight additional private migration regressions cover interruption, write failure and refusal of edited source/data. |
| Installed uConsole | Controlled code-only deployment, full 87-file source manifest, native Update Game display and full game startup passed. The check reports current from the clean review repository; ESP32 serial opened and cleanup completed without errors. |
| Preservation | Current player data, keys, contacts, configuration, Proximity pairing and legacy recovery reference were unchanged. Fresh code/data backups and the rollback journal were hash-verified on the device and the owner's computer. |

The new Update Game capture shows the deployed 0.9.17 drawing code with isolated player state. Its source line and O shortcut fit inside the existing window above OUTPUT. The accepted feature descriptions and other unchanged-interface captures are retained. No local AUTO approvals were created, no radio was started and no test chat messages were sent in these checks.

Those standalone checks initially used private authenticated source and film previews. Its later public source, feed, images, rendered native video and redirects were checked anonymously. A direct attachment-route 404 was a false failure criterion: the public README supplied a working signed media URL. None of those earlier results proves attachment access after deletion of the old repository or updates through the new fork ID. They do not extend the recorded RF, AUTO, chat-delivery or WDGWars evidence below.

## Earlier private updater preview — 2026-11-01

The earlier preview retained numeric version **0.9.16** and the original SYSTEM menu row. Its startup added **Modified by KiloGramowy** under LOCOSP; a 32 px avatar appeared only inside Update Game. At that preview stage, no approved release feed, new version or public repository had been published.

| Check | Result |
| --- | --- |
| Windows full software suite | 500 cases: **487 PASS / 13 SKIP**, no failures/errors. |
| uConsole final full software suite, including sudo-owned staging | **501 PASS / 0 SKIP**, no failures/errors, after supplying the existing private recorded-frame fixture outside the repository. |
| Native uConsole rendering | Startup credit, original SYSTEM menu and update-window avatar/notes rendered using the actual drawing methods; the window stays above OUTPUT. |
| Full staged game startup | Actual ESP32 serial initialization, updater window and private GitHub check passed with isolated player data and account plugins disabled. No radio was started; cleanup completed without errors. |
| Installed-source startup | Same checks passed after exact-file deployment of code commit `76fc298`; the protected current-player fingerprint remained unchanged. |

The 22 updater UI cases cover main-screen deferral, durable once-per-release notices, OK opening notes without installing, No/Esc, menu availability color, all note lines, layout bounds and separate installation confirmation. The 30 backend cases use temporary Git histories and local release fixtures to cover the pinned source, legacy deployed manifest, local edits, data preservation, code rollback, interrupted-install recovery and permissions under sudo. The additional cross-checkout recovery regression passed on Windows after its 500-case full run; another installation's journal is ignored without executing its helper.

The first uConsole run skipped two optional replay tests because their private fixture was not specified. The preserved capture was found and the complete suite then passed without skips. This replay is software processing of historical frames, not new RF reception.

The live private-fork check returned **No published update is available**, as expected. Installation and notification scenarios with a release are software fixtures, not a published remote release or a new RF/chat test. Backup and deployment details stay in the private operational report. An initial installed-source smoke harness waited only six seconds for the first private Git fetch; the corrected harness waits for the background result and passed. The game itself remained responsive while checking.

## Shutdown fix — 2026-11-01

The shutdown patch was tested before deployment in an isolated checkout and separate home/loot directories. It does not change RF presets, GPS policy, scoring rules, chat semantics or upload accounting.

| Check | Result |
| --- | --- |
| Windows main suite | 449 cases: **438 PASS / 11 SKIP**, no failures/errors. |
| uConsole main suite | 449 cases: **447 PASS / 2 SKIP**, no failures/errors. |
| Real uConsole normal quit | Real Meshtastic worker ready and ESP32 serial open; final STOP acknowledged, workers stopped, serial closed, managed LoRa/SDR/GPS/USB OFF. |
| Native Pyxel quit / window-close callback path | Same real hardware checks; Python atexit cleanup executed before native process exit. |
| Intentional isolated update-callback error | Cleanup completed before Pyxel's error exit; same resource and power checks passed. |
| Player data and installed source during staged tests | Unchanged; current data was backed up separately and fingerprinted. |

All four AIO rails were measured ON before each exit and OFF afterwards. GPS/SDR rail checks do not qualify GPS fixes or SDR reception. Real receiver readiness is not a new decoded-packet/reception claim. No test messages, WiFi/BLE attacks or local AUTO approvals were requested by the test harness.

The tests cover bounded shutdown STOP writes without tcdrain/flush, final versus preliminary firmware STOP responses, partial initialization, repeated cleanup, failed workers, callback errors, power ownership, queued scan cancellation and late auxiliary connector/process races. Optional platform/private-fixture skips remain explicit. Forced SIGKILL and power loss cannot execute cleanup; active flashing and unconfirmed worker release protect the affected power.

## Initial release-preparation checks — 2026-11-01

The initial documentation candidate `1304a5f` preserved the exact runtime, test and dependency-file blobs of `3481cf9`; that preparation changed documentation, packaging rules and reviewed media. Both earlier prototype and integration branch histories are included. The historical checks below ran in isolated checkouts without opening the physical radio. The later shutdown changes and their separate hardware checks are recorded above.

| Fresh software check | Cases | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| Windows main suite | 370 | 359 | 11 | 0 |
| uConsole main suite | 370 | 368 | 2 | 0 |
| uConsole standalone receiver/SPI regression suites | 49 | 49 | 0 | 0 |

The Windows standalone receiver/SPI run initially passed 47 of 49 cases with two missing-driver-source skips. Supplying the existing LoRaRF 1.4.0 source let both skipped adapter-contract cases pass separately. These exercise fake SPI/GPIO transports with real driver code; they remain software tests, not RF reception.

Platform/optional-fixture skips remain documented in the test output. Packaging checks cover local documentation links, image metadata, excluded runtime/credential files, source preservation, media hashes and decoder integrity. Public demo conversations and node information are intentionally visible; precise location and credentials are hidden.

## Historical software baseline

The recorded consolidated baseline is commit `3481cf9ea2486248d2710b8e19a29cb3bc1a6d08`, dated **2026-10-30**, on top of Watch Dogs Go 0.9.16.

| Platform | Tests run | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| uConsole | 370 | 368 | 2 | 0 |
| Windows | 370 | 359 | 11 | 0 |

These are historical recorded runs, **not a new test result for this release-preparation branch**. Skips include optional/private replay or platform-dependent checks; they are not physical RF verification. Before a release, record the new candidate's exact commit, commands, results and skip reasons.

The regression coverage includes packet/CRC validation, declared NodeID parsing, deduplication and stored discovery awards, GPS freshness and RF policy, UI controls, scanner USB selection, STOP acknowledgements, alternating wardrive, CSV persistence failures, and upload receipt accounting including cooldown and pending-cache invalidation.

## Recorded hardware and server evidence

| Area | Recorded evidence | What it does not establish |
| --- | --- | --- |
| Meshtastic SFNarrow | Genuine natural packet reception on uConsole, including supported public messages and contact data. | Guaranteed coverage, direct range to a packet author, authenticated identities, or reception on every preset. |
| WiFi/BLE alternation | Seven complete WiFi and seven complete BLE phases were recorded with GPS and CSV output; LoRa remained active on its separate hardware. | Simultaneous WiFi and BLE scanning, unlimited throughput, or compatibility with every ESP32 firmware/USB device. |
| GPS/profile UI | Real GPS status and region resolution, manual controls and on-screen profile/assignment dialogs were checked. | A fresh qualification of every approved AUTO retune, country, border condition or RF preset on this candidate branch. |
| WDGWars upload | Complete application-level receipts and recorded DONE results were checked against submitted counts, including known and cooldown-handled observations. | New import credit for cooldown/known records, ownership transfer, or continued compatibility with an independently changed server. |
| Chat | Real public reception and a user-initiated local radio transmit completion were recorded. | End-to-end delivery, another device receiving the nickname, or a read receipt. |

## Important boundaries

- A passing test with a fake radio, injected failure or replayed frame is a software result. It must not be presented as new physical reception.
- A quiet RF session is inconclusive: it can reflect no traffic, different settings, range or a receiver problem. It is not proof that a radio is broken.
- MeshCore local reception on a newly selected Spanish preset has not been established by these results. The separately documented SF7 / CR4/6 variant is not implemented in the current catalog.
- US profiles and the installed RF path/antenna have not been physically qualified by this validation. Not every MeshCore preset has an on-air test.
- GPS recognition does not discover local RF settings. Existing user approvals and user-reported experience must not be replaced with invented approvals or inferred country-wide defaults during testing.
- HTTP 200, a cached duplicate notice, a UI ON label, a local XP award and a server discovery credit are different observations.

## Before calling a candidate validated

Use a separate checkout and preserve current player/configuration data. Run the relevant existing suites, document all skips, inspect the English dialogs without changing their layout, and distinguish synthetic fixtures from actual device results. Record remaining hardware gaps explicitly. A documentation-only preparation should remain labelled as such until those checks are performed.

See [Release preparation](RELEASE_PREPARATION.md) for privacy, packaging and publication boundaries.
