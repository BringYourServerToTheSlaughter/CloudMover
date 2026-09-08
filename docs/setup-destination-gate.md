# Setup destination gate — September 7, 2026

## Cause and old state sequence

The wizard's System Check transition validated destination syntax, but not remote occupancy. It then entered the Scan page. Clicking Scan set active scan labels, constructed a QThread/MigrationScanWorker, and started it. The worker performed remote destination preflight before inventory. A collision raised RuntimeError through the worker's generic failed signal, which invoked `scan_failed`: a source-oriented scan failure label and modal appeared on a page without an editable destination. Inventory did not run in that failure case, but the UI had already incorrectly advertised an active scan.

## New state sequence and ownership

System Check Continue → source/readability/CLI/disk checks → destination syntax and read-only remote preflight → ready Scan page. Any setup failure leaves the user on System Check. A collision shows the exact requested path next to the editable destination, focuses that field and selects its text. Source, acknowledgement, destination customization and existing database contents are preserved. A failed attempt does not create a worker, thread, inventory or valid scan summary.

Scan action → recheck source prerequisites and remote destination → only then create/start MigrationScanWorker and show an active inventory operation. This second preflight is deliberate: another actor could create the destination while the ready screen remains open. Changing the destination clears the stale validation message and scan summary, and the next action validates again. A second colliding destination is still refused. Returning from a successful scan to System Check and proceeding again also revalidates.

`SetupDialog.check_destination_before_scan` owns setup preflight and presentation. It calls the existing backend `preflight_destination`, whose exact-path no-adoption policy and read-only info commands remain unchanged. A `DestinationExistsError` subtype now carries the conflicting path, avoiding UI classification by matching error text. Other destination failures use destination-specific inline wording. Invalid syntax uses the same inline presentation instead of a modal.

MigrationScanWorker now performs inventory only; no Proton preflight remains inside it. `initialize_migration` was already inventory/database-only and has no duplicated Proton check. The engine's independent `prepare_destination` ownership/identity check is retained: setup success is not authorization to adopt an existing folder later.

Source preflight failures retain source-specific System Check messaging. Only failures after actual inventory startup invoke `scan_failed`. Navigation is disabled while the synchronous destination check runs, with a Checking Proton destination label and a reentry guard. No merge, overwrite, destination creation, or engine/verification/release policy was introduced.

## Tests

Nine new tests cover:

- Existing folder rejected before worker construction, with no new database or scan summary.
- Exact inline collision path, destination focus/selection, source and acknowledgement preservation, no Scan failed message.
- A second collision followed by an edited fresh destination and successful local inventory startup.
- A destination appearing between the ready screen and Scan action.
- Source-specific preflight failure, destination unavailability/syntax failure, and actual inventory failure categories.
- Existing database bytes preserved and an old UI scan summary invalidated on setup failure.
- Worker inventory has no remote-preflight dependency.
- Disabled controls and prevention of preflight reentry.

The strict fake Proton backend proves preflight uses only info calls and leaves existing folders/contents untouched. No live Proton migration was performed. An existing wizard collision test was updated to require staying on the editable page rather than navigating Back from an incorrectly entered scan state.

## Limits and handoff

Read-only destination checks are synchronous and retain the existing CLI timeouts; a slow Proton response can pause wizard interaction. The two checks do not reserve the remote name. A destination created after the final preflight is still refused by the engine's independent fail-closed destination claim before transfer. Source availability can likewise change during inventory, which remains an actual scan error. These races never authorize merge or overwrite.

Changed in this pass: `src/cloudmover/gui.py`, `src/cloudmover/destination.py`, new `tests/test_setup_destination_gate.py`, updated `tests/test_destination_semantics.py`, and this report. Earlier uncommitted CLI-discovery/tree-stability work is preserved. No commit or live migration.

Focused validation: **83 tests passed**, including the additional setup-preflight suite present in the workspace. The build ran the complete suite: **277 passed**. Rebuilt `/Users/gabem/GitHub/cloudmover/dist/CloudMover.app`; strict deep signature verification passed. Source constant, packaged constant, CFBundleShortVersionString and CFBundleVersion all report **0.19.6**.
