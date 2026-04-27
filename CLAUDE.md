# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Greenfield (scaffolding in progress). **`docs/IMPLEMENTATION_PLAN.md` is the authoritative spec** for v1 — read it before making non-trivial changes. It defines the data model, module layout, CLI surface, detector list, config schema, and a recommended 11-step build order. Treat it as the source of truth; if you diverge, update the plan in the same change.

## Working environment — read first

This project uses **`uv`** for packaging and a project-local virtual environment at `.venv/`. **Never install anything globally and never `pip install` outside the venv.**

- `uv sync` — install/refresh deps from `pyproject.toml` + `uv.lock` into `.venv/`.
- `uv add <pkg>` — add a runtime dep (writes to `pyproject.toml` and lockfile).
- `uv add --dev <pkg>` — add a dev dep.
- `uv run <cmd>` — run any command inside the venv without manual activation. Prefer this over `source .venv/bin/activate`. Examples: `uv run inbox-scanner --help`, `uv run pytest`, `uv run alembic upgrade head`.
- Python is pinned to 3.11 via `.python-version`. The plan requires 3.11+.

If you find yourself reaching for `python`, `pip`, or `pytest` directly, stop — prefix it with `uv run`.

### Runtime data lives inside the repo during dev

`load_settings()` auto-detects whether it's running from a source checkout (a `pyproject.toml` is reachable upward from cwd) and picks the data directory accordingly:

| Mode | Trigger | data_dir |
|---|---|---|
| **Source checkout (dev)** | `pyproject.toml` found upward | `<repo>/.inbox-scanner-data/` |
| **Installed wheel (end user)** | no `pyproject.toml` upward | `~/.inbox-scanner/` (documented in plan) |
| **Explicit override** | `INBOX_SCANNER__DATA_DIR` env var or `data_dir=` arg | wins over both above |

So everything (SQLite, blobs, extracted text, logs, models, OAuth token, `credentials.json`) lands at `<repo>/.inbox-scanner-data/` during dev — gitignored, easy to inspect, easy to nuke. No `.env` file, no template to copy, no manual setup.

- **Do not** introduce a `.env` file. `.gitignore` blocks `.env*` defensively. Real secrets and per-environment overrides go in `<data_dir>/config.yaml` (gitignored as part of `.inbox-scanner-data/`) or in shell-exported env vars — never in a tracked dotfile.
- **Do not** point dev runs at `~/.inbox-scanner/` — that's the documented end-user default; leave it clean.
- Tests / CI should pass `INBOX_SCANNER__DATA_DIR=<tmpdir>` explicitly. Relative paths there resolve against the project root, so they work from any cwd inside the repo.
- Alembic reads the same resolved path via `alembic/env.py`, so `uv run alembic upgrade head` targets the repo-local DB automatically. **Migrations also run automatically on every CLI invocation** via `inbox_scanner.migrations.apply_migrations` — a fresh data dir Just Works without a manual `alembic upgrade` step. Run alembic by hand only when generating new revisions.

## What the tool is

A self-hosted, **strictly read-only** local CLI + web UI that scans a Gmail inbox for emails with sensitive attachments (gov IDs, financial, tax, medical, credentials, legal docs), extracts text, runs PII detection, and lets the user review flagged emails one at a time. Acting on a flagged email always means opening it in Gmail's web UI manually — the tool never writes to the mailbox.

Target environment: macOS on Apple Silicon, single user, Python 3.11+, `uv` for packaging.

## Architecture: two independent phases

This is the single most important design decision and it shapes every module. Do not collapse the phases.

1. **Sync (`inbox-scanner sync`)** — network-bound, Gmail API, idempotent on re-run. Downloads message metadata and raw attachment bytes into content-addressed blob storage. Run once; expensive.
2. **Scan (`inbox-scanner scan`)** — fully offline. Runs extraction (Docling for born-digital, Qwen2.5-VL via local `llama-server` for scans/images) and detection (Presidio + OpenAI Privacy Filter + custom regex) against the local cache. Re-runnable any number of times with different thresholds/detectors without touching Gmail.

Within scan, extraction and detection are separately runnable (`--only-extract`, `--only-detect`) because extraction (especially VLM) is the expensive step and you want to iterate on detectors without paying it again.

The DB schema mirrors this split: `syncs` and `scans` are separate run tables; `messages` and `attachments` carry sync state; `detections` and `message_verdicts` are rewritten on every scan. See plan §"Database schema".

## Load-bearing constraints

- **Read-only Gmail scope only** (`gmail.readonly`). Never request or use a write scope. Each user supplies their own OAuth client; we don't run a verified app.
- **Localhost only.** FastAPI binds `127.0.0.1`. No auth, no remote access, warn loudly if the user overrides the host.
- **Content-addressed blob storage.** Attachments live at `~/.inbox-scanner/attachments/blobs/<sha[:2]>/<sha[2:4]>/<sha>`. Two messages with identical attachments share a blob. Multiple `attachments` rows can point to the same `content_hash`; cache extraction keyed on hash. Deletion of a single attachment is therefore not safe — out of scope for v1.
- **Gmail rate limit:** token-bucket at 20 req/sec global, exp backoff with jitter on 429/503. 4 worker tasks max.
- **VLM concurrency cap:** max 2 concurrent calls to `llama-server` (a 7B model on a single Apple Silicon box can't sustain more). Configurable, but don't raise the default.
- **Sync skip filters happen pre-download** (mime denylist, <1KB, >50MB, inline `Content-ID` images) — the goal is to avoid pulling bytes we'll never use.
- **Data dir holds plaintext PII** (raw attachments, extracted text, PII spans in SQLite). README must call this out and recommend FileVault. No SQLCipher in v1.

## Non-goals (do not add without explicit ask)

No write access to Gmail, no daemon/incremental mode, no email-body scanning, no non-US ID patterns, no user-configurable detection rules, no encrypted-at-rest DB, no Outlook/IMAP, no multi-user, no bulk actions. v2 backlog is enumerated at the bottom of the implementation plan.

## Intended module layout

Defined in plan §"Module structure". Key boundaries:

- `gmail/` — only touched in Phase 1. Once sync is done, nothing else imports from it.
- `extraction/router.py` — single decision point for docling vs qwen-vl vs unparseable, based on mime + (for PDFs) text-layer sniff via `pypdfium2`.
- `detection/categorizer.py` — maps raw detector labels (Presidio + Privacy Filter + custom regex) to user-facing categories (`gov_id`, `financial`, `tax`, `medical`, `credentials`, `legal`, `other_pii`). A message flags only on the first six; `other_pii` alone is informational.
- `pipelines/sync_pipeline.py` and `pipelines/scan_pipeline.py` — orchestrators, one per phase, share DB/blob/config infra but are otherwise independent.

## Commands (once implemented)

```
inbox-scanner auth                                  # OAuth flow, writes token.json
inbox-scanner sync [--limit N] [--since YYYY-MM-DD] # Phase 1 — idempotent, resumable
inbox-scanner scan [--force-extract] [--only-extract|--only-detect]  # Phase 2
inbox-scanner serve [--port 8765]                   # FastAPI + Alpine.js UI on 127.0.0.1
inbox-scanner status
inbox-scanner reset [--keep-token|--keep-attachments|--keep-extractions]
```

`uv run inbox-scanner ...` once `pyproject.toml` exists. No build/test commands defined yet — when you add them (likely `uv run pytest`, `uv run ruff`), update this file.

## External services the user runs themselves

- **`llama-server`** with Qwen2.5-VL-7B-Instruct GGUF + `--mmproj` for vision. Default endpoint `http://127.0.0.1:8080/v1` (OpenAI-compatible). The scanner is an HTTP client; it does not start or manage llama-server.
- **Google Cloud OAuth client** — user creates the project, enables Gmail API, drops `credentials.json` into `~/.inbox-scanner/`.

If either is missing, fail with a clear actionable error pointing to the README, not a stack trace.

## Gotchas worth remembering

- `messages.list?q=has:attachment` returns ~30% noise (inline images, meeting invites). Skip filters in sync handle this; don't double-filter downstream.
- Privacy Filter is a token classifier (`transformers.pipeline("token-classification", ...)`), not generative. ~3 GB first-run download. Don't try to route it through Ollama or llama.cpp.
- Docling's first run downloads ~2 GB of layout/table models — surface this in the CLI.
- Qwen2.5-VL needs both the model GGUF **and** the mmproj file; llama-server silently runs without vision if mmproj is missing.
- Use Gmail **message IDs** as primary keys (stable). Thread IDs can shift.
- Re-running `scan` deletes prior `detections` and `message_verdicts` for the affected scope and rewrites them. Extraction results are cached by `content_hash` and skipped unless `--force-extract`.
