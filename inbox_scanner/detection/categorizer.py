"""Map raw detector findings → user-facing categories + per-message verdict.

The single source of truth for what each detector subtype means in the
UI and how it interacts with ``--profile``. Add a new detector subtype?
Add **one** row to ``_REGISTRY``; the rest of the pipeline keeps working
without further code changes.

Verdict computation (``compute_verdict``):

* ``is_flagged`` is true when at least one finding belongs to one of the
  flaggable user categories (gov_id, financial, tax, credentials). A
  message with only ``other_pii`` findings (names, addresses, emails
  alone) is informational and does not flag.
* ``risk_score`` sums per-category weights from
  :data:`inbox_scanner.detection.types.RISK_WEIGHTS` over the message's
  detections, capped at :data:`RISK_SCORE_CAP`.
* ``top_category`` is the highest-weighted category present (ties
  broken by detection count, then alphabetical for determinism).
* ``category_summary`` is ``{category: count}`` for the UI's badges.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, NamedTuple

from inbox_scanner.detection.types import (
    FLAGGABLE_CATEGORIES,
    RISK_SCORE_CAP,
    RISK_WEIGHTS,
    Detection,
    Finding,
    Profile,
    profile_includes_tier,
)


class _Entry(NamedTuple):
    """One row of the detector registry.

    ``category`` is the user-facing bucket (gov_id, financial, tax,
    credentials, other_pii). ``tier`` is the criticality classification
    the ``--profile`` filter consults (critical, all).
    """

    category: str
    tier: str


# (detector, subtype) → (user category, criticality tier).
#
# Subtype matching is case-sensitive — Presidio uses UPPER_SNAKE,
# Privacy Filter uses lower_snake, custom regex uses lower_snake.
#
# To add a new detector subtype: add ONE row here. The categorize()
# function will pick it up; the test_every_registry_entry_is_valid
# coverage test enforces that ``tier`` is a known value and that
# ``category`` has a weight in RISK_WEIGHTS.
_REGISTRY: dict[tuple[str, str], _Entry] = {
    # ---- Presidio (9 entities) -----------------------------------------
    ("presidio", "US_SSN"):              _Entry("gov_id",      "critical"),
    ("presidio", "US_PASSPORT"):         _Entry("gov_id",      "critical"),
    ("presidio", "US_DRIVER_LICENSE"):   _Entry("gov_id",      "critical"),
    ("presidio", "US_ITIN"):             _Entry("gov_id",      "critical"),
    ("presidio", "CREDIT_CARD"):         _Entry("financial",   "critical"),
    ("presidio", "IBAN_CODE"):           _Entry("financial",   "critical"),
    ("presidio", "US_BANK_NUMBER"):      _Entry("financial",   "critical"),
    ("presidio", "EMAIL_ADDRESS"):       _Entry("other_pii",   "all"),
    ("presidio", "PHONE_NUMBER"):        _Entry("other_pii",   "all"),
    # ---- Privacy Filter (8 entities) -----------------------------------
    ("privacy_filter", "secret"):           _Entry("credentials", "critical"),
    ("privacy_filter", "account_number"):   _Entry("financial",   "all"),
    ("privacy_filter", "private_person"):   _Entry("other_pii",   "all"),
    ("privacy_filter", "private_address"):  _Entry("other_pii",   "all"),
    ("privacy_filter", "private_email"):    _Entry("other_pii",   "all"),
    ("privacy_filter", "private_phone"):    _Entry("other_pii",   "all"),
    ("privacy_filter", "private_url"):      _Entry("other_pii",   "all"),
    ("privacy_filter", "private_date"):     _Entry("other_pii",   "all"),
}


def categorize(
    finding: Finding, profile: Profile = Profile.CRITICAL
) -> Detection | None:
    """Return the categorized Detection, or ``None`` to drop the finding.

    Drops in two cases:

    1. The ``(detector, subtype)`` pair is unknown.
    2. The entity's criticality tier isn't included in ``profile`` —
       e.g. an ``all``-tier ``private_address`` is dropped at the
       default ``critical`` profile.
    """
    entry = _REGISTRY.get((finding.detector, finding.subtype))
    if entry is None:
        return None
    if not profile_includes_tier(profile, entry.tier):
        return None
    return Detection(finding=finding, category=entry.category)


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
