# CloudMover

CloudMover is a desktop-first migration engine for safely moving large cloud file libraries between providers without requiring users to understand shell commands, cloud-only placeholders, temporary staging, retries, or resumable transfers.

## MVP scope

CloudMover v0.1 targets macOS and the iCloud Drive → Proton Drive migration path.

The first milestone is a scanner that inspects an iCloud Drive source and reports:

- file and directory counts
- total logical size
- currently available local disk space
- an initial migration strategy recommendation
- a migration manifest that later components can persist to SQLite

## Project layout

- `src/cloudmover/` — migration engine code
- `src/cloudmover/providers/` — provider adapters
- `tests/` — automated tests
- `docs/` — product and architecture notes

## Run the scanner

Python 3.11+ is recommended.

```bash
python3 -m src.cloudmover.scanner "/Users/$USER/Library/Mobile Documents/com~apple~CloudDocs"
```

Or scan any test folder:

```bash
python3 -m src.cloudmover.scanner ~/CloudMover-Test
```

## Durable migration state

CloudMover 0.3 can persist a migration manifest to SQLite without opening or hydrating file contents:

```bash
python3 -m src.cloudmover.state init "/Users/$USER/Library/Mobile Documents/com~apple~CloudDocs" --db .cloudmover/migration.db
```

Inspect the saved state later:

```bash
python3 -m src.cloudmover.state status --db .cloudmover/migration.db
```

The per-file state machine is `PENDING → HYDRATING/READY → UPLOADING → VERIFYING → COMPLETE`, with explicit failure and recovery paths.

## Current status

CloudMover 0.3 inventories iCloud hydration state and saves durable per-file migration state. It still does not transfer or delete files yet.

## v0.16.4: single-file iCloud hydration

CloudMover can now hydrate exactly one cloud-only file from an existing migration manifest. The v0.16.4 command deliberately chooses the smallest eligible file (100 MB safety cap by default), records `PENDING -> HYDRATING -> READY`, verifies the expected logical size and readability, and marks failures safely in SQLite.

```bash
python3 -m src.cloudmover.hydrate --db .cloudmover/migration.db
```

This command does **not** upload, evict, move, or delete files. v0.16.4 is intentionally single-file only.


## v0.16.4 batch recovery trial

Hydrate up to 10 small non-zero iCloud placeholders:

```bash
python3 -m src.cloudmover.hydrate --db .cloudmover/migration.db --batch 10
```

The automatic batch selector uses files between 10 KB and 10 MB. Press `Ctrl+C`
during a batch to test restart recovery. On the next batch run, any file left
in `HYDRATING` is safely returned to `PENDING` before work continues.

v0.16.4 remains hydration-only: it does not upload, evict, move, or delete files.


## v0.16.4 interruption test

The automatic batch profile now selects files from 1 MB through 10 MB. This is
deliberately large enough to make an interruption/restart test practical.

Run:

```bash
python3 -m src.cloudmover.hydrate --db .cloudmover/migration.db --batch 10
```

Press `Ctrl+C` while a numbered item is actively hydrating. Then inspect state:

```bash
python3 -m src.cloudmover.state status --db .cloudmover/migration.db
```

Run the batch command again. v0.16.4 recovers any row stranded in `HYDRATING`
back to `PENDING` before selecting the next work.

No upload, eviction, move, or deletion is performed.

## v0.16.4 Proton destination trial

Uploads exactly one non-zero `READY` file to `/my-files/CloudMover-Test`, then
queries Proton Drive using JSON output and verifies the remote filename, claimed
original size, and SHA-1 digest when exposed by the CLI.

```bash
python3 -m src.cloudmover.upload --db .cloudmover/migration.db
```

The source file is never deleted or evicted in v0.16.4.

## v0.16.4 end-to-end batch

Runs 10 real files (100 KB–5 MB by default) through hydration, local checksum,
Proton upload, and remote verification. It uploads the exact file just hydrated,
not an arbitrary READY row.

```bash
python3 -m src.cloudmover.pipeline --db .cloudmover/migration.db --batch 10
```

Eviction and deletion remain disabled.

## v0.16.4 directory-tree preservation

Destination paths now preserve the source relative hierarchy. A source such as
`Personal/Mortgage Application 2020/file.pdf` is uploaded beneath
`/my-files/CloudMover-Tree-Test/Personal/Mortgage Application 2020/file.pdf`.

The default end-to-end test destination is intentionally new so results are not
confused with v0.9's flat-folder experiment.

```bash
python3 -m src.cloudmover.pipeline --db .cloudmover/migration.db --batch 5
```

Eviction remains disabled until this exact-path behavior is validated live.

## v0.16.4 performance instrumentation

Reports per-file time for hydration+SHA, Proton directory setup, upload, and
remote verification. Remote directory paths already confirmed during the run
are cached, avoiding repeated provider calls for shared parent paths.

```bash
python3 -m src.cloudmover.pipeline --db .cloudmover/migration.db --batch 5
```

## v0.16.4 bulk-upload performance trial

Instead of creating every Proton folder and uploading every file through separate
CLI processes, CloudMover hydrates a batch, builds the relative hierarchy in a
temporary local tree using hard links where possible, and sends the top-level
batch entries to Proton in one `filesystem upload` invocation with folder merge.

Remote verification remains exact-path and per-file for safety.

```bash
python3 -m src.cloudmover.bulk_pipeline --db .cloudmover/migration.db --batch 10
```

The test uses `/my-files/CloudMover-Bulk-Test`. Eviction remains disabled.

## v0.16.4 grouped verification

The Proton CLI has no recursive listing option, so v0.16.4 does not attempt fake
bulk recursion. Instead, CloudMover groups uploaded files by their exact remote
parent directory, lists each parent once, and validates every expected filename,
size, and SHA-1 from that shared response.

```bash
python3 -m src.cloudmover.bulk_pipeline --db .cloudmover/migration.db --batch 10
```

This preserves exact-path safety while reducing verification round trips whenever
multiple files share a destination directory.

## v0.16.4 bounded parallel verification

Exact parent-directory verification is now performed concurrently with a small
worker pool. The default is four workers, chosen to overlap network latency
without aggressively hammering Proton.

```bash
python3 -m src.cloudmover.bulk_pipeline   --db .cloudmover/migration.db   --batch 10   --verify-workers 4
```

Verification remains exact-path, filename, size, and digest aware. Eviction is
still disabled.

## v0.16.4 verification recovery and diagnostics

Rows stranded in `VERIFYING` are returned to `READY` on startup so they can be
retried safely. Parallel verification failures now persist a meaningful error
on each affected file instead of leaving state ambiguous.

Proton CLI failures now include the exit code, command, stderr, and stdout rather
than collapsing to a decorative separator line.

For diagnosis, start conservatively with one verification worker:

```bash
python3 -m src.cloudmover.bulk_pipeline   --db .cloudmover/migration.db   --batch 10   --verify-workers 1
```

## v0.16.4 resume-first scheduling

READY work is now drained before CloudMover hydrates new PENDING files. This
fixes the live recovery issue where rows recovered from VERIFYING could sit in
READY while newer files were processed.

The default Proton verification worker count is now 1 because live testing
showed the single-worker path is stable while concurrent CLI verification is not
yet proven safe.

```bash
python3 -m src.cloudmover.bulk_pipeline   --db .cloudmover/migration.db   --batch 10
```

Existing READY rows appear as `[resume ...]` and are completed before new source
files are hydrated.

## v0.16.4 SMART_STAGING local release

CloudMover can now release exactly one local iCloud staging copy only when its
database row is `COMPLETE`, `verified=1`, and currently local. It requests
macOS/iCloud eviction, waits for the file to become `CLOUD_ONLY`, and records
the release in SQLite.

This does **not** delete the iCloud source. It only frees the hydrated local
cache after the Proton destination has been verified.

```bash
python3 -m src.cloudmover.release --db .cloudmover/migration.db
```

v0.16.4 deliberately releases one file only for the first live safety test.

## v0.16.4 rolling SMART_STAGING engine

Runs multiple bounded cycles automatically: resume/hydrate -> bulk Proton upload
-> exact verification -> local iCloud cache release -> free-space check -> next
cycle. A protected free-space reserve is checked before every cycle.

First live trial:

```bash
python3 -m src.cloudmover.engine   --db .cloudmover/migration.db   --cycles 2   --batch 10   --reserve-gb 20
```

The default destination is `/my-files/CloudMover-Engine-Test`. Source-cloud
deletion remains impossible in this build.

## v0.16.4 schema fix

Fixes the rolling engine's disk-space lookup to use the actual migration schema:
`migrations.id` and `migrations.source`. v0.11.0 incorrectly queried nonexistent
`migration_id` / `source_root` columns.

The rolling-engine command is unchanged.

## v0.16.4 continuous production-engine trial

Adds `src.cloudmover.start`, a continuous resume-first SMART_STAGING runner with:

- continuous cycles until complete, paused, interrupted, or safety-stopped
- 5 GB protected reserve by default (plus batch hydration/staging headroom)
- conservative adaptive batch count near the reserve
- backlog release before new staging
- overall file/data progress and ETA
- JSONL event logging in `.cloudmover/cloudmover.jsonl`
- `--dry-run`
- durable RUNNING / PAUSED / COMPLETE migration status
- Ctrl+C safe pause and same-command resume

For the first live test, do not run continuously yet:

```bash
python3 -m src.cloudmover.start   --db .cloudmover/migration.db   --batch 25   --max-cycles 2   --reserve-gb 20   --dry-run
```

Then remove `--dry-run` while retaining `--max-cycles 2`.

## v0.16.4 ETA + verification benchmark

- ETA now starts from work completed in the current process, not historical COMPLETE rows.
- ETA prefers current-session bytes/sec and falls back to files/sec.
- Verification first attempts a top-level subtree snapshot to reduce Proton CLI calls.
- If Proton CLI 0.8.x does not expose descendants in that listing, verification automatically
  falls back to the proven exact-parent grouped method without weakening verification.

Run the same controlled two-cycle benchmark and compare `verification` timing.

## v0.16.4 byte-targeted batching

Production scheduling is now byte-targeted rather than file-count-first.

Default policy:
- target ~250 MB per cycle
- up to 250 files
- select 1 MB–100 MB files for the benchmark class
- READY work still resumes first
- 5 GB protected free-space reserve remains enforced, with batch headroom checked before hydration
- files >100 MB are intentionally excluded from this benchmark and will get a
  dedicated large-file lane after medium-file performance is measured

First test:

```bash
python3 -m src.cloudmover.start   --db .cloudmover/migration.db   --target-mb 250   --batch 250   --max-cycles 1   --reserve-gb 20
```

## v0.16.4 concurrent hydration benchmark

The first pipelining optimization overlaps iCloud hydration across several selected
files while preserving the proven single bulk Proton upload and exact verification.

Why this conservative design:
- The Proton CLI upload accepts a completed local tree, so true file-by-file
  hydration/upload streaming would give up the efficient single bulk upload.
- v0.14 therefore attacks the 40-second hydration stage without weakening the
  bulk-upload path that performed well in v0.13.
- Upload never starts unless every selected file reaches READY.
- Durable manifest state and verify-before-release safety are unchanged.

Benchmark against v0.13's ~250 MB result:

```bash
python3 -m src.cloudmover.start   --db .cloudmover/migration.db   --target-mb 250   --batch 250   --max-cycles 1   --reserve-gb 20   --hydrate-workers 3
```

## v0.16.4 — terminal UX groundwork

This release adds dependency-free terminal UI primitives for truthful determinate
progress and indeterminate provider activity, changes the default hydration worker
count back to 1 after the 0.14 benchmark showed no hydration benefit from 3 workers,
and introduces `--verbose` as the switch reserved for detailed per-file output.

The migration safety model is unchanged: source-cloud data is retained, Proton
completion is verified before local cache release, and Ctrl+C remains resumable.

Note: Proton's bulk-upload command does not currently give CloudMover reliable
byte-level progress, so CloudMover intentionally uses indeterminate activity for
that opaque stage rather than displaying a fabricated upload percentage.

## v0.16.4 — usable CLI

Normal mode now suppresses much of the per-file firehose and shows truthful activity
during Proton's opaque bulk-upload stage. `--verbose` preserves detailed diagnostics.
No migration safety semantics changed.

## v0.16.4 — first GUI prototype

Adds a read-only Tk GUI over the existing migration database. It shows real overall
byte progress, file counts, active transfer state/path, free disk, reserve, and safety
status. It deliberately has no destructive controls and does not replace the engine.

Launch with:
`python3 -m src.cloudmover.gui --db .cloudmover/migration.db`

## v0.16.4 — Qt GUI

Replaces the deprecated macOS system Tk prototype with a PySide6/Qt read-only monitor.
The migration engine and safety semantics are unchanged.

Install GUI dependency: `python3 -m pip install PySide6`
Launch: `python3 -m src.cloudmover.gui --db .cloudmover/migration.db`

## v0.16.4 — live file activity

The Qt monitor now adds a second, per-file activity bar.

- Hydration: shows a real percentage only when macOS exposes meaningful partial
  local allocation; otherwise it truthfully switches to indeterminate activity.
- Upload / verify: indeterminate activity because Proton CLI does not expose
  reliable byte-level progress.
- Completed hydration shows 100%.
- Contrast was increased substantially for headings, metadata, and safety copy.

The GUI remains read-only.

## v0.16.4 — honest engine status

The GUI no longer labels the migration "Paused" during the gap between engine startup
and the first active file-state transition. It now respects the migration RUNNING /
PAUSED / COMPLETE status and displays an indeterminate "Preparing next batch" state
while the engine is active but no individual row is yet HYDRATING / UPLOADING /
VERIFYING.

## v0.16.4 — consistent paused state

The GUI now treats migration-level RUNNING / PAUSED / COMPLETE as authoritative.
A stale transient row can no longer make a paused migration appear active.

Paused/error engine paths also normalize retry-safe state:
- HYDRATING → PENDING
- UPLOADING → READY
- VERIFYING → READY

Hydration only shows a percentage when macOS exposes a genuine intermediate
allocation. Otherwise CloudMover uses an indeterminate activity indicator.

No source-cloud deletion is introduced.

## v0.19.6 — migration controls + selection tree

The GUI is now the primary migration control surface.

- High-contrast stateful Resume/Pause buttons.
- Buttons visibly transition through Resuming / Running / Pausing / Paused.
- Hierarchical iCloud Drive tree with checkboxes for folders and individual files.
- Selection is editable only while paused and locked while running.
- Folder toggles apply to all unfinished descendants.
- Already-COMPLETE files remain selected as immutable migration history.
- Progress, selected file count, selected bytes, and ETA denominator use only selected rows.
- The scheduler ignores deselected PENDING/READY rows.
- Existing databases migrate automatically with selected=1 for every file.

This release intentionally does not support changing selection while the engine is running.

## v0.19.6 — picker usability polish

- Opens at 1200×850 with a larger minimum size.
- Gives the file/folder picker substantially more vertical space by default.
- Keeps the status/picker divider draggable.
- Adds file/folder search that filters the visible tree.
- Adds Expand All and Collapse All controls.
- Migration engine behavior is unchanged.

## v0.19.6 — reliable pause controls + taller picker

- Long-running Proton uploads now run in a separate process group.
- Ctrl+C and GUI Pause can interrupt the active Proton child instead of waiting
  indefinitely for the CLI upload to return.
- Normal Proton list/verify calls keep the proven subprocess.run behavior.
- GUI control labels are driven by the live engine PID, not stale transition copy.
- Running always exposes Pause Migration.
- Default window: 1260×930.
- Picker gets ~650 px initial vertical space and remains draggable.

## v0.19.6 — first-run experience

CloudMover now has an initial onboarding path for the locked v1 scope:
iCloud Drive → Proton Drive on macOS.

On a new database the GUI now:
1. Explains the migration safety model.
2. Detects the standard iCloud Drive folder.
3. Checks free disk space and Proton CLI availability.
4. Allows choosing a different iCloud folder when necessary.
5. Inventories iCloud Drive without intentionally hydrating cloud-only contents.
6. Creates the migration plan.
7. Opens the existing file/folder picker so the user can choose what moves.
8. Does not transfer anything until Resume Migration is pressed.

Existing migration databases skip onboarding and open directly in the proven migration monitor.

## v0.19.6 — migration process isolation

Engine PID files are now scoped to each migration database. A fresh onboarding/test
database can no longer mistake a real migration engine in the same `.cloudmover`
directory for its own process.

## v0.19.6 — selection safety hotfix
- Fixed critical byte-target scheduler omission: PENDING candidates now require selected=1.
- Release backlog also respects selected=1.
- Added executable regression tests proving deselected rows cannot enter the batch.
- Pause escalates to SIGTERM after four seconds if graceful SIGINT does not stop the engine.
- New migrations say Start Migration; previously-started migrations say Resume Migration.

## v0.19.6 — destination + complete-selection safety

- The production engine now reads the Proton destination from the migration plan.
- There is no default `CloudMover-013-Test` destination in normal engine startup.
- If a migration has no destination, CloudMover refuses to start.
- A CLI destination argument is now only a safety assertion and must match the stored plan.
- Selected READY/PENDING files are eligible regardless of size.
- Batch target size controls grouping only; it never excludes selected files.
- Zero-byte and sub-1 MB selected files are no longer silently stranded.
- The GUI displays the stored Proton destination beside the route.

## v0.19.6 — completion workflow

- COMPLETE is no longer a dead end.
- The primary action becomes **New Migration**.
- New Migration launches the same iCloud→Proton setup flow and creates a new
  migration record in the existing database; completed migration history is preserved.
- The secondary action becomes **View Report** with completed/verified/failed counts,
  bytes moved, and destination.
- Stale Starting/Resuming/Pausing notices are cleared on completion.
- Completed file selection is presented as migration history rather than editable work.

## v0.19.6 — single-window lifecycle + reordered workspace

- New Migration no longer opens a second/modal window.
- Setup/onboarding now lives in the same main window using a stacked page layout.
- New Migration transitions the current window back into setup.
- Main migration workspace order is now:
  1. Choose what to move
  2. Migration status/progress
  3. Controls
- New/paused wording now says: “Choose what to move, then start the migration.”

### v0.19.6 process lifecycle safety

- Closing the CloudMover window pauses/stops the engine tied to that migration DB.
- Ctrl+C in the Terminal running the GUI also stops the detached migration engine.
- Shutdown tries SIGINT first, then SIGTERM, and only uses SIGKILL as a last resort.
- This prevents an invisible background migration from continuing after the GUI exits.


## v0.19.6 — macOS packaging foundation

CloudMover now has a first-class macOS application build path.

Runtime data defaults to:

`~/Library/Application Support/CloudMover/`

including the migration database and JSONL log. Development commands can still
override `--db` and `--log`.

To build the unsigned local app on macOS:

```bash
cd ~/GitHub/cloudmover
./scripts/build_macos_app.sh
```

or:

```bash
make app
```

The result is:

`dist/CloudMover.app`

Launch it with Finder or:

```bash
open dist/CloudMover.app
```

This build is intentionally unsigned and unnotarized. Code signing,
notarization, DMG packaging, and Proton CLI provisioning are later packaging
milestones.

## v0.19.6 — live iCloud inventory correctness

- The picker now overlays migration rows onto the current live iCloud directory tree.
- Empty folders and folders with no currently tracked regular files are visible.
- While paused/planned, CloudMover reconciles newly added, changed, and removed files
  without intentionally hydrating cloud-only contents.
- New files discovered after selection are added **unselected** for safety.
- Completed rows are preserved as immutable migration history.
- Inventory is reconciled again immediately before Start/Resume.
- `0 / 0 selected files` now renders as 0%, never 100%.

## v0.19.6 — packaged migration worker

- Fixed Start Migration inside `CloudMover.app`.
- Development mode still launches the engine with
  `python -m src.cloudmover.start`.
- Packaged mode now relaunches the bundled CloudMover executable with a private
  `--engine-worker` switch.
- The packaged migration engine therefore does not require an external Python
  interpreter and no longer receives the invalid `-m src.cloudmover.start` arguments.

## v0.19.6 — CloudMover app icon

- Added the official orange/grey CloudMover application icon.
- Added a 1024px PNG source asset.
- Added a macOS `.icns` bundle icon.
- PyInstaller now embeds the icon into `CloudMover.app`.
- Finder and the Dock will use the branded CloudMover icon after rebuilding.

## v0.19.6 — adaptive batching + recovery hardening

- The 250 MB target remains the primary batch goal; the file-count safety ceiling
  rises from 250 to 5,000 to avoid tiny ~10 MB batches on trees full of small files.
- Stranded VERIFYING rows are explicitly regression-tested to recover to READY.
- Proton list/verification commands now have a 600-second timeout and Proton
  subprocesses inherit the worker process group so Pause/Close can interrupt them cleanly.
- SIGTERM follows CloudMover's graceful KeyboardInterrupt cleanup path.
- GUI Pause/Close signals the worker process group.
- Hydration and verification report determinate 0–100% activity progress through SQLite.
  Proton CLI 0.8.x upload remains indeterminate because it exposes no byte progress.

## v0.19.6 — authoritative pause/resume state machine

- A migration is shown as Running only when its worker PID is actually alive.
- Stale RUNNING database state is automatically repaired to PAUSED when no worker exists.
- HYDRATING rows recover to PENDING; UPLOADING and VERIFYING rows recover to READY.
  COMPLETE rows are never changed.
- Resume order is now: recover stale runtime state → reconcile iCloud inventory while
  paused → launch worker → worker claims PID → mark migration RUNNING.
- The same recovery runs in the worker itself, so direct/packaged resumes are safe.
- Stale PID files are removed automatically.
- Proton CLI children inherit the worker process group, so GUI Pause/Close reaches
  worker and Proton together; internal timeouts still terminate the child directly.
- Paused UI no longer displays stale Verifying/Uploading activity as if work is active.

### Active-state feedback and scan cancellation

Welcome acknowledgement and scan navigation use both disabled controls and disabled
button styling. During a scan, Cancel immediately switches to `Cancelling scan…`;
Cancel, Back, and Scan remain unavailable until the worker exits. Cancellation is
checked before and after directory enumeration and metadata inspection, and while
persisting inventory rows. A cancelled inventory still rolls back its transaction.
An individual synchronous macOS/iCloud filesystem call (such as directory enumeration,
`stat`, or path resolution) cannot be interrupted by the cooperative cancellation
flag. Cancellation waits for that call to return; CloudMover does not kill the worker.

The dashboard derives its active stage from current file state and matching batch
activity. Overall completed migration bytes remain separate from current-file
hydration and current-batch progress. Verification counters describe folder groups,
not files; upload activity remains indeterminate when Proton provides no total.
`Finalizing migration…` does not mean complete: completion still follows the existing
migration status criteria. Healthy runs show `Migration safety`; `Needs attention`
is visible only when failed files exist.

### Proton destination creation regression

The installed Proton CLI contract is `filesystem create-folder parentPath name`.
The parent must already exist; this is not recursive directory creation. Adding a
readable migration-name folder above the UUID exposed a missing-parent bug: CloudMover
attempted to create the UUID child without first creating its readable parent.
The old log joined argv with spaces, making this look like argument splitting.
Destination setup now ensures missing ancestors in order. Only an explicit Proton
`Node not found:` error triggers creation; authentication/network errors propagate.
Readable names and full UUID isolation remain unchanged.

Audited CLI boundaries (all use subprocess argv arrays, never `shell=True`):

| Operation | Positional arguments |
| --- | --- |
| create-folder | existing parent path, one complete folder name |
| list / verification listing | one complete remote path (after list options) |
| info / exact verification | one complete remote path; `-j` enables JSON |
| temporary upload | local path(s), then one complete remote parent path |
| rename / promotion | one complete temporary remote path, one complete final name |
| trash cleanup | verified temporary node UID path |

Spaces, apostrophes, parentheses, hyphens and Unicode stay within the original
argument. Errors render argv as a JSON array so boundaries remain visible.
All engine JSONL lifecycle and batch hydration/upload/verification/release events
carry `migration_id`. The GUI incrementally reads scoped fatal errors (including
older unscoped errors following a scoped start), displays a stopped/needs-attention
state after the worker exits, and keeps raw CLI details behind Show error details.
A new scoped start clears the previous run's error; other migrations' errors are
never displayed as the current migration's failure.

### Exact user-selected destinations (supersedes the earlier destination layout)

New wizard migrations default to `/my-files/iCloud Migration - <date and time>`.
The displayed destination is the actual transfer root: `Pics/file.jpg` goes directly
under that folder. There is no implicit CloudMover parent or UUID directory.
The editable destination can instead be `/my-files/My iCloud Backup` or another new
folder in an existing parent. The source's relative hierarchy is preserved.

Before scanning, read-only preflight refuses any existing destination, including an
empty folder. Choose a different human-readable name after a collision; CloudMover
never silently changes the chosen name or merges into an existing destination.
The transfer path repeats preflight before creating the exact root. The created
folder UID is stored with the migration ID in SQLite's existing metadata table,
not as a remote directory or file. Resumes check that UID and the stored path.
Existing per-file no-overwrite, verified promotion, and release checks remain in force.

A crash after remote creation but before saving ownership fails closed: the folder
is preserved and is not automatically adopted. Existing migration plans retain their
original stored paths; an existing legacy root without an ownership record is also
refused. Old verified test copies are not moved, renamed, or deleted by this change.
The app never implicitly uses the user's `/my-files/CloudMover` build-storage folder.

Creation response handling follows the Proton CLI's
[create-folder implementation](https://github.com/ProtonDriveApps/sdk/blob/main/cli/src/commands/fileSystem/commandFileSystemCreateFolder.ts),
which emits the created folder object with JSON output enabled.

### Source-folder defaults, cleanup warnings, and upload diagnostics

The default destination now preserves the selected source folder name. Selecting
`SyncMeBro-2` displays and creates `/my-files/SyncMeBro-2`, with `Pics` and other
relative children directly inside it. Selecting the iCloud Drive root uses the
human-readable name `iCloud Drive`. Changing the source updates an automatic
destination; a user-edited destination remains exactly as chosen. Existing-folder
preflight, recorded destination ownership, no-clobber checks, and legacy paths are
unchanged. No implicit timestamp, CloudMover, or UUID wrapper is added.

New inventories exclude only the regular macOS metadata file `.DS_Store`, including
nested occurrences. Other dotfiles remain eligible. Previously uploaded copies are
not removed. Legacy `.DS_Store` rows are not eligible for cache release.

Schema version 5 adds nullable `files.release_error`. A cleanup failure writes this
field without changing COMPLETE, verified, or transfer `last_error`. The engine logs
`release_warning`, continues with the remaining verified files, and reaches COMPLETE
with a cleanup warning count. It does not repeatedly retry a recorded warning on
every batch. A successful explicit release retry clears the warning. Disk reserve
checks still pause safely if remaining local data leaves insufficient staging space.

Overall progress now says `files verified`; selection counts are separate. Durable
batch transfer/recovery states are unchanged. The existing activity JSON metadata
additionally identifies the actual safe-upload file, its index/total, size, and phase,
so the dashboard follows serial per-file activity rather than an arbitrary UPLOADING
batch row. A file's safe-upload verification does not increment overall COMPLETE
until the existing batch verification criteria pass. Byte progress remains unknown
when Proton does not report it. Internet upload bandwidth, file sizes/counts, and
provider/API overhead all affect duration; many small files incur more overhead.

Bounded per-file `safe_upload_stage` JSONL events contain migration_id, relative_path,
logical_bytes, phase, status, duration_ms, and exit_status where known. Phases are
safe_upload_total, destination_preflight, cli_upload, temp_verification,
rename_promote (including its immediate no-clobber preflight), and final_verification.
Each phase produces one start and one terminal event, not a polling stream. Timing
records contain no command output or credentials. Error diagnostics are bounded and
redact common credential fields. CLI upload duration includes startup/auth/API and
network work; these measurements locate expensive phases but cannot independently
measure network bandwidth. No verification step was removed or shortened.
