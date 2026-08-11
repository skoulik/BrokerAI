# pii — local PII stripping tool

Phase 1 of the BrokerAI revival ([../ROADMAP.md](../ROADMAP.md)). Strips
personally identifiable information from documents locally so the stripped
version can be shared with cloud models. Uses **pseudonymization with a
consistent mapping** (`John Smith → PERSON_1` everywhere), not blank
redaction, so cloud answers can be rehydrated and analytical utility is
preserved.

Standalone from the RAG app — nothing here imports `rag_tools` or the web app.

The tool is organised into three components under `pii/`: `pii.core` (the engine),
`pii.cli` (this command-line front-end), and `pii.gui` (a planned GUI). This file
covers installation and CLI usage. Component map and dependency rules:
[ARCHITECTURE.md](ARCHITECTURE.md); status: [ROADMAP.md](ROADMAP.md). The engine's
design and open tasks are in [core/ARCHITECTURE.md](core/ARCHITECTURE.md) and
[core/TODO.md](core/TODO.md).

## Install

```
pip install -r pii/requirements.txt
```

**Detection needs a running llama-server.** Layer 0 — a local LLM — is the
detector for every input mode, reached over HTTP: set `--vlm-url` or
`$PII_VLM_URL` (default `http://localhost:8080`). Nothing here starts it, and
there is no offline fallback.

## Usage

```
python -m pii strip document.txt -o document.clean.txt --report
python -m pii strip scan.png --image -o scan.clean.png
python -m pii strip statement.pdf --pdf -o statement.clean.pdf
python -m pii analyze document.txt            # show detections, change nothing
python -m pii rehydrate cloud_answer.txt --map statement.pii_map.json
python -m pii debug ocr statement.pdf --format overlay -o ocr.png  # inspect OCR
```

`strip`/`analyze` accept `-` for stdin. The pseudonym map is
**per-document by default**: `--map` defaults to
`<input>.pii_map.json` next to the input file, so placeholder numbering
restarts with each document. Pass one `--map` path across runs to keep
placeholders consistent over a document set instead; `rehydrate` and
stdin input always need an explicit `--map` (there is no input document
to derive it from). **The map contains the original PII — it is
gitignored and must never leave the machine.**

Flags: `--strip-orgs` (organization names are kept by default — merchant
names carry analytical value), `--threshold` (default 0.4), and the
checksum-invalid identifier controls below.

## Images

`strip --image` detects the PII on the page, locates each detected value in
the OCR text, and paints its placeholder over the matching pixels —
background-filled boxes with the placeholder drawn in, so the output image
stays pseudonymized and rehydratable, not blacked out. Painting always happens
on the original image (`pii/core/image_mode.py`).

A local LLM finds the PII in every input mode — reading the page image for
`--image`/`--pdf` and the document text otherwise. The pattern/checksum
recognizers then refine each value into its precise class, restore the
checksum-invalid shadows, and add anything the model missed. Minutes per page
on `--image`/`--pdf`; text is far cheaper, with no image to ingest.

On text and CSV a detected value is located by finding it in the text, and
**every** occurrence of it is replaced, not only the one the model reported. A
value the model returns that is not in the text cannot be redacted; those are
counted and always reported on stderr, independently of `--report`.

`--geometry` chooses how detected values are placed on the *page*, so it
applies to `--image`/`--pdf` only.

- `hybrid` (**default**) — a second model pass boxes each detected value, and
  those boxes constrain the search for it in the OCR text. Painting still uses
  exact OCR word boxes; the model's own box is painted only where there is no
  OCR text at all, as for a logo or a barcode, padded and reported separately.
- `ocr` — no second pass; each value is searched for across the whole page
  string. The behaviour before boxes were used, kept for comparison.
- `vlm` — paint the model's own boxes, skipping OCR entirely.

**Use the default.** The model's boxes are far too unreliable to *paint* — 16%
clip by more than 20 px, stochastically, leaving part of a value legible — but
reliable enough to say which occurrence of a value you are looking at, which
is all `hybrid` asks of them. `vlm` is a comparison instrument, not a
production option.

Two lines in the run output report the weaker outcomes, and they are printed
whether or not you passed `--report`: values painted from the model's own box
(approximate geometry, no checksum validation) and values that could not be
placed at all (**not redacted** — treat as a leak and re-run or handle by
hand).

The OCR engine is **PaddleOCR**, and it supplies *geometry*, not detection.
`--ocr-backend` selects the model tier: `paddle` (default, = `paddle:v6_medium`),
`paddle:v6_medium` or `paddle:v5_server`. Models auto-download to
`models/paddlex` on first use. OCR runs in-process on either wheel.

The numbers behind these defaults are in
[core/reports/2026-08-08-vlm-oneshot-qwen36.md](core/reports/2026-08-08-vlm-oneshot-qwen36.md).

## PDFs

`strip --pdf` treats the PDF as images: every page is rendered to pixels
(`--dpi`, default 300), run through the image path above, and embedded
into a **fresh, image-only output PDF** at the source page's physical
size. Nothing from the source document's internal structure survives —
no text layer, annotations, attachments or metadata — which eliminates
the hidden-text-layer leak class (financial PDFs have been observed
hiding account numbers under white rectangles) by construction rather
than by scrubbing. Placeholders are consistent across the document's
pages; processing is lossless end-to-end with a JPEG embed only at the
final step (~0.2 MB/page at 300 DPI). Progress is reported per page on
stderr; `--report` prefixes detections with their page number.

## OCR inspection (debug)

`pii debug ocr <image|pdf>` OCRs the page(s) and dumps the **perceived structure** — lines,
words and their boxes — for inspecting what the OCR stage produced (no PII detection, no
painting). This is the geometry the strip path paints with, so a value missing from these
lines can only be redacted from the model's own box. `--format json` (round-trippable), `text` (human
summary), or `overlay` (annotated raster) — the overlay outlines each word in grey and each
assembled line in blue with its index, which makes the row banding visible. `--ocr-backend`
takes the same model tiers as `strip` (`paddle` default).

For PDFs, **all pages** are processed by default (`--page N` selects one; `--dpi` sets the
render resolution). `overlay` output follows the `-o` extension: `-o out.pdf` reconstructs a
fresh image-only PDF with every page annotated (same reassembly as `--pdf` strip — no source
structure survives), `-o out.png` annotates a single page. **The overlay PDF is not redacted** —
it shows the original text with boxes drawn on top, so it (and any json/text dump) is near-PII:
keep it local, like the map file.

## Checksum-invalid identifiers

A value shaped like a TFN whose mod-11 arithmetic fails is a typo, bad OCR,
or forgery — all three worth surfacing rather than silently dropping.
Shadow recognizers (`pii/core/invalid_recognizers.py`) mirror the checksummed
recognizers (TFN, Medicare, ABN, ACN, credit card/Luhn) with the validation
inverted, emitting distinct classes: `*_INVALID` (checksum fails) and
`*_MALFORMED` (structurally impossible, e.g. a Medicare first digit outside
2-6) — the typo-vs-impossible distinction is exactly the forgery signal.

Three orthogonal controls on `strip`:

- `--invalid-identifiers {ignore,likely,context,all}` — which candidates
  are *collected* (default `likely`). Cumulative tiers: `likely` needs
  evidence inside the span (canonical grouping "123 456 782" or an adjacent
  label "TFN: 123456780"); `context` adds bare digit runs promoted by
  nearby context words; `all` takes every
  failing match — noisy, ~90% of random 9-digit runs fail the TFN checksum.
- `--log-invalid-identifiers {yes,no}` (default `yes`) — list the collected
  candidates on stderr with the precise failed rule. **The log is
  near-PII** (a typo'd TFN is a real TFN minus a digit): local-only, like
  the map file.
- `--mask-invalid-identifiers {yes,no}` (default `no`) — also pseudonymize
  them (`TFN_INVALID_1`, `MEDICARE_MALFORMED_1`, ...), so the
  valid/invalid distinction survives into the stripped text. Combining
  with `all` warns: it would eat most reference/receipt numbers.

Guardrails: a candidate covered by a *validated* detection of another class
is not collected (every valid TFN fails the ACN checksum; suppression keys
on the validating recognizer's name, so an unvalidated guess of the same
class never suppresses); when a collected span overlaps a valid detection in masking,
the extents union and the valid class wins the placeholder (recall-first).
Tier-1 eval (2026-07-14): `likely` and `context` run zero-noise; `all`
logged 44 noise findings over 11 docs.

## Detection layers

0. **Local LLM** — reads the page image (`pii/core/vlm.py`) or the document
   text (`pii/core/text_llm.py`) and names the values, in five coarse classes.
   The semantic detector: names, addresses, organizations, dates of birth, and
   anything identifier-shaped. Layer 1 still runs over the same string and is
   merged on top.
1. **Patterns and checksums** (`pii/core/recognizers.py`, run by
   `pii/core/engine.py`) — `AU_TFN`, `AU_MEDICARE`, `AU_ABN`, `AU_ACN` and
   payment cards, each from ONE rule that emits the valid class or its
   `*_INVALID` shadow from a single checksum call; plus BSB (`AU_BSB`),
   account numbers (`AU_BANK_ACCOUNT`), PayID (`AU_PAYID`), the joint-account
   initials form ("E & J Moore" → `PERSON`), `ATF` trustee clauses, email,
   IBAN and AU-region phones. Layer 1 is the deterministic floor under a
   stochastic detector: it types identifiers, validates their checksums, and
   catches what the model missed.
2. ~~**Zero-shot NER**~~ — retired 2026-08-09; layer 0 replaced it. Bare place
   names are still not detected — a lone city/town name is acceptable verbatim
   in financial documents.
3. **Local-LLM audit pass** — planned.

Behaviour worth knowing when running the tool: `DATE_TIME` and
`ORGANIZATION` are detected but **kept** by default (transaction dates and
merchant names are the analytical substance of a statement; `DATE_OF_BIRTH`
is stripped); some over-stripping is the accepted recall-first cost — every
ambiguity resolves toward stripping. The design rationale behind all of this
lives in [core/ARCHITECTURE.md](core/ARCHITECTURE.md).

## Performance

Dominated by the model server. Text runs at roughly 15 s per document;
`--image`/`--pdf` at minutes per page, most of it spent ingesting the page
image. Locally there is nothing heavy left to load — layer 1 is regexes —
and OCR runs only for `--image`/`--pdf`, where it supplies painting geometry.

## Evaluation

Scored by the Tier-1 synthetic corpus in [pii_eval](../pii_eval/README.md)
(`python -m pii_eval generate` / `score`). Current state: all pattern
entities 100%; PERSON, PERSON_REVERSED and PERSON_COMMA 100%. Contextual
identifiers ("a dentist in Wagga Wagga") remain at 0% — a target for the
planned layer-3 audit.
