"""ESP32 Watch Dogs — pyxel game frontend for ESP32 security device.

Standalone application — no external TUI required.
Controls:
  [TAB] Toggle cyberdeck menu    [SPACE] Hack nearby device
  [=/]] Zoom in   [-/[] Zoom out  [0] Reset zoom
  [S] Quick stop   [ESC] Quit + stop ESP32
  Arrow keys — manual pan (GPS overrides when fix available)
  PgUp/PgDn (Fn+U / Fn+K on uConsole) — scroll terminal
"""

import atexit
import logging
import math
import os
import random
import re
import signal
import sys
import threading
import time
from queue import Queue
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

import pyxel

from .serial_manager import SerialManager, detect_esp32_port
from .gps_manager import GpsManager
from .loot_manager import LootManager
from .app_state import AppState, Network
from .network_manager import NetworkManager
from .coastline import COASTLINES
from .tile_manager import TileRenderer, download_tiles
from .dragon_drain import DragonDrainAttack
from .mitm import MITMAttack
from .bt_ducky import BlueDuckyAttack
from .race_attack import RACEAttack
from .aio_manager import AioManager, AioPowerSession
from .whitelist_manager import WhitelistManager
from .lora_manager import LoRaManager
from .meshtastic_manager import MeshtasticManager
from .sdr_manager import SDRManager
from .subghz_manager import SubGhzManager
from .plugin_loader import discover_plugins
from .watch_manager import WatchManager
from .flipper_manager import FlipperManager
from .proximity_manager import ProximityManager
from .portals import get_all_portals, upload_html_to_esp32
from . import upload_manager
from .game_updates import GameUpdates

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
W, H = 640, 360
FPS = 30
HUD_TOP = 16
HUD_BOT = 16
TERM_H = 110
MAP_H = H - HUD_TOP - HUD_BOT - TERM_H
TERM_Y = H - HUD_BOT - TERM_H

# Pyxel palette (16 colors)
C_WATER = 0
C_LAND = 1
C_COAST = 5
C_GRID = 1
C_COAT = 4
C_COAT_DARK = 5
C_CAP = 5
C_SCARF = 9
C_SKIN = 15
C_PANTS = 1
C_BOOTS = 0
C_HACK_CYAN = 3
C_HUD_BG = 0
C_HUD_LINE = 1
C_TEXT = 7
C_DIM = 13
C_WARNING = 10
C_ERROR = 8
C_SUCCESS = 11
C_DEVICE_SCREEN = 3
C_MENU_BG = 0
C_MENU_BORDER = 5
C_MENU_SEL = 3
C_MENU_TEXT = 7

# Proximity (state/trend labels mirror watchdogs/proximity/prox.py)
PROX_STATE_NAMES = ("UNKNOWN", "WEAK", "NEAR", "CLOSE", "VERY CLOSE", "LOST")
PROX_TREND_WORDS = {2: "CLOSING FAST", 1: "GETTING CLOSER", 0: "HOLDING",
                    -1: "MOVING AWAY", -2: "MOVING AWAY FAST"}

ZOOM_LEVELS = [
    (360.0, "WORLD"),
    (180.0, "HEMISPHERE"),
    (90.0, "CONTINENT"),
    (45.0, "REGION"),
    (20.0, "COUNTRY"),
    (10.0, "PROVINCE"),
    (5.0, "CITY"),
    (2.0, "DISTRICT"),
    (1.0, "QUARTER"),
    (0.5, "NEIGHBORHOOD"),
    (0.2, "STREET"),
    (0.1, "BLOCK"),
    (0.05, "BUILDING"),
    (0.02, "CLOSE-UP"),
]

# (xp_threshold, title)
LEVEL_TABLE = [
    (0,          "NOOB"),
    (100,        "SCRIPT_KIDDIE"),
    (500,        "SKIDDIE+"),
    (1500,       "WANNABE"),
    (3000,       "PACKET_MONKEY"),
    (6000,       "WARDRIVER"),
    (10000,      "HACKER"),
    (20000,      "NETRUNNER"),
    (35000,      "PHREAKER"),
    (50000,      "EXPLOIT_DEV"),
    (75000,      "ELITE"),
    (100000,     "SHADOW_OPS"),
    (150000,     "GHOST"),
    (250000,     "ZERO_DAY"),
    (400000,     "APT_AGENT"),
    (600000,     "CYBER_DEMON"),
    (1000000,    "CYBER_GOD"),
    (2500000,    "DIGITAL_DEITY"),
    (5000000,    "MATRIX_BREAKER"),
    (10000000,   "FINAL_BOSS"),
]

# Hacker quips per category (cycles every ~3 s in menu)
HACKER_QUIPS = [
    # SCAN
    ["scanning...", "i see you", "networks exposed", "who's hiding?", "SSID hunter"],
    # SNIFF
    ["sniff sniff!", "packets=power", "wardrive!", "collecting...", "GPS+WiFi=pwn"],
    # ATTACK
    ["hack the world", "no mercy", "DEAUTH ALL!", "shell acquired", "fear the cap"],
    # ADDONS
    ["LoRa waves~", "BLE signals", "HID inject!", "mesh me up", "radio active"],
    # SYSTEM
    ["connection", "is power", "stay dark", "ghost mode", "ctrl+alt+hack"],
    # CRISIS / SURVIVAL
    ["off-grid ready", "knowledge=power", "no signal, no fear", "survive the dark", "offline vault"],
]

# (cat_name, [(hotkey, label, cmd_or_action, state_key, input_type), ...])
# input_type: None | "bssid_ch" | "ssid" | "mac" | "text"
# cmd starting with "_" = special Python-side action (not ESP32 serial)
# Attacks that require an external WiFi adapter (monitor mode)
_NEEDS_EXT_WIFI = {"dragon_drain"}

# Emergency scanner: RF band name -> human-readable service (ASCII for pyxel font)
EMERGENCY_SVC = {
    "SM-VHF":    "STRAZ MIEJ.",
    "STRAZ-VHF": "STRAZ POZ.",
    "EMS-VHF":   "KARETKA",
    "POL-VHF":   "POLICJA",
    "TETRA-UL":  "SLUZBY TETRA",
    "TETRA-DL":  "SIEC TETRA",
}
# Per-service HUD colour (pyxel palette idx): white=karetka, red=straż, blue=policja
EMERGENCY_SVC_COL = {
    "SM-VHF":    9,            # orange - municipal guard
    "STRAZ-VHF": C_ERROR,     # red    - fire
    "EMS-VHF":   C_TEXT,      # white  - ambulance
    "POL-VHF":   12,          # blue   - police
    "TETRA-UL":  C_HACK_CYAN, # cyan   - digital services
    "TETRA-DL":  C_HACK_CYAN,
}
# Per-service alert sound (WAV in assets/, pyxel sound slot). Fire->straż,
# ambulance->karetka, police->policja, everything else -> generic warning.
EMERGENCY_SND = {
    "STRAZ-VHF": ("fire.wav",      57),
    "EMS-VHF":   ("ambulance.wav", 58),
    "POL-VHF":   ("police.wav",    56),
    "SM-VHF":    ("warning.wav",   59),
    "TETRA-UL":  ("warning.wav",   59),
}
EMERGENCY_SND_DEFAULT = ("warning.wav", 59)

# uConsole-side durable library for captured sub-GHz signals (independent of
# the Monster SD, which is unreliable). One place, survives reboots/port shuffles.
SUBGHZ_LIB_DIR = os.path.expanduser("~/.watchdogs/subghz_library")
SUBGHZ_FREQ_FILE = os.path.expanduser("~/.watchdogs/subghz_freq")  # persist [F]

# Monster RF sub-GHz screen menu (label, action|None-for-separator)
SUBGHZ_MENU = [
    ("--- MONSTER RF / CC1101 (JanOS RF) ---", None),
    ("Read (listen / decode)",     "read"),
    ("Record RAW",                 "record"),
    ("Frequency analyzer (find freq)", "analyzer"),
    ("Saved signals (SD)",         "files"),
    ("Status / info",              "status"),
]

MENU_CATS = [
    ("SCAN", [
        ("1", "WiFi Scan",       "scan_networks",          "wifi_scan",    None),
        ("2", "BLE Scan",        "scan_bt",                "ble_scan",     None),
        ("t", "BT Tracker",      "scan_bt",                "bt_tracking",  "mac"),
        ("a", "AirTag Scan",     "scan_airtag",            "bt_airtag",    None),
    ]),
    ("SNIFF", [
        ("1", "WiFi Wardrive",   "scan_networks",          "wardriving",   None),
        ("2", "BT Wardrive",     "scan_bt",                "bt_scanning",  None),
        ("3", "Pkt Sniffer",     "start_sniffer",          "sniffer",      None),
        ("4", "HS Capture",      "start_handshake",        "handshake",    None),
        ("5", "HS Capture no SD","start_handshake_serial", "handshake",    None),
    ]),
    ("ATTACK", [
        ("1", "Deauth",          "start_deauth",           "deauth",       "bssid_ch"),
        ("2", "Blackout",        "start_blackout",         "blackout",     None),
        ("3", "HS Capture",      "start_handshake",        "handshake",    None),
        ("4", "Evil Twin",       "_evil_twin",             "evil_twin",    None),
        ("5", "SAE Flood",       "sae_overflow",           "sae_flood",    "bssid_ch"),
        ("6", "HS Capture no SD","start_handshake_serial", "handshake",    None),
        ("7", "Evil Portal",     "_evil_portal",           "portal",       None),
        ("d", "Dragon Drain",    "_dragon_drain",          "dragon_drain", None),
        ("m", "MITM",            "_mitm",                  "mitm",         None),
        ("k", "BlueDucky (WIP)", "_bd_wip",                "_bd_wip",      None),
        ("j", "RACE Attack (WIP)", "_race_wip",            "_race_wip",    None),
    ]),
    ("ADDONS", [
        ("h", "BLE HID (WIP)",   "_bt_hid_wip",            "_bt_hid_wip",  None),
        ("i", "HID Type (WIP)",  "_bt_hid_wip",            "_bt_hid_wip",  None),
        ("9", "MeshCore Chat",    "_meshcore",            "meshcore",     None),
        ("8", "Meshtastic Chat",  "_meshtastic",          "meshtastic",   None),
        ("7", "Meshtastic Profile", "_meshtastic_profile", "_mt_profile_screen", None),
        ("r", "MeshCore Region", "_meshcore_region",         "_mc_region_screen", None),
        ("w", "WDGWars Chat",    "_wdgwars",               "wdgwars",      None),
        ("f", "Flipper Zero",    "_flipper",               "_flipper",     None),
        ("a", "ADS-B Radar",     "_sdr_adsb",              "_sdr_adsb",    None),
        ("4", "433 MHz Scanner", "_sdr_433",               "_sdr_433",     None),
        ("e", "Emergency Radio", "_sdr_emergency",         "_sdr_emergency", None),
        ("s", "Monster RF SubGHz", "_subghz",              "_subghz",        None),
        ("m", "Mute Sounds",     "_mute_toggle",           "_mute_toggle",   None),
        ("p", "PipBoy Watch",   "_watch_connect",         "_watch",       None),
        ("y", "Proximity",       "_prox_toggle",           "_prox_toggle",   None),
        ("z", "Prox Radar",      "_prox_view",             "_prox_view",     None),
    ]),
    ("SYSTEM", [
        ("x", "STOP ALL",        "stop",                   "_stop_all",    None),
        ("r", "Reboot ESP32",    "_reboot_esp32",          "_reboot",      None),
        ("m", "Download Map",       "_download_map",       "_dl_map",      None),
        ("g", "GPS",             "_toggle_gps",            "_gps_toggle",  None),
        ("l", "LoRa",            "_toggle_lora",           "_lora_toggle", None),
        ("d", "SDR",             "_toggle_sdr",            "_sdr_toggle",  None),
        ("b", "USB",             "_toggle_usb",            "_usb_toggle",  None),
        ("w", "Whitelist",       "_whitelist",             "_wl_screen",   None),
        ("u", "Upload WPA-SEC",  "_wpasec_upload",         "_wpasec_up",   None),
        ("p", "Download WPA-SEC","_wpasec_download",       "_wpasec_dl",   None),
        ("f", "Flash ESP32",    "_flash_esp32",           "_flash_esp",   None),
        ("i", "Update Game",    "_update_game",           "_gameupd",     None),
    ]),
    ("CRISIS", [
        ("w", "Wikipedia",   "_srv_wiki",   "_srv_wiki",   None),
        ("c", "ctOS Map",    "_srv_ctos",   "_srv_ctos",   None),
        ("n", "Navigation",  "_srv_nav",    "_srv_nav",    None),
        ("s", "Status",      "_srv_status", "_srv_status", None),
    ]),
]

# BLE device line regex (from JanOS bt_wardriving)
# Groups: 1=MAC, 2=RSSI, 3=Name (optional), 4=[AirTag]/[SmartTag] (optional)
_BLE_RE = re.compile(
    r'^\s*\d+\.\s+([0-9A-Fa-f:]{17})\s+RSSI:\s*(-?\d+)\s*dBm'
    r'(?:\s+Name:\s*(.+?))?(\s*\[AirTag\]|\s*\[SmartTag\])?\s*$'
)
# AirTag scanner count line: "X,Y" (airtags, smarttags)
_AIRTAG_COUNT_RE = re.compile(r'^(\d+),(\d+)$')

# Lines to suppress in terminal
_TERM_SKIP_EXACT = {"", ">", "OK"}
_TERM_SKIP_PREFIX = (
    "> ", "[MEM]", "Command returned", "Stop command received",
    "Stopping ", "cleanup", "=====", "PCAP buffer:", "Dumping",
    "Total handshakes",
)
_TERM_SKIP_CONTAIN = ("already running",)


def _color_for_terminal_line(line: str) -> int:
    """Pick a palette color for a terminal line. Computed once at append time
    so the draw loop doesn't re-evaluate 11 string checks per line per frame."""
    if line.startswith("[WiFi]") or line.startswith("[BLE]"):
        return C_SUCCESS
    if line.startswith("[TRACKER]"):
        return C_WARNING
    if line.startswith("[WL]"):
        return C_DIM
    if ":PWD]" in line:
        return 12
    if ":CLIENT]" in line:
        return C_HACK_CYAN
    if line.startswith(">>>") or "RSSI:" in line or "dBm" in line:
        return C_HACK_CYAN
    if "SSID:" in line or ("SSID" in line and "scan" not in line.lower()):
        return C_SUCCESS
    if "AP:" in line or "BSSID:" in line:
        return C_SUCCESS
    low = line.lower()
    if "handshake" in low or "captured" in low:
        return C_WARNING
    if "[ERR]" in line or "[WARN]" in line:
        return C_ERROR
    if "[OK]" in line or "[SYS]" in line:
        return C_DIM
    if "Ch:" in line or "Channel" in line or "Auth:" in line:
        return 6
    return C_TEXT


# ---------------------------------------------------------------------------
# Game objects
# ---------------------------------------------------------------------------

class BleDevice:
    def __init__(self, lat, lon, mac, name, rssi):
        self.lat, self.lon, self.mac = lat, lon, mac
        self.name = name[:18]
        self.rssi, self.hacked = rssi, False
        self.blink_phase = random.random() * 6.28
        self.spawn_frame = 0

    @property
    def color(self):
        if self.rssi > -45: return 11
        if self.rssi > -60: return 10
        if self.rssi > -75: return 9
        return 8


class WifiNetwork:
    def __init__(self, lat, lon, bssid, ssid, channel, rssi):
        self.lat, self.lon, self.bssid = lat, lon, bssid
        self.ssid = ssid[:18]
        self.channel, self.rssi, self.hacked = channel, rssi, False
        self.spawn_frame = 0

    @property
    def color(self):
        if self.rssi > -50: return 11
        if self.rssi > -65: return 10
        if self.rssi > -80: return 9
        return 8


class Particle:
    def __init__(self, x, y, color=3):
        self.x, self.y = x, y
        a = random.random() * 6.28
        s = random.random() * 2.5 + 0.5
        self.vx, self.vy = math.cos(a) * s, math.sin(a) * s
        self.life = random.randint(10, 30)
        self.color = color


class MapMarker:
    def __init__(self, lat, lon, label, mtype):
        self.lat, self.lon, self.label, self.type = lat, lon, label, mtype


# ---------------------------------------------------------------------------
# Map projection
# ---------------------------------------------------------------------------

class MapProjection:
    def __init__(self):
        self.center_lat, self.center_lon = 20.0, 0.0
        self.zoom = 0
        self._target_lat, self._target_lon = 20.0, 0.0

    @property
    def lon_span(self): return ZOOM_LEVELS[self.zoom][0]
    @property
    def lat_span(self): return self.lon_span * MAP_H / W
    @property
    def label(self): return ZOOM_LEVELS[self.zoom][1]

    def smooth_move(self, lat, lon):
        self._target_lat, self._target_lon = lat, lon

    def update(self):
        self.center_lat += (self._target_lat - self.center_lat) * 0.08
        self.center_lon += (self._target_lon - self.center_lon) * 0.08

    def geo_to_screen(self, lat, lon):
        dx = lon - self.center_lon
        if dx > 180: dx -= 360
        elif dx < -180: dx += 360
        dy = self.center_lat - lat
        return (int(W/2 + dx * W / self.lon_span),
                int(HUD_TOP + MAP_H/2 + dy * MAP_H / self.lat_span))

    def screen_visible(self, sx, sy):
        return -20 <= sx <= W+20 and HUD_TOP-10 <= sy <= TERM_Y+10

    def zoom_in(self):
        if self.zoom < len(ZOOM_LEVELS) - 1: self.zoom += 1
    def zoom_out(self):
        if self.zoom > 0: self.zoom -= 1
    def reset_view(self):
        self.zoom = 0
        self._target_lat, self._target_lon = 20.0, 0.0


# ---------------------------------------------------------------------------
# Main game
# ---------------------------------------------------------------------------

class WatchDogsGame:

    def __init__(self, serial_port=None, loot_path=None):
        self._shutting_down = False
        self._cleanup_done = False
        self._cleanup_running = False
        self._cleanup_errors = []
        self._exit_requested = False
        self._exit_handlers = {}
        self._aio_power = AioPowerSession()
        # Pyxel's native quit runs Python atexit callbacks before process::exit.
        # Register before acquiring USB or any other device, including failed boot.
        atexit.register(self._cleanup)
        try:
            self._initialize(serial_port, loot_path)
        finally:
            self._cleanup()
            self._restore_exit_handlers()
            atexit.unregister(self._cleanup)

    def _initialize(self, serial_port=None, loot_path=None):
        pyxel.init(W, H, title="ESP32 Watch Dogs", fps=FPS,
                   quit_key=pyxel.KEY_NONE, display_scale=2)
        self._install_exit_handlers()
        try:
            pyxel.fullscreen(True)
        except Exception:
            pass
        pyxel.mouse(False)

        # Load hacker character sprite into image bank 1
        self._hacker_sprite_ok = False
        self._hacker_w = 0
        self._hacker_h = 0
        self._menu_sprites: dict[int, str] = {}  # tab_idx → sprite path
        self._current_menu_sprite = -1           # currently loaded tab
        self._load_hacker_sprite()

        self.proj = MapProjection()
        self._coastlines = COASTLINES
        self._coast_bounds: list[tuple | None] = []
        for seg in self._coastlines:
            if len(seg) < 2:
                self._coast_bounds.append(None)
                continue
            min_lat = min_lon = 1e9
            max_lat = max_lon = -1e9
            for lat, lon in seg:
                if lat < min_lat: min_lat = lat
                if lat > max_lat: max_lat = lat
                if lon < min_lon: min_lon = lon
                if lon > max_lon: max_lon = lon
            antimerid = (max_lon - min_lon) >= 180
            self._coast_bounds.append(
                (min_lat, max_lat, min_lon, max_lon, antimerid))

        # --- Direct serial/GPS (no IPC) ---
        self.state = AppState()
        self.serial: SerialManager | None = None
        self.gps = GpsManager()
        self.net_mgr = NetworkManager(self.state)
        self.loot: LootManager | None = None

        # AIO v2: auto-enable USB BEFORE serial detection
        # (ESP32 XIAO needs power via USB GPIO before we can find it)
        self._aio_available = AioManager.is_installed()
        self._usb_enabled = False
        if self._aio_available:
            _aio_st = AioManager.get_status() or {}
            self._usb_enabled = _aio_st.get("usb", False)
            if not self._usb_enabled:
                if self._aio_power.toggle("usb", True):
                    self._usb_enabled = True
                    import time as _t
                    _t.sleep(6)  # ESP32 boot + USB enumerate on CM5

        # Open serial (with probe retry for cold-boot)
        port = serial_port or detect_esp32_port()
        if not port and self._usb_enabled:
            # USB just powered on — port may not be visible yet, retry
            import time as _t
            for _retry in range(3):
                _t.sleep(2)
                port = detect_esp32_port()
                if port:
                    break
        if port:
            try:
                self.serial = SerialManager(port)
                self.serial.setup()
                # Wait for firmware to be ready (cold-boot can take 2-6s)
                import time as _t
                _ready = False
                for _attempt in range(10):  # up to 5s (10 × 0.5s)
                    if self.serial.probe():
                        _ready = True
                        break
                    _t.sleep(0.5)
                if not _ready:
                    # Drain anything that arrived late
                    self.serial.serial_conn.reset_input_buffer()
                self.state.connected = True
                self._esp32 = True
                if self._aio_available:
                    self._aio_power.claim("usb")
                # Clear any leftover state from previous crashed session
                self.serial.send_command("stop")
                self.serial.serial_conn.reset_input_buffer()
            except Exception as e:
                self._esp32 = False
                self._term_add(f"[ERR] Serial {port}: {e}", raw=True)
        else:
            self._esp32 = False

        # Open GPS
        self.gps.setup()
        if self._aio_available and self.gps.available:
            self._aio_power.claim("gps")

        # Init loot manager (uses app dir or home)
        # Resolve project root from this file's location (works under sudo).
        # __file__ = .../esp32-watch-dogs/watchdogs/app.py → go up two levels.
        _project_root = Path(__file__).resolve().parent.parent
        _app_dir = loot_path or str(_project_root)
        # Load bigger font for messenger (8x8 BDF) — kept for chat overlay
        _font_path = _project_root / "assets" / "font_8x8.bdf"
        try:
            self._big_font = pyxel.Font(str(_font_path))
        except Exception:
            self._big_font = None
        # Primary UI font — Spleen 5x8 BSD-2-Clause. Crisper than pyxel's
        # built-in 4x6 and applied globally via a pyxel.text monkey-patch
        # a few lines below.
        _ui_font_path = _project_root / "assets" / "spleen-5x8.bdf"
        try:
            self._font = pyxel.Font(str(_ui_font_path))
        except Exception:
            self._font = None
        self._term_font = self._font
        # Global default: every pyxel.text() call without an explicit font
        # now renders with Spleen 5x8.
        if self._font is not None:
            _orig_text = pyxel.text
            _ui_font = self._font
            def _patched_text(x, y, s, col, font=None, _o=_orig_text, _f=_ui_font):
                _o(x, y, s, col, font if font is not None else _f)
            pyxel.text = _patched_text
        try:
            self.loot = LootManager(_app_dir, gps_manager=self.gps)
        except Exception:
            self.loot = None

        # Tile map renderer (offline OSM tiles)
        self._app_dir = _app_dir
        _maps_dir = Path(_app_dir) / "maps"
        self.tile_renderer = TileRenderer(_maps_dir) if _maps_dir.exists() else None
        self._map_downloading = False
        self._map_download_cancel = False

        # AIO v2 hardware — read remaining GPIO states (USB already handled above)
        if self._aio_available:
            _aio_st = AioManager.get_status() or {}
            self._gps_enabled = _aio_st.get("gps", False)
            self._lora_enabled = _aio_st.get("lora", False)
            self._sdr_enabled = _aio_st.get("sdr", False)
        else:
            self._gps_enabled = self.gps.available
            self._lora_enabled = False
            self._sdr_enabled = False

        # Whitelist
        self._whitelist = WhitelistManager(Path(_app_dir) / "whitelist.json")
        self._wl_screen = False
        self._wl_sel = 0
        self._wl_add_step = ""   # "" | "type" | "scan_select" | (input via dialog)
        self._wl_add_type = ""   # "wifi" or "ble"
        self._wl_add_mac = ""
        self._wl_scan_list: list[dict] = []   # merged WiFi+BLE for scan picker
        self._wl_scan_sel = 0                 # selected index in scan picker
        self._wl_scan_status = ""             # "" | "wifi" | "ble" (what's scanning)
        self._wl_scan_timer = 0.0             # time.time() when scan started

        # WPA-sec upload/download
        self._wpasec_busy = False
        self._wpasec_result: Queue = Queue()
        self._wpasec_pending_action = ""  # "upload" or "download"

        # Map clustering + popup
        self._clusters: list[dict] = []
        self._cluster_zoom = -1
        self._cluster_center = (0.0, 0.0)
        self._cluster_frame = 0
        self._cluster_sel = -1             # selected cluster index (-1 = none)
        self._cluster_popup: dict | None = None
        self._popup_scroll = 0
        self._cracked_ssids: dict = {}  # SSID → password (from potfile)

        # Firmware version tracking
        self._fw_version: str = ""          # detected from ESP32
        self._fw_remote_version: str = ""   # latest on GitHub
        self._fw_update_available = False

        # Own-fork release checks never pull code merely by opening the screen.
        self._init_game_updates()

        # MeshCore Messenger (runs in background when LoRa enabled)
        self._lora = LoRaManager()
        self._lora._on_node = self._on_mc_node
        self._lora._on_message = self._on_mc_message
        self._lora._on_dm = self._on_mc_dm
        self._lora._on_dm_ack = self._on_mc_dm_ack
        self._mt = MeshtasticManager()
        self._mt._on_node = self._on_mt_node
        self._mt._on_text = self._on_mt_text
        self._mt._on_status = self._on_mt_status
        self._mt._on_tx = self._on_mt_tx
        self._mt_event_queue = Queue()
        self._mt_screen = False
        self._mt_profile = self._mt.profile_id
        self._mt_profile_screen = False
        self._mt_profile_sel = 0
        self._mt_tx_pending = {}
        self._lora_protocol_picker = False
        self._lora_protocol_sel = 1  # Meshtastic LEFT, MeshCore RIGHT/default
        self._radio_protocol = ""
        self._mesh_view_protocol = "meshcore"
        self._sdr = SDRManager()
        self._sdr_aircraft_xp: set = set()  # ICAO set for XP dedup
        self._subghz = SubGhzManager()      # Monster RF (JanOS RF, CC1101) bridge
        self._subghz_logf = None            # loot capture log file handle
        self._subghz_last_file = ""         # last saved capture path (for replay)
        # Dedicated Sub-GHz screen state (Flipper/Momentum-style)
        self._subghz_screen = False
        self._subghz_mode = "idle"          # idle | read | record | files
        self._subghz_sel = 0
        self._subghz_catches: list = []     # caught/decoded signals (newest first)
        self._subghz_rssi = -120            # live RSSI meter value
        self._subghz_files: list = []       # saved signals (index-aware entries)
        self._subghz_sd_by_idx: dict = {}   # SD idx -> entry (for subghz_tx)
        self._subghz_naming = False         # renaming a saved signal
        self._subghz_name_buf = ""          # rename text buffer
        self._subghz_pending_name = False   # auto-name the newest capture
        self._subghz_rec_start = 0.0        # RAW record start time
        self._subghz_freqs = [433.92, 433.89, 434.08, 315.00, 318.00,
                              390.00, 868.35, 915.00]   # [F] cycles these
        # capture frequency (Read/Record) - persisted so it survives restarts
        # (the light is on 433.89, not the 433.92 default -> must not reset).
        self._subghz_freq = 433.92
        try:
            with open(SUBGHZ_FREQ_FILE) as _fh:
                self._subghz_freq = float(_fh.read().strip())
        except Exception:
            pass
        self._subghz_notice = ""            # transient status line on the screen
        self._subghz_drops = 0              # unrecognized bursts this session (activity)
        self._watch = WatchManager()
        self._watch_screen = False     # watch overlay active
        self._watch_scan_sel = 0       # selected device in scan list
        self._watch_pin_input = ""     # PIN being typed
        self._watch_menu_sel = 0       # selected item in watch menu
        self._watch_nfc_tags: list = []  # cached NFC tag list
        self._watch_log: list = []     # [(text, color)] for watch overlay
        # Auto-connect to watch if already connected in BlueZ
        _watch_addr = self._watch.check_existing()
        if _watch_addr:
            self._watch.connect(_watch_addr)
        # Plugins
        self._plugins = discover_plugins()
        self._plugin_overlay = None  # active plugin overlay
        for p in self._plugins:
            p.on_load(self)
        # Inject PLUGINS menu tab if any plugins loaded
        if self._plugins:
            plugin_items = []
            for p_idx, p in enumerate(self._plugins):
                for mi in p.menu_items():
                    plugin_items.append(
                        (mi.key, f"{mi.label}", f"_plugin_{p_idx}_{mi.action}",
                         f"_p_{p_idx}_{mi.action}", None))
            if plugin_items:
                MENU_CATS.append(("PLUGINS", plugin_items))
        self._lora._on_tx_confirm = self._on_mc_tx_confirm
        self._mc_dm_target = None  # node dict when in DM mode
        self._mc_dm_ack_map: dict[bytes, int] = {}  # ack_hash → log index
        self._mc_screen = False
        self._mc_log: list = []  # [(text, color, tag?)]
        self._mc_tx_pending: dict[bytes, int] = {}  # dedup_key → log index
        self._mc_input = ""
        self._mc_scroll = 0
        # WDGWars Reticulum/LXMF chat (TCP to hub — Phase 1, radio-independent)
        from .reticulum_manager import ReticulumManager, RNS_AVAILABLE
        self._wd = ReticulumManager() if RNS_AVAILABLE else None
        if self._wd:
            self._wd._on_message = self._on_wd_message
            self._wd._on_delivered = self._on_wd_delivered
            self._wd._on_failed = self._on_wd_failed
            self._wd._on_status = self._on_wd_status
        self._wd_event_queue: Queue = Queue()  # thread-safe callback events
        self._wd_screen = False
        self._wd_log: list = []  # [(text, color, tag?)]
        self._wd_input = ""
        self._wd_scroll = 0
        self._wd_started = False
        self._wd_tx_pending: dict[int, int] = {}  # token → log index
        # Load MeshCore config (node name, channels)
        from .lora_manager import load_meshcore_config, save_meshcore_config
        _mc_cfg = load_meshcore_config()
        node_name = _mc_cfg.get("node_name", "")
        # Generate unique name from Ed25519 pubkey if missing OR still on legacy
        # default ("WatchDogs" with no suffix) — old configs from a buggy default.
        if not node_name or node_name == "WatchDogs":
            _, pub = self._lora._get_ed25519_keypair()
            node_name = f"WatchDogs_{pub[:4].hex()}"
            save_meshcore_config(node_name, _mc_cfg.get("_channels", []))
        self._mc_node_name = node_name
        # MeshCore regional preset (EU/UK, US/CA, ...) — picker in ADDONS menu.
        from .lora_manager import DEFAULT_MESHCORE_REGION
        self._mc_region = _mc_cfg.get("region", DEFAULT_MESHCORE_REGION)
        from .radio_profiles import RadioProfileController
        self._rf_profiles = RadioProfileController(initial={
            "meshcore": self._mc_region, "meshtastic": self._mt_profile})
        # The independent RF state records the last successful application;
        # old MeshCore channel configuration may still carry its legacy region.
        self._mc_region = self._rf_profiles.status("meshcore")["active_profile"]
        self._mt_profile = self._rf_profiles.status("meshtastic")["active_profile"]
        self._rf_last_poll = -float("inf")
        self._rf_notices = {}
        self._rf_confirm = None
        self._mc_region_screen = False
        self._mc_region_sel = 0
        self._mc_channels_list = _mc_cfg.get("_channels", [])
        self._mc_active_ch = 0
        self._mc_nodes_panel = False
        self._mc_node_sel = 0  # selected node index in panel
        self._mc_node_action = False  # action menu overlay
        self._mc_node_action_sel = 0
        self._mc_note_editing = False  # note edit mode
        self._mc_note_buf = ""
        self._mc_chan_picker = False
        self._mc_chan_sel = 0
        self._mc_bubbles: list[tuple[str, int]] = []  # (text, frame_expire)
        self._mc_nodes: list[dict] = []
        # Load persistent contacts from loot_db
        if self.loot:
            saved = self.loot.load_contacts()
            for nid, nd in saved.items():
                nd["id"] = nid
                self._mc_nodes.append(nd)
                # Restore pubkey to LoRa manager for DM decryption
                if nd.get("pubkey"):
                    self._lora._known_pubkeys[nid] = bytes.fromhex(
                        nd["pubkey"])

        # Meshtastic uses the same chat/contact renderer with its own state.
        self._mt_view = {
            "log": [], "input": "", "scroll": 0, "nodes_panel": False,
            "node_sel": 0, "node_action": False, "node_action_sel": 0,
            "note_editing": False, "note_buf": "", "dm_target": None,
            "chan_picker": False, "chan_sel": 0, "active_ch": 0,
            "channels_list": [], "node_name": "", "nodes": [],
        }
        if self.loot:
            self._mt_view["nodes"] = list(self.loot.load_meshtastic_contacts().values())
            for channel, message, rssi, timestamp in self.loot.load_meshtastic_messages():
                self._mt_view["log"].append((f"\x10 {message}", C_SUCCESS, timestamp[-8:-3]))
        self._mt.set_known_nodes(self._mt_view["nodes"])

        # Flipper Zero
        self._flipper = FlipperManager()
        self._flipper_screen = False
        self._flipper_log: list[tuple[str, int]] = []  # (text, color)
        self._flipper_scroll = 0
        self._flipper_mode = "idle"  # idle, subghz_rx, subghz_files
        self._flipper_sel = 0
        self._flipper_freq = "433.92"
        self._flipper_files: list[str] = []
        # Badges (persistent via loot_db)
        self._badges: set[str] = set()
        if self.loot:
            try:
                self._badges = self.loot.load_badges()
            except Exception:
                pass
            # Bootstrap badges from historical loot data
            try:
                t = self.loot.loot_totals
                if t.get("wardriving", 0) > 0 or t.get("bt_devices", 0) > 0:
                    self._earn_badge("wardriver")
                if t.get("pcap", 0) > 0:
                    self._earn_badge("handshake_hunter")
                if t.get("et_captures", 0) > 0:
                    self._earn_badge("evil_twin")
                if t.get("mc_messages", 0) > 0:
                    self._earn_badge("meshcore")
                # Check if wpasec potfile exists
                pwd_dir = self.loot.loot_root / "passwords"
                if (pwd_dir / "wpasec_cracked.potfile").exists():
                    self._earn_badge("wpasec_uploader")
                # Check if NFC files exist in any session
                for entry in self.loot.loot_root.iterdir():
                    if entry.is_dir() and (entry / "nfc").is_dir():
                        self._earn_badge("flipper")
                        break
            except Exception:
                pass
        self._flipper_used = "flipper" in self._badges
        self._mc_event_queue: Queue = Queue()  # thread-safe callback events

        # Player
        self.player_lat, self.player_lon = 51.1, 17.9  # Opole default
        self.gps_fix = False
        self.gps_sats = 0
        self.gps_sats_vis = 0
        self._manual_move = False
        self._breath = 0
        self._battery_pct = self._read_battery()  # -1 if unavailable

        # Game state — XP loaded from loot database below
        self.xp = 0
        self._xp_dirty = False
        if self.loot:
            saved_xp = self.loot.load_xp()
            if saved_xp > 0:
                self.xp = saved_xp
            else:
                self.xp = self.loot.calculate_xp_from_loot()
                if self.xp > 0:
                    self.loot.save_xp(self.xp)
        self.ble_devices: list[BleDevice] = []
        self.wifi_networks: list[WifiNetwork] = []
        self.markers: list[MapMarker] = []
        for protocol, contacts in (("meshcore", self._mc_nodes), ("meshtastic", self._mt_view["nodes"])):
            for contact in contacts:
                self._update_mesh_marker(protocol, contact)
        self.particles: list[Particle] = []
        self.msgs: list[tuple[str, int, int]] = []
        self.scan_pulse = 0
        self.glitch_timer = 0
        self.scan_lines: list[int] = []
        # Known devices — loaded from loot history for dedup XP
        self._known_ble: set[str] = set()
        self._known_wifi: set[str] = set()
        if self.loot:
            try:
                hist_ble, hist_wifi = self.loot.get_known_devices()
                self._known_ble = hist_ble
                self._known_wifi = hist_wifi
            except Exception:
                pass

        # Hack
        self.hack_target = None
        self.hack_progress = 0
        self.hacking = False

        # Operation state
        self.wifi_scanning = False
        self.ble_scanning = False
        self.sniffing = False
        self.capturing_hs = False
        self._wifi_scan_only = False   # SCAN tab WiFi (no GPS needed)
        self._ble_scan_only = False    # SCAN tab BLE (no GPS needed)
        self._bt_tracking = False      # BT Tracker
        self._bt_airtag = False        # AirTag Scan
        self._airtag_count = 0         # Apple AirTags detected
        self._smarttag_count = 0       # Samsung SmartTags detected
        self._last_hs_count = 0

        # Wardriving auto-repeat (continuous scan loop)
        self._wifi_scan_done_time = 0.0  # time.time() when last WiFi scan finished
        self._wifi_scan_start_time = 0.0
        self._bt_scan_done_time = 0.0    # time.time() when last BT scan finished
        self._bt_scan_start_time = 0.0   # timeout guard for BT scan
        self._SCAN_INTERVAL = 2.0        # seconds between scan cycles
        self._BT_SCAN_TIMEOUT = 15.0     # auto-finish BT scan after this
        self._ESP_SCAN_TIMEOUT = 20.0    # missing completion is an error, not a busy loop
        self._wardrive_selected = set()  # User choices; physical WiFi/BLE stay exclusive.
        self._wardrive_phase = ""
        self._wardrive_last_phase = ""
        self._wardrive_phase_done = 0.0
        self._wardrive_paused = False
        self._wardrive_save_failed = False
        self._wardrive_save_errors_at_start = None

        # GPS wait dialog (before wardriving)
        self._gps_wait = False           # waiting for GPS fix before starting
        self._gps_wait_cmd = ""          # command to run after fix
        self._gps_wait_state = ""        # state_key for the command
        self._gps_wait_name = ""         # display name
        self._gps_wait_dialog = False    # showing Y/N dialog

        # Menu
        self.menu_open = False
        self.menu_cat = 0       # selected category index
        self.menu_sel = 0       # selected item index within category
        self._pending_cmd = None
        self._pending_cmd_frame = 0
        self._pending_cmd_name = ""
        self._pending_cmd_state = ""
        self._esp_stop_ack = False
        self._esp_stop_requested_at = 0.0
        self._esp_stop_waiting = False
        self._esp_stop_timed_out = False
        self._esp_stop_required = False

        # Input dialog (for commands needing params)
        self.input_mode = False
        self.input_fields: list[dict] = []
        self.input_field_idx = 0
        self._input_pending_cat = 0
        self._input_pending_item = 0

        # Loot screen
        self.loot_screen = False
        self._loot_totals: dict = {}
        self._muted = False   # mute all notification sounds (siren, MC notify)
        self._svc_snd_loaded: set = set()   # pyxel slots with a WAV already loaded
        self._loot_search = ""              # search query
        self._loot_search_active = False    # cursor in search field
        self._loot_search_results: list[dict] = []
        self._loot_search_scroll = 0

        # Python-native attacks (no ESP32 needed)
        self._dragon_drain = DragonDrainAttack(msg_fn=self._attack_msg, loot=self.loot)
        self._mitm = MITMAttack(msg_fn=self._mitm_msg, loot=self.loot)
        self._blueducky = BlueDuckyAttack(msg_fn=self._attack_msg, loot=self.loot)
        self._race = RACEAttack(msg_fn=self._attack_msg, loot=self.loot)

        # WDGWars proximity (host hci0 BLE presence — separate subprocess radio)
        self._prox = ProximityManager()
        self._prox_screen = False
        if self._prox.wanted():
            self._prox.start()          # auto-start unless turned off last time

        # Attack sub-screen state (for multi-step flows)
        self._attack_mode = ""  # "" = none, "dragon_drain", "blueducky", "race", "evil_portal", "evil_twin"
        self._attack_step = ""  # current step in flow
        self._attack_scan_results: list = []
        self._attack_iface = ""

        # Evil Portal / Evil Twin shared state
        self._portal_ssid = ""           # SSID for Evil Portal
        self._portal_list: list = []     # cached [(name, html|None)]
        self._portal_sel = 0             # cursor in portal picker
        self._portal_select_screen = False  # show portal picker overlay
        self._et_net_idx = 0             # primary network index (Evil Twin clone)
        self._et_net_sel = 0             # cursor in network picker
        self._et_net_selected: set[int] = set()  # multi-select: toggled rows
        self._et_select_args = ""        # "1 3 5" for select_networks cmd
        self._et_net_screen = False      # show network picker overlay
        self._et_scan_pending = False    # waiting for scan results

        # Loot password history viewer
        self._loot_pwd_screen = False
        self._loot_pwd_data: list[str] = []   # parsed credential lines
        self._loot_pwd_scroll = 0

        # MITM dedicated screen (JanOS-style sub-screen)
        self._mitm_screen = False
        self._mitm_state = "idle"  # idle/iface/target_mode/scan/input_ip/confirm/running
        self._mitm_ifaces: list = []
        self._mitm_hosts: list = []  # scan results [(ip, mac), ...]
        self._mitm_sel = 0  # cursor for lists
        self._mitm_log: list = []  # [(text, color_idx), ...]
        self._mitm_log_scroll = 0
        self._mitm_victim_ip = ""  # pending single IP
        self._mitm_error = ""  # error message for error state
        self._mitm_close_frame = -1  # frame when MITM screen closed (ESC guard)
        self._esc_consumed_frame = -1  # frame when ESC was consumed by sub-screen
        self._mitm_lock = threading.Lock()

        # Quit confirm dialog
        self.confirm_quit = False

        # Terminal
        self.terminal_lines: list[str] = []
        self._terminal_colors: list[int] = []
        self.term_scroll = 0
        self._term_lock = threading.Lock()

        # Loot GPS points (loaded from all loot sessions)
        self.loot_points: list[dict] = []
        self._loot_points_ts = 0.0

        # Init
        self.proj.smooth_move(self.player_lat, self.player_lon)
        self.proj.zoom = 6

        # --- Boot screen state ---
        self._boot_phase = True
        self._boot_lines: list[tuple[str, int]] = []  # (text, color)
        self._boot_frame = 0
        self._boot_checks_done = False
        self._boot_serial_port = port
        self._build_boot_checks()

        # Already powered hardware still requires an explicit protocol choice.
        if self._lora_enabled:
            self._lora_protocol_picker = True

        if not self._exit_requested:
            pyxel.run(self._guard_update, self._guard_draw)

    def _install_exit_handlers(self):
        if threading.current_thread() is not threading.main_thread():
            return
        def request_exit(signum, frame):
            # Finish the current GPIO/write operation before the next frame
            # closes it. Do not throw SystemExit through a Rust callback.
            self._exit_requested = True
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                self._exit_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_exit)

    def _restore_exit_handlers(self):
        for signum, previous in self._exit_handlers.items():
            signal.signal(signum, previous)
        self._exit_handlers.clear()

    def _guard_update(self):
        try:
            if self._exit_requested and not getattr(self, "_flash_running", False):
                self._cleanup()
                pyxel.quit()
                return
            self.update()
        except BaseException:
            # Pyxel exits directly on callback exceptions, bypassing atexit.
            self._cleanup()
            raise

    def _guard_draw(self):
        try:
            self.draw()
        except BaseException:
            self._cleanup()
            raise

    # ------------------------------------------------------------------
    # XP & Level
    # ------------------------------------------------------------------

    @property
    def level(self) -> int:
        """Calculate level index from XP using LEVEL_TABLE thresholds."""
        for i in range(len(LEVEL_TABLE) - 1, -1, -1):
            if self.xp >= LEVEL_TABLE[i][0]:
                return i + 1
        return 1

    @property
    def level_title(self) -> str:
        """Current level title from LEVEL_TABLE."""
        for i in range(len(LEVEL_TABLE) - 1, -1, -1):
            if self.xp >= LEVEL_TABLE[i][0]:
                return LEVEL_TABLE[i][1]
        return LEVEL_TABLE[0][1]

    @property
    def xp_for_next_level(self) -> int:
        """XP needed for next level (for progress bar)."""
        for i in range(len(LEVEL_TABLE) - 1):
            if self.xp < LEVEL_TABLE[i + 1][0]:
                return LEVEL_TABLE[i + 1][0] - LEVEL_TABLE[i][0]
        return 1

    @property
    def xp_in_current_level(self) -> int:
        """XP progress within current level."""
        for i in range(len(LEVEL_TABLE) - 1):
            if self.xp < LEVEL_TABLE[i + 1][0]:
                return self.xp - LEVEL_TABLE[i][0]
        return 0

    def _earn_badge(self, badge: str) -> None:
        """Award a badge if not already earned. Persists to loot_db."""
        if badge not in self._badges:
            self._badges.add(badge)
            if self.loot:
                self.loot.save_badge(badge)

    def gain_xp(self, amount: int) -> None:
        """Add XP and mark for periodic save."""
        self.xp += amount
        self._xp_dirty = True

    def _save_xp_if_dirty(self) -> None:
        """Persist XP to loot_db.json if changed."""
        if self._xp_dirty and self.loot:
            self.loot.save_xp(self.xp)
            self._xp_dirty = False

    def _load_hacker_sprite(self):
        """Load hacker character PNG into pyxel image bank 1.

        Priority:
        1. assets/hacker.png (user's original hi-res image) → convert to sprite
        2. assets/hacker_sprite.png (pre-converted or generated pixel art)
        3. Generate pixel art fallback
        """
        _project_root = Path(__file__).resolve().parent.parent
        sprite_path = _project_root / "assets" / "hacker_sprite.png"

        # Try converting hi-res source image first
        src = _project_root / "assets" / "hacker.png"
        if src.exists():
            try:
                from .convert_sprite import convert
                convert(str(src), str(sprite_path))
            except Exception:
                pass

        # If no sprite yet, generate pixel art fallback
        if not sprite_path.exists():
            try:
                from .generate_hacker_sprite import generate
                generate()
            except Exception:
                pass

        if not sprite_path.exists():
            return

        try:
            pyxel.image(1).load(0, 0, str(sprite_path))
            from PIL import Image
            img = Image.open(str(sprite_path))
            self._hacker_w, self._hacker_h = img.size
            self._hacker_sprite_ok = True
        except Exception:
            self._hacker_sprite_ok = False

        # Build per-tab sprite map: tab_index → sprite path
        # 0=SCAN, 1=SNIFF, 2=ATTACK, 3=ADDONS, 4=SYSTEM
        _tab_sprites = {
            0: "scan_sprite.png",      # SCAN tab
            1: "sniff_sprite.png",     # SNIFF tab
            2: "attack_sprite.png",    # ATTACK tab
            3: "addons_sprite.png",    # ADDONS tab
            4: "hacker_sprite.png",    # SYSTEM tab
        }
        for tab_idx, fname in _tab_sprites.items():
            p = _project_root / "assets" / fname
            if p.exists():
                self._menu_sprites[tab_idx] = str(p)
        self._current_menu_sprite = 4  # hacker_sprite loaded by default

        # Load CB radio sprite into image bank 2 (for MeshCore notifications)
        self._radio_sprite_ok = False
        radio_sprite = _project_root / "assets" / "radio_sprite.png"
        radio_src = _project_root / "assets" / "radio.png"
        # Convert hi-res source if available (like hacker sprite)
        if radio_src.exists():
            try:
                from .convert_radio_sprite import convert
                convert(str(radio_src), str(radio_sprite), target_h=48)
            except Exception:
                pass
        if radio_sprite.exists():
            try:
                pyxel.images[2].load(0, 0, str(radio_sprite))
                from PIL import Image
                img = Image.open(str(radio_sprite))
                self._radio_w, self._radio_h = img.size
                self._radio_sprite_ok = True
            except Exception:
                self._radio_w, self._radio_h = 32, 48

    def _build_boot_checks(self):
        """Prepare boot screen check lines."""
        import pyxel as px

        checks: list[tuple[str, int]] = [
            ("ESP32 WATCH DOGS v1.0", C_HACK_CYAN),
            ("=" * 40, C_DIM),
            ("", 0),
            ("SYSTEM CHECK", C_HACK_CYAN),
            ("", 0),
        ]

        # Python
        v = sys.version_info
        checks.append((f"  Python {v.major}.{v.minor}.{v.micro}", C_SUCCESS))

        # pyxel
        checks.append((f"  pyxel {px.VERSION}", C_SUCCESS))

        # pyserial
        try:
            import serial as ser
            checks.append((f"  pyserial {ser.__version__}", C_SUCCESS))
        except ImportError:
            checks.append(("  pyserial — NOT FOUND", C_ERROR))

        # Pillow
        try:
            import PIL
            checks.append((f"  Pillow {PIL.__version__}", C_SUCCESS))
        except ImportError:
            checks.append(("  Pillow — NOT FOUND", C_ERROR))

        # scapy (Dragon Drain, MITM)
        try:
            import scapy
            _sv = getattr(scapy, "VERSION", "?")
            checks.append((f"  scapy {_sv}", C_SUCCESS))
        except ImportError:
            checks.append(("  scapy — not found (Dragon Drain/MITM)", C_WARNING))

        # cryptography (MeshCore AES)
        try:
            import cryptography
            checks.append((f"  cryptography {cryptography.__version__}", C_SUCCESS))
        except ImportError:
            checks.append(("  cryptography — not found (MeshCore)", C_WARNING))

        # LoRaRF (LoRa SX1262)
        try:
            import LoRaRF
            checks.append(("  LoRaRF (SX1262)", C_SUCCESS))
        except ImportError:
            checks.append(("  LoRaRF — not found (LoRa radio)", C_WARNING))

        checks.append(("", 0))
        checks.append(("HARDWARE", C_HACK_CYAN))
        checks.append(("", 0))

        # ESP32
        if self._esp32:
            fw_info = f" (FW: v{self._fw_version})" if self._fw_version else ""
            checks.append((f"  ESP32 on {self._boot_serial_port}{fw_info}", C_SUCCESS))
        else:
            checks.append(("  ESP32 — not connected", C_WARNING))

        # GPS
        if self.gps.available:
            checks.append((f"  GPS on {self.gps.device}", C_SUCCESS))
        else:
            checks.append(("  GPS — not found", C_WARNING))

        # AIO v2 (GPIO power control)
        if self._aio_available:
            _gps_st = "ON" if self._gps_enabled else "OFF"
            _lora_st = "ON" if self._lora_enabled else "OFF"
            checks.append((f"  AIO v2 — GPS:{_gps_st}  LoRa:{_lora_st}", C_SUCCESS))
        else:
            checks.append(("  AIO v2 — not detected", C_DIM))

        # pinctrl (GPIO toggling)
        import shutil
        if shutil.which("pinctrl"):
            checks.append(("  pinctrl available", C_SUCCESS))
        else:
            checks.append(("  pinctrl — not found (GPIO toggle)", C_WARNING))

        # SDR tools
        _sdr_tools = []
        if shutil.which("dump1090"):
            _sdr_tools.append("dump1090")
        if shutil.which("rtl_433"):
            _sdr_tools.append("rtl_433")
        if _sdr_tools:
            checks.append((f"  SDR: {' + '.join(_sdr_tools)}", C_SUCCESS))
        else:
            checks.append(("  SDR: dump1090/rtl_433 not found", C_WARNING))

        # Loot
        loot_ok = self.loot is not None
        if loot_ok:
            checks.append(("  Loot manager ready", C_SUCCESS))
        else:
            checks.append(("  Loot manager — FAILED", C_ERROR))

        # Tile maps
        has_tiles = self.tile_renderer and self.tile_renderer.has_tiles()
        if has_tiles:
            checks.append(("  Offline map tiles loaded", C_SUCCESS))
        else:
            checks.append(("  Offline map — none (SYSTEM>Download Map)", C_DIM))

        checks.append(("", 0))
        checks.append(("=" * 40, C_DIM))

        # Check if any FAIL
        has_errors = any("NOT FOUND" in t or "FAILED" in t for t, c in checks if c == C_ERROR)
        if has_errors:
            checks.append(("ERRORS DETECTED — run setup.sh", C_ERROR))
            checks.append(("Press [ENTER] to continue anyway", C_DIM))
            checks.append(("Press [ESC] to quit", C_DIM))
        else:
            checks.append(("ALL SYSTEMS GO", C_SUCCESS))
            checks.append(("Starting in 2s... [ENTER] to skip", C_DIM))

        self._boot_checks = checks
        self._boot_has_errors = has_errors

    def _finish_boot(self):
        """Transition from boot screen to main game."""
        self._boot_phase = False
        port = self._boot_serial_port

        self._term_add("[SYS] ESP32 Watch Dogs v1.0", raw=True)
        self._term_add("[SYS] TAB=menu  SPACE=hack  =/]=zoom+  -/[=zoom-", raw=True)
        if self._esp32:
            self._term_add(f"[OK] ESP32 connected on {port}", raw=True)
            # Probe firmware version
            if not self._fw_version:
                try:
                    self.serial.send_command("version")
                except Exception:
                    pass
        else:
            self._term_add("[WARN] No ESP32 — serial offline", raw=True)
        if self.gps.available:
            self._term_add(f"[OK] GPS on {self.gps.device}", raw=True)
        else:
            self._term_add("[WARN] GPS not found", raw=True)

        self.msg("[SYS] ESP32 Watch Dogs", C_HACK_CYAN)
        self.msg("[SYS] TAB=menu  `=loot  SPACE=hack", C_DIM)

        self._refresh_loot_points()
        self._loot_points_ts = time.time()

    # Big pixel font for boot logo (5x7 bitmap per letter)
    _LOGO_FONT = {
        'W': ["10001","10001","10001","10101","10101","11011","10001"],
        'A': ["01110","10001","10001","11111","10001","10001","10001"],
        'T': ["11111","00100","00100","00100","00100","00100","00100"],
        'C': ["01110","10001","10000","10000","10000","10001","01110"],
        'H': ["10001","10001","10001","11111","10001","10001","10001"],
        'D': ["11100","10010","10001","10001","10001","10010","11100"],
        'O': ["01110","10001","10001","10001","10001","10001","01110"],
        'G': ["01110","10001","10000","10011","10001","10001","01110"],
        'S': ["01110","10001","10000","01110","00001","10001","01110"],
    }

    def _draw_logo_word(self, text, x0, y0, cw, ch, color, gx=0):
        """Render word using big pixel font with optional glitch offset."""
        cx = x0 + gx
        for letter in text:
            bmap = self._LOGO_FONT.get(letter)
            if not bmap:
                cx += cw * 3
                continue
            for ri, row in enumerate(bmap):
                for ci, bit in enumerate(row):
                    if bit == '1':
                        pyxel.rect(cx + ci * cw, y0 + ri * ch,
                                   cw - 1, ch - 1, color)
            cx += 6 * cw  # 5 cols + 1 gap

    def _draw_boot_screen(self):
        """Draw the startup check / boot screen with cyber glitch logo."""
        pyxel.cls(0)
        frame = self._boot_frame

        # Dark scanlines
        for y in range(0, H, 3):
            pyxel.line(0, y, W - 1, y, 1)

        # ── Logo: single line WATCH_DOGS_GO ──
        cw, ch = 6, 7  # cell size (fits 13 chars + 2 spaces on 640px)
        word = "WATCH DOGS GO"
        # Calculate total width
        logo_w = 0
        for c in word:
            if c == ' ':
                logo_w += cw * 3
            else:
                logo_w += 6 * cw
        x1 = (W - logo_w) // 2
        y1 = 25
        gx1 = (frame % 7) - 3 if frame % 19 < 3 else 0
        self._draw_logo_word(word, x1 + 2, y1 + 2, cw, ch, 1)  # shadow
        self._draw_logo_word(word, x1, y1, cw, ch, C_HACK_CYAN, gx1)

        # ── Glitch artifacts ──
        logo_bottom = y1 + 7 * ch
        if frame % 11 < 2:
            gy = random.randint(y1, logo_bottom)
            gw = random.randint(40, 200)
            gx = random.randint(0, W - gw)
            pyxel.rect(gx, gy, gw, 2,
                       random.choice([C_HACK_CYAN, C_ERROR, 1]))
        for _ in range(6):
            nx = random.randint(x1 - 15, x1 + logo_w + 15)
            ny = random.randint(y1 - 5, logo_bottom + 5)
            pyxel.pset(nx, ny, random.choice([C_HACK_CYAN, C_DIM, 1]))

        # ── Subtitle ──
        sub_y = logo_bottom + 8
        pyxel.text((W - 36) // 2 + 40, sub_y, "by LOCOSP", C_DIM)
        _modified = "Modified by KiloGramowy"
        pyxel.text((W - len(_modified) * 5) // 2, sub_y + 12,
                   _modified, C_DIM)
        # Edition subtitle types in as the system initializes.
        _ed = "ESP32-C5 Edition"
        _ex = (W - 64) // 2
        _en = min(len(_ed), max(0, frame - 6))
        pyxel.text(_ex, sub_y + 24, _ed[:_en], C_TEXT)
        if _en < len(_ed) and frame % 6 < 4:
            pyxel.text(_ex + _en * 5, sub_y + 24, "_", C_HACK_CYAN)

        # ── System checks (bottom, auto-scroll) ──
        visible = min(len(self._boot_checks), frame // 4 + 1)
        cx0 = 30
        cy0 = sub_y + 42
        max_y = H - 14  # bottom limit for text
        max_visible_lines = (max_y - cy0) // 10
        # Auto-scroll: if more lines than fit, scroll up
        scroll_off = max(0, visible - max_visible_lines)

        for i in range(scroll_off, visible):
            text, color = self._boot_checks[i]
            if not text:
                continue
            y = cy0 + (i - scroll_off) * 10
            if y > max_y:
                break
            # Status dot before each indented check line — reads as a checklist.
            if text.startswith("  "):
                _dc = color if color in (C_ERROR, C_WARNING) else C_SUCCESS
                pyxel.circ(cx0 + 4, y + 3, 1, _dc)
            if i == visible - 1 and frame % 20 < 10:
                pyxel.text(cx0 + len(text) * 4 + 2, y, "_", C_HACK_CYAN)
            pyxel.text(cx0, y, text, color)

        # ── Progress bar ──
        bar_y = H - 8
        total = len(self._boot_checks)
        pct = min(visible / total, 1.0) if total > 0 else 1.0
        bar_w = int((W - 60) * pct)
        pyxel.rect(30, bar_y, bar_w, 3, C_HACK_CYAN)
        # Boot energy: a shimmer runs the filled bar + a glowing leading tip.
        if bar_w > 4:
            _bs = int((pyxel.frame_count * 3) % (bar_w + 10))
            if _bs < bar_w:
                pyxel.line(30 + _bs, bar_y, 30 + _bs, bar_y + 2, 7)
        if 0 < bar_w < (W - 60):
            _tipc = 7 if pyxel.frame_count % 10 < 5 else C_HACK_CYAN
            pyxel.rect(30 + bar_w - 1, bar_y - 1, 2, 5, _tipc)
        pyxel.rectb(30, bar_y, W - 60, 3, C_DIM)

    def msg(self, text, color=C_TEXT):
        self.msgs.append((text, 180, color))
        if len(self.msgs) > 8:
            self.msgs.pop(0)

    def _attack_msg(self, text, color=C_DIM):
        """Callback for Python-native attacks (thread-safe msg + terminal)."""
        self.msg(text, color)
        self._term_add(text, raw=True)

    def _mitm_msg(self, text, color=C_TEXT):
        """Callback for MITM attack — logs to MITM screen."""
        with self._mitm_lock:
            self._mitm_log.append((text, color))
            if len(self._mitm_log) > 500:
                self._mitm_log = self._mitm_log[-500:]

    def _try_reconnect_esp32(self) -> bool:
        """Try to detect and reconnect ESP32. Like JanOS wait_for_esp32."""
        if getattr(self, "_shutting_down", False):
            return False
        port = detect_esp32_port()
        if not port:
            return False
        try:
            self.serial = SerialManager(port)
            self.serial.setup()
            # A replugged ESP32 cannot acknowledge a stop sent to the old port.
            # Establish a new barrier directly, without recursing through _send.
            if not self.serial.send_command("stop"):
                raise OSError("ESP32 stop handshake could not be written")
            self._clear_esp_scan_state(cancel_pending=True)
            self._esp_stop_waiting = True
            self._esp_stop_required = False
            self._esp_stop_timed_out = self._esp_stop_ack = False
            self._esp_stop_requested_at = time.monotonic()
            self._pending_cmd_frame = pyxel.frame_count + 15
            if getattr(self, "_wardrive_selected", None):
                self._wardrive_pause("ESP32 reconnected", request_stop=False)
            self._term_add("[TX] stop (reconnect handshake)", raw=True)
            self._esp32 = True
            self.state.connected = True
            self._boot_serial_port = port
            self._term_add(f"[SYS] ESP32 found on {port}", raw=True)
            # Probe firmware version
            if not self._fw_version:
                try:
                    self.serial.send_command("version")
                except Exception:
                    pass
            return True
        except Exception as e:
            self._esp32 = False
            self.state.connected = False
            if self.serial:
                try:
                    self.serial.close()
                except Exception:
                    pass
            self.serial = None
            self._term_add(f"[ERR] Reconnect failed: {e}", raw=True)
            return False

    def _check_fw_update(self) -> None:
        """Background: check GitHub for newer firmware release."""
        from .config import FIRMWARE_RELEASE_URL
        import json
        from urllib.request import Request, urlopen
        try:
            req = Request(FIRMWARE_RELEASE_URL)
            req.add_header("User-Agent", "ESP32-Watch-Dogs")
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            remote = tag.lstrip("v")
            local = self._fw_version.lstrip("v")
            if remote and local:
                r_parts = [int(x) for x in remote.split(".")]
                l_parts = [int(x) for x in local.split(".")]
                if r_parts > l_parts:
                    self._fw_remote_version = remote
                    self._fw_update_available = True
                    self.msg(f"[FW] Update available: v{local} -> v{remote}",
                             C_WARNING)
                    self._term_add(
                        f"[FW] New firmware v{remote} available! "
                        f"(current: v{local})", raw=True)
        except Exception:
            pass

    def _game_git(self, root, *args, timeout=30):
        """Run a git command in `root`, as the repo OWNER when the game is root.

        The game runs under `sudo ./run.sh` (root), but the checkout is owned by
        the player and root has no GitHub SSH key / known_hosts — so git over
        SSH fails with 'Host key verification failed' and the updater's fetch /
        pull silently die. Dropping to the owner (`sudo -u <owner>`) makes git
        use THEIR ssh config, which works for the private dev repo (SSH) and the
        public repo (HTTPS) alike. `-c safe.directory` covers the ownership
        guard. When the game isn't root, git runs directly.
        """
        import os
        import subprocess
        cmd = ["git", "-C", str(root), "-c", f"safe.directory={root}", *args]
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                import pwd
                uid = os.stat(root).st_uid
                if uid != 0:
                    owner = pwd.getpwuid(uid).pw_name
                    cmd = ["sudo", "-n", "-H", "-u", owner] + cmd
        except Exception:
            pass
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _check_game_update(self) -> None:
        """Worker: fetch a published release; UI changes stay on the main loop."""
        try:
            self._game_update_events.put(("check", self._game_updates.check()))
        except Exception:
            logging.getLogger(__name__).exception("Game release check failed")
            self._game_update_events.put(("check_error", "Could not check for updates."))

    def _send(self, cmd: str, *, wardrive=False):
        if getattr(self, "_shutting_down", False):
            return False
        if (not wardrive and getattr(self, "_wardrive_selected", None)
                and cmd.strip().partition(" ")[0] not in ("version", "ping")):
            self._cancel_wardrive_plan(clear_active=True)
        reconnected = False
        if not self.serial or not self.serial.is_open:
            reconnected = self._try_reconnect_esp32()
        if not self.serial or not self.serial.is_open:
            self._term_add("[ERR] No serial connection", raw=True)
            return False
        if reconnected and getattr(self, "_esp_stop_waiting", False):
            if cmd.strip() == "stop":
                return True  # The reconnect handshake already wrote this command.
            self._term_add("[ERR] ESP32 reconnected; wait for stop confirmation", raw=True)
            return False
        try:
            sent = self.serial.send_command(cmd)
        except Exception as exc:
            self._term_add(f"[ERR] Serial command failed: {exc}", raw=True)
            return False
        if not sent:
            self._term_add("[ERR] Serial command was not sent", raw=True)
            return False
        suffix = " (after reconnect)" if reconnected else ""
        self._term_add(f"[TX] {cmd}{suffix}", raw=True)
        return True

    # ------------------------------------------------------------------
    # Terminal helpers
    # ------------------------------------------------------------------

    def _term_add(self, line: str, raw=False):
        """Add line to terminal. raw=True skips filtering."""
        if not raw:
            if not self._term_filter(line):
                return
        color = _color_for_terminal_line(line)
        with self._term_lock:
            self.terminal_lines.append(line)
            self._terminal_colors.append(color)
            if len(self.terminal_lines) > 500:
                self.terminal_lines = self.terminal_lines[-500:]
                self._terminal_colors = self._terminal_colors[-500:]

    def _term_filter(self, s: str) -> bool:
        """Return True if line should be shown in terminal."""
        s = s.strip()
        if s in _TERM_SKIP_EXACT:
            return False
        # Echo of sent commands
        if s.startswith("> ") and len(s) < 40:
            return False
        for prefix in _TERM_SKIP_PREFIX:
            if s.startswith(prefix):
                return False
        for substr in _TERM_SKIP_CONTAIN:
            if substr in s:
                return False
        # Long base64 blobs (outside HS capture)
        if not self.capturing_hs:
            if len(s) > 50 and all(
                c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n"
                for c in s
            ):
                return False
            if s.startswith("---") and ("PCAP" in s or "HCCAPX" in s):
                return False
        # Raw hex dumps (sniffer data without useful keywords)
        if len(s) > 30 and s.count(":") > 5 and not any(
            kw in s for kw in ("RSSI", "SSID", "Name", "AP", "BSSID")
        ):
            return False
        return True

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self):
        # --- Boot screen phase ---
        if self._boot_phase:
            self._boot_frame += 1
            if pyxel.btnp(pyxel.KEY_RETURN):
                self._finish_boot()
            elif pyxel.btnp(pyxel.KEY_ESCAPE) and self._boot_has_errors:
                pyxel.quit()
            elif not self._boot_has_errors and self._boot_frame >= 300:
                # Auto-skip after 4s if no errors
                self._finish_boot()
            return

        # Once devices were stopped for an upgrade, never restart scans or
        # poll closed transports. Only the updater remains interactive.
        if getattr(self, "_gameupd_closed_for_install", False):
            self._poll_game_update_events()
            self._update_gameupd_screen()
            return

        # One-shot: check GitHub for a newer game version (background, once).
        if not getattr(self, "_game_check_started", False):
            self._game_check_started = True
            self._request_game_update_check()

        self._poll_serial()
        self._poll_gps()
        self._assign_missing_mesh_gps()
        self._poll_lora()
        self._poll_reticulum()
        self._poll_sdr()
        self._poll_subghz()
        self._poll_watch()
        self._poll_wpasec_result()
        self._poll_proximity()
        # LoRa Reticulum active → hand the GIL to the radio RX thread each
        # frame so it services RX_DONE promptly and reads each packet before
        # the next arrives (clean FIFO reads → RNS can decrypt → delivery).
        # This throttles the game to ~12 FPS for the duration of LoRa use;
        # deliberate — the CM5 has headroom and reliable chat RX wins here.
        if self._wd is not None and self._wd.lora_active:
            time.sleep(0.05)
        self._breath = (self._breath + 1) % 120

        # Camera follows player
        self.proj.smooth_move(self.player_lat, self.player_lon)
        self.proj.update()

        self.scan_pulse = (self.scan_pulse + 1) % 60

        # Delayed command (stop → wait → new cmd)
        self._dispatch_pending_esp_command()

        self._update_hack()
        self._update_wardriving_loop()

        # Particles
        for p in self.particles:
            p.x += p.vx; p.y += p.vy; p.life -= 1
        self.particles = [p for p in self.particles if p.life > 0]
        self.msgs = [(t, tm-1, c) for t, tm, c in self.msgs if tm > 1]

        if self.glitch_timer > 0:
            self.glitch_timer -= 1
        if pyxel.frame_count % 15 == 0:
            self.scan_lines = [random.randint(0, H-1) for _ in range(random.randint(0, 2))]

        # Refresh loot points every 30s + persist XP
        if time.time() - self._loot_points_ts > 30:
            self._refresh_loot_points()
            self._save_xp_if_dirty()
            self._battery_pct = self._read_battery()
            self._loot_points_ts = time.time()

        self._poll_game_update_events()
        self._maybe_show_game_update_notice()
        if getattr(self, "_game_update_notice", None) is not None:
            self._update_game_update_notice()
            return
        if getattr(self, "_gameupd_screen", False):
            self._update_gameupd_screen()
            return

        # Keys
        # Loot screen toggle (backtick / grave accent)
        _bq = getattr(pyxel, "KEY_BACKQUOTE", getattr(pyxel, "KEY_GRAVE_ACCENT", 96))
        if pyxel.btnp(_bq):
            self.loot_screen = not self.loot_screen
            if not self.loot_screen:
                self._loot_pwd_screen = False

        # Overlay screens — block normal input
        if self._et_net_screen or self._portal_select_screen:
            self._update_picker_overlay()
        elif self.input_mode:
            self._update_input_dialog()
        elif self._mitm_screen:
            self._update_mitm_screen()
        elif self._flipper_screen:
            self._update_flipper_screen()
        elif self._subghz_screen:
            self._update_subghz_screen()
        elif getattr(self, '_flash_screen', False):
            self._update_flash_screen()
        elif any(getattr(p, 'overlay_active', False) for p in self._plugins):
            for p in self._plugins:
                if getattr(p, 'overlay_active', False):
                    p.on_update()
                    break
        elif self._watch_screen:
            self._update_watch_screen()
        elif self._wl_screen:
            self._update_wl_screen()
        elif self._lora_protocol_picker:
            self._update_lora_protocol_picker()
        elif self._mt_profile_screen:
            self._update_mt_profile_picker()
        elif self._mt_screen:
            self._update_mt_screen()
        elif self._mc_screen:
            self._update_mc_screen()
        elif self._wd_screen:
            self._update_wd_screen()
        elif self._mc_region_screen:
            self._update_mc_region_picker()
        elif self._prox_screen:
            self._update_prox_screen()
        elif self._cluster_popup:
            self._update_cluster_popup()
        elif self.loot_screen:
            self._update_loot_screen()
        else:
            if pyxel.btnp(pyxel.KEY_TAB):
                self.menu_open = not self.menu_open
                if self.menu_open:
                    self.menu_sel = 0

            if self.menu_open:
                self._update_menu()
            else:
                # Cluster keyboard navigation (C = enter/exit, arrows, ENTER)
                self._handle_cluster_nav()
                # Check Python-native attack key handling first
                if self._attack_mode:
                    for _k in range(pyxel.KEY_1, pyxel.KEY_9 + 1):
                        if pyxel.btnp(_k):
                            if self._handle_attack_key(_k):
                                pass  # consumed
                    for _k in (pyxel.KEY_S, pyxel.KEY_C, pyxel.KEY_E,
                               pyxel.KEY_H, pyxel.KEY_L, pyxel.KEY_R,
                               pyxel.KEY_X, pyxel.KEY_ESCAPE):
                        if pyxel.btnp(_k):
                            if self._handle_attack_key(_k):
                                pass  # consumed

                if (pyxel.btnp(pyxel.KEY_PLUS) or pyxel.btnp(pyxel.KEY_KP_PLUS)
                        or pyxel.btnp(pyxel.KEY_EQUALS)
                        or pyxel.btnp(pyxel.KEY_RIGHTBRACKET)):
                    self.proj.zoom_in()
                if (pyxel.btnp(pyxel.KEY_MINUS) or pyxel.btnp(pyxel.KEY_KP_MINUS)
                        or pyxel.btnp(pyxel.KEY_LEFTBRACKET)):
                    self.proj.zoom_out()
                if pyxel.btnp(pyxel.KEY_0):
                    self.proj.reset_view()
                if pyxel.btnp(pyxel.KEY_S) and not self._attack_mode:
                    self._request_esp_stop()
                    self._clear_esp_scan_state(cancel_pending=True)
                    self.wifi_scanning = False
                    self.ble_scanning = False
                    self._wifi_scan_only = False
                    self._ble_scan_only = False
                    self._bt_tracking = False
                    self._bt_airtag = False
                    self.sniffing = False
                    self.capturing_hs = False
                    self._wifi_scan_done_time = 0.0
                    self._bt_scan_done_time = 0.0
                    self._gps_wait = False
                    self._gps_wait_cmd = ""
                    # Stop Python-native attacks
                    if self._dragon_drain.running:
                        self._dragon_drain.stop()
                    if self._mitm.running:
                        self._mitm.stop()
                    if self._blueducky.running:
                        self._blueducky.stop()
                    if self._race.running:
                        self._race.stop()
                    self._attack_mode = ""
                    self._attack_step = ""
                    self._attack_scan_results = []
                    self.msg("[STOP] All operations stopped", C_WARNING)
                    self.glitch_timer = 3
                # Manual pan (disabled when cluster is selected — arrows navigate clusters)
                if self._cluster_sel < 0:
                    speed = self.proj.lon_span / W * 3
                    if pyxel.btn(pyxel.KEY_UP):
                        self.player_lat += speed; self._manual_move = True
                    if pyxel.btn(pyxel.KEY_DOWN):
                        self.player_lat -= speed; self._manual_move = True
                    if pyxel.btn(pyxel.KEY_LEFT):
                        self.player_lon -= speed; self._manual_move = True
                    if pyxel.btn(pyxel.KEY_RIGHT):
                        self.player_lon += speed; self._manual_move = True
                self.player_lat = max(-85, min(85, self.player_lat))

            # Terminal scroll
            if pyxel.btnp(pyxel.KEY_PAGEUP):
                with self._term_lock:
                    self.term_scroll = min(self.term_scroll + 5,
                                           max(0, len(self.terminal_lines) - 5))
            if pyxel.btnp(pyxel.KEY_PAGEDOWN):
                self.term_scroll = max(0, self.term_scroll - 5)
            if pyxel.btnp(pyxel.KEY_END):
                self.term_scroll = 0

        # GPS wait dialog (Y=wait, N=cancel)
        if self._gps_wait_dialog:
            if pyxel.btnp(pyxel.KEY_Y):
                self._gps_wait_dialog = False
                self._gps_wait = True
                self.msg("[GPS] Waiting for fix...", C_WARNING)
            elif pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                if getattr(self, "_wardrive_selected", None):
                    self._cancel_wardrive_plan()
                self._gps_wait_dialog = False
                self._gps_wait = False
                self._gps_wait_cmd = ""
                self.msg("[GPS] Cancelled", C_DIM)
        elif self.confirm_quit:
            if pyxel.btnp(pyxel.KEY_Y):
                if getattr(self, "_flash_running", False):
                    self.msg("[FLASH] Wait for firmware flashing to finish", C_WARNING)
                    self.confirm_quit = False
                    return
                self._cleanup()
                pyxel.quit()
            if pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                self.confirm_quit = False
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            if self._esc_consumed_frame == pyxel.frame_count:
                pass  # ESC already handled by sub-screen this frame
            elif any(hasattr(p, '_auth_pin_active') and p._auth_pin_active()
                     for p in self._plugins):
                # Dismiss plugin PIN popup — remember token so it doesn't reappear
                for p in self._plugins:
                    if hasattr(p, '_auth_pin_active') and p._auth_pin_active():
                        p._auth_dismissed_token = getattr(p, '_auth_current_token', '')
                        p._auth_pin = ""
                        p._auth_pin_expiry = 0
            elif self._mitm_screen:
                pass  # handled in _update_mitm_screen
            elif self._watch_screen:
                pass  # handled in _update_watch_screen
            elif self._wl_screen:
                pass  # handled in _update_wl_screen
            elif (self._mc_screen or self._mt_screen or self._lora_protocol_picker
                  or self._mt_profile_screen or self._mc_region_screen):
                pass  # handled in _update_mc_screen
            elif self._wd_screen:
                pass  # handled in _update_wd_screen
            elif self._prox_screen:
                pass  # handled in _update_prox_screen
            elif self._gps_wait:
                if getattr(self, "_wardrive_selected", None):
                    self._cancel_wardrive_plan()
                self._gps_wait = False
                self._gps_wait_cmd = ""
                self.msg("[GPS] Wait cancelled", C_DIM)
            elif self.loot_screen:
                if self._loot_search_active:
                    self._loot_search_active = False
                elif self._loot_search_results or self._loot_search:
                    self._loot_search = ""
                    self._loot_search_results = []
                    self._loot_search_scroll = 0
                else:
                    self.loot_screen = False
            elif self.input_mode:
                pass  # handled in _update_input_dialog
            elif self.menu_open:
                self.menu_open = False
            else:
                self.confirm_quit = True

    def _update_menu(self):
        _, items = MENU_CATS[self.menu_cat]
        if pyxel.btnp(pyxel.KEY_LEFT):
            self.menu_cat = (self.menu_cat - 1) % len(MENU_CATS)
            self.menu_sel = 0
        if pyxel.btnp(pyxel.KEY_RIGHT):
            self.menu_cat = (self.menu_cat + 1) % len(MENU_CATS)
            self.menu_sel = 0
        _, items = MENU_CATS[self.menu_cat]
        if pyxel.btnp(pyxel.KEY_UP):
            self.menu_sel = (self.menu_sel - 1) % len(items)
        if pyxel.btnp(pyxel.KEY_DOWN):
            self.menu_sel = (self.menu_sel + 1) % len(items)
        if pyxel.btnp(pyxel.KEY_RETURN):
            self._activate_menu_item(self.menu_cat, self.menu_sel)

    def _activate_menu_item(self, cat_idx: int, item_idx: int):
        _, items = MENU_CATS[cat_idx]
        _hotkey, name, cmd, state_key, input_type = items[item_idx]
        if input_type:
            self.menu_open = False
            self._start_input_dialog(cat_idx, item_idx, input_type)
            return
        # Keep menu open for toggle actions (GPS, LoRa, SDR, USB, Mute)
        if state_key not in ("_gps_toggle", "_lora_toggle",
                             "_sdr_toggle", "_usb_toggle", "_mute_toggle",
                             "_prox_toggle"):
            self.menu_open = False
        self._execute_item(cmd, state_key, name, [])

    def _start_input_dialog(self, cat_idx: int, item_idx: int, input_type: str):
        self._input_pending_cat = cat_idx
        self._input_pending_item = item_idx
        if input_type == "bssid_ch":
            self.input_fields = [{"label": "BSSID", "value": ""},
                                  {"label": "CH",    "value": ""}]
        elif input_type == "ssid":
            self.input_fields = [{"label": "SSID",  "value": ""}]
        elif input_type == "mac":
            self.input_fields = [{"label": "MAC",   "value": ""}]
        elif input_type == "text":
            self.input_fields = [{"label": "TEXT",  "value": ""}]
        else:
            self.input_fields = [{"label": "VALUE", "value": ""}]
        self.input_field_idx = 0
        self.input_mode = True

    def _survival_open(self, url, label):
        """Crisis hub: open an offline resource (Kiwix / ctOS map) in a browser."""
        import subprocess
        from .config import SURVIVAL_BROWSER
        try:
            subprocess.Popen(
                [SURVIVAL_BROWSER, f"--app={url}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.msg(f"[CRISIS] {label}", C_SUCCESS)
            self._term_add(f"[CRISIS] launched {label} -> {url}", raw=True)
        except FileNotFoundError:
            self.msg(f"[CRISIS] {SURVIVAL_BROWSER} not found", C_ERROR)
        except Exception as e:
            self.msg(f"[CRISIS] launch failed: {e}", C_ERROR)

    def _survival_nav(self):
        """Crisis hub: launch Organic Maps (offline vector maps + routing)."""
        import subprocess
        from .config import SURVIVAL_NAV_CMD
        try:
            subprocess.Popen(
                list(SURVIVAL_NAV_CMD),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.msg("[CRISIS] Navigation (Organic Maps)", C_SUCCESS)
            self._term_add("[CRISIS] launched Organic Maps (offline nav)", raw=True)
        except FileNotFoundError:
            self.msg("[CRISIS] flatpak / Organic Maps not installed", C_ERROR)
        except Exception as e:
            self.msg(f"[CRISIS] launch failed: {e}", C_ERROR)

    def _survival_status(self):
        """Crisis hub: probe local offline services (non-blocking) and report."""
        import threading
        threading.Thread(target=self._survival_status_worker, daemon=True).start()
        self.msg("[CRISIS] Checking offline resources...", C_HACK_CYAN)

    def _survival_status_worker(self):
        import urllib.request
        from .config import SURVIVAL_WIKI_URL, SURVIVAL_CTOS_MAP_URL
        self._term_add("[CRISIS] Offline resources:", raw=True)
        for label, url in (("Wikipedia/Kiwix", SURVIVAL_WIKI_URL),
                           ("ctOS Map", SURVIVAL_CTOS_MAP_URL)):
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    ok = 200 <= r.getcode() < 400
                self._term_add(f"  [{'UP  ' if ok else '??  '}] {label}: {url}",
                               raw=True)
            except Exception:
                self._term_add(f"  [DOWN] {label}: {url}", raw=True)

    def _execute_item(self, cmd: str, state_key: str, name: str,
                      field_values: list):
        if state_key == "_stop_all":
            self._request_esp_stop()
            self._clear_esp_scan_state(cancel_pending=True)
            self.wifi_scanning = False
            self.ble_scanning = False
            self._wifi_scan_only = False
            self._ble_scan_only = False
            self._bt_tracking = False
            self._bt_airtag = False
            self._airtag_count = 0
            self._smarttag_count = 0
            self.sniffing = False
            self.capturing_hs = False
            self._wifi_scan_done_time = 0.0
            self._bt_scan_done_time = 0.0
            self._gps_wait = False
            self._gps_wait_cmd = ""
            # Stop Python-native attacks
            if self._dragon_drain.running:
                self._dragon_drain.stop()
            if self._mitm.running:
                self._mitm.stop()
            if self._blueducky.running:
                self._blueducky.stop()
            if self._race.running:
                self._race.stop()
            if self.state.portal_running:
                self.state.reset_portal()
            if self.state.evil_twin_running:
                self.state.reset_evil_twin()
            self._et_scan_pending = False
            self._attack_mode = ""
            self._attack_step = ""
            self._attack_scan_results = []
            self.msg("[STOP] All operations stopped", C_WARNING)
            self.glitch_timer = 3
            return
        if state_key == "_reboot":
            self._send("reboot")
            self.msg("[SYS] Reboot command sent", C_DIM)
            return
        if state_key == "_dl_map":
            if self._map_downloading:
                self._map_download_cancel = True
                self.msg("[MAP] Cancelling download...", C_WARNING)
            else:
                self._start_map_download()
            return
        if state_key == "_srv_wiki":
            from .config import SURVIVAL_WIKI_URL
            self._survival_open(SURVIVAL_WIKI_URL, "Wikipedia offline")
            return
        if state_key == "_srv_ctos":
            from .config import SURVIVAL_CTOS_MAP_URL
            self._survival_open(SURVIVAL_CTOS_MAP_URL, "ctOS Map")
            return
        if state_key == "_srv_nav":
            self._survival_nav()
            return
        if state_key == "_srv_status":
            self._survival_status()
            return
        if state_key == "_bt_hid_wip":
            self.msg("[HID] BLE HID disabled — work in progress", C_WARNING)
            self.msg("[HID] Feature will return in a future update", C_DIM)
            return
        if state_key == "_bd_wip":
            self.msg("[BD] BlueDucky disabled — work in progress", C_WARNING)
            self.msg("[BD] Feature will return in a future update", C_DIM)
            return
        if state_key == "_race_wip":
            self.msg("[RACE] RACE Attack disabled — work in progress", C_WARNING)
            self.msg("[RACE] Feature will return in a future update", C_DIM)
            return
        if state_key == "_gps_toggle":
            self._toggle_gps()
            return
        if state_key == "_lora_toggle":
            self._toggle_lora()
            return
        if state_key == "_sdr_toggle":
            self._toggle_sdr()
            return
        if state_key == "_sdr_adsb":
            self._sdr_adsb()
            return
        if state_key == "_sdr_433":
            self._sdr_433()
            return
        if state_key == "_sdr_emergency":
            self._sdr_emergency()
            return
        if state_key == "_subghz":
            self._subghz_open_screen()
            return
        if state_key == "_mute_toggle":
            self._toggle_mute()
            return
        if state_key == "_prox_toggle":
            self._toggle_proximity()
            return
        if state_key == "_prox_view":
            self._prox_screen = True
            return
        if state_key == "_watch":
            self._watch_connect()
            return
        if state_key == "_usb_toggle":
            self._toggle_usb()
            return
        if state_key == "_wl_screen":
            self._wl_screen = True
            self._wl_sel = 0
            self._wl_add_step = ""
            return
        if state_key == "_wpasec_up":
            self._wpasec_upload()
            return
        if state_key == "_wpasec_dl":
            self._wpasec_download()
            return
        if state_key == "_flash_esp":
            self._start_flash_esp32()
            return
        if state_key == "_gameupd":
            self._start_game_update()
            return
        # Plugin commands: format is "_p_<idx>_<action>"
        if state_key and state_key.startswith("_p_"):
            rest = state_key[3:]
            idx_str, _, action = rest.partition("_")
            try:
                p_idx = int(idx_str)
            except ValueError:
                return
            if 0 <= p_idx < len(self._plugins):
                p = self._plugins[p_idx]
                try:
                    getattr(p, action, lambda: None)()
                except Exception as exc:
                    log.exception("Plugin dispatch error")
                    self.msg(f"[PLUGIN] Error: {exc}", C_ERROR)
            return
        if cmd.startswith("_"):
            self._handle_python_attack(cmd, state_key, name, field_values)
            return
        if state_key in ("wardriving", "bt_scanning") and hasattr(self, "_wardrive_selected"):
            self._toggle_wardrive(state_key)
            return
        if not self._esp32:
            # Try reconnect before giving up
            if self._try_reconnect_esp32():
                self.msg("[SYS] ESP32 reconnected!", C_SUCCESS)
            else:
                self.msg(f"[ERR] No ESP32 — {name}", C_ERROR)
                return

        # GPS fix check — only wardriving modes (SNIFF tab) need GPS for loot
        _is_wardrive = state_key in ("wardriving", "bt_scanning")
        running = self._is_running(state_key)
        if _is_wardrive and not running and not self.gps_fix:
            # No GPS fix — show wait/cancel dialog
            final_cmd = (cmd + " " + " ".join(v for v in field_values if v)
                         if field_values else cmd)
            self._gps_wait_cmd = final_cmd
            self._gps_wait_state = state_key
            self._gps_wait_name = name
            self._gps_wait_dialog = True
            return

        # Build final command with optional params
        final_cmd = (cmd + " " + " ".join(v for v in field_values if v)
                     if field_values else cmd)
        if running:
            if self._request_esp_stop():
                self.msg(f"[STOP] {name}", C_DIM)
                self._clear_esp_scan_state(cancel_pending=True)
            else:
                self.msg(f"[ERR] Could not stop {name}", C_ERROR)
        else:
            self._start_scan_cmd(final_cmd, state_key, name)

    def _anything_running_on_esp(self) -> bool:
        """True if any ESP32 operation is currently active."""
        return (self.wifi_scanning or self.ble_scanning
                or self._wifi_scan_only or self._ble_scan_only
                or self._bt_tracking or self._bt_airtag
                or self.sniffing or self.capturing_hs
                or self.state.portal_running
                or self.state.evil_twin_running)

    def _start_scan_cmd(self, final_cmd: str, state_key: str, name: str):
        """Start an ESP32 command. Send stop first only if something is running."""
        scheduled = state_key in ("wardriving", "bt_scanning")
        if not scheduled and getattr(self, "_wardrive_selected", None):
            self._cancel_wardrive_plan()
        if self._esp_stop_waiting and self._esp_stop_timed_out:
            self.msg("[ERR] ESP32 stop is unconfirmed; check its connection", C_ERROR)
            return False
        if (self._esp_stop_waiting or getattr(self, "_esp_stop_required", False)
                or self._anything_running_on_esp()):
            if not self._request_esp_stop(cancel_pending=False, wardrive=scheduled):
                self.msg("[ERR] Could not stop the current ESP32 operation", C_ERROR)
                return False
            self._pending_cmd = final_cmd
            self._pending_cmd_name = name
            self._pending_cmd_state = state_key
            self.msg(f"[INIT] Stopping ESP32 before {name}...", C_DIM)
            return True
        return self._begin_esp_scan(final_cmd, state_key, name)

    def _request_esp_stop(self, *, cancel_pending=True, wardrive=False):
        """Keep the stop barrier even if the player has not queued a next scan."""
        if not wardrive and getattr(self, "_wardrive_selected", None):
            self._cancel_wardrive_plan()
        if self._esp_stop_waiting:
            if cancel_pending:
                self._clear_esp_scan_state(cancel_pending=True)
            return True
        if not self._send("stop", wardrive=True):
            self._esp_stop_required = True
            return False
        self._clear_esp_scan_state(cancel_pending=cancel_pending)
        self._esp_stop_waiting = True
        self._esp_stop_required = False
        self._esp_stop_timed_out = False
        self._esp_stop_ack = False
        self._esp_stop_requested_at = time.monotonic()
        self._pending_cmd_frame = pyxel.frame_count + 15  # ~500ms minimum
        return True

    def _clear_esp_scan_state(self, *, cancel_pending=False):
        """The ESP32 has one operation owner; old rescan timers cannot survive it."""
        for key in ("wifi_scanning", "ble_scanning", "_wifi_scan_only", "_ble_scan_only",
                    "_bt_tracking", "_bt_airtag", "sniffing", "capturing_hs"):
            setattr(self, key, False)
        self._wifi_scan_done_time = self._bt_scan_done_time = 0.0
        self._wifi_scan_start_time = self._bt_scan_start_time = 0.0
        if cancel_pending:
            self._pending_cmd = None
            self._pending_cmd_state = self._pending_cmd_name = ""

    def _begin_esp_scan(self, final_cmd, state_key, name):
        scheduled = state_key in getattr(self, "_wardrive_selected", ())
        if not self._send(final_cmd, wardrive=scheduled):
            self.msg(f"[ERR] Could not start {name}", C_ERROR)
            return False
        self._clear_esp_scan_state()
        self._set_running(state_key, True)
        if state_key in ("wardriving", "wifi_scan"):
            self._wifi_scan_start_time = time.time()
        if state_key in ("bt_scanning", "ble_scan"):
            self._bt_scan_start_time = time.time()
        if scheduled:
            self._wardrive_phase_started(state_key)
        self.msg(f"[START] {name}...", C_HACK_CYAN)
        self.glitch_timer = 5
        return True

    def _dispatch_pending_esp_command(self):
        if not self._esp_stop_waiting:
            return
        if (time.monotonic() - self._esp_stop_requested_at >= 5.0
                and not self._esp_stop_timed_out):
            self._clear_esp_scan_state(cancel_pending=True)
            self._esp_stop_timed_out = True
            if getattr(self, "_wardrive_selected", None):
                self._wardrive_pause("ESP32 stop was not confirmed")
            message = "[ERR] ESP32 stop was not confirmed; new operation cancelled"
            self.msg(message, C_ERROR)
            self._term_add(message, raw=True)
        if not self._esp_stop_ack or pyxel.frame_count < self._pending_cmd_frame:
            return
        cmd, state, name = self._pending_cmd, self._pending_cmd_state, self._pending_cmd_name
        self._pending_cmd = None
        self._pending_cmd_state = self._pending_cmd_name = ""
        self._esp_stop_waiting = False
        self._esp_stop_timed_out = False
        self._esp_stop_ack = False
        self._esp_stop_requested_at = 0.0
        if cmd:
            self._begin_esp_scan(cmd, state, name)

    def _is_running(self, state_key: str) -> bool:
        if state_key in ("wardriving", "bt_scanning") and hasattr(self, "_wardrive_selected"):
            return state_key in self._wardrive_selected
        return {
            "wardriving":    self.wifi_scanning,
            "bt_scanning":   self.ble_scanning,
            "wifi_scan":     self._wifi_scan_only,
            "ble_scan":      self._ble_scan_only,
            "bt_tracking":   self._bt_tracking,
            "bt_airtag":     self._bt_airtag,
            "sniffer":       self.sniffing,
            "handshake":     self.capturing_hs,
            "mitm":          self._mitm.running,
            "dragon_drain":  self._dragon_drain.running,
            "blueducky":     self._blueducky.running or self._blueducky.connected,
            "race":          self._race.running,
            "portal":        self.state.portal_running,
            "evil_twin":     self.state.evil_twin_running,
            "meshcore":      self._lora.running and self._lora.ready and self._lora.mode == "meshcore",
            "meshtastic":    self._mt.running and self._mt.ready,
            "_sdr_adsb":     self._sdr.running and "adsb" in self._sdr.mode,
            "_sdr_433":      self._sdr.running and "433" in self._sdr.mode,
            "_sdr_emergency": self._sdr.running and "emerg" in self._sdr.mode,
            "_subghz":        self._subghz.connected,
            "_mute_toggle":   getattr(self, "_muted", False),
            "_prox_toggle":   self._prox.enabled,
            "_watch":        self._watch.connected,
            "_gps_toggle":   self._gps_enabled,
            "_lora_toggle":  bool(self._active_lora_protocol()),
            "_sdr_toggle":   self._sdr_enabled,
            "_usb_toggle":   self._usb_enabled,
        }.get(state_key, False)

    def _set_running(self, state_key: str, val: bool):
        if state_key == "wardriving":
            self.wifi_scanning = val
            if val:
                self._earn_badge("wardriver")
        elif state_key == "bt_scanning":
            self.ble_scanning = val
            if val:
                self._earn_badge("wardriver")
        elif state_key == "wifi_scan":    self._wifi_scan_only = val
        elif state_key == "ble_scan":     self._ble_scan_only  = val
        elif state_key == "bt_tracking":  self._bt_tracking    = val
        elif state_key == "bt_airtag":    self._bt_airtag      = val
        elif state_key == "sniffer":      self.sniffing        = val
        elif state_key == "handshake":    self.capturing_hs    = val

    def _wardrive_notice(self, text, color=C_DIM):
        self._term_add("[WARD] " + text, raw=True)
        self.msg("[WARD] " + text, color)

    def _wardrive_next_key(self):
        selected = self._wardrive_selected
        previous = self._wardrive_phase or self._wardrive_last_phase
        if len(selected) == 2 and previous:
            return "bt_scanning" if previous == "wardriving" else "wardriving"
        return "wardriving" if "wardriving" in selected else "bt_scanning"

    def _cancel_wardrive_plan(self, *, clear_active=False):
        if not hasattr(self, "_wardrive_selected"):
            return
        self._wardrive_selected.clear()
        self._wardrive_phase = self._wardrive_last_phase = ""
        self._wardrive_phase_done = 0.0
        self._wardrive_paused = self._wardrive_save_failed = False
        self._wardrive_save_errors_at_start = None
        self._gps_wait = self._gps_wait_dialog = False
        self._gps_wait_cmd = self._gps_wait_state = self._gps_wait_name = ""
        if self._pending_cmd_state in ("wardriving", "bt_scanning"):
            self._pending_cmd = None
            self._pending_cmd_state = self._pending_cmd_name = ""
        if clear_active:
            self.wifi_scanning = self.ble_scanning = False
            self._wifi_scan_start_time = self._bt_scan_start_time = 0.0
            self._wifi_scan_done_time = self._bt_scan_done_time = 0.0

    def _wardrive_pause(self, reason, *, request_stop=True):
        if not getattr(self, "_wardrive_selected", None):
            return
        if (request_stop and not getattr(self, "_esp_stop_required", False)
                and self._anything_running_on_esp()):
            self._request_esp_stop(wardrive=True)
        self._clear_esp_scan_state(cancel_pending=True)
        self._wardrive_phase = ""
        self._wardrive_phase_done = 0.0
        self._wardrive_paused = True
        self._gps_wait = self._gps_wait_dialog = False
        self._gps_wait_cmd = ""
        self._wardrive_notice(reason + "; wardrive paused. Toggle OFF/ON to retry", C_ERROR)

    def _toggle_wardrive(self, key):
        label = "WiFi" if key == "wardriving" else "BLE"
        if key in self._wardrive_selected:
            self._wardrive_selected.remove(key)
            if not self._wardrive_selected:
                owned = bool(self._wardrive_phase or self._pending_cmd_state in ("wardriving", "bt_scanning"))
                self._cancel_wardrive_plan()
                if owned and not self._request_esp_stop(wardrive=True):
                    self._clear_esp_scan_state(cancel_pending=True)
                    self._wardrive_notice("Could not confirm ESP32 stop", C_ERROR)
                self._wardrive_notice("WiFi and BLE OFF")
                return
            if self._wardrive_phase == key:
                if self._wardrive_phase_done:
                    self._wardrive_notice(label + " OFF; finishing the scan pause")
                    self._wardrive_tick()
                    return
                if not self._wardrive_saved():
                    self._wardrive_pause("Scan data could not be saved")
                    return
                if not self._request_esp_stop(wardrive=True):
                    self._wardrive_pause("Could not stop the current phase")
                    return
                self._wardrive_last_phase, self._wardrive_phase = key, ""
                self._wardrive_phase_done = 0.0
            if self._pending_cmd_state == key:
                self._pending_cmd = None
                self._pending_cmd_state = self._pending_cmd_name = ""
            self._wardrive_notice(label + " OFF; remaining mode kept")
            self._wardrive_tick()
            return
        if not self._esp32 and not self._try_reconnect_esp32():
            self._wardrive_notice("No ESP32 connection", C_ERROR)
            return
        first = not self._wardrive_selected
        self._wardrive_selected.add(key)
        self._wardrive_paused = False
        if first and not self.gps_fix:
            self._gps_wait_cmd = "scan_networks" if key == "wardriving" else "scan_bt"
            self._gps_wait_state, self._gps_wait_name = key, label + " Wardrive"
            self._gps_wait_dialog = True
            self._wardrive_notice(label + " selected; GPS fix required")
            return
        if self._gps_wait or self._gps_wait_dialog:
            self._wardrive_notice(label + " selected; waiting for GPS fix")
            return
        self._wardrive_notice(label + (" queued after current phase" if self._wardrive_phase else " ON"))
        self._wardrive_tick()

    def _wardrive_phase_started(self, key):
        self._wardrive_phase = key
        self._wardrive_phase_done = 0.0
        self._wardrive_save_failed = False
        self._wardrive_save_errors_at_start = getattr(self.loot, "wardrive_save_errors", None)
        label = "WiFi" if key == "wardriving" else "BLE"
        queued = self._wardrive_next_key()
        suffix = ""
        if queued != key:
            suffix = "; " + ("WiFi" if queued == "wardriving" else "BLE") + " queued"
        self._wardrive_notice(label + " active" + suffix, C_HACK_CYAN)

    def _wardrive_saved(self):
        counter = getattr(self.loot, "wardrive_save_errors", None)
        return (getattr(self.loot, "active", False) and not self._wardrive_save_failed and counter is not None
                and counter == self._wardrive_save_errors_at_start)

    def _wardrive_complete(self, key):
        if getattr(self, "_wardrive_phase", "") != key:
            return False
        if self._wardrive_phase_done:
            return True
        if not self._wardrive_saved():
            self._wardrive_pause("Scan data could not be saved")
            return True
        self._wardrive_phase_done = time.monotonic()
        if key == "wardriving":
            self.wifi_scanning = False
            self._wifi_scan_start_time = 0.0
        else:
            self.ble_scanning = False
            self._bt_scan_start_time = 0.0
        label = "WiFi" if key == "wardriving" else "BLE"
        self._wardrive_notice(label + " complete and saved; next phase in 2 s", C_SUCCESS)
        return True

    def _wardrive_tick(self):
        if (not self._wardrive_selected or self._wardrive_paused or self._gps_wait
                or self._gps_wait_dialog or self._pending_cmd):
            return
        if not getattr(self.loot, "active", False):
            self._wardrive_pause("Loot storage is unavailable")
            return
        phase = self._wardrive_phase
        if phase:
            if not self._wardrive_phase_done or time.monotonic() - self._wardrive_phase_done < self._SCAN_INTERVAL:
                return
            if not self._wardrive_saved():
                self._wardrive_pause("Scan data could not be saved")
                return
            next_key = self._wardrive_next_key()
            if next_key != phase and not self._request_esp_stop(wardrive=True):
                self._wardrive_pause("Could not stop the current phase")
                return
            self._wardrive_last_phase, self._wardrive_phase = phase, ""
            self._wardrive_phase_done = 0.0
        else:
            next_key = self._wardrive_next_key()
        command = "scan_networks" if next_key == "wardriving" else "scan_bt"
        label = "WiFi Wardrive" if next_key == "wardriving" else "BLE Wardrive"
        if not self._start_scan_cmd(command, next_key, label):
            self._wardrive_pause("Could not start " + label)

    # ------------------------------------------------------------------
    # Python-native attacks (Dragon Drain, MITM, BlueDucky, RACE)
    # ------------------------------------------------------------------

    def _handle_python_attack(self, cmd: str, state_key: str, name: str,
                              field_values: list | None):
        """Handle Python-native attacks with full scan→select→run flow."""

        # ── Dragon Drain ──
        if cmd == "_dragon_drain":
            if self._dragon_drain.running:
                self._dragon_drain.stop()
                self._attack_mode = ""
                return
            # Step 1: detect monitor interface
            mon = self._dragon_drain.detect_monitor_ifaces()
            if not mon:
                managed = self._dragon_drain.detect_managed_ifaces()
                if managed:
                    self.msg(f"[DD] Enabling monitor on {managed[0][0]}...", C_WARNING)
                    self._dragon_drain.enable_monitor(managed[0][0])
                    mon = self._dragon_drain.detect_monitor_ifaces()
                if not mon:
                    self.msg("[DD] No monitor iface — plug in WiFi adapter", C_ERROR)
                    return
            # Step 2: scan for WPA3 APs in background
            self._attack_mode = "dragon_drain"
            self._attack_step = "scanning"
            self.msg("[DD] Scanning for WPA3 APs (10s)...", C_HACK_CYAN)

            def _scan():
                iface = mon[0]
                aps = self._dragon_drain.scan_wpa3(iface, duration=10)
                self._attack_scan_results = aps
                if aps:
                    self._attack_step = "select"
                    self.msg(f"[DD] {len(aps)} WPA3 AP(s) — press [1-9] to select", C_TEXT)
                    for i, ap in enumerate(aps[:9]):
                        s = ap['ssid'] or '<hidden>'
                        self._term_add(
                            f"  {i+1}. {ap['bssid']}  CH:{ap['channel']}  "
                            f"{ap['rssi']}dBm  {s}", raw=True)
                else:
                    self.msg("[DD] No WPA3 APs found", C_WARNING)
                    self._attack_mode = ""

            threading.Thread(target=_scan, daemon=True).start()
            return

        # ── MITM ── (dedicated sub-screen)
        if cmd == "_mitm":
            if self._mitm.running:
                self._mitm.stop()
                self._attack_mode = ""
                return
            self._mitm_screen = True
            self._mitm_state = "idle"
            self._mitm_log = []
            self.menu_open = False
            return

        # ── BlueDucky ──
        if cmd == "_blueducky":
            if self._blueducky.running or self._blueducky.connected:
                self._blueducky.stop()
                self._attack_mode = ""
                return
            self._attack_mode = "blueducky"
            self._attack_step = "menu"
            self.msg("[BD] [s]Scan [c]Connect(MAC) [r]RickRoll [x]Stop", C_HACK_CYAN)
            return

        # ── RACE Attack ──
        if cmd == "_race":
            if self._race.running:
                self._race.stop()
                self._attack_mode = ""
                return
            self._attack_mode = "race"
            self._attack_step = "menu"
            self.msg("[RACE] [s]Scan [c]Check [e]Extract [h]Hijack [l]Listen [x]Stop", C_HACK_CYAN)
            return

        # ── Evil Portal ── (standalone captive portal)
        if cmd == "_evil_portal":
            if not self._esp32:
                if not self._try_reconnect_esp32():
                    self.msg("[EP] No ESP32 — plug in device", C_ERROR)
                    return
            self._attack_mode = "evil_portal"
            self._attack_step = "input_ssid"
            self._portal_ssid = ""
            self.input_fields = [{"label": "Portal SSID", "value": "", "pos": 0}]
            self.input_field_idx = 0
            self.input_mode = True
            self._input_pending_cat = -7   # marker for Evil Portal SSID
            self._input_pending_item = -1
            return

        # ── Evil Twin ── (spoof existing network)
        if cmd == "_evil_twin":
            if not self._esp32:
                if not self._try_reconnect_esp32():
                    self.msg("[ET] No ESP32 — plug in device", C_ERROR)
                    return
            self._attack_mode = "evil_twin"
            # Check if we have fresh scan results (<30s)
            nets = self.state.networks
            if nets and (time.time() - self._wifi_scan_done_time) < 30:
                self._attack_scan_results = list(nets)
                self._show_net_selection()
            else:
                # Need to scan first
                self._attack_step = "scanning"
                self._et_scan_pending = True
                self.state.networks.clear()
                self._send("scan_networks")
                self.msg("[ET] Scanning for networks...", C_HACK_CYAN)
            return

        # ── MeshCore Messenger ──
        if cmd == "_meshcore":
            if self._lora_enabled and not self._switch_lora_protocol("meshcore"):
                return
            aio_lora = self._lora_enabled and self._lora.running
            watch_lora = self._watch.connected
            if not aio_lora and not watch_lora:
                self.msg("[MC] Enable LoRa or connect PipBoy Watch", C_WARNING)
                return
            if watch_lora and not aio_lora:
                # Start watch LoRa if not AIO
                self._watch.send_command("lora_start")
                self._watch_log_add("[Watch] LoRa started for MeshCore", C_HACK_CYAN)
            self._mc_screen = True
            self._mt_screen = False
            self._mc_scroll = 0
            self.menu_open = False
            # Show keyboard shortcuts on first open of this session
            if not getattr(self, "_mc_help_shown", False):
                self._mc_log.append(
                    (f"\x11 Your node: {self._mc_node_name}", C_HACK_CYAN))
                self._mc_log.append(
                    ("\x11 Shortcuts: Ctrl+N=name  Ctrl+A=advert  "
                     "Ctrl+C=channel  Ctrl+X=clear", 13))
                self._mc_help_shown = True
            return

        if cmd == "_meshtastic":
            if not self._lora_enabled:
                self.msg("[MT] Enable LoRa first (SYSTEM > LoRa)", C_WARNING)
                return
            if not self._switch_lora_protocol("meshtastic"):
                return
            self._mt_screen = True
            self._mc_screen = False
            self._mt_view["scroll"] = 0
            self.menu_open = False
            if not getattr(self, "_mt_help_shown", False):
                self._mt_view["log"].append(("\x11 C-N Name  C-H Nodes  C-X Clear  ESC Back", C_DIM))
                self._mt_help_shown = True
            return

        if cmd == "_meshtastic_profile":
            profiles = (self._rf_profiles.choices("meshtastic")
                        if getattr(self, "_rf_profiles", None) else ("sfnarrow", "longfast"))
            self._mt_profile_sel = profiles.index(self._mt_profile) if self._mt_profile in profiles else 0
            self._rf_confirm = None
            self._mt_profile_screen = True
            self.menu_open = False
            return

        # ── WDGWars Reticulum/LXMF chat ──
        if cmd == "_wdgwars":
            if self._wd is None:
                self.msg("[WD] Reticulum missing — pip install rns lxmf",
                         C_WARNING)
                return
            self._wd_screen = True
            self._wd_scroll = 0
            self.menu_open = False
            if not self._wd_started:
                self._wd.nick = self._mc_node_name
                self._wd.start()
                self._wd_started = True
                self._wd_log.append(
                    ("\x11 Connecting to WDGWars hub over TCP...",
                     C_HACK_CYAN))
                self._wd_log.append(
                    (f"\x11 You: {self._mc_node_name}  —  #general", 13))
                self._wd_log.append(
                    ("\x11 Send a message to join the fan-out, then you "
                     "receive broadcasts.", C_DIM))
            return

        # ── MeshCore Region picker ──
        if cmd == "_meshcore_region":
            from .lora_manager import MESHCORE_PRESETS
            keys = (self._rf_profiles.choices("meshcore")
                    if getattr(self, "_rf_profiles", None) else list(MESHCORE_PRESETS.keys()))
            try:
                self._mc_region_sel = keys.index(self._mc_region)
            except ValueError:
                self._mc_region_sel = 0
            self._mc_region_screen = True
            self._rf_confirm = None
            self.menu_open = False
            return

        if cmd == "_flipper":
            if not self._flipper.connected:
                self._flipper_log = []
                self.msg("[FLIP] Connecting...", C_DIM)
                if not self._flipper.connect():
                    self.msg("[FLIP] Not found — plug in Flipper Zero USB",
                             C_ERROR)
                    return
                self.msg(f"[FLIP] {self._flipper.device_name} "
                         f"({self._flipper.firmware})", C_SUCCESS)
                self._flipper_log.append(
                    (f"Connected: {self._flipper.device_name} "
                     f"({self._flipper.firmware})", C_SUCCESS))
                self._flipper_log.append(
                    (f"Region: {self._flipper.region}", C_DIM))
            self._flipper_screen = True
            self._flipper_scroll = 0
            self._flipper_mode = "idle"
            # Start selection on first actionable item (skip separators)
            self._flipper_sel = 0
            for i, (_, act) in enumerate(self._FLIPPER_MENU):
                if act is not None:
                    self._flipper_sel = i
                    break
            self.menu_open = False
            return

        self.msg(f"[N/A] {name}: not yet implemented", C_DIM)

    def _handle_attack_key(self, key: int) -> bool:
        """Handle keypresses for active Python-native attack flows.
        Returns True if key was consumed."""
        import pyxel as px

        if not self._attack_mode:
            return False

        # ── Dragon Drain: select AP ──
        if self._attack_mode == "dragon_drain" and self._attack_step == "select":
            if px.KEY_1 <= key <= px.KEY_9:
                idx = key - px.KEY_1
                aps = self._attack_scan_results
                if idx < len(aps):
                    ap = aps[idx]
                    if self._whitelist.is_blocked(ap['bssid']):
                        self.msg(f"[WL] {ap['bssid'][-8:]} whitelisted", C_WARNING)
                        return True
                    mon = self._dragon_drain.detect_monitor_ifaces()
                    iface = mon[0] if mon else ""
                    self._dragon_drain.start(ap['bssid'], iface)
                    self._attack_step = "running"
                    self.msg(f"[DD] Attacking {ap['bssid']} CH:{ap['channel']}", C_WARNING)
                return True
            if key == px.KEY_ESCAPE:
                self._attack_mode = ""
                self._esc_consumed_frame = pyxel.frame_count
                self.msg("[DD] Cancelled", C_DIM)
                return True

        # ── MITM: now handled by dedicated _mitm_screen ──

        # ── BlueDucky menu ──
        if self._attack_mode == "blueducky":
            if key == px.KEY_S and not self._blueducky.running:
                self._blueducky.scan()
                self._attack_step = "scan_done"
                return True
            if key == px.KEY_C:
                # MAC input
                self.input_mode = True
                self.input_fields = [{"label": "Target BT MAC", "value": "", "pos": 0}]
                self.input_field_idx = 0
                self._input_pending_cat = -2  # marker for BlueDucky
                self._input_pending_item = -1
                return True
            if key == px.KEY_R:
                if self._blueducky.connected:
                    from .bt_ducky import RICKROLL_PAYLOAD
                    self._blueducky.execute_payload(RICKROLL_PAYLOAD, "Rick Roll")
                else:
                    self.msg("[BD] Connect first, then [r]", C_WARNING)
                return True
            if key == px.KEY_X:
                self._blueducky.stop()
                self._attack_mode = ""
                return True
            # Quick-select from scan (1-9)
            if px.KEY_1 <= key <= px.KEY_9 and self._blueducky.scanned_devices:
                idx = key - px.KEY_1
                devs = self._blueducky.scanned_devices
                if idx < len(devs):
                    addr, name = devs[idx]
                    if self._whitelist.is_blocked(addr):
                        self.msg(f"[WL] {name} whitelisted", C_WARNING)
                        return True
                    self._blueducky.connect(addr, name)
                return True
            if key == px.KEY_ESCAPE:
                self._blueducky.stop()
                self._attack_mode = ""
                self._esc_consumed_frame = pyxel.frame_count
                return True

        # ── RACE menu ──
        if self._attack_mode == "race":
            if key == px.KEY_S and not self._race.running:
                self._race.scan()
                return True
            if key == px.KEY_C:
                if self._race.scanned:
                    self._attack_step = "select_race"
                    self.msg("[RACE] Select device [1-9]:", C_HACK_CYAN)
                elif self._race.target_addr:
                    self._race.check(self._race.target_addr, self._race.target_name)
                else:
                    self.msg("[RACE] Scan first [s]", C_WARNING)
                return True
            if key == px.KEY_E:
                self._race.extract()
                return True
            if key == px.KEY_H:
                self._race.hijack()
                return True
            if key == px.KEY_L:
                self._race.listen()
                return True
            if key == px.KEY_X:
                self._race.stop()
                self._attack_mode = ""
                return True
            # Quick-select from scan (1-9)
            if px.KEY_1 <= key <= px.KEY_9 and self._race.scanned:
                idx = key - px.KEY_1
                if idx < len(self._race.scanned):
                    d = self._race.scanned[idx]
                    if self._whitelist.is_blocked(d["addr"]):
                        self.msg(f"[WL] {d.get('name','')} whitelisted", C_WARNING)
                        return True
                    self._race.check(d["addr"], d.get("name", ""))
                return True
            if key == px.KEY_ESCAPE:
                self._race.stop()
                self._attack_mode = ""
                self._esc_consumed_frame = pyxel.frame_count
                return True

        # ── Evil Portal / Evil Twin: running → stop / show data ──
        if (self._attack_mode in ("evil_portal", "evil_twin")
                and self._attack_step == "running"):
            if key == px.KEY_D:
                # Toggle captured data overlay
                self._portal_data_screen = not getattr(self, '_portal_data_screen', False)
                self._portal_data_scroll = 0
                return True
            if key == px.KEY_X or key == px.KEY_ESCAPE:
                self._send("stop")
                tag = "EP" if self._attack_mode == "evil_portal" else "ET"
                self.msg(f"[{tag}] Stopped", C_DIM)
                if self.state.portal_running:
                    self.state.reset_portal()
                if self.state.evil_twin_running:
                    self.state.reset_evil_twin()
                self._attack_mode = ""
                self._attack_step = ""
                self._portal_data_screen = False
                if key == px.KEY_ESCAPE:
                    self._esc_consumed_frame = pyxel.frame_count
                return True
            if key == px.KEY_UP and getattr(self, '_portal_data_screen', False):
                self._portal_data_scroll = max(0, self._portal_data_scroll - 1)
                return True
            if key == px.KEY_DOWN and getattr(self, '_portal_data_screen', False):
                self._portal_data_scroll += 1
                return True

        return False

    def _show_portal_selection(self, tag: str):
        """Show portal picker overlay. Tag = 'EP' or 'ET'."""
        loot_dir = Path(self._app_dir) / "loot"
        self._portal_list = get_all_portals(loot_dir)
        self._portal_sel = 0
        self._portal_select_screen = True
        self._attack_step = "select_portal"

    def _show_net_selection(self):
        """Show network picker overlay for Evil Twin."""
        self._et_net_sel = 0
        self._et_net_selected.clear()
        self._et_net_screen = True
        self._attack_step = "select_net"

    @staticmethod
    def _parse_post_fields(line: str) -> str:
        """Parse 'Received POST data: k=v&k2=v2' into formatted string.

        Returns e.g. 'Email: user@gmail.com | Password: secret123'
        or empty string if no recognized fields.
        """
        m = re.search(r'[Pp]ost data:\s*(.+)$', line)
        if not m:
            return ""
        try:
            from urllib.parse import parse_qs
            fields = {k: v[0] for k, v in parse_qs(m.group(1)).items()}
        except Exception:
            return ""
        parts = []
        for key in ("username", "email", "login", "user"):
            if key in fields:
                parts.append(f"{key.title()}: {fields[key]}")
        if "password" in fields:
            parts.append(f"Password: {fields['password']}")
        # Any other fields not already shown
        for k, v in fields.items():
            if k not in ("username", "email", "login", "user", "password"):
                parts.append(f"{k}: {v}")
        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Map tile download
    # ------------------------------------------------------------------

    def _start_map_download(self):
        """Download OSM tiles around current GPS position (or manual pos)."""
        # Preflight: Stadia returns HTTP 401 for every request without an
        # API key, so without this guard the user just sees hundreds of
        # `HTTP 401 Unauthorized` lines in the terminal and no map. Bail
        # early with a clear pointer to the secrets.conf line + signup URL.
        from .tile_manager import _load_stadia_key
        if not _load_stadia_key():
            self.msg("[MAP] No Stadia API key — add STADIA_API_KEY to secrets.conf",
                     C_WARNING)
            self._term_add(
                "[MAP] Free signup: https://stadiamaps.com", raw=True)
            return
        if not self.gps_fix:
            self.msg("[MAP] No GPS fix — using current view center", C_WARNING)
        lat = self.player_lat
        lon = self.player_lon
        self._map_downloading = True
        self._map_download_cancel = False
        self.msg(f"[MAP] Downloading tiles ({lat:.2f}, {lon:.2f})...", C_HACK_CYAN)
        self._term_add(f"[MAP] Downloading 10km area around ({lat:.4f}, {lon:.4f})", raw=True)
        self._term_add("[MAP] Press [m] in SYSTEM to cancel", raw=True)

        def _dl_thread():
            maps_dir = Path(self._app_dir) / "maps"
            try:
                def cb(pct, msg):
                    if self._map_download_cancel:
                        raise InterruptedError("cancelled")
                    if pct % 10 < 1 or pct >= 99:
                        self._term_add(f"[MAP] {pct:.0f}% — {msg}", raw=True)

                manifest = download_tiles(lat, lon, maps_dir,
                                          radius_km=10.0, callback=cb)
                # Reload renderer
                if self.tile_renderer is None:
                    self.tile_renderer = TileRenderer(maps_dir)
                else:
                    self.tile_renderer.reload_manifest()
                self._term_add(
                    f"[MAP] Done! {manifest['tile_count']} tiles "
                    f"({manifest['errors']} errors)", raw=True)
                self.msg("[MAP] Tiles ready! Zoom in to see streets", C_SUCCESS)
            except InterruptedError:
                self._term_add("[MAP] Download cancelled by user", raw=True)
                self.msg("[MAP] Download cancelled", C_WARNING)
                # Reload whatever was downloaded so far
                if self.tile_renderer:
                    self.tile_renderer.reload_manifest()
            except Exception as e:
                self._term_add(f"[MAP] Error: {e}", raw=True)
                self.msg("[MAP] Download failed", C_ERROR)
            finally:
                self._map_downloading = False
                self._map_download_cancel = False

        threading.Thread(target=_dl_thread, daemon=True).start()

    # ------------------------------------------------------------------
    # GPS / LoRa toggle (AIO v2 GPIO or software)
    # ------------------------------------------------------------------

    def _toggle_gps(self):
        new_state = not self._gps_enabled
        if self._aio_available:
            ok = self._aio_power.toggle("gps", new_state)
            if not ok:
                self.msg("[GPS] GPIO toggle failed", C_ERROR)
                return
        if new_state:
            # Enable GPS — re-open serial
            if not self.gps.available:
                self.gps.setup()
            if self.gps.available:
                self._gps_enabled = True
                self.msg("[GPS] ON", C_SUCCESS)
                self._term_add("[SYS] GPS enabled", raw=True)
            else:
                self.msg("[GPS] No device found", C_ERROR)
                self._gps_enabled = False
        else:
            # Disable GPS — close serial, clear fix
            self.gps.close()
            self._gps_enabled = False
            self.gps_fix = False
            self.gps_sats = 0
            self.gps_sats_vis = 0
            self.msg("[GPS] OFF", C_WARNING)
            self._term_add("[SYS] GPS disabled", raw=True)
        self.glitch_timer = 2

    def _active_lora_protocol(self):
        if self._mt.running and self._mt.ready:
            return "meshtastic"
        if self._lora.running and getattr(self._lora, "ready", False):
            return self._lora.mode
        return ""

    def _stop_radio_owner(self):
        """Stop before retuning; a timed-out worker retains ownership."""
        if self._wd is not None and self._wd.lora_active:
            self._wd.disable_lora()
        if self._mt.running and not self._mt.stop():
            self.msg("[LoRa] Meshtastic stop pending; radio remains owned", C_ERROR)
            return False
        if self._lora.running and not self._lora.stop():
            self.msg("[LoRa] Radio stop pending; cannot switch", C_ERROR)
            return False
        self._radio_protocol = ""
        return True

    def _switch_lora_protocol(self, protocol):
        if getattr(self, "_shutting_down", False):
            return False
        if self._active_lora_protocol() == protocol:
            return True
        if not self._lora_enabled or not self._stop_radio_owner():
            return False
        try:
            controller = getattr(self, "_rf_profiles", None)
            target = controller.target(protocol) if controller is not None else None
            if protocol == "meshtastic":
                profile = target or self._mt_profile
                ok = self._mt.start(profile)
            else:
                profile = target or self._mc_region
                self._lora.set_mc_channels(self._mc_channels_list)
                self._lora.set_mc_active_channel(self._mc_active_ch)
                ok = self._lora.start_meshcore(profile)
            if not ok:
                self._radio_protocol = ""
                self.msg("[LoRa] Radio initialization failed", C_ERROR)
                return False
            if self._aio_available:
                self._aio_power.claim("lora")
            if protocol == "meshtastic":
                self._mt_profile = profile
            else:
                self._mc_region = profile
            if controller is not None:
                controller.applied(protocol, profile)
            self._radio_protocol = protocol
            self.state.lora_running = True
            self.state.lora_mode = protocol
            self.msg(f"[LoRa] {protocol} active", C_SUCCESS)
            if protocol == "meshcore":
                lat = self.player_lat if self.gps_fix else 0.0
                lon = self.player_lon if self.gps_fix else 0.0
                self._lora.send_meshcore_advert(self._mc_node_name, lat, lon)
            return True
        except Exception as exc:
            self._radio_protocol = ""
            self.msg(f"[LoRa] {exc}", C_ERROR)
            return False

    def _toggle_lora(self):
        if not self._lora_enabled:
            self._lora_protocol_sel = 1
            self._lora_protocol_picker = True
            self.menu_open = False
            return
        if not self._stop_radio_owner():
            return
        if self._aio_available and not self._aio_power.toggle("lora", False):
            self.msg("[LoRa] GPIO toggle failed", C_ERROR)
            return
        self._lora_enabled = False
        self.state.lora_running = False
        self.state.lora_mode = ""
        self._mc_screen = self._mt_screen = False
        self._mc_bubbles.clear()
        self.msg("[LoRa] OFF", C_WARNING)
        self.glitch_timer = 2

    def _update_lora_protocol_picker(self):
        if pyxel.btnp(pyxel.KEY_LEFT):
            self._lora_protocol_sel = 0
        elif pyxel.btnp(pyxel.KEY_RIGHT):
            self._lora_protocol_sel = 1
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            self._lora_protocol_picker = False
            self._esc_consumed_frame = pyxel.frame_count
        elif pyxel.btnp(pyxel.KEY_RETURN):
            was_powered = self._lora_enabled
            if not was_powered:
                if not self._aio_available or not self._aio_power.toggle("lora", True):
                    self.msg("[LoRa] GPIO toggle failed", C_ERROR)
                    return
                self._lora_enabled = True
            protocol = ("meshtastic", "meshcore")[self._lora_protocol_sel]
            self._lora_protocol_picker = False
            if not self._switch_lora_protocol(protocol) and not was_powered:
                if self._stop_radio_owner():
                    self._aio_power.toggle("lora", False)
                    self._lora_enabled = False

    def _draw_lora_protocol_picker(self):
        # Same frame, palette, row height and keyboard hints as the region picker.
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        dx, dy, dw, dh = 100, 120, 440, 90
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        pyxel.text(dx + 4, dy + 4, "LORA - Select protocol", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        for index, label in enumerate(("Meshtastic", "MeshCore")):
            x = dx + 12 + index * 212
            selected = self._lora_protocol_sel == index
            if selected:
                pyxel.rect(x, dy + 30, 200, 17, C_WARNING)
            pyxel.text(x + 8, dy + 35, label, 0 if selected else C_TEXT)
        pyxel.text(dx + 4, dy + dh - 12, "LEFT/RIGHT select   ENTER apply   ESC cancel", C_DIM)

    def _update_mt_profile_picker(self):
        if getattr(self, "_rf_profiles", None) is not None:
            self._update_radio_profile_picker("meshtastic")
            return
        profiles = ("sfnarrow", "longfast")
        if pyxel.btnp(pyxel.KEY_UP):
            self._mt_profile_sel = max(0, self._mt_profile_sel - 1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._mt_profile_sel = min(1, self._mt_profile_sel + 1)
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            self._mt_profile_screen = False
            self._esc_consumed_frame = pyxel.frame_count
        elif pyxel.btnp(pyxel.KEY_RETURN):
            selected = profiles[self._mt_profile_sel]
            if self._mt.running:
                if not self._mt.stop():
                    self.msg("[MT] Stop pending; profile unchanged", C_ERROR)
                    return
                if not self._mt.start(selected):
                    self.msg(f"[MT] {self._mt.last_error}", C_ERROR)
                    return
            elif not self._mt.set_profile(selected):
                self.msg(f"[MT] {self._mt.last_error}", C_ERROR)
                return
            self._mt_profile = selected
            self._mt_profile_screen = False

    def _draw_mt_profile_picker(self):
        if getattr(self, "_rf_profiles", None) is not None:
            self._draw_radio_profile_picker("meshtastic")
            return
        from meshtastic_rx_probe import get_profile
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        dx, dy, dw, dh = 100, 118, 440, 100
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        pyxel.text(dx + 4, dy + 4, "MESHTASTIC - Select profile (MANUAL)", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        for i, key in enumerate(("sfnarrow", "longfast")):
            cfg = get_profile(key)
            y = dy + 25 + i * 18
            selected = self._mt_profile_sel == i
            if selected:
                pyxel.rect(dx + 2, y - 1, dw - 4, 17, C_WARNING)
            label = f"{cfg['channel_name']} {cfg['frequency_hz']/1e6:.3f} SF{cfg['sf']} BW{cfg['bandwidth_hz']/1000:g} CR4/5"
            pyxel.text(dx + 6, y + 3, label, 0 if selected else C_TEXT)
        pyxel.text(dx + 4, dy + dh - 12, "UP/DOWN select   ENTER apply   ESC cancel", C_DIM)

    def _apply_radio_profile(self, protocol, key, notify=False):
        """Apply RF only; protocol, channels, chat and player data stay owned elsewhere."""
        controller = self._rf_profiles
        manager = self._mt if protocol == "meshtastic" else self._lora
        notices = self._rf_notices
        if manager.running and getattr(manager, "mode", protocol) != protocol:
            notices[protocol] = "Profile pending; radio belongs to another protocol"
            return False
        try:
            if not manager.retune_profile(key):
                notices[protocol] = "RF pending / error: " + str(manager.last_error or "radio busy")
                if notify:
                    self.msg("[RF] " + notices[protocol], C_WARNING)
                return False
            if protocol == "meshtastic":
                self._mt_profile = key
            else:
                self._mc_region = key
            controller.applied(protocol, key)
            notices[protocol] = "RF profile set; chat channel kept"
            if notify:
                self.msg("[RF] " + notices[protocol], C_SUCCESS)
            return True
        except Exception as exc:
            notices[protocol] = "RF settings error: " + str(exc)
            if notify:
                self.msg("[RF] " + notices[protocol], C_ERROR)
            return False

    def _poll_radio_profiles(self):
        """Resolve GPS at most once a second, and retune only the current owner."""
        controller = getattr(self, "_rf_profiles", None)
        if controller is None:
            return
        now = time.monotonic()
        if now - getattr(self, "_rf_last_poll", -float("inf")) < 1.0:
            return
        self._rf_last_poll = now
        try:
            radio_fix = getattr(self.gps, "radio_fix", None)
            controller.update(radio_fix() if radio_fix else None)
            protocol = self._active_lora_protocol()
            if protocol not in ("meshcore", "meshtastic"):
                return
            target = controller.target(protocol)
            current = self._mt_profile if protocol == "meshtastic" else self._mc_region
            if target and target != current:
                self._apply_radio_profile(protocol, target)
            elif target == current and controller.status(protocol)["active_profile"] != current:
                controller.applied(protocol, current)
        except Exception as exc:
            self._rf_notices["gps"] = "AUTO GPS error: " + str(exc)

    def _update_radio_profile_picker(self, protocol):
        controller = self._rf_profiles
        choices = controller.choices(protocol)
        index_name = "_mt_profile_sel" if protocol == "meshtastic" else "_mc_region_sel"
        screen_name = "_mt_profile_screen" if protocol == "meshtastic" else "_mc_region_screen"
        if not choices:
            setattr(self, screen_name, False)
            return
        index = min(max(0, getattr(self, index_name)), len(choices) - 1)
        confirm = self._rf_confirm
        if confirm is not None and confirm["protocol"] == protocol:
            if pyxel.btnp(pyxel.KEY_ESCAPE) or pyxel.btnp(pyxel.KEY_N):
                self._rf_confirm = None
                self._esc_consumed_frame = pyxel.frame_count
            elif pyxel.btnp(pyxel.KEY_Y):
                try:
                    if confirm["action"] == "remember":
                        # Repeat the manual/actual check in case an external
                        # event changed the selection while the dialog was open.
                        status = controller.status(protocol)
                        actual = self._mt_profile if protocol == "meshtastic" else self._mc_region
                        if (status["mode"] != "manual" or status["manual_profile"] != confirm["key"]
                                or actual != confirm["key"]):
                            raise ValueError("Press ENTER first to apply a MANUAL profile")
                        controller.remember(protocol, confirm["key"], confirm["preview"])
                        notice = "Profile saved for the displayed 2 km circle only"
                    else:
                        controller.forget_area(protocol, confirm["preview"])
                        notice = "Local AUTO assignment removed"
                    self._rf_notices[protocol] = notice
                    self.msg("[RF] " + notice, C_SUCCESS)
                except Exception as exc:
                    self._rf_notices[protocol] = str(exc)
                    self.msg("[RF] " + str(exc), C_ERROR)
                self._rf_confirm = None
            return
        if pyxel.btnp(pyxel.KEY_UP):
            index = max(0, index - 1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            index = min(len(choices) - 1, index + 1)
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            setattr(self, screen_name, False)
            self._rf_confirm = None
            self._esc_consumed_frame = pyxel.frame_count
        elif pyxel.btnp(pyxel.KEY_A):
            try:
                controller.set_auto(protocol)
                self._rf_notices[protocol] = "AUTO requires a GPS fix and an approved local profile"
            except Exception as exc:
                self._rf_notices[protocol] = str(exc)
                self.msg("[RF] " + str(exc), C_ERROR)
        elif pyxel.btnp(pyxel.KEY_M) or pyxel.btnp(pyxel.KEY_RETURN):
            try:
                controller.set_manual(protocol, choices[index])
                self._apply_radio_profile(protocol, choices[index], notify=True)
            except Exception as exc:
                self._rf_notices[protocol] = str(exc)
                self.msg("[RF] " + str(exc), C_ERROR)
        elif pyxel.btnp(pyxel.KEY_S) or pyxel.btnp(pyxel.KEY_F):
            action = "remember" if pyxel.btnp(pyxel.KEY_S) else "forget"
            key = choices[index]
            status = controller.status(protocol)
            actual = self._mt_profile if protocol == "meshtastic" else self._mc_region
            if action == "remember" and (status["mode"] != "manual"
                    or status["manual_profile"] != key or actual != key):
                self._rf_notices[protocol] = "Press ENTER first to apply a MANUAL profile"
            else:
                preview = controller.preview_area(protocol)
                if preview is None:
                    self._rf_notices[protocol] = "Saving/removing requires a stable GPS fix"
                elif action == "forget" and hasattr(controller, "has_area") and not controller.has_area(protocol, preview):
                    self._rf_notices[protocol] = "No saved assignment for this area"
                else:
                    self._rf_confirm = dict(protocol=protocol, action=action, key=key,
                                            preview=dict(preview))
        setattr(self, index_name, index)

    def _draw_radio_profile_picker(self, protocol):
        from .radio_profiles import PROFILES
        controller = self._rf_profiles
        choices = controller.choices(protocol)
        index = getattr(self, "_mt_profile_sel" if protocol == "meshtastic" else "_mc_region_sel")
        status = controller.status(protocol)
        actual = self._mt_profile if protocol == "meshtastic" else self._mc_region
        label = "MESHTASTIC" if protocol == "meshtastic" else "MESHCORE"
        visible = min(6, len(choices))
        dx, dw, dh = 100, 440, 128 + visible * 14
        dy = HUD_TOP + max(0, (TERM_Y - HUD_TOP - dh) // 2)
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        pyxel.text(dx + 4, dy + 4, label + " - RF Profile", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        confirm = self._rf_confirm
        if confirm is not None and confirm["protocol"] == protocol:
            preview = confirm["preview"]
            action = "SAVE" if confirm["action"] == "remember" else "REMOVE"
            lines = [action + " LOCAL AUTO ASSIGNMENT?",
                     "Protocol: " + label,
                     "Profile: " + PROFILES[confirm["key"]]["label"],
                     "Area: " + preview["label"],
                     f"GPS center: {preview['lat']:.6f}, {preview['lon']:.6f}",
                     f"CIRCLE - radius {preview['radius_km']:g} km",
                     "This circle only; not the whole country.",
                     "Y confirm   N/ESC cancel"]
            for i, text in enumerate(lines):
                pyxel.text(dx + 6, dy + 23 + i * 14, text[:106], C_WARNING if i == 0 else C_TEXT)
            return
        mode = "AUTO GPS" if status["mode"] == "auto" else "MANUAL"
        pyxel.text(dx + 6, dy + 20, f"Mode: {mode}    GPS region: {status['region']}", C_HACK_CYAN)
        actual_cfg = PROFILES.get(actual, {})
        actual_label = actual_cfg.get("label", actual)
        if actual_cfg:
            actual_label += (f" {actual_cfg['frequency_hz']/1e6:.3f} SF{actual_cfg['sf']}"
                             f" BW{actual_cfg['bandwidth_hz']/1000:g} CR4/{actual_cfg['coding_rate_denominator']}")
        manager = self._mt if protocol == "meshtastic" else self._lora
        rf_state = "Active RF" if manager.running and manager.ready and manager.mode == protocol else "Saved RF / radio OFF"
        pyxel.text(dx + 6, dy + 32, f"{rf_state}: {actual_label}"[:106], C_TEXT)
        pyxel.text(dx + 6, dy + 44, status["gps_status"][:106], C_WARNING)
        pyxel.text(dx + 6, dy + 56, status["reason"][:106], C_DIM)
        notice = self._rf_notices.get(protocol) or self._rf_notices.get("gps", "")
        pyxel.text(dx + 6, dy + 68, notice[:106], C_WARNING)
        if protocol == "meshtastic":
            channel = self._mt.channel_name
        else:
            channels = self._mc_channels_list
            channel = channels[self._mc_active_ch].name if channels else "--"
        channel_text = "Chat channel: " + channel + " (unchanged by GPS)"
        if choices and PROFILES[choices[min(max(index, 0), len(choices) - 1)]]["region"] == "US":
            channel_text += "  USA RF/antenna: TEST PENDING"
        pyxel.text(dx + 6, dy + 80, channel_text[:106], C_DIM)
        start = max(0, min(index - visible + 1, len(choices) - visible))
        for row, key in enumerate(choices[start:start + visible]):
            cfg = PROFILES[key]
            y = dy + 96 + row * 14
            selected = start + row == index
            if selected:
                pyxel.rect(dx + 2, y - 1, dw - 4, 13, C_WARNING)
            entry = (f"{cfg['label']}  {cfg['frequency_hz']/1e6:.3f} "
                     f"SF{cfg['sf']} BW{cfg['bandwidth_hz']/1000:g} CR4/{cfg['coding_rate_denominator']}")
            pyxel.text(dx + 6, y + 3, entry[:106], 0 if selected else C_TEXT)
        pyxel.text(dx + 4, dy + dh - 21, "A AUTO  M/ENTER MANUAL  UP/DOWN profile  ESC Back", C_DIM)
        pyxel.text(dx + 4, dy + dh - 11, "S Save for this area   F Remove assignment", C_DIM)

    def _toggle_sdr(self):
        new_state = not self._sdr_enabled
        if not self._aio_available:
            self.msg("[SDR] AIO v2 not available", C_ERROR)
            return
        ok = self._aio_power.toggle("sdr", new_state)
        if not ok:
            self.msg("[SDR] GPIO toggle failed", C_ERROR)
            return
        self._sdr_enabled = new_state
        if new_state:
            self.msg("[SDR] ON", C_SUCCESS)
            self._term_add("[SYS] SDR enabled", raw=True)
        else:
            self._sdr.stop()
            self.msg("[SDR] OFF", C_WARNING)
            self._term_add("[SYS] SDR disabled", raw=True)
        self.glitch_timer = 2

    def _sdr_adsb(self):
        if not self._sdr_enabled:
            self.msg("[SDR] Enable SDR first (SYSTEM > SDR)", C_WARNING)
            return
        if self._sdr.running and "adsb" in self._sdr.mode:
            self._sdr.stop()
            self.msg("[ADS-B] Stopped", C_WARNING)
            self._term_add("[SDR] ADS-B radar stopped", raw=True)
            return
        try:
            loot_dir = self.loot.session_dir if self.loot else ""
        except Exception:
            loot_dir = ""
        ok = self._sdr.start_adsb(loot_dir)
        if ok:
            if self._aio_available:
                self._aio_power.claim("sdr")
            self.msg("[ADS-B] Radar active", C_SUCCESS)
            self._term_add("[SDR] ADS-B radar started — tracking aircraft", raw=True)
            self._earn_badge("skywatch")
        else:
            # Drain error events so user sees the reason
            for etype, data in self._sdr.poll_events():
                if etype == "error":
                    self.msg(f"[SDR] {data}", C_ERROR)
                    self._term_add(f"[SDR] {data}", raw=True)
            if not self._sdr.has_dump1090():
                self.msg("[ADS-B] dump1090 not installed", C_ERROR)
                self._term_add("[SDR] Run ./setup.sh — it builds the FlightAware",
                               raw=True)
                self._term_add("[SDR] fork from source (apt dump1090-mutability",
                               raw=True)
                self._term_add("[SDR] was archived upstream in 2018).",
                               raw=True)
        self.glitch_timer = 2

    def _sdr_433(self):
        if not self._sdr_enabled:
            self.msg("[SDR] Enable SDR first (SYSTEM > SDR)", C_WARNING)
            return
        if self._sdr.running and "433" in self._sdr.mode:
            self._sdr.stop()
            self.msg("[433] Stopped", C_WARNING)
            self._term_add("[SDR] 433 MHz scanner stopped", raw=True)
            return
        try:
            loot_dir = self.loot.session_dir if self.loot else ""
        except Exception:
            loot_dir = ""
        ok = self._sdr.start_433(loot_dir, self.player_lat, self.player_lon)
        if ok:
            if self._aio_available:
                self._aio_power.claim("sdr")
            self.msg("[433] Scanner active", C_SUCCESS)
            self._term_add("[SDR] 433 MHz scanner started — listening for sensors", raw=True)
            self._earn_badge("iot_hunter")
        else:
            for etype, data in self._sdr.poll_events():
                if etype == "error":
                    self.msg(f"[SDR] {data}", C_ERROR)
                    self._term_add(f"[SDR] {data}", raw=True)
            if not self._sdr.has_rtl433():
                self.msg("[433] rtl_433 not installed", C_ERROR)
                self._term_add("[SDR] Install: sudo apt install rtl-433", raw=True)
        self.glitch_timer = 2

    def _sdr_emergency(self):
        """Emergency-vehicle RF presence scanner (fire VHF + police/EMS TETRA)."""
        self._term_add("[SVC] Emergency Radio selected...", raw=True)
        if not self._sdr.has_rtl_power():
            self.msg("[SDR] rtl_power missing (apt install rtl-sdr)", C_ERROR)
            self._term_add("[SDR] rtl_power not installed", raw=True)
            return
        if self._sdr.running and "emerg" in self._sdr.mode:
            self._sdr.stop()
            self.msg("[SVC] Emergency scan stopped", C_WARNING)
            self._term_add("[SDR] Emergency radio scanner stopped", raw=True)
            return
        # RTL-SDR is a USB dongle (always enumerated) -> don't hard-block on the
        # AIO SDR toggle; power the AIO rail if we can, but run either way.
        if not self._sdr_enabled and self._aio_available:
            try:
                if self._aio_power.toggle("sdr", True):
                    self._sdr_enabled = True
                    self._term_add("[SYS] SDR power on (AIO)", raw=True)
            except Exception:
                pass
        # One RTL-SDR dongle -> emergency scan needs it exclusively.
        if self._sdr.running:
            self._sdr.stop()
            self._term_add("[SDR] stopped other SDR mode (single dongle)", raw=True)
        try:
            loot_dir = self.loot.session_dir if self.loot else ""
        except Exception:
            loot_dir = ""
        ok = self._sdr.start_emergency(loot_dir, self.player_lat, self.player_lon)
        if ok:
            if self._aio_available:
                self._aio_power.claim("sdr")
            self.msg("[SVC] Emergency radio scan ACTIVE", C_SUCCESS)
            self._term_add("[SDR] Emergency scan ON: STRAZ-VHF + TETRA "
                           "(presence/RSSI, no decode)", raw=True)
            self._earn_badge("emergency")
        else:
            self._term_add("[SDR] Emergency scan FAILED to start", raw=True)
            for etype, data in self._sdr.poll_events():
                if etype == "error":
                    self.msg(f"[SDR] {data}", C_ERROR)
                    self._term_add(f"[SDR] {data}", raw=True)
        self.glitch_timer = 2

    # ---- Monster RF (JanOS RF, CC1101) sub-GHz — dedicated screen ----------
    def _subghz_open_screen(self):
        if not SubGhzManager.available():
            self.msg("[SubGHz] pyserial missing", C_ERROR)
            return
        self._subghz_screen = True
        self._subghz_mode = "idle"
        self._subghz_sel = 1                      # first actionable (0 = header)
        self.menu_open = False
        if not self._subghz.connected:
            self._subghz_notice = "Connecting to Monster RF (ttyUSB)..."
            threading.Thread(target=self._subghz_connect_worker, daemon=True).start()
        else:
            self._subghz_notice = f"Connected: {self._subghz.port}"

    def _subghz_connect_worker(self, then=None):
        """Connect to the Monster RF off the main thread (find_port blocks)."""
        if getattr(self, "_shutting_down", False):
            return
        if not self._subghz.connected:
            excl = (self._boot_serial_port,) if self._boot_serial_port else ()
            if not self._subghz.connect(exclude=excl):
                if getattr(self, "_shutting_down", False):
                    return
                # Not found. If the game's ESP32 auto-detect grabbed the Monster
                # itself (a CH340 -> ttyUSB port; the real pentest XIAO is
                # native-USB -> ttyACM), release it and retry so sub-GHz can use
                # it. This happens when ttyACM0 hangs and detection falls back.
                bp = self._boot_serial_port or ""
                if "ttyUSB" in bp and self.serial is not None:
                    try:
                        self.serial.close()
                    except Exception:
                        pass
                    self.serial = None
                    self._esp32 = False
                    self._boot_serial_port = None
                    self._term_add(
                        f"[SubGHz] released {bp} from ESP32 (it's the Monster)",
                        raw=True)
                    if not self._subghz.connect():
                        self._subghz_notice = "Monster RF not found (JanOS RF?)"
                        return
                else:
                    self._subghz_notice = "Monster RF not found (JanOS RF?)"
                    return
            if getattr(self, "_shutting_down", False):
                self._subghz.shutdown()
                return
            self._earn_badge("flipper")
            try:
                if (self.loot and self.loot.session_dir
                        and self._subghz_logf is None):
                    self._subghz_logf = open(
                        os.path.join(self.loot.session_dir,
                                     "subghz_captures.log"), "a")
            except Exception:
                self._subghz_logf = None
            self._subghz_notice = f"Connected: {self._subghz.port}"
        if then:
            time.sleep(1.2)   # drain the connect subghz_status reply first so it
            if getattr(self, "_shutting_down", False):
                return
            try:              # doesn't merge into the next command's output
                then()
            except Exception:
                pass

    def _subghz_screen_action(self, action):
        """Run a screen action, connecting first (async) if needed."""
        if not self._subghz.connected:
            threading.Thread(
                target=self._subghz_connect_worker,
                args=(lambda: self._subghz_do(action),), daemon=True).start()
            self._subghz_notice = "Connecting..."
            return
        self._subghz_do(action)

    def _subghz_do(self, action):
        if action == "read":
            self._subghz_drops = 0
            self._subghz.drops = 0
            self._subghz_catches = []
            self._subghz_mode = "read"
            self._subghz_notice = (f"Listening @ {self._subghz_freq:.2f} MHz "
                                   "- press your remote")
            threading.Thread(target=self._subghz_start_capture,
                             args=(False,), daemon=True).start()
        elif action == "record":
            self._subghz_drops = 0
            self._subghz.drops = 0
            self._subghz_catches = []
            self._subghz_mode = "record"
            self._subghz_rec_start = time.time()
            self._subghz_notice = (f"Recording RAW @ {self._subghz_freq:.2f} MHz"
                                   " - press your remote")
            threading.Thread(target=self._subghz_start_capture,
                             args=(True,), daemon=True).start()
        elif action == "analyzer":
            self._subghz_drops = 0
            self._subghz.drops = 0
            self._subghz_catches = []
            self._subghz_fa_hits = []
            self._subghz.cmd_analyzer(-90)
            self._subghz_mode = "analyzer"
            self._subghz_notice = ("Analyzer: press a remote - finds its REAL "
                                   "frequency (any band)")
        elif action == "files":
            self._subghz_mode = "files"
            self._subghz_sel = 0
            self._subghz_files = []
            self._subghz_notice = "Loading saved signals from SD..."
            self._subghz_expect_list = True
            self._subghz.cmd_list("sd")      # JanOS-style: ONE read, static list
        elif action == "status":
            self._subghz.cmd_status()
            self._subghz_notice = "Status requested"

    def _subghz_start_capture(self, raw):
        """Tune to the selected capture frequency, then start RX. Paced in a
        thread (the JanOS RF console drops back-to-back commands)."""
        try:
            m = self._subghz
            m.cmd_stop()
            time.sleep(0.35)
            m.cmd_clear()                 # start clean - drop stale ambient
            time.sleep(0.35)
            m.cmd_freq(self._subghz_freq)
            time.sleep(0.35)
            m.cmd_rx(raw=bool(raw))
        except Exception:
            pass

    def _subghz_relist_sd(self, delay=1.4):
        """Re-read the SD list ONCE after rename/delete. Coalesced (~2.5s) so
        the explicit call and the 'renamed' event don't fire two overlapping
        subghz_list -> duplicated/flickering rows."""
        now = time.time()
        if now - getattr(self, "_subghz_relist_ts", 0.0) < 2.5:
            return
        self._subghz_relist_ts = now
        def _w():
            time.sleep(delay)
            self._subghz_expect_list = True
            try:
                self._subghz.cmd_list("sd")
            except Exception:
                pass
        threading.Thread(target=_w, daemon=True).start()

    def _subghz_save_and_list(self):
        """After a capture: persist mem -> SD, then list the SD (JanOS-style)."""
        try:
            m = self._subghz
            m.cmd_stop(); time.sleep(0.5)
            m.cmd_save("all"); time.sleep(1.8)
            self._subghz_expect_list = True
            m.cmd_list("sd")     # -> list_result -> _subghz_files
        except Exception:
            pass

    def _subghz_stop_and_save(self):
        """Stop RX/record, then copy captured mem signals to SD (so they show up
        in Saved and can be replayed). Paced in a thread — the JanOS RF console
        has no flow control, so back-to-back commands get dropped."""
        if not self._subghz.connected:
            return

        def _work():
            try:
                self._subghz.cmd_stop()
                time.sleep(0.8)
                self._subghz.cmd_list("mem")     # -> catches (list_result mem)
                time.sleep(1.0)
                self._subghz.cmd_save("all")     # persist mem -> SD
                time.sleep(1.6)                  # SD writes are slow
                # export each signal's clean metadata to the uConsole library
                if not getattr(self, "_subghz_refreshing", False):
                    self._subghz_refresh_from_monster()
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    @staticmethod
    def _subghz_parse_entry(line: str) -> dict:
        """Parse a [SUBGHZ_LIST] row, e.g.
        '[SUBGHZ_LIST] idx=1 type=Princeton freq=433.92 bits=24 id=037C74 ...
         name=00001_Princeton_433' or a RAW '... edges=9 name=00002_raw_433920'."""
        idx = typ = freq = name = idcode = ""
        for tok in line.split()[1:]:
            if tok.startswith("idx="):
                idx = tok[4:]
            elif tok.startswith("type="):
                typ = tok[5:]
            elif tok.startswith("freq="):
                freq = tok[5:]
            elif tok.startswith("name="):
                name = tok[5:]
            elif tok.startswith("id="):
                idcode = tok[3:]
            elif "=" not in tok and not name:
                name = tok
        idpart = f" id={idcode}" if idcode else ""
        disp = f"[{idx}] {typ:<9} {freq}MHz{idpart}  {name}".rstrip()
        # Saved signals live under /sdcard/lab/subghz/<name>
        path = ("/sdcard/lab/subghz/" + name) if name else ""
        return {"idx": idx, "type": typ, "freq": freq, "name": name,
                "display": disp, "path": path}

    @staticmethod
    def _subghz_fmt_name(fname: str) -> str:
        """'00001_Princeton_433.92_037C74.sub' -> 'Princeton 433.92 037C74'."""
        n = fname[:-4] if fname.endswith(".sub") else fname
        parts = n.split("_", 1)
        if len(parts) == 2 and parts[0].isdigit():
            n = parts[1]
        return n.replace("_", " ")

    def _subghz_info_entry(self, line):
        """Parse a [SUBGHZ_INFO] reply into an index-aware Saved entry usable for
        subghz_tx: {idx, type, raw, name, display}."""
        import re
        m = re.search(r"\[SUBGHZ_INFO\][^\[]*", line)
        meta = m.group(0) if m else line

        def g(k):
            mm = re.search(k + r"=(\S+)", meta)
            return mm.group(1) if mm else ""

        idx = g("idx")
        if not idx:
            return None
        try:
            idxn = int(idx)
        except ValueError:
            return None
        typ, name, frq, idc = g("type"), g("name"), g("freq"), g("id")
        israw = (typ.lower() == "raw")
        disp_name = self._subghz_fmt_name(name) if name else f"signal {idxn}"
        idpart = f" {idc}" if idc else ""
        disp = f"[{idxn}] {(typ or '?'):<9} {frq or '?'}MHz{idpart}  {disp_name}".rstrip()
        return {"idx": idxn, "type": typ, "raw": israw, "name": name,
                "display": disp,
                "path": ("/sdcard/lab/subghz/" + name if name else "")}

    def _subghz_ingest_info(self, line):
        """Update the live SD index map from an info reply. Does NOT rebuild the
        Saved list — the uConsole library owns display (custom names)."""
        e = self._subghz_info_entry(line)
        if not e:
            return
        self._subghz_sd_by_idx[e["idx"]] = e

    def _subghz_fa_parse(self, line):
        """Pull freq / protocol / id / te from a [SUBGHZ_RX] analyzer decode."""
        import re
        fr = re.search(r"freq=([\d.]+)", line)
        if not fr:
            return None
        idm = re.search(r"id=([0-9A-Fa-f]+)", line)
        tp = re.search(r"type=(\w+)", line) or re.search(r"proto=(\w+)", line)
        te = re.search(r"te=(\d+)", line)
        return {"freq": fr.group(1),
                "proto": tp.group(1) if tp else "?",
                "id": idm.group(1) if idm else "?",
                "te": te.group(1) if te else "?"}

    def _subghz_read_library(self):
        """The Saved list, 100% from the uConsole library (instant, offline).
        Each .sub carries the user Display name + the Monster SD index used to
        transmit (subghz_tx) + the Raw flag."""
        out = []
        try:
            for fn in sorted(os.listdir(SUBGHZ_LIB_DIR)):
                if not fn.endswith(".sub"):
                    continue
                disp, idx, israw, typ, frq = "", None, False, "", ""
                try:
                    for ln in open(os.path.join(SUBGHZ_LIB_DIR, fn)):
                        if ln.startswith("# Display:"):
                            disp = ln.split(":", 1)[1].strip()
                        elif ln.startswith("# SDIndex:"):
                            try:
                                idx = int(ln.split(":", 1)[1].strip())
                            except ValueError:
                                idx = None
                        elif ln.startswith("# Raw:"):
                            israw = ln.split(":", 1)[1].strip() == "1"
                        elif ln.startswith("Protocol:"):
                            typ = ln.split(":", 1)[1].strip()
                        elif ln.startswith("Frequency:"):
                            frq = ln.split(":", 1)[1].strip()
                except Exception:
                    continue
                if not disp:
                    disp = self._subghz_fmt_name(fn)
                fmhz = ""
                try:
                    fmhz = ("%.2f" % (int(frq) / 1e6)) if frq else ""
                except Exception:
                    pass
                tag = "TX?" if idx is None else "TX"
                label = f"{disp}   [{typ or '?'} {fmhz}MHz {tag}]"
                out.append({"name": fn, "display": label, "disp_raw": disp,
                            "idx": idx, "raw": israw})
        except Exception:
            pass
        out.sort(key=lambda e: (e["idx"] is None, e["idx"] or 0, e["name"]))
        return out

    def _subghz_set_display(self, fname, newname):
        """Rename a saved signal = edit its Display line locally (instant, no
        Monster round-trip; the SD copy and its index are untouched)."""
        fp = os.path.join(SUBGHZ_LIB_DIR, fname)
        try:
            lines = open(fp).read().splitlines()
        except Exception:
            return False
        out, found = [], False
        for ln in lines:
            if ln.startswith("# Display:"):
                out.append("# Display: " + newname)
                found = True
            else:
                out.append(ln)
        if not found:
            out.append("# Display: " + newname)
        try:
            with open(fp, "w") as fh:
                fh.write("\n".join(out) + "\n")
            return True
        except Exception:
            return False

    def _subghz_refresh_from_monster(self):
        """Rebuild the index-aware Saved list from the Monster SD. This thread
        reads the replies itself (main-loop poll is paused via _subghz_refreshing)
        so it can stop at the real end of the library and show live progress.
        Bounded by list_dir\'s "Found N files" count -> no 12x fixed loop, no hang."""
        import re
        self._subghz_refreshing = True
        m = self._subghz
        entries = {}
        try:
            if not m.connected:
                return
            m.send("subghz_stop")
            time.sleep(0.7)
            try:
                m._ser.reset_input_buffer()
            except Exception:
                pass
            for _et, _d in m.poll_events():
                pass
            # How many signals are on SD? The short "Found N file(s)" line
            # survives even when the file list itself gets serial-mangled.
            count = 0
            m.send("list_dir /sdcard/lab/subghz")
            time.sleep(2.0)
            for _et, _d in m.poll_events():
                txt = _d if isinstance(_d, str) else (
                    " ".join(_d) if isinstance(_d, (list, tuple)) else str(_d))
                mm = re.search(r"Found\s+(\d+)\s+file", txt)
                if mm:
                    count = int(mm.group(1))
            n = count if 0 < count <= 60 else 20
            misses = 0
            for idx in range(1, n + 1):
                self._subghz_notice = f"Reading Monster SD {idx}/{n}..."
                m.cmd_info(idx, "sd")
                time.sleep(0.9)
                hit = False
                for _et, _d in m.poll_events():
                    if _et in ("info_meta", "info_result"):
                        line = _d if isinstance(_d, str) else " ".join(_d)
                        if "[SUBGHZ_INFO]" in line:
                            e = self._subghz_info_entry(line)
                            if e:
                                entries[e["idx"]] = e
                                self._subghz_write_sub([line])
                                hit = True
                if not hit:
                    misses += 1
                    if misses >= 3 and not count:   # no count -> stop after gaps
                        break
                else:
                    misses = 0
            if entries:
                self._subghz_sd_by_idx = dict(entries)
            # Saved list is rebuilt from the uConsole library (custom names +
            # the SD index each signal transmits with).
            self._subghz_files = self._subghz_read_library()
            self._subghz_notice = f"Saved: {len(self._subghz_files)} signal(s)"
        except Exception:
            pass
        finally:
            self._subghz_refreshing = False

    def _subghz_write_sub(self, lines):
        """Build a Flipper .sub in the uConsole library from a subghz_info reply.
        Metadata is clean on the wire; decoded fixed-code signals -> a proper
        key .sub (portable/replayable); RAW -> best-effort timings."""
        import re
        text = " ".join(lines)
        if "SUBGHZ_INFO_ERR" in text or "not_found" in text:
            return
        m = re.search(r"\[SUBGHZ_INFO\][^\[]*", text)
        meta = m.group(0) if m else text

        def g(k):
            mm = re.search(k + r"=(\S+)", meta)
            return mm.group(1) if mm else ""

        name, typ = g("name"), g("type")
        frq, idc, bits, te = g("freq"), g("id"), g("bits"), g("te")
        if not name:
            return
        # Reject garbage names produced when the serial merges/drops bytes
        # (e.g. "00004_rawsubghz_info", "00001_77") so junk files stop piling up.
        if ("subghz" in name.lower() or "info" in name.lower()
                or not re.match(r"^\d{3,}_[A-Za-z0-9][A-Za-z0-9._-]{2,}$", name)):
            return
        if not typ:
            return
        if not name.endswith(".sub"):
            name += ".sub"
        freq = "433920000"
        if frq:
            try:
                freq = str(int(round(float(frq) * 1e6)))
            except Exception:
                pass
        if typ and typ.lower() != "raw" and idc:
            try:
                keyb = int(idc, 16).to_bytes(8, "big")
            except Exception:
                keyb = bytes(8)
            key = " ".join("%02X" % b for b in keyb)
            body = ("Filetype: Flipper SubGhz Key File\nVersion: 1\n"
                    f"Frequency: {freq}\n"
                    "Preset: FuriHalSubGhzPresetOok650Async\n"
                    f"Protocol: {typ}\nBit: {bits or '24'}\nKey: {key}\n")
            if te:
                body += f"TE: {te}\n"
        else:
            raw = ""
            if "[SUBGHZ_INFO_RAW]" in text:
                raw = text.split("[SUBGHZ_INFO_RAW]", 1)[1]
            ints = re.findall(r"-?\d{2,}", raw)
            body = ("Filetype: Flipper SubGhz RAW File\nVersion: 1\n"
                    f"Frequency: {freq}\n"
                    "Preset: FuriHalSubGhzPresetOok650Async\n"
                    f"Protocol: RAW\nRAW_Data: {' '.join(ints)}\n")
        # --- uConsole-authoritative metadata ---
        # File is keyed by the Monster SD auto-name (stable link). We store the
        # user Display name, the SD index (for subghz_tx) and Raw flag, plus
        # capture time + GPS. Display / Captured / GPS are PRESERVED across
        # refreshes so renames and stamps survive.
        idx = g("idx")
        israw = bool(typ) and typ.lower() == "raw"
        fpath = os.path.join(SUBGHZ_LIB_DIR, name)
        keep = {}
        try:
            if os.path.exists(fpath):
                for ln in open(fpath):
                    for k in ("# Display:", "# Captured:", "# GPS:"):
                        if ln.startswith(k):
                            keep[k] = ln.rstrip("\n")
        except Exception:
            pass
        disp = keep.get("# Display:", "# Display: " + self._subghz_fmt_name(name))
        cap = keep.get("# Captured:",
                       "# Captured: " + time.strftime("%Y-%m-%dT%H:%M:%S"))
        gps = keep.get("# GPS:")
        if gps is None and self.gps_fix:
            latc = "N" if self.player_lat >= 0 else "S"
            lonc = "E" if self.player_lon >= 0 else "W"
            gps = (f"# GPS: {abs(self.player_lat):.6f}{latc} "
                   f"{abs(self.player_lon):.6f}{lonc}")
        body += disp + "\n" + f"# SDName: {name}\n"
        if idx:
            body += f"# SDIndex: {idx}\n"
        body += f"# Raw: {1 if israw else 0}\n" + cap + "\n"
        if gps:
            body += gps + "\n"
        try:
            os.makedirs(SUBGHZ_LIB_DIR, exist_ok=True)
            with open(fpath, "w") as fh:
                fh.write(body)
        except Exception:
            return

    def _update_subghz_screen(self):
        px = pyxel
        mode = self._subghz_mode
        if mode == "idle":
            n = len(SUBGHZ_MENU)
            if px.btnp(px.KEY_UP):
                s = self._subghz_sel - 1
                while s >= 0 and SUBGHZ_MENU[s][1] is None:
                    s -= 1
                if s >= 0:
                    self._subghz_sel = s
            elif px.btnp(px.KEY_DOWN):
                s = self._subghz_sel + 1
                while s < n and SUBGHZ_MENU[s][1] is None:
                    s += 1
                if s < n:
                    self._subghz_sel = s
            elif px.btnp(px.KEY_RETURN):
                act = SUBGHZ_MENU[self._subghz_sel][1]
                if act:
                    self._subghz_screen_action(act)
            elif px.btnp(px.KEY_F):
                fl = self._subghz_freqs
                try:
                    i = (fl.index(self._subghz_freq) + 1) % len(fl)
                except ValueError:
                    i = 0
                self._subghz_freq = fl[i]
                try:
                    os.makedirs(os.path.dirname(SUBGHZ_FREQ_FILE), exist_ok=True)
                    with open(SUBGHZ_FREQ_FILE, "w") as _fh:
                        _fh.write(f"{self._subghz_freq:.2f}")
                except Exception:
                    pass
                self._subghz_notice = (f"Capture freq: {self._subghz_freq:.2f}"
                                       " MHz (saved - survives restart)")
            elif px.btnp(px.KEY_ESCAPE):
                self._subghz_screen = False
                self._esc_consumed_frame = px.frame_count
        elif mode in ("read", "record", "analyzer"):
            if px.btnp(px.KEY_S):
                # [S] = SAVE the caught signal(s) to SD, then show the list
                caught = bool(self._subghz_catches) or self._subghz.key_count > 0
                if caught:
                    self._subghz_mode = "files"
                    self._subghz_sel = 0
                    self._subghz_files = []
                    self._subghz_notice = "Saving to SD..."
                    threading.Thread(target=self._subghz_save_and_list,
                                     daemon=True).start()
                else:
                    self._subghz.cmd_stop()
                    self._subghz_mode = "idle"
                    self._subghz_sel = 1
                    self._subghz_notice = "Nothing caught"
                self._esc_consumed_frame = px.frame_count
            elif px.btnp(px.KEY_ESCAPE):
                # [ESC] = stop and leave WITHOUT saving
                self._subghz.cmd_stop()
                self._subghz.cmd_clear()          # drop what was caught
                self._subghz_mode = "idle"
                self._subghz_sel = 1
                self._subghz_notice = "Cancelled (not saved)"
                self._esc_consumed_frame = px.frame_count
        elif mode == "files" and self._subghz_naming:
            # Typing a custom name for the selected saved signal.
            if px.btnp(px.KEY_RETURN):
                entry = (self._subghz_files[self._subghz_sel]
                         if self._subghz_files else None)
                nm = self._subghz_name_buf.strip()
                if entry and entry.get("idx") and nm:
                    self._subghz.cmd_rename(entry["idx"], nm)   # rename on SD
                    self._subghz_notice = f"Renaming to {nm}..."
                    self._subghz_relist_sd()
                self._subghz_naming = False
                self._subghz_name_buf = ""
            elif px.btnp(px.KEY_ESCAPE):
                self._subghz_naming = False
                self._subghz_name_buf = ""
                self._subghz_notice = "Rename cancelled"
                self._esc_consumed_frame = px.frame_count
            elif px.btnp(px.KEY_BACKSPACE):
                self._subghz_name_buf = self._subghz_name_buf[:-1]
            else:
                c = self._get_char_input()
                if c and len(self._subghz_name_buf) < 40:
                    # firmware allows [A-Za-z0-9_.-]; map spaces to _
                    if c == " ":
                        c = "_"
                    if c.isalnum() or c in "_.-":
                        self._subghz_name_buf += c
        elif mode == "files":
            if px.btnp(px.KEY_UP):
                self._subghz_sel = max(0, self._subghz_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._subghz_sel = min(max(0, len(self._subghz_files) - 1),
                                       self._subghz_sel + 1)
            elif px.btnp(px.KEY_RETURN) and self._subghz_files:
                entry = self._subghz_files[self._subghz_sel]
                idx = entry.get("idx")
                if idx:
                    self._subghz.cmd_tx(idx, "sd", entry.get("raw", False))
                    nm = entry.get("display", f"idx {idx}")
                    self._subghz_notice = f"Emulating {nm} ..."
                    self._term_add(f"[SUBGHZ] TX idx={idx} sd"
                                   + (" raw" if entry.get("raw") else ""), raw=True)
                else:
                    self._subghz_notice = "Indexing from SD... wait a moment, then ENTER"
            elif px.btnp(px.KEY_R) and self._subghz_files:
                # RAW replay (faithful waveform) - like the reference app's 'r'
                # action. Use when decoded TX doesn't trigger the device.
                entry = self._subghz_files[self._subghz_sel]
                idx = entry.get("idx")
                if idx:
                    self._subghz.cmd_tx(idx, "sd", True)
                    self._subghz_notice = ("Emulating RAW %s ..."
                                           % entry.get("disp_raw", f"idx {idx}"))
                    self._term_add(f"[SUBGHZ] TX idx={idx} sd raw", raw=True)
                else:
                    self._subghz_notice = "Indexing from SD... wait, then press R"
            elif px.btnp(px.KEY_N) and self._subghz_files:
                # rename the selected signal (writes subghz_rename on the SD)
                entry = self._subghz_files[self._subghz_sel]
                if entry.get("idx"):
                    self._subghz_naming = True
                    self._subghz_name_buf = ""
                    self._subghz_notice = "Type a name, ENTER to save"
            elif px.btnp(px.KEY_D) and self._subghz_files:
                # delete the selected signal from the SD, then re-list
                entry = self._subghz_files[self._subghz_sel]
                if entry.get("idx"):
                    self._subghz.cmd_delete(entry["idx"])
                    self._subghz_notice = "Deleting..."
                    self._subghz_sel = max(0, self._subghz_sel - 1)
                    self._subghz_relist_sd()
            elif px.btnp(px.KEY_ESCAPE):
                self._subghz_mode = "idle"
                self._subghz_sel = 1
                self._esc_consumed_frame = px.frame_count

    def _draw_subghz_rssi(self, x, y):
        px = pyxel
        r = self._subghz_rssi
        frac = max(0.0, min(1.0, (r + 110) / 70.0))
        px.text(x, y, f"RSSI {r:>4} dBm", C_DIM)
        bx, bw = x + 90, 190
        px.rectb(bx, y - 1, bw, 9, C_COAST)
        # Segmented LED signal meter — greens low, yellow mid, red hot.
        seg_w, gap = 8, 2
        nseg = max(1, (bw - 4) // (seg_w + gap))
        lit = int(nseg * frac)
        for i in range(nseg):
            sx = bx + 2 + i * (seg_w + gap)
            pos = i / max(1, nseg - 1)
            if i < lit:
                sc = 8 if pos > 0.8 else (C_WARNING if pos > 0.55 else C_SUCCESS)
            else:
                sc = 1
            px.rect(sx, y, seg_w, 7, sc)
        # Peak-hold marker that slowly decays back down.
        pk = max(frac, self.__dict__.get("_subghz_rssi_peak", 0.0) - 0.01)
        self._subghz_rssi_peak = pk
        pkx = bx + 2 + int((nseg - 1) * pk) * (seg_w + gap)
        px.rect(pkx, y - 1, 2, 9, 7)

    def _draw_subghz_screen(self):
        px = pyxel
        px.cls(0)
        px.rect(0, 0, W, 14, 1)
        px.text(6, 4, "MONSTER RF // SUB-GHZ", C_HACK_CYAN)
        px.text(220, 4, f"{getattr(self._subghz, 'freq', 433.92):.2f} MHz", C_WARNING)
        conn = self._subghz.connected
        px.text(340, 4, (self._subghz.port or "?") if conn else "connecting...",
                C_SUCCESS if conn else C_DIM)
        px.text(W - 60, 4, "[ESC]", C_DIM)
        px.line(0, 14, W, 14, C_HACK_CYAN)
        if self._subghz_notice:
            px.text(6, 20, self._subghz_notice[:98], C_DIM)

        mode = self._subghz_mode
        if mode == "idle":
            y = 44
            for i, (label, act) in enumerate(SUBGHZ_MENU):
                if act is None:
                    px.text(10, y, label, C_DIM)
                    y += 15
                    continue
                sel = (i == self._subghz_sel)
                if sel:
                    px.rect(8, y - 1, 320, 12, C_HACK_CYAN)
                    # Live selection glint — matches the main menu cursor.
                    _sw = int((px.frame_count * 4) % (320 + 24)) - 12
                    for _dx, _sc in ((0, 7), (-1, 6), (1, 6)):
                        _sx = 8 + _sw + _dx
                        if 8 < _sx < 327:
                            px.line(_sx, y - 1, _sx, y + 8, _sc)
                px.text(14, y + 2, label, 0 if sel else C_TEXT)
                y += 15
            px.text(10, H - 36,
                    f"Capture freq: {self._subghz_freq:.2f} MHz   [F] change   "
                    "(Analyzer auto-finds it)", C_WARNING)
            px.text(10, H - 24, "UP/DOWN select    ENTER    [F] freq    ESC exit",
                    C_DIM)
            px.text(10, H - 12,
                    "Read=listen  Record=RAW  Analyzer=find freq  Saved=replay",
                    C_DIM)
        elif mode in ("read", "record"):
            rec = (mode == "record")
            if rec:
                if (px.frame_count // 8) % 2 == 0:
                    px.circ(15, 39, 4, C_ERROR)
                el = int(time.time() - self._subghz_rec_start)
                px.text(26, 36, f"REC RAW  {el}s   (listening in background)", C_ERROR)
            else:
                px.text(10, 36, "LISTENING in background - waiting for a signal",
                        C_HACK_CYAN)
            self._draw_subghz_rssi(10, 54)
            px.text(300, 54, f"activity: {self._subghz_drops} bursts", C_DIM)
            px.text(440, 54, f"in mem: {self._subghz.key_count}", C_SUCCESS)
            px.text(10, 72, "CAUGHT SIGNALS:", C_WARNING)
            if not self._subghz_catches:
                px.text(14, 88,
                        "recording raw timings..." if rec
                        else "press your remote (gate / doorbell, 433 MHz)", C_DIM)
            else:
                yy = 88
                for sgl in self._subghz_catches[:22]:
                    px.text(14, yy, sgl[:112], C_SUCCESS)
                    yy += 11
            px.text(10, H - 12,
                    "[S] save to SD    [ESC] cancel (no save)", C_DIM)
        elif mode == "analyzer":
            px.text(10, 36, "FREQUENCY ANALYZER - point a remote and press it",
                    C_HACK_CYAN)
            self._draw_subghz_rssi(10, 54)
            px.text(440, 54, f"in mem: {self._subghz.key_count}", C_SUCCESS)
            hits = getattr(self, "_subghz_fa_hits", [])
            px.text(10, 74, "STRONGEST SIGNAL NEAR YOU:", C_WARNING)
            if not hits:
                px.text(14, 92,
                        "waiting - press any remote (gate / light / car key)...",
                        C_DIM)
            else:
                top = hits[0]
                px.text(14, 92, f"{top['freq']} MHz", C_SUCCESS)
                px.text(150, 92,
                        f"{top['proto']}   id={top['id']}   te={top['te']}us",
                        C_TEXT)
                yy = 112
                for h in hits[1:9]:
                    px.text(14, yy,
                            f"{h['freq']} MHz   {h['proto']}   id={h['id']}",
                            C_DIM)
                    yy += 11
            px.text(10, H - 12,
                    "[ESC]/[S] stop & save the caught signal (at its real freq)",
                    C_DIM)
        elif mode == "files":
            px.text(10, 36, "SAVED SIGNALS (SD):", C_WARNING)
            if not self._subghz_files:
                px.text(14, 54, "no saved signals / loading from SD...", C_DIM)
            else:
                yy = 54
                for i, entry in enumerate(self._subghz_files[:22]):
                    sel = (i == self._subghz_sel)
                    if sel:
                        px.rect(8, yy - 1, W - 16, 12, C_HACK_CYAN)
                    px.text(14, yy + 2, entry.get("display", "?")[:114],
                            0 if sel else C_TEXT)
                    yy += 12
            if self._subghz_naming:
                # name input overlay
                px.rect(8, H - 44, W - 16, 16, 0)
                px.rectb(8, H - 44, W - 16, 16, C_HACK_CYAN)
                cur = "_" if px.frame_count % 30 < 20 else ""
                px.text(12, H - 39, "NEW NAME: " + self._subghz_name_buf + cur,
                        C_SUCCESS)
                px.text(10, H - 12,
                        "[A-Z0-9_.-] type   ENTER save   ESC cancel", C_DIM)
            else:
                px.text(10, H - 12,
                        "[ENTER]TX  [R]RAW-TX  [N]rename  [D]delete  [ESC]back",
                        C_DIM)

    def _poll_subghz(self):
        if not self._subghz.connected:
            return
        if getattr(self, "_subghz_refreshing", False):
            return   # the refresh thread drains replies itself (avoids a race)
        # While listening, poll status ~1.5s so the "captured (mem)" count is live.
        if (self._subghz_mode in ("read", "record", "analyzer")
                and pyxel.frame_count % 45 == 0):
            self._subghz.cmd_status()
        try:
            for et, d in self._subghz.poll_events():
                if et == "rssi":
                    self._subghz_rssi = d
                elif et == "signal":
                    self._subghz_catches.insert(0, d)
                    del self._subghz_catches[40:]
                    fa = self._subghz_fa_parse(d)
                    if fa:
                        hs = [h for h in getattr(self, "_subghz_fa_hits", [])
                              if (h["freq"], h["id"]) != (fa["freq"], fa["id"])]
                        hs.insert(0, fa)
                        self._subghz_fa_hits = hs[:12]
                    self._subghz_notice = (f"Detected {fa['freq']}MHz {fa['proto']}"
                                           if fa else "Signal caught!")
                    self._play_subghz_beep()
                    self.gain_xp(10)
                    if self._subghz_logf:
                        try:
                            _gps = ""
                            if self.gps_fix:
                                _la = "N" if self.player_lat >= 0 else "S"
                                _lo = "E" if self.player_lon >= 0 else "W"
                                _gps = (f" gps={abs(self.player_lat):.6f}{_la},"
                                        f"{abs(self.player_lon):.6f}{_lo}")
                            self._subghz_logf.write(
                                time.strftime('%Y-%m-%dT%H:%M:%S') + _gps
                                + " " + d + "\n")
                            self._subghz_logf.flush()
                        except Exception:
                            pass
                    if ".sub" in d:
                        for tok in d.replace(",", " ").split():
                            if tok.endswith(".sub"):
                                self._subghz_last_file = tok
                elif et == "list_result":
                    src, lines = d
                    if src == "mem":
                        entries = [self._subghz_parse_entry(ln) for ln in lines]
                        if entries:
                            self._subghz_catches = [e["display"] for e in entries]
                    elif src == "sd":
                        if not getattr(self, "_subghz_expect_list", False):
                            continue   # stray emit (e.g. mangled TX line) - keep list
                        self._subghz_expect_list = False
                        fs = []
                        seen = set()
                        for ln in lines:
                            e = self._subghz_parse_entry(ln)
                            nm = e.get("name", "")
                            if not (e.get("idx") and nm and e.get("freq")):
                                continue            # drop mangled/partial rows
                            if nm in seen:
                                continue            # dedup within this one read
                            seen.add(nm)
                            e["raw"] = (e.get("type", "").upper() == "RAW")
                            e["disp_raw"] = self._subghz_fmt_name(nm)
                            e["display"] = (f"{e['disp_raw']}   "
                                            f"[{e.get('type','?')} "
                                            f"{e.get('freq','?')}MHz]")
                            fs.append(e)
                        self._subghz_files = fs
                        if self._subghz_mode == "files":
                            self._subghz_sel = min(self._subghz_sel,
                                                   max(0, len(fs) - 1))
                            self._subghz_notice = (f"Saved: {len(fs)} signal(s)"
                                                   if fs else "No saved signals")
                elif et == "info_meta":
                    self._subghz_write_sub([d])
                    self._subghz_ingest_info(d)
                elif et == "info_result":
                    self._subghz_write_sub(d)
                    self._subghz_ingest_info(
                        " ".join(d) if isinstance(d, (list, tuple)) else str(d))
                elif et == "tx":
                    sd = str(d).lower()
                    # Decoded key -> "...done"; RAW replay -> "frame=N/N reps=M".
                    if "force raw replay" in sd and "frame=" not in sd:
                        continue  # opening line only; the result line follows
                    ok = ("done" in sd or "frame=" in sd or "reps=" in sd)
                    self._subghz_notice = ("Transmitted OK - sent to device" if ok
                                           else "TX failed: " + str(d))
                    self._term_add("[SUBGHZ] " + str(d), raw=True)
                    if ok:
                        self.gain_xp(15)
                        self._play_subghz_beep()
                elif et == "renamed":
                    self._term_add("[SUBGHZ] " + str(d), raw=True)
                    self._subghz_relist_sd(0.3)
                elif et == "files_result":
                    pass  # SD list is best-effort; Saved is driven by the
                          # uConsole library + subghz_info refresh
                elif et == "drop":
                    self._subghz_drops += 1
                elif et == "status":
                    self._subghz_notice = d
                elif et == "error":
                    self._subghz_notice = "ERR: " + str(d)
                # info -> ignored (no terminal flood)
        except Exception:
            pass

    def _toggle_mute(self):
        """Toggle all notification sounds (siren + MeshCore notify)."""
        self._muted = not getattr(self, "_muted", False)
        if self._muted:
            self.msg("[SND] Powiadomienia wyciszone", C_WARNING)
            self._term_add("[SND] notifications MUTED", raw=True)
        else:
            self.msg("[SND] Dzwiek wlaczony", C_SUCCESS)
            self._term_add("[SND] notifications ON", raw=True)

    def _toggle_proximity(self):
        """Turn WDGWars proximity radar on/off (persisted across restarts)."""
        on = self._prox.toggle()
        if on:
            self.msg("[PROX] Radar wlaczony", C_SUCCESS)
            self._term_add("[PROX] proximity ON", raw=True)
        else:
            self.msg("[PROX] Radar wylaczony", C_WARNING)
            self._term_add("[PROX] proximity OFF", raw=True)

    def _host_bt_busy(self) -> bool:
        """True when something else is using the host hci0 radio (BlueDucky,
        RACE, the watch) or an ESP32 BT scan is running — proximity yields for
        the duration so it never fights for the controller / 2.4 GHz airtime."""
        return bool(
            self._blueducky.running or self._blueducky.connected
            or self._race.running or self._watch.connected
            or self.ble_scanning or self._ble_scan_only
            or self._bt_tracking or self._bt_airtag)

    def _poll_proximity(self):
        """Once per frame: pause proximity while the host BT is otherwise busy,
        keep the radio subprocess alive, and refresh the snapshot."""
        if self._host_bt_busy():
            self._prox.pause()
        else:
            self._prox.resume()
        self._prox.tick()

    def _update_prox_screen(self):
        if pyxel.btnp(pyxel.KEY_O):
            self._toggle_proximity()
        # Exit on any of the keys a player reaches for: ESC, TAB (the menu key),
        # Q or Backspace. Only ESC needs the consumed-frame guard so it does not
        # also trip the global quit dialog.
        if (pyxel.btnp(pyxel.KEY_TAB) or pyxel.btnp(pyxel.KEY_Q)
                or pyxel.btnp(pyxel.KEY_BACKSPACE)
                or pyxel.btnp(pyxel.KEY_ESCAPE)):
            self._prox_screen = False
            self._esc_consumed_frame = pyxel.frame_count

    def _toggle_usb(self):
        new_state = not self._usb_enabled
        if not self._aio_available:
            self.msg("[USB] AIO v2 not available", C_ERROR)
            return
        ok = self._aio_power.toggle("usb", new_state)
        if not ok:
            self.msg("[USB] GPIO toggle failed", C_ERROR)
            return
        self._usb_enabled = new_state
        if new_state:
            self.msg("[USB] ON", C_SUCCESS)
            self._term_add("[SYS] USB enabled", raw=True)
        else:
            self.msg("[USB] OFF", C_WARNING)
            self._term_add("[SYS] USB disabled", raw=True)
        self.glitch_timer = 2

    # ------------------------------------------------------------------
    # WPA-sec upload / download
    # ------------------------------------------------------------------

    def _wpasec_upload(self):
        """Upload all handshake pcaps to WPA-sec."""
        if not upload_manager.wpasec_configured():
            # No token — ask user to enter one
            self._wpasec_pending_action = "upload"
            self.input_mode = True
            self.input_fields = [{"label": "WPA-SEC KEY", "value": ""}]
            self.input_field_idx = 0
            self._input_pending_cat = -6  # WPA-sec token input
            self._input_pending_item = -1
            return
        self._wpasec_do_upload()

    def _wpasec_do_upload(self):
        if self._wpasec_busy:
            self.msg("[WPA-sec] Operation in progress...", C_WARNING)
            return
        loot_dir = Path(self._app_dir) / "loot"
        if not loot_dir.exists():
            self.msg("[WPA-sec] No loot directory", C_ERROR)
            return
        self._wpasec_busy = True
        self.msg("[WPA-sec] Uploading pcap files...", C_DIM)
        self._term_add("[WPA-sec] Starting handshake upload...", raw=True)

        # Pass whitelisted MACs so their handshakes are NOT uploaded
        blocked = {e.mac.upper() for e in self._whitelist.entries}

        def _bg():
            up, total, msg = upload_manager.upload_wpasec_all(
                loot_dir, blocked_macs=blocked)
            self._wpasec_result.put(("upload", up, total, msg))

        threading.Thread(target=_bg, daemon=True).start()

    def _wpasec_download(self):
        """Download cracked passwords from WPA-sec."""
        if not upload_manager.wpasec_configured():
            self._wpasec_pending_action = "download"
            self.input_mode = True
            self.input_fields = [{"label": "WPA-SEC KEY", "value": ""}]
            self.input_field_idx = 0
            self._input_pending_cat = -6
            self._input_pending_item = -1
            return
        self._wpasec_do_download()

    def _wpasec_do_download(self):
        if self._wpasec_busy:
            self.msg("[WPA-sec] Operation in progress...", C_WARNING)
            return
        loot_dir = Path(self._app_dir) / "loot"
        loot_dir.mkdir(parents=True, exist_ok=True)
        self._wpasec_busy = True
        self.msg("[WPA-sec] Downloading potfile...", C_DIM)
        self._term_add("[WPA-sec] Fetching cracked passwords...", raw=True)

        def _bg():
            ok, count, msg = upload_manager.download_wpasec_potfile(loot_dir)
            self._wpasec_result.put(("download", ok, count, msg))

        threading.Thread(target=_bg, daemon=True).start()

    def _poll_sdr(self):
        """Process SDR events (ADS-B aircraft, 433 MHz sensors)."""
        closing = getattr(self, "_shutting_down", False)
        if not self._sdr.running and not closing:
            return
        try:
            # Prune stale entries every ~2 seconds
            if not closing and pyxel.frame_count % 60 == 0:
                self._sdr.prune_stale()
                self._sdr.update_gps(self.player_lat, self.player_lon)
            for etype, data in self._sdr.poll_events():
                if etype == "log":
                    self._term_add(data, raw=True)
                elif etype == "status":
                    self._term_add(f"[SDR] {data}", raw=True)
                elif etype == "error":
                    self.msg(f"[SDR] {data}", C_ERROR)
                elif etype == "aircraft_new":
                    ac = data
                    if ac.icao not in self._sdr_aircraft_xp:
                        self._sdr_aircraft_xp.add(ac.icao)
                        self.gain_xp(10)
                elif etype == "sensor_new":
                    self.gain_xp(5)
                elif etype == "emergency_alert":
                    sig = data
                    arrow = " ^" if sig.trend == "up" else ""
                    svc = EMERGENCY_SVC.get(sig.band, sig.band)
                    svc_col = EMERGENCY_SVC_COL.get(sig.band, C_ERROR)
                    # Latch: keep the alert visible ~20s after the brief keyup.
                    self._emerg_latch = (sig.band, sig.rssi, sig.trend, time.time())
                    self.msg(f"[SVC] {svc} {sig.rssi:.0f}dB{arrow}", svc_col)
                    self._term_add(
                        f"[SDR] {svc} ({sig.band}) {sig.freq:.3f}MHz "
                        f"{sig.rssi:.0f}dB (+{sig.jump:.0f}) {sig.trend}", raw=True)
                    self._play_svc_sound(sig.band)
                    self.gain_xp(15)
                    self._earn_badge("emergency")
                elif etype == "emergency_clear":
                    self._term_add(f"[SDR] {data} cleared", raw=True)
        except Exception:
            pass

    def _watch_log_add(self, text: str, color: int = C_DIM):
        """Add line to watch overlay log (and terminal)."""
        self._watch_log.append((text, color))
        if len(self._watch_log) > 100:
            self._watch_log = self._watch_log[-100:]
        self._term_add(text, raw=True)

    def _poll_watch(self):
        """Process PipBoy watch events."""
        try:
            for etype, data in self._watch.poll_events():
                if etype == "status":
                    self._watch_log_add(f"[Watch] {data}", C_HACK_CYAN)
                elif etype == "error":
                    self._watch_log_add(f"[Watch] ERROR: {data}", C_ERROR)
                    self.msg(f"[Watch] {data}", C_ERROR)
                elif etype == "log":
                    self._watch_log_add(data, C_DIM)
                elif etype == "connected":
                    self._watch_log_add(f"[Watch] Connected: {data}", C_SUCCESS)
                    self.msg(f"[Watch] Connected: {data}", C_SUCCESS)
                elif etype == "disconnected":
                    self._watch_log_add("[Watch] Disconnected", C_WARNING)
                    self.msg("[Watch] Disconnected", C_WARNING)
                elif etype == "pin_request":
                    self._watch_pin_input = ""
                    self._watch_log_add("[Watch] PIN requested — enter on screen", C_WARNING)
                elif etype == "device":
                    self._watch_log_add(
                        f"[Watch] Found: {data['name']} "
                        f"RSSI:{data['rssi']}", C_SUCCESS)
                elif etype == "version":
                    ver = data.get("version", "?")
                    feats = data.get("features", [])
                    self._watch_log_add(
                        f"[Watch] {ver} [{','.join(feats)}]", C_HACK_CYAN)
                elif etype == "status_data":
                    bat = data.get("bat", 0)
                    chrg = "CHG" if data.get("charging") else ""
                    gps_lat = data.get("gps_lat", 0)
                    gps_lon = data.get("gps_lon", 0)
                    t = data.get("time", "")
                    parts = [f"BAT:{bat}%{chrg}"]
                    if t:
                        parts.append(t)
                    if gps_lat:
                        parts.append(f"GPS:{gps_lat:.4f},{gps_lon:.4f}")
                    parts.append(f"heap:{data.get('heap',0)}K")
                    self._watch_log_add(f"[Watch] {' | '.join(parts)}", C_SUCCESS)
                elif etype == "nfc_tag":
                    uid = data.get("uid", "?")
                    ndef = data.get("ndef", "")
                    self._watch_log_add(
                        f"[NFC] Tag detected: {uid} {ndef[:40]}", C_SUCCESS)
                    self.msg(f"[NFC] Tag: {uid}", C_SUCCESS)
                    self.gain_xp(25)
                elif etype == "nfc_list":
                    self._watch_nfc_tags = data.get("tags", [])
                    self._watch_log_add(
                        f"[NFC] {len(self._watch_nfc_tags)} tags on watch", C_HACK_CYAN)
                elif etype == "nfc_file":
                    self._save_nfc_file(data)
                    self._watch_log_add(
                        f"[NFC] File saved: {data.get('name','?')}", C_SUCCESS)
                elif etype == "lora_msg":
                    ch = data.get("channel", "?")
                    txt = data.get("text", "")
                    hops = data.get("hops", 0)
                    rssi = data.get("rssi", 0)
                    ts = time.strftime("%H:%M")
                    # Add to MeshCore chat with [W] tag (watch source)
                    self._mc_log.append(
                        (f"\x10 {txt}", C_SUCCESS,
                         f"{ts} {hops}hop W"))
                    self._mc_bubbles.append(
                        (txt[:40], pyxel.frame_count + 300))
                    self._play_mc_notify()
                    self._earn_badge("meshcore")
                    self._watch_log_add(
                        f"[LoRa] [{ch}] {txt}", C_HACK_CYAN)
                elif etype == "recon":
                    wifi = data.get("wifi", [])
                    ble = data.get("ble", [])
                    self._watch_log_add(
                        f"[Recon] {len(wifi)} WiFi, {len(ble)} BLE", C_SUCCESS)
                    for net in wifi:
                        auth = net.get("auth", "?")
                        self._watch_log_add(
                            f"  {net.get('ssid','?'):<20} ch:{net.get('ch',''):>2} "
                            f"{net.get('rssi',0):>4}dBm {auth}", C_DIM)
                    for dev in ble:
                        name = dev.get("name", "") or dev.get("mac", "?")
                        tag = " [AirTag]" if dev.get("airtag") else ""
                        self._watch_log_add(
                            f"  {name:<20} {dev.get('rssi',0):>4}dBm{tag}",
                            C_DIM)
                elif etype == "compass":
                    h = data.get("heading", 0)
                    r = data.get("roll", 0)
                    p = data.get("pitch", 0)
                    self._watch_log_add(
                        f"[Compass] {h:.0f}\u00b0  roll:{r:.1f} pitch:{p:.1f}",
                        C_HACK_CYAN)
                elif etype == "et_cred":
                    email = data.get("email", "")
                    pwd = data.get("password", "")
                    self._watch_log_add(
                        f"[ET] CREDENTIAL: {email} / {pwd}", C_SUCCESS)
                    self.msg(f"[ET] Cred captured via watch!", C_SUCCESS)
                    self.gain_xp(150)
                    self._earn_badge("evil_twin")
                elif etype == "deauth_detected":
                    src = data.get("src", "?")
                    count = data.get("count", 0)
                    ch = data.get("ch", "?")
                    self._watch_log_add(
                        f"[TSCM] Deauth detected! src:{src} ch:{ch} x{count}",
                        C_ERROR)
                    self.msg(f"[TSCM] Deauth on ch:{ch}!", C_ERROR)
                elif etype == "ack":
                    m = data.get("msg", "OK")
                    self._watch_log_add(f"[Watch] {m}", C_HACK_CYAN)
        except Exception:
            pass

    def _save_nfc_file(self, data: dict):
        """Save NFC file from watch to loot directory."""
        import base64
        name = data.get("name", "tag.nfc")
        b64 = data.get("data", "")
        if not b64:
            return
        try:
            content = base64.b64decode(b64).decode("utf-8", errors="replace")
            loot_dir = self.loot.session_dir if self.loot else ""
            if loot_dir:
                nfc_dir = os.path.join(loot_dir, "nfc")
                os.makedirs(nfc_dir, exist_ok=True)
                fname = os.path.basename(name)
                path = os.path.join(nfc_dir, fname)
                with open(path, "w") as f:
                    f.write(content)
                self.msg(f"[NFC] Saved: {fname}", C_SUCCESS)
                self._term_add(f"[Watch] NFC saved: {path}", raw=True)
                self.gain_xp(25)
        except Exception as e:
            self._term_add(f"[Watch] NFC save error: {e}", raw=True)

    def _watch_connect(self):
        """Handle PipBoy Watch menu action."""
        if self._watch.connected:
            # Already connected — open watch control screen
            self._watch_screen = True
            self._watch_menu_sel = 0
            self._watch.send_command("status")
            self._watch.send_command("nfc_list")
            return
        # Start scanning
        self._watch_screen = True
        self._watch_scan_sel = 0
        self._watch.scan()

    def _poll_wpasec_result(self):
        """Check for WPA-sec background operation results (called from update)."""
        if self._wpasec_result.empty():
            return
        result = self._wpasec_result.get_nowait()
        self._wpasec_busy = False
        if result[0] == "upload":
            _, up, total, msg = result
            if up > 0:
                self.msg(f"[WPA-sec] {up}/{total} uploaded", C_SUCCESS)
                self._earn_badge("wpasec_uploader")
            else:
                self.msg(f"[WPA-sec] {msg}", C_WARNING if total == 0 else C_ERROR)
            self._term_add(f"[WPA-sec] Upload: {msg}", raw=True)
        elif result[0] == "download":
            _, ok, count, msg = result
            if ok and count > 0:
                self.msg(f"[WPA-sec] {count} passwords!", C_SUCCESS)
            elif ok:
                self.msg("[WPA-sec] No cracked passwords yet", C_DIM)
            else:
                self.msg(f"[WPA-sec] {msg}", C_ERROR)
            self._term_add(f"[WPA-sec] Download: {msg}", raw=True)

    # ------------------------------------------------------------------
    # Flash ESP32
    # ------------------------------------------------------------------

    def _start_flash_esp32(self):
        """Start firmware flash wizard — board picker → download → flash."""
        from .config import FLASH_BOARDS
        self._flash_boards = list(FLASH_BOARDS.keys())
        self._flash_sel = 0
        self._flash_screen = True
        self._flash_running = False
        self.menu_open = False

    def _update_flash_screen(self):
        """Handle board picker input."""
        if self._flash_running:
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._flash_screen = False
                self._esc_consumed_frame = pyxel.frame_count
            return
        if pyxel.btnp(pyxel.KEY_UP):
            self._flash_sel = max(0, self._flash_sel - 1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._flash_sel = min(len(self._flash_boards) - 1, self._flash_sel + 1)
        elif pyxel.btnp(pyxel.KEY_RETURN):
            board = self._flash_boards[self._flash_sel]
            self._flash_running = True
            self._term_add(f"[FLASH] Selected: {board}", raw=True)
            threading.Thread(target=self._run_flash, args=(board,),
                             daemon=True).start()
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            self._flash_screen = False
            self._esc_consumed_frame = pyxel.frame_count

    def _run_flash(self, board: str):
        try:
            if not getattr(self, "_shutting_down", False):
                self._flash_do(board)
        finally:
            self._flash_running = False

    def _flash_do(self, board: str):
        """Download firmware and flash in background thread."""
        from .config import FLASH_BOARDS, FIRMWARE_RELEASE_URL
        import json
        import zipfile
        import subprocess
        import shutil
        from urllib.request import Request, urlopen
        from pathlib import Path

        profile = FLASH_BOARDS[board]
        fw_dir = Path(self._app_dir) / "firmware_cache"
        fw_dir.mkdir(exist_ok=True)

        self._term_add(f"[FLASH] Board: {profile['label']}", raw=True)

        if board == "xiao":
            self._term_add("[FLASH] XIAO: auto-reset via USB-JTAG (no BOOT button needed)", raw=True)

        # Step 1: Download
        self._term_add("[FLASH] Fetching latest release...", raw=True)
        try:
            req = Request(FIRMWARE_RELEASE_URL)
            req.add_header("User-Agent", "ESP32-Watch-Dogs")
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            tag = data["tag_name"]
            self._term_add(f"[FLASH] Release: {tag}", raw=True)

            # Find ZIP
            zip_suffix = "-xiao" if board == "xiao" else ""
            zip_url = None
            target = f"projectzerobylocosp{zip_suffix}"
            for asset in data.get("assets", []):
                name = asset["name"].lower()
                if name.startswith(target) and name.endswith(".zip") \
                        and "fap" not in name and "with" not in name:
                    zip_url = asset["browser_download_url"]
                    break

            if not zip_url:
                ver = tag.lstrip("v")
                zip_url = (
                    f"https://github.com/LOCOSP/projectZero/releases"
                    f"/download/{tag}/projectZerobyLOCOSP{zip_suffix}-{ver}.zip"
                )

            self._term_add(f"[FLASH] Downloading...", raw=True)
            self.msg("[FLASH] Downloading firmware...", C_DIM)

            zip_path = fw_dir / "firmware.zip"
            req = Request(zip_url)
            req.add_header("User-Agent", "ESP32-Watch-Dogs")
            with urlopen(req, timeout=120) as resp:
                with open(zip_path, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)

            self._term_add("[FLASH] Extracting...", raw=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(fw_dir)

            # Verify required files
            missing = [fn for fn in profile["offsets"]
                       if not (fw_dir / fn).exists()]
            if missing:
                self._term_add(f"[FLASH] Missing: {', '.join(missing)}", raw=True)
                self.msg("[FLASH] Download failed — missing files", C_ERROR)
                return

            self._term_add("[FLASH] Firmware ready", raw=True)

        except Exception as exc:
            self._term_add(f"[FLASH] Download error: {exc}", raw=True)
            self.msg("[FLASH] Download failed", C_ERROR)
            return

        # Step 2: Close serial
        port = self._boot_serial_port
        if self.serial and self.serial.is_open:
            self.serial.close()
            self._esp32 = False
            self.state.connected = False
            self._term_add(f"[FLASH] Serial {port} released", raw=True)

        # Step 3: USB power cycle + catch the ROM download window before flash.
        # The XIAO C5's running firmware disables the USB-JTAG reset, so esptool
        # can't reset the running app (Write timeout). A GPIO23 power cycle resets
        # the chip; esptool must then connect during the brief ROM window (before
        # the app boots and disables reset again) — so poll for the port and flash
        # IMMEDIATELY (no long wait). See reference_flash_xiao_c5.
        if self._aio_available and board == "xiao":
            self._term_add("[FLASH] USB power cycle (reset ESP32)...", raw=True)
            self._aio_power.toggle("usb", False)
            time.sleep(2)
            self._aio_power.toggle("usb", True)
            for _ in range(80):  # tight-poll for the port (~1s), catch ROM window
                if Path(port).exists():
                    break
                time.sleep(0.03)
            else:
                port = detect_esp32_port() or port
            self._term_add(f"[FLASH] ESP32 on {port} (ROM window)", raw=True)

        # Flash with esptool
        self._term_add("[FLASH] Flashing...", raw=True)
        self.msg("[FLASH] Flashing ESP32...", C_WARNING)

        # --no-stub: the stub flasher fails to start on the ESP32-C5
        # ("Failed to start stub flasher") — write via the ROM loader instead.
        # After a xiao flash we power-cycle to boot (a hard-reset boot-loops the
        # chip: rst:0x7 TG0_WDT), so leave the chip alone with --after no-reset.
        power_cycle_after = self._aio_available and board == "xiao"
        after = "no-reset" if power_cycle_after else "hard-reset"
        cmd = [
            sys.executable, "-m", "esptool",
            "-p", port, "-b", str(profile["baud"]),
            "--before", profile["before"],
            "--after", after,
            "--chip", "esp32c5",
            "--no-stub",
            "write-flash",
            "--flash-mode", "dio",
            "--flash-freq", "80m",
            "--flash-size", "detect",
        ]
        for filename, offset in profile["offsets"].items():
            cmd.extend([offset, str(fw_dir / filename)])

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self._term_add(f"[FLASH] {line}", raw=True)
            proc.wait()

            if proc.returncode == 0:
                self._term_add("[FLASH] Flash complete!", raw=True)
                if power_cycle_after:
                    # --after no-reset left the chip halted; a GPIO23 power cycle
                    # boots the new firmware cleanly (a hard-reset boot-loops it).
                    self._term_add("[FLASH] Power cycle to boot new firmware...",
                                   raw=True)
                    self._aio_power.toggle("usb", False)
                    time.sleep(2)
                    self._aio_power.toggle("usb", True)
                    time.sleep(4)
                self.msg("[FLASH] Success! ESP32 rebooting...", C_SUCCESS)
                # Reconnect after short delay
                time.sleep(3)
                self._try_reconnect_esp32()
            else:
                self._term_add(f"[FLASH] esptool error (code {proc.returncode})", raw=True)
                self.msg("[FLASH] Flash failed!", C_ERROR)
        except FileNotFoundError:
            self._term_add("[FLASH] esptool not found! pip install esptool", raw=True)
            self.msg("[FLASH] esptool not installed", C_ERROR)
        except Exception as exc:
            self._term_add(f"[FLASH] Error: {exc}", raw=True)
            self.msg("[FLASH] Flash failed", C_ERROR)

    # ------------------------------------------------------------------
    # Wardriving auto-repeat loop
    # ------------------------------------------------------------------

    def _update_wardriving_loop(self):
        """Auto-repeat WiFi/BT scans for continuous wardriving (like JanOS)."""
        now = time.time()

        # GPS wait — start scanning once fix is acquired
        if self._gps_wait:
            if self.gps_fix:
                self._gps_wait = False
                cmd = self._gps_wait_cmd
                state = self._gps_wait_state
                name = self._gps_wait_name
                self._gps_wait_cmd = ""
                self.msg("[GPS] Fix acquired — starting!", C_SUCCESS)
                if getattr(self, "_wardrive_selected", None):
                    self._wardrive_tick()
                else:
                    self._start_scan_cmd(cmd, state, name)
            else:
                # Show satellite info periodically
                if pyxel.frame_count % 60 == 0:
                    if self.gps_sats:
                        sat = f"Sat:{self.gps_sats}"
                    elif self.gps_sats_vis:
                        sat = f"Vis:{self.gps_sats_vis}"
                    else:
                        sat = "no satellites"
                    self.msg(f"[GPS] Waiting for fix... {sat}", C_WARNING)
            return

        if self._pending_cmd:
            return

        # A scan that never acknowledges completion must not look busy forever.
        timeout = getattr(self, "_ESP_SCAN_TIMEOUT", 20.0)
        for label, active, started, done in (
                ("WiFi", self.wifi_scanning or self._wifi_scan_only,
                 self._wifi_scan_start_time, self._wifi_scan_done_time),
                ("BLE", self.ble_scanning or self._ble_scan_only,
                 self._bt_scan_start_time, self._bt_scan_done_time)):
            if active and started > 0 and not done and now - started > timeout:
                if getattr(self, "_wardrive_phase", ""):
                    self._wardrive_pause(label + " scan timed out")
                    return
                self._request_esp_stop()
                self._clear_esp_scan_state(cancel_pending=True)
                message = f"[ERR] {label} scan timed out; check ESP32 connection"
                self.msg(message, C_ERROR)
                self._term_add(message, raw=True)
                return

        if hasattr(self, "_wardrive_selected"):
            self._wardrive_tick()
            return

        # WiFi wardriving auto-repeat
        if (self.wifi_scanning and self._wifi_scan_done_time > 0
                and not self._pending_cmd):
            if now - self._wifi_scan_done_time >= self._SCAN_INTERVAL:
                if self._send("scan_networks"):
                    self._wifi_scan_done_time = 0.0
                    self._wifi_scan_start_time = now
                    self.msg("[SCAN] WiFi rescan...", C_DIM)
                else:
                    self._clear_esp_scan_state(cancel_pending=True)
                    self.msg("[ERR] WiFi rescan failed", C_ERROR)

        # BT wardriving auto-repeat
        if (self.ble_scanning and self._bt_scan_done_time > 0
                and not self._pending_cmd):
            if now - self._bt_scan_done_time >= self._SCAN_INTERVAL:
                if self._send("scan_bt"):
                    self._bt_scan_done_time = 0.0
                    self._bt_scan_start_time = now
                    self.msg("[SCAN] BT rescan...", C_DIM)
                else:
                    self._clear_esp_scan_state(cancel_pending=True)
                    self.msg("[ERR] BT rescan failed", C_ERROR)

    # ------------------------------------------------------------------
    # Picker overlay (network / portal selection)
    # ------------------------------------------------------------------

    def _update_picker_overlay(self):
        """Handle UP/DOWN/ENTER/ESC for network or portal picker overlays."""
        import pyxel as px
        if self._et_net_screen:
            nets = self._attack_scan_results
            if px.btnp(px.KEY_UP):
                self._et_net_sel = max(0, self._et_net_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._et_net_sel = min(len(nets) - 1, self._et_net_sel + 1)
            elif px.btnp(px.KEY_SPACE) and nets:
                # Toggle multi-select (SPACE)
                idx = self._et_net_sel
                net = nets[idx]
                if self._whitelist.is_blocked(net.bssid):
                    self.msg(f"[WL] {net.bssid[-8:]} whitelisted", C_WARNING)
                    return
                if idx in self._et_net_selected:
                    self._et_net_selected.discard(idx)
                else:
                    self._et_net_selected.add(idx)
            elif px.btnp(px.KEY_RETURN) and nets:
                # Confirm selection
                selected = sorted(self._et_net_selected)
                if not selected:
                    # Nothing toggled → use cursor position as single select
                    selected = [self._et_net_sel]
                # Whitelist check on primary target
                primary = nets[selected[0]]
                if self._whitelist.is_blocked(primary.bssid):
                    self.msg(f"[WL] {primary.bssid[-8:]} whitelisted", C_WARNING)
                    return
                # First selected = clone SSID, rest = deauth targets
                self._et_net_idx = primary.index if hasattr(primary, 'index') else selected[0]
                # Build "select_networks 1 3 5" command args
                idx_args = []
                for si in selected:
                    n = nets[si]
                    idx_args.append(n.index if hasattr(n, 'index') else str(si))
                self._et_select_args = " ".join(idx_args)
                self._portal_ssid = primary.ssid or "<hidden>"
                self._et_net_screen = False
                self._show_portal_selection("ET")
            elif px.btnp(px.KEY_ESCAPE):
                self._et_net_screen = False
                self._attack_mode = ""
                self._attack_step = ""
                self._esc_consumed_frame = pyxel.frame_count
                self.msg("[ET] Cancelled", C_DIM)
        elif self._portal_select_screen:
            portals = self._portal_list
            if px.btnp(px.KEY_UP):
                self._portal_sel = max(0, self._portal_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._portal_sel = min(len(portals) - 1, self._portal_sel + 1)
            elif px.btnp(px.KEY_RETURN) and portals:
                name, html = portals[self._portal_sel]
                tag = "EP" if self._attack_mode == "evil_portal" else "ET"
                self._portal_select_screen = False
                self.msg(f"[{tag}] Portal: {name}", C_HACK_CYAN)

                def _start_portal_attack():
                    if html is not None:
                        self._term_add(f"[{tag}] Uploading portal HTML...",
                                       raw=True)
                        upload_html_to_esp32(html, self._send)
                        time.sleep(0.4)
                    if self._attack_mode == "evil_portal":
                        self._send(f"start_portal {self._portal_ssid}")
                        self.state.portal_running = True
                        self.state.portal_ssid = self._portal_ssid
                    else:
                        # Evil Twin: stop → clean state → select → start
                        self._term_add("[ET:CMD] stop (clean state)", raw=True)
                        self._send("stop")
                        time.sleep(1.5)
                        # Drain stale serial data
                        if self.serial and self.serial.is_open:
                            try:
                                while self.serial.ser.in_waiting:
                                    self.serial.ser.read(self.serial.ser.in_waiting)
                            except Exception:
                                pass

                        # Select target network(s)
                        args = self._et_select_args or str(self._et_net_idx)
                        cmd_sel = f"select_networks {args}"
                        self._term_add(f"[ET:CMD] {cmd_sel}", raw=True)
                        self._send(cmd_sel)
                        time.sleep(0.5)
                        if html is not None:
                            self._term_add("[ET:CMD] (HTML uploaded)", raw=True)
                        else:
                            self._term_add("[ET:CMD] Using firmware default portal", raw=True)
                        self._term_add("[ET:CMD] start_evil_twin", raw=True)
                        self._send("start_evil_twin")
                        self.state.evil_twin_running = True
                        self.state.evil_twin_ssid = self._portal_ssid
                    self._attack_step = "running"
                    self.msg(f"[{tag}] Started — SSID: {self._portal_ssid}",
                             C_WARNING)
                    self.glitch_timer = 8

                threading.Thread(target=_start_portal_attack,
                                 daemon=True).start()
            elif px.btnp(px.KEY_ESCAPE):
                self._portal_select_screen = False
                if self._attack_mode == "evil_twin":
                    self._show_net_selection()
                else:
                    self._attack_mode = ""
                    self._attack_step = ""
                self._esc_consumed_frame = pyxel.frame_count

    # ------------------------------------------------------------------
    # Input dialog
    # ------------------------------------------------------------------

    def _update_input_dialog(self):
        if not self.input_fields:
            self.input_mode = False
            return
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self.input_mode = False
            self._esc_consumed_frame = pyxel.frame_count
            # Cancel any pending attack flow that uses input dialog
            if self._input_pending_cat == -7:  # Evil Portal SSID
                self._attack_mode = ""
                self._attack_step = ""
            self.msg("[ESC] Input cancelled", C_DIM)
            return
        field = self.input_fields[self.input_field_idx]
        if pyxel.btnp(pyxel.KEY_BACKSPACE):
            field["value"] = field["value"][:-1]
            return
        if pyxel.btnp(pyxel.KEY_RETURN):
            if self.input_field_idx < len(self.input_fields) - 1:
                self.input_field_idx += 1
            else:
                self.input_mode = False
                vals = [f["value"].strip() for f in self.input_fields]
                # Special attack input handlers
                if self._input_pending_cat == -1:  # MITM victim IP
                    ip = vals[0] if vals else ""
                    if ip:
                        self._mitm_victim_ip = ip
                        self._mitm_state = "confirm"
                    else:
                        self._mitm_state = "idle"
                    return
                if self._input_pending_cat == -2:  # BlueDucky MAC
                    mac = vals[0].upper() if vals else ""
                    if mac:
                        if self._whitelist.is_blocked(mac):
                            self.msg(f"[WL] {mac[-8:]} whitelisted", C_WARNING)
                            return
                        self._blueducky.connect(mac)
                    return
                if self._input_pending_cat == -3:  # Whitelist MAC
                    mac = vals[0].upper().strip() if vals else ""
                    if mac:
                        self._wl_add_mac = mac
                        lbl = "SSID" if self._wl_add_type == "wifi" else "Name"
                        self.input_mode = True
                        self.input_fields = [{"label": lbl, "value": ""}]
                        self.input_field_idx = 0
                        self._input_pending_cat = -4
                        self._input_pending_item = -1
                    else:
                        self._wl_add_step = ""
                    return
                if self._input_pending_cat == -4:  # Whitelist Name/SSID
                    name_val = vals[0].strip() if vals else ""
                    if name_val and self._wl_add_mac:
                        ok = self._whitelist.add(
                            self._wl_add_type, self._wl_add_mac, name_val)
                        if ok:
                            self._term_add(
                                f"[WL] Added: {name_val} "
                                f"({self._wl_add_mac[-8:]})", raw=True)
                            self.msg("[WL] Entry added", C_SUCCESS)
                        else:
                            self.msg("[WL] MAC already exists", C_WARNING)
                    self._wl_add_step = ""
                    self._wl_add_mac = ""
                    return
                if self._input_pending_cat == -5:  # MeshCore node name
                    name_val = vals[0].strip() if vals else ""
                    if name_val:
                        self._mc_node_name = name_val
                        from .lora_manager import save_meshcore_config
                        save_meshcore_config(name_val, self._mc_channels_list)
                        self.msg(f"[MC] Node name: {name_val}", C_SUCCESS)
                    return
                if self._input_pending_cat == -9:  # Meshtastic local node name
                    name_val = vals[0] if vals else ""
                    if self._mt.set_node_name(name_val):
                        self._mt_view["node_name"] = self._mt.node_name
                        self._mt_view["log"].append(
                            (f"\x11 Node name: {self._mt.node_name}", C_HACK_CYAN))
                        self.msg(f"[MT] Node name: {self._mt.node_name}", C_SUCCESS)
                    else:
                        self.input_mode = True
                        self._mt_view["log"].append(
                            (f"\x11 {self._mt.last_error}", C_ERROR))
                        self.msg(f"[MT] {self._mt.last_error}", C_ERROR)
                    return
                if self._input_pending_cat == -6:  # WPA-sec token
                    token = vals[0].strip() if vals else ""
                    if token:
                        upload_manager.save_wpasec_key(self._app_dir, token)
                        self.msg("[WPA-sec] Token saved!", C_SUCCESS)
                        self._term_add("[WPA-sec] API key configured",
                                       raw=True)
                        # Continue with the pending action
                        if self._wpasec_pending_action == "upload":
                            self._wpasec_do_upload()
                        elif self._wpasec_pending_action == "download":
                            self._wpasec_do_download()
                        self._wpasec_pending_action = ""
                    else:
                        self.msg("[WPA-sec] No token — cancelled", C_DIM)
                        self._wpasec_pending_action = ""
                    return
                if self._input_pending_cat == -8:  # NFC tag save
                    name = vals[0].strip() if vals else ""
                    if name and hasattr(self, '_nfc_pending_save'):
                        tag = self._nfc_pending_save
                        safe_name = name.replace(" ", "_").replace("/", "_")
                        fname = f"{safe_name}.txt"
                        try:
                            nfc_dir = Path(self.loot.session_path) / "nfc"
                            nfc_dir.mkdir(exist_ok=True)
                            with open(nfc_dir / fname, "w") as f:
                                f.write("\n".join(tag["data"]))
                            self.msg(f"[NFC] Saved: {fname}", C_SUCCESS)
                            self._flipper_log.append(
                                (f"Saved: nfc/{fname}", C_SUCCESS))
                            self.gain_xp(25)
                        except Exception as exc:
                            self.msg(f"[NFC] Save error: {exc}", C_ERROR)
                        del self._nfc_pending_save
                    else:
                        self.msg("[NFC] Save cancelled", C_DIM)
                    self._flipper_screen = True
                    return
                if self._input_pending_cat == -7:  # Evil Portal SSID
                    ssid = vals[0].strip() if vals else ""
                    if ssid:
                        self._portal_ssid = ssid
                        self._show_portal_selection("EP")
                    else:
                        self.msg("[EP] No SSID — cancelled", C_DIM)
                        self._attack_mode = ""
                        self._attack_step = ""
                    return
                _, items = MENU_CATS[self._input_pending_cat]
                _hk, name, cmd, state_key, _it = items[self._input_pending_item]
                # Whitelist check for attack targets (BSSID/MAC)
                if _it in ("bssid_ch", "mac") and vals:
                    target = vals[0].upper()
                    if self._whitelist.is_blocked(target):
                        self.msg(f"[WL] {target[-8:]} whitelisted", C_WARNING)
                        return
                self._execute_item(cmd, state_key, name, vals)
            return
        c = self._get_char_input()
        if c and len(field["value"]) < 32:
            field["value"] += c

    def _get_char_input(self) -> str | None:
        ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
        if ctrl:
            return None  # Ctrl+key handled elsewhere, don't produce chars
        # Shift detection: btn() for normal keyboards, sticky for uConsole
        # (USB HID may report shift and letter in separate frames)
        shift_now = pyxel.btn(pyxel.KEY_LSHIFT) or pyxel.btn(pyxel.KEY_RSHIFT)
        if shift_now:
            self._shift_frames = 4  # hold shift state for 4 frames
        elif hasattr(self, '_shift_frames') and self._shift_frames > 0:
            self._shift_frames -= 1
        shift = shift_now or (getattr(self, '_shift_frames', 0) > 0)
        for k in range(pyxel.KEY_A, pyxel.KEY_Z + 1):
            if pyxel.btnp(k):
                c = chr(k)
                return c.upper() if shift else c
        for k in range(pyxel.KEY_0, pyxel.KEY_9 + 1):
            if pyxel.btnp(k):
                if shift:
                    return ")!@#$%^&*("[k - pyxel.KEY_0]
                return chr(k)
        specials = {
            pyxel.KEY_SEMICOLON:     (";", ":"),
            pyxel.KEY_PERIOD:        (".", ">"),
            pyxel.KEY_COMMA:         (",", "<"),
            pyxel.KEY_MINUS:         ("-", "_"),
            pyxel.KEY_SLASH:         ("/", "?"),
            pyxel.KEY_SPACE:         (" ", " "),
            pyxel.KEY_LEFTBRACKET:   ("[", "{"),
            pyxel.KEY_RIGHTBRACKET:  ("]", "}"),
            pyxel.KEY_BACKSLASH:     ("\\", "|"),
        }
        # Optional keys (not in all pyxel versions)
        for attr, pair in [("KEY_APOSTROPHE", ("'", '"')),
                           ("KEY_EQUAL", ("=", "+")),
                           ("KEY_GRAVE_ACCENT", ("`", "~"))]:
            k = getattr(pyxel, attr, None)
            if k is not None:
                specials[k] = pair
        for k, (norm, sh) in specials.items():
            if pyxel.btnp(k):
                return sh if shift else norm
        return None

    # ------------------------------------------------------------------
    # Serial polling + line parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _read_battery() -> int:
        """Read uConsole battery percentage. Returns -1 if unavailable."""
        try:
            with open("/sys/class/power_supply/axp20x-battery/capacity") as f:
                return int(f.read().strip())
        except Exception:
            return -1

    def _poll_serial(self):
        if getattr(self, '_serial_busy', False):
            return
        if not self.serial or not self.serial.is_open:
            if getattr(self, "_shutting_down", False):
                return
            # Auto-reconnect: try every ~3s (90 frames at 30 FPS)
            if not self._esp32 and pyxel.frame_count % 90 == 0:
                if self._try_reconnect_esp32():
                    self.msg("[SYS] ESP32 reconnected!", C_SUCCESS)
            return
        try:
            lines = self.serial.read_available()
        except Exception:
            # USB cable yanked or serial error — mark ESP32 offline
            self._esp32 = False
            self.state.connected = False
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None
            if getattr(self, "_wardrive_selected", None):
                self._wardrive_pause("ESP32 disconnected", request_stop=False)
            self.msg("[ERR] ESP32 disconnected!", C_ERROR)
            self._term_add("[ERR] ESP32 disconnected — plug in & retry", raw=True)
            return
        for line in lines:
            self._handle_serial_line(line)

    def _handle_serial_line(self, line: str):
        s = line.strip()
        if not s:
            return

        # Firmware version detection (from boot banner, version cmd, ping)
        # Match both legacy "JanOS version" and newer "WatchDogsGo version" — the
        # ESP32 projectZero firmware still emits "JanOS" in older builds.
        if not self._fw_version:
            _vm = re.search(r"(?:JanOS|WatchDogsGo) version:\s*v?(\d+\.\d+\.\d+)", s)
            if not _vm:
                _vm = re.search(r"APP_MAIN START \(v?(\d+\.\d+\.\d+)\)", s)
            if not _vm:
                _vm = re.search(r"pong\s+v?(\d+\.\d+\.\d+)", s)
            if _vm:
                self._fw_version = _vm.group(1)
                self._term_add(f"[FW] Firmware v{self._fw_version}", raw=True)
                # Trigger background GitHub check
                threading.Thread(target=self._check_fw_update,
                                 daemon=True).start()

        # Log to loot (handles PCAP/HCCAPX detection internally)
        if self.loot:
            try:
                self.loot.log_serial(s)
            except Exception:
                if getattr(self, "_wardrive_phase", ""):
                    self._wardrive_save_failed = True

        # Detect handshake completion for game event
        if s.startswith("SSID:") and "AP:" in s:
            self._trigger_hs_event()
            return

        # --- AirTag scanner count line: "X,Y" ---
        at_m = _AIRTAG_COUNT_RE.match(s)
        if at_m and self._bt_airtag:
            self._airtag_count = int(at_m.group(1))
            self._smarttag_count = int(at_m.group(2))
            total = self._airtag_count + self._smarttag_count
            parts = []
            if self._airtag_count:
                parts.append(f"AirTag:{self._airtag_count}")
            if self._smarttag_count:
                parts.append(f"SmartTag:{self._smarttag_count}")
            if total > 0:
                self._term_add(
                    f"[TRACKER] {' | '.join(parts)}  total:{total}",
                    raw=True)
                self.msg(f"[TAG] {' '.join(parts)}", C_WARNING)
            return

        # --- BLE device ---
        m = _BLE_RE.match(s)
        if m:
            mac = m.group(1)
            if self._whitelist.is_blocked(mac):
                return  # silently skip whitelisted BLE device
            rssi = int(m.group(2))
            name = (m.group(3) or "").strip() or "?"
            tag_type = (m.group(4) or "").strip()  # [AirTag] or [SmartTag]
            mac_upper = mac.upper()
            is_new = mac_upper not in self._known_ble
            if is_new:
                self._known_ble.add(mac_upper)
                if self.gps_fix:
                    _dlat = (random.random()-0.5) * 0.002
                    _dlon = (random.random()-0.5) * 0.002
                else:
                    _dlat = _dlon = 0.0
                d = BleDevice(
                    self.player_lat + _dlat,
                    self.player_lon + _dlon,
                    mac, name, rssi)
                d.spawn_frame = pyxel.frame_count
                self.ble_devices.append(d)
                self.msg(f"[BLE] {d.name} {mac[-8:]} {rssi}dBm", C_HACK_CYAN)
                self.gain_xp(10)  # new device: full XP
            else:
                self.gain_xp(1)   # duplicate: minimal XP
            # Always save to loot (dedup + RSSI update handled by loot_manager)
            if self.loot:
                try:
                    self.loot.save_bt_device(mac, rssi, name, False, False)
                    self.loot.save_wardriving_bt(mac, rssi, name)
                except Exception:
                    if getattr(self, "_wardrive_phase", "") == "bt_scanning":
                        self._wardrive_save_failed = True
            # Pretty terminal line instead of raw
            new_mark = "*" if is_new else " "
            suffix = f" {tag_type}" if tag_type else ""
            self._term_add(
                f"[BLE]{new_mark} {name[:16]:<16} {mac} {rssi}dBm{suffix}",
                raw=True)
            return

        # --- WiFi network (CSV format) ---
        net = self.net_mgr.parse_network_line(s)
        if net:
            bssid = net.bssid
            if bssid:
                if self._whitelist.is_blocked(bssid):
                    return  # silently skip whitelisted WiFi
                # Keep full Network objects for Evil Twin target selection
                self.state.networks.append(net)
                bssid_upper = bssid.upper()
                is_new = bssid_upper not in self._known_wifi
                if is_new:
                    self._known_wifi.add(bssid_upper)
                    try:
                        ch = int(net.channel)
                    except (ValueError, TypeError):
                        ch = 0
                    try:
                        rssi_v = int(net.rssi)
                    except (ValueError, TypeError):
                        rssi_v = -80
                    if self.gps_fix:
                        _dlat = (random.random()-0.5) * 0.002
                        _dlon = (random.random()-0.5) * 0.002
                    else:
                        _dlat = _dlon = 0.0
                    n = WifiNetwork(
                        self.player_lat + _dlat,
                        self.player_lon + _dlon,
                        bssid, net.ssid, ch, rssi_v)
                    n.spawn_frame = pyxel.frame_count
                    self.wifi_networks.append(n)
                    self.msg(f"[WiFi] {n.ssid} Ch:{ch}", C_WARNING)
                    self.gain_xp(15)  # new network: full XP
                else:
                    self.gain_xp(1)   # duplicate: minimal XP
                # Always save to loot (dedup + RSSI update handled by loot_manager)
                if self.loot:
                    try:
                        self.loot.save_wardriving_network(net)
                    except Exception:
                        if getattr(self, "_wardrive_phase", "") == "wardriving":
                            self._wardrive_save_failed = True
                # Pretty terminal line instead of raw CSV
                tag = "*" if is_new else " "
                ssid = net.ssid[:20] if net.ssid else "<hidden>"
                self._term_add(
                    f"[WiFi]{tag} {ssid:<20} Ch:{net.channel:>2} "
                    f"{net.rssi:>4}dBm {net.auth[:8]:<8} {bssid}",
                    raw=True)
                return

        # --- Operation state from serial output ---
        sl = s.lower()
        self._handle_esp_operation_state(sl)

        # --- Portal / Evil Twin capture parsing ---
        if self.state.portal_running or self.state.evil_twin_running:
            is_form = ("received post" in sl or "form submission" in sl)
            is_client = ("client connected" in sl or "client count" in sl)
            if is_form or is_client:
                tag = "EP" if self.state.portal_running else "ET"
                if self.state.portal_running and self.loot:
                    self.loot.save_portal_event(s)
                if self.state.evil_twin_running and self.loot:
                    self.loot.save_evil_twin_event(s)
                if is_form:
                    # URL-decode captured data for display
                    try:
                        from urllib.parse import unquote_plus
                        decoded = unquote_plus(s)
                    except Exception:
                        decoded = s
                    if self.state.portal_running:
                        self.state.submitted_forms += 1
                    if self.state.evil_twin_running:
                        self.state.evil_twin_captured_data.append(decoded)
                    # Parse POST fields for nice formatting
                    _fields = self._parse_post_fields(decoded)
                    if _fields:
                        self._term_add(f"[{tag}:PWD] {_fields}", raw=True)
                    else:
                        self._term_add(f"[{tag}:PWD] {decoded}", raw=True)
                    self.msg(f"[{tag}] CREDENTIAL CAPTURED!", 12)  # blue
                    self.gain_xp(150)
                    self.glitch_timer = 6
                    self._earn_badge("evil_twin")
                    self._load_loot_totals()  # refresh PWD count
                elif is_client:
                    if "client count" in sl:
                        self.state.portal_client_count = int(
                            re.search(r'\d+', s.split("=")[-1] if "=" in s else s).group()
                        ) if re.search(r'\d+', s) else 0
                    self.msg(f"[{tag}] Client connected", C_HACK_CYAN)
                    self._term_add(f"[{tag}:CLIENT] {s}", raw=True)
                    self.gain_xp(25)
                return  # don't add raw line to terminal again

        # --- Scan cycle completion ---
        # One-shot scans (SCAN tab): mark as done, stop running flag
        if "scan results printed" in sl:
            # Evil Twin scan pending → show network picker
            if self._et_scan_pending:
                self._et_scan_pending = False
                self._wifi_scan_done_time = time.time()
                nets = self.state.networks
                if nets:
                    self._attack_scan_results = list(nets)
                    self._show_net_selection()
                else:
                    self.msg("[ET] No networks found", C_WARNING)
                    self._attack_mode = ""
                    self._attack_step = ""
            elif self._wardrive_complete("wardriving"):
                pass
            elif self._wifi_scan_only:
                self._wifi_scan_only = False
                self._wifi_scan_start_time = 0.0
                self.msg("[WiFi] Scan complete", C_SUCCESS)
            elif self.wifi_scanning:
                # Wardriving auto-repeat
                self._wifi_scan_done_time = time.time()
        if ("summary:" in sl or "scan complete" in sl
                or "ble scan done" in sl or "bt scan done" in sl):
            if getattr(self, "_wardrive_phase", "") == "bt_scanning":
                if "summary:" in sl:
                    self._wardrive_complete("bt_scanning")
            elif self._ble_scan_only:
                self._ble_scan_only = False
                self._bt_scan_start_time = 0.0
                self.msg("[BLE] Scan complete", C_SUCCESS)
            elif self.ble_scanning:
                # Wardriving auto-repeat
                self._bt_scan_done_time = time.time()

        # Add to terminal panel (non-parsed lines)
        self._term_add(s)

    def _handle_esp_operation_state(self, line):
        stopped = "all operations stopped" in line or "all stopped" in line
        if "stop command" in line or stopped:
            # Receipt of "stop" alone is not completion. JanOS 1.7.0 follows
            # it with "All operations stopped."; do not start before that reply.
            if self._esp_stop_waiting:
                if stopped:
                    self._clear_esp_scan_state()
                    self._esp_stop_ack = True
                return
            if stopped:
                self._clear_esp_scan_state(cancel_pending=True)
                self._airtag_count = self._smarttag_count = 0
                if getattr(self, "_wardrive_selected", None):
                    self._wardrive_pause("ESP32 stopped unexpectedly")
        elif "sniffer start" in line or "packet sniffer" in line:
            if getattr(self, "_wardrive_selected", None) and not self._esp_stop_waiting:
                self._cancel_wardrive_plan()
            self._clear_esp_scan_state()
            self.sniffing = True
        elif "handshake" in line and ("start" in line or "captur" in line):
            if getattr(self, "_wardrive_selected", None) and not self._esp_stop_waiting:
                self._cancel_wardrive_plan()
            self._clear_esp_scan_state()
            self.capturing_hs = True

    def _trigger_hs_event(self):
        """Trigger game event when handshake is captured."""
        self._last_hs_count += 1
        self.msg("[HS] Handshake captured!", C_SUCCESS)
        self.gain_xp(200)
        self._earn_badge("handshake_hunter")
        self.glitch_timer = 10
        self.markers.append(MapMarker(
            self.player_lat, self.player_lon, "HS", "handshake"))
        px, py = self.proj.geo_to_screen(self.player_lat, self.player_lon)
        for _ in range(30):
            self.particles.append(Particle(px, py, random.choice([11, 10, 3])))

    # ------------------------------------------------------------------
    # GPS polling
    # ------------------------------------------------------------------

    def _poll_gps(self):
        # UART NMEA can start after the one-time boot probe. Reopen only the
        # configured UART while its power is enabled; never probe ESP32 USB.
        if (not self.gps.available and getattr(self, "_gps_enabled", False)
                and str(getattr(self.gps, "device", "")).startswith("/dev/ttyAMA")):
            now = time.monotonic()
            if now >= getattr(self, "_gps_uart_retry_at", -float("inf")):
                self._gps_uart_retry_at = now + 5.0
                if self.gps._try_open(self.gps.device):
                    self._term_add("[GPS] UART connection restored; waiting for a fix", raw=True)
        if not self.gps.available:
            self._poll_radio_profiles()
            return
        sentences = self.gps.read_available()
        if sentences:
            self.gps.process_sentences(sentences)
        fix = self.gps.fix
        self.gps_sats = fix.satellites
        self.gps_sats_vis = fix.satellites_visible
        if fix.valid and fix.latitude != 0 and fix.longitude != 0:
            # Jitter filter: ignore moves < ~30 m (0.0003°)
            dlat = abs(fix.latitude - self.player_lat)
            dlon = abs(fix.longitude - self.player_lon)
            if dlat > 0.0003 or dlon > 0.0003 or not self.gps_fix:
                self.player_lat = fix.latitude
                self.player_lon = fix.longitude
            self.gps_fix = True
            self._manual_move = False
        else:
            self.gps_fix = False
        self._poll_radio_profiles()

    # ------------------------------------------------------------------
    # LoRa / MeshCore polling + callbacks
    # ------------------------------------------------------------------

    def _assign_missing_mesh_gps(self):
        """Use a fresh receiver fix for contacts awaiting the user's fallback."""
        if not self.loot or time.monotonic() < getattr(self, "_mesh_gps_next", 0): return
        self._mesh_gps_next = time.monotonic() + 2.0
        try:
            result = self.loot.assign_receiver_gps_to_unlocated_contacts()
        except Exception as exc:
            self._term_add(f"[LoRa] Receiver GPS assignment failed: {exc}", raw=True)
            return
        for protocol, changed in result["contacts"].items():
            if not changed: continue
            nodes = self._mc_nodes if protocol == "meshcore" else self._mt_view["nodes"]
            for contact in changed:
                existing = next((n for n in nodes if n["id"] == contact["id"]), None)
                if existing is not None:
                    existing.clear(); existing.update(contact)
                else: nodes.append(contact)
                self._update_mesh_marker(protocol, contact)
            if protocol == "meshtastic": self._mt.set_known_nodes(nodes)
            self._term_add(f"[LoRa] {protocol}: receiver GPS assigned to {len(changed)} contact(s)", raw=True)
        if any(result["contacts"].values()): self._load_loot_totals()

    def _register_mesh_node(self, protocol, node_id, ntype, name, lat, lon, rssi, snr, pubkey=None,
                            receiver_position=None, received_at=None):
        nodes = self._mc_nodes if protocol == "meshcore" else self._mt_view["nodes"]
        known_contact = any(n["id"] == node_id for n in nodes)
        data = {"id": node_id, "protocol": protocol, "type": ntype, "name": name,
                "lat": lat, "lon": lon, "rssi": rssi, "snr": snr or 0,
                "last_seen": received_at if received_at is not None else time.time()}
        if receiver_position is not None:
            data["receiver_position"] = receiver_position
        if pubkey:
            data["pubkey"] = pubkey.hex()
        if self.loot:
            try:
                result = self.loot.register_mesh_contact(protocol, node_id, data, current_xp=self.xp)
            except Exception as exc:
                self._term_add(f"[{protocol}] Contact persistence failed: {exc}", raw=True)
                self.msg("[LoRa] Contact could not be saved; XP unchanged", C_ERROR)
                return False
            data = dict(result["contact"])
            self.xp = result["xp_total"]
            self._xp_dirty = False
        else:
            from .mesh_rules import MESH_NEW_CONTACT_XP
            previous = next((n for n in nodes if n["id"] == node_id), None)
            if previous is None:
                self.gain_xp(MESH_NEW_CONTACT_XP)
            else:
                data = dict(previous, **{k: v for k, v in data.items() if v not in (None, "")})
        existing = next((n for n in nodes if n["id"] == node_id), None)
        if existing is None:
            nodes.append(data)
        else:
            existing.clear()
            existing.update(data)
        self._update_mesh_marker(protocol, data)
        self._load_loot_totals()
        if protocol == "meshtastic":
            self._mt.set_known_nodes(nodes)
            self.state.mt_nodes = len(nodes)
            if self.loot:
                status = "known" if known_contact else "new"
                rssi_text = f"{rssi:.0f}" if isinstance(rssi, (int, float)) else "?"
                snr_text = f"{snr:.1f}" if isinstance(snr, (int, float)) else "?"
                self._term_add(f"[MT] RX {status} {node_id} {data.get('name') or '?'} "
                               f"RSSI:{rssi_text}dBm SNR:{snr_text}dB", raw=True)
        else:
            self.state.mc_nodes = len(nodes)

    def _update_mesh_marker(self, protocol, data):
        from watchdogs.mesh_position import mesh_position
        position = mesh_position(data)
        if position is None: return
        lat, lon = position["lat"], position["lon"]
        key = f"{protocol}:{data['id']}"
        marker = next((m for m in self.markers if getattr(m, "mesh_key", None) == key), None)
        label = data.get("name") or data["id"]
        if position["source"] != "node": label += " [RX GPS]"
        if marker is None:
            marker = MapMarker(lat, lon, label, protocol)
            marker.mesh_key = key
            self.markers.append(marker)
        else:
            marker.lat, marker.lon, marker.label = lat, lon, label

    def _on_mt_node(self, *values):
        from watchdogs.mesh_position import receiver_observation
        values = list(values)
        if len(values) == 7: values.append(None)
        self._mt_event_queue.put(("node", *values,
            receiver_observation(getattr(self, "gps", None)), time.time()))

    def _on_mt_text(self, node_id, name, text, rssi, hops=0):
        # The UI can drain this after a manual profile change. Keep the
        # channel that was active when this packet was received.
        self._mt_event_queue.put(("text", node_id, name, text, rssi, hops,
                                  self._mt.channel_name))

    def _on_mt_status(self, state, error):
        self._mt_event_queue.put(("status", state, error))

    def _on_mt_tx(self, token, success, error):
        self._mt_event_queue.put(("tx", token, success, error))

    def _poll_meshtastic(self):
        while not self._mt.queue.empty():
            text, attr = self._mt.queue.get_nowait()
            self._term_add(f"[MT] {text}", raw=True)
        while not self._mt_event_queue.empty():
            event = self._mt_event_queue.get_nowait()
            if event[0] == "node":
                self._register_mesh_node("meshtastic", *event[1:])
            elif event[0] == "text":
                _, node_id, name, text, rssi, hops, channel = event
                message = f"{name or node_id}: {text}"
                timestamp = time.strftime("%H:%M")
                self._mt_view["log"].append((f"\x10 {message}", C_SUCCESS, f"{timestamp} {hops}hop"))
                self._mc_bubbles.append((message[:40], pyxel.frame_count + 300))
                if self.loot:
                    try:
                        self.loot.save_meshtastic_message(channel, message, rssi)
                    except Exception as exc:
                        self._term_add(f"[MT] Message persistence failed: {exc}", raw=True)
                self.state.mt_messages += 1
                self._load_loot_totals()
                self._play_mc_notify()
                self._earn_badge("meshcore")  # same existing mesh-message achievement
            elif event[0] == "tx":
                _, token, success, error = event
                pending = self._mt_tx_pending.pop(token, None)
                if pending is not None:
                    index, text, channel = pending
                    if index < len(self._mt_view["log"]):
                        entry = self._mt_view["log"][index]
                        self._mt_view["log"][index] = (entry[0], entry[1], "TX" if success else "FAILED")
                    if self.loot:
                        label = "TX" if success else "TX failed"
                        try:
                            self.loot.save_meshtastic_message(channel,
                                f"{self._mt.node_name}: {text} [{label}]", 0, direction="tx")
                        except Exception as exc:
                            self._term_add(f"[MT] Message persistence failed: {exc}", raw=True)
                if not success:
                    self._mt_view["log"].append((f"\x11 TX failed: {error}", C_ERROR))
            elif event[0] == "status":
                _, state, error = event
                current_ready = self._mt.running and self._mt.ready
                if (state in ("stopped", "error") and not current_ready
                        and self._radio_protocol == "meshtastic"):
                    self._radio_protocol = ""
                    self.state.lora_running = False
                    self.state.lora_mode = ""
                elif state == "ready" and current_ready:
                    self._radio_protocol = "meshtastic"
                    self.state.lora_running = True
                    self.state.lora_mode = "meshtastic"
                if error:
                    self._mt_view["log"].append((f"\x11 {error}", C_ERROR))

    @contextmanager
    def _meshtastic_view(self):
        """Reuse the original MeshCore renderer without mixing either history."""
        self._mt_view["channels_list"] = [SimpleNamespace(name=self._mt.channel_name)]
        self._mt_view["node_name"] = self._mt.node_name
        saved = {key: getattr(self, "_mc_" + key) for key in self._mt_view}
        previous = self._mesh_view_protocol
        for key, value in self._mt_view.items():
            setattr(self, "_mc_" + key, value)
        self._mesh_view_protocol = "meshtastic"
        try:
            yield
        finally:
            for key in self._mt_view:
                self._mt_view[key] = getattr(self, "_mc_" + key)
                setattr(self, "_mc_" + key, saved[key])
            self._mesh_view_protocol = previous

    def _update_mt_screen(self):
        with self._meshtastic_view():
            ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
            if ctrl and pyxel.btnp(pyxel.KEY_H):
                self._mc_nodes_panel = not self._mc_nodes_panel
                self._mc_node_action = self._mc_note_editing = False
                return
            if self._mc_nodes_panel:
                self._update_mc_nodes_panel()
                return
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mt_screen = False
                self._esc_consumed_frame = pyxel.frame_count
                return
            if pyxel.btnp(pyxel.KEY_PAGEUP):
                self._mc_scroll = min(self._mc_scroll + 5, max(0, len(self._mc_log) - 5))
            if pyxel.btnp(pyxel.KEY_PAGEDOWN):
                self._mc_scroll = max(0, self._mc_scroll - 5)
            if ctrl:
                if pyxel.btnp(pyxel.KEY_N):
                    self.input_mode = True
                    self.input_fields = [{"label": "Node Name", "value": self._mt.node_name}]
                    self.input_field_idx = 0
                    self._input_pending_cat = -9
                    self._input_pending_item = -1
                elif pyxel.btnp(pyxel.KEY_X):
                    self._mc_log.clear()
                    self._mc_scroll = 0
                return
            if pyxel.btnp(pyxel.KEY_RETURN):
                text = self._mc_input.strip()
                if text:
                    channel = self._mt.channel_name
                    token = self._mt.send_message(text)
                    if token is None:
                        self._mc_log.append((f"\x11 {self._mt.last_error or 'Meshtastic is OFF'}", C_ERROR))
                    else:
                        index = len(self._mc_log)
                        self._mc_log.append((f"\x11 {self._mt.node_name}: {text}", C_WARNING, "QUEUED"))
                        self._mt_tx_pending[token] = (index, text, channel)
                        self._mc_input = ""
                        self._mc_scroll = 0
                return
            if pyxel.btnp(pyxel.KEY_BACKSPACE):
                self._mc_input = self._mc_input[:-1]
                return
            character = self._get_char_input()
            if character and len(self._mc_input) < 120:
                self._mc_input += character

    def _draw_mt_screen(self):
        with self._meshtastic_view():
            self._draw_mc_screen()

    def _poll_lora(self):
        self._poll_meshtastic()
        active = self._active_lora_protocol()
        self.state.lora_running = bool(active)
        self.state.lora_mode = active
        try:
            while not self._lora.queue.empty():
                text, attr = self._lora.queue.get_nowait()
                # Telemetry goes to terminal only, not chat
                self._term_add(f"[LoRa] {text}", raw=True)
        except Exception:
            pass
        # Process thread-safe event queue from callbacks
        try:
            while not self._mc_event_queue.empty():
                evt = self._mc_event_queue.get_nowait()
                if evt[0] == "msg":
                    _, channel, message, rssi, hops = evt
                    ts = time.strftime("%H:%M")
                    self._mc_log.append((f"\x10 {message}", C_SUCCESS, f"{ts} {hops}hop"))
                    self._mc_bubbles.append(
                        (message[:40], pyxel.frame_count + 300))
                    self.msg(f"[MC] {message[:30]}", C_SUCCESS)
                    # Play notification sound
                    self._play_mc_notify()
                    self._earn_badge("meshcore")
                elif evt[0] == "dm":
                    _, from_id, message, rssi, hops = evt
                    ts = time.strftime("%H:%M")
                    # Find sender name
                    sender = next(
                        (n["name"] for n in self._mc_nodes
                         if n["id"] == from_id), from_id[:8])
                    self._mc_log.append(
                        (f"\x10 [DM] {sender}: {message}",
                         12, f"{ts} {hops}hop"))  # 12 = blue
                    self._mc_bubbles.append(
                        (f"DM: {message[:30]}", pyxel.frame_count + 300))
                    self.msg(f"[DM] {sender}: {message[:25]}", 12)
                    self._play_mc_notify()
                elif evt[0] == "dm_ack":
                    _, ack_hash = evt
                    idx = self._mc_dm_ack_map.pop(ack_hash, None)
                    if idx is not None and idx < len(self._mc_log):
                        entry = self._mc_log[idx]
                        old_tag = entry[2] if len(entry) > 2 else ""
                        self._mc_log[idx] = (entry[0], entry[1],
                                             f"{old_tag} \u221a")
                elif evt[0] == "tx_confirm":
                    _, dk = evt
                    idx = self._mc_tx_pending.pop(dk, None)
                    if idx is not None and idx < len(self._mc_log):
                        entry = self._mc_log[idx]
                        # Replace tag with √ + timestamp
                        old_tag = entry[2] if len(entry) > 2 else ""
                        self._mc_log[idx] = (entry[0], entry[1],
                                             f"{old_tag} \u221a")
                elif evt[0] == "node":
                    _, node_id, ntype, name, lat, lon, rssi, snr, pubkey, rx_position, received_at = evt
                    self._register_mesh_node("meshcore", node_id, ntype, name, lat, lon, rssi, snr, pubkey,
                                             rx_position, received_at)
                    # Node adverts go to terminal only, not chat
                    _type_names = {0: "Client", 1: "Client",
                                   2: "Repeater", 3: "Room", 4: "Sensor"}
                    tname = _type_names.get(ntype, f"T{ntype}")
                    self.msg(f"[MC] {name or '?'} [{tname}] "
                             f"RSSI:{rssi:.0f}", C_HACK_CYAN)
        except Exception:
            pass
        # Prune expired bubbles
        frame = pyxel.frame_count
        self._mc_bubbles = [(t, e) for t, e in self._mc_bubbles if e > frame]

    def _poll_reticulum(self):
        """Drain WDGWars LXMF events from the background thread."""
        if self._wd is None:
            return
        try:
            while not self._wd_event_queue.empty():
                evt = self._wd_event_queue.get_nowait()
                kind = evt[0]
                if kind == "msg":
                    _, nick, text = evt
                    ts = time.strftime("%H:%M")
                    self._wd_log.append((f"\x10 {nick}: {text}", C_SUCCESS, ts))
                    self.msg(f"[WD] {nick}: {text[:28]}", C_SUCCESS)
                    self._play_mc_notify()
                elif kind in ("delivered", "failed"):
                    _, token = evt
                    idx = self._wd_tx_pending.pop(token, None)
                    if idx is not None and idx < len(self._wd_log):
                        entry = self._wd_log[idx]
                        old_tag = entry[2] if len(entry) > 2 else ""
                        mark = "√" if kind == "delivered" else "✗"
                        self._wd_log[idx] = (entry[0], entry[1],
                                             f"{old_tag} {mark}".strip())
                elif kind == "status":
                    _, text = evt
                    self._wd_log.append((f"\x11 {text}", C_HACK_CYAN))
        except Exception:
            pass

    def _on_wd_message(self, nick, text):
        """Callback from Reticulum thread — enqueue inbound #general message."""
        self._wd_event_queue.put(("msg", nick, text))
        if self.loot:
            try:
                self.loot.save_wdgwars_message(nick, text)
            except Exception:
                pass

    def _on_wd_delivered(self, token):
        self._wd_event_queue.put(("delivered", token))

    def _on_wd_failed(self, token):
        self._wd_event_queue.put(("failed", token))

    def _on_wd_status(self, text):
        self._wd_event_queue.put(("status", text))

    def _wd_toggle_lora(self):
        """Toggle the RNode-compatible LoRa transport for WDGWars (Ctrl+L).

        One radio = one mode — enabling LoRa stops MeshCore."""
        if self._wd is None:
            return
        if self._wd.lora_active:
            self._wd.disable_lora()
            protocol = getattr(self, "_before_rnode_protocol", "meshcore")
            if self._lora_enabled:
                resumed = self._switch_lora_protocol(protocol)
            else:
                resumed = False
            self._wd_log.append(
                (f"\x11 Switched to TCP — {protocol if resumed else 'LoRa OFF'}", C_HACK_CYAN))
            return
        if not self._lora_enabled:
            self._wd_log.append(
                ("\x10 Enable LoRa hardware first (SYSTEM > LoRa)", C_WARNING))
            return
        self._before_rnode_protocol = self._active_lora_protocol() or "meshcore"
        if not self._stop_radio_owner():
            return
        self._wd_log.append(
            ("\x11 Switching to LoRa-only (TCP off, stops mesh radio). "
             "Needs gateway in range.", C_WARNING))
        self._wd.enable_lora(self._lora)

    def _on_mc_node(self, node_id, ntype, name, lat, lon, rssi, snr,
                    pubkey=None):
        """Callback from LoRaManager background thread — enqueue for main."""
        from watchdogs.mesh_position import receiver_observation
        self._mc_event_queue.put(
            ("node", node_id, ntype, name, lat, lon, rssi, snr, pubkey,
             receiver_observation(getattr(self, "gps", None)), time.time()))

    _mc_sound_loaded = False

    def _play_subghz_beep(self):
        """Subtle SubGHz beep - distinct from the mesh notify sound."""
        if getattr(self, "_muted", False):
            return
        if not getattr(self, "_subghz_beep_loaded", False):
            try:
                pyxel.sounds[62].set("e3g3", "t", "3", "f", 22)
                self._subghz_beep_loaded = True
            except Exception:
                return
        try:
            pyxel.play(3, 62, loop=False)
        except Exception:
            pass

    def _play_mc_notify(self):
        """Play MeshCore notification sound via pyxel audio."""
        if getattr(self, "_muted", False):
            return
        if not self._mc_sound_loaded:
            _project_root = Path(__file__).resolve().parent.parent
            wav = _project_root / "assets" / "meshcore_notify.wav"
            if not wav.exists():
                return
            try:
                pyxel.sounds[63].pcm(str(wav))
                self._mc_sound_loaded = True
            except Exception:
                return
        try:
            pyxel.play(3, 63, loop=False)
        except Exception:
            pass

    def _play_svc_sound(self, band):
        """Play the per-service alert WAV (fire/ambulance/police, else warning)."""
        if getattr(self, "_muted", False):
            return
        fname, slot = EMERGENCY_SND.get(band, EMERGENCY_SND_DEFAULT)
        try:
            if slot not in self._svc_snd_loaded:
                wav = Path(__file__).resolve().parent.parent / "assets" / fname
                if not wav.exists():
                    return self._play_emergency_siren()   # fallback to synth
                pyxel.sounds[slot].pcm(str(wav))
                self._svc_snd_loaded.add(slot)
            pyxel.play(3, slot, loop=False)
        except Exception:
            pass

    def _play_emergency_siren(self):
        """Distinctive two-tone 'nee-naw' police siren (pyxel synth) — fallback
        if a per-service WAV is missing."""
        if getattr(self, "_muted", False):
            return
        try:
            if not getattr(self, "_emerg_snd_loaded", False):
                # hi-lo alternation, pulse wave, slight vibrato -> siren feel
                pyxel.sounds[62].set("a3a3a3a3d3d3d3d3", "p", "6", "v", 22)
                self._emerg_snd_loaded = True
            pyxel.play(3, 62, loop=False)
        except Exception:
            pass

    def _on_mc_message(self, channel, message, rssi, hops=0):
        """Callback from LoRaManager background thread — enqueue for main."""
        self._mc_event_queue.put(("msg", channel, message, rssi, hops))
        if self.loot:
            try:
                self.loot.save_meshcore_message(channel, message, rssi)
            except Exception:
                pass

    def _on_mc_dm(self, from_id, message, rssi, hops=0):
        """Callback: incoming DM addressed to us (dedup by content)."""
        import time as _time
        key = f"dm:{from_id}:{message}"
        now = _time.time()
        last = getattr(self, '_dm_dedup', {}).get(key, 0)
        if now - last < 30:
            return  # duplicate DM within 30s
        if not hasattr(self, '_dm_dedup'):
            self._dm_dedup = {}
        self._dm_dedup[key] = now
        # Prune old entries
        if len(self._dm_dedup) > 50:
            self._dm_dedup = {k: v for k, v in self._dm_dedup.items()
                              if now - v < 60}
        self._mc_event_queue.put(("dm", from_id, message, rssi, hops))

    def _on_mc_dm_ack(self, ack_hash):
        """Callback: ACK received for our sent DM."""
        self._mc_event_queue.put(("dm_ack", ack_hash))

    def _on_mc_tx_confirm(self, dedup_key):
        """Callback: our TX packet was retransmitted by another node."""
        self._mc_event_queue.put(("tx_confirm", dedup_key))

    # ------------------------------------------------------------------
    # Loot points refresh
    # ------------------------------------------------------------------

    def _refresh_loot_points(self):
        if not self.loot:
            return
        try:
            pts = self.loot.get_gps_points()
            if pts:
                self.loot_points = pts
                self._cluster_zoom = -1  # force re-cluster
        except Exception:
            pass
        # Load cracked passwords from potfile (for map popup)
        try:
            loot_dir = Path(self._app_dir) / "loot"
            pwd_data = upload_manager.load_wpasec_passwords(loot_dir)
            self._cracked_ssids = {
                ssid: entries[0]["password"]
                for ssid, entries in pwd_data.get("by_ssid", {}).items()
            }
        except Exception:
            pass
        self._load_loot_totals()

    def _load_loot_totals(self):
        if not self.loot:
            return
        try:
            t = self.loot.loot_totals
            self._loot_totals = {
                "sessions":    t.get("sessions", 0),
                "wifi":        t.get("wardriving", 0),
                "bt":          t.get("bt_devices", 0),
                "hs":          t.get("hc22000", 0),
                "pcap":        t.get("pcap", 0),
                "passwords":   t.get("passwords", 0),
                "et_captures": t.get("et_captures", 0),
                "mc_nodes":    t.get("mc_nodes", 0),
                "mc_msgs":     t.get("mc_messages", 0),
                "mt_nodes":    t.get("mt_nodes", 0),
                "mt_msgs":     t.get("mt_messages", 0),
                "emergency":   t.get("emergency", 0),
            }
        except Exception:
            pass

    def _scan_loot_dir(self, base: Path) -> list[dict]:
        """Read GPS points from any loot base directory (JanOS-compatible format)."""
        import csv as _csv
        points: list[dict] = []
        for session_dir in base.iterdir():
            if not session_dir.is_dir():
                continue
            # wardriving.csv (WiGLE format — skip 2 header lines)
            wd = session_dir / "wardriving.csv"
            if wd.is_file():
                try:
                    with open(wd, encoding="utf-8") as fh:
                        for i, line in enumerate(fh):
                            if i <= 1:
                                continue
                            p = line.strip().split(",")
                            if len(p) < 8:
                                continue
                            try:
                                lat, lon = float(p[6]), float(p[7])
                                if lat == 0.0 and lon == 0.0:
                                    continue
                                ptype = "wifi"
                                if len(p) >= 11 and p[10].strip().upper() == "BLE":
                                    ptype = "bt"
                                points.append({"lat": lat, "lon": lon,
                                               "type": ptype, "label": p[1]})
                            except (ValueError, IndexError):
                                pass
                except OSError:
                    pass
            # handshakes/*.gps.json
            hs_dir = session_dir / "handshakes"
            if hs_dir.is_dir():
                for gps_file in hs_dir.glob("*.gps.json"):
                    try:
                        import json as _json
                        data = _json.loads(gps_file.read_text())
                        lat = data.get("Latitude", data.get("lat", 0))
                        lon = data.get("Longitude", data.get("lon", 0))
                        if lat and lon and not (lat == 0.0 and lon == 0.0):
                            label = gps_file.stem.replace(".gps", "").split("_")[0]
                            points.append({"lat": float(lat), "lon": float(lon),
                                           "type": "handshake", "label": label[:20]})
                    except Exception:
                        pass
            for protocol in ("meshcore", "meshtastic"):
                nodes_file = session_dir / f"{protocol}_nodes.csv"
                if not nodes_file.is_file():
                    continue
                try:
                    with nodes_file.open(encoding="utf-8", newline="") as fh:
                        for row in _csv.DictReader(fh):
                            try:
                                lat, lon = float(row["lat"]), float(row["lon"])
                                if not math.isfinite(lat) or not math.isfinite(lon):
                                    continue
                                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                                    continue
                                if lat == lon == 0:
                                    continue
                                points.append({"lat": lat, "lon": lon, "type": protocol,
                                               "label": row.get("name") or row.get("node_id", "")})
                            except (KeyError, ValueError, TypeError):
                                continue
                except OSError:
                    pass
        return points

    # ------------------------------------------------------------------
    # Hack mechanic
    # ------------------------------------------------------------------

    def _update_hack(self):
        if pyxel.btn(pyxel.KEY_SPACE) and not self.menu_open:
            if not self.hacking:
                best, best_d = None, 999
                px, py = W // 2, HUD_TOP + MAP_H // 2
                for d in self.ble_devices + self.wifi_networks:
                    if d.hacked: continue
                    sx, sy = self.proj.geo_to_screen(d.lat, d.lon)
                    dist = math.hypot(sx - px, sy - py)
                    if dist < 40 and dist < best_d:
                        best, best_d = d, dist
                if best:
                    self.hacking, self.hack_target, self.hack_progress = True, best, 0
            elif self.hack_target:
                self.hack_progress += 1
                if pyxel.frame_count % 3 == 0: self.glitch_timer = 2
                if self.hack_progress >= 45:
                    self.hack_target.hacked = True
                    self.hacking = False
                    self.gain_xp(50)
                    name = getattr(self.hack_target, "name",
                                   getattr(self.hack_target, "ssid", "?"))
                    self.msg(f"[PWNED] {name}", C_SUCCESS)
                    sx, sy = self.proj.geo_to_screen(
                        self.hack_target.lat, self.hack_target.lon)
                    for _ in range(20):
                        self.particles.append(Particle(sx, sy, C_SUCCESS))
                    self.hack_target = None
        else:
            if self.hacking and self.hack_progress < 45:
                self.hacking, self.hack_target = False, None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup(self):
        """Stop this game's producers, persist final data, then release its power."""
        if getattr(self, "_cleanup_done", False) or getattr(self, "_cleanup_running", False):
            return
        self._cleanup_running = self._shutting_down = True
        self._muted = True
        self._map_download_cancel = True
        self._gps_wait = self._gps_wait_dialog = False
        self._pending_cmd = None
        blocked = set()
        flashing = getattr(self, "_flash_running", False)
        power = getattr(self, "_aio_power", None)

        def step(label, action):
            try:
                if action() is not False:
                    return True
                self._cleanup_errors.append(label)
                logging.getLogger(__name__).warning("Shutdown %s did not release its resource", label)
                return False
            except Exception as exc:
                self._cleanup_errors.append(label)
                logging.getLogger(__name__).warning("Shutdown %s failed: %s", label, exc)
                return False

        def stop(name, method="stop"):
            manager = getattr(self, name, None)
            return manager is None or step(name, lambda: getattr(manager, method)())

        try:
            if (not getattr(self, "_gameupd_closed_for_install", False)
                    and getattr(self, "_gameupd_plan", None) is not None):
                step("staged update download", self._gameupd_discard_plan)
            if power is not None and not flashing:
                step("power quiesce", power.begin_shutdown)
            if hasattr(self, "_wardrive_selected"):
                step("wardrive cancellation", self._cancel_wardrive_plan)
            # Never reconnect or schedule a new scan while flushing the current one.
            serial = getattr(self, "serial", None)
            if serial is not None and serial.is_open and not flashing:
                self._esp_stop_ack = False
                self._esp_stop_waiting = True
                self._esp_stop_timed_out = False
                self._esp_stop_requested_at = time.monotonic()
                wrote_stop = step("ESP32 stop", serial.request_stop_for_shutdown)
                deadline = time.monotonic() + 5.0
                while wrote_stop and time.monotonic() < deadline:
                    if not step("final serial data", self._poll_serial):
                        break
                    if getattr(self, "_esp_stop_ack", False):
                        break
                    time.sleep(0.02)
                if wrote_stop and not self._esp_stop_ack:
                    self._cleanup_errors.append("ESP32 stop acknowledgement")
                    logging.getLogger(__name__).warning("ESP32 final STOP acknowledgement timed out; received data will be saved before owned USB power-off")

            for name in ("_dragon_drain", "_mitm", "_blueducky", "_race"):
                manager = getattr(self, name, None)
                active = manager is not None and (getattr(manager, "running", False)
                    or any(getattr(manager, key, None) is not None
                           for key in ("_audio_proc", "_tcpdump_proc"))
                    or any(thread is not None and thread.is_alive()
                           for thread in (getattr(manager, key, None)
                                          for key in ("_thread", "_spoof_thread", "_sniff_thread"))))
                if active and not stop(name):
                    blocked.add("usb")
            # Reticulum shutdown must detach LoRa without restoring a TCP path.
            if not stop("_wd"):
                blocked.add("lora")
            for name in ("_mt", "_lora"):
                if not stop(name):
                    blocked.add("lora")
            if not stop("_sdr"):
                blocked.update(("sdr", "usb"))
            for name, method in (("_subghz", "shutdown"), ("_flipper", "close"),
                                 ("_watch", "disconnect"), ("_prox", "shutdown")):
                if not stop(name, method):
                    blocked.add("usb")

            # Producers are stopped; their last node/TX/data events still count.
            if hasattr(self, "_mt_event_queue") and hasattr(self, "_mc_event_queue"):
                step("final mesh data", self._poll_lora)
            if hasattr(self, "_sdr"):
                step("final SDR data", self._poll_sdr)
            if serial is not None and not step("serial close", serial.close):
                blocked.add("usb")
            if not stop("gps", "close"):
                blocked.add("gps")
            for plugin in getattr(self, "_plugins", ()):
                step("plugin unload", plugin.on_unload)
            if getattr(self, "loot", None) is not None:
                step("XP save", self._save_xp_if_dirty)
                if not step("loot flush", self.loot.close):
                    blocked.add("usb")
            if flashing:
                # A native window close/crash must not cut power to esptool.
                logging.getLogger(__name__).warning("Shutdown leaves power on: firmware flashing is in progress")
            elif power is not None:
                results = power.shutdown(blocked_features=blocked)
                for feature, released in results.items():
                    if not released:
                        self._cleanup_errors.append(feature + " power")
            logging.getLogger(__name__).info("Shutdown complete; unreleased resources: %s", self._cleanup_errors)
        finally:
            self._cleanup_running = False
            self._cleanup_done = True

    # ------------------------------------------------------------------
    # Draw
    # ------------------------------------------------------------------

    def _draw_plugin_pin(self):
        """Draw auth PIN popup from plugins (on top of everything)."""
        for p in self._plugins:
            if hasattr(p, '_auth_pin_active') and p._auth_pin_active():
                p._draw_auth_pin()

    def draw(self):
        self._draw_inner()
        if not getattr(self, "_gameupd_closed_for_install", False):
            self._draw_plugin_pin()

    def _draw_inner(self):
        if getattr(self, "_gameupd_closed_for_install", False):
            pyxel.cls(0)
            self._draw_gameupd_screen()
            return
        if self._boot_phase:
            self._draw_boot_screen()
            return
        if self._mitm_screen:
            self._draw_mitm_screen()
            if self.input_mode:
                self._draw_input_dialog()
            self._draw_mc_toast()
            return
        for p in self._plugins:
            if getattr(p, 'overlay_active', False):
                p.draw(0, 0, 640, 360)
                self._draw_mc_toast()
                return
        if self._watch_screen:
            self._draw_watch_screen()
            self._draw_mc_toast()
            return
        if self._wl_screen:
            self._draw_wl_screen()
            if self.input_mode:
                self._draw_input_dialog()
            self._draw_mc_toast()
            return
        if self._lora_protocol_picker:
            self._draw_lora_protocol_picker()
            return
        if self._mt_profile_screen:
            self._draw_mt_profile_picker()
            return
        if self._mt_screen:
            self._draw_mt_screen()
            return
        if self._mc_screen:
            self._draw_mc_screen()
            return
        if self._wd_screen:
            self._draw_wd_screen()
            return
        if self._mc_region_screen:
            self._draw_mc_region_picker()
            return
        if self._prox_screen:
            self._draw_prox_screen()
            self._draw_mc_toast()
            return
        if self.loot_screen:
            self._draw_loot_screen()
            self._draw_mc_toast()
            return
        if self._flipper_screen:
            self._draw_flipper_screen()
            self._draw_mc_toast()
            return
        if self._subghz_screen:
            self._draw_subghz_screen()
            self._draw_mc_toast()
            return
        tiles_drawn = False
        if self.tile_renderer and self.tile_renderer.has_tiles():
            pyxel.cls(0)  # black bg for dark tiles
            tiles_drawn = self.tile_renderer.draw(
                self.proj, W, H, HUD_TOP, TERM_Y)
        if not tiles_drawn:
            pyxel.cls(0)  # dark bg even without tiles
        if not tiles_drawn:
            self._draw_coastlines()
        self._draw_grid()
        self._draw_ambient_motes()
        self._draw_loot_points()
        self._draw_wifi()
        self._draw_ble()
        self._draw_scan_fx()
        self._draw_player()
        # Radar-style overlays go on top of the player skull so they're
        # visible even at low zoom, where every source within the dongle
        # range lands within the 12-pixel ring around the centre.
        self._draw_aircraft()
        self._draw_sensors()
        self._draw_markers()
        self._draw_particles()
        self._draw_hack_bar()
        self._draw_glitch()
        self._draw_terminal()
        self._draw_hud_top()
        self._draw_hud_bottom()
        self._draw_prox_icon()
        self._draw_messages()
        self._draw_radar()
        self._draw_emergency_alert()
        self._draw_scanlines()
        if self._cluster_popup:
            self._draw_cluster_popup()
        # Cluster mode hint (above terminal, visible on map)
        if not self.menu_open and not self._cluster_popup and self._clusters:
            if self._cluster_sel >= 0:
                cl = self._clusters[self._cluster_sel] if self._cluster_sel < len(self._clusters) else None
                n = cl["count"] if cl else 0
                hint = f"[C]exit  [arrows]nav  [RET]open  ({n} APs)"
                pyxel.text(W - len(hint) * 4 - 4, TERM_Y - 8, hint, C_HACK_CYAN)
            else:
                hint = f"[C] Cluster Select  ({len(self._clusters)})"
                pyxel.text(W - len(hint) * 4 - 4, TERM_Y - 8, hint, C_TEXT)
        if self.menu_open:
            self._draw_menu()
            self._draw_menu_hacker()
        if self._et_net_screen:
            self._draw_net_picker()
        if self._portal_select_screen:
            self._draw_portal_picker()
        if getattr(self, '_portal_data_screen', False):
            self._draw_captured_data()
        if getattr(self, '_flash_screen', False):
            self._draw_flash_screen()
        if getattr(self, '_gameupd_screen', False):
            self._draw_gameupd_screen()
        if self.input_mode:
            self._draw_input_dialog()
        if self.confirm_quit:
            self._draw_confirm_quit()
        if self._gps_wait_dialog:
            self._draw_gps_wait_dialog()
        if getattr(self, "_game_update_notice", None) is not None:
            self._draw_game_update_notice()

    def _draw_coastlines(self):
        vl = self.proj.center_lat - self.proj.lat_span
        vh = self.proj.center_lat + self.proj.lat_span
        wl = self.proj.center_lon - self.proj.lon_span
        wh = self.proj.center_lon + self.proj.lon_span
        hi_zoom = self.proj.zoom >= 5
        geo2scr = self.proj.geo_to_screen
        visible = self.proj.screen_visible
        W_THRESH = W * 0.8
        for seg, bounds in zip(self._coastlines, self._coast_bounds):
            if bounds is None: continue
            min_lat, max_lat, min_lon, max_lon, antimerid = bounds
            if max_lat < vl or min_lat > vh: continue
            if not antimerid and (max_lon < wl or min_lon > wh): continue
            psx, psy = None, None
            for lat, lon in seg:
                sx, sy = geo2scr(lat, lon)
                if psx is not None and abs(sx - psx) < W_THRESH:
                    pyxel.line(psx, psy, sx, sy, C_LAND)
                if hi_zoom and visible(sx, sy):
                    pyxel.pset(sx, sy, C_COAST)
                psx, psy = sx, sy

    def _draw_grid(self):
        if self.proj.zoom < 4: return
        sp = max(0.01, self.proj.lon_span / 8)
        for e in [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30]:
            if e >= sp: sp = e; break
        lat = int((self.proj.center_lat - self.proj.lat_span) / sp) * sp
        while lat < self.proj.center_lat + self.proj.lat_span:
            _, sy = self.proj.geo_to_screen(lat, 0)
            if HUD_TOP < sy < TERM_Y:
                pyxel.line(0, sy, W-1, sy, C_GRID)
            lat += sp
        lon = int((self.proj.center_lon - self.proj.lon_span) / sp) * sp
        while lon < self.proj.center_lon + self.proj.lon_span:
            sx, _ = self.proj.geo_to_screen(0, lon)
            if 0 < sx < W:
                pyxel.line(sx, HUD_TOP, sx, TERM_Y, C_GRID)
            lon += sp

    # ------------------------------------------------------------------
    # Map clustering
    # ------------------------------------------------------------------

    def _update_clusters(self):
        """Recompute clusters when zoom/pan changes.

        Uses a fixed screen-space radius (CLUSTER_PX).  As the user zooms
        in, geo_to_screen spreads points further apart so clusters split
        naturally — just like Leaflet MarkerCluster.
        """
        cur_center = (round(self.proj.center_lat, 4),
                      round(self.proj.center_lon, 4))
        if (self.proj.zoom == self._cluster_zoom
                and self._cluster_center == cur_center
                and pyxel.frame_count - self._cluster_frame < 30):
            return
        self._cluster_zoom = self.proj.zoom
        self._cluster_center = cur_center
        self._cluster_frame = pyxel.frame_count

        CLUSTER_PX = 30  # constant screen-space merge radius (pixels)
        R2 = CLUSTER_PX * CLUSTER_PX

        # Grid-based clustering — O(n) amortised.
        # Points are NOT copied; original dicts are referenced directly.
        grid: dict[tuple[int, int], list[int]] = {}
        clusters: list[dict] = []
        geo2scr = self.proj.geo_to_screen
        vis = self.proj.screen_visible

        for pt in self.loot_points:
            sx, sy = geo2scr(pt["lat"], pt["lon"])
            if not vis(sx, sy):
                continue
            gcx, gcy = sx // CLUSTER_PX, sy // CLUSTER_PX
            merged = False
            for dx in (-1, 0, 1):
                if merged:
                    break
                for dy in (-1, 0, 1):
                    for ci in grid.get((gcx + dx, gcy + dy), ()):
                        cl = clusters[ci]
                        ddx = sx - cl["x"]
                        ddy = sy - cl["y"]
                        if ddx * ddx + ddy * ddy <= R2:
                            n = cl["count"]
                            cl["x"] = (cl["x"] * n + sx) // (n + 1)
                            cl["y"] = (cl["y"] * n + sy) // (n + 1)
                            cl["points"].append(pt)
                            cl["_sx"].append(sx)
                            cl["_sy"].append(sy)
                            cl["count"] = n + 1
                            merged = True
                            break
            if not merged:
                clusters.append({
                    "x": sx, "y": sy,
                    "points": [pt], "_sx": [sx], "_sy": [sy],
                    "count": 1,
                })
                grid.setdefault((gcx, gcy), []).append(len(clusters) - 1)

        for cl in clusters:
            pts = cl["points"]
            if cl["count"] == 1:
                cl["color"] = (C_HACK_CYAN
                               if pts[0].get("type") == "bt" else C_SUCCESS)
                cl["radius"] = 0
            else:
                wifi = sum(1 for p in pts if p.get("type") == "wifi")
                bt = sum(1 for p in pts if p.get("type") == "bt")
                cl["color"] = C_HACK_CYAN if bt > wifi else C_SUCCESS
                cl["radius"] = min(5 + cl["count"] // 3, 12)

        self._clusters = clusters

    @staticmethod
    def _cluster_color(points: list[dict]) -> int:
        """Dominant color for a cluster: wifi=green, bt=cyan."""
        wifi = sum(1 for p in points if p.get("type") == "wifi")
        bt = sum(1 for p in points if p.get("type") == "bt")
        return C_HACK_CYAN if bt > wifi else C_SUCCESS

    def _draw_loot_points(self):
        self._update_clusters()
        zoom = self.proj.zoom
        for idx, cl in enumerate(self._clusters):
            selected = (idx == self._cluster_sel)
            c = cl["color"]
            if cl["count"] == 1:
                # Single point — small dot, label at high zoom
                pt = cl["points"][0]
                if zoom >= 8:
                    pyxel.circ(cl["x"], cl["y"], 2, c)
                    if zoom >= 10:
                        pyxel.text(cl["x"] + 4, cl["y"] - 2,
                                   pt.get("label", "")[:16], c)
                elif zoom >= 3:
                    pyxel.rect(cl["x"], cl["y"], 2, 2, c)
                else:
                    pyxel.pset(cl["x"], cl["y"], c)
                # Selection ring for single points
                if selected:
                    pyxel.circb(cl["x"], cl["y"], 5, C_TEXT)
                    if pyxel.frame_count % 20 < 14:
                        pyxel.circb(cl["x"], cl["y"], 7, C_HACK_CYAN)
            else:
                # Cluster bubble — radius scales with count
                r = cl["radius"]
                pyxel.circ(cl["x"], cl["y"], r, c)
                pyxel.circb(cl["x"], cl["y"], r, 0)
                txt = str(cl["count"])
                # Center label — 5px per glyph halved
                tx = cl["x"] - (len(txt) * 5) // 2
                pyxel.text(tx, cl["y"] - 3, txt, 0)
                # Ambient radiating pulse — a soft ring breathes out of each
                # cluster so data nodes read as alive; staggered by index so
                # they don't all beat in lockstep.
                _cp = (pyxel.frame_count + idx * 13) % 64
                if _cp < 21:
                    _pr = r + 2 + _cp // 7
                    _pc = c if _cp < 8 else (5 if _cp < 14 else 1)
                    pyxel.circb(cl["x"], cl["y"], _pr, _pc)
                # Selection ring for clusters
                if selected:
                    pyxel.circb(cl["x"], cl["y"], r + 3, C_TEXT)
                    if pyxel.frame_count % 20 < 14:
                        pyxel.circb(cl["x"], cl["y"], r + 5, C_HACK_CYAN)

    # ------------------------------------------------------------------
    # Cluster popup (mouse click)
    # ------------------------------------------------------------------

    def _classify_auth(self, auth: str, ssid: str) -> tuple[str, int]:
        """Return (type_label, color) for an AP based on auth + potfile."""
        if ssid and ssid in self._cracked_ssids:
            return "Cracked", C_SUCCESS
        if not auth or auth == "[ESS]":
            return "Open", C_WARNING
        if auth == "[BLE]":
            return "BLE", C_HACK_CYAN
        return "Secured", C_ERROR

    def _find_nearest_cluster(self, dx: int, dy: int) -> int:
        """Find nearest cluster in given direction from current selection.

        dx/dy: -1, 0, +1 for direction (LEFT/RIGHT, UP/DOWN).
        Returns cluster index or -1.
        """
        if not self._clusters:
            return -1
        if self._cluster_sel < 0 or self._cluster_sel >= len(self._clusters):
            # No selection — pick cluster closest to screen center
            cx, cy = W // 2, (HUD_TOP + TERM_Y) // 2
            best, bd = 0, 999999
            for i, cl in enumerate(self._clusters):
                d = (cl["x"] - cx) ** 2 + (cl["y"] - cy) ** 2
                if d < bd:
                    bd, best = d, i
            return best
        cur = self._clusters[self._cluster_sel]
        best, bd = -1, 999999
        for i, cl in enumerate(self._clusters):
            if i == self._cluster_sel:
                continue
            # Direction filter — only consider clusters in the pressed direction
            ok = True
            if dx < 0 and cl["x"] >= cur["x"]:
                ok = False
            if dx > 0 and cl["x"] <= cur["x"]:
                ok = False
            if dy < 0 and cl["y"] >= cur["y"]:
                ok = False
            if dy > 0 and cl["y"] <= cur["y"]:
                ok = False
            if not ok:
                continue
            d = (cl["x"] - cur["x"]) ** 2 + (cl["y"] - cur["y"]) ** 2
            if d < bd:
                bd, best = d, i
        return best if best >= 0 else self._cluster_sel

    def _handle_cluster_nav(self):
        """Navigate clusters with C + arrow keys, ENTER opens popup.

        C key: toggle cluster navigation mode (select nearest / deselect).
        Arrows: jump between clusters when one is selected.
        ENTER: open popup for selected cluster.
        ESC: exit cluster selection.
        """
        if not self._clusters:
            self._cluster_sel = -1
            return
        # Clamp selection after clusters were recomputed
        if self._cluster_sel >= len(self._clusters):
            self._cluster_sel = -1

        # C key — enter or exit cluster navigation
        if pyxel.btnp(pyxel.KEY_C) and not self._attack_mode:
            if self._cluster_sel >= 0:
                self._cluster_sel = -1  # exit cluster mode
                self.msg("[MAP] Cluster nav OFF", C_DIM)
            else:
                self._cluster_sel = self._find_nearest_cluster(0, 0)  # select nearest to center
                if self._cluster_sel >= 0:
                    self.msg(f"[MAP] Cluster selected ({len(self._clusters)} total)", C_HACK_CYAN)
                else:
                    self.msg("[MAP] No clusters visible", C_WARNING)
            return

        # ESC — exit cluster selection (before other ESC handlers)
        if pyxel.btnp(pyxel.KEY_ESCAPE) and self._cluster_sel >= 0:
            self._cluster_sel = -1
            self._esc_consumed_frame = pyxel.frame_count
            return

        # Nothing to do if no cluster selected
        if self._cluster_sel < 0:
            return

        # ENTER — open popup
        if pyxel.btnp(pyxel.KEY_RETURN):
            if 0 <= self._cluster_sel < len(self._clusters):
                self._cluster_popup = self._clusters[self._cluster_sel]
                self._popup_scroll = 0
            return

        # Arrow keys — navigate between clusters
        if pyxel.btnp(pyxel.KEY_LEFT):
            self._cluster_sel = self._find_nearest_cluster(-1, 0)
        elif pyxel.btnp(pyxel.KEY_RIGHT):
            self._cluster_sel = self._find_nearest_cluster(1, 0)
        elif pyxel.btnp(pyxel.KEY_UP):
            self._cluster_sel = self._find_nearest_cluster(0, -1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._cluster_sel = self._find_nearest_cluster(0, 1)

    def _update_cluster_popup(self):
        """Handle popup scroll and close keys."""
        if not self._cluster_popup:
            return
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._cluster_popup = None
            self._esc_consumed_frame = pyxel.frame_count
            return
        pts = self._cluster_popup["points"]
        max_scroll = max(0, len(pts) - 8)
        if pyxel.btnp(pyxel.KEY_UP):
            self._popup_scroll = max(0, self._popup_scroll - 1)
        if pyxel.btnp(pyxel.KEY_DOWN):
            self._popup_scroll = min(max_scroll, self._popup_scroll + 1)

    def _draw_cluster_popup(self):
        if not self._cluster_popup:
            return
        pts = self._cluster_popup["points"]
        n = len(pts)

        # Count types
        n_open, n_sec, n_crk = 0, 0, 0
        for p in pts:
            lbl, _c = self._classify_auth(p.get("auth", ""), p.get("label", ""))
            if lbl == "Open":
                n_open += 1
            elif lbl == "Cracked":
                n_crk += 1
            else:
                n_sec += 1

        # Popup dimensions — 14px rows for 5x8 font
        ROW_H = 14
        pw, ph = 340, min(200, 50 + min(n, 8) * ROW_H + 16)
        px = max(4, min(W - pw - 4, self._cluster_popup["x"] - pw // 2))
        py = max(HUD_TOP + 4, min(TERM_Y - ph - 4,
                                   self._cluster_popup["y"] - ph // 2))
        # Background + border
        pyxel.rect(px, py, pw, ph, 0)
        pyxel.rectb(px, py, pw, ph, C_HACK_CYAN)

        # Header
        pyxel.text(px + 4, py + 4, f"Point networks: {n}", C_TEXT)
        summary = f"Open:{n_open} | Secured:{n_sec} | Cracked:{n_crk}"
        pyxel.text(px + 4, py + 16, summary, C_DIM)

        # Close hint (ESC)
        pyxel.text(px + pw - 24, py + 4, "ESC", C_DIM)

        # Column headers
        hdr_y = py + 30
        pyxel.text(px + 14,  hdr_y, "SSID", C_DIM)
        pyxel.text(px + 190, hdr_y, "RSSI", C_DIM)
        pyxel.text(px + 230, hdr_y, "Type", C_DIM)
        pyxel.text(px + 295, hdr_y, "Ch",   C_DIM)

        # Rows
        visible = pts[self._popup_scroll:self._popup_scroll + 8]
        for i, p in enumerate(visible):
            ry = hdr_y + 14 + i * ROW_H
            ssid = p.get("label", "?")
            auth = p.get("auth", "")
            rssi = p.get("rssi", "")
            ch = p.get("channel", "")
            type_lbl, type_c = self._classify_auth(auth, ssid)

            # Lock icon for secured/cracked
            if type_lbl in ("Secured", "Cracked"):
                pyxel.text(px + 4, ry, "\x0f", type_c)  # filled block
            else:
                pyxel.text(px + 4, ry, "\x07", C_WARNING)  # dot for open

            pyxel.text(px + 14,  ry, ssid[:28], C_TEXT)
            pyxel.text(px + 190, ry, str(rssi)[:5], C_DIM)
            pyxel.text(px + 230, ry, type_lbl, type_c)
            pyxel.text(px + 295, ry, str(ch)[:3], C_DIM)

        # Scroll indicator
        if n > 8:
            bar_h = max(4, ph * 8 // n)
            bar_y = py + 30 + (ph - 46) * self._popup_scroll // max(1, n - 8)
            pyxel.rect(px + pw - 3, bar_y, 2, bar_h, C_DIM)

    def _draw_markers(self):
        """Draw handshake and MeshCore node markers. Always visible at any
        zoom level; label shown from zoom 5 up."""
        zoom = self.proj.zoom
        for m in self.markers:
            sx, sy = self.proj.geo_to_screen(m.lat, m.lon)
            if not self.proj.screen_visible(sx, sy):
                continue
            if m.type in ("meshcore", "meshtastic"):
                # Cyan diamond + pulsing ring, always visible
                c = C_HACK_CYAN
                if pyxel.frame_count % 40 < 22:
                    pyxel.circb(sx, sy, 6, c)
                pyxel.rect(sx - 2, sy - 2, 5, 5, c)
                pyxel.pset(sx, sy, 0)
                pyxel.pset(sx, sy - 3, c)
                pyxel.pset(sx, sy + 3, c)
                pyxel.pset(sx - 3, sy, c)
                pyxel.pset(sx + 3, sy, c)
                if zoom >= 4:
                    _lx = (sx + 6 if sx + 6 + len(m.label) * 5 < W
                           else sx - 6 - len(m.label) * 5)
                    pyxel.text(_lx, sy - 3, m.label, c)
            else:
                # Handshake — red skull style
                if pyxel.frame_count % 30 < 20:
                    pyxel.circb(sx, sy, 4, C_ERROR)
                pyxel.rect(sx - 2, sy - 1, 5, 4, C_ERROR)
                pyxel.rect(sx - 1, sy - 3, 3, 2, C_ERROR)
                pyxel.pset(sx, sy, C_WARNING)
                if zoom >= 5:
                    _lx = (sx + 5 if sx + 5 + len(m.label) * 5 < W
                           else sx - 5 - len(m.label) * 5)
                    pyxel.text(_lx, sy - 3, m.label, C_ERROR)

    def _draw_wifi(self):
        for net in self.wifi_networks:
            if net.lat == 0.0 and net.lon == 0.0:
                continue  # no GPS fix when discovered — skip
            sx, sy = self.proj.geo_to_screen(net.lat, net.lon)
            if not self.proj.screen_visible(sx, sy): continue
            age = pyxel.frame_count - net.spawn_frame
            if age < 15:
                if age % 2 == 0: pyxel.circb(sx, sy, 15-age, C_WARNING)
                continue
            if net.hacked:
                pyxel.line(sx-2, sy, sx, sy-3, C_SUCCESS)
                pyxel.line(sx+2, sy, sx, sy-3, C_SUCCESS)
                pyxel.line(sx-2, sy, sx, sy+2, C_SUCCESS)
                pyxel.line(sx+2, sy, sx, sy+2, C_SUCCESS)
            else:
                blink = math.sin(pyxel.frame_count * 0.1 + hash(net.bssid) % 100)
                c = net.color if blink > 0 else 2
                pyxel.pset(sx, sy, c)
                if self.proj.zoom >= 5: pyxel.circb(sx, sy, 3, c)

    def _draw_ble(self):
        for d in self.ble_devices:
            if d.lat == 0.0 and d.lon == 0.0:
                continue  # no GPS fix when discovered — skip
            sx, sy = self.proj.geo_to_screen(d.lat, d.lon)
            if not self.proj.screen_visible(sx, sy): continue
            age = pyxel.frame_count - d.spawn_frame
            if age < 15:
                if age % 2 == 0: pyxel.circb(sx, sy, 15-age, C_HACK_CYAN)
                continue
            if d.hacked:
                pyxel.rect(sx-2, sy-2, 5, 5, C_HACK_CYAN)
                pyxel.pset(sx, sy, C_SUCCESS)
            else:
                blink = math.sin(pyxel.frame_count * 0.15 + d.blink_phase)
                pyxel.rect(sx-1, sy-1, 3, 3, d.color if blink > 0 else 2)

    def _draw_aircraft(self):
        """Draw ADS-B aircraft on the map. Always visible regardless of
        zoom; label from zoom 4 up."""
        try:
            aircraft_list = list(self._sdr.aircraft.values())
        except Exception:
            return
        zoom = self.proj.zoom
        for ac in aircraft_list:
            if not ac.has_position:
                continue
            sx, sy = self.proj.geo_to_screen(ac.lat, ac.lon)
            if not self.proj.screen_visible(sx, sy):
                continue
            # Altitude-based color: low=green, mid=yellow, high=cyan
            if ac.altitude < 5000:
                c = C_SUCCESS
            elif ac.altitude < 20000:
                c = C_WARNING
            else:
                c = C_HACK_CYAN
            # Plane icon — visible plus-shape with blinking pulse ring
            pyxel.line(sx - 3, sy, sx + 3, sy, c)       # wings
            pyxel.pset(sx, sy - 2, c)                   # nose
            pyxel.pset(sx, sy - 1, c)
            pyxel.pset(sx, sy + 1, c)
            pyxel.pset(sx - 1, sy + 2, c)               # tail fins
            pyxel.pset(sx + 1, sy + 2, c)
            if pyxel.frame_count % 40 < 14:
                pyxel.circb(sx, sy, 5, c)
            # Label at zoom >= 4
            if zoom >= 4:
                label = ac.callsign or ac.icao
                alt_k = ac.altitude // 1000
                _lt = f"{label} {alt_k}k"
                # Flip the label to the left of the plane near the right edge
                # so callsigns don't get clipped off-screen.
                _lx = sx + 6 if sx + 6 + len(_lt) * 5 < W else sx - 6 - len(_lt) * 5
                pyxel.text(_lx, sy - 4, _lt, c)

    def _draw_sensors(self):
        """Draw 433 MHz sensors on the map."""
        try:
            sensor_list = list(self._sdr.sensors.values())
        except Exception:
            return
        for s in sensor_list:
            if s.lat == 0.0 and s.lon == 0.0:
                continue
            sx, sy = self.proj.geo_to_screen(s.lat, s.lon)
            if not self.proj.screen_visible(sx, sy):
                continue
            blink = math.sin(pyxel.frame_count * 0.08 + hash(s.sid) % 100)
            c = 12 if blink > 0 else 2  # blue blink
            pyxel.rect(sx - 1, sy - 1, 3, 3, c)
            pyxel.pset(sx, sy - 2, c)  # antenna dot
            if self.proj.zoom >= 5:
                _sm = s.model[:12]
                _lx = sx + 4 if sx + 4 + len(_sm) * 5 < W else sx - 4 - len(_sm) * 5
                pyxel.text(_lx, sy - 3, _sm, 12)

    def _draw_scan_fx(self):
        cx, cy = W // 2, HUD_TOP + MAP_H // 2
        if self.scan_pulse < 30:
            r = int(self.scan_pulse * 1.5)
            pyxel.circb(cx, cy, r, C_GRID if self.scan_pulse > 20 else C_HACK_CYAN)
        if self.ble_scanning or self.wifi_scanning:
            r = 25 + int(math.sin(pyxel.frame_count * 0.3) * 8)
            pyxel.circb(cx, cy, r, 12)

    def _draw_ambient_motes(self):
        """Subtle data motes drifting across the map — ambient cyberspace life,
        most noticeable when the map is empty (no GPS/tiles yet)."""
        fc = pyxel.frame_count
        for i in range(10):
            mx = int((i * 137 + fc * 0.35) % W)
            my = HUD_TOP + int((i * 71 + 3 + fc * 0.18) % MAP_H)
            pyxel.pset(mx, my, 5 if i % 3 else 1)

    def _draw_player(self):
        if self.menu_open:
            return
        cx, cy = W // 2, HUD_TOP + MAP_H // 2

        # Sonar ping — an expanding ring sweeps out from the avatar across the
        # map every ~4s, fading cyan -> dim. Clipped to the map so it never
        # bleeds onto the HUD or terminal.
        pyxel.clip(0, HUD_TOP, W, MAP_H)
        _pg = pyxel.frame_count % 120
        if _pg < 55:
            _pr = _pg * 3 + 6
            _pc = C_HACK_CYAN if _pg < 18 else (5 if _pg < 36 else 1)
            pyxel.circb(cx, cy, _pr, _pc)
        pyxel.clip()

        # Skull sprite (9x9 pixel art)
        # Subtle breathing bob
        b = int(math.sin(self._breath * 0.06) * 0.8)
        sy = cy - 4 + b

        # Shadow
        pyxel.elli(cx - 4, cy + 6, 9, 3, 1)

        # Skull top (cranium)
        pyxel.rect(cx - 3, sy - 3, 7, 3, 7)       # top row
        pyxel.rect(cx - 4, sy,     9, 4, 7)       # middle (widest)
        # Eye sockets
        pyxel.rect(cx - 3, sy + 1, 2, 2, 0)       # left eye
        pyxel.rect(cx + 2, sy + 1, 2, 2, 0)       # right eye
        # Eye glow (blinks)
        if pyxel.frame_count % 120 < 115:
            pyxel.pset(cx - 2, sy + 1, C_HACK_CYAN)  # left pupil
            pyxel.pset(cx + 2, sy + 1, C_HACK_CYAN)  # right pupil
        # Nose
        pyxel.pset(cx, sy + 3, 13)
        # Jaw
        pyxel.rect(cx - 3, sy + 4, 7, 2, 7)
        # Teeth (dark gaps)
        pyxel.pset(cx - 2, sy + 5, 0)
        pyxel.pset(cx,     sy + 5, 0)
        pyxel.pset(cx + 2, sy + 5, 0)
        # Crossbones under skull
        pyxel.line(cx - 4, sy + 7, cx + 4, sy + 9, 7)
        pyxel.line(cx + 4, sy + 7, cx - 4, sy + 9, 7)

        # Hologram scan line sweeping down through the skull (a dashed cyan line,
        # every ~2s) — gives the avatar a live "projection" feel.
        _hl = sy - 4 + (pyxel.frame_count % 60) // 5
        if sy - 4 <= _hl <= sy + 6:
            for _hx in range(cx - 4, cx + 5, 2):
                pyxel.pset(_hx, _hl, C_HACK_CYAN)

        # Scan ring (pulsing) + rotating radar sweep + orbiting data bits —
        # a subtle "live hacker aura" around the avatar.
        ring_r = 10 + int(math.sin(self._breath * 0.03) * 2)
        pyxel.circb(cx, cy, ring_r, C_HACK_CYAN)
        sweep = pyxel.frame_count * 0.08
        pyxel.line(cx, cy,
                   cx + int(math.cos(sweep) * ring_r),
                   cy + int(math.sin(sweep) * ring_r), C_HACK_CYAN)
        _orb_cols = (C_HACK_CYAN, C_SUCCESS, C_WARNING)
        for _i in range(3):
            _a = sweep * 0.6 + _i * 2.094          # 3 bits, 120 deg apart
            pyxel.pset(cx + int(math.cos(_a) * (ring_r + 3)),
                       cy + int(math.sin(_a) * (ring_r + 3)), _orb_cols[_i])

    def _draw_particles(self):
        for p in self.particles:
            if p.life > 7:
                pyxel.pset(int(p.x), int(p.y), p.color)

    def _draw_hack_bar(self):
        if not self.hacking or not self.hack_target: return
        sx, sy = self.proj.geo_to_screen(self.hack_target.lat, self.hack_target.lon)
        hy = sy - 14
        pct = self.hack_progress / 45
        fill = int(30 * pct)
        # Pulsing lock-on reticle bracketing the target while it cracks.
        _lr = 8 + int(math.sin(pyxel.frame_count * 0.2) * 2)
        for _dx, _dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            _rx, _ry = sx + _dx * _lr, sy + _dy * _lr
            pyxel.line(_rx, _ry, _rx - _dx * 3, _ry, C_HACK_CYAN)
            pyxel.line(_rx, _ry, _rx, _ry - _dy * 3, C_HACK_CYAN)
        pyxel.rect(sx-15, hy, 30, 4, 0)
        pyxel.rect(sx-15, hy, fill, 4, C_SUCCESS)
        # Sweeping glint along the filled bar — active "cracking" feel.
        if fill > 2:
            _gx = sx - 15 + int((pyxel.frame_count * 3) % (fill + 6))
            if sx - 15 <= _gx < sx - 15 + fill:
                pyxel.line(_gx, hy, _gx, hy + 3, 7)
        pyxel.rectb(sx-15, hy, 30, 4, C_HACK_CYAN)
        _dots = "." * ((pyxel.frame_count // 6) % 4)
        pyxel.text(sx-18, hy-8, f"HACKING {int(100*pct)}%{_dots}", C_HACK_CYAN)

    def _draw_glitch(self):
        if self.glitch_timer > 0:
            for _ in range(4):
                pyxel.rect(random.randint(0, W-1), random.randint(0, H-1),
                           random.randint(5, 50), 1,
                           random.choice([C_HACK_CYAN, C_TEXT, C_SUCCESS]))

    def _draw_scanlines(self):
        for sl in self.scan_lines:
            pyxel.rect(0, sl, W, 1, C_GRID)

    def _draw_terminal(self):
        pyxel.rect(0, TERM_Y, W, TERM_H, 0)
        pyxel.line(0, TERM_Y, W - 1, TERM_Y, C_MENU_BORDER)
        # Live data packet sweeping the top rail — the feed reads as flowing.
        _dp = int((pyxel.frame_count * 2.5) % (W + 30))
        for _o in range(6):
            _x = _dp - _o
            if 0 <= _x < W:
                _c = C_HACK_CYAN if _o == 0 else (7 if _o < 2 else (5 if _o < 4 else 1))
                pyxel.pset(_x, TERM_Y, _c)
        pyxel.text(3, TERM_Y + 1, "> OUTPUT", C_HACK_CYAN)

        tool_name = ""
        if self.capturing_hs: tool_name = "HANDSHAKE"
        elif self.wifi_scanning or self._wifi_scan_only: tool_name = "WiFi SCAN"
        elif self.ble_scanning or self._ble_scan_only: tool_name = "BT SCAN"
        elif self._bt_airtag:
            parts = []
            if self._airtag_count: parts.append(f"AT:{self._airtag_count}")
            if self._smarttag_count: parts.append(f"ST:{self._smarttag_count}")
            tool_name = "TAG SCAN " + " ".join(parts) if parts else "TAG SCAN"
        elif self._bt_tracking: tool_name = "BT TRACK"
        elif self.sniffing: tool_name = "SNIFFER"
        elif self._mitm.running: tool_name = f"MITM [{self._mitm.packets}pkt]"
        elif self._dragon_drain.running: tool_name = "DRAGON DRAIN"
        elif self.state.portal_running:
            tool_name = f"EVIL PORTAL [{self.state.submitted_forms}cap]"
        elif self.state.evil_twin_running:
            tool_name = f"EVIL TWIN [{len(self.state.evil_twin_captured_data)}cap]"
        if tool_name:
            pyxel.text(54, TERM_Y + 1, tool_name, C_WARNING)
        else:
            # Idle "system ready" indicator — a slow green pulse dot.
            if pyxel.frame_count % 50 < 30:
                pyxel.circ(57, TERM_Y + 4, 1, C_SUCCESS)
            pyxel.text(62, TERM_Y + 1, "READY", C_DIM)

        with self._term_lock:
            lines_snap = list(self.terminal_lines)
            colors_snap = list(self._terminal_colors)
        total = len(lines_snap)

        if self.term_scroll > 0:
            pyxel.text(W - 60, TERM_Y + 1, f"SCROLL +{self.term_scroll}", C_DIM)
        pyxel.text(W - 30, TERM_Y + 1, f"L:{total}", C_DIM)

        # Spleen 5x8 — set as global default via monkey-patch, so pyxel.text
        # calls below render with it even without an explicit font arg.
        if self._font is not None:
            line_h = 9        # 8px glyphs + 1px gap
            char_w = 5
            content_y = TERM_Y + 10
            max_chars = (W - 8) // char_w  # ~126 chars fit at 640px
        else:
            line_h = 5
            char_w = 4
            content_y = TERM_Y + 8
            max_chars = 150
        content_h = TERM_H - (content_y - TERM_Y) - 2
        max_visible = content_h // line_h

        if self.term_scroll == 0:
            start = max(0, total - max_visible)
            end = total
        else:
            end = max(0, total - self.term_scroll)
            start = max(0, end - max_visible)

        y = content_y
        have_colors = len(colors_snap) == total
        for i in range(start, end):
            line = lines_snap[i]
            c = colors_snap[i] if have_colors else C_TEXT
            pyxel.text(4, y, line[:max_chars], c)
            y += line_h
            if y >= TERM_Y + TERM_H - line_h:
                break

        if self.term_scroll == 0 and pyxel.frame_count % 30 < 20:
            pyxel.text(4, min(y, TERM_Y + TERM_H - line_h - 1), "_",
                       C_HACK_CYAN)
        if total > max_visible and self.term_scroll == 0:
            pyxel.text(W - 120, TERM_Y + TERM_H - line_h - 1,
                       "Fn+U/Fn+K scroll", C_DIM)

    def _draw_mc_toast(self):
        """Draw MeshCore message toast on top of any screen."""
        if not self._mc_bubbles:
            return
        msg, expire = self._mc_bubbles[-1]
        remaining = expire - pyxel.frame_count
        if remaining <= 0:
            return
        # Large toast bar — centered, double height (5px per char)
        tw = max(len(msg) * 5 + 60, 300)
        if tw > W - 20:
            tw = W - 20
        tx = (W - tw) // 2
        # Slide-in from above when the toast first appears (dur = 300 frames).
        _slide = max(0, remaining - 290) * 4
        ty = 20 - _slide
        th = 32
        # Background with thick border
        pyxel.rect(tx, ty, tw, th, 0)
        pyxel.rect(tx + 1, ty + 1, tw - 2, th - 2, 1)
        pyxel.rectb(tx, ty, tw, th, 2)
        pyxel.rectb(tx + 1, ty + 1, tw - 2, th - 2, 2)
        # Icon + label
        pyxel.text(tx + 6, ty + 4, "MESHCORE", 2)
        # Message text — larger area
        pyxel.text(tx + 6, ty + 16, msg[:70], C_SUCCESS)
        # Blinking radio icon
        if pyxel.frame_count % 30 < 20:
            pyxel.circ(tx + tw - 10, ty + th // 2, 3, 2)
            pyxel.circ(tx + tw - 10, ty + th // 2, 1, C_SUCCESS)
        # Countdown bar — shrinks as the toast's time runs out.
        _frac = max(0.0, min(1.0, remaining / 300.0))
        pyxel.rect(tx + 2, ty + th - 3, int((tw - 4) * _frac), 2, C_SUCCESS)

    def _get_hat_profile(self) -> tuple[str, int]:
        """Determine hacker hat color based on activity profile.

        Returns (hat_name, pyxel_color).
        Hat types:
          White  (7)  — recon only: scanning, wardriving, no attacks
          Blue   (12) — defensive: mostly scanning, few handshakes
          Grey   (13) — mixed: scanning + some attacks
          Red    (8)  — offensive: heavy attacks, handshakes, deauth
          Black  (0)  — aggressive: ET credentials, MITM, exploits
        """
        t = self._loot_totals
        scan = t.get("wifi", 0) + t.get("bt", 0)
        hs = t.get("hs", 0) + t.get("pcap", 0)
        creds = (t.get("passwords", 0) + t.get("et_captures", 0)
                 + self.state.submitted_forms
                 + len(self.state.evil_twin_captured_data))
        hacked = sum(1 for d in self.ble_devices if d.hacked) + \
                 sum(1 for n in self.wifi_networks if n.hacked)

        attack_score = hs * 3 + creds * 10 + hacked * 2
        total = scan + attack_score

        if total == 0:
            return "WHITE", 7

        attack_ratio = attack_score / max(total, 1)

        if creds >= 5 or attack_ratio > 0.6:
            return "BLACK", 0
        if hs >= 10 or attack_ratio > 0.35:
            return "RED", 8
        if hs >= 3 or attack_ratio > 0.15:
            return "GREY", 13
        if scan > 50:
            return "BLUE", 12
        return "WHITE", 7

    def _draw_emergency_alert(self):
        """Emergency scan HUD: flashing police lightbar (top-left, mirrors radar)
        + status text. Lamps flash fast/red on a real vehicle keyup."""
        if not (self._sdr.running and "emerg" in self._sdr.mode):
            return
        sigs = list(self._sdr.emergency.values())
        active_sigs = [s for s in sigs if getattr(s, "active", False)]
        live = bool(getattr(self._sdr, "emergency_active", False) and active_sigs)
        # Latch a recent keyup so it stays on screen ~20s after the brief event
        # (a pass-by keys up for only a sweep or two, easy to miss otherwise).
        LATCH_S = 20.0
        latch = getattr(self, "_emerg_latch", None)
        latched = bool(latch and (time.time() - latch[3] < LATCH_S))
        active = live or latched

        # --- whole-screen colour flash on a detection (service colour) ---
        if active:
            _ab = (max(active_sigs, key=lambda s: s.rssi).band if live
                   else latch[0])
            fcol = EMERGENCY_SVC_COL.get(_ab, C_ERROR)
            if (pyxel.frame_count // 4) % 2 == 0:          # pulse ~4 Hz
                for _i in range(6):                        # thick edge flash
                    pyxel.rectb(_i, _i, W - _i * 2, H - _i * 2, fcol)

        # --- police lightbar (radiowoz koguty) — top-left, opposite the radar ---
        lx, ly = 6, HUD_TOP + 4
        W_BAR, H_BAR, LAMP = 34, 14, 14        # ~1.7x bigger than before
        period = 3 if active else 14           # fast strobe on a real alert
        phase = (pyxel.frame_count // period) % 2
        C_BLUE = 12
        blue_on = phase == 0
        red_on = phase == 1
        pyxel.rect(lx + 3, ly + H_BAR, W_BAR - 6, 2, C_DIM)                    # mount
        pyxel.rect(lx, ly, W_BAR, H_BAR, 1)                                    # housing
        pyxel.rect(lx + 2, ly + 2, LAMP, H_BAR - 4, C_BLUE if blue_on else 1)  # blue lamp
        if blue_on:                                                           # glowing core
            pyxel.rect(lx + 2 + LAMP // 2 - 1, ly + 3, 3, H_BAR - 6, 7)
        pyxel.rect(lx + W_BAR - 2 - LAMP, ly + 2, LAMP, H_BAR - 4,
                   C_ERROR if red_on else 1)                                   # red lamp
        if red_on:                                                            # glowing core
            pyxel.rect(lx + W_BAR - 2 - LAMP + LAMP // 2 - 1, ly + 3, 3, H_BAR - 6, 7)
        if active:                                                            # halo on real alert
            if blue_on:
                pyxel.rectb(lx + 1, ly + 1, LAMP + 2, H_BAR - 2, C_BLUE)
            else:
                pyxel.rectb(lx + W_BAR - 3 - LAMP, ly + 1, LAMP + 2, H_BAR - 2, C_ERROR)
        pyxel.rectb(lx, ly, W_BAR, H_BAR, C_DIM)

        # --- status text next to the lightbar ---
        net = getattr(self._sdr, "network_present", False)
        if live:
            sig = max(active_sigs, key=lambda s: s.rssi)
            arrow = ">" if sig.trend == "up" else ""
            svc = EMERGENCY_SVC.get(sig.band, sig.band)
            txt = f"{svc}! {sig.rssi:.0f}dB{arrow}"
            col = (EMERGENCY_SVC_COL.get(sig.band, C_ERROR)
                   if phase == 0 else C_DIM)      # blink svc-colour <-> dim
        elif latched:
            band, rssi, _tr, ts = latch
            svc = EMERGENCY_SVC.get(band, band)
            ago = int(time.time() - ts)
            txt = f"{svc} {rssi:.0f}dB ({ago}s temu)"
            col = EMERGENCY_SVC_COL.get(band, C_ERROR) if phase == 0 else C_DIM
        else:
            veh = [s for s in sigs if getattr(s, "role", "") == "vehicle"]
            vtxt = f" {max(veh, key=lambda s: s.rssi).rssi:+.0f}dB" if veh else ""
            txt = f"SVC TETRA:{'ON' if net else '--'}{vtxt}"
            # persistent memo of the last detection this session
            if latch:
                txt += (" | ost:" + EMERGENCY_SVC.get(latch[0], latch[0])
                        + " " + time.strftime('%H:%M', time.localtime(latch[3])))
            col = C_SUCCESS if net else C_DIM
        pyxel.text(lx + W_BAR + 5, ly + 3, txt, col)

    def _draw_hud_top(self):
        # Spleen 5x8: each char = 5px wide, 8px tall. HUD is 16px tall so
        # text at y=4 sits with 4px margins. All horizontal jumps are
        # computed from actual string widths so nothing overlaps.
        CW, TY = 5, 4
        pyxel.rect(0, 0, W, HUD_TOP, C_HUD_BG)
        pyxel.line(0, HUD_TOP - 1, W - 1, HUD_TOP - 1, C_HUD_LINE)
        x = 4
        # Title
        title = "WATCH DOGS"
        pyxel.text(x, TY, title, C_HACK_CYAN)
        x += len(title) * CW + 6
        # Level
        lv_txt = f"LV:{self.level}"
        pyxel.text(x, TY, lv_txt, C_SUCCESS)
        x += len(lv_txt) * CW + 4
        lvl = self.level_title
        # Rank title in its own accent colour + a pulsing underline, so the
        # player's earned handle reads distinctly from the LV number.
        pyxel.text(x, TY, lvl, C_HACK_CYAN)
        _uc = C_HACK_CYAN if pyxel.frame_count % 40 < 24 else 1
        pyxel.line(x, TY + 9, x + len(lvl) * CW - 2, TY + 9, _uc)
        x += len(lvl) * CW + 4
        # XP bar
        xw = 40
        nxt = self.xp_for_next_level
        cur = self.xp_in_current_level
        xf = int(xw * cur / nxt) if nxt > 0 else xw
        pyxel.rect(x, TY + 1, xw, 7, C_GRID)
        pyxel.rect(x, TY + 1, xf, 7, C_HACK_CYAN)
        # Energy shimmer sweeping the filled XP — progress reads as "charging".
        if xf > 3:
            _period = xf + 45
            _xs = int((pyxel.frame_count * 2) % _period)
            if _xs < xf:
                _px = x + _xs
                pyxel.line(_px, TY + 2, _px, TY + 6, 7)
                if _px + 1 < x + xf:
                    pyxel.pset(_px + 1, TY + 4, 6)
        pyxel.rectb(x, TY + 1, xw, 7, C_COAST)
        x += xw + 3
        xp_txt = f"{self.xp}"
        pyxel.text(x, TY, xp_txt, C_DIM)
        x += len(xp_txt) * CW + 6
        # Hat badge — identity chip with a breathing halo so rank feels live.
        hat_name, hat_color = self._get_hat_profile()
        _hy = HUD_TOP // 2
        _halo = hat_color if hat_color != 0 else 6
        _hb = pyxel.frame_count % 50
        if _hb < 30:
            pyxel.circb(x + 3, _hy, 4 + _hb // 20, _halo)
        pyxel.circ(x + 3, _hy, 3, 7)
        pyxel.circ(x + 3, _hy, 2, hat_color)
        x += 9
        pyxel.text(x, TY, hat_name, C_DIM)
        x += len(hat_name) * CW + 4
        # Achievement badges
        _BADGE_HUD = [
            ("flipper",          "FLP", C_WARNING),
            ("wardriver",        "WDR", C_SUCCESS),
            ("meshcore",         "MSH", 2),
            ("handshake_hunter", "HS",  C_ERROR),
            ("wpasec_uploader",  "WPA", 12),
            ("evil_twin",        "ET",  8),
            ("skywatch",         "ADB", C_HACK_CYAN),
            ("iot_hunter",       "433", 12),
            ("emergency",        "SVC", C_ERROR),
        ]
        _badge_x0 = x
        for badge_id, label, color in _BADGE_HUD:
            if badge_id in self._badges:
                lw = len(label) * CW + 4
                if x + lw + 2 > W - 70:
                    break  # don't overflow past zoom / battery
                pyxel.rectb(x, 2, lw, HUD_TOP - 4, color)
                pyxel.text(x + 2, TY, label, color)
                x += lw + 2
        # Trophy shine — a glint glides along the badge-row frame every ~6.6s,
        # riding the top & bottom edges so it never smears the labels.
        _badge_x1 = x - 2
        if _badge_x1 > _badge_x0 + 4:
            _t = pyxel.frame_count % 200
            _span = _badge_x1 - _badge_x0
            if _t <= _span:
                _sx = _badge_x0 + _t
                for _lx, _c in ((_sx, 7), (_sx - 1, 6)):
                    if _badge_x0 <= _lx <= _badge_x1:
                        pyxel.pset(_lx, 2, _c)
                        pyxel.pset(_lx, HUD_TOP - 3, _c)
        # Battery (icon + %) + Zoom — far right
        bat = self._battery_pct
        if bat >= 0:
            bc = C_SUCCESS if bat > 25 else (C_WARNING if bat > 10 else C_ERROR)
            pct_txt = f"{bat}%"
            icon_w = 12
            px0 = W - len(pct_txt) * CW - 4 - icon_w - 2
            # Low battery: urgent blink of the whole indicator.
            _low = bat <= 10 and (pyxel.frame_count % 30 < 15)
            _ic = C_ERROR if _low else bc
            pyxel.rectb(px0, TY, 10, 7, _ic)            # body
            pyxel.rect(px0 + 10, TY + 2, 2, 3, _ic)     # terminal nub
            _fw = max(0, min(8, int(8 * bat / 100)))
            if _fw > 0:
                pyxel.rect(px0 + 1, TY + 1, _fw, 5, _ic)  # charge fill
            pyxel.text(px0 + icon_w + 2, TY, pct_txt, _ic)
            zoom_txt = f"Z:{self.proj.label}"
            pyxel.text(px0 - len(zoom_txt) * CW - 6, TY, zoom_txt, C_DIM)
        else:
            zoom_txt = f"Z:{self.proj.label}"
            pyxel.text(W - len(zoom_txt) * CW - 4, TY, zoom_txt, C_DIM)

    def _draw_hud_bottom(self):
        pyxel.rect(0, H - HUD_BOT, W, HUD_BOT, C_HUD_BG)
        pyxel.line(0, H - HUD_BOT, W - 1, H - HUD_BOT, C_HUD_LINE)
        y = H - HUD_BOT + 5

        # All-time totals from loot_db + current session additions
        t_bt   = self._loot_totals.get("bt",   0) + len(self.ble_devices)
        t_wifi = self._loot_totals.get("wifi", 0) + len(self.wifi_networks)
        n_hs_ses = sum(1 for m in self.markers if m.type == "handshake")
        t_hs   = self._loot_totals.get("hs",   0) + n_hs_ses
        n_pwn  = (sum(1 for d in self.ble_devices if d.hacked)
                  + sum(1 for n in self.wifi_networks if n.hacked))
        t_pwd  = (self._loot_totals.get("passwords", 0)
                  + self._loot_totals.get("et_captures", 0)
                  + self.state.submitted_forms
                  + len(self.state.evil_twin_captured_data))
        # Columns sized for Spleen 5x8 + 4-digit counts (e.g. "BLE:9999"=40px).
        # Each counter "pops" (white flash + rising +tick) the moment it grows,
        # so finding a device/handshake/pwn gives instant visual feedback.
        _counters = [
            ("BLE",  4,   t_bt,   C_HACK_CYAN),
            ("WiFi", 60,  t_wifi, C_WARNING),
            ("HS",   125, t_hs,   C_ERROR),
            ("PWD",  170, t_pwd,  12),
            ("PWN",  220, n_pwn,  C_SUCCESS),
        ]
        _prev = self.__dict__.setdefault("_hud_counts", {})
        _flash = self.__dict__.setdefault("_hud_flash", {})
        _fc = pyxel.frame_count
        for _k, _cx, _val, _col in _counters:
            if _k in _prev and _val > _prev[_k]:
                _flash[_k] = _fc
            _prev[_k] = _val
            _txt = f"{_k}:{_val}"
            _age = _fc - _flash.get(_k, -999)
            if 0 <= _age < 12:
                pyxel.text(_cx, y, _txt, 7)            # white pop
                pyxel.text(_cx + len(_txt) * 5 + 1, y - _age // 3, "+", _col)
            else:
                pyxel.text(_cx, y, _txt, _col)

        tools = []
        if self.wifi_scanning: tools.append("WiFi")
        if self.ble_scanning: tools.append("BT")
        if self.sniffing: tools.append("SNF")
        if self.capturing_hs: tools.append("HS")
        if self.state.portal_running: tools.append("EP")
        if self.state.evil_twin_running: tools.append("ET")
        if self._sdr.running and "adsb" in self._sdr.mode:
            n_pos = sum(1 for a in self._sdr.aircraft.values() if a.has_position)
            n_all = len(self._sdr.aircraft)
            tools.append(f"ADS-B:{n_pos}/{n_all}")
        if self._sdr.running and "433" in self._sdr.mode:
            tools.append(f"433:{self._sdr.total_sensors_seen}")
        if self._sdr.running and "emerg" in self._sdr.mode:
            tools.append("SVC!" if self._sdr.emergency_active else "SVC")
        if getattr(self, "_muted", False):
            tools.append("MUTE")
        if tools:
            dots = "." * ((pyxel.frame_count // 10) % 4)
            pyxel.text(270, y, " ".join(tools) + dots, 12)
        elif not self.menu_open:
            # Keybind legend — keys highlighted, labels dim (matches menu style).
            _kx = 270
            for _seg, _sc in (("[TAB]", C_HACK_CYAN), ("Menu ", C_COAST),
                              ("[`]", C_HACK_CYAN), ("Loot ", C_COAST),
                              ("[S]", C_WARNING), ("Stop", C_COAST)):
                pyxel.text(_kx, y, _seg, _sc)
                _kx += len(_seg) * 5

        # GPS status — lock-reticle icon reflects state (locked/searching/off).
        if self.gps_fix:
            lat_c = "N" if self.player_lat >= 0 else "S"
            lon_c = "E" if self.player_lon >= 0 else "W"
            g_txt = (f"{abs(self.player_lat):.5f}{lat_c} "
                     f"{abs(self.player_lon):.5f}{lon_c}")
            g_x, g_col, g_state = W - 108, C_SUCCESS, "fix"
        elif self.gps.available:
            if self.gps_sats_vis > 0:
                g_txt = f"Waiting fix Vis:{self.gps_sats_vis}"
                g_x, g_col, g_state = W - 120, C_WARNING, "search"
            else:
                g_txt = "Waiting for GPS fix"
                g_x, g_col, g_state = W - 100, C_ERROR, "search"
        else:
            g_txt = "GPS offline"
            g_x, g_col, g_state = W - 60, C_DIM, "off"
        _ix, _iy = g_x - 10, y + 3
        if g_state == "search":
            pyxel.circb(_ix, _iy, 2 + (pyxel.frame_count // 10) % 2, g_col)
            pyxel.pset(_ix, _iy, g_col)
        elif g_state == "fix":
            pyxel.circb(_ix, _iy, 3, g_col)
            pyxel.circ(_ix, _iy, 1, g_col)
        else:
            pyxel.circb(_ix, _iy, 3, g_col)
        pyxel.text(g_x, y, g_txt, g_col)

        # The label reflects a ready radio, not merely GPIO power or a thread.
        protocol = self._active_lora_protocol()
        label = {"meshcore": "MeshCore", "meshtastic": "Meshtastic", "rnode": "RNode"}.get(protocol, "OFF")
        pyxel.text(W - 200, y, f"LoRa:{label}", C_SUCCESS if protocol else C_DIM)


    def _draw_prox_icon(self):
        """Little pager/badge icon in the map's top-left. Blinks when a WDGWars
        node is in range; brighter and faster when mutual or very close."""
        if not self._prox.enabled:
            return
        # Left edge, mid-map: clear of the emergency/TETRA lightbar (top-left)
        # and the message log (bottom-left).
        x, y = 6, HUD_TOP + 100
        fc = pyxel.frame_count
        rows = self._prox.rows()
        inrange = bool(rows)
        mutual = any(r.get("mutual") for r in rows)
        veryclose = any((r.get("state") or 0) >= 4 for r in rows)
        meeting = self._prox.meeting() is not None

        # Colour = relationship, blink speed = closeness. Mutual is the loud one.
        if self._prox.paused or not self._prox.fresh() or not inrange:
            col, rate, ring = C_DIM, 0, False           # idle / held: calm, dim
        elif meeting:
            col, rate, ring = C_SUCCESS, 5, True        # met! green, fastest, ring
        elif mutual:
            col, rate, ring = C_SUCCESS, 10, veryclose  # mutual: green blink (+ring if very close)
        else:
            col = C_HACK_CYAN                            # seen one-way: calm cyan
            rate = 12 if veryclose else 22
            ring = False
        lit = True if rate == 0 else ((fc // rate) % 2 == 0)
        scr = col if (inrange and lit) else 1

        # antenna
        pyxel.line(x + 2, y, x + 2, y - 3, col)
        pyxel.pset(x + 2, y - 4, col if lit else 1)
        # pager body + screen + buttons
        pyxel.rectb(x, y, 12, 15, col)
        pyxel.rect(x + 2, y + 2, 8, 5, scr)
        pyxel.rect(x + 2, y + 9, 8, 1, col)
        pyxel.pset(x + 3, y + 12, col)
        pyxel.pset(x + 6, y + 12, col)
        pyxel.pset(x + 9, y + 12, col)
        # a single emanating ring only on a real (mutual) contact
        if ring and lit:
            rr = 8 + (fc // 3) % 3
            pyxel.circb(x + 6, y + 4, rr, col)
        # count / hold hint to the right of the icon
        if self._prox.paused:
            pyxel.text(x + 15, y + 5, "II", C_DIM)
        elif inrange:
            n_m = sum(1 for r in rows if r.get("mutual"))
            pyxel.text(x + 15, y + 5, "%d/%d" % (n_m, len(rows)), col)

    def _draw_prox_screen(self):
        pyxel.cls(0)
        pyxel.rect(0, 0, W, HUD_TOP, C_HUD_BG)
        pyxel.line(0, HUD_TOP, W - 1, HUD_TOP, C_HUD_LINE)
        pyxel.text(6, 5, "WDGWARS // PROXIMITY RADAR", C_HACK_CYAN)
        my = self._prox.my_ident()
        pyxel.text(W - 168, 5, "YOU WDG-%s" % (my or "------"), C_SUCCESS)
        if self._prox.paused:
            st_txt, st_col = "HOLD (BT busy)", C_WARNING
        elif not self._prox.enabled:
            st_txt, st_col = "OFF", C_DIM
        elif not self._prox.fresh():
            st_txt, st_col = "starting", C_WARNING
        else:
            st_txt, st_col = "ON", C_SUCCESS
        pyxel.text(W - 60, 5, st_txt, st_col)

        if not self._prox.enabled:
            pyxel.text(W // 2 - 88, 150, "Proximity radar is OFF", C_DIM)
            pyxel.text(W // 2 - 84, 166, "press [O] to turn it ON", C_TEXT)
            pyxel.text(8, H - 12, "[O] on/off    [TAB/ESC/Q] back", C_DIM)
            return

        hy = HUD_TOP + 8
        pyxel.text(12, hy, "PLAYER", C_DIM)
        pyxel.text(120, hy, "STATE", C_DIM)
        pyxel.text(210, hy, "TREND", C_DIM)
        pyxel.text(330, hy, "SIGNAL", C_DIM)
        pyxel.text(474, hy, "dBm", C_DIM)
        pyxel.text(540, hy, "MUTUAL", C_DIM)
        pyxel.line(8, hy + 10, W - 8, hy + 10, C_HUD_LINE)

        rows = self._prox.rows()
        if not self._prox.fresh():
            pyxel.text(W // 2 - 56, 170, "radio starting...", C_WARNING)
        elif not rows:
            pyxel.text(W // 2 - 44, 158, "LISTENING", C_HACK_CYAN)
            pyxel.text(W // 2 - 116, 174, "no WDGWars nodes in range yet", C_DIM)
        else:
            seen = set()
            i = 0
            for r in rows:
                lbl = str(r.get("label", "?"))
                if lbl in seen:
                    continue
                seen.add(lbl)
                yy = HUD_TOP + 28 + i * 30
                i += 1
                if i > 8:
                    break
                mut = r.get("mutual")
                stt = r.get("state") or 0
                ncol = C_SUCCESS if mut else C_HACK_CYAN
                pyxel.text(12, yy, lbl[:16], ncol)
                pyxel.text(120, yy, PROX_STATE_NAMES[stt], C_TEXT)
                pyxel.text(210, yy, PROX_TREND_WORDS.get(r.get("trend") or 0, ""),
                           C_HACK_CYAN)
                # signal bar (0..20)
                pyxel.rectb(330, yy - 1, 130, 8, C_DIM)
                bar = max(0, min(20, r.get("bar") or 0))
                pyxel.rect(331, yy, int(128 * bar / 20), 6,
                           C_SUCCESS if mut else C_HACK_CYAN)
                ema = r.get("ema")
                pyxel.text(474, yy, "%d" % int(ema) if ema is not None else "--",
                           C_TEXT)
                if mut:
                    pyxel.rect(540, yy - 1, 30, 8, C_SUCCESS)
                    pyxel.text(545, yy, "YES", 0)
                else:
                    pyxel.text(548, yy, "--", C_DIM)

        unk = self._prox.unknown()
        pyxel.text(12, H - 26, "unknown nodes nearby: %d" % unk, C_DIM)
        meet = self._prox.meeting()
        if meet:
            pyxel.rect(0, H - 44, W, 16, C_SUCCESS)
            rssi = meet.get("rssi")
            pyxel.text(W // 2 - 110, H - 40, "*** SPOTKANIE z %s   %s dBm ***"
                       % (meet.get("label", "?"),
                          int(rssi) if rssi is not None else "?"), 0)
        pyxel.text(8, H - 12, "[O] on/off    [TAB/ESC/Q] back", C_DIM)

    def _draw_messages(self):
        y = TERM_Y - 12
        for text, timer, col in reversed(self.msgs):
            fade = min(timer, 30) / 30
            c = col if fade > 0.5 else C_COAST
            x_off = max(0, timer - 168) * 4   # fresh msgs slide in from the left
            bx = 4 + x_off
            # Colored accent tab makes each event read as a distinct notification.
            pyxel.rect(bx, y, 2, 8, col if fade > 0.3 else C_COAST)
            pyxel.text(bx + 5, y, text[:78], c)
            y -= 10
            if y < HUD_TOP + 10: break

    def _draw_radar(self):
        rx, ry, rr = W - 30, HUD_TOP + 24, 20
        pyxel.rect(rx-rr-1, ry-rr-1, rr*2+3, rr*2+3, 0)
        pyxel.circb(rx, ry, rr, C_GRID)
        pyxel.line(rx, ry-rr, rx, ry+rr, C_GRID)
        pyxel.line(rx-rr, ry, rx+rr, ry, C_GRID)
        sa = pyxel.frame_count * 0.04
        # Phosphor sweep: dim trailing radii fade behind the bright leading edge.
        for _k, _tc in ((0.12, 5), (0.24, 5), (0.38, 1), (0.54, 1)):
            _ta = sa - _k
            pyxel.line(rx, ry, rx+int(math.cos(_ta)*rr),
                       ry+int(math.sin(_ta)*rr), _tc)
        pyxel.line(rx, ry, rx+int(math.cos(sa)*rr), ry+int(math.sin(sa)*rr), C_HACK_CYAN)
        scale = rr / max(self.proj.lon_span * 0.5, 0.001)
        for d in self.ble_devices:
            dx = (d.lon - self.player_lon) * scale
            dy = (self.player_lat - d.lat) * scale
            if abs(dx) < rr and abs(dy) < rr:
                pyxel.pset(rx+int(dx), ry+int(dy), C_SUCCESS if d.hacked else d.color)
        for n in self.wifi_networks:
            dx = (n.lon - self.player_lon) * scale
            dy = (self.player_lat - n.lat) * scale
            if abs(dx) < rr and abs(dy) < rr:
                pyxel.pset(rx+int(dx), ry+int(dy), C_SUCCESS if n.hacked else C_WARNING)
        for m in self.markers:
            dx = (m.lon - self.player_lon) * scale
            dy = (self.player_lat - m.lat) * scale
            if abs(dx) < rr and abs(dy) < rr:
                pyxel.pset(rx+int(dx), ry+int(dy), C_ERROR)
        loot_colors = {"wifi": C_SUCCESS, "bt": C_HACK_CYAN,
                       "handshake": C_ERROR, "meshcore": C_WARNING,
                       "meshtastic": C_WARNING}
        step = max(1, len(self.loot_points) // 100)
        for i in range(0, len(self.loot_points), step):
            pt = self.loot_points[i]
            dx = (pt["lon"] - self.player_lon) * scale
            dy = (self.player_lat - pt["lat"]) * scale
            if abs(dx) < rr and abs(dy) < rr:
                pyxel.pset(rx+int(dx), ry+int(dy),
                           loot_colors.get(pt.get("type", ""), C_DIM))
        # ADS-B aircraft on radar
        try:
            for ac in list(self._sdr.aircraft.values()):
                if not ac.has_position:
                    continue
                dx = (ac.lon - self.player_lon) * scale
                dy = (self.player_lat - ac.lat) * scale
                if abs(dx) < rr and abs(dy) < rr:
                    pyxel.pset(rx+int(dx), ry+int(dy), C_HACK_CYAN)
        except Exception:
            pass

        # Player marker at radar center — a small pulsing cross ("you are here"),
        # distinct from the single-pixel contact blips around it.
        _pc = 7 if pyxel.frame_count % 30 < 15 else C_HACK_CYAN
        pyxel.pset(rx, ry, _pc)
        pyxel.pset(rx - 1, ry, _pc)
        pyxel.pset(rx + 1, ry, _pc)
        pyxel.pset(rx, ry - 1, _pc)
        pyxel.pset(rx, ry + 1, _pc)

        # The same CB radio sprite represents the actual ready LoRa owner.
        radio_active = bool(self._active_lora_protocol())
        if radio_active:
            # Radio sprite centered under radar
            rw = getattr(self, '_radio_w', 32)
            rh = getattr(self, '_radio_h', 48)
            ix = rx - rw // 2   # centered on radar X
            iy = ry + rr + 4    # just below radar
            if self._radio_sprite_ok:
                pyxel.blt(ix, iy, 2, 0, 0, rw, rh, 0)  # 0=transparent
            else:
                # Fallback pixel radio
                pyxel.rect(ix + 8, iy + 4, 16, 20, C_HACK_CYAN)
                pyxel.rect(ix + 14, iy, 2, 6, C_HACK_CYAN)

            # Signal waves when bubbles active
            if self._mc_bubbles:
                if pyxel.frame_count % 30 < 20:
                    pyxel.circb(ix - 2, iy + rh // 2, 4, C_WARNING)
                if pyxel.frame_count % 30 < 12:
                    pyxel.circb(ix - 5, iy + rh // 2, 8, C_WARNING)

            # Message bubbles (left of radio) — 5x8 font + 2px padding
            if self._mc_bubbles:
                by = iy + 4
                bx_right = ix - 4
                for txt, _exp in self._mc_bubbles[-3:]:
                    disp = txt[:28]
                    tw = len(disp) * 5 + 8
                    bx = bx_right - tw
                    pyxel.rect(bx, by, tw, 12, 1)
                    pyxel.rectb(bx, by, tw, 12, C_WARNING)
                    pyxel.text(bx + 3, by + 2, disp, C_SUCCESS)
                    by += 12

        # SDR dish indicator (under LoRa radio, or under radar if no LoRa).
        # Only for ADS-B / 433 — Emergency has its own police-lightbar indicator
        # (top-left), so no dish here (a bare dish reads as "ADS-B is on").
        if self._sdr.running and ("adsb" in self._sdr.mode or "433" in self._sdr.mode):
            if radio_active:
                rw = getattr(self, '_radio_w', 32)
                rh = getattr(self, '_radio_h', 48)
                sy = ry + rr + 4 + rh + 4  # below radio sprite
            else:
                sy = ry + rr + 6            # below radar
            sx = rx - 10
            # Pixel-art satellite dish
            pyxel.line(sx + 4, sy + 8, sx + 10, sy + 2, C_HACK_CYAN)
            pyxel.circ(sx + 11, sy + 1, 2, C_HACK_CYAN)
            pyxel.rect(sx + 3, sy + 9, 3, 3, C_HACK_CYAN)
            # Scanning waves
            t = pyxel.frame_count % 40
            if t < 15:
                pyxel.circb(sx + 11, sy + 1, 4 + t // 5, C_SUCCESS)
            # Counter label
            parts = []
            if "adsb" in self._sdr.mode:
                n_pos = sum(1 for a in self._sdr.aircraft.values() if a.has_position)
                parts.append(f"AC:{n_pos}/{len(self._sdr.aircraft)}")
            if "433" in self._sdr.mode:
                parts.append(f"S:{self._sdr.total_sensors_seen}")
            if parts:
                pyxel.text(sx - 2, sy + 14, " ".join(parts), C_HACK_CYAN)

    def _draw_menu(self):
        # Dim background — black scanlines
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)

        # ── Left panel: category tabs + item list ──────────────────────
        PX = 6       # left panel left edge
        PW = 310     # left panel width
        TAB_Y = HUD_TOP + 3

        # Category tabs — 12px tall to fit 8px text + 2px padding each side
        TAB_H = 12
        cat_w = PW // len(MENU_CATS)
        for i, (cat_name, _items) in enumerate(MENU_CATS):
            tx = PX + i * cat_w
            sel = (i == self.menu_cat)
            if sel:
                pyxel.rect(tx, TAB_Y - 1, cat_w - 1, TAB_H, C_HACK_CYAN)
                pyxel.text(tx + 3, TAB_Y + 2, cat_name[:6], 0)
            else:
                pyxel.rectb(tx, TAB_Y - 1, cat_w - 1, TAB_H, C_COAST)
                pyxel.text(tx + 3, TAB_Y + 2, cat_name[:6], C_DIM)

        # Active-tab affordance: a lit (gently breathing) top edge + a downward
        # connector notch tying the open tab to the item list below it.
        _atx = PX + self.menu_cat * cat_w
        _edge = 7 if pyxel.frame_count % 44 < 22 else C_HACK_CYAN
        pyxel.line(_atx, TAB_Y - 1, _atx + cat_w - 2, TAB_Y - 1, _edge)
        _ncx = _atx + cat_w // 2
        _nty = TAB_Y - 1 + TAB_H
        pyxel.tri(_ncx - 3, _nty, _ncx + 3, _nty, _ncx, _nty + 3, C_HACK_CYAN)

        # Item list — 13px tall rows
        _, items = MENU_CATS[self.menu_cat]
        IY = TAB_Y + TAB_H + 3
        IH = 13
        max_vis = (TERM_Y - IY - 24) // IH
        for i, (hotkey, label, cmd, state_key, input_type) in enumerate(items):
            if i >= max_vis:
                break
            ty = IY + i * IH
            sel = (i == self.menu_sel)
            running = self._is_running(state_key)
            is_stop = (state_key == "_stop_all")
            is_na = cmd.startswith("_") and state_key not in (
                "_stop_all", "_reboot", "_dl_map", "_gps_toggle",
                "_lora_toggle", "_sdr_toggle", "_usb_toggle",
                "_wl_screen", "_wpasec_up", "_wpasec_dl", "_flash_esp", "_gameupd",
                "_bt_hid_wip", "_bd_wip", "_race_wip",
                "_srv_wiki", "_srv_ctos", "_srv_nav", "_srv_status"
            ) and not state_key.startswith("_p_")
            if sel:
                bg = C_ERROR if is_stop else C_HACK_CYAN
                pyxel.rect(PX, ty - 1, PW, IH, bg)
                # Live selection glint — a soft highlight sweeping across the
                # active row, so the cursor reads as "alive" not just painted.
                if not is_stop:
                    _sw = int((pyxel.frame_count * 4) % (PW + 24)) - 12
                    for _dx, _sc in ((0, 7), (-1, 6), (1, 6)):
                        _sx = PX + _sw + _dx
                        if PX < _sx < PX + PW - 1:
                            pyxel.line(_sx, ty, _sx, ty + IH - 3, _sc)
                pyxel.rectb(PX, ty - 1, PW, IH, C_TEXT)
                tc = 0
            else:
                bc = C_ERROR if is_stop else (C_COAST if is_na else C_MENU_BORDER)
                pyxel.rectb(PX, ty - 1, PW, IH, bc)
                tc = C_ERROR if is_stop else (C_DIM if is_na else C_TEXT)
            if state_key == "_gameupd":
                if not sel:
                    tc = C_TEXT if self._game_update_available else C_DIM
            pyxel.text(PX + 4, ty + 2, f"[{hotkey}] {label}", tc)
            needs_wifi = state_key in _NEEDS_EXT_WIFI
            if needs_wifi:
                # Red warning triangle with black "!"
                wx = PX + PW - 32
                wy = ty + 1
                pyxel.tri(wx, wy + 8, wx + 4, wy, wx + 8, wy + 8, C_ERROR)
                pyxel.text(wx + 3, wy + 2, "!", 0)
            if running:
                pyxel.text(PX + PW - 18, ty + 2, "ON",
                           C_SUCCESS if not sel else 0)
            if input_type:
                pyxel.text(PX + PW - 8, ty + 2, ">", C_DIM if not sel else 0)

        # Show ext WiFi warning if selected item needs it
        _, sel_items = MENU_CATS[self.menu_cat]
        if self.menu_sel < len(sel_items):
            _sk = sel_items[self.menu_sel][3]
            if _sk in _NEEDS_EXT_WIFI:
                pyxel.text(PX + 3, TERM_Y - 22,
                           "\x17 Requires external WiFi adapter", C_WARNING)

        # Navigation hint — keys highlighted, labels dim (reads as a key legend).
        _hx, _hy = PX + 1, TERM_Y - 10
        for _seg, _sc in (("\x1c/\x1d", C_HACK_CYAN), (" cat  ", C_DIM),
                          ("\x1e/\x1f", C_HACK_CYAN), (" sel  ", C_DIM),
                          ("ENTER", C_SUCCESS), ("  ", C_DIM),
                          ("TAB", C_WARNING)):
            pyxel.text(_hx, _hy, _seg, _sc)
            _hx += len(_seg) * 5

        # ─ ESP32 status + category (top-left of right panel) ─
        sx2 = PX + 310 + 8
        esp_c = C_SUCCESS if self._esp32 else C_ERROR
        pyxel.text(sx2, HUD_TOP + 4,  "ESP32:", C_DIM)
        _est = "OK" if self._esp32 else "OFFLINE"
        pyxel.text(sx2, HUD_TOP + 16, _est, esp_c)
        # Live-link heartbeat — a pulsing dot beside the status when connected.
        _ld = sx2 + len(_est) * 5 + 5
        if self._esp32:
            pyxel.circb(_ld, HUD_TOP + 19, 3, C_SUCCESS)
            if pyxel.frame_count % 40 < 24:
                pyxel.circ(_ld, HUD_TOP + 19, 1, C_SUCCESS)
        else:
            pyxel.circb(_ld, HUD_TOP + 19, 2, C_ERROR)
        cat_name = MENU_CATS[self.menu_cat][0]
        pyxel.text(sx2, HUD_TOP + 30, f"// {cat_name}", C_HACK_CYAN)

    def _draw_menu_hacker(self):
        """Draw hacker sprite + speech bubble ON TOP of menu dim overlay.

        Black rect under sprite covers the dim-overlay scanlines completely.
        Then blt with colkey=0 makes the sprite's black background transparent,
        revealing the black rect beneath — so the sprite keeps its dark look
        with no blue scanlines bleeding through.

        Sprite changes per active menu tab (SCAN, SNIFF, etc.)
        """
        # Swap sprite if tab changed
        target_tab = self.menu_cat
        if target_tab != self._current_menu_sprite:
            if target_tab in self._menu_sprites:
                try:
                    pyxel.image(1).load(0, 0, self._menu_sprites[target_tab])
                    self._current_menu_sprite = target_tab
                except Exception:
                    pass
            elif self._current_menu_sprite != 4 and 4 in self._menu_sprites:
                # Fallback to default (SYSTEM/hacker) for tabs without sprite
                try:
                    pyxel.image(1).load(0, 0, self._menu_sprites[4])
                    self._current_menu_sprite = 4
                except Exception:
                    pass

        rpx = 490
        feet_y = TERM_Y + 60  # legs extend below terminal line

        if self._hacker_sprite_ok and self._hacker_w > 0:
            # Breathing idle bob (+/-1px) so the character reads as alive.
            _bob = int(math.sin(pyxel.frame_count * 0.05) * 1.5)
            sx = rpx - self._hacker_w // 2
            sy = feet_y - self._hacker_h + _bob
            # BLACK rect covers dim overlay scanlines — padded so the bob never
            # exposes the blue scanlines above/below the sprite.
            bg_top = max(sy - 2, HUD_TOP)
            pyxel.rect(sx, bg_top, self._hacker_w, feet_y + 2 - bg_top, 0)
            # Sprite on top — colkey=0 transparent pixels show black beneath
            pyxel.blt(sx, sy, 1, 0, 0, self._hacker_w, self._hacker_h, 0)
            bubble_cx = sx + self._hacker_w // 2
            bubble_y = sy + self._hacker_h // 3
        else:
            pyxel.rect(rpx - 10, feet_y - 80, 20, 80, 1)
            pyxel.circ(rpx, feet_y - 88, 10, 1)
            bubble_cx = rpx
            bubble_y = feet_y - 100

        # ─ Comic speech bubble (bigger, at torso level) ──────────────
        quip_list = HACKER_QUIPS[self.menu_cat % len(HACKER_QUIPS)]
        quip = quip_list[(pyxel.frame_count // 90) % len(quip_list)]
        quip_upper = quip.upper()
        qw = len(quip_upper) * 5 + 16
        qh = 20
        qx = bubble_cx - (self._hacker_w if self._hacker_w else 60) - qw // 2
        qy = bubble_y
        if qx < 2:
            qx = 2
        if qy < HUD_TOP + 2:
            qy = HUD_TOP + 2
        if qx + qw > W - 4:
            qx = W - qw - 4
        # Bubble body
        pyxel.rect(qx + 2, qy, qw - 4, qh, 0)
        pyxel.rect(qx, qy + 2, qw, qh - 4, 0)
        pyxel.rect(qx + 1, qy + 1, qw - 2, qh - 2, 0)
        # Border
        pyxel.line(qx + 3, qy, qx + qw - 4, qy, C_HACK_CYAN)
        pyxel.line(qx + 3, qy + qh - 1, qx + qw - 4, qy + qh - 1, C_HACK_CYAN)
        pyxel.line(qx, qy + 3, qx, qy + qh - 4, C_HACK_CYAN)
        pyxel.line(qx + qw - 1, qy + 3, qx + qw - 1, qy + qh - 4, C_HACK_CYAN)
        # Corners
        for cx, cy in [(qx+1,qy+1),(qx+2,qy+1),(qx+1,qy+2),
                        (qx+qw-2,qy+1),(qx+qw-3,qy+1),(qx+qw-2,qy+2),
                        (qx+1,qy+qh-2),(qx+2,qy+qh-2),(qx+1,qy+qh-3),
                        (qx+qw-2,qy+qh-2),(qx+qw-3,qy+qh-2),(qx+qw-2,qy+qh-3)]:
            pyxel.pset(cx, cy, C_HACK_CYAN)
        # Tail → right
        tail_x = qx + qw
        tail_y = qy + qh // 2
        pyxel.line(tail_x, tail_y - 2, tail_x + 6, tail_y, C_HACK_CYAN)
        pyxel.line(tail_x, tail_y + 2, tail_x + 6, tail_y, C_HACK_CYAN)
        # Text — Spleen 5x8, typewriter reveal + blinking caret (live chatter).
        text_x = qx + (qw - len(quip_upper) * 5) // 2
        text_y = qy + (qh - 8) // 2
        _wpos = pyxel.frame_count % 90
        _shown = quip_upper[:min(len(quip_upper), 2 + _wpos // 2)]
        pyxel.text(text_x, text_y, _shown, C_HACK_CYAN)
        if _wpos % 12 < 7:
            pyxel.text(text_x + len(_shown) * 5, text_y, "_", C_HACK_CYAN)

    # ------------------------------------------------------------------
    # Picker overlays (network / portal selection)
    # ------------------------------------------------------------------

    def _draw_net_picker(self):
        """Draw Evil Twin network selection overlay."""
        nets = self._attack_scan_results
        # Build client count lookup from sniffer data (if available)
        cli_map: dict[str, int] = {}
        for sap in self.state.sniffer_aps:
            if sap.ssid:
                cli_map[sap.ssid] = sap.client_count
        # Solid dim background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        # Dialog box — 14px rows for 5x8 font
        ROW_H = 14
        dw, dh = 540, min(260, 60 + len(nets) * ROW_H)
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_HACK_CYAN)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 4, "EVIL TWIN — Select Target Network", C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        # Column headers — spaced for 5x8 font (widened from 4x6)
        hy = dy + 18
        pyxel.text(dx + 26,  hy, "SSID",  C_DIM)
        pyxel.text(dx + 220, hy, "BSSID", C_DIM)
        pyxel.text(dx + 360, hy, "CH",    C_DIM)
        pyxel.text(dx + 390, hy, "RSSI",  C_DIM)
        pyxel.text(dx + 430, hy, "Auth",  C_DIM)
        pyxel.text(dx + 495, hy, "Cli",   C_DIM)
        pyxel.line(dx + 2, hy + 10, dx + dw - 3, hy + 10, 1)
        # Network list with scroll
        max_vis = (dh - 70) // ROW_H
        start = max(0, self._et_net_sel - max_vis + 1)
        y = hy + 14
        picked = self._et_net_selected
        for i in range(start, min(len(nets), start + max_vis)):
            net = nets[i]
            cursor = (i == self._et_net_sel)
            checked = (i in picked)
            if cursor:
                pyxel.rect(dx + 2, y - 1, dw - 4, ROW_H - 1, C_HACK_CYAN)
                # Live selection glint — matches the game's other pickers.
                _ng = int((pyxel.frame_count * 4) % (dw - 4 + 24)) - 12
                for _dx2, _sc in ((0, 7), (-1, 6), (1, 6)):
                    _nx = dx + 2 + _ng + _dx2
                    if dx + 2 < _nx < dx + dw - 3:
                        pyxel.line(_nx, y - 1, _nx, y + ROW_H - 4, _sc)
            # Checkbox: [X] or [ ]
            mark = "X" if checked else " "
            pyxel.text(dx + 4, y + 2, f"[{mark}]",
                       0 if cursor else (C_WARNING if checked else C_DIM))
            ssid = (net.ssid or "<hidden>")[:24]
            bssid = net.bssid if hasattr(net, 'bssid') else "?"
            ch = str(net.channel) if hasattr(net, 'channel') else "?"
            rssi = str(net.rssi) if hasattr(net, 'rssi') else "?"
            auth = (net.auth[:8] if hasattr(net, 'auth') else "?")
            n_cli = cli_map.get(net.ssid, -1)
            cli_str = str(n_cli) if n_cli >= 0 else "-"
            c = 0 if cursor else C_SUCCESS
            pyxel.text(dx + 26,  y + 2, ssid,  c)
            pyxel.text(dx + 220, y + 2, bssid, 0 if cursor else C_TEXT)
            pyxel.text(dx + 360, y + 2, ch,    0 if cursor else C_DIM)
            pyxel.text(dx + 390, y + 2, rssi,  0 if cursor else C_DIM)
            pyxel.text(dx + 430, y + 2, auth,  0 if cursor else C_DIM)
            cli_c = (0 if cursor else C_WARNING) if n_cli > 0 else (
                    0 if cursor else C_DIM)
            pyxel.text(dx + 495, y + 2, cli_str, cli_c)
            y += ROW_H
        # Scroll indicator
        if len(nets) > max_vis:
            bar_total = dh - 70
            bar_h = max(4, bar_total * max_vis // len(nets))
            bar_y = dy + 30 + (bar_total - bar_h) * start // max(
                1, len(nets) - max_vis)
            pyxel.rect(dx + dw - 4, bar_y, 2, bar_h, C_HACK_CYAN)
        # Selection info
        n_sel = len(picked)
        if n_sel > 0:
            pyxel.text(dx + dw - 130, dy + 4,
                       f"1st=clone {n_sel-1}=deauth" if n_sel > 1 else "1 target",
                       C_WARNING)
        # Hints
        has_cli = bool(cli_map)
        hint = "SPACE mark  ENTER confirm  ESC cancel"
        if not has_cli:
            hint += "  (run Sniffer first for client counts)"
        pyxel.text(dx + 4, dy + dh - 11, hint, C_DIM)
        # Count
        pyxel.text(dx + dw - 90, dy + 4, f"{len(nets)} networks", C_DIM)

    def _draw_portal_picker(self):
        """Draw portal selection overlay."""
        portals = self._portal_list
        tag = "EVIL PORTAL" if self._attack_mode == "evil_portal" else "EVIL TWIN"
        # Solid dim background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        # Dialog box — 14px rows for 5x8 font
        ROW_H = 14
        dw, dh = 420, min(220, 60 + len(portals) * ROW_H)
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 4, f"{tag} — Select Portal", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        # SSID info
        pyxel.text(dx + 4, dy + 18, f"SSID: {self._portal_ssid}", C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 28, dx + dw - 3, dy + 28, 1)
        # Portal list
        max_vis = (dh - 60) // ROW_H
        start = max(0, self._portal_sel - max_vis + 1)
        y = dy + 32
        for i in range(start, min(len(portals), start + max_vis)):
            name, html = portals[i]
            sel = (i == self._portal_sel)
            if sel:
                pyxel.rect(dx + 2, y - 1, dw - 4, ROW_H - 1, C_WARNING)
            # Type indicator
            if html is None:
                src = "[FW]"
                sc = C_DIM
            elif name.startswith("[C]"):
                src = "[USR]"
                sc = C_HACK_CYAN
            else:
                src = "[B-IN]"
                sc = C_SUCCESS
            c = 0 if sel else C_TEXT
            pyxel.text(dx + 6, y + 3, src, 0 if sel else sc)
            pyxel.text(dx + 44, y + 3, name[:28], c)
            # Upload time estimate
            if html is None:
                t_str = "instant"
            else:
                import math
                b64_len = math.ceil(len(html.encode("utf-8")) * 4 / 3)
                secs = b64_len / 128 * 0.2
                t_str = f"~{int(secs)}s" if secs >= 1 else "<1s"
            pyxel.text(dx + dw - 46, y + 3, t_str, 0 if sel else C_DIM)
            y += ROW_H
        # Hints
        hint = "UP/DOWN select  ENTER confirm  ESC "
        hint += "back" if self._attack_mode == "evil_twin" else "cancel"
        pyxel.text(dx + 4, dy + dh - 11, hint, C_DIM)

    # ------------------------------------------------------------------
    # MeshCore region picker (ADDONS > MeshCore Region)
    # ------------------------------------------------------------------

    def _update_mc_region_picker(self):
        """UP/DOWN/ENTER/ESC for the MeshCore regional preset picker."""
        if getattr(self, "_rf_profiles", None) is not None:
            self._update_radio_profile_picker("meshcore")
            return
        import pyxel as px
        from .lora_manager import MESHCORE_PRESETS, save_meshcore_config
        keys = list(MESHCORE_PRESETS.keys())
        if not keys:
            self._mc_region_screen = False
            return
        if px.btnp(px.KEY_UP):
            self._mc_region_sel = max(0, self._mc_region_sel - 1)
        elif px.btnp(px.KEY_DOWN):
            self._mc_region_sel = min(len(keys) - 1, self._mc_region_sel + 1)
        elif px.btnp(px.KEY_RETURN):
            new_region = keys[self._mc_region_sel]
            old_region = self._mc_region
            # This picker controls MeshCore only. RNode and Meshtastic keep
            # their current RF profile until explicitly switched by the user.
            if (self._lora_enabled and self._lora.running
                    and self._lora.mode == "meshcore" and new_region != old_region):
                if not self._lora.stop():
                    self.msg("[MC] Stop pending; region unchanged", C_ERROR)
                    return
                if not self._lora.start_meshcore(new_region):
                    self.state.lora_running = False
                    self.state.lora_mode = ""
                    self._radio_protocol = ""
                    self.msg(f"[MC] Retune failed: {self._lora.last_error}", C_ERROR)
                    return
            self._mc_region = new_region
            # Persist to ~/.watchdogs_meshcore.json
            try:
                save_meshcore_config(self._mc_node_name,
                                     self._mc_channels_list,
                                     region=new_region)
            except Exception as exc:
                self._term_add(f"[MC] config save failed: {exc}", raw=True)
            label = MESHCORE_PRESETS[new_region][4]
            self.msg(f"[MC] Region: {label}", C_SUCCESS)
            self._mc_region_screen = False
        elif px.btnp(px.KEY_ESCAPE):
            self._mc_region_screen = False
            self._esc_consumed_frame = pyxel.frame_count

    def _draw_mc_region_picker(self):
        """Draw MeshCore regional preset picker overlay."""
        if getattr(self, "_rf_profiles", None) is not None:
            self._draw_radio_profile_picker("meshcore")
            return
        from .lora_manager import MESHCORE_PRESETS
        keys = list(MESHCORE_PRESETS.keys())
        # Solid dim background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        ROW_H = 14
        dw = 440
        dh = min(240, 70 + len(keys) * ROW_H)
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        pyxel.text(dx + 4, dy + 4, "MESHCORE — Select Region", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        pyxel.text(dx + 4, dy + 18,
                   f"Current: {MESHCORE_PRESETS[self._mc_region][4]}",
                   C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 28, dx + dw - 3, dy + 28, 1)
        y = dy + 32
        for i, key in enumerate(keys):
            freq, sf, cr, bw, label = MESHCORE_PRESETS[key]
            sel = (i == self._mc_region_sel)
            if sel:
                pyxel.rect(dx + 2, y - 1, dw - 4, ROW_H - 1, C_WARNING)
            c_label = 0 if sel else C_TEXT
            c_hz = 0 if sel else C_DIM
            pyxel.text(dx + 6, y + 3, label, c_label)
            freq_str = f"{freq / 1_000_000:.3f}MHz"
            pyxel.text(dx + dw - 64, y + 3, freq_str, c_hz)
            y += ROW_H
        pyxel.text(dx + 4, dy + dh - 11,
                   "UP/DOWN select   ENTER apply   ESC cancel", C_DIM)

    # ------------------------------------------------------------------
    # Flipper Zero screen
    # ------------------------------------------------------------------

    _FLIPPER_MENU = [
        # SubGHz Toolkit
        ("--- SUBGHZ TOOLKIT ---",      None),
        ("Signal Scanner (433 MHz)",    "rx_433"),
        ("Signal Scanner (868 MHz)",    "rx_868"),
        ("Signal Scanner (RAW 433)",    "rx_raw_433"),
        ("Replay Signals (SD card)",    "files"),
        ("Flipper Chat (SubGHz)",       "chat"),
        # NFC Toolkit
        ("--- NFC TOOLKIT ---",         None),
        ("NFC Read Tag",                "nfc_read"),
        ("NFC Scanner",                 "nfc_scan"),
        ("NFC Emulate (SD card)",       "nfc_files"),
        # System
        ("--- SYSTEM ---",              None),
        ("Device Info",                 "info"),
    ]

    def _update_flipper_screen(self):
        px = pyxel
        if self._flipper_mode == "idle":
            menu_len = len(self._FLIPPER_MENU)
            if px.btnp(px.KEY_UP):
                sel = self._flipper_sel - 1
                while sel >= 0 and self._FLIPPER_MENU[sel][1] is None:
                    sel -= 1
                if sel >= 0:
                    self._flipper_sel = sel
            elif px.btnp(px.KEY_DOWN):
                sel = self._flipper_sel + 1
                while sel < menu_len and self._FLIPPER_MENU[sel][1] is None:
                    sel += 1
                if sel < menu_len:
                    self._flipper_sel = sel
            elif px.btnp(px.KEY_RETURN):
                _, action = self._FLIPPER_MENU[self._flipper_sel]
                if action:
                    self._flipper_action(action)
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_screen = False
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "rx_active":
            if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_X):
                self._flipper.subghz_stop()
                self._flipper.stop_rx()
                self._flipper_mode = "idle"
                self._flipper_log.append(("Scanner stopped", C_WARNING))
                self._esc_consumed_frame = px.frame_count
            elif px.btnp(px.KEY_PAGEUP):
                self._flipper_scroll = min(self._flipper_scroll + 5,
                                           max(0, len(self._flipper_log) - 10))
            elif px.btnp(px.KEY_PAGEDOWN):
                self._flipper_scroll = max(0, self._flipper_scroll - 5)
        elif self._flipper_mode == "files":
            if px.btnp(px.KEY_UP):
                self._flipper_sel = max(0, self._flipper_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._flipper_sel = min(len(self._flipper_files) - 1,
                                        self._flipper_sel + 1)
            elif px.btnp(px.KEY_RETURN) and self._flipper_files:
                path, display = self._flipper_files[self._flipper_sel]
                self._flipper_log.append((f"Transmitting: {display}", C_WARNING))
                self._flipper.subghz_tx_file(path)
                self.gain_xp(25)
                self._flipper_log.append((f"Signal sent!", C_SUCCESS))
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "nfc_emulate":
            if px.btnp(px.KEY_UP):
                self._flipper_sel = max(0, self._flipper_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._flipper_sel = min(len(self._flipper_files) - 1,
                                        self._flipper_sel + 1)
            elif px.btnp(px.KEY_RETURN) and self._flipper_files:
                path, display = self._flipper_files[self._flipper_sel]
                self._flipper_log.append(
                    (f"Emulating: {display}", C_WARNING))
                self._flipper.start_rx(self._on_flipper_line)
                self._flipper.nfc_emulate(path)
                self._flipper_mode = "nfc_active"
                self.gain_xp(25)
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "nfc_active":
            if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_X):
                self._flipper.nfc_stop()
                self._flipper.stop_rx()
                self._flipper_mode = "idle"
                self._flipper_log.append(("NFC stopped", C_WARNING))
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "folders":
            if px.btnp(px.KEY_UP):
                self._flipper_sel = max(0, self._flipper_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._flipper_sel = min(len(self._flipper_folders) - 1,
                                        self._flipper_sel + 1)
            elif px.btnp(px.KEY_RETURN) and self._flipper_folders:
                folder = self._flipper_folders[self._flipper_sel]
                self._load_flipper_folder(folder)
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count
        else:
            if px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count

    def _flipper_action(self, action: str):
        if not self._flipper.ensure_connected():
            self._flipper_log.append(("Flipper not connected!", C_ERROR))
            return
        if not self._flipper_used:
            self._flipper_used = True
            self._earn_badge("flipper")

        if action == "rx_433":
            self._flipper_log.append(("Scanning 433.92 MHz...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.subghz_rx(433920000)
        elif action == "rx_868":
            self._flipper_log.append(("Scanning 868.35 MHz...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.subghz_rx(868350000)
        elif action == "rx_raw_433":
            self._flipper_log.append(("RAW capture 433.92 MHz...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.subghz_rx_raw(433920000)
        elif action == "files":
            self._flipper_log.append(("Loading SD card...", C_DIM))
            self._load_flipper_sd()
        elif action == "nfc_read":
            self._flipper_log.append(("Reading NFC tag...", 12))
            self._flipper_log.append(("Hold tag near Flipper", C_DIM))
            def _nfc_read():
                lines = self._flipper.send("nfc")
                time.sleep(0.3)
                lines = self._flipper.send("mfu info")
                tag_data = []
                uid = ""
                tag_type = ""
                for l in lines:
                    if l and not l.startswith("[nfc]"):
                        self._flipper_log.append((l, C_SUCCESS))
                        tag_data.append(l)
                        if "UID:" in l:
                            uid = l.split("UID:")[-1].strip()
                        if "Type:" in l:
                            tag_type = l.split("Type:")[-1].strip()
                self._flipper.send("exit")
                self.gain_xp(15)
                # Store tag data for save dialog
                if tag_data and self.loot:
                    self._nfc_pending_save = {
                        "data": tag_data,
                        "uid": uid,
                        "type": tag_type,
                    }
                    # Show name input dialog
                    default_name = tag_type.replace(" ", "_") if tag_type else "tag"
                    self._flipper_screen = False
                    self.input_mode = True
                    self.input_fields = [{"label": "Tag name", "value": default_name}]
                    self.input_field_idx = 0
                    self._input_pending_cat = -8  # NFC tag save
            threading.Thread(target=_nfc_read, daemon=True).start()
        elif action == "nfc_scan":
            self._flipper_log.append(("NFC Scanner starting...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.nfc_scan()
        elif action == "nfc_files":
            self._flipper_log.append(("Loading NFC cards...", C_DIM))
            lines = self._flipper.storage_list("/ext/nfc")
            files = []
            for l in lines:
                if l.startswith("[F]") and ".nfc" in l:
                    fname = l.split("]")[-1].strip().split(" ")[0]
                    path = f"/ext/nfc/{fname}"
                    display = fname.replace(".nfc", "")
                    files.append((path, display))
            if files:
                self._flipper_files = files
                self._flipper_mode = "nfc_emulate"
                self._flipper_sel = 0
                self._flipper_log.append(
                    (f"Found {len(files)} NFC cards", C_SUCCESS))
            else:
                self._flipper_log.append(("No .nfc files on SD", C_WARNING))
        elif action == "chat":
            self._flipper_log.append(("SubGHz Chat — not yet implemented", C_DIM))
        elif action == "info":
            lines = self._flipper.send("device_info")
            for l in lines[:20]:
                self._flipper_log.append((l, C_DIM))
        elif action == "led":
            self._flipper.led(0, 0, 255)
            self._flipper_log.append(("LED: blue", C_HACK_CYAN))
            threading.Thread(target=lambda: (
                time.sleep(1), self._flipper.led(0, 255, 0),
                time.sleep(1), self._flipper.led(255, 0, 0),
                time.sleep(1), self._flipper.led(0, 0, 0)),
                daemon=True).start()

    def _load_flipper_sd(self):
        """Load SubGHz folder structure from Flipper SD."""
        lines = self._flipper.send("storage tree /ext/subghz")
        folders = set()
        root_files = []
        for l in lines:
            if l.startswith("[D] /ext/subghz/"):
                name = l.split("/ext/subghz/")[-1]
                if "/" not in name and name not in ("assets", "playlist", "remote"):
                    folders.add(name)
            elif l.startswith("[F] /ext/subghz/") and ".sub" in l:
                path = l.split(" ")[1] if " " in l else l[4:]
                path = path.split(" ")[0]  # remove size
                fname = path.split("/")[-1]
                # Only root-level .sub files
                parts = path.replace("/ext/subghz/", "").split("/")
                if len(parts) == 1:
                    root_files.append((path, fname))
                elif len(parts) == 2:
                    folders.add(parts[0])

        self._flipper_folders = sorted(folders)
        self._flipper_root_files = root_files
        if self._flipper_folders:
            self._flipper_mode = "folders"
            self._flipper_sel = 0
            self._flipper_log.append(
                (f"Found {len(self._flipper_folders)} folders, "
                 f"{len(root_files)} root files", C_SUCCESS))
        elif root_files:
            self._flipper_files = root_files
            self._flipper_mode = "files"
            self._flipper_sel = 0
        else:
            self._flipper_log.append(("No .sub files found on SD", C_WARNING))

    def _load_flipper_folder(self, folder: str):
        """Load .sub files from a specific folder."""
        lines = self._flipper.storage_list(f"/ext/subghz/{folder}")
        files = []
        for l in lines:
            if l.startswith("[F]") and ".sub" in l:
                fname = l.split("]")[-1].strip().split(" ")[0]
                path = f"/ext/subghz/{folder}/{fname}"
                files.append((path, fname))
        if files:
            self._flipper_files = files
            self._flipper_mode = "files"
            self._flipper_sel = 0
            self._flipper_log.append(
                (f"{folder}: {len(files)} signals", C_SUCCESS))
        else:
            self._flipper_log.append((f"{folder}: empty", C_WARNING))

    def _on_flipper_line(self, line: str):
        """Callback from Flipper RX background thread."""
        if line.startswith(">"):
            return
        self._flipper_log.append((line, C_SUCCESS))
        self.gain_xp(5)  # XP for captured signal
        if len(self._flipper_log) > 500:
            self._flipper_log = self._flipper_log[-500:]

    _FLIPPER_ASCII = [
        "_.-------.._                    -,",
        '.-"```"--..,,_/ /`-,               -,  \\',
        '.:"          /:/  /\'\\  \\     ,_...,  `. |  |',
        "/       ,----/:/  /`\\ _\\~`_-\"`     _;",
        "'      / /`\"\"\"'\\ \\ \\.~`_-'      ,-\"'/",
        "|      | |  0    | | .-'      ,/`  /",
        "|    ,..\\  \\     ,.-\"`       ,/`    /",
        ';    :    `/`""\\`           ,/--==,/-----,',
        "|    `-...|        -.___-Z:_______J...---;",
        ":         `                           _-'",
        " _L_  _     ___  ___  ___  ___  ____--\"`",
        " | __|| |   |_ _|| _ || _ || __|| _ |",
        " | _| | |__  | | |  _||  _|| _| |   /",
        " |_|  |____||___||_|  |_|  |___||_|_\\",
    ]

    def _draw_flipper_screen(self):
        """Draw fullscreen Flipper Zero interface."""
        pyxel.cls(0)
        pyxel.rectb(1, 1, W - 2, H - 2, C_WARNING)
        pyxel.rect(2, 2, W - 4, 14, 1)
        name = self._flipper.device_name or "Flipper Zero"
        fw = self._flipper.firmware or "?"
        conn = "ONLINE" if self._flipper.connected else "OFFLINE"
        conn_c = C_SUCCESS if self._flipper.connected else C_ERROR
        pyxel.text(6, 5, f"FLIPPER ZERO // {name} // {fw}", C_WARNING)
        pyxel.text(W - 75, 5, conn, conn_c)
        pyxel.text(W - 35, 5, "[ESC]", C_DIM)
        pyxel.line(1, 16, W - 2, 16, C_WARNING)

        mid_y = 18
        split_y = H // 2 + 50  # top: ASCII + menu, bottom: log
        ROW_H = 13              # 5x8 font with 5px gap/padding

        if self._flipper_mode == "idle":
            # ASCII dolphin on left — 9px per line (8px glyph + 1)
            for i, line in enumerate(self._FLIPPER_ASCII):
                pyxel.text(10, mid_y + 2 + i * 9, line, C_WARNING)

            # Menu on right
            mx = 310
            y = mid_y + 2
            for i, (label, action) in enumerate(self._FLIPPER_MENU):
                if action is None:
                    # Section header
                    pyxel.text(mx, y + 2, label.strip("- "), C_HACK_CYAN)
                    pyxel.line(mx, y + 11, mx + 140, y + 11, 1)
                    y += ROW_H
                else:
                    sel = (i == self._flipper_sel)
                    if sel:
                        pyxel.rect(mx - 2, y - 1, 200, ROW_H - 1, C_WARNING)
                        # Live selection glint — matches the game's other menus.
                        _fg = int((pyxel.frame_count * 4) % (200 + 24)) - 12
                        for _dx, _sc in ((0, 7), (-1, 6), (1, 6)):
                            _fx = mx - 2 + _fg + _dx
                            if mx - 2 < _fx < mx + 197:
                                pyxel.line(_fx, y - 1, _fx, y + ROW_H - 4, _sc)
                    c = 0 if sel else C_TEXT
                    pyxel.text(mx + 2, y + 2, label, c)
                    y += ROW_H

            pyxel.text(mx, split_y - 12,
                       "UP/DOWN  ENTER  ESC=close", C_DIM)

        elif self._flipper_mode == "folders":
            pyxel.text(10, mid_y + 2, "SIGNAL LIBRARY (SD)", C_HACK_CYAN)
            pyxel.line(10, mid_y + 12, 200, mid_y + 12, 1)
            max_vis = (split_y - mid_y - 32) // ROW_H
            scroll = max(0, self._flipper_sel - max_vis + 1)
            folders = self._flipper_folders
            for i in range(scroll, min(len(folders), scroll + max_vis)):
                y = mid_y + 16 + (i - scroll) * ROW_H
                sel = (i == self._flipper_sel)
                if sel:
                    pyxel.rect(8, y - 1, 300, ROW_H - 2, C_WARNING)
                c = 0 if sel else C_TEXT
                pyxel.text(10, y + 1, f"[DIR] {folders[i]}", c)
            # Also show root files count
            if self._flipper_root_files:
                pyxel.text(320, mid_y + 2,
                           f"+{len(self._flipper_root_files)} root signals",
                           C_DIM)
            pyxel.text(10, split_y - 12,
                       "ENTER=open folder  ESC=back", C_DIM)

        elif self._flipper_mode == "files":
            pyxel.text(10, mid_y + 2, "SIGNALS — ENTER to transmit", C_HACK_CYAN)
            pyxel.line(10, mid_y + 12, 260, mid_y + 12, 1)
            max_vis = (split_y - mid_y - 32) // ROW_H
            scroll = max(0, self._flipper_sel - max_vis + 1)
            files = self._flipper_files
            for i in range(scroll, min(len(files), scroll + max_vis)):
                y = mid_y + 16 + (i - scroll) * ROW_H
                sel = (i == self._flipper_sel)
                if sel:
                    pyxel.rect(8, y - 1, 400, ROW_H - 2, C_WARNING)
                c = 0 if sel else C_TEXT
                _, display = files[i]
                pyxel.text(10, y + 1, f"[TX] {display}", c)
            pyxel.text(10, split_y - 12,
                       "ENTER=transmit  ESC=back", C_DIM)

        elif self._flipper_mode == "nfc_emulate":
            pyxel.text(10, mid_y + 2, "NFC CARDS — select to emulate", C_HACK_CYAN)
            pyxel.line(10, mid_y + 12, 270, mid_y + 12, 1)
            max_vis = (split_y - mid_y - 32) // ROW_H
            scroll = max(0, self._flipper_sel - max_vis + 1)
            files = self._flipper_files
            for i in range(scroll, min(len(files), scroll + max_vis)):
                y = mid_y + 16 + (i - scroll) * ROW_H
                sel = (i == self._flipper_sel)
                if sel:
                    pyxel.rect(8, y - 1, 400, ROW_H - 2, 12)
                c = 0 if sel else C_TEXT
                _, display = files[i]
                pyxel.text(10, y + 1, f"[NFC] {display}", c)
            pyxel.text(10, split_y - 12,
                       "ENTER=emulate  ESC=back", C_DIM)

        elif self._flipper_mode == "nfc_active":
            pyxel.text(10, mid_y + 2, "NFC EMULATION — ACTIVE", 12)
            dots = "." * ((pyxel.frame_count // 15) % 4)
            pyxel.text(10, mid_y + 16, f"Card emulating{dots}", C_SUCCESS)
            pyxel.text(10, mid_y + 30,
                       "Hold Flipper near reader", C_DIM)
            pyxel.text(220, mid_y + 2, "[X] or [ESC] to stop", C_DIM)

        elif self._flipper_mode == "rx_active":
            pyxel.text(10, mid_y + 2, "SIGNAL SCANNER — LIVE", C_HACK_CYAN)
            freq_txt = "Listening for signals..."
            pyxel.text(10, mid_y + 16, freq_txt, C_SUCCESS)
            dots = "." * ((pyxel.frame_count // 15) % 4)
            pyxel.text(10, mid_y + 30, f"Scanning{dots}", C_DIM)
            pyxel.text(220, mid_y + 2, "[X] or [ESC] to stop", C_DIM)

        # Bottom: log output (9px per line for 5x8 font)
        pyxel.line(1, split_y, W - 2, split_y, C_COAST)
        pyxel.text(6, split_y + 3, "OUTPUT", C_DIM)
        cnt = len(self._flipper_log)
        pyxel.text(70, split_y + 3, f"({cnt} lines)", C_DIM)
        log_y = split_y + 14
        LINE_H = 9
        max_lines = (H - log_y - 4) // LINE_H
        total = len(self._flipper_log)
        if self._flipper_scroll == 0:
            start = max(0, total - max_lines)
            end = total
        else:
            end = max(0, total - self._flipper_scroll)
            start = max(0, end - max_lines)
        y = log_y
        for i in range(start, end):
            text, color = self._flipper_log[i]
            pyxel.text(6, y, text[:120], color)
            y += LINE_H
            if y >= H - 4:
                break

    def _draw_flash_screen(self):
        """Draw board picker overlay for firmware flash."""
        from .config import FLASH_BOARDS
        ROW_H = 14
        boards = list(FLASH_BOARDS.items())
        dw = 340
        dh = max(120, 50 + len(boards) * ROW_H + 24)
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        pyxel.text(dx + 4, dy + 4, "FLASH ESP32 — Select Board", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)

        if self._flash_running:
            pyxel.text(dx + 4, dy + 22, "Flashing in progress...", C_HACK_CYAN)
            pyxel.text(dx + 4, dy + 36, "Check terminal for status", C_DIM)
            pyxel.text(dx + 4, dy + dh - 14, "ESC to close", C_DIM)
        else:
            fw_info = (f"Current: v{self._fw_version}"
                       if self._fw_version else "Current: unknown")
            if self._fw_update_available:
                fw_info += f" -> v{self._fw_remote_version}"
            pyxel.text(dx + 4, dy + 18, fw_info, C_DIM)

            y = dy + 34
            for i, (key, profile) in enumerate(boards):
                sel = (i == self._flash_sel)
                if sel:
                    pyxel.rect(dx + 2, y - 1, dw - 4, ROW_H - 1, C_WARNING)
                c = 0 if sel else C_TEXT
                pyxel.text(dx + 6, y + 2, f"[{i+1}]", 0 if sel else C_DIM)
                pyxel.text(dx + 28, y + 2, profile["label"], c)
                y += ROW_H

            pyxel.text(dx + 4, dy + dh - 14,
                       "UP/DOWN  ENTER flash  ESC cancel", C_DIM)

    # ------------------------------------------------------------------
    # Update Game: own-fork releases, persistent notices and code-only upgrades.
    # ------------------------------------------------------------------

    def _init_game_updates(self):
        self._game_updates = GameUpdates(root=Path(__file__).resolve().parent.parent)
        self._game_update_events = Queue()
        self._game_check_started = False
        self._game_update_checking = False
        self._game_update_status = "idle"
        self._game_update_message = "Press R to check for updates."
        self._game_update_release = None
        self._game_update_notice = None
        self._game_update_notice_queued = None
        self._game_update_seen_in_session = set()
        self._game_remote_version = ""
        self._game_update_available = False
        self._gameupd_screen = False
        self._gameupd_running = False
        self._gameupd_done = False
        self._gameupd_ok = False
        self._gameupd_log = []
        self._gameupd_scroll = 0
        self._gameupd_confirm = False
        self._gameupd_plan = None
        self._gameupd_open_frame = -1
        self._gameupd_install_frame = -1
        self._gameupd_closed_for_install = False
        self._gameupd_avatar = None
        try:
            from . import __version__
            self._game_installed_version = __version__
        except ImportError:
            self._game_installed_version = "?"
        try:
            asset = Path(__file__).resolve().parent.parent / "assets" / "kilogramowy-avatar-32.png"
            self._gameupd_avatar = pyxel.Image.from_image(str(asset))
        except Exception:
            # A missing optional portrait cannot prevent game startup.
            pass

    def _request_game_update_check(self):
        if self._game_update_checking or self._gameupd_running or getattr(self, "_shutting_down", False):
            return
        self._game_update_checking = True
        self._game_update_status = "checking"
        self._game_update_message = "Checking GitHub..."
        threading.Thread(target=self._check_game_update, daemon=True).start()

    def _poll_game_update_events(self):
        while not self._game_update_events.empty():
            event, value = self._game_update_events.get_nowait()
            if event == "check":
                self._game_update_checking = False
                self._game_update_status = value.status
                self._game_update_message = value.message
                self._game_installed_version = value.installed_version
                if value.status in ("available", "current", "unpublished"):
                    self._game_update_release = value.release
                    self._game_update_available = value.status == "available"
                    self._game_remote_version = value.release.version if value.release else ""
                    self._gameupd_scroll = 0
                if value.status == "available":
                    self._game_update_notice_queued = value.release
                elif value.status != "error":
                    self._game_update_notice_queued = None
            elif event == "check_error":
                self._game_update_checking = False
                self._game_update_status = "error"
                self._game_update_message = value
            elif event == "prepared":
                self._gameupd_plan = value
                self._gameupd_running = False
                self._gameupd_confirm = True
                self._gameupd_open_frame = pyxel.frame_count
            elif event == "prepare_error":
                self._gameupd_running = False
                self._gameupd_log_add("[UPD] " + value)

    def _game_update_notice_allowed(self):
        flags = ("_boot_phase", "menu_open", "input_mode", "confirm_quit", "loot_screen",
                 "_gps_wait_dialog", "_cluster_popup", "_et_net_screen", "_portal_select_screen",
                 "_portal_data_screen", "_mitm_screen", "_flipper_screen", "_subghz_screen",
                 "_flash_screen", "_gameupd_screen", "_watch_screen", "_wl_screen",
                 "_lora_protocol_picker", "_mt_profile_screen", "_mt_screen", "_mc_screen",
                 "_wd_screen", "_mc_region_screen", "_prox_screen", "_shutting_down")
        if any(getattr(self, name, False) for name in flags):
            return False
        for plugin in getattr(self, "_plugins", ()):
            if getattr(plugin, "overlay_active", False):
                return False
            if hasattr(plugin, "_auth_pin_active") and plugin._auth_pin_active():
                return False
        return True

    def _maybe_show_game_update_notice(self):
        release = self._game_update_notice_queued
        if release is None or self._game_update_notice is not None or not self._game_update_notice_allowed():
            return
        identity = (release.repository, release.channel, release.release_id)
        if identity in self._game_update_seen_in_session:
            self._game_update_notice_queued = None
            return
        self._game_update_seen_in_session.add(identity)
        self._game_update_notice_queued = None
        try:
            if not self._game_updates.notice_pending(release):
                return
            if self._game_updates.acknowledge(release) is False:
                raise OSError("Notification history was not saved")
        except Exception:
            logging.getLogger(__name__).exception("Could not save game update notification history")
            self._term_add("[UPD] Update available; could not save notification history.", raw=True)
            return
        # Persist only when the main screen is ready to actually show this release.
        self._game_update_notice = release
        self._gameupd_open_frame = pyxel.frame_count
        self._term_add(f"[UPD] New game v{release.version}: SYSTEM > Update Game", raw=True)

    def _update_game_update_notice(self):
        if pyxel.frame_count == self._gameupd_open_frame:
            return
        if pyxel.btnp(pyxel.KEY_RETURN) or pyxel.btnp(pyxel.KEY_Y):
            self._game_update_notice = None
            self._start_game_update()
        elif pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
            self._game_update_notice = None
            self._esc_consumed_frame = pyxel.frame_count

    def _draw_game_update_notice(self):
        release = self._game_update_notice
        if release is None:
            return
        dx, dy, dw, dh = 120, 70, 400, 100
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        pyxel.text(dx + 10, dy + 10, "UPDATE AVAILABLE", C_WARNING)
        pyxel.text(dx + 10, dy + 24, f"WatchDogsGo v{release.version}", C_TEXT)
        pyxel.text(dx + 10, dy + 46, "Open Update Game to see what's new?", C_TEXT)
        pyxel.text(dx + 10, dy + 62, "You can update later from SYSTEM.", C_DIM)
        pyxel.text(dx + 10, dy + 83, "[ENTER/Y] OK    [N/ESC] No", C_DIM)

    def _start_game_update(self):
        self._gameupd_screen = True
        self._gameupd_running = False
        self._gameupd_done = False
        self._gameupd_ok = False
        self._gameupd_log = []
        self._gameupd_scroll = 0
        self._gameupd_confirm = False
        self._gameupd_plan = None
        self._gameupd_open_frame = pyxel.frame_count
        self.menu_open = False
        # Keep navigation anchored to the existing SYSTEM item.
        for category, (label, items) in enumerate(MENU_CATS):
            if label == "SYSTEM":
                self.menu_cat = category
                self.menu_sel = next(i for i, item in enumerate(items) if item[3] == "_gameupd")
                break

    def _gameupd_note_lines(self):
        import textwrap
        release = self._game_update_release
        if release is not None:
            content = release.notes
        else:
            try:
                content = (Path(__file__).resolve().parent.parent / "docs" / "UPDATE_PREVIEW.txt").read_text(encoding="utf-8")
            except OSError:
                content = "PRIVATE PREVIEW\nNo release has been published yet."
        lines = []
        for paragraph in content.splitlines():
            lines.extend(textwrap.wrap(paragraph.expandtabs(4), width=83,
                         break_long_words=True, break_on_hyphens=False) or [""])
        return lines or ["No release notes provided."]

    def _update_gameupd_screen(self):
        if pyxel.frame_count == self._gameupd_open_frame:
            return
        if self._gameupd_install_frame >= 0:
            if pyxel.frame_count > self._gameupd_install_frame:
                self._gameupd_install_frame = -1
                self._gameupd_do()
            return
        if self._gameupd_running:
            return
        if self._gameupd_done and self._gameupd_closed_for_install:
            if pyxel.btnp(pyxel.KEY_ESCAPE) or pyxel.btnp(pyxel.KEY_RETURN) or pyxel.btnp(pyxel.KEY_Q):
                pyxel.quit()
            return
        if self._gameupd_confirm:
            if pyxel.btnp(pyxel.KEY_Y):
                self._gameupd_confirm = False
                self._gameupd_closed_for_install = True
                self._gameupd_running = True
                self._gameupd_log_add("[UPD] Stopping devices and saving progress...")
                self._cleanup()
                if getattr(self, "_cleanup_errors", ()):
                    self._gameupd_running = False
                    self._gameupd_done = True
                    self._gameupd_discard_plan()
                    self._gameupd_log_add("[UPD] Devices did not stop cleanly. Update cancelled.")
                    self._gameupd_log_add("[UPD] Close and relaunch the game.")
                else:
                    self._gameupd_log_add("[UPD] Installing verified code. Please wait...")
                    # Draw one Installing frame before the short, synchronous
                    # transaction. A window-close event cannot kill its worker.
                    self._gameupd_install_frame = pyxel.frame_count
            elif pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                self._gameupd_confirm = False
                self._gameupd_discard_plan()
                self._esc_consumed_frame = pyxel.frame_count
            return
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._gameupd_screen = False
            self._esc_consumed_frame = pyxel.frame_count
            return
        if pyxel.btnp(pyxel.KEY_R):
            self._request_game_update_check()
        lines = self._gameupd_note_lines()
        maximum = max(0, len(lines) - 9)
        if pyxel.btnp(pyxel.KEY_UP, 15, 3):
            self._gameupd_scroll = max(0, self._gameupd_scroll - 1)
        if pyxel.btnp(pyxel.KEY_DOWN, 15, 3):
            self._gameupd_scroll = min(maximum, self._gameupd_scroll + 1)
        if pyxel.btnp(pyxel.KEY_PAGEUP):
            self._gameupd_scroll = max(0, self._gameupd_scroll - 9)
        if pyxel.btnp(pyxel.KEY_PAGEDOWN):
            self._gameupd_scroll = min(maximum, self._gameupd_scroll + 9)
        if pyxel.btnp(pyxel.KEY_HOME):
            self._gameupd_scroll = 0
        if pyxel.btnp(pyxel.KEY_END):
            self._gameupd_scroll = maximum
        if ((pyxel.btnp(pyxel.KEY_RETURN) or pyxel.btnp(pyxel.KEY_I))
                and self._game_update_available and not self._game_update_checking):
            self._gameupd_running = True
            self._gameupd_log = []
            self._gameupd_log_add("[UPD] Preparing a verified code update...")
            threading.Thread(target=self._gameupd_prepare, args=(self._game_update_release,), daemon=True).start()

    def _gameupd_prepare(self, release):
        try:
            self._game_update_events.put(("prepared", self._game_updates.prepare(release)))
        except Exception as exc:
            logging.getLogger(__name__).exception("Game update preparation failed")
            self._game_update_events.put(("prepare_error", str(exc)[:200] or "Could not prepare this update."))

    def _gameupd_log_add(self, line: str):
        self._gameupd_log.append(line)
        self._term_add(line, raw=True)

    def _gameupd_discard_plan(self):
        plan, self._gameupd_plan = self._gameupd_plan, None
        if plan is not None:
            try:
                self._game_updates.discard(plan)
            except Exception:
                # The verified backup is independent of this staging directory.
                # Never remove an unverified path just to tidy a cancelled update.
                logging.getLogger(__name__).exception("Could not remove staged game update")
                self._gameupd_log_add("[UPD] Could not remove the staged download; see game log.")

    def _gameupd_do(self):
        try:
            if not self._gameupd_closed_for_install or not getattr(self, "_cleanup_done", False) or self._cleanup_errors:
                raise RuntimeError("Devices must stop cleanly before updating.")
            receipt = self._game_updates.install(self._gameupd_plan, quiescent=True)
            self._gameupd_ok = True
            self._game_update_available = False
            self._gameupd_log_add(f"[UPD] Updated to v{receipt.version}. Backup saved.")
            self._gameupd_log_add("[UPD] Settings and progress kept. Relaunch to apply.")
        except Exception as exc:
            logging.getLogger(__name__).exception("Game update installation failed")
            self._gameupd_ok = False
            self._gameupd_log_add("[UPD] Update failed: " + (str(exc)[:200] or "see game log"))
            self._gameupd_log_add("[UPD] Close and relaunch the game.")
        finally:
            self._gameupd_discard_plan()
            self._gameupd_running = False
            self._gameupd_done = True

    def _draw_gameupd_screen(self):
        import textwrap
        dx, dy, dw, dh = 100, 22, 440, 206
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        if self._gameupd_avatar is not None:
            pyxel.blt(dx + 10, dy + 6, self._gameupd_avatar, 0, 0, 32, 32)
        pyxel.text(dx + 52, dy + 8, "UPDATE GAME", C_WARNING)
        pyxel.text(dx + 52, dy + 22, "Modified by KiloGramowy", C_DIM)
        version = "Current: v" + self._game_installed_version
        if self._game_update_release is not None:
            version += "   Latest: v" + self._game_update_release.version
        pyxel.text(dx + 10, dy + 44, version, C_DIM)
        status = self._game_update_message
        if self._game_update_checking:
            status = "Checking GitHub..."
        status_lines = textwrap.wrap(status, width=83)[:2] or [""]
        for i, line in enumerate(status_lines):
            pyxel.text(dx + 10, dy + 56 + i * 9, line, C_WARNING if self._game_update_status == "error" else C_DIM)
        pyxel.line(dx + 6, dy + 76, dx + dw - 7, dy + 76, C_COAST)
        title = self._game_update_release.title if self._game_update_release else "PRIVATE PREVIEW - what's new"
        pyxel.text(dx + 10, dy + 80, title[:83], C_TEXT)
        lines = self._gameupd_note_lines()
        maximum = max(0, len(lines) - 9)
        self._gameupd_scroll = min(maximum, max(0, self._gameupd_scroll))
        for i, line in enumerate(lines[self._gameupd_scroll:self._gameupd_scroll + 9]):
            pyxel.text(dx + 10, dy + 94 + i * 9, line, C_TEXT)
        if maximum:
            count = f"{self._gameupd_scroll + 1}-{min(len(lines), self._gameupd_scroll + 9)}/{len(lines)}"
            pyxel.text(dx + dw - len(count) * 5 - 10, dy + 175, count, C_DIM)
        if self._gameupd_log:
            log_lines = textwrap.wrap(self._gameupd_log[-1], width=83) or [""]
            pyxel.rect(dx + 6, dy + 183, dw - 12, 10, 0)
            pyxel.text(dx + 10, dy + 184, log_lines[0], C_SUCCESS if self._gameupd_ok else C_WARNING)
        if self._gameupd_confirm:
            # A separate confirmation prevents OK/Enter on the notice from
            # installing, and it remains above the OUTPUT panel.
            pyxel.rect(dx + 4, dy + 149, dw - 8, 52, 0)
            pyxel.rectb(dx + 4, dy + 149, dw - 8, 52, C_WARNING)
            pyxel.text(dx + 12, dy + 155, "Install this update and close the game?", C_WARNING)
            pyxel.text(dx + 12, dy + 169, "Settings and progress will be kept. Backup first.", C_TEXT)
            pyxel.text(dx + 12, dy + 186, "[Y] Update    [N/ESC] Cancel", C_DIM)
            return
        if self._gameupd_running:
            hint = "Please wait..."
        elif self._gameupd_closed_for_install:
            hint = "ENTER/ESC close - relaunch the game"
        elif self._game_update_available:
            hint = "UP/DOWN scroll  R check  ENTER update  ESC back"
        else:
            hint = "UP/DOWN scroll  R check  ESC back"
        pyxel.text(dx + 10, dy + dh - 12, hint, C_DIM)

    def _draw_captured_data(self):
        """Draw overlay showing captured credentials from portal/evil twin."""
        # Collect captured lines from terminal
        with self._term_lock:
            captures = [l for l in self.terminal_lines
                        if ":PWD]" in l]
        tag = "EVIL PORTAL" if self._attack_mode == "evil_portal" else "EVIL TWIN"
        n_clients = self.state.portal_client_count
        n_forms = (self.state.submitted_forms
                   + len(self.state.evil_twin_captured_data))
        # Solid background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        # Dialog box — 12px rows for 5x8 font
        ROW_H = 12
        dw, dh = 560, 240
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, 12)  # blue border
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 4,
                   f"{tag} — Captured Data", 12)
        pyxel.text(dx + dw - 150, dy + 4,
                   f"Clients:{n_clients} Forms:{n_forms}", C_DIM)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        # SSID info
        pyxel.text(dx + 4, dy + 18,
                   f"SSID: {self._portal_ssid}", C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 28, dx + dw - 3, dy + 28, 1)
        # Captured data list
        if not captures:
            pyxel.text(dx + (dw - 27 * 5) // 2, dy + dh // 2,
                       "No credentials captured yet", C_DIM)
        else:
            max_vis = (dh - 60) // ROW_H
            scroll = getattr(self, '_portal_data_scroll', 0)
            scroll = min(scroll, max(0, len(captures) - max_vis))
            self._portal_data_scroll = scroll
            y = dy + 32
            for i in range(scroll, min(len(captures), scroll + max_vis)):
                line = captures[i]
                # Strip the [EP:PWD] or [ET:PWD] prefix for cleaner display
                display = line
                if ":PWD] " in line:
                    display = line.split(":PWD] ", 1)[1]
                pyxel.text(dx + 6, y, display[:90], 12)  # blue text
                y += ROW_H
            # Scroll indicator
            if len(captures) > max_vis:
                bar_total = dh - 60
                bar_h = max(4, bar_total * max_vis // len(captures))
                bar_y = dy + 32 + (bar_total - bar_h) * scroll // max(
                    1, len(captures) - max_vis)
                pyxel.rect(dx + dw - 4, bar_y, 2, bar_h, 12)
        # Hints
        pyxel.text(dx + 4, dy + dh - 11,
                   "[D] close  [UP/DOWN] scroll  [X] stop attack", C_DIM)

    def _draw_input_dialog(self):
        # Dim overlay
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)
        # Dialog box — 20px per field (was 16 for 4x6 font; now 8px text + 12 gap)
        FIELD_H = 20
        dh = 40 + len(self.input_fields) * FIELD_H
        dw = 320
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_HACK_CYAN)
        # Title — special cat codes use custom labels
        _SPECIAL_TITLES = {
            -1: "MITM Target",
            -2: "BlueDucky",
            -3: "Whitelist",
            -4: "Whitelist",
            -5: "MeshCore",
            -6: "WPA-sec Token",
            -7: "Evil Portal",
            -9: "Meshtastic",
        }
        if self._input_pending_cat < 0:
            name = _SPECIAL_TITLES.get(self._input_pending_cat, "Input")
        else:
            _, items = MENU_CATS[self._input_pending_cat]
            _hk, name, _cmd, _sk, _it = items[self._input_pending_item]
        pyxel.text(dx + 4, dy + 4, f"INPUT: {name}", C_HACK_CYAN)
        pyxel.line(dx, dy + 14, dx + dw - 1, dy + 14, C_COAST)
        # Fields
        for i, field in enumerate(self.input_fields):
            fy = dy + 20 + i * FIELD_H
            active = (i == self.input_field_idx)
            pyxel.text(dx + 6, fy, f"{field['label']}:",
                       C_HACK_CYAN if active else C_DIM)
            bx = dx + 56
            bw2 = dw - 64
            pyxel.rect(bx, fy - 2, bw2, 12, 1)
            pyxel.rectb(bx, fy - 2, bw2, 12, C_HACK_CYAN if active else C_COAST)
            val = field["value"]
            if active and pyxel.frame_count % 30 < 20:
                val += "_"
            pyxel.text(bx + 3, fy, val[:36], C_SUCCESS if active else C_TEXT)
        # Hint
        pyxel.text(dx + 4, dy + dh - 11, "ENTER confirm  ESC cancel", C_DIM)

    def _draw_confirm_quit(self):
        # Dim overlay
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)
        # Dialog box — larger for readability (5x8 font)
        dw, dh = 260, 78
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_ERROR)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_ERROR)
        # Glitch accent line
        pyxel.line(dx + 3, dy + 2, dx + dw - 4, dy + 2, C_ERROR)
        # Title (15 chars × 5 = 75px)
        pyxel.text(dx + (dw - 15 * 5) // 2, dy + 8,
                   "QUIT WATCHDOGS?", C_ERROR)
        pyxel.line(dx + 2, dy + 20, dx + dw - 3, dy + 20, C_COAST)
        # Subtitle (25 chars × 5 = 125px)
        pyxel.text(dx + (dw - 25 * 5) // 2, dy + 26,
                   "All operations will stop.", C_DIM)
        # Prompt — centered, larger spacing
        bx = dx + dw // 2 - 60
        pyxel.text(bx, dy + 46, "[Y]", C_SUCCESS)
        pyxel.text(bx + 20, dy + 46, "Quit", C_TEXT)
        pyxel.text(bx + 70, dy + 46, "[N]", C_ERROR)
        pyxel.text(bx + 90, dy + 46, "Cancel", C_TEXT)
        # Blink hint (12 chars × 5 = 60px)
        if pyxel.frame_count % 40 < 28:
            pyxel.text(dx + (dw - 12 * 5) // 2, dy + 62,
                       "press Y or N", C_DIM)

    def _draw_gps_wait_dialog(self):
        # Dim overlay
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)
        # Dialog box
        dw, dh = 280, 70
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.line(dx + 2, dy + 1, dx + dw - 3, dy + 1, C_WARNING)
        # Title
        pyxel.text(dx + 4, dy + 4, "NO GPS FIX", C_WARNING)
        pyxel.line(dx, dy + 13, dx + dw - 1, dy + 13, C_COAST)
        # Info
        if self.gps_sats:
            sat = f"Satellites: {self.gps_sats}"
        elif self.gps_sats_vis:
            sat = f"Visible: {self.gps_sats_vis}"
        else:
            sat = "No satellites detected"
        pyxel.text(dx + 4, dy + 18, sat, C_DIM)
        pyxel.text(dx + 4, dy + 30, "Wait for GPS fix?", C_TEXT)
        # Buttons
        pyxel.text(dx + 18, dy + 44, "[Y]", C_SUCCESS)
        pyxel.text(dx + 38, dy + 44, "wait", C_TEXT)
        pyxel.text(dx + 90, dy + 44, "[N]", C_ERROR)
        pyxel.text(dx + 110, dy + 44, "cancel", C_TEXT)

    # ------------------------------------------------------------------
    # MITM dedicated screen (JanOS-style sub-screen)
    # ------------------------------------------------------------------

    def _update_mitm_screen(self):
        """Handle keys for the MITM sub-screen state machine."""
        st = self._mitm_state

        if st == "idle":
            if pyxel.btnp(pyxel.KEY_S):
                ifaces = self._mitm.get_interfaces()
                if not ifaces:
                    self._mitm_msg("[MITM] No network interface with IP", C_ERROR)
                    return
                self._mitm_ifaces = ifaces
                if len(ifaces) == 1:
                    self._attack_iface = ifaces[0][0]
                    self._mitm_msg(f"[MITM] Interface: {ifaces[0][0]} ({ifaces[0][1]})")
                    self._mitm_state = "target_mode"
                    self._mitm_sel = 0
                else:
                    self._mitm_state = "iface"
                    self._mitm_sel = 0
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_screen = False
                self._mitm_state = "idle"
                self._mitm_close_frame = pyxel.frame_count
                self._esc_consumed_frame = pyxel.frame_count
            return

        if st == "iface":
            if pyxel.btnp(pyxel.KEY_UP):
                self._mitm_sel = max(0, self._mitm_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mitm_sel = min(len(self._mitm_ifaces) - 1, self._mitm_sel + 1)
            elif pyxel.btnp(pyxel.KEY_RETURN):
                idx = self._mitm_sel
                if idx < len(self._mitm_ifaces):
                    self._attack_iface = self._mitm_ifaces[idx][0]
                    self._mitm_msg(f"[MITM] Interface: {self._mitm_ifaces[idx][0]}")
                    self._mitm_state = "target_mode"
                    self._mitm_sel = 0
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "target_mode":
            if pyxel.btnp(pyxel.KEY_UP):
                self._mitm_sel = max(0, self._mitm_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mitm_sel = min(2, self._mitm_sel + 1)
            elif pyxel.btnp(pyxel.KEY_RETURN):
                if self._mitm_sel == 0:
                    # Single target — use input dialog
                    self._mitm_state = "input_ip"
                    self.input_mode = True
                    self.input_fields = [{"label": "Victim IP", "value": "", "pos": 0}]
                    self.input_field_idx = 0
                    self._input_pending_cat = -1
                    self._input_pending_item = -1
                elif self._mitm_sel == 1:
                    # Scan subnet
                    self._mitm_state = "scan"
                    self._mitm_hosts = []
                    self._mitm_sel = 0
                    self._mitm_error = ""

                    def _do_scan():
                        try:
                            iface = self._attack_iface
                            subnet = self._mitm.get_subnet(iface)
                            self._mitm_msg(f"[MITM] Scanning {subnet}...", C_HACK_CYAN)
                            hosts = self._mitm.arp_scan(subnet, iface)
                            gw = self._mitm.get_default_gateway()
                            my_ips = {ip for _, ip in self._mitm.get_interfaces()}
                            filtered = [(ip, mac) for ip, mac in hosts
                                        if ip != gw and ip not in my_ips]
                            self._mitm_hosts = filtered
                            if filtered:
                                self._mitm_msg(f"[MITM] Found {len(filtered)} hosts")
                            else:
                                self._mitm_error = "No hosts found on subnet"
                                self._mitm_state = "error"
                        except Exception as e:
                            self._mitm_error = str(e)[:80]
                            self._mitm_state = "error"

                    threading.Thread(target=_do_scan, daemon=True).start()
                elif self._mitm_sel == 2:
                    # All devices — scan first, then confirm
                    self._mitm_state = "scan_all"
                    self._mitm_hosts = []
                    self._mitm_victim_ip = ""  # means target_all
                    self._mitm_error = ""

                    def _resolve_all():
                        try:
                            iface = self._attack_iface
                            subnet = self._mitm.get_subnet(iface)
                            self._mitm_msg(f"[MITM] Scanning {subnet} (all hosts)...", C_WARNING)
                            hosts = self._mitm.arp_scan(subnet, iface)
                            gw = self._mitm.get_default_gateway()
                            my_ips = {ip for _, ip in self._mitm.get_interfaces()}
                            filtered = [(ip, mac) for ip, mac in hosts
                                        if ip != gw and ip not in my_ips]
                            self._mitm_hosts = filtered
                            if filtered:
                                self._mitm_msg(f"[MITM] {len(filtered)} hosts found")
                                self._mitm_state = "confirm"
                            else:
                                self._mitm_error = "No hosts found on subnet"
                                self._mitm_state = "error"
                        except Exception as e:
                            self._mitm_error = str(e)[:80]
                            self._mitm_state = "error"

                    threading.Thread(target=_resolve_all, daemon=True).start()
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "scan":
            if self._mitm_hosts:
                if pyxel.btnp(pyxel.KEY_UP):
                    self._mitm_sel = max(0, self._mitm_sel - 1)
                elif pyxel.btnp(pyxel.KEY_DOWN):
                    self._mitm_sel = min(len(self._mitm_hosts) - 1, self._mitm_sel + 1)
                elif pyxel.btnp(pyxel.KEY_RETURN):
                    idx = self._mitm_sel
                    if idx < len(self._mitm_hosts):
                        self._mitm_victim_ip = self._mitm_hosts[idx][0]
                        self._mitm_state = "confirm"
                elif pyxel.btnp(pyxel.KEY_ESCAPE):
                    self._mitm_state = "idle"
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "scan_all":
            # Waiting for scan to finish — thread transitions to confirm or error
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "error":
            if pyxel.btnp(pyxel.KEY_ESCAPE) or pyxel.btnp(pyxel.KEY_RETURN):
                self._mitm_state = "idle"
                self._mitm_error = ""
            return

        if st == "confirm":
            if pyxel.btnp(pyxel.KEY_Y):
                self._mitm_state = "running"
                self._mitm_log_scroll = 0

                def _start():
                    if self._mitm_victim_ip:
                        ok = self._mitm.start(self._attack_iface,
                                              victim_ip=self._mitm_victim_ip)
                    else:
                        ok = self._mitm.start(self._attack_iface, target_all=True)
                    if not ok:
                        self._mitm_error = "Attack failed to start (check logs)"
                        self._mitm_state = "error"

                threading.Thread(target=_start, daemon=True).start()
            elif pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "running":
            if pyxel.btnp(pyxel.KEY_X):
                threading.Thread(target=self._mitm.stop, daemon=True).start()
                self._mitm_state = "idle"
                self._mitm_msg("[MITM] Stopped", C_WARNING)
            elif pyxel.btnp(pyxel.KEY_PAGEUP):
                self._mitm_log_scroll = min(
                    self._mitm_log_scroll + 5,
                    max(0, len(self._mitm_log) - 5))
            elif pyxel.btnp(pyxel.KEY_PAGEDOWN):
                self._mitm_log_scroll = max(0, self._mitm_log_scroll - 5)
            elif pyxel.btnp(pyxel.KEY_END):
                self._mitm_log_scroll = 0
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                # ESC while running → go back to idle (attack keeps running)
                pass
            return

    def _draw_mitm_screen(self):
        """Draw the MITM sub-screen (full overlay like loot_screen)."""
        pyxel.cls(0)
        st = self._mitm_state
        BAR_H = 14  # title/status bar height (8px text + padding)

        # ── Info bar (top) ──
        pyxel.rect(0, 0, W, BAR_H, 1)
        if self._mitm.running:
            victims = ", ".join(ip for ip, _ in self._mitm._victims[:4])
            if len(self._mitm._victims) > 4:
                victims += f" +{len(self._mitm._victims) - 4}"
            gw = self._mitm._gateway_ip
            info = f"MITM RUNNING | {victims} <-> {gw} | Packets: {self._mitm.packets}"
            pyxel.text(4, 4, info, C_WARNING)
        else:
            pyxel.text(4, 4, "MITM -- idle", C_DIM)

        # ── Status bar (bottom) ──
        pyxel.rect(0, H - BAR_H, W, BAR_H, 1)
        if st == "running":
            pyxel.text(4, H - 10, "[X]Stop", C_ERROR)
        elif st == "idle":
            pyxel.text(4, H - 10, "[S]Start  [ESC]Back", C_DIM)
        elif st in ("iface", "target_mode", "scan"):
            pyxel.text(4, H - 10, "[Enter]Select  [ESC]Cancel", C_DIM)
        elif st == "confirm":
            pyxel.text(4, H - 10, "[Y]Yes  [N]No", C_DIM)
        elif st == "error":
            pyxel.text(4, H - 10, "[ESC]Back", C_DIM)
        elif st == "scan_all":
            pyxel.text(4, H - 10, "Scanning...  [ESC]Cancel", C_DIM)

        # ── Dividers ──
        pyxel.line(0, BAR_H, W - 1, BAR_H, C_MENU_BORDER)
        pyxel.line(0, H - BAR_H - 1, W - 1, H - BAR_H - 1, C_MENU_BORDER)

        # ── Content area ──
        CY = BAR_H + 4       # content start Y
        CH = H - BAR_H * 2 - 6  # content height
        ROW_H = 12           # 5x8 font

        if st == "idle":
            pyxel.text(20, CY + 10, "MITM -- ARP Spoofing Attack", C_HACK_CYAN)
            pyxel.text(20, CY + 32, "Intercepts traffic between victim(s)", C_TEXT)
            pyxel.text(20, CY + 42, "and the network gateway.", C_TEXT)
            pyxel.text(20, CY + 62, "Captures: DNS queries, HTTP requests,", C_DIM)
            pyxel.text(20, CY + 72, "cleartext credentials (FTP/Telnet/POP3)", C_DIM)
            pyxel.text(20, CY + 92, "Saves full pcap to loot/mitm/", C_DIM)
            pyxel.text(20, CY + 116, "Press [S] to start", C_HACK_CYAN)

        elif st == "iface":
            pyxel.text(20, CY + 6, "Select interface:", C_HACK_CYAN)
            for i, (iface, ip) in enumerate(self._mitm_ifaces):
                ty = CY + 24 + i * ROW_H
                if i == self._mitm_sel:
                    pyxel.rect(18, ty - 2, W - 36, ROW_H, C_HACK_CYAN)
                    _mg = int((pyxel.frame_count * 4) % (W - 36 + 24)) - 12
                    for _dx2, _sc in ((0, 7), (-1, 6), (1, 6)):
                        _mx = 18 + _mg + _dx2
                        if 18 < _mx < W - 19:
                            pyxel.line(_mx, ty - 2, _mx, ty + ROW_H - 5, _sc)
                    pyxel.text(22, ty, f"{iface} ({ip})", 0)
                else:
                    pyxel.text(22, ty, f"{iface} ({ip})", C_TEXT)

        elif st == "target_mode":
            pyxel.text(20, CY + 6, "Target mode:", C_HACK_CYAN)
            options = [
                "1. Single target (enter IP)",
                "2. Scan subnet + select",
                "3. All devices on subnet",
            ]
            for i, opt in enumerate(options):
                ty = CY + 26 + i * (ROW_H + 2)
                if i == self._mitm_sel:
                    pyxel.rect(18, ty - 2, W - 36, ROW_H + 2, C_HACK_CYAN)
                    _mg = int((pyxel.frame_count * 4) % (W - 36 + 24)) - 12
                    for _dx2, _sc in ((0, 7), (-1, 6), (1, 6)):
                        _mx = 18 + _mg + _dx2
                        if 18 < _mx < W - 19:
                            pyxel.line(_mx, ty - 2, _mx, ty + ROW_H - 3, _sc)
                    pyxel.text(22, ty, opt, 0)
                else:
                    pyxel.text(22, ty, opt, C_TEXT)

        elif st == "scan":
            if not self._mitm_hosts:
                pyxel.text(20, CY + 20, ">>> Scanning subnet...", C_HACK_CYAN)
            else:
                pyxel.text(20, CY + 6, f"Found {len(self._mitm_hosts)} hosts:",
                           C_HACK_CYAN)
                for i, (ip, mac) in enumerate(self._mitm_hosts[:15]):
                    ty = CY + 24 + i * ROW_H
                    label = f"{ip}  ({mac})"
                    if i == self._mitm_sel:
                        pyxel.rect(18, ty - 2, W - 36, ROW_H, C_HACK_CYAN)
                        pyxel.text(22, ty, label, 0)
                    else:
                        pyxel.text(22, ty, label, C_TEXT)

        elif st == "scan_all":
            pyxel.text(20, CY + 20, ">>> Scanning all hosts on subnet...",
                       C_WARNING)
            pyxel.text(20, CY + 38, "Please wait", C_DIM)

        elif st == "error":
            pyxel.text(20, CY + 20, "ERROR", C_ERROR)
            pyxel.text(20, CY + 40, self._mitm_error or "Unknown error",
                       C_WARNING)
            # Show recent log messages for context
            with self._mitm_lock:
                recent = list(self._mitm_log[-8:])
            for i, (txt, _) in enumerate(recent):
                pyxel.text(20, CY + 60 + i * 10, txt[:120], C_DIM)
            pyxel.text(20, CY + 60 + len(recent) * 10 + 14,
                       "Press [ESC] or [ENTER] to go back", C_HACK_CYAN)

        elif st == "confirm":
            # Dialog overlay
            dw, dh = 340, 90
            dx = (W - dw) // 2
            dy = (H - dh) // 2
            pyxel.rect(dx, dy, dw, dh, 0)
            pyxel.rectb(dx, dy, dw, dh, C_HACK_CYAN)
            pyxel.text(dx + 4, dy + 4, "-- Confirm --", C_HACK_CYAN)
            pyxel.text(dx + 8, dy + 20, "Start MITM attack?", C_TEXT)
            if self._mitm_victim_ip:
                pyxel.text(dx + 8, dy + 32,
                           f"Victim: {self._mitm_victim_ip}", C_TEXT)
            elif self._mitm_hosts:
                pyxel.text(dx + 8, dy + 32,
                           f"Victims: {len(self._mitm_hosts)} hosts", C_TEXT)
            else:
                pyxel.text(dx + 8, dy + 32,
                           "Victims: all on subnet", C_TEXT)
            gw = self._mitm.get_default_gateway() or "?"
            pyxel.text(dx + 8, dy + 44, f"Gateway: {gw}", C_TEXT)
            pyxel.text(dx + 8, dy + 56,
                       f"Interface: {self._attack_iface}", C_TEXT)
            pyxel.text(dx + 8, dy + 74, "[Y] Yes   [N] No", C_DIM)

        elif st == "running":
            # Live scrolling log (9px per line for 5x8 font)
            with self._mitm_lock:
                log_snap = list(self._mitm_log)
            total = len(log_snap)
            line_h = 9
            max_vis = CH // line_h
            if self._mitm_log_scroll == 0:
                start = max(0, total - max_vis)
                end = total
            else:
                end = max(0, total - self._mitm_log_scroll)
                start = max(0, end - max_vis)
            y = CY
            for i in range(start, end):
                text, color = log_snap[i]
                # Color-code by content
                if "[DNS]" in text:
                    c = C_HACK_CYAN
                elif "[HTTP]" in text:
                    c = C_SUCCESS
                elif "[CREDS]" in text or "[AUTH" in text:
                    c = C_ERROR
                elif "[MITM]" in text:
                    c = C_WARNING
                else:
                    c = C_TEXT
                pyxel.text(4, y, text[:155], c)
                y += line_h
                if y >= CY + CH:
                    break
            # Scroll indicator
            if total > max_vis and self._mitm_log_scroll == 0:
                pyxel.text(W - 110, H - 22, "PgUp/PgDn scroll", C_DIM)

    # ------------------------------------------------------------------
    # Whitelist screen
    # ------------------------------------------------------------------

    def _wl_build_scan_list(self):
        """Build merged WiFi + BLE device list for scan picker."""
        seen: set[str] = set()
        items: list[dict] = []
        for n in self.wifi_networks:
            mac = n.bssid.upper()
            if mac in seen or self._whitelist.is_blocked(mac):
                continue
            seen.add(mac)
            items.append({"type": "wifi", "mac": mac,
                          "name": n.ssid, "rssi": n.rssi,
                          "extra": f"Ch:{n.channel}"})
        for d in self.ble_devices:
            mac = d.mac.upper()
            if mac in seen or self._whitelist.is_blocked(mac):
                continue
            seen.add(mac)
            items.append({"type": "ble", "mac": mac,
                          "name": d.name, "rssi": d.rssi,
                          "extra": ""})
        # Sort by RSSI (strongest first)
        items.sort(key=lambda x: x["rssi"], reverse=True)
        return items

    # ------------------------------------------------------------------
    # PipBoy Watch screen
    # ------------------------------------------------------------------
    def _update_watch_screen(self):
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._watch_screen = False
            self._esc_consumed_frame = pyxel.frame_count
            return

        # PIN entry mode
        if self._watch.pin_requested:
            for k in range(10):
                if pyxel.btnp(pyxel.KEY_0 + k):
                    self._watch_pin_input += str(k)
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._watch_pin_input:
                self._watch_pin_input = self._watch_pin_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and len(self._watch_pin_input) == 6:
                self._watch.provide_pin(int(self._watch_pin_input))
                self._watch_pin_input = ""
            return

        # Scanning — pick device or rescan
        if not self._watch.connected:
            results = self._watch.scan_results
            if pyxel.btnp(pyxel.KEY_R) and not self._watch.scanning:
                self._watch.scan()
                self._watch_scan_sel = 0
            if pyxel.btnp(pyxel.KEY_UP) and self._watch_scan_sel > 0:
                self._watch_scan_sel -= 1
            if pyxel.btnp(pyxel.KEY_DOWN) and results:
                self._watch_scan_sel = min(
                    self._watch_scan_sel + 1, len(results) - 1)
            if pyxel.btnp(pyxel.KEY_RETURN) and results:
                sel = results[min(self._watch_scan_sel, len(results) - 1)]
                self._watch.connect(sel["address"])
            return

        # Connected — watch command menu
        if self._watch.connected:
            cmds = self._watch_menu_items()
            if pyxel.btnp(pyxel.KEY_UP) and self._watch_menu_sel > 0:
                self._watch_menu_sel -= 1
            if pyxel.btnp(pyxel.KEY_DOWN):
                self._watch_menu_sel = min(
                    self._watch_menu_sel + 1, len(cmds) - 1)
            if pyxel.btnp(pyxel.KEY_RETURN) and cmds:
                cmd_key = cmds[min(self._watch_menu_sel, len(cmds) - 1)][0]
                self._exec_watch_cmd(cmd_key)

    def _watch_menu_items(self) -> list:
        """Return list of (cmd_key, label) for connected watch."""
        items = [
            ("status",        "Request Status"),
            ("compass",       "Compass Heading"),
            ("haptic",        "Haptic Buzz"),
            ("recon_wifi",    "WiFi Recon (auto results)"),
            ("recon_ble",     "BLE Recon (auto results)"),
            ("recon_deauth",  "Deauth Target"),
            ("deauth_all",    "Blackout (all channels)"),
            ("evil_twin",     "Evil Twin"),
            ("evil_twin_stop", "Evil Twin Stop"),
            ("sniffer_start", "Sniffer Start"),
            ("sniffer_stop",  "Sniffer Stop"),
            ("deauth_detect", "Deauth Detector (TSCM)"),
            ("recon_stop",    "Stop All Recon"),
            ("nfc_scan",      "NFC Scan"),
            ("nfc_stop",      "NFC Stop"),
            ("nfc_save",      "NFC Save Tag"),
            ("nfc_list",      "NFC List Tags"),
            ("nfc_export",    "NFC Download All"),
            ("lora_start",    "LoRa Start"),
            ("lora_stop",     "LoRa Stop"),
            ("lora_advert",   "LoRa Advertise"),
            ("gps_on",        "GPS ON"),
            ("gps_off",       "GPS OFF"),
            ("disconnect",    "Disconnect"),
            ("forget",        "Forget Watch (unpair)"),
        ]
        return items

    def _exec_watch_cmd(self, cmd_key: str):
        if cmd_key == "disconnect":
            self._watch.disconnect()
            self._watch_screen = False
            self.msg("[Watch] Disconnected", C_WARNING)
            return
        if cmd_key == "forget":
            self._watch.forget()
            self._watch_screen = False
            self.msg("[Watch] Unpaired — bonding removed", C_WARNING)
            self._term_add("[Watch] Bonding removed, will need re-pair", raw=True)
            return
        if cmd_key == "nfc_export":
            for i, tag in enumerate(self._watch_nfc_tags):
                self._watch.send_command("nfc_download", {"idx": i})
            self.msg(f"[NFC] Downloading {len(self._watch_nfc_tags)} tags",
                     C_HACK_CYAN)
            return
        if cmd_key == "sniffer_start":
            # Channel hopping by default
            self._watch.send_command("sniffer_start", {"ch": 0})
            self._watch_log_add("[Watch] Sniffer started (ch hopping)", C_HACK_CYAN)
            return
        if cmd_key == "evil_twin":
            # Use last scanned WiFi results if available
            # For now send without params — firmware uses default
            self._watch.send_command("evil_twin",
                                     {"ssid": "FreeWiFi", "ch": 6})
            self._watch_log_add("[Watch] Evil Twin started", C_WARNING)
            return
        if cmd_key == "deauth_all":
            self._watch.send_command("deauth_all")
            self._watch_log_add("[Watch] Blackout started — all channels", C_ERROR)
            return
        if cmd_key == "deauth_detect":
            self._watch.send_command("deauth_detect")
            self._watch_log_add("[Watch] Deauth detector active (TSCM)", C_HACK_CYAN)
            return
        self._watch.send_command(cmd_key)
        self._watch_log_add(f"[Watch] {cmd_key}", C_HACK_CYAN)

    def _draw_watch_screen(self):
        W, H = 640, 360
        # Background
        pyxel.rect(0, 0, W, H, 0)

        # Title bar — 14px tall for 5x8 font + padding
        BAR_H = 14
        if self._watch.connected:
            title = f"PIPBOY WATCH — {self._watch.device_name}"
            bat = self._watch.battery
            pyxel.rect(0, 0, W, BAR_H, 1)
            pyxel.text(4, 4, title, C_SUCCESS)
            if bat > 0:
                bc = C_SUCCESS if bat > 25 else (
                    C_WARNING if bat > 10 else C_ERROR)
                pyxel.text(W - 60, 4, f"BAT:{bat}%", bc)
        else:
            pyxel.rect(0, 0, W, BAR_H, 1)
            pyxel.text(4, 4, "PIPBOY WATCH — SCAN", C_HACK_CYAN)

        # PIN entry overlay — watch firmware since v0.4 requires MITM-
        # authenticated encryption on NUS. First connect displays a 6-digit
        # PIN on the watch screen; user types it here, bond is stored and
        # subsequent connects are silent.
        if self._watch.pin_requested:
            cy = H // 2
            pyxel.rect(W // 2 - 160, cy - 56, 320, 112, 1)
            pyxel.rectb(W // 2 - 160, cy - 56, 320, 112, C_WARNING)
            pyxel.text(W // 2 - 60, cy - 44, "PAIRING PIPBOY WATCH", C_WARNING)
            pyxel.text(W // 2 - 140, cy - 28,
                       "Look at the watch — it shows a 6-digit PIN.", C_TEXT)
            pyxel.text(W // 2 - 140, cy - 16,
                       "Type it here to complete pairing.", C_DIM)
            # PIN digits — centred, Spleen 5x8 so 6 chars * 5 = 30px wide
            pin_disp = self._watch_pin_input + "_" * (
                6 - len(self._watch_pin_input))
            pyxel.text(W // 2 - 18, cy + 4, pin_disp, C_HACK_CYAN)
            pyxel.text(W // 2 - 90, cy + 28,
                       "[0-9] Type   [BKSP] Erase   [ENTER] Confirm", C_DIM)
            return

        # Scanning / device list
        if not self._watch.connected:
            y = BAR_H + 6
            if self._watch.scanning:
                dots = "." * ((pyxel.frame_count // 10) % 4)
                pyxel.text(4, y, f"Scanning for PipBoy devices{dots}",
                           C_HACK_CYAN)
                y += 16

            results = self._watch.scan_results
            if results:
                pyxel.text(4, y, "PipBoy devices found:", C_SUCCESS)
                y += 14
                for i, dev in enumerate(results):
                    sel = i == self._watch_scan_sel
                    c = C_TEXT if sel else C_DIM
                    prefix = "\x10" if sel else " "
                    pyxel.text(8, y,
                               f"{prefix} {dev['name']}  {dev['address']}  "
                               f"RSSI:{dev['rssi']}", c)
                    y += 12

            if not self._watch.scanning and not results:
                pyxel.text(4, y, "No PipBoy devices found", C_WARNING)
                y += 16

            # Scan log (show what BLE found)
            y += 8
            pyxel.text(4, y, "-- BLE Scan Log --", C_DIM)
            y += 12
            # Pull recent watch log lines from terminal
            with self._term_lock:
                watch_lines = [l for l in self.terminal_lines[-30:]
                               if "[Watch]" in l]
            for line in watch_lines[-10:]:
                pyxel.text(8, y, line[:100], C_DIM)
                y += 10

            # Checklist
            cx = 340
            cy = BAR_H + 6
            pyxel.text(cx, cy, "-- Pairing Checklist --", C_HACK_CYAN)
            cy += 16
            checks = [
                "1. Watch BLE enabled (WiFi app > BLE ON)",
                "2. Watch advertising as PipBoy-xxxxx",
                "3. uConsole Bluetooth ON (bluetoothctl power on)",
                "4. Watch in range (<10m)",
                "5. No other device connected to watch",
            ]
            for check in checks:
                pyxel.text(cx, cy, check, C_DIM)
                cy += 12

            pyxel.text(4, H - 14,
                       "[R] Rescan  [ENTER] Connect  [ESC] Back", C_DIM)
            return

        # Connected — command menu + live log
        if self._watch.connected:
            # Left panel: commands
            cmds = self._watch_menu_items()
            pyxel.text(4, 16, "-- Commands --", C_HACK_CYAN)
            y = 28
            for i, (key, label) in enumerate(cmds):
                sel = i == self._watch_menu_sel
                c = C_TEXT if sel else C_DIM
                prefix = "\x10" if sel else " "
                pyxel.text(4, y, f"{prefix} {label}", c)
                y += 10
                if y > H - 20:
                    break

            # Right panel: live log
            lx = 220
            pyxel.line(lx - 4, 14, lx - 4, H - 16, 1)
            pyxel.text(lx, 16, "-- Watch Log --", C_HACK_CYAN)
            log_y = 28
            max_lines = (H - 44) // 8
            visible = self._watch_log[-max_lines:]
            for text, color in visible:
                pyxel.text(lx, log_y, text[:52], color)
                log_y += 8

            pyxel.text(4, H - 12, "[ENTER] Execute  [ESC] Back", C_DIM)

    def _update_wl_screen(self):
        if self._wl_add_step == "type":
            if pyxel.btnp(pyxel.KEY_1) or pyxel.btnp(pyxel.KEY_W):
                self._wl_add_type = "wifi"
                self._wl_add_step = ""
                self.input_mode = True
                self.input_fields = [{"label": "BSSID/MAC", "value": ""}]
                self.input_field_idx = 0
                self._input_pending_cat = -3
                self._input_pending_item = -1
            elif pyxel.btnp(pyxel.KEY_2) or pyxel.btnp(pyxel.KEY_B):
                self._wl_add_type = "ble"
                self._wl_add_step = ""
                self.input_mode = True
                self.input_fields = [{"label": "MAC", "value": ""}]
                self.input_field_idx = 0
                self._input_pending_cat = -3
                self._input_pending_item = -1
            elif pyxel.btnp(pyxel.KEY_S):
                # Scan & Select — build list from live scan data
                self._wl_scan_list = self._wl_build_scan_list()
                self._wl_scan_sel = 0
                self._wl_add_step = "scan_select"
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._wl_add_step = ""
            return

        if self._wl_add_step == "scan_select":
            # Auto-refresh: 10s after scan started, rebuild list and stop status
            if self._wl_scan_status and time.time() - self._wl_scan_timer >= 10:
                self._wl_scan_list = self._wl_build_scan_list()
                self._wl_scan_sel = 0
                self.msg(f"[SCAN] Done — {len(self._wl_scan_list)} devices",
                         C_SUCCESS)
                self._wl_scan_status = ""

            items = self._wl_scan_list
            if pyxel.btnp(pyxel.KEY_UP) and items:
                self._wl_scan_sel = (self._wl_scan_sel - 1) % len(items)
            elif pyxel.btnp(pyxel.KEY_DOWN) and items:
                self._wl_scan_sel = (self._wl_scan_sel + 1) % len(items)
            elif pyxel.btnp(pyxel.KEY_RETURN) and items:
                it = items[self._wl_scan_sel]
                ok = self._whitelist.add(it["type"], it["mac"], it["name"])
                if ok:
                    self._term_add(
                        f"[WL] Added: {it['name']} ({it['mac'][-8:]})",
                        raw=True)
                    self.msg("[WL] Entry added", C_SUCCESS)
                    self._wl_scan_list = self._wl_build_scan_list()
                    if self._wl_scan_sel >= len(self._wl_scan_list):
                        self._wl_scan_sel = max(0, len(self._wl_scan_list) - 1)
                else:
                    self.msg("[WL] MAC already exists", C_WARNING)
            elif pyxel.btnp(pyxel.KEY_W) and not self._wl_scan_status:
                self._wl_scan_list = []
                self._wl_scan_sel = 0
                self._send("scan_networks")
                self._wl_scan_status = "wifi"
                self._wl_scan_timer = time.time()
                self.msg("[SCAN] WiFi scan started", C_SUCCESS)
            elif pyxel.btnp(pyxel.KEY_B) and not self._wl_scan_status:
                self._wl_scan_list = []
                self._wl_scan_sel = 0
                self._send("scan_bt")
                self._wl_scan_status = "ble"
                self._wl_scan_timer = time.time()
                self.msg("[SCAN] BLE scan started", C_HACK_CYAN)
            elif pyxel.btnp(pyxel.KEY_R):
                self._wl_scan_list = self._wl_build_scan_list()
                self._wl_scan_sel = min(self._wl_scan_sel,
                                        max(0, len(self._wl_scan_list) - 1))
                self.msg(f"[WL] {len(self._wl_scan_list)} devices",
                         C_HACK_CYAN)
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._wl_add_step = ""
                self._wl_scan_list = []
                self._wl_scan_status = ""
            return

        # Main list view
        entries = self._whitelist.entries
        if pyxel.btnp(pyxel.KEY_UP) and entries:
            self._wl_sel = (self._wl_sel - 1) % len(entries)
        if pyxel.btnp(pyxel.KEY_DOWN) and entries:
            self._wl_sel = (self._wl_sel + 1) % len(entries)
        if pyxel.btnp(pyxel.KEY_S):
            # Direct shortcut: scan & select from main whitelist screen
            self._wl_scan_list = self._wl_build_scan_list()
            self._wl_scan_sel = 0
            self._wl_add_step = "scan_select"
            return
        if pyxel.btnp(pyxel.KEY_A):
            self._wl_add_step = "type"
            return
        if pyxel.btnp(pyxel.KEY_D) and entries:
            e = entries[self._wl_sel]
            self._whitelist.remove(self._wl_sel)
            self._term_add(f"[WL] Removed: {e.name} ({e.mac[-8:]})", raw=True)
            self.msg("[WL] Entry removed", C_WARNING)
            if self._wl_sel >= self._whitelist.count:
                self._wl_sel = max(0, self._whitelist.count - 1)
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._wl_screen = False
            self._esc_consumed_frame = pyxel.frame_count

    def _draw_wl_screen(self):
        pyxel.cls(0)
        # Title bar
        pyxel.rect(0, 0, W, 12, 1)
        pyxel.text(4, 3, f"WHITELIST  [{self._whitelist.count} entries]",
                   C_HACK_CYAN)

        if self._wl_add_step == "type":
            # Type selection — manual or scan
            n_dev = len(self.wifi_networks) + len(self.ble_devices)
            pyxel.text(20, 36, "Add device to whitelist:", C_TEXT)
            pyxel.text(20, 54, "[1] WiFi  (manual BSSID + SSID)", C_SUCCESS)
            pyxel.text(20, 66, "[2] BLE   (manual MAC + Name)", C_HACK_CYAN)
            pyxel.text(20, 86, f"[S] Scan & Select  ({n_dev} devices nearby)",
                       C_WARNING if n_dev else C_DIM)
            pyxel.text(20, 106, "[ESC] Cancel", C_DIM)
            return

        if self._wl_add_step == "scan_select":
            self._draw_wl_scan_select()
            return

        entries = self._whitelist.entries
        if not entries:
            pyxel.text(20, 50, "No entries.", C_DIM)
            pyxel.text(20, 65, "Press [A] to add a device.", C_DIM)
        else:
            ROW_H = 12
            y = 20
            max_vis = (H - 44) // ROW_H
            start = max(0, self._wl_sel - max_vis + 1)
            for i in range(start, min(len(entries), start + max_vis)):
                e = entries[i]
                sel = (i == self._wl_sel)
                if sel:
                    pyxel.rect(2, y - 1, W - 4, ROW_H, 1)
                tag = "WiFi" if e.type == "wifi" else " BLE"
                tc = C_SUCCESS if e.type == "wifi" else C_HACK_CYAN
                lbl = f"[{tag}] {e.mac}  {e.name[:24]}"
                pyxel.text(6, y + 2, lbl, tc if not sel else 7)
                pyxel.text(W - 80, y + 2, e.added_date[:10], C_DIM)
                y += ROW_H

        # Bottom bar
        pyxel.rect(0, H - 14, W, 14, 1)
        pyxel.text(4, H - 10,
                   "[A]Add manual  [S]Scan & Select  [D]Delete  [ESC]Back",
                   C_DIM)

    def _draw_wl_scan_select(self):
        """Draw the scan-and-select device picker for whitelist."""
        items = self._wl_scan_list
        # Sub-title + scan status
        scanning = self._wl_scan_status
        title = f"Select device to whitelist  ({len(items)} found)"
        pyxel.text(4, 18, title, C_TEXT)
        if scanning and pyxel.frame_count % 30 < 20:
            status = "WiFi scanning..." if scanning == "wifi" else "BLE scanning..."
            pyxel.text(4 + len(title) * 5 + 8, 18, status, C_WARNING)

        if not items:
            pyxel.text(20, 50, "No devices discovered yet.", C_DIM)
            pyxel.text(20, 72, "Press [W] to scan WiFi", C_SUCCESS)
            pyxel.text(20, 86, "Press [B] to scan BLE", C_HACK_CYAN)
            pyxel.text(20, 104, "List auto-refreshes after scan.", C_DIM)
            pyxel.rect(0, H - 14, W, 14, 1)
            pyxel.text(4, H - 10,
                       "[W]WiFi scan  [B]BLE scan  [R]Refresh  [ESC]Back",
                       C_DIM)
            return

        # Column headers
        hdr_y = 32
        pyxel.text(6, hdr_y, "Type", C_DIM)
        pyxel.text(44, hdr_y, "Name/SSID", C_DIM)
        pyxel.text(240, hdr_y, "MAC", C_DIM)
        pyxel.text(380, hdr_y, "RSSI", C_DIM)
        pyxel.text(420, hdr_y, "Info", C_DIM)

        # Rows
        row_h = 12
        max_vis = (H - 58) // row_h
        start = max(0, self._wl_scan_sel - max_vis + 1)
        y = hdr_y + 12
        for i in range(start, min(len(items), start + max_vis)):
            it = items[i]
            sel = (i == self._wl_scan_sel)
            if sel:
                pyxel.rect(2, y - 1, W - 4, row_h, 1)

            is_wifi = it["type"] == "wifi"
            tag = "WiFi" if is_wifi else " BLE"
            tc = C_SUCCESS if is_wifi else C_HACK_CYAN
            c = 7 if sel else tc

            pyxel.text(6, y + 2, f"[{tag}]", tc)
            pyxel.text(44, y + 2, (it["name"] or "???")[:32], c)
            pyxel.text(240, y + 2, it["mac"], c)
            pyxel.text(380, y + 2, str(it["rssi"]), C_DIM)
            pyxel.text(420, y + 2, it["extra"][:12], C_DIM)
            y += row_h

        # Scroll indicator
        if len(items) > max_vis:
            bar_h = max(4, (H - 58) * max_vis // len(items))
            bar_y = 44 + (H - 58 - bar_h) * start // max(1, len(items) - max_vis)
            pyxel.rect(W - 3, bar_y, 2, bar_h, C_DIM)

        # Bottom bar
        pyxel.rect(0, H - 14, W, 14, 1)
        pyxel.text(4, H - 10,
                   "[ENTER]Add [W]WiFi [B]BLE [R]Refresh [ESC]Back", C_DIM)

    # ------------------------------------------------------------------
    # MeshCore Messenger screen
    # ------------------------------------------------------------------

    def _update_wd_screen(self):
        """Input for the WDGWars #general chat overlay."""
        # ESC — close overlay
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._wd_screen = False
            self._esc_consumed_frame = pyxel.frame_count
            return

        # Scroll
        if pyxel.btnp(pyxel.KEY_PAGEUP):
            self._wd_scroll = min(self._wd_scroll + 5,
                                  max(0, len(self._wd_log) - 5))
        if pyxel.btnp(pyxel.KEY_PAGEDOWN):
            self._wd_scroll = max(0, self._wd_scroll - 5)

        # ENTER — send to #general
        if pyxel.btnp(pyxel.KEY_RETURN):
            text = self._wd_input.strip()
            if text and self._wd is not None:
                ts = time.strftime("%H:%M")
                token = self._wd.send(text)
                idx = len(self._wd_log)
                self._wd_log.append(
                    (f"\x11 {self._wd.nick}: {text}", C_WARNING, ts))
                self._wd_tx_pending[token] = idx
                self._wd_input = ""
                self._wd_scroll = 0
            return

        # Backspace
        if pyxel.btnp(pyxel.KEY_BACKSPACE):
            self._wd_input = self._wd_input[:-1]
            return

        # Ctrl shortcuts (don't pass to typing)
        ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
        if ctrl:
            if pyxel.btnp(pyxel.KEY_X):
                self._wd_log.clear()
                self._wd_scroll = 0
            elif pyxel.btnp(pyxel.KEY_E):
                # Toggle dev echo-beacon target (no #general spam)
                self._wd.dev_echo = not self._wd.dev_echo
                tgt = "ECHO beacon (dev)" if self._wd.dev_echo else "#general"
                self._wd_log.append((f"\x11 Target: {tgt}", C_HACK_CYAN))
            elif pyxel.btnp(pyxel.KEY_L):
                self._wd_toggle_lora()
            return

        # Typing
        c = self._get_char_input()
        if c and len(self._wd_input) < 120:
            self._wd_input += c

    def _draw_wd_screen(self):
        pyxel.cls(0)
        BAR_H = 14
        pyxel.rect(0, 0, W, BAR_H, 1)
        if self._wd is not None and self._wd.running:
            status = "ECHO" if self._wd.dev_echo else "ACTIVE"
        else:
            status = "CONNECTING"
        if self._wd and self._wd.lora_active:
            transport = f"LoRa rx:{self._lora.packets_received}"
        else:
            transport = "TCP"
        pyxel.text(4, 4, f"WDGWARS #general [{status}] [{transport}] "
                   f"you:{self._wd.nick if self._wd else '?'}  "
                   f"(Ctrl+L LoRa  Ctrl+E echo  Ctrl+X clear)", C_HACK_CYAN)

        _PL = str.maketrans(
            "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")

        log = self._wd_log
        total = len(log)
        line_h = 10
        char_w = 5
        chat_top = BAR_H + 2
        chat_bot = H - 24
        msg_bot = chat_bot - (line_h if self._wd_input else 0)
        max_vis = (msg_bot - chat_top) // line_h

        if self._wd_scroll == 0:
            start = max(0, total - max_vis)
            end = total
        else:
            end = max(0, total - self._wd_scroll)
            start = max(0, end - max_vis)

        y = chat_top
        for i in range(start, end):
            entry = log[i]
            text, color = entry[0], entry[1]
            tag = entry[2] if len(entry) > 2 else None
            full = text.translate(_PL)
            tag_chars = len(tag) + 1 if tag else 0
            first_max = (W - 8) // char_w - tag_chars
            wrap_max = (W - 8) // char_w
            pyxel.text(4, y, full[:first_max], color)
            if tag:
                tx = W - len(tag) * char_w - 4
                pyxel.text(tx, y, tag, C_DIM)
            y += line_h
            pos = first_max
            while pos < len(full) and y < msg_bot:
                pyxel.text(4, y, full[pos:pos + wrap_max], color)
                pos += wrap_max
                y += line_h
            if y >= msg_bot:
                break

        if total == 0:
            pyxel.text(20, chat_top + 40, "No messages yet.", C_DIM)
            pyxel.text(20, chat_top + 54,
                       "Type a message and press ENTER to send.", C_DIM)

        if self._wd_scroll > 0:
            pyxel.text(W - 120, chat_top, f"SCROLL +{self._wd_scroll}", C_DIM)

        # Input bar
        cursor = "_" if pyxel.frame_count % 30 < 20 else ""
        prefix = "> "
        pyxel.rect(0, chat_bot, W, 14, 1)
        visible_w = (W - 8) // char_w - len(prefix)
        inp = self._wd_input
        if len(inp) > visible_w:
            inp = inp[len(inp) - visible_w:]
        pyxel.text(4, chat_bot + 3, f"{prefix}{inp}{cursor}", C_TEXT)

        inp_len = len(self._wd_input)
        if inp_len > 0:
            at_max = inp_len >= 120
            cnt_c = C_ERROR if at_max else (
                C_WARNING if inp_len > 100 else C_DIM)
            cnt_txt = f"{inp_len}/120"
            pyxel.text(W - len(cnt_txt) * char_w - 4, chat_bot - line_h,
                       cnt_txt, cnt_c)

    def _update_mc_screen(self):
        # Ctrl+H toggles contacts panel (always, even when panel is open)
        ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
        if ctrl and pyxel.btnp(pyxel.KEY_H):
            self._mc_nodes_panel = not self._mc_nodes_panel
            self._mc_node_action = False
            self._mc_note_editing = False
            return

        # Overlay inputs take priority over everything
        # Nodes panel input (when open and has nodes)
        if self._mc_nodes_panel and self._mc_nodes:
            self._update_mc_nodes_panel()
            return

        # ESC — exit DM mode or hide chat
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            if self._mc_dm_target:
                self._mc_dm_target = None
                self._mc_log.append(
                    ("\x10 DM mode off — back to channel", C_HACK_CYAN))
                return
            self._mc_screen = False
            self._esc_consumed_frame = pyxel.frame_count
            return

        # Scroll
        if pyxel.btnp(pyxel.KEY_PAGEUP):
            self._mc_scroll = min(self._mc_scroll + 5,
                                  max(0, len(self._mc_log) - 5))
        if pyxel.btnp(pyxel.KEY_PAGEDOWN):
            self._mc_scroll = max(0, self._mc_scroll - 5)

        # Channel picker overlay input
        if self._mc_chan_picker:
            if pyxel.btnp(pyxel.KEY_UP):
                self._mc_chan_sel = max(0, self._mc_chan_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mc_chan_sel = min(len(self._mc_channels_list) - 1,
                                        self._mc_chan_sel + 1)
            elif pyxel.btnp(pyxel.KEY_RETURN):
                self._mc_active_ch = self._mc_chan_sel
                self._lora.set_mc_active_channel(self._mc_active_ch)
                ch = self._mc_channels_list[self._mc_active_ch]
                self._mc_log.append((f"Channel: {ch.name}", C_HACK_CYAN))
                self._mc_chan_picker = False
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_chan_picker = False
            return

        # ENTER — send message (DM or channel)
        if pyxel.btnp(pyxel.KEY_RETURN):
            text = self._mc_input.strip()
            if text:
                ts = time.strftime("%H:%M")
                if self._mc_dm_target:
                    # Encrypted DM
                    pk_hex = self._mc_dm_target.get("pubkey", "")
                    if pk_hex:
                        dest_pub = bytes.fromhex(pk_hex)
                        dedup_key = self._lora.send_meshcore_dm(
                            text, self._mc_node_name, dest_pub)
                        tgt = self._mc_dm_target["name"]
                        idx = len(self._mc_log)
                        self._mc_log.append(
                            (f"\x11 [DM\u2192{tgt}] {text}", 12, ts))
                        # Map pending ACK hashes to log index for √
                        for ah in list(self._lora._pending_dm_acks):
                            self._mc_dm_ack_map[ah] = idx
                        if dedup_key:
                            self._mc_tx_pending[dedup_key] = idx
                    else:
                        self._mc_log.append(
                            ("\x10 No pubkey — can't send DM", C_WARNING))
                else:
                    # Channel message — AIO LoRa or watch fallback
                    if self._lora.running and self._lora.mode == "meshcore":
                        dedup_key = self._lora.send_meshcore_message(
                            text, self._mc_node_name)
                        idx = len(self._mc_log)
                        self._mc_log.append(
                            (f"\x11 {self._mc_node_name}: {text}",
                             C_WARNING, ts))
                        if dedup_key:
                            self._mc_tx_pending[dedup_key] = idx
                    elif self._watch.connected:
                        # Send via watch LoRa
                        self._watch.send_command("lora_send",
                                                 {"text": text})
                        self._mc_log.append(
                            (f"\x11 {self._mc_node_name}: {text}",
                             C_WARNING, f"{ts} W"))
                self._mc_input = ""
                self._mc_scroll = 0
            return

        # Backspace
        if pyxel.btnp(pyxel.KEY_BACKSPACE):
            self._mc_input = self._mc_input[:-1]
            return

        # Shortcut keys (Ctrl+key — always available, don't block typing)
        ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
        if ctrl:
            if pyxel.btnp(pyxel.KEY_A):
                lat = self.player_lat if self.gps_fix else 0.0
                lon = self.player_lon if self.gps_fix else 0.0
                self._lora.send_meshcore_advert(
                    self._mc_node_name, lat, lon)
                self._mc_log.append(
                    (f"\x11 Advert: {self._mc_node_name}", C_HACK_CYAN))
                return
            if pyxel.btnp(pyxel.KEY_N):
                # Name change via input dialog
                self.input_mode = True
                self.input_fields = [{"label": "Node Name", "value":
                                      self._mc_node_name}]
                self.input_field_idx = 0
                self._input_pending_cat = -5
                self._input_pending_item = -1
                return
            if pyxel.btnp(pyxel.KEY_X):
                self._mc_log.clear()
                self._mc_scroll = 0
                return
            if pyxel.btnp(pyxel.KEY_C):
                self._mc_chan_picker = not self._mc_chan_picker
                self._mc_chan_sel = self._mc_active_ch
                return
            return  # Ctrl held — don't pass to typing

        # Channel quick-switch with [ and ]
        if pyxel.btnp(pyxel.KEY_LEFTBRACKET):
            if self._mc_channels_list:
                self._mc_active_ch = (self._mc_active_ch - 1) % len(self._mc_channels_list)
                self._lora.set_mc_active_channel(self._mc_active_ch)
                ch = self._mc_channels_list[self._mc_active_ch]
                self._mc_log.append((f"Channel: {ch.name}", C_HACK_CYAN))
            return
        if pyxel.btnp(pyxel.KEY_RIGHTBRACKET):
            if self._mc_channels_list:
                self._mc_active_ch = (self._mc_active_ch + 1) % len(self._mc_channels_list)
                self._lora.set_mc_active_channel(self._mc_active_ch)
                ch = self._mc_channels_list[self._mc_active_ch]
                self._mc_log.append((f"Channel: {ch.name}", C_HACK_CYAN))
            return

        # Typing
        c = self._get_char_input()
        if c and len(self._mc_input) < 120:
            self._mc_input += c

    def _update_mc_nodes_panel(self):
        """Handle input for the contacts panel overlay."""
        if not self._mc_nodes:
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_nodes_panel = False
            return
        # Note editing mode
        if self._mc_note_editing:
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_note_editing = False
            elif pyxel.btnp(pyxel.KEY_RETURN):
                nd = self._mc_nodes[self._mc_node_sel]
                nd["note"] = self._mc_note_buf
                if self.loot:
                    if self._mesh_view_protocol == "meshtastic":
                        self.loot.save_meshtastic_contact_note(nd["id"], self._mc_note_buf)
                    else:
                        self.loot.save_contact_note(nd["id"], self._mc_note_buf)
                self._mc_note_editing = False
            elif pyxel.btnp(pyxel.KEY_BACKSPACE):
                self._mc_note_buf = self._mc_note_buf[:-1]
            else:
                c = self._get_char_input()
                if c and len(self._mc_note_buf) < 40:
                    self._mc_note_buf += c
            return

        # Action menu
        if self._mc_node_action:
            _actions = self._mesh_contact_actions()
            if pyxel.btnp(pyxel.KEY_UP):
                self._mc_node_action_sel = max(0, self._mc_node_action_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mc_node_action_sel = min(
                    len(_actions) - 1, self._mc_node_action_sel + 1)
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_node_action = False
            elif pyxel.btnp(pyxel.KEY_RETURN):
                nd = self._mc_nodes[self._mc_node_sel]
                act = _actions[self._mc_node_action_sel]
                if act == "DM":
                    ntype = nd.get("type", 0)
                    # Repeaters/Room/Sensor don't process DMs
                    ntype_str = str(ntype)
                    is_client = ntype in (0, 1, "Client")
                    if not nd.get("pubkey"):
                        self._mc_log.append(
                            (f"\x10 No pubkey for {nd['name']} "
                             f"— need advert first", C_WARNING))
                    elif not is_client:
                        self._mc_log.append(
                            (f"\x10 {nd['name']} is {ntype_str} "
                             f"— DM only works with Clients",
                             C_WARNING))
                    else:
                        self._mc_dm_target = nd
                        self._mc_input = ""
                        self._mc_log.append(
                            (f"\x10 DM mode: {nd['name']} "
                             f"(ESC to exit)", 12))
                    self._mc_node_action = False
                    self._mc_nodes_panel = False
                elif act == "Note":
                    self._mc_note_buf = nd.get("note", "")
                    self._mc_note_editing = True
                    self._mc_node_action = False
                elif act == "Info":
                    fs = nd.get("first_seen", 0)
                    fs_str = time.strftime("%m-%d %H:%M",
                                           time.localtime(fs)) if fs else "?"
                    note = nd.get("note", "")
                    self._mc_log.append(
                        (f"\x10 [{nd['name']}] id:{nd['id']} "
                         f"type:{nd.get('type','?')} "
                         f"first:{fs_str}", C_HACK_CYAN))
                    if note:
                        self._mc_log.append(
                            (f"  Note: {note}", C_DIM))
                    from watchdogs.mesh_position import mesh_position
                    position = mesh_position(nd)
                    if position is not None:
                        source = "Node GPS" if position["source"] == "node" else "Receiver GPS"
                        self._mc_log.append(
                            (f"  {source}: {position['lat']:.5f}, {position['lon']:.5f}",
                             C_DIM))
                    self._mc_node_action = False
                elif act == "Delete":
                    name = nd.get("name", "?")
                    nid = nd["id"]
                    self._mc_nodes.remove(nd)
                    if self.loot:
                        if self._mesh_view_protocol == "meshtastic":
                            self.loot.delete_meshtastic_contact(nid)
                        else:
                            self.loot.delete_contact(nid)
                    # Remove from LoRa known pubkeys
                    if self._mesh_view_protocol != "meshtastic":
                        self._lora._known_pubkeys.pop(nid, None)
                    key = f"{self._mesh_view_protocol}:{nid}"
                    self.markers[:] = [m for m in self.markers if getattr(m, "mesh_key", None) != key]
                    self._mc_log.append(
                        (f"\x10 Deleted contact: {name}", C_WARNING))
                    self._mc_node_action = False
                    self._mc_node_sel = min(self._mc_node_sel,
                                            max(0, len(self._mc_nodes) - 1))
                else:
                    self._mc_node_action = False
            return

        # Node list navigation
        if pyxel.btnp(pyxel.KEY_UP):
            self._mc_node_sel = max(0, self._mc_node_sel - 1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._mc_node_sel = min(len(self._mc_nodes) - 1,
                                    self._mc_node_sel + 1)
        elif pyxel.btnp(pyxel.KEY_RETURN):
            self._mc_node_action = True
            self._mc_node_action_sel = 0
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            self._mc_nodes_panel = False

    def _mesh_contact_actions(self):
        return (["Note", "Info", "Delete", "Close"] if self._mesh_view_protocol == "meshtastic"
                else ["DM", "Note", "Info", "Delete", "Close"])

    def _draw_mc_screen(self):
        pyxel.cls(0)
        # Title bar — 14px tall for 5x8 font + padding
        BAR_H = 14
        pyxel.rect(0, 0, W, BAR_H, 1)
        is_mt = self._mesh_view_protocol == "meshtastic"
        manager = self._mt if is_mt else self._lora
        active = manager.running and manager.ready and (is_mt or manager.mode == "meshcore")
        status = "ACTIVE" if active else ("WATCH" if not is_mt and self._watch.connected else "OFF")
        pkts = manager.packets_received
        title = "MESHTASTIC" if is_mt else "MESHCORE"
        ch_name = "?"
        if self._mc_channels_list and self._mc_active_ch < len(self._mc_channels_list):
            ch_name = self._mc_channels_list[self._mc_active_ch].name
        if is_mt:
            ch_name = f"PUBLIC/{ch_name}"
        if self._mc_dm_target:
            dm_name = self._mc_dm_target.get("name", "?")
            pyxel.text(4, 4, f"{title} [DM: {dm_name}] [{status}] "
                       f"pkts:{pkts} node:{self._mc_node_name}", 12)
        else:
            pyxel.text(4, 4, f"{title} [{ch_name}] [{status}] "
                       f"pkts:{pkts} node:{self._mc_node_name} "
                       f"nodes:{len(self._mc_nodes)}", C_HACK_CYAN)

        # Polish character transliteration (BDF/pyxel fonts are ASCII-only)
        _PL = str.maketrans(
            "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
            "acelnoszzACELNOSZZ")

        # Chat log — now uses the global Spleen 5x8 via pyxel.text monkey-patch
        log = self._mc_log
        total = len(log)
        line_h = 10
        char_w = 5
        chat_top = BAR_H + 2
        chat_bot = H - 24
        # Reserve line for char counter when typing
        msg_bot = chat_bot - (line_h if self._mc_input else 0)
        max_vis = (msg_bot - chat_top) // line_h

        if self._mc_scroll == 0:
            start = max(0, total - max_vis)
            end = total
        else:
            end = max(0, total - self._mc_scroll)
            start = max(0, end - max_vis)

        y = chat_top
        for i in range(start, end):
            entry = log[i]
            text, color = entry[0], entry[1]
            tag = entry[2] if len(entry) > 2 else None
            full = text.translate(_PL)
            # Reserve space for actual tag length (right-aligned to edge)
            tag_chars = len(tag) + 1 if tag else 0  # +1 for gap
            first_max = (W - 8) // char_w - tag_chars
            wrap_max = (W - 8) // char_w  # continuation lines use full width
            pyxel.text(4, y, full[:first_max], color)
            if tag:
                tx = W - len(tag) * char_w - 4
                pyxel.text(tx, y, tag, C_DIM)
            y += line_h
            # Wrap remaining text
            pos = first_max
            while pos < len(full) and y < msg_bot:
                pyxel.text(4, y, full[pos:pos + wrap_max], color)
                pos += wrap_max
                y += line_h
            if y >= msg_bot:
                break

        if total == 0:
            pyxel.text(20, chat_top + 40, "No messages yet.", C_DIM)
            pyxel.text(20, chat_top + 54,
                       "Type a message and press ENTER to send.", C_DIM)

        # Scroll indicator
        if self._mc_scroll > 0:
            pyxel.text(W - 120, chat_top, f"SCROLL +{self._mc_scroll}", C_DIM)

        # Input bar
        cursor = "_" if pyxel.frame_count % 30 < 20 else ""
        prefix = "DM> " if self._mc_dm_target else "> "
        bar_bg = 12 if self._mc_dm_target else 1
        bar_fg = 0 if self._mc_dm_target else C_TEXT
        pyxel.rect(0, chat_bot, W, 14, bar_bg)
        visible_w = (W - 8) // char_w - len(prefix)
        inp = self._mc_input
        if len(inp) > visible_w:
            inp = inp[len(inp) - visible_w:]
        pyxel.text(4, chat_bot + 3, f"{prefix}{inp}{cursor}", bar_fg)

        # Character counter above input bar (right side)
        inp_len = len(self._mc_input)
        if inp_len > 0:
            at_max = inp_len >= 120
            cnt_c = C_ERROR if at_max else (
                C_WARNING if inp_len > 100 else C_DIM)
            cnt_txt = f"{inp_len}/120"
            pyxel.text(W - len(cnt_txt) * char_w - 4, chat_bot - line_h,
                       cnt_txt, cnt_c)
            # Flash "MAX" warning when limit reached
            if at_max and pyxel.frame_count % 40 < 25:
                pyxel.text(W - len(cnt_txt) * char_w - 45,
                           chat_bot - line_h, "MAX", C_ERROR)

        # Contacts panel overlay — 12px rows for 5x8 font
        if self._mc_nodes_panel and self._mc_nodes:
            pw = 280
            px_x = W - pw - 2
            panel_top = BAR_H + 2
            panel_bot = chat_bot - 14
            row_h = 12
            max_vis = (panel_bot - 28) // row_h
            pyxel.rect(px_x, panel_top, pw, panel_bot, 0)
            pyxel.rectb(px_x, panel_top, pw, panel_bot, 2)
            pyxel.text(px_x + 4, panel_top + 4,
                       f"CONTACTS ({len(self._mc_nodes)})", 2)
            pyxel.text(px_x + pw - 90, panel_top + 4, "ENTER=action", C_DIM)
            pyxel.line(px_x + 2, panel_top + 14,
                       px_x + pw - 3, panel_top + 14, 1)
            _type_icons = {0: "C", 1: "C", 2: "R", 3: "M", 4: "S",
                           "Client": "C", "Repeater": "R", "Router": "R",
                           "Room": "M", "Sensor": "S"}
            # Clamp selection
            self._mc_node_sel = max(0, min(
                self._mc_node_sel, len(self._mc_nodes) - 1))
            # Scroll to keep selection visible
            scroll_off = max(0, self._mc_node_sel - max_vis + 1)
            ny = panel_top + 18
            for idx in range(scroll_off,
                             min(len(self._mc_nodes), scroll_off + max_vis)):
                nd = self._mc_nodes[idx]
                raw_type = nd.get("type", 0)
                try:
                    raw_type = int(raw_type)
                except (ValueError, TypeError):
                    pass
                icon = _type_icons.get(raw_type, "?")
                name = nd.get("name", "?")[:14]
                rssi = nd.get("rssi", 0)
                snr = nd.get("snr", 0)
                note = nd.get("note", "")
                age = ""
                ls = nd.get("last_seen", 0)
                if ls:
                    elapsed = int(time.time() - ls)
                    if elapsed < 60:
                        age = f"{elapsed}s"
                    elif elapsed < 3600:
                        age = f"{elapsed // 60}m"
                    else:
                        age = f"{elapsed // 3600}h"
                is_rep = icon == "R"
                selected = idx == self._mc_node_sel
                # Row background
                if selected:
                    pyxel.rect(px_x + 2, ny - 1, pw - 4, row_h, 2)
                elif is_rep:
                    pyxel.rect(px_x + 2, ny - 1, pw - 4, row_h, 1)
                c_icon = (0 if selected else
                          C_SUCCESS if is_rep else
                          (2 if icon == "M" else C_TEXT))
                c_name = 0 if selected else (C_SUCCESS if is_rep else C_TEXT)
                c_info = 0 if selected else (C_SUCCESS if is_rep else C_DIM)
                pyxel.text(px_x + 4, ny, icon, c_icon)
                pyxel.text(px_x + 14, ny, name, c_name)
                if note:
                    pyxel.text(px_x + 120, ny, f'"{note[:10]}"', c_info)
                pyxel.text(px_x + 185, ny, f"{rssi:.0f}dB", c_info)
                pyxel.text(px_x + 220, ny, age, c_info)
                ny += row_h

            # Action menu popup
            if self._mc_node_action and self._mc_node_sel < len(self._mc_nodes):
                _actions = self._mesh_contact_actions()
                aw = 80
                ah = len(_actions) * 12 + 8
                ax = px_x + 40
                ay = 40
                pyxel.rect(ax, ay, aw, ah, 0)
                pyxel.rectb(ax, ay, aw, ah, C_WARNING)
                for ai, act in enumerate(_actions):
                    sel = ai == self._mc_node_action_sel
                    if sel:
                        pyxel.rect(ax + 2, ay + 4 + ai * 12, aw - 4, 11,
                                   C_WARNING)
                    pyxel.text(ax + 6, ay + 6 + ai * 12, act,
                               0 if sel else C_TEXT)

            # Note editing overlay
            if self._mc_note_editing:
                nw = 200
                nx = (W - nw) // 2
                ny2 = 80
                pyxel.rect(nx, ny2, nw, 30, 0)
                pyxel.rectb(nx, ny2, nw, 30, C_HACK_CYAN)
                pyxel.text(nx + 4, ny2 + 3, "EDIT NOTE (ENTER=save ESC=cancel)",
                           C_HACK_CYAN)
                cur = "_" if pyxel.frame_count % 30 < 20 else ""
                pyxel.text(nx + 4, ny2 + 16,
                           f"{self._mc_note_buf}{cur}", C_TEXT)

        # Channel picker overlay
        if self._mc_chan_picker:
            pw = 200
            px_x = (W - pw) // 2
            py_y = 40
            ph = len(self._mc_channels_list) * 12 + 30
            pyxel.rect(px_x, py_y, pw, ph, 0)
            pyxel.rectb(px_x, py_y, pw, ph, C_HACK_CYAN)
            pyxel.text(px_x + 4, py_y + 3, "SELECT CHANNEL", C_HACK_CYAN)
            pyxel.line(px_x + 2, py_y + 11, px_x + pw - 3, py_y + 11, 1)
            cy = py_y + 15
            for i, ch in enumerate(self._mc_channels_list):
                sel = (i == self._mc_chan_sel)
                active = (i == self._mc_active_ch)
                if sel:
                    pyxel.rect(px_x + 2, cy - 1, pw - 4, 11, C_HACK_CYAN)
                c = 0 if sel else C_TEXT
                marker = " <<<" if active else ""
                pyxel.text(px_x + 6, cy + 1, f"{ch.name}{marker}", c)
                cy += 12
            pyxel.text(px_x + 4, cy + 2, "ENTER=select  ESC=cancel", C_DIM)

        # Bottom hints
        pyxel.rect(0, H - 10, W, 10, 0)
        pyxel.text(4, H - 8,
                   ("C-N Name  C-H Nodes  C-X Clear  ENTER Send  ESC Back" if is_mt else
                    "C-A Advert  C-N Name  C-H Nodes  C-C Chan  [/]Switch  C-X Clear  ESC Back"),
                   C_DIM)
        if getattr(self, "input_mode", False):
            self._draw_input_dialog()

    # ------------------------------------------------------------------
    # Loot screen search
    # ------------------------------------------------------------------

    def _update_loot_screen(self):
        """Handle input on the loot database screen."""
        # Password history viewer overlay
        if self._loot_pwd_screen:
            if pyxel.btnp(pyxel.KEY_ESCAPE) or pyxel.btnp(pyxel.KEY_D):
                self._loot_pwd_screen = False
            elif pyxel.btnp(pyxel.KEY_UP):
                self._loot_pwd_scroll = max(0, self._loot_pwd_scroll - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._loot_pwd_scroll += 1
            return

        if self._loot_search_active:
            # Typing in search field
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._loot_search:
                self._loot_search = self._loot_search[:-1]
            elif pyxel.btnp(pyxel.KEY_RETURN):
                self._loot_search_exec()
                self._loot_search_active = False
            else:
                c = self._get_char_input()
                if c and len(self._loot_search) < 28:
                    self._loot_search += c
        elif self._loot_search_results:
            # Scrolling results
            if pyxel.btnp(pyxel.KEY_UP):
                self._loot_search_scroll = max(0, self._loot_search_scroll - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._loot_search_scroll += 1
        else:
            # Normal loot screen — activate search or password viewer
            if pyxel.btnp(pyxel.KEY_SLASH) or pyxel.btnp(pyxel.KEY_F):
                self._loot_search_active = True
                self._loot_search = ""
                self._loot_search_results = []
                self._loot_search_scroll = 0
            elif pyxel.btnp(pyxel.KEY_D):
                self._load_all_captures()
                self._loot_pwd_screen = True
                self._loot_pwd_scroll = 0

    def _loot_search_exec(self):
        """Search all loot points (all sessions) by SSID/name or BSSID/MAC."""
        q = self._loot_search.lower().strip()
        if not q:
            self._loot_search_results = []
            return
        results = []
        for pt in self.loot_points:
            label = (pt.get("label") or "").lower()
            bssid = (pt.get("bssid") or "").lower()
            if q in label or q in bssid:
                results.append(pt)
        # Dedup by BSSID/MAC — keep strongest RSSI
        seen: dict[str, dict] = {}
        for r in results:
            key = r.get("bssid") or r.get("label", "")
            if key not in seen:
                seen[key] = r
            else:
                try:
                    if int(r.get("rssi", -99)) > int(seen[key].get("rssi", -99)):
                        seen[key] = r
                except (ValueError, TypeError):
                    pass
        self._loot_search_results = sorted(
            seen.values(), key=lambda x: (x.get("label") or "").lower())
        self._loot_search_scroll = 0

    def _load_all_captures(self):
        """Scan all session dirs for portal_passwords.log & evil_twin_capture.log."""
        from urllib.parse import unquote_plus
        loot_dir = Path(self._app_dir) / "loot"
        entries: list[tuple[str, str]] = []  # (session_date, formatted_line)
        if not loot_dir.is_dir():
            self._loot_pwd_data = []
            return
        for session in sorted(loot_dir.iterdir(), reverse=True):
            if not session.is_dir():
                continue
            name = session.name
            # Validate session dir format: YYYY-MM-DD_HH-MM-SS
            if len(name) < 19 or name[4] != "-" or name[7] != "-":
                continue
            date_tag = name[:10]  # YYYY-MM-DD
            for logfile in ("portal_passwords.log", "evil_twin_capture.log"):
                fpath = session / logfile
                if not fpath.is_file():
                    continue
                tag = "EP" if "portal" in logfile else "ET"
                try:
                    for raw_line in fpath.read_text(
                            encoding="utf-8", errors="replace").splitlines():
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        decoded = unquote_plus(raw_line)
                        fields = self._parse_post_fields(decoded)
                        if fields:
                            entries.append((date_tag, f"[{tag}] {date_tag} {fields}"))
                        # Skip non-credential ET/EP diagnostics (client
                        # connects, portal counts, channel switches) — they
                        # live in the log file for forensics but shouldn't
                        # appear under "Captured Passwords".
                except OSError:
                    pass
        # Sort newest first (already reversed session order)
        self._loot_pwd_data = [e[1] for e in entries]

    def _draw_loot_screen(self):
        pyxel.cls(0)
        ROW_H = 10   # 8px text + 2px gap for 5x8 font
        # Border + header
        pyxel.rectb(1, 1, W - 2, H - 2, C_HACK_CYAN)
        pyxel.rect(2, 2, W - 4, 14, 1)
        pyxel.text(6, 5, "LOOT DATABASE", C_HACK_CYAN)
        pyxel.text(120, 5, "//", C_COAST)
        pyxel.text(134, 5, "ESP32 WATCH DOGS", C_DIM)
        pyxel.text(W - 70, 5, "[`] close", C_DIM)
        pyxel.line(1, 16, W - 2, 16, C_HACK_CYAN)

        t = self._loot_totals
        col1, col2, col3 = 10, 230, 440  # 3 columns

        # ─── COLUMN 1: ALL-TIME TOTALS ───
        y = 22
        pyxel.text(col1, y, "ALL-TIME TOTALS", C_HACK_CYAN)
        pyxel.line(col1, y + 10, col1 + 100, y + 10, 1)
        _ua = int((pyxel.frame_count * 2) % 150)
        if _ua < 100:
            pyxel.pset(col1 + _ua, y + 10, C_HACK_CYAN)
            if _ua > 0:
                pyxel.pset(col1 + _ua - 1, y + 10, 5)
        y += 14
        pyxel.text(col1, y, f"Sessions", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('sessions', 0)}", C_TEXT)
        y += ROW_H
        pyxel.text(col1, y, f"WiFi nets", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('wifi', 0)}", C_SUCCESS)
        y += ROW_H
        pyxel.text(col1, y, f"BT devices", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('bt', 0)}", C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col1, y, f"Handshakes", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('pcap', 0)}", C_ERROR)
        y += ROW_H
        pyxel.text(col1, y, f"HC22000", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('hs', 0)}", C_ERROR)
        y += ROW_H
        t_pwd_all = t.get('passwords', 0) + t.get('et_captures', 0)
        pyxel.text(col1, y, f"Passwords", C_DIM)
        pyxel.text(col1 + 80, y, f"{t_pwd_all}", 12)  # blue
        y += ROW_H
        pyxel.text(col1, y, f"MC Nodes", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('mc_nodes', 0)}", C_WARNING)
        y += ROW_H
        pyxel.text(col1, y, f"MC Msgs", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('mc_msgs', 0)}", C_WARNING)
        y += ROW_H
        pyxel.text(col1, y, "MT Nodes", C_DIM)
        pyxel.text(col1 + 80, y, str(t.get("mt_nodes", 0)), C_WARNING)
        y += ROW_H
        pyxel.text(col1, y, "MT Msgs", C_DIM)
        pyxel.text(col1 + 80, y, str(t.get("mt_msgs", 0)), C_WARNING)
        y += ROW_H
        pyxel.text(col1, y, f"SVC alerts", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('emergency', 0)}", C_ERROR)
        y += ROW_H
        n_contacts = (len(self.loot.load_contacts()) + len(self.loot.load_meshtastic_contacts())) if self.loot else 0
        pyxel.text(col1, y, f"Contacts", C_DIM)
        pyxel.text(col1 + 80, y, f"{n_contacts}", C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col1, y, f"GPS points", C_DIM)
        pyxel.text(col1 + 80, y, f"{len(self.loot_points)}", C_TEXT)

        # ─── COLUMN 2: THIS SESSION ───
        y = 22
        pyxel.text(col2, y, "THIS SESSION", C_HACK_CYAN)
        pyxel.line(col2, y + 10, col2 + 100, y + 10, 1)
        _ua = int((pyxel.frame_count * 2 + 50) % 150)
        if _ua < 100:
            pyxel.pset(col2 + _ua, y + 10, C_HACK_CYAN)
            if _ua > 0:
                pyxel.pset(col2 + _ua - 1, y + 10, 5)
        y += 14
        n_hs_ses = sum(1 for m in self.markers if m.type == "handshake")
        n_pwn = (sum(1 for d in self.ble_devices if d.hacked)
                 + sum(1 for n in self.wifi_networks if n.hacked))
        pyxel.text(col2, y, f"WiFi", C_DIM)
        pyxel.text(col2 + 70, y, f"{len(self.wifi_networks)}", C_SUCCESS)
        y += ROW_H
        pyxel.text(col2, y, f"BLE", C_DIM)
        pyxel.text(col2 + 70, y, f"{len(self.ble_devices)}", C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col2, y, f"Handshakes", C_DIM)
        pyxel.text(col2 + 70, y, f"{n_hs_ses}", C_ERROR)
        y += ROW_H
        n_pwd_ses = (self.state.submitted_forms
                     + len(self.state.evil_twin_captured_data))
        pyxel.text(col2, y, f"Passwords", C_DIM)
        pyxel.text(col2 + 70, y, f"{n_pwd_ses}", 12)  # blue
        y += ROW_H
        pyxel.text(col2, y, f"Hacked", C_DIM)
        pyxel.text(col2 + 70, y, f"{n_pwn}", C_SUCCESS)
        y += 14
        # XP bar
        lv = self.level_title
        pyxel.text(col2, y, f"LV:{self.level}", C_SUCCESS)
        pyxel.text(col2 + 36, y, lv, C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col2, y, f"XP", C_DIM)
        xw = 60
        nxt = self.xp_for_next_level
        cur = self.xp_in_current_level
        xf = int(xw * cur / nxt) if nxt > 0 else xw
        pyxel.rect(col2 + 24, y, xw, 8, 1)
        pyxel.rect(col2 + 24, y, xf, 8, C_HACK_CYAN)
        # Energy shimmer — mirrors the HUD XP bar so progress reads consistent.
        if xf > 3:
            _period = xf + 45
            _xs = int((pyxel.frame_count * 2) % _period)
            if _xs < xf:
                pyxel.line(col2 + 24 + _xs, y + 1, col2 + 24 + _xs, y + 6, 7)
        pyxel.rectb(col2 + 24, y, xw, 8, C_COAST)
        pyxel.text(col2 + 90, y, f"{self.xp}", C_DIM)
        y += 16
        # Hat profile badge
        hat_name, hat_color = self._get_hat_profile()
        _HAT_DESC = {
            "WHITE": "Ethical recon — scanning & mapping only",
            "BLUE":  "Blue Team — defensive security research",
            "GREY":  "Grey Hat — mixed recon & offensive ops",
            "RED":   "Red Team — active penetration testing",
            "BLACK": "Black Hat — aggressive attack operator",
        }
        pyxel.circ(col2 + 4, y + 4, 4, 7)          # white outline
        pyxel.circ(col2 + 4, y + 4, 3, hat_color)   # hat color
        pyxel.text(col2 + 14, y, f"{hat_name} HAT",
                   hat_color if hat_color != 0 else C_TEXT)
        y += ROW_H
        desc = _HAT_DESC.get(hat_name, "")
        pyxel.text(col2 + 14, y, desc, C_DIM)
        # Badges (3 columns x 2 rows grid) — 5x8 font sizing
        y += 14
        _BADGE_INFO = [
            ("flipper",          "FLIPPER",   C_WARNING),
            ("wardriver",        "WARDRIVER", C_SUCCESS),
            ("meshcore",         "MESHCORE",  2),
            ("handshake_hunter", "HS HUNTER", C_ERROR),
            ("wpasec_uploader",  "WPA-SEC",   12),
            ("evil_twin",        "EVIL TWIN", 8),
            ("skywatch",         "SKYWATCH",  C_HACK_CYAN),
            ("iot_hunter",       "IOT HUNTER", 12),
        ]
        earned = [(b, l, c) for b, l, c in _BADGE_INFO if b in self._badges]
        cols = 3
        cw = 72  # column width
        for i, (badge_id, label, color) in enumerate(earned):
            row = i // cols
            col = i % cols
            bx = col2 + col * cw
            by = y + row * 15
            lw = len(label) * 5 + 6
            pyxel.rectb(bx, by, lw, 12, color)
            pyxel.text(bx + 2, by + 2, label, color)
            # Trophy shine gliding along this badge's frame, staggered by slot.
            _bt = (pyxel.frame_count + i * 22) % 210
            if _bt < lw:
                pyxel.pset(bx + _bt, by, 7)
                pyxel.pset(bx + _bt, by + 11, 7)

        # ─── COLUMN 3: STATUS ───
        y = 22
        pyxel.text(col3, y, "STATUS", C_HACK_CYAN)
        pyxel.line(col3, y + 10, col3 + 80, y + 10, 1)
        _ua = int((pyxel.frame_count * 2 + 100) % 130)
        if _ua < 80:
            pyxel.pset(col3 + _ua, y + 10, C_HACK_CYAN)
            if _ua > 0:
                pyxel.pset(col3 + _ua - 1, y + 10, 5)
        y += 14
        # ESP32 — LED dot + label for at-a-glance status.
        pyxel.text(col3, y, "ESP32", C_DIM)
        _lc = C_SUCCESS if self._esp32 else C_ERROR
        pyxel.circ(col3 + 52, y + 3, 2, _lc)
        pyxel.text(col3 + 60, y, "ONLINE" if self._esp32 else "OFFLINE", _lc)
        y += ROW_H
        # GPS
        pyxel.text(col3, y, "GPS", C_DIM)
        if self.gps_fix:
            _lc, _lt = C_SUCCESS, "FIX"
        elif self.gps.available:
            _lc, _lt = C_WARNING, f"Vis:{self.gps_sats_vis}"
        else:
            _lc, _lt = C_ERROR, "N/A"
        pyxel.circ(col3 + 52, y + 3, 2, _lc)
        pyxel.text(col3 + 60, y, _lt, _lc)
        y += ROW_H
        # AIO modules
        if self._aio_available:
            for _lbl, _on in (("LoRa", self._lora_enabled),
                              ("SDR", self._sdr_enabled),
                              ("USB", self._usb_enabled)):
                _lc = C_SUCCESS if _on else C_ERROR
                pyxel.text(col3, y, _lbl, C_DIM)
                pyxel.circ(col3 + 52, y + 3, 2, _lc)
                pyxel.text(col3 + 60, y, "ON" if _on else "OFF", _lc)
                y += ROW_H
        # Active operations
        pyxel.text(col3, y, "Active", C_DIM)
        ops = []
        if self.wifi_scanning: ops.append("WiFi")
        if self.ble_scanning: ops.append("BT")
        if self.sniffing: ops.append("Sniff")
        if self.capturing_hs: ops.append("HS")
        if self.state.portal_running: ops.append("EP")
        if self.state.evil_twin_running: ops.append("ET")
        if self._gps_wait: ops.append("GPS wait")
        _ac = C_HACK_CYAN if ops else C_DIM
        if ops and pyxel.frame_count % 30 < 18:
            pyxel.circ(col3 + 52, y + 3, 2, C_HACK_CYAN)
        elif not ops:
            pyxel.circb(col3 + 52, y + 3, 2, C_DIM)
        pyxel.text(col3 + 60, y, " ".join(ops) if ops else "idle", _ac)

        # ─── BOTTOM: Last discovered devices ───
        # Divider line
        div_y = 180
        pyxel.line(4, div_y, W - 5, div_y, 1)
        pyxel.text(6, div_y + 4, "RECENT WiFi", C_SUCCESS)
        pyxel.text(W // 2 + 4, div_y + 4, "RECENT BLE", C_HACK_CYAN)
        pyxel.line(4, div_y + 14, W - 5, div_y + 14, 1)
        # Vertical divider
        mid_x = W // 2
        pyxel.line(mid_x, div_y, mid_x, H - 22, 1)

        # WiFi list (last N) — 10px rows for 5x8 font
        LIST_H = 10
        y = div_y + 17
        max_rows = (H - 22 - y) // LIST_H
        recent_wifi = list(reversed(self.wifi_networks[-max_rows:]))
        for n in recent_wifi:
            if y > H - 22:
                break
            rssi_c = n.color
            ssid = n.ssid[:14] or "?"
            pyxel.text(6, y, ssid, C_TEXT)
            pyxel.text(6 + 76, y, f"Ch:{n.channel:>2}", C_DIM)
            pyxel.text(6 + 110, y, f"{n.rssi}dBm", rssi_c)
            if n.hacked:
                pyxel.text(6 + 150, y, "PWN", C_SUCCESS)
            y += LIST_H
        if not recent_wifi:
            _t1 = "- no WiFi this session -"
            _lc = C_COAST if pyxel.frame_count % 44 < 22 else 5
            pyxel.text(4 + (mid_x - 4 - len(_t1) * 5) // 2, div_y + 66, _t1, 1)
            _e = "listening" + "." * ((pyxel.frame_count // 12) % 4)
            pyxel.text(4 + (mid_x - 4 - 12 * 5) // 2, div_y + 78, _e, _lc)

        # BLE list (last N)
        y = div_y + 17
        recent_ble = list(reversed(self.ble_devices[-max_rows:]))
        bx = mid_x + 6
        for d in recent_ble:
            if y > H - 22:
                break
            rssi_c = d.color
            dname = d.name[:12] or "?"
            pyxel.text(bx, y, dname, C_TEXT)
            pyxel.text(bx + 66, y, d.mac[-8:], C_DIM)
            pyxel.text(bx + 110, y, f"{d.rssi}dBm", rssi_c)
            if d.hacked:
                pyxel.text(bx + 150, y, "PWN", C_SUCCESS)
            y += LIST_H
        if not recent_ble:
            _bx0 = mid_x + 6
            _bw = (W - 5) - _bx0
            _t1 = "- no BLE this session -"
            _lc = C_COAST if pyxel.frame_count % 44 < 22 else 5
            pyxel.text(_bx0 + (_bw - len(_t1) * 5) // 2, div_y + 66, _t1, 1)
            _e = "listening" + "." * ((pyxel.frame_count // 12) % 4)
            pyxel.text(_bx0 + (_bw - 12 * 5) // 2, div_y + 78, _e, _lc)

        # GPS + controls bar
        y = H - 18
        pyxel.line(1, y - 3, W - 2, y - 3, 1)
        if self.gps_fix:
            lat_c = "N" if self.player_lat >= 0 else "S"
            lon_c = "E" if self.player_lon >= 0 else "W"
            pyxel.text(6, y,
                       f"GPS {abs(self.player_lat):.5f}{lat_c} "
                       f"{abs(self.player_lon):.5f}{lon_c}", C_SUCCESS)
        else:
            pyxel.text(6, y, "GPS: no fix", C_ERROR)
        # Controls — key-legend styling (keys highlighted, labels dim), matching
        # the menu / HUD. Start nudged left so [S] stop no longer clips the edge.
        def _legend(x0, segs):
            _kx = x0
            for _seg, _sc in segs:
                pyxel.text(_kx, y, _seg, _sc)
                _kx += len(_seg) * 5
        _K, _L, _S = C_HACK_CYAN, C_DIM, C_WARNING
        if not self._loot_search_active and not self._loot_search_results:
            _legend(W - 292, [("[/]", _K), (" search  ", _L),
                              ("[D]", _K), (" passwords  ", _L),
                              ("[`]", _K), (" close  ", _L),
                              ("[ESC]", _K), (" back  ", _L),
                              ("[S]", _S), (" stop", _L)])
        else:
            _legend(W - 164, [("[`]", _K), (" close  ", _L),
                              ("[ESC]", _K), (" back  ", _L),
                              ("[S]", _S), (" stop", _L)])

        # ─── OVERLAYS ───
        if self._loot_pwd_screen:
            self._draw_loot_pwd_viewer()
        elif self._loot_search_active or self._loot_search_results or self._loot_search:
            self._draw_loot_search_overlay()

    def _draw_loot_pwd_viewer(self):
        """Draw overlay showing ALL captured credentials across all sessions."""
        captures = self._loot_pwd_data
        # Solid background
        pyxel.rect(2, 14, W - 4, H - 32, 0)
        # Dialog box
        dw, dh = W - 20, H - 40
        dx = (W - dw) // 2
        dy = 16
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, 12)  # blue border
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 3,
                   f"CAPTURED PASSWORDS — All Sessions", 12)
        pyxel.text(dx + dw - 80, dy + 3,
                   f"Total: {len(captures)}", C_DIM)
        pyxel.line(dx + 2, dy + 11, dx + dw - 3, dy + 11, C_COAST)
        # Column headers
        pyxel.text(dx + 6, dy + 14, "TAG", C_DIM)
        pyxel.text(dx + 30, dy + 14, "DATE", C_DIM)
        pyxel.text(dx + 90, dy + 14, "CREDENTIALS", C_DIM)
        pyxel.line(dx + 2, dy + 22, dx + dw - 3, dy + 22, 1)
        # Data
        if not captures:
            pyxel.text(dx + (dw - 160) // 2, dy + dh // 2,
                       "No credentials captured in any session", C_DIM)
        else:
            max_vis = (dh - 55) // 10
            scroll = min(self._loot_pwd_scroll,
                         max(0, len(captures) - max_vis))
            self._loot_pwd_scroll = scroll
            y = dy + 26
            max_chars = (dw - 16) // 5  # Spleen is 5px/char (was 4 -> overflowed)
            for idx, i in enumerate(range(scroll,
                                          min(len(captures), scroll + max_vis))):
                if idx % 2:                       # zebra striping for readability
                    pyxel.rect(dx + 3, y - 1, dw - 8, 10, 1)
                pyxel.text(dx + 6, y, captures[i][:max_chars], 12)  # blue
                y += 10
            # Scrollbar
            if len(captures) > max_vis:
                bar_total = dh - 55
                bar_h = max(4, bar_total * max_vis // len(captures))
                bar_y = (dy + 26 + (bar_total - bar_h) * scroll
                         // max(1, len(captures) - max_vis))
                pyxel.rect(dx + dw - 4, bar_y, 2, bar_h, 12)
        # Hints
        pyxel.text(dx + 4, dy + dh - 9,
                   "[D/ESC] close  [UP/DOWN] scroll", C_DIM)

    def _draw_loot_search_overlay(self):
        """Draw search field + results over the loot screen."""
        # Solid black background over loot content
        pyxel.rect(2, 14, W - 4, H - 32, 0)

        # Search box
        sx, sy, sw = 20, 20, W - 40
        pyxel.rect(sx, sy, sw, 16, 0)
        pyxel.rectb(sx, sy, sw, 16,
                    C_HACK_CYAN if self._loot_search_active else C_COAST)
        cursor = "_" if self._loot_search_active and pyxel.frame_count % 30 < 20 else ""
        pyxel.text(sx + 4, sy + 5, f"SEARCH: {self._loot_search}{cursor}",
                   C_TEXT if self._loot_search else C_DIM)
        if not self._loot_search and self._loot_search_active:
            pyxel.text(sx + 52, sy + 5, "type SSID, name or MAC...", C_DIM)

        # Results area
        ry = sy + 20
        results = self._loot_search_results

        if not results and self._loot_search and not self._loot_search_active:
            pyxel.text(sx + 4, ry + 10,
                       f'No matches for "{self._loot_search}"', C_WARNING)
            pyxel.text(sx + 4, ry + 22, "[ESC] clear search", C_DIM)
            return

        if not results:
            return

        # Header
        pyxel.text(sx + 4, ry,
                   f"Found: {len(results)} devices", C_HACK_CYAN)
        ry += 10
        # Column headers
        pyxel.text(sx + 4, ry, "Type", C_DIM)
        pyxel.text(sx + 30, ry, "SSID / Name", C_DIM)
        pyxel.text(sx + 170, ry, "BSSID / MAC", C_DIM)
        pyxel.text(sx + 280, ry, "Ch", C_DIM)
        pyxel.text(sx + 300, ry, "RSSI", C_DIM)
        pyxel.text(sx + 340, ry, "Auth", C_DIM)
        ry += 2
        pyxel.line(sx + 2, ry + 6, sx + sw - 4, ry + 6, 1)
        ry += 9

        # Rows
        max_rows = (H - 40 - ry) // 9
        scroll = min(self._loot_search_scroll,
                     max(0, len(results) - max_rows))
        self._loot_search_scroll = scroll

        for idx, i in enumerate(range(scroll,
                                      min(scroll + max_rows, len(results)))):
            if idx % 2:                       # zebra striping for readability
                pyxel.rect(sx + 2, ry - 1, sw - 4, 9, 1)
            pt = results[i]
            ptype = pt.get("type", "?")
            label = (pt.get("label") or "<hidden>")[:18]
            bssid = pt.get("bssid", "")
            ch = pt.get("channel", "")
            rssi = pt.get("rssi", "")
            auth = pt.get("auth", "")[:10]
            ssid_str = pt.get("label", "")

            # Type color
            if ptype == "wifi":
                tc = C_SUCCESS
                tl = "WiFi"
            elif ptype == "bt":
                tc = C_HACK_CYAN
                tl = "BLE"
            else:
                tc = C_DIM
                tl = ptype[:4]

            # Cracked?
            cracked = ssid_str and ssid_str in self._cracked_ssids
            if cracked:
                tc = C_SUCCESS
                label = f"* {label}"

            pyxel.text(sx + 4, ry, tl, tc)
            pyxel.text(sx + 30, ry, label, C_TEXT if not cracked else C_SUCCESS)
            pyxel.text(sx + 170, ry, bssid[-17:], C_DIM)
            pyxel.text(sx + 280, ry, str(ch)[:3], C_DIM)
            # RSSI color
            try:
                rv = int(rssi)
                rc = C_SUCCESS if rv > -60 else C_WARNING if rv > -75 else C_ERROR
            except (ValueError, TypeError):
                rc = C_DIM
            pyxel.text(sx + 300, ry, str(rssi), rc)
            pyxel.text(sx + 340, ry, auth, C_DIM)
            ry += 9

        # Scroll hint
        if len(results) > max_rows:
            pyxel.text(sx + sw - 100, sy + 20,
                       f"[UP/DN] {scroll+1}-{min(scroll+max_rows, len(results))}"
                       f"/{len(results)}", C_DIM)
