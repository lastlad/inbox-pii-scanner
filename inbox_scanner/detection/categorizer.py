"""Map raw detector findings → user-facing categories + per-message verdict.

The single source of truth for what each detector subtype means in the UI.
Add a new detector? You add a row here; the rest of the pipeline keeps
working without code changes.

Verdict computation (`compute_verdict`):

* ``is_flagged`` is true when at least one finding belongs to one of the
  six "real PII" categories (gov_id, financial, tax, medical,
  credentials, legal). A message with only ``other_pii`` findings (names,
  addresses, emails alone) is informational and does not flag.
* ``risk_score`` sums the per-category weights from
  :data:`inbox_scanner.detection.types.RISK_WEIGHTS` over the message's
  detections, capped at :data:`RISK_SCORE_CAP`.
* ``top_category`` is the highest-weighted category present (ties broken
  by detection count, then alphabetical for determinism).
* ``category_summary`` is ``{category: count}`` for the UI's badges.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from inbox_scanner.detection.types import (
    FLAGGABLE_CATEGORIES,
    RISK_SCORE_CAP,
    RISK_WEIGHTS,
    Detection,
    Finding,
)

# detector → subtype → user category
# Subtype matching is case-sensitive — Presidio uses UPPER_SNAKE,
# Privacy Filter uses lower_snake, our regex uses lower_snake.
_CATEGORY_MAP: dict[str, dict[str, str]] = {
    "presidio": {
        "US_SSN": "gov_id",
        "US_PASSPORT": "gov_id",
        "US_DRIVER_LICENSE": "gov_id",
        "US_ITIN": "gov_id",
        "CREDIT_CARD": "financial",
        "IBAN_CODE": "financial",
        "US_BANK_NUMBER": "financial",
        "EMAIL_ADDRESS": "other_pii",
        "PHONE_NUMBER": "other_pii",
    },
    "privacy_filter": {
        "account_number": "financial",
        "secret": "credentials",
        "private_address": "other_pii",
        "private_email": "other_pii",
        "private_person": "other_pii",
        "private_phone": "other_pii",
        "private_url": "other_pii",
        "private_date": "other_pii",
    },
    "custom_regex": {
        "tax_form": "tax",
        "medical_record_number": "medical",
        "insurance_id": "medical",
        "medical_keyword": "medical",
        "credential_kv": "credentials",
        "mnemonic_phrase": "credentials",
        "recovery_code": "credentials",
        "legal_keyword": "legal",
    },
}


def categorize(finding: Finding) -> Detection | None:
    """Return the categorized Detection, or ``None`` to drop the finding."""
    by_detector = _CATEGORY_MAP.get(finding.detector)
    if by_detector is None:
        return None
    category = by_detector.get(finding.subtype)
    if category is None:
        return None
    return Detection(finding=finding, category=category)


def categorize_all(findings: Iterable[Finding]) -> list[Detection]:
    out: list[Detection] = []
    for f in findings:
        d = categorize(f)
        if d is not None:
            out.append(d)
    return out


def compute_verdict(detections: Iterable[Detection]) -> dict:
    """Aggregate per-message verdict from a list of categorized detections."""
    detections = list(detections)
    if not detections:
        return {
            "is_flagged": False,
            "top_category": None,
            "risk_score": 0.0,
            "category_summary": {},
        }

    counts: Counter[str] = Counter(d.category for d in detections)

    # Flag if any flaggable category appears at all.
    is_flagged = any(c in FLAGGABLE_CATEGORIES for c in counts)

    # Risk score: sum of per-category weight × count, capped.
    raw_score = sum(RISK_WEIGHTS.get(cat, 0) * n for cat, n in counts.items())
    risk_score = float(min(RISK_SCORE_CAP, raw_score))

    # Top category: highest weight, then highest count, then alphabetical.
    def _rank(cat: str) -> tuple[int, int, str]:
        return (-RISK_WEIGHTS.get(cat, 0), -counts[cat], cat)

    flaggable_present = [c for c in counts if c in FLAGGABLE_CATEGORIES]
    if flaggable_present:
        top_category = sorted(flaggable_present, key=_rank)[0]
    else:
        # All-other-pii fallback: pick the most common informational
        # category so the UI has *something* to show.
        top_category = sorted(counts, key=_rank)[0]

    return {
        "is_flagged": is_flagged,
        "top_category": top_category,
        "risk_score": risk_score,
        "category_summary": dict(counts),
    }
