# 💾 Keep your progress when upgrading from LOCOSP

**Your local XP lives in `loot/loot_db.json`, inside the installation/data folder.** It is not downloaded from WDGWars. The level is calculated from saved XP using the same thresholds as the public LOCOSP version.

## What 0.9.18 fixes

LOCOSP's older release uses loot database format **1**. Earlier KG builds required format **2** and rebuilt a format-1 database from session files, dropping saved XP, badges and other player metadata. That rebuild could recover only part of the XP, or none when the logs were absent.

Version **0.9.18** upgrades format 1 while keeping the exact saved XP, badges, contacts, discovery ledger, other player metadata and existing session counts. It adds missing counters without granting discovery XP again. Before changing the database, it keeps an exact private copy beside it:

```text
loot/loot_db.v1.<sha256-of-original-file>.bak
```

Repeated launches use the migrated save without repeating the migration or adding XP. An unreadable database, unsupported future format or failed safe migration stops startup; it does not overwrite the save with an empty player. Check `~/.watchdogs/last_run.log` for the error.

## Updating an existing installation

Close the game normally, retain a backup of the complete `loot/` folder and apply the code update. The in-game updater changes code, not player data. On the next launch, 0.9.18 upgrades a valid format-1 database automatically. Do not delete `loot_db.json` to force a rescan: earned XP cannot be reconstructed exactly from discovery counts.

## Installing a separate clone

A fresh clone has its own empty `loot/` folder. Keeping an old installation elsewhere does not automatically select its save. **Close both game versions**, keep the old folder, and launch the new code with the old installation root as its data argument. For example:

```bash
cd "$HOME/python/WatchDogsGo-KiloGramowy"
./run.sh "$HOME/python/WatchDogsGo"
```

Replace both paths with your actual folders. The argument is the old installation root containing `loot/loot_db.json`, **not the `loot/` directory itself**. This existing option also selects that root's maps and whitelist. Configuration stored in the user's home remains in place; project-local `secrets.conf` and plugin upload/account state are separate and are not imported by this argument.

Use the same data argument on later launches, or retain it in your launcher. Do not run two game instances against the same data. This route uses the original save directly and avoids overwriting a newer save in the new folder.

## Already seeing zero or reduced XP?

- **Old installation still intact:** stop both games and use its data root with 0.9.18 as above. Keep a backup first.
- **An older KG build already overwrote the original database:** stop the game and find the intact original database or a backup. Keep the current save too, especially if it contains newer progress. Do not blindly replace it with an older snapshot or add both XP totals together.
- **No intact save or backup remains:** the exact old XP cannot be guaranteed. Logs omit some awards; the local save and WDGWars server score are different records. This fix prevents further migration loss, but cannot invent already-erased XP.

The `.bak` files contain player data and may include contact locations. Keep them private; do not attach them to a public issue.
