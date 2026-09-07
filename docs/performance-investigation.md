# CloudMover performance investigation — September 7, 2026

No live migration or remote mutation was performed for this investigation. Existing verified Proton trees were preserved. No commit was made.

## Measured live evidence

Read-only inspection of the existing JSONL log confirms the reported 14-file, 50,834,803-byte run: upload wall time 431 seconds, subsequent batch verification 17 seconds. That run predates per-file timing instrumentation, so its process durations cannot be reconstructed from the log.

The later instrumented successful run has 13 files (after .DS_Store exclusion), 50,828,655 logical bytes, 377 seconds upload wall time, and 3 seconds subsequent batch verification:

| Stage inside upload phase | Seconds |
|---|---:|
| Initial destination checks | 88.791 |
| Upload CLI (includes provider processing, not just wire transfer) | 103.702 |
| Temp metadata/digest verification | 46.449 |
| Immediate absence checks + rename | 95.889 |
| Final metadata/digest verification | 42.002 |
| Other time / timestamp rounding | ~0.167 |

Average safe-upload latency was 29.005 seconds. Effective logical throughput was 0.1348 decimal MB/s across the whole upload phase; this is not an observed network transfer rate. Metadata/safety operations consumed about 72.5% of upload wall time. These measurements establish the repeated-command bottleneck; they do not separate process startup, auth setup, cryptography, parent resolution, and network/API latency. No claim is made that the user's internet caused the poor speed.

## Exact command accounting

Before this pass, each file incurred:

1. Engine list of final parent to check final-name absence.
2. Provider list of final parent for the same final-name check.
3. Provider list of final parent to check UUID temp-name absence.
4. Upload one temp file.
5. Info temp file: name, size if available, digest, UID.
6. List final parent again immediately before promotion.
7. Rename temp to final basename.
8. Info final file: exact content proof and unchanged UID.

Thus 8 commands/file, 112 for 14 files, excluding folder setup and subsequent batch verification. The existing 250 MB / 5000-file scheduler batch was bookkeeping/staging batching; its transport was one process per file.

The executable regression fixture measures an entire fresh migration of 14 files directly inside `Pics`, under one new selected destination at `/my-files/SyncMeBro-2`:

| Component | Before | After |
|---|---:|---:|
| Claim exact new migration root (info missing, parent info, create, info UID) | 4 | 4 |
| Ensure new Pics folder (list missing, parent list, create, list) | 4 | 4 |
| Upload/proof path | 112 | 58 |
| Exact batch tree verification | 1 | 1 |
| **Total** | **121** | **67** |

This is a 54-command / 44.6% reduction, measured against strict argv-driven integration tests. It is an exact count for the stated hierarchy, not a universal count for every 14-file migration. Extra directories, multiple scheduler cycles, singleton parent groups, resume ownership checks, retries, failed cleanup, and incomplete tree-listing fallback change totals. The optional wizard preflight is separate (two read-only calls on a new destination). Historical logs did not record actual process counts, so 121 must not be presented as a measured count for the old live run.

For N siblings in one transport group of at least two files, the new provider uses `4N + 2` commands: one initial directory snapshot, one multi-file upload, N temp infos, N immediate lists, N renames, N final infos. Singleton groups retain the existing seven-command provider. Initial snapshots are never reused for pre-promote absence checks. Existing per-cycle folder caching is retained; no new long-lived metadata/UID cache was added.

## Proton argument contract and session findings

Installed binary reports `Proton Drive CLI cli-drive@0.8.0+06e8c605`, SDK `js@0.21.0+06e8c605`. Installed `filesystem upload --help` explicitly reports `localPath... parentPath`.

The [official upload implementation](https://github.com/ProtonDriveApps/sdk/blob/main/cli/src/commands/fileSystem/commandFileSystemUpload.ts) takes all positional arguments except the last as local sources, resolves the last argument as the common parent once, and queues the sources. The implemented argv is:

```python
[cli, 'filesystem', 'upload', '-f', 'rename', '-t', temp_A, temp_B, ..., remote_parent]
```

Every path is one argv element; there is no shell or quote concatenation. Temps retain unique UUID basenames, never directory-level UUIDs. Conflict strategy remains rename, never replace/merge; if a temp is renamed due to a collision, exact metadata proof fails closed.

The [official REPL](https://github.com/ProtonDriveApps/sdk/blob/main/cli/src/cli/repl.ts) initializes a session once and awaits commands serially. It accepts parsed text lines and prints a prompt, with no request IDs, result lengths, or structured command outcome. Recoverable errors go to text stderr and return to the prompt. JSON command output does not add a response envelope. Prompt parsing can therefore confuse data/progress with command boundaries. Killing a stuck session would leave the command outcome unknown; automatic replay could repeat a mutation. A reliable persistent adapter would require a structured upstream protocol plus metadata reconciliation, not just a process restart. It was rejected here; existing one-shot timeout/signal behavior is retained.

The [runner](https://github.com/ProtonDriveApps/sdk/blob/main/cli/src/cli/run.ts) creates/disposes a session for each one-shot operation. The [initializer](https://github.com/ProtonDriveApps/sdk/blob/main/cli/src/init.ts) sets up credentials/API/crypto/cache/SDK/path services. Reducing launches avoids repeating that setup and repeated upload-parent resolution. It does not imply re-login or wholly cold caches on every call; persisted caches can survive processes. Current public source was inspected; URLs for the exact installed short revision returned 404, so source equivalence to that binary is not claimed. Installed help independently confirms the multi-source argument contract.

## Implemented transport and safety bounds

- Groups contain at most 32 same-parent files, with a 32,000-byte argv budget, inside the unchanged approximately 250 MB / 5000-file scheduler ceiling. Large individual files retain existing single-file behavior and disk admission rules.
- Temp hard links reuse staging bytes. If a group hard link fails, each fallback copy requires enough currently free space to retain the 5 GB reserve. No unbounded payload is held in memory.
- All temps must pass independent exact verification before the first promotion. A missing file, mismatched digest/size, missing UID, duplicated UID, or nonzero upload exit fails the group closed. CLI output alone never proves success.
- Every promotion retains the immediate fresh final-name collision check and final content/UID verification. Batch verification and COMPLETE/verified transitions remain unchanged. Local release still requires verified COMPLETE state.
- On failure, cleanup only trashes a previously proven temp UID after rechecking its name, parent UID, digest and size. Unproven nodes are left isolated. Interrupts skip remote cleanup and propagate to the existing worker pause/recovery handling. No automatic retry of an ambiguous mutation was added.
- A partial group may leave isolated temps; a final promoted before a later failure is preserved. Existing recovery refuses collisions on retry rather than overwriting or adopting uncertain data. This is deliberately conservative and can need attention after interruption.
- Proton has no atomic no-clobber rename in this provider contract. The existing isolation + immediate preflight residual race remains; it was not broadened by caching the final check.
- No database schema, hydration policy, destination ownership, release, or completion semantics changed in this pass.

## Local concurrency benchmark

Reproduce with `uv run python scripts/benchmark_transport.py --files 14 --delay-ms 10`. The harness uses the strict in-memory filesystem and launches a real local Python child for each simulated CLI command, with a 10 ms synthetic delay. It never invokes Proton or reads credentials. Setup/verification calls outside safe upload are excluded. Raw results: [performance-benchmark.json](performance-benchmark.json).

| Mode | Workers | Wall s | Average file latency s | Commands | Child CPU s | CPU % of one core |
|---|---:|---:|---:|---:|---:|---:|
| Original per-file | 1 | 3.314 | 0.237 | 112 | 1.674 | 54.7 |
| Original per-file | 2 | 1.703 | 0.243 | 112 | 1.716 | 108.8 |
| Original per-file | 3 | 1.264 | 0.253 | 112 | 1.799 | 153.6 |
| Grouped transport | 1 | 1.702 | 1.130 | 58 | 0.862 | 54.9 |

All four runs: zero failures/timeouts/simulated provider errors. Real provider errors are unobserved, not zero. Group latency includes shared transport and the wait for all temp proofs; it is intentionally not directly comparable to a per-file job's service latency. CPU percentages include parent and children.

Production concurrency remains one. Synthetic overlap demonstrates scheduling potential but cannot establish Proton bandwidth, block-parallelism, rate-limit, auth-cache contention or cancellation behavior under concurrent processes. A controlled provider benchmark would be required before enabling it. The grouped command savings are deterministic; actual live speedup remains unmeasured.

## Developer telemetry and UI

`cli_process` JSONL events report command family, duration, exit status/error type, and migration ID, without argv, stdout, stderr or credentials. The engine observes all provider calls during each bulk cycle, including setup and batch verification. Upload completion now has a monotonic duration. Shared group upload timings are emitted once; per-file temp/promote/final stages remain independent.

Run `python -m src.cloudmover.performance PATH_TO_JSONL --migration-id ID` for attempted file/byte totals, upload wall, stage sums, estimated outside-stage overhead, process count/average duration, failures, effective logical throughput, and average successful file latency. Counts include retries as attempts. Missing old telemetry is `null`. Interrupted upload wall/rate is unavailable. Stage sums assume the serialized production pipeline; do not use this helper unchanged with future concurrent stages. CLI counts cover the observed bulk cycles, not external GUI preflights. The nonzero-exit count includes expected missing-node probes used by destination preflight; it is not the GUI actionable-failure count. Old timestamp-only upload wall measurements have one-second resolution.

The normal UI keeps existing speed caveats. It does not add an inferred network rate or ETA. Shared transport shows the group rather than a fictitious active file or byte percentage. Per-file proof phases still identify the actual file. Both the route and selection-tree root now use the selected source basename (`SyncMeBro-2`), with `iCloud Drive` reserved for the default root.

## Verification and changed files for this pass

- Focused: 80 passed. Full: 229 passed (208 baseline + 21 new cases).
- Strict argv/count tests cover baseline and optimized 14-file migrations, multiple basenames with punctuation/Unicode, group bounds, missing/corrupt/partial uploads, UID proof, fresh pre-promote collisions, interruption, DB recovery, final-proof failure, low-space fallback, telemetry, and actual Qt route/tree labels. Existing destination, cleanup-warning, .DS_Store, and pause child-process tests remain green.
- Runtime changes: `src/cloudmover/providers/proton.py`, `bulk_pipeline.py`, `start.py`, `gui.py`; new `performance.py`.
- Test changes: `tests/test_destination_regression.py` (multi-file argv fake), `tests/test_bulk_pipeline.py` (removed redundant preflight expectations); new `tests/test_transport_performance.py`.
- New developer artifacts: `scripts/benchmark_transport.py`, `docs/performance-benchmark.json`, this report.
- Rebuilt `/Users/gabem/GitHub/cloudmover/dist/CloudMover.app`; `codesign --verify --deep --strict` passed. Source constant, bundled Python constant, CFBundleShortVersionString and CFBundleVersion all report 0.19.6. The build script reran all 229 tests successfully. Pre-existing uncommitted changes from earlier requested passes are preserved.
