# CLI reference

Typer app at
[`inbox_scanner/cli.py`](../inbox_scanner/cli.py). Entry point
`inbox-scanner` (declared as a console script in `pyproject.toml`).

All commands share a small bootstrap pass:

```python
def _bootstrap(phase: str) -> Settings:
    settings = load_settings()
    configure_logging(settings.logs_dir, phase=phase)
    apply_migrations(settings)
    return settings
```

Side effects:

1. Resolve the data dir (`<repo>/.inbox-scanner-data/` in dev,
   `~/.inbox-scanner/` for installed wheels, `INBOX_SCANNER__DATA_DIR`
   wins over both).
2. Create directory skeleton if missing.
3. Configure structlog (console + file handler).
4. Auto-run Alembic to bring the DB to head.

`uv run inbox-scanner --help` lists every subcommand. Each one has its
own `--help` with full flag documentation.

## Commands

### `auth`

Interactive OAuth flow. Opens a browser, walks the user through
Google's consent screen, persists the resulting token to
`token.json`.

```sh
uv run inbox-scanner auth
```

**Prerequisites:** `credentials.json` (Google OAuth client of type
"Desktop app") in the data dir. If absent, prints the README-pointing
error from `gmail/auth.py::CredentialsMissing` and exits non-zero.

**Output:** confirmation line with the saved token path and the
granted scopes.

After this, every other Gmail-using command reuses the token (with
silent refresh) until it expires or is revoked.

### `sync`

Phase 1. Downloads message metadata + attachment bytes for every
message matching `has:attachment`.

```sh
uv run inbox-scanner sync [--limit N] [--since YYYY-MM-DD] [--resume / --no-resume]
```

| Flag | Default | Notes |
|---|---|---|
| `--limit N` | unbounded | Stop after N messages. Useful for first-run smoke tests |
| `--since DATE` | none | Pass `after:DATE` to Gmail's query. ISO-8601 format only — invalid dates fail with a Typer `BadParameter` |
| `--mailbox SCOPE` | `all` | `all` (default, matches every label except spam/trash — inbox + sent + archive), `inbox`, or `sent`. Persisted on the `Sync` row as `mailbox_scope` so `status` can show which scope each run used |
| `--resume / --no-resume` | `--resume` | Default behavior is always idempotent; the flag is kept for documentation. `--no-resume` is currently a no-op |

**Idempotent.** Skips messages that are fully synced. Re-pulls
`sync_status IN ('pending', 'sync_error')`. Safe to Ctrl-C and re-run.

**Outputs:** rich progress bar (per-message). Console log handler is
lifted to WARNING for the duration so the bar isn't fighting log
lines; the file log keeps INFO.

See [Sync pipeline](sync-pipeline.md) for the full mechanics.

### `scan`

Phase 2. Extracts text from cached attachments and runs detection.

```sh
uv run inbox-scanner scan [--force-extract] [--only-extract] [--only-detect]
```

| Flag | Default | Notes |
|---|---|---|
| `--force-extract` | `false` | Re-run extraction even on attachments already `extraction_status='extracted'`. Skips the content-hash cache |
| `--only-extract` | `false` | Run stage A and stop; skip detection |
| `--only-detect` | `false` | Skip extraction; run detection against the cached markdown only |
| `--profile SCOPE` | `critical` | Detection filter: `critical` (default — irreversible-harm entities only: SSN, passport, credit card, IBAN, US bank, ITIN, driver's license, secret, BIP-39 mnemonic), `standard` (adds `account_number` + `tax_form`), or `all` (adds informational `other_pii` context: names, addresses, emails, phones, URLs, dates). Detection still runs in full; the profile filters what gets persisted. Persisted on the `Scan` row as `config_snapshot.profile` |

`--only-extract` and `--only-detect` are **mutually exclusive**.
Passing both fails with a Typer `BadParameter`.

**No-flags behavior:** runs both stages end-to-end. Detection is
always a per-scan rewrite — re-running scan produces bit-for-bit
identical detection + verdict tuples.

**First run cost:** ~5 GB of models lazy-downloaded into
`~/.cache/huggingface/hub/` (Docling layout/table/OCR + Privacy
Filter). Subsequent runs reuse cached weights.

**Outputs:** two rich progress bars (`Extracting`, `Detecting`); only
the relevant one is visible if `--only-extract` / `--only-detect` was
passed.

See [Scan pipeline](scan-pipeline.md).

### `serve`

Start the FastAPI review server.

```sh
uv run inbox-scanner serve [--host HOST] [--port PORT]
```

| Flag | Default | Notes |
|---|---|---|
| `--host HOST` | `127.0.0.1` | Override at your own risk; the scanner prints a loud red warning before binding to any non-loopback address |
| `--port PORT` | `8765` | |

**Read-only.** No POST/PUT/DELETE routes exist. See [API](api.md).

Uvicorn's per-request access log is disabled. Ctrl-C exits cleanly.

### `status`

Print a dashboard of the current state.

```sh
uv run inbox-scanner status
```

Outputs (in order, with sections suppressed when not yet populated):

1. `data_dir` and `db` paths.
2. **Last sync** — sync row, status, started/finished, message counts.
3. **Messages** table — counts by `sync_status`.
4. **Attachments** table — counts by `sync_status` (`downloaded`,
   `pending`, `skipped`).
5. **Last scan** — scan row, status, processed count.
6. **Extraction** table — counts by `extraction_status` for
   downloaded attachments.
7. **Detection** counters — total findings, flagged-message count,
   verdict count.
8. **Flagged messages by top category** — table.
9. **Top 5 flagged by risk score** — table with risk, top_category,
   short sender, short subject.

Performs no work — just reads. Safe to run anytime.

### `reset`

Wipe local state. Default behavior preserves the OAuth artefacts
(token + credentials) so the user doesn't need to redo sign-in.

```sh
uv run inbox-scanner reset [--keep-attachments] [--keep-extractions] [--all] [-y]
```

| Flag | Default | Effect |
|---|---|---|
| (none) | — | Wipe `state.db`, `attachments/`, `extracted/`, `logs/`. Keep `token.json`, `credentials.json` |
| `--keep-attachments` | `false` | Additionally preserve `attachments/` (skip the next sync's downloads) |
| `--keep-extractions` | `false` | Additionally preserve `extracted/` (skip the next scan's extraction) |
| `--all` | `false` | Wipe the entire data directory, including OAuth artefacts. Forces re-running README step 3 |
| `--yes` / `-y` | `false` | Skip the confirmation prompt |

Prints the list of paths it intends to delete and prompts for
confirmation; `n` aborts with exit 1.

After a default reset the next `inbox-scanner sync` re-pulls
everything from Gmail. After a reset with `--keep-attachments` the
blobs survive, so the next sync is much cheaper (it re-creates DB rows
but reuses the on-disk files via content-hash dedup).

## Global behaviors

### Migration on every invocation

`apply_migrations(settings)` runs as part of every command's
bootstrap. The implementation in
[`inbox_scanner/migrations.py`](../inbox_scanner/migrations.py)
short-circuits when the DB is already at head (it checks
`MigrationContext.get_current_revision()`), so the overhead is
microseconds.

### Friendly error paths

User-facing failures raise typed exceptions that the CLI catches and
prints cleanly:

- `gmail.auth.CredentialsMissing` — `auth` (no `credentials.json`)
  and `sync` (no `token.json`).
- `migrations.AlembicConfigMissing` — only fires from an installed
  wheel without bundled migrations; v1 ships from source so we don't
  hit it in practice.

Stack traces never reach the user from `auth`, `sync`, `serve`, or
`reset`. They do reach the user from `scan` if Docling explodes — by
design, since those are bugs we want to surface.

### Console quieting during long-running commands

`sync` and `scan` wrap their bodies in `_quiet_console_logging()`,
which lifts the stderr `StreamHandler` to `WARNING` for the duration.
The file log keeps `INFO`. This is what lets the rich progress bar
stay readable without losing the per-event audit trail.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | User-facing error (missing credentials, aborted reset prompt, `_not_implemented` stubs) or Typer validation error (rendered by Typer itself) |
| Other | Unhandled exception bubbling out of the runtime |

## Examples

```sh
# Full first-run flow
uv run inbox-scanner auth
uv run inbox-scanner sync --limit 5    # smoke-test the OAuth + sync chain
uv run inbox-scanner sync              # full sync
uv run inbox-scanner scan              # extract + detect, ~5 GB models on first run
uv run inbox-scanner serve             # open http://127.0.0.1:8765
```

```sh
# Iterate on detector tuning without re-pulling from Gmail
# (edit config.yaml or detection thresholds, then:)
uv run inbox-scanner scan --only-detect
```

```sh
# Try a different extractor configuration
uv run inbox-scanner reset --keep-attachments -y
uv run inbox-scanner scan
```

```sh
# Nuke and start completely over
uv run inbox-scanner reset --all -y
# Now redo steps 3+ of the top-level README.
```

## See also

- [Sync pipeline](sync-pipeline.md), [Scan pipeline](scan-pipeline.md)
  — the two big commands explained in depth.
- [Operations](operations.md) — data dir layout that `status` and
  `reset` operate on.
