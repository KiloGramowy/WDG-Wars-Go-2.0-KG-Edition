#!/usr/bin/env python3
"""Check a prepared formal fork; rename only after explicit instruction.

Default / --check / --dry-run make no remote changes. Future activation needs
--activate --confirm-public --expected-head FULL_SHA. It changes only the same
public fork's name, then verifies its upstream and anonymous source/media access.

GitHub's rename redirects cover web and Git operations; do not reuse the old
name. Attachments follow repository access. Anonymous checks can only establish
public accessibility after activation, not while the repository is private.
https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository
https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files
"""
import argparse
import ast
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXPECTED_REPOSITORY_ID = 1442123109
EXPECTED_OWNER_ID = 315514197
OWNER = "KiloGramowy"
REVIEW_NAME = "WatchDogsGo-KG-Edition"
PUBLIC_NAME = "WDG-Wars-Go-2.0-KG-Edition"
BRANCH = "main"
EXPECTED_UPSTREAM_ID = 1208909098
EXPECTED_UPSTREAM_OWNER_ID = 4149309
UPSTREAM_OWNER = "LOCOSP"
UPSTREAM_REPOSITORY = "LOCOSP/WatchDogsGo"
APPROVED_UPSTREAM_TIP = "6412603d6fa7c8f2e6fe76873531d83c61adc63d"
APPROVED_SNAPSHOT_TIP = "29412dccbfe9aff4c798252a7510d4b0314d7168"
INTEGRATION_BASE = "be199c2ced736192399949bbf2b68098a4b5ae57"
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_RESPONSE = 12 * 1024 * 1024


class PublicationError(RuntimeError):
    """Safe public diagnostic; raw CLI output and signed URLs are not echoed."""


def package_module():
    path = Path(__file__).resolve().with_name("release_package.py")
    spec = importlib.util.spec_from_file_location("wdg_publication_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise PublicationError("Cannot verify the local release checkout.")
    return result.stdout.decode("utf-8").strip()


def literal_assignments(data):
    values = {}
    try:
        tree = ast.parse(data.decode("utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    values[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
    except (UnicodeError, SyntaxError):
        raise PublicationError("A release source file cannot be parsed.") from None
    return values


class GitHub:
    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def _request(self, method, endpoint, data=None, missing_ok=False):
        command = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        if data is not None:
            command += ["--input", "-"]
        try:
            result = self.runner(command, input=None if data is None else json.dumps(data),
                                 capture_output=True, text=True, timeout=35)
        except (OSError, subprocess.SubprocessError):
            # A PATCH can succeed before its CLI response is lost. Let activation
            # reconcile the actual repository state through its stable ID.
            raise PublicationError("GitHub request did not complete; verify the connection and current repository state.") from None
        if result.returncode:
            if missing_ok and re.search(r"HTTP\s+404\b", result.stderr or ""):
                return None
            raise PublicationError("GitHub access failed; check authentication and repository administration rights.")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise PublicationError("GitHub returned an invalid response.") from None

    def get(self, endpoint, missing_ok=False):
        return self._request("GET", endpoint, missing_ok=missing_ok)

    def patch(self, endpoint, data):
        return self._request("PATCH", endpoint, data)


def allowed_https(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except (ValueError, TypeError):
        return False
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and port in (None, 443) and (host in ("github.com", "api.github.com") or host.endswith(".githubusercontent.com")
            or host == "raw.githubusercontent.com" or re.fullmatch(
                r"github-production-(?:user|release)-asset-[a-z0-9-]+\.s3(?:\.[a-z0-9-]+)?\.amazonaws\.com", host)))


class SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not allowed_https(new_url):
            raise PublicationError("An anonymous check was redirected outside the approved HTTPS media hosts.")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def anonymous_get(url, *, video=False):
    """No GitHub token, browser cookie or account session is attached."""
    if not allowed_https(url):
        raise PublicationError("An anonymous check has an unsafe URL.")
    headers = {"User-Agent": "WatchDogsGo-release-check", "Accept": "*/*"}
    if video:
        headers["Range"] = "bytes=0-65535"
    request = Request(url, headers=headers, method="GET")
    last = None
    for attempt in range(3):
        try:
            with build_opener(SafeRedirects()).open(request, timeout=12) as response:
                if response.status not in (200, 206):
                    raise PublicationError("Public source or media is not available anonymously.")
                limit = 65536 if video else MAX_RESPONSE
                data = response.read(limit + 1)
                if not video and len(data) > limit:
                    raise PublicationError("An anonymous source/media response exceeds its limit.")
                return data[:limit]
        except (HTTPError, URLError, OSError) as error:
            last = error
            if attempt < 2:
                time.sleep(2)
    raise PublicationError("Public source or media is not yet available anonymously; verify the public state before announcing it.") from last


def fork_identity(value, *, canonical=False):
    """Require this exact public fork and both original upstream identities."""
    owner = value.get("owner", {}) if isinstance(value, dict) else {}
    if (not isinstance(value, dict) or not isinstance(owner, dict)
            or value.get("private") is not False or value.get("id") != EXPECTED_REPOSITORY_ID
            or owner.get("id") != EXPECTED_OWNER_ID or owner.get("login") != OWNER
            or not isinstance(value.get("name"), str)
            or value.get("full_name") != OWNER + "/" + value.get("name", "")
            or value.get("name") not in (REVIEW_NAME, PUBLIC_NAME)
            or value.get("default_branch") != BRANCH or value.get("archived")
            or value.get("disabled") or value.get("fork") is not True
            or (canonical and value["name"] != PUBLIC_NAME)):
        raise PublicationError("Repository identity does not match the approved public fork.")
    for relation in ("parent", "source"):
        upstream = value.get(relation)
        upstream_owner = upstream.get("owner") if isinstance(upstream, dict) else None
        if (not isinstance(upstream, dict) or upstream.get("id") != EXPECTED_UPSTREAM_ID
                or upstream.get("full_name") != UPSTREAM_REPOSITORY
                or upstream.get("private") is not False or not isinstance(upstream_owner, dict)
                or upstream_owner.get("id") != EXPECTED_UPSTREAM_OWNER_ID
                or upstream_owner.get("login") != UPSTREAM_OWNER):
            raise PublicationError("The formal fork is not connected to the approved LOCOSP upstream.")
    return value


def metadata(client):
    return fork_identity(client.get("repositories/" + str(EXPECTED_REPOSITORY_ID)))


def only_documentation_changes(names):
    """Allow packaging-only changes after the approved game runtime."""
    roots = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
             ".gitignore", ".gitattributes"}
    tools = {"tools/public_release.py", "tests/test_public_release.py"}
    return all(name != "docs/UPDATE_PREVIEW.txt" and (name in roots or name in tools or
        name.startswith(("docs/", "updates/"))) for name in names)


def runtime_entries(repo, ref):
    """Compare Git modes/blobs without executing code from either snapshot."""
    roots = {"meshtastic_rx_probe.py", "requirements.txt", "run.sh", "setup.sh",
             "watchdogs-launcher", "watchdogs.desktop", "LICENSE", "THIRD_PARTY_NOTICES.md",
             "CHANGELOG.md", "docs/UPDATE_PREVIEW.txt"}
    asset_extensions = {".png", ".wav", ".mp3", ".bdf", ".svg"}
    entries = {}
    for row in git(repo, "ls-tree", "-r", "-z", ref).split("\0"):
        if not row:
            continue
        header, name = row.split("\t", 1)
        path = PurePosixPath(name)
        if any(part.startswith(".") for part in path.parts):
            continue
        if (name in roots or (path.parts[0] in ("watchdogs", "plugins") and
                (path.suffix == ".py" or name == "watchdogs/radio_countries.json"))
                or (path.parts[0] == "assets" and path.suffix.lower() in asset_extensions)):
            entries[name] = tuple(header.split())
    return entries


class VideoSources(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        source = dict(attrs).get("src")
        if tag in ("video", "source") and source:
            self.sources.append(source)


def public_video_source(source, film_id):
    if not allowed_https(source) or any(ord(char) < 32 for char in source):
        return False
    parsed = urlsplit(source)
    return (parsed.hostname == "private-user-images.githubusercontent.com" and
            re.fullmatch("/" + str(EXPECTED_OWNER_ID) + r"/[0-9]+-" + re.escape(film_id) + r"\.mp4",
                         parsed.path) is not None)


def reviewed_history(repo, commit):
    """Accept two pinned public histories and one exact snapshot merge only.

    Old upstream commits were already public and predate our source packaging.
    Every snapshot/message from the reviewed release line and its descendants
    remains subject to validation. No private branches or extra merges enter.
    """
    upstream = set(git(repo, "rev-list", APPROVED_UPSTREAM_TIP).splitlines())
    snapshot = set(git(repo, "rev-list", APPROVED_SNAPSHOT_TIP).splitlines())
    commits = git(repo, "rev-list", commit).splitlines()
    reachable = set(commits)
    if not (upstream | snapshot | {INTEGRATION_BASE}).issubset(reachable):
        raise PublicationError("The formal fork is missing its approved public histories.")
    parents = git(repo, "show", "-s", "--format=%P", INTEGRATION_BASE).split()
    if len(parents) != 2 or set(parents) != {APPROVED_UPSTREAM_TIP, APPROVED_SNAPSHOT_TIP}:
        raise PublicationError("The integration merge must have exactly its two approved public parents.")
    if git(repo, "rev-parse", INTEGRATION_BASE + "^{tree}") != git(
            repo, "rev-parse", APPROVED_SNAPSHOT_TIP + "^{tree}"):
        raise PublicationError("The integration merge does not preserve the approved complete snapshot.")
    introduced = reachable - upstream - snapshot
    for item in introduced - {INTEGRATION_BASE}:
        parents = git(repo, "show", "-s", "--format=%P", item).split()
        if len(parents) != 1 or parents[0] not in introduced:
            raise PublicationError("Unapproved merged or private history follows the integration snapshot.")
    return commits, snapshot | introduced, upstream


def source_checks(repo, package, expected_head=None):
    if git(repo, "status", "--porcelain"):
        raise PublicationError("The release checkout has uncommitted or untracked changes.")
    if git(repo, "branch", "--show-current") != BRANCH:
        raise PublicationError("The release checkout must be on main.")
    files, modes, commit, tree = package.snapshot(repo, "HEAD")
    if expected_head is not None and (not SHA.fullmatch(expected_head) or commit != expected_head):
        raise PublicationError("The requested publication commit is not the checked checkout HEAD.")
    commits, reviewed, upstream = reviewed_history(repo, commit)
    validation = package.validate(files)
    if not validation["ok"]:
        raise PublicationError("The current source/media package has unresolved validation errors.")
    readme_prose = package.prose(files["README.md"].decode("utf-8"))
    if re.search(r"repository remains private|private preview|no release (?:feed.{0,50}published|published yet)",
                 readme_prose, re.IGNORECASE):
        raise PublicationError("The release README still contains obsolete private-preview/no-release claims.")
    # The pinned original upstream predates our package layout; audit the exact
    # reviewed release history and all newly introduced snapshots/messages.
    messages=git(repo,"log","--no-walk","--format=%B",*sorted(reviewed))
    if package.TOKEN_PATTERN.search(messages) or package.PRIVATE_PATH_PATTERN.search(messages):
        raise PublicationError("A reachable commit message contains material requiring private review.")
    for prior in reviewed:
        if prior != commit and not package.validate(package.snapshot(repo, prior)[0])["ok"]:
            raise PublicationError("An earlier reachable candidate commit contains material that is not cleared by source validation.")
    settings = literal_assignments(files.get("watchdogs/game_updates.py", b""))
    if (settings.get("REPOSITORY_ID") != EXPECTED_REPOSITORY_ID or settings.get("BRANCH") != BRANCH
            or settings.get("CHANNEL") != "stable" or settings.get("FEED_PATH") != "updates/stable.json"
            or settings.get("REPOSITORY") not in (OWNER+"/"+REVIEW_NAME,OWNER+"/"+PUBLIC_NAME)):
        raise PublicationError("The updater is not pinned to the approved repository ID/main/stable source.")
    version = literal_assignments(files.get("watchdogs/__init__.py", b"")).get("__version__")
    feed_path = settings.get("FEED_PATH", "updates/stable.json")
    try:
        feed = json.loads(files[feed_path])
    except (KeyError, ValueError, TypeError):
        raise PublicationError("The approved baseline update feed is missing or invalid.") from None
    if not isinstance(feed, dict):
        raise PublicationError("The baseline update feed is not an object.")
    source = feed.get("source_commit", "")
    release_id = feed.get("release_id")
    title, notes = feed.get("title"), feed.get("notes")
    if (set(feed) - {"schema", "repository", "repository_id", "channel", "release_id",
                    "version", "source_commit", "title", "notes"}
            or not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version)
            or type(feed.get("schema")) is not int or feed["schema"] != 2
            or type(feed.get("repository_id")) is not int or feed["repository_id"] != EXPECTED_REPOSITORY_ID
            or feed.get("repository") not in (OWNER + "/" + REVIEW_NAME, OWNER + "/" + PUBLIC_NAME)
            or feed.get("channel") != "stable" or feed.get("version") != version
            or not isinstance(source, str) or not SHA.fullmatch(source)
            or not isinstance(release_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", release_id)
            or not isinstance(title, str) or not title.strip() or len(title) > 120
            or any(ord(c) < 32 or ord(c) == 127 for c in title)
            or not isinstance(notes, str) or not notes.strip() or len(notes.encode("utf-8")) > 65536
            or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in notes)):
        raise PublicationError("The baseline feed does not match this repository, version and approved notes.")
    ancestors = set(commits)
    if source not in ancestors or not only_documentation_changes(
            git(repo, "diff", "--name-only", source, commit).splitlines()):
        raise PublicationError("The baseline feed must identify this runtime; only reviewed packaging changes may follow it.")
    if runtime_entries(repo, source) != runtime_entries(repo, commit):
        raise PublicationError("The game runtime differs from the approved baseline source.")
    return dict(files=files, commit=commit, tree=tree, version=version, feed=feed,
                validation=validation, checked_history_commits=len(commits),
                reviewed_history_commits=len(reviewed), preexisting_upstream_commits=len(upstream))


class Publication:
    def __init__(self, repo, *, client=None, probe=None, package=None):
        self.repo = Path(repo).resolve()
        self.client = client or GitHub()
        self.probe = probe or anonymous_get
        self.package = package or package_module()

    def _video_header(self, film, commit):
        try:
            return self.probe(film, video=True)
        except PublicationError as error:
            # GitHub can serve a public README video through a signed src while
            # its standalone attachment route returns 404. No login is needed.
            if not isinstance(error.__cause__, HTTPError) or error.__cause__.code != 404:
                raise
        rendered = self.probe("https://github.com/" + OWNER + "/" + PUBLIC_NAME +
                              "/blob/" + commit + "/README.md")
        parser = VideoSources()
        try:
            parser.feed(rendered.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise PublicationError("The public README video cannot be verified.") from None
        film_id = film.rsplit("/", 1)[-1]
        for source in parser.sources:
            if public_video_source(source, film_id):
                # Signed URLs are used only in memory, never in diagnostics.
                return self.probe(source, video=True)
        raise PublicationError("The public README has no matching anonymous video source.")

    def _remote_head(self, info):
        branches = self.client.get("repos/" + info["full_name"] + "/branches?per_page=100")
        if (not isinstance(branches, list) or len(branches) != 1
                or not isinstance(branches[0], dict) or branches[0].get("name") != BRANCH
                or not isinstance(branches[0].get("commit"), dict)):
            raise PublicationError("The release fork must have exactly one main branch.")
        return branches[0]["commit"].get("sha")

    def check(self, expected_head=None):
        checked = source_checks(self.repo, self.package, expected_head)
        info = metadata(self.client)
        if not isinstance(info.get("permissions"),dict) or info["permissions"].get("admin") is not True:
            raise PublicationError("Repository administration access is required for publication.")
        if self._remote_head(info) != checked["commit"]:
            raise PublicationError("Remote main differs from the checked local commit.")
        tags = self.client.get("repos/" + info["full_name"] + "/tags?per_page=100")
        if tags != []:
            raise PublicationError("The clean candidate must have no unreviewed tags or tagged private history.")
        runs=self.client.get("repos/"+info["full_name"]+"/actions/runs?per_page=1")
        if not isinstance(runs,dict) or runs.get("total_count") != 0:
            raise PublicationError("Private Actions history needs review before it can become public.")
        if info["name"] != PUBLIC_NAME:
            occupied = self.client.get("repos/" + OWNER + "/" + PUBLIC_NAME, missing_ok=True)
            if occupied is not None and (not isinstance(occupied, dict) or occupied.get("id") != EXPECTED_REPOSITORY_ID):
                raise PublicationError("The intended public repository name is already in use.")
        result = dict(ok=True, mode="check", remote_changes_made=False,
            repository_id=EXPECTED_REPOSITORY_ID, repository=info["full_name"],
            target_repository=OWNER+"/"+PUBLIC_NAME, private=info["private"],
            branch=BRANCH, source_commit=checked["commit"], version=checked["version"],
            checked_history_commits=checked["checked_history_commits"],
            source_files=checked["validation"]["source_files"],
            reviewed_history_commits=checked["reviewed_history_commits"],
            preexisting_upstream_commits=checked["preexisting_upstream_commits"],
            formal_fork=True, upstream=UPSTREAM_REPOSITORY, upstream_repository_id=EXPECTED_UPSTREAM_ID,
            anonymous_checks="available through --activate verification",
            planned_mutations=["rename same public fork to "+PUBLIC_NAME] if info["name"]!=PUBLIC_NAME else [])
        return result, checked

    def _public_checks(self, checked):
        canonical = OWNER + "/" + PUBLIC_NAME
        try:
            public_info = json.loads(self.probe("https://api.github.com/repositories/" + str(EXPECTED_REPOSITORY_ID)))
        except (ValueError, TypeError):
            raise PublicationError("Anonymous repository metadata is unavailable.") from None
        fork_identity(public_info, canonical=True)
        prefix = "https://raw.githubusercontent.com/" + canonical + "/" + checked["commit"] + "/"
        readme = self.probe(prefix+"README.md")
        if readme != checked["files"]["README.md"]:
            raise PublicationError("Anonymous README does not match the approved source.")
        if self.probe(prefix + "updates/stable.json") != checked["files"]["updates/stable.json"]:
            raise PublicationError("The anonymous update feed does not match the approved baseline.")
        names = set()
        for link in self.package.links(readme.decode("utf-8")):
            name = self.package.relative_target("README.md", link)
            if name and PurePosixPath(name).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
                names.add(name)
        for name in sorted(names):
            encoded = quote(name, safe="/")
            if self.probe(prefix+encoded) != checked["files"][name]:
                raise PublicationError("An anonymous README image does not match the approved source.")
        films = set(re.findall(r"https://github\.com/user-attachments/assets/[0-9a-fA-F-]{36}", readme.decode("utf-8")))
        if not films:
            raise PublicationError("The approved README has no native video attachment.")
        for film in films:
            header = self._video_header(film, checked["commit"])
            if len(header)<12 or header[4:8] != b"ftyp":
                raise PublicationError("The native video attachment is not accessible anonymously as video.")
        # Old README/clone links remain useful after the rename while the old name is not reused.
        try:
            old_info = json.loads(self.probe("https://api.github.com/repos/" + OWNER + "/" + REVIEW_NAME))
        except (ValueError, TypeError):
            raise PublicationError("The old repository address does not resolve to verified public metadata.") from None
        fork_identity(old_info, canonical=True)
        self.probe("https://github.com/"+OWNER+"/"+REVIEW_NAME)
        return dict(anonymous_repository_identity_verified=True, anonymous_formal_fork_verified=True, anonymous_readme_verified=True,
                    anonymous_update_feed_verified=True, anonymous_images_verified=len(names),
                    anonymous_native_videos_verified=len(films), old_repository_web_redirect_checked=True)

    def activate(self, expected_head, *, confirmed=False):
        if confirmed is not True or not SHA.fullmatch(expected_head or ""):
            raise PublicationError("Activation needs explicit --confirm-public and the full --expected-head commit.")
        result, checked = self.check(expected_head)
        info = metadata(self.client)
        if self._remote_head(info) != expected_head:
            raise PublicationError("Remote main changed after preflight; publication stopped.")
        result.update(mode="activate",remote_changes_made=False)
        if info["name"] != PUBLIC_NAME:
            try:
                self.client.patch("repos/"+info["full_name"], {"name": PUBLIC_NAME})
            except PublicationError:
                observed=metadata(self.client)
                if observed["name"] != PUBLIC_NAME:
                    raise PublicationError("The rename request failed. Inspect the repository name and visibility before retrying.") from None
            result["remote_changes_made"] = True
            info = metadata(self.client)
            if info["name"] != PUBLIC_NAME:
                raise PublicationError("Rename verification failed; inspect repository visibility before continuing.")
        if self._remote_head(info) != expected_head:
            raise PublicationError("Remote main changed after rename; repository remains in its current visibility.")
        info = metadata(self.client)
        if info["name"] != PUBLIC_NAME or info["private"] or self._remote_head(info) != expected_head:
            raise PublicationError("Public state verification failed; inspect GitHub before announcing the release.")
        try:
            result.update(self._public_checks(checked))
        except PublicationError:
            # Publication can already have exposed source. Do not hide that fact
            # with a silent re-private/rename rollback after an anonymous failure.
            raise PublicationError("The repository is public, but anonymous media/source verification failed. Do not announce success; inspect the public state.") from None
        result.update(ok=True,repository=info["full_name"],private=False,
                      anonymous_checks="passed",planned_mutations=[])
        return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo",type=Path,default=Path(__file__).resolve().parents[1])
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--check",action="store_true")
    mode.add_argument("--dry-run",action="store_true")
    mode.add_argument("--activate",action="store_true")
    parser.add_argument("--confirm-public",action="store_true")
    parser.add_argument("--expected-head")
    args=parser.parse_args(argv)
    try:
        operation=Publication(args.repo)
        if args.activate:
            result=operation.activate(args.expected_head,confirmed=args.confirm_public)
        else:
            if args.confirm_public:
                raise PublicationError("--confirm-public is only valid with --activate.")
            result,_=operation.check(args.expected_head)
        print(json.dumps(result,indent=2))
        return 0
    except (RuntimeError, OSError, UnicodeError, subprocess.SubprocessError) as error:
        message=str(error) if isinstance(error,PublicationError) else "Release check could not complete; verify local tools and network access."
        print(json.dumps(dict(ok=False,error=message),indent=2))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
