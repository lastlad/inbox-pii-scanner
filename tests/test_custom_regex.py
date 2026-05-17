"""Tests for inbox_scanner.detection.custom_regex.

Only two patterns survive in v1: ``tax_form`` and ``mnemonic_phrase``.
Each gets a positive case (definitely should match) and at least one
negative case (a near-miss we shouldn't false-positive on).
"""

from __future__ import annotations

from inbox_scanner.detection import custom_regex


def _subtypes(text: str) -> list[str]:
    return [f.subtype for f in custom_regex.detect(text)]


# ---------- tax forms ----------


def test_tax_form_w2():
    assert "tax_form" in _subtypes("Your W-2 form for tax year 2024 is attached.")


def test_tax_form_1099():
    assert "tax_form" in _subtypes("1099-NEC: Nonemployee Compensation")
    assert "tax_form" in _subtypes("1099-MISC for $1,234.56")


def test_tax_form_1040():
    assert "tax_form" in _subtypes("File Form 1040-SR for the senior return")


def test_tax_form_schedule():
    assert "tax_form" in _subtypes("Attach Schedule C with your return")


def test_tax_form_hsa_8889():
    # The dev-corpus HSA Withdrawal Form caught only because of this one.
    assert "tax_form" in _subtypes(
        "I further understand that … report the distribution … "
        "Form 8889 for HSA …"
    )


def test_tax_form_no_match_in_words():
    # "1040" inside a longer number shouldn't match (word boundary).
    assert "tax_form" not in _subtypes("Order number 1040567890 confirmed")


# ---------- mnemonic phrase ----------


def test_mnemonic_phrase_12_words():
    phrase = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    assert "mnemonic_phrase" in _subtypes(phrase)


def test_mnemonic_phrase_24_words():
    phrase = " ".join(["zebra"] * 24)
    assert "mnemonic_phrase" in _subtypes(phrase)


def test_mnemonic_phrase_too_short():
    phrase = " ".join(["zebra"] * 6)
    assert "mnemonic_phrase" not in _subtypes(phrase)


def test_mnemonic_phrase_rejects_capitalized():
    # All-caps or mixed-case shouldn't match — canonical BIP-39 wordlists
    # are lowercase.
    phrase = "Apple Banana Cherry " * 4
    assert "mnemonic_phrase" not in _subtypes(phrase)


# ---------- public surface ----------


def test_supported_subtypes_pinned():
    """Pin the v1 set so accidental additions / removals show up here."""
    assert set(custom_regex.supported_subtypes()) == {"tax_form", "mnemonic_phrase"}


def test_empty_text_returns_empty():
    assert custom_regex.detect("") == []
    assert custom_regex.detect("   \n  ") == []
