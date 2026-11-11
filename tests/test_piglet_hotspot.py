"""Collection network ownership/lifecycle regressions; no real network commands."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from watchdogs.piglet_hotspot import (ConcurrentHotspot, HotspotError,
                                    PigletCollectionController, _atomic_json,
                                    pairing_settings, supports_combination)


STA_UUID = "11223344-1122-3344-5566-112233445566"
PAIRING = {"ssid": "Collection-Test", "password": "private-test-password",
           "token": "f" * 64}
COMBINATIONS = """Wiphy phy0
valid interface combinations:
 * #{ managed } <= 1, #{ AP } <= 1, #{ P2P-device } <= 1,
   total <= 3, #channels <= 1
Band 1:
 * 2437 MHz [6] (20.0 dBm)
 * 5180 MHz [36] (20.0 dBm)
"""


class NetworkRunner:
    """Stateful kernel/NM model. An unexpected command fails the test."""
    def __init__(self):
        self.calls = []
        self.ap = False
        self.alias = ""
        self.profiles = {}
        self.ap_ip = False
        self.channel, self.frequency = 6, 2437
        self.connected, self.managed = True, True
        self.connection = STA_UUID
        self.sta_ip = "192.168.22.9"
        self.defaults = [{"dst": "default", "gateway": "192.168.22.1",
                          "dev": "wlan0", "metric": 600}]
        self.inventory = "phy#0\n\tInterface wlan0\n\t\ttype managed\n\tInterface p2p-dev-wlan0\n\t\ttype P2P-device\n"
        self.phy_info = COMBINATIONS
        self.extra_addresses = []
        self.fail = None
        self.on_up = None
        self.suppress_ap_ip = False

    def __call__(self, command, **kwargs):
        args = [Path(command[0]).name, *command[1:]]
        self.calls.append((args, kwargs))
        if self.fail is not None:
            failure = self.fail(args)
            if isinstance(failure, BaseException):
                raise failure
            if failure:
                return subprocess.CompletedProcess(command, 1, "", "sensitive-test-diagnostic")
        rc, value = 0, ""
        if args == ["iw", "dev", "wlan0", "info"]:
            value = f"Interface wlan0\n wiphy 0\n type {'managed' if self.managed else 'monitor'}\n channel {self.channel} ({self.frequency} MHz)"
        elif args == ["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", "wlan0"]:
            value = self.connection
        elif args == ["iw", "dev", "wlan0", "link"]:
            value = "Connected to 00:11:22:33:44:55" if self.connected else "Not connected."
        elif args == ["ip", "-j", "address", "show", "dev", "wlan0"]:
            value = json.dumps([{"ifname": "wlan0", "addr_info": self._sta_addresses()}])
        elif args == ["ip", "-j", "route", "show", "default"]:
            value = json.dumps(self.defaults)
        elif args == ["iw", "phy", "phy0", "info"]:
            value = self.phy_info
        elif args == ["iw", "dev"]:
            value = self.inventory
        elif args == ["ip", "-j", "link", "show", "dev", "wdgpc0"]:
            rc, value = (0, json.dumps([{"ifname": "wdgpc0", "ifalias": self.alias}])) if self.ap else (1, "")
        elif args == ["ip", "-j", "address", "show"]:
            value = json.dumps([{"ifname": "wlan0", "addr_info": self._sta_addresses()}, *self.extra_addresses])
        elif args == ["iw", "dev", "wlan0", "interface", "add", "wdgpc0", "type", "__ap"]:
            if self.ap:
                rc = 1
            else:
                self.ap = True
        elif args[:7] == ["ip", "link", "set", "dev", "wdgpc0", "alias", args[-1]]:
            self.alias = args[-1]
        elif args[:3] == ["nmcli", "connection", "add"]:
            identifier = args[args.index("connection.uuid") + 1]
            self.profiles[identifier] = args[args.index("ifname") + 1]
        elif args[:5] == ["nmcli", "--wait", "15", "connection", "up"]:
            self.ap_ip = not self.suppress_ap_ip
            if self.on_up:
                self.on_up()
        elif args == ["ip", "-j", "address", "show", "dev", "wdgpc0"]:
            value = json.dumps([{"addr_info": [{"family": "inet", "local": "10.72.0.1", "prefixlen": 24}] if self.ap_ip else []}])
        elif args[:6] == ["nmcli", "-g", "connection.interface-name", "connection", "show", "uuid"]:
            identifier = args[-1]
            rc, value = (0, self.profiles[identifier]) if identifier in self.profiles else (1, "")
        elif args[:5] == ["nmcli", "--wait", "5", "connection", "down"]:
            self.ap_ip = False
        elif args[:4] == ["nmcli", "connection", "delete", "uuid"]:
            self.profiles.pop(args[-1], None)
        elif args == ["iw", "dev", "wdgpc0", "del"]:
            self.ap, self.alias, self.ap_ip = False, "", False
        else:
            raise AssertionError("Unexpected simulated command: " + repr(args))
        return subprocess.CompletedProcess(command, rc, value, "")

    def _sta_addresses(self):
        return ([{"family": "inet", "local": self.sta_ip, "prefixlen": 24}]
                if self.sta_ip else [])

    def mutations(self):
        return [args for args, _ in self.calls if
                (args[:3] == ["nmcli", "connection", "add"]
                 or "up" in args or "down" in args or "delete" in args
                 or "add" in args or "del" in args or args[:3] == ["ip", "link", "set"])]


class RealIwOutputTests(unittest.TestCase):
    def test_decimal_frequency_from_installed_iw_is_accepted_without_mutations(self):
        runner = NetworkRunner()
        runner.phy_info = runner.phy_info.replace("2437 MHz", "2437.0 MHz")
        with tempfile.TemporaryDirectory() as directory:
            backend = ConcurrentHotspot(directory, run=runner)
            self.assertEqual(backend.preflight()["frequency"], 2437)
        self.assertEqual(runner.mutations(), [])


class CombinationTests(unittest.TestCase):
    def test_existing_sta_p2p_and_one_ap_fit_one_advertised_combination(self):
        self.assertTrue(supports_combination(COMBINATIONS, ["managed", "P2P-device"]))
        self.assertTrue(supports_combination(COMBINATIONS, ["managed"]))

    def test_p2p_total_and_group_limits_are_all_enforced(self):
        for existing in (["managed", "managed"], ["managed", "P2P-client"],
                         ["managed", "AP", "P2P-device"]):
            with self.subTest(existing=existing):
                self.assertFalse(supports_combination(COMBINATIONS, existing))
        restricted = COMBINATIONS.replace("total <= 3", "total <= 2")
        self.assertFalse(supports_combination(restricted, ["managed", "P2P-device"]))

    def test_constraints_from_separate_combinations_are_not_merged(self):
        info = "valid interface combinations:\n * #{ managed } <= 1, total <= 1, #channels <= 1\n * #{ AP } <= 1, total <= 1, #channels <= 1"
        self.assertFalse(supports_combination(info, ["managed"]))

    def test_unadvertised_or_invalid_channel_combinations_refused(self):
        for info in ("Supported interface modes: managed AP", "valid interface combinations:",
                     COMBINATIONS.replace("#channels <= 1", "#channels <= 0")):
            self.assertFalse(supports_combination(info, ["managed"]))


class ConcurrentHotspotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runner = NetworkRunner()
        self.hotspot = ConcurrentHotspot(self.folder.name, run=self.runner, sleep=lambda _: None)

    def assert_sta_unchanged(self, old):
        self.assertEqual((self.runner.connection, self.runner.sta_ip, self.runner.defaults), old)
        for args in self.runner.mutations():
            self.assertNotIn(STA_UUID, args)
            self.assertFalse(args[:3] == ["nmcli", "device", "disconnect"])
            self.assertNotIn("set-channel", args)

    def test_success_owns_only_virtual_ap_and_preserves_router_connection_routes_ip(self):
        before = copy.deepcopy((self.runner.connection, self.runner.sta_ip, self.runner.defaults))
        result = self.hotspot.start(PAIRING)
        self.assertEqual(result, {"address": "10.72.0.1", "channel": 6, "interface": "wdgpc0"})
        self.assertTrue(self.hotspot.healthy())
        profile = next(args for args, _ in self.runner.calls if args[:3] == ["nmcli", "connection", "add"])
        for setting, value in (("connection.autoconnect", "no"), ("ipv4.never-default", "yes"),
                               ("wifi.channel", "6"), ("ipv6.method", "disabled")):
            self.assertEqual(profile[profile.index(setting)+1], value)
        self.assertTrue(self.hotspot.stop())
        self.assertFalse(self.runner.ap)
        self.assertEqual(self.runner.profiles, {})
        self.assertFalse(self.hotspot.lease_path.exists())
        self.assert_sta_unchanged(before)

    def test_five_ghz_ap_uses_same_sta_channel_without_channel_change(self):
        self.runner.channel, self.runner.frequency = 36, 5180
        self.hotspot.start(PAIRING)
        profile = next(args for args, _ in self.runner.calls if args[:3] == ["nmcli", "connection", "add"])
        self.assertEqual(profile[profile.index("wifi.band")+1], "a")
        self.assertEqual(profile[profile.index("wifi.channel")+1], "36")
        self.hotspot.stop()

    def test_start_twice_does_not_launch_a_second_ap(self):
        self.hotspot.start(PAIRING)
        mutations = len(self.runner.mutations())
        with self.assertRaises(HotspotError):
            self.hotspot.start(PAIRING)
        self.assertEqual(len(self.runner.mutations()), mutations)
        self.hotspot.stop()

    def test_disconnected_nonmanaged_or_unaddressed_sta_refused_before_mutation(self):
        for attribute, value in (("connected", False), ("managed", False),
                                 ("connection", "--"), ("sta_ip", "")):
            with self.subTest(attribute=attribute):
                original = getattr(self.runner, attribute)
                setattr(self.runner, attribute, value)
                with self.assertRaises(HotspotError):
                    self.hotspot.start(PAIRING)
                self.assertEqual(self.runner.mutations(), [])
                self.assertFalse(self.hotspot.lease_path.exists())
                setattr(self.runner, attribute, original)

    def test_unusable_regulatory_or_missing_frequency_refused_without_mutation(self):
        for flag in ("disabled", "no IR", "radar detection"):
            with self.subTest(flag=flag):
                self.runner.phy_info = COMBINATIONS.replace("2437 MHz [6] (20.0 dBm)", "2437 MHz [6] ("+flag+")")
                with self.assertRaises(HotspotError):
                    self.hotspot.start(PAIRING)
                self.assertEqual(self.runner.mutations(), [])
        self.runner.phy_info = COMBINATIONS.replace("2437 MHz", "2442 MHz")
        with self.assertRaises(HotspotError):
            self.hotspot.start(PAIRING)

    def test_missing_sta_p2p_ap_combination_does_not_attempt_takeover(self):
        self.runner.phy_info = COMBINATIONS.replace("#{ P2P-device } <= 1,", "")
        with self.assertRaisesRegex(HotspotError, "combination"):
            self.hotspot.start(PAIRING)
        self.assertEqual(self.runner.mutations(), [])

    def test_subnet_overlap_and_existing_interface_are_never_adopted(self):
        self.runner.extra_addresses = [{"addr_info": [{"family": "inet", "local": "10.72.0.22", "prefixlen": 24}]}]
        with self.assertRaisesRegex(HotspotError, "overlaps"):
            self.hotspot.start(PAIRING)
        self.runner.extra_addresses = []
        self.runner.ap, self.runner.alias = True, "foreign-owner"
        with self.assertRaisesRegex(HotspotError, "already in use"):
            self.hotspot.start(PAIRING)
        self.assertTrue(self.runner.ap)
        self.assertEqual(self.runner.mutations(), [])

    def test_changed_sta_during_activation_unwinds_owned_resources(self):
        self.runner.on_up = lambda: setattr(self.runner, "channel", 11)
        with self.assertRaisesRegex(HotspotError, "STA changed"):
            self.hotspot.start(PAIRING)
        self.assertFalse(self.runner.ap)
        self.assertFalse(self.runner.profiles)
        self.assertFalse(self.hotspot.lease_path.exists())

    def test_no_ap_address_after_nm_success_is_failure_with_cleanup(self):
        self.runner.suppress_ap_ip = True
        with self.assertRaisesRegex(HotspotError, "local address"):
            self.hotspot.start(PAIRING)
        self.assertFalse(self.runner.ap)
        self.assertFalse(self.runner.profiles)

    def test_nm_error_does_not_expose_password_token_or_raw_diagnostics(self):
        self.runner.fail = lambda args: args[:3] == ["nmcli", "connection", "add"]
        with self.assertRaises(HotspotError) as caught:
            self.hotspot.start(PAIRING)
        self.assertFalse(self.runner.ap)
        for secret in (*PAIRING.values(), "sensitive-test-diagnostic"):
            self.assertNotIn(secret, str(caught.exception))

    def test_alias_assignment_error_removes_only_just_created_unaliased_interface(self):
        self.runner.fail = lambda args: args[:3] == ["ip", "link", "set"]
        with self.assertRaises(HotspotError):
            self.hotspot.start(PAIRING)
        self.assertFalse(self.runner.ap)
        self.assertFalse(self.hotspot.lease_path.exists())

    def test_preexisting_owned_lease_is_recovered_before_fresh_start(self):
        self.hotspot.start(PAIRING)
        old = json.loads(self.hotspot.lease_path.read_text())
        replacement = ConcurrentHotspot(self.folder.name, run=self.runner)
        replacement.start(PAIRING)
        fresh = json.loads(replacement.lease_path.read_text())
        self.assertNotEqual(old["uuid"], fresh["uuid"])
        self.assertNotIn(old["uuid"], self.runner.profiles)
        self.assertIn(fresh["uuid"], self.runner.profiles)
        replacement.stop()

    def test_foreign_alias_prevents_profile_or_interface_deletion(self):
        self.hotspot.start(PAIRING)
        self.runner.alias = "foreign-owner"
        count = len(self.runner.calls)
        with self.assertRaisesRegex(HotspotError, "ownership changed"):
            self.hotspot.stop()
        self.assertTrue(self.runner.ap)
        self.assertTrue(self.hotspot.lease_path.exists())
        self.assertFalse(any("delete" in args or "del" in args for args, _ in self.runner.calls[count:]))

    def test_foreign_profile_binding_is_not_deleted_even_with_own_interface(self):
        self.hotspot.start(PAIRING)
        identifier = self.hotspot._lease["uuid"]
        self.runner.profiles[identifier] = "wlan0"
        with self.assertRaisesRegex(HotspotError, "profile ownership changed"):
            self.hotspot.stop()
        self.assertEqual(self.runner.profiles[identifier], "wlan0")
        self.assertTrue(self.runner.ap)

    def test_malformed_lease_is_not_used_as_authority_to_delete_resources(self):
        self.hotspot.lease_path.write_text(json.dumps({"interface": "wlan0", "uuid": STA_UUID,
                                                       "alias": "wdg-piglet:"+STA_UUID}))
        with self.assertRaisesRegex(HotspotError, "Invalid AP ownership"):
            self.hotspot.stop()
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(self.hotspot.lease_path.exists())

    def test_health_detects_external_sta_changes_and_stop_does_not_restore_old_routes(self):
        self.hotspot.start(PAIRING)
        self.runner.defaults[0]["metric"] = 701
        self.assertFalse(self.hotspot.healthy())
        self.hotspot.stop()
        self.assertEqual(self.runner.defaults[0]["metric"], 701)
        self.assertEqual(self.runner.connection, STA_UUID)

    def test_cancel_before_create_changes_no_network_and_clears_empty_lease(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(HotspotError, "cancelled"):
            self.hotspot.start(PAIRING, cancel)
        self.assertEqual(self.runner.mutations(), [])
        self.assertFalse(self.hotspot.lease_path.exists())

    def test_cancel_after_profile_creation_unwinds_before_activation(self):
        cancel = threading.Event()
        def cancel_after_add(args):
            if args[:3] == ["nmcli", "connection", "add"]:
                cancel.set()
            return False
        self.runner.fail = cancel_after_add
        with self.assertRaisesRegex(HotspotError, "cancelled"):
            self.hotspot.start(PAIRING, cancel)
        self.assertFalse(self.runner.ap)
        self.assertFalse(self.runner.profiles)
        self.assertFalse(any(args[:5] == ["nmcli", "--wait", "15", "connection", "up"] for args, _ in self.runner.calls))

    def test_command_timeout_is_sanitized_and_all_calls_have_finite_timeout(self):
        self.runner.fail = lambda args: subprocess.TimeoutExpired(args, 15, stderr="private-test-password")
        with self.assertRaisesRegex(HotspotError, "timed out") as caught:
            self.hotspot.start(PAIRING)
        self.assertNotIn(PAIRING["password"], str(caught.exception))
        for _, kwargs in self.runner.calls:
            self.assertGreater(kwargs["timeout"], 0)
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
            self.assertTrue(kwargs["capture_output"])


class PairingSettingsTests(unittest.TestCase):
    def test_credentials_are_persistent_locally_generated_and_written_private(self):
        with tempfile.TemporaryDirectory() as folder:
            real_open = os.open
            with patch("watchdogs.piglet_hotspot.os.open", wraps=real_open) as opened:
                first = pairing_settings(folder)
            second = pairing_settings(folder)
            self.assertEqual(first, second)
            self.assertEqual(set(first), {"ssid", "password", "token"})
            self.assertRegex(first["token"], r"^[a-f0-9]{64}$")
            self.assertTrue(any(call.args[2:3] == (0o600,) for call in opened.call_args_list))
            with tempfile.TemporaryDirectory() as other:
                self.assertNotEqual(first, pairing_settings(other))

    def test_invalid_settings_are_retained_and_not_echoed_in_error(self):
        cases = [dict(PAIRING, password="short"), dict(PAIRING, token="not-a-token"),
                 dict(PAIRING, ssid="a"*33), dict(PAIRING, extra="unexpected"), []]
        for value in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/"pairing.json"
                original = json.dumps(value).encode()
                path.write_bytes(original)
                with self.assertRaises(HotspotError) as caught:
                    pairing_settings(folder)
                self.assertEqual(path.read_bytes(), original)
                self.assertNotIn(PAIRING["password"], str(caught.exception))
                self.assertNotIn(PAIRING["token"], str(caught.exception))

    def test_atomic_replacement_failure_keeps_previous_pairing_and_no_partial_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"pairing.json"
            _atomic_json(path, PAIRING)
            before = path.read_bytes()
            with patch("watchdogs.piglet_hotspot.os.replace", side_effect=OSError("simulated disk failure")):
                with self.assertRaises(OSError):
                    _atomic_json(path, dict(PAIRING, ssid="Replacement"))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["pairing.json"])


class FakeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.overview = {"busy": False, "active_devices": [], "records": 3}
        self.close_calls = 0
        self.cancel_calls = 0
        self.status_calls = 0
        self.closed = self.cancelled = False
        self.close_ok = True
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_release.set()

    def status_overview(self):
        self.status_calls += 1
        if self.closed:
            raise RuntimeError("Store is closed")
        return copy.deepcopy(self.overview)

    def cancel_processing(self):
        self.cancel_calls += 1
        self.cancelled = True

    def close(self):
        if self.closed:
            return True
        self.close_calls += 1
        self.close_entered.set()
        if not self.close_release.wait(2):
            raise AssertionError("Test did not release fake worker cleanup")
        self.closed = self.close_ok
        return self.close_ok


class FakeBackend:
    def __init__(self):
        self.active = False
        self.entered = threading.Event()
        self.release_start = threading.Event()
        self.release_start.set()
        self.start_calls = self.stop_calls = self.health_calls = 0
        self.stop_ok = True
        self.healthy_value = True
        self.error = None

    def start(self, settings, cancel):
        self.start_calls += 1
        self.entered.set()
        if not self.release_start.wait(2):
            raise AssertionError("Test did not release fake backend START")
        if self.error:
            raise self.error
        self.active = True
        return {"address": "10.72.0.1", "channel": 6, "interface": "wdgpc0"}

    def healthy(self):
        self.health_calls += 1
        return self.healthy_value

    def stop(self):
        self.stop_calls += 1
        self.active = False
        return self.stop_ok


class FakeService:
    def __init__(self, store, token, address, *, on_finish):
        self.store, self.token, self.address = store, token, address
        self.on_finish = on_finish
        self.started = threading.Event()
        self.stop_ok = True
        self.stop_calls = 0
        self.start_error = None

    def start(self):
        self.started.set()
        if self.start_error:
            raise self.start_error

    def stop(self):
        self.stop_calls += 1
        return self.stop_ok


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store, self.backend = FakeStore(self.folder.name), FakeBackend()
        self.services = []
        self.service_error = None
        def factory(*args, **kwargs):
            service = FakeService(*args, **kwargs)
            service.start_error = self.service_error
            self.services.append(service)
            return service
        with patch("watchdogs.piglet_collection.PigletCollectionStore", return_value=self.store):
            self.controller = PigletCollectionController(object(), backend=self.backend, service_factory=factory)
        self.addCleanup(self._release)

    def _release(self):
        self.backend.release_start.set()
        self.controller.stop(wait=True, timeout=3)

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Fake lifecycle did not reach expected state: " + repr(self.controller.snapshot()))
            time.sleep(.005)

    def active(self):
        self.assertTrue(self.controller.start())
        self.wait_for(lambda: self.controller.snapshot()["state"] == "active")

    def test_start_stop_and_close_release_receiver_before_backend_then_store(self):
        self.active()
        self.assertFalse(self.controller.start())
        service = self.services[0]
        order = []
        service.stop = lambda: order.append("receiver") or True
        self.backend.stop = lambda: order.append("backend") or True
        self.assertTrue(self.controller.close())
        self.assertEqual(order, ["receiver", "backend"])
        self.assertEqual(self.store.close_calls, 1)
        self.assertEqual(self.controller.snapshot()["state"], "stopped")

    def test_stop_during_start_cannot_create_receiver_or_a_second_worker(self):
        self.backend.release_start.clear()
        self.assertTrue(self.controller.start())
        self.assertTrue(self.backend.entered.wait(1))
        self.assertTrue(self.controller.stop(wait=False))
        self.assertFalse(self.controller.start())
        self.backend.release_start.set()
        self.assertTrue(self.controller.stop(timeout=2))
        self.assertEqual(self.services, [])
        self.assertEqual(self.backend.start_calls, 1)
        self.assertEqual(self.backend.stop_calls, 1)

    def test_stop_timeout_reports_incomplete_without_closing_store(self):
        self.backend.release_start.clear()
        self.controller.start()
        self.assertTrue(self.backend.entered.wait(1))
        self.assertFalse(self.controller.stop(timeout=.01))
        self.assertEqual(self.store.close_calls, 0)
        self.backend.release_start.set()
        self.assertTrue(self.controller.stop(timeout=2))

    def test_backend_failure_and_partial_receiver_start_are_cleaned_and_sanitized(self):
        self.service_error = OSError("secret-test-error " + PAIRING["password"])
        self.controller.start()
        self.wait_for(lambda: self.controller.snapshot()["state"] == "error")
        self.assertEqual(self.services[0].stop_calls, 1)
        self.assertEqual(self.backend.stop_calls, 1)
        self.assertNotIn(PAIRING["password"], self.controller.snapshot()["message"])
        self.assertTrue(self.controller.stop())

    def test_backend_start_failure_never_starts_receiver_and_releases_partial_resources(self):
        self.backend.error = HotspotError("Concurrent AP is not supported")
        self.controller.start()
        self.wait_for(lambda: self.controller.snapshot()["state"] == "error")
        self.assertEqual(self.services, [])
        self.assertEqual(self.backend.stop_calls, 1)
        self.assertTrue(self.controller.stop())

    def test_network_health_failure_stops_owned_services_with_actionable_message(self):
        self.backend.healthy_value = False
        self.active()
        self.wait_for(lambda: self.controller.snapshot()["state"] == "error")
        self.assertIn("STA/channel changed", self.controller.snapshot()["message"])
        self.assertEqual(self.services[0].stop_calls, 1)
        self.assertFalse(self.backend.active)

    def test_finish_requires_explicit_permission_and_no_busy_or_other_device(self):
        self.active()
        self.services[0].on_finish({"shutdown_allowed": False})
        self.assertFalse(self.controller._finished.is_set())
        self.store.overview.update(busy=True, active_devices=["other-device"])
        self.services[0].on_finish({"shutdown_allowed": True})
        self.wait_for(lambda: self.backend.health_calls > 0)
        self.assertEqual(self.controller.snapshot()["state"], "active")
        self.store.overview.update(busy=False)
        self.wait_for(lambda: not self.controller._finished.is_set())
        self.assertEqual(self.controller.snapshot()["state"], "active")
        self.store.overview.update(busy=False, active_devices=[])
        self.services[0].on_finish({"shutdown_allowed": True})
        self.wait_for(lambda: self.controller.snapshot()["state"] == "complete")
        self.assertFalse(self.backend.active)
        self.assertEqual(self.services[0].stop_calls, 1)

    def test_one_successful_finish_is_not_lost_when_first_status_snapshot_is_busy(self):
        self.active()
        self.store.overview.update(busy=True, active_devices=[])
        self.services[0].on_finish({"shutdown_allowed": True})
        # Observe at least one loop with the busy snapshot. No second finish
        # callback will arrive after the sender receives its successful reply.
        self.wait_for(lambda: self.backend.health_calls > 0)
        self.assertEqual(self.controller.snapshot()["state"], "active")
        self.store.overview.update(busy=False, active_devices=[])
        self.wait_for(lambda: self.controller.snapshot()["state"] == "complete")
        self.assertFalse(self.backend.active)

    def test_receiver_cleanup_failure_keeps_backend_and_store_available_for_recovery(self):
        self.active()
        self.services[0].stop_ok = False
        self.assertFalse(self.controller.stop())
        self.assertEqual(self.backend.stop_calls, 0)
        self.assertEqual(self.store.close_calls, 0)
        self.assertFalse(self.controller.close())
        self.assertFalse(self.controller.start())
        self.assertEqual(self.backend.start_calls, 1)
        self.assertIn("cleanup incomplete", self.controller.snapshot()["message"])

    def test_backend_cleanup_failure_is_not_success_just_because_active_flag_is_false(self):
        self.active()
        self.backend.stop_ok = False
        self.assertFalse(self.controller.stop())
        self.assertFalse(self.backend.active)
        self.assertFalse(self.controller.close())
        self.assertEqual(self.store.close_calls, 0)
        self.assertFalse(self.controller.start())

    def test_cleanup_exception_retains_uncertain_resources_and_blocks_store_close(self):
        self.active()
        def partial_cleanup():
            self.backend.active = False
            raise OSError("Cannot unlink retained lease")
        self.backend.stop = partial_cleanup
        self.assertFalse(self.controller.stop())
        self.assertFalse(self.controller.close())
        self.assertEqual(self.store.close_calls, 0)
        self.assertFalse(self.controller.start())

    def test_snapshot_never_contains_pairing_password_or_token(self):
        self.active()
        snapshot = json.dumps(self.controller.snapshot())
        self.assertNotIn(self.controller.settings["password"], snapshot)
        self.assertNotIn(self.controller.settings["token"], snapshot)
        self.assertEqual(self.controller.snapshot()["progress"]["records"], 3)

    def test_failed_pairing_initialization_closes_created_store_before_next_attempt(self):
        store = FakeStore(self.folder.name)
        with patch("watchdogs.piglet_collection.PigletCollectionStore", return_value=store), \
                patch("watchdogs.piglet_hotspot.pairing_settings", side_effect=HotspotError("Invalid pairing")):
            with self.assertRaisesRegex(HotspotError, "Invalid pairing"):
                PigletCollectionController(object(), backend=self.backend)
        self.assertEqual(store.close_calls, 1)
        self.assertEqual(self.backend.start_calls, 0)

    def test_failed_backend_initialization_closes_created_store(self):
        store = FakeStore(self.folder.name)
        with patch("watchdogs.piglet_collection.PigletCollectionStore", return_value=store), \
                patch("watchdogs.piglet_hotspot.ConcurrentHotspot", side_effect=ValueError("Invalid backend")):
            with self.assertRaisesRegex(ValueError, "Invalid backend"):
                PigletCollectionController(object())
        self.assertEqual(store.close_calls, 1)


class ControllerCancellationTests(unittest.TestCase):
    setUp = ControllerTests.setUp
    _release = ControllerTests._release
    wait_for = ControllerTests.wait_for
    active = ControllerTests.active
    # Only the focused tests below are repeated; inherited lifecycle tests stay
    # in ControllerTests' existing suite.
    def test_idle_pending_cancel_closes_worker_without_starting_hotspot(self):
        self.store.overview.update(pending_files=1, records=23)
        self.controller.snapshot()
        self.assertTrue(self.controller.cancel())
        self.wait_for(lambda:self.controller.snapshot()["state"]=="cancelled")
        self.assertTrue(self.store.cancelled)
        self.assertEqual(self.store.close_calls,1)
        self.assertEqual(self.backend.start_calls,0)
        self.assertEqual(self.services,[])
        self.assertEqual(self.controller.snapshot()["progress"]["records"],23)
        self.assertTrue(self.controller.stop())

    def test_cancel_active_releases_http_then_backend_then_worker(self):
        self.active()
        order=[]
        service=self.services[0]
        service.stop=lambda:order.append("receiver") or True
        self.backend.stop=lambda:order.append("backend") or True
        original=self.store.close
        def close_worker():
            order.append("worker")
            return original()
        self.store.close=close_worker
        self.assertTrue(self.controller.cancel())
        self.wait_for(lambda:self.controller.snapshot()["state"]=="cancelled")
        self.assertEqual(order,["receiver","backend","worker"])
        self.assertTrue(self.store.closed)

    def test_cancel_snapshot_uses_cache_while_worker_closes_and_after_closed(self):
        self.controller.snapshot()
        self.store.close_release.clear()
        self.controller.cancel()
        self.assertTrue(self.store.close_entered.wait(1))
        calls=self.store.status_calls
        self.assertEqual(self.controller.snapshot()["progress"]["records"],3)
        self.assertEqual(self.store.status_calls,calls)
        self.assertFalse(self.controller.start())
        self.store.close_release.set()
        self.wait_for(lambda:self.controller.snapshot()["state"]=="cancelled")
        self.assertEqual(self.store.status_calls,calls)

    def test_start_after_cancel_reopens_store_and_resets_cancel_state(self):
        self.controller.cancel()
        self.wait_for(lambda:self.controller.snapshot()["state"]=="cancelled")
        self.controller.stop()
        old=self.store
        new=FakeStore(self.folder.name)
        with patch("watchdogs.piglet_collection.PigletCollectionStore",return_value=new) as factory:
            self.assertTrue(self.controller.start())
            self.wait_for(lambda:self.controller.snapshot()["state"]=="active")
        factory.assert_called_once_with(self.controller._loot,state_dir=self.controller._state_dir)
        self.assertIs(self.controller.store,new)
        self.assertTrue(old.closed)
        self.assertFalse(self.controller.cancel_requested)
        self.assertFalse(new.cancelled)
        self.assertEqual(self.services[-1].store,new)

    def test_failed_worker_close_is_retained_and_blocks_start_until_cancel_retry(self):
        self.store.close_ok=False
        self.controller.cancel()
        self.wait_for(lambda:self.controller.snapshot()["state"]=="error")
        self.assertFalse(self.store.closed)
        self.assertFalse(self.controller._released)
        self.assertFalse(self.controller.start())
        self.assertFalse(self.controller.close())
        self.assertIn("cleanup incomplete",self.controller.snapshot()["message"])
        self.store.close_ok=True
        self.controller.cancel()
        self.wait_for(lambda:self.controller.snapshot()["state"]=="cancelled")
        self.assertTrue(self.store.closed)
        self.assertEqual(self.store.close_calls,2)

    def test_repeated_cancel_is_idempotent_after_success(self):
        self.controller.cancel()
        self.wait_for(lambda:self.controller.snapshot()["state"]=="cancelled")
        self.controller.stop()
        calls=(self.store.close_calls,self.backend.stop_calls)
        self.controller.cancel()
        self.controller.stop()
        self.assertEqual((self.store.close_calls,self.backend.stop_calls),calls)
        self.assertEqual(self.controller.snapshot()["state"],"cancelled")

    def test_regular_close_tracks_closed_store_and_snapshot_does_not_read_sql(self):
        self.controller.snapshot()
        self.assertTrue(self.controller.close())
        calls=self.store.status_calls
        self.assertEqual(self.controller.snapshot()["progress"]["records"],3)
        self.assertEqual(self.store.status_calls,calls)
        self.assertTrue(self.controller._store_closed)

    def test_regular_close_failure_blocks_start_but_can_retry_worker_cleanup(self):
        self.store.close_ok=False
        self.assertFalse(self.controller.close())
        self.assertFalse(self.controller.start())
        self.store.close_ok=True
        self.assertTrue(self.controller.close())
        self.assertTrue(self.store.closed)


class ControllerPeerActivityTests(unittest.TestCase):
    setUp = ControllerTests.setUp
    _release = ControllerTests._release
    wait_for = ControllerTests.wait_for
    active = ControllerTests.active

    def test_pending_work_and_legacy_service_never_imply_connected_device(self):
        self.store.overview["active_devices"]=["historical-pending-device"]
        self.active()
        progress=self.controller.snapshot()["progress"]
        self.assertEqual(progress["active_devices"],["historical-pending-device"])
        self.assertEqual(progress["connected_devices"],[])

    def test_snapshot_uses_live_service_activity_and_clears_it_after_stop(self):
        self.active()
        self.store.overview["active_devices"]=[]
        self.services[0].connected_devices=lambda:["piglet-123456789abc"]
        self.assertEqual(self.controller.snapshot()["progress"]["connected_devices"],["piglet-123456789abc"])
        self.assertTrue(self.controller.stop())
        self.assertEqual(self.controller.snapshot()["progress"]["connected_devices"],[])
        self.assertTrue(self.controller.close())
        self.assertEqual(self.controller.snapshot()["progress"]["connected_devices"],[])


if __name__ == "__main__":
    unittest.main()
