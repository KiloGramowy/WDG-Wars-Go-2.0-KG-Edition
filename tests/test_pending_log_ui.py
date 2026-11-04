"""Real Sync browser methods with recorded Pyxel input/drawing and no I/O."""
import copy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.wardrive_upload import WardriveUpload


class PyxelRecorder:
    def __init__(self):
        self.frame_count = 10
        self.pressed = set()
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("KEY_"):
            return name
        return lambda *args: self.calls.append((name, args))

    def btnp(self, key, *unused):
        return key in self.pressed


def entry(session="2026-11-04_12-00-00", filename="meshtastic_nodes.csv", **changes):
    value = dict(session=session, filename=filename, kind="meshtastic", status="waiting_gps",
        records=4, eligible=0, no_gps=4, bytes=200, active=False, confirmed=False,
        can_delete=True, token=session + ":" + filename + ":original", reason="Waiting for receiver GPS.")
    value.update(changes)
    return value


class PendingLogUiTests(unittest.TestCase):
    def setUp(self):
        self.px = PyxelRecorder()
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"pyxel": self.px}).start()
        with patch.object(WardriveUpload, "_load_state"):
            self.plugin = WardriveUpload()
        self.plugin.app = SimpleNamespace(level=1, _esc_consumed_frame=-1, _big_font=None)
        self.plugin.list_pending_logs = Mock(return_value=[entry()])
        self.plugin.delete_pending_log = Mock(return_value=dict(deleted=True, reason=""))
        self.plugin._pending_sessions = Mock(side_effect=AssertionError("Browser must not prune or upload"))
        self.plugin._start_upload = Mock(side_effect=AssertionError("Local browser cannot upload"))
        self.plugin._start_auth_poll = Mock(side_effect=AssertionError("Local browser cannot start auth"))

    def press(self, *keys, advance=True):
        if advance:
            self.px.frame_count += 1
        self.px.pressed = {getattr(self.px, "KEY_" + key) for key in keys}
        self.plugin.on_update()
        self.px.pressed.clear()

    def texts(self):
        return [args for name, args in self.px.calls if name == "text"]

    def assert_bounds(self):
        for x, y, text, color, *unused in self.texts():
            self.assertGreaterEqual(x, 0, text)
            self.assertGreaterEqual(y, 0, text)
            self.assertLessEqual(x + len(text) * 5, 640, text)
            self.assertLessEqual(y + 8, 360, text)

    def test_dedicated_local_action_needs_neither_level_nor_credentials_and_lists_waiting_gps(self):
        local = next(item for item in self.plugin.menu_items() if item.action == "open_pending_logs")
        self.assertEqual(local.label, "Pending Upload Logs")
        with patch.object(self.plugin, "_check_level", side_effect=AssertionError("Local files have no level gate")):
            getattr(self.plugin, local.action)()
        self.press("RETURN")
        self.plugin.draw(0, 0, 640, 360)
        self.assertTrue(self.plugin.overlay_active)
        self.assertTrue(self.plugin._pending_browser)
        self.assertIn("WAIT GPS", [row[2] for row in self.texts()])
        self.assertIn("0/4", [row[2] for row in self.texts()])
        self.plugin._pending_sessions.assert_not_called()
        self.plugin._start_upload.assert_not_called()
        self.plugin._start_auth_poll.assert_not_called()
        self.plugin.delete_pending_log.assert_not_called()
        self.assertEqual(self.plugin._api_key, "")
        self.assert_bounds()

    def test_open_and_delete_key_frames_require_a_separate_later_confirmation(self):
        self.plugin.open_pending_logs()
        self.press("D", "Y", advance=False)
        self.assertIsNone(self.plugin._pending_delete)
        self.press("D", "Y")
        self.assertIsNotNone(self.plugin._pending_delete)
        self.press("Y", advance=False)
        self.plugin.delete_pending_log.assert_not_called()
        self.press("Y")
        row = entry()
        self.plugin.delete_pending_log.assert_called_once_with(row["session"], row["filename"], row["token"])
        self.assertIsNone(self.plugin._pending_delete)

    def test_normal_sync_entry_keeps_its_existing_level_gate(self):
        with patch.object(self.plugin, "_check_level", return_value=False) as check:
            self.plugin.open_overlay()
        check.assert_called_once_with()
        self.assertFalse(self.plugin.overlay_active)
        self.plugin.list_pending_logs.assert_not_called()
        self.plugin._start_auth_poll.assert_not_called()

    def test_successful_last_file_deletion_refreshes_to_an_empty_stable_browser(self):
        self.plugin.list_pending_logs.side_effect = [[entry()], []]
        self.plugin.open_pending_logs()
        self.press("D")
        self.press("Y")
        self.plugin.draw(0, 0, 640, 360)
        self.assertIn("No pending upload logs.", [row[2] for row in self.texts()])
        self.assertIn("0/0", [row[2] for row in self.texts()])
        self.assertEqual(self.plugin._pending_sel, 0)
        self.assertIsNone(self.plugin._pending_delete)
        self.assertTrue(self.plugin.overlay_active)
        self.assert_bounds()

    def test_confirmation_binds_exact_token_and_stale_failure_refreshes_without_deleting_new_selection(self):
        original = entry()
        self.plugin.open_pending_logs()
        self.press("D")
        replacement = entry(session="2026-11-04_13-00-00", filename="wardriving.csv", token="replacement")
        self.plugin._pending_rows[0] = replacement
        self.plugin.list_pending_logs.return_value = [replacement]
        self.plugin.delete_pending_log.return_value = dict(deleted=False, reason="Log changed. Refresh and review it again.")
        self.press("Y")
        self.plugin.delete_pending_log.assert_called_once_with(
            original["session"], original["filename"], original["token"])
        self.assertEqual(self.plugin._pending_rows, [replacement])
        self.assertIn("Log changed", self.plugin._pending_message)
        self.press("Y")
        self.assertEqual(self.plugin.delete_pending_log.call_count, 1)

    def test_cancel_and_escape_never_delete_and_preserve_the_correct_parent_overlay(self):
        self.plugin.open_pending_logs()
        self.press("D")
        self.press("N")
        self.assertIsNone(self.plugin._pending_delete)
        self.assertTrue(self.plugin._pending_browser)
        self.press("D")
        self.press("ESCAPE")
        self.assertTrue(self.plugin._pending_browser)
        self.assertEqual(self.plugin.app._esc_consumed_frame, self.px.frame_count)
        self.press("ESCAPE")
        self.assertFalse(self.plugin.overlay_active)
        self.plugin._exec("show_pending")
        self.assertTrue(self.plugin._pending_from_sync)
        self.press("ESCAPE")
        self.assertTrue(self.plugin.overlay_active)
        self.assertFalse(self.plugin._pending_browser)
        self.plugin.delete_pending_log.assert_not_called()

    def test_protected_rows_and_upload_in_progress_cannot_open_or_execute_deletion(self):
        for changes in (dict(active=True), dict(confirmed=True), dict(status="unreadable"),
                        dict(status="active"), dict(status="confirmed"), dict(can_delete=False)):
            with self.subTest(changes=changes):
                self.plugin.list_pending_logs.return_value = [entry(**changes)]
                self.plugin.open_pending_logs()
                self.press("D")
                self.assertIsNone(self.plugin._pending_delete)
        self.plugin.list_pending_logs.return_value = [entry()]
        self.plugin.open_pending_logs()
        self.plugin._uploading = True
        self.press("D")
        self.assertIsNone(self.plugin._pending_delete)
        self.plugin._uploading = False
        self.press("D")
        self.plugin._uploading = True
        self.press("Y")
        self.assertIn("Upload in progress", self.plugin._pending_message)
        self.plugin.delete_pending_log.assert_not_called()

    def test_file_errors_are_visible_without_starting_network_or_leaving_a_confirmation(self):
        self.plugin.list_pending_logs.side_effect = OSError("private diagnostic")
        self.plugin.open_pending_logs()
        self.assertIn("Could not read", self.plugin._pending_message)
        self.assertNotIn("private diagnostic", self.plugin._pending_message)
        self.plugin.list_pending_logs.side_effect = None
        self.plugin.list_pending_logs.return_value = [entry()]
        self.press("R")
        self.press("D")
        self.plugin.delete_pending_log.side_effect = OSError("private diagnostic")
        self.press("Y")
        self.assertIn("Could not delete", self.plugin._pending_message)
        self.assertIsNone(self.plugin._pending_delete)
        self.plugin._start_upload.assert_not_called()

    def test_all_pages_and_confirmation_fit_original_fullscreen_font_geometry(self):
        rows = [entry(session=f"2026-11-04_12-{index:02d}-00", records=10**12, eligible=10**11,
                      no_gps=10**10, bytes=10**12, reason="Long reason. " * 30) for index in range(31)]
        self.plugin.list_pending_logs.return_value = copy.deepcopy(rows)
        self.plugin.open_pending_logs()
        self.plugin.draw(0, 0, 640, 360)
        self.assertIn(("rect", (0, 0, 640, 360, 0)), self.px.calls)
        self.assertIn(("text", (4, 3, "PENDING UPLOAD LOGS", 3)), self.px.calls)
        self.assert_bounds()
        self.press("PAGEDOWN")
        self.assertGreater(self.plugin._pending_sel, 0)
        self.press("END")
        self.px.calls.clear()
        self.plugin.draw(0, 0, 640, 360)
        self.assertIn(rows[-1]["session"], [row[2] for row in self.texts()])
        self.assert_bounds()
        self.press("D")
        self.px.calls.clear()
        self.plugin.draw(0, 0, 640, 360)
        self.assertIn("DELETE THIS UPLOAD LOG?", [row[2] for row in self.texts()])
        self.assertIn(rows[-1]["session"], [row[2] for row in self.texts()])
        self.assert_bounds()
        self.press("N")
        self.press("HOME")
        self.assertEqual(self.plugin._pending_sel, 0)

    def test_auth_pin_has_priority_over_browser_confirmation_and_escape(self):
        self.plugin.open_pending_logs()
        self.press("D")
        captured = self.plugin._pending_delete
        with patch.object(self.plugin, "_auth_pin_active", return_value=True), \
                patch.object(self.plugin, "_draw_auth_pin") as draw_pin:
            self.press("Y", "ESCAPE")
            self.assertEqual(self.plugin._pending_delete, captured)
            self.assertEqual(self.plugin.app._esc_consumed_frame, -1)
            self.plugin.draw(0, 0, 640, 360)
            draw_pin.assert_called_once_with(640, 360)
        self.plugin.delete_pending_log.assert_not_called()
        self.press("N")
        self.assertIsNone(self.plugin._pending_delete)


if __name__ == "__main__":
    unittest.main()
