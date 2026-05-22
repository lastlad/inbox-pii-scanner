# 0003 — Collapse Docling + Qwen-VL to Docling-only

**Status:** Accepted — supersedes
[plan §"Tech stack"](../archives/IMPLEMENTATION_PLAN.md#tech-stack) and
[plan §"5. Qwen2.5-VL extractor"](../archives/IMPLEMENTATION_PLAN.md)
**Date:** 2026-04-29

## Context

The original plan specified a two-track extractor:

| Plan-era route | Handler |
|---|---|
| Born-digital PDFs, Office docs | Docling |
| Images, scanned PDFs | Qwen2.5-VL-7B-Instruct via `llama-server` |

The Qwen-VL track meant: user installs `llama.cpp`, downloads a ~4 GB
GGUF + an `mmproj` companion, starts `llama-server` as a separate
process listening on `127.0.0.1:8080`, and the scanner talks to it
over the OpenAI-compatible API. A separate concurrency cap (2) was
needed because the 7B model on Apple Silicon can't sustain more.

By the time we got to building that route, two things changed:

1. **Docling 2.x had landed.** v1.x didn't have native image support;
   2.x does, with on-by-default OCR via `OcrAutoOptions` →
   EasyOCR / Apple Vision / RapidOCR.
2. **We had real attachments to test against** — the dev corpus
   already had a USPS pre-paid mailing label PDF (scanned), several
   USPS Informed Delivery JPEGs (marketing-style mailpiece scans),
   PNG logos, and PDF receipts.

A direct A/B test: feed those four representative samples to Docling
2.x without any VLM. Result: every one extracted cleanly in <5 s,
including both addresses + tracking number on the shipping label and
the user's name + student ID + addresses on the eCornell receipt.

## Decision

Drop the Qwen-VL track entirely. Use Docling 2.x for everything:
PDFs (born-digital + scanned via auto-OCR), Office docs, and supported
image formats (`image/{png,jpeg,tiff,bmp,webp}`).

If quality ever drops on a class of attachment, the escape hatch is
Docling's own `do_picture_description=True`, which loads SmolVLM
**in-process** — no second HTTP service, no model file management.

## Consequences

**Good:**

- One extraction backend instead of two. One install step. One failure
  mode. One concurrency knob.
- No `llama-server` to start, no GGUF + mmproj to download, no
  "endpoint unreachable" error path.
- Docling's literal-text OCR output turns out to be *more* useful for
  downstream entity-style PII detection than a VLM's narrative
  description — Presidio + Privacy Filter want raw text, not
  paraphrased prose.
- Total scan time on the dev corpus: ~80 s for 37 attachments end to
  end. Comparable or better than the planned two-backend split.

**Costs:**

- `easyocr` (Docling's default OCR backend on macOS in some
  configurations) imports `cv2`. The GUI-flavored `opencv-python`
  wheel fails to load on headless Macs. We carry an explicit
  `opencv-python-headless` dependency to dodge this. (Encoded in
  `pyproject.toml`, called out in CLAUDE.md gotchas.)
- ~2.6 GB of Privacy Filter weights plus ~2 GB of Docling layout/
  table/OCR weights still end up under `~/.cache/huggingface/hub/`.
  That's not specific to this decision — both sets would download
  either way — but it's the bulk of the "~5 GB on first run" the
  README warns about.
- If a class of document genuinely needs handwriting recognition or
  chart extraction, Docling 2.x's optional `do_picture_description` is
  cheaper to wire in than reviving `llama-server`. But it does mean a
  separate eval pass before flipping the flag.

## Encoded in

- `inboxaudit/extraction/router.py` — single-allowlist mime
  routing. The `qwen-vl` branch is gone; the only remaining values
  for `ExtractionRoute` are `"docling"` and `"unparseable"`.
- `inboxaudit/extraction/docling_extractor.py` — singleton
  DocumentConverter.
- `inboxaudit/pipelines/scan_pipeline.py::_process_one` — no
  qwen-vl deferral branch; the `extraction_route` column only ever
  takes `"docling"` or `"unparseable"`.
- `inboxaudit/config.py::ExtractionConfig` — vlm_* fields removed;
  one `extract_concurrency` knob remains.
- Plan revision note at the top of `docs/archives/IMPLEMENTATION_PLAN.md`.

## Alternatives considered

- **Keep both backends as planned.** The operational complexity
  wasn't justified once real-corpus testing showed Docling 2.x had
  closed the gap.
- **Use a hosted VLM** (OpenAI / Anthropic API). Out of scope —
  scanner is local-first by design and uploading attachment bytes to
  a third party is exactly what the scanner is trying to help users
  avoid in the first place.
