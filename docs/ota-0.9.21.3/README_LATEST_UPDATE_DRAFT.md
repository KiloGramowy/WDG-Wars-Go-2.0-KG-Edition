## 📍 Latest update — 0.9.21.3

**RIGHT HELPER. RIGHT ADDRESS. 🐇💾**

❌ **Before:** a separate data/loot folder could send recovery to the wrong script path. Python exited before the helper ran; this was not a HardReset failure.

✅ **Now:**

* 📍 **Code finds code:** recovery locates its helper beside the running game, independently of the data directory and CWD — including paths with spaces.
* 🛑 **Check before USB:** a missing helper stops the attempt before the game connection is closed or the device is reset.
* 🧪 **Real launch check:** the production path is exercised with the actual helper's `--help`; this does not open or test a C5.

The loot chest keeps the loot. The helper finally uses its office address. 😏💾

**The original `rom-chip-id` timeout remains unresolved.** This fixes recovery launching, not a physically confirmed migration on PapaSucio's board. Hardware guards, backups and manual **M → Y** migration remain; KG/SOLO and Piglet firmware bytes stay unchanged. **Game OTA never flashes ESP32.**

[📦 Release 0.9.21.3](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases/tag/v0.9.21.3) · [📝 All releases](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases) · [📚 Recovery guide](../FIRMWARE_KG_SOLO_0.9.21.3.md) · [Full changelog](../../CHANGELOG.md)

🎮 **TAB → SYSTEM → Update Game → R → Enter → Y**. **N / Esc** cancels. Clients 0.9.19.1 onward restart after success; older 0.9.17–0.9.19 clients need one manual relaunch on their first upgrade.

---
