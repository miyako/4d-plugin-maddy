# Native string ownership audit

## Immutable scope and provenance

Baseline: `84356d98842ff8d8cca8ed697e0b0fe416a44016` in
`miyako/4d-plugin-maddy`. The bundled SDK implementation is Git blob
`62f745e91d81ef2c3c901c656ea8e175d3b173e8` at
`4D Plugin API/4DPluginAPI.c`; its most recent introducing commit is
`c2b8b883af57267410fd38a0b8baa0990e39a205`. Header blob:
`528bf1d2552df08d6a8b1bc95011a9b72934ad5c`.
These identify local bundled bytes, not a verified upstream SDK release.
The SDK implementation and all SDK headers remain unchanged.

Guidance: `miyako/skills`, `4d-skills/skills/4dplugin/SKILL.md` at
`e6b492fcb81e5e1a7f7db723594b420e63bf6eb9`. Harness design references, fetched
by GitHub tree/contents without another checkout:
`miyako/4d-plugin-jsmin` at `d12035643f874ec57ae634194ec0f1206b05a5f2`,
`tests/ownership/{host.mm,run.py,README.md}`; and
`miyako/4d-plugin-jpegoptim` at `e49adcd32bd5500cf3de680faa3649b32463ff5e`,
`.github/tests/{ownership_host.mm,test_ownership.py}`.
The target SDK contracts were inspected independently; other repositories'
results are not substituted for this target's execution.

## Complete native/build inventory

| Tracked surface | Actual reachability and ownership conclusion |
|---|---|
| `4DPlugin-maddy.{cpp,h}` | Both Xcode and MSBuild compile the dispatcher. SDK-exported `FourDPackex` calls `PluginMain`; selector 1 calls `_maddy`. Input text is copied into `C_TEXT`, then UTF-8; options are borrowed. `ob_is_defined`/`ob_get_b` read parser flags. Generated HTML is returned using the content setter, not descriptor transfer. Exceptions now have a stderr diagnostic instead of being silently swallowed. |
| `support/4DPlugin-JSON.{cpp,h}` | Both builds compile the whole helper translation unit. All helpers, including helpers unused by `_maddy`, were inventoried: `json_wconv`; `ob_set_p`; both `ob_set_s`, `ob_set_a`, `ob_set_o`, `ob_set_n`, `ob_set_0` overloads; `ob_set_c`, `ob_set_i`, `ob_set_b`; `ob_is_defined`; `ob_get_a/b/n/c/o`; `ob_stringify`. String/picture setters now avoid creating then overwriting an empty owner. Scalar property results and all kind mismatches are cleared; matching object/collection results retain the existing owning handoff. Failed getters are checked before kind/output access or cleanup. Stringify saves command error before argument cleanup, rejects failed raw-kind-zero results, and clears successful owning returns. |
| `4D Plugin API/Classes/C_TEXT.{cpp,h}` | Both builds compile this wrapper. All parameter, return, UTF-8/UTF-16, Objective-C string/path/URL, replacement and destructor helpers inspected. Inputs copy borrowed bytes; output uses `PA_SetUnistring` content setting. CF/Objective-C owning copies are released by corresponding consumers. Copy constructor/assignment already deleted. No transfer/dispose fix needed here. |
| `4D Plugin API/Classes/ARRAY_TEXT.{cpp,h}` | Tracked but not compiled or included by either target. All conversion, indexed copy/set/append, parameter, path/URL, size and destruction helpers inspected. Normal output allocates a separate string per element and does not dispose after transfer. Input snapshots borrowed descriptors into independent C++ strings before any destination cleanup. Initial creation and resize OOM are explicitly deferred below. |
| `4D Plugin API/Classes/C_BLOB.{cpp,h}` | Tracked but not compiled by either target. `CBytes` owns bytes; `C_BLOB` forwards to it. Hex/base64 helpers copy through `C_TEXT`; no `PA_Unistring` transfer. Non-string allocation failures, handle replacement, zero-sized vector operations and implicit wrapper copying are outside this bounded string fix and are not certified. |
| `4D Plugin API/Classes/C_LONGINT.{cpp,h}`, `C_REAL.{cpp,h}` | Tracked, dormant, direct scalar parameter/result copies; no string owner. Read implementations and declarations, not just signatures. |
| `4D Plugin API/Classes/C_TYPES.h` | Shared UTF-16/UTF-8 string/vector and parameter typedefs plus platform includes; no ownership operation. |
| `4D Plugin API/{4DPluginAPI.c,4DPluginAPI.h,PrivateTypes.h,PublicTypes.h,EntryPoints.h,Flags.h,4DPluginAPI.def}` | Complete bundled engine boundary is compiled by the regression, not copied/reimplemented SDK functions. Headers define packing, variables, callbacks and selectors. The Windows definition file exports `FourDPackex`. SDK-only ownership ambiguities remain unchanged. |
| `manifest.json`, `constants.json`, bundled resource `manifest.json`, `Info.plist`, bundled `Info.plist` | One exported scalar command `maddy(&T;&J):T`, selector 1; constants have no themes. Scalar-only manifest is not evidence that shared helpers are safe. No array command/callback entry is hidden by the manifest. |
| `maddy.vcxproj`, `.filters`, `.user`, `.sln`; `maddy.xcodeproj/project.pbxproj`, shared scheme and workspace metadata | Both actual source lists contain entry, JSON helper, `C_TEXT` and SDK only. Includes reach Maddy parser headers; JSON headers/static archive are tracked dependencies, not first-party ownership implementation. Xcode's Deployment script signs, packages and notarizes: not executed. The nested `maddy/maddy.xcodeproj` contains workspace metadata, not another native source target. |
| `include/maddy/*.h`, `include/json/*.h`, `a/libjsoncpp.a` | Third-party/parser surfaces, excluded from production fixes. Parser paragraph/block output was read only to derive exact synthetic output assertions. No parser algorithm changed. |
| `.github/workflows/{bump-version.yml,release.yml}`, scripts, hook, `.github/README.md` | Existing Bump and Build workflows are manual-dispatch only; no dispatch performed. Pre-push guard is source-only. Existing dynamic Pages workflow uses legacy `main:/`; merging to main requires separate authorization for that publication trigger. New `ownership.yml` runs only unsigned synthetic compilation on `macos-latest`, with read-only contents permission, no upload, release, signing, packaging or dependency installation. |
| `test/Project/Sources/Methods/test_1.4dm` and tracked bundle binaries/resources, root `.dmg`/`.zip`, site assets | Not native ownership implementations and not executed or modified. No private document, browser, UI, script output, device or actual 4D process used. |

## Contracts verified in this bundled implementation

Line references here refer to the unchanged baseline SDK implementation.

| Operation | Exact contract used |
|---|---|
| `PA_CreateUnistring` / `PA_DisposeUnistring`, 265-287 | Engine creates a descriptor; dispose passes it to engine then zeros the local descriptor. Creation failure output is not initialized by the SDK wrapper: do not infer a valid descriptor on failure. |
| `PA_SetUnistring`, 289-299; `PA_ReturnString`, 2600-2603 | Engine content setter receives the existing descriptor and a UTF-16 buffer; distinct from array transfer. Synthetic engine copies content; result survives local wrapper destruction. Actual host behavior is not independently certified here. |
| `PA_CreateVariable`, 3920-4070 | Unistring creates an empty owned string; picture creates an empty picture. Object is null. Undefined has no allocated payload. |
| `PA_SetStringVariable`, 4254-4259; `PA_SetPictureVariable`, 4280-4285 | Type/descriptor shallow assignment, not a copy or disposal of previous owner. Use Undefined before adopting an existing owner. The property setter's synchronous consumer copies/retains its property; local variable cleanup remains necessary. |
| `PA_GetStringVariable`, 4107-4121 | Engine-backed conversion takes a variable and returns a descriptor. The tested matching-string path aliases its live variable; copy content before clearing the successful property result. Conversion of other kinds and failure allocation semantics are not certified by the shim. |
| `PA_GetObjectProperty`, 636-651; `PA_GetCollectionElement`, 586-601 | Explicit SDK comment says returned variable should be cleared after use. On error the local output is unspecified. Check error before kind/output/adoption/clear. All first-party property getter sites handled; no first-party collection-element calls exist. |
| Object/collection variable getters/setters, 566-575, 4214-4225, 4329-4341 | Descriptor/reference aliases. Matching helper returns transfer the owning getter reference to their caller; `PA_DisposeObject`/`PA_DisposeCollection` eventually clear it. Do not clear then relabel a borrowed reference. |
| `PA_GetStringInArray`, 4738-4755 | Borrowed descriptor; do not dispose it. Snapshot before mutation/shrink/clear. |
| `PA_SetStringInArray`, 4933-4947 | Accepts only Unicode array, non-null handle, `0 <= index <= count`, non-null supplied descriptor pointer. Disposes old slot, then shallow-assigns supplied descriptor. Index zero is accepted. Rejected calls leave input owned by caller. |
| `PA_ResizeArray`, 4428-4603 | Shrink disposes removed strings; growth initializes new storage. Count is updated even if handle resize fails. Ordinary successful arrays cannot certify this OOM path. |
| `PA_ExecuteCommandByID`, 5778-5811 | Failed parameter-handle allocation returns a zero-initialized variable with raw kind 0, not Undefined. `UnifyVarKind` maps raw 0 to string. Save error before any cleanup overwrites SDK error; never convert/clear failed result. Successful owning stringify results are cleared after copying. |
| `PA_HasObjectProperty`, 681-691; `PA_DuplicateObject`, 554-564 | Synchronous calls alias borrowed argument references. Clearing these borrowed aliases would release caller owners. A successful duplicate returns an owner which stringify clears independently. SDK-internal failed duplicate handling cannot be changed in this audit. |
| `PA_JsonParse`, 711-731 | Synchronously aliases its input; SDK `PA_CreateObject`'s legacy JSON-parse path explicitly disposes its own string after the command. No first-party JSON-parse call or owning input to repair here. |

## Regression and immutable differential

Run `python3 .github/tests/test_ownership.py` on macOS. No package install is
needed. It compiles actual entry/helper/wrapper translation units (including all
dormant wrapper `.cpp` files), the complete unchanged SDK implementation, and a
strict synthetic engine in `ownership_host.mm`. Only the engine callback is
substituted. `OWNERSHIP_SOURCE_ROOT` selects an immutable `git archive` extraction
for an identical baseline run; SDK Git blob identity is checked before compiling.
ASan and UBSan run with alignment checking disabled because the bundled SDK uses
packed structures. macOS LeakSanitizer is unavailable; explicit string, handle,
object/collection reference and picture counters must all end at zero.

The matrix uses ASCII, BMP Omega, supplementary emoji (a surrogate pair), and
empty text, each with allocated and null empty descriptors. It asserts exact
UTF-8 bytes, UTF-16 code units, allocation capacity and terminator boundaries;
successful property copies, kind mismatches and owning handoff survival;
poisoned failed getter/command outputs; raw-kind-zero handle failure; independent
argument cleanup; all string-setter overloads and picture empty-owner control;
array independent allocation, borrowed snapshot, replacement, shrink, clear,
accepted slot zero and every rejection predicate; scalar content setters and
the actual exported dispatcher, with and without options. Generated HTML is
compared only as text and is never executed.

Identical 29-case x 4-fixture x 2-empty-mode differential (232 executions):
baseline **138 failed / 94 passed**; fixed sources **232 passed / 0 failed**.
Exact baseline failures:

| Named cases | Failures per case |
|---|---:|
| `get-string`, `mismatch-bool`, `mismatch-number`, `mismatch-object`, `mismatch-collection`, `plugin-options` | 7 |
| `failed-string`, `failed-bool`, `failed-number`, `failed-object`, `failed-collection` | 8 |
| `set-narrow`, `set-wide`, `set-utf16`, `set-wchar` | 4 |
| `set-picture` | 8 |
| `stringify-command-failure`, `stringify-handle-failure`, `stringify-duplicate-failure`, `stringify-nonstring` | 8 |

Allocated empty strings expose the otherwise hidden initial-owner overwrite;
null empty strings do not. Successful scalar-only getters, matching owning
handoffs, array and content-setter controls continue to pass. Baseline poisoned
outputs fail on wrong-kind use, missing explicit failure reporting or invalid
sentinel conversion; none is silently accepted as a passing case.

The default matrix excludes deliberately deferred failure probes. To reproduce
the unchanged initial-array-create boundary:
`OWNERSHIP_CASES=deferred-array-create python3 .github/tests/test_ownership.py`.
This is expected to fail with
`DEFERRED: initial array creation publishes success-shaped null descriptor`,
not to pass silently or to count toward fixed-path coverage.

## Deferred boundaries and platform limits

- Dormant `ARRAY_TEXT::toParamAtIndex` has a void API and no established
  allocation-failure channel. Failed initial creation changes the destination
  type and can publish a null array descriptor instead of preserving original
  bytes; the dedicated probe reproduces this unchanged. SDK resize OOM can
  corrupt the recorded count. Neither is fixed or certified by successful array
  tests; do not activate this helper for untrusted allocation-failure paths.
- Initial `PA_CreateUnistring` failures in legacy void JSON setters/key creation
  lack initialized SDK output and a transactional ownership boundary. These
  pre-existing paths, setter failure propagation, conversion OOM/invalid-text
  handling and arbitrary C++ allocation exceptions require a separate checked
  API design; this fix does not claim their safety or provide silent fallbacks.
  The new getter/stringify exceptions preserve caller output and the dispatcher
  logs native exceptions, but are not a new 4D-visible status-result API.
- SDK-only application-path strings, picture-data strings, pointer metadata,
  generic variable-to-string conversions, callback retention beyond synchronous
  calls, and non-string/CBytes allocation failures remain unchanged and outside
  the certified runtime matrix. There is no `CPicture` class in this repository.
  Dormant wrapper implicit copying is not exercised by production calls.
- No Windows build, Windows runtime, real 4D version, signing, notarization,
  release, packaging, UI or browser execution is claimed. Synthetic engine
  contracts are bounded evidence, not proof of the original Poppler Windows
  crash or universal host allocation behavior.
