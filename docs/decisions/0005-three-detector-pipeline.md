# 0005 — Three detectors (Presidio + Privacy Filter + custom regex)

**Status:** Accepted
**Date:** 2026-05-01 (codified in v1 build-order step 5)

## Context

PII detection has many viable shapes. The relevant axis for v1 is
**what kind of signal each detector is good at**:

1. **Pattern-based detectors with checksums.** Best for things like
   credit card numbers (Luhn), IBAN (mod-97), US SSNs (area
   ranges). Low false-positive rate; high precision.
2. **Contextual / statistical detectors.** Best for things like
   names, addresses, emails — where the *string* could be anything
   but the *context* tells you it's PII.
3. **Domain-specific lexicons.** Best for tax-form titles, medical
   record-number prefixes, credential keywords, mnemonic-phrase
   shapes. These don't generalize; they need explicit patterns.

No single tool covers all three well. Presidio is great at (1) but
ships with overly-aggressive NER for (2) and has no built-in domain
patterns for (3). A pure transformer-based PII detector covers (2)
but misses the validated patterns in (1) and the domain patterns in
(3). Hand-written regex covers (3) but is bad at (1) and useless for
(2).

## Decision

Run all three detectors per attachment, in parallel-by-spirit (each
emits `Finding`s; the categorizer merges):

- **Presidio** (`presidio-analyzer`) with an explicit nine-entity
  allowlist: `CREDIT_CARD, IBAN_CODE, US_SSN, US_PASSPORT,
  US_DRIVER_LICENSE, US_BANK_NUMBER, US_ITIN, EMAIL_ADDRESS,
  PHONE_NUMBER`. spaCy `en_core_web_sm` for NLP — small model, since
  we're not using its NER.
- **Privacy Filter** (`openai/privacy-filter` via Transformers token
  classification): contextual labels for `account_number,
  private_address, private_email, private_person, private_phone,
  private_url, private_date, secret`.
- **Custom regex** for one US-specific pattern the two models can't
  replicate: tax-form titles (`W-2`, `1099-*`, `1040-*`,
  `Schedule [A-K]`, `Form NNNN`, `K-1`). An earlier set of seven other
  patterns (medical record numbers, insurance IDs, medical keyword
  cues, credential `key=value`, recovery codes, legal-document
  keywords, BIP-39 mnemonic phrases) was dropped — see the trailing
  "Revisions" section below.

A single `categorizer` maps every `(detector, subtype)` to one of
seven user-facing categories. A coverage test ensures the map is
exhaustive.

## Consequences

**Good:**

- Recall on the dev corpus is excellent. Every clearly-PII span in
  every tested document was caught by at least one detector. The
  shipping-label PDF — the most PII-dense piece in the corpus — got
  both addresses, both names, the tracking number, and the order ID.
- Each detector's false-positive style is bounded. Presidio's
  checksums catch real cards, not just any 16-digit string. The
  regex `mnemonic_phrase` requires 12 or 24 lowercase words in a row
  — very low FPR. Privacy Filter's `private_person` is sometimes
  over-eager but stays in the `other_pii` category and doesn't trip
  flags on its own.
- The detectors are isolated: failing or upgrading one doesn't break
  the others. `detection/runner.py` is a tiny orchestrator
  (~30 lines).
- Operational cost is modest. Presidio is fast (~50 ms/doc). Privacy
  Filter is the bottleneck at ~2 s/doc on CPU. Custom regex is
  microseconds. Total ~80 s for the 37-attachment dev corpus.

**Costs:**

- Three model + library footprints to maintain. Presidio brings in
  spaCy; Privacy Filter brings in Transformers + ~2.6 GB of weights;
  the regex module is free.
- Three confidence-score conventions to reconcile. Presidio uses 0..1
  with checksum-bonus thresholds; Privacy Filter uses 0..1 from
  softmax; our regex assigns fixed confidences per pattern based on
  observed FPR. The categorizer doesn't try to normalise them — it
  just stores each one and lets the UI surface them.
- Span-boundary mismatches between detectors. Presidio's
  `EMAIL_ADDRESS` and Privacy Filter's `private_email` both fire on
  the same address with slightly different boundaries. We keep both,
  the verdict counts both, and the UI shows both — the user can see
  the corroboration.

## Encoded in

- `inboxaudit/detection/presidio_detector.py`
- `inboxaudit/detection/privacy_filter_detector.py`
- `inboxaudit/detection/custom_regex.py`
- `inboxaudit/detection/categorizer.py` — the single source of
  truth for `(detector, subtype) → user_category`.
- `inboxaudit/detection/runner.py` — orchestrator.
- `tests/test_categorizer.py::test_every_mapped_category_is_known` —
  the coverage test that forces new subtypes to land a category +
  weight in the same commit.

## Alternatives considered

- **Privacy Filter alone.** Missed the structured-pattern stuff
  (credit cards, SSN, IBAN checksums). The model is genuinely good at
  contextual entities but doesn't validate.
- **Presidio alone.** Missed addresses and names that Privacy Filter
  catches in context. Custom-regex coverage of `gov_id` would have
  been required, ballooning the regex set.
- **A larger LLM with tool use to "judge" each document.** Out of
  scope for v1 (local, no API calls). v2 backlog item:
  *Local LLM enrichment layer (document-type classification, risk
  explanations, smart grouping)*.

## Revision: 2026-05-17 — custom regex pared from 8 patterns to 2

The original v1 cut shipped eight custom patterns. After empirical
review (1 of 130 detections on the dev corpus came from custom regex
— the `tax_form` hit on the user's HSA Withdrawal Form), six of the
eight were dropped:

| Removed subtype | Reason |
|---|---|
| `credential_kv` | Duplicates Privacy Filter's `secret` label at lower precision |
| `recovery_code` | Duplicates Privacy Filter's `secret` label |
| `medical_record_number` | Never fired on the dev corpus; would mis-categorise some `account_number`-tagged spans as `medical` but otherwise low signal |
| `insurance_id` | Same as MRN |
| `medical_keyword` | Bare keyword spotter (`diagnosis`, `prescription`, etc.) — documents containing these words aren't necessarily PHI |
| `legal_keyword` | Bare keyword spotter (`Tenant`, `Lessor`, etc.) — same problem |

Survivors at the time: `tax_form` (document-type signal, catches
blank/template forms) and `mnemonic_phrase` (BIP-39 wordlist).

**Consequence:** the `medical` and `legal` user categories no longer
had any v1 feeders after this pare-down. Originally kept in
`RISK_WEIGHTS` and `FLAGGABLE_CATEGORIES` as placeholders; later
removed entirely as part of a broader v1 simplification (no orphan
concepts in the codebase). Re-add them to `RISK_WEIGHTS` +
`FLAGGABLE_CATEGORIES` + the categorizer's registry if a future
detector ships a feeder for either.

## Revision: 2026-05-17 (later that day) — `mnemonic_phrase` dropped

Empirical follow-up testing showed `mnemonic_phrase` produced no real
signal on broader corpora: crypto seed phrases simply don't appear
in email attachments in practice (users store them in password
managers, hardware wallets, or on paper, not in inboxes). The
theoretical "catastrophic loss class" justification didn't survive
contact with real data.

Removed. Custom regex now ships one pattern (`tax_form`). The
`credentials` user category still has a feeder via Privacy Filter's
`secret` label, so removing this row didn't orphan any category.

## Revision: 2026-05-17 (later still) — custom_regex retired entirely; profile tiers collapsed to two

Empirical follow-up on `tax_form`: the only catch on the dev corpus
was a single self-sent HSA Withdrawal Form. Useful, but not enough
signal to justify maintaining a third detector subsystem (orchestrator
plumbing, tests, registry entries, docs). Dropped.

With `tax_form` gone, the `standard` profile tier had only one
entity gating it separately from `all`: Privacy Filter's
`account_number`. The flagged-set behaviour of `standard` and `all`
was already identical on real data — both flag via `account_number`;
the only difference was whether the informational ``private_*``
entries got recorded. That distinction wasn't worth a separate
profile.

Outcome:

- Custom regex detector deleted (module + tests + registry rows).
  Title of this ADR is preserved for history but the system is now a
  **two-detector pipeline** (Presidio + Privacy Filter).
- `Profile.STANDARD` removed; `account_number` moved to tier `all`
  (still flags via `category=financial`).
- `tax` category remains in `RISK_WEIGHTS` + `FLAGGABLE_CATEGORIES`
  with no v1 feeder (matches the existing pattern for `medical` and
  `legal`). Future custom detector can repopulate.
