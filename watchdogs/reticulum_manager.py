"""WDGWars Reticulum/LXMF chat manager.

Background client that talks to the WDGWars #general channel over Reticulum,
using LXMF as the message layer. Phase 1 reaches the hub over TCP only — the
wdgwars node has no LoRa radio, so the route to the hub exists solely over IP
(see plan / wdgwars_mesh_answers.md). A LoRa interface can be added later as a
second RNS interface without touching this class.

Mirrors the LoRaManager pattern: a daemon thread does the network work, app-set
callbacks only enqueue events for the main game loop to render.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from queue import Empty, Queue
from typing import Callable, Optional

log = logging.getLogger("reticulum")

# Reticulum + LXMF are optional deps — the app must still run without them.
try:
    import RNS  # type: ignore
    import LXMF  # type: ignore
    RNS_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    RNS = None  # type: ignore
    LXMF = None  # type: ignore
    RNS_AVAILABLE = False

# WDGWars network addresses (all lxmf.delivery, SINGLE) — verified on the live
# node. See ~/Downloads/wdgwars_mesh_answers.md.
HUB_HASH = bytes.fromhex("b29de9f746bd9d7a421ee84f3e0a6941")   # #general
PROP_HASH = bytes.fromhex("90ab9d448f17f3a121dc0f1230af39be")  # propagation
ECHO_HASH = bytes.fromhex("a9bb788a6a9723d709de2d5b321c2a7e")  # dev echo beacon
# LoRa-Relay: announces over LoRa every 20s, reposts to #general over TCP and
# fans #general back to LoRa leaves. On LoRa we target THIS (not the hub) with
# OPPORTUNISTIC — the only thing that works over slow LoRa (see WDGWars vademecum).
RELAY_HASH = bytes.fromhex("ddcd11421ae8b01d4fc58368c3038208")

# Direct-mesh #general channel: a Reticulum GROUP destination shared by ALL
# nodes (uConsole, gateway, T-Deck). Same identity → same channel hash; same
# 64-byte key → mutual decryption. GROUP broadcasts 1 hop over LoRa, so a peer
# in direct RF range receives it WITHOUT the gateway round-trip. Only usable
# while lora_active (must own the radio to broadcast GROUP — see vademecum
# lora-reticulum/mesh-group-czat-bezposredni-peer-to-peer.md).
#
# The shared identity+key are a SECRET (anyone holding them reads/injects the
# channel) so they are NOT in the repo — loaded from a config file distributed
# out-of-band to each node: {configdir}/group_creds, two hex lines
#   line 1: RNS.Identity private key  (128 hex)
#   line 2: GROUP symmetric key       (128 hex)
# Absent file → direct-mesh disabled, relay-only (current behaviour). Channel
# hash for the WDGWars creds: 0263685665b9d84fad33ac3bc8d110c4
GROUP_APP = "wdgwars"
GROUP_ASPECT = "general"
GROUP_MAX = 150       # keep to one packet (opportunistic ≤~159B); longer → relay only

TCP_HOST = "rns.wdgwars.pl"
TCP_PORT = 4242

_CONFIG_TEMPLATE = (
    "[reticulum]\n"
    "  enable_transport = False\n"
    "  share_instance = True\n"
    "\n"
    "[logging]\n"
    "  loglevel = 3\n"
    "\n"
    "[interfaces]\n"
    "  [[wdgwars-tcp]]\n"
    "    type = TCPClientInterface\n"
    "    interface_enabled = True\n"
    f"    target_host = {TCP_HOST}\n"
    f"    target_port = {TCP_PORT}\n"
)


def _ensure_configdir(configdir: str) -> None:
    """Create the RNS config dir + a TCP-only config file if missing."""
    os.makedirs(configdir, exist_ok=True)
    cfg = os.path.join(configdir, "config")
    if not os.path.isfile(cfg):
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(_CONFIG_TEMPLATE)


class ReticulumManager:
    """Background WDGWars LXMF chat client (TCP transport, Phase 1)."""

    def __init__(self, nick: str = "uConsole",
                 configdir: Optional[str] = None) -> None:
        self.nick = nick or "uConsole"
        self.configdir = configdir or os.path.expanduser(
            "~/.watchdogs_reticulum")
        self.dev_echo = False  # True → send to ECHO beacon instead of #general

        self.queue: Queue = Queue()            # status/log lines (optional)
        self._tx_queue: Queue = Queue()        # (token, text) outgoing
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.running = False
        self.connected = False
        self._tx_counter = 0

        # App-set callbacks (fired from the bg thread — must only enqueue)
        self._on_message: Optional[Callable[[str, str], None]] = None
        self._on_delivered: Optional[Callable[[int], None]] = None
        self._on_failed: Optional[Callable[[int], None]] = None
        self._on_status: Optional[Callable[[str], None]] = None

        self._reticulum = None
        self._identity = None
        self._router = None
        self._source = None        # our LXMF delivery destination
        self._hub_dest = None      # cached OUT destination to the hub
        self._lora_iface = None    # RNodeCompatInterface when LoRa transport on
        self._lora_mgr = None      # LoRaManager owning the SX1262
        self._rx_seen: list = []   # dedup keys (source+content) for inbound
        self._rx_parts: list = []  # partial (k/n) chunk reassembly buffers
        self._group_identity = None
        self._group_in = None      # GROUP IN dest (direct-mesh #general RX)
        self._group_out = None     # GROUP OUT dest (direct-mesh #general TX)

    # ------------------------------------------------------------------
    def send(self, text: str) -> int:
        """Queue a message, or return -1 after shutdown has started."""
        if self._stop_event.is_set():
            return -1
        token = self._tx_counter
        self._tx_counter += 1
        self._tx_queue.put((token, text))
        return token

    def start(self) -> None:
        """Spawn the background networking thread (idempotent)."""
        if not RNS_AVAILABLE:
            self._status("Reticulum not installed (pip install rns lxmf)")
            return
        if self.running or (self._thread and self._thread.is_alive()):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="reticulum", daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """Stop our worker and radio without creating a replacement TCP route."""
        self._stop_event.set()
        radio_stopped = self.disable_lora(restore_tcp=False)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        if thread and thread.is_alive():
            log.warning("Reticulum worker has not stopped")
            return False
        self._thread = None
        self.running = False
        self.connected = False
        while True:
            try:
                self._tx_queue.get_nowait()
            except Empty:
                break
        return radio_stopped

    # ------------------------------------------------------------------
    # LoRa transport (Phase 2) — RNode-compatible SX1262 interface
    # ------------------------------------------------------------------
    @property
    def lora_active(self) -> bool:
        return self._lora_iface is not None

    def _find_tcp_iface(self):
        return next((i for i in RNS.Transport.interfaces
                     if "TCPClient" in type(i).__name__), None)

    def _drop_hub_path(self) -> None:
        """Forget the cached route to the hub so RNS re-resolves it over the
        interfaces currently up (RNS otherwise keeps the shortest known path)."""
        try:
            with RNS.Transport.path_table_lock:
                RNS.Transport.path_table.pop(HUB_HASH, None)
        except Exception:
            pass

    def enable_lora(self, lora_manager) -> bool:
        """Switch the WDGWars transport to LoRa-only.

        RNS always prefers the shortest path — the hub is 1 hop over TCP vs
        2 hops over LoRa, so while TCP is up everything goes over TCP. To
        actually use LoRa we DETACH the TCP interface and drop the cached hub
        path, so the only remaining route is via the gateway over the air.
        Also stops MeshCore (one radio = one mode).
        """
        if not RNS_AVAILABLE or not self.running or self._stop_event.is_set():
            self._status("Start WDGWars Chat (TCP) first")
            return False
        if self._lora_iface is not None:
            return True
        try:
            from .rns_lora_interface import RNodeCompatInterface
            if getattr(lora_manager, "running", False) and not lora_manager.stop():
                raise RuntimeError("previous radio owner did not stop")
            # Detach TCP so RNS can't prefer the 1-hop internet route.
            tcp = self._find_tcp_iface()
            if tcp is not None:
                try:
                    tcp.detach()
                    if tcp in RNS.Transport.interfaces:
                        RNS.Transport.interfaces.remove(tcp)
                except Exception:
                    pass
            self._drop_hub_path()
            # Free the radio from MeshCore and bring up the LoRa interface.
            self._lora_mgr = lora_manager
            iface = RNodeCompatInterface(
                radio_send=lora_manager.send_rnode_frame,
                owner=RNS.Transport)
            lora_manager._on_rnode_rx = iface.feed_rx
            if not lora_manager.start_rnode():
                raise RuntimeError("RNode radio did not become ready")
            if self._stop_event.is_set():
                raise RuntimeError("Reticulum is stopping")
            iface.activate()
            self._lora_iface = iface
            # Actively resolve the hub over LoRa (needs the gateway in range).
            try:
                if not self._stop_event.is_set():
                    RNS.Transport.request_path(HUB_HASH)
            except Exception:
                pass
            self._status("LoRa-only — TCP detached (need gateway in range)")
            return True
        except Exception as exc:
            log.exception("enable_lora failed: %s", exc)
            self._status(f"LoRa enable failed: {exc}")
            self._lora_iface = None
            if self._lora_mgr is lora_manager:
                lora_manager._on_rnode_rx = None
                lora_manager.stop()
            self._restore_tcp()
            return False

    def disable_lora(self, *, restore_tcp: bool = True) -> bool:
        """Detach LoRa; ordinary handovers restore TCP, shutdown never does.

        Failed stops retain the radio/interface references for a later retry.
        """
        if self._lora_iface is None and self._lora_mgr is None:
            return True
        stopped = True
        if self._lora_iface is not None:
            try:
                self._lora_iface.detach()
            except Exception as exc:
                log.warning("Reticulum LoRa detach failed: %s", exc)
                stopped = False
        if self._lora_mgr is not None:
            try:
                self._lora_mgr._on_rnode_rx = None
                if self._lora_mgr.stop() is not True:
                    stopped = False
            except Exception as exc:
                log.warning("Reticulum radio stop failed: %s", exc)
                stopped = False
        if not stopped:
            log.warning("Reticulum LoRa stop not confirmed; ownership retained")
            return False
        self._lora_iface = self._lora_mgr = None
        if restore_tcp and not self._stop_event.is_set():
            self._restore_tcp()
            self._status("Back on TCP")
        return True

    def _restore_tcp(self) -> None:
        if self._stop_event.is_set():
            return
        # Restore the TCP interface (re-create it; RNS interfaces don't
        # cleanly re-attach after detach).
        try:
            if RNS_AVAILABLE and self._find_tcp_iface() is None:
                from RNS.Interfaces.TCPInterface import TCPClientInterface
                tcp = TCPClientInterface(RNS.Transport, {
                    "name": "wdgwars-tcp",
                    "target_host": TCP_HOST, "target_port": TCP_PORT})
                tcp.OUT = True
                RNS.Transport.interfaces.append(tcp)
            self._drop_hub_path()
        except Exception as exc:
            log.warning("TCP restore failed: %s", exc)

    # ------------------------------------------------------------------
    def _status(self, text: str) -> None:
        log.info(text)
        if self._on_status:
            try:
                self._on_status(text)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            if self._stop_event.is_set():
                return
            # Both RNS.Reticulum() and LXMF.LXMRouter() install SIGINT/SIGTERM
            # handlers, which only work on the main thread — we run in a daemon
            # thread, so neutralise signal.signal for the whole init (the app
            # owns its own shutdown), guaranteed-restored by the finally.
            import signal as _signal
            _orig_signal = _signal.signal
            try:
                _signal.signal = lambda *a, **k: None
                _ensure_configdir(self.configdir)
                self._reticulum = RNS.Reticulum(configdir=self.configdir)
                if self._stop_event.is_set():
                    return

                id_path = os.path.join(self.configdir, "identity")
                if os.path.isfile(id_path):
                    self._identity = RNS.Identity.from_file(id_path)
                if self._identity is None:
                    self._identity = RNS.Identity()
                    self._identity.to_file(id_path)

                storage = os.path.join(self.configdir, "lxmf")
                os.makedirs(storage, exist_ok=True)
                self._router = LXMF.LXMRouter(
                    identity=self._identity, storagepath=storage)
                self._source = self._router.register_delivery_identity(
                    self._identity, display_name=self.nick)
                self._router.register_delivery_callback(self._handle_inbound)
                self._setup_group_channel()
                try:
                    self._router.set_outbound_propagation_node(PROP_HASH)
                except Exception as exc:
                    log.warning("set_outbound_propagation_node failed: %s", exc)
            finally:
                _signal.signal = _orig_signal

            if self._stop_event.is_set():
                return
            self.running = True
            self._status("Reticulum up — announcing to WDGWars")
            try:
                if not self._stop_event.is_set():
                    self._source.announce()
            except Exception as exc:
                log.warning("announce failed: %s", exc)

            # Main loop: drain outgoing queue, keep alive.
            last_announce = 0.0
            while not self._stop_event.is_set():
                # Announce on LoRa so the relay learns our RF path (and the
                # route stays pinned to radio, not TCP). 45s not 20s: each
                # announce is a TX that blacks out RX during the post-TX radio
                # reconfigure — too-frequent announces cost us inbound frames.
                if self.lora_active and time.monotonic() - last_announce > 45:
                    try:
                        if not self._stop_event.is_set():
                            self._source.announce()
                    except Exception:
                        pass
                    last_announce = time.monotonic()
                try:
                    token, text = self._tx_queue.get(timeout=0.5)
                except Empty:
                    continue
                if self._stop_event.is_set():
                    break
                self._do_send(token, text)
        except Exception as exc:  # pragma: no cover
            log.exception("Reticulum thread crashed: %s", exc)
            self._status(f"Reticulum error: {exc}")
            self.running = False
        finally:
            self.running = False
            self.connected = False

    # --- direct-mesh GROUP channel (#general 1-hop over LoRa) ---------------
    def _setup_group_channel(self) -> None:
        """Register the shared GROUP destination for direct-mesh #general.
        Same identity+key on every node → same channel hash, mutual decrypt.
        Creds are loaded from {configdir}/group_creds (kept out of the repo, see
        GROUP_APP comment). Absent/invalid → direct-mesh off, relay-only. Auto-
        registers with Transport; RX starts once the LoRa iface is attached."""
        self._group_in = self._group_out = None
        creds_path = os.path.join(self.configdir, "group_creds")
        if not os.path.isfile(creds_path):
            log.info("GROUP creds absent (%s) — direct-mesh off, relay only",
                     creds_path)
            return
        try:
            lines = [ln.strip() for ln in open(creds_path).read().splitlines()
                     if ln.strip()]
            prv, key = bytes.fromhex(lines[0]), bytes.fromhex(lines[1])
            self._group_identity = RNS.Identity.from_bytes(prv)
            self._group_in = RNS.Destination(
                self._group_identity, RNS.Destination.IN,
                RNS.Destination.GROUP, GROUP_APP, GROUP_ASPECT)
            self._group_in.load_private_key(key)
            self._group_in.set_packet_callback(self._handle_group)
            self._group_out = RNS.Destination(
                self._group_identity, RNS.Destination.OUT,
                RNS.Destination.GROUP, GROUP_APP, GROUP_ASPECT)
            self._group_out.load_private_key(key)
            log.info("GROUP mesh channel ready: %s",
                     RNS.hexrep(self._group_in.hash, delimit=False))
        except Exception as exc:
            log.warning("GROUP channel setup failed: %s", exc)
            self._group_in = self._group_out = None

    def _group_broadcast(self, text: str) -> None:
        """Broadcast one #general message on the GROUP channel — reaches peers in
        direct RF range without the gateway. Independent of the relay send (works
        even if the gateway is unreachable). Payload is 'nick: text' so it matches
        the relay's fan-out content byte-for-byte → cross-path dedup. One packet
        only; longer text rides the relay path (which chunks)."""
        if self._group_out is None or self._stop_event.is_set():
            return
        try:
            payload = (self.nick + ": " + text).encode("utf-8")
            if len(payload) > GROUP_MAX:
                return
            if self._stop_event.is_set():
                return
            RNS.Packet(self._group_out, payload).send()
        except Exception as exc:
            log.warning("GROUP broadcast failed: %s", exc)

    def _handle_group(self, data, packet) -> None:
        """RX callback for the GROUP channel: the payload is the display line
        'nick: text' (same as a relay #general line). Dedup shared with the relay
        path via the 'chan|<content>' key so a message heard both ways shows once."""
        if self._stop_event.is_set():
            return
        try:
            content = bytes(data).decode("utf-8", "replace")
            if content.startswith(self.nick + ":"):   # our own broadcast echoed
                return
            key = "chan|" + content
            if key in self._rx_seen:
                return
            self._rx_seen.append(key)
            if len(self._rx_seen) > 32:
                self._rx_seen = self._rx_seen[-32:]
            log.info("GROUP RX (direct mesh, no gateway): %r", content[:80])
            if self._on_message:
                self._on_message("gen", content)
        except Exception as exc:
            log.warning("GROUP inbound parse failed: %s", exc)

    def _resolve_dest(self, dest_hash: bytes):
        """Resolve an LXMF delivery destination, requesting a path if needed."""
        if self._stop_event.is_set():
            return None
        idn = RNS.Identity.recall(dest_hash)
        if idn is None:
            if self._stop_event.is_set():
                return None
            RNS.Transport.request_path(dest_hash)
            # Wait briefly for the path/announce to arrive.
            for _ in range(20):
                if self._stop_event.wait(0.25):
                    return None
                idn = RNS.Identity.recall(dest_hash)
                if idn is not None:
                    break
        if idn is None:
            return None
        return RNS.Destination(
            idn, RNS.Destination.OUT, RNS.Destination.SINGLE,
            "lxmf", "delivery")

    def _do_send(self, token: int, text: str) -> None:
        if self._stop_event.is_set():
            return
        lora = self.lora_active
        # Direct-mesh first: on LoRa, also broadcast on the GROUP channel so a
        # peer in RF range (T-Deck) gets it immediately, without the gateway.
        # Independent of the relay send below — fires even if no relay path.
        if lora and not self.dev_echo:
            self._group_broadcast(text)
        # On LoRa: send to the relay with OPPORTUNISTIC (DIRECT/Link never
        # establishes over slow LoRa). On TCP: straight to the hub with DIRECT.
        if self.dev_echo:
            target = ECHO_HASH
        elif lora:
            target = RELAY_HASH
        else:
            target = HUB_HASH
        dest = self._resolve_dest(target)
        if self._stop_event.is_set():
            return
        if dest is None:
            self._status("No LoRa path to relay yet (gateway in range?)"
                         if lora else "No path to WDGWars hub yet")
            self._failed(token)
            return
        method = (LXMF.LXMessage.OPPORTUNISTIC if lora
                  else LXMF.LXMessage.DIRECT)
        # One copy: the relay reposts every copy it receives, so a burst shows up
        # as duplicates on #general. One opportunistic packet is reliable at
        # working range; bump only if messages start dropping at distance.
        burst = 1
        try:
            for i in range(burst):
                if self._stop_event.is_set():
                    return
                lxm = LXMF.LXMessage(dest, self._source, text, self.nick,
                                     desired_method=method)
                lxm.try_propagation_on_fail = not lora
                if i == 0 and not lora:
                    lxm.register_delivery_callback(
                        lambda m, t=token: self._delivered(t))
                    lxm.register_failed_callback(
                        lambda m, t=token: self._failed(t))
                if self._stop_event.is_set():
                    return
                self._router.handle_outbound(lxm)
                if burst > 1 and i < burst - 1 and self._stop_event.wait(1.2):
                    break
            if lora:
                # Opportunistic has no delivery proof — mark optimistically sent.
                self._delivered(token)
        except Exception as exc:
            log.warning("send failed: %s", exc)
            self._failed(token)

    # --- callbacks from RNS/LXMF threads → app event queue ---
    def _handle_inbound(self, message) -> None:
        if self._stop_event.is_set():
            return
        try:
            src = getattr(message, "source_hash", b"") or b""
            # Skip our own messages echoed back.
            if self._source is not None and src == self._source.hash:
                return
            content = (message.content or b"").decode("utf-8", "replace")
            title = (message.title or b"").decode("utf-8", "replace").strip()
            # Dedup by SOURCE+CONTENT (not msg hash — resends re-pack with a new
            # hash on a lossy link). #general (relay/hub) uses a source-independent
            # 'chan|' key so a message heard BOTH via the relay AND the direct
            # GROUP mesh shows once; DMs keep source+content (same text from two
            # senders stays distinct).
            if src in (RELAY_HASH, HUB_HASH):
                key = "chan|" + content
            else:
                key = src.hex()[:16] + "|" + content
            if key in self._rx_seen:
                return
            self._rx_seen.append(key)
            if len(self._rx_seen) > 32:
                self._rx_seen = self._rx_seen[-32:]
            # (k/n) chunk from the relay? Reassemble at the receiver.
            m = re.match(r"^\((\d+)/(\d+)\)(.*)$", content, re.S)
            if m:
                k, n, body = int(m.group(1)), int(m.group(2)), m.group(3)
                if n > 1 and 1 <= k <= n:
                    self._reassemble(src, title, k, n, body)
                    return
            nick = title or (src.hex()[:8] if src else "????")
            if self._on_message:
                self._on_message(nick, content)
        except Exception as exc:
            log.warning("inbound parse failed: %s", exc)

    def _reassemble(self, src: bytes, title: str, k: int, n: int,
                    body: str) -> None:
        """Reassemble (k/n) chunks by (source, n) with scan-back — tolerates
        interleaving of two multipart messages from the same sender."""
        sid = src.hex()[:16]
        nick = title or (sid[:8] if sid else "????")
        if k == 1:
            self._rx_parts.append({"sid": sid, "nick": nick, "n": n,
                                   "next": 2, "text": body})
        else:
            for p in reversed(self._rx_parts[-8:]):
                if p["sid"] == sid and p["n"] == n and p["next"] == k:
                    p["text"] += body
                    p["next"] = k + 1 if k < n else 0
                    if p["next"] == 0:
                        # Cross-path dedup: a short message the relay chunked can
                        # also arrive whole on the direct GROUP mesh. Same 'chan|'
                        # key as _handle_group/_handle_inbound → show once.
                        ckey = "chan|" + p["text"]
                        if ckey not in self._rx_seen:
                            self._rx_seen.append(ckey)
                            if len(self._rx_seen) > 32:
                                self._rx_seen = self._rx_seen[-32:]
                            if self._on_message:
                                self._on_message(p["nick"], p["text"])
                    break
            else:
                # Lost part 1 — best-effort partial.
                self._rx_parts.append({"sid": sid, "nick": nick, "n": n,
                                       "next": k + 1, "text": body})
        if len(self._rx_parts) > 12:
            self._rx_parts = [p for p in self._rx_parts[-12:]]

    def _delivered(self, token: int) -> None:
        if self._stop_event.is_set():
            return
        self.connected = True
        if self._on_delivered:
            try:
                self._on_delivered(token)
            except Exception:
                pass

    def _failed(self, token: int) -> None:
        if self._stop_event.is_set():
            return
        if self._on_failed:
            try:
                self._on_failed(token)
            except Exception:
                pass
