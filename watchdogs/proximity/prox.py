"""Proximity: who else is carrying a WDGWars badge, and are they getting closer.

This module is deliberately free of firmware globals -- no `screen`, no `badge`,
no `time`. Everything that needs a clock takes the time as an argument. That is
not tidiness for its own sake: it means the whole engine runs on a desktop
Python, so the filtering, the trend and the state machine can be tested against
recorded traces instead of by walking around a car park.

The radio sits behind `Radio`, with two implementations, because at the time of
writing it is not established that this firmware ships a usable `bluetooth`
module. If it does not, the same UI runs on a Wi-Fi backend at a slower rhythm,
and nothing above this file has to know which one answered.

    RADIO -> observe(ident, rssi) -> Engine -> nearby() / target()

Every `now_ms` in this module is UTC milliseconds since the epoch, taken from
the RTC -- not time.ticks_ms(). Ageing would be happy with any monotonic clock,
but the rotating identity is derived from the wall clock so that two badges
land in the same slot, and having two different clocks in one file is how that
distinction gets lost.
"""

import hashlib
import binascii


# ───────────────────────────────────────────────────────────────────────────
# TUNING
#
# Every one of these wants checking against a real pair of badges. The values
# here are a starting point taken from the published RSSI bands, not measured
# ones -- see docs/proximity-notes.md for what has to be calibrated and how.
# ───────────────────────────────────────────────────────────────────────────

MEDIAN_N = 5             # samples in the outlier-killing median window
EMA_ALPHA = 0.35         # weight of the newest median in the smoothed value
TREND_N = 12             # smoothed samples kept for the trend and the strip
TREND_MIN = 4            # fewer than this and the trend is not reported at all

# Signal bands, in dBm, loosest first. Hysteresis keeps a peer sitting on a
# boundary from flapping between two states -- which on e-paper is not a
# cosmetic problem: every flap is a full panel refresh.
BAND_NEAR = -85
BAND_CLOSE = -70
BAND_VERY_CLOSE = -55
BAND_HYST = 3

# Trend classes, in dB across the window.
TREND_STRONG = 6
TREND_WEAK = 2

# A peer stays on the list this long after its last advertisement. Somebody
# walking past a doorway must not make a row appear and vanish and appear.
LOST_MS = 30_000
DROP_MS = 120_000        # ... and is forgotten entirely this long after

# What it takes to call it a meeting rather than a reading.
CONTACT_STATE = 3        # CLOSE
CONTACT_MS = 15_000
CONTACT_GAP_MS = 300_000  # do not re-fire for the same peer within this

# Identity rotation.
ROTATE_MS = 60_000
IDENT_BYTES = 3
SLOT_SLACK = 1           # slots either side accepted when resolving (clock drift)

UNKNOWN, WEAK, NEAR, CLOSE, VERY_CLOSE, LOST = range(6)
STATE_NAMES = ("UNKNOWN", "WEAK", "NEAR", "CLOSE", "VERY CLOSE", "LOST")

# Trend, -2..+2. Kept as a number so the UI can pick its own glyphs: the ROM
# fonts have no arrows, so whatever draws this has to build them from shapes.
T_AWAY_FAST, T_AWAY, T_FLAT, T_NEAR, T_NEAR_FAST = -2, -1, 0, 1, 2
TREND_WORDS = {
    T_AWAY_FAST: "MOVING AWAY FAST",
    T_AWAY: "MOVING AWAY",
    T_FLAT: "HOLDING",
    T_NEAR: "GETTING CLOSER",
    T_NEAR_FAST: "CLOSING FAST",
}


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# ───────────────────────────────────────────────────────────────────────────
# IDENTITY
#
# The badge knows which WDGWars user it belongs to, but broadcasting that would
# let any BLE scanner in the room tie a name to a person walking around. So the
# radio name is derived from the user's provisioned secret and the clock, and
# rotates. Anybody can see that a WDGWars badge is present; only somebody
# holding the matching resolution key can say whose.
#
# Resolution keys come from the portal at sync time, for the people you are
# entitled to resolve -- your team, an event roster. This is the same shape as
# a BLE resolvable private address and an IRK, and it is what stops rotation
# from turning the nearby list into churn.
# ───────────────────────────────────────────────────────────────────────────

# The portal's packer already personalises the download: it substitutes the
# HANDLE constant and drops handle.txt next to the app. The proximity secret
# rides the same road -- placeholder in the source, real value injected at
# package time -- so there is no second provisioning mechanism to build, keep
# in step, or get wrong.
#
# What this secret is NOT: strong. It lands in a file on a FAT volume that
# anybody can read by double-tapping RESET. It is enough to stop a stranger's
# BLE scanner tying an advertisement to a named player, which is the threat
# worth defending against here. It is NOT proof of who sent something, so the
# portal must not accept a proximity event as authentic merely because it
# carries a value derived from one.
PROX_SECRET = "YOUR-PROX-SECRET"

for _p in ("prox.txt",
           "/system/apps/wdgwars/prox.txt",
           "/apps/wdgwars/prox.txt"):
    try:
        with open(_p) as _f:
            _s = _f.read().strip()
        if _s:
            PROX_SECRET = _s
            break
    except Exception:
        pass


def provisioned():
    """The secret to advertise under, or "" if this copy was never packaged."""
    return "" if PROX_SECRET.startswith("YOUR-") else PROX_SECRET


def derive(secret, slot):
    """The radio identity a holder of `secret` advertises during `slot`."""
    if not secret:
        return ""
    h = hashlib.sha256()
    h.update(secret.encode("utf-8") if isinstance(secret, str) else secret)
    h.update(b"|wdgprox|")
    h.update(str(int(slot)).encode("utf-8"))
    return binascii.hexlify(h.digest()[:IDENT_BYTES]).decode("ascii").upper()


def slot_of(epoch_ms):
    """Which rotation slot a moment falls in.

    This MUST be wall-clock time -- UTC milliseconds since the epoch, taken
    from the RTC, which the firmware syncs from NTP. It must never be
    time.ticks_ms(): that counts from power-on, so two badges would each be in
    their own private slot and could never derive or resolve each other's
    identity. Everything else in this file is happy with any monotonic clock;
    this one function is not, and the difference is invisible until two badges
    are in the same room.
    """
    return int(epoch_ms // ROTATE_MS)


class Directory:
    """Maps a rotating radio identity back to a person, when we are allowed to.

    `keys` is {label: secret}, handed over by the portal. Unresolvable peers are
    not second-class citizens by accident -- they are deliberately never given a
    name or a row of their own, because a name that changes every minute is
    worse than no name at all.
    """

    def __init__(self, keys=None):
        self.keys = dict(keys or {})
        self._cache = {}

    def resolve(self, ident, now_ms):
        if not ident:
            return None
        hit = self._cache.get(ident)
        if hit is not None:
            return hit
        slot = slot_of(now_ms)
        # Neighbouring slots too. Two reasons, and both happen: a badge that
        # advertised just before the turn of the minute is still that person,
        # and two RTCs synced from NTP at different times will not agree to the
        # second. One slot of slack each way costs a couple of hashes and saves
        # a peer flickering in and out of being resolvable every minute.
        for s in range(slot - SLOT_SLACK, slot + SLOT_SLACK + 1):
            for label, secret in self.keys.items():
                if derive(secret, s) == ident:
                    self._cache[ident] = label
                    return label
        return None

    def forget_stale(self, now_ms):
        """Drop cache entries for slots that can no longer be advertised."""
        if len(self._cache) > 64:
            self._cache = {}


# ───────────────────────────────────────────────────────────────────────────
# ONE PEER
# ───────────────────────────────────────────────────────────────────────────

class Peer:
    def __init__(self, ident, now_ms):
        self.ident = ident
        self.label = None            # set once the directory resolves it
        self.first_seen = now_ms
        self.last_seen = now_ms
        self.count = 0
        self.raw = []                # last MEDIAN_N raw readings
        self.smooth = []             # last TREND_N smoothed readings
        self.ema = None
        self.state = UNKNOWN
        self.saw_me_at = None        # when this peer last reported seeing us
        self.contact_since = None
        self.contact_at = None

    # -- measurement ------------------------------------------------------
    def observe(self, rssi, now_ms, saw_me=False):
        self.last_seen = now_ms
        self.count += 1
        if saw_me:
            self.saw_me_at = now_ms

        self.raw.append(rssi)
        if len(self.raw) > MEDIAN_N:
            self.raw.pop(0)
        med = _median(self.raw)

        # Median first, then the exponential average. One kills the spikes that
        # a body or a turn of the wrist puts in a single reading; the other
        # stops the value stepping about between refreshes.
        self.ema = med if self.ema is None else (
            EMA_ALPHA * med + (1.0 - EMA_ALPHA) * self.ema)

        self.smooth.append(self.ema)
        if len(self.smooth) > TREND_N:
            self.smooth.pop(0)
        self._classify()

    def _classify(self):
        v = self.ema
        if v is None:
            return
        # Hysteresis: moving up a band needs the threshold plus a margin,
        # moving down needs it minus the margin. Without this a peer parked on
        # a boundary repaints the screen every few seconds.
        cur = self.state if self.state not in (UNKNOWN, LOST) else None
        bands = ((BAND_VERY_CLOSE, VERY_CLOSE), (BAND_CLOSE, CLOSE), (BAND_NEAR, NEAR))
        new = WEAK
        for edge, state in bands:
            up = edge + BAND_HYST
            down = edge - BAND_HYST
            limit = down if (cur is not None and cur >= state) else up
            if v >= limit:
                new = state
                break
        self.state = new

    # -- derived ----------------------------------------------------------
    def trend_db(self):
        """Change across the smoothed window, in dB. Positive = closing."""
        n = len(self.smooth)
        if n < TREND_MIN:
            return None
        half = n // 2
        old = sum(self.smooth[:half]) / half
        new = sum(self.smooth[half:]) / (n - half)
        return new - old

    def trend(self):
        d = self.trend_db()
        if d is None:
            return T_FLAT
        if d >= TREND_STRONG:
            return T_NEAR_FAST
        if d >= TREND_WEAK:
            return T_NEAR
        if d <= -TREND_STRONG:
            return T_AWAY_FAST
        if d <= -TREND_WEAK:
            return T_AWAY
        return T_FLAT

    def mutual(self, now_ms):
        return self.saw_me_at is not None and (now_ms - self.saw_me_at) <= LOST_MS

    def age_ms(self, now_ms):
        return now_ms - self.last_seen

    def bar(self, width=10):
        """Signal as a filled-cells count, for a bar the UI can draw."""
        if self.ema is None:
            return 0
        lo, hi = -95.0, -45.0
        f = (self.ema - lo) / (hi - lo)
        f = 0.0 if f < 0 else (1.0 if f > 1 else f)
        return max(1, int(round(f * width)))


# ───────────────────────────────────────────────────────────────────────────
# THE ENGINE
#
# This is the whole public surface. Game logic talks to these names and must
# never learn whether the numbers arrived over BLE, over Wi-Fi, or from a file
# of recorded readings.
# ───────────────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, directory=None):
        self.dir = directory or Directory()
        self.peers = {}
        self.tracking = None
        self.events = []

    # -- input ------------------------------------------------------------
    def observe(self, ident, rssi, now_ms, saw_me=False):
        p = self.peers.get(ident)
        if p is None:
            p = self.peers[ident] = Peer(ident, now_ms)
        p.observe(rssi, now_ms, saw_me=saw_me)
        if p.label is None:
            p.label = self.dir.resolve(ident, now_ms)
        self._contact(p, now_ms)
        return p

    def _contact(self, p, now_ms):
        """A meeting is mutual, close, and sustained -- never one strong reading.

        A single -40 dBm sample means somebody's badge was briefly pointed at
        yours. Requiring both directions, a band, and time is what makes this a
        fact worth sending to the portal.
        """
        ok = p.state >= CONTACT_STATE and p.mutual(now_ms)
        if not ok:
            p.contact_since = None
            return
        if p.contact_since is None:
            p.contact_since = now_ms
            return
        if now_ms - p.contact_since < CONTACT_MS:
            return
        if p.contact_at is not None and (now_ms - p.contact_at) < CONTACT_GAP_MS:
            return
        p.contact_at = now_ms
        self.events.append({
            "type": "contact",
            "ident": p.ident,
            "label": p.label,
            "at": now_ms,
            "duration": now_ms - p.contact_since,
            "rssi": round(p.ema, 1) if p.ema is not None else None,
            "samples": p.count,
        })

    def prune(self, now_ms):
        for ident in list(self.peers):
            p = self.peers[ident]
            if p.age_ms(now_ms) > DROP_MS:
                del self.peers[ident]
                if self.tracking == ident:
                    self.tracking = None
            elif p.age_ms(now_ms) > LOST_MS:
                p.state = LOST
        self.dir.forget_stale(now_ms)

    # -- output (the interface game logic is allowed to know about) -------
    def nearby(self, now_ms=None):
        """Resolvable peers, strongest first. Unresolved ones are not here."""
        out = [p for p in self.peers.values() if p.label and p.state != LOST]
        out.sort(key=lambda p: -(p.ema if p.ema is not None else -999))
        return out

    def unknown_count(self, now_ms=None):
        """How many badges are around that we are not allowed to name.

        Deliberately a count and not a list: their identity rotates, so rows
        for them would multiply every minute and mean nothing.
        """
        return len([p for p in self.peers.values()
                    if not p.label and p.state != LOST])

    def get(self, ident):
        return self.peers.get(ident)

    def signal(self, ident):
        p = self.peers.get(ident)
        return None if p is None else p.ema

    def trend(self, ident):
        p = self.peers.get(ident)
        return T_FLAT if p is None else p.trend()

    def is_mutual(self, ident, now_ms):
        p = self.peers.get(ident)
        return bool(p and p.mutual(now_ms))

    def last_seen(self, ident, now_ms):
        p = self.peers.get(ident)
        return None if p is None else p.age_ms(now_ms)

    def track(self, ident):
        self.tracking = ident if ident in self.peers else None
        return self.tracking

    def untrack(self):
        self.tracking = None

    def target(self):
        return self.peers.get(self.tracking) if self.tracking else None

    def take_events(self):
        out, self.events = self.events, []
        return out
