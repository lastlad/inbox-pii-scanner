# Operations

How the scanner's files are laid out on disk, what's in them, when
they're created, and how to clean them up safely.

## Data directory

Resolved by
[`inbox_scanner/config.py::load_settings`](../inbox_scanner/config.py)
on every CLI invocation:

| Mode | Trigger | data_dir |
|---|---|---|
| Source checkout (dev) | `pyproject.toml` reachable upward from cwd | `<repo>/.inbox-scanner-data/` |
| Installed wheel | no `pyproject.toml` upward | `~/.inbox-scanner/` |
| Explicit override | `INBOX_SCANNER__DATA_DIR=...` env var | wins over both |

Relative override paths resolve against the project root (the folder
containing `pyproject.toml`), so `INBOX_SCANNER__DATA_DIR=./foo` works
from any subdir of the checkout.

## Directory layout

After a full setup + sync + scan, the data dir looks like:

```
<data_dir>/
├── credentials.json         # User-provided Google OAuth client (step 3 of README)
├── token.json               # OAuth refresh token (created by `inbox-scanner auth`)
├── state.db                 # SQLite — sync state, scan state, findings, verdicts
├── state.db-wal             # WAL journal (transient, present while DB is open)
├── state.db-shm             # WAL shared-memory file (same)
│
├── attachments/
│   └── blobs/
│       ├── ab/
│       │   └── cd/
│       │       └── abcd1234...   # raw attachment bytes (no extension)
│       └── ...               # SHA-256 sharded into 2 levels of 2-char prefixes
│
├── extracted/
│   ├── abcd1234...md          # Docling output keyed by content_hash
│   └── ...
│
└── logs/
    ├── cli.log
    ├── sync.log
    ├── scanner.log
    └── server.log
```

External to the data dir but tied to it:

| Path | Purpose | Owner |
|---|---|---|
| `~/.cache/huggingface/hub/` | Docling models (~2 GB) + Privacy Filter (~2.6 GB) | shared across all checkouts |
| `~/.cache/easyocr/` | EasyOCR weights (downloaded lazily on first OCR call) | shared |

Both HF caches sit outside the data dir on purpose — `inbox-scanner
reset --all` doesn't blow them away, so the next setup is fast.

## File lifecycle

### `credentials.json`

- Created by: the user, during README step 3.
- Read by: `inbox-scanner auth` (once) and `inbox-scanner serve` /
  `sync` indirectly when they call into the Gmail SDK.
- Deleted by: `inbox-scanner reset --all`. Default `reset` preserves
  it.

### `token.json`

- Created by: `inbox-scanner auth`.
- Read by: every Gmail-touching command via
  [`gmail/auth.py::load_credentials`](../inbox_scanner/gmail/auth.py).
- Updated by: silent refresh when the access token expires (the
  refresh token is long-lived).
- Deleted by: `inbox-scanner reset --all`.

### `state.db` (+ `-wal`, `-shm`)

- Created by: `_bootstrap()` on first invocation via
  `apply_migrations`. Schema is brought to head before any command
  runs.
- WAL files appear when any SQLite handle is open. They're checkpointed
  back into the main file on clean shutdown. Safe to delete only when
  no scanner process is running.
- Deleted by: any default `inbox-scanner reset`.

### `attachments/blobs/<ab>/<cd>/<hash>`

- Created by: the sync pipeline's `store_blob` call after a successful
  `attachments.get`. Atomic write via `<final>.tmp` → `rename`.
- Read by: the scan pipeline's extract stage (via
  `read_blob(blob_path, attachments_dir)`).
- Content-addressed: two attachments with identical bytes share one
  file. The DB schema acknowledges this — `attachments.content_hash`
  is the join key, not the file path.
- Deleted by: default `inbox-scanner reset` (unless
  `--keep-attachments`).

### `extracted/<content_hash>.md`

- Created by: the scan pipeline's extract stage on first extraction of
  a given content hash. Atomic write via `.tmp` rename.
- Read by: the scan pipeline's detect stage and `GET /api/email/{id}`.
- Cached across scan runs: re-running scan reuses these unless
  `--force-extract` is passed.
- Deleted by: default `inbox-scanner reset` (unless
  `--keep-extractions`).

### `logs/*.log`

- Created by: structlog file handler at first log emission per phase.
- Phase mapping: `cli.log` (status, reset, auth), `sync.log` (sync),
  `scanner.log` (scan), `server.log` (serve).
- Format: newline-delimited JSON. Every event has `timestamp` (ISO
  UTC), `level`, `event`, plus any extra key-value pairs.
- Rotation: none in v1. Logs grow indefinitely; a periodic `reset`
  truncates them. v2 backlog item.
- Deleted by: default `inbox-scanner reset`.

## Reset semantics

[`inbox-scanner reset`](cli.md#reset) implements four behaviours:

| Variant | OAuth (token + credentials) | `state.db` | `attachments/` | `extracted/` | `logs/` |
|---|---|---|---|---|---|
| `reset` | ✓ keep | wipe | wipe | wipe | wipe |
| `reset --keep-attachments` | ✓ keep | wipe | ✓ keep | wipe | wipe |
| `reset --keep-extractions` | ✓ keep | wipe | wipe | ✓ keep | wipe |
| `reset --keep-attachments --keep-extractions` | ✓ keep | wipe | ✓ keep | ✓ keep | wipe |
| `reset --all` | wipe | wipe | wipe | wipe | wipe (entire dir gone) |

Confirmation prompt unless `-y`. Source of truth:
[`cli.py::_planned_reset_targets`](../inbox_scanner/cli.py) and
`_execute_reset` immediately below it.

Recovery from each:

- After default `reset`: `inbox-scanner sync` re-pulls everything.
  Token survives so no re-OAuth.
- After `reset --keep-attachments`: `sync` rebuilds DB rows but blob
  storage is intact, so no Gmail bandwidth.
- After `reset --keep-attachments --keep-extractions`: `scan
  --only-detect` is enough to recover all findings.
- After `reset --all`: redo README from step 3 (Cloud Console OAuth
  client setup).

## Idempotence and resumability guarantees

| Action | Property |
|---|---|
| `sync` twice in a row | second run does ≈ zero work (the listing pass still happens; DB writes don't) |
| Ctrl-C during `sync` | next `sync` resumes; in-flight message marked `sync_error`, retried |
| `scan` twice in a row | bit-for-bit identical `detections` + `message_verdicts` tuple sets (modulo `scan_id` and timestamps) |
| `scan` after detector tuning | old detections are dropped and replaced; verdicts re-aggregate from fresh findings |
| `scan --force-extract` | re-runs Docling on every attachment; same blob bytes → same `content_hash` → same on-disk `.md` filename (overwritten in place) |
| `auth` twice | second run repeats the browser flow and overwrites `token.json` |
| `reset` then `sync` then `scan` | full re-population; produces the same verdict set as before reset (assuming no Gmail-side changes) |

## Migration discipline

Schema changes follow the standard Alembic flow. The CLI auto-applies
migrations on every invocation
([`migrations.py::apply_migrations`](../inbox_scanner/migrations.py)),
so end users never need to run `alembic` directly. Developers do, to
generate new revisions:

```sh
# 1. Edit inbox_scanner/models.py
# 2. Bring a tmpdir DB to current head:
TMP=$(mktemp -d)
INBOX_SCANNER_DATA_DIR=$TMP uv run alembic upgrade head
# 3. Generate the new revision against that tmpdir:
INBOX_SCANNER_DATA_DIR=$TMP uv run alembic revision --autogenerate -m "<slug>"
# 4. Review alembic/versions/<rev>_<slug>.py — autogenerate misses
#    constraint-only changes and never picks up data backfills.
```

See [Data model § migrations](data-model.md#migrations) for the
history of revisions.

## Disk usage expectations

| Component | Typical size |
|---|---|
| `state.db` | 1–10 MB per 1,000 messages |
| `attachments/blobs/` | dominated by user content — 100 MB to 200 GB depending on inbox |
| `extracted/` | small (~50 KB per attachment as markdown) |
| `logs/` | grows over time; tens of MB after months of regular use |
| HF caches | ~5 GB combined, one-time |

`inbox-scanner status` prints `Total bytes on disk` for the blob
store, computed at request time.

The plan flagged adding a `--max-total-bytes` safety cap to sync; it's
not yet wired (v1 caveat). The current default `max_total_bytes` in
`config.yaml` is 100 GB but the sync pipeline doesn't enforce it. v2.

## See also

- [Data model](data-model.md) — what lives in `state.db`.
- [CLI § reset](cli.md#reset) — the user-facing flags.
- [Security](security.md) — why the data dir needs FileVault.
