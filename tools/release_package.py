#!/usr/bin/env python3
"""Validate and export one reviewed source snapshot, never its Git history.

Examples (write the ZIP outside the checkout):
  python tools/release_package.py validate --worktree
  python tools/release_package.py validate --ref HEAD
  python tools/release_package.py export --ref HEAD --output ../wdg-review.zip

This is a source/media consistency check, not an OCR review, a history audit,
an RF qualification or permission to publish the private repository.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import zipfile
from urllib.parse import unquote, urlsplit


class PackageError(RuntimeError):
    pass


FORBIDDEN_PARTS = frozenset((".git", ".scratch", ".venv", "__pycache__", "loot",
    "maps", "firmware_cache", "operational-docs-private", "frames-source", "media-qa"))
FORBIDDEN_NAMES = frozenset(("secrets.conf", "whitelist.json", ".meshtastic-deployment.json",
    "code-rollback.json", ".wardrive_state.json", ".wardrive_uploaded.json"))
FORBIDDEN_SUFFIXES = (".log", ".csv", ".jsonl", ".pcap", ".pcapng", ".hccapx", ".22000",
    ".cap", ".bundle", ".tar.gz", ".zip", ".tar", ".tgz", ".7z", ".pyc", ".pyo")
MEDIA_EXTENSIONS = frozenset((".png", ".gif", ".mp4"))
REQUIRED_FILES = frozenset(("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
    "watchdogs/__init__.py", "watchdogs/app.py", "requirements.txt", "setup.sh", "run.sh"))
TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{45,}|AKIA[A-Z0-9]{16})\b")
KEY_PATTERN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{80,}-----END ")
PRIVATE_PATH_PATTERN = re.compile(r"192\.168\.1\.133|/home/x/(?:auto-gps-|meshtastic-rx-|update-preview-)|C:[/\\]uConsole[/\\]2026[-/]")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    if not isinstance(name, str):
        raise PackageError("Unsafe source path")
    path = PurePosixPath(name)
    if (not name or "\\" in name or any(ord(char) < 32 for char in name)
            or path.is_absolute() or any(p in ("", ".", "..") for p in name.split("/"))
            or ":" in path.parts[0]):
        raise PackageError("Unsafe source path")
    return name


def forbidden(name):
    path = PurePosixPath(name)
    return (bool(set(path.parts) & FORBIDDEN_PARTS) or path.name in FORBIDDEN_NAMES
            or path.name.startswith((".watchdogs", ".janos"))
            or name.endswith(FORBIDDEN_SUFFIXES)
            or (path.suffix.lower() in (".mp4", ".mov") and name != "docs/media/demo.mp4"))


def git(repo, *args, input_data=None):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, input=input_data)
    if result.returncode:
        # Avoid echoing command lines, credentials or private paths from stderr.
        raise PackageError("Git could not read the requested source snapshot")
    return result.stdout


def snapshot(repo, ref):
    """Read only exact regular Git blobs; export-ignore/subst cannot hide edits."""
    commit = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    tree = git(repo, "rev-parse", commit + "^{tree}").decode().strip()
    entries = {}
    for row in git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not row:
            continue
        header, raw_name = row.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        name = safe_name(raw_name.decode("utf-8"))
        if kind != "blob" or mode not in ("100644", "100755"):
            raise PackageError("Source contains a symlink, submodule or unsupported entry: " + name)
        entries[name] = (mode, oid)
    # cat-file returns exact object bytes. Unlike git archive, it cannot apply
    # export-ignore, export-subst or platform-dependent EOL conversion.
    oids = list(dict.fromkeys(oid for _, oid in entries.values()))
    batch = git(repo, "cat-file", "--batch", input_data=b"".join(oid.encode() + b"\n" for oid in oids))
    blobs, offset = {}, 0
    for oid in oids:
        line_end = batch.find(b"\n", offset)
        try:
            reported, kind, raw_size = batch[offset:line_end].decode("ascii").split()
            size = int(raw_size)
        except (UnicodeError, ValueError):
            raise PackageError("Git returned an invalid blob header") from None
        start, end = line_end + 1, line_end + 1 + size
        if (reported != oid or kind != "blob" or size < 0 or end >= len(batch)
                or batch[end:end + 1] != b"\n"):
            raise PackageError("Git returned an invalid blob payload")
        data = batch[start:end]
        algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        if algorithm(b"blob " + str(size).encode() + b"\0" + data).hexdigest() != oid:
            raise PackageError("Git blob verification failed")
        blobs[oid], offset = data, end + 1
    if offset != len(batch):
        raise PackageError("Git returned unexpected trailing blob data")
    files = {name: blobs[oid] for name, (_, oid) in entries.items()}
    return files, {name: mode for name, (mode, _) in entries.items()}, commit, tree


def worktree(repo):
    names = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
    files, modes = {}, {}
    for raw in names:
        if not raw:
            continue
        name = safe_name(raw.decode("utf-8"))
        path = repo / name
        if (any((repo.joinpath(*PurePosixPath(name).parts[:i])).is_symlink()
                for i in range(1, len(PurePosixPath(name).parts) + 1))
                or not path.is_file()):
            raise PackageError("Worktree entry is missing or is not a regular file: " + name)
        files[name] = path.read_bytes()
        modes[name] = "100755" if path.stat().st_mode & 0o111 else "100644"
    return files, modes


def image_dimensions(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return list(struct.unpack(">II", data[16:24]))
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return list(struct.unpack("<HH", data[6:10]))
    return None


def prose(text):
    """Exclude fenced examples when checking actual document links."""
    output, fence = [], None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)[0]
            fence = None if fence == marker else (marker if fence is None else fence)
            output.append("")
        else:
            output.append(line if fence is None else "")
    return "\n".join(output)


def links(text):
    text = prose(text)
    pattern = r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+['\"][^\n]*?['\"])?\s*\)"
    for match in re.finditer(pattern, text):
        yield match.group(1).strip("<>")
    for match in re.finditer(r"\b(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", text, re.I):
        yield match.group(1)
    for match in re.finditer(r"^\s*\[[^\]]+\]:\s*(\S+)", text, re.M):
        yield match.group(1).strip("<>")


def relative_target(document, target):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    name = unquote(parsed.path)
    parts = [] if name.startswith("/") else list(PurePosixPath(document).parent.parts)
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PackageError("Document link escapes the source snapshot")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def validate(files):
    errors, warnings = [], []
    for name in sorted(REQUIRED_FILES - files.keys()):
        errors.append("Required source file is missing: " + name)
    external_links, checked_links = set(), 0
    for name, data in files.items():
        safe_name(name)
        if forbidden(name):
            errors.append("Runtime/private artifact is tracked: " + name)
        if len(data) > 100 * 1024 * 1024:
            errors.append("Source file exceeds GitHub's regular-file limit: " + name)
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            continue
        if TOKEN_PATTERN.search(text) or KEY_PATTERN.search(text):
            errors.append("Potential credential requires private inspection: " + name)
        # Test fixtures intentionally contain private-looking sample addresses.
        if not name.startswith(("tests/", "tools/", "test_")) and PRIVATE_PATH_PATTERN.search(text):
            errors.append("Device-specific operational path requires review: " + name)
        if not name.endswith(".md"):
            continue
        for target in links(text):
            try:
                resolved = relative_target(name, target)
            except (ValueError, PackageError):
                errors.append("Unsafe document link in " + name)
                continue
            if resolved is None:
                if urlsplit(target).scheme in ("http", "https"):
                    external_links.add(target)
                continue
            checked_links += 1
            if resolved not in files:
                errors.append("Broken local link: " + name + " -> " + target)
    main = "docs/media/media-manifest.json"
    coverage = set()
    if main in files:
        try:
            manifest = json.loads(files[main])
            entries = manifest["files"]
            if not isinstance(entries, list):
                raise ValueError
            for entry in entries:
                short = safe_name(entry["path"])
                name = "docs/media/" + short
                if name in coverage:
                    errors.append("Duplicate media manifest entry: " + name)
                coverage.add(name)
                if name not in files:
                    errors.append("Media manifest file is missing: " + name)
                    continue
                data = files[name]
                if entry.get("bytes") != len(data) or entry.get("sha256") != digest(data):
                    errors.append("Stale media hash/size: " + name)
                if "dimensions" in entry and image_dimensions(data) is not None:
                    if entry["dimensions"] != image_dimensions(data):
                        errors.append("Stale media dimensions: " + name)
        except (ValueError, TypeError, KeyError, PackageError):
            errors.append("Media manifest is malformed")
    for name in files:
        if name.startswith("docs/media/") and PurePosixPath(name).suffix.lower() in MEDIA_EXTENSIONS and name not in coverage:
            errors.append("Media output lacks a verified manifest entry: " + name)
    if any("github.com/user-attachments/assets/" in link for link in external_links):
        warnings.append("GitHub attachment access must be checked separately in the rendered public README; snapshot validation does not test remote video access.")
    warnings.append("Snapshot checks do not audit older Git history or certify image pixels, RF behavior or anonymous external links.")
    return dict(ok=not errors, source_files=len(files), source_bytes=sum(map(len, files.values())),
        checked_local_links=checked_links, media_outputs_verified=len([n for n in coverage if n in files]),
        external_link_count=len(external_links), errors=sorted(set(errors)), warnings=warnings)


def export(repo, ref, output):
    repo, output = repo.resolve(), output.resolve()
    if output.is_relative_to(repo):
        raise PackageError("Write the review archive outside the source checkout")
    if output.exists() or output.with_suffix(output.suffix + ".sha256").exists():
        raise PackageError("Refusing to overwrite an existing review archive or checksum")
    files, modes, commit, tree = snapshot(repo, ref)
    if "RELEASE_SOURCE.json" in files:
        raise PackageError("Keep the generated RELEASE_SOURCE.json outside Git before exporting another snapshot")
    result = validate(files)
    if not result["ok"]:
        return result
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = "WatchDogsGo-" + commit[:12] + "/"
    provenance = dict(schema=1, source_commit=commit, source_tree=tree,
        source_history_included=False, private_review_artifact=True,
        files=[dict(path=name, bytes=len(data), sha256=digest(data), git_mode=modes[name])
               for name, data in sorted(files.items())])
    entries = dict(files)
    entries["RELEASE_SOURCE.json"] = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(prefix + name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = ((0o100755 if modes.get(name) == "100755" else 0o100644) << 16)
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != {prefix + n for n in entries}:
                raise PackageError("Exported ZIP failed verification")
            for name, data in entries.items():
                if archive.read(prefix + name) != data:
                    raise PackageError("Exported source differs from its Git blob")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    sha256 = digest(output.read_bytes())
    output.with_suffix(output.suffix + ".sha256").write_text(sha256 + "  " + output.name + "\n", encoding="utf-8")
    result.update(source_commit=commit, source_tree=tree, archive=str(output),
                  archive_bytes=output.stat().st_size, archive_sha256=sha256,
                  source_history_included=False, private_review_artifact=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    actions = parser.add_subparsers(dest="action", required=True)
    check = actions.add_parser("validate")
    check.add_argument("--ref", default="HEAD")
    check.add_argument("--worktree", action="store_true")
    build = actions.add_parser("export")
    build.add_argument("--ref", required=True)
    build.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "export":
            result = export(args.repo, args.ref, args.output)
        elif args.worktree:
            files, _ = worktree(args.repo)
            result = validate(files)
            result["source"] = "worktree, including uncommitted reviewed changes"
        else:
            files, _, commit, tree = snapshot(args.repo, args.ref)
            result = validate(files)
            result.update(source_commit=commit, source_tree=tree)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    except (PackageError, OSError, UnicodeError, zipfile.BadZipFile) as error:
        print(json.dumps(dict(ok=False, errors=[str(error)])))
        return 1


if __name__ == "__main__":
    sys.exit(main())
