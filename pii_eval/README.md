# pii_eval — Tier 1 synthetic evaluation corpus

Evaluation harness for the [pii](../pii/README.md) stripping tool
(ROADMAP Phase 1, Tier 1). Generates Australian financial documents
populated with fake PII, ground truth known by construction, and scores
the pipeline against it. **Everything is synthetic and shareable** —
layouts and phrasing are modeled on the (classified, gitignored) reference
statements, but no value in a generated corpus comes from them.

## Usage

```
python -m pii_eval generate -o pii_eval/corpus --seed 42 --docs 9
python -m pii_eval score --no-ner     # patterns only, seconds
python -m pii_eval score              # full pipeline, ~1 min/doc on CPU
```

`score` exits 1 if any critical-type entity (TFN, Medicare, BSB, account,
card, person name) leaked — the ROADMAP's zero-critical-miss gate.

## What gets generated

One persona pool per run (seeded, reproducible), so the same people,
businesses and accounts recur across documents — later used to check
pseudonym-mapping consistency across a document set.

- `legacy_*.txt` — fixed-column monospace bank statement (the plain-text
  legacy format from the reference corpus): ALL-CAPS particulars, joint-name
  forms ("J & E Lawrence", "ROCHA RANDALL"), un-hyphenated BSB/account
  numbers inside descriptions. The GLiNER worst case.
- `loan_*.txt` — broker applicant summary: the full PII battery (TFN,
  Medicare, DOB, licence, card, address, contacts) plus a `CONTEXTUAL_ID`
  note ("a dentist in Wagga Wagga") that only the future LLM-audit layer
  can catch — reported as a gap, not gated on.
- `tx_*.csv` — transaction CSV with per-cell ground truth; scored through
  `pii`'s column-aware CSV mode on the Description column.

Identifiers are checksum-valid (TFN mod-11, ABN mod-89, ACN, Medicare,
Luhn cards) because Presidio validates check digits — see `au.py`.
Transaction descriptions mix strip-targets (person counterparties, PayIDs,
account refs) with keep-targets (merchant names, ORGANIZATION), which the
scorer tracks as over-stripping.

## Scoring semantics

- Text docs: span coverage of each truth entity by the union of applied
  replacement spans — `stripped` / `partial` / `leaked`. Partial counts as
  a leak for the gate (recall-first).
- CSV docs: value survival in the same output cell.
- Keep-types report the opposite failure: `over-stripped` (analytical
  value destroyed, e.g. a merchant name replaced).

## Not here yet

PDF/image tier: reportlab statement templates mimicking the reference
layouts (mail barcodes included), pdftoppm rendering, degradation pipeline
(DPI/skew/blur/JPEG), bbox ground truth. See ROADMAP Phase 1.
