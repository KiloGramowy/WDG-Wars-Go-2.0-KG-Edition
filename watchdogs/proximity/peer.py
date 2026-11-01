"""uConsole side of the WDGWars proximity smoke test -- the second radio.

Mirror of tools/badge_peer.py, but on Linux/BlueZ instead of a badge:
advertise a rotating WDGWars identity AND listen for everyone else, so the
question "do two different radios see one another" can be answered. Point this
at the badge (running badge_peer.py) and watch both logs.

    sudo python3 tools/uconsole_peer.py --secret peer-jeden --know badger=badger-test

The maths, the trend and the meeting state machine all live in app/wdgwars/prox.py,
which is the SAME file the badge runs -- so both sides land in the same rotation
slot and derive/resolve each other's identity identically. This file is only the
radio and the I/O; it must never re-implement identity derivation.

Radio transports, chosen for what actually works on this hardware (bluez 5.82,
ClockworkPi CM5 UART controller):

  * SCAN  -- bleak (asyncio). Central role; each advertisement gives us its local
             name (the rotating identity) and manufacturer data (peer
             fingerprints).
  * ADVERTISE -- the kernel mgmt API via `btmgmt add-adv` (needs root). It sets
             the whole 31-byte payload (flags + complete local name + 0xFFFF
             manufacturer data with up to three fingerprints) -- exactly the
             bytes prox_ble.build_payload() lays out for the badge. This is the
             half `btmgmt name` could never send, without which the badge hears
             us but never marks us MUTUAL.

TWO FINDINGS THIS FILE IS BUILT AROUND (see docs/proximity-notes.md):

  1. D-Bus org.bluez.LEAdvertisement1 (the task's second suggested route) is the
     dead end it predicted: RegisterAdvertisement returned org.bluez.Error.Failed
     and, even alone, never put anything on air. btmgmt add-adv works.

  2. This controller CANNOT advertise and scan at once: `add-adv` fails with
     status 0x03 (Failed) whenever an LE scan is running (it succeeds the moment
     the scan stops). The badge (RP2350) does both simultaneously; this one does
     not. So the default here is TIME-DIVISION -- a short advertise burst with
     the scan stopped, then a scan window with advertising off, alternating.
     Pass --duplex both to attempt simultaneous operation on hardware that can.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

# app/wdgwars next to tools/ -- the shared engine + wire format live there.
sys.path.insert(0, os.path.dirname(__file__))
import prox            # noqa: E402  the shared engine -- do not duplicate its maths
import prox_ble        # noqa: E402  PREFIX, fingerprint, build_payload, ECHO_MAX


def epoch_ms():
    """Wall clock in ms since the 1970 epoch. Both sides must land on the same
    UNIX millisecond or the rotation slots never line up -- which means the
    badge's RTC has to be UTC, a thing to check, not assume."""
    return int(time.time() * 1000)


def clock():
    tm = time.localtime()
    return "%02d:%02d:%02d" % (tm[3], tm[4], tm[5])


class MgmtAdvertiser:
    """Advertise WDG-<ident> + peer echoes as a hardware advertising instance,
    via `btmgmt add-adv`. Needs root; the peer is meant to run under sudo."""

    def __init__(self, index=0, instance=1):
        self.index = int(index)
        self.instance = str(instance)
        self._err = None

    def start(self):
        if subprocess.call(["which", "btmgmt"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            raise RuntimeError("btmgmt not found -- apt install bluez-tools")
        if os.geteuid() != 0:
            raise RuntimeError("advertising needs root -- run under sudo")
        self._rm()

    def on(self, name, echoes):
        """Register the advertisement. Caller guarantees no scan is running."""
        payload = prox_ble.build_payload(name, list(echoes)[:prox_ble.ECHO_MAX])
        self._rm()
        r = subprocess.run(
            ["btmgmt", "--index", str(self.index),
             "add-adv", "-d", payload.hex(), self.instance],
            capture_output=True, text=True)
        if "Instance added" not in (r.stdout + r.stderr):
            self._err = "add-adv: %s" % ((r.stderr or r.stdout).strip() or "unknown")
            return False
        return True

    def off(self):
        self._rm()

    def _rm(self):
        subprocess.run(["btmgmt", "--index", str(self.index), "rm-adv", self.instance],
                       capture_output=True, text=True)

    def stop(self):
        self._rm()


def _make_scanner(on_adv, adapter):
    from bleak import BleakScanner
    try:
        return BleakScanner(detection_callback=on_adv, adapter=adapter)
    except TypeError:
        return BleakScanner(detection_callback=on_adv)


async def run(args, known):
    secret, adapter = args.secret, args.adapter
    do_scan = not args.advertise_only
    do_adv = not args.scan_only and bool(secret)
    alternate = do_scan and do_adv and args.duplex == "alternate"

    print("uConsole jako peer WDGWars")
    print("  sekret     :", secret or "(brak -- tylko sluchanie)")
    print("  rozwiazuje :", ", ".join(known) or "nikogo")
    print("  radio      :", adapter,
          "| adv:", "tak" if do_adv else "nie",
          "| scan:", "tak" if do_scan else "nie",
          "| duplex:", args.duplex if (do_scan and do_adv) else "-")
    print("  Ctrl-C konczy\n")

    engine = prox.Engine(prox.Directory(known))
    seen = []
    company = int.from_bytes(prox_ble._COMPANY, "little")

    def on_adv(device, adv):
        name = adv.local_name or ""
        if not name.startswith(prox_ble.PREFIX):
            return
        ident = name[len(prox_ble.PREFIX):].upper()
        if not ident:
            return
        raw = adv.manufacturer_data.get(company, b"")
        echoes = [bytes(raw[i:i + 2]) for i in range(0, len(raw) - 1, 2)]
        rssi = adv.rssi if adv.rssi is not None else -127
        seen.append((ident, rssi, echoes))

    scanner = _make_scanner(on_adv, adapter) if do_scan else None
    advertiser = MgmtAdvertiser(index=_adapter_index(adapter)) if do_adv else None
    if advertiser:
        advertiser.start()

    scanning = False
    if do_scan and not alternate:
        await scanner.start()
        scanning = True

    my_slot = None
    my_ident = ""
    started = epoch_ms()

    def drain_and_report():
        t = epoch_ms()
        mine = prox_ble.fingerprint(my_ident) if my_ident else b"\x00\x00"
        heard, seen[:] = list(seen), []
        for ident, rssi, echoes in heard:
            engine.observe(ident, rssi, t, saw_me=(mine in echoes))
        engine.prune(t)
        report(engine, t, len(heard))

    try:
        while True:
            t = epoch_ms()
            slot = prox.slot_of(t)
            if secret:
                my_ident = prox.derive(secret, slot)
            echoes = [prox_ble.fingerprint(p.ident)
                      for p in list(engine.peers.values())[:prox_ble.ECHO_MAX]]

            if alternate:
                # --- advertise burst, scan OFF (this controller cannot do both) ---
                if scanning:
                    await scanner.stop()
                    scanning = False
                advertiser.on(prox_ble.PREFIX + my_ident, echoes)
                if advertiser._err:
                    print("[%s] ADV BLAD: %s" % (clock(), advertiser._err.splitlines()[0]))
                    advertiser._err = None
                if slot != my_slot:
                    print("[%s] nadaje jako %s%s" % (clock(), prox_ble.PREFIX, my_ident))
                    my_slot = slot
                await asyncio.sleep(args.adv_secs)
                # --- scan window, advertising OFF ---
                advertiser.off()
                await scanner.start()
                scanning = True
                await asyncio.sleep(args.scan_secs)
            else:
                if advertiser:
                    advertiser.on(prox_ble.PREFIX + my_ident, echoes)
                    if advertiser._err:
                        print("[%s] ADV BLAD: %s" % (clock(), advertiser._err.splitlines()[0]))
                        advertiser._err = None
                if secret and slot != my_slot:
                    print("[%s] nadaje jako %s%s" % (clock(), prox_ble.PREFIX, my_ident))
                    my_slot = slot
                await asyncio.sleep(args.window)

            drain_and_report()
            if args.snapshot:
                write_snapshot(args.snapshot, engine, epoch_ms(), my_ident, slot)
            if args.seconds and (epoch_ms() - started) >= args.seconds * 1000:
                print("[%s] koniec (--seconds %d)" % (clock(), args.seconds))
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if scanner is not None and scanning:
            try:
                await scanner.stop()
            except Exception:
                pass
        if advertiser is not None:
            advertiser.stop()
        print("radio opuszczone")


def write_snapshot(path, engine, t, my_ident, slot):
    """Dump the current view as JSON for a separate UI to draw (pyxel etc.).

    Written atomically each window so a reader never sees a half file. Keeps the
    radio (this process, root) and the screen (a plain user process) apart, which
    is what a pyxel window on Wayland wants and what a threaded asyncio radio in
    the same process fought over.
    """
    rows, seen = [], set()
    for p in engine.nearby():
        if p.label in seen:
            continue
        seen.add(p.label)
        rows.append({"label": p.label,
                     "ema": round(p.ema, 1) if p.ema is not None else None,
                     "state": p.state, "trend": p.trend(),
                     "mutual": p.mutual(t), "bar": p.bar(20)})
    meet = None
    for p in engine.peers.values():
        if p.contact_at is not None and (t - p.contact_at) < 6000:
            meet = {"label": p.label or p.ident,
                    "rssi": round(p.ema, 1) if p.ema is not None else None}
    snap = {"my_ident": my_ident, "slot": slot, "rows": rows,
            "unknown": engine.unknown_count(), "peers": len(engine.peers),
            "meet": meet, "ts": t}
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(snap, f)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def report(engine, t, raw):
    rows = engine.nearby()
    unknown = engine.unknown_count()
    if not rows and not unknown:
        print("[%s] nikogo nie widac (%d reklam w oknie)" % (clock(), raw))
        return
    for p in rows:
        print("[%s] %-12s %6.1f dBm  %-16s %-11s %s"
              % (clock(), p.label, p.ema,
                 prox.TREND_WORDS.get(p.trend(), ""),
                 prox.STATE_NAMES[p.state],
                 "MUTUAL" if p.mutual(t) else ""))
    for ident, p in engine.peers.items():
        if p.label or p.state == prox.LOST:
            continue
        print("[%s] %s%-10s %6.1f dBm  %-11s (nierozpoznany)"
              % (clock(), prox_ble.PREFIX, ident, p.ema,
                 prox.STATE_NAMES[p.state]))
    for ev in engine.take_events():
        print("[%s] *** SPOTKANIE z %s, %.0f s, %s dBm"
              % (clock(), ev["label"] or ev["ident"],
                 ev["duration"] / 1000.0, ev["rssi"]))


def _adapter_index(adapter):
    """hci0 -> 0. btmgmt speaks controller indexes, bleak speaks hciN names."""
    digits = "".join(c for c in adapter if c.isdigit())
    return int(digits) if digits else 0


def parse_args():
    ap = argparse.ArgumentParser(description="WDGWars proximity peer (uConsole/BlueZ)")
    ap.add_argument("--secret", default="", help="my proximity secret")
    ap.add_argument("--know", action="append", default=[], metavar="label=secret",
                    help="a peer we are allowed to resolve; repeatable")
    ap.add_argument("--adapter", default="hci0")
    ap.add_argument("--duplex", choices=("alternate", "both"), default="alternate",
                    help="alternate = advertise burst / scan window (this hardware); "
                         "both = simultaneous (only if the controller allows)")
    ap.add_argument("--adv-secs", type=float, default=3.0, dest="adv_secs",
                    help="alternate mode: seconds per advertise burst")
    ap.add_argument("--scan-secs", type=float, default=6.0, dest="scan_secs",
                    help="alternate mode: seconds per scan window")
    ap.add_argument("--window", type=float, default=5.0,
                    help="both/single mode: report every N s")
    ap.add_argument("--seconds", type=int, default=0, help="stop after N s (0 = forever)")
    ap.add_argument("--snapshot", default="", help="write JSON view here each window (for a UI)")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--advertise-only", action="store_true")
    a = ap.parse_args()
    known = {}
    for kv in a.know:
        if "=" not in kv:
            ap.error("--know wants label=secret, got %r" % kv)
        label, secret = kv.split("=", 1)
        known[label] = secret
    if a.scan_only and a.advertise_only:
        ap.error("--scan-only and --advertise-only are mutually exclusive")
    if not a.scan_only and not a.secret:
        print("uwaga: brak --secret, nie bede nadawac (tylko skan)")
    return a, known


def main():
    a, known = parse_args()
    try:
        asyncio.run(run(a, known))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
