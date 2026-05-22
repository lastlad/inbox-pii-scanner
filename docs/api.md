# API reference

FastAPI app at [`inboxaudit/server.py`](../inboxaudit/server.py).
Bound to `127.0.0.1:8765` by default; **no auth**, **read-only**
against the local SQLite store.

Interactive OpenAPI explorer is served at `/docs` when the server is
running.

## Endpoints at a glance

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/stats` | Dashboard summary: sync + scan counters |
| GET | `/api/flagged` | Paginated list of flagged messages |
| GET | `/api/email/{message_id}` | Full review payload for one message |
| GET | `/` | Static frontend (`inboxaudit/frontend/index.html`) |
| GET | `/docs` | OpenAPI / Swagger UI (FastAPI auto-generated) |
| GET | `/openapi.json` | OpenAPI schema |

The server is **read-only** — there is no `POST`/`PUT`/`DELETE` route.
Mutating state requires running the CLI.

## `GET /api/stats`

Dashboard payload. Aggregates sync + scan state and the per-category
breakdown of flagged messages.

### Response

```json
{
  "sync": {
    "last_sync_at": "2026-04-27T13:25:33.687589",
    "last_sync_status": "completed",
    "total_messages": 20,
    "total_attachments": 43,
    "total_attachments_downloaded": 37,
    "total_blob_bytes": 3301748
  },
  "scan": {
    "last_scan_at": "2026-05-07T12:43:11.509695",
    "last_scan_status": "completed",
    "total_messages_with_verdict": 18,
    "total_flagged": 9,
    "total_findings": 74,
    "by_top_category": {
      "financial": 8,
      "tax": 1
    }
  }
}
```

### Pydantic models

[`server.py::StatsResponse`](../inboxaudit/server.py).

- `sync.last_sync_at` is `null` if no sync has run.
- `sync.total_blob_bytes` is a `sum()` over file sizes under
  `<data_dir>/attachments/blobs/` — not cached, computed per request
  (cheap for typical inbox sizes).
- `scan.by_top_category` only includes categories that *are present*
  in the verdict set; missing categories are absent rather than zero.

## `GET /api/flagged`

Paginated list of flagged messages with the summary fields the review
list needs.

### Query parameters

| Param | Type | Default | Notes |
|---|---|---|---|
| `cursor` | int (≥0) | `0` | Offset for pagination |
| `limit` | int (1..100) | `20` | Page size |
| `category` | str | none | Filter by `top_category`. Values: `gov_id`, `financial`, `tax`, `medical`, `credentials`, `legal` |
| `sort` | enum | `risk` | `risk` (high→low, secondary on date desc) or `date` (newest→oldest) |

### Response

```json
{
  "items": [
    {
      "message_id": "19db6ba33144e4fc",
      "sender": "eCornell <helpdesk@ecornell.cornell.edu>",
      "subject": "eCornell Email Receipt",
      "received_at": "2026-04-22T19:45:48",
      "is_flagged": true,
      "top_category": "financial",
      "risk_score": 14.0,
      "category_summary": {"other_pii": 8, "financial": 2},
      "attachment_count": 2,
      "gmail_url": "https://mail.google.com/mail/u/0/#all/19db6ba33144e4fc"
    }
  ],
  "next_cursor": "3",
  "total": 9
}
```

- `next_cursor` is `null` on the last page; pass it as the next
  request's `cursor` to advance.
- `total` is the filtered total (not the global total). With
  `category=tax`, `total` is the tax-flagged count.
- `gmail_url` uses Gmail's `#all/` fragment so it works whether the
  message is in the inbox, archived, or labelled. Opens in the user's
  default Gmail account context (`u/0`).

### Validation errors

- `sort` outside `{risk, date}` → 422.
- `limit` outside `[1, 100]` → 422.
- `cursor` < 0 → 422.

## `GET /api/email/{message_id}`

Full review payload for one message: every attachment, every finding
per attachment, and a snippet of the cached extracted text around each
finding.

### Path parameter

| Param | Type | Notes |
|---|---|---|
| `message_id` | str | Gmail message ID (the same one returned in `/api/flagged.items[].message_id`) |

### Query parameter

| Param | Type | Default | Notes |
|---|---|---|---|
| `snippet_window` | int (0..2000) | `200` | Characters of context to include on each side of every finding's span |

### Response

```json
{
  "message_id": "19db6ba33144e4fc",
  "sender": "eCornell <helpdesk@ecornell.cornell.edu>",
  "subject": "eCornell Email Receipt",
  "received_at": "2026-04-22T19:45:48",
  "sync_status": "synced",
  "is_flagged": true,
  "top_category": "financial",
  "risk_score": 14.0,
  "category_summary": {"other_pii": 8, "financial": 2},
  "gmail_url": "https://mail.google.com/mail/u/0/#all/19db6ba33144e4fc",
  "attachments": [
    {
      "attachment_id": "19db6ba33144e4fc:1",
      "filename": "SchoolEmailLogo",
      "mime_type": "image/gif",
      "size_bytes": 5597,
      "sync_status": "skipped_filter",
      "extraction_status": null,
      "extraction_route": null,
      "extraction_error": null,
      "findings": []
    },
    {
      "attachment_id": "19db6ba33144e4fc:2",
      "filename": "Receipt.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 219756,
      "sync_status": "downloaded",
      "extraction_status": "extracted",
      "extraction_route": "docling",
      "extraction_error": null,
      "findings": [
        {
          "detector": "privacy_filter",
          "subtype": "private_address",
          "category": "other_pii",
          "span_text": "950 Danby Road, Suite 150 Ithaca, NY, 14850",
          "span_start": 25,
          "span_end": 68,
          "confidence": 0.99,
          "snippet": "...50 chars before... 950 Danby Road, Suite 150 Ithaca, NY, 14850 ...50 chars after...",
          "snippet_relative_start": 50,
          "snippet_relative_end": 93
        }
      ]
    }
  ]
}
```

### How to render the snippet

The UI's job is to draw the matched span highlighted within its
surrounding context. Three fields make this possible without any
substring-search on the client:

| Field | Meaning |
|---|---|
| `snippet` | The ±N-char window pulled from the cached markdown |
| `snippet_relative_start` | Where the matched span begins **inside** `snippet` |
| `snippet_relative_end` | Where it ends |

So:

```js
const pre  = f.snippet.slice(0, f.snippet_relative_start);
const hit  = f.snippet.slice(f.snippet_relative_start, f.snippet_relative_end);
const post = f.snippet.slice(f.snippet_relative_end);
// render: pre <mark>hit</mark> post
```

That's exactly what
[`frontend/index.html`](../inboxaudit/frontend/index.html) does.
See [Frontend § snippet rendering](frontend.md#finding-snippets).

### 404

Returned when no `messages` row matches the path parameter.

```json
{"detail": "message not found"}
```

## `GET /`

Serves [`inboxaudit/frontend/index.html`](../inboxaudit/frontend/index.html)
verbatim if present, or a small placeholder HTML otherwise. The
placeholder lists the API endpoints and links to `/docs` — useful as a
sanity-check from a fresh browser before the UI ships.

## Authentication and authorization

There is no authentication and no authorization. Defending against
that is delegated to the bind address:

- Default `--host 127.0.0.1` means only processes on the same machine
  can reach the server. macOS firewall + the loopback interface keeps
  the surface tight enough for a single-user local tool.
- `--host 0.0.0.0` prints a loud red warning before starting. The
  scanner does not validate the override; it's the operator's choice.

For threat-model rationale see [Security](security.md).

## CORS

Not configured. The frontend is served by the same FastAPI app, so
same-origin requests work; cross-origin won't. If you need to host
the frontend separately, add `fastapi.middleware.cors.CORSMiddleware`
and pin allowed origins to `127.0.0.1` variants.

## Logging

`uvicorn.access` is lifted to WARNING in `create_app()` and
`access_log=False` is set in `uvicorn.run()` — the per-request log
spam is suppressed by default. Our structlog events (e.g.
`serve.invoked`) still log normally.

## Testing

[`tests/test_server.py`](../tests/test_server.py) uses FastAPI's
`TestClient` against a `create_app(settings)` instance pointed at a
fresh tmpdir. The `_seed_basic_corpus` helper shows the fixture
pattern: drop two synthetic messages + verdicts into the DB and
exercise the endpoints.

## See also

- [Frontend](frontend.md) — what consumes these endpoints.
- [Scan pipeline](scan-pipeline.md) — produces the data these
  endpoints read.
- [Data model](data-model.md) — the underlying tables.
