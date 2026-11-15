"""Synthetic portal snapshots, account isolation and real controller scheduling."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from watchdogs.white_rabbit import MAX_RABBITS, REFRESH_SECONDS, WhiteRabbit, _identity, _parse_snapshot


NOW = 2_000_000_000


def rabbit(ident=1, **changes):
    row = dict(id=ident, state="open", lat=12.25, lng=23.5,
               ends_at=NOW + 600, closes_at=NOW + 1200,
               claimed_at=None, winner=None, mine="none")
    row.update(changes)
    return row


def snapshot(*rows):
    return dict(ok=True, now=NOW, rabbits=list(rows))


class Portal:
    _api_key = "synthetic-key-A"
    _api_url = "https://portal.example.invalid/api/upload/"

    def __init__(self, payload=None):
        self.payload = snapshot(rabbit()) if payload is None else payload
        self.calls = 0

    def fetch_rabbit_snapshot(self):
        self.calls += 1
        return self.payload


class WhiteRabbitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.maps = Path(self.temp.name) / "maps"
        self.mono, self.wall = 100.0, float(NOW)
        self.controller = self.make_controller()
        self.portal = Portal()

    def make_controller(self):
        controller = WhiteRabbit(self.maps, clock=lambda: self.mono, wall_clock=lambda: self.wall)
        self.addCleanup(controller.close)
        return controller

    def finish(self, controller=None, portal=None):
        controller = controller or self.controller
        portal = portal or self.portal
        if controller._thread is not None:
            controller._thread.join(3)
            self.assertFalse(controller._thread.is_alive())
        return controller.poll(portal)

    def fetch(self):
        self.controller.poll(self.portal)
        return self.finish()

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds

    def cache_path(self, portal=None):
        return self.maps / "white_rabbits" / (_identity(portal or self.portal) + ".json")

    def test_without_account_neither_fetches_nor_creates_maps(self):
        self.portal._api_key = ""
        stats = self.controller.poll(self.portal)
        self.assertFalse(stats["configured"])
        self.assertEqual(self.portal.calls, 0)
        self.assertFalse(self.maps.exists())

    def test_startup_fetch_is_automatic_without_gps_or_auto_map_toggle(self):
        stats = self.fetch()
        self.assertEqual((self.portal.calls, stats["active"], stats["state"]), (1, 1, "online"))
        self.assertTrue(stats["cache_saved"])

    def test_requests_are_bounded_to_one_per_minute(self):
        self.fetch()
        self.advance(REFRESH_SECONDS - 0.1)
        self.controller.poll(self.portal)
        self.assertEqual(self.portal.calls, 1)
        self.advance(0.1)
        self.controller.poll(self.portal)
        self.finish()
        self.assertEqual(self.portal.calls, 2)

    def test_cache_excludes_credentials_and_unneeded_server_fields(self):
        self.portal.payload["rabbits"][0]["private_name"] = "DO NOT CACHE"
        self.fetch()
        raw = self.cache_path().read_text()
        self.assertNotIn(self.portal._api_key, raw)
        self.assertNotIn(self.portal._api_url, raw)
        self.assertNotIn("DO NOT CACHE", raw)
        self.assertEqual(set(json.loads(raw)["rabbits"][0]), {"id", "state", "lat", "lng", "ends_at"})

    def test_offline_restart_loads_only_its_account_cache(self):
        self.fetch()
        self.portal.payload = None
        restarted = self.make_controller()
        self.assertEqual(restarted.poll(self.portal)["active"], 1)
        stats = self.finish(restarted)
        self.assertEqual((stats["state"], stats["active"]), ("offline", 1))

    def test_failure_preserves_previous_snapshot_and_exact_cache_bytes(self):
        self.fetch()
        before = self.cache_path().read_bytes()
        self.portal.payload = None
        self.advance(REFRESH_SECONDS)
        self.controller.poll(self.portal)
        stats = self.finish()
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["state"], "offline")
        self.assertEqual(before, self.cache_path().read_bytes())

    def test_successful_empty_response_clears_old_targets_and_persists_empty(self):
        self.fetch()
        self.portal.payload = snapshot()
        self.advance(REFRESH_SECONDS)
        self.controller.poll(self.portal)
        self.assertEqual(self.finish()["active"], 0)
        self.assertEqual(json.loads(self.cache_path().read_text())["rabbits"], [])
        self.assertEqual(self.make_controller().poll(self.portal)["active"], 0)

    def test_key_change_clears_previous_accounts_targets_immediately(self):
        self.fetch()
        self.portal._api_key = "synthetic-key-B"
        self.portal.payload = None
        self.assertEqual(self.controller.poll(self.portal)["active"], 0)
        self.assertEqual(self.finish()["active"], 0)

    def test_endpoint_change_cannot_reuse_another_portals_cache(self):
        self.fetch()
        self.portal._api_url = "https://second.example.invalid/api/upload/"
        self.portal.payload = None
        self.assertEqual(self.controller.poll(self.portal)["active"], 0)

    def test_key_removal_hides_targets_without_deleting_old_account_cache(self):
        self.fetch()
        path = self.cache_path()
        self.portal._api_key = ""
        self.assertEqual(self.controller.poll(self.portal)["active"], 0)
        self.assertTrue(path.is_file())

    def test_wrong_account_cache_document_is_ignored_without_rewriting(self):
        self.fetch()
        path = self.cache_path()
        data = json.loads(path.read_text()); data["account"] = "other-account"
        path.write_text(json.dumps(data)); before = path.read_bytes()
        self.portal.payload = None
        restarted = self.make_controller()
        self.assertEqual(restarted.poll(self.portal)["active"], 0)
        self.finish(restarted)
        self.assertEqual(before, path.read_bytes())

    def test_malformed_cache_is_ignored_without_deleting_saved_bytes(self):
        self.cache_path().parent.mkdir(parents=True)
        self.cache_path().write_text("[malformed]")
        self.portal.payload = None
        self.assertEqual(self.controller.poll(self.portal)["active"], 0)
        self.finish()
        self.assertEqual(self.cache_path().read_text(), "[malformed]")

    def test_symlink_cache_is_not_read_or_overwritten(self):
        target = Path(self.temp.name) / "private-target"
        target.write_text("PRESERVE")
        self.cache_path().parent.mkdir(parents=True)
        try:
            self.cache_path().symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        stats = self.fetch()
        self.assertEqual(target.read_text(), "PRESERVE")
        self.assertEqual(stats["error"], "cache_write_error")
        self.assertEqual(stats["active"], 1)

    def test_server_time_controls_expiry_when_device_clock_differs(self):
        self.wall = NOW - 3600
        self.fetch()
        self.advance(601)
        self.assertEqual(self.controller.stats()["active"], 0)

    def test_wall_clock_jump_does_not_extend_session_target_lifetime(self):
        self.fetch()
        self.wall -= 10000
        self.mono += 601
        self.assertEqual(self.controller.stats()["active"], 0)

    def test_offline_cache_ages_across_restart(self):
        self.fetch()
        self.advance(601)
        self.portal.payload = None
        self.assertEqual(self.make_controller().poll(self.portal)["active"], 0)

    def test_paused_game_does_not_reset_expiry_when_draining_an_old_response(self):
        self.controller.poll(self.portal)
        self.controller._thread.join(3)
        self.assertFalse(self.controller._thread.is_alive())
        self.advance(601)
        self.assertEqual(self.controller.poll(self.portal)["active"], 0)

    def test_late_response_from_previous_account_is_discarded(self):
        entered, release = threading.Event(), threading.Event()
        def fetch():
            entered.set(); release.wait(3)
            return snapshot(rabbit())
        self.portal.fetch_rabbit_snapshot = fetch
        self.controller.poll(self.portal)
        self.assertTrue(entered.wait(1))
        old_path = self.cache_path()
        self.portal._api_key = "synthetic-key-B"
        self.controller.poll(self.portal)
        self.portal.fetch_rabbit_snapshot = lambda: None
        release.set()
        self.finish()
        self.finish()
        self.assertEqual(self.controller.stats()["active"], 0)
        self.assertFalse(old_path.exists())

    def test_inflight_request_cannot_spawn_overlapping_poll(self):
        entered, release = threading.Event(), threading.Event()
        def fetch():
            self.portal.calls += 1
            entered.set(); release.wait(3)
            return snapshot()
        self.portal.fetch_rabbit_snapshot = fetch
        self.controller.poll(self.portal)
        self.assertTrue(entered.wait(1))
        self.advance(1000)
        self.controller.poll(self.portal)
        self.assertEqual(self.portal.calls, 1)
        release.set(); self.finish()

    def test_close_retires_inflight_result_without_cache_write(self):
        entered, release = threading.Event(), threading.Event()
        def fetch():
            entered.set(); release.wait(3)
            return snapshot(rabbit())
        self.portal.fetch_rabbit_snapshot = fetch
        self.controller.poll(self.portal)
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.controller.close())
        release.set(); self.controller._thread.join(3)
        self.assertTrue(self.controller.close())
        self.assertEqual(self.controller.poll(self.portal)["active"], 0)
        self.assertFalse(self.cache_path().exists())

    def test_disk_write_failure_keeps_live_snapshot_usable(self):
        with patch("watchdogs.white_rabbit._save_snapshot", side_effect=OSError):
            stats = self.fetch()
        self.assertEqual(stats["active"], 1)
        self.assertFalse(stats["cache_saved"])

    def test_draw_uses_separate_white_glyphs_and_clips_outside_viewport(self):
        self.portal.payload = snapshot(rabbit(1), rabbit(2, lat=-80))
        self.fetch()
        class Projection:
            def geo_to_screen(self, lat, lng):
                return (40, 40) if lat > 0 else (40, -100)
        class Pixels:
            def __init__(self): self.calls = []
            def rect(self, *args): self.calls.append(("rect", args))
            def pset(self, *args): self.calls.append(("pset", args))
        graphics = Pixels()
        self.assertEqual(self.controller.draw(graphics, Projection(), 100, 10, 90), 1)
        self.assertTrue(any(call[1][-1] == 7 for call in graphics.calls))
        self.assertTrue(all(10 <= call[1][1] < 90 for call in graphics.calls))
        self.assertEqual(self.controller.stats()["active"], 2)
        self.advance(601)
        self.assertEqual(self.controller.draw(graphics, Projection(), 100, 10, 90), 0)


class RabbitContractTests(unittest.TestCase):
    def test_open_targets_use_actual_lat_lng_shape(self):
        now, rows, count = _parse_snapshot(snapshot(rabbit()))
        self.assertEqual((now, len(rows), count), (NOW, 1, 1))

    def test_caught_closed_claimed_winner_and_expired_targets_are_not_active(self):
        rows = [rabbit(1, state="caught"), rabbit(2, state="closed"), rabbit(3, state="unknown"),
                rabbit(4, claimed_at=NOW-1), rabbit(5, winner="synthetic"),
                rabbit(6, ends_at=NOW), rabbit(7, closes_at=NOW-1), rabbit(8)]
        self.assertEqual([r["id"] for r in _parse_snapshot(snapshot(*rows))[1]], ["8"])

    def test_caught_state_hides_null_claimed_at_even_with_future_closes_at(self):
        self.assertFalse(_parse_snapshot(snapshot(rabbit(state="caught", winner="synthetic")))[1])

    def test_invalid_coordinates_ids_and_deadlines_cannot_become_markers(self):
        rows = [rabbit(1, lat=True), rabbit(2, lng=float("nan")), rabbit(3, lat=91),
                rabbit(4, lng=-181), rabbit(5, ends_at="later"), rabbit(6, closes_at=False),
                rabbit(True), rabbit({}, lat=1), rabbit("x"*129), rabbit(10, lat="12.25"),
                rabbit(11, lat=10**1000)]
        self.assertFalse(_parse_snapshot(snapshot(*rows))[1])

    def test_duplicate_target_identity_is_hidden_as_ambiguous(self):
        self.assertFalse(_parse_snapshot(snapshot(rabbit(), rabbit(state="caught")))[1])

    def test_malformed_or_oversized_top_level_snapshot_is_rejected(self):
        for payload in [None, [], {"ok": False}, {"ok": True, "now": NOW, "rabbits": {}},
                        {"ok": True, "now": True, "rabbits": []},
                        {"ok": True, "now": float("nan"), "rabbits": []},
                        {"ok": True, "now": 10**1000, "rabbits": []},
                        snapshot(*[rabbit(i) for i in range(MAX_RABBITS+1)])]:
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ValueError):
                _parse_snapshot(payload)


if __name__ == "__main__":
    unittest.main()
