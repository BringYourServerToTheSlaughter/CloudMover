# Selection-tree crash fix — September 7, 2026

This pass changes GUI selection handling only. No live migration or commit was performed. The earlier CLI-discovery changes remain in the working tree.

## Root cause and crash comparison

The exact former path was:

`QStyledItemDelegate::editorEvent → QTreeWidgetItem::setCheckState/setData → itemChanged → Window.selection_changed → set_selection_prefix → notice("Selection updated…") → load_selection_tree → tree.clear()`.

Both the success and Python-exception branches of `selection_changed` rebuilt the tree synchronously. `clear()` destroys its items while the native delegate is still on the initiating mouse-release stack. The loading guard suppressed recursive Python handling during construction, but could not make destroying the active native item safe. The user-visible last notice appears immediately before that clear, consistent with the reported reproduction.

The local `CloudMover-2026-09-07-080805.ips` confirms main-thread SIGSEGV / EXC_BAD_ACCESS, with `QTreeWidgetItem::setData`, wrapper `setData`, `QStyledItemDelegate::editorEvent`, and `QTreeView::mouseReleaseEvent` at the top. This matches the invalidation path found in code. We did not intentionally rerun the old crashing implementation.

The requested comparison report, `CloudMover-2026-09-06-220735.ips`, has a different signature: SIGABRT / EXC_CRASH through `QMessageLogger::fatal`, `QThread::~QThread` and widget/dialog teardown. It does not provide evidence of the same tree bug. Thread teardown is outside this focused fix.

## Fix

- The checkbox handler copies the relative path and requested state, then performs the existing single SQLite prefix update. The SQL, selection eligibility, and protection of completed rows are unchanged.
- The handler never reloads, clears, recursively updates descendants, or invokes the periodic status refresh. It schedules a window-owned single-shot zero-interval timer, which reconciles after the originating event returns. Rapid requests coalesce into one pending reconciliation; database updates retain their original order.
- Reconciliation reads persisted selection once and updates the existing items in place, deriving folder/root states from child counts. It retains expansion, filtering, scroll position and item identity. No tree rebuild occurs for ordinary checkbox changes.
- `QSignalBlocker(self.tree)` plus the internal loading/handling guards prevent programmatic check-state changes from re-entering persistence. A scalar path-to-check-state map also ignores itemChanged events caused by changes to unrelated item roles.
- Explicit full reload requests made during a selection signal, a programmatic update, or pending reconciliation are deferred. Their options are coalesced, and a generation counter changes only when a real rebuild occurs. Normal periodic status refresh never rebuilds the tree.
- Deferred work captures the window method and scalar state, never an item wrapper. No item pointers survive a requested rebuild. The timer is owned by the window, so QObject destruction cancels it.
- A rejected database update reconciles the same items back to persisted truth. Python persistence failures stay visible. No native exceptions are caught or suppressed, and no GUI work moves off the main thread.

## Diagnostics

A sibling JSONL file next to the migration database records selection diagnostics. For the default database this is `~/Library/Application Support/CloudMover/migration.selection.jsonl`.

Events include `tree_selection_change_start`, `tree_selection_change_complete`, `tree_selection_reconcile_deferred`, `tree_selection_reconcile_complete`, and deferred explicit-refresh start/completion events. Records include migration ID, relative path, requested check state, descendant **file** count, programmatic versus checkbox-handler origin, tree generation, changed-row count, duration and any persistence error. Programmatic reconciliation logs an aggregate event rather than one line per descendant. Diagnostic file errors do not change selection behavior.

## Regression coverage and measured performance

Eight new GUI tests collectively cover root select-all, deselecting a child, sibling toggles, 40 rapid toggles, partial-state transitions, absence of recursive signals/database updates, no synchronous rebuild even when another itemChanged slot requests one, nearby timer refresh, restart persistence, persistence failure recovery, completed-row protection, and actual Qt checkbox mouse press/release events. The large-tree case contains 10,000 manifest files and 100 folders; no source hydration or remote access occurs.

Initial local macOS offscreen Qt measurements:

| Operation | Time |
|---|---:|
| Construct 10,000-file tree | 0.159 s |
| Root select + persistence + reconciliation | 0.093 s |
| Five folder deselections + reconciliation | 0.109 s |

Final count was 9,500 selected files. Tree generation and root identity did not change. The test records fewer than 30 aggregate diagnostic lines for six actions, rather than logging 10,000 descendants. One SQL update is performed per user action; one O(N) reconciliation is performed per coalesced event-loop turn.

Validation: 8 focused new selection tests, 67 broader focused GUI tests, and 259 full-suite tests passed. The build reran all 259 tests successfully. Rebuilt `/Users/gabem/GitHub/cloudmover/dist/CloudMover.app`; `codesign --verify --deep --strict` passed. Source constant, packaged constant, CFBundleShortVersionString and CFBundleVersion all report 0.19.6.

Remaining limits: these automated tests use real Qt event/delegate handling with the offscreen platform on macOS, not an extended manual Finder GUI soak. Much larger or unusually deep trees and a contended SQLite database can still take longer; reconciliation remains a main-thread O(N) traversal. No remaining item-invalidation failure was observed in these tests. The distinct historical QThread teardown report is not addressed by this tree-only fix.

Files changed in this pass: `src/cloudmover/gui.py`, new `tests/test_tree_selection_stability.py`, and this report. No transfer, provider, hydration, verification, release or schema changes were introduced by this pass.
