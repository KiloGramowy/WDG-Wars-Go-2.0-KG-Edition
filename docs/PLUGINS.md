# ESP32 Watch Dogs — Plugin Development Guide

## Overview

Plugins extend the game with custom functionality. They appear as a **PLUGINS** tab in the game menu (only when at least one plugin is installed).

Plugins are Python files in the `plugins/` directory that inherit from `PluginBase`.

## Quick Start

Create `plugins/my_plugin.py`:

```python
from plugins.plugin_base import PluginBase, PluginMenuItem

class MyPlugin(PluginBase):
    NAME = "My Plugin"
    VERSION = "1.0"
    AUTHOR = "YourName"

    def menu_items(self):
        return [
            PluginMenuItem("m", "My Action", "do_action"),
        ]

    def do_action(self):
        self.msg("[MyPlugin] Hello!", 11)  # green HUD message
        self.term("[MyPlugin] Action executed")  # terminal line
```

Restart the game and the PLUGINS tab displays `[m] My Action`. Use the normal arrows and Enter menu controls to select it. The prefix is menu metadata; the current main menu does not dispatch it as a direct key binding.

## Plugin Discovery

- Game scans `plugins/` at startup
- Any `.py` file (except `__init__.py` and `plugin_base.py`) is loaded
- Every class inheriting from `PluginBase` is instantiated
- No registration needed — just drop the file and restart

## PluginBase API

### Required Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `NAME` | str | Display name in menus and logs |
| `VERSION` | str | Version string |
| `AUTHOR` | str | Author name |

### Lifecycle Methods

| Method | When Called | Use For |
|--------|------------|---------|
| `on_load(app)` | Game startup, after init | Setup, start background threads |
| `on_unload()` | Game exit | Cleanup, stop threads, close files |
| `on_update()` | Every frame (30 FPS) | Input handling when overlay active |
| `draw(x, y, w, h)` | Every frame when overlay active | Render plugin screen |

### Menu Integration

```python
def menu_items(self) -> list[PluginMenuItem]:
    return [
        PluginMenuItem("a", "Action One", "method_one"),
        PluginMenuItem("b", "Action Two", "method_two"),
    ]
```

- `key`: single-character menu prefix (shown as `[a]`; selection uses the normal arrows/Enter controls)
- `label`: display text
- `action`: method name on your plugin class — called when user selects

### Convenience Methods

| Method | Description |
|--------|-------------|
| `self.msg(text, color)` | Show HUD message (top of screen, 3 seconds) |
| `self.term(text)` | Add line to terminal log (bottom panel) |
| `self.app` | Reference to main game app (full access) |

### Overlay (Full-Screen Plugin UI)

If your plugin needs its own screen:

```python
class MyPlugin(PluginBase):
    NAME = "My Plugin"

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False

    @property
    def overlay_active(self):
        return self._overlay_active

    def menu_items(self):
        return [PluginMenuItem("m", "Open My Screen", "open_screen")]

    def open_screen(self):
        self._overlay_active = True

    def on_update(self):
        """Handle input when overlay is active."""
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            self.app._esc_consumed_frame = pyxel.frame_count  # prevent ESC propagation!
            return
        # ... handle other keys ...

    def draw(self, x, y, w, h):
        """Draw your overlay. w=640, h=360."""
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, 0)  # black background
        pyxel.text(4, 4, "MY PLUGIN SCREEN", 3)
        pyxel.text(4, h - 10, "[ESC] Back", 13)
```

**Important**: When handling ESC, always set `self.app._esc_consumed_frame = pyxel.frame_count` — otherwise ESC propagates to the main game and opens the quit dialog.

## Color Constants

Use pyxel's 16-color palette:

| ID | Color | Usage in Game |
|----|-------|---------------|
| 0 | Black | Background, HUD bg |
| 1 | Dark blue | Land, dark panels |
| 3 | Cyan | Main accent, selections, UI |
| 7 | White | Primary text |
| 8 | Red | Errors, attacks, danger |
| 10 | Orange/Yellow | Warnings |
| 11 | Green | Success, scans |
| 12 | Blue | DMs, WPA-sec, info |
| 13 | Grey | Dim/inactive text |

## Accessing Game Data

Through `self.app` you have access to everything:

```python
# GPS
lat, lon = self.app.player_lat, self.app.player_lon
has_fix = self.app.gps_fix

# WiFi networks found this session
networks = self.app.wifi_networks  # list of Network objects

# BLE devices
devices = self.app.ble_devices  # list of BLEDevice objects

# Loot manager
loot = self.app.loot
session_dir = loot.session_dir  # current session path

# ESP32 serial
self.app.serial.send_command("ping")

# SDR
aircraft = self.app._sdr.aircraft  # dict of Aircraft
sensors = self.app._sdr.sensors    # dict of Sensor433

# PipBoy Watch
if self.app._watch.connected:
    self.app._watch.send_command("status")

# XP
self.app.gain_xp(50)

# Big font (8x8 BDF)
bf = self.app._big_font  # pyxel.Font or None
pyxel.text(10, 10, "BIG TEXT", 7, bf)
```

## Background Threads

For network requests, file operations, etc. — always use threads:

```python
import threading

class MyPlugin(PluginBase):
    def __init__(self):
        super().__init__()
        self._working = False

    def do_action(self):
        if self._working:
            self.msg("Already working...", 10)
            return
        self._working = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            # ... slow operation ...
            self.msg("Done!", 11)
            self.term("[MyPlugin] Operation complete")
        except Exception as e:
            self.msg(f"Error: {e}", 8)
        finally:
            self._working = False
```

**Never block `on_update()` or `draw()`** — the game runs at 30 FPS. Keep frame methods under 1ms.

## File Storage

Store plugin data in the `plugins/` directory:

```python
from pathlib import Path

state_file = Path(__file__).parent / ".my_plugin_state.json"
```

Add private files to `.gitignore`:
```
plugins/my_plugin.py
plugins/.my_plugin_state.json
```

## Example: Simple Network Logger

```python
import json
import time
from pathlib import Path
from plugins.plugin_base import PluginBase, PluginMenuItem

class NetLogger(PluginBase):
    NAME = "Network Logger"
    VERSION = "1.0"
    AUTHOR = "Community"

    def __init__(self):
        super().__init__()
        self._last_count = 0

    def menu_items(self):
        return [
            PluginMenuItem("n", "Log Networks Now", "log_now"),
            PluginMenuItem("s", "Show Stats", "show_stats"),
        ]

    def log_now(self):
        nets = self.app.wifi_networks
        new = len(nets) - self._last_count
        self._last_count = len(nets)
        self.msg(f"[NetLog] {len(nets)} total, +{new} new", 11)
        # Save to file
        out = Path(__file__).parent / ".netlog.json"
        data = [{"ssid": n.ssid, "bssid": n.bssid,
                 "rssi": n.rssi, "ch": n.channel}
                for n in nets]
        out.write_text(json.dumps(data, indent=2))
        self.term(f"[NetLog] Saved {len(data)} networks")

    def show_stats(self):
        nets = self.app.wifi_networks
        bts = self.app.ble_devices
        self.msg(f"WiFi:{len(nets)} BLE:{len(bts)}", 3)
```

## Example: Auto-Upload Plugin with Overlay

See `plugins/wardrive_upload.py` (private, not in repo) for a full example with:
- Full-screen overlay with left menu + right log panel
- API key management
- Background upload threads with progress
- Signed HTTPS API calls (HMAC-SHA256)
- Auth push (game as 2FA authenticator)
- VPN toggle
- State persistence

## Tips

1. **Import pyxel inside methods**, not at module level — plugin loads before pyxel init
2. **Use `daemon=True`** on threads so they don't block game exit
3. **ESC handling**: always set `_esc_consumed_frame` or ESC will close the game
4. **Test without game**: `python3 -c "from plugins.my_plugin import MyPlugin; p = MyPlugin(); print(p.NAME)"`
5. **Private plugins**: add to `.gitignore` — they won't be shared via git
6. **Public plugins**: commit to `plugins/` — available to all users
