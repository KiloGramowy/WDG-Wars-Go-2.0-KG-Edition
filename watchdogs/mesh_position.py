"""Keep node coordinates separate from the receiver's observation position.

Receiver coordinates describe where a packet identifying a node was heard.
They establish neither that node's location nor direct radio range to it.
"""

from collections.abc import Mapping
import math
import time


_MAX_FIX_AGE = 10.0
_OBSERVATION_KINDS = ("reception", "user_assignment")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _coordinates(data):
    if not isinstance(data, Mapping):
        return None
    lat, lon = _number(data.get("lat")), _number(data.get("lon"))
    if (lat is None or lon is None or not -90 <= lat <= 90
            or not -180 <= lon <= 180 or (lat == 0 and lon == 0)):
        return None
    return lat, lon


def _credible_metrics(data):
    quality, sats, hdop = data.get("quality"), data.get("sats"), _number(data.get("hdop"))
    return (type(quality) is int and quality in (1, 2, 4, 5)
            and type(sats) is int and sats >= 4 and hdop is not None and 0 < hdop <= 3)


def node_position(data):
    """Return valid canonical node coordinates; never read map/player state."""
    coordinates = _coordinates(data)
    if coordinates is None:
        return None
    return dict(lat=coordinates[0], lon=coordinates[1], source="node")


def receiver_observation(gps, kind="reception", now=None, wall_now=None):
    """Snapshot a fresh measured GPS fix with explicit observation provenance.

    The fix provider's monotonic time is translated to UTC epoch seconds once.
    A manual assignment retains its current assignment time; it does not
    manufacture a historical reception or change the node's first/last seen.
    """
    if kind not in _OBSERVATION_KINDS:
        return None
    now = _number(time.monotonic() if now is None else now)
    wall_now = _number(time.time() if wall_now is None else wall_now)
    if now is None or now < 0 or wall_now is None or wall_now <= 0:
        return None
    try:
        fix = gps.radio_fix(now=now)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    coordinates = _coordinates(fix)
    if coordinates is None or not _credible_metrics(fix):
        return None
    fix_time = _number(fix.get("observed_at"))
    if fix_time is None:
        return None
    age = now - fix_time
    observed_at = wall_now - age
    if not 0 <= age <= _MAX_FIX_AGE or observed_at <= 0:
        return None
    return dict(lat=coordinates[0], lon=coordinates[1], source="receiver_gps",
                kind=kind, observed_at=observed_at, assigned_at=wall_now,
                quality=fix["quality"], sats=fix["sats"], hdop=float(fix["hdop"]))


def mesh_position(contact):
    """Select a node position, otherwise an explicitly recorded receiver fix.

    A stored genuine observation remains useful as historical evidence; its
    age is not refreshed or rechecked against the current clock here.
    """
    position = node_position(contact)
    if position is not None:
        return position
    if not isinstance(contact, Mapping):
        return None
    receiver = contact.get("receiver_position")
    coordinates = _coordinates(receiver)
    if (coordinates is None or receiver.get("source") != "receiver_gps"
            or receiver.get("kind") not in _OBSERVATION_KINDS or not _credible_metrics(receiver)):
        return None
    observed_at = _number(receiver.get("observed_at"))
    assigned_at = _number(receiver.get("assigned_at"))
    if (observed_at is None or assigned_at is None or observed_at <= 0
            or assigned_at < observed_at):
        return None
    source = "receiver_gps" if receiver["kind"] == "reception" else "receiver_assignment"
    return dict(lat=coordinates[0], lon=coordinates[1], source=source,
                observed_at=observed_at, assigned_at=assigned_at)
