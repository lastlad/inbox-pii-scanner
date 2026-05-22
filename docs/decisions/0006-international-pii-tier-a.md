# 0006 — International Tier A national IDs in Presidio

**Status:** Accepted
**Date:** 2026-05-17

## Context

Through v1 the project was scoped US-only. The original plan listed
"No multi-country ID patterns — US only" as a non-goal, and the
Presidio allowlist held exactly nine entities (CREDIT_CARD, IBAN_CODE,
plus seven `US_*` entities).

That scoping was a project-discipline choice, not a technical limit.
The user base for a personal Gmail PII scanner is global, and
Presidio 2.2 already ships strong recognizers for several national
IDs as part of `predefined_recognizers` — they're just not loaded into
the default English registry. With one user explicitly asking for
coverage of "other major countries," it became the right time to
expand.

## Decision

Add an **international Tier A** set of eleven national IDs to the
Presidio entity allowlist. Criteria for inclusion:

1. Recognizer ships in stock Presidio (no custom regex).
2. Pattern includes a checksum (Mod-11, Verhoeff, weighted-mod-10,
   character-substitution) **or** a strict structural format that
   doesn't collide with common 8–16 digit strings.
3. The country has a significant English-speaking diaspora *or* its
   government issues bilingual documents that frequently appear in
   English-language email.

Selected entities:

| Entity | Country | Validation |
|---|---|---|
| `UK_NHS` | United Kingdom | Mod-11 checksum |
| `UK_NINO` | United Kingdom | Strict prefix rules (invalid prefix list) |
| `ES_NIF` | Spain | Letter-checksum (mod-23 → letter) |
| `IT_FISCAL_CODE` | Italy | 16-char deterministic structure + control char |
| `AU_TFN` | Australia | Weighted-mod-11 checksum |
| `AU_MEDICARE` | Australia | Luhn-style checksum |
| `SG_NRIC_FIN` | Singapore | Character-substitution checksum |
| `IN_AADHAAR` | India | Verhoeff checksum |
| `IN_PAN` | India | Strict 10-char format (entity-type letter enumerated) |
| `PL_PESEL` | Poland | Weighted-mod-10 checksum — **dropped, see revision below** |
| `FI_PERSONAL_IDENTITY_CODE` | Finland | Character-substitution checksum |

All eleven mapped to the `gov_id` user category except `AU_TFN` and
`IN_PAN`, which are tax IDs and follow the `US_ITIN` precedent under
`tax`. All were tier `critical` — the checksum precision was deemed
high enough to warrant including them in the default profile.

### Language registration

Four recognizer classes default to a non-English `supported_language`
in stock Presidio: `EsNifRecognizer` ("es"), `ItFiscalCodeRecognizer`
("it"), `PlPeselRecognizer` ("pl"), `FiPersonalIdentityCodeRecognizer`
("fi"). The remaining seven default to "en".

Our `AnalyzerEngine` runs with `language="en"` for every analysis call
(we use a single English spaCy model). To make the four non-English
recognizers participate, we register them with
`supported_language="en"` overridden at construction. This is safe
because the recognizers are pattern-based — the regex matches Latin
characters and digits, and the checksum is arithmetic. Nothing in the
recognizer body actually consults the language code.

### Confirming a sample fires

Each Tier A recognizer was validated against the analyser's 0.5 score
threshold with a context-token-prepended sample (`NHS`, `NINO`, `TFN`,
`PAN`, etc.). `IN_PAN` scores 0.45 with the bare format token "PAN"
and 0.85 with "PAN card no." — the recognizer's context boost is
sensitive to token phrasing. The integration tests
(`tests/test_presidio_international.py`) use samples that consistently
clear the threshold.

## Consequences

**Good:**

- Coverage extends to countries representing a large share of likely
  users without adding any new model, library, or HTTP service.
- All eleven recognizers carry checksums or strict formats, so the
  false-positive rate on English email is low — confirmed by the
  negative-test set, which throws shape-similar bad-checksum strings
  at each recognizer and verifies they're rejected.
- One source of truth: the `_REGISTRY` in
  `inboxaudit/detection/categorizer.py` gained eleven rows; no
  other code needed to change for the categorizer or profile filter
  to work.

**Costs:**

- Four classes need an explicit `supported_language="en"` override.
  Not invasive (one extra tuple in `presidio_detector.py`), but a
  reader unfamiliar with Presidio's language-routing might find this
  surprising.
- The `--profile critical` default now records more findings on
  inboxes containing non-US documents. This is the intended outcome,
  but is a behaviour change for existing dev-corpus scans.

## Encoded in

- `inboxaudit/detection/presidio_detector.py` — `PRESIDIO_ENTITIES`
  allowlist + registration logic in `_get_engine()`.
- `inboxaudit/detection/categorizer.py` — eleven `_REGISTRY` rows.
- `tests/test_presidio_international.py` — positive and negative
  integration tests + a wiring check.

## Alternatives considered

- **Tier B: include format-only recognizers** (`IT_DRIVER_LICENSE`,
  `IT_PASSPORT`, `IT_IDENTITY_CARD`, `IT_VAT_CODE`, `ES_NIE`,
  `IN_VEHICLE_REGISTRATION`, `IN_VOTER`, `IN_PASSPORT`). Rejected for
  v1 because they're regex-only without checksums and over-fire on
  generic numeric or alphanumeric strings. Easy to revisit later if
  user reports demand it.
- **Per-country opt-in flag.** Considered a CLI flag to enable a
  country-set at scan time. Decided against — the Tier A set is small
  enough that "on by default" is fine, and the criticality-tier
  filter (`--profile`) already gives the user the off-switch they
  need if false positives become a problem.
- **Ship business-ID recognizers (`AU_ABN`, `SG_UEN`).** Rejected:
  not personal PII, would only flag if a user emails themselves
  corporate filings, and the noise floor is higher.

## Future revision points

- If we ship Korean, Thai, Nigerian, or German support, those
  recognizers (`Kr*`, `ThTnin`, `NgNin`, others) follow the same
  pattern: add the class import, the language override (if needed),
  the entity name, and the categorizer row.
- If a Tier A recognizer turns out to over-fire on real corpora, the
  fix is to move it from tier `critical` to tier `all` in
  `_REGISTRY` — no code changes required.

## Revision: 2026-05-18 — `PL_PESEL` dropped

The Mod-10 weighted checksum gates ~1 in 10 random 11-digit numbers,
which sounded acceptable in isolation but didn't survive contact with
real inbox data. Personal Gmail accounts contain large numbers of
11-digit values that pass the check by coincidence: shipping tracking
numbers (USPS/UPS/FedEx are 12-20 digits but truncated forms appear),
e-commerce order IDs, bank transaction references, Amazon order
numbers, etc. Each generates a false positive in the `gov_id`
category, which is the worst possible bucket for FPs since `gov_id` is
the highest-weighted flaggable category in `RISK_WEIGHTS`.

The other Tier A recognizers don't share this failure mode because
they're either longer (Aadhaar's 12 digits + Verhoeff, IT_FISCAL_CODE's
16 alphanumeric with positional rules) or have non-numeric structure
that excludes random digit runs (IN_PAN's strict 5-letter prefix +
4-digit + 1-letter, NIF's terminating check letter). Only PESEL is
"11 digits + simple weighted checksum" — the lowest signal-to-noise
shape in the set.

Outcome:

- Removed from `PRESIDIO_ENTITIES`, the categorizer registry, the
  pipeline's English-override registration, and the test file.
- Other ten Tier A entries retained — their FP rates on the dev
  corpus didn't reproduce the same pattern.
- Future re-add path: if a user with a Polish corpus needs PESEL
  coverage, the right place to put it back is tier `all` (not
  `critical`) so it's recorded but doesn't drive the per-message flag
  / risk score. That requires no code change beyond restoring the
  registry row.
