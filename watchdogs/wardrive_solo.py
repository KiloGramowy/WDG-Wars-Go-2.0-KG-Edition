"""JanOS SOLO UART v1 controller, using the application's sole ESP arbiter.

No serial object, scan scheduler, GPS policy, persistence or XP lives here.
See firmware/wardriving-solo/CONTRACT.md for the bounded source event format.
"""

from datetime import datetime, timezone
import json
import math
import re
import secrets
import time


PREFIX = "@SOLO "
MAX_LINE_BYTES = 4096
ACK_TIMEOUT = 5.0
_TOKEN = re.compile(r"[0-9a-f]{16}\Z")
_MAC = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z")
KG_CHANNELS = frozenset((1, 6, 11, 36, 40, 44, 48))


def _uint(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _text(value, limit, *, empty=True):
    return isinstance(value, str) and (empty or bool(value)) and len(value.encode("utf-8")) <= limit


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_json_number(_value):
    raise ValueError("Non-finite JSON constant")


class WardriveSoloController:
    def __init__(self, start, stop, on_observation, *, cancel_pending=None,
                 notice=None, monotonic=time.monotonic, utc_clock=time.time):
        self._start, self._stop, self._on_observation = start, stop, on_observation
        self._cancel_pending = cancel_pending or (lambda: None)
        self._notice = notice or (lambda _message: None)
        self._monotonic, self._utc_clock = monotonic, utc_clock
        self.desired_wifi = self.desired_ble = False
        self.confirmed_wifi = self.confirmed_ble = False
        self.profile = "full"
        self.phase, self.state, self.last_error = "", "idle", ""
        self.capabilities = None
        self.boot_id = None
        self._pending = None
        self._active_session = None
        self._active_target = None
        self._rf_unconfirmed = False
        self._rf_session = None
        self._active_start_mono = None
        self._anchor = None
        self._last_seq = 0
        self._last_emitted = 0
        self._stop_deadline = None
        self.invalid_events = self.sequence_gaps = self.dropped_observations = 0

    @property
    def busy(self):
        return bool(self._pending or self._active_session or self._rf_unconfirmed
                    or self.confirmed_wifi or self.confirmed_ble)

    def request(self, wifi=None, ble=None, profile=None):
        if wifi is not None and type(wifi) is not bool or ble is not None and type(ble) is not bool:
            raise ValueError("SOLO WiFi/BLE choices must be booleans")
        if profile is not None and profile not in ("full", "kg"):
            raise ValueError("Unknown SOLO profile")
        target = (self.profile if profile is None else profile,
                  self.desired_wifi if wifi is None else wifi,
                  self.desired_ble if ble is None else ble)
        old = self.profile, self.desired_wifi, self.desired_ble
        self.profile, self.desired_wifi, self.desired_ble = target
        if not target[1] and not target[2]:
            return self.cancel()
        if target == old and (self._pending or self._active_target == target):
            return True
        self.last_error = ""
        if self._pending and self._pending["kind"] == "caps":
            return True  # Coalesce toggles into the one in-flight probe.
        self._cancel_pending()
        self._pending = None
        return self._queue("wardrive_solo_caps", "caps", "Check SOLO firmware")

    def _queue(self, command, kind, label, *, session=None, target=None):
        self._pending = dict(command=command, kind=kind, session=session, target=target,
                             sent_mono=None, sent_utc=None, deadline=None, actual_caps=False)
        self.state = "queued_" + kind
        try:
            accepted = self._start(command, "solo_control", label)
        except Exception:
            accepted = False
        if not accepted:
            self._fail("Could not queue SOLO operation; check ESP32 connection")
            return False
        return True

    def command_sent(self, command, monotonic=None, utc=None):
        pending = self._pending
        if pending is None or command != pending["command"]:
            return False
        mono = self._monotonic() if monotonic is None else monotonic
        wall = self._utc_clock() if utc is None else utc
        if not all(type(value) in (int, float) and math.isfinite(value) for value in (mono, wall)):
            self._fail("Cannot anchor SOLO receive clock", stop=pending["kind"] == "start")
            return False
        pending.update(sent_mono=mono, sent_utc=wall, deadline=mono + ACK_TIMEOUT)
        if pending["kind"] == "start":
            self._rf_unconfirmed, self._rf_session = True, pending["session"]
        self.state = "await_" + pending["kind"]
        return True

    def _fail(self, message, *, stop=False):
        self.last_error = message
        self.desired_wifi = self.desired_ble = False
        self._cancel_pending()
        self._pending = None
        self.state = "error"
        self._notice(message)
        if stop:
            self._request_stop()

    def _request_stop(self):
        try:
            accepted = self._stop()
        except Exception:
            accepted = False
        if accepted:
            self._stop_deadline = self._monotonic() + ACK_TIMEOUT
            self.state = "stopping"
        else:
            self.stop_unconfirmed()
        return bool(accepted)

    def cancel(self, stop=True):
        needs_stop = bool(self._active_session or self._rf_unconfirmed or (self._pending and
            self._pending["kind"] == "start" and self._pending["sent_mono"] is not None))
        self.desired_wifi = self.desired_ble = False
        self._cancel_pending()
        self._pending = None
        self.state = "stopping" if needs_stop else "idle"
        # Confirmed ON remains until the firmware's stopped event/universal
        # STOP ACK. The app still sees an owner while another tool queues STOP.
        return self._request_stop() if stop and needs_stop else True

    def stop_acknowledged(self):
        self.confirmed_wifi = self.confirmed_ble = False
        self.phase = ""
        self._active_session = self._active_target = None
        self._rf_unconfirmed, self._rf_session, self._active_start_mono = False, None, None
        self._stop_deadline = None
        if self._pending:
            self.state = ("queued_" if self._pending["sent_mono"] is None else "await_") + self._pending["kind"]
        elif not self.last_error:
            self.state = "idle"

    def stop_unconfirmed(self):
        self._stop_deadline = None
        self._fail("SOLO stop was not confirmed; new operation cancelled")

    def disconnected(self):
        self._cancel_pending()
        self._pending = self._active_session = self._active_target = None
        self._rf_unconfirmed, self._rf_session, self._active_start_mono = False, None, None
        self._anchor = self.capabilities = self.boot_id = None
        self.desired_wifi = self.desired_ble = False
        self.confirmed_wifi = self.confirmed_ble = False
        self.phase, self.state = "", "disconnected"
        self.last_error = "ESP32 disconnected; SOLO OFF"
        self._stop_deadline = None
        self._last_seq = self._last_emitted = 0

    def tick(self, now=None):
        now = self._monotonic() if now is None else now
        pending = self._pending
        if pending and pending["deadline"] is not None and now >= pending["deadline"]:
            if pending["kind"] == "caps":
                self._fail("SOLO firmware did not answer; compatible firmware required")
            else:
                self._fail("SOLO start was not confirmed; stop required", stop=True)
        if self._stop_deadline is not None and now >= self._stop_deadline:
            self.stop_unconfirmed()

    def _make_anchor(self, event, received):
        pending = self._pending
        if not pending or pending["sent_mono"] is None or received < pending["sent_mono"]:
            raise ValueError("No matching transmitted clock request")
        clock = event.get("clock_monotonic_us")
        if not _uint(clock):
            raise ValueError("Missing source clock")
        delay = received - pending["sent_mono"]
        anchor = dict(device_us=clock, host_mono=pending["sent_mono"] + delay / 2,
                      host_utc=pending["sent_utc"] + delay / 2, uncertainty=delay / 2)
        if self._anchor is None or anchor["uncertainty"] < self._anchor["uncertainty"]:
            self._anchor = anchor

    def handle_line(self, line, monotonic=None, utc=None):
        """Consume only SOLO frames; True also keeps malformed frames out of legacy parsers."""
        # JanOS leaves its exact CLI prompt without a newline before the first
        # asynchronous frame. Do not search arbitrary console text for JSON.
        if isinstance(line, str) and line.startswith("> " + PREFIX):
            line = line[2:]
        if not isinstance(line, str) or not line.startswith(PREFIX):
            return False
        try:
            if len(line) > MAX_LINE_BYTES or len(line.rstrip("\r\n").encode("utf-8")) > MAX_LINE_BYTES:
                raise ValueError("Oversized event")
            event = json.loads(line[len(PREFIX):], object_pairs_hook=_unique_object,
                               parse_constant=_invalid_json_number)
            received = self._monotonic() if monotonic is None else monotonic
            if (not isinstance(event, dict) or type(event.get("v")) is not int or event["v"] != 1
                    or not isinstance(event.get("boot_id"), str) or not _TOKEN.fullmatch(event["boot_id"])
                    or not _uint(event.get("emitted_monotonic_us"))
                    or type(received) not in (int, float) or not math.isfinite(received)):
                raise ValueError("Malformed event envelope")
            kind = event.get("event")
            if kind not in ("caps", "state", "observation", "stopped", "error"):
                raise ValueError("Unknown event")
            session = event.get("session")
            sessions = tuple(value for value in self._sessions() if value is not None)
            matches = ((kind == "caps" and self._pending is not None)
                or (kind == "state" and session in sessions)
                or (kind == "observation" and self._active_session is not None and session == self._active_session)
                or (kind == "stopped" and session in sessions)
                or (kind == "error" and bool(self._pending or sessions) and (not session or session in sessions)))
            if not matches:
                return True
            if self.boot_id is not None and event["boot_id"] != self.boot_id:
                self.disconnected()
                self.last_error = "ESP32 restarted; select SOLO again"
                self._notice(self.last_error)
                return True
            if self.boot_id is not None and event["emitted_monotonic_us"] < self._last_emitted:
                raise ValueError("Reordered source clock")
            if kind == "caps":
                self._caps(event, received)
            elif kind == "state":
                self._state_event(event, received)
            elif kind == "observation":
                self._observation(event, received)
            elif kind == "stopped":
                self._stopped(event)
            elif kind == "error":
                if not event.get("session") or event["session"] in self._sessions():
                    if not _text(event.get("code"), 64, empty=False) or not _text(event.get("message"), 256):
                        raise ValueError("Malformed firmware error")
                    if event["code"] == "restore_failed":
                        # Even an idle capability probe temporarily owns RF.
                        # Failed restoration needs the universal STOP barrier,
                        # despite having no accepted scan/session identity yet.
                        self._rf_unconfirmed = True
                    self._fail("SOLO firmware: " + event["message"],
                               stop=bool(self._active_session or self._rf_unconfirmed))
            if self.boot_id == event["boot_id"]:
                self._last_emitted = event["emitted_monotonic_us"]
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            self.invalid_events += 1
        return True

    def _sessions(self):
        return (self._active_session, self._rf_session,
                self._pending.get("session") if self._pending else None)

    def _caps(self, event, received):
        if not self._pending or self._pending["kind"] not in ("caps", "start"):
            return
        starting = self._pending["kind"] == "start"
        if event.get("supported") is not True or type(event.get("contract")) is not int or event["contract"] != 1:
            self._fail("Compatible SOLO firmware required; scanning remains OFF", stop=starting)
            return
        channels = event.get("channels")
        if (not isinstance(channels, list) or not channels or len(channels) > 196
                or any(type(value) is not int or not 1 <= value <= 196 for value in channels)
                or len(set(channels)) != len(channels)
                or type(event.get("max_line_bytes")) is not int or not 1 <= event["max_line_bytes"] <= MAX_LINE_BYTES
                or not _text(event.get("firmware"), 64, empty=False)
                or not _text(event.get("sdk"), 128, empty=False)
                or not isinstance(event.get("skipped"), list) or not isinstance(event.get("country"), dict)):
            raise ValueError("Malformed capabilities")
        self._anchor = None  # A new explicit probe starts a new clock origin.
        self._make_anchor(event, received)
        self.boot_id = event["boot_id"]
        self.capabilities = event
        if self.desired_wifi and self.profile == "kg" and not KG_CHANNELS.issubset(channels):
            self._fail("KG SETTINGS channels unavailable; scanning remains OFF", stop=starting)
            return
        if starting:
            # ensure_wifi_mode may change driver country/configuration. This
            # fresh start capability replaces the idle probe, without issuing
            # another start or creating another observation identity namespace.
            self._pending["actual_caps"] = True
            return
        self._pending = None
        if not self.desired_wifi and not self.desired_ble:
            self.state = "idle"
            return
        session = secrets.token_hex(8)
        target = self.profile, self.desired_wifi, self.desired_ble
        command = f"wardrive_solo {session} {self.profile} {int(self.desired_wifi)} {int(self.desired_ble)}"
        self._queue(command, "start", "Wardriving SOLO", session=session, target=target)

    def _state_event(self, event, received):
        session = event.get("session")
        pending = self._pending
        starting = bool(pending and pending["kind"] == "start" and session == pending["session"])
        target = pending["target"] if starting else self._active_target if session == self._active_session else None
        if target is None:
            return
        if (starting and not pending["actual_caps"]
                or not _TOKEN.fullmatch(str(session)) or event.get("profile") != target[0]
                or type(event.get("wifi")) is not bool or type(event.get("ble")) is not bool
                or (event["wifi"], event["ble"]) != target[1:]
                or event.get("phase") not in ("wifi", "ble", "pause")
                or event["phase"] == "pause" and starting
                or event["phase"] != "pause" and not event[event["phase"]]
                or not _uint(event.get("cycles"))
                or not _uint(event.get("clock_monotonic_us"))):
            raise ValueError("State does not confirm requested hardware mode")
        if starting:
            self._make_anchor(event, received)
            self._active_session, self._active_target = session, target
            self._active_start_mono = pending["sent_mono"]
            self._rf_unconfirmed, self._rf_session = False, session
            self._last_seq = 0
            self._pending = None
        self.confirmed_wifi, self.confirmed_ble = event["wifi"], event["ble"]
        self.phase = event["phase"]
        if self.state not in ("stopping", "error"):
            self.state = "running" if not self._pending else (
                "queued_" if self._pending["sent_mono"] is None else "await_") + self._pending["kind"]

    def _observation(self, event, received):
        if self._anchor is None or event.get("session") != self._active_session:
            return
        protocol, seq = event.get("protocol"), event.get("seq")
        capture = event.get("capture_monotonic_us")
        if (protocol not in ("wifi", "ble") or not self._active_target[(1 if protocol == "wifi" else 2)]
                or not _uint(seq) or seq == 0 or not _uint(capture)
                or capture > event["emitted_monotonic_us"]
                or not _MAC.fullmatch(str(event.get("mac", "")))
                or type(event.get("rssi")) is not int or not -128 <= event["rssi"] <= 127):
            raise ValueError("Malformed observation")
        if seq <= self._last_seq:
            return
        if protocol == "wifi":
            # Firmware reports the AP's advertised primary channel. A frame
            # received while tuned to a KG channel can belong to an adjacent
            # channel, so this is not the receiver's tuning/capability list.
            if (not _text(event.get("ssid"), 32) or not _text(event.get("auth"), 64)
                    or type(event.get("channel")) is not int
                    or not 1 <= event["channel"] <= 196):
                raise ValueError("Malformed WiFi observation")
        elif not _text(event.get("name"), 32):
            raise ValueError("Malformed BLE observation")
        delta = (capture - self._anchor["device_us"]) / 1_000_000
        observed = self._anchor["host_mono"] + delta
        if (observed > received + self._anchor["uncertainty"] + 0.000001
                or observed < self._active_start_mono - self._anchor["uncertainty"] - 0.000001):
            raise ValueError("Capture clock lies outside this transmitted session")
        # An accepted midpoint estimate can lead RX within its uncertainty,
        # but the physical capture cannot happen after receiving the frame.
        # Keep the source clocks intact and bound both host clocks together.
        receive_correction = max(0.0, observed - received)
        observed = min(observed, received)
        wall = self._anchor["host_utc"] + delta - receive_correction
        record = dict(event, identity=(event["boot_id"], event["session"], seq),
            observed_at=observed,
            captured_at=datetime.fromtimestamp(wall, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            clock_source="host_anchor", clock_uncertainty_s=self._anchor["uncertainty"],
            clock_receive_correction_s=receive_correction)
        try:
            saved = self._on_observation(record)
        except Exception:
            saved = False
        if saved is False:
            self._fail("Cannot save SOLO observation; stop required", stop=True)
        else:
            self.sequence_gaps += seq - self._last_seq - 1
            self._last_seq = seq

    def _stopped(self, event):
        session = event.get("session")
        if not isinstance(session, str) or not _TOKEN.fullmatch(session):
            raise ValueError("Malformed stop session")
        if session not in (self._active_session, self._rf_session):
            return
        if not _uint(event.get("last_seq")) or not _uint(event.get("dropped")) or event["last_seq"] < self._last_seq:
            raise ValueError("Malformed stop summary")
        self.dropped_observations += event["dropped"]
        if event["dropped"]:
            self._notice(f"SOLO stopped: {event['dropped']} observation(s) dropped by firmware")
        self.stop_acknowledged()
