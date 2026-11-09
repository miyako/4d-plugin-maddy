"""Execute the release workflow's real scripts with offline native-tool doubles."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import version


@contextmanager
def cwd(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


# Every native executable in PATH points here, never to an installed Apple or
# Microsoft tool. Python and the small POSIX utility allowlist are the exceptions.
NATIVE_STUB = r'''
import base64
import json
import os
from pathlib import Path
import shutil
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
record = {"command": name, "args": args}
if name == "xcodebuild":
    bundle = Path("test/Plugins/maddy.bundle/Contents")
    record["stale"] = [str(p) for p in (
        bundle / "MacOS/maddy", bundle / "Info.plist",
        bundle / "Resources/English.lproj/InfoPlist.strings",
        bundle / "_CodeSignature",
    ) if p.exists()]
with open(os.environ["NATIVE_LOG"], "a") as log:
    log.write(json.dumps(record) + "\n")
if os.environ.get("NATIVE_FAIL") == name:
    sys.exit(19)

if name == "msbuild":
    mode = os.environ.get("MSBUILD_OUTPUT", "normal")
    path = Path("test/plugins/maddy.bundle/contents/windows64/maddy.4DX")
    if mode != "missing":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"" if mode == "empty" else b"new Windows executable")
elif name == "xcodebuild":
    mode = os.environ.get("XCODE_OUTPUT", "normal")
    bundle = Path("test/Plugins/maddy.bundle/Contents")
    if mode != "missing":
        binary = bundle / "MacOS/maddy"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"" if mode == "empty" else b"new universal executable")
        for relative in ("Info.plist", "Resources/English.lproj/InfoPlist.strings"):
            if mode == "no-plist" and relative == "Info.plist":
                continue
            if mode == "no-localized" and relative != "Info.plist":
                continue
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path("expected") / relative, path)
elif name == "lipo":
    assert args[:1] == ["-archs"], args
    print(os.environ.get("LIPO_ARCHES", "arm64 x86_64"))
elif name == "security":
    if args[0] == "find-identity":
        if os.environ.get("IDENTITY", "present") == "present":
            print('  1) ABCDEF "Developer ID Application: Offline Fixture (TEST)"')
elif name == "base64":
    Path(args[args.index("-o") + 1]).write_bytes(base64.b64decode(sys.stdin.read()))
elif name == "codesign":
    stage = "VERIFY" if "--verify" in args else "SIGN"
    sys.exit(int(os.environ.get("CODESIGN_" + stage + "_RC", "0")))
elif name in ("ditto", "hdiutil"):
    Path(args[-1]).write_bytes(b"offline package")
elif name == "xcrun":
    if args[:2] == ["notarytool", "submit"]:
        suffix = "ZIP" if args[2].endswith(".zip") else "DMG"
        assert "--wait" in args and args[-2:] == ["--output-format", "json"], args
        print(os.environ.get("NOTARY_" + suffix + "_JSON", '{"status":"Accepted"}'))
        sys.exit(int(os.environ.get("NOTARY_" + suffix + "_RC", "0")))
    elif args[:2] == ["stapler", "staple"]:
        sys.exit(int(os.environ.get("STAPLE_RC", "0")))
    else:
        assert args[:2] == ["notarytool", "store-credentials"], args
else:
    raise AssertionError("Unexpected native command: " + name)
'''

RELEASE_STUB = '''
import json
import os
import sys
with open(os.environ["NATIVE_LOG"], "a") as log:
    log.write(json.dumps({"command": "release-helper", "args": sys.argv[1:],
                         "authenticated": bool(os.environ.get("GH_TOKEN"))}) + "\\n")
'''


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        cls.pwsh = shutil.which("pwsh")
        if not cls.bash or not cls.pwsh:
            raise RuntimeError("Native offline tests require bash and pwsh; no skips")
        cls.workflow = yaml.load(
            (ROOT / ".github/workflows/release.yml").read_text(),
            Loader=yaml.BaseLoader,
        )

    def setUp(self):
        # Never use the system temporary directory, including via tempfile.
        self.root = ROOT / ".github/tests" / (".native-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.bin = self.root / "closed-path"
        self.bin.mkdir()
        self.log = self.root / "native.jsonl"
        for command in ("mkdir", "rm", "ls", "cp", "grep", "head", "sed"):
            executable = shutil.which(command)
            self.assertIsNotNone(executable, command)
            (self.bin / command).symlink_to(executable)
        (self.bin / "python3").symlink_to(sys.executable)
        for command in ("msbuild", "xcodebuild", "lipo", "security",
                        "base64", "codesign", "ditto", "hdiutil", "xcrun"):
            stub = self.bin / command
            stub.write_text(f"#!{sys.executable}\n" + NATIVE_STUB)
            stub.chmod(0o755)
        for relative in (*version.SOURCE_PATHS, Path(".github/scripts/version.py")):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        (self.root / ".github/scripts/release.py").write_text(RELEASE_STUB)
        with cwd(self.root):
            self.current = version.inspect()[0]
            values = dict.fromkeys(version.KEYS, self.current)
            self.plist = {
                key: version.resolved(token.value, values)
                for key, token in version.xml_fields(version.Text.read(version.PLIST)).items()
            }
            self.localized = {
                key: token.value if key == version.INFO else version.resolved(token.value, values)
                for key, token in version.localized_fields(
                    version.Text.read(version.LOCALIZED[0])).items()
            }
        self.bundle = Path("test/Plugins/maddy.bundle")
        self.contents = self.bundle / "Contents"
        self.write(self.contents / "Resources/manifest.json", b"tracked resource")
        self.expected("strings")
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.root),
            **self.workflow["env"],
            "VERSION": self.current, "TAG": "v" + self.current,
            "GITHUB_SHA": "a" * 40, "GH_TOKEN": "offline-test-token",
            "GITHUB_ENV": str(self.root / "github-env"),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "RUNNER_TEMP": str(self.root / "runner"),
            "KEYCHAIN_PATH": str(self.root / "runner/build.keychain-db"),
            "KEYCHAIN_PASSWORD": "dummy", "CERT_BASE64": "ZHVtbXk=",
            "CERT_PASSWORD": "dummy", "APPLE_ID": "offline@example.invalid",
            "TEAM_ID": "TEST", "APP_PASSWORD": "dummy",
            "BUNDLE_PATH": str(self.bundle),
            "ZIP_PATH": f"maddy-{self.current}.zip",
            "DMG_PATH": f"maddy-{self.current}.dmg",
            "NATIVE_LOG": str(self.log),
        }
        (self.root / "runner").mkdir()

    def write(self, relative, data=b"preserved"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def expected(self, style):
        self.write("expected/Info.plist", plistlib.dumps(
            self.plist, fmt=plistlib.FMT_BINARY if style == "binary" else plistlib.FMT_XML))
        if style in ("binary", "xml"):
            data = plistlib.dumps(self.localized, fmt=(
                plistlib.FMT_BINARY if style == "binary" else plistlib.FMT_XML))
        else:
            text = "\n".join(f"{key} = {json.dumps(value)};"
                             for key, value in self.localized.items())
            data = text.encode(style if style.startswith("utf-16") else "utf-8")
            if style == "utf-16-be":
                data = b"\xfe\xff" + data
        self.write("expected/Resources/English.lproj/InfoPlist.strings", data)

    def step(self, name):
        matches = [step for job in self.workflow["jobs"].values()
                   for step in job["steps"] if step.get("name") == name]
        self.assertEqual(len(matches), 1, name)
        return matches[0]

    def run_step(self, name, success=True, **env):
        step = self.step(name)
        script = step["run"]
        shell = step["shell"]
        if shell == "bash":
            command = [self.bash, "--noprofile", "--norc", "-eo", "pipefail", "-s"]
        elif shell == "pwsh":
            command = [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive",
                       "-Command", script]
        else:
            self.fail(f"Uncovered workflow shell: {shell}")
        result = subprocess.run(
            command, input=script if shell == "bash" else None,
            text=True, capture_output=True, cwd=self.root,
            env={**self.env, **env}, timeout=30,
        )
        if success is not None:
            self.assertEqual(result.returncode == 0, success,
                             f"{name}: {result.stdout}\n{result.stderr}")
        if result.returncode == 0:
            path = Path(self.env["GITHUB_ENV"])
            if path.exists():
                for line in path.read_text().splitlines():
                    key, value = line.split("=", 1)
                    self.env[key] = value
        return result

    def calls(self, command=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if command is None or call["command"] == command]

    def build(self, **env):
        self.run_step("build macOS plugin (universal)", **env)

    def verify(self, success=True, **env):
        self.run_step("verify universal binary", success=success, **env)

    def test_closed_path_contains_only_allowed_utilities_and_native_stubs(self):
        native = {"msbuild", "xcodebuild", "lipo", "security", "base64",
                  "codesign", "ditto", "hdiutil", "xcrun"}
        self.assertEqual({p.name for p in self.bin.iterdir()},
                         native | {"python3", "mkdir", "rm", "ls", "cp", "grep", "head", "sed"})
        for name in native:
            self.assertFalse((self.bin / name).is_symlink())
        self.assertFalse((self.bin / "gh").exists())

    def test_selected_source_read_cli(self):
        self.run_step("read selected source version")
        output = Path(self.env["GITHUB_OUTPUT"]).read_text()
        self.assertIn(f"version={self.current}\n", output)
        self.assertIn(f"tag=v{self.current}\n", output)

    def test_windows_cleanup_is_selective(self):
        generated = self.write("test/plugins/maddy.bundle/contents/windows64/maddy.4DX")
        intermediate = self.write("Objs/Release/old.obj")
        preserved = [
            self.write("test/plugins/maddy.bundle/contents/windows64/keep.dll"),
            self.write(self.contents / "Resources/manifest.json"),
            self.write(self.contents / "QuickLook/Thumbnail.png"),
            self.write("test/Plugins/Default/keep-resource"),
            self.write("Objs/Debug/keep.obj"),
        ]
        before = {p: p.read_bytes() for p in preserved}
        sources = {p: (self.root / p).read_bytes() for p in version.SOURCE_PATHS}
        self.run_step("clean stale outputs")
        self.assertFalse(generated.exists())
        self.assertFalse(intermediate.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in preserved})
        self.assertEqual(sources, {p: (self.root / p).read_bytes() for p in sources})
        self.run_step("clean stale outputs")

    def test_windows_cleanup_real_permission_error_is_fatal(self):
        parent = self.root / "Objs"
        self.write("Objs/Release/locked.obj")
        parent.chmod(0o555)
        try:
            self.run_step("clean stale outputs", success=False)
        finally:
            parent.chmod(0o755)

    def test_windows_build_exact_toolchain_and_fresh_outdir(self):
        self.assertEqual(self.step("build x64")["shell"], "pwsh")
        self.run_step("build x64")
        self.run_step("normalize windows output to canonical case")
        args = self.calls("msbuild")[0]["args"]
        self.assertEqual(args, ["maddy.sln", "/p:Configuration=Release", "/p:Platform=x64",
                                "/p:PlatformToolset=v143", "/p:WindowsTargetPlatformVersion=10.0", "/m"])
        self.assertEqual((self.root / self.contents / "Windows64/maddy.4DX").read_bytes(),
                         b"new Windows executable")

    def test_windows_exact_outdir_rejects_recursive_stale_and_empty(self):
        self.write("recursive/stale/maddy.4DX")
        self.run_step("normalize windows output to canonical case", success=False)
        source = self.root / "test/plugins/maddy.bundle/contents/windows64/maddy.4DX"
        source.mkdir(parents=True)
        self.run_step("normalize windows output to canonical case", success=False)
        source.rmdir()
        self.run_step("build x64", MSBUILD_OUTPUT="empty")
        self.run_step("normalize windows output to canonical case", success=False)

    def test_windows_build_failure_and_missing_output(self):
        result = self.run_step("build x64", success=False, NATIVE_FAIL="msbuild")
        self.assertIn("MSBuild failed with exit code 19", result.stderr)
        self.assertEqual(len(self.calls("msbuild")), 1)
        self.run_step("build x64", MSBUILD_OUTPUT="missing")
        self.run_step("normalize windows output to canonical case", success=False)

    def test_xcode_selective_freshness_and_native_arguments(self):
        preserved = []
        for prefix in ("test/Plugins", "test/Plugins/Default"):
            contents = Path(prefix) / "maddy.bundle/Contents"
            for relative in ("MacOS/maddy", "Info.plist",
                             "Resources/English.lproj/InfoPlist.strings",
                             "_CodeSignature/CodeResources"):
                self.write(contents / relative, b"stale")
            for relative in ("Resources/manifest.json", "Resources/other.lproj/keep",
                             "QuickLook/Thumbnail.png", "Windows64/maddy.4DX",
                             "Windows64/keep.dll"):
                preserved.append(self.write(contents / relative))
            self.write(Path(prefix) / "maddy.build/old.o")
            preserved.append(self.write(Path(prefix) / "other.bundle/Resources/keep"))
        preserved.append(self.write("test/Plugins/Default/sibling-resource"))
        self.write("build/maddy.build/old.o")
        sources = {p: (self.root / p).read_bytes() for p in version.SOURCE_PATHS}
        self.build()
        call = self.calls("xcodebuild")[0]
        self.assertEqual(call["stale"], [])
        self.assertEqual(call["args"], [
            "-project", "maddy.xcodeproj", "-target", "maddy", "-configuration", "Default",
            "ARCHS=arm64 x86_64", "ONLY_ACTIVE_ARCH=NO", "MACOSX_DEPLOYMENT_TARGET=10.13",
            "CODE_SIGNING_ALLOWED=NO",
            f"CONFIGURATION_BUILD_DIR={self.root}/test/Plugins",
            f"OBJROOT={self.root}/build", "build",
        ])
        for path in preserved:
            self.assertEqual(path.read_bytes(), b"preserved")
        self.assertEqual(sources, {p: (self.root / p).read_bytes() for p in sources})
        for relative in ("test/Plugins/maddy.build", "test/Plugins/Default/maddy.build",
                         "build/maddy.build", "test/Plugins/Default/maddy.bundle/Contents/MacOS/maddy"):
            self.assertFalse((self.root / relative).exists(), relative)
        self.verify()

    def test_mac_cleanup_real_permission_error_stops_before_build(self):
        executable = self.write(self.contents / "MacOS/maddy", b"stale")
        executable.parent.chmod(0o555)
        try:
            self.run_step("build macOS plugin (universal)", success=False)
            self.assertFalse(self.calls("xcodebuild"))
        finally:
            executable.parent.chmod(0o755)

    def test_xcode_failure_and_missing_empty_or_incomplete_outputs(self):
        self.run_step("build macOS plugin (universal)", success=False, NATIVE_FAIL="xcodebuild")
        for mode in ("missing", "empty", "no-plist", "no-localized"):
            with self.subTest(mode=mode):
                self.build(XCODE_OUTPUT=mode)
                self.verify(success=False)

    def test_lipo_required_exact_tokens_extra_arches_allowed(self):
        self.build()
        for arches, success in (
            ("arm64 x86_64", True), ("x86_64 arm64 i386 arm64e", True),
            ("arm64e x86_64", False), ("arm64 x86_64h", False),
            ("arm64", False), ("x86_64", False), ("", False),
        ):
            with self.subTest(arches=arches):
                self.verify(success=success, LIPO_ARCHES=arches)
        self.assertTrue(all(call["args"] == ["-archs", str(self.contents / "MacOS/maddy")]
                            for call in self.calls("lipo")))

    def test_lipo_command_failure_is_fatal(self):
        self.build()
        self.verify(success=False, NATIVE_FAIL="lipo")

    def test_built_metadata_formats_use_selected_source(self):
        for style in ("strings", "utf-16", "utf-16-be", "xml", "binary"):
            with self.subTest(style=style):
                self.expected(style)
                self.build()
                self.verify()

    def test_built_plist_and_localized_drift_are_rejected(self):
        self.build()
        different = self.current + "0"
        plist = self.root / self.contents / "Info.plist"
        values = plistlib.loads(plist.read_bytes())
        values[version.SHORT] = different
        plist.write_bytes(plistlib.dumps(values))
        self.verify(success=False)
        self.build()
        path = self.root / self.contents / "Resources/English.lproj/InfoPlist.strings"
        path.write_text(f'{version.SHORT} = "{different}";')
        self.verify(success=False)
        if version.INFO in self.localized:
            self.build()
            fields = {**self.localized, version.INFO: "wrong selected-source information"}
            path.write_bytes(plistlib.dumps(fields, fmt=plistlib.FMT_BINARY))
            self.verify(success=False)

    def test_merge_requires_exact_nonempty_download(self):
        self.write("winbuild/nested/maddy.4DX", b"stale nested")
        self.write(self.contents / "Windows64/maddy.4DX", b"old Windows")
        self.run_step("merge Windows binaries into the bundle", success=False)
        self.write("winbuild/maddy.4DX", b"")
        self.run_step("merge Windows binaries into the bundle", success=False)
        self.write("winbuild/maddy.4DX", b"new exact Windows")
        self.run_step("merge Windows binaries into the bundle")
        self.assertEqual((self.root / self.contents / "Windows64/maddy.4DX").read_bytes(),
                         b"new exact Windows")
        self.assertEqual(self.env["BUNDLE_PATH"], str(self.bundle))

    def test_certificate_import_and_notary_credentials_are_stubbed(self):
        self.run_step("import Developer ID certificate")
        self.run_step("store notarytool credentials")
        security = self.calls("security")
        self.assertEqual([call["args"][0] for call in security],
                         ["create-keychain", "set-keychain-settings", "unlock-keychain",
                          "import", "list-keychain", "set-key-partition-list"])
        self.assertEqual((self.root / "runner/certificate.p12").read_bytes(), b"dummy")
        self.assertIn(self.env["KEYCHAIN_PATH"], security[3]["args"])
        self.assertEqual(self.calls("xcrun")[0]["args"][:3],
                         ["notarytool", "store-credentials", "notarytool-profile"])

    def test_certificate_and_credential_failures_stop(self):
        self.run_step("import Developer ID certificate", success=False, NATIVE_FAIL="security")
        self.run_step("store notarytool credentials", success=False, NATIVE_FAIL="xcrun")

    def test_codesign_signs_and_strictly_verifies(self):
        self.run_step("codesign")
        calls = self.calls("codesign")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["args"], [
            "--verbose", "--deep", "--timestamp", "--force", "--options=runtime",
            "--sign", "Developer ID Application: Offline Fixture (TEST)", str(self.bundle)])
        self.assertEqual(calls[1]["args"],
                         ["--verify", "--deep", "--strict", "--verbose=2", str(self.bundle)])

    def test_codesign_identity_sign_and_verify_failures(self):
        for env, count in (({"IDENTITY": "missing"}, 0),
                           ({"NATIVE_FAIL": "security"}, 0),
                           ({"CODESIGN_SIGN_RC": "3"}, 1),
                           ({"CODESIGN_VERIFY_RC": "4"}, 2)):
            with self.subTest(env=env):
                before = len(self.calls("codesign"))
                self.run_step("codesign", success=False, **env)
                self.assertEqual(len(self.calls("codesign")) - before, count)

    def test_package_both_accepted_then_staple(self):
        self.run_step("package, notarize & staple")
        calls = self.calls()
        self.assertEqual([call["command"] for call in calls],
                         ["ditto", "hdiutil", "xcrun", "xcrun", "xcrun"])
        self.assertEqual(calls[0]["args"],
                         ["-c", "-k", "--keepParent", str(self.bundle), self.env["ZIP_PATH"]])
        self.assertEqual(calls[1]["args"], [
            "create", "-format", "UDBZ", "-volname", "maddy",
            "-srcfolder", str(self.bundle), self.env["DMG_PATH"]])
        for call, asset in zip(calls[2:4], (self.env["ZIP_PATH"], self.env["DMG_PATH"])):
            self.assertEqual(call["args"], [
                "notarytool", "submit", asset, "--keychain-profile", "notarytool-profile",
                "--keychain", self.env["KEYCHAIN_PATH"], "--wait", "--output-format", "json"])
        self.assertEqual(calls[-1]["args"], ["stapler", "staple", self.env["DMG_PATH"]])

    def pipeline(self, **env):
        """Model Actions' success-only step scheduling using the real run blocks."""
        self.write("winbuild/maddy.4DX", b"downloaded fresh Windows")
        steps = [step for step in self.workflow["jobs"]["build-macos"]["steps"] if "run" in step]
        names = [step["name"] for step in steps]
        self.assertEqual(names[-1], "create GitHub Release")
        for name in names:
            if self.run_step(name, success=None, **env).returncode:
                return False
        return True

    def test_full_success_publishes_last_with_exact_helper_arguments(self):
        self.assertTrue(self.pipeline())
        calls = self.calls()
        self.assertEqual(calls[-2]["args"][:2], ["stapler", "staple"])
        self.assertEqual(calls[-1], {
            "command": "release-helper", "authenticated": True,
            "args": ["publish", "--tag", "v" + self.current, "--sha", "a" * 40,
                     "--asset", self.env["ZIP_PATH"], "--asset", self.env["DMG_PATH"]],
        })
        self.assertEqual(self.step("create GitHub Release")["env"]["GH_TOKEN"], "${{ github.token }}")

    def test_each_notary_rejects_bad_json_status_or_exit_even_if_accepted(self):
        cases = (
            {"JSON": "not json"}, {"JSON": "{}"}, {"JSON": '{"status":"Invalid"}'},
            {"JSON": '{"status":"accepted"}'}, {"JSON": "[]"},
            {"JSON": '{"status":"Accepted"}', "RC": "5"},
        )
        for suffix in ("ZIP", "DMG"):
            for case in cases:
                with self.subTest(asset=suffix, case=case):
                    self.log.unlink(missing_ok=True)
                    env = {f"NOTARY_{suffix}_{key}": value for key, value in case.items()}
                    self.assertFalse(self.pipeline(**env))
                    calls = [c for c in self.calls("xcrun") if c["args"][:2] !=
                             ["notarytool", "store-credentials"]]
                    self.assertEqual(len(calls), 1 if suffix == "ZIP" else 2)
                    self.assertFalse(self.calls("release-helper"))
                    self.assertFalse(any(c["args"][:1] == ["stapler"] for c in calls))

    def test_packaging_signing_and_staple_failures_never_publish(self):
        for env in (
            {"NATIVE_FAIL": "ditto"}, {"NATIVE_FAIL": "hdiutil"},
            {"CODESIGN_SIGN_RC": "1"}, {"CODESIGN_VERIFY_RC": "1"},
            {"STAPLE_RC": "8"},
        ):
            with self.subTest(env=env):
                self.log.unlink(missing_ok=True)
                self.assertFalse(self.pipeline(**env))
                self.assertFalse(self.calls("release-helper"))
        self.assertEqual(self.calls("xcrun")[-1]["args"][:2], ["stapler", "staple"])

    def test_build_verification_failures_never_reach_publication(self):
        for env in (
            {"NATIVE_FAIL": "xcodebuild"}, {"XCODE_OUTPUT": "missing"},
            {"XCODE_OUTPUT": "empty"}, {"XCODE_OUTPUT": "no-plist"},
            {"XCODE_OUTPUT": "no-localized"}, {"NATIVE_FAIL": "lipo"},
            {"LIPO_ARCHES": "arm64e x86_64"},
        ):
            with self.subTest(env=env):
                self.log.unlink(missing_ok=True)
                self.assertFalse(self.pipeline(**env))
                self.assertFalse(self.calls("codesign"))
                self.assertFalse(self.calls("release-helper"))


if __name__ == "__main__":
    unittest.main()
