import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import bump
import version


class BumpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "checkout"
        self.repo.mkdir()
        self.origin = self.base / "origin.git"
        shutil.copytree(ROOT / ".github", self.repo / ".github",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for path in version.SOURCE_PATHS:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, target)
        self.git("init", "--quiet", "-b", "fixture")
        self.git("config", "user.name", "Offline Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture source")
        self.first = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("commit", "--quiet", "--allow-empty", "-m", "selected fixture")
        self.old = self.git("rev-parse", "HEAD").stdout.strip()
        subprocess.run(["git", "init", "--bare", "--quiet", str(self.origin)], check=True)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("-c", "push.followTags=false", "push", "--quiet", "--no-follow-tags",
                 "origin", "HEAD:refs/heads/fixture")
        self.env = os.environ.copy()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("GIT_CONFIG_COUNT", None)
        self.ref = "refs/heads/fixture"

    def git(self, *args, check=True, input=None, env=None):
        return subprocess.run(["git", *args], cwd=self.repo, text=True, input=input,
                              capture_output=True, check=check, env=env)

    def remote(self):
        return self.git("ls-remote", "--refs", "origin", self.ref).stdout.split()[0]

    def run_bump(self, check=True):
        result = subprocess.run(
            [sys.executable, ".github/scripts/bump.py", "--ref", self.ref,
             "--sha", self.old, "--mode", "patch"], cwd=self.repo, env=self.env,
            text=True, capture_output=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def new_commit(self, trailer=True, non_source=False):
        if non_source:
            (self.repo / "unrelated.txt").write_text("not version source")
        else:
            subprocess.run([sys.executable, ".github/scripts/version.py", "bump", "patch"],
                           cwd=self.repo, check=True, capture_output=True, env=self.env)
        self.git("add", ".")
        message = "source fixture" + ("\n\n" + bump.TRAILER if trailer else "")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def guard(self, new, rows, check=False):
        env = self.env.copy()
        env.update(BUMP_REF=self.ref, BUMP_OLD_SHA=self.old, BUMP_NEW_SHA=new)
        result = subprocess.run([sys.executable, ".github/scripts/bump.py", "--guard"],
                                cwd=self.repo, env=env, input=rows, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode == 0, check, result.stdout + result.stderr)
        return result

    def row(self, new):
        return f"HEAD {new} {self.ref} {self.old}\n"

    def test_genuine_bump_clean_exact_direct_parent_source_only_trailer_no_tags(self):
        self.git("config", "push.followTags", "true")
        self.git("tag", "-a", "fixture-unpushed", "-m", "never follow")
        result = self.run_bump()
        new = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(new, self.old)
        self.assertEqual(self.remote(), new)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", new).stdout.split(), [new, self.old])
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        paths = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", new).stdout.splitlines()
        self.assertTrue(paths)
        self.assertTrue(set(paths).issubset({str(p) for p in version.SOURCE_PATHS}))
        self.assertTrue(self.git("show", "-s", "--format=%B", new).stdout.strip().endswith(bump.TRAILER))
        self.assertEqual(self.git("ls-remote", "--tags", "origin").stdout, "")
        self.assertIn("Bumped source", result.stdout)

    def test_tag_selection_rejected_before_writes(self):
        self.ref = "refs/tags/fixture"
        self.run_bump(False)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_initial_dirty_rejected(self):
        (self.repo / "unknown").write_text("dirty")
        self.run_bump(False)
        self.assertEqual(self.remote(), self.old)

    def test_initial_hook_missing_and_nonexecutable(self):
        hook = self.repo / bump.HOOK
        hook.chmod(0o644)
        self.run_bump(False)
        hook.unlink()
        self.run_bump(False)
        self.assertEqual(self.remote(), self.old)

    def test_initial_advance_rewind_delete_rejected(self):
        for target in (self.first, "0" * 40):
            with self.subTest(target=target):
                subprocess.run(["git", "--git-dir", str(self.origin), "update-ref", self.ref, target], check=True)
                self.run_bump(False)
                self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
                subprocess.run(["git", "--git-dir", str(self.origin), "update-ref", self.ref, self.old], check=True)
        advanced = self.git("commit-tree", "HEAD^{tree}", "-p", self.old, input="advanced\n").stdout.strip()
        self.git("push", "--quiet", "origin", f"{advanced}:{self.ref}")
        self.run_bump(False)

    def test_exact_guard_identity_and_update_count(self):
        new = self.new_commit()
        row = self.row(new)
        self.guard(new, row, True)
        self.guard(new, row.replace("HEAD", new), True)
        for invalid in ("", row + row, row.replace("HEAD", "refs/heads/fixture"),
                        row.replace(self.old, self.first), row.replace(new, self.old),
                        row.replace(self.ref, "refs/heads/other"),
                        row.replace(self.old, "0" * 40), "malformed\n"):
            with self.subTest(advertisement=invalid):
                self.guard(new, invalid)

    def test_guard_non_source_trailer_and_multiple_parents(self):
        new = self.new_commit(non_source=True)
        self.guard(new, self.row(new))
        self.git("reset", "--soft", self.old)
        self.git("restore", "--staged", ".")
        (self.repo / "unrelated.txt").unlink()
        new = self.new_commit(trailer=False)
        self.guard(new, self.row(new))
        merge = self.git("commit-tree", "HEAD^{tree}", "-p", self.old, "-p", self.first,
                         input="merge\n\n" + bump.TRAILER + "\n").stdout.strip()
        self.git("update-ref", "HEAD", merge)
        self.guard(merge, self.row(merge))

    def test_guard_empty_commit_rejected(self):
        self.git("commit", "--quiet", "--allow-empty", "-m", "empty", "-m", bump.TRAILER)
        new = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.guard(new, self.row(new))
        self.assertIn("nonempty and source-version-only", result.stderr)

    def test_guard_more_than_one_commit_from_selected_rejected(self):
        self.new_commit()
        new = self.new_commit()
        result = self.guard(new, self.row(new))
        self.assertIn("single direct parent", result.stderr)

    def test_guard_same_result_real_zero_update_push(self):
        new = self.new_commit()
        self.git("push", "--quiet", "origin", f"HEAD:{self.ref}")
        env = self.env.copy()
        env.update(BUMP_REF=self.ref, BUMP_OLD_SHA=self.old, BUMP_NEW_SHA=new)
        result = self.git("-c", "push.followTags=false", "-c",
                          f"core.hooksPath={self.repo / '.github/hooks'}", "push",
                          "--no-follow-tags", "origin", f"HEAD:{self.ref}", check=False, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one advertised update", result.stderr)

    def wrapper(self, action):
        real_git = shutil.which("git")
        directory = self.base / "bin"
        directory.mkdir()
        script = directory / "git"
        script.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, subprocess, sys\n"
            f"real={real_git!r}\n"
            "args=sys.argv[1:]\n"
            "result=subprocess.run([real,*args],capture_output=True)\n"
            "if args and args[0]=='ls-remote':\n"
            f" counter=pathlib.Path({str(self.base / 'reads')!r})\n"
            " n=int(counter.read_text())+1 if counter.exists() else 1\n"
            " counter.write_text(str(n))\n"
            " if n==2:\n"
            f"  repo={str(self.repo)!r}; origin={str(self.origin)!r}; ref={self.ref!r}\n"
            f"  action={action!r}\n"
            "  if action in ('missing','nonexec'):\n"
            "   hook=pathlib.Path(repo)/'.github/hooks/pre-push'\n"
            "   hook.unlink() if action=='missing' else hook.chmod(0o644)\n"
            "  else:\n"
            "   if action=='same':\n"
            "    subprocess.run([real,'--git-dir',origin,'fetch','--quiet','--no-tags',repo,'HEAD'],check=True)\n"
            "    target=subprocess.check_output([real,'rev-parse','HEAD'],text=True).strip()\n"
            f"   elif action=='rewind': target={self.first!r}\n"
            "   elif action=='delete': target='0'*40\n"
            "   else:\n"
            "    target=subprocess.check_output([real,'commit-tree','HEAD^{tree}','-p','HEAD'],input='race\\n',text=True).strip()\n"
            "    subprocess.run([real,'--git-dir',origin,'fetch','--quiet','--no-tags',repo,target],check=True)\n"
            "   subprocess.run([real,'--git-dir',origin,'update-ref',ref,target],check=True)\n"
            "sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr)\n"
            "sys.exit(result.returncode)\n")
        script.chmod(0o755)
        self.env["PATH"] = str(directory) + os.pathsep + self.env["PATH"]

    def test_after_final_read_advertised_advance(self):
        self.wrapper("advance")
        result = self.run_bump(False)
        self.assertIn("Push guard requires exactly one advertised update", result.stderr)
        self.assertNotEqual(self.remote(), self.git("rev-parse", "HEAD").stdout.strip())

    def test_after_final_read_advertised_rewind(self):
        self.wrapper("rewind")
        result = self.run_bump(False)
        self.assertIn("Advertised push does not match", result.stderr)
        self.assertEqual(self.remote(), self.first)

    def test_after_final_read_advertised_delete(self):
        self.wrapper("delete")
        result = self.run_bump(False)
        self.assertIn("Advertised push does not match", result.stderr)
        self.assertEqual(self.git("ls-remote", "--refs", "origin", self.ref).stdout, "")

    def test_after_final_read_advertised_same_result_zero_updates(self):
        self.wrapper("same")
        result = self.run_bump(False)
        self.assertIn("exactly one advertised update", result.stderr)
        self.assertEqual(self.remote(), self.git("rev-parse", "HEAD").stdout.strip())

    def test_late_missing_hook(self):
        self.wrapper("missing")
        self.run_bump(False)
        self.assertEqual(self.remote(), self.old)

    def test_late_nonexecutable_hook(self):
        self.wrapper("nonexec")
        self.run_bump(False)
        self.assertEqual(self.remote(), self.old)


if __name__ == "__main__":
    unittest.main()
