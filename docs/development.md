# Development guide

How to set up the dev environment, run tests, follow the conventions,
and execute the common change workflows. Companion to
[`../CLAUDE.md`](../CLAUDE.md) — that file has the agent-facing
condensed version; this one is the human-facing prose.

## Environment setup

```sh
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone and sync
git clone https://github.com/lastlad/inbox-pii-scanner.git
cd inbox-pii-scanner
uv sync

# 3. Verify
uv run pytest -q              # 114 tests, ~4 s
uv run inbox-scanner --help
```

Python is pinned to 3.11 via `.python-version`. `uv` handles
everything from there: bootstrap of the interpreter, dependency
install, lockfile resolution.

**Always `uv run`.** Don't `pip install` outside the venv; don't run
bare `python` / `pytest`. The `uv run` prefix is enforced by
convention rather than tooling — but if you find yourself reaching
past it, something is off.

## Repository layout

| Path | What's there |
|---|---|
| `inbox_scanner/` | Source |
| `inbox_scanner/frontend/index.html` | Single-file Alpine.js UI |
| `alembic/` | Database migrations |
| `tests/` | Pytest suite |
| `docs/` | These reference docs |
| `docs/archives/IMPLEMENTATION_PLAN.md` | Historical v1 spec (don't edit) |
| `docs/decisions/` | ADRs (one file per major decision) |
| `README.md` | User-facing setup walkthrough |
| `CLAUDE.md` | AI-agent context |
| `pyproject.toml`, `uv.lock` | Dependency + build config |

Module-level documentation for the source lives [in
`docs/architecture.md`](architecture.md#component-map).

## Testing

### Running

```sh
uv run pytest -q                            # full suite, quiet
uv run pytest -v                            # verbose
uv run pytest tests/test_categorizer.py     # one file
uv run pytest -k "test_default_reset"       # one test by name pattern
```

### Layout

```
tests/
├── test_blobs.py                  # content-addressed blob storage
├── test_categorizer.py            # detection categorization + verdict math + profile filter
├── test_gmail_parsing.py          # pure helpers: header / date / MIME-tree walker
├── test_privacy_filter_merge.py   # BIE→S span merging
├── test_rate_limiter.py           # TokenBucket pacing under concurrency
├── test_reset.py                  # reset command (path planning + execution)
├── test_router.py                 # extraction mime-allowlist routing
├── test_server.py                 # FastAPI endpoints via TestClient
├── test_sync_classifier.py        # mime/size skip-filter rules
└── test_sync_query.py             # Gmail query string + MailboxScope
```

Tests are fast (a few seconds). Heavy deps (Presidio, Privacy Filter)
are loaded lazily via singletons inside the production code, and
tests stay fast because they don't exercise those code paths
themselves — they test pure helpers + integration via TestClient.

### What to test (and what not to)

**Test:**

- Pure helpers in `extraction/router.py`, `detection/categorizer.py`,
  `gmail/client.py`, `gmail/rate_limiter.py`,
  `gmail/sync.py::_classify_attachment` and `_build_query`,
  `blobs.py`, `cli.py::_planned_reset_targets` / `_execute_reset`.
- FastAPI endpoints against `TestClient` with a seeded sqlite tmpdir
  (see `tests/test_server.py::_seed_basic_corpus` for the canonical
  pattern).

**Don't test:**

- The accuracy of Presidio, Privacy Filter, or Docling. Those are
  upstream concerns; we'd be testing the model, not our code.
- Real Gmail API calls. Friends-of-friends-of-coverage is not worth
  the OAuth ceremony and rate-limit risk in CI.

### Isolated data dir fixture

The pattern that tests use to avoid touching the dev corpus:

```python
@pytest.fixture
def fresh_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("INBOX_SCANNER__DATA_DIR", str(tmp_path))
    settings = load_settings()
    apply_migrations(settings)
    return tmp_path
```

See `tests/test_server.py` and `tests/test_reset.py` for live use.
Reuse this fixture rather than rolling your own — `apply_migrations`
needs to happen before any session-using code.

## Code conventions

- **Type hints on every public function.** Use `from __future__ import
  annotations` at the top of every module so PEP 604 (`X | None`)
  syntax works on 3.11. Prefer `pathlib.Path` over strings for paths.
- **Module docstrings on every module** explaining what it does and
  any non-obvious design choice. Public functions get a short
  docstring. Trivial helpers don't.
- **Comments explain *why*, not *what*.** Match the existing tone:
  short, declarative, present-tense.
- **Logging via `inbox_scanner.logging.get_logger(name)`.** Use
  structured key/value pairs: `log.info("event_name", k=v, …)`. The
  console handler is lifted to WARNING during `sync` and `scan` long
  runs via `_quiet_console_logging()` in `cli.py`; INFO still hits
  the file log.
- **Errors at boundaries.** User-facing failures raise typed exceptions
  (e.g. `CredentialsMissing` in `gmail/auth.py`). The CLI catches them
  and prints a clean message. Don't let stack traces escape from
  `auth`, `sync`, `serve`, or `reset`.
- **Async DB writes via `asyncio.to_thread`.** SQLAlchemy is sync. The
  async pipelines wrap DB-touching calls in `asyncio.to_thread(...)`
  and use `session_scope()` for transaction boundaries. SQLite WAL
  handles 4-worker concurrent writes.
- **Silence noisy third-party loggers at use-site.** Lift the relevant
  `logging.getLogger("...")` to ERROR inside the singleton that
  instantiates the third-party object — see the pattern in
  `detection/presidio_detector.py::_get_engine` and
  `detection/privacy_filter_detector.py::_get_pipeline`. Don't
  silence them globally at startup; that interacts badly with rich
  progress bars.

## Common workflows

### Adding a new detector subtype

1. Update the detector to emit `Finding(detector="...", subtype="<new>")`.
2. Add one row to `detection/categorizer.py::_REGISTRY`:
   `("detector", "<new>"): _Entry(category, tier)`. The category must
   already be in `RISK_WEIGHTS`; the tier must be one of `critical`,
   `standard`, `all` (see [Profile](../inbox_scanner/detection/types.py)
   for the meaning).
3. `tests/test_categorizer.py::test_every_registry_entry_is_valid`
   enforces both invariants — failing test means a malformed row.
4. Add positive + negative tests in the appropriate `tests/test_*`
   file.

### Changing a SQLAlchemy model

```sh
# 1. Edit inbox_scanner/models.py
# 2. Bring a tmpdir DB to current head:
TMP=$(mktemp -d)
INBOX_SCANNER_DATA_DIR=$TMP uv run alembic upgrade head
# 3. Generate the migration:
INBOX_SCANNER_DATA_DIR=$TMP uv run alembic revision --autogenerate -m "<slug>"
# 4. Review alembic/versions/<rev>_<slug>.py — autogenerate misses
#    constraint-only changes and never picks up data backfills.
```

Schema changes that invalidate existing rows (column rename, key
change) need a data backfill or a data wipe in the migration's
`upgrade()`. See
`alembic/versions/1c965f28e09a_stable_attachment_composite_via_partid.py`
for an example that combines a column add with a delete-and-reset of
the affected rows.

### Changing the FastAPI surface

1. Define / update the Pydantic response model in `server.py` (top
   of the file, above the app factory).
2. Add / change the route handler. Use `Depends(_session)` for DB
   access.
3. Update the corresponding test in `tests/test_server.py` — most
   handlers already have at least one happy-path test and one
   error-path test (404, 422).
4. Document the endpoint in `docs/api.md` with example
   request/response.

### Changing the frontend

1. Edit `inbox_scanner/frontend/index.html`. Single file — no build
   step.
2. `uv run inbox-scanner serve` and visit `http://127.0.0.1:8765`.
3. Open browser devtools and watch for errors. Alpine evaluates
   templates eagerly even under `x-show=false`, so always use
   optional chaining (`stats?.scan?.x ?? 0`) for fields that load
   asynchronously. See [frontend § null-safety
   gotcha](frontend.md#null-safety-gotcha).
4. For full smoke tests, drive the page via the Playwright MCP tools.
   **Never commit the screenshots** — they render real PII from the
   dev corpus. `.gitignore` already blocks `*.png` etc. at repo root.

### Smoke-testing against a tmpdir (no risk to dev data)

```sh
TMP=$(mktemp -d)
INBOX_SCANNER__DATA_DIR=$TMP uv run inbox-scanner status   # bootstraps schema
# … drop credentials.json into $TMP, then …
INBOX_SCANNER__DATA_DIR=$TMP uv run inbox-scanner auth
INBOX_SCANNER__DATA_DIR=$TMP uv run inbox-scanner sync --limit 5
INBOX_SCANNER__DATA_DIR=$TMP uv run inbox-scanner scan
```

## Commit conventions

- Subject ≤ 70 chars, imperative mood, no period.
- Body explains *why*, what trade-offs were considered, and concrete
  numbers from local testing where relevant
  ("scan dropped from 118 → 61 findings on the dev corpus").
- No `Co-Authored-By` footer in this repo.
- Reference the build-order step in the body if applicable.

Sample subjects to match in tone:

```
Add async attachment downloads with rate limiting
Collapse extraction to Docling-only; drop the qwen-vl/llama-server route
Verify full-scan idempotence and tidy detect-stage console output
Add read-only FastAPI server; fix Privacy Filter span splitting
```

## Architecture decision records

When making a non-obvious design decision, add an ADR in
[`docs/decisions/`](decisions/). Numbered sequentially. Use the
template the existing ADRs follow: Status, Context, Decision,
Consequences. Keep them short — one screen is the target.

The current set:

- [0001 — two-phase architecture](decisions/0001-two-phase-architecture.md)
- [0002 — content-addressed blob storage](decisions/0002-content-addressed-blob-storage.md)
- [0003 — single extraction backend](decisions/0003-single-extraction-backend.md)
- [0004 — attachment composite key uses part_id](decisions/0004-attachment-key-uses-part-id.md)
- [0005 — three-detector pipeline](decisions/0005-three-detector-pipeline.md)

## Versioning and releases

There are no releases yet. Versioning kicks in when we publish a
wheel. v2 backlog has the polish items
([`archives/IMPLEMENTATION_PLAN.md`](archives/IMPLEMENTATION_PLAN.md)).

## See also

- [`../CLAUDE.md`](../CLAUDE.md) — agent-facing condensed version.
- [Architecture](architecture.md) — module boundaries.
- [Operations](operations.md) — what files the dev environment lays
  down and how to clean them up.
