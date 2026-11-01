"""Single-owner SX1262 Meshtastic public RX/TX for the WDG frontend.

Starting/listening never transmits. Only explicit send_message() queues TX.
TX_DONE is reported as local transmission completion, never as delivery.
"""

import importlib
from importlib.metadata import version
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import secrets
import struct
import subprocess
import sys
import threading
import time

from meshtastic_rx_probe import SpiAudit, configure_radio, get_profile as get_channel, poll_frame
from .radio_profiles import get_meshtastic_rf as get_profile
from .meshtastic_protocol import decode_packet, encode_text


_SPI_OWNER_LOCK = threading.Lock()
TX_POWER_DBM = 14  # Same conservative output as the existing RNode integration.
TX_DUTY_FRACTION = 0.10  # EU_868 869.4–869.65 MHz; Meshtastic RadioInterface.cpp.


def _home():
    if getattr(os, "geteuid", lambda: 1)() == 0 and os.environ.get("SUDO_USER"):
        import pwd
        return Path(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
    return Path.home()


class MeshtasticManager:
    def __init__(self, queue=None, home=None):
        self.queue = queue or Queue()
        self.running = False
        self.ready = False
        self.mode = "meshtastic"
        self.last_error = ""
        self.packets_received = 0
        self.duplicates_received = 0
        self.profile_id = "sfnarrow"
        self.channel_profile_id = "sfnarrow"
        self._on_node = self._on_text = self._on_message = None
        self._on_status = self._on_tx = None
        self._on_packet = None  # optional validated metadata callback, no raw ciphertext
        self._on_radio_event = None  # optional private RF audit, including raw frames
        self._thread = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._guard = threading.RLock()
        self._quiescing = False
        self._tx_active = False
        self._pending_tx = None  # Includes a frame dequeued while waiting for duty cycle.
        self._tx_queue = Queue(maxsize=16)
        self._known_nodes = {}
        self._seen = {}
        self._state_path = Path(home) / ".watchdogs_meshtastic.json" if home is not None else _home() / ".watchdogs_meshtastic.json"
        self._state = {}
        self._identity_error = ""
        try:
            if self._state_path.exists():
                self._state = json.loads(self._state_path.read_text())
                if not isinstance(self._state, dict):
                    raise ValueError("identity configuration must be an object")
                number = self._state["node_num"]
                if not isinstance(number, int) or not 3 < number < 0xFFFFFFFF:
                    raise ValueError("invalid stored node number")
                get_profile(self._state.get("profile_id", "sfnarrow"))
                previous_profile = self._state.get("profile_id", "sfnarrow")
                self._state.setdefault("channel_id", previous_profile if previous_profile in ("sfnarrow", "longfast") else "sfnarrow")
                get_channel(self._state["channel_id"])
                packet_id = self._state.get("packet_id", 1)
                duty_time = self._state.get("next_tx_at", 0.0)
                if not isinstance(packet_id, int) or not 0 < packet_id <= 0xFFFFFFFF:
                    raise ValueError("invalid stored packet number")
                if not isinstance(duty_time, (int, float)) or not math.isfinite(duty_time) or duty_time < 0:
                    raise ValueError("invalid stored duty-cycle time")
            else:
                self._state = {"version": 1, "node_num": secrets.randbelow(0xFFFFFFFB) + 4,
                               "packet_id": secrets.randbelow(0xFFFFFFFE) + 1,
                               "profile_id": "sfnarrow", "channel_id": "sfnarrow", "next_tx_at": 0.0}
                self._save_state()
            self.profile_id = self._state.get("profile_id", "sfnarrow")
            self.channel_profile_id = self._state["channel_id"]
            seen = self._state.get("seen_rx", {})
            if not isinstance(seen, dict) or any(not isinstance(k, str) or not isinstance(v, (int, float))
                                                 or not math.isfinite(v) for k, v in seen.items()):
                raise ValueError("invalid packet journal")
            self._seen = dict(sorted(seen.items(), key=lambda item: item[1])[-4096:])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Never replace a damaged identity/journal or risk reusing a nonce.
            self._state = {}
            self._identity_error = f"Meshtastic identity unavailable: {exc}"
            self.last_error = self._identity_error

    @property
    def node_id(self):
        number = self._state.get("node_num")
        return f"!{number:08x}" if isinstance(number, int) else ""

    @property
    def node_name(self):
        return str(self._state.get("node_name") or f"WDG {self.node_id[-4:]}")

    @property
    def channel_name(self):
        return get_channel(self.channel_profile_id)["channel_name"]

    def set_node_name(self, name):
        """Persist the local human label; never transmits a NodeInfo advert."""
        try:
            if not isinstance(name, str):
                raise ValueError("Meshtastic nickname must be text")
            name = name.strip()
            if not name:
                raise ValueError("Meshtastic nickname is empty")
            if any(ord(character) < 32 or ord(character) == 127 for character in name):
                raise ValueError("Meshtastic nickname contains control characters")
            if len(name.encode("utf-8")) > 40:
                raise ValueError("Meshtastic nickname exceeds 40 UTF-8 bytes")
            with self._guard:
                if self._identity_error:
                    raise ValueError(self._identity_error)
                previous = dict(self._state)
                self._state["node_name"] = name
                try:
                    self._save_state()
                except OSError:
                    self._state = previous
                    raise
            self.last_error = ""
            return True
        except (ValueError, OSError) as exc:
            self.last_error = str(exc)
            return False

    def set_known_nodes(self, nodes):
        values = nodes.values() if isinstance(nodes, dict) else nodes
        with self._guard:
            self._known_nodes = {n["id"]: dict(n) for n in values if isinstance(n, dict) and n.get("id")}
            for data in self._known_nodes.values():
                data.setdefault("node_type", data.get("type", "Client"))
                if data.get("has_position") is False:
                    data["lat"] = data["lon"] = None
                if data.get("pubkey") and not data.get("public_key"):
                    try:
                        key = bytes.fromhex(data["pubkey"])
                        if len(key) == 32:
                            data["public_key"] = key
                    except (ValueError, TypeError):
                        pass

    def set_profile(self, profile_id):
        """Legacy stopped-radio selection, including its old public-channel choice.

        AUTO/manual RF changes use retune_profile(), which preserves channels.
        """
        try:
            get_profile(profile_id)
            with self._guard:
                if self.running:
                    self.last_error = "stop Meshtastic before changing its profile"
                    return False
                if self._identity_error:
                    self.last_error = self._identity_error
                    return False
                previous = dict(self._state)
                self._state["profile_id"] = profile_id
                if profile_id in ("sfnarrow", "longfast"):
                    self._state["channel_id"] = profile_id
                try:
                    self._save_state()
                except OSError:
                    self._state = previous
                    raise
                self.profile_id = profile_id
                self.channel_profile_id = self._state["channel_id"]
                return True
        except (KeyError, OSError, ValueError) as exc:
            self.last_error = str(exc)
            return False

    def retune_profile(self, profile_id):
        """Retune only RF, preserving channel, nonce, identity and RX journal.

        Pending duty-cycle waits are busy too. Closing admission and dequeue
        under the same guard prevents a new TX slipping into a stop/restart.
        The guard is released before stop/join so worker cleanup can finish.
        """
        try:
            get_profile(profile_id)
        except (KeyError, ValueError) as exc:
            self.last_error = str(exc)
            return False
        with self._guard:
            if self._identity_error:
                self.last_error = self._identity_error
                return False
            if (self._quiescing or self._tx_active or self._pending_tx is not None
                    or not self._tx_queue.empty()):
                self.last_error = "Meshtastic TX pending or active; profile change deferred"
                return False
            if self.running and not self.ready:
                self.last_error = "Meshtastic radio is not ready for profile change"
                return False
            if self.profile_id == profile_id:
                self.last_error = ""
                return True
            self._quiescing = True
            was_running, previous = self.running, self.profile_id
        try:
            if was_running and not self.stop():
                return False
            with self._guard:
                self._state["profile_id"] = profile_id
                try:
                    self._save_state()
                except OSError:
                    self._state["profile_id"] = previous
                    raise
                self.profile_id = profile_id
            if not was_running or self.start(profile_id):
                self.last_error = ""
                return True
            error = self.last_error or "Meshtastic profile restart failed"
            if self.stop():
                with self._guard:
                    self._state["profile_id"] = previous
                    self._save_state()
                    self.profile_id = previous
                self.start(previous)
            self.last_error = error
            return False
        except OSError as exc:
            self.last_error = str(exc)
            return False
        finally:
            with self._guard:
                self._quiescing = False

    def _save_state(self):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(self._state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._state_path)
        # A root launcher must leave the user's own configuration user-owned.
        if getattr(os, "geteuid", lambda: 1)() == 0 and os.environ.get("SUDO_USER") and self._state_path.parent == _home():
            import pwd
            user = pwd.getpwnam(os.environ["SUDO_USER"])
            os.chown(self._state_path, user.pw_uid, user.pw_gid)

    def _emit(self, text, attr="default"):
        self.queue.put((text, attr))

    def _status(self, state, error=""):
        if error:
            self.last_error = str(error)
            self._emit(f"[MT] {error}", "error")
        if self._on_status:
            self._on_status(state, str(error))

    def _radio_event(self, event):
        if self._on_radio_event:
            self._on_radio_event(dict(event, profile_id=self.profile_id,
                channel_id=self.channel_profile_id, channel_name=self.channel_name,
                logged_at_utc=datetime.now(timezone.utc).isoformat()))

    def start(self, profile_id="sfnarrow"):
        try:
            get_profile(profile_id)  # fail before touching the radio
        except KeyError:
            self.last_error = "unknown Meshtastic profile"
            return False
        if self._identity_error:
            self.last_error = self._identity_error
            return False
        with self._guard:
            if self.running:
                if self.ready and self.profile_id == profile_id:
                    return True
                self.last_error = "Meshtastic already owns or is starting the radio"
                return False
            self.profile_id = profile_id
            if not self._identity_error:
                self._state["profile_id"] = profile_id
                try:
                    self._save_state()
                except OSError as exc:
                    self.last_error = str(exc)
                    return False
            self._stop.clear()
            self._started.clear()
            self.last_error = ""
            self.running = True
            self.ready = False
            self._thread = threading.Thread(target=self._run, daemon=True, name="WDG-Meshtastic")
            self._status("starting")
            self._thread.start()
        self._started.wait(10)
        if not self.ready:
            if not self.last_error:
                self.last_error = "Meshtastic radio startup timed out"
            self._stop.set()
            return False
        return True

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=12)
        if thread and thread.is_alive():
            self.last_error = "Meshtastic radio thread has not released SPI yet"
            return False
        self._thread = None
        return not self.running

    def send_message(self, text):
        with self._guard:
            if self._quiescing:
                self.last_error = "Meshtastic profile is changing; TX deferred"
                return None
            if not self.ready or not self.running or self._stop.is_set():
                self.last_error = "Meshtastic radio is not ready"
                return None
            if self._identity_error:
                self.last_error = self._identity_error
                return None
            try:
                packet_id = self._state.get("packet_id", 1) + 1
                if packet_id > 0xFFFFFFFF:
                    raise ValueError("local packet numbers exhausted; identity must be renewed explicitly")
                frame = encode_text(text, self._state["node_num"], packet_id, self.channel_profile_id)
                self._state["packet_id"] = packet_id
                self._save_state()  # reserve nonce before the frame can be transmitted
                token = f"{self.node_id}:{packet_id:08x}"
                self._tx_queue.put_nowait((token, frame))
            except (ValueError, OSError, Full) as exc:
                self.last_error = str(exc) or "Meshtastic transmit queue is full"
                return None
            return token

    def _radio_guard(self):
        if sys.platform != "linux":
            raise RuntimeError("Meshtastic SX1262 requires Linux")
        if version("LoRaRF") != "1.4.0":
            raise RuntimeError("Meshtastic adapter requires LoRaRF 1.4.0")
        for process in Path("/proc").glob("[0-9]*"):
            try:
                args = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            if any(Path(arg).name == "meshtasticd" for arg in args):
                raise RuntimeError("meshtasticd owns the shared radio GPIO")
        owner = subprocess.run(["fuser", "/dev/spidev1.0"], capture_output=True, timeout=5)
        if owner.returncode != 1 or owner.stdout.strip() or owner.stderr.strip():
            raise RuntimeError("SX1262 SPI is already in use or cannot be checked")
        power = subprocess.run(["pinctrl", "get", "16"], capture_output=True, text=True, timeout=5)
        if power.returncode or "| hi" not in power.stdout:
            raise RuntimeError("AIO LoRa power is off")

    def _handle_frame(self, frame, metadata):
        if (metadata.get("crc_status") != "pass" or metadata.get("crc_present") is not True
                or metadata.get("header_error") or metadata.get("irq", 0) & 0x260):
            return False
        try:
            packet = decode_packet(frame, self.channel_profile_id)
        except (ValueError, UnicodeDecodeError):
            return False
        now = time.time()
        wire_key = packet["key"]
        key = self.channel_profile_id + ":" + hashlib.sha256(
            struct.pack("<IIIB", *wire_key[:4]) + wire_key[4]).hexdigest()
        self.packets_received += 1
        if key in self._seen:
            self.duplicates_received += 1
            return False
        with self._guard:
            self._seen[key] = now
            if len(self._seen) > 4096:
                self._seen.pop(min(self._seen, key=self._seen.get))
            self._state["seen_rx"] = self._seen
            self._save_state()  # at-most-once callback delivery across a normal restart
        node_id = packet["source_node_id"]
        if node_id == self.node_id:
            return True  # our own forwarded public message is not a new discovery/chat RX
        known = self._known_nodes.setdefault(node_id, {})
        for field in ("name", "lat", "lon", "node_type", "public_key", "hw_model"):
            value = packet[field]
            if value is not None and (field != "name" or value):
                known[field] = value
        name = known.get("name", "")
        rssi, snr = metadata.get("rssi_dbm", 0), metadata.get("snr_db", 0)
        if self._on_node:
            self._on_node(node_id, known.get("node_type", "Client"), name,
                          known.get("lat"), known.get("lon"), rssi, snr, known.get("public_key"))
        if packet["text"] is not None:
            hops = max(0, packet["hop_start"] - packet["hop_limit"])
            if self._on_text:
                self._on_text(node_id, name or node_id, packet["text"], rssi, hops)
            elif self._on_message:
                self._on_message(self.channel_name, f"{name or node_id}: {packet['text']}", rssi, hops)
        if self._on_packet:
            self._on_packet({k: v for k, v in packet.items() if k not in ("key", "payload", "public_key")})
        return True

    def _transmit_frame(self, radio, frame):
        with self._guard:
            if self._quiescing:
                return False, "Meshtastic profile is changing; TX deferred"
            self._tx_active = True
        try:
            return self._transmit_frame_owned(radio, frame)
        finally:
            with self._guard:
                self._tx_active = False

    def _transmit_frame_owned(self, radio, frame):
        """Low-level TX only; no GPIO IRQ callbacks or LoRaRF wrapper corruption."""
        profile = get_profile(self.profile_id)
        sf, bw, cr = profile["sf"], profile["bandwidth_hz"], profile["coding_rate_denominator"]
        de = int(profile["ldro"])
        payload = 8 + max(math.ceil((8 * len(frame) - 4 * sf + 28 + 16) /
                                   (4 * (sf - 2 * de))) * cr, 0)
        air = (profile["preamble_symbols"] + 4.25 + payload) * 2 ** sf / bw
        started = time.time()
        # Reserve duty-cycle off-time before TX, so restart does not reset it.
        with self._guard:
            self._state["next_tx_at"] = started + air / TX_DUTY_FRACTION
            self._save_state()
        success, error = False, "transmit timed out"
        try:
            radio.setStandby(radio.STANDBY_RC)
            radio.setTxPower(TX_POWER_DBM, radio.TX_POWER_SX1262)
            radio.setBufferBaseAddress(0, 0)
            radio.writeBuffer(0, tuple(frame), len(frame))
            radio.setLoRaPacket(radio.HEADER_EXPLICIT, profile["preamble_symbols"], len(frame), True, False)
            value = radio.readRegister(0x0736, 1)[0]
            radio.writeRegister(0x0736, (value | 4,), 1)
            radio.setDioIrqParams(radio.IRQ_TX_DONE | radio.IRQ_TIMEOUT, 0, 0, 0)
            radio.clearIrqStatus(radio.IRQ_ALL)
            timeout = max(2.0, air + 1.0)
            radio.setTx(round(timeout * 64_000))
            deadline = time.monotonic() + timeout
            while not self._stop.is_set() and time.monotonic() < deadline:
                irq = radio.getIrqStatus()
                if irq & radio.IRQ_TIMEOUT:
                    break
                if irq & radio.IRQ_TX_DONE:
                    success, error = True, ""
                    break
                self._stop.wait(0.01)
            if self._stop.is_set() and not success:
                error = "radio stopped before TX completion"
        except Exception as exc:
            error = str(exc)
        finally:
            radio.setStandby(radio.STANDBY_RC)
            configure_radio(radio, profile)  # verifies RX, sync and IQ on every path
            with self._guard:
                spent = max(air, time.time() - started)
                self._state["next_tx_at"] = max(self._state.get("next_tx_at", 0),
                                                time.time() + spent * (1 / TX_DUTY_FRACTION - 1))
                self._save_state()
        return success, error

    def _run(self):
        module = original_spi = radio = None
        lock_fd = None
        owns_lock = initialized = begin_attempted = False
        try:
            if not _SPI_OWNER_LOCK.acquire(blocking=False):
                raise RuntimeError("another Meshtastic manager owns SPI")
            owns_lock = True
            import fcntl
            lock_fd = os.open("/run/lock/wdg-meshtastic-spi1.0.lock",
                              os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._radio_guard()
            module = importlib.import_module("LoRaRF.SX126x")
            module.gpio.setmode(module.gpio.BCM)
            original_spi = module.spi
            audit = SpiAudit(original_spi)
            module.spi = audit
            radio = module.SX126x()
            self._stop.wait(0.1)
            if self._stop.is_set():
                return
            begin_attempted = True
            detected = radio.begin(bus=1, cs=0, reset=25, busy=24, irq=-1)
            if not detected:
                raise RuntimeError("SX1262 initialization failed")
            initialized = True
            profile = get_profile(self.profile_id)
            readbacks = configure_radio(radio, profile)
            self._radio_event(dict(audit.report(profile), event="rx_started",
                                   readbacks=readbacks, rf_parameters=profile))
            self.ready = True
            self._status("ready")
            self._started.set()
            while not self._stop.is_set():
                event = poll_frame(radio)
                if event:
                    self._radio_event(event)
                if event and event.get("raw_frame_hex"):
                    self._handle_frame(bytes.fromhex(event["raw_frame_hex"]), event)
                pending = self._take_pending_tx()
                if pending:
                    token, frame = pending
                    success, error = self._transmit_frame(radio, frame)
                    with self._guard:
                        self._pending_tx = None
                    if self._on_tx:
                        self._on_tx(token, success, error)
                self._stop.wait(0.01)
        except Exception as exc:
            self._status("error", str(exc))
        finally:
            self.ready = False
            cleanup_errors = []
            try:
                if initialized:
                    radio.setStandby(radio.STANDBY_RC)
            except Exception as exc:
                self._emit(f"[MT] standby failed during cleanup: {exc}", "error")
            if original_spi is not None:
                try:
                    original_spi.close()
                except Exception as exc:
                    cleanup_errors.append(f"SPI close failed: {exc}")
            if module is not None and begin_attempted:
                try:
                    module.gpio.cleanup((25, 24))
                except Exception as exc:
                    cleanup_errors.append(f"GPIO cleanup failed: {exc}")
            if module is not None and original_spi is not None:
                module.spi = original_spi
            # Retain ownership if resources could not be released. A second
            # protocol must not start after an apparently successful stop.
            if cleanup_errors:
                self._status("error", "; ".join(cleanup_errors))
            else:
                if lock_fd is not None:
                    os.close(lock_fd)
                if owns_lock:
                    _SPI_OWNER_LOCK.release()
                self.running = False
            self._started.set()
            with self._guard:
                pending = self._pending_tx
                self._pending_tx = None
                self._tx_active = False
            if pending and self._on_tx:
                self._on_tx(pending[0], False, self.last_error or "radio stopped before queued transmission")
            while True:
                with self._guard:
                    try:token, _ = self._tx_queue.get_nowait()
                    except Empty:break
                if self._on_tx:
                    self._on_tx(token, False, "radio stopped before queued transmission")
            self._status("error" if cleanup_errors else "stopped", self.last_error)

    def _take_pending_tx(self):
        """Atomically claim a queued frame, retaining it throughout duty wait."""
        with self._guard:
            if self._quiescing or self._stop.is_set():
                return None
            if self._pending_tx is None:
                try:
                    self._pending_tx = self._tx_queue.get_nowait()
                except Empty:
                    return None
            if time.time() < self._state.get("next_tx_at", 0):
                return None
            self._tx_active = True
            return self._pending_tx
