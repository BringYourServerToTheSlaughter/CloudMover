# Grouped metadata proof and completion UX — September 7, 2026

This pass performed no live Proton migration or remote mutation and made no commit. The supplied pasted log is a tail fragment; totals below use the complete local JSONL records for the same migration, `f4387f9d-508e-44bd-88c7-52e5b23cf24a`. Existing verified test trees are untouched.

## Live result and remaining command graph

13 files, 50,828,655 logical bytes. Upload wall: **210.710 seconds**. Post-upload batch listing: **2.078 seconds**. Relative to the prior 377-second upload pipeline, this is 166.29 seconds saved, 44.1% less time, 1.789× faster. The earlier instrumented 13-file baseline had a 3-second batch verification; the 17-second verification belonged to the older 14-file run. Neither discrepancy changes the measured upload improvement.

The existing batched implementation launched **63 CLI processes** over the complete bulk cycle, **54** inside upload. This time these counts are measured from `cli_process` events, not inferred. No Proton processes are used by local cache release.

| Upload command type | Calls | Cumulative seconds | Average seconds | % of upload wall |
|---|---:|---:|---:|---:|
| filesystem upload | 1 | 27.750 | 27.750 | 13.17% |
| filesystem list | 14 | 43.713 | 3.122 | 20.75% |
| filesystem rename | 13 | 52.672 | 4.052 | 25.00% |
| filesystem info | 26 | 86.213 | 3.316 | 40.92% |
| Outside CLI calls | — | 0.362 | — | 0.17% |

Upload-command time is not pure wire transfer time: it includes process/SDK initialization, API, cryptography and waiting. We cannot separately measure process startup or API-only time from these events; the helper reports them as unavailable rather than inventing a split. Metadata/promotion commands account for 86.66% of pipeline wall time. Effective logical throughput is 0.2412 decimal MB/s, not a byte-counter network rate.

Outside upload: three infos (6.740 seconds), two creates (5.961 seconds), four lists (13.370 seconds, including the 2.078-second final batch verification). The full cycle has 63 processes totaling 236.419 CLI seconds. Two nonzero exits are expected missing-node probes during new-folder setup, not actionable transfer failures.

## Implemented command reduction

| Phase, 13 files directly inside new Pics/new destination | Before | After, full metadata listings |
|---|---:|---:|
| Claim new exact migration destination | 4 | 4 |
| Ensure new Pics folder | 4 | 4 |
| Initial final/temp-name occupancy preflight | 1 | 1 |
| Batch temp upload | 1 | 1 |
| Temp proof | 13 info | 1 list |
| Immediate final-name collision checks | 13 list | 13 list |
| Serial rename/promote | 13 rename | 13 rename |
| Post-promote final proof | 13 info | 1 list |
| Existing subsequent batch verification | 1 list | 1 list |
| Remote cleanup on success / local release | 0 | 0 |
| **Upload pipeline** | **54** | **30** |
| **Complete bulk cycle** | **63** | **39** |

Every multi-file group now uses `2N + 4` provider commands when listings include all proof fields, versus `4N + 2`. A strict argv-driven fresh 13-file migration test verifies the 39 total. Overall commands/file drop from 4.846 to 3.000; upload commands/file from 4.154 to 2.308. Single-file groups keep the existing seven-command safe provider. Nested hierarchies, multiple groups, resumes, retries, cleanup and metadata fallbacks add commands.

The [official list implementation](https://github.com/ProtonDriveApps/sdk/blob/main/cli/src/commands/fileSystem/commandFileSystemList.ts) emits folder-child entities through its JSON iterable. CloudMover already parses the name, UID, revision size and SHA-1 fields from those entities. A fresh temp listing now proves every expected temporary child; a fresh final listing after all renames proves every expected final child against its retained temp UID. Each proof checks an unambiguous exact child name, size and digest, unique UID, and unchanged parent UID when known. Names are interpreted only within the queried parent, never flattened across folders.

If a listing entry lacks required proof metadata, only that entry gets an exact-info fallback. Contradictory metadata, missing names, duplicate names/UIDs or changed identities fail closed; an info fallback does not override a contradiction. A size still unavailable after fallback fails rather than silently accepting incomplete proof. Failures include the exact remote path. CLI exit zero is never sufficient.

No row becomes verified/COMPLETE until all grouped final proofs and the unchanged downstream batch verification succeed. A later failure can leave earlier names promoted; those nodes remain preserved, and existing recovery refuses to overwrite them. Known promoted nodes are excluded from temp cleanup. Remaining proven temp nodes still require the existing UID/name/parent/digest recheck before trash; unknown nodes remain isolated. Interrupts retain the existing cooperative worker/signal and recovery behavior. Production concurrency remains **one**. The 32-file / 32 KB transport limit and approximately 250 MB / 5000-file scheduler bounds remain unchanged.

## Safety and race model

The temp snapshot is used as positive content/identity evidence. It is **not** used as a cached absence result for the promotion loop. For each final name, CloudMover performs a fresh list immediately before that file's rename. No other CloudMover remote call intervenes between the check and the mutation.

- An occupied name at initial preflight stops before transport.
- A competing name appearing during upload or between promotions is caught by that file's fresh collision check.
- A competitor can still act between the last check and rename. Client-side list-plus-rename is not a transaction. The [SDK rename contract](https://github.com/ProtonDriveApps/sdk/blob/main/client/js/src/protonDriveClient.ts) specifies a validation error for an existing same-name node; CloudMover propagates rename failure, with no replace/merge/retry fallback. Tests inject a collision immediately before the fake provider's rename and verify the rejection leaves the competitor untouched. These tests validate CloudMover's behavior under that provider contract; they are not a new live server atomicity proof.
- A final node disappearing/changing after promotion is caught by grouped final UID/content proof. A subsequent batch check also remains. No remote observation can guarantee that another authorized actor will never change a file after verification; this existing limitation has not been changed.

We retain the existing immediate checks instead of implying that one earlier snapshot closes the race. Grouping final proofs delays error discovery until the end of a small group, but it never makes unproved files releasable. Incomplete groups keep the previous conservative resume behavior and may need attention.

## UID promotion and narrower checks: investigated, deferred

The [CLI rename implementation](https://github.com/ProtonDriveApps/sdk/blob/main/cli/src/commands/fileSystem/commandFileSystemRename.ts) resolves its path to a node and calls SDK rename. Existing CloudMover cleanup demonstrates the provider's UID-addressing convention. However, addressing rename by the temp UID does not remove a required subprocess here: a current final-name absence check and final name/content proof are still necessary. A UID does not answer whether some other node now occupies the desired final name.

More significantly, a top-level UID address follows a node if another actor moves it out of the intended parent; the CLI offers no caller-supplied expected-parent condition. This pass keeps the parent-scoped temp path, retains its UID as proof, and does not introduce a new moved-node mutation case for an unmeasured path-resolution speed gain. Direct UID rename can be revisited with an explicit conditional-parent contract or sufficient provider evidence.

A targeted `info final-path` could probe absence, but it still requires one process and resolving the path. An absent ancestor can produce missing-node output too, while a parent listing demonstrates the intended parent is enumerable. The live info calls were not cheaper on average than lists, and there is no controlled evidence that absent-path info improves this case. Full fresh lists are retained. A single preflight followed by an unchecked serial rename loop was rejected outright. Production concurrency and persistent interactive sessions remain deferred.

## Estimated speed, not a new live result

The removed 26 info commands consumed 86.213 seconds. If both replacement listings cost the observed batch-listing time of 2.078 seconds, the predicted savings are **82.057 seconds**, giving **128.653 seconds** (~2m09s), approximately **38.9% less upload wall time** than 210.710 seconds. Using the observed upload-list average of 3.122 seconds instead gives ~130.74 seconds. Treat ~129–131 seconds as a timing-based scenario, not a guaranteed runtime: temp/final listing sizes, service latency, SDK caching, incomplete metadata fallbacks and provider throttling can change it. No live benchmark was performed.

After this change the remaining measured-cost components are primarily 13 renames, 13 immediate collision lists, and the shared upload itself. Further savings need a better provider contract, cheaper in-process operations or a separately authorized controlled concurrency experiment, not weaker proofs.

## Developer report and UX

`python -m src.cloudmover.performance LOG --migration-id ID` now includes command-family count/total/average and upload-wall percentage, separate all-cycle/upload/post-batch scopes, commands/file, effective logical MB/s, upload-command versus metadata shares, and outside-CLI time. Startup/API-only figures remain null. Read-only report for this live run: [performance-live-batched.json](performance-live-batched.json). No raw diagnostics or fabricated rates were added to normal UI.

The completion report now uses a custom dark dialog with a green check, heading, copied-and-verified count, decimal MB transferred, human-readable destination, originals-in-iCloud reminder, and Done. It uses layout widgets and plain text, fixing the literal `\\n` bug and avoiding accidental HTML interpretation in folder names. Cleanup warnings get a distinct amber completed-with-warnings variant. A non-complete or incompletely verified state displays migration progress, never success. The wording says copied rather than implying that iCloud originals were moved away.

Route and tree naming from the previous pass were already correct in this source checkout; actual Qt tests reconfirm `SyncMeBro-2`. Scan title/button/activity now use that same selected folder name, and initial route text no longer briefly claims iCloud Drive before loading the saved plan. Selection wording stays `13 files selected`, separate from `0 / 13 files verified` and `5 / 13 files verified`. Existing internet/upload-speed, small-file-count and provider/API caveats remain.

Both success and warning dialogs were rendered offscreen and visually inspected. Their Done buttons are exercised in GUI tests. No broad redesign or database/schema/hydration/release/destination changes were made.

## Changed files and validation

Runtime: `src/cloudmover/providers/proton.py`, `src/cloudmover/performance.py`, `src/cloudmover/gui.py`.
Tests: new `tests/test_grouped_proof_ux.py`; updated `tests/test_transport_performance.py`, `tests/test_destination_regression.py`, `tests/test_first_run_safety.py`, `tests/test_gui.py`, `tests/test_completion_workflow.py`.
Developer artifacts: this report and `docs/performance-live-batched.json`.

Validation: **121 focused tests passed; 245 full-suite tests passed** (229 baseline + 16 new cases). The build script reran all 245 successfully. Rebuilt `/Users/gabem/GitHub/cloudmover/dist/CloudMover.app`; `codesign --verify --deep --strict` passed. Source version, bundled Python version, CFBundleShortVersionString and CFBundleVersion all equal **0.19.6**. Earlier requested uncommitted changes are preserved. No commit or live migration.
