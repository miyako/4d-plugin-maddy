#!/usr/bin/env python3
"""Create-only releases; never update, retry, or remove remote objects."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

import version


TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ReleaseError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def numeric_id(value):
    return type(value) is int and value > 0


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


class GitHub:
    def __init__(self, repository):
        require(
            isinstance(repository, str)
            and REPOSITORY.fullmatch(repository) is not None
            and all(part not in (".", "..") for part in repository.split("/")),
            "GITHUB_REPOSITORY must be owner/repository",
        )
        self.repository = repository

    def api(self, method, endpoint, payload=None, asset=None):
        command = [
            "gh", "api", "--method", method, endpoint,
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28",
        ]
        data = None
        if asset is not None:
            command += ["-H", "Content-Type: application/octet-stream", "--input", str(asset)]
        elif payload is not None:
            command += ["--input", "-"]
            data = json.dumps(payload)
        try:
            result = subprocess.run(
                command, input=data, text=True, capture_output=True, check=False,
            )
        except (OSError, UnicodeError) as exc:
            raise ReleaseError(f"{method} {endpoint}: could not run gh: {exc}") from exc
        require(result.returncode == 0,
                f"{method} {endpoint}: gh failed: {result.stderr.strip()}")
        try:
            return json.loads(result.stdout, object_pairs_hook=json_object,
                              parse_constant=invalid_json_constant)
        except (ValueError, TypeError) as exc:
            raise ReleaseError(f"{method} {endpoint}: malformed JSON response") from exc


def validate_tag(tag):
    require(isinstance(tag, str) and TAG.fullmatch(tag) is not None,
            "tag must be canonical vX.Y.Z (no leading zeros or suffixes)")


def validate_context(tag, sha):
    validate_tag(tag)
    require(valid_sha(sha), "selected SHA must be a full lowercase 40-character commit SHA")
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False,
        )
        require(head.returncode == 0, f"cannot resolve checkout HEAD: {head.stderr.strip()}")
        require(head.stdout.strip() == sha, "checkout HEAD does not match selected SHA")
        require("v" + version.inspect()[0] == tag, "source version does not match selected tag")
        unchanged = subprocess.run(
            ["git", "diff", "--exit-code", sha, "--", *map(str, version.SOURCE_PATHS)],
            text=True, capture_output=True, check=False,
        )
        require(unchanged.returncode == 0,
                "source version metadata differs from selected SHA or git diff failed: "
                + unchanged.stderr.strip())
    except (OSError, ValueError, SyntaxError) as exc:
        raise ReleaseError(f"cannot validate release context: {exc}") from exc


def validate_ref(entry):
    require(isinstance(entry, dict), "malformed tag ref entry")
    ref = entry.get("ref")
    obj = entry.get("object")
    require(isinstance(ref, str) and ref.startswith("refs/tags/") and len(ref) > 10,
            "malformed tag ref name")
    require(isinstance(obj, dict) and valid_sha(obj.get("sha"))
            and obj.get("type") in ("commit", "tag"), "malformed tag ref object")
    return ref, obj["sha"], obj["type"]


def check_available(api, tag):
    validate_tag(tag)
    occupied_release = False
    release_ids = set()
    page = 1
    while True:
        releases = api.api(
            "GET", f"repos/{api.repository}/releases?per_page=100&page={page}",
        )
        require(isinstance(releases, list), f"malformed releases page {page}: expected list")
        for release in releases:
            require(isinstance(release, dict) and numeric_id(release.get("id"))
                    and isinstance(release.get("tag_name"), str) and bool(release["tag_name"])
                    and type(release.get("draft")) is bool
                    and type(release.get("prerelease")) is bool,
                    f"malformed release entry on page {page}")
            require(release["id"] not in release_ids,
                    f"malformed releases page {page}: duplicate release ID {release['id']}")
            release_ids.add(release["id"])
            if release["tag_name"] == tag:
                occupied_release = True
        if not releases:
            break
        page += 1

    refs = api.api(
        "GET", f"repos/{api.repository}/git/matching-refs/tags/{quote(tag, safe='')}",
    )
    require(isinstance(refs, list), "malformed matching refs: expected list")
    occupied_tag = False
    ref_names = set()
    for entry in refs:
        ref, _, _ = validate_ref(entry)
        require(ref.startswith(f"refs/tags/{tag}"), "unexpected matching ref prefix")
        require(ref not in ref_names, f"malformed matching refs: duplicate ref name {ref}")
        ref_names.add(ref)
        if ref == f"refs/tags/{tag}":
            occupied_tag = True
    conflicts = []
    if occupied_release:
        conflicts.append("release (including drafts)")
    if occupied_tag:
        conflicts.append("tag")
    require(not conflicts, f"{tag} already has: {', '.join(conflicts)}")
    return release_ids


def validate_assets(tag, assets):
    expected = [f"maddy-{tag[1:]}.zip", f"maddy-{tag[1:]}.dmg"]
    require([str(asset) for asset in assets] == expected,
            "release assets must be exactly these ordered unqualified paths: "
            + ", ".join(expected))
    paths = []
    names = set()
    for asset in assets:
        path = Path(asset)
        require(path.name not in names, f"duplicate asset filename: {path.name}")
        names.add(path.name)
        try:
            require(not path.is_symlink() and path.is_file() and path.stat().st_size > 0,
                    f"asset must be an existing nonempty file: {path}")
            with path.open("rb"):
                pass
        except OSError as exc:
            raise ReleaseError(f"cannot read asset {path}: {exc}") from exc
        paths.append(path)
    return paths


def publish(api, tag, sha, assets):
    validate_tag(tag)
    require(valid_sha(sha), "sha must be a full lowercase 40-character commit SHA")
    paths = validate_assets(tag, assets)
    # This is the final remote availability check, after every local preflight.
    existing_ids = check_available(api, tag)
    tag_attempted = False
    tag_created = False
    release_attempted = False
    release_id = None
    upload_attempted = None
    uploaded = []
    try:
        tag_attempted = True
        ref = api.api("POST", f"repos/{api.repository}/git/refs",
                      {"ref": f"refs/tags/{tag}", "sha": sha})
        require(validate_ref(ref) == (f"refs/tags/{tag}", sha, "commit"),
                "created tag response does not match selected tag/commit")
        tag_created = True
        remote = api.api("GET", f"repos/{api.repository}/git/ref/tags/{tag}")
        require(validate_ref(remote) == (f"refs/tags/{tag}", sha, "commit"),
                "remote tag does not match selected tag/commit")

        release_attempted = True
        release = api.api("POST", f"repos/{api.repository}/releases", {
            "tag_name": tag,
            "name": tag,
            "target_commitish": sha,
            "draft": False,
            "prerelease": False,
            "generate_release_notes": True,
            "body": f"Release {tag}, built from commit `{sha}`.\n\n"
                    "The attached archives contain the release build for this commit.",
        })
        if isinstance(release, dict) and numeric_id(release.get("id")):
            release_id = release["id"]
        require(release_id is not None, "malformed new release ID")
        require(release_id not in existing_ids, "new release response reused an existing release ID")
        require(release.get("tag_name") == tag and release.get("name") == tag
                and release.get("target_commitish") == sha
                and release.get("draft") is False and release.get("prerelease") is False,
                "new release response does not match requested public release")
        upload_url = f"https://uploads.github.com/repos/{api.repository}/releases/{release_id}/assets"
        require(release.get("upload_url") == upload_url + "{?name,label}",
                "malformed new release upload URL")
        for path in paths:
            upload_attempted = path.name
            asset = api.api("POST", f"{upload_url}?name={quote(path.name, safe='')}", asset=path)
            require(isinstance(asset, dict) and numeric_id(asset.get("id"))
                    and asset.get("name") == path.name and asset.get("state") == "uploaded"
                    and type(asset.get("size")) is int and asset["size"] == path.stat().st_size,
                    f"malformed upload response for {path.name}")
            uploaded.append(path.name)
            upload_attempted = None
    except (ReleaseError, OSError) as exc:
        retained = [
            f"tag {tag} retained" if tag_created else
            f"tag {tag} creation may have succeeded" if tag_attempted else "tag not attempted",
            f"release ID {release_id} retained" if release_id is not None else
            "release creation may have succeeded; ID unknown" if release_attempted else
            "release not attempted",
            f"uploaded assets retained: {', '.join(uploaded) if uploaded else '(none confirmed)'}",
        ]
        if upload_attempted is not None:
            retained.append(f"upload of {upload_attempted} may have succeeded")
        raise ReleaseError(f"{exc}. Manual recovery required: {'; '.join(retained)}. "
                           "No retry, rollback, update, or deletion was performed.") from exc
    return release_id, uploaded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--tag", required=True)
    create = commands.add_parser("publish")
    create.add_argument("--tag", required=True)
    create.add_argument("--sha", required=True)
    create.add_argument("--asset", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        require(any(os.environ.get(name, "").strip() for name in ("GH_TOKEN", "GITHUB_TOKEN")),
                "explicit nonempty GH_TOKEN or GITHUB_TOKEN is required")
        api = GitHub(os.environ.get("GITHUB_REPOSITORY"))
        selected_sha = os.environ.get("GITHUB_SHA")
        require(valid_sha(selected_sha),
                "GITHUB_SHA must be a full lowercase 40-character commit SHA")
        if args.command == "publish":
            require(args.sha == selected_sha, "--sha must match GITHUB_SHA")
        validate_context(args.tag, selected_sha)
        if args.command == "check":
            check_available(api, args.tag)
            print(f"{args.tag} is available")
        else:
            release_id, assets = publish(api, args.tag, args.sha, args.asset)
            print(f"Published {args.tag}: release ID {release_id}; assets: {', '.join(assets)}")
    except (ReleaseError, OSError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
