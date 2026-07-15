# pii_eval — Tier 1 synthetic evaluation corpus

Evaluation harness for the [pii](../pii/README.md) stripping tool —
Tier 1 of the eval plan in [pii/ROADMAP.md](../pii/ROADMAP.md).
Generates Australian financial documents
populated with fake PII, ground truth known by construction, and scores
the pipeline against it. **Everything is synthetic and shareable** —
layouts and phrasing are modeled on the (classified, gitignored) reference
statements, but no value in a generated corpus comes from them.

## Usage

```
python -m pii_eval generate --seed 42 --docs 9   # -> pii_eval/corpora/text/s42
python -m pii_eval score                         # scores corpora/text/s42; full pipeline (GLiNER2 on CUDA)
python -m pii_eval score --seed 7                # another seed's corpus
```

`score` exits 1 if any critical-type entity (TFN, Medicare, BSB, account,
card, person name) leaked — the roadmap's zero-critical-miss gate.

## Corpus layout

Every generated corpus lives under `pii_eval/corpora/` (gitignored,
regenerable): one folder per modality — `text/` today, `image/` when that
tier lands — with one subfolder per seed (`text/s42`, `text/s7`, ...).
Both CLIs default to `corpora/text/s<seed>`; `-o`/`-c` override for
throwaway experiments, but durable corpora belong in the seed folders —
not in session scratchpads (convention set 2026-07-15).

## What gets generated

One persona pool per run (seeded, reproducible), so the same people,
businesses and accounts recur across documents — later used to check
pseudonym-mapping consistency across a document set.

- `legacy_*.txt` — fixed-column monospace bank statement (the plain-text
  legacy format from the reference corpus): ALL-CAPS particulars, joint-name
  forms ("J & E Lawrence", "ROCHA RANDALL"), un-hyphenated BSB/account
  numbers inside descriptions. The NER stress case (ALL-CAPS text collapsed
  the removed GLiNER v1 backend's recall).
- `loan_*.txt` — broker applicant summary: the full PII battery (TFN,
  Medicare, DOB, licence, card, address, contacts) plus a `CONTEXTUAL_ID`
  note ("a dentist in Wagga Wagga") that only the future LLM-audit layer
  can catch — reported as a gap, not gated on.
- `tx_*.csv` — transaction CSV with per-cell ground truth; scored through
  `pii`'s column-aware CSV mode on the Description column.
- `loan_inv_*.txt` / `tx_inv_*.csv` — checksum-invalid injection docs
  (`--no-invalid` omits them; they are appended after the base rotation so
  base docs stay byte-identical per seed). Single-digit typos and a
  structurally impossible Medicare number, annotated as
  `AU_TFN_INVALID`, `AU_MEDICARE_MALFORMED`, etc., each carrying an
  `evidence` field — `in-span` (label/canonical grouping), `context`
  (nearby context word only), `none` (bare digit run) — matching the
  collection tiers of `pii`'s `--invalid-identifiers` feature.

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
- Injected checksum-invalid identifiers are scored on their own axes:
  `logged`/`missed` against the pipeline's invalid findings (broken down
  by evidence tier), `stripped-anyway` (leak risk at mask=no — did another
  layer remove the mangled value?), and the noise floor (findings matching
  no injected entity). `--invalid-identifiers` selects the collection tier
  (default `likely`). 2026-07-14 results on seed 42: `likely` and
  `context` both zero-noise (context also catches its bare-run injection);
  `all` produces 44 noise findings over 11 docs — licences, ATO/policy
  refs — as predicted.
- Two documented-hard person surface forms carry distinct truth types
  (`PERSON_JOINT` "E & J Moore", `PERSON_REVERSED` "MOORE OLGA") so their
  intermittent GLiNER2 misses report per-form without tripping the
  layers-1/2 gate — the CONTEXTUAL_ID precedent; both move into CRITICAL
  when the layer-3 LLM audit lands.

## Not here yet

PDF/image tier: reportlab statement templates mimicking the reference
layouts (mail barcodes included), pdftoppm rendering, degradation pipeline
(DPI/skew/blur/JPEG), bbox ground truth. See the image-tier task in
[pii/TODO.md](../pii/TODO.md).
