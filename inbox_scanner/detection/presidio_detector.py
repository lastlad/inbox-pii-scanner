"""Pattern-based PII detection via Microsoft Presidio.

Plan's stock entity allowlist: ``CREDIT_CARD``, ``IBAN_CODE``, ``US_SSN``,
``US_PASSPORT``, ``US_DRIVER_LICENSE``, ``US_BANK_NUMBER``, ``US_ITIN``,
``EMAIL_ADDRESS``, ``PHONE_NUMBER``. Generic spaCy NER labels (``PERSON``,
``LOCATION``, etc.) are **not** in this list — Privacy Filter handles
contextual entities and Presidio's NER would just duplicate noise.

The :class:`AnalyzerEngine` is heavy (loads spaCy + recognizer registry on
first construction), so we instantiate it lazily as a process-singleton.
"""

from __future__ import annotations

import logging
import threading

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider

from inbox_scanner.detection.types import Finding
from inbox_scanner.logging import get_logger

log = get_logger("detection.presidio")

# Pinned to the small spaCy model — the large one is 580 MB and offers
# basically no benefit for the pattern-based recognizers we actually use.
_SPACY_MODEL = "en_core_web_sm"

PRESIDIO_ENTITIES: tuple[str, ...] = (
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "US_BANK_NUMBER",
    "US_ITIN",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
)

_engine_lock = threading.Lock()
_engine: AnalyzerEngine | None = None


def _get_engine() -> AnalyzerEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            log.info("presidio.initializing", spacy_model=_SPACY_MODEL)
            # Presidio logs a WARNING for every spaCy NER label that isn't
            # in its own entity map (MONEY, DATE_TIME, ORG, …). Those are
            # informational — we explicitly pass our allowlist to
            # ``analyze`` so the unmapped labels are dropped anyway. Lift
            # the logger to ERROR so they don't spam the scan output.
            logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)
            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": _SPACY_MODEL}],
                }
            )
            _engine = AnalyzerEngine(nlp_engine=provider.create_engine())
        return _engine


def detect(text: str, *, score_threshold: float = 0.5) -> list[Finding]:
    """Run Presidio against ``text`` and return findings above the threshold."""
    if not text:
        return []
    engine = _get_engine()
    results = engine.analyze(
        text=text,
        language="en",
        entities=list(PRESIDIO_ENTITIES),
        score_threshold=score_threshold,
    )
    return [
        Finding(
            detector="presidio",
            subtype=r.entity_type,
            span_text=text[r.start:r.end],
            span_start=r.start,
            span_end=r.end,
            confidence=float(r.score),
        )
        for r in results
    ]
