"""Tests for inbox_scanner.detection.custom_regex.

Each pattern gets a positive case (a string we definitely should match)
and a negative case (a near-miss we shouldn't false-positive on).
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


def test_tax_form_no_match_in_words():
    # "1040" inside a longer number shouldn't match (word boundary).
    assert "tax_form" not in _subtypes("Order number 1040567890 confirmed")


# ---------- medical ----------


def test_medical_record_number():
    found = _subtypes("Patient MRN: 12345-67")
    assert "medical_record_number" in found


def test_medical_record_long_form():
    found = _subtypes("Medical Record Number: ABC-7788")
    assert "medical_record_number" in found


def test_insurance_id():
    found = _subtypes("Member ID: BCBS-123456789")
    assert "insurance_id" in found


def test_medical_keyword():
    found = _subtypes("Diagnosis: hypertension. Prescribed: lisinopril 10mg.")
    assert "medical_keyword" in found


def test_medical_no_match_in_unrelated_text():
    # Avoid catching "patient" alone or other non-clinical contexts.
    assert "medical_record_number" not in _subtypes(
        "Be patient with the system while it loads."
    )


# ---------- credentials ----------


def test_credential_kv_password():
    found = _subtypes("password: hunter2pass")
    assert "credential_kv" in found


def test_credential_kv_api_key():
    found = _subtypes("api_key=sk_live_12345abcdef")
    assert "credential_kv" in found


def test_credential_kv_ignores_short_value():
    # "***" or 3-character placeholders shouldn't trigger.
    assert "credential_kv" not in _subtypes("password: ***")


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
    # All-caps or mixed-case shouldn't match (BIP-39 mnemonics are
    # canonical lowercase).
    phrase = "Apple Banana Cherry " * 4
    assert "mnemonic_phrase" not in _subtypes(phrase)


def test_recovery_code():
    found = _subtypes("Recovery code: ABCD-EFGH-IJKL")
    assert "recovery_code" in found


# ---------- legal ----------


def test_legal_keyword_lease():
    found = _subtypes("This Lease Agreement is between Landlord and Tenant")
    assert "legal_keyword" in found


def test_legal_keyword_will():
    found = _subtypes("Last Will and Testament of John Q Public")
    assert "legal_keyword" in found


def test_legal_keyword_party_of():
    found = _subtypes("hereinafter Party of the first part shall pay…")
    assert "legal_keyword" in found


# ---------- public surface ----------


def test_supported_subtypes_pinned():
    expected = {
        "tax_form",
        "medical_record_number",
        "insurance_id",
        "medical_keyword",
        "credential_kv",
        "mnemonic_phrase",
        "recovery_code",
        "legal_keyword",
    }
    assert set(custom_regex.supported_subtypes()) == expected


def test_empty_text_returns_empty():
    assert custom_regex.detect("") == []
    assert custom_regex.detect("   \n  ") == []
