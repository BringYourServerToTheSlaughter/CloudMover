# CloudMover Roadmap

## 0.1 — Scanner
- [x] Recursive source inventory
- [x] File/folder/symlink counts
- [x] Logical byte total
- [x] Free-disk measurement
- [x] Initial full-vs-smart staging recommendation
- [x] Detect macOS iCloud placeholder / hydration state
- [x] Generate a durable migration manifest

## 0.2 — Durable state
- [x] SQLite migration database
- [x] Per-file state machine
- [x] Resume-readable unfinished migration state
- [x] Retry/error metadata

## 0.3 — iCloud source adapter
- [ ] Hydrate cloud-only items safely
- [ ] Track hydration readiness
- [ ] Bounded staging cache
- [ ] Safe eviction after verified transfer
- [ ] Sleep prevention / restoration

## 0.4 — Proton destination adapter
- [ ] Authentication / destination validation
- [ ] Upload files and create folders
- [ ] Controlled concurrency
- [ ] Retry/backoff and throttling handling
- [ ] Destination verification

## 0.5 — Migration engine
- [ ] Pipeline hydration + upload + verification
- [ ] Dynamic worker/concurrency tuning
- [ ] Disk reserve enforcement
- [ ] Network interruption recovery
- [ ] Idempotent reruns

## 0.6 — Reliability gate
- [ ] 1,000+ mixed-file migration
- [ ] Kill and resume test
- [ ] Wi-Fi interruption test
- [ ] Low-disk test
- [ ] Weird/unicode filenames
- [ ] Very large files
- [ ] Many tiny files
- [ ] Zero missing/corrupted files

## 1.0 — Desktop MVP
- [ ] Tauri desktop UI
- [ ] Source/destination wizard
- [ ] Preflight report
- [ ] Live progress and errors
- [ ] Pause/resume
- [ ] Final migration report
- [ ] Signed/notarized macOS package
