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
    Profile,
    profile_includes_tier,
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
        "mnemonic_phrase": "credentials",
        # Earlier subtypes (medical_record_number, insurance_id,
        # medical_keyword, credential_kv, recovery_code, legal_keyword)
        # were removed — see custom_regex.py docstring for the
        # rationale. The ``medical`` and ``legal`` user categories no
        # longer have any v1 feeders but are kept in RISK_WEIGHTS so a
        # future custom pattern can re-populate them without a
        # categorizer change.
    },
}


# Per-entity criticality tier. Drives the ``--profile`` filter:
#   * ``critical`` — irreversible-harm class; always reported regardless
#     of profile.
#   * ``standard`` — sensitive-but-recoverable; reported at ``standard``
#     and ``all``.
#   * ``all`` — informational context (today's ``other_pii``); only
#     reported at ``all``.
#
# A coverage test (test_categorizer.py) asserts every (detector,
# subtype) in _CATEGORY_MAP has a tier here, so adding a new entity
# without classifying it would fail loudly rather than silently default
# to "always show".
_TIER_MAP: dict[tuple[str, str], str] = {
    # ---- Presidio (9 entities) ----
    ("presidio", "US_SSN"):              "critical",
    ("presidio", "US_PASSPORT"):         "critical",
    ("presidio", "US_DRIVER_LICENSE"):   "critical",
    ("presidio", "US_ITIN"):             "critical",
    ("presidio", "CREDIT_CARD"):         "critical",
    ("presidio", "IBAN_CODE"):           "critical",
    ("presidio", "US_BANK_NUMBER"):      "critical",
    ("presidio", "EMAIL_ADDRESS"):       "all",
    ("presidio", "PHONE_NUMBER"):        "all",
    # ---- Privacy Filter (8 entities) ----
    ("privacy_filter", "secret"):           "critical",
    ("privacy_filter", "account_number"):   "standard",
    ("privacy_filter", "private_person"):   "all",
    ("privacy_filter", "private_address"):  "all",
    ("privacy_filter", "private_email"):    "all",
    ("privacy_filter", "private_phone"):    "all",
    ("privacy_filter", "private_url"):      "all",
    ("privacy_filter", "private_date"):     "all",
    # ---- Custom regex (2 patterns) ----
    ("custom_regex", "mnemonic_phrase"):    "critical",
    ("custom_regex", "tax_form"):           "standard",
}


def categorize(
    finding: Finding, profile: Profile = Profile.CRITICAL
) -> Detection | None:
    """Return the categorized Detection, or ``None`` to drop the finding.

    Drops in three cases:

    1. Unknown detector (no entry in ``_CATEGORY_MAP``).
    2. Unknown subtype for that detector.
    3. The entity's criticality tier isn't included in ``profile`` —
       e.g. an ``all``-tier ``private_address`` is dropped at the
       default ``critical`` profile.
    """
    by_detector = _CATEGORY_MAP.get(finding.detector)
    if by_detector is None:
        return None
    category = by_detector.get(finding.subtype)
    if category is None:
        return None
    tier = _TIER_MAP.get((finding.detector, finding.subtype))
    if tier is None or not profile_includes_tier(profile, tier):
        return None
    return Detection(finding=finding, category=category)


def categorize_all(
    findings: Iterable[Finding], profile: Profile = Profile.CRITICAL
) -> list[Detection]:
    out: list[Detection] = []
    for f in findings:
        d = categorize(f, profile)
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
