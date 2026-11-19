## 📞 Latest update — 0.9.21.2

**USB, PICK UP THE PHONE. 🐇💾**

❌ **Before:** a failed first ROM connection could enter recovery that still assumed a working ROM, while repeated generic messages hid the real tool output.

✅ **Now:**

* 📞 **Recovery without that assumption:** a bounded native USB reset can request the previous app without ROM synchronization. A fresh firmware reply still has to confirm its return.
* 🔌 **Careful USB hand-off:** idle control lines before native USB open, stable detection of the same bound device, and retained ownership until old connections and tool processes really finish.
* 🧾 **Keep the first clue:** redacted `[KG-FW]` stage, original-error and actual tool-output records persist through the existing game log. The recovery error cannot replace the first failure.

USB gets a better phone script, not an honorary “booted successfully” sticker. 😏📞

**The cause of the first physical C5 ROM timeout is still unconfirmed.** Software checks do not claim a repair of PapaSucio's device. The supported hardware/layout checks, recovery backups, separate **M → Y** migration and fresh SOLO confirmation remain. Original KG/SOLO and Piglet firmware bytes stay unchanged; **game OTA never flashes ESP32**.

[📦 Release 0.9.21.2](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases/tag/v0.9.21.2) · [📝 All releases](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition/releases) · [📚 USB recovery / diagnostic guide](../FIRMWARE_KG_SOLO_0.9.21.2.md) · [Full changelog](../../CHANGELOG.md)

🎮 **TAB → SYSTEM → Update Game → R → Enter → Y**. **N / Esc** cancels. The 0.9.21.1 updater restarts after success; firmware remains a separate manual choice.

---
