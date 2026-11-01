# Contributing to Watch Dogs Go

Contributions to this uConsole fork should preserve the original LOCOSP game style and existing player data. The repository remains private during release preparation; use a separate checkout for development.

## TL;DR

- **Bug reports** → open an [issue](https://github.com/KiloGramowy/esp32-watch-dogs/issues),
  attach only reviewed, redacted excerpts from `~/.watchdogs/last_run.log` and `~/.watchdogs/launcher.log`.
- **Feature ideas** → open an issue with the `enhancement` label first,
  let's talk before you write code.
- **Pull requests** → use the repository's default branch: `codex/wdg-go-wars-release` in the development repository, or `main` in the clean private review repository. Small focused PRs
  merge faster than big ones.
- **Be nice.** This is meant to be fun.

---

## Reporting bugs

Before opening an issue, please check:

1. Check the current [feature guide](docs/FEATURE_GUIDE.md) and the dated history
   in [CHANGELOG.md](CHANGELOG.md). BLE HID, HID Type, BlueDucky and RACE Attack
   remain disabled WIP menu entries. **SYSTEM → Download Map is implemented**;
   it requires a configured Stadia API key.
2. Record the current version and source commit. Use the [update instructions](docs/UPDATING.md)
   appropriate to your installation; a deployed source overlay may differ from its old Git HEAD.
   Do not run an unrestricted pull/setup over a working installation merely to file a report.
3. Include the hardware relevant to the failing feature. Most UI tests work without an ESP32;
   WiFi/BLE hardware tests need the compatible ESP32/firmware, and LoRa needs its own module.

**A good bug report contains:**

- What you tried to do (one sentence)
- What happened instead (one sentence)
- A reviewed, redacted excerpt of the last 30 lines of `~/.watchdogs/last_run.log`
- A reviewed, redacted excerpt of the last 20 lines of `~/.watchdogs/launcher.log` if the game crashed
  before opening
- Your hardware: `uname -a`, `lsusb`, `python3 --version`
- A screenshot if it's a UI bug

The log files contain GPS coordinates, BSSIDs, and sometimes captured
credentials. **Review them before pasting into a public issue** — or send
them privately if you'd rather.

---

## Setting up a dev environment

```bash
git clone --branch codex/wdg-go-wars-release --single-branch https://github.com/KiloGramowy/esp32-watch-dogs.git
cd esp32-watch-dogs
bash setup.sh        # creates .venv and installs dependencies in this separate checkout
./run.sh            # the launcher obtains sudo for GPIO/serial
```

You don't need an ESP32 to run the software UI tests. They use fake transports,
temporary player data and recorded drawing calls, without opening the radio.
An interactive game startup can attempt hardware detection and share home-directory
settings with another checkout, so it is not an isolated test by itself.

```bash
.venv/bin/python3 -B -m unittest discover -s tests -v
.venv/bin/python3 -B -m unittest test_meshtastic_rx_probe test_spi_mutation_regression
```

Optional platform dependencies and private recorded-frame fixtures can cause
explicit skips. Keep private captures outside Git. Record the exact command,
commit, failures and skips; current results are in [Validation](docs/VALIDATION.md).

For LoRa and AIO v2 features you need a ClockworkPi uConsole with the
AIO v2 module. For wardriving with attacks you need an ESP32-C5 running
[projectZero](https://github.com/LOCOSP/projectZero).

---

## Code style

- **Python 3.10+**, no type-checking required but typed signatures
  welcome.
- Match the surrounding style of the file you're editing.
- Keep functions small. `app.py` is already large — please don't
  add to that monster without strong justification, prefer a new module
  in `watchdogs/`.
- **No emoji in code or commit messages** unless the surrounding context
  uses them.
- **No `Co-Authored-By: Claude` lines** in commits — if you used AI to
  help, that's fine, but the commit author is you.

## Commit messages

Format: `<type>: <one-line summary>`

Where `<type>` is one of `feat`, `fix`, `docs`, `refactor`, `chore`,
`perf`, `test`. Examples from the existing history:

```
feat: badge sync — push game badges to server, pull server badges to game
fix: every fresh user broadcasted as literal 'WatchDogs' on LoRa mesh
docs: add one-liner install command to README
chore: bump pyxel to 2.5.0
```

Body (after a blank line) should explain *why*, not *what* — the diff
shows what.

---

## Plugin development

Plugins live in `plugins/` and are auto-discovered. The simplest plugin:

```python
# plugins/my_plugin.py
from plugins.plugin_base import PluginBase, PluginMenuItem

class MyPlugin(PluginBase):
    NAME = "My Plugin"
    VERSION = "1.0"
    AUTHOR = "yourname"

    def menu_items(self):
        return [PluginMenuItem("m", "My Plugin", "open_overlay")]

    def open_overlay(self):
        self.msg("Hello from my plugin!", 11)
```

Drop it in `plugins/`, restart the game, and it appears under the
**PLUGINS** menu tab. Select the entry with the normal arrows/Enter menu controls; its displayed key prefix is metadata, not a global key binding. See `plugins/wardrive_upload.py` for a real-world example with overlay
UI, threaded HTTP requests, persistent state and background workers.
The plugin API is small and stable — read `plugins/plugin_base.py` for
the full base class.

---

## What needs help right now

- Focused fixes with a reproducible report, including the disabled WIP features
- Useful regression coverage for scanning, radio ownership, storage and updating
- Better documentation for the plugin API
- Linux-distro support beyond Debian/Ubuntu (Fedora, Arch, Alpine)
- macOS support for the game frontend (Pyxel works there, but lots of
  code assumes Linux serial paths and `lgpio`)
- Consistent English UI wording and readable layout within the original windows

---

## License

By contributing, you agree that your contributions will be licensed under
the same license as the project (see [LICENSE](LICENSE)).

---

## Project contacts

- **[This fork's GitHub Issues](https://github.com/KiloGramowy/esp32-watch-dogs/issues)** — bugs and feature discussions for KiloGramowy's changes.
- **[LOCOSP](https://github.com/LOCOSP)** — original WatchDogsGo author; original credits and license are retained.
