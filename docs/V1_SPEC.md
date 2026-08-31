# CloudMover V1 Specification

## Product promise

Pick a source cloud and a destination cloud. CloudMover chooses the fastest safe migration strategy, keeps the computer awake when required, resumes interrupted work, verifies transferred files, and never deletes the source during V1.

## V1 scope

- Platform: macOS first
- Source: iCloud Drive
- Destination: Proton Drive
- Operation: copy only
- User interface: developer CLI first; desktop GUI after engine reliability is proven
- State: resumable local migration state
- Verification: every completed file must be verifiably present at the destination before being marked complete

## Required preflight checks

CloudMover must inspect:

- source availability
- destination availability/authentication
- total file count and logical data size
- free local disk space
- whether local staging is feasible
- power state / sleep risk
- expected provider or filesystem constraints

## Migration strategies

### Full local staging

Use when sufficient temporary local storage is available. Hydration and upload should be pipelined rather than strictly sequential.

### Smart staging

Use when local storage is constrained. Maintain a bounded staging pool, download ahead, upload concurrently, verify, and release temporary local copies only after successful verification.

### Future cloud-to-cloud

Not part of V1. Consider only where provider-supported APIs and authentication make it reliable and commercially supportable.

## File state model

Each file should eventually move through a durable state machine:

`pending -> hydrating -> ready -> uploading -> verifying -> complete`

Failures should preserve enough information for retry and diagnosis.

## Non-negotiable reliability behavior

- Resume after process termination or reboot
- Survive temporary network loss
- Never silently skip ambiguous failures
- Never delete original source files in V1
- Protect a minimum reserve of free disk space
- Prevent macOS sleep while a migration is active where appropriate
- Be safe to run a migration more than once
- Produce an end-of-run report containing completed, skipped, retried, and failed items

## Initial success criterion

CloudMover must migrate a test library of at least 1,000 files, survive being killed mid-transfer, resume, and finish with zero missing or corrupted files.
