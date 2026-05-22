# 0002 — Attachments stored by SHA-256 hash

**Status:** Accepted
**Date:** 2026-04-25 (codified in v1 build-order step 3)

## Context

Real Gmail inboxes contain a lot of duplicate attachments — the same
insurance card emailed every year, the same receipt template across
many vendors, identical marketing images attached to a newsletter run.
Storing each instance separately on disk wastes space and means every
duplicate gets re-extracted by Docling on scan.

Two options were on the table:

1. Store each attachment under a path keyed on `(message_id,
   attachment_id)`. Simple; one file per attachment row.
2. Store each attachment under a path keyed on the SHA-256 of its
   bytes ("content-addressed"). Two attachments with identical bytes
   share one file.

## Decision

Content-addressed storage:

```
<data_dir>/attachments/blobs/<sha[:2]>/<sha[2:4]>/<sha>
```

The first four hex chars shard the directory tree so no single
directory grows past ~250 entries even for huge corpora.

The `attachments` table carries a `content_hash` column. Multiple
`attachments` rows can point to the same hash; the scan pipeline
caches extraction results keyed on the hash too
(`extracted/<hash>.md`).

## Consequences

**Good:**

- Storage savings. On the dev corpus, 37 downloaded attachments
  deduped to 36 unique blobs (one identical Receipt.pdf appeared in
  two different eCornell emails). On a real inbox the savings are
  larger.
- Extraction savings. The scan pipeline's
  `_read_cached_extraction(extracted_dir, content_hash)` lookup skips
  Docling for any attachment whose bytes have been extracted before —
  even from a different message.
- `store_blob` is idempotent and atomic (`.tmp` rename), so a crashed
  sync that re-downloads the same bytes is a no-op on disk and a
  cheap UPDATE in the DB.

**Costs:**

- **Per-attachment delete is unsafe.** If two messages share a blob
  and the user wants to scrub one, the blob has to stay because the
  other message still references it. v1 punts on this: the only
  delete path is `inboxaudit reset` (full or scoped), never
  per-attachment.
- The DB-on-disk picture is harder to inspect by hand. You can't
  `ls attachments/` and see filenames; you have to join through the
  `attachments` table.

## Encoded in

- `inboxaudit/blobs.py` — `store_blob` / `read_blob` / `blob_exists`.
- `inboxaudit/models.py::Attachment.content_hash` and `blob_path`.
- `inboxaudit/pipelines/scan_pipeline.py::_read_cached_extraction`
  — extraction cache keyed on content hash.
- `tests/test_blobs.py` — roundtrip, dedup, no-`.tmp`-leftover tests.

## Alternatives considered

- **Per-attachment file paths.** Simpler, but no dedup, and forces
  Docling to re-extract every copy of every identical attachment. For
  inboxes with shared templates that's a significant ongoing cost.
- **Blob-store inside SQLite (BLOB columns).** Loses the easy
  filesystem inspection, makes backups awkward, and SQLite isn't
  optimised for 50 GB of BLOB columns.
