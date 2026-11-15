"""Portal ownership survives locally without inventing game achievements/XP."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import BADGE_GAME_TO_SERVER, WardriveUpload
from test_pending_log_ui import PyxelRecorder


class PortalBadgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.conf = dict(WARDRIVE_API_URL="https://portal.example.invalid/api/upload/",
                         WARDRIVE_API_KEY="synthetic-account-alpha")
        self.enterContext(patch.object(WardriveUpload, "_state_file", return_value=self.path))
        self.enterContext(patch("plugins.wardrive_upload._load_secrets_conf", return_value=self.conf))
        self.network = self.enterContext(patch("plugins.wardrive_upload._open",
            side_effect=AssertionError("Only explicitly mocked HTTP is allowed")))
        self.p = WardriveUpload()
        self.app = SimpleNamespace(_shutting_down=False, _badges={"existing-local"},
            _earn_badge=Mock(), _term_add=Mock(), msg=Mock(), xp=1234,
            _mc_node_name="custom radio", _esc_consumed_frame=-1)
        self.p.app = self.app
        self.p._overlay_active = True
        self.p._next_cleanup = float("inf")
        self.p._start_auth_poll = Mock()
        self.releases = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for event in self.releases:
            event.set()
        self.p.on_unload()
        with self.p._background_lock:
            threads = list(self.p._background_threads)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def wait_workers(self):
        with self.p._background_lock:
            threads = list(self.p._background_threads)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def ids(self, plugin=None):
        return [row[0] for row in (plugin or self.p)._portal_badge_view()]

    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode())

    def test_all_unknown_portal_badges_persist_and_do_not_award_game_xp(self):
        badges = ["synthetic_portal_%02d" % n for n in range(48)]
        self.assertTrue(self.p._remember_portal_badges(badges))
        self.assertEqual(self.ids(), badges)
        self.assertEqual(self.ids(WardriveUpload()), badges)
        self.assertEqual(self.app._badges, {"existing-local"})
        self.app._earn_badge.assert_not_called()
        self.assertEqual(self.app.xp, 1234)
        stored = self.path.read_text()
        self.assertNotIn(self.conf["WARDRIVE_API_KEY"], stored)
        self.assertNotIn(self.conf["WARDRIVE_API_URL"], stored)

    def test_identity_keeps_all_server_ids_but_only_existing_mapping_can_earn(self):
        badges = ["wardriver", "synthetic_portal_alpha", "synthetic_portal_beta"]
        self.p._on_user_info(dict(username="synthetic", badges=badges), sync_lora_name=False)
        self.assertEqual(self.ids(), badges)
        self.app._earn_badge.assert_called_once_with("wardriver")
        self.assertEqual(self.app.xp, 1234)
        self.assertEqual(BADGE_GAME_TO_SERVER, {
            "wardriver": "wardriver", "handshake_hunter": "handshake_hunter",
            "evil_twin": "evil_twin", "meshcore": "mesh_first",
            "wpasec_uploader": "wpa_sec_user", "flipper": "flipper_user",
            "skywatch": "skywatch", "iot_hunter": "iot_hunter"})

    def test_unknown_existing_state_fields_and_upload_history_are_preserved(self):
        original = {"uploaded": ["synthetic-session"], "uploaded_meshtastic": [],
                    "private_extension": {"keep": [1, 2, 3]}, "future_field": "unchanged"}
        self.path.write_text(json.dumps(original))
        p = WardriveUpload()
        self.assertTrue(p._remember_portal_badges(["synthetic_portal_alpha"]))
        saved = json.loads(self.path.read_text())
        for key, value in original.items():
            self.assertEqual(saved[key], value)

    def test_extension_added_after_loading_is_preserved_by_later_cache_write(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        disk = json.loads(self.path.read_text())
        disk["future_extension"] = {"keep": "synthetic"}
        self.path.write_text(json.dumps(disk))
        self.p._remember_portal_badges(["synthetic_portal_beta"])
        self.assertEqual(json.loads(self.path.read_text())["future_extension"], disk["future_extension"])

    def test_corrupt_existing_state_is_not_overwritten_to_save_a_badge_cache(self):
        original = b'{"synthetic-interrupted-state":'
        self.path.write_bytes(original)
        self.assertFalse(self.p._remember_portal_badges(["synthetic_portal_alpha"]))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.ids(), [])

    def test_key_or_url_change_hides_previous_account_and_restart_cannot_load_it(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        for field in ("WARDRIVE_API_KEY", "WARDRIVE_API_URL"):
            changed = dict(self.conf, **{field: self.conf[field] + "different"})
            with patch("plugins.wardrive_upload._load_secrets_conf", return_value=changed):
                self.assertEqual(self.ids(WardriveUpload()), [])
        self.p._api_key = "synthetic-account-beta"
        self.assertEqual(self.ids(), [])

    def test_explicit_paired_key_switch_clears_cache_without_resetting_local_progress(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        with patch.object(self.p, "_save_secret"):
            self.p.adopt_api_key("b" * 64)
        self.assertEqual(self.ids(), [])
        self.assertIsNone(json.loads(self.path.read_text())["portal_badge_cache_v1"])
        self.assertEqual(self.app._badges, {"existing-local"})
        self.assertEqual(self.app.xp, 1234)

    def test_failed_atomic_save_keeps_previous_cache_and_file(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        before = self.path.read_bytes()
        with patch("plugins.wardrive_upload.os.replace", side_effect=OSError("synthetic")):
            self.assertFalse(self.p._remember_portal_badges(["synthetic_portal_beta"]))
        self.assertEqual(self.ids(), ["synthetic_portal_alpha"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("previous cache kept", self.p._portal_badges_message)
        self.assertEqual(list(self.path.parent.glob(".wardrive-state-*")), [])

    def test_metadata_labels_are_used_and_bare_ids_are_safely_humanized(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha", "synthetic_portal_beta"],
            metadata={"synthetic_portal_alpha": {"name": "Server\nLabel", "description": "A\x00description"}})
        rows = self.p._portal_badge_view()
        self.assertEqual(rows[0], ("synthetic_portal_alpha", "Server Label", "A description"))
        self.assertEqual(rows[1][1], "Synthetic Portal Beta")
        self.assertEqual(self.ids(WardriveUpload()), self.ids())

    def test_upload_award_delta_merges_unknown_ids_and_keeps_server_label(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"],
            metadata={"synthetic_portal_alpha": {"name": "Friendly server label"}})
        self.p._handle_upload_badges_for_account(
            {"new_badges": ["synthetic_portal_alpha", "synthetic_portal_beta"]}, self.p._portal_account())
        self.assertEqual(self.ids(), ["synthetic_portal_alpha", "synthetic_portal_beta"])
        self.assertEqual(self.p._portal_badge_view()[0][1], "Friendly server label")
        self.app._earn_badge.assert_not_called()
        self.assertEqual(self.app.xp, 1234)

    def test_stale_upload_account_cannot_change_cache_or_game_badges(self):
        old = self.p._portal_account()
        self.p._api_key = "synthetic-account-beta"
        self.p._handle_upload_badges_for_account({"new_badges": ["wardriver"]}, old)
        self.assertEqual(self.ids(), [])
        self.app._earn_badge.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_late_identity_worker_cannot_publish_another_accounts_badges(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def delayed():
            entered.set()
            self.assertTrue(release.wait(3))
            return dict(username="synthetic-old", badges=["wardriver"])
        with patch.object(self.p, "fetch_user_info", side_effect=delayed):
            self.p._start_background_worker(self.p._validate_and_apply_user, name="synthetic-identity")
            self.assertTrue(entered.wait(2))
            self.p._api_key = "synthetic-account-beta"
            release.set()
            self.wait_workers()
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.p._username, "")
        self.app._earn_badge.assert_not_called()

    def test_actual_http_read_rejects_response_after_account_change(self):
        class ChangedResponse(io.BytesIO):
            def read(inner, *args):
                self.p._api_key = "synthetic-account-beta"
                return super().read(*args)
        self.network.side_effect = None
        self.network.return_value = ChangedResponse(b'{"ok":true,"username":"synthetic","badges":["wardriver"]}')
        self.assertIsNone(self.p.fetch_user_info())
        self.assertEqual(self.ids(), [])

    def test_late_badge_sync_post_response_cannot_publish_previous_accounts_ids(self):
        class ChangedResponse(io.BytesIO):
            def read(inner, *args):
                self.p._api_key = "synthetic-account-beta"
                return super().read(*args)
        self.network.side_effect = None
        self.network.return_value = ChangedResponse(b'{"badges":["wardriver","synthetic_portal_alpha"]}')
        self.p._badge_sync_worker()
        self.assertEqual(self.ids(), [])
        self.app._earn_badge.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_old_read_only_refresh_cannot_replace_a_new_accounts_completed_refresh(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def fetch():
            if self.p._api_key == self.conf["WARDRIVE_API_KEY"]:
                entered.set()
                self.assertTrue(release.wait(3))
                return dict(ok=True, badges=["synthetic_portal_alpha"])
            return dict(ok=True, badges=["synthetic_portal_beta"])
        with patch.object(self.p, "fetch_user_info", side_effect=fetch):
            self.p._open_portal_badges()
            self.assertTrue(entered.wait(2))
            with self.p._upload_queue_lock():
                self.p._api_key = "synthetic-account-beta"
                self.p._clear_portal_account_state()
            self.p._start_portal_badge_refresh()
            release.set()
            self.wait_workers()
        self.assertEqual(self.ids(), ["synthetic_portal_beta"])
        self.app._earn_badge.assert_not_called()

    def test_full_id_only_refresh_retains_previously_supplied_server_label(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"],
            metadata={"synthetic_portal_alpha": {"name": "Friendly server label"}})
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        self.assertEqual(self.p._portal_badge_view()[0][1], "Friendly server label")

    def test_invalid_or_oversized_badge_list_does_not_erase_saved_ownership(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        before = self.path.read_bytes()
        for invalid in (None, {}, "bad", ["synthetic"] * 4097, [None], [{}],
                        [""], [5], ["synthetic_portal_beta", None], ["bad\x00id"]):
            self.assertFalse(self.p._remember_portal_badges(invalid))
            self.assertEqual(self.ids(), ["synthetic_portal_alpha"])
            self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_cache_timestamp_cannot_break_startup_or_edit_the_source_file(self):
        for stamp in (10 ** 1000, float("inf"), float("nan"), "invalid", None, True):
            saved = {"portal_badge_cache_v1": {"scope": self.p._portal_account_scope(),
                "badges": [{"id": "synthetic_portal_alpha"}], "fetched_at": stamp}}
            self.path.write_text(json.dumps(saved))
            before = self.path.read_bytes()
            self.assertEqual(self.ids(WardriveUpload()), [])
            self.assertEqual(self.path.read_bytes(), before)

    def test_successfully_empty_server_snapshot_replaces_old_ownership(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        self.assertTrue(self.p._remember_portal_badges([]))
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.ids(WardriveUpload()), [])

    def test_portal_view_refresh_is_get_only_and_never_syncs_or_awards(self):
        calls = []
        def opened(req, **unused):
            calls.append(req)
            return self.response(dict(ok=True, username="synthetic", badges=["wardriver", "synthetic_portal_alpha"]))
        self.network.side_effect = opened
        with patch.object(self.p, "_start_badge_sync") as sync, patch.object(self.p, "_start_upload") as upload:
            self.p._exec("portal_badges")
            self.wait_workers()
            sync.assert_not_called()
            upload.assert_not_called()
        self.assertEqual([(r.get_method(), r.full_url) for r in calls],
                         [("GET", "https://portal.example.invalid/api/me")])
        self.assertEqual(self.ids(), ["wardriver", "synthetic_portal_alpha"])
        self.app._earn_badge.assert_not_called()
        self.assertEqual(self.app.xp, 1234)

    def test_failed_read_only_refresh_keeps_cache_without_awards(self):
        self.p._remember_portal_badges(["synthetic_portal_alpha"])
        with patch.object(self.p, "fetch_user_info", return_value=None):
            self.p._open_portal_badges()
            self.wait_workers()
        self.assertEqual(self.ids(), ["synthetic_portal_alpha"])
        self.assertIn("saved portal badges kept", self.p._portal_badges_message)
        self.app._earn_badge.assert_not_called()

    def test_read_only_refresh_input_does_not_wait_for_a_busy_upload_disk_lock(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        self.releases.append(release)
        def busy_disk_worker():
            with self.p._upload_queue_lock():
                entered.set()
                release.wait(3)
        def input_callback():
            try:
                self.p._start_portal_badge_refresh()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        holder = threading.Thread(target=busy_disk_worker)
        holder.start()
        self.assertTrue(entered.wait(2))
        with patch.object(self.p, "_start_background_worker", return_value=True) as start:
            ui = threading.Thread(target=input_callback)
            ui.start()
            try:
                self.assertTrue(finished.wait(0.5), "Badge refresh input waited on the upload/disk worker")
            finally:
                release.set()
                holder.join(2)
                ui.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(holder.is_alive())
        self.assertFalse(ui.is_alive())
        start.assert_called_once()

    def test_read_only_list_pages_to_every_badge_and_esc_returns_to_sync(self):
        self.p._remember_portal_badges(["synthetic_portal_%02d" % n for n in range(48)])
        self.p._portal_badges_open = True
        px = PyxelRecorder()
        with patch.dict(sys.modules, {"pyxel": px}):
            for width, height in ((640, 360), (640, 240)):
                self.p.draw(0, 0, width, height)
                for name, args in px.calls:
                    if name == "text":
                        x, y, text, _ = args
                        self.assertLessEqual(x + len(text) * 5, width)
                        self.assertLessEqual(y + 8, height)
                px.calls.clear()
            px.pressed = {px.KEY_END}
            self.p.on_update()
            self.assertEqual(self.p._portal_badges_sel, 47)
            self.p.draw(0, 0, 640, 360)
            self.assertTrue(any("48. Portal badge:" in args[2] for name, args in px.calls if name == "text"))
            px.pressed = {px.KEY_HOME}
            self.p.on_update()
            self.assertEqual(self.p._portal_badges_sel, 0)
            px.pressed = {px.KEY_PAGEDOWN}
            self.p.on_update()
            self.assertEqual(self.p._portal_badges_sel, self.p._portal_badges_page_rows)
            px.pressed = {px.KEY_ESCAPE}
            self.p.on_update()
        self.assertFalse(self.p._portal_badges_open)
        self.assertTrue(self.p._overlay_active)
        self.assertEqual(self.app._esc_consumed_frame, px.frame_count)
        self.network.assert_not_called()
        self.app._earn_badge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
