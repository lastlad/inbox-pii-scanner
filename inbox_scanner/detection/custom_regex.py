"""US-specific regex patterns Presidio + Privacy Filter miss or handle weakly.

Each pattern emits a Finding with ``detector='custom_regex'`` and a
distinct ``subtype`` that the categorizer maps to a user-facing category.

These patterns intentionally err on the recall side — a v1 user reviews
each flagged email manually, so a moderate false-positive rate is fine,
but a tax form or medical record number that slips past detection is the
failure mode we care about. The categorizer downgrades the noisiest of
these (e.g. ``legal_keyword``) to lower-weight categories so they don't
dominate the risk score.
"""

from __future__ import annotations

import re
from typing import Iterable

from inbox_scanner.detection.types import Finding

# ---------- Tax forms ----------
# Common US tax-form headers and titles. Matches the form name itself, not
# its content; the *presence* of "W-2" in a document is signal that the
# document is tax-related.
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

# ---------- Medical / insurance ----------
# Medical record numbers — usually labelled.
_MEDICAL_RECORD_PATTERN = re.compile(
    r"\b(?:MRN|Medical\s+Record\s+(?:Number|No\.?|#)|Patient\s+(?:ID|Number|#))"
    r"\s*[:#-]?\s*([A-Z0-9][A-Z0-9-]{3,15})\b",
    re.IGNORECASE,
)

# Health insurance member / group / policy IDs.
_INSURANCE_ID_PATTERN = re.compile(
    r"\b(?:Member\s+(?:ID|Number|#)|Subscriber\s+(?:ID|#)|Group\s+(?:ID|Number|#)|Policy\s+(?:ID|Number|#)|RxBIN|RxPCN|RxGroup)"
    r"\s*[:#-]?\s*([A-Z0-9][A-Z0-9-]{4,20})\b",
    re.IGNORECASE,
)

# Common health-record keyword cues — used as a low-confidence signal that
# something *medical* is in the document even when no formatted ID is.
_MEDICAL_KEYWORD_PATTERN = re.compile(
    r"\b(?:diagnosis|diagnosed\s+with|prescription|prescribed|patient\s+name|date\s+of\s+birth\s+\(DOB\)"
    r"|visit\s+summary|lab\s+results?|ICD-?10|CPT\s+code)\b",
    re.IGNORECASE,
)

# ---------- Credentials ----------
# ``key: value`` style credential leaks. The labels are case-insensitive
# but the value must be at least 6 non-space chars to avoid catching
# placeholders like "password: ***".
_CREDENTIAL_KV_PATTERN = re.compile(
    r"\b(password|passphrase|api[_-]?key|access\s+token|bearer\s+token|secret(?:\s+key)?|client\s+secret|private[_-]?key)"
    r"\s*[:=]\s*([^\s'\"`<>]{6,})",
    re.IGNORECASE,
)

# 12- or 24-word BIP-39 mnemonic phrase. Very high signal, very low FPR
# because the surrounding context (noun-only, all lowercase, exactly the
# right count) is so unusual.
_MNEMONIC_PHRASE_PATTERN = re.compile(
    r"(?<![A-Za-z])"
    r"(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}"
    r"(?![A-Za-z])"
)

# Recovery codes — often presented as "Recovery code: XXXX-XXXX" or
# "Backup code: 12345 67890".
_RECOVERY_CODE_PATTERN = re.compile(
    r"\b(?:recovery|backup|one[_-]?time|2fa|two[_-]?factor)\s+code"
    r"\s*[:#-]?\s*([A-Z0-9]{4,}(?:[-\s][A-Z0-9]{4,}){0,5})\b",
    re.IGNORECASE,
)

# ---------- Legal ----------
# Document keywords that suggest a contract, lease, or other binding
# instrument. Low specificity — categorizer assigns the lowest risk
# weight.
_LEGAL_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"Tenant|Landlord|Lessor|Lessee|Sublessee|Sublessor"
    r"|Effective\s+Date"
    r"|Party\s+of\s+the\s+(?:first|second)\s+part"
    r"|Power\s+of\s+Attorney"
    r"|Last\s+Will\s+(?:and\s+Testament)?"
    r"|Notarized?"
    r"|Witness(?:eth|ing)?"
    r"|Hereinafter\s+referred\s+to"
    r")\b",
    re.IGNORECASE,
)


_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_TAX_FORM_PATTERN, "tax_form"),
    (_MEDICAL_RECORD_PATTERN, "medical_record_number"),
    (_INSURANCE_ID_PATTERN, "insurance_id"),
    (_MEDICAL_KEYWORD_PATTERN, "medical_keyword"),
    (_CREDENTIAL_KV_PATTERN, "credential_kv"),
    (_MNEMONIC_PHRASE_PATTERN, "mnemonic_phrase"),
    (_RECOVERY_CODE_PATTERN, "recovery_code"),
    (_LEGAL_KEYWORD_PATTERN, "legal_keyword"),
)


# Regex matches don't have a meaningful confidence score — we use a fixed
# value per pattern based on how rarely each yields false positives in
# practice. (Mnemonic phrases are very specific; legal keywords are not.)
_CONFIDENCE: dict[str, float] = {
    "tax_form": 0.85,
    "medical_record_number": 0.85,
    "insurance_id": 0.75,
    "medical_keyword": 0.55,
    "credential_kv": 0.85,
    "mnemonic_phrase": 0.95,
    "recovery_code": 0.85,
    "legal_keyword": 0.55,
}


def detect(text: str) -> list[Finding]:
    """Run all custom regex patterns and return findings."""
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
