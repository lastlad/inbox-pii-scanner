"""Shared dataclasses for the detection layer.

A :class:`Finding` is what one detector produces (one match in the input
text). A :class:`Detection` is what gets persisted — same shape plus the
user-facing category produced by the categorizer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Finding:
    """One raw match from a single detector.

    ``detector`` is the source name (``"presidio"`` or
    ``"privacy_filter"``). ``subtype`` is the detector-native label
    (e.g. ``"US_SSN"``, ``"private_address"``) — the categorizer maps
    these to user-facing categories.
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
# isn't here — names/addresses/emails alone are too noisy to flag on.
# The plan also defined ``medical`` and ``legal``; both were removed
# in the v1 simplification (no detector fed them after the custom regex
# pare-down — see docs/decisions/0005). Re-add here + the categorizer's
# registry if you ever ship a detector for them.
FLAGGABLE_CATEGORIES: frozenset[str] = frozenset(
    {"gov_id", "financial", "tax", "credentials"}
)


# Per-category risk weight. Sum across a message's detections, cap at 100.
RISK_WEIGHTS: dict[str, int] = {
    "gov_id": 10,
    "credentials": 10,
    "financial": 7,
    "tax": 5,
    "other_pii": 0,
}
RISK_SCORE_CAP = 100


class Profile(str, Enum):
    """How aggressive should detection be?

    ``critical`` — only ever-flag-this-class entities (SSN, passport,
    credit card, IBAN, US bank, ITIN, driver's license, secret). The
    default. Catches catastrophic-leak PII and nothing else.

    ``all`` — additionally records Privacy Filter's broader catches:
    ``account_number`` (still flags the message via the financial
    category) plus the informational ``other_pii`` entities (names,
    addresses, emails, phones, URLs, dates) which surface as
    context but don't flag on their own.

    An intermediate ``standard`` profile was dropped during the v1
    simplification: with ``tax_form`` removed it would have differed
    from ``all`` only by which informational entities got recorded,
    and the flagged-set was identical. See
    docs/decisions/0005-three-detector-pipeline.md.
    """

    CRITICAL = "critical"
    ALL = "all"


# Lower index = more selective. ``profile_includes_tier`` is the only
# function that should consult this — callers should use the helper.
_TIER_ORDER: tuple[str, ...] = ("critical", "all")


def profile_includes_tier(profile: Profile, tier: str) -> bool:
    """True if ``profile`` should include findings whose tier is ``tier``.

    A ``critical`` profile includes only critical-tier findings; a
    ``standard`` profile includes critical + standard; ``all``
    includes everything.
    """
    try:
        return _TIER_ORDER.index(tier) <= _TIER_ORDER.index(profile.value)
    except ValueError:
        # Unknown tier string — be conservative and drop.
        return False
