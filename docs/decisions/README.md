# Architecture decision records

Short, focused write-ups of the non-obvious design choices that shape
the codebase. Each ADR follows the same skeleton — Status, Context,
Decision, Consequences — and is numbered sequentially.

| # | Title | Status |
|---|---|---|
| [0001](0001-two-phase-architecture.md) | Sync and scan as independent phases | Accepted |
| [0002](0002-content-addressed-blob-storage.md) | Attachments stored by SHA-256 hash | Accepted |
| [0003](0003-single-extraction-backend.md) | Collapse Docling + Qwen-VL to Docling-only | Accepted (supersedes a part of the original plan) |
| [0004](0004-attachment-key-uses-part-id.md) | Composite primary key on `attachments` uses `part_id` | Accepted (supersedes a part of the original plan) |
| [0005](0005-three-detector-pipeline.md) | Three detectors (Presidio + Privacy Filter + custom regex) | Accepted |

## When to write a new ADR

Write one when a decision:

- changes a public surface (CLI flag, API endpoint, DB schema), or
- changes a load-bearing constraint listed in [`../../CLAUDE.md`](../../CLAUDE.md), or
- supersedes something the original [`../IMPLEMENTATION_PLAN.md`](../IMPLEMENTATION_PLAN.md) specified, or
- you'd want to leave a note for someone six months from now about
  *why* a thing is the way it is.

Don't write one for routine refactors or fixes — those go in commit
messages.

## Template

```markdown
# NNNN — <short title>

**Status:** Proposed | Accepted | Superseded by [#NNNN](...)
**Date:** YYYY-MM-DD

## Context

What problem were we facing? What constraints applied? Cite real
numbers from local testing if relevant.

## Decision

What we chose, in one or two sentences.

## Consequences

What follows from the decision — both the wins and the costs. Be
honest about the costs. Cross-reference the modules and tests where
the decision is encoded.

## Alternatives considered

Optional. The ones close enough to be worth recording for posterity.
```
