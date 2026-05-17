"""Pattern-based PII detection via Microsoft Presidio.

Stock US/global entities the AnalyzerEngine loads for English by default:
``CREDIT_CARD``, ``IBAN_CODE``, ``US_SSN``, ``US_PASSPORT``,
``US_DRIVER_LICENSE``, ``US_BANK_NUMBER``, ``US_ITIN``, ``EMAIL_ADDRESS``,
``PHONE_NUMBER``. Generic spaCy NER labels (``PERSON``, ``LOCATION``, etc.)
are **not** in this list — Privacy Filter handles contextual entities and
Presidio's NER would just duplicate noise.

On top of those, we explicitly register a Tier A international set:
``UK_NHS``, ``UK_NINO``, ``ES_NIF``, ``IT_FISCAL_CODE``, ``AU_TFN``,
``AU_MEDICARE``, ``SG_NRIC_FIN``, ``IN_AADHAAR``, ``IN_PAN``, ``PL_PESEL``,
``FI_PERSONAL_IDENTITY_CODE``. All eleven have strict format rules and/or
checksums, so their false-positive rate on English-language email is low.
The four whose stock language code is non-English (Spanish, Italian,
Polish, Finnish) are re-registered with ``supported_language="en"`` —
the underlying patterns are language-agnostic regex over Latin
characters and digits, so cross-language reuse is safe.

The :class:`AnalyzerEngine` is heavy (loads spaCy + recognizer registry on
first construction), so we instantiate it lazily as a process-singleton.
"""

from __future__ import annotations

import logging
import threading

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    AuMedicareRecognizer,
    AuTfnRecognizer,
    EsNifRecognizer,
    FiPersonalIdentityCodeRecognizer,
    InAadhaarRecognizer,
    InPanRecognizer,
    ItFiscalCodeRecognizer,
    PlPeselRecognizer,
    SgFinRecognizer,
    UkNinoRecognizer,
)

from inbox_scanner.detection.types import Finding
from inbox_scanner.logging import get_logger

log = get_logger("detection.presidio")

# Pinned to the small spaCy model — the large one is 580 MB and offers
# basically no benefit for the pattern-based recognizers we actually use.
_SPACY_MODEL = "en_core_web_sm"

# Tier A international IDs — strong format/checksum, low FP risk.
# AU/IN/SG/UK_NINO classes already default to ``en``; the four below
# default to their native language code and need an explicit override so
# they participate in our English-only analyze() calls.
_INTERNATIONAL_RECOGNIZER_CLASSES: tuple[type, ...] = (
    UkNinoRecognizer,
    AuTfnRecognizer,
    AuMedicareRecognizer,
    SgFinRecognizer,
    InAadhaarRecognizer,
    InPanRecognizer,
)
_INTERNATIONAL_RECOGNIZER_CLASSES_REQUIRING_EN_OVERRIDE: tuple[type, ...] = (
    EsNifRecognizer,
    ItFiscalCodeRecognizer,
    PlPeselRecognizer,
    FiPersonalIdentityCodeRecognizer,
)

PRESIDIO_ENTITIES: tuple[str, ...] = (
    # ---- US / global (stock English defaults) -------------------------
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "US_BANK_NUMBER",
    "US_ITIN",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    # ---- International Tier A (explicitly registered below) -----------
    "UK_NHS",
    "UK_NINO",
    "ES_NIF",
    "IT_FISCAL_CODE",
    "AU_TFN",
    "AU_MEDICARE",
    "SG_NRIC_FIN",
    "IN_AADHAAR",
    "IN_PAN",
    "PL_PESEL",
    "FI_PERSONAL_IDENTITY_CODE",
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
            # Stock English bundle already includes ``UK_NHS`` via
            # NhsRecognizer; the others ship as classes but aren't
            # auto-loaded for ``en``. Register them explicitly.
            for cls in _INTERNATIONAL_RECOGNIZER_CLASSES:
                _engine.registry.add_recognizer(cls())
            for cls in _INTERNATIONAL_RECOGNIZER_CLASSES_REQUIRING_EN_OVERRIDE:
                _engine.registry.add_recognizer(cls(supported_language="en"))
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
