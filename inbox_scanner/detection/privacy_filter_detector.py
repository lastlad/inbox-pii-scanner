"""Contextual PII detection via OpenAI's privacy-filter token classifier.

Loaded as a HuggingFace ``transformers`` token-classification pipeline with
``aggregation_strategy='simple'`` so adjacent same-entity tokens collapse
into one span. Detects: ``account_number``, ``private_address``,
``private_email``, ``private_person``, ``private_phone``, ``private_url``,
``private_date``, ``secret``.

The model handles up to 128 k tokens per call but it's much faster on
shorter inputs. We chunk at ``_CHUNK_TOKENS`` characters and re-base the
returned spans into the original text so callers see absolute offsets.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from inbox_scanner.detection.types import Finding
from inbox_scanner.logging import get_logger

log = get_logger("detection.privacy_filter")

_MODEL_NAME = "openai/privacy-filter"

# 4096 *characters* (not tokens) is a comfortable chunk size that keeps
# inference snappy on CPU. The plan said ≤4096 tokens; that's roughly
# ≤16 000 characters, but we don't gain anything by going larger and
# shorter chunks make the rich progress smoother.
_CHUNK_CHARS = 4_000

# Overlap between chunks so an entity that straddles a boundary still
# gets detected on at least one side.
_CHUNK_OVERLAP = 200

_pipeline_lock = threading.Lock()
_pipeline: Any | None = None
_first_call_warned = False


def _get_pipeline() -> Any:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            # Quiet transformers' own per-call logging during inference.
            # ``transformers.logging`` is its public surface; the ``transformers``
            # stdlib logger is what writes to our console handler.
            logging.getLogger("transformers").setLevel(logging.ERROR)

            from transformers import pipeline  # heavy import; defer until needed

            log.info("privacy_filter.initializing", model=_MODEL_NAME)
            _pipeline = pipeline(
                "token-classification",
                model=_MODEL_NAME,
                aggregation_strategy="simple",
            )
        return _pipeline


def _iter_chunks(text: str) -> list[tuple[int, str]]:
    """Yield ``(absolute_start_offset, chunk_text)`` pairs."""
    if len(text) <= _CHUNK_CHARS:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + _CHUNK_CHARS)
        out.append((pos, text[pos:end]))
        if end == len(text):
            break
        pos = end - _CHUNK_OVERLAP
    return out


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Drop exact-overlap duplicates (caused by chunk overlap)."""
    seen: set[tuple[str, int, int]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.subtype, f.span_start, f.span_end)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def detect(text: str, *, score_threshold: float = 0.6) -> list[Finding]:
    """Run Privacy Filter and return findings above ``score_threshold``."""
    global _first_call_warned
    if not text:
        return []
    if not _first_call_warned:
        _first_call_warned = True
        log.info("privacy_filter.first_call_may_download_models")

    clf = _get_pipeline()
    out: list[Finding] = []

    for offset, chunk in _iter_chunks(text):
        try:
            results = clf(chunk)
        except Exception as e:
            log.exception(
                "privacy_filter.chunk_failed",
                chunk_offset=offset,
                chunk_chars=len(chunk),
                error=str(e),
            )
            continue
        for r in results:
            score = float(r.get("score", 0.0))
            if score < score_threshold:
                continue
            start = int(r.get("start", 0)) + offset
            end = int(r.get("end", 0)) + offset
            out.append(
                Finding(
                    detector="privacy_filter",
                    subtype=str(r.get("entity_group", "unknown")),
                    span_text=text[start:end],
                    span_start=start,
                    span_end=end,
                    confidence=score,
                )
            )

    return _dedupe(out)
