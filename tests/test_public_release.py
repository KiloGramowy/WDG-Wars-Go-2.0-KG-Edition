"""Publication state-machine tests: temporary Git and fake GitHub/HTTP only.

No real repository is renamed, made public, or otherwise changed by tests.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


TOOL=Path(__file__).resolve().parents[1]/"tools/public_release.py"
spec=importlib.util.spec_from_file_location("wdg_publication_tests",TOOL)
publication=importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)
package=publication.package_module()
FILM="https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111"


def git(repo,*args):
    return subprocess.check_output(["git","-c","core.autocrlf=false","-c","commit.gpgsign=false",
        "-C",str(repo),*args],stderr=subprocess.PIPE,text=True).strip()


class FakeGitHub:
    def __init__(self,head):
        self.info=dict(id=publication.EXPECTED_REPOSITORY_ID,name=publication.REVIEW_NAME,
            full_name=publication.OWNER+"/"+publication.REVIEW_NAME,private=True,
            owner=dict(id=publication.EXPECTED_OWNER_ID,login=publication.OWNER),
            default_branch="main",archived=False,disabled=False,fork=False,permissions=dict(admin=True))
        self.head=head
        self.patches=[]
        self.requests=[]
        self.occupied=None
        self.extra_branch=False
        self.actions=0
        self.tags=[]
        self.fail_visibility=False
        self.visibility_response_lost=False
        self.rename_response_lost=False

    def get(self,endpoint,missing_ok=False):
        self.requests.append(("GET",endpoint))
        if endpoint=="repositories/"+str(publication.EXPECTED_REPOSITORY_ID):
            return copy.deepcopy(self.info)
        if endpoint.endswith("/branches?per_page=100"):
            result=[dict(name="main",commit=dict(sha=self.head))]
            if self.extra_branch:result.append(dict(name="work",commit=dict(sha=self.head)))
            return result
        if endpoint.endswith("/actions/runs?per_page=1"):
            return dict(total_count=self.actions,workflow_runs=[])
        if endpoint.endswith("/tags?per_page=100"):
            return copy.deepcopy(self.tags)
        if endpoint=="repos/"+publication.OWNER+"/"+publication.PUBLIC_NAME:
            return copy.deepcopy(self.occupied)
        raise AssertionError(endpoint)

    def patch(self,endpoint,data):
        self.requests.append(("PATCH",endpoint))
        self.patches.append(copy.deepcopy(data))
        if "name" in data:
            self.info.update(name=data["name"],full_name=publication.OWNER+"/"+data["name"])
            if self.rename_response_lost:raise publication.PublicationError("simulated lost response")
        if "private" in data:
            if self.fail_visibility:raise publication.PublicationError("simulated rejected public visibility")
            self.info["private"]=data["private"]
            if self.visibility_response_lost:raise publication.PublicationError("simulated lost response")
        return copy.deepcopy(self.info)


class UrlPolicyTests(unittest.TestCase):
    def test_packaging_exception_is_limited_to_the_publication_helper_and_its_tests(self):
        self.assertTrue(publication.only_documentation_changes(
            ["tools/public_release.py", "tests/test_public_release.py", "docs/media/README.md"]))
        for name in ("tools/release_package.py", "tests/test_game_updates.py", "docs/UPDATE_PREVIEW.txt"):
            self.assertFalse(publication.only_documentation_changes([name]), name)

    def test_only_approved_https_hosts_are_allowed(self):
        for url in ("https://github.com/a/b","https://raw.githubusercontent.com/a/b/main/README.md",
                    "https://private-user-images.githubusercontent.com/file",
                    "https://github-production-user-asset-6210df.s3.amazonaws.com/file"):
            self.assertTrue(publication.allowed_https(url),url)
        for url in ("http://github.com/a","https://localhost/x","https://github.com.attacker.invalid/x",
                    "https://user:password@github.com/x","https://github.com:1234/a",
                    "https://github.com:bad/a","file:///private.txt"):
            self.assertFalse(publication.allowed_https(url),url)

    def test_untrusted_cli_error_does_not_expose_raw_output(self):
        result=type("Result",(),dict(returncode=1,stdout="",stderr="private token: sensitive-value (HTTP 403)"))()
        client=publication.GitHub(runner=lambda *a,**k:result)
        with self.assertRaises(publication.PublicationError) as error:client.get("repositories/1")
        self.assertNotIn("sensitive-value",str(error.exception))

    def test_missing_name_requires_an_actual_404(self):
        result=type("Result",(),dict(returncode=1,stdout="",stderr="gh: Not Found (HTTP 404)"))()
        client=publication.GitHub(runner=lambda *a,**k:result)
        self.assertIsNone(client.get("repos/owner/new",missing_ok=True))
        result.stderr="authentication required (HTTP 401)"
        with self.assertRaises(publication.PublicationError):client.get("repos/owner/new",missing_ok=True)

    def test_runner_transport_errors_are_safe_and_not_treated_as_a_missing_name(self):
        for failure in (subprocess.TimeoutExpired(["gh","sensitive-value"],35,
                            output="private output: sensitive-value",stderr="private token: sensitive-value"),
                        OSError("private path: sensitive-value")):
            with self.subTest(failure=type(failure).__name__):
                def failed_runner(*args,**kwargs):
                    raise failure
                client=publication.GitHub(runner=failed_runner)
                with self.assertRaisesRegex(publication.PublicationError,"did not complete") as error:
                    client.get("repos/owner/new",missing_ok=True)
                self.assertNotIn("sensitive-value",str(error.exception))


class VideoFallbackTests(unittest.TestCase):
    def setUp(self):
        self.commit = "a" * 40
        self.rendered = ("https://github.com/" + publication.OWNER + "/" + publication.PUBLIC_NAME +
                         "/blob/" + self.commit + "/README.md")
        self.source = ("https://private-user-images.githubusercontent.com/" +
                       str(publication.EXPECTED_OWNER_ID) + "/12345-" + FILM.rsplit("/", 1)[-1] +
                       ".mp4?jwt=synthetic-query&download=1")
        self.page = ('<video controls><source src="' + self.source.replace("&", "&amp;") + '"></video>').encode()
        self.calls = []
        self.operation = publication.Publication(TOOL.parent.parent, probe=self.probe)

    def probe(self, url, video=False):
        self.calls.append((url, video))
        if url == FILM:
            raise publication.PublicationError("Attachment unavailable.") from HTTPError(
                FILM, 404, "Not Found", None, None)
        if url == self.rendered:
            self.assertFalse(video)
            return self.page
        if url == self.source:
            self.assertTrue(video)
            return b"\x00\x00\x00\x18ftypmp42"
        raise AssertionError("Unexpected anonymous request")

    def test_public_rendered_readme_resolves_video_after_standalone_attachment_404(self):
        self.assertEqual(self.operation._video_header(FILM, self.commit)[4:8], b"ftyp")
        self.assertEqual(self.calls, [(FILM, True), (self.rendered, False), (self.source, True)])

    def test_foreign_uuid_owner_and_unsafe_sources_are_not_requested(self):
        for source in (self.source.replace("11111111-1111", "22222222-2222"),
                       self.source.replace("/315514197/", "/999/"),
                       self.source.replace("https://", "http://"),
                       self.source.replace("private-user-images.githubusercontent.com", "attacker.invalid"),
                       self.source.replace("https://", "https://user:password@")):
            with self.subTest(source=source.partition("?")[0]):
                self.calls.clear()
                self.page = ('<video src="' + source + '"></video>').encode()
                with self.assertRaises(publication.PublicationError) as error:
                    self.operation._video_header(FILM, self.commit)
                self.assertNotIn("synthetic-query", str(error.exception))
                self.assertEqual(self.calls, [(FILM, True), (self.rendered, False)])

    def test_login_page_or_unavailable_public_html_does_not_pass(self):
        self.page = b"<html>Sign in to view this private resource.</html>"
        with self.assertRaises(publication.PublicationError):
            self.operation._video_header(FILM, self.commit)
        original = self.operation.probe
        def private_page(url, video=False):
            if url == self.rendered:
                raise publication.PublicationError("Not available.") from HTTPError(url, 404, "Not Found", None, None)
            return original(url, video)
        self.operation.probe = private_page
        with self.assertRaises(publication.PublicationError):
            self.operation._video_header(FILM, self.commit)

    def test_non_404_transport_failure_does_not_trigger_fallback(self):
        def failed(url, video=False):
            self.calls.append((url, video))
            raise publication.PublicationError("Network failed.") from HTTPError(url, 403, "Forbidden", None, None)
        self.operation.probe = failed
        with self.assertRaises(publication.PublicationError):
            self.operation._video_header(FILM, self.commit)
        self.assertEqual(self.calls, [(FILM, True)])


@unittest.skipUnless(shutil.which("git"),"Git required for isolated release checkout tests")
class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.seed=Path(cls.temp.name)/"seed";cls.seed.mkdir()
        git(cls.seed,"init","-b","main")
        git(cls.seed,"config","user.name","Release Tests")
        git(cls.seed,"config","user.email","tests@example.invalid")
        files={name:b"reviewed source\n" for name in package.REQUIRED_FILES}
        files[".gitignore"]=b"__pycache__/\n*.pyc\n"
        files["README.md"]=("# Approved game description\n\n![Current UI](docs/media/example.png)\n\n"+FILM+"\n").encode()
        files["watchdogs/__init__.py"]=b'__version__ = "0.9.16"\n'
        files["watchdogs/game_updates.py"]=(f'REPOSITORY_ID = {publication.EXPECTED_REPOSITORY_ID}\n'
            'REPOSITORY = "KiloGramowy/WatchDogsGo-1"\nBRANCH = "main"\nCHANNEL = "stable"\nFEED_PATH = "updates/stable.json"\n').encode()
        image=b"\x89PNG\r\n\x1a\n"+b"\x00\x00\x00\rIHDR"+struct.pack(">II",32,24)
        files["docs/media/example.png"]=image
        files["docs/media/media-manifest.json"]=json.dumps(dict(files=[dict(path="example.png",
            bytes=len(image),sha256=hashlib.sha256(image).hexdigest(),dimensions=[32,24])])).encode()
        for name,data in files.items():
            target=cls.seed/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        git(cls.seed,"add",".");git(cls.seed,"commit","-qm","Approved clean root")
        cls.root=git(cls.seed,"rev-parse","HEAD")
        target=cls.seed/"updates/stable.json";target.parent.mkdir()
        target.write_text(json.dumps(dict(schema=2,repository_id=publication.EXPECTED_REPOSITORY_ID,
            repository="KiloGramowy/WatchDogsGo-1",channel="stable",release_id="v0.9.16",
            version="0.9.16",source_commit=cls.root,title="Approved baseline",notes="Full approved notes.")),encoding="utf-8")
        git(cls.seed,"add",".");git(cls.seed,"commit","-qm","Approved baseline feed")

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.repo=Path(self.folder.name)/"candidate"
        git(Path(self.folder.name),"clone","--quiet",str(self.seed),str(self.repo))
        git(self.repo,"config","user.name","Release Tests")
        git(self.repo,"config","user.email","tests@example.invalid")
        self.head=git(self.repo,"rev-parse","HEAD")
        self.github=FakeGitHub(self.head)
        self.root_patch=patch.object(publication,"CLEAN_ROOT",self.root)
        self.root_patch.start();self.addCleanup(self.root_patch.stop)
        self.probed=[]
        self.operation=publication.Publication(self.repo,client=self.github,probe=self.probe,package=package)

    def probe(self,url,video=False):
        self.probed.append((url,video))
        if video:return b"\x00\x00\x00\x18ftypmp42"
        if url.startswith("https://api.github.com/"):
            return json.dumps(self.github.info).encode()
        if url.startswith("https://raw.githubusercontent.com/"):
            suffix=url.split(self.head+"/",1)[1]
            return (self.repo/suffix).read_bytes()
        return b"repository page"

    def commit(self,message="Reviewed preparation"):
        git(self.repo,"add",".");git(self.repo,"commit","-qm",message)
        self.head=git(self.repo,"rev-parse","HEAD");self.github.head=self.head

    def test_default_check_is_read_only_and_leaves_anonymous_checks_pending(self):
        result,_=self.operation.check()
        self.assertTrue(result["ok"])
        self.assertTrue(result["private"])
        self.assertFalse(result["remote_changes_made"])
        self.assertEqual(len(result["planned_mutations"]),2)
        self.assertEqual(result["anonymous_checks"],"pending visibility change")
        self.assertEqual(self.github.patches,[])
        self.assertEqual(self.probed,[])

    def test_activation_requires_explicit_confirmation_and_full_expected_head(self):
        for head,confirmed in ((None,True),(self.head,False),("abc",True)):
            with self.assertRaises(publication.PublicationError):self.operation.activate(head,confirmed=confirmed)
        self.assertEqual(self.github.patches,[])

    def test_activation_only_renames_and_changes_visibility_then_checks_media(self):
        result=self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.patches,[dict(name=publication.PUBLIC_NAME),dict(private=False)])
        self.assertFalse(result["private"])
        self.assertEqual(result["anonymous_images_verified"],1)
        self.assertEqual(result["anonymous_native_videos_verified"],1)
        self.assertTrue(result["anonymous_readme_verified"])
        self.assertTrue(result["anonymous_repository_identity_verified"])
        self.assertTrue(result["anonymous_update_feed_verified"])
        self.assertTrue(any(video for _,video in self.probed))

    def test_repeated_activation_after_publication_is_verification_only(self):
        self.operation.activate(self.head,confirmed=True);self.github.patches.clear();self.probed.clear()
        result=self.operation.activate(self.head,confirmed=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self.github.patches,[])
        self.assertFalse(result["remote_changes_made"])
        self.assertTrue(self.probed)

    def test_wrong_remote_identity_owner_or_admin_rights_blocks_writes(self):
        for field,value in (("id",1),("fork",True),("default_branch","work"),
                            ("permissions",dict(admin=False)),("name",None),("owner",None)):
            before=copy.deepcopy(self.github.info)
            self.github.info[field]=value
            with self.subTest(field=field),self.assertRaises(publication.PublicationError):self.operation.activate(self.head,confirmed=True)
            self.github.info=before
        self.github.info["owner"]["id"]=1
        with self.assertRaises(publication.PublicationError):self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.patches,[])

    def test_tags_cannot_expose_an_unreviewed_private_ancestor(self):
        self.github.tags=[dict(name="old-private-tag",commit=dict(sha="c"*40))]
        with self.assertRaisesRegex(publication.PublicationError,"tags"):
            self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.patches,[])

    def test_invalid_baseline_notes_or_shape_block_activation(self):
        path=self.repo/"updates/stable.json"
        approved=path.read_text()
        for change in ({"title":""},{"title":"bad\rtitle"},{"notes":"bad\x00notes"},
                       {"repository":"Unrelated/project"},{"unknown":True}):
            with self.subTest(change=change):
                feed=json.loads(approved);feed.update(change)
                path.write_text(json.dumps(feed));self.commit()
                with self.assertRaises(publication.PublicationError):
                    self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.patches,[])

    def test_obsolete_private_only_readme_claim_blocks_prepared_public_release(self):
        path=self.repo/"README.md";approved=path.read_text()
        for phrase in ("The repository remains private.","Private preview; no release published yet.",
                       "No release feed has been published yet."):
            with self.subTest(phrase=phrase):
                path.write_text(approved+"\n"+phrase);self.commit()
                with self.assertRaisesRegex(publication.PublicationError,"obsolete"):
                    self.operation.check()
        self.assertEqual(self.github.patches,[])

    def test_occupied_name_extra_branch_and_private_actions_history_block_writes(self):
        for field,value in (("occupied",dict(id=99)),("extra_branch",True),("actions",1)):
            with self.subTest(field=field):
                setattr(self.github,field,value)
                with self.assertRaises(publication.PublicationError):self.operation.activate(self.head,confirmed=True)
                setattr(self.github,field,None if field=="occupied" else False if field=="extra_branch" else 0)
        self.assertEqual(self.github.patches,[])

    def test_dirty_checkout_and_unexpected_head_block_before_mutation(self):
        with self.assertRaises(publication.PublicationError):self.operation.activate("a"*40,confirmed=True)
        (self.repo/"unreviewed.txt").write_text("uncommitted")
        with self.assertRaises(publication.PublicationError):self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.patches,[])

    def test_remote_main_changed_after_preflight_stops_before_rename(self):
        original=self.operation.check
        def changed(*args):
            result=original(*args);self.github.head="b"*40;return result
        with patch.object(self.operation,"check",side_effect=changed),self.assertRaises(publication.PublicationError):
            self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.patches,[])

    def test_missing_or_foreign_baseline_feed_blocks_activation(self):
        path=self.repo/"updates/stable.json";feed=json.loads(path.read_text())
        feed["repository_id"]=99;path.write_text(json.dumps(feed));self.commit()
        with self.assertRaises(publication.PublicationError):self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.patches,[])

    def test_runtime_change_after_baseline_requires_a_new_approved_feed_source(self):
        (self.repo/"watchdogs/app.py").write_text("NEW_RUNTIME=True\n");self.commit()
        with self.assertRaises(publication.PublicationError):self.operation.check()
        self.assertEqual(self.github.patches,[])

    def test_publication_tool_and_its_regression_can_follow_unchanged_runtime(self):
        for name in ("tools/public_release.py", "tests/test_public_release.py"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Reviewed packaging-only correction.\n")
        self.commit()
        result, _ = self.operation.check()
        self.assertTrue(result["ok"])
        self.assertEqual(publication.runtime_entries(self.repo, self.root),
                         publication.runtime_entries(self.repo, self.head))

    def test_packaging_path_cannot_hide_a_runtime_file_change(self):
        (self.repo / "CHANGELOG.md").write_text("Changed runtime release file.\n")
        self.commit()
        with self.assertRaisesRegex(publication.PublicationError, "runtime differs"):
            self.operation.check()

    def test_runtime_equality_includes_git_executable_mode(self):
        (self.repo / "LICENSE").chmod(0o755)
        git(self.repo, "update-index", "--chmod=+x", "LICENSE")
        git(self.repo, "commit", "-qm", "Changed runtime mode")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.github.head = self.head
        with self.assertRaisesRegex(publication.PublicationError, "runtime differs"):
            self.operation.check()

    def test_installed_release_notes_after_baseline_require_an_updated_source(self):
        (self.repo/"docs/UPDATE_PREVIEW.txt").write_text("Changed in-game offline release notes.\n")
        self.commit()
        with self.assertRaises(publication.PublicationError):self.operation.check()
        self.assertEqual(self.github.patches,[])

    def test_removed_secret_in_older_reachable_snapshot_is_still_blocking(self):
        path=self.repo/"docs/private.md";path.write_text("ghp_"+"A"*40);self.commit("Private fixture")
        path.unlink();self.commit("Remove fixture")
        with self.assertRaises(publication.PublicationError):self.operation.check()
        self.assertEqual(self.github.patches,[])

    def test_sensitive_commit_message_is_blocking_even_if_files_are_clean(self):
        path=self.repo/"docs/note.md";path.write_text("Reviewed notes")
        self.commit("Host "+"192."+"168.1.133")
        with self.assertRaises(publication.PublicationError):self.operation.check()

    def test_failed_visibility_leaves_renamed_repository_private(self):
        self.github.fail_visibility=True
        with self.assertRaisesRegex(publication.PublicationError,"remains private"):
            self.operation.activate(self.head,confirmed=True)
        self.assertEqual(self.github.info["name"],publication.PUBLIC_NAME)
        self.assertTrue(self.github.info["private"])
        self.assertEqual(self.probed,[])

    def test_lost_success_responses_are_resolved_by_stable_id_reads(self):
        self.github.visibility_response_lost=self.github.rename_response_lost=True
        result=self.operation.activate(self.head,confirmed=True)
        self.assertTrue(result["ok"])
        self.assertFalse(self.github.info["private"])

    def test_real_request_reconciles_timeout_and_oserror_after_successful_patches(self):
        for failure_type in (subprocess.TimeoutExpired,OSError):
            with self.subTest(failure=failure_type.__name__):
                self.github=FakeGitHub(self.head)
                self.probed.clear()

                def runner(command,**kwargs):
                    method_index=command.index("--method")
                    method,endpoint=command[method_index+1:method_index+3]
                    if method=="PATCH":
                        self.github.patch(endpoint,json.loads(kwargs["input"]))
                        if failure_type is subprocess.TimeoutExpired:
                            raise subprocess.TimeoutExpired(command,35,stderr="sensitive-value")
                        raise OSError("sensitive-value")
                    value=self.github.get(endpoint,missing_ok=True)
                    if value is None:
                        return subprocess.CompletedProcess(command,1,stdout="",stderr="gh: Not Found (HTTP 404)")
                    return subprocess.CompletedProcess(command,0,stdout=json.dumps(value),stderr="")

                self.operation.client=publication.GitHub(runner=runner)
                result=self.operation.activate(self.head,confirmed=True)
                self.assertTrue(result["ok"])
                self.assertFalse(self.github.info["private"])
                self.assertEqual(self.github.patches,[dict(name=publication.PUBLIC_NAME),dict(private=False)])
                for index,(method,_) in enumerate(self.github.requests):
                    if method=="PATCH":
                        self.assertEqual(self.github.requests[index+1],
                            ("GET","repositories/"+str(publication.EXPECTED_REPOSITORY_ID)))

    def test_anonymous_failure_is_not_reported_as_success_or_hidden_with_rollback(self):
        self.operation.probe=lambda *a,**k:b"login page"
        with self.assertRaisesRegex(publication.PublicationError,"is public, but"):
            self.operation.activate(self.head,confirmed=True)
        self.assertFalse(self.github.info["private"])
        self.assertEqual(self.github.patches,[dict(name=publication.PUBLIC_NAME),dict(private=False)])

    def test_native_attachment_must_return_video_not_an_html_login_page(self):
        original=self.operation.probe
        self.operation.probe=lambda url,video=False:b"login page" if video else original(url)
        with self.assertRaisesRegex(publication.PublicationError,"is public, but"):
            self.operation.activate(self.head,confirmed=True)

    def test_anonymous_feed_must_be_the_approved_baseline(self):
        original=self.operation.probe
        self.operation.probe=lambda url,video=False:b"{}" if url.endswith("updates/stable.json") else original(url,video)
        with self.assertRaisesRegex(publication.PublicationError,"is public, but"):
            self.operation.activate(self.head,confirmed=True)

    def test_old_name_must_resolve_to_the_same_repository_not_just_an_http_200(self):
        original=self.operation.probe
        old_api="https://api.github.com/repos/"+publication.OWNER+"/"+publication.REVIEW_NAME
        wrong=dict(id=99,full_name=publication.OWNER+"/"+publication.REVIEW_NAME,
                   private=False,owner=dict(id=publication.EXPECTED_OWNER_ID))
        self.operation.probe=lambda url,video=False:json.dumps(wrong).encode() if url==old_api else original(url,video)
        with self.assertRaisesRegex(publication.PublicationError,"is public, but"):
            self.operation.activate(self.head,confirmed=True)
        self.assertFalse(self.github.info["private"])
        self.assertEqual(self.github.patches,[dict(name=publication.PUBLIC_NAME),dict(private=False)])


if __name__=="__main__":unittest.main()
