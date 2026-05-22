# 0001 — Sync and scan as independent phases

**Status:** Accepted
**Date:** 2026-04-25 (codified in v1 build-order step 1)

## Context

The scanner has two cost models that don't share constraints:

1. **Talking to Gmail** is bandwidth-bound, rate-limited (20 req/sec
   self-imposed budget under Gmail's per-user quota), idempotent on the
   server side but irritating to do repeatedly because of rate limits
   and OAuth ceremony.
2. **Running PII detection** is CPU-bound, fully local, and the part
   that we want to *iterate* on while developing and tuning detector
   thresholds.

If both lived in one pipeline, every threshold change would require a
fresh Gmail round-trip on the full corpus. That's slow, wasteful, and
makes the development loop painful.

## Decision

Split the runtime into two commands that share storage but otherwise
run independently:

- **`inboxaudit sync`** talks to Gmail. Writes message metadata and
  raw attachment bytes into the local store. Idempotent and
  resumable.
- **`inboxaudit scan`** runs entirely offline against the local
  store. Re-runnable any number of times.

The DB schema mirrors the split: `messages` and `attachments` carry
sync state; `detections` and `message_verdicts` are scan-scoped and
get rewritten every scan.

## Consequences

**Good:**

- A user pays the Gmail API cost exactly once. Detector tuning iterates
  locally at the speed of CPU (~80 s / 37 attachments on the dev corpus).
- Sync can be Ctrl-C'd freely; the next `sync` resumes. Scan re-runs
  are bit-for-bit identical. Both properties are covered by tests.
- The `gmail/` subpackage is only imported during sync — easy to keep
  isolated and trivial to mock in scan-pipeline tests.
- Reset's `--keep-attachments` flag falls out naturally: the artefact
  the sync phase produces (blob files) survives independently of the
  artefacts the scan phase produces.

**Costs:**

- Two separate progress UIs to maintain, two separate command flows
  for the user to learn. Mitigated by `inboxaudit scan` running
  both stages end-to-end when called with no flags.
- The CLI has more commands than the minimum. Worth it.

## Encoded in

- `inboxaudit/pipelines/sync_pipeline.py` and
  `inboxaudit/pipelines/scan_pipeline.py` — two separate orchestrators.
- DB schema in `inboxaudit/models.py`: `syncs` / `scans` run tables,
  per-row `sync_status` and `extraction_status` columns.
- CLAUDE.md "Architecture: keep the two phases separate" rule.
- Build-order step 4 onwards: the scan pipeline was developed entirely
  against the already-synced dev corpus, never re-pulling from Gmail.
