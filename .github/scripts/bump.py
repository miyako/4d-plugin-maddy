#!/usr/bin/env python3
"""Create one source-only commit and push it only over the selected branch SHA."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

import version

TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
HOOK = Path(".github/hooks/pre-push")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def git(*args, input=None):
    return subprocess.run(["git", *args], input=input, text=True, check=True,
                          stdout=subprocess.PIPE).stdout.strip()


def hook_ready():
    if not HOOK.is_file() or not os.access(HOOK, os.X_OK):
        raise ValueError(f"Mandatory executable push guard is missing: {HOOK}")


def clean():
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Selected checkout must be completely clean")


def advertised(ref):
    result = git("ls-remote", "--refs", "origin", ref).splitlines()
    if len(result) != 1:
        raise ValueError("Selected remote branch is missing or ambiguous")
    fields = result[0].split()
    if len(fields) != 2 or fields[1] != ref or not SHA.fullmatch(fields[0]):
        raise ValueError("Malformed remote branch advertisement")
    return fields[0]


def initial(ref, selected):
    hook_ready()
    if not ref.startswith("refs/heads/") or not SHA.fullmatch(selected):
        raise ValueError("Bump requires a selected branch and exact selected SHA")
    git("check-ref-format", ref)
    if git("rev-parse", "HEAD") != selected:
        raise ValueError("Checkout does not equal selected SHA")
    clean()
    if advertised(ref) != selected:
        raise ValueError("Selected branch advanced, rewound, or disappeared")


def commit_ready(ref, selected, new):
    hook_ready()
    if not ref.startswith("refs/heads/") or not SHA.fullmatch(selected) or not SHA.fullmatch(new):
        raise ValueError("Invalid guarded push identity")
    if git("rev-parse", "HEAD") != new or new == selected:
        raise ValueError("Push must be a new literal HEAD")
    clean()
    if git("rev-list", "--parents", "-n", "1", new).split() != [new, selected]:
        raise ValueError("Bump commit must have exactly the selected SHA as its single direct parent")
    paths = git("diff-tree", "--no-commit-id", "--name-only", "-r", new).splitlines()
    if not paths or not set(paths).issubset({str(path) for path in version.SOURCE_PATHS}):
        raise ValueError("Bump commit must be nonempty and source-version-only")
    if not git("show", "-s", "--format=%B", new).endswith(TRAILER):
        raise ValueError("Bump commit requires the generated co-author trailer")
    version.inspect()


def guard(updates):
    ref = os.environ["BUMP_REF"]
    selected = os.environ["BUMP_OLD_SHA"]
    new = os.environ["BUMP_NEW_SHA"]
    commit_ready(ref, selected, new)
    rows = updates.splitlines()
    if len(rows) != 1:
        raise ValueError("Push guard requires exactly one advertised update, including no-op rejection")
    fields = rows[0].split()
    if len(fields) != 4:
        raise ValueError("Malformed push advertisement")
    local_ref, local_sha, remote_ref, remote_sha = fields
    if local_ref not in ("HEAD", new) or local_sha != new or remote_ref != ref or remote_sha != selected:
        raise ValueError("Advertised push does not match exact selected oldSHA/ref and literal newSHA/HEAD")


def run(ref, selected, mode):
    initial(ref, selected)
    new_version = version.bump(mode)
    git("add", "--", *(str(path) for path in version.SOURCE_PATHS))
    git("-c", "user.name=github-actions[bot]", "-c",
        "user.email=github-actions[bot]@users.noreply.github.com",
        "commit", "-m", f"chore: bump version to {new_version}", "-m", TRAILER)
    new = git("rev-parse", "HEAD")
    commit_ready(ref, selected, new)
    if advertised(ref) != selected:
        raise ValueError("Selected branch changed before push; commit retained for inspection")
    hook_ready()
    env = os.environ.copy()
    env.update(BUMP_REF=ref, BUMP_OLD_SHA=selected, BUMP_NEW_SHA=new)
    subprocess.run(["git", "-c", "push.followTags=false", "-c",
                    f"core.hooksPath={HOOK.parent.resolve()}", "push", "--no-follow-tags",
                    "origin", f"HEAD:{ref}"], env=env, check=True)
    if advertised(ref) != new:
        raise ValueError("Remote branch verification failed after push; inspect manually")
    print(f"Bumped source to {new_version}: {new}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard", action="store_true")
    parser.add_argument("--ref")
    parser.add_argument("--sha")
    parser.add_argument("--mode", choices=("patch", "minor", "major"))
    args = parser.parse_args()
    if args.guard:
        guard(sys.stdin.read())
    elif args.ref and args.sha and args.mode:
        run(args.ref, args.sha, args.mode)
    else:
        parser.error("--ref, --sha and --mode are required")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Bump stopped: {error}. No tag, release, or build was requested; inspect retained state.",
              file=sys.stderr)
        sys.exit(1)
