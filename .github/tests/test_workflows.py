from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


class WorkflowTests(unittest.TestCase):
    def test_build_exact_manual_zero_inputs(self):
        build = workflow("release.yml")
        self.assertEqual(build["name"], "Build")
        self.assertEqual(set(build["on"]), {"workflow_dispatch"})
        self.assertIn(build["on"]["workflow_dispatch"], ("", None))
        self.assertEqual(build["concurrency"]["cancel-in-progress"], "false")
        self.assertNotIn("${{", build["concurrency"]["group"])
        self.assertEqual(set(build["jobs"]), {"version", "build-windows", "build-macos"})

    def test_bump_only_manual_semver_source(self):
        config = workflow("bump-version.yml")
        self.assertEqual(config["name"], "Bump")
        self.assertEqual(set(config["on"]), {"workflow_dispatch"})
        inputs = config["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"mode"})
        self.assertEqual(inputs["mode"]["options"], ["patch", "minor", "major"])
        text = (ROOT / ".github/workflows/bump-version.yml").read_text()
        self.assertNotRegex(text, r"(gh release|git tag|workflow run|dispatches|needs:)")

    def test_all_checkouts_use_selected_sha(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            config = workflow(path.name)
            for job in config["jobs"].values():
                for step in job["steps"]:
                    if step.get("uses", "").startswith("actions/checkout@"):
                        self.assertEqual(step["with"]["ref"], "${{ github.sha }}")

    def test_final_publication_after_native_no_hidden_branch_gate(self):
        config = workflow("release.yml")
        for job in config["jobs"].values():
            self.assertNotIn("if", job)
        steps = config["jobs"]["build-macos"]["steps"]
        names = [step.get("name") for step in steps]
        self.assertLess(names.index("codesign"), names.index("package, notarize & staple"))
        self.assertLess(names.index("package, notarize & staple"), names.index("create GitHub Release"))
        self.assertEqual(names[-1], "create GitHub Release")
        last = steps[-1]
        self.assertEqual(last["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn('publish --tag "$TAG" --sha "$GITHUB_SHA"', last["run"])
        early = config["jobs"]["version"]["steps"][-1]
        self.assertEqual(early["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn('check --tag "$TAG"', early["run"])

    def test_actual_windows_toolchain_and_exact_artifact(self):
        config = workflow("release.yml")
        steps = config["jobs"]["build-windows"]["steps"]
        command = next(step["run"] for step in steps if step.get("name") == "build x64")
        for token in ("/p:Configuration=Release", "/p:Platform=x64",
                      "/p:PlatformToolset=v143", "/p:WindowsTargetPlatformVersion=10.0"):
            self.assertIn(token, command)
        artifact = steps[-1]["with"]
        self.assertEqual(artifact["path"],
                         "${{ env.BASE_DIR }}/Plugins/${{ env.PRODUCT_NAME }}.bundle/Contents/Windows64/${{ env.PRODUCT_NAME }}.4DX")
        self.assertEqual(artifact["if-no-files-found"], "error")
        project = (ROOT / "maddy.vcxproj").read_text(encoding="utf-8-sig")
        self.assertIn(r"$(ProjectDir)test\plugins\$(ProjectName).bundle\contents\windows64" + "\\", project)

    def test_secret_names_unchanged(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        names = set(re.findall(r"secrets\.([A-Z_]+)", text))
        self.assertEqual(names, {
            "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
            "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID",
            "NOTARYTOOL_PASSWORD",
        })

    def test_bump_push_has_both_no_follow_tags_controls(self):
        text = (ROOT / ".github/scripts/bump.py").read_text()
        self.assertIn('"push.followTags=false"', text)
        self.assertIn('"--no-follow-tags"', text)
        self.assertNotIn('"--force"', text)
        self.assertNotIn('"--amend"', text)
        self.assertTrue((ROOT / ".github/hooks/pre-push").stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
