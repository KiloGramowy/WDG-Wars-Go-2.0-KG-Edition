# 0.9.21 — prepublication preparation record

**Historical preparation record for 0.9.21.** Owner publication approval has
been given. Current publication/OTA availability is determined by the public
Release, tag and feed, not by unchecked preparation items below. No device
installation, fresh hardware test or firmware flash is implied.

## Prepared scope

- [x] Complete game source, including KG changes; retain LOCOSP's MIT license,
  original attribution and third-party notices. The former closed game binary,
  Nuitka, APT and binary OTA proposals are historical, not release requirements.
- [x] Keep the existing verified Git/source updater, plugin loader, code-only
  rollback, player data, signatures, XP rules and protocols.
- [x] Canonical [Release/Discord](RELEASE_AND_DISCORD_EN.md), [Latest update](README_LATEST_UPDATE_DRAFT.md),
  [feature sections](README_FEATURE_SECTIONS_DRAFT.md), [complete README](../../README_PUBLIC_0.9.21.md),
  [changelog/update instructions](CHANGELOG_AND_UPDATE_GUIDE.md), runtime
  [canonical text](../UPDATE_PREVIEW.txt) and [actual bitmap projection](UPDATE_GAME_DRAFT.txt).
- [x] Preserve the established motif, Before/Now, emoji, humour, controls and
  attribution from the actual published v0.9.20 Release and README.
- [x] Distinguish host Wardriving Collection support from the unavailable public
  modified Dark3D sender firmware. No private Dark3D input or image is included.
- [x] Include the unchanged, already published KG/SOLO 1.7.0 image, original
  schema-1 manifest, exact build inputs, source patch and firmware notices.
  Keep firmware 1.7.0 separate from game 0.9.21. A schema-2 proposal is not active.
- [x] Preserve the v0.9.20 feed during pre-release validation; publication binds
  0.9.21 to its approved public source commit. Prepare source without private
  history, operational reports or old delivery archives.

## Preparation checks and publication follow-through

- [ ] Review and accept the exact candidate commit, current exact-commit ledger and
  package hashes delivered with this PR. Historical 1800 PASS is not a new run.
- [ ] Approve the concrete Release/Discord body, README replacement and Update
  Game preview. Cloud native images are synthetic; older screenshots retain
  their actual version and provenance. Obtain a new physical capture only if
  required for publication, without relabelling the Cloud preview.
- [x] Record the completed local Windows baseline:
  **1796 Python PASS / 14 Windows platform SKIP / 0 FAIL or ERROR**, plus
  **45 auxiliary PASS**. The two private Meshtastic replay fixtures and the
  private Piglet 17-CSV archive executed successfully; they are not skipped.
- [x] Record the complete local code validation: **1817 Python methods / 1802 PASS / 1 FAIL / 0 ERROR / 14 Windows SKIP**, plus **45 auxiliary PASS**, syntax and imports. The one failure was the Piglet README checksum after documentation cleanup; the original failure remains documented.
- [x] Repair only that checksum and rerun affected packaging/copy/Update Game checks: **98 methods / 97 PASS / 1 existing Windows symlink SKIP / 0 FAIL or ERROR**. The formerly failed case passes. All other 464 tracked file bytes, including runtime/tests/images/firmware, are identical to the full-run code. No unresolved test failure remains; this is not a claim of a third full rerun.
- [ ] Accept or complete physical ARM64/uConsole runtime, root-owned staging
  and the remaining platform-specific checks. SKIP is not PASS.
- [ ] Accept or complete the recorded physical limits: 3 km relocation, complete
  network loss/recovery, new Collection transfer, KG client flash/recovery and
  untested RF/hardware configurations. Multiple physical Piglets remain
  unsupported. No physical test, flash or real elapsed-five-day test is implied.
- [ ] Review retained third-party asset provenance limits, including the inherited
  8x8 font/artwork. Spleen's full BSD notice, firmware component licenses and
  soundtrack attribution stay. No invented blanket license clearance.
- [ ] Recheck current public main and apply the reviewed source snapshot on its
  existing public ancestry; never push private DEV history or change visibility.
  Reconcile concurrent public edits. Update the single Latest update block above
  the retained feature-tour video and only affected feature text.
- [ ] Supply the KG/SOLO release assets under their exact expected names. Reuse
  the unchanged verified legacy manifest/image, or separately approve and test
  the schema-2 manifest. A newer game Release without these assets causes the
  fail-closed firmware client to reject its package; never substitute stock.
- [ ] After a separate publication instruction, bind the existing schema-2 game
  feed to the approved **public code commit**, release identity and full notes.
  A private RC SHA or a ZIP URL is not an updater source. No new updater design.
- [ ] Check anonymous clone/Release/feed/media and exact assets after authorized
  publication, including dependencies, licenses and hashes; verify source update
  and code-only rollback on an isolated matching installation with synthetic
  data. Preserve real progress, settings, keys, receipts and pending exports.
- [ ] Before presenting Dark3D as downloadable in public 0.9.21, verify the separate firmware repo is publicly accessible and contains the separately approved compatible `.bin`, documentation and rollback guidance. Until then, state that its Release is unavailable.
- [ ] Rebuild and validate the complete source package and media manifest after adding the two owner-supplied 0.9.21 map illustrations; older RC ZIP hashes and preview screenshots predate these assets.
- [ ] Verify both image URLs resolve anonymously after the approved v0.9.21 public tag includes these images; do not claim physical device evidence from illustrations.
- [ ] Private Markdown preview uses relative `../media/0.9.21-*.png` links so the two original images render on the private RC branch. Before authorized GitHub Release/Discord publication, replace them with validated, absolute public image URLs from the approved tag. An unreleased public `v0.9.21` raw URL is currently broken; do not create a public tag just to preview images.
- [x] [Copy-ready GitHub Release draft](GITHUB_RELEASE_READY.md) staged privately: exactly two absolute image URL replacements, with all other Release/Discord wording unchanged. These URLs will become live only after the approved public tag contains both original PNGs.
- [x] Conditional joint-release copy `GITHUB_RELEASE_JOINT_DARK3D_CONDITIONAL.md` (private DEV only; not in the public source snapshot) staged separately and **not active**. Select it only after the Dark3D repo is public, the specifically approved modified `.bin` is actually downloadable, rights/compliance and board/rollback evidence pass, and both releases are authorized. Otherwise retain the accurate unavailable-firmware wording in the standard draft.
- [x] Obtain explicit owner approval to publish/activate 0.9.21. Actual publication receipts remain separate.

A source archive or preparation checklist is not proof of device installation,
hardware compatibility or blanket third-party rights clearance.
