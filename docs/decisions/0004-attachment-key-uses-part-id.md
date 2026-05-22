# 0004 — Composite primary key on `attachments` uses `part_id`

**Status:** Accepted — supersedes the original schema in
[plan §"Database schema"](../archives/IMPLEMENTATION_PLAN.md#database-schema)
**Date:** 2026-04-27

## Context

The original schema specified `attachments.id` as Gmail's
`attachment_id`, possibly prefixed with the message id to form a
composite. We built it that way, and immediately hit a bug during
sync's third run:

> Same content_hash, same filename, same parent message — but two
> `attachments` rows, each with a different `gmail_attachment_id`.

Gmail's documentation explains it:

> The attachment ID is part of the MIME structure of the message. The
> attachment ID expires after a few hours.

So:

- Day 1 sync writes rows keyed on attachment IDs A, B, C.
- A few hours later, those IDs are invalid.
- Day 2 sync calls `messages.get` again, gets fresh attachment IDs
  A', B', C'.
- Each composite lookup misses → three new rows inserted alongside
  the stale ones.

The composite key was unstable. Re-syncing a corpus would double the
row count every time.

## Decision

Switch to a composite primary key of `{message_id}:{part_id}`. Gmail's
`partId` is documented as **"the immutable ID of the message part"**,
so the same logical attachment always maps to the same composite ID
across any number of resyncs.

Move the volatile `attachment_id` into its own column,
`gmail_attachment_id`, and refresh it on every metadata fetch.
Downloads use whichever value the metadata pass just produced — so
the id is always seconds old when the download starts.

## Consequences

**Good:**

- Re-syncs are truly idempotent. The bug went from "doubles the table
  every run" to verifiable: a re-sync after a multi-hour gap leaves
  the same row count.
- The DB only ever stores fresh, downloadable attachment IDs (or the
  empty string if the row is in `skipped_filter`).
- `make_composite_attachment_id(message_id, part_id)` is a one-line
  helper everyone calls; the volatility lives in exactly one place.

**Costs:**

- One extra column on `attachments`. Trivial.
- A migration was required to wipe pre-fix rows. Their composite IDs
  were stale and useless — the safest path was to delete them and
  reset the affected messages to `sync_status='pending'` so the next
  sync rebuilt the table with the new key. Migration is
  `1c965f28e09a_stable_attachment_composite_via_partid.py`.

## Encoded in

- `inboxaudit/models.py::Attachment` — `id`, `part_id`,
  `gmail_attachment_id` columns.
- `inboxaudit/gmail/client.py::make_composite_attachment_id` —
  the only place that builds the key.
- `inboxaudit/gmail/sync.py::_process_message_metadata` —
  refreshes `gmail_attachment_id` on every metadata fetch; never
  changes the composite `id`.
- CLAUDE.md "load-bearing constraints": **Never** put
  `gmail_attachment_id` in a primary key.
- Migration `alembic/versions/1c965f28e09a_*.py`.

## Alternatives considered

- **Fetch metadata twice per download** to minimize the window
  between getting an attachment_id and using it. Doesn't fix the
  composite-key instability; just hides it. Costs 2× the quota.
- **Drop the composite entirely**, use an opaque INT primary key and
  enforce uniqueness on `(message_id, part_id)` via a separate
  constraint. Equivalent semantics; we picked the composite because
  it's easier to grep for in DB dumps.
