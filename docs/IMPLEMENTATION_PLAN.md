# Inbox PII Scanner — v1 Implementation Plan

## What this is

A self-hosted, local-first tool that scans a Gmail inbox for emails containing sensitive attachments (IDs, financial documents, tax forms, medical records, credentials, legal docs), extracts the text from those attachments, runs PII detection on the extracted text, and presents flagged emails in a local web UI for the user to review one at a time. The tool is **strictly read-only** — it never modifies the user's mailbox. When the user wants to act on a flagged email, the UI provides a button that opens that specific email in Gmail's web interface, where the user does the actual cleanup manually.

> **Plan revision (2026-04-29):** the original plan called for a two-track extractor — Docling for born-digital documents, Qwen2.5-VL via local `llama-server` for images and scanned PDFs. Real-attachment testing showed Docling 2.x's native image pipeline (with on-by-default OCR via `OcrAutoOptions` → EasyOCR / Apple Vision / RapidOCR) handles every category we care about, including USPS shipping labels and marketing images, in <5s each on Apple Silicon. Single-backend extraction is the v1 design now. The build order below has been updated; step 5 (originally "Qwen2.5-VL extractor") collapsed into step 4. If we ever need handwriting or chart-data fallback, Docling's own opt-in `do_picture_description` flag wires in SmolVLM without bringing back a second HTTP service.

## Non-goals for v1

- No write access to Gmail (no labels, no archive, no delete, no quarantine).
- No incremental/daemon mode — one-shot historical sync, then offline scans.
- No email body scanning — attachments only.
- No multi-country ID patterns — US only.
- No user-configurable detection rules.
- No encrypted local storage — relies on user putting the data dir on an encrypted volume (FileVault is default on macOS).
- No Outlook/IMAP/Yahoo support.
- No multi-user support — single-user, runs locally.

## Two-phase design

The pipeline is split into **two independent phases** that can be run separately:

1. **Sync phase** (`inbox-scanner sync`): talks to Gmail, downloads message metadata and all attachment bytes to local disk. Network-bound, rate-limited, slow on first run, idempotent on re-run.
2. **Scan phase** (`inbox-scanner scan`): operates entirely on local files. Runs extraction (Docling/Qwen-VL) and detection (Presidio/Privacy Filter/regex). Can be re-run any number of times — to try different thresholds, new detectors, or after fixing a bug — without touching Gmail.

This means the user pays the Gmail API cost exactly once, then iterates locally as much as they want. It also makes development much faster, since you can sync a real inbox once and then iterate on extractors and detectors against cached data.

---

## Target environment

- **Platform:** macOS on Apple Silicon (M-series). Don't bother with Linux/Windows/Intel Mac compatibility for v1, but don't go out of the way to break them either.
- **User:** Technical (comfortable with CLI, Python, OAuth setup).
- **Deployment:** CLI to run scans + local FastAPI server with a simple HTML/JS frontend on the same machine (loopback only, never bind to 0.0.0.0).

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLI (Typer)                              │
│  Commands: auth, sync, scan, serve, status, reset                │
└────────────┬────────────────────────────────────────────────────┘
             │
             ├─── PHASE 1: SYNC (network-bound, run once) ────────┐
             ▼                                                      │
┌─────────────────────────────────────────────────────────────────┐│
│                Gmail Sync (async, throttled)                     ││
│                                                                  ││
│   List messages w/ has:attachment ──► Insert message stubs      ││
│            │                                                     ││
│            ▼                                                     ││
│   For each pending message:                                     ││
│     - Fetch full message (headers, structure)                   ││
│     - Download each attachment bytes → attachments/blobs/       ││
│     - Update messages.sync_status = 'synced'                    ││
│                                                                  ││
│   Idempotent: skips already-synced messages on re-run.          ││
└──────────────────────────────────────┬──────────────────────────┘│
                                       │                           │
             ┌─────────────────────────┴───────────────────────────┘
             │
             ├─── PHASE 2: SCAN (offline, re-runnable) ────────────┐
             ▼                                                      │
┌─────────────────────────────────────────────────────────────────┐│
│              Local Scan (no Gmail access)                        ││
│                                                                  ││
│   Read attachments from disk ──► Attachment Router              ││
│                                          │                       ││
│                                          ▼                       ││
│                              ┌─────────────────────────────┐    ││
│                              │ Docling (PDF, Office, image)│    ││
│                              │ + auto OCR for scans/photos │    ││
│                              └─────────────────────────────┘    ││
│                                          │                       ││
│                                          ▼                       ││
│                              ┌──────────────────┐               ││
│                              │   Detection      │               ││
│                              │ Presidio +        │               ││
│                              │ Privacy Filter +  │               ││
│                              │ custom regex      │               ││
│                              └──────────────────┘               ││
│                                          │                       ││
│                                          ▼                       ││
│                                   SQLite store                   ││
└──────────────────────────────────────┬──────────────────────────┘│
                                       │                           │
                                       ▼                           │
┌─────────────────────────────────────────────────────────────────┐│
│             FastAPI server (localhost:8765)                      ││
│   /api/flagged, /api/email/{id}, /api/stats                     ││
│   Serves static frontend at /                                    ││
└─────────────────────────────────────────────────────────────────┘│
                                                                    │
   Re-run scan any number of times against the local cache ─────────┘
```

---

## Tech stack

| Layer | Tool | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| CLI | Typer | Simple, type-hinted |
| Backend | FastAPI + Uvicorn | Localhost only |
| Frontend | Plain HTML + Alpine.js + Tailwind (CDN) | No build step. Single `index.html` served by FastAPI. |
| DB | SQLite (via SQLAlchemy 2.0 or sqlite3 stdlib) | Single file at `~/.inbox-scanner/state.db` |
| Gmail | `google-api-python-client` + `google-auth-oauthlib` | Read-only scope |
| Extraction (PDF, Office, image, OCR) | Docling 2.x | Single backend; OCR auto-routes via OcrAutoOptions (EasyOCR / Apple Vision / RapidOCR). Needs `opencv-python-headless` for the EasyOCR path. |
| Pattern PII | Microsoft Presidio (`presidio-analyzer`) | |
| Contextual PII | OpenAI Privacy Filter via `transformers` | |
| Async/concurrency | `asyncio` + `httpx` for HTTP, thread pool for sync libs | |
| Logging | `structlog` | JSON logs to file, pretty to console |
| Config | `pydantic-settings` | YAML at `~/.inbox-scanner/config.yaml` |
| Package manager | `uv` | |

---

## Data layout

```
~/.inbox-scanner/
├── config.yaml              # User settings
├── credentials.json         # Google OAuth client (user-provided)
├── token.json               # OAuth refresh token after auth
├── state.db                 # SQLite — emails, attachments, detections
├── attachments/
│   └── blobs/               # Raw attachment bytes (Phase 1 output)
│       ├── ab/cd/abcd1234...   # sharded by hash prefix
│       └── ...
├── extracted/               # Cached extracted text per attachment (Phase 2 output)
└── logs/
    ├── sync.log
    └── scanner.log
```

Docling caches its layout/table/OCR models under `~/.cache/huggingface/hub/`
(the standard HF cache path) on first scan — that's outside the data dir
on purpose so multiple checkouts share one ~2 GB model directory.

Attachments are stored under `attachments/blobs/` with filenames derived from a SHA-256 hash of the content (sharded into two-character directories to avoid huge flat directories). Two emails with identical attachments (very common — e.g., the same insurance card emailed multiple times) share storage.

**Important:** the README must clearly tell users this directory contains:
- Raw attachment bytes (passport scans, IDs, etc.)
- Extracted text from those attachments
- A SQLite DB with PII spans

Users should ensure this directory is on an encrypted volume (FileVault default on macOS handles this for most users).

---

## Database schema

The schema separates **sync state** (Phase 1, talks to Gmail) from **scan state** (Phase 2, runs locally and may be repeated).

```sql
-- One row per sync run (Phase 1)
CREATE TABLE syncs (
    id INTEGER PRIMARY KEY,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT,              -- 'running', 'completed', 'failed'
    total_messages INTEGER,
    synced_messages INTEGER,
    error TEXT
);

-- One row per scan run (Phase 2 — can be many per sync)
CREATE TABLE scans (
    id INTEGER PRIMARY KEY,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT,              -- 'running', 'completed', 'failed'
    total_attachments INTEGER,
    processed_attachments INTEGER,
    config_snapshot JSON,     -- detector thresholds etc., for reproducibility
    error TEXT
);

-- One row per Gmail message we've looked at
CREATE TABLE messages (
    id TEXT PRIMARY KEY,                  -- Gmail message ID
    thread_id TEXT,
    sync_id INTEGER REFERENCES syncs(id),
    sender TEXT,
    subject TEXT,
    received_at TIMESTAMP,
    has_attachments BOOLEAN,
    attachment_count INTEGER,
    sync_status TEXT,         -- 'pending', 'synced', 'sync_error'
    sync_error TEXT,
    synced_at TIMESTAMP
);

-- One row per attachment (created during sync)
CREATE TABLE attachments (
    id TEXT PRIMARY KEY,                  -- Gmail attachment ID, or composite
    message_id TEXT REFERENCES messages(id),
    filename TEXT,
    mime_type TEXT,
    size_bytes INTEGER,
    content_hash TEXT,        -- SHA-256 of bytes; used for blob path
    blob_path TEXT,           -- relative path under attachments/blobs/
    sync_status TEXT,         -- 'pending', 'downloaded', 'skipped_too_large', 'sync_error'
    sync_error TEXT,
    downloaded_at TIMESTAMP,

    -- Filled by scan phase (latest scan only; old results archived if needed)
    last_scan_id INTEGER REFERENCES scans(id),
    extraction_route TEXT,    -- 'docling', 'qwen-vl', 'unparseable'
    extraction_status TEXT,   -- 'extracted', 'unparseable', 'pending'
    extracted_text_path TEXT, -- relative path under extracted/
    extracted_at TIMESTAMP,
    extraction_error TEXT
);

-- One row per PII finding (rewritten on each scan)
CREATE TABLE detections (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER REFERENCES scans(id),
    attachment_id TEXT REFERENCES attachments(id),
    category TEXT,            -- 'gov_id', 'financial', 'tax', 'medical', 'credentials', 'legal', 'other_pii'
    subtype TEXT,              -- 'us_ssn', 'us_passport', 'credit_card', 'iban', etc.
    detector TEXT,             -- 'presidio', 'privacy_filter', 'custom_regex'
    span_text TEXT,            -- the matched text (for review)
    span_start INTEGER,
    span_end INTEGER,
    confidence REAL,
    created_at TIMESTAMP
);

-- Aggregated per-message verdict for quick UI rendering (rewritten on each scan)
CREATE TABLE message_verdicts (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    scan_id INTEGER REFERENCES scans(id),
    is_flagged BOOLEAN,
    top_category TEXT,
    risk_score REAL,           -- weighted by category + count
    category_summary JSON      -- {"gov_id": 2, "financial": 1, ...}
);

CREATE INDEX idx_messages_sync_status ON messages(sync_status);
CREATE INDEX idx_attachments_message ON attachments(message_id);
CREATE INDEX idx_attachments_extraction ON attachments(extraction_status);
CREATE INDEX idx_attachments_hash ON attachments(content_hash);
CREATE INDEX idx_detections_scan ON detections(scan_id);
CREATE INDEX idx_detections_attachment ON detections(attachment_id);
CREATE INDEX idx_verdicts_flagged ON message_verdicts(is_flagged, risk_score DESC);
```

**Re-scan behavior:** when `inbox-scanner scan` runs a second time, it creates a new `scans` row, deletes prior `detections` and `message_verdicts` rows, and re-runs extraction + detection against the cached blobs. Extraction results can be cached (skip if `extraction_status='extracted'` and config hasn't changed) or forced (`scan --force-extract`).

---

## CLI commands

```
inbox-scanner auth                        # Walk through OAuth, save token.json

inbox-scanner sync [--limit N] [--since YYYY-MM-DD] [--resume]
    # Phase 1: download messages and attachment bytes from Gmail.
    # Idempotent: skips messages already marked 'synced'.
    # --resume continues an interrupted sync (default behavior on re-run).

inbox-scanner scan [--force-extract] [--only-extract] [--only-detect]
    # Phase 2: run extraction + detection on locally cached attachments.
    # No Gmail access. Can be run any number of times.
    # --force-extract re-runs extraction even on attachments already extracted.
    # --only-extract skips detection (useful when iterating on extractors).
    # --only-detect skips extraction (uses cached extracted text).

inbox-scanner serve [--port 8765]         # Start FastAPI + frontend

inbox-scanner status                      # Print sync state + last scan summary

inbox-scanner reset [options]
    # --keep-token        keep OAuth token
    # --keep-attachments  keep downloaded blobs (only wipe scan results)
    # --keep-extractions  keep extracted text cache
    # By default, wipes everything except token.
```

The `sync` and `scan` commands print live progress to stdout (rich progress bar), write structured logs to file, and exit cleanly on Ctrl-C. Both commands are resumable on re-run.

**Typical user workflow:**

```bash
inbox-scanner auth                        # one-time OAuth setup
inbox-scanner sync                        # one-time, may take hours for large inbox
inbox-scanner scan                        # run scan; iterate freely
inbox-scanner serve                       # review results in browser
# ... user reviews, decides to tighten thresholds, edits config.yaml ...
inbox-scanner scan                        # re-run with new config, no Gmail access needed
```

---

## Module structure

```
inbox_scanner/
├── __init__.py
├── cli.py                    # Typer commands
├── config.py                 # Pydantic settings, YAML loader
├── server.py                 # FastAPI app
├── db.py                     # SQLAlchemy setup, session helpers
├── models.py                 # SQLAlchemy ORM models
├── blobs.py                  # Content-addressed blob storage helpers
├── gmail/
│   ├── __init__.py
│   ├── auth.py               # OAuth flow, token refresh
│   ├── client.py             # Gmail API wrapper, rate-limited
│   └── sync.py               # Phase 1: list + fetch messages + download attachments
├── extraction/
│   ├── __init__.py
│   ├── router.py             # Decides docling vs qwen-vl vs skip
│   ├── docling_extractor.py
│   ├── qwen_vl_extractor.py  # HTTP client to llama-server
│   └── types.py              # ExtractionResult dataclass
├── detection/
│   ├── __init__.py
│   ├── runner.py             # Orchestrates all detectors, aggregates results
│   ├── presidio_detector.py
│   ├── privacy_filter_detector.py
│   ├── custom_regex.py       # US-specific patterns Privacy Filter misses
│   ├── categorizer.py        # Maps detector outputs → user-facing categories
│   └── types.py              # Detection, Finding dataclasses
├── pipelines/
│   ├── __init__.py
│   ├── sync_pipeline.py      # Phase 1 orchestrator
│   └── scan_pipeline.py      # Phase 2 orchestrator (offline)
├── frontend/
│   └── index.html            # Single-file Alpine.js app
└── tests/
    ├── test_router.py
    ├── test_categorizer.py
    ├── test_blobs.py
    └── ...
```

---

## Component specs

### 1. Gmail integration (`inbox_scanner/gmail/`)

This module handles **only Phase 1 (sync)**. Once sync completes, Gmail is never touched again until the user explicitly runs `inbox-scanner sync` to fetch new messages.

**OAuth setup:** the user creates their own Google Cloud project, enables Gmail API, downloads OAuth client credentials (`credentials.json`), drops it in `~/.inbox-scanner/`. The README must walk through this. We use the **`gmail.readonly` scope only**. No restricted-scope verification needed because each user uses their own OAuth client.

**Rate limiting:** Gmail API quota is ~250 quota units/sec per user. A `messages.get` with `format=full` costs 5 units, `attachments.get` costs 5 units. To stay safely under, throttle to **20 requests/second** using a token-bucket limiter. Use exponential backoff with jitter on 429/503.

**Sync behavior (`gmail/sync.py`):**
- `users.messages.list` with query `has:attachment` to enumerate candidate messages.
- Page through results, store stub records in `messages` table immediately (sync_status=`pending`).
- For each pending message:
  1. Fetch full content with `users.messages.get(format='full')`, walk MIME parts to find attachments.
  2. For each attachment:
     - Apply skip filters (see below). If skipped, record reason in DB.
     - Otherwise, download bytes via `users.messages.attachments.get`.
     - Compute SHA-256 of bytes, store under `attachments/blobs/{hash[:2]}/{hash[2:4]}/{hash}`.
     - If a blob with that hash already exists (deduplication), reuse it.
     - Insert `attachments` row with `sync_status='downloaded'`, `blob_path`, `content_hash`.
  3. Mark message `sync_status='synced'`.
- **Idempotent on re-run:** queries `WHERE sync_status='pending' OR sync_status='sync_error'` and only processes those. Already-synced messages are skipped.
- On Ctrl-C or crash: in-flight message marked `sync_error`, will be retried on next sync run. Already-completed messages stay `synced`.

**Attachment skip filters (during sync — saves bandwidth and disk):**
- Mime types in skip-list: `image/gif` (almost always tracking pixels or animations), `text/calendar`, `application/pkcs7-signature`, `application/pgp-signature`.
- Size < 1 KB (likely tracking pixels or sigs).
- Size > `max_attachment_bytes` (default 50 MB) → mark `skipped_too_large`, do NOT download.
- Inline images with `Content-ID` headers and no filename (these are usually inline body images, not real attachments).

Use Gmail message ID as primary key; never re-fetch already-synced messages.

### 2. Blob storage (`inbox_scanner/blobs.py`)

Content-addressed storage for downloaded attachments. Two emails carrying the same file (which is common — the same insurance card, the same form template) share one blob.

```python
def store_blob(content: bytes) -> tuple[str, Path]:
    """Returns (sha256_hex, relative_path). Idempotent."""
    digest = hashlib.sha256(content).hexdigest()
    rel_path = Path("blobs") / digest[:2] / digest[2:4] / digest
    full_path = data_dir / "attachments" / rel_path
    if not full_path.exists():
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)
    return digest, rel_path

def read_blob(rel_path: Path) -> bytes:
    return (data_dir / "attachments" / rel_path).read_bytes()
```

Multiple `attachments` rows can point to the same `content_hash` / `blob_path`. When extracting, the scanner can cache extraction results keyed on `content_hash` to avoid re-extracting identical files.

### 3. Attachment router (`inbox_scanner/extraction/router.py`)

Mime-only allowlist — Docling 2.x sniffs born-digital vs. scanned PDFs internally via `do_ocr=True` so we don't pre-classify:

```python
ExtractionRoute = Literal["docling", "unparseable"]

DOCLING_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/msword",
    "text/html", "text/plain", "text/csv", "text/markdown",
    "image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp",
}

def route(mime_type: str | None) -> ExtractionRoute:
    return "docling" if _canonicalize(mime_type) in DOCLING_MIME_TYPES else "unparseable"
```

Anything outside the allowlist (HEIC, SVG, GIF, archives, audio/video) gets `unparseable`. We canonicalize a few common aliases (`image/jpg` → `image/jpeg`, `image/x-png` → `image/png`) before matching.

### 4. Docling extractor

```python
from docling.datamodel.base_models import DocumentStream
from docling.document_converter import DocumentConverter

converter = DocumentConverter()  # singleton, ~2 GB models lazy-downloaded on first call

def extract(content: bytes, filename: str) -> str:
    stream = DocumentStream(name=filename, stream=BytesIO(content))
    result = converter.convert(stream)
    return result.document.export_to_markdown()
```

Default Docling settings (`PdfPipelineOptions(do_ocr=True, do_table_structure=True, ocr_options=OcrAutoOptions())`) handle every category we care about: born-digital PDFs, scanned PDFs (auto-OCR fallback), Office docs, and the supported image formats. Markdown export preserves table structure for receipts/forms.

**Why no separate VLM:** the original plan routed images and scanned PDFs through Qwen2.5-VL via `llama-server`. End-to-end testing on a real corpus showed Docling's literal-text OCR output is actually more useful for downstream entity-style PII detection than a VLM's narrative description, and the operational simplification (one backend, no second HTTP service, no model file management) is significant. If we ever hit content Docling can't OCR well (handwriting, rotated images, charts), Docling 2.x exposes `do_picture_description=True` which loads SmolVLM in-process — no llama-server.

**OCR backend selection:** `OcrAutoOptions` picks the first available of:
1. **Apple Vision** (`ocrmac`) on macOS — installed via Docling's deps, no extra config.
2. **EasyOCR** (`easyocr`) — needs `cv2`; we depend on `opencv-python-headless` explicitly because the GUI variant `opencv-python` fails to import on headless macOS environments.
3. **RapidOCR** (`rapidocr`) — ONNX-based fallback.

**Concurrency:** the scan pipeline wraps Docling in an `asyncio.Semaphore(extract_concurrency)` (default 2). Docling does its own internal concurrency for layout/table/OCR; the outer semaphore mostly keeps the progress bar honest and avoids spawning more workers than CPU cores.

### 6. Detection layer

Run all three detectors on the extracted text, then aggregate:

**Presidio:** stock recognizers for `CREDIT_CARD`, `IBAN_CODE`, `US_SSN`, `US_PASSPORT`, `US_DRIVER_LICENSE`, `US_BANK_NUMBER`, `US_ITIN`, `EMAIL_ADDRESS`, `PHONE_NUMBER`. Confidence threshold: configurable, default 0.5.

**OpenAI Privacy Filter:** load via HuggingFace transformers pipeline, `task="token-classification"`, `aggregation_strategy="simple"`. Detects: `account_number`, `private_address`, `private_email`, `private_person`, `private_phone`, `private_url`, `private_date`, `secret`. Run on chunks of ≤ 4096 tokens (it supports 128k but we don't need that and it's much slower at full context). Confidence threshold: configurable, default 0.6.

**Custom regex (US-specific):** patterns Privacy Filter and Presidio miss or handle weakly:
- Medical record number patterns
- US tax form headers (`W-2`, `1099-MISC`, `1099-NEC`, `1040`, `Schedule C`)
- Credential markers (`password:`, `api_key`, `recovery code`, `mnemonic phrase` — 12/24-word patterns)
- Insurance member ID patterns

**Categorization** (`categorizer.py`): map detector outputs to user-facing categories:

| Detected | User category |
|---|---|
| `US_SSN`, `US_PASSPORT`, `US_DRIVER_LICENSE`, `US_ITIN` | `gov_id` |
| `CREDIT_CARD`, `IBAN_CODE`, `US_BANK_NUMBER`, `account_number` | `financial` |
| Tax form regex hits | `tax` |
| Medical record regex, insurance ID | `medical` |
| `secret`, password/API key regex | `credentials` |
| Lease/contract keyword regex (e.g., "Tenant:", "Effective Date:", "Party of the first part") | `legal` |
| `private_person`, `private_address`, `private_phone`, `private_email` (alone) | `other_pii` (informational, doesn't flag on its own) |

A message is **flagged** if it has any detection in `gov_id`, `financial`, `tax`, `medical`, `credentials`, or `legal`. A message with only `other_pii` is not flagged in v1 (too noisy).

**Risk score** (for sorting): weighted sum of detections by category. Suggested weights: `gov_id=10, credentials=10, financial=7, medical=7, tax=5, legal=3`. Cap at 100.

### 7. Pipelines (`pipelines/`)

Two separate pipelines, one per phase. They share DB/blob/config infrastructure but are otherwise independent.

#### 7a. Sync pipeline (`pipelines/sync_pipeline.py`)

Handles Phase 1. Network-bound, throttled by Gmail rate limit.

```python
async def run_sync(sync_id, limit=None, since=None):
    # 1. Create syncs row, status='running'
    # 2. List candidate messages from Gmail (has:attachment)
    #    - If `since` provided, add 'after:YYYY/MM/DD' to query
    # 3. Insert message stubs (sync_status='pending') for messages not seen before
    # 4. Producer: feeds pending message IDs into asyncio.Queue (max 50)
    # 5. Workers (4 concurrent): for each message:
    #    a. Fetch full message via Gmail API
    #    b. For each attachment part: apply skip filters, download bytes,
    #       hash, dedupe-store, insert attachments row
    #    c. Mark message synced
    # 6. On completion, update syncs row to status='completed'
```

Concurrency:
- 4 worker tasks consume messages.
- Global token-bucket limiter: 20 Gmail requests/second across all workers.
- All Gmail HTTP via `httpx.AsyncClient`.

Resume on re-run: query is always `WHERE sync_status IN ('pending','sync_error')` so partial syncs continue cleanly.

#### 7b. Scan pipeline (`pipelines/scan_pipeline.py`)

Handles Phase 2. CPU/GPU-bound, no network calls. Operates entirely on locally cached blobs and DB rows.

```python
async def run_scan(scan_id, force_extract=False, only_extract=False, only_detect=False):
    # 1. Create scans row with config_snapshot
    # 2. Determine work set: all attachments where sync_status='downloaded'
    # 3. Stage A — Extract (skip if only_detect):
    #    For each attachment:
    #    a. If force_extract OR extraction_status != 'extracted':
    #       - Read blob from disk
    #       - Route → docling or qwen-vl
    #       - Run extractor, write extracted text to extracted/<hash>.txt
    #       - Update attachments row with extraction_route, extraction_status,
    #         extracted_text_path, extracted_at
    # 4. Stage B — Detect (skip if only_extract):
    #    a. Delete prior detections + verdicts (full overwrite for current scan)
    #    b. For each attachment with extraction_status='extracted':
    #       - Read extracted text
    #       - Run all detectors, write detections rows
    #    c. For each affected message: compute aggregated verdict, write
    # 5. Mark scan completed
```

Concurrency model:
- Extraction uses a thread pool (Docling and the rasterizer are sync; VLM HTTP calls go through async).
- VLM calls limited by semaphore: max 2 concurrent (configurable). The 7B model on a single Apple Silicon machine cannot serve more than that.
- Detection runs in a thread pool — Presidio and Privacy Filter are CPU-bound.
- No global rate limiting (everything is local).

**Why two stages within scan:** the user might want to swap detectors and re-run quickly without re-extracting (extraction is the expensive part — VLM on 1000 image pages is hours). `--only-detect` against cached extracted text is fast. Conversely, while iterating on extractors, `--only-extract` regenerates text without burning time on detection.

### 8. FastAPI server

Endpoints:

```
GET  /api/stats
     → { sync: { last_sync_at, total_messages, total_attachments,
                 total_blob_bytes },
         scan: { last_scan_at, total_flagged, by_category: {...} } }

GET  /api/flagged?cursor=&limit=20&category=&sort=risk|date
     → paginated list of flagged messages with summaries
     [{ message_id, sender, subject, received_at, top_category,
        risk_score, category_summary, gmail_url }]

GET  /api/email/{message_id}
     → full detail: all attachments, all findings per attachment,
       extracted text snippets around each finding (±200 chars)
       gmail_url for "open in Gmail" button

GET  /                       → serves index.html
GET  /static/*               → serves frontend assets if any
```

`gmail_url` format: `https://mail.google.com/mail/u/0/#inbox/{message_id}` (or `#all/{message_id}` to be safe — message may be archived).

The server **never** modifies state. The only stateful endpoint is implicit: SQLite reads. All scanning happens via the CLI, separately.

Bind to `127.0.0.1` only. No auth — single-user local tool. Print a clear warning at startup if `--host` is anything else.

### 9. Frontend (`frontend/index.html`)

Single file, no build step. Uses Tailwind via CDN and Alpine.js for reactivity.

Views:
- **Dashboard:** stats card (scanned, flagged, by category), "Start Review" button.
- **Review:** linear one-at-a-time view. Shows current email's metadata, list of attachments with extraction status, list of findings grouped by category with text snippets. Two buttons: **"Open in Gmail"** (opens `gmail_url` in new tab) and **"Next →"**. Keyboard shortcuts: `J` next, `K` prev, `O` open in Gmail.
- **Filter sidebar:** filter by category, sort by risk score or date.

The UI must be functional, not pretty. Tailwind's defaults are fine.

---

## Configuration (`~/.inbox-scanner/config.yaml`)

```yaml
gmail:
  credentials_path: ~/.inbox-scanner/credentials.json
  rate_limit_rps: 20
  max_total_bytes: 107374182400        # 100 GB safety cap; abort sync if exceeded

extraction:
  max_attachment_bytes: 52428800        # 50 MB per attachment
  extract_concurrency: 2                # outer asyncio.Semaphore for Docling calls

detection:
  presidio_threshold: 0.5
  privacy_filter_threshold: 0.6
  enabled_categories:
    - gov_id
    - financial
    - tax
    - medical
    - credentials
    - legal

server:
  host: 127.0.0.1
  port: 8765
```

---

## Error handling and "unparseable" surfacing

Per the user's choice: when an attachment can't be parsed, mark it `unparseable` in the DB with an error message, and surface in the UI. The detail page for any email containing unparseable attachments shows a clear "⚠️ This email has attachments we couldn't parse — review manually in Gmail" banner.

Unparseable causes to handle explicitly:
- Encrypted/password-protected PDF (catch `pypdfium2.PdfiumError`)
- Corrupted file (catch generic exceptions, log)
- Mime type not in router's allowlist
- VLM returned `[UNREADABLE]` for all pages
- VLM endpoint unreachable (this is a **scan-level** error — pause scan, report clearly, don't mark every attachment as unparseable)

---

## Testing strategy for v1

This is a personal/friends tool — don't over-engineer testing, but do cover the things most likely to break:

- **Unit tests** for `router.py` (mime → route mapping), `categorizer.py` (detection → category), and the regex patterns (golden file of positive and negative examples).
- **Integration test** for the pipeline using a local fake Gmail (mock the `gmail.client` layer, feed in a few canned messages with attachments — including one PDF, one image, one DOCX, one unparseable file).
- **Smoke test** the FastAPI endpoints with `httpx.AsyncClient`.
- **Manual test plan** in the README: "scan your own inbox with `--limit 100`, verify nothing crashes, verify findings look reasonable."

Don't write tests for Docling, Privacy Filter, or Qwen2.5-VL accuracy — those are upstream concerns and require real attachments.

---

## Build order (recommended for Claude Code)

Ship in this order so each step produces something testable:

1. **Project scaffolding** ✅ — `pyproject.toml` with `uv`, module layout, empty CLI commands, config loader, DB schema + Alembic migrations, structured logging setup, blob storage helpers.
2. **Gmail auth + sync (no attachments)** ✅ — `inbox-scanner auth` works, `inbox-scanner sync --limit 5` lists 5 messages and writes stubs to DB. Just metadata, no attachment bytes yet.
3. **Sync with attachment download** ✅ — extend sync to download attachment bytes to content-addressed blob storage. `inbox-scanner sync --limit 20` produces a fully populated local cache. 4-worker async with 20 RPS bucket; resume tested.
4. **Docling extractor + router (offline)** ✅ — `inbox-scanner scan --only-extract` works against cached blobs. Single-backend (Docling 2.x) handles PDFs, Office docs, and supported images via on-by-default OCR. Original "step 5: Qwen2.5-VL extractor" was collapsed into this — see plan revision note at the top.
5. **Detection layer** ✅ — Presidio + Privacy Filter + custom regex, categorizer, verdict computation. `inbox-scanner scan --only-detect` works on cached extracted text.
6. **Full scan pipeline** ✅ — `inbox-scanner scan` runs extract + detect end to end. Idempotence verified on the dev corpus: the meaningful payload (attachment-id + category + subtype + detector + span boundaries + verdict tuple set) is bit-for-bit identical across two consecutive runs.
7. **FastAPI server + endpoints** — JSON API works against the populated DB.
8. **Frontend** — single-page Alpine.js review UI. Hook up "Open in Gmail" buttons.
9. **README** — installation, OAuth setup, sync vs scan workflow, how to interpret results, security notes about the data directory.
10. **Polish** — progress bars during sync and scan, status command, reset command, Ctrl-C handling.

Each step should be a separate PR/commit so the user can review incrementally.

**Key milestone** at step 3: once sync works end to end, the rest of development happens entirely offline against your local cache. You sync your real inbox once and never touch Gmail again until v1 ships.

---

## Known gotchas to flag for Claude Code

- **Disk usage scales with inbox size.** A 50,000-message inbox with attachments could be 50–200 GB on disk after sync. The sync command should print a periodic estimate (`Downloaded 1,234 attachments, 4.2 GB so far`). Add a `--max-total-bytes` config option that aborts sync gracefully if exceeded.
- **Privacy Filter is not a generative model** — it's a token classifier that needs `transformers` directly, not Ollama or llama.cpp. Use `transformers.pipeline("token-classification", model="openai/privacy-filter", aggregation_strategy="simple")`. First run downloads ~3 GB of weights.
- **Gmail message IDs are stable but thread IDs can change.** Use message IDs as primary keys.
- **Gmail attachment IDs expire after a few hours.** Don't include them in any composite primary key. We key attachments by `(message_id, part_id)` since `part_id` is documented as immutable; the volatile `gmail_attachment_id` lives in its own column and is refreshed on every metadata fetch.
- **Docling's first run downloads layout/table/OCR models** (~2 GB combined) under `~/.cache/huggingface/hub/`. The CLI emits a one-time `docling.first_call_may_download_models` log line on the first extraction attempt.
- **EasyOCR + opencv on macOS:** Docling's `OcrAutoOptions` may select EasyOCR, which imports `cv2`. The default `opencv-python` wheel can fail to load on headless macOS environments — depend on `opencv-python-headless` explicitly. (We've already done this and pinned it in `pyproject.toml`.)
- **`messages.list` with `has:attachment`** still returns plenty of messages whose only "attachment" is an inline image or a meeting invite. The skip filters in the sync phase handle most of these but expect ~30% of "has:attachment" results to yield zero useful attachments after filtering.
- **The data directory contains plaintext attachment bytes, extracted text, and PII spans.** README must mention this prominently. Suggest verifying FileVault is on.
- **Content-addressed dedup means you cannot delete a single attachment by deleting its blob.** If two messages share a blob and the user wants to scrub one, the blob must stay. For v1, just document this; deletion isn't in scope anyway.

---

## Known v1 polish backlog (fix before shipping the UI)

Issues we've consciously parked because they don't block forward progress
on the build order, but should be cleaned up before the FastAPI/UI step
since they affect what users will see:

- **Privacy Filter span splitting.** ``aggregation_strategy="simple"``
  doesn't merge across a BIE-sequence → S-single-token boundary, so an
  entity whose tokenizer split is 2+1 subwords comes out as two adjacent
  findings (e.g. ``"Sa…V"`` + ``"emu"`` for one ``private_person``).
  Aggregate counts and verdicts are correct; per-span boundaries aren't.
  Two cheap mitigations to evaluate when we wire highlight rendering:
  (a) switch to ``aggregation_strategy="first"`` and re-test precision,
  or (b) post-process in ``_dedupe`` to merge adjacent same-subtype
  findings whose ``span_end == next.span_start`` (or gap ≤ 1 char). See
  TODO comment at ``inbox_scanner/detection/privacy_filter_detector.py``.

## Out of scope for v1, captured for v2 backlog

- Outlook/IMAP/Yahoo support
- Incremental daemon mode (watch new mail)
- Custom user-defined detection rules (regex or natural language)
- Encrypted-at-rest local DB (SQLCipher)
- Multi-country ID patterns (UK NHS number, Indian Aadhaar, EU IDs)
- Local LLM enrichment layer (document-type classification, risk explanations, smart grouping)
- Bulk actions UI (after we add Gmail write scope)
- Export reports (CSV/JSON of findings)
- Email body scanning
- Multi-account support
