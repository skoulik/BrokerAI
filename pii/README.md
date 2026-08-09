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
python -m spacy download en_core_web_sm
```

The NER model (GLiNER2-PII, ~1.2 GB) downloads into `models/hf-cache/` on
first use.

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

`--detector` chooses what finds the PII:

- `vlm` (**default** for `--image`/`--pdf`) — a local vision LLM reads the
  page image and names the values; the pattern/checksum recognizers then
  refine each one into its precise class, restore the checksum-invalid
  shadows, and add anything the model missed. **Needs a running
  llama-server** — set `--vlm-url` or `$PII_VLM_URL` (default
  `http://localhost:8080`) — and costs minutes per page.
- `layers` — the pattern/checksum recognizers and the NER model over the OCR
  text only. No model server, seconds per page. Text and CSV input always
  uses this, since there is no page image to read.

`--geometry` (only with `--detector vlm`) chooses how detected values are
placed on the page.

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
`models/paddlex` on first use. With the GPU paddle wheel the engine and torch
cannot share a Windows process, so the pipeline drives OCR through a
persistent worker subprocess (`pii/core/ocr_worker.py`); the CPU wheel runs it
in-process.

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
  nearby context words (Presidio's lemma enhancer); `all` takes every
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
on the validating recognizer's name so a GLiNER2 phone/card guess never
suppresses); when a collected span overlaps a valid detection in masking,
the extents union and the valid class wins the placeholder (recall-first).
Tier-1 eval (2026-07-14): `likely` and `context` run zero-noise; `all`
logged 44 noise findings over 11 docs.

## Detection layers

1. **Presidio patterns/checksums** — built-in `AU_TFN`, `AU_MEDICARE`,
   `AU_ABN`, `AU_ACN` (checksum-validated, explicit registration needed —
   they are not in Presidio's default registry), credit cards, emails,
   AU-region phones; custom recognizers in `pii/core/recognizers.py` for BSB
   (`AU_BSB`), account numbers (`AU_BANK_ACCOUNT`), PayID (`AU_PAYID`), and
   joint-account name forms ("E & J Moore", "JULIE AND BRIAN SUMMERS" →
   `PERSON`) — mechanical shapes GLiNER2 loses inside transaction-line junk.
2. **Zero-shot NER** — names, addresses and DOB, distinguishing person vs
   organization for bank transaction descriptions. GLiNER2
   (`pii/core/gliner2_recognizer.py`, Fastino's PII-tuned model). Bare place
   names are not detected — a lone city/town name is acceptable verbatim in
   financial documents; the address passes still own address-shaped lines.
   spaCy (`en_core_web_sm`) is Presidio's NLP engine only, not a detector.
3. **Local-LLM audit pass** — planned; will use llama-server.

Behaviour worth knowing when running the tool: `DATE_TIME` and
`ORGANIZATION` are detected but **kept** by default (transaction dates and
merchant names are the analytical substance of a statement; `DATE_OF_BIRTH`
is stripped); some over-stripping is the accepted recall-first cost — every
ambiguity resolves toward stripping. The design rationale behind all of this
lives in [core/ARCHITECTURE.md](core/ARCHITECTURE.md).

## Performance

The NER model moves itself to CUDA when available. On the 9-document eval
corpus the NER share of the run is ~0.7 s (GLiNER2 on the RTX 2080 Ti).
GLiNER2 always loads; spaCy loads too, as Presidio's NLP engine (still
required — keep the `en_core_web_sm` download above).

## Evaluation

Scored by the Tier-1 synthetic corpus in [pii_eval](../pii_eval/README.md)
(`python -m pii_eval generate` / `score`). Current state: all pattern
entities 100%; PERSON 100%; ADDRESS ~83% (the residual leaks are bare
ALL-CAPS street lines with no state/postcode context). Contextual
identifiers ("a dentist in Wagga Wagga") are undetectable by layers 1–2 by
nature — a target for the planned layer-3 LLM audit. Keep presidio ≥
2.2.364: 2.2.362's ACN validator rejects every ACN with check digit 0, and
2.2.364 changed the ABN validator's leading-zero handling, which the tool's
own checksum copy mirrors.
