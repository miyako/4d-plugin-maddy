import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "release", Path(__file__).resolve().parents[1] / "scripts" / "release.py",
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

TAG = "v1.2.3"
SHA = "a" * 40
OTHER = "b" * 40
REPO = "owner/repo"
BASE = f"repos/{REPO}"
UPLOAD = f"https://uploads.github.com/repos/{REPO}/releases/123/assets"


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def ref(tag=TAG, sha=SHA, kind="commit"):
    return {"ref": f"refs/tags/{tag}", "object": {"sha": sha, "type": kind}}


def existing(tag=TAG, draft=False, **changes):
    result = {"id": 9, "tag_name": tag, "draft": draft, "prerelease": False}
    result.update(changes)
    return result


def created(**changes):
    result = {
        "id": 123, "tag_name": TAG, "name": TAG, "target_commitish": SHA,
        "draft": False, "prerelease": False, "upload_url": UPLOAD + "{?name,label}",
    }
    result.update(changes)
    return result


class FakeAPI:
    repository = REPO

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def api(self, method, endpoint, payload=None, asset=None):
        self.calls.append((method, endpoint, payload, asset))
        if not self.responses:
            raise AssertionError(f"unexpected call: {self.calls[-1]}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CheckTests(unittest.TestCase):
    def test_strict_tags(self):
        for tag in ("1.2.3", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2", "v1.2.3-rc.1",
                    "v1.2.3+build", "v1.2.3\n", "v1.2.3/extra", None):
            with self.subTest(tag=tag), self.assertRaises(release.ReleaseError):
                release.check_available(FakeAPI([]), tag)
        for tag in ("v0.0.0", "v10.20.300"):
            release.check_available(FakeAPI([[], []]), tag)

    def test_all_pages_and_drafts_and_both_checks(self):
        api = FakeAPI([[existing("v0.0.1")], [existing(draft=True, id=10)], [], [ref()]])
        with self.assertRaisesRegex(release.ReleaseError, "release .*tag"):
            release.check_available(api, TAG)
        self.assertEqual([call[1] for call in api.calls], [
            f"{BASE}/releases?per_page=100&page=1",
            f"{BASE}/releases?per_page=100&page=2",
            f"{BASE}/releases?per_page=100&page=3",
            f"{BASE}/git/matching-refs/tags/{TAG}",
        ])
        self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_prefix_is_not_exact_tag(self):
        release.check_available(FakeAPI([[], [ref(TAG + "0"), ref(TAG + "-rc1")]]), TAG)

    def test_duplicate_release_ids_within_and_across_pages_fail_closed(self):
        first = existing("v0.0.1")
        same_id = existing("v0.0.2")
        for pages in ([[first, same_id]], [[first], [same_id]]):
            api = FakeAPI(pages)
            with self.subTest(pages=pages), self.assertRaisesRegex(
                    release.ReleaseError, "duplicate release ID"):
                release.check_available(api, TAG)
            self.assertEqual(len(api.calls), len(pages))

    def test_duplicate_matching_ref_names_fail_closed(self):
        for tag in (TAG, TAG + "0"):
            api = FakeAPI([[], [ref(tag), ref(tag, sha=OTHER)]])
            with self.subTest(tag=tag), self.assertRaisesRegex(
                    release.ReleaseError, "duplicate ref name"):
                release.check_available(api, TAG)
            self.assertEqual(len(api.calls), 2)

    def test_tag_only_and_release_only(self):
        for replies in ([[], [ref()]], [[existing()], [], []]):
            with self.subTest(replies=replies), self.assertRaises(release.ReleaseError):
                release.check_available(FakeAPI(replies), TAG)

    def test_api_errors_on_every_read(self):
        for replies in ([release.ReleaseError("404")],
                        [[existing("v0.0.0")], release.ReleaseError("network")],
                        [[], release.ReleaseError("auth")]):
            with self.subTest(replies=replies), self.assertRaises(release.ReleaseError):
                release.check_available(FakeAPI(replies), TAG)

    def test_invalid_release_pages_and_entries(self):
        bad_entries = [None, [], {}, existing(id=True), existing(id=0),
                       existing(tag_name=None), existing(tag_name=""),
                       existing(draft=0), existing(prerelease="false")]
        for bad in (None, {}, "[]", 0):
            with self.subTest(page=bad), self.assertRaises(release.ReleaseError):
                release.check_available(FakeAPI([bad]), TAG)
        for entry in bad_entries:
            with self.subTest(entry=entry), self.assertRaises(release.ReleaseError):
                release.check_available(FakeAPI([[existing("v0.0.0")], [entry]]), TAG)

    def test_invalid_matching_ref_responses(self):
        bad = [None, {}, "", [None], [{}], [{"ref": "refs/heads/main", "object": ref()["object"]}],
               [ref("")], [ref("v9.0.0")], [ref(sha="short")], [ref(kind="blob")],
               [{"ref": "refs/tags/" + TAG, "object": None}]]
        for response in bad:
            with self.subTest(response=response), self.assertRaises(release.ReleaseError):
                release.check_available(FakeAPI([[], response]), TAG)


class PublishTests(unittest.TestCase):
    def setUp(self):
        # Keep test artifacts inside the checkout, never in system temporary directories.
        self.folder = (Path(__file__).parent / (".release-test-" + uuid.uuid4().hex)).resolve()
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.folder)
        self.zip = Path("maddy-1.2.3.zip")
        self.dmg = Path("maddy-1.2.3.dmg")
        self.zip.write_bytes(b"zip")
        self.dmg.write_bytes(b"dmg")
        self.assets = [self.zip, self.dmg]

    def replies(self):
        return [[], [], ref(), ref(), created(),
                {"id": 1, "name": self.zip.name, "size": 3, "state": "uploaded"},
                {"id": 2, "name": self.dmg.name, "size": 3, "state": "uploaded"}]

    def test_success_selected_sha_flags_new_id_separate_uploads(self):
        api = FakeAPI(self.replies())
        self.assertEqual(release.publish(api, TAG, SHA, self.assets),
                         (123, [self.zip.name, self.dmg.name]))
        self.assertEqual([c[0] for c in api.calls],
                         ["GET", "GET", "POST", "GET", "POST", "POST", "POST"])
        self.assertEqual(api.calls[2][1:3], (f"{BASE}/git/refs",
                                           {"ref": "refs/tags/" + TAG, "sha": SHA}))
        self.assertEqual(api.calls[3][1], f"{BASE}/git/ref/tags/{TAG}")
        body = api.calls[4][2]
        self.assertEqual(body, {
            "target_commitish": SHA, "tag_name": TAG, "name": TAG,
            "draft": False, "prerelease": False,
            "generate_release_notes": False, "body": "",
        })
        self.assertEqual(api.calls[5][1], UPLOAD + "?name=maddy-1.2.3.zip")
        self.assertEqual(api.calls[6][1], UPLOAD + "?name=maddy-1.2.3.dmg")
        self.assertEqual([c[3] for c in api.calls[5:]], self.assets)

    def test_local_preflight_makes_no_remote_calls(self):
        for state in ("empty", "missing", "symlink", "directory"):
            self.zip.unlink(missing_ok=True)
            if state == "empty":
                self.zip.touch()
            elif state == "symlink":
                target = Path("symlink-target")
                target.write_bytes(b"zip")
                self.zip.symlink_to(target)
            elif state == "directory":
                self.zip.mkdir()
            api = FakeAPI([])
            with self.subTest(state=state), self.assertRaises(release.ReleaseError):
                release.publish(api, TAG, SHA, self.assets)
            self.assertEqual(api.calls, [])

    def test_exact_selected_asset_names_required_before_remote_calls(self):
        wrong_paths = []
        for name in ("maddy.zip", "maddy-9.9.9.zip", "maddy-1.2.3.ZIP",
                     "maddy-v1.2.3.zip", "maddy-1.2.3.zip.backup"):
            path = Path(name)
            path.write_bytes(b"nonempty but wrong release asset")
            wrong_paths.append(path)
        invalid_assets = [[path, self.dmg] for path in wrong_paths]
        invalid_assets += [[self.zip], [self.dmg], [*self.assets, wrong_paths[0]]]
        invalid_assets += [
            list(reversed(self.assets)),
            [self.zip, self.zip],
            [self.zip.resolve(), self.dmg.resolve()],
            ["./" + str(self.zip), str(self.dmg)],
            ["nested/" + str(self.zip), str(self.dmg)],
        ]
        for assets in invalid_assets:
            api = FakeAPI([])
            with self.subTest(assets=assets), self.assertRaises(release.ReleaseError):
                release.publish(api, TAG, SHA, assets)
            self.assertEqual(api.calls, [])

    def test_invalid_sha_rejected_before_remote_calls(self):
        for sha in ("a" * 39, "a" * 41, "A" * 40, "g" * 40, SHA + "\n", None):
            api = FakeAPI([])
            with self.subTest(sha=sha), self.assertRaises(release.ReleaseError):
                release.publish(api, TAG, sha, self.assets)
            self.assertEqual(api.calls, [])

    def test_final_check_catches_race_after_early_check(self):
        api = FakeAPI([[], [], [existing(draft=True)], [], [ref()]])
        release.check_available(api, TAG)
        with self.assertRaises(release.ReleaseError):
            release.publish(api, TAG, SHA, self.assets)
        self.assertTrue(all(c[0] == "GET" for c in api.calls))

    def test_final_check_fails_closed_on_later_pages_before_any_write(self):
        for bad in (None, {}, [None], release.ReleaseError("later page unavailable")):
            api = FakeAPI([[existing("v0.0.0")], bad])
            with self.subTest(bad=bad), self.assertRaises(release.ReleaseError):
                release.publish(api, TAG, SHA, self.assets)
            self.assertTrue(all(c[0] == "GET" for c in api.calls))

    def test_new_release_id_cannot_reuse_preexisting_id(self):
        api = FakeAPI([[existing("v0.0.0", id=123)], [], [], ref(), ref(), created()])
        with self.assertRaisesRegex(release.ReleaseError, "reused an existing release ID"):
            release.publish(api, TAG, SHA, self.assets)
        self.assertEqual(len(api.calls), 6)

    def test_tag_creation_race_no_release_or_retries(self):
        api = FakeAPI([[], [], release.ReleaseError("422 already exists")])
        with self.assertRaisesRegex(release.ReleaseError, "creation may have succeeded"):
            release.publish(api, TAG, SHA, self.assets)
        self.assertEqual(len(api.calls), 3)

    def test_tag_post_and_remote_verification_mismatches(self):
        for bad in (None, {}, ref(sha=OTHER), ref("v9.9.9"), ref(kind="tag"),
                    release.ReleaseError("verification network failure")):
            for index in (2, 3):
                replies = self.replies()[:index] + [bad]
                api = FakeAPI(replies)
                with self.subTest(bad=bad, index=index), self.assertRaisesRegex(
                        release.ReleaseError, "Manual recovery required"):
                    release.publish(api, TAG, SHA, self.assets)
                self.assertEqual(len(api.calls), index + 1)

    def test_release_post_failure_retains_tag(self):
        api = FakeAPI([[], [], ref(), ref(), release.ReleaseError("422 conflict")])
        with self.assertRaisesRegex(release.ReleaseError,
                                    "tag v1.2.3 retained; release creation may have succeeded"):
            release.publish(api, TAG, SHA, self.assets)
        self.assertEqual(len(api.calls), 5)

    def test_malformed_new_release_never_uploads_or_reuses(self):
        bad = [None, [], {}, created(id=True), created(id=0), created(id="123"),
               created(tag_name="v9.0.0"), created(name="other"),
               created(target_commitish=OTHER), created(draft=True),
               created(draft=0), created(prerelease=True), created(prerelease=0),
               created(upload_url="https://evil.example/upload"),
               created(upload_url=UPLOAD.replace("/123/", "/456/") + "{?name,label}")]
        for result in bad:
            api = FakeAPI([[], [], ref(), ref(), result])
            with self.subTest(result=result), self.assertRaisesRegex(
                    release.ReleaseError, "Manual recovery required"):
                release.publish(api, TAG, SHA, self.assets)
            self.assertEqual(len(api.calls), 5)

    def test_later_upload_failure_retains_new_release_and_first_asset(self):
        api = FakeAPI(self.replies()[:-1] + [release.ReleaseError("upload failed")])
        with self.assertRaisesRegex(
                release.ReleaseError,
                "release ID 123 retained; uploaded assets retained: maddy-1.2.3.zip; "
                "upload of maddy-1.2.3.dmg may have succeeded"):
            release.publish(api, TAG, SHA, self.assets)
        self.assertEqual(len(api.calls), 7)

    def test_malformed_upload_responses_stop_without_retry(self):
        for bad in (None, {}, {"id": True}, {"id": 1, "name": self.zip.name, "size": True},
                    {"id": 1, "name": "wrong", "size": 3, "state": "uploaded"},
                    {"id": 1, "name": self.zip.name, "size": 0, "state": "uploaded"},
                    {"id": 1, "name": self.zip.name, "size": 3, "state": "new"}):
            api = FakeAPI(self.replies()[:5] + [bad])
            with self.subTest(bad=bad), self.assertRaisesRegex(
                    release.ReleaseError, "release ID 123 retained"):
                release.publish(api, TAG, SHA, self.assets)
            self.assertEqual(len(api.calls), 6)


class TransportTests(unittest.TestCase):
    def test_authenticated_gh_commands_and_json_input(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"id": 1}', "")
            api = release.GitHub(REPO)
            self.assertEqual(api.api("POST", BASE + "/releases", {"draft": False}), {"id": 1})
            args, kwargs = run.call_args
            self.assertEqual(args[0][:5], ["gh", "api", "--method", "POST", BASE + "/releases"])
            self.assertIn("Accept: application/vnd.github+json", args[0])
            self.assertEqual(json.loads(kwargs["input"]), {"draft": False})
            self.assertTrue(kwargs["capture_output"])
            api.api("POST", UPLOAD + "?name=a.zip", asset=Path("a.zip"))
            args, kwargs = run.call_args
            self.assertIn("Content-Type: application/octet-stream", args[0])
            self.assertEqual(args[0][-2:], ["--input", "a.zip"])
            self.assertIsNone(kwargs["input"])

    def test_transport_fails_closed(self):
        for code, output, error in ((1, "[]", "404"), (1, "[]", "401"),
                                    (1, "[]", "network"), (0, "not json", ""),
                                    (0, '{"id": 1, "id": 2}', ""),
                                    (0, '[NaN]', ""), (0, '[Infinity]', "")):
            with self.subTest(error=error, output=output), mock.patch.object(
                    subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], code, output, error)), self.assertRaises(release.ReleaseError):
                release.GitHub(REPO).api("GET", BASE + "/releases")
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("gh")), \
                self.assertRaises(release.ReleaseError):
            release.GitHub(REPO).api("GET", BASE)

    def test_cli_with_fake_gh_subprocess_rejects_late_failure(self):
        results = [
            subprocess.CompletedProcess([], 0, json.dumps([existing("v0.0.0")]), ""),
            subprocess.CompletedProcess([], 1, "", "HTTP 404"),
        ]
        with mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": REPO, "GH_TOKEN": "test-token", "GITHUB_SHA": SHA,
        }), mock.patch.object(release, "validate_context"), \
                mock.patch.object(subprocess, "run", side_effect=results) as run, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(release.main(["check", "--tag", TAG]), 1)
            self.assertIn("404", stderr.getvalue())
            self.assertEqual(run.call_count, 2)
            self.assertIn("page=2", run.call_args.args[0][4])

    def test_publish_cli_with_fake_gh_subprocess_retains_created_tag(self):
        folder = (Path(__file__).parent / (".release-test-" + uuid.uuid4().hex)).resolve()
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        path = folder / "maddy-1.2.3.zip"
        path.write_bytes(b"zip")
        dmg = folder / "maddy-1.2.3.dmg"
        dmg.write_bytes(b"dmg")
        replies = [[], [], ref(), ref()]
        results = [subprocess.CompletedProcess([], 0, json.dumps(value), "") for value in replies]
        results.append(subprocess.CompletedProcess([], 1, "", "HTTP 422"))
        with working_directory(folder), mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": REPO, "GH_TOKEN": "test-token", "GITHUB_SHA": SHA,
        }), mock.patch.object(release, "validate_context"), \
                mock.patch.object(subprocess, "run", side_effect=results) as run, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(release.main([
                "publish", "--tag", TAG, "--sha", SHA, "--asset", path.name,
                "--asset", dmg.name,
            ]), 1)
            self.assertIn("tag v1.2.3 retained", stderr.getvalue())
            self.assertIn("Manual recovery required", stderr.getvalue())
            self.assertEqual(run.call_count, 5)

    def test_repository_validation(self):
        for repo in (None, "", "owner", "../repo", "owner/..", "a/b/c", "a/b?x=1", "a/b\n"):
            with self.subTest(repo=repo), self.assertRaises(release.ReleaseError):
                release.GitHub(repo)

    def test_cli_requires_explicit_nonempty_auth_without_using_local_credentials(self):
        for tokens in ({}, {"GH_TOKEN": ""}, {"GITHUB_TOKEN": ""},
                       {"GH_TOKEN": " \n", "GITHUB_TOKEN": "\t"}):
            for command in (["check", "--tag", TAG],
                            ["publish", "--tag", TAG, "--sha", SHA, "--asset", "unused.zip"]):
                environment = {"GITHUB_REPOSITORY": REPO, **tokens}
                with self.subTest(tokens=tokens, command=command), \
                        mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(subprocess, "run") as run, \
                        contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(release.main(command), 1)
                    self.assertIn("GH_TOKEN or GITHUB_TOKEN is required", stderr.getvalue())
                    run.assert_not_called()

    def test_cli_accepts_either_explicit_token(self):
        for tokens in ({"GH_TOKEN": "test-gh-token"}, {"GITHUB_TOKEN": "test-github-token"},
                       {"GH_TOKEN": "", "GITHUB_TOKEN": "test-github-token"}):
            with self.subTest(tokens=tokens), \
                    mock.patch.dict(os.environ, {
                        "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": SHA, **tokens,
                    }, clear=True), mock.patch.object(release, "validate_context"), \
                    mock.patch.object(release.GitHub, "api", return_value=[]) as api, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(release.main(["check", "--tag", TAG]), 0)
                self.assertEqual(api.call_count, 2)

    def test_cli_stderr_nonzero_and_success(self):
        with mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": REPO, "GH_TOKEN": "test-token", "GITHUB_SHA": SHA,
        }), mock.patch.object(release, "validate_context"), \
                mock.patch.object(release.GitHub, "api", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(release.main(["check", "--tag", TAG]), 0)
            self.assertIn("available", stdout.getvalue())
        with mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": REPO, "GH_TOKEN": "test-token", "GITHUB_SHA": SHA,
        }), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(release.main(["check", "--tag", "bad"]), 1)
            self.assertIn("canonical", stderr.getvalue())


class RealCLIContextTests(unittest.TestCase):
    def setUp(self):
        self.folder = (Path(__file__).parent / (".release-test-" + uuid.uuid4().hex)).resolve()
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GIT_", "GH_", "GITHUB_"))}
        self.env.update({
            "GH_TOKEN": "offline-test-token", "GITHUB_REPOSITORY": REPO,
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_AUTHOR_NAME": "Offline Test", "GIT_COMMITTER_NAME": "Offline Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        })
        scripts = self.folder / ".github/scripts"
        scripts.mkdir(parents=True)
        for name in ("release.py", "version.py"):
            shutil.copyfile(Path(release.__file__).with_name(name), scripts / name)
        self.script = scripts / "release.py"
        self.sources = list(release.version.SOURCE_PATHS)
        root = Path(release.__file__).resolve().parents[2]
        for source in self.sources:
            path = self.folder / source
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / source, path)
        inspected = subprocess.run(
            [sys.executable, str(scripts / "version.py"), "read"],
            cwd=self.folder, env=self.env, text=True, capture_output=True, check=True,
        )
        self.current_version = inspected.stdout.strip()
        self.tag = "v" + self.current_version
        self.env["RELEASE_TEST_TAG"] = self.tag
        contents = Path("test/Plugins/maddy.bundle/Contents")
        self.outputs = [contents / "MacOS/maddy", contents / "Info.plist",
                        contents / "Resources/English.lproj/InfoPlist.strings"]
        for output in self.outputs:
            path = self.folder / output
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"old tracked build")
        self.bin = self.folder / "fake-bin"
        self.bin.mkdir()
        fake_gh = self.bin / "gh"
        fake_gh.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

args = sys.argv[1:]
assert args[:2] == ["api", "--method"], args
method, endpoint = args[2:4]
payload = json.load(sys.stdin) if args[-2:] == ["--input", "-"] else None
with Path(".gh-calls.jsonl").open("a") as log:
    log.write(json.dumps({"method": method, "endpoint": endpoint, "payload": payload}) + "\n")
sha = os.environ["GITHUB_SHA"]
tag = os.environ["RELEASE_TEST_TAG"]
ref = {"ref": "refs/tags/" + tag, "object": {"sha": sha, "type": "commit"}}
base = "repos/owner/repo"
uploads = "https://uploads.github.com/repos/owner/repo/releases/123/assets"
if method == "GET" and endpoint in (
    base + "/releases?per_page=100&page=1",
    base + "/git/matching-refs/tags/" + tag,
):
    result = []
elif (method, endpoint) == ("POST", base + "/git/refs"):
    assert payload == {"ref": ref["ref"], "sha": sha}, payload
    result = ref
elif (method, endpoint) == ("GET", base + "/git/ref/tags/" + tag):
    result = ref
elif (method, endpoint) == ("POST", base + "/releases"):
    assert payload == {
        "target_commitish": sha, "tag_name": tag, "name": tag,
        "draft": False, "prerelease": False,
        "generate_release_notes": False, "body": "",
    }, payload
    result = {**payload, "id": 123, "upload_url": uploads + "{?name,label}"}
elif method == "POST" and endpoint.startswith(uploads + "?name="):
    assert args[-2] == "--input", args
    path = Path(args[-1])
    name = parse_qs(urlparse(endpoint).query)["name"][0]
    assert name == path.name, (name, path)
    result = {"id": 456, "name": name, "size": path.stat().st_size, "state": "uploaded"}
else:
    raise AssertionError((method, endpoint))
print(json.dumps(result))
''')
        fake_gh.chmod(0o755)
        self.env["PATH"] = str(self.bin) + os.pathsep + os.environ.get("PATH", "")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git("-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
                 "commit", "--quiet", "-m", "Offline fixture")
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.env["GITHUB_SHA"] = self.sha
        self.assets = [Path(f"maddy-{self.current_version}.{suffix}")
                       for suffix in ("zip", "dmg")]
        for path in self.assets:
            (self.folder / path).write_bytes(b"release archive")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.folder, env=self.env,
                              text=True, capture_output=True, check=True)

    def cli(self, command, tag=None, sha=None, env=None):
        arguments = [sys.executable, str(self.script), command, "--tag",
                     self.tag if tag is None else tag]
        if command == "publish":
            arguments += ["--sha", self.sha if sha is None else sha]
            for path in self.assets:
                arguments += ["--asset", str(path)]
        return subprocess.run(arguments, cwd=self.folder, env=self.env if env is None else env,
                              text=True, capture_output=True, check=False)

    def assert_no_api_calls(self):
        self.assertFalse((self.folder / ".gh-calls.jsonl").exists())

    def test_real_cli_success_with_tracked_generated_bundle_dirt(self):
        executable, plist, localized = (self.folder / path for path in self.outputs)
        executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"rebuilt native executable")
        executable.chmod(0o755)
        plist.write_bytes(plistlib.dumps(
            plistlib.loads((self.folder / release.version.PLIST).read_bytes()),
            fmt=plistlib.FMT_BINARY))
        localized.write_bytes(release.version.Text.read(
            self.folder / release.version.LOCALIZED[0]).text.encode("utf-16"))
        verified = subprocess.run(
            [sys.executable, ".github/scripts/version.py", "verify-built",
             "--bundle", "test/Plugins/maddy.bundle"],
            cwd=self.folder, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        changed = set(self.git("diff", "--name-only").stdout.splitlines())
        self.assertEqual(changed, {str(path) for path in self.outputs})
        checked = self.cli("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        published = self.cli("publish")
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertIn("release ID 123", published.stdout)
        calls = [json.loads(line) for line in
                 (self.folder / ".gh-calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 9)
        self.assertEqual([call["method"] for call in calls],
                         ["GET", "GET", "GET", "GET", "POST", "GET", "POST", "POST", "POST"])
        self.assertEqual(calls[4]["payload"]["sha"], self.sha)
        self.assertEqual(calls[6]["payload"]["target_commitish"], self.sha)
        self.assertTrue(all(call["endpoint"].startswith(UPLOAD + "?name=")
                            for call in calls[-2:]))

    def test_source_metadata_mutation_rejected_without_remote_calls(self):
        for source in self.sources:
            path = self.folder / source
            original = path.read_bytes()
            encoding = release.version.Text.read(path).encoding
            path.write_bytes(original + "\n ".encode(encoding))
            for staged in (False, True):
                if staged:
                    self.git("add", str(source))
                for command in ("check", "publish"):
                    with self.subTest(source=source, staged=staged, command=command):
                        result = self.cli(command)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("metadata differs from selected SHA", result.stderr)
                        self.assert_no_api_calls()
            path.write_bytes(original)
            self.git("add", str(source))

    def test_wrong_or_missing_selected_assets_rejected_before_remote_calls(self):
        expected_assets = list(self.assets)
        other_version = "0.0.0" if self.current_version != "0.0.0" else "0.0.1"
        for name in ("maddy.zip", f"maddy-{other_version}.zip", f"maddy-{self.tag}.zip"):
            wrong = self.folder / name
            wrong.write_bytes(b"wrong asset")
            self.assets = [Path(wrong.name), expected_assets[1]]
            with self.subTest(name=name):
                result = self.cli("publish")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("release assets must be exactly", result.stderr)
                self.assert_no_api_calls()
        for assets in ([expected_assets[0]], expected_assets + [wrong],
                       [expected_assets[0], expected_assets[0]], list(reversed(expected_assets)),
                       [self.folder / path for path in expected_assets],
                       ["./" + str(path) for path in expected_assets],
                       ["nested/" + str(path) for path in expected_assets]):
            self.assets = assets
            with self.subTest(assets=assets):
                result = self.cli("publish")
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_api_calls()

    def test_changed_head_rejected_without_remote_calls(self):
        self.git("-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
                 "commit", "--quiet", "--allow-empty", "-m", "Different HEAD")
        for command in ("check", "publish"):
            with self.subTest(command=command):
                result = self.cli(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("HEAD does not match selected SHA", result.stderr)
                self.assert_no_api_calls()

    def test_tag_version_mismatch_rejected_without_remote_calls(self):
        other_tag = "v0.0.0" if self.tag != "v0.0.0" else "v0.0.1"
        for command in ("check", "publish"):
            with self.subTest(command=command):
                result = self.cli(command, tag=other_tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("source version does not match selected tag", result.stderr)
                self.assert_no_api_calls()

    def test_selected_runner_sha_required_and_publish_argument_must_match(self):
        for selected in (None, "", "short", "A" * 40, self.sha + "\n"):
            env = dict(self.env)
            if selected is None:
                env.pop("GITHUB_SHA")
            else:
                env["GITHUB_SHA"] = selected
            for command in ("check", "publish"):
                with self.subTest(selected=selected, command=command):
                    result = self.cli(command, env=env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("GITHUB_SHA must be", result.stderr)
                    self.assert_no_api_calls()
        result = self.cli("publish", sha=OTHER)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--sha must match GITHUB_SHA", result.stderr)
        self.assert_no_api_calls()


if __name__ == "__main__":
    unittest.main()
