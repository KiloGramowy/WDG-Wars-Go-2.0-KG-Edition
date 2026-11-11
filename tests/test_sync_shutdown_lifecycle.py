"""Delayed HTTP responses cannot update a game after Sync starts unloading."""
import io
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import WardriveUpload


class SyncShutdownLifecycleTests(unittest.TestCase):
    def setUp(self):
        with patch.object(WardriveUpload, "_load_state"):
            self.plugin = WardriveUpload()
        self.plugin._api_url = "https://example.invalid/api/upload/"
        self.plugin._api_key = "synthetic-shutdown-test-key"
        self.app = SimpleNamespace(_shutting_down=False, _term_add=Mock(), msg=Mock(),
                                   _badges=set(), _earn_badge=Mock(),
                                   _mc_node_name="WatchDogs_synthetic", _mc_channels_list=[])
        self.plugin.app = self.app
        self.releases = []
        self.network = self.enterContext(patch("plugins.wardrive_upload._open",
            side_effect=AssertionError("No real API call is allowed")))
        self.errors = self.enterContext(patch("threading.excepthook"))
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for event in self.releases:
            event.set()
        self.plugin.on_unload()
        with self.plugin._background_lock:
            threads = list(self.plugin._background_threads)
        threads.append(self.plugin._auth_poll_thread)
        for thread in threads:
            if thread is not None:
                thread.join(3)
                self.assertFalse(thread.is_alive(), "Synthetic HTTP worker did not finish")
        self.errors.assert_not_called()

    def barrier(self, result):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def blocked():
            entered.set()
            if not release.wait(3):
                raise AssertionError("Synthetic response was not released")
            return result

        return entered, release, blocked

    def response(self, result, read):
        class Response(io.BytesIO):
            def read(self, *args):
                read()
                return super().read(*args)
        return Response(json.dumps(result).encode())

    def one_shot_thread(self):
        with self.plugin._background_lock:
            threads = list(self.plugin._background_threads)
        self.assertEqual(len(threads), 1)
        return threads[0]

    def stop_while_blocked(self, thread):
        with patch.object(thread, "join"):
            self.assertFalse(self.plugin.on_unload(), "Blocked HTTP was called quiescent")
        self.app._shutting_down = True
        self.app._term_add.reset_mock()
        self.app.msg.reset_mock()

    def assert_no_late_game_mutation(self):
        self.app._term_add.assert_not_called()
        self.app.msg.assert_not_called()
        self.app._earn_badge.assert_not_called()
        self.assertEqual(self.app._mc_node_name, "WatchDogs_synthetic")
        self.assertEqual(self.plugin._username, "")
        self.assertTrue(self.plugin.on_unload())

    def test_load_tracks_identity_and_retires_a_late_success_before_app_callback(self):
        entered, release, fetch = self.barrier(dict(username="late", badges=["wardriver"]))
        self.plugin.fetch_user_info = Mock(side_effect=fetch)
        self.plugin._on_user_info = Mock()
        self.plugin._start_auth_poll = Mock()
        self.plugin.on_load(self.app)
        self.assertTrue(entered.wait(2))
        thread = self.one_shot_thread()
        self.stop_while_blocked(thread)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.plugin._on_user_info.assert_not_called()
        self.assert_no_late_game_mutation()

    def test_profile_action_late_response_does_not_apply_identity_badges_or_radio_name(self):
        entered, release, fetch = self.barrier(dict(username="late", badges=["wardriver"]))
        self.plugin.fetch_user_info = Mock(side_effect=fetch)
        self.plugin._show_profile()
        self.assertTrue(entered.wait(2))
        thread = self.one_shot_thread()
        self.stop_while_blocked(thread)
        release.set()
        thread.join(2)
        self.assert_no_late_game_mutation()

    def test_actual_identity_http_response_after_unload_is_ignored(self):
        entered, release, read = self.barrier(None)
        self.network.side_effect = lambda *args, **kwargs: self.response(
            dict(ok=True, username="late", badges=["wardriver"]), read)
        self.plugin._start_background_worker(self.plugin._validate_and_apply_user,
                                              name="synthetic-identity")
        self.assertTrue(entered.wait(2))
        thread = self.one_shot_thread()
        self.stop_while_blocked(thread)
        release.set()
        thread.join(2)
        self.assert_no_late_game_mutation()

    def test_actual_badge_http_response_after_unload_cannot_earn_badges(self):
        entered, release, read = self.barrier(None)
        self.network.side_effect = lambda *args, **kwargs: self.response(
            dict(badges=["wardriver"], server_awarded=["wardriver"]), read)
        self.plugin._start_badge_sync()
        self.assertTrue(entered.wait(2))
        thread = self.one_shot_thread()
        self.stop_while_blocked(thread)
        release.set()
        thread.join(2)
        self.assert_no_late_game_mutation()

    def test_api_test_response_can_finish_without_reaching_closed_terminal(self):
        entered, release, read = self.barrier(None)
        self.network.side_effect = lambda *args, **kwargs: self.response(dict(total=42), read)
        self.plugin._test_api()
        self.assertTrue(entered.wait(2))
        thread = self.one_shot_thread()
        self.stop_while_blocked(thread)
        release.set()
        thread.join(2)
        self.assert_no_late_game_mutation()

    def test_auth_poll_response_after_unload_cannot_display_or_save_a_login_pin(self):
        entered, release, read = self.barrier(None)
        self.network.side_effect = lambda *args, **kwargs: self.response(
            dict(pending=True, pin="123456", token="synthetic-request", expires=60), read)
        self.plugin._start_auth_poll()
        self.assertTrue(entered.wait(2))
        thread = self.plugin._auth_poll_thread
        self.stop_while_blocked(thread)
        release.set()
        thread.join(2)
        self.assertEqual(self.plugin._auth_pin, "")
        self.assertEqual(self.plugin._auth_current_token, "")
        self.assert_no_late_game_mutation()

    def test_auth_poll_interval_is_woken_by_shutdown_instead_of_waiting_five_seconds(self):
        received = threading.Event()
        def read():
            received.set()
        self.network.side_effect = lambda *args, **kwargs: self.response(dict(pending=False), read)
        self.plugin._start_auth_poll()
        self.assertTrue(received.wait(2))
        started = time.monotonic()
        self.assertTrue(self.plugin.on_unload())
        self.assertLess(time.monotonic() - started, 0.75)
        self.assertFalse(self.plugin._auth_poll_thread.is_alive())

    def test_unload_has_one_bounded_budget_for_multiple_blocked_workers(self):
        entered1, release1, blocked1 = self.barrier(None)
        entered2, release2, blocked2 = self.barrier(None)
        self.plugin._start_background_worker(blocked1, name="synthetic-first")
        self.plugin._start_background_worker(blocked2, name="synthetic-second")
        self.assertTrue(entered1.wait(2) and entered2.wait(2))
        started = time.monotonic()
        self.assertFalse(self.plugin.on_unload())
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLess(elapsed, 1.7)
        release1.set()
        release2.set()

    def test_no_new_network_worker_can_start_after_unload(self):
        self.assertTrue(self.plugin.on_unload())
        target = Mock()
        self.assertFalse(self.plugin._start_background_worker(target, name="synthetic-retired"))
        self.plugin._start_auth_poll()
        self.plugin._validate_and_apply_user()
        self.plugin._show_profile()
        self.plugin._start_badge_sync()
        self.plugin._test_api()
        target.assert_not_called()
        self.network.assert_not_called()
        self.app._term_add.assert_not_called()
        self.assertIsNone(self.plugin._auth_poll_thread)
        self.assertFalse(self.plugin._background_threads)

    def test_shutdown_flag_alone_prevents_profile_badges_radio_and_ui_callbacks(self):
        self.app._shutting_down = True
        self.plugin._on_user_info(dict(username="late", badges=["wardriver"]))
        self.assertEqual(self.plugin._apply_server_badges(["wardriver"]), [])
        self.plugin._log_add("late diagnostic")
        self.plugin.msg("late HUD message")
        self.assert_no_late_game_mutation()

    def test_active_identity_sync_keeps_existing_badges_stats_radio_and_messages(self):
        saver = Mock()
        with patch.dict("sys.modules", {"watchdogs.lora_manager": SimpleNamespace(save_meshcore_config=saver)}):
            self.plugin._on_user_info(dict(username="active", gang="synthetic", wifi=12,
                                           ble=3, total=15, badges=["wardriver"]))
        self.assertEqual(self.plugin._username, "active")
        self.assertEqual(self.plugin._user_stats["wifi"], 12)
        self.app._earn_badge.assert_called_once_with("wardriver")
        self.assertEqual(self.app._mc_node_name, "WDG_active")
        saver.assert_called_once_with("WDG_active", [])
        self.assertTrue(any("Welcome, active!" in call.args[0] for call in self.app._term_add.call_args_list))
        self.app.msg.assert_any_call("[LoRa] Identity synced: WDG_active", 11)

    def test_active_profile_refresh_keeps_radio_name_when_sync_not_requested(self):
        self.plugin._on_user_info(dict(username="active"), sync_lora_name=False)
        self.assertEqual(self.plugin._username, "active")
        self.assertEqual(self.app._mc_node_name, "WatchDogs_synthetic")

    def test_active_terminal_errors_are_not_silently_hidden(self):
        self.app._term_add.side_effect = AttributeError("synthetic unexpected active UI failure")
        with self.assertRaisesRegex(AttributeError, "active UI failure"):
            self.plugin._log_add("active diagnostic")

    def test_thread_start_failure_releases_registration_and_remains_visible(self):
        fake = Mock()
        fake.start.side_effect = RuntimeError("synthetic start failure")
        with patch("plugins.wardrive_upload.threading.Thread", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "start failure"):
                self.plugin._start_background_worker(Mock(), name="synthetic-error")
        self.assertFalse(self.plugin._background_threads)
        self.assertTrue(self.plugin.on_unload())


if __name__ == "__main__":
    unittest.main(verbosity=2)
