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
python -m pii_eval render --seed 42              # text/s42 -> image/s42 (paired image corpus)
python -m pii_eval score --modality image        # image pipeline + re-OCR value survival
python -m pii_eval score --modality pdf -c pii_eval/corpora/real/1   # full strip_pdf on real source PDFs
python -m pii_eval ocr-report                    # OCR-fidelity sweep: font x glyph size (resumable)
python -m pii_eval ocr-report --summary-only     # re-print matrices from the existing report
python -m pii_eval ocr-report --ocr-backend paddle:v5_server   # same sweep, another engine
```

`score` exits 1 if any critical-type entity (TFN, Medicare, BSB, account,
card, person name) leaked — the roadmap's zero-critical-miss gate.

## Corpus layout

Every corpus lives under `pii_eval/corpora/` (gitignored): one folder
per modality — `text/` and `image/` (regenerable, one subfolder per
seed: `text/s42`, `image/s42`, ...) and `real/` (imported real
documents, one subfolder per set: `real/1`, ... — sources sensitive,
truth hand-authored, see `realdocs.py`).
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
- `names_*.csv` — name-form statistics doc (2026-07-15,
  `pii_eval/nameforms.py`): 32 curated distinct names (Anglo, particle
  surnames, multi-word non-Anglo), each drawn once per surface form —
  canonical, reversed caps, comma form — so per-form n is fixed by
  construction (~74 rows) instead of a handful of random pool draws.
  Every person appears both canonically and reversed in the same doc,
  reproducing the same-window interference condition the cell-isolation
  windows fix (pii/DONE.md).

Transaction descriptions (shared by the legacy statements and the CSVs)
also carry two per-form probes added 2026-07-15: `ADDRESS_BARE` street
lines with no suburb/state context ("RENT 53 MILES ST", the documented
GLiNER2 recall-miss class) and suburb-suffixed merchants ("EFTPOS
WOOLWORTHS NEWTOWN 4821 AU") ground-truthed whole as keep-ORGANIZATION —
GLiNER2 stripping the embedded suburb (the 2026-07-14 image-demo wart)
registers as over-stripped. Trust names ("OAKFIELD FAMILY TRUST") appear
as statement account holders and loan trustee lines; they are business
entities, so keep-ORGANIZATION despite the surname stem. The loan notes
carry bare-town mentions: `LOCATION` (real towns, standalone — no address
context) and `LOCATION_SHORT` (real 3-letter suburbs: Kew, Ayr, Hay — the
class the `LOCATION_MIN_CHARS=4` floor knowingly sacrifices, expected to
leak until the gazetteer task lands). Loan applicant 1 additionally gets a
PO Box postal address (`ADDRESS`).
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
- Documented-hard person surface forms carry distinct truth types — the
  CONTEXTUAL_ID precedent: distinct rows for known-hard forms.
  `PERSON_JOINT` ("E & J Moore") is a CRITICAL gate member since
  2026-07-15, when the layer-1 joint-name recognizer took ownership of
  the mechanical joint forms (100% on seeds 42/123). `PERSON_REVERSED`
  ("MOORE OLGA") still reports per-form without gating — 70/72 across
  seeds after the cell-isolation window fix, residual is GLiNER2 label
  competition (see the residual task in pii/TODO.md). `PERSON_COMMA`,
  `PERSON_PARTICLE` and `PERSON_MULTIWORD` (the names doc's other forms)
  follow the same convention, as do `ADDRESS_BARE`, `LOCATION` and
  `LOCATION_SHORT`; none of them are gate members.
- The joint-name recognizer's precision trade-offs are measured per-form
  (2026-07-15): `ORGANIZATION_AND` — 'X and Y Z' orgs with a corporate
  marker, must stay kept; `ORGANIZATION_AND_BARE` — orgs in the joint-name
  shape with no marker anywhere ("P & O CRUISES"), the documented
  recall-first sacrifice, expected to report over-stripped. Colliding
  surnames (Fee, Card — also statement vocabulary) are drawn as ordinary
  critical `PERSON` joint forms, so a guard regression trips the gate.

## Image tier (iteration 1, 2026-07-16)

`render` prints an existing text corpus onto page images
(`pii_eval/render.py`, Pillow + Windows system TTFs) — a **paired
corpus**: same content, same `truth.json`, two modalities, so any score
delta is attributable to OCR errors or to structure the text path
exploits (CSV cell isolation, `RECORD_SEPARATOR` windows). Fonts are
drawn per doc from a seeded RNG (recorded in `manifest.json`);
fixed-column docs (legacy statements, CSVs rendered as aligned tables)
stay monospace — their layout is the whitespace — while loan docs mix in
proportional fonts.

`score --modality image` runs each page through the real image pipeline
(OCR → detect → paint), **re-OCRs the painted output**, and scores every
truth entity by value survival in the redacted image. Matching is
OCR-tolerant and recall-first: confusion-squashed containment (0/O, 1/l,
5/S...) and, for long values, a banded edit-distance scan — a value
surviving with one misread glyph counts as leaked (the `~ocr` column
counts fuzzy-only leaks); values squashing under 4 chars match exactly
only. Same critical gate as the text tier. Known delta classes (first
run's leaks root-caused in the pii/core/DONE.md record): OCR breaking
the shape/context that pattern recognizers key on (collapsed spacing,
misread labels, label/value columns segmented into distant blocks),
digit misreads breaking checksums (expected under degradation), and
reversed-name interference returning where cell isolation is lost.

Known limitation (accepted for iteration 1): whole-value survival has no
`partial` axis — a value with any word painted out no longer matches, so
a partially painted multi-word value scores `stripped` even if a fragment
stays readable (the text tier's `partial` counts as a leak). Token-level
survival would need occurrence disambiguation first: personas share
surname stems with kept business names ("DECKER SERVICES PTY LTD"), so a
naive token match would report false partials against kept text.

`score --modality pdf -c corpora/real/<set>` (2026-07-18) is the same
re-OCR value-survival scoring over the **full PDF pipeline**: each real
corpus source PDF goes through `pii.core.pdf_mode.strip_pdf`, the
stripped output PDF's pages are rendered and re-OCR'd, and the
hand-authored truth is matched with the image tier's matcher. Real-truth
specifics: criticality derives from `build.CRITICAL` by type (authored
truth carries no flags); valueless entities (barcodes) are skipped until
barcode masking exists; one fresh `PseudonymMap` per document (the CLI's
per-document default); stripped PDFs stay under `<corpus>/stripped/` for
eyeballing. Expect the keep-side table to report institutional
identities (bank names/ABNs/1300 numbers) as over-stripped until the
keep-list mechanism lands — that is the axis working, not a truth bug.
Summary tables (shared with the image tier) split strip/keep rows by
each entity's `strip_expected`, so a type may appear in both tables —
real corpora have ORGANIZATION/PHONE_NUMBER on both sides.

## OCR-fidelity sweep (`ocr-report`, 2026-07-16)

Measures OCR fidelity directly (not PII leaks): renders every corpus doc
at each font x em-size grid cell, OCRs it through the `pii.core.ocr`
seam, aligns the output against the exact drawn text, and buckets each
divergence — substitution classes (digit/letter/case), word merges and
splits, lost/spurious lines — plus a measured glyph confusion matrix and
per-word conf-vs-correctness data. The analysis axis is the *measured
x-height in px* per cell (equal em sizes land on very different x-heights
across faces). Findings are engine-scoped; the harness is engine-neutral —
`--ocr-backend` selects the engine through the `pii.core.ocr.get_ocr`
seam (`paddle` default = `paddle:v6_medium`; `paddle:v5_server` is the
other tier — Tesseract was retired 2026-07-17 after round 1), and each
backend writes its own report file. Fixed-column docs sweep the 3 mono fonts only (font comparisons
are valid within a doc class). Output:
`pii_eval/reports/ocr_fidelity[_<backend>].jsonl` (gitignored, appended
per-cell — an interrupted sweep resumes). Findings records in
`pii/core/DONE.md`.

## Not here yet

Realistic-layout rendering: reportlab statement templates mimicking the
reference layouts (mail barcodes included), pdftoppm rendering, the
degradation pipeline (DPI/skew/blur/JPEG — composes on top of `render`'s
clean pages), bbox ground truth. See the image-tier task in
[pii/core/TODO.md](../pii/core/TODO.md).
