"""Real temporary Git histories and code transactions; no network or radio.

The transport substitutes a local repository for the fixed GitHub destination.
Every installed file, player fixture and notification preference is temporary.
"""
from dataclasses import replace
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from watchdogs import game_updates as updates


class PowerLoss(BaseException):
    pass


def git(root, *args):
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                           "-C", str(root), *args], capture_output=True, check=True)


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not path.is_symlink() and ".git" not in path.relative_to(root).parts}


@unittest.skipUnless(shutil.which("git"), "Git is required for isolated update tests")
class GameUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed_home = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.seed_home.cleanup)
        cls.seed = Path(cls.seed_home.name) / "seed"
        cls.seed.mkdir()
        git(cls.seed, "init", "-b", updates.BRANCH)
        git(cls.seed, "config", "user.name", "Update Tests")
        git(cls.seed, "config", "user.email", "updates@example.invalid")
        git(cls.seed, "config", "core.autocrlf", "false")
        initial = {".gitignore": "loot/\nsecrets.conf\n",
                   "watchdogs/__init__.py": '__version__ = "0.9.15"\n',
                   "watchdogs/app.py": 'VALUE = "legacy"\n',
                   "watchdogs/config.py": "USER_DEFAULT = 1\n",
                   "watchdogs/old_module.py": "OLD = True\n",
                   "watchdogs/__main__.py": "def _recover_game_update():\n    pass\n\ndef main():\n    _recover_game_update()\n",
                   "watchdogs/game_updates.py": "# Installed updater fixture\n",
                   "requirements.txt": "pyxel>=2.0.0\n",
                   "plugins/example.py": "EXAMPLE = True\n",
                   "assets/test.png": b"\x89PNG\x00\xffold\n",
                   "docs/README.md": "A non-runtime document.\n"}
        for name, data in initial.items():
            write(cls.seed, name, data)
        git(cls.seed, "add", ".")
        git(cls.seed, "commit", "-qm", "Legacy")
        cls.legacy = git(cls.seed, "rev-parse", "HEAD").stdout.decode().strip()
        write(cls.seed, "watchdogs/__init__.py", '__version__ = "0.9.16"\n')
        write(cls.seed, "watchdogs/app.py", 'VALUE = "installed overlay"\n')
        git(cls.seed, "add", ".")
        git(cls.seed, "commit", "-qm", "Installed source overlay")
        cls.base = git(cls.seed, "rev-parse", "HEAD").stdout.decode().strip()
        cls.base_files = snapshot(cls.seed)
        write(cls.seed, "watchdogs/__init__.py", '__version__ = "0.9.17"\n')
        write(cls.seed, "watchdogs/app.py", 'VALUE = "new release"\n')
        write(cls.seed, "watchdogs/new_feature.py", 'NEW = True\n')
        write(cls.seed, "assets/test.png", b"\x89PNG\x00\xffnew\n")
        (cls.seed / "watchdogs/old_module.py").unlink()
        git(cls.seed, "add", ".")
        git(cls.seed, "commit", "-qm", "Approved code")
        cls.target = git(cls.seed, "rev-parse", "HEAD").stdout.decode().strip()
        cls.feed = dict(schema=1, repository=updates.REPOSITORY, channel=updates.CHANNEL,
                        release_id="v0.9.17", version="0.9.17", source_commit=cls.target,
                        title="A new version", notes="\n".join("Full note " + str(i) for i in range(80)))
        write(cls.seed, updates.FEED_PATH, json.dumps(cls.feed))
        git(cls.seed, "add", ".")
        git(cls.seed, "commit", "-qm", "Advertise release")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.directory_path = Path(self.directory.name)
        self.remote = self.directory_path / "upstream"
        git(self.directory_path, "clone", "--no-hardlinks", str(self.seed), str(self.remote))
        git(self.remote, "config", "user.name", "Update Tests")
        git(self.remote, "config", "user.email", "updates@example.invalid")
        git(self.remote, "config", "core.autocrlf", "false")
        self.root = self.directory_path / "installed"
        git(self.directory_path, "clone", "--no-hardlinks", str(self.remote), str(self.root))
        git(self.root, "checkout", "--detach", self.legacy)
        git(self.root, "remote", "set-url", "origin", "https://github.com/LOCOSP/esp32-watch-dogs.git")
        for name, data in self.base_files.items():
            write(self.root, name, data)
        self.manifest = dict(source_commit=self.base, branch=updates.BRANCH,
                             files={name: {"sha256": hashlib.sha256(data).hexdigest()}
                                    for name, data in self.base_files.items() if updates._runtime(name)})
        write(self.root, updates.MANIFEST_PATH, json.dumps(self.manifest))
        self.home = self.directory_path / "home"
        self.home.mkdir()
        self.player_files = {"loot/player.json": b'{"xp":1250,"level":5}',
                             "loot/new_nodes.csv": b"private,data\n",
                             "secrets.conf": b"private-key-for-test-only",
                             "plugins/.wardrive_state.json": b'{"pending":3}',
                             "whitelist.json": b'["keep"]',
                             "maps/cache.json": b'{"tiles":4}'}
        for name, data in self.player_files.items():
            write(self.root, name, data)
        self.calls = []
        self.source_name = updates.REPOSITORY
        self.source_id = updates.REPOSITORY_ID
        self.owner_id = updates.OWNER_ID
        self.owner_login = updates.OWNER
        self.fail_fetch = False
        self.manager = updates.GameUpdates(root=self.root, home=self.home, git=self.transport, metadata=self.source_metadata)

    def source_metadata(self):
        return dict(id=self.source_id, full_name=self.source_name,
                    owner=dict(id=self.owner_id, login=self.owner_login))

    def transport(self, root, *args, timeout=30):
        self.calls.append(tuple(args))
        args = list(args)
        if args[0] == "fetch":
            if self.fail_fetch:
                return subprocess.CompletedProcess(args, 128, "", "PRIVATE_TOKEN_SECRET failed")
            expected = f"https://github.com/{self.source_name}.git"
            self.assertIn(expected, args)
            args[args.index(expected)] = str(self.remote)
        return subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(root), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout)

    def publish(self, **changes):
        feed = dict(self.feed, **changes)
        write(self.remote, updates.FEED_PATH, json.dumps(feed))
        git(self.remote, "add", updates.FEED_PATH)
        git(self.remote, "commit", "-qm", "Adjust advertised release")
        return feed

    def release(self):
        result = self.manager.check()
        self.assertEqual(result.status, "available", result.message)
        return result.release

    def assert_player_kept(self):
        for name, value in self.player_files.items():
            self.assertEqual((self.root / name).read_bytes(), value, name)

    def test_check_uses_fixed_fork_and_manifest_not_legacy_origin_or_head(self):
        release = self.release()
        self.assertEqual(self.manager.installed_commit(), self.base)
        self.assertEqual(git(self.root, "rev-parse", "HEAD").stdout.decode().strip(), self.legacy)
        self.assertEqual(release.version, "0.9.17")
        self.assertEqual(release.notes, self.feed["notes"])
        self.assertIn("Full note 79", release.notes)
        self.assertTrue(self.manager.notice_pending(release))
        fetch = next(call for call in self.calls if call[0] == "fetch")
        self.assertIn(f"https://github.com/{updates.REPOSITORY}.git", fetch)
        self.assertNotIn("origin", fetch)
        self.assertEqual(git(self.root, "remote", "get-url", "origin").stdout.decode().strip(),
                         "https://github.com/LOCOSP/esp32-watch-dogs.git")
        self.assert_player_kept()

    def test_absent_feed_is_unpublished_not_commit_based_update(self):
        git(self.remote, "rm", updates.FEED_PATH)
        git(self.remote, "commit", "-qm", "No release published")
        result = self.manager.check()
        self.assertEqual(result.status, "unpublished")
        self.assertIsNone(result.release)
        self.assertFalse(self.manager.state_path.exists())

    def test_source_display_is_cached_and_rename_fetches_the_same_repository_id(self):
        with patch.object(self.manager, "_metadata", wraps=self.source_metadata) as metadata:
            self.assertFalse(self.manager.source_info()["verified"])
            self.assertEqual(self.manager.source_info()["repository_id"], updates.REPOSITORY_ID)
            metadata.assert_not_called()
            self.publish(schema=2, repository_id=updates.REPOSITORY_ID)
            first = self.release()
            self.manager.acknowledge(first)
            original_key = self.manager.notice_key(first)
            self.source_name = updates.OWNER + "/WatchDogsGo-Renamed"
            self.publish(schema=2, repository_id=updates.REPOSITORY_ID, repository=self.source_name)
            renamed = self.release()
            source = self.manager.source_info()
            self.assertTrue(source["verified"])
            self.assertEqual(source["url"], "https://github.com/" + self.source_name)
            self.assertEqual(source["repository_id"], updates.REPOSITORY_ID)
            self.assertEqual(self.manager.notice_key(renamed), original_key)
            self.assertFalse(self.manager.notice_pending(renamed))
            self.assertIn("https://github.com/" + self.source_name + ".git",
                          [part for call in self.calls for part in call])

    def test_rename_keeps_original_feed_alias_and_old_notification_history(self):
        release = self.release()
        legacy_key = f"{updates.REPOSITORY}:{release.channel}:{release.release_id}"
        write(self.home, self.manager.state_path.name, json.dumps(dict(schema=1, seen=[legacy_key])))
        self.source_name = updates.OWNER + "/WatchDogsGo-Renamed"
        # The baseline feed can retain its label after the repository is renamed.
        self.publish(schema=2, repository_id=updates.REPOSITORY_ID)
        renamed = self.release()
        self.assertEqual(renamed.repository, updates.REPOSITORY)
        self.assertFalse(self.manager.notice_pending(renamed))
        plan = self.manager.prepare(renamed)
        receipt = self.manager.install(plan, quiescent=True)
        manifest = json.loads((self.root / updates.MANIFEST_PATH).read_text())
        self.assertEqual(manifest["source_repository"], self.source_name)
        self.assertEqual(manifest["source_repository_id"], updates.REPOSITORY_ID)
        self.manager.rollback(receipt, quiescent=True)
        self.assert_player_kept()

    def test_foreign_repository_identity_owner_and_unsafe_canonical_names_fail_closed(self):
        for changes in (dict(source_id=updates.REPOSITORY_ID + 1),
                        dict(owner_id=updates.OWNER_ID + 1),
                        dict(owner_login="OtherOwner"),
                        dict(source_name="OtherOwner/WatchDogsGo"),
                        dict(source_name=updates.OWNER + "/../outside"),
                        dict(source_name=updates.OWNER + "/WatchDogsGo.git?token=private")):
            with self.subTest(changes=changes):
                original = {name: getattr(self, name) for name in changes}
                for name, value in changes.items():
                    setattr(self, name, value)
                self.calls.clear()
                result = self.manager.check()
                self.assertEqual(result.status, "error")
                self.assertFalse(self.manager.source_info()["verified"])
                self.assertFalse(any(call[0] == "fetch" for call in self.calls))
                for name, value in original.items():
                    setattr(self, name, value)

    def test_schema_two_feed_requires_pinned_id_and_a_verified_repository_name(self):
        for changes in (dict(schema=2),
                        dict(schema=2, repository_id=updates.REPOSITORY_ID + 1),
                        dict(schema=2, repository_id=str(updates.REPOSITORY_ID)),
                        dict(schema=2, repository_id=updates.REPOSITORY_ID,
                             repository=updates.OWNER + "/DifferentRepository")):
            with self.subTest(changes=changes):
                self.publish(**changes)
                self.assertEqual(self.manager.check().status, "error")

    def test_feed_only_descendant_with_identical_runtime_is_current(self):
        # A fresh clone has the feed commit after the source commit in the feed.
        source = git(self.remote, "rev-parse", "HEAD").stdout.decode().strip()
        self.publish(schema=2, repository_id=updates.REPOSITORY_ID, source_commit=source)
        advertised = git(self.remote, "rev-parse", "HEAD").stdout.decode().strip()
        git(self.root, "fetch", str(self.remote), updates.BRANCH)
        git(self.root, "checkout", "--detach", "--force", advertised)
        (self.root / updates.MANIFEST_PATH).unlink()
        result = self.manager.check()
        self.assertEqual(result.status, "current", result.message)
        self.assertEqual(result.installed_version, "0.9.17")

    def test_same_version_descendant_with_different_runtime_is_not_called_current(self):
        release = self.release()
        write(self.remote, "watchdogs/app.py", "UNREVIEWED = True\n")
        git(self.remote, "add", "watchdogs/app.py")
        git(self.remote, "commit", "-qm", "Unadvertised runtime change")
        git(self.root, "fetch", str(self.remote), updates.BRANCH)
        newer = git(self.remote, "rev-parse", "HEAD").stdout.decode().strip()
        git(self.root, "checkout", "--detach", "--force", newer)
        (self.root / updates.MANIFEST_PATH).unlink()
        result = self.manager.check()
        self.assertEqual(result.status, "error")
        self.assertIn("unapproved game changes", result.message)
        self.assert_player_kept()

    def test_network_failure_does_not_use_stale_ref_or_erase_notification_history(self):
        release = self.release()
        self.manager.acknowledge(release)
        previous = self.manager.state_path.read_bytes()
        self.fail_fetch = True
        result = self.manager.check()
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.release)
        self.assertNotIn("PRIVATE_TOKEN", result.message)
        self.assertEqual(self.manager.state_path.read_bytes(), previous)

    def test_current_identity_comes_from_manifest_even_with_older_git_head(self):
        self.publish(source_commit=self.base, version="0.9.16", release_id="v0.9.16")
        result = self.manager.check()
        self.assertEqual(result.status, "current", result.message)
        self.assertEqual(result.installed_version, "0.9.16")

    def test_malformed_and_foreign_release_metadata_is_rejected(self):
        for changes in ({"repository": "Other/Fork"}, {"channel": "testing"},
                        {"release_id": "../traversal"}, {"version": "KG 1.0"},
                        {"source_commit": "HEAD"}, {"title": "bad\x1btitle"},
                        {"notes": "\x00bad"}, {"command": "run something"}):
            with self.subTest(changes=changes):
                self.publish(**changes)
                self.assertEqual(self.manager.check().status, "error")

    def test_downgrade_or_non_newer_version_is_rejected(self):
        self.publish(source_commit=self.legacy, version="0.9.18")
        self.assertEqual(self.manager.check().status, "error")
        self.publish(version="0.9.16")
        self.assertEqual(self.manager.check().status, "error")

    def test_corrupt_installed_manifest_is_not_replaced_by_legacy_git_fallback(self):
        write(self.root, updates.MANIFEST_PATH, '{"source_commit":"wrong"}')
        self.assertEqual(self.manager.check().status, "error")

    def test_notice_once_per_release_survives_restart_and_notes_edits(self):
        release = self.release()
        self.manager.acknowledge(release)
        self.manager.acknowledge(release)
        restarted = updates.GameUpdates(self.root, self.home, git=self.transport, metadata=self.source_metadata)
        self.assertFalse(restarted.notice_pending(release))
        self.assertFalse(restarted.notice_pending(replace(release, notes="Edited release notes")))
        self.assertTrue(restarted.notice_pending(replace(release, release_id="v0.9.18")))
        self.assertEqual(len(json.loads(self.manager.state_path.read_text())["seen"]), 1)

    def test_notice_is_not_marked_seen_by_checking_or_preparing(self):
        release = self.release()
        plan = self.manager.prepare(release)
        self.assertTrue(self.manager.notice_pending(release))
        self.assertFalse(self.manager.state_path.exists())
        self.manager.discard(plan)

    def test_failed_atomic_notification_write_keeps_prior_state(self):
        release = self.release()
        self.manager.acknowledge(release)
        before = self.manager.state_path.read_bytes()
        original = os.replace
        def fail_state(source, target):
            if Path(target) == self.manager.state_path:
                raise PermissionError("test failure")
            return original(source, target)
        next_release = replace(release, release_id="next")
        with patch.object(updates.os, "replace", side_effect=fail_state):
            with self.assertRaises(updates.UpdateError):
                self.manager.acknowledge(next_release)
        self.assertEqual(self.manager.state_path.read_bytes(), before)
        self.assertTrue(self.manager.notice_pending(next_release))

    def test_corrupt_seen_state_is_preserved_without_a_popup_reset(self):
        write(self.home, self.manager.state_path.name, "invalid")
        with self.assertRaises(updates.UpdateError):
            self.manager.notice_pending(self.release())
        self.assertEqual(self.manager.state_path.read_text(), "invalid")

    def test_prepare_is_read_only_and_discard_never_changes_installed_data(self):
        before = snapshot(self.root)
        plan = self.manager.prepare(self.release())
        self.assertEqual(snapshot(self.root), before)
        self.assertIn("watchdogs/new_feature.py", plan.changed_files)
        self.assertIn("watchdogs/old_module.py", plan.changed_files)
        self.assertNotIn("watchdogs/config.py", plan.changed_files)
        self.manager.discard(plan)
        self.manager.discard(plan)
        self.assertFalse(plan.staging_dir.exists())
        self.assertEqual(snapshot(self.root), before)

    def test_release_revoked_after_notification_cannot_be_prepared(self):
        release = self.release()
        self.publish(release_id="replacement")
        before = snapshot(self.root)
        with self.assertRaises(updates.UpdateError):
            self.manager.prepare(release)
        self.assertEqual(snapshot(self.root), before)

    def test_tracked_local_config_unchanged_by_release_is_preserved(self):
        config = self.root / "watchdogs/config.py"
        config.write_text("USER_DEFAULT = 42\n")
        plan = self.manager.prepare(self.release())
        self.manager.install(plan, quiescent=True)
        self.assertEqual(config.read_text(), "USER_DEFAULT = 42\n")
        self.assert_player_kept()

    def test_installed_release_notes_are_updated_without_other_docs_or_player_data(self):
        notes = "WatchDogsGo 0.9.17\nApproved installed release notes.\n"
        write(self.remote, "docs/UPDATE_PREVIEW.txt", notes)
        write(self.remote, "docs/README.md", "Unrelated upstream documentation.\n")
        git(self.remote, "add", "docs")
        git(self.remote, "commit", "-qm", "Approved offline release notes")
        target = git(self.remote, "rev-parse", "HEAD").stdout.decode().strip()
        self.publish(source_commit=target)
        local_doc = "Locally maintained documentation.\n"
        write(self.root, "docs/README.md", local_doc)
        plan = self.manager.prepare(self.release())
        self.assertIn("docs/UPDATE_PREVIEW.txt", plan.changed_files)
        self.assertNotIn("docs/README.md", plan.changed_files)
        receipt = self.manager.install(plan, quiescent=True)
        self.assertEqual((self.root / "docs/UPDATE_PREVIEW.txt").read_text(), notes)
        self.assertEqual((self.root / "docs/README.md").read_text(), local_doc)
        self.assert_player_kept()
        progress = b'{"xp":1500,"level":6}'
        write(self.root, "loot/player.json", progress)
        self.manager.rollback(receipt.backup_dir, quiescent=True)
        self.assertFalse((self.root / "docs/UPDATE_PREVIEW.txt").exists())
        self.assertEqual((self.root / "docs/README.md").read_text(), local_doc)
        self.assertEqual((self.root / "loot/player.json").read_bytes(), progress)

    def test_local_edits_to_changed_code_block_entire_update(self):
        write(self.root, "watchdogs/app.py", "MY_LOCAL_CHANGE = True\n")
        before = snapshot(self.root)
        with self.assertRaisesRegex(updates.UpdateError, "Local source changes"):
            self.manager.prepare(self.release())
        self.assertEqual(snapshot(self.root), before)

    def test_untracked_new_code_collision_is_not_overwritten(self):
        write(self.root, "watchdogs/new_feature.py", "MY_PLUGIN = True\n")
        before = snapshot(self.root)
        with self.assertRaisesRegex(updates.UpdateError, "conflicts with a local file"):
            self.manager.prepare(self.release())
        self.assertEqual(snapshot(self.root), before)

    def test_requirements_change_blocks_update_without_running_setup(self):
        write(self.remote, "requirements.txt", "different-package>=1.0\n")
        git(self.remote, "add", "requirements.txt")
        git(self.remote, "commit", "-qm", "Different dependencies")
        target = git(self.remote, "rev-parse", "HEAD").stdout.decode().strip()
        self.publish(source_commit=target)
        with self.assertRaisesRegex(updates.UpdateError, "Dependencies changed"):
            self.manager.prepare(self.release())
        self.assert_player_kept()

    def test_malicious_path_forms_are_rejected(self):
        for name in ("/etc/passwd", "../loot/player.json", "watchdogs/../config.py",
                     "watchdogs\\app.py", "watchdogs//app.py", "watchdogs/.git/config",
                     "C:/outside.py", "watchdogs/bad\nname.py"):
            with self.subTest(name=name), self.assertRaises(updates.UpdateError):
                updates._runtime(name)
        self.assertFalse(updates._runtime(".gitignore"))
        self.assertFalse(updates._runtime("docs/media/demo.mp4"))
        self.assertFalse(updates._runtime("docs/UPDATE_PREVIEW.md"))
        self.assertFalse(updates._runtime("plugins/.wardrive_state.json"))
        self.assertFalse(updates._runtime("loot/player.py"))

    def test_staging_tamper_and_changed_source_after_prepare_are_refused(self):
        plan = self.manager.prepare(self.release())
        before = snapshot(self.root)
        write(plan.staging_dir / "files", "watchdogs/app.py", "TAMPERED = True\n")
        with self.assertRaises(updates.UpdateError):
            self.manager.install(plan, quiescent=True)
        self.assertEqual(snapshot(self.root), before)
        self.manager.discard(plan)
        plan = self.manager.prepare(self.release())
        write(self.root, "watchdogs/app.py", "CHANGED_DURING_PREPARATION = True\n")
        before = snapshot(self.root)
        with self.assertRaises(updates.UpdateError):
            self.manager.install(plan, quiescent=True)
        self.assertEqual(snapshot(self.root), before)

    def test_install_requires_quiescence_and_preserves_player_files_and_modes(self):
        app = self.root / "watchdogs/app.py"
        os.chmod(app, 0o640)
        prior_mode = app.stat().st_mode & 0o777
        before = snapshot(self.root)
        plan = self.manager.prepare(self.release())
        with self.assertRaises(updates.UpdateError):
            self.manager.install(plan)
        self.assertEqual(snapshot(self.root), before)
        receipt = self.manager.install(plan, quiescent=True)
        self.assertEqual(receipt.source_commit, self.target)
        self.assertEqual(receipt.version, "0.9.17")
        self.assertIn("new release", app.read_text())
        self.assertEqual(app.stat().st_mode & 0o777, prior_mode)
        self.assertFalse((self.root / "watchdogs/old_module.py").exists())
        self.assertTrue((self.root / "watchdogs/new_feature.py").exists())
        self.assertEqual(self.manager.installed_commit(), self.target)
        self.assert_player_kept()
        self.assertFalse(any("loot" in Path(name).parts or "secrets.conf" in name
                             for name in snapshot(receipt.backup_dir)))

    def test_new_nested_code_directories_match_checkout_ownership(self):
        write(self.remote, "watchdogs/new_package/__init__.py", "PACKAGE = True\n")
        git(self.remote, "add", ".")
        git(self.remote, "commit", "-qm", "Nested runtime package")
        target = git(self.remote, "rev-parse", "HEAD").stdout.decode().strip()
        self.publish(source_commit=target)
        plan = self.manager.prepare(self.release())
        receipt = self.manager.install(plan, quiescent=True)
        created = self.root / "watchdogs/new_package"
        self.assertTrue(created.is_dir())
        self.assertEqual(created.stat().st_uid, self.root.stat().st_uid)
        self.assertEqual(created.stat().st_gid, self.root.stat().st_gid)
        self.manager.rollback(receipt, quiescent=True)
        self.assertFalse(created.exists())

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0
                         and shutil.which("sudo"), "Root-only archive ownership integration")
    def test_root_staging_is_readable_by_owner_git_without_widening_private_permissions(self):
        import pwd
        try:
            owner = pwd.getpwnam(os.environ.get("SUDO_USER", "nobody"))
            if owner.pw_uid == 0:
                owner = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("No non-root test account is available")
        os.chmod(self.directory_path, 0o711)
        os.chown(self.home, owner.pw_uid, owner.pw_gid)
        original = self.manager._git
        def owner_archive(root, *args, timeout=30):
            if args[0] != "archive":
                return original(root, *args, timeout=timeout)
            return subprocess.run(["sudo", "-n", "-H", "-u", owner.pw_name,
                                   "git", "-C", str(root), "-c", f"safe.directory={root}", *args],
                                  capture_output=True, text=True, timeout=timeout)
        self.manager._git = owner_archive
        plan = self.manager.prepare(self.release())
        self.assertEqual(plan.staging_dir.stat().st_uid, owner.pw_uid)
        self.assertEqual(plan.staging_dir.stat().st_mode & 0o777, 0o700)
        for path in plan.staging_dir.rglob("*"):
            self.assertEqual(path.stat().st_uid, owner.pw_uid)
        self.manager.discard(plan)

    def test_recovery_function_definition_without_call_blocks_packaging(self):
        write(self.remote, "watchdogs/__main__.py", "def _recover_game_update():\n    pass\n")
        git(self.remote, "add", ".")
        git(self.remote, "commit", "-qm", "Missing recovery invocation")
        target = git(self.remote, "rev-parse", "HEAD").stdout.decode().strip()
        self.publish(source_commit=target)
        with self.assertRaisesRegex(updates.UpdateError, "startup update recovery"):
            self.manager.prepare(self.release())

    def test_code_rollback_preserves_newer_player_progress_and_metadata(self):
        before = snapshot(self.root)
        mode = (self.root / "watchdogs/app.py").stat().st_mode & 0o777
        plan = self.manager.prepare(self.release())
        receipt = self.manager.install(plan, quiescent=True)
        write(self.root, "loot/player.json", '{"xp":9000,"level":10}')
        self.manager.rollback(receipt, quiescent=True)
        self.assertEqual((self.root / "watchdogs/app.py").read_bytes(), before["watchdogs/app.py"])
        self.assertEqual((self.root / "watchdogs/app.py").stat().st_mode & 0o777, mode)
        self.assertEqual((self.root / updates.MANIFEST_PATH).read_bytes(), before[updates.MANIFEST_PATH])
        self.assertEqual((self.root / "loot/player.json").read_text(), '{"xp":9000,"level":10}')
        self.assertFalse((self.root / "watchdogs/new_feature.py").exists())
        self.assertEqual((self.root / "watchdogs/old_module.py").read_bytes(), before["watchdogs/old_module.py"])
        self.manager.rollback(receipt, quiescent=True)

    def test_rollback_refuses_later_source_edit_and_tampered_backup(self):
        plan = self.manager.prepare(self.release())
        receipt = self.manager.install(plan, quiescent=True)
        write(self.root, "watchdogs/app.py", "LATER_CODE = True\n")
        before = snapshot(self.root)
        with self.assertRaisesRegex(updates.UpdateError, "Code changed after"):
            self.manager.rollback(receipt, quiescent=True)
        self.assertEqual(snapshot(self.root), before)

    def test_mid_install_failure_restores_all_previous_code_and_manifest(self):
        before = snapshot(self.root)
        plan = self.manager.prepare(self.release())
        original = updates._atomic
        failed = []
        def fail_once(path, *args, **kwargs):
            if Path(path) == self.root / "watchdogs/app.py" and not failed:
                failed.append(True)
                raise OSError("simulated write failure")
            return original(path, *args, **kwargs)
        with patch.object(updates, "_atomic", side_effect=fail_once):
            with self.assertRaisesRegex(updates.UpdateError, "previous code was restored"):
                self.manager.install(plan, quiescent=True)
        self.assertEqual(snapshot(self.root), before)
        self.assert_player_kept()

    def _power_loss(self):
        plan = self.manager.prepare(self.release())
        original = updates._atomic
        def fail_after_write(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if Path(path) == self.root / "watchdogs/app.py":
                raise PowerLoss()
            return result
        with patch.object(updates, "_atomic", side_effect=fail_after_write):
            with self.assertRaises(PowerLoss):
                self.manager.install(plan, quiescent=True)
        receipts = list(self.manager.backup_root.glob("update-*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["status"], "installing")
        return receipts[0]

    def test_power_loss_recovered_before_app_import_using_frozen_helper(self):
        before = snapshot(self.root)
        receipt = self._power_loss()
        self.assertNotEqual((self.root / "watchdogs/app.py").read_bytes(), before["watchdogs/app.py"])
        entry = importlib.import_module("watchdogs.__main__")
        with patch.object(entry, "__file__", str(self.root / "watchdogs/__main__.py")), \
                patch.object(entry, "_resolve_user_home", return_value=(str(self.home), None)), \
                patch.object(entry.os, "execv") as restart:
            entry._recover_game_update()
        restart.assert_called_once()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(json.loads(receipt.read_text())["status"], "rolled_back")
        self.assert_player_kept()

    def test_corrupt_recovery_helper_blocks_startup_instead_of_importing_mixed_code(self):
        receipt = self._power_loss()
        (receipt.parent / "recovery.py").write_text("raise RuntimeError('tampered')\n")
        entry = importlib.import_module("watchdogs.__main__")
        with patch.object(entry, "__file__", str(self.root / "watchdogs/__main__.py")), \
                patch.object(entry, "_resolve_user_home", return_value=(str(self.home), None)), \
                self.assertRaises(SystemExit):
            entry._recover_game_update()
        self.assert_player_kept()

    def test_other_installation_journal_is_ignored_without_executing_its_helper(self):
        receipt = self._power_loss()
        other = self.directory_path / "another-checkout"
        other.mkdir()
        manager = updates.GameUpdates(root=other, home=self.home, git=self.transport, metadata=self.source_metadata)
        entry = importlib.import_module("watchdogs.__main__")
        before = snapshot(self.root)
        # Even a broken helper in the other checkout is never loaded here.
        (receipt.parent / "recovery.py").write_text("raise AssertionError('must not run')\n")
        with patch.object(entry, "__file__", str(other / "watchdogs/__main__.py")), \
                patch.object(entry, "_resolve_user_home", return_value=(str(self.home), None)), \
                patch.object(entry.os, "execv") as restart:
            entry._recover_game_update()
        restart.assert_not_called()
        manager._incomplete()
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(json.loads(receipt.read_text())["status"], "installing")
        # Missing/relative roots remain fail-closed, not silently ignored.
        journal = json.loads(receipt.read_text())
        journal["root"] = "relative-checkout"
        receipt.write_text(json.dumps(journal))
        with self.assertRaises(updates.UpdateError):
            manager._incomplete()
        with patch.object(entry, "__file__", str(other / "watchdogs/__main__.py")), \
                patch.object(entry, "_resolve_user_home", return_value=(str(self.home), None)), \
                self.assertRaises(SystemExit):
            entry._recover_game_update()

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic links and xattrs")
    def test_source_symlink_is_rejected_and_code_xattrs_survive_rollback(self):
        app = self.root / "watchdogs/app.py"
        value = app.read_bytes()
        app.unlink()
        outside = self.home / "outside.py"
        outside.write_bytes(value)
        app.symlink_to(outside)
        with self.assertRaises(updates.UpdateError):
            self.manager.prepare(self.release())
        self.assertEqual(outside.read_bytes(), value)
        app.unlink()
        app.write_bytes(value)
        if hasattr(os, "setxattr"):
            try:
                os.setxattr(app, "user.wdg-update-test", b"keep")
            except OSError:
                return
            plan = self.manager.prepare(self.release())
            receipt = self.manager.install(plan, quiescent=True)
            self.assertEqual(os.getxattr(app, "user.wdg-update-test"), b"keep")
            self.manager.rollback(receipt, quiescent=True)
            self.assertEqual(os.getxattr(app, "user.wdg-update-test"), b"keep")


class GitHubSourceMetadataTests(unittest.TestCase):
    """Public and authenticated-private source lookup, with no HTTP or gh I/O."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.manager = updates.GameUpdates(root=self.folder, home=self.folder)
        self.value = dict(id=updates.REPOSITORY_ID, full_name=updates.REPOSITORY,
                          owner=dict(id=updates.OWNER_ID, login=updates.OWNER))

    def test_public_lookup_needs_no_github_cli_or_authentication(self):
        with patch.object(updates.urllib.request, "urlopen",
                          return_value=io.BytesIO(json.dumps(self.value).encode())) as request, \
                patch.object(updates.shutil, "which", return_value=None), \
                patch.object(updates.subprocess, "run") as command:
            self.assertEqual(self.manager._resolve_source(), self.value["full_name"])
        outgoing = request.call_args.args[0]
        self.assertEqual(outgoing.full_url,
                         f"https://api.github.com/repositories/{updates.REPOSITORY_ID}")
        self.assertIsNone(outgoing.get_header("Authorization"))
        command.assert_not_called()
        self.assertTrue(self.manager.source_info()["verified"])

    def test_private_lookup_uses_existing_cli_login_without_exposing_credentials(self):
        denied = urllib.error.HTTPError("https://api.github.com", 404, "private", None, None)
        result = subprocess.CompletedProcess([], 0, json.dumps(self.value), "")
        with patch.object(updates.urllib.request, "urlopen", side_effect=denied), \
                patch.object(updates.shutil, "which", return_value="/usr/bin/gh"), \
                patch.object(updates.subprocess, "run", return_value=result) as command:
            self.assertEqual(self.manager._resolve_source(), self.value["full_name"])
        args = command.call_args.args[0]
        self.assertEqual(args[-3:], ["gh", "api", f"repositories/{updates.REPOSITORY_ID}"])
        self.assertNotIn("--hostname", args)
        self.assertTrue(self.manager.source_info()["verified"])

    def test_failed_private_lookup_has_only_a_safe_error_and_no_verified_url(self):
        result = subprocess.CompletedProcess([], 1, "", "PRIVATE_TOKEN_SECRET authentication failure")
        with patch.object(updates.urllib.request, "urlopen", side_effect=OSError("offline")), \
                patch.object(updates.shutil, "which", return_value="/usr/bin/gh"), \
                patch.object(updates.subprocess, "run", return_value=result), \
                self.assertRaises(updates.UpdateError) as failure:
            self.manager._resolve_source()
        self.assertNotIn("PRIVATE_TOKEN", str(failure.exception))
        self.assertFalse(self.manager.source_info()["verified"])

    def test_oversized_metadata_and_foreign_repo_id_are_rejected(self):
        with patch.object(updates.urllib.request, "urlopen",
                          return_value=io.BytesIO(b" " * (updates.MAX_FEED_BYTES + 1))), \
                self.assertRaises(updates.UpdateError):
            self.manager._resolve_source()
        self.value["id"] += 1
        self.manager._metadata = lambda: self.value
        with self.assertRaises(updates.UpdateError):
            self.manager._resolve_source()
        self.assertFalse(self.manager.source_info()["verified"])


if __name__ == "__main__":
    unittest.main()
