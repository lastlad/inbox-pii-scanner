"""High-signal US-specific patterns that the two models miss.

Pared down to the two patterns that provided unique signal in real-world
testing and aren't covered by Presidio or Privacy Filter:

* ``tax_form`` — US tax form titles (W-2, 1099-*, 1040-*, Schedule [A-K],
  Form NNNN, K-1). Document-type signal: catches blank or filled forms
  that contain little or no fillable PII (e.g. an HSA Withdrawal Form
  template) and that the two models would otherwise return nothing for.
* ``mnemonic_phrase`` — 12- or 24-word BIP-39-shaped sequences. Crypto
  wallet seed phrases look like ordinary plain-English wordlists, so
  Privacy Filter's ``secret`` label doesn't generalize to them. The
  surrounding context (12 or 24 short lowercase words in a row) is so
  unusual that the false-positive rate is near zero.

Earlier iterations also shipped patterns for medical record numbers,
insurance IDs, medical/legal keyword cues, credential ``key=value``
leaks, and recovery codes. They were dropped because they either
duplicated Privacy Filter's ``secret`` / ``account_number`` labels at
lower precision (credential_kv, recovery_code), or were bare keyword
spotters with too-low specificity to be actionable (medical_keyword,
legal_keyword), or never fired on the dev corpus despite the
attendant maintenance cost (medical_record_number, insurance_id).
See `docs/decisions/0005-three-detector-pipeline.md` for the full
history.
"""

from __future__ import annotations

import re
from typing import Iterable

from inbox_scanner.detection.types import Finding

# ---------- Tax forms ----------
# Matches the form name itself, not its content; the *presence* of "W-2"
# in a document is signal that the document is tax-related, regardless
# of whether the form is filled in.
_TAX_FORM_PATTERN = re.compile(
    r"\b("
    r"W-?2(?:G)?"                 # W-2, W2, W-2G
    r"|W-?9"                      # W-9
    r"|W-?4"                      # W-4
    r"|1099-?(?:MISC|NEC|DIV|INT|R|G|B|K|S|Q|SA|LTC|CAP|OID)"
    r"|1098(?:-T|-E|-C|-MA)?"
    r"|1040(?:-EZ|-X|-NR|-SR|-V|-ES)?"
    r"|Schedule\s+[A-K]"          # Schedule A, B, C, ...
    r"|Form\s+(?:8606|8888|8889|4868|2555|2106|4562|5329|941|940|1065|1120(?:-S)?|1041|706|709)"
    r"|K-?1"                      # K-1 (partnership / S-corp)
    r")\b",
    re.IGNORECASE,
)

# ---------- BIP-39 mnemonic phrase ----------
# 12- or 24-word sequence of short lowercase tokens. Very high signal,
# very low FPR — the all-lowercase, no-punctuation, exact-count
# combination is so unusual that the rare false positive (e.g. a list of
# common English words in marketing copy) is acceptable.
_MNEMONIC_PHRASE_PATTERN = re.compile(
    r"(?<![A-Za-z])"
    r"(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}"
    r"(?![A-Za-z])"
)


_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_TAX_FORM_PATTERN, "tax_form"),
    (_MNEMONIC_PHRASE_PATTERN, "mnemonic_phrase"),
)


# Regex matches don't have a meaningful confidence score; we use a fixed
# value per pattern based on observed false-positive rate.
_CONFIDENCE: dict[str, float] = {
    "tax_form": 0.85,
    "mnemonic_phrase": 0.95,
}


def detect(text: str) -> list[Finding]:
    """Run the custom regex patterns and return findings."""
    if not text:
        return []
    out: list[Finding] = []
    for pattern, subtype in _PATTERNS:
        for m in pattern.finditer(text):
            out.append(
                Finding(
                    detector="custom_regex",
                    subtype=subtype,
                    span_text=m.group(0),
                    span_start=m.start(),
                    span_end=m.end(),
                    confidence=_CONFIDENCE[subtype],
                )
            )
    return out


def supported_subtypes() -> Iterable[str]:
    """Used by tests to pin the public surface."""
    return tuple(s for _, s in _PATTERNS)
