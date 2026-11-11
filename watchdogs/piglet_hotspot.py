"""Owned STA+AP collection lifecycle. Network changes happen only on START."""
from collections import Counter
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid


class HotspotError(RuntimeError):
    pass


def _atomic_json(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise HotspotError("Collection settings must not be links")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def pairing_settings(directory):
    """Persistent locally generated credentials; never logged or sent to Git."""
    path = Path(directory) / "pairing.json"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise HotspotError("Invalid Collection pairing file")
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        value = dict(ssid="WDG-Piglet-" + secrets.token_hex(3),
                     password=secrets.token_urlsafe(18), token=secrets.token_hex(32))
        _atomic_json(path, value)
    if (not isinstance(value, dict) or set(value) != {"ssid", "password", "token"}
            or not isinstance(value["ssid"], str) or not 1 <= len(value["ssid"].encode()) <= 32
            or not isinstance(value["password"], str) or not 8 <= len(value["password"]) <= 63
            or not isinstance(value["token"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["token"])):
        raise HotspotError("Invalid Collection pairing settings; original file retained")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise HotspotError("Collection pairing file must be private (mode 600)")
    return value


def supports_combination(info, existing_types):
    """Check a single advertised combination, including already present P2P."""
    if "valid interface combinations:" not in info:
        return False
    section = info.split("valid interface combinations:", 1)[1]
    for block in re.split(r"\n\s*\*\s*", section):
        groups = re.findall(r"#\{\s*([^}]+)\}\s*<=\s*(\d+)", block)
        total = re.search(r"total\s*<=\s*(\d+)", block)
        channels = re.search(r"#channels\s*<=\s*(\d+)", block)
        if not groups or not total or not channels or int(channels[1]) < 1:
            continue
        counts = Counter(existing_types)
        counts["AP"] += 1
        covered = set()
        valid = sum(counts.values()) <= int(total[1])
        for names, maximum in groups:
            names = {name.strip() for name in names.split(",")}
            covered |= names
            valid &= sum(counts.get(name, 0) for name in names) <= int(maximum)
        if valid and set(counts) <= covered:
            return True
    return False


class ConcurrentHotspot:
    """Create/delete only an owned virtual AP; never reconfigure the STA."""
    def __init__(self, state_dir, *, sta="wlan0", interface="wdgpc0", address="10.72.0.1",
                 run=None, sleep=time.sleep):
        if not all(re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", name) for name in (sta, interface)) or sta == interface:
            raise ValueError("Invalid separate STA/AP interfaces")
        self.directory = Path(state_dir)
        self.lease_path = self.directory / "hotspot-lease.json"
        self.sta, self.interface = sta, interface
        self.address = str(ipaddress.IPv4Address(address))
        self.network = ipaddress.ip_network(self.address + "/24", strict=False)
        if not ipaddress.ip_address(self.address).is_private:
            raise ValueError("AP address must be private")
        self._runner = run or subprocess.run
        self._sleep = sleep
        self._lease = None
        self._before = None
        self.active = False

    def _command(self, tool, *args, required=True, timeout=15):
        executable = shutil.which(tool) or ("/usr/sbin/" + tool if tool == "iw" else tool)
        try:
            result = self._runner([executable, *args], capture_output=True, text=True,
                                  timeout=timeout, env=dict(os.environ, LC_ALL="C"))
        except (OSError, subprocess.SubprocessError):
            raise HotspotError("Collection network command failed or timed out; check local permissions/tools") from None
        if required and result.returncode:
            # Never include command lines (NM credentials) or raw diagnostics.
            raise HotspotError("Collection network operation failed; check STA+AP support and network permissions")
        return result.stdout.strip() if result.returncode == 0 else None

    def _snapshot(self):
        info = self._command("iw", "dev", self.sta, "info")
        frequency = re.search(r"channel\s+(\d+)\s+\((\d+) MHz\)", info)
        phy = re.search(r"wiphy\s+(\d+)", info)
        if not frequency or not phy or not re.search(r"type managed\b", info):
            raise HotspotError("Collection requires an already connected managed STA")
        connection = self._command("nmcli", "-g", "GENERAL.CON-UUID", "device", "show", self.sta)
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", connection):
            raise HotspotError("The router STA has no active NetworkManager connection")
        link = self._command("iw", "dev", self.sta, "link")
        if "Connected to " not in link:
            raise HotspotError("The router STA is disconnected; no network takeover was attempted")
        addresses = json.loads(self._command("ip", "-j", "address", "show", "dev", self.sta))
        ips = sorted(item["local"] + "/" + str(item["prefixlen"])
                     for row in addresses for item in row.get("addr_info", []) if item.get("family") == "inet")
        if not ips:
            raise HotspotError("The router STA has no IPv4 address")
        routes = json.loads(self._command("ip", "-j", "route", "show", "default"))
        defaults = sorted(json.dumps(row, sort_keys=True) for row in routes if row.get("dev") == self.sta)
        return dict(connection=connection, ips=ips, defaults=defaults,
                    channel=int(frequency[1]), frequency=int(frequency[2]), phy="phy" + phy[1])

    def preflight(self):
        before = self._snapshot()
        info = self._command("iw", "phy", before["phy"], "info")
        devices = self._command("iw", "dev")
        phy_block = devices.split("phy#" + before["phy"][3:], 1)[-1].split("\nphy#", 1)[0]
        existing = re.findall(r"^\s*type\s+(.+?)\s*$", phy_block, re.M)
        if not supports_combination(info, existing):
            raise HotspotError("Driver does not advertise the current STA/P2P plus AP combination")
        frequency_line = next((line for line in info.splitlines()
                               if re.search(r"\b" + str(before["frequency"]) + r"(?:\.0+)? MHz\b", line)), "")
        if (not frequency_line or any(flag in frequency_line.lower() for flag in ("disabled", "no ir", "radar"))
                or not 2400 <= before["frequency"] < 5900):
            raise HotspotError("The STA channel cannot be used for the local AP; no channel change was attempted")
        if self._command("ip", "-j", "link", "show", "dev", self.interface, required=False) is not None:
            raise HotspotError("Collection AP interface name is already in use")
        addresses = json.loads(self._command("ip", "-j", "address", "show"))
        for row in addresses:
            for item in row.get("addr_info", []):
                if item.get("family") == "inet" and self.network.overlaps(ipaddress.ip_network(
                        item["local"] + "/" + str(item["prefixlen"]), strict=False)):
                    raise HotspotError("Collection subnet overlaps an existing interface")
        return before

    def _owned_interface(self, lease):
        raw = self._command("ip", "-j", "link", "show", "dev", self.interface, required=False)
        if raw is None:
            return False
        rows = json.loads(raw)
        if not rows or rows[0].get("ifalias") != lease["alias"]:
            raise HotspotError("AP ownership changed; foreign interface was left untouched")
        return True

    def stop(self):
        lease = self._lease
        if lease is None and self.lease_path.exists():
            if self.lease_path.is_symlink():
                raise HotspotError("Invalid AP ownership file")
            lease = json.loads(self.lease_path.read_text(encoding="utf-8"))
        if lease is None:
            return True
        if (lease.get("interface") != self.interface or not re.fullmatch(r"[0-9a-f-]{36}", lease.get("uuid", ""))
                or lease.get("alias") != "wdg-piglet:" + lease["uuid"]):
            raise HotspotError("Invalid AP ownership record; resources retained for inspection")
        owned = self._owned_interface(lease)
        binding = self._command("nmcli", "-g", "connection.interface-name", "connection", "show", "uuid", lease["uuid"], required=False)
        if binding is not None:
            if binding != self.interface:
                raise HotspotError("AP profile ownership changed; profile was left untouched")
            # UUID is unique to this operation; never down the router profile.
            self._command("nmcli", "--wait", "5", "connection", "down", "uuid", lease["uuid"], required=False)
            self._command("nmcli", "connection", "delete", "uuid", lease["uuid"])
        if owned:
            self._command("iw", "dev", self.interface, "del")
        self.active = False
        self._lease = None
        self.lease_path.unlink(missing_ok=True)
        return True

    def start(self, credentials, cancel=None):
        if self.active:
            raise HotspotError("Collection AP is already active")
        # Recover only a previous explicitly owned resource, never arbitrary APs.
        if self.lease_path.exists():
            self.stop()
        before = self.preflight()
        identifier = str(uuid.uuid4())
        lease = dict(uuid=identifier, interface=self.interface, alias="wdg-piglet:" + identifier)
        _atomic_json(self.lease_path, lease)
        self._lease, self._before = lease, before
        created = False
        try:
            if cancel and cancel.is_set():
                raise HotspotError("Collection start cancelled")
            self._command("iw", "dev", self.sta, "interface", "add", self.interface, "type", "__ap")
            created = True
            self._command("ip", "link", "set", "dev", self.interface, "alias", lease["alias"])
            self._command("nmcli", "connection", "add", "type", "wifi", "ifname", self.interface,
                "con-name", "WDG-Piglet-" + identifier[:8], "connection.uuid", identifier,
                "connection.autoconnect", "no", "ssid", credentials["ssid"], "wifi.mode", "ap",
                "wifi.band", "bg" if before["frequency"] < 3000 else "a", "wifi.channel", str(before["channel"]),
                "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn", "wifi-sec.psk", credentials["password"],
                "ipv4.method", "shared", "ipv4.addresses", self.address + "/24", "ipv4.never-default", "yes",
                "ipv6.method", "disabled")
            if cancel and cancel.is_set():
                raise HotspotError("Collection start cancelled")
            self._command("nmcli", "--wait", "15", "connection", "up", "uuid", identifier, "ifname", self.interface, timeout=20)
            if self._snapshot() != before:
                raise HotspotError("STA changed during AP start; Collection stopped without reconfiguring STA")
            raw = json.loads(self._command("ip", "-j", "address", "show", "dev", self.interface))
            if not any(item.get("local") == self.address for row in raw for item in row.get("addr_info", [])):
                raise HotspotError("The owned AP did not acquire its local address")
            self.active = True
            return dict(address=self.address, channel=before["channel"], interface=self.interface)
        except Exception:
            # If alias assignment failed, the just-created interface is still ours
            # in this synchronous operation. Delete it before ownership recovery.
            if created:
                raw = self._command("ip", "-j", "link", "show", "dev", self.interface, required=False)
                if raw and not json.loads(raw)[0].get("ifalias"):
                    self._command("iw", "dev", self.interface, "del")
            self.stop()
            raise

    def healthy(self):
        return self.active and self._owned_interface(self._lease) and self._snapshot() == self._before


class PigletCollectionController:
    """Background network lifecycle; durable import is pumped by the game thread."""
    def __init__(self, loot, *, state_dir=None, backend=None, service_factory=None):
        from .piglet_collection import PigletCollectionStore
        from .piglet_http import PigletHTTPService
        self._loot, self._state_dir = loot, state_dir
        self.store = PigletCollectionStore(loot, state_dir=state_dir)
        try:
            self.settings = pairing_settings(self.store.root)
            self.backend = backend or ConcurrentHotspot(self.store.root)
        except Exception:
            self.store.close()
            raise
        self._service_factory = service_factory or PigletHTTPService
        self._service = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread = None
        self._cancel_requested = threading.Event()
        self._store_closed = False
        self._store_closing = False
        self._last_progress = {}
        self._state = "stopped"
        self._message = "Ready for local collection"
        self._network = {}
        self._released = True

    def _set(self, state, message):
        with self._lock:
            self._state, self._message = state, message

    def start(self):
        if (not self._released or self._store_closing and not self._store_closed
                or self._thread is not None and self._thread.is_alive()):
            return False
        if self._store_closed:
            from .piglet_collection import PigletCollectionStore
            try:
                self.store = PigletCollectionStore(self._loot, state_dir=self._state_dir)
            except Exception:
                self._set("error", "Collection could not reopen; saved CSV and XP retained")
                return False
            self._store_closed = False
            self._store_closing = False
            self._last_progress = {}
        self._cancel_requested.clear()
        self._stop.clear()
        self._finished.clear()
        self._set("starting", "Checking concurrent STA + AP")
        self._released = False
        self._thread = threading.Thread(target=self._run, name="piglet-collection-network", daemon=True)
        self._thread.start()
        return True

    def _on_finish(self, payload):
        if payload.get("shutdown_allowed"):
            self._finished.set()

    def _run(self):
        error = None
        completed = False
        try:
            self._network = self.backend.start(self.settings, self._stop)
            if self._stop.is_set():
                return
            self._service = self._service_factory(self.store, self.settings["token"], self._network["address"],
                                                  on_finish=self._on_finish)
            self._service.start()
            self._set("active", "Local hotspot and receiver active")
            while not self._stop.wait(.5):
                if self._finished.is_set():
                    snapshot = self.store.status_overview()
                    if not snapshot.get("busy") and not snapshot.get("active_devices"):
                        completed = True
                        break
                    # A busy import lock is not evidence of a new peer. Retain
                    # this finish notification until a real snapshot is available.
                    if not snapshot.get("busy"):
                        self._finished.clear()
                if not self.backend.healthy():
                    raise HotspotError("STA/channel changed; Collection stopped and unfinished data retained")
        except Exception as exc:
            error = str(exc) if isinstance(exc, HotspotError) else "Collection could not start; check local network permissions and tools"
        finally:
            error = self._release_owned() or error
            cancelled = self._cancel_requested.is_set()
            self._set("error" if error else "cancelled" if cancelled else "complete" if completed else "stopped",
                      error or ("Collection cancelled; completed CSV and XP kept" if cancelled else
                                "Transfer complete; hotspot stopped" if completed else "Collection stopped; saved data retained"))

    def _release_owned(self):
        self._set("stopping", "Releasing owned hotspot and receiver")
        released = True
        try:
            if self._service is not None:
                released = self._service.stop() is not False
            if released:
                released = self.backend.stop() is not False
            if released and self._cancel_requested.is_set():
                self._store_closing = True
                released = self.store.close() is not False
                self._store_closed = released
        except Exception:
            released = False
        self._released = released
        self._service = None if released else self._service
        return None if released else "Collection cleanup incomplete; owned resources retained for inspection"

    @property
    def cancel_requested(self):
        return self._cancel_requested.is_set()

    def cancel(self):
        """Stop receiving and processing; completed data/rewards remain durable."""
        self._cancel_requested.set()
        self._stop.set()
        if (self._store_closed and self._released
                and (self._thread is None or not self._thread.is_alive())):
            self._set("cancelled", "Collection cancelled; completed CSV and XP kept")
            return True
        self.store.cancel_processing()
        if self._thread is None or not self._thread.is_alive():
            self._released = False
            self._thread = threading.Thread(target=self._cancel_idle, name="piglet-collection-cancel", daemon=True)
            self._thread.start()
        return True

    def _cancel_idle(self):
        error = self._release_owned()
        self._set("error" if error else "cancelled", error or "Collection cancelled; completed CSV and XP kept")

    def snapshot(self):
        with self._lock:
            result = dict(state=self._state, message=self._message, network=dict(self._network))
        if not self._store_closed and not self._store_closing:
            try:
                self._last_progress = self.store.status_overview()
            except Exception:
                if not self._cancel_requested.is_set():
                    raise
        connected = []
        service = self._service
        if result["state"] == "active" and not self._store_closed and not self._store_closing and service is not None:
            activity = getattr(service,"connected_devices",None)
            if callable(activity):
                try:
                    connected = activity()
                except Exception:
                    pass
        result["progress"] = dict(self._last_progress,connected_devices=connected)
        return result

    def stop(self, *, wait=True, timeout=8):
        self._stop.set()
        if not wait:
            return True
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return False
        return self._released

    def close(self):
        if not self.stop():
            return False
        if self._store_closed:
            return True
        self._store_closing = True
        try:
            self._store_closed = self.store.close() is not False
        except Exception:
            self._store_closed = False
        if not self._store_closed:
            self._set("error", "Collection worker cleanup incomplete; saved CSV and XP retained")
        return self._store_closed
