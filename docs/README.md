# Technical documentation

Reference docs for the as-built v1 of InboxAudit. The user-facing
setup walkthrough is the top-level [`../README.md`](../README.md); this
folder is for developers and reviewers who need to understand how the
system actually works.

## Reading order

If you're new to the codebase, read these in order — each builds on the
previous:

1. **[Architecture](architecture.md)** — system overview, two-phase
   design, component map, tech stack.
2. **[Data model](data-model.md)** — SQLite schema, per-table
   lifecycle, key relationships.
3. **[Sync pipeline](sync-pipeline.md)** — phase 1: Gmail integration,
   rate limiting, attachment downloads, resume semantics.
4. **[Scan pipeline](scan-pipeline.md)** — phase 2: extraction
   (Docling), detection (Presidio + Privacy Filter + custom regex),
   categorization, verdict computation.
5. **[API](api.md)** — FastAPI surface: endpoints, request/response
   shapes, snippet windows.
6. **[Frontend](frontend.md)** — single-file Alpine + Tailwind UI,
   views, keyboard shortcuts.
7. **[CLI](cli.md)** — every command and flag with examples.
8. **[Operations](operations.md)** — data dir layout, model cache,
   reset semantics, idempotence guarantees.
9. **[Security](security.md)** — OAuth scope, localhost-only design,
   data-at-rest, threat model.
10. **[Development](development.md)** — dev environment, testing
    strategy, conventions, common workflows.

## Architecture decisions

Important design decisions are captured as [ADRs](decisions/) so the
*why* survives even after the code changes:

- [0001](decisions/0001-two-phase-architecture.md) — Sync and scan as
  independent phases.
- [0002](decisions/0002-content-addressed-blob-storage.md) —
  Attachments stored by SHA-256.
- [0003](decisions/0003-single-extraction-backend.md) — Collapsing
  Docling + Qwen-VL to Docling-only.
- [0004](decisions/0004-attachment-key-uses-part-id.md) — Composite
  primary key on `attachments` uses `part_id`, not the volatile Gmail
  attachment ID.
- [0005](decisions/0005-three-detector-pipeline.md) — Why three
  detectors (Presidio + Privacy Filter + custom regex), not one.

## Historical

- [`archives/IMPLEMENTATION_PLAN.md`](archives/IMPLEMENTATION_PLAN.md)
  — the original v1 spec, including the build order and v2 backlog.
  **Preserved verbatim** for historical context. Where the build
  diverged from the plan, the relevant ADR captures the *why*. The
  plan still contains the canonical v2 wishlist at the bottom.

## Style

- Mermaid diagrams render natively on GitHub.
- File paths are relative to the repo root. Code references use
  `module/file.py::symbol` so they're easy to navigate.
- "AS-BUILT" is the rule: these docs describe what the code does, not
  what the plan wished for. When the two disagree, the docs win and
  the plan stays as-is.
