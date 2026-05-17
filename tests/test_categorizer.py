"""Tests for inbox_scanner.detection.categorizer.

The categorizer is the choke point that turns raw detector findings into
the per-message verdicts the UI shows. Keep it well-pinned.
"""

from __future__ import annotations

from inbox_scanner.detection.categorizer import (
    _CATEGORY_MAP,
    categorize,
    categorize_all,
    compute_verdict,
)
from inbox_scanner.detection.types import (
    FLAGGABLE_CATEGORIES,
    RISK_SCORE_CAP,
    RISK_WEIGHTS,
    Detection,
    Finding,
)


def _f(detector: str, subtype: str, **kw) -> Finding:
    """Build a Finding with sane defaults so tests focus on what matters."""
    return Finding(
        detector=detector,
        subtype=subtype,
        span_text=kw.get("span_text", "x"),
        span_start=kw.get("span_start", 0),
        span_end=kw.get("span_end", 1),
        confidence=kw.get("confidence", 0.9),
    )


# ---------- categorize() ----------


def test_presidio_ssn_is_gov_id():
    d = categorize(_f("presidio", "US_SSN"))
    assert d is not None
    assert d.category == "gov_id"


def test_presidio_credit_card_is_financial():
    assert categorize(_f("presidio", "CREDIT_CARD")).category == "financial"


def test_presidio_email_is_other_pii():
    assert categorize(_f("presidio", "EMAIL_ADDRESS")).category == "other_pii"


def test_privacy_filter_account_number_is_financial():
    assert categorize(_f("privacy_filter", "account_number")).category == "financial"


def test_privacy_filter_secret_is_credentials():
    assert categorize(_f("privacy_filter", "secret")).category == "credentials"


def test_privacy_filter_person_is_other_pii():
    assert categorize(_f("privacy_filter", "private_person")).category == "other_pii"


def test_custom_regex_tax_is_tax():
    assert categorize(_f("custom_regex", "tax_form")).category == "tax"


def test_custom_regex_mnemonic_is_credentials():
    assert categorize(_f("custom_regex", "mnemonic_phrase")).category == "credentials"


def test_dropped_custom_subtypes_no_longer_categorize():
    """The six subtypes removed in the v1 simplification must drop
    silently if any legacy code path ever emits them — never
    accidentally re-categorise."""
    for subtype in (
        "medical_record_number",
        "insurance_id",
        "medical_keyword",
        "credential_kv",
        "recovery_code",
        "legal_keyword",
    ):
        assert categorize(_f("custom_regex", subtype)) is None


def test_unknown_detector_dropped():
    assert categorize(_f("unknown_detector", "anything")) is None


def test_unknown_subtype_dropped():
    assert categorize(_f("presidio", "MADE_UP_ENTITY")) is None


def test_categorize_all_filters_unmapped():
    findings = [
        _f("presidio", "US_SSN"),
        _f("presidio", "MADE_UP_ENTITY"),  # dropped
        _f("custom_regex", "tax_form"),
    ]
    out = categorize_all(findings)
    assert [d.category for d in out] == ["gov_id", "tax"]


# ---------- compute_verdict() ----------


def _det(category: str) -> Detection:
    """Detection in the requested category, with an arbitrary finding."""
    finding = _f("custom_regex", "tax_form")  # subtype here doesn't matter
    return Detection(finding=finding, category=category)


def test_empty_verdict():
    v = compute_verdict([])
    assert v == {
        "is_flagged": False,
        "top_category": None,
        "risk_score": 0.0,
        "category_summary": {},
    }


def test_only_other_pii_does_not_flag():
    """A message with only names/addresses/emails is informational."""
    v = compute_verdict([_det("other_pii"), _det("other_pii")])
    assert v["is_flagged"] is False
    assert v["top_category"] == "other_pii"  # fallback for UI
    assert v["risk_score"] == 0.0
    assert v["category_summary"] == {"other_pii": 2}


def test_flag_on_any_real_pii():
    v = compute_verdict([_det("other_pii"), _det("financial")])
    assert v["is_flagged"] is True
    assert v["top_category"] == "financial"


def test_top_category_picks_highest_weight():
    """When multiple categories present, the one with the highest risk
    weight wins. tax(5) < financial(7) < credentials(10) == gov_id(10)."""
    v = compute_verdict([_det("tax"), _det("financial"), _det("legal")])
    assert v["top_category"] == "financial"


def test_top_category_breaks_ties_by_count():
    """gov_id and credentials both weight 10 — the one with more
    detections should win the tiebreak."""
    v = compute_verdict([_det("gov_id"), _det("credentials"), _det("credentials")])
    assert v["top_category"] == "credentials"


def test_top_category_breaks_double_tie_alphabetically():
    v = compute_verdict([_det("gov_id"), _det("credentials")])
    # Same weight (10), same count (1). Alphabetical → "credentials".
    assert v["top_category"] == "credentials"


def test_risk_score_sums_weights():
    # gov_id=10 + financial=7 + tax=5 = 22
    v = compute_verdict([_det("gov_id"), _det("financial"), _det("tax")])
    assert v["risk_score"] == 22.0


def test_risk_score_caps_at_100():
    # 12 × gov_id (weight 10) = 120 → capped at 100
    v = compute_verdict([_det("gov_id")] * 12)
    assert v["risk_score"] == float(RISK_SCORE_CAP)


def test_category_summary_counts_per_category():
    v = compute_verdict([_det("financial"), _det("financial"), _det("tax")])
    assert v["category_summary"] == {"financial": 2, "tax": 1}


# ---------- coverage / consistency ----------


def test_every_flaggable_category_has_a_weight():
    for cat in FLAGGABLE_CATEGORIES:
        assert RISK_WEIGHTS.get(cat, 0) > 0, f"missing risk weight for {cat}"


def test_every_mapped_category_is_known():
    """Every category referenced in _CATEGORY_MAP must appear in
    RISK_WEIGHTS — otherwise scoring would silently drop it."""
    seen_categories = {
        cat for by_detector in _CATEGORY_MAP.values() for cat in by_detector.values()
    }
    assert seen_categories <= set(RISK_WEIGHTS.keys()), (
        f"unweighted categories: {seen_categories - set(RISK_WEIGHTS.keys())}"
    )
