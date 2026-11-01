"""Snapshot/export checks with synthetic files and a temporary Git repository.

No account, network, radio or real player data is used. The ZIP is verified
against its committed blobs and never includes repository history.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile

TOOL = Path(__file__).resolve().parents[1] / "tools/release_package.py"
spec = importlib.util.spec_from_file_location("release_package_test_module", TOOL)
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def minimal():
    return {name: b"reviewed source\n" for name in package.REQUIRED_FILES}


def png(width=32, height=24):
    # Header fixture for dimensions only; no image pixels or private data.
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


def add_media(files):
    files["docs/media/example.png"] = png()
    data = files["docs/media/example.png"]
    files["docs/media/media-manifest.json"] = json.dumps({"files": [dict(
        path="example.png", bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        dimensions=[32, 24])]}).encode()


class PackageValidationTests(unittest.TestCase):
    def test_local_links_resolve_from_the_document_and_fenced_examples_are_ignored(self):
        files = minimal()
        files["docs/nested/guide.md"] = b"[Home](../../README.md#overview)\n![Image](<../media/example.png>)\n```md\n[Example](not-a-real-file.md)\n```\n"
        add_media(files)
        result = package.validate(files)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked_local_links"], 2)

    def test_missing_local_link_and_escape_are_reported(self):
        files = minimal()
        files["README.md"] = b"[Missing](docs/absent.md) [Escape](../outside.md)"
        result = package.validate(files)
        self.assertFalse(result["ok"])
        self.assertTrue(any("Broken local link" in row for row in result["errors"]))
        self.assertTrue(any("Unsafe document link" in row for row in result["errors"]))

    def test_media_hash_size_dimensions_and_complete_output_coverage_are_required(self):
        files = minimal()
        add_media(files)
        self.assertTrue(package.validate(files)["ok"])
        files["docs/media/example.png"] = png(40, 30) + b"changed"
        files["docs/media/unlisted.png"] = png()
        errors = package.validate(files)["errors"]
        self.assertTrue(any("Stale media hash/size" in row for row in errors))
        self.assertTrue(any("Stale media dimensions" in row for row in errors))
        self.assertTrue(any("lacks a verified manifest" in row for row in errors))

    def test_private_runtime_paths_are_rejected_but_public_templates_are_kept(self):
        for name in ("secrets.conf", "loot/session/file.txt", "a/.git/config", "capture.jsonl",
                     ".meshtastic-deployment.json", "plugins/.wardrive_state.json",
                     ".scratch/result.txt", "home/.watchdogs_keys.json", "old-review.zip"):
            with self.subTest(name=name):
                files = minimal()
                files[name] = b"not exported"
                self.assertFalse(package.validate(files)["ok"])
        files = minimal()
        files["secrets.conf.example"] = b"# API_KEY=your-key-here\n"
        self.assertTrue(package.validate(files)["ok"])

    def test_high_confidence_token_is_flagged_without_printing_its_value(self):
        files = minimal()
        token = "ghp_" + "A" * 40
        files["watchdogs/example.py"] = token.encode()
        result = package.validate(files)
        self.assertFalse(result["ok"])
        self.assertNotIn(token, json.dumps(result))

    def test_private_attachment_warning_is_not_claimed_to_be_a_public_link_test(self):
        files = minimal()
        files["README.md"] = b"[Film](https://github.com/user-attachments/assets/example)"
        result = package.validate(files)
        self.assertTrue(result["ok"])
        self.assertTrue(any("attachment access" in row for row in result["warnings"]))

    def test_unsafe_archive_paths_are_rejected(self):
        for name in ("../outside", "/absolute", "C:/drive", "a\\b", "a/../b", "a//b", "a\nb", None):
            with self.subTest(name=name), self.assertRaises(package.PackageError):
                package.safe_name(name)


@unittest.skipUnless(shutil.which("git"), "Git is required for exact snapshot exports")
class PackageExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "review")
        self.git("config", "user.name", "Package Test")
        self.git("config", "user.email", "package@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        for name, data in minimal().items():
            self.write(name, data)
        self.write(".gitignore", b"secrets.conf\nloot/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic reviewed source")
        self.commit = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
            stderr=subprocess.PIPE, text=True)

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_export_contains_only_exact_committed_files_and_no_history_or_local_player_data(self):
        original = (self.repo / "README.md").read_bytes()
        self.write("README.md", b"uncommitted draft")
        self.write("secrets.conf", b"private local setting")
        self.write("loot/player.json", b'{"xp":100}')
        self.write("untracked-review.txt", b"do not include")
        output = self.base / "snapshot.zip"
        result = package.export(self.repo, self.commit, output)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["source_history_included"])
        self.assertTrue(result["private_review_artifact"])
        prefix = "WatchDogsGo-" + self.commit[:12] + "/"
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertEqual(archive.read(prefix + "README.md"), original)
            self.assertNotIn(prefix + "secrets.conf", names)
            self.assertNotIn(prefix + "loot/player.json", names)
            self.assertNotIn(prefix + "untracked-review.txt", names)
            self.assertFalse(any("/.git/" in name for name in names))
            source = json.loads(archive.read(prefix + "RELEASE_SOURCE.json"))
            self.assertEqual(source["source_commit"], self.commit)
            for entry in source["files"]:
                self.assertEqual(hashlib.sha256(archive.read(prefix + entry["path"])).hexdigest(), entry["sha256"])
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), result["archive_sha256"])
        self.assertIn(result["archive_sha256"], output.with_suffix(".zip.sha256").read_text())

    def test_export_is_deterministic_and_refuses_to_overwrite_or_write_inside_checkout(self):
        first, second = self.base / "first.zip", self.base / "second.zip"
        a = package.export(self.repo, self.commit, first)
        b = package.export(self.repo, self.commit, second)
        self.assertEqual(a["archive_sha256"], b["archive_sha256"])
        with self.assertRaises(package.PackageError):
            package.export(self.repo, self.commit, first)
        with self.assertRaises(package.PackageError):
            package.export(self.repo, self.commit, self.repo / "unsafe.zip")

    def test_committed_private_artifact_blocks_export_without_creating_zip(self):
        self.write("capture.jsonl", b"private capture")
        self.git("add", "capture.jsonl")
        self.git("commit", "-qm", "Deliberately unsafe fixture")
        output = self.base / "blocked.zip"
        result = package.export(self.repo, "HEAD", output)
        self.assertFalse(result["ok"])
        self.assertFalse(output.exists())

    def test_archive_attributes_cannot_omit_or_expand_exact_committed_files(self):
        self.write(".gitattributes", b"README.md export-ignore\nnotes.txt export-subst\n")
        self.write("notes.txt", b"$Format:%H$\n")
        original = (self.repo / "README.md").read_bytes()
        self.git("add", ".")
        self.git("commit", "-qm", "Export attribute fixture")
        files, _, _, _ = package.snapshot(self.repo, "HEAD")
        self.assertEqual(files["README.md"], original)
        self.assertEqual(files["notes.txt"], b"$Format:%H$\n")

    def test_preexisting_generated_manifest_cannot_be_overwritten_in_an_export(self):
        self.write("RELEASE_SOURCE.json", b'{"old":"provenance"}')
        self.git("add", "RELEASE_SOURCE.json")
        self.git("commit", "-qm", "Reserved export manifest fixture")
        with self.assertRaisesRegex(package.PackageError, "outside Git"):
            package.export(self.repo, "HEAD", self.base / "blocked.zip")
        self.assertFalse((self.base / "blocked.zip").exists())

    def test_platform_line_ending_settings_cannot_change_committed_blob_bytes(self):
        self.write("line-endings.txt", b"first\r\nsecond\r\n")
        self.git("-c", "core.autocrlf=false", "add", "line-endings.txt")
        self.git("commit", "-qm", "Exact CRLF fixture")
        self.git("config", "core.autocrlf", "true")
        files, _, _, _ = package.snapshot(self.repo, "HEAD")
        self.assertEqual(files["line-endings.txt"], b"first\r\nsecond\r\n")

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic link fixture")
    def test_committed_symbolic_link_is_not_exported_as_a_source_file(self):
        link = self.repo / "outside-link"
        link.symlink_to(self.base / "outside")
        self.git("add", "outside-link")
        self.git("commit", "-qm", "Unsafe symlink fixture")
        with self.assertRaisesRegex(package.PackageError, "symlink"):
            package.snapshot(self.repo, "HEAD")


if __name__ == "__main__":
    unittest.main()
