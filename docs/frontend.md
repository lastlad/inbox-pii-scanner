# Frontend

Single HTML file at
[`inboxaudit/frontend/index.html`](../inboxaudit/frontend/index.html),
served verbatim by the FastAPI app's `GET /` route. No build step. No
bundler. No npm.

**Dependencies (both via CDN):**

- [Alpine.js](https://alpinejs.dev/) 3.14 — declarative reactivity.
- [Tailwind CSS](https://tailwindcss.com/) (Play CDN) — utility
  classes.

Both load via `<script>` tags. Going offline means losing the UI; this
is acceptable for a v1 personal tool. If we ever need offline, vendor
both into the repo.

## Page structure

```
┌──────────────────────────────────────────────────────────────────┐
│ Inbox PII Scanner   [Dashboard] [Review]      N flagged of M scanned │  <-- top bar
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   <view: dashboard>   OR   <view: review>                        │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

Two top-level views, toggled by the `view` state field. Only one is
visible at a time; both render into the same `<main>` element.

### Dashboard view

```
┌─────────────────────┬─────────────────────┐
│ Sync card           │ Scan card           │
│ - last sync, status │ - last scan, status │
│ - message counts    │ - flagged count     │
│ - blob bytes        │ - total findings    │
└─────────────────────┴─────────────────────┘

┌──────────────────────────────────────────┐
│ Flagged by top category                  │
│ [Financial 8]  [Tax 1]   (click to jump) │
└──────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│ Review flagged messages   [Start review →]│
└──────────────────────────────────────────┘
```

Renders the entirety of `GET /api/stats`. Category chips are
clickable — they jump to Review with the corresponding filter
pre-applied.

### Review view

```
┌──────────────────────────┬───────────────────────────────────────┐
│ FILTER BY CATEGORY       │ [← Prev] [Next →] 1 of 9 [Open in Gmail ↗] │
│   ◉ All flagged          │                                       │
│   ◯ Gov ID               │ ┌─────────────────────────────────┐  │
│   ◯ Credentials          │ │ FROM    sender@example.com      │  │
│   ◯ Financial    8       │ │ SUBJECT eCornell Email Receipt  │  │
│   ◯ Medical              │ │ RECEIVED 4/22/2026, 7:45 PM     │  │
│   ◯ Tax          1       │ │                  [risk 14] [Financial] [Other PII·8] [Financial·2] │
│   ◯ Legal                │ └─────────────────────────────────┘  │
│                          │                                       │
│ SORT                     │ ┌─ Attachments (2) ─────────────────┐ │
│   ◉ Risk (high → low)    │ │ SchoolEmailLogo  gif 5.5KB skipped│ │
│   ◯ Date (newest first)  │ │ Receipt.pdf      pdf 220KB extracted · 10 findings │ │
│                          │ └────────────────────────────────────┘ │
│ Shortcuts                │                                       │
│   J next ·  K prev       │ ┌─ Findings ────────────────────────┐ │
│   O open in Gmail        │ │ [Financial] 2 findings            │ │
│   Esc dashboard          │ │ ┌────────────────────────────────┐│ │
│                          │ │ │ privacy_filter · account_number│ │
│                          │ │ │ confidence 1.00 · Receipt.pdf  │ │
│                          │ │ │ ── snippet with [highlighted span] ─ │
│                          │ │ └────────────────────────────────┘│ │
│                          │ │ [Other PII] 8 findings ...        │ │
│                          │ └────────────────────────────────────┘ │
└──────────────────────────┴───────────────────────────────────────┘
```

Drives `GET /api/flagged` (list) + `GET /api/email/{id}` (per
message). Layout is a CSS grid: `[14rem 1fr]` columns on `md` and up,
single column below.

## Alpine state

All state lives in one `x-data` object returned by the `app()` factory
at the bottom of the file. Shape:

```js
{
  // ─── navigation ───
  view: 'dashboard',  // 'dashboard' | 'review'

  // ─── dashboard data ───
  stats: null,         // entire GET /api/stats payload, or null while loading

  // ─── review list ───
  list: [],            // FlaggedSummary[]
  listTotal: 0,        // server-side total for the current filter
  index: 0,            // index into list[] for the current item
  filter: 'all',       // 'all' | gov_id | credentials | financial | medical | tax | legal
  sort: 'risk',        // 'risk' | 'date'
  loadingList: false,

  // ─── current message ───
  currentEmail: null,  // EmailDetailResponse, or null
  loadingEmail: false,

  // ─── error display ───
  error: null,         // string | null (banner under the header)

  // ─── constants ───
  categoryOrder: [...]
}
```

Plus methods:

| Method | What it does |
|---|---|
| `init()` | On mount: `loadStats()` |
| `loadStats()` | `fetch('/api/stats')` → assign to `stats` |
| `startReview()` | Switch view to `'review'` and call `loadList()` if list is empty |
| `loadList()` | `fetch('/api/flagged?sort=&category=&limit=100')`, reset `index=0`, load the first item |
| `loadEmail(mid)` | `fetch('/api/email/<mid>')` → assign to `currentEmail` |
| `next()` / `prev()` | Move `index`, call `loadEmail` |
| `openInGmail()` | `window.open(currentEmail.gmail_url, '_blank', 'noopener')` |
| `handleKey($event)` | Keyboard shortcut dispatcher |
| `formatDate`, `formatBytes`, `categoryLabel`, `categoryClass`, `riskClass`, `extractionStatusLabel`, `extractionStatusClass`, `snippetParts` | View helpers; all pure |

Getters:

- `groupedFindings` — derives `{category: [Finding, ...]}` from
  `currentEmail.attachments[].findings`.
- `categoriesPresent` — ordered list of categories with at least one
  finding, in canonical risk-weight order (other_pii goes last).
- `totalFindings` — sum across all categories.
- `hasUnparseable` — whether any attachment failed extraction.

## Keyboard shortcuts

Bound at `<body @keydown.window="handleKey($event)">`, with two
guards:

1. Don't fire when the user is typing into a form input
   (`['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)`).
2. Don't fire when a modifier key is held.

| Key | Action | Active in view |
|---|---|---|
| `J` or `→` | Next flagged message | review |
| `K` or `←` | Previous flagged message | review |
| `O` | Open current message in Gmail (new tab) | review |
| `Esc` | Back to dashboard | any |

## Finding snippets

Each finding from `/api/email/{id}` carries three fields:

- `snippet` — the ±N-character window of extracted markdown
- `snippet_relative_start` / `snippet_relative_end` — offsets of the
  matched span **within** `snippet`

The template splits the snippet into three pieces and wraps the middle
in `<mark>`:

```html
<template x-if="f.snippet">
  <span>
    <span class="text-gray-500" x-text="snippetParts(f).pre"></span>
    <mark class="finding-hit" x-text="snippetParts(f).hit"></mark>
    <span class="text-gray-500" x-text="snippetParts(f).post"></span>
  </span>
</template>
```

`x-text` (not `v-html`) is the right binding here — it
auto-escapes, so OCR'd content with HTML-like fragments can't smuggle
in markup. The yellow highlight comes from a single `mark.finding-hit
{ background-color: #fde68a; }` rule in the inline `<style>` block.

## Null-safety gotcha

Alpine evaluates child templates even under `x-show=false`. Code like
`stats.scan.by_top_category[cat]` will throw between page load and the
`/api/stats` promise resolving. Use optional chaining everywhere data
comes from a fetch:

```html
<!-- bad: throws during the brief unloaded window -->
<span x-text="stats.scan.by_top_category[cat]"></span>

<!-- good -->
<span x-text="stats?.scan?.by_top_category?.[cat] ?? 0"></span>
```

This rule applies to the findings template too — `groupedFindings`
mid-transition during a filter change has a brief inconsistent state
where the outer `x-for="cat in categoriesPresent"` may iterate a key
that isn't in `groupedFindings[cat]` yet. Defensive `?.length ?? 0`
and `?? []` handle it.

## Category color coding

| Category | Tailwind classes |
|---|---|
| `gov_id`, `credentials` | `bg-red-100 text-red-800` |
| `financial`, `medical` | `bg-orange-100 text-orange-800` |
| `tax`, `legal` | `bg-yellow-100 text-yellow-800` |
| `other_pii` (and fallback) | `bg-gray-100 text-gray-700` |

The risk badge uses a slightly different bucketing:

| Score | Classes |
|---|---|
| `≥ 20` | `bg-red-100 text-red-800` |
| `10..20` | `bg-orange-100 text-orange-800` |
| `> 0..10` | `bg-yellow-100 text-yellow-800` |
| `0` | `bg-gray-100 text-gray-700` |

## Browser-driven smoke tests

`tests/test_server.py` covers the API surface. The UI is exercised
manually via the Playwright MCP tools available to Claude Code:
navigate to `http://127.0.0.1:8765/`, click through dashboard →
review → filter change, capture screenshots.

**Never commit those screenshots.** They render real PII from the dev
corpus. `.gitignore` blocks `*.png` / `*.jpg` / `*.jpeg` at the repo
root and the `.playwright-mcp/` workspace directory for that reason.

## See also

- [API](api.md) — endpoints the frontend consumes.
- [Scan pipeline § verdict computation](scan-pipeline.md#verdict-computation)
  — defines the `is_flagged`, `top_category`, `risk_score`, and
  `category_summary` fields rendered here.
