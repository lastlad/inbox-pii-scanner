"""Integration tests for the Tier A international Presidio recognizers.

These exercise the full AnalyzerEngine (singleton), so the first test in
the run pays a one-time ~3 s spaCy load. Subsequent tests are fast.

Positive samples use values with valid checksums or strict-format strings
plus a context token (NHS, NINO, TFN, …) so the recognizer scores above
the detector's 0.5 threshold. Negative samples use junk that matches the
loose shape but should fail validation or score below threshold — they
guard against the recognizers being too eager.
"""

from __future__ import annotations

import pytest

from inbox_scanner.detection.presidio_detector import detect


# ---------- positive cases ----------

# Each row: (subtype, text containing a valid sample + context token).
_POSITIVES: list[tuple[str, str]] = [
    ("UK_NHS",                    "Patient NHS number: 943 476 5919."),
    ("UK_NINO",                   "NINO AB123456C, NI number on payslip."),
    ("ES_NIF",                    "Mi DNI es 12345678Z."),
    ("IT_FISCAL_CODE",            "Il codice fiscale è RSSMRA85T10A562S."),
    ("AU_TFN",                    "My Tax File Number is 123 456 782."),
    ("AU_MEDICARE",               "Medicare card 2123 45670 1."),
    ("SG_NRIC_FIN",               "NRIC S1234567D."),
    ("IN_AADHAAR",                "Aadhaar number 234123412346."),
    ("IN_PAN",                    "PAN card no.: ABCPK1234D."),
    ("FI_PERSONAL_IDENTITY_CODE", "Henkilötunnus 131052-308T."),
]


@pytest.mark.parametrize("subtype,text", _POSITIVES, ids=[s for s, _ in _POSITIVES])
def test_international_recognizer_fires(subtype: str, text: str):
    """Each Tier A recognizer must fire on a known-good sample."""
    findings = detect(text)
    matching = [f for f in findings if f.subtype == subtype]
    assert matching, (
        f"expected {subtype} to fire on {text!r}, "
        f"got {[(f.detector, f.subtype, f.span_text) for f in findings]}"
    )
    # Span must contain the expected ID-shaped substring, not random text.
    assert any(any(ch.isdigit() for ch in m.span_text) for m in matching)


# ---------- negative cases ----------

# Junk strings that approximate the shape but fail the recognizer's
# checksum or strict-format check. ``IT_FISCAL_CODE`` is harder to break
# without a checksum function reimplementation here; skip it (the
# positive case already pins the regex shape).
_NEGATIVES: list[tuple[str, str]] = [
    # NHS rejects strings whose Mod-11 checksum doesn't match.
    ("UK_NHS",      "Random number 943 476 5910 in some unrelated context."),
    # Bad NIF check letter (right format, wrong terminator).
    ("ES_NIF",      "Mi número 12345678A es incorrecto."),
    # TFN with mismatched weighted checksum.
    ("AU_TFN",      "Reference 111 222 333 should not match."),
    # Aadhaar with bad Verhoeff checksum.
    ("IN_AADHAAR",  "Number 123456789012 is not a real Aadhaar."),
    # PAN with invalid 4th character (entity-type letter must be valid).
    ("IN_PAN",      "Code ABCDX1234F is not a PAN."),
]


@pytest.mark.parametrize("subtype,text", _NEGATIVES, ids=[s for s, _ in _NEGATIVES])
def test_international_recognizer_rejects_junk(subtype: str, text: str):
    """Each recognizer must reject a shape-similar value with a bad
    checksum / out-of-spec character. Guards against the recognizers
    being so loose they fire on every digit run."""
    findings = detect(text)
    matching = [f for f in findings if f.subtype == subtype]
    assert not matching, (
        f"{subtype} unexpectedly fired on junk text {text!r}: "
        f"{[m.span_text for m in matching]}"
    )


# ---------- coverage / wiring ----------


def test_all_tier_a_entities_are_in_the_allowlist():
    """Smoke check: the eleven Tier A subtypes are wired into
    PRESIDIO_ENTITIES so ``analyze(entities=...)`` lets them through."""
    from inbox_scanner.detection.presidio_detector import PRESIDIO_ENTITIES

    expected = {s for s, _ in _POSITIVES}
    missing = expected - set(PRESIDIO_ENTITIES)
    assert not missing, f"not in PRESIDIO_ENTITIES allowlist: {missing}"


def test_all_tier_a_entities_have_categorizer_rows():
    """Each new subtype must have a (category, tier) row, or it would
    silently get dropped by the categorizer with no log."""
    from inbox_scanner.detection.categorizer import _REGISTRY

    for subtype, _ in _POSITIVES:
        assert ("presidio", subtype) in _REGISTRY, (
            f"missing categorizer row for ('presidio', {subtype!r})"
        )
