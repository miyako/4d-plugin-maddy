# Manual source versioning and releases

`Bump` and `Build` are independent manual workflows. Neither has a push, tag,
pull-request, or workflow-run trigger. Run either from Actions using GitHub's
built-in ref selector. `Build` has **no custom inputs**.

## Source authority

The selected commit's `maddy.xcodeproj/project.pbxproj` is authoritative.
`MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` are coordinated canonical
`X.Y.Z` versions across all six configurations; a CURRENT reference to MARKETING
is preserved. Missing or extra real assignments are rejected. `Info.plist` must contain
`CFBundleShortVersionString`; only `CFBundleVersion` is optional. Present
literal versions must agree. Known `$(MARKETING_VERSION)` and
`$(CURRENT_PROJECT_VERSION)` plist references remain references (both `$()` and
`${}` syntax are supported); absent optional
build keys remain absent. The source `English.lproj/InfoPlist.strings` short
version and the version in its information string are validated and co-bumped.
Do not treat committed bundles, dependencies, 4D database/project metadata, or
schema counters as source-version authorities.
In particular, manifest command ID `20000`, XLIFF schema version `1.0`, and
4D settings stamp `2` are not plugin semantic versions.

The original committed Bump header at `c2b8b88` explicitly prescribed coordinated
MARKETING/CURRENT/plist updates, but its body selected MARKETING first and left
CURRENT behind. The automated commits `b35ccbc`, `8386dbc`, and `0886a64` moved
MARKETING and the source plist from 1.0.0 through 1.0.3. All six CURRENT values
and source localized strings remained at their original 1.0.0. This migration
repairs **only** those proven stale source fields to the existing **1.0.3**
authority. It does not increment intake. The missing source `CFBundleVersion`
stays absent, and the committed prebuilt bundle under `test/Plugins` is untouched.
`git log --follow -- .github/workflows/bump-version.yml` reaches its creation at
`c2b8b88`; there is no earlier renamed workflow overriding this contract.

`version.py` validates all inputs before edits. Its lexical scanner rejects
quoted or unquoted duplicate assignments, including assignments following other
assignments on the same line, while ignoring comment and string noise. Rewrites
use real token spans and preserve BOM/encoding (including UTF-16), each individual
line ending, file modes, references, and unrelated bytes.

## Bump

Select a branch and patch/minor/major. Checkout uses the immutable selected
`github.sha`, not a moving branch name. The helper requires a completely clean
checkout and an exact match to the selected remote branch SHA. It produces one
nonempty source-only commit with exactly that SHA as its single direct parent,
including:

```
Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
```

The mandatory executable `.github/hooks/pre-push` is checked initially and late,
then installed for this push using `core.hooksPath`. It validates Git's actual
advertisement, not merely an earlier `ls-remote` read: exactly one update,
literal new SHA/HEAD, exact selected old SHA and branch ref. Advancement, rewind,
deletion, multiple updates, and same-result zero-update pushes fail closed.
The push is nonforce with both `push.followTags=false` and `--no-follow-tags`.
No tag, release, build, or dispatch is performed. Failures retain any local
source edits/commit for inspection; there is no amend or automatic retry.

## Build

The selected SHA can come from a branch or tag; no branch gate is added. A
constant concurrency group serializes Builds across refs without cancellation.
An authenticated preflight reads **all** release pages, including drafts, and
requires exact tag absence before native work. Errors, including API 404, auth,
network, malformed data, and later-page failures, stop the workflow.

Native topology remains Windows Release x64 (v143, Windows SDK 10.0), then macOS
target `maddy`, configuration `Default`, `arm64 x86_64`, with the existing 10.13
deployment override. Project frameworks, resource phases, dependencies, and
secret names are unchanged. Paths intentionally use `test`, not `.4dbase`.
The Windows output is exactly the vcxproj OutDir:
`test/plugins/maddy.bundle/contents/windows64/maddy.4DX`.
No recursive search may substitute a stale binary.

Freshness cleanup removes only generated executable, plist/localized metadata,
signatures, and intermediates. It does not remove whole bundles, Resources,
QuickLook, Windows payloads during macOS cleanup, or Default sibling resources.
Explicit `CONFIGURATION_BUILD_DIR` resolves the canonical output to
`test/Plugins`, with selective Default-output cleanup. Required `lipo` tokens
must be exactly `arm64` and `x86_64`; additional architectures are not forbidden.
The built plist and actual `Contents/Resources/English.lproj/InfoPlist.strings`
destination must match the selected source, including optional build-key
absence. Built normal strings, XML, binary plists, and UTF-16 are checked.

Both signing and strict signature verification must succeed. Both ZIP and DMG
notarization commands must succeed **and** return JSON status `Accepted`.
DMG stapling must succeed. Only after all these steps does publication perform
another complete authenticated absence check.
Early and final CLI checks require immutable selected HEAD and unchanged source
version metadata matching the tag. They intentionally do **not** require a
globally clean worktree: native builds legitimately rewrite tracked generated
bundle files. Product-output dirt must not block final publication; source
metadata or selected-HEAD changes must.

Publication uses separate create-only operations:

1. POST a new `refs/tags/vX.Y.Z` at the selected SHA, then remotely verify it.
2. POST a new public, non-prerelease release with explicit tag, name, target,
   body, and generated release notes.
3. Upload each exact asset separately to the newly returned release ID.

The only accepted asset arguments, in order, are `maddy-X.Y.Z.zip` and
`maddy-X.Y.Z.dmg` for the selected version: unqualified, regular, nonempty files.
Absolute/directory aliases, symlinks, extra files, and reversed order fail closed.

There is no reuse, PATCH/update, delete, clobber, retry, rollback, or combined
release-and-assets operation. The final check does not claim atomicity across
GitHub API calls: a racing tag creation causes the create-only POST to fail,
and any already-created state remains visible for manual recovery.

## Manual recovery

Do not rerun Build blindly after partial publication. Inspect the error, exact
tag SHA, release ID (including drafts), and each uploaded asset. An API error
may mean a creation succeeded but its response was lost. The helper reports
confirmed and uncertain retained state and never removes it. A maintainer must
decide how to reconcile that state manually; a fresh run will reject occupied
tags/releases. Do not silently overwrite an existing version. Bump remains
available independently when a genuinely new source version is appropriate.

## Offline validation

Use Python 3, PyYAML, PowerShell (`pwsh`), and `actionlint`:

```sh
actionlint .github/workflows/*.yml
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/tests -v
```

Tests use disposable Git repositories and local bare origins, mocked API
responses, and actual extracted workflow shell/PowerShell with a closed PATH
containing native stubs. They must not call native build/sign/notary/4D tools,
read real secrets, dispatch Actions, or mutate production tags/releases.
The same complete suite must pass on live source and after a genuine guarded
helper bump in a disposable checkout, with clean local=bare SHA, direct parent,
nonempty source-only diff, trailer, and no pushed tags.

Pinned migration reference inputs (adapted to this repository, not copied as
project configuration):

| Repository | Commit |
|---|---|
| miyako/4d-plugin-native-encoding-detection | c42ca565857f674bde5ea6ed3435c87375eda4a7 |
| miyako/4d-plugin-file-information | 19848973601acfd3a2b5070230fa40cdc7e4edb2 |
| miyako/4d-plugin-common-string-algorithm | 2606a68201866ec9fcbb38b51462d79215f504ef |
| miyako/4d-plugin-window-button | 3f14facb9aaaaff0e22eeb8e07531163680c3a8a |
| miyako/4d-plugin-raw-picture-data | 8144537fdc8dcc3d0c053dfe97aff0a8c0c3a6f9 |
| miyako/4d-plugin-PRNG | 522f62e1e9f388be0f3f226c1d4c39362dad2491 |
