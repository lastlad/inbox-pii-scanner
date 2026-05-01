"""Shared dataclasses for the detection layer.

A :class:`Finding` is what one detector produces (one match in the input
text). A :class:`Detection` is what gets persisted — same shape plus the
user-facing category produced by the categorizer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """One raw match from a single detector.

    ``detector`` is the source name (``"presidio"``, ``"privacy_filter"``,
    or ``"custom_regex"``). ``subtype`` is the detector-native label
    (e.g. ``"US_SSN"``, ``"private_address"``, ``"tax_form"``) — the
    categorizer maps these to user-facing categories.
    """

    detector: str
    subtype: str
    span_text: str
    span_start: int
    span_end: int
    confidence: float


@dataclass(frozen=True)
class Detection:
    """A persisted finding with its user-facing category attached."""

    finding: Finding
    category: str  # gov_id | financial | tax | medical | credentials | legal | other_pii


# Categories that flag a message for review. ``other_pii`` deliberately
# isn't here — names/addresses/emails alone are too noisy to flag on,
# and the plan's UX is "flag if the message has at least one finding in
# one of these six categories".
FLAGGABLE_CATEGORIES: frozenset[str] = frozenset(
    {"gov_id", "financial", "tax", "medical", "credentials", "legal"}
)


# Plan's risk weights. Sum across detections, cap at 100.
RISK_WEIGHTS: dict[str, int] = {
    "gov_id": 10,
    "credentials": 10,
    "financial": 7,
    "medical": 7,
    "tax": 5,
    "legal": 3,
    "other_pii": 0,
}
RISK_SCORE_CAP = 100
