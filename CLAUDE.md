# CLAUDE.md

Context for Claude Code working autonomously on this repo. Read it once per session before making changes.

## What this is

Local-first, **strictly read-only** Gmail PII scanner. Two-phase design:

1. **Sync** (`inboxaudit sync`) — talks to Gmail, downloads message metadata + attachment bytes into content-addressed blob storage. Idempotent and resumable.
2. **Scan** (`inboxaudit scan`) — fully offline. Extracts text via Docling 2.x, runs Presidio + Privacy Filter + custom-regex detection, writes findings + per-message verdicts to SQLite.

`inboxaudit serve` exposes a read-only FastAPI on `127.0.0.1:8765` with an Alpine.js review UI at `/`.

**`docs/archives/IMPLEMENTATION_PLAN.md` is the authoritative spec** — data model, module structure, detector list, build-order, v2 backlog. Read it before making non-trivial changes; update it in the same change if you diverge.

## Repository status

v1 complete. All 10 build-order steps shipped, 121 tests passing, browser-tested on the dev corpus. The user wants to review everything before declaring shipped — don't say "v1 done" in commits or docs.

## Working environment

This project uses **`uv`** with a project-local `.venv/`. **Never `pip install` outside the venv. Never use plain `python` or `pytest` — always `uv run`.**

| Command | What it does |
|---|---|
| `uv sync` | Install/refresh deps from `pyproject.toml` + `uv.lock` |
| `uv add <pkg>` | Add a runtime dep |
| `uv add --dev <pkg>` | Add a dev dep |
| `uv run pytest -q` | Run the full test suite (~4 s) |
| `uv run pytest tests/test_X.py -v` | Run one test file verbosely |
| `uv run inboxaudit <cmd>` | Run any CLI command inside the venv |
| `INBOXAUDIT_DATA_DIR=$(mktemp -d) uv run alembic revision --autogenerate -m "..."` | Generate a new migration after model changes |
| `uv build --wheel` | Build a wheel under `dist/` |

Python is pinned to **3.11** via `.python-version`.

### Runtime data lives inside the repo during dev

`load_settings()` auto-detects:

| Mode | Trigger | data_dir |
|---|---|---|
| Source checkout (dev) | `pyproject.toml` reachable upward | `<repo>/.inboxaudit-data/` |
| Installed wheel (end user) | no `pyproject.toml` upward | `~/.inboxaudit/` |
| Explicit override | `INBOXAUDIT__DATA_DIR` env var | wins over both |

Everything (SQLite, blobs, extracted markdown, logs, OAuth artefacts) lands at `<repo>/.inboxaudit-data/` during dev. Gitignored. Easy to nuke (`inboxaudit reset --all -y`).

- **Do not** introduce a `.env` file. `.gitignore` blocks `.env*` defensively. Per-environment overrides go in `<data_dir>/config.yaml` or shell env vars.
- **Do not** point dev runs at `~/.inboxaudit/` — that's the documented end-user default.
- Tests pass `INBOXAUDIT__DATA_DIR=<tmpdir>` via the `fresh_data_dir` fixture in `tests/test_server.py` (and the populated variant in `tests/test_reset.py`). Reuse those fixtures rather than rolling your own.
- Migrations auto-run on every CLI invocation via `inboxaudit.migrations.apply_migrations`. Run alembic by hand only when generating new revisions.

## Architecture: keep the two phases separate

The sync/scan split is the single most important design decision. Don't collapse it.

- `gmail/` — only touched during sync. Don't import from it elsewhere.
- `extraction/router.py` — single mime-allowlist decision: Docling or `unparseable`. PDFs are not pre-classified; Docling auto-OCRs scanned ones via `do_ocr=True`.
- `extraction/docling_extractor.py` — singleton `DocumentConverter`, `DocumentStream(name=, stream=BytesIO)` API, returns markdown via `result.document.export_to_markdown()`.
- `detection/{presidio,privacy_filter}_detector.py` — two independent detectors returning `Finding` dataclasses. A third `custom_regex` detector existed in earlier iterations but was retired during v1 simplification; see [ADR 0005](docs/decisions/0005-three-detector-pipeline.md) for the trail.
- `detection/categorizer.py` — single source of truth for `(detector, subtype) → (user_category, criticality_tier)` in the `_REGISTRY` dict (values are `_Entry` NamedTuples). Adding a new detector subtype means adding **one** row; a coverage test enforces that both fields are valid. The tier (`critical | standard | all`) drives the `--profile` filter — see `Profile` in `detection/types.py`.
- `pipelines/sync_pipeline.py` and `pipelines/scan_pipeline.py` — orchestrators, one per phase, share DB/blob/config but are otherwise independent.

DB: `syncs`/`scans` are run tables; `messages`/`attachments` carry sync state; `detections`/`message_verdicts` are scan-scoped (rewritten every scan).

## Load-bearing constraints

- **Read-only Gmail scope only** (`gmail.readonly`). Never request a write scope. Each user supplies their own OAuth client.
- **Localhost only.** `inboxaudit serve` binds `127.0.0.1`. Warn loudly if `--host` overrides.
- **Content-addressed blob storage.** `<data_dir>/attachments/blobs/<sha[:2]>/<sha[2:4]>/<sha>`. Identical bytes share a blob; multiple `attachments` rows can point to the same `content_hash`. Cache extraction by hash. Per-attachment delete is unsafe — out of scope for v1.
- **Gmail rate limit:** token-bucket at 20 req/sec global, exp backoff with jitter on 429/503, 4 worker tasks. Implementation: `inboxaudit.gmail.rate_limiter.TokenBucket`.
- **Gmail attachment IDs expire** after a few hours. Composite primary key on `attachments` is `(message_id, part_id)` — `part_id` is documented immutable. The volatile `gmail_attachment_id` lives in its own column and is refreshed on every metadata fetch. **Never** put `gmail_attachment_id` in a primary key.
- **Single extraction backend (Docling 2.x).** The plan originally called for a Qwen-VL via `llama-server` second backend; we collapsed to Docling-only after testing showed its built-in OCR handles everything. **Don't reintroduce a separate VLM backend.** If quality drops on a class of attachment, opt into Docling's own `do_picture_description=True` (loads SmolVLM in-process) before reaching for a second HTTP service.
- **Sync skip filters happen pre-download** (mime denylist, <1 KB, >50 MB, inline Content-ID images). Goal: don't pull bytes we'll never use.
- **Data dir holds plaintext PII.** README must call this out + recommend FileVault. No SQLCipher in v1.

## Non-goals (don't add without explicit ask)

No write access to Gmail. No daemon/incremental mode. No email-body scanning. No further national-ID expansion beyond the Tier A international set (UK, ES, IT, AU, SG, IN, PL, FI — see [ADR 0006](docs/decisions/0006-international-pii-tier-a.md)). No user-configurable detection rules. No encrypted-at-rest DB. No Outlook/IMAP/Yahoo. No multi-user. No bulk-actions UI. v2 backlog is at the bottom of the plan.

## CLI surface

```
inboxaudit auth                                              # OAuth handshake; saves token.json
inboxaudit sync   [--limit N] [--since YYYY-MM-DD]           # Phase 1
inboxaudit scan   [--force-extract] [--only-extract|--only-detect] [--profile critical|all] [--detectors presidio|privacy_filter|all]  # Phase 2
inboxaudit serve  [--host HOST] [--port 8765]                # FastAPI + UI
inboxaudit status                                            # sync + scan + verdict tables
inboxaudit reset  [--keep-attachments] [--keep-extractions] [--all] [-y]
```

## Code conventions

- **Type hints required** on every public function. Use `from __future__ import annotations` so PEP 604 (`X | None`) works on 3.11. Prefer `pathlib.Path` over strings for paths.
- **Module docstring on every module** explaining what it does and any non-obvious design choice. Public functions get a short docstring; trivial helpers don't. Match the existing style — comments explain *why*, not *what*.
- **Logging:** use `inboxaudit.logging.get_logger(name)`. Structured key=value via `log.info("event_name", k=v, …)`. Console handler is lifted to WARNING during long sync/scan runs (see `_quiet_console_logging` in `cli.py`); INFO still hits the file log.
- **Errors at boundaries:** raise typed exceptions for user-facing failures (e.g. `CredentialsMissing` in `gmail/auth.py`). The CLI catches them and prints a clean message — never let stack traces surface in `auth`/`serve` paths.
- **Async DB writes via `asyncio.to_thread`.** SQLAlchemy is sync. Async pipelines wrap DB calls in `asyncio.to_thread(...)` and use `session_scope()` for transaction boundaries. SQLite WAL handles 4-worker concurrency.
- **Silence noisy third-party loggers at use site.** Pattern: at first call to `_get_engine()` / `_get_pipeline()`, lift the relevant `logging.getLogger("...")` to `ERROR` (we already do this for `presidio-analyzer`, `transformers`, `huggingface_hub`, `alembic`). Don't try to silence them globally at startup — it interacts poorly with rich progress bars.

## Testing

- 121 tests, ~6 s wall clock. `uv run pytest -q`.
- Heavy detection deps (Presidio + Privacy Filter) are loaded lazily; tests that exercise them stay fast because singletons load once per process.
- **Don't write tests that hit real Gmail or load real models.** Unit-test pure helpers (router, categorizer, regex patterns, span merger, blob storage, rate limiter, reset planning). Integration-test the API via FastAPI's `TestClient` with `fresh_data_dir`.
- After model or schema changes: regenerate migrations against a tmpdir (see the alembic command in the table above).

## Common workflows

**Adding a new detector subtype:**
1. Make the detector emit `Finding(detector=..., subtype="...")`.
2. Add one row to `_REGISTRY` in `detection/categorizer.py`: `("detector", "subtype"): _Entry(category, tier)`. Category must be in `RISK_WEIGHTS`; tier must be `critical | standard | all`. Both invariants are enforced by `tests/test_categorizer.py::test_every_registry_entry_is_valid`.
3. Add positive + negative tests in the appropriate `tests/test_*` file.

**Changing a SQLAlchemy model:**
1. Edit `inboxaudit/models.py`.
2. `INBOXAUDIT_DATA_DIR=$(mktemp -d) uv run alembic upgrade head` to bring a tmpdir DB to current head.
3. Same env var for: `uv run alembic revision --autogenerate -m "<msg>"` to generate the migration.
4. Review the generated file in `alembic/versions/` — autogenerate sometimes misses constraint-only changes and never picks up data backfills.

**UI changes to `frontend/index.html`:**
1. `uv run inboxaudit serve`, visit `http://127.0.0.1:8765`.
2. Open browser devtools and check the console. Alpine evaluates templates eagerly even under `x-show=false`, so always use optional chaining (`stats?.scan?.x ?? 0`) for fields that load asynchronously.
3. The Playwright MCP tools are the right way to drive a real browser session for smoke tests — but **never commit screenshots**: they render real PII from the dev corpus and `.gitignore` blocks `*.png` at the repo root for that reason.

**Smoke-testing the full pipeline against a tmpdir** (no risk to dev data):
```sh
TMP=$(mktemp -d)
INBOXAUDIT__DATA_DIR=$TMP uv run inboxaudit status   # bootstraps schema
# … drop credentials.json into $TMP, then …
INBOXAUDIT__DATA_DIR=$TMP uv run inboxaudit auth
INBOXAUDIT__DATA_DIR=$TMP uv run inboxaudit sync --limit 5
INBOXAUDIT__DATA_DIR=$TMP uv run inboxaudit scan
```

## Commit style

Subject ≤ 70 chars, imperative mood, no period. Body explains *why*, what tradeoffs were considered, and concrete numbers from local testing where relevant ("scan dropped from 118 → 61 findings"). Reference the build-order step. **No `Co-Authored-By` footer in this repo** — the user has explicitly opted out.

Recent commits to match in tone:

```
Add async attachment downloads with rate limiting
Collapse extraction to Docling-only; drop the qwen-vl/llama-server route
Verify full-scan idempotence and tidy detect-stage console output
Add read-only FastAPI server; fix Privacy Filter span splitting
```

## External services the user runs themselves

- **Google Cloud OAuth client.** User creates the project, enables Gmail API, drops `credentials.json` into the data dir. If missing, `inboxaudit auth` prints exactly what to do — don't let stack traces surface.

That's the only external dependency. (Earlier plan had `llama-server`; removed when we collapsed to Docling-only.)

## Gotchas

- `messages.list?q=has:attachment` returns ~30% noise (inline images, meeting invites). Sync's pre-download skip filters handle this; don't double-filter downstream.
- Use Gmail **message IDs** as primary keys (stable). Thread IDs can shift.
- **Gmail attachment IDs expire** — composite key is `(message_id, part_id)`, never `(message_id, gmail_attachment_id)`. The latter goes in its own column and is refreshed each metadata fetch.
- **Privacy Filter span splitting.** `aggregation_strategy="simple"` doesn't merge BIE → S boundaries; we post-process via `_merge_adjacent_same_subtype` in `privacy_filter_detector.py`. If you change the aggregation strategy, retest the merger.
- **First scan downloads ~5 GB of models** under `~/.cache/huggingface/hub/`: ~2 GB Docling layout/table/OCR + ~2.6 GB Privacy Filter. The `docling.first_call_may_download_models` log line fires once per process.
- **`opencv-python-headless` (not `opencv-python`)** is an explicit dep. Docling's `OcrAutoOptions` may pick EasyOCR on macOS, which imports `cv2`; the GUI-flavored wheel often fails to load on headless Macs. If you ever see `ModuleNotFoundError: No module named 'cv2'` from a Docling extraction, check the headless variant is installed.
- Re-running `scan` rewrites `detections` and `message_verdicts` (scan-scoped). Extraction results are cached by `content_hash` and skipped unless `--force-extract`. Identical bytes from multiple emails share one `.md` cache file.
- **Reset semantics:** `inboxaudit reset` (default) keeps OAuth artefacts and wipes everything else. `--keep-attachments` / `--keep-extractions` are composable. `--all` nukes the entire data dir. Confirmation prompt unless `-y`.
- **Browser-test screenshots from Playwright contain real PII** from the dev corpus. `.gitignore` blocks `*.png` / `*.jpg` / `*.jpeg` at the repo root and `.playwright-mcp/`. Never commit them.
