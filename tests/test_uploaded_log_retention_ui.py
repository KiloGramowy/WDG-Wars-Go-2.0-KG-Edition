"""Retention controls through real plugin UI methods; fake keyboard/storage."""
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import WardriveUpload
from test_pending_log_ui import PyxelRecorder, entry


class UploadedLogRetentionUiTests(unittest.TestCase):
    def setUp(self):
        self.px = PyxelRecorder()
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"pyxel": self.px}).start()
        with patch.object(WardriveUpload, "_load_state"):
            self.plugin = WardriveUpload()
        self.plugin.app = SimpleNamespace(level=1, _esc_consumed_frame=-1, _big_font=None)
        self.retention = SimpleNamespace(days=0)
        self.plugin._get_retention = Mock(return_value=self.retention)
        self.plugin._set_retention_days = Mock(side_effect=self.save_days)
        self.plugin.list_pending_logs = Mock(return_value=[entry()])
        self.plugin.delete_pending_log = Mock(return_value={"deleted": True})
        self.plugin._pending_sessions = Mock(side_effect=AssertionError("No automatic cleanup while browsing"))
        self.plugin._start_upload = Mock(side_effect=AssertionError("Settings must not upload"))
        self.plugin._start_auth_poll = Mock(side_effect=AssertionError("Settings must not start auth"))
        self.plugin.open_pending_logs()

    def save_days(self, days):
        self.retention.days = days
        return True

    def press(self, *keys, advance=True):
        if advance:
            self.px.frame_count += 1
        self.px.pressed = {getattr(self.px, "KEY_" + key) for key in keys}
        self.plugin.on_update()
        self.px.pressed.clear()

    def open_settings(self):
        self.press("A")
        self.assertTrue(self.plugin._retention_settings_open)

    def choose_custom(self):
        self.open_settings()
        for _ in range(5):
            self.press("DOWN")
        self.press("RETURN")
        self.assertTrue(self.plugin._retention_custom)

    def texts(self):
        return [args for name, args in self.px.calls if name == "text"]

    def assert_bounds(self):
        for x, y, text, color, *unused in self.texts():
            self.assertGreaterEqual(x, 0, text)
            self.assertGreaterEqual(y, 0, text)
            self.assertLessEqual(x + len(text) * 5, 640, text)
            self.assertLessEqual(y + 8, 360, text)

    def test_default_keep_all_and_uploaded_only_rule_are_visible_without_enabling(self):
        self.plugin.draw(0, 0, 640, 360)
        labels = [row[2] for row in self.texts()]
        self.assertTrue(any("Auto-clean: Keep all" in label and "Uploaded logs only" in label
                            for label in labels))
        self.assertTrue(any("A auto-clean" in label for label in labels))
        self.plugin._set_retention_days.assert_not_called()
        self.assert_bounds()

    def test_empty_browser_with_no_key_and_low_level_can_open_the_settings(self):
        self.plugin.list_pending_logs.return_value = []
        self.plugin.open_pending_logs()
        self.open_settings()
        self.plugin.draw(0, 0, 640, 360)
        labels = [row[2] for row in self.texts()]
        for rule in ("Only successfully uploaded logs.", "Pending / failed / active logs stay.",
                     "Days since confirmed upload.", "Keep all", "After 1 day", "After 5 days",
                     "After 15 days", "After 31 days", "Custom days..."):
            self.assertIn(rule, labels)
        self.assertEqual(self.plugin._api_key, "")
        self.plugin._set_retention_days.assert_not_called()
        self.assert_bounds()

    def test_all_presets_apply_only_after_enter(self):
        for index, days in enumerate((0, 1, 5, 15, 31)):
            with self.subTest(days=days):
                self.retention.days = 0
                self.plugin._set_retention_days.reset_mock()
                self.open_settings()
                for _ in range(index):
                    self.press("DOWN")
                self.plugin._set_retention_days.assert_not_called()
                self.press("RETURN")
                self.plugin._set_retention_days.assert_called_once_with(days)
                self.assertEqual(self.retention.days, days)
                self.assertFalse(self.plugin._retention_settings_open)
                self.assertIn("Uploaded logs only", self.plugin._pending_message)

    def test_opening_key_and_enter_cannot_apply_in_the_same_frame(self):
        self.press("A", "RETURN")
        self.assertTrue(self.plugin._retention_settings_open)
        self.plugin._set_retention_days.assert_not_called()
        self.press("RETURN", advance=False)
        self.plugin._set_retention_days.assert_not_called()
        self.press("RETURN")
        self.plugin._set_retention_days.assert_called_once_with(0)

    def test_browser_open_frame_ignores_auto_clean_shortcut(self):
        self.press("A", "RETURN", advance=False)
        self.assertFalse(self.plugin._retention_settings_open)
        self.plugin._set_retention_days.assert_not_called()

    def test_custom_number_and_backspace_save_the_displayed_positive_days(self):
        self.choose_custom()
        for digit in "720":
            self.press(digit)
        self.press("BACKSPACE")
        self.press("5")
        self.assertEqual(self.plugin._retention_input, "725")
        self.plugin._set_retention_days.assert_not_called()
        self.press("RETURN")
        self.plugin._set_retention_days.assert_called_once_with(725)
        self.assertFalse(self.plugin._retention_settings_open)
        self.assertIn("After 725 days", self.plugin._pending_message)

    def test_custom_empty_and_zero_are_rejected_without_changing_the_policy(self):
        self.choose_custom()
        self.press("RETURN")
        self.assertTrue(self.plugin._retention_custom)
        self.assertIn("1 to 999999", self.plugin._retention_message)
        self.press("0")
        self.press("RETURN")
        self.assertIn("1 to 999999", self.plugin._retention_message)
        self.assertEqual(self.retention.days, 0)
        self.plugin._set_retention_days.assert_not_called()

    def test_custom_limit_is_explicit_and_the_input_is_bounded(self):
        self.choose_custom()
        for _ in range(6):
            self.press("9")
        self.press("9")
        self.assertEqual(self.plugin._retention_input, "999999")
        self.assertIn("6 digits", self.plugin._retention_message)
        self.plugin.draw(0, 0, 640, 360)
        self.assertIn("Custom retention: 1-999999 days", [row[2] for row in self.texts()])
        self.assert_bounds()
        self.press("RETURN")
        self.plugin._set_retention_days.assert_called_once_with(999999)

    def test_saved_custom_value_is_selected_and_prefilled_when_reopening(self):
        self.retention.days = 720
        self.open_settings()
        self.assertEqual(self.plugin._retention_sel, 5)
        self.press("RETURN")
        self.assertEqual(self.plugin._retention_input, "720")
        self.press("RETURN", advance=False)
        self.plugin._set_retention_days.assert_not_called()
        self.press("RETURN")
        self.plugin._set_retention_days.assert_called_once_with(720)

    def test_custom_escape_returns_to_choices_then_browser_then_parent(self):
        self.choose_custom()
        self.press("2")
        self.press("ESCAPE")
        self.assertFalse(self.plugin._retention_custom)
        self.assertTrue(self.plugin._retention_settings_open)
        self.assertTrue(self.plugin._pending_browser)
        self.assertEqual(self.plugin.app._esc_consumed_frame, self.px.frame_count)
        self.press("ESCAPE")
        self.assertFalse(self.plugin._retention_settings_open)
        self.assertTrue(self.plugin._pending_browser)
        self.assertTrue(self.plugin.overlay_active)
        self.press("ESCAPE")
        self.assertFalse(self.plugin._pending_browser)
        self.assertFalse(self.plugin.overlay_active)
        self.plugin._set_retention_days.assert_not_called()

    def test_cancelling_from_sync_returns_to_the_existing_sync_overlay(self):
        self.press("ESCAPE")
        self.plugin._open_pending_logs(from_sync=True)
        self.open_settings()
        self.press("DOWN")
        self.press("ESCAPE")
        self.press("ESCAPE")
        self.assertFalse(self.plugin._pending_browser)
        self.assertTrue(self.plugin.overlay_active)
        self.plugin._set_retention_days.assert_not_called()

    def test_failed_save_keeps_old_policy_and_allows_retry(self):
        self.retention.days = 5
        attempts = []
        def retry(days):
            attempts.append(days)
            if len(attempts) == 1:
                return False
            self.retention.days = days
            return True
        self.plugin._set_retention_days.side_effect = retry
        self.open_settings()
        self.press("DOWN")
        self.press("RETURN")
        self.assertEqual(self.retention.days, 5)
        self.assertTrue(self.plugin._retention_settings_open)
        self.assertEqual(self.plugin._retention_sel, 3)
        self.assertIn("Previous policy stays", self.plugin._retention_message)
        self.plugin.draw(0, 0, 640, 360)
        self.assert_bounds()
        self.press("RETURN")
        self.assertFalse(self.plugin._retention_settings_open)
        self.assertEqual(self.plugin._set_retention_days.call_count, 2)
        self.assertEqual(self.retention.days, 15)
        self.assertIn("After 15 days", self.plugin._pending_message)

    def test_save_exception_does_not_expose_diagnostics_or_claim_success(self):
        self.plugin._set_retention_days.side_effect = OSError("private diagnostic")
        self.open_settings()
        self.press("DOWN")
        self.press("RETURN")
        self.assertTrue(self.plugin._retention_settings_open)
        self.assertIn("Could not save", self.plugin._retention_message)
        self.assertNotIn("private diagnostic", self.plugin._retention_message)
        self.assertEqual(self.retention.days, 0)

    def test_setting_modal_consumes_file_keys_without_refresh_upload_or_delete(self):
        self.open_settings()
        self.press("D", "Y", "R", "PAGEDOWN")
        self.assertTrue(self.plugin._retention_settings_open)
        self.assertIsNone(self.plugin._pending_delete)
        self.plugin.delete_pending_log.assert_not_called()
        self.plugin.list_pending_logs.assert_called_once_with()
        self.plugin._pending_sessions.assert_not_called()
        self.plugin._start_upload.assert_not_called()
        self.plugin._start_auth_poll.assert_not_called()
        self.plugin._set_retention_days.assert_not_called()

    def test_saving_policy_does_not_refresh_prune_or_delete_the_browsed_logs(self):
        self.open_settings()
        self.press("DOWN")
        self.press("RETURN")
        self.assertEqual(self.retention.days, 1)
        self.plugin.list_pending_logs.assert_called_once_with()
        self.plugin.delete_pending_log.assert_not_called()
        self.plugin._pending_sessions.assert_not_called()
        self.plugin._start_upload.assert_not_called()

    def test_pin_keeps_priority_over_settings_keys_and_drawing(self):
        self.open_settings()
        with patch.object(self.plugin, "_auth_pin_active", return_value=True), \
                patch.object(self.plugin, "_draw_auth_pin") as pin:
            self.press("RETURN", "ESCAPE", "Y")
            self.plugin.draw(0, 0, 640, 360)
            pin.assert_called_once_with(640, 360)
        self.assertTrue(self.plugin._retention_settings_open)
        self.assertEqual(self.plugin.app._esc_consumed_frame, -1)
        self.plugin._set_retention_days.assert_not_called()

    def test_modal_and_custom_max_value_fit_the_existing_font_and_screen(self):
        self.open_settings()
        self.plugin.draw(0, 0, 640, 360)
        labels = self.texts()
        self.assertTrue(any(text == "UPLOADED LOG AUTO-CLEAN" and color == 10
                            for x, y, text, color, *unused in labels))
        self.assert_bounds()
        for _ in range(5):
            self.press("DOWN")
        self.press("RETURN")
        for _ in range(6):
            self.press("9")
        self.px.calls.clear()
        self.plugin.draw(0, 0, 640, 360)
        self.assertIn("Days: 999999_", [row[2] for row in self.texts()])
        self.assert_bounds()

    def test_existing_manual_delete_confirmation_still_takes_priority_over_a(self):
        self.press("D")
        original = self.plugin._pending_delete
        self.press("A")
        self.assertFalse(self.plugin._retention_settings_open)
        self.assertEqual(self.plugin._pending_delete, original)
        self.press("N")
        self.open_settings()
        self.assertIsNone(self.plugin._pending_delete)


if __name__ == "__main__":
    unittest.main()
