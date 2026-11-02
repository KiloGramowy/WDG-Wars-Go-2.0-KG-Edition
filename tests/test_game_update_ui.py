"""Actual updater UI methods with recorded drawing and fake release/device I/O.

No GitHub requests, player files, radio, installer writes or real Pyxel window.
The lifecycle tests verify that notes/confirmation cannot install accidentally
and the final transaction runs only after the main thread has stopped devices.
"""
import ast
import copy
import logging
import os
from pathlib import Path
from queue import Queue
import random
import re
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs import __version__ as INSTALLED_VERSION


class PyxelRecorder:
    def __init__(self):
        self.calls = []
        self.pressed = set()
        self.frame_count = 100
        self.Image = SimpleNamespace(from_image=lambda path: ("portrait", path))

    def __getattr__(self, name):
        if name.startswith("KEY_"):
            return name
        return lambda *args: self.calls.append((name, args))

    def btnp(self, key, *args):
        return key in self.pressed


class FakeUpdates:
    def __init__(self):
        self.seen = set()
        self.notice_pending = Mock(side_effect=lambda release: release.release_id not in self.seen)
        self.acknowledge = Mock(side_effect=lambda release: self.seen.add(release.release_id) or True)
        self.notice_key = Mock(side_effect=lambda release: "github:1441947361:" + release.channel + ":" + release.release_id)
        self.source_info = Mock(return_value=dict(repository_id=1441947361,
            repository="KiloGramowy/WatchDogsGo-1",
            url="https://github.com/KiloGramowy/WatchDogsGo-1", verified=False))
        self.check = Mock()
        self.prepare = Mock(return_value="verified-plan")
        self.discard = Mock()
        self.install = Mock(return_value=SimpleNamespace(version="0.9.17"))


class RecordedThread:
    started = []

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.started.append(self)


PX = PyxelRecorder()
SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
METHODS = {"_init_game_updates", "_check_game_update", "_request_game_update_check",
           "_poll_game_update_events", "_game_update_notice_allowed", "_maybe_show_game_update_notice",
           "_update_game_update_notice", "_draw_game_update_notice", "_start_game_update",
           "_gameupd_note_lines", "_update_gameupd_screen", "_gameupd_prepare", "_gameupd_log_add",
           "_gameupd_do", "_gameupd_discard_plan", "_draw_gameupd_screen", "_draw_menu", "_draw_boot_screen",
           "_gameupd_source_info", "_gameupd_open_source", "_gameupd_open_source_worker",
           "update", "_draw_inner", "draw"}
ENV = dict(__name__="watchdogs.update_ui_test", __package__="watchdogs", __file__=str(SOURCE),
           Path=Path, Queue=Queue, pyxel=PX, logging=logging, random=random, re=re, time=time,
           threading=SimpleNamespace(Thread=RecordedThread), GameUpdates=lambda **kwargs: FakeUpdates())
for statement in TREE.body:
    if isinstance(statement, ast.Assign):
        try:
            exec(compile(ast.Module(body=[statement], type_ignores=[]), str(SOURCE), "exec"), ENV)
        except (NameError, AttributeError, TypeError):
            pass
node = ast.ClassDef(name="Game", bases=[], keywords=[], decorator_list=[],
                   body=[copy.deepcopy(n) for n in GAME.body if isinstance(n, ast.FunctionDef) and n.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(SOURCE), "exec"), ENV)
Game = ENV["Game"]


def release(identity="release-1", notes="Actual changes.\nSettings are kept."):
    return SimpleNamespace(repository="KiloGramowy/esp32-watch-dogs", channel="stable",
                           release_id=identity, version="0.9.17", source_commit="a" * 40,
                           title="WatchDogsGo update", notes=notes)


def result(status="available", item=None):
    return SimpleNamespace(status=status, installed_version="0.9.16",
                           release=release() if item is None and status == "available" else item,
                           message={"available": "A new release is available.", "error": "Could not check GitHub.",
                                    "current": "You are up to date.", "unpublished": "No release published yet."}[status])


def game(backend=None):
    PX.calls.clear()
    PX.pressed.clear()
    PX.frame_count = 100
    RecordedThread.started.clear()
    g = Game()
    g._init_game_updates()
    if backend is not None:
        g._game_updates = backend
    g._boot_phase = False
    g.menu_open = False
    g.menu_cat = next(i for i, row in enumerate(ENV["MENU_CATS"]) if row[0] == "SYSTEM")
    g.menu_sel = 0
    g._plugins = []
    g._shutting_down = False
    g._cleanup_done = False
    g._cleanup_errors = []
    g._esc_consumed_frame = -1
    g._esp32 = True
    g._is_running = lambda key: False
    g._term_add = Mock()
    g.events = []
    def cleanup():
        g.events.append("stop-and-save")
        g._shutting_down = g._cleanup_done = True
    g._cleanup = Mock(side_effect=cleanup)
    return g


class GameUpdateUiTests(unittest.TestCase):
    def press(self, g, key, method="_update_gameupd_screen"):
        PX.frame_count += 1
        PX.pressed = {key}
        getattr(g, method)()
        PX.pressed.clear()

    def texts(self):
        return [args for name, args in PX.calls if name == "text"]

    def available(self, g, item=None):
        g._game_update_events.put(("check", result(item=item)))
        g._poll_game_update_events()

    def assert_above_output(self):
        for x, y, text, color, *rest in self.texts():
            self.assertGreaterEqual(y, ENV["HUD_TOP"], text)
            self.assertLessEqual(y + 8, ENV["TERM_Y"], text)
            self.assertGreaterEqual(x, 100, text)
            self.assertLessEqual(x + len(text) * 5, 540, text)

    def test_background_check_only_queues_and_main_loop_applies_result(self):
        g = game()
        g._game_updates.check.return_value = result()
        g._check_game_update()
        self.assertFalse(g._game_update_available)
        g._term_add.assert_not_called()
        g._poll_game_update_events()
        self.assertTrue(g._game_update_available)
        self.assertIsNotNone(g._game_update_notice_queued)
        self.assertIsNone(g._game_update_notice)

    def test_check_is_background_deduplicated_and_new_launch_checks_again(self):
        g = game()
        g._request_game_update_check()
        g._request_game_update_check()
        self.assertTrue(g._game_update_checking)
        self.assertEqual(len(RecordedThread.started), 1)
        g._game_updates.check.assert_not_called()
        g = game()
        g._request_game_update_check()
        self.assertEqual(len(RecordedThread.started), 1)

    def test_notice_waits_for_main_screen_before_persisting(self):
        blockers = ("menu_open", "_boot_phase", "input_mode", "_mt_screen", "_mc_screen",
                    "_gameupd_screen", "_gps_wait_dialog", "confirm_quit", "loot_screen",
                    "_cluster_popup", "_flash_screen", "_lora_protocol_picker")
        for flag in blockers:
            with self.subTest(flag=flag):
                g = game()
                self.available(g)
                setattr(g, flag, True)
                g._maybe_show_game_update_notice()
                g._game_updates.acknowledge.assert_not_called()
                self.assertIsNone(g._game_update_notice)
                setattr(g, flag, False)
                g._maybe_show_game_update_notice()
                g._game_updates.acknowledge.assert_called_once()
                self.assertIsNotNone(g._game_update_notice)

    def test_notice_waits_for_plugin_overlay_and_auth_pin(self):
        for plugin in (SimpleNamespace(overlay_active=True), SimpleNamespace(_auth_pin_active=lambda: True)):
            g = game()
            self.available(g)
            g._plugins = [plugin]
            g._maybe_show_game_update_notice()
            g._game_updates.acknowledge.assert_not_called()
            self.assertIsNone(g._game_update_notice)

    def test_no_keeps_update_available_and_same_release_never_nags_after_restart(self):
        backend = FakeUpdates()
        g = game(backend)
        self.available(g)
        g._maybe_show_game_update_notice()
        self.press(g, PX.KEY_N, "_update_game_update_notice")
        self.assertIsNone(g._game_update_notice)
        self.assertTrue(g._game_update_available)
        self.assertFalse(g._gameupd_screen)
        again = game(backend)
        self.available(again)
        again._maybe_show_game_update_notice()
        self.assertIsNone(again._game_update_notice)
        self.available(again, release("release-2"))
        again._maybe_show_game_update_notice()
        self.assertEqual(again._game_update_notice.release_id, "release-2")

    def test_changed_notes_do_not_retrigger_same_release(self):
        g = game()
        self.available(g)
        g._maybe_show_game_update_notice()
        self.press(g, PX.KEY_N, "_update_game_update_notice")
        self.available(g, release(notes="Edited notes for exactly the same release."))
        g._maybe_show_game_update_notice()
        self.assertIsNone(g._game_update_notice)
        g._game_updates.acknowledge.assert_called_once()

    def test_ok_opens_notes_and_consumes_enter_without_preparing_or_installing(self):
        g = game()
        self.available(g)
        g._maybe_show_game_update_notice()
        self.press(g, PX.KEY_RETURN, "_update_game_update_notice")
        self.assertTrue(g._gameupd_screen)
        self.assertIsNone(g._game_update_notice)
        PX.pressed = {PX.KEY_RETURN}
        g._update_gameupd_screen()
        g._game_updates.prepare.assert_not_called()
        g._game_updates.install.assert_not_called()
        self.assertEqual(RecordedThread.started, [])
        self.assertEqual(ENV["MENU_CATS"][g.menu_cat][0], "SYSTEM")
        self.assertEqual(ENV["MENU_CATS"][g.menu_cat][1][g.menu_sel][3], "_gameupd")

    def test_escape_notice_consumed_and_newly_shown_notice_ignores_old_key(self):
        g = game()
        self.available(g)
        g._maybe_show_game_update_notice()
        PX.pressed = {PX.KEY_ESCAPE}
        g._update_game_update_notice()
        self.assertIsNotNone(g._game_update_notice)
        self.press(g, PX.KEY_ESCAPE, "_update_game_update_notice")
        self.assertIsNone(g._game_update_notice)
        self.assertEqual(g._esc_consumed_frame, PX.frame_count)

    def test_notice_history_error_does_not_repeat_or_claim_up_to_date(self):
        g = game()
        self.available(g)
        g._game_updates.acknowledge.side_effect = OSError("disk full")
        with self.assertLogs(level="ERROR"):
            g._maybe_show_game_update_notice()
        self.assertIsNone(g._game_update_notice)
        self.assertTrue(g._game_update_available)
        self.available(g)
        g._maybe_show_game_update_notice()
        g._game_updates.acknowledge.assert_called_once()

    def test_offline_error_keeps_last_confirmed_release_and_white_menu(self):
        g = game()
        self.available(g)
        previous = g._game_update_release
        g._game_update_events.put(("check", result("error")))
        g._poll_game_update_events()
        self.assertEqual(g._game_update_status, "error")
        self.assertTrue(g._game_update_available)
        self.assertIs(g._game_update_release, previous)
        self.assertNotIn("up to date", g._game_update_message.lower())

    def test_unpublished_preview_cannot_prepare_install(self):
        g = game()
        g._game_update_events.put(("check", result("unpublished")))
        g._poll_game_update_events()
        g._start_game_update()
        self.press(g, PX.KEY_RETURN)
        self.assertFalse(g._game_update_available)
        self.assertEqual(RecordedThread.started, [])
        g._game_updates.install.assert_not_called()

    def test_long_notes_wrap_without_loss_and_end_reaches_last_line(self):
        g = game()
        notes = "\n".join("Item %02d: " % i + "word " * 40 for i in range(30)) + "\nFINAL CHANGE"
        self.available(g, release(notes=notes))
        lines = g._gameupd_note_lines()
        self.assertTrue(all(len(line) <= 83 for line in lines))
        self.assertEqual(" ".join(" ".join(lines).split()), " ".join(notes.split()))
        g._start_game_update()
        self.press(g, PX.KEY_END)
        g._draw_gameupd_screen()
        self.assertIn("FINAL CHANGE", [args[2] for args in self.texts()])
        self.assert_above_output()
        self.press(g, PX.KEY_HOME)
        self.assertEqual(g._gameupd_scroll, 0)
        self.press(g, PX.KEY_PAGEDOWN)
        self.assertGreater(g._gameupd_scroll, 0)
        self.press(g, PX.KEY_PAGEUP)
        self.assertEqual(g._gameupd_scroll, 0)

    def test_numbered_change_titles_are_yellow_and_fact_lines_stay_white(self):
        g = game()
        notes = ('1. XP AND PLAYER DATA\n---------------------\nBefore: old saves could lose XP.\n'
                 'Changed: preserve XP and create a backup.\n\n'
                 '2. BLE SCAN COMPLETION\nBefore: bridge scan timed out.\n'
                 'Changed: accept its final completion marker.')
        self.available(g, release(notes=notes))
        g._draw_gameupd_screen()
        colors = {args[2]: args[3] for args in self.texts()}
        self.assertEqual(colors['1. XP AND PLAYER DATA'], ENV['C_WARNING'])
        self.assertEqual(colors['2. BLE SCAN COMPLETION'], ENV['C_WARNING'])
        self.assertEqual(colors['---------------------'], ENV['C_WARNING'])
        self.assertEqual(colors['Before: old saves could lose XP.'], ENV['C_TEXT'])
        self.assertEqual(colors['Changed: preserve XP and create a backup.'], ENV['C_TEXT'])
        self.assertIn(('rect', (108, 115, 424, 9, 1)), PX.calls)
        self.assert_above_output()
        g._game_updates.prepare.assert_not_called()
        g._game_updates.install.assert_not_called()

    def test_versioned_notes_show_only_missing_blocks_without_changing_release(self):
        notes = ('One confirmation installs all missing fixes.\n\n'
                 'RELEASE 0.9.19 - MAP MICROFIX\nCurrent map fixes.\n\n'
                 'RELEASE 0.9.18 - PREVIOUS FIXES\nEarlier XP and BLE fixes.')
        for installed, previous_visible in (("0.9.17", True), ("0.9.18", False), ("0.9.19", False)):
            with self.subTest(installed=installed):
                g = game()
                item = release(notes=notes)
                item.version = "0.9.19"
                original = copy.deepcopy(vars(item))
                self.available(g, item)
                g._game_installed_version = installed
                text = "\n".join(g._gameupd_note_lines())
                self.assertIn("One confirmation installs all missing fixes.", text)
                self.assertIn("RELEASE 0.9.19 - MAP MICROFIX", text)
                self.assertEqual("RELEASE 0.9.18 - PREVIOUS FIXES" in text, previous_visible)
                self.assertEqual(vars(item), original)
                g._game_updates.prepare.assert_not_called()
                g._game_updates.install.assert_not_called()

    def test_release_headings_use_the_existing_yellow_title_style(self):
        g = game()
        notes = ('RELEASE 0.9.19 - MAP MICROFIX\nCurrent map fixes.\n\n'
                 'RELEASE 0.9.18 - PREVIOUS FIXES\nEarlier XP and BLE fixes.')
        item = release(notes=notes)
        item.version = "0.9.19"
        self.available(g, item)
        g._game_installed_version = "0.9.17"
        g._draw_gameupd_screen()
        colors = {args[2]: args[3] for args in self.texts()}
        for header in ("RELEASE 0.9.19 - MAP MICROFIX", "RELEASE 0.9.18 - PREVIOUS FIXES"):
            self.assertEqual(colors[header], ENV["C_WARNING"])
        self.assertEqual(colors["Current map fixes."], ENV["C_TEXT"])
        self.assertEqual(colors["Earlier XP and BLE fixes."], ENV["C_TEXT"])
        self.assert_above_output()

    def test_note_selection_still_prepares_the_same_complete_release(self):
        notes = ('RELEASE 0.9.19 - MAP MICROFIX\nCurrent map fixes.\n\n'
                 'RELEASE 0.9.18 - PREVIOUS FIXES\nEarlier XP and BLE fixes.')
        for installed in ("0.9.17", "0.9.18"):
            with self.subTest(installed=installed):
                g = game()
                item = release(notes=notes)
                item.version = "0.9.19"
                self.available(g, item)
                g._game_installed_version = installed
                self.assertIn("MAP MICROFIX", "\n".join(g._gameupd_note_lines()))
                self.press(g, PX.KEY_RETURN)
                self.assertEqual(len(RecordedThread.started), 1)
                thread = RecordedThread.started[0]
                self.assertEqual(thread.args, (item,))
                thread.target(*thread.args)
                g._game_updates.prepare.assert_called_once_with(item)
                self.assertEqual(item.notes, notes)
                self.assertEqual(item.source_commit, "a" * 40)
                g._game_updates.install.assert_not_called()

    def test_scrolled_change_title_keeps_yellow_color(self):
        g = game()
        notes = 'Body line.\n' * 12 + '\n3. MESHCORE CONTACT LIST\nChanged: show empty list.'
        self.available(g, release(notes=notes))
        g._gameupd_scroll = 13
        g._draw_gameupd_screen()
        colors = {args[2]: args[3] for args in self.texts()}
        self.assertEqual(colors['3. MESHCORE CONTACT LIST'], ENV['C_WARNING'])
        self.assertEqual(colors['Changed: show empty list.'], ENV['C_TEXT'])
        self.assertEqual(' '.join(g._gameupd_note_lines()).split(), notes.split())

    def test_notes_confirmation_notice_and_logs_fit_above_output(self):
        for state in ("notes", "confirm", "log", "notice"):
            with self.subTest(state=state):
                g = game()
                self.available(g, release(notes="Lots of changes. " * 200))
                if state == "notice":
                    g._game_update_notice = g._game_update_release
                    g._draw_game_update_notice()
                else:
                    g._start_game_update()
                    g._gameupd_confirm = state == "confirm"
                    if state == "log":
                        g._gameupd_log_add("[UPD] Saved a code backup and kept all settings.")
                    g._draw_gameupd_screen()
                self.assert_above_output()

    def test_avatar_is_only_inside_update_window_and_versions_remain_clean(self):
        g = game()
        g.menu_open = True
        g._draw_menu()
        self.assertFalse(any(name == "blt" for name, args in PX.calls))
        self.assertIn("[i] Update Game", [args[2] for args in self.texts()])
        PX.calls.clear()
        g._draw_gameupd_screen()
        portraits = [args for name, args in PX.calls if name == "blt"]
        self.assertEqual(len(portraits), 1)
        self.assertEqual(portraits[0][-2:], (32, 32))
        self.assertIn("Current: v" + INSTALLED_VERSION, [args[2] for args in self.texts()])
        self.assertFalse(any("KG Edition" in args[2] for args in self.texts()))

    def test_menu_keeps_original_label_position_and_dim_white_state(self):
        g = game()
        positions = []
        for available, expected in ((False, ENV["C_DIM"]), (True, ENV["C_TEXT"])):
            PX.calls.clear()
            g._game_update_available = available
            g._draw_menu()
            row = next(args for args in self.texts() if args[2] == "[i] Update Game")
            positions.append(row[:2])
            self.assertEqual(row[3], expected)
        self.assertEqual(*positions)

    def verified_source(self, g, repository="KiloGramowy/WatchDogsGo-1"):
        g._game_updates.source_info.return_value = dict(repository_id=1441947361,
            repository=repository, url="https://github.com/" + repository, verified=True)

    def test_source_header_is_cached_and_follows_verified_rename(self):
        g = game()
        for repository in ("KiloGramowy/WatchDogsGo-1", "KiloGramowy/WatchDogsGo"):
            PX.calls.clear()
            self.verified_source(g, repository)
            g._draw_gameupd_screen()
            self.assertIn("Source: " + repository, [args[2] for args in self.texts()])
            self.assertTrue(any("O GitHub" in args[2] for args in self.texts()))
            self.assert_above_output()
        self.assertEqual(RecordedThread.started, [])
        g._game_updates.check.assert_not_called()
        g._game_updates.prepare.assert_not_called()
        g._game_updates.install.assert_not_called()

    def test_offline_check_still_displays_installed_release_notes(self):
        g = game()
        g._game_update_status = "error"
        g._game_update_message = "Cannot check for updates. Check network and GitHub access."
        notes = "WatchDogsGo 0.9.17\nMesh and wardriving improvements."
        with patch.object(Path, "read_text", return_value=notes):
            g._draw_gameupd_screen()
        values = [args[2] for args in self.texts()]
        self.assertIn("INSTALLED RELEASE NOTES", values)
        self.assertIn("WatchDogsGo 0.9.17", values)
        self.assertIn("Mesh and wardriving improvements.", values)
        self.assertFalse(any("PRIVATE PREVIEW" in value for value in values))
        self.assert_above_output()
        g._game_updates.check.assert_not_called()
        g._game_updates.install.assert_not_called()

    def test_missing_installed_notes_uses_neutral_fallback(self):
        g = game()
        with patch.object(Path, "read_text", side_effect=OSError("missing local notes")):
            g._draw_gameupd_screen()
        values = [args[2] for args in self.texts()]
        self.assertIn("INSTALLED RELEASE NOTES", values)
        self.assertIn("Release notes are unavailable.", values)
        self.assertFalse(any("PRIVATE PREVIEW" in value for value in values))
        self.assertFalse(any("No release has been published" in value for value in values))
        self.assert_above_output()

    def test_long_source_name_still_fits_existing_header(self):
        g = game()
        self.verified_source(g, "KiloGramowy/" + "a" * 100)
        g._draw_gameupd_screen()
        self.assert_above_output()
        source = next(args[2] for args in self.texts() if args[2].startswith("Source: "))
        self.assertTrue(source.endswith("..."))

    def test_o_requires_verified_source_and_only_requests_check(self):
        g = game()
        g._start_game_update()
        self.press(g, PX.KEY_O)
        self.assertFalse(g._gameupd_source_opening)
        self.assertTrue(g._game_update_checking)
        self.assertEqual(len(RecordedThread.started), 1)
        self.assertEqual(RecordedThread.started[0].target.__name__, "_check_game_update")
        g._game_updates.prepare.assert_not_called()
        g._game_updates.install.assert_not_called()
        self.assertIn("Checking the source", g._gameupd_log[-1])

    def test_o_hands_verified_renamed_url_to_worker_without_installing(self):
        g = game()
        self.verified_source(g, "KiloGramowy/WatchDogsGo")
        g._start_game_update()
        self.press(g, PX.KEY_O)
        self.assertTrue(g._gameupd_source_opening)
        self.assertEqual(len(RecordedThread.started), 1)
        thread = RecordedThread.started[0]
        self.assertEqual(thread.target.__name__, "_gameupd_open_source_worker")
        self.assertEqual(thread.args, ("https://github.com/KiloGramowy/WatchDogsGo",))
        self.press(g, PX.KEY_O)
        self.assertEqual(len(RecordedThread.started), 1)
        g._game_updates.check.assert_not_called()
        g._game_updates.prepare.assert_not_called()
        g._game_updates.install.assert_not_called()

    def test_browser_worker_uses_normal_desktop_without_shell(self):
        g = game()
        self.verified_source(g)
        url = "https://github.com/KiloGramowy/WatchDogsGo-1"
        with patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                patch.object(os, "geteuid", return_value=1000, create=True), \
                patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            g._gameupd_open_source_worker(url)
        self.assertEqual(run.call_args.args[0], ["xdg-open", url])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        g._poll_game_update_events()
        self.assertFalse(g._gameupd_source_opening)
        self.assertIn("GitHub opened", g._gameupd_log[-1])
        g._game_updates.install.assert_not_called()

    def test_browser_worker_drops_root_to_desktop_user(self):
        g = game()
        self.verified_source(g)
        owner = SimpleNamespace(pw_uid=1000, pw_name="x", pw_dir="/home/x")
        pwd = SimpleNamespace(getpwnam=Mock(return_value=owner), getpwuid=Mock(return_value=owner))
        with patch.dict(os.environ, {"DISPLAY": ":0", "SUDO_USER": "x"}, clear=True), \
                patch.dict(sys.modules, {"pwd": pwd}), \
                patch.object(os, "geteuid", return_value=0, create=True), \
                patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            g._gameupd_open_source_worker("https://github.com/KiloGramowy/WatchDogsGo-1")
        command = run.call_args.args[0]
        self.assertEqual(command[:7], ["sudo", "-n", "-H", "-u", "x", "--", "env"])
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1000", command)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", command)
        self.assertIn("XAUTHORITY=" + str(Path(owner.pw_dir) / ".Xauthority"), command)
        self.assertEqual(command[-2:], ["xdg-open", "https://github.com/KiloGramowy/WatchDogsGo-1"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_browser_worker_rejects_unverified_or_malformed_source(self):
        for url in ("https://example.invalid/collect", "https://github.com/KiloGramowy/WatchDogsGo-1?token=x"):
            with self.subTest(url=url):
                g = game()
                self.verified_source(g)
                g._game_updates.source_info.return_value["url"] = url
                with patch("subprocess.run") as run:
                    g._gameupd_open_source_worker(url)
                run.assert_not_called()
                g._poll_game_update_events()
                self.assertIn("Check the source again", g._gameupd_log[-1])
                g._game_updates.install.assert_not_called()

    def test_browser_error_stays_generic_and_does_not_change_update_state(self):
        g = game()
        self.verified_source(g)
        self.available(g)
        with patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                patch.object(os, "geteuid", return_value=1000, create=True), \
                patch("subprocess.run", side_effect=OSError("private diagnostics must stay hidden")):
            g._gameupd_open_source_worker("https://github.com/KiloGramowy/WatchDogsGo-1")
        g._poll_game_update_events()
        self.assertTrue(g._game_update_available)
        self.assertNotIn("private diagnostics", g._gameupd_log[-1])
        self.assertIn("not confirmed", g._gameupd_log[-1])

    def test_headless_browser_is_not_launched(self):
        g = game()
        self.verified_source(g)
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run") as run:
            g._gameupd_open_source_worker("https://github.com/KiloGramowy/WatchDogsGo-1")
        run.assert_not_called()
        g._poll_game_update_events()
        self.assertIn("No desktop browser session", g._gameupd_log[-1])

    def test_browser_worker_refuses_to_start_a_root_browser(self):
        g = game()
        self.verified_source(g)
        pwd = SimpleNamespace(getpwnam=Mock(), getpwuid=Mock())
        with patch.dict(os.environ, {"DISPLAY": ":0", "SUDO_USER": "root"}, clear=True), \
                patch.dict(sys.modules, {"pwd": pwd}), \
                patch.object(os, "geteuid", return_value=0, create=True), \
                patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0)), \
                patch("subprocess.run") as run:
            g._gameupd_open_source_worker("https://github.com/KiloGramowy/WatchDogsGo-1")
        run.assert_not_called()
        pwd.getpwuid.assert_not_called()
        g._poll_game_update_events()
        self.assertIn("desktop user is required", g._gameupd_log[-1])

    def test_same_release_notice_key_survives_repository_rename(self):
        g = game()
        self.available(g)
        g._maybe_show_game_update_notice()
        self.assertIsNotNone(g._game_update_notice)
        g._game_update_notice = None
        item = release()
        item.repository = "KiloGramowy/WatchDogsGo"
        self.available(g, item)
        g._maybe_show_game_update_notice()
        self.assertIsNone(g._game_update_notice)
        self.assertEqual(g._game_updates.acknowledge.call_count, 1)

    def test_boot_keeps_locosp_and_adds_modified_credit_below(self):
        g = game()
        g._boot_frame = 50
        g._boot_checks = [("  Ready", ENV["C_SUCCESS"])]
        g._draw_logo_word = Mock()
        g._draw_boot_screen()
        credits = {args[2]: args for args in self.texts()}
        self.assertIn("by LOCOSP", credits)
        self.assertIn("Modified by KiloGramowy", credits)
        self.assertGreater(credits["Modified by KiloGramowy"][1], credits["by LOCOSP"][1])
        self.assertFalse(any("KG Edition" in value for value in credits))

    def test_preparation_is_not_install_and_separate_cancel_leaves_game_running(self):
        g = game()
        self.available(g)
        g._start_game_update()
        self.press(g, PX.KEY_RETURN)
        self.assertEqual(len(RecordedThread.started), 1)
        g._cleanup.assert_not_called()
        g._game_updates.install.assert_not_called()
        thread = RecordedThread.started[0]
        thread.target(*thread.args)
        g._poll_game_update_events()
        self.assertTrue(g._gameupd_confirm)
        self.press(g, PX.KEY_N)
        self.assertFalse(g._gameupd_confirm)
        self.assertIsNone(g._gameupd_plan)
        g._game_updates.discard.assert_called_once_with("verified-plan")
        self.assertFalse(g._shutting_down)
        g._cleanup.assert_not_called()
        g._game_updates.install.assert_not_called()

    def test_yes_stops_saves_then_installs_after_one_paint_frame(self):
        g = game()
        self.available(g)
        g._start_game_update()
        g._gameupd_confirm = True
        g._gameupd_plan = "verified-plan"
        def install(plan, quiescent):
            self.assertTrue(g._cleanup_done)
            self.assertTrue(quiescent)
            g.events.append("install")
            return SimpleNamespace(version="0.9.17")
        g._game_updates.install.side_effect = install
        self.press(g, PX.KEY_Y)
        self.assertTrue(g._gameupd_closed_for_install)
        self.assertEqual(g.events, ["stop-and-save"])
        g._game_updates.install.assert_not_called()
        g._draw_gameupd_screen()
        PX.frame_count += 1
        g._update_gameupd_screen()
        self.assertEqual(g.events, ["stop-and-save", "install"])
        self.assertTrue(g._gameupd_done)
        self.assertTrue(g._gameupd_ok)
        self.assertFalse(g._game_update_available)

    def test_failed_device_cleanup_blocks_install_and_requires_relaunch(self):
        g = game()
        self.available(g)
        g._start_game_update()
        g._gameupd_confirm = True
        g._gameupd_plan = "verified-plan"
        g._cleanup_errors = ["LoRa worker did not stop"]
        self.press(g, PX.KEY_Y)
        g._game_updates.install.assert_not_called()
        self.assertTrue(g._gameupd_done)
        self.assertTrue(g._gameupd_closed_for_install)
        self.assertFalse(g._gameupd_ok)
        self.assertTrue(any("relaunch" in line for line in g._gameupd_log))

    def test_closed_transports_are_not_polled_after_install_cleanup(self):
        g = game()
        g._gameupd_closed_for_install = g._gameupd_done = True
        g._poll_serial = Mock(side_effect=AssertionError("closed serial polled"))
        g._poll_gps = Mock(side_effect=AssertionError("closed GPS polled"))
        g.update()
        g._poll_serial.assert_not_called()
        g._poll_gps.assert_not_called()
        g._draw_plugin_pin = Mock()
        g.draw()
        g._draw_plugin_pin.assert_not_called()

    def test_open_updater_blocks_global_loot_and_quit_keys_but_keeps_polling(self):
        g = game()
        g._game_check_started = True
        g._gameupd_screen = True
        g.loot_screen = False
        g._wd = None
        g._breath = g.scan_pulse = g.glitch_timer = 0
        g.player_lat = g.player_lon = 0
        g.proj = SimpleNamespace(smooth_move=Mock(), update=Mock())
        g.particles, g.msgs = [], []
        g._loot_points_ts = time.time()
        for name in ('_poll_serial', '_poll_gps', '_assign_missing_mesh_gps',
                     '_poll_lora', '_poll_reticulum', '_poll_sdr', '_poll_subghz',
                     '_poll_watch', '_poll_wpasec_result', '_poll_proximity',
                     '_dispatch_pending_esp_command', '_update_hack',
                     '_update_wardriving_loop'):
            setattr(g, name, Mock())
        g._update_gameupd_screen = Mock()
        PX.pressed = {PX.KEY_BACKQUOTE, PX.KEY_ESCAPE}
        g.update()
        self.assertFalse(g.loot_screen)
        g._update_gameupd_screen.assert_called_once_with()
        g._poll_serial.assert_called_once_with()
        g._poll_lora.assert_called_once_with()
        g._cleanup.assert_not_called()

    def test_prepare_error_does_not_stop_hardware(self):
        g = game()
        g._game_updates.prepare.side_effect = RuntimeError("Local code changed; update refused.")
        with self.assertLogs(level="ERROR"):
            g._gameupd_prepare(release())
        g._poll_game_update_events()
        self.assertFalse(g._gameupd_running)
        self.assertFalse(g._gameupd_confirm)
        self.assertFalse(g._shutting_down)
        g._cleanup.assert_not_called()
        self.assertIn("Local code changed", g._gameupd_log[-1])


if __name__ == "__main__":
    unittest.main()
