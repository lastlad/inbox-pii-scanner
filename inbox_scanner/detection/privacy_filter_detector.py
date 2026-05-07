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
import warnings
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
            # Silence huggingface_hub's "unauthenticated requests" tip on
            # every cache check. It's informational — the call still
            # works and the tip applies only to public models hitting
            # rate limits, which we don't. Both routes (stdlib logger
            # and warnings.warn) need silencing because the library
            # version changes which one it uses.
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            warnings.filterwarnings(
                "ignore",
                message=".*unauthenticated requests.*",
            )

            from transformers import pipeline  # heavy import; defer until needed
            from transformers.utils import logging as hf_logging

            # Suppress the ``Loading weights: 0%…100%`` tqdm bar shown
            # during model load. We surface our own structlog event when
            # the model is initialized, which is the canonical record.
            hf_logging.set_verbosity_error()
            hf_logging.disable_progress_bar()

            log.info("privacy_filter.initializing", model=_MODEL_NAME)
            # ``aggregation_strategy="simple"`` doesn't merge across a
            # BIE-sequence → S-single-token boundary; we fix that
            # post-hoc with :func:`_merge_adjacent_same_subtype` so the
            # API and UI see one finding per logical entity.
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


def _merge_adjacent_same_subtype(
    findings: list[Finding], text: str
) -> list[Finding]:
    """Coalesce consecutive same-subtype findings whose spans touch.

    Mitigates the BIE → S aggregation artifact in HuggingFace's "simple"
    strategy, where a tokenizer's 2+1 subword split produces two
    findings for what should be one entity (e.g. ``"Sa…V"`` then
    ``"emu"`` for one ``private_person``).

    Two findings merge when:

    1. They share the same ``subtype`` (and therefore the same detector,
       since subtypes are detector-namespaced).
    2. There's at most one character of text between them
       (``span_end >= next.span_start - 1``).

    Confidence of the merged finding is a length-weighted mean of the
    components — closest to what a single span would have if the model
    had emitted one. ``span_text`` is re-sliced from ``text`` so it
    spans the merged range exactly.
    """
    if not findings:
        return []
    sorted_findings = sorted(findings, key=lambda f: f.span_start)
    merged: list[Finding] = []
    for f in sorted_findings:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.subtype == f.subtype
            and prev.detector == f.detector
            and prev.span_end >= f.span_start - 1
        ):
            new_start = prev.span_start
            new_end = max(prev.span_end, f.span_end)
            prev_len = max(1, prev.span_end - prev.span_start)
            cur_len = max(1, f.span_end - f.span_start)
            total_len = prev_len + cur_len
            new_conf = (
                prev.confidence * prev_len + f.confidence * cur_len
            ) / total_len
            merged[-1] = Finding(
                detector=prev.detector,
                subtype=prev.subtype,
                span_text=text[new_start:new_end],
                span_start=new_start,
                span_end=new_end,
                confidence=new_conf,
            )
        else:
            merged.append(f)
    return merged


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

    return _merge_adjacent_same_subtype(_dedupe(out), text)
