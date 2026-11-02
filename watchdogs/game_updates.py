"""Release checks and code-only updates for this fork.

A commit alone is not an advertised release. The owner identifies approved
code in ``updates/stable.json``. GitHub's immutable repository ID keeps the
same source after a rename or visibility change. Git supplies transport;
the installed checkout's legacy origin/HEAD are never an update destination.

Preparing an update only writes a private staging directory. Installing needs
an explicit quiescent caller, rejects local edits and dependency changes, and
keeps a verified code-only backup. It never runs setup, pip, hooks or commands
from a release manifest, and never restores player data during rollback.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import ast
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
from typing import Optional
import urllib.error
import urllib.request


# The immutable GitHub identity stays valid when the review suffix is removed.
# The name is a bootstrap label, not authority to fetch another repository.
REPOSITORY = "KiloGramowy/WatchDogsGo-KG-Edition"
REPOSITORY_ID = 1442123109
OWNER = "KiloGramowy"
OWNER_ID = 315514197
BRANCH = "main"
CHANNEL = "stable"
FEED_PATH = "updates/stable.json"
FETCH_REF = "refs/remotes/kg-updates/stable"
MANIFEST_PATH = ".meshtastic-deployment.json"
MAX_FEED_BYTES = 128 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 2048
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_ROOT_FILES = frozenset(("meshtastic_rx_probe.py", "requirements.txt", "run.sh",
                         "setup.sh", "watchdogs-launcher", "watchdogs.desktop",
                         "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md",
                         "docs/UPDATE_PREVIEW.txt"))
_ASSET_EXTENSIONS = frozenset((".png", ".wav", ".mp3", ".bdf", ".svg"))


class UpdateError(RuntimeError):
    """A safe, player-facing message; never contains Git's raw stderr or URLs."""


@dataclass(frozen=True)
class Release:
    repository: str
    channel: str
    release_id: str
    version: str
    source_commit: str
    title: str
    notes: str
    repository_id: int = 0

    @property
    def identity(self):
        repository = f"github:{self.repository_id}" if self.repository_id else self.repository
        return f"{repository}:{self.channel}:{self.release_id}"


@dataclass(frozen=True)
class UpdateResult:
    status: str
    installed_version: str
    release: Optional[Release] = None
    message: str = ""


@dataclass(frozen=True)
class UpdatePlan:
    staging_dir: Path
    plan_sha256: str
    release: Release
    changed_files: tuple


@dataclass(frozen=True)
class InstallReceipt:
    backup_dir: Path
    source_commit: str
    version: str
    changed_files: tuple
    rollback_instruction: str


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _blob(data):
    # GitHub's SHA-1 object format. Full commit/blob IDs are validated below.
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _name(name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise UpdateError("The update contains an unsafe file path.")
    pure = PurePosixPath(name)
    if (pure.is_absolute() or pure.as_posix() != name or
            any(part in ("", ".", "..", ".git") for part in pure.parts) or
            any(ord(char) < 32 or ord(char) == 127 for char in name) or ":" in name):
        raise UpdateError("The update contains an unsafe file path.")
    return name


def _runtime(name):
    name = _name(name)
    if any(part.startswith(".") for part in PurePosixPath(name).parts):
        return False
    if name in _ROOT_FILES:
        return True
    path = PurePosixPath(name)
    if path.parts[0] in ("watchdogs", "plugins"):
        return path.suffix == ".py" or name == "watchdogs/radio_countries.json"
    return path.parts[0] == "assets" and path.suffix.lower() in _ASSET_EXTENSIONS


def _regular(path):
    path = Path(path)
    if path.is_symlink():
        raise UpdateError("An update path is a symbolic link; no files were replaced.")
    if not path.exists():
        return None
    if not path.is_file():
        raise UpdateError("An update path is not a regular file.")
    info = path.stat()
    if info.st_size > MAX_FILE_BYTES:
        raise UpdateError("An update file is too large.")
    value = {"sha256": _digest(path.read_bytes()), "mode": stat.S_IMODE(info.st_mode),
             "uid": info.st_uid, "gid": info.st_gid, "xattrs": {}}
    if hasattr(os, "listxattr"):
        value["xattrs"] = {key: base64.b64encode(os.getxattr(path, key)).decode("ascii")
                           for key in os.listxattr(path)}
    return value


def _fsync_directory(path):
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic(path, data, metadata=None, private=False):
    path = Path(path)
    previous = _regular(path)
    metadata = metadata or previous
    if metadata is None:
        parent = path.parent.stat()
        metadata = {"mode": 0o600 if private else 0o644,
                    "uid": parent.st_uid, "gid": parent.st_gid, "xattrs": {}}
    descriptor, filename = tempfile.mkstemp(prefix=".wdg-update-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600 if private else metadata["mode"])
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(temporary, metadata["uid"], metadata["gid"])
        if hasattr(os, "setxattr"):
            for key, value in metadata.get("xattrs", {}).items():
                os.setxattr(temporary, key, base64.b64decode(value, validate=True))
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _home():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        user = os.environ.get("SUDO_USER", "")
        if user and user != "root":
            import pwd
            return Path(pwd.getpwnam(user).pw_dir)
    return Path.home()


def _numeric(version):
    values = tuple(map(int, version.split(".")))
    return values + (0,) * (4 - len(values))


def filter_release_notes(notes, installed_version, latest_version):
    """Select missing version blocks for display without changing the release.

    Plain-text ``RELEASE 0.9.19 - DESCRIPTION`` sections remain readable by
    earlier clients, which display the complete string. This reader preserves
    their common preamble and shows only newer blocks, or the latest block when
    already current. Ambiguous/unrecognised input is displayed in full rather
    than silently dropping information. Installation always uses the complete
    approved target tree, independently of this presentation helper.
    """
    if (not isinstance(notes, str) or
            not isinstance(installed_version, str) or not _VERSION.fullmatch(installed_version) or
            not isinstance(latest_version, str) or not _VERSION.fullmatch(latest_version)):
        return notes
    try:
        installed, latest = _numeric(installed_version), _numeric(latest_version)
    except ValueError:
        return notes
    if installed > latest:
        return notes
    lines = notes.splitlines(keepends=True)
    headers = []
    versions = set()
    for index, line in enumerate(lines):
        text = line.strip()
        if not text.startswith("RELEASE"):
            continue
        match = re.fullmatch(r"RELEASE ([0-9]+(?:\.[0-9]+){1,3})(?: - [A-Z0-9][A-Z0-9 /()+,._:-]*)?", text)
        if match is None:
            return notes
        try:
            version = _numeric(match.group(1))
        except ValueError:
            return notes
        if version > latest or version in versions:
            return notes
        versions.add(version)
        headers.append((index, version))
    if not headers or latest not in versions:
        return notes
    selected = []
    for number, (start, version) in enumerate(headers):
        stop = headers[number + 1][0] if number + 1 < len(headers) else len(lines)
        if (installed < version <= latest) or (installed == latest == version):
            selected.extend(lines[start:stop])
    return "".join(lines[:headers[0][0]] + selected) if selected else notes


class GameUpdates:
    def __init__(self, root=None, home=None, git=None, metadata=None):
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        self.home = Path(home) if home is not None else _home()
        self.state_path = self.home / ".watchdogs_game_updates.json"
        self.staging_root = self.home / ".watchdogs_update_staging"
        self.backup_root = self.home / ".watchdogs_update_backups"
        self._git = git
        self._metadata = metadata
        self._source = dict(repository_id=REPOSITORY_ID, repository=REPOSITORY,
                            url=f"https://github.com/{REPOSITORY}", verified=False)
        self._thread_lock = threading.Lock()

    def source_info(self):
        """Return a cached display snapshot; drawing never starts network I/O."""
        return dict(self._source)

    def notice_key(self, release):
        return self._release(asdict(release)).identity

    def _github_metadata(self):
        """Read the pinned repository by ID, with owner credentials if private.

        Public installations use plain HTTPS and do not require gh or a token.
        The private preview can use the checkout owner's existing gh login;
        authentication material is never placed in a request URL or game log.
        """
        request = urllib.request.Request(
            f"https://api.github.com/repositories/{REPOSITORY_ID}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "WatchDogsGo-Updater"})
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                data = response.read(MAX_FEED_BYTES + 1)
            if len(data) > MAX_FEED_BYTES:
                raise UpdateError("The update source metadata is too large.")
            return json.loads(data.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, UnicodeError):
            pass
        if not shutil.which("gh"):
            raise UpdateError("Cannot verify the update source. Check network and GitHub access.")
        command = ["gh", "api", f"repositories/{REPOSITORY_ID}"]
        if hasattr(os, "geteuid") and os.geteuid() == 0 and self.root.stat().st_uid != 0:
            import pwd
            owner = pwd.getpwuid(self.root.stat().st_uid).pw_name
            command = ["sudo", "-n", "-H", "-u", owner] + command
        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                encoding="utf-8", errors="strict")
        if result.returncode or len(result.stdout.encode("utf-8")) > MAX_FEED_BYTES:
            raise UpdateError("Cannot verify the update source. Check network and GitHub access.")
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise UpdateError("The update source metadata is invalid.") from None

    def _resolve_source(self):
        # A failed fresh identity check must not leave an unverified link active.
        self._source = dict(self._source, verified=False)
        value = self._metadata() if self._metadata is not None else self._github_metadata()
        try:
            if not isinstance(value, dict):
                raise ValueError
            owner = value.get("owner")
            name = value.get("full_name")
            if (type(value.get("id")) is not int or value["id"] != REPOSITORY_ID or
                    not isinstance(owner, dict) or type(owner.get("id")) is not int or
                    owner["id"] != OWNER_ID or not isinstance(owner.get("login"), str) or
                    owner["login"].casefold() != OWNER.casefold() or
                    not isinstance(name, str) or not _REPOSITORY_NAME.fullmatch(name) or
                    name.partition("/")[0].casefold() != OWNER.casefold()):
                raise ValueError
        except (ValueError, TypeError):
            raise UpdateError("The update source identity does not match this fork.") from None
        self._source = dict(repository_id=REPOSITORY_ID, repository=name,
                            url=f"https://github.com/{name}", verified=True)
        return name

    def _run(self, *args, timeout=30):
        if self._git is not None:
            # The app's owner-aware _game_git has this exact signature.
            return self._git(self.root, *args, timeout=timeout)
        command = ["git", "-C", str(self.root), "-c", f"safe.directory={self.root}"]
        if shutil.which("gh"):
            command += ["-c", "credential.helper=!gh auth git-credential"]
        command += list(args)
        if hasattr(os, "geteuid") and os.geteuid() == 0 and self.root.stat().st_uid != 0:
            import pwd
            owner = pwd.getpwuid(self.root.stat().st_uid).pw_name
            command = ["sudo", "-n", "-H", "-u", owner] + command
        environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              env=environment, encoding="utf-8", errors="strict")

    def _required(self, *args, timeout=30):
        result = self._run(*args, timeout=timeout)
        if result.returncode:
            raise UpdateError("Cannot read the update source. Check network and GitHub access.")
        return result.stdout

    def _fetch(self):
        if not (self.root / ".git").exists():
            raise UpdateError("This installation has no Git metadata; a manual update is required.")
        repository = self._resolve_source()
        # Do not read/fetch `origin`: deployed installations retain LOCOSP's
        # original origin and an old HEAD beneath the reviewed source overlay.
        self._required("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                       f"https://github.com/{repository}.git",
                       f"+refs/heads/{BRANCH}:{FETCH_REF}", timeout=60)
        commit = self._required("rev-parse", "--verify", FETCH_REF + "^{commit}", timeout=10).strip()
        if not _COMMIT.fullmatch(commit):
            raise UpdateError("The update source returned an invalid commit.")
        return commit

    def _manifest(self):
        path = self.root / MANIFEST_PATH
        if _regular(path) is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not _COMMIT.fullmatch(value.get("source_commit", "")):
                raise ValueError
            if not isinstance(value.get("files", {}), dict):
                raise ValueError
            return value
        except (ValueError, TypeError, UnicodeError):
            raise UpdateError("The installed source manifest is invalid; a manual check is required.") from None

    def installed_version(self):
        path = self._target("watchdogs/__init__.py")
        if _regular(path) is None:
            raise UpdateError("The installed game version cannot be read.")
        return self._version_bytes(path.read_bytes())

    @staticmethod
    def _version_bytes(data):
        try:
            tree = ast.parse(data.decode("utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                        target.id == "__version__" for target in node.targets):
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str) and _VERSION.fullmatch(value):
                        return value
        except (ValueError, TypeError, SyntaxError, UnicodeError):
            pass
        raise UpdateError("The game version is invalid.")

    def installed_commit(self):
        manifest = self._manifest()
        if manifest is not None:
            return manifest["source_commit"]
        commit = self._required("rev-parse", "--verify", "HEAD^{commit}", timeout=10).strip()
        if not _COMMIT.fullmatch(commit):
            raise UpdateError("The installed source commit cannot be read.")
        return commit

    @staticmethod
    def _release(value):
        try:
            if not isinstance(value, dict) or set(value) - {"schema", "repository", "repository_id", "channel",
                    "release_id", "version", "source_commit", "title", "notes"}:
                raise ValueError
            schema = value.get("schema", 1)
            if type(schema) is not int or schema not in (1, 2):
                raise ValueError
            repository = value.get("repository")
            repository_id = value.get("repository_id", 0)
            if (not isinstance(repository, str) or not _REPOSITORY_NAME.fullmatch(repository) or
                    repository.partition("/")[0].casefold() != OWNER.casefold() or
                    type(repository_id) is not int or
                    repository_id not in (0, REPOSITORY_ID) or
                    (schema == 2 and repository_id != REPOSITORY_ID) or
                    (repository_id == 0 and repository != REPOSITORY) or
                    value.get("channel") != CHANNEL):
                raise ValueError
            if not _ID.fullmatch(value.get("release_id", "")):
                raise ValueError
            if not _VERSION.fullmatch(value.get("version", "")):
                raise ValueError
            if not _COMMIT.fullmatch(value.get("source_commit", "")):
                raise ValueError
            title, notes = value.get("title"), value.get("notes")
            if (not isinstance(title, str) or not title.strip() or len(title) > 120 or
                    any(ord(c) < 32 or ord(c) == 127 for c in title)):
                raise ValueError
            if (not isinstance(notes, str) or not notes.strip() or len(notes.encode("utf-8")) > 65536 or
                    any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in notes)):
                raise ValueError
            return Release(**{key: value[key] for key in Release.__dataclass_fields__
                              if key != "repository_id"}, repository_id=REPOSITORY_ID)
        except (ValueError, TypeError, UnicodeError):
            raise UpdateError("The published update manifest is invalid.") from None

    def _ancestor(self, parent, child):
        value = self._run("merge-base", "--is-ancestor", parent, child, timeout=15)
        if value.returncode not in (0, 1):
            raise UpdateError("Cannot verify the update history; a manual check is required.")
        return value.returncode == 0

    def _validate_release(self, release, fetched_commit):
        release = self._release(asdict(release))
        if release.repository not in {REPOSITORY, self._source["repository"]}:
            raise UpdateError("The release does not belong to the verified update source.")
        installed = self.installed_commit()
        version = self.installed_version()
        if not self._ancestor(release.source_commit, fetched_commit):
            raise UpdateError("The release is outside the approved update branch.")
        if release.source_commit == installed:
            if release.version != version:
                raise UpdateError("The release version does not match the installed source.")
            return False
        if (_numeric(release.version) == _numeric(version) and
                self._ancestor(release.source_commit, installed)):
            # A release feed is committed after the code it identifies. A fresh
            # clone can therefore have a newer documentation/feed-only HEAD.
            # Accept that baseline only when the complete runtime Git trees
            # match; never silently label an unrelated or modified build current.
            if self._tree(release.source_commit) == self._tree(installed):
                return False
            raise UpdateError("This installation has unapproved game changes after the release.")
        if not self._ancestor(installed, release.source_commit):
            raise UpdateError("This update would replace a newer or unrelated installation.")
        if _numeric(release.version) <= _numeric(version):
            raise UpdateError("The release does not have a newer version number.")
        return True

    def check(self):
        version = "unknown"
        try:
            version = self.installed_version()
            fetched = self._fetch()
            entry = self._required("ls-tree", fetched, "--", FEED_PATH, timeout=10).strip()
            if not entry:
                return UpdateResult("unpublished", version,
                                    message="No published update is available.")
            # Only a regular text blob is a release feed; never follow a link.
            if not entry.startswith("100644 blob ") and not entry.startswith("100755 blob "):
                raise UpdateError("The published update manifest is not a regular file.")
            size = int(self._required("cat-file", "-s", f"{fetched}:{FEED_PATH}", timeout=10))
            if size > MAX_FEED_BYTES:
                raise UpdateError("The published update manifest is too large.")
            text = self._required("show", f"{fetched}:{FEED_PATH}", timeout=10)
            release = self._release(json.loads(text))
            available = self._validate_release(release, fetched)
            return UpdateResult("available" if available else "current", version, release,
                                "An update is available." if available else "Your game is up to date.")
        except UpdateError as error:
            return UpdateResult("error", version, message=str(error))
        except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
            # No raw subprocess diagnostics, command lines, private URLs or tokens.
            return UpdateResult("error", version,
                                message="Cannot check for updates. Check network and GitHub access.")

    def _seen(self):
        if _regular(self.state_path) is None:
            return {"schema": 1, "seen": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("schema") != 1 or
                    not isinstance(data.get("seen"), list) or len(data["seen"]) > 10000 or
                    any(not isinstance(key, str) or len(key) > 256 for key in data["seen"])):
                raise ValueError
            return data
        except (ValueError, TypeError, UnicodeError):
            raise UpdateError("Update notification history cannot be read; open Update Game manually.") from None

    def notice_pending(self, release):
        release = self._release(asdict(release))
        seen = self._seen()["seen"]
        legacy = {f"{repository}:{release.channel}:{release.release_id}"
                  for repository in (REPOSITORY, release.repository, self._source["repository"])}
        return release.identity not in seen and not legacy.intersection(seen)

    def acknowledge(self, release):
        """Persist immediately before showing the modal (not at fetch time).

        Both OK and No/Esc then lead to the same durable, once-per-release
        behavior. Editing release notes or restarting the game does not nag.
        Failure leaves the original state untouched and must suppress the modal.
        """
        release = self._release(asdict(release))
        with self._thread_lock:
            data = self._seen()
            if release.identity not in data["seen"]:
                data["seen"].append(release.identity)
                try:
                    _atomic(self.state_path, _encoded(data), private=True)
                except OSError:
                    raise UpdateError("Cannot save update notification history; open Update Game manually.") from None

    def _target(self, name):
        # The one dot-prefixed, exact metadata filename is controlled locally.
        if name != MANIFEST_PATH:
            _name(name)
        path = self.root
        for part in PurePosixPath(name).parts:
            path = path / part
            if path.is_symlink():
                raise UpdateError("An update path is a symbolic link; no files were replaced.")
        return path

    def _private_directory(self, path):
        path = Path(path)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise UpdateError("The private update directory is not safe.")
        if not path.exists():
            path.mkdir(mode=0o700)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                owner = self.home.stat()
                os.chown(path, owner.st_uid, owner.st_gid)
            _fsync_directory(path.parent)
        return path

    def _fresh_directory(self, parent, prefix):
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            owner = self.home.stat()
            os.chown(path, owner.st_uid, owner.st_gid)
        _fsync_directory(parent)
        return path

    def _owned_parents(self, path, *, private=True):
        """Create only missing parents, owned by the actual player/checkout."""
        missing = []
        parent = Path(path)
        while not parent.exists():
            if parent.is_symlink():
                raise UpdateError("An update directory is a symbolic link.")
            missing.append(parent)
            parent = parent.parent
        owner = (self.home if private else self.root).stat()
        for parent in reversed(missing):
            parent.mkdir(mode=0o700 if private else 0o755)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(parent, owner.st_uid, owner.st_gid)
            _fsync_directory(parent.parent)
        return missing

    def _tree(self, commit):
        output = self._required("ls-tree", "-r", "-l", "-z", commit, timeout=15)
        result = {}
        total = 0
        for entry in output.split("\0"):
            if not entry:
                continue
            try:
                header, name = entry.split("\t", 1)
                mode, kind, oid, size = header.split()
                selected = _runtime(name)
                if not selected:
                    continue
                if mode not in ("100644", "100755") or kind != "blob" or not _COMMIT.fullmatch(oid):
                    raise UpdateError("The release contains an unsupported code or asset link.")
                size = int(size)
                if size < 0 or size > MAX_FILE_BYTES:
                    raise UpdateError("A release file is too large.")
                total += size
                if total > MAX_TOTAL_BYTES or len(result) >= MAX_FILES:
                    raise UpdateError("The release is too large for an in-game update.")
                result[name] = {"oid": oid, "mode": 0o755 if mode == "100755" else 0o644,
                                "size": size}
            except (ValueError, TypeError):
                raise UpdateError("The release file list is invalid.") from None
        if not {"watchdogs/app.py", "watchdogs/__init__.py", "watchdogs/__main__.py",
                "watchdogs/game_updates.py"} <= result.keys():
            raise UpdateError("The release is missing required game code.")
        return result

    def _archive(self, commit, tree, directory):
        archive_path = directory / "source.tar"
        self._required("archive", "--format=tar", "--output=" + str(archive_path),
                       commit, "--", *tree, timeout=45)
        if archive_path.is_symlink() or not archive_path.is_file():
            raise UpdateError("The staged release archive is missing.")
        if archive_path.stat().st_size > MAX_TOTAL_BYTES + MAX_FILES * 2048:
            raise UpdateError("The staged release archive is too large.")
        contents = {}
        try:
            with tarfile.open(archive_path, "r:") as archive:
                for member in archive:
                    if member.isdir():
                        _name(member.name.rstrip("/"))
                        continue
                    name = _name(member.name)
                    if (not member.isfile() or name not in tree or name in contents or
                            member.size != tree[name]["size"]):
                        raise UpdateError("The staged release archive is invalid.")
                    stream = archive.extractfile(member)
                    data = stream.read()
                    if _blob(data) != tree[name]["oid"]:
                        raise UpdateError("A staged release file failed verification.")
                    if name.endswith(".py"):
                        try:
                            compile(data, name, "exec", dont_inherit=True)
                        except (SyntaxError, ValueError):
                            raise UpdateError("A release Python file has invalid syntax.") from None
                    contents[name] = data
            if contents.keys() != tree.keys():
                raise UpdateError("The staged release archive is incomplete.")
            return contents
        except (tarfile.TarError, OSError):
            raise UpdateError("Cannot read the staged release archive.") from None
        finally:
            archive_path.unlink(missing_ok=True)

    def prepare(self, release):
        """Freshly verify a release and stage it without changing installed files."""
        release = self._release(asdict(release))
        fetched = self._fetch()
        if not self._validate_release(release, fetched):
            raise UpdateError("This release is already installed.")
        # Re-read the feed after the fresh fetch. A revoked/replaced release
        # cannot be installed from an earlier notification's cached manifest.
        text = self._required("show", f"{fetched}:{FEED_PATH}", timeout=10)
        if len(text.encode("utf-8")) > MAX_FEED_BYTES or self._release(json.loads(text)) != release:
            raise UpdateError("The published release changed. Check for updates again.")
        installed = self.installed_commit()
        target_tree, base_tree = self._tree(release.source_commit), self._tree(installed)
        old_requirements = base_tree.get("requirements.txt", {}).get("oid")
        if target_tree.get("requirements.txt", {}).get("oid") != old_requirements:
            raise UpdateError("Dependencies changed. A manual update is required.")
        parent = self._private_directory(self.staging_root)
        directory = self._fresh_directory(parent, "release-")
        try:
            contents = self._archive(release.source_commit, target_tree, directory)
            if self._version_bytes(contents["watchdogs/__init__.py"]) != release.version:
                raise UpdateError("The release version does not match its game code.")
            if "watchdogs/game_updates.py" in base_tree:
                startup = contents.get("watchdogs/__main__.py", b"")
                startup_tree = ast.parse(startup) if startup else None
                calls_recovery = startup_tree is not None and any(
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                    node.func.id == "_recover_game_update" for node in ast.walk(startup_tree))
                if "watchdogs/game_updates.py" not in contents or not calls_recovery:
                    raise UpdateError("The release is missing startup update recovery.")
            manifest = self._manifest()
            changes = {}
            for name in sorted(base_tree.keys() | target_tree.keys()):
                before, after = base_tree.get(name), target_tree.get(name)
                if before == after:
                    continue
                current = _regular(self._target(name))
                if before is None:
                    if current is not None:
                        raise UpdateError("A new release file conflicts with a local file; no files were replaced.")
                else:
                    if current is None:
                        raise UpdateError("An installed source file is missing; no files were replaced.")
                    data = self._target(name).read_bytes()
                    tracked = (manifest or {}).get("files", {}).get(name)
                    expected_sha = tracked.get("sha256") if isinstance(tracked, dict) else None
                    if ((expected_sha is not None and current["sha256"] != expected_sha) or
                            (expected_sha is None and _blob(data) != before["oid"])):
                        raise UpdateError("Local source changes need review; no files were replaced.")
                changes[name] = {"before": current,
                                 "after": None if after is None else {
                                     "sha256": _digest(contents[name]), "mode": after["mode"]}}
                if after is not None:
                    target = directory / "files" / name
                    self._owned_parents(target.parent)
                    _atomic(target, contents[name], private=True)
            if not changes:
                raise UpdateError("This release contains no game changes.")
            manifest_metadata = _regular(self._target(MANIFEST_PATH))
            plan = {"schema": 1, "root": str(self.root), "release": asdict(release),
                    "installed_commit": installed, "installed_version": self.installed_version(),
                    "changes": changes, "manifest_before": manifest_metadata,
                    "manifest_value": manifest}
            packed = _encoded(plan)
            _atomic(directory / "plan.json", packed, private=True)
            return UpdatePlan(directory, _digest(packed), release, tuple(changes))
        except Exception:
            # This directory was created by us and no install path has changed.
            shutil.rmtree(directory)
            raise

    def _load_plan(self, plan):
        if not isinstance(plan, UpdatePlan) or plan.staging_dir.is_symlink():
            raise UpdateError("The staged update plan is invalid.")
        directory = plan.staging_dir.resolve()
        if directory.parent != self.staging_root.resolve() or not directory.is_dir():
            raise UpdateError("The staged update plan is outside its private directory.")
        plan_file = directory / "plan.json"
        if _regular(plan_file) is None:
            raise UpdateError("The staged update plan is missing.")
        packed = plan_file.read_bytes()
        if _digest(packed) != plan.plan_sha256:
            raise UpdateError("The staged update plan failed verification.")
        value = json.loads(packed)
        if (value.get("schema") != 1 or value.get("root") != str(self.root) or
                self._release(value.get("release")) != plan.release or
                tuple(value.get("changes", {})) != plan.changed_files):
            raise UpdateError("The staged update plan is invalid.")
        for name, change in value["changes"].items():
            if not _runtime(name):
                raise UpdateError("The staged update includes a non-code file.")
            target = self._target(name)
            if _regular(target) != change["before"]:
                raise UpdateError("Installed source changed since preparation; no files were replaced.")
            if change["after"] is not None:
                source = self._private_target(directory / "files", name)
                _regular(source)
                if source.is_symlink() or _digest(source.read_bytes()) != change["after"]["sha256"]:
                    raise UpdateError("A staged release file failed verification.")
        if (self.installed_commit() != value["installed_commit"] or
                self.installed_version() != value["installed_version"] or
                _regular(self._target(MANIFEST_PATH)) != value["manifest_before"]):
            raise UpdateError("The installed version changed since preparation; no files were replaced.")
        return value

    @staticmethod
    def _private_target(directory, name):
        _name(name)
        path = Path(directory)
        if path.is_symlink():
            raise UpdateError("A private update path is a symbolic link.")
        for part in PurePosixPath(name).parts:
            path = path / part
            if path.is_symlink():
                raise UpdateError("A private update path is a symbolic link.")
        return path

    def discard(self, plan):
        """Remove only this verified private staging plan, never installed files."""
        if (not isinstance(plan, UpdatePlan) or self.staging_root.is_symlink() or
                plan.staging_dir.is_symlink() or
                plan.staging_dir.resolve().parent != self.staging_root.resolve()):
            raise UpdateError("The staged update plan is outside its private directory.")
        if not plan.staging_dir.exists():
            return
        path = plan.staging_dir / "plan.json"
        if _regular(path) is None or _digest(path.read_bytes()) != plan.plan_sha256:
            raise UpdateError("The staged update plan failed verification.")
        shutil.rmtree(plan.staging_dir)

    @contextmanager
    def _installation_lock(self):
        with self._thread_lock:
            self._private_directory(self.backup_root)
            path = self.backup_root / ".lock"
            if path.is_symlink():
                raise UpdateError("The update lock is not safe.")
            with path.open("a+b") as stream:
                if os.name == "posix":
                    import fcntl
                    os.fchmod(stream.fileno(), 0o600)
                    if os.geteuid() == 0:
                        owner = self.home.stat()
                        os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise UpdateError("Another game update is running.") from None
                try:
                    yield
                finally:
                    if os.name == "posix":
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _incomplete(self):
        for path in self.backup_root.glob("update-*/receipt.json"):
            if path.is_symlink() or path.parent.is_symlink():
                raise UpdateError("An update recovery record is not safe.")
            value = json.loads(path.read_text(encoding="utf-8"))
            declared_root = value.get("root")
            if (value.get("schema") != 1 or not isinstance(declared_root, str) or
                    not Path(declared_root).is_absolute() or
                    any(part in (".", "..") for part in Path(declared_root).parts) or
                    value.get("status") not in ("prepared", "installing", "recovery_required",
                                                 "committed", "rolled_back")):
                raise UpdateError("An update recovery record is invalid.")
            if declared_root != str(self.root):
                continue
            if value.get("status") in ("installing", "recovery_required"):
                raise UpdateError("An interrupted update needs code rollback before another update.")

    def install(self, plan, *, quiescent=False):
        """Apply staged code only, after the caller has stopped/flushed the game.

        No automatic restart occurs. On a recoverable write failure the old
        code and manifest are restored immediately; player data is never read,
        written, deleted or included in the backup's restore scope.
        """
        if quiescent is not True:
            raise UpdateError("Stop game operations before installing the update.")
        with self._installation_lock():
            self._incomplete()
            value = self._load_plan(plan)
            backup = self._fresh_directory(self.backup_root, "update-")
            journal = {"schema": 1, "root": str(self.root), "status": "prepared",
                       "release": asdict(plan.release), "source_before": value["installed_commit"],
                       "changes": value["changes"], "manifest_before": value["manifest_before"],
                       "manifest_after": None, "created_directories": []}
            recovery_bytes = Path(__file__).read_bytes()
            _atomic(backup / "recovery.py", recovery_bytes, private=True)
            journal["recovery_sha256"] = _digest(recovery_bytes)
            for name, change in value["changes"].items():
                if change["before"] is not None:
                    saved = backup / "files" / name
                    self._owned_parents(saved.parent)
                    _atomic(saved, self._target(name).read_bytes(), private=True)
            if value["manifest_before"] is not None:
                _atomic(backup / "manifest-before.json", self._target(MANIFEST_PATH).read_bytes(), private=True)
            new_manifest = dict(value["manifest_value"] or {})
            new_manifest["files"] = dict(new_manifest.get("files", {}))
            for name, change in value["changes"].items():
                if change["after"] is None:
                    new_manifest["files"].pop(name, None)
                else:
                    new_manifest["files"][name] = {"sha256": change["after"]["sha256"]}
            new_manifest.update(source_commit=plan.release.source_commit, branch=BRANCH,
                                source_repository=self._source["repository"], source_repository_id=REPOSITORY_ID,
                                version=plan.release.version,
                                game_update_release=plan.release.release_id)
            manifest_bytes = _encoded(new_manifest)
            journal["manifest_after"] = _digest(manifest_bytes)
            receipt_file = backup / "receipt.json"
            journal["status"] = "installing"
            # Flush all newly created backup directory entries before making
            # the durable journal visible. The frozen recovery module is loaded
            # by __main__ before any possibly mixed application modules.
            for directory, _, _ in os.walk(backup, topdown=False):
                _fsync_directory(directory)
            _fsync_directory(self.backup_root)
            _atomic(receipt_file, _encoded(journal), private=True)
            try:
                for name, change in value["changes"].items():
                    target = self._target(name)
                    missing = []
                    parent = target.parent
                    while not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    for parent in reversed(missing):
                        parent.mkdir(mode=0o755)
                        if hasattr(os, "geteuid") and os.geteuid() == 0:
                            owner = self.root.stat()
                            os.chown(parent, owner.st_uid, owner.st_gid)
                        _fsync_directory(parent.parent)
                        journal["created_directories"].append(parent.relative_to(self.root).as_posix())
                        _atomic(receipt_file, _encoded(journal), private=True)
                    if change["after"] is None:
                        target.unlink()
                        _fsync_directory(target.parent)
                    else:
                        metadata = change["before"]
                        if metadata is None:
                            owner = self.root.stat()
                            metadata = dict(mode=change["after"]["mode"], uid=owner.st_uid,
                                            gid=owner.st_gid, xattrs={})
                        _atomic(target, self._private_target(plan.staging_dir / "files", name).read_bytes(), metadata)
                _atomic(self._target(MANIFEST_PATH), manifest_bytes, value["manifest_before"], private=True)
                for name, change in value["changes"].items():
                    actual = _regular(self._target(name))
                    expected = change["after"]
                    if ((expected is None and actual is not None) or
                            (expected is not None and (actual is None or actual["sha256"] != expected["sha256"]))):
                        raise UpdateError("An installed code file failed verification.")
                journal["status"] = "committed"
                _atomic(receipt_file, _encoded(journal), private=True)
            except Exception:
                try:
                    self._restore(backup, journal)
                except Exception:
                    journal["status"] = "recovery_required"
                    _atomic(receipt_file, _encoded(journal), private=True)
                    raise UpdateError("The update stopped. Its code backup needs manual rollback.") from None
                raise UpdateError("The update failed; previous code was restored. Player data was kept.") from None
            return InstallReceipt(backup, plan.release.source_commit, plan.release.version,
                                  plan.changed_files,
                                  "Close the game, then use GameUpdates.rollback with this backup directory.")

    def _restore(self, backup, journal):
        # Verify EVERY backup and current target before touching any file.
        for name, change in journal["changes"].items():
            if not _runtime(name):
                raise UpdateError("The code recovery record contains an unsafe path.")
            before, after = change["before"], change["after"]
            current = _regular(self._target(name))
            if before is not None:
                saved = self._private_target(backup / "files", name)
                if _regular(saved) is None or _digest(saved.read_bytes()) != before["sha256"]:
                    raise UpdateError("A code backup failed verification.")
            current_sha = None if current is None else current["sha256"]
            allowed = {None if before is None else before["sha256"],
                       None if after is None else after["sha256"]}
            if current_sha not in allowed:
                raise UpdateError("Code changed after the update; automatic rollback was refused.")
        current_manifest = _regular(self._target(MANIFEST_PATH))
        before = journal["manifest_before"]
        if (None if current_manifest is None else current_manifest["sha256"]) not in {
                None if before is None else before["sha256"], journal["manifest_after"]}:
            raise UpdateError("The installed manifest changed; automatic rollback was refused.")
        if before is not None:
            saved_manifest = backup / "manifest-before.json"
            if _regular(saved_manifest) is None or _digest(saved_manifest.read_bytes()) != before["sha256"]:
                raise UpdateError("The previous source manifest backup failed verification.")
        for name, change in journal["changes"].items():
            target = self._target(name)
            if change["before"] is None:
                target.unlink(missing_ok=True)
                if target.parent.exists():
                    _fsync_directory(target.parent)
            else:
                _atomic(target, self._private_target(backup / "files", name).read_bytes(), change["before"])
        if before is None:
            self._target(MANIFEST_PATH).unlink(missing_ok=True)
            _fsync_directory(self.root)
        else:
            _atomic(self._target(MANIFEST_PATH), (backup / "manifest-before.json").read_bytes(), before)
        for name in reversed(journal.get("created_directories", [])):
            directory = self._target(_name(name))
            try:
                directory.rmdir()
            except OSError:
                pass  # A newer file is never deleted to tidy an update directory.
        journal["status"] = "rolled_back"
        _atomic(backup / "receipt.json", _encoded(journal), private=True)

    def rollback(self, receipt_or_directory, *, quiescent=False):
        if quiescent is not True:
            raise UpdateError("Close the game before rolling back its code.")
        directory = (receipt_or_directory.backup_dir if isinstance(receipt_or_directory, InstallReceipt)
                     else Path(receipt_or_directory))
        if directory.is_symlink() or directory.resolve().parent != self.backup_root.resolve():
            raise UpdateError("The code backup is outside its private directory.")
        with self._installation_lock():
            path = directory / "receipt.json"
            if _regular(path) is None:
                raise UpdateError("The code recovery record is missing.")
            journal = json.loads(path.read_text(encoding="utf-8"))
            if journal.get("schema") != 1 or journal.get("root") != str(self.root):
                raise UpdateError("The code backup belongs to another installation.")
            self._release(journal.get("release"))
            if journal.get("status") == "rolled_back":
                return
            if journal.get("status") not in ("installing", "recovery_required", "committed"):
                raise UpdateError("The code recovery record has an unsupported state.")
            self._restore(directory, journal)
