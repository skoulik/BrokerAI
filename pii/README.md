# pii — local PII stripping tool

Phase 1 of the BrokerAI revival ([../ROADMAP.md](../ROADMAP.md)). Strips
personally identifiable information from documents locally so the stripped
version can be shared with cloud models. Uses **pseudonymization with a
consistent mapping** (`John Smith → PERSON_1` everywhere), not blank
redaction, so cloud answers can be rehydrated and analytical utility is
preserved.

Standalone from the RAG app — nothing here imports `rag_tools` or the web app.

This file covers installation and usage. Architecture and design decisions:
[ARCHITECTURE.md](ARCHITECTURE.md); activity overview: [ROADMAP.md](ROADMAP.md);
open tasks: [TODO.md](TODO.md); completed-task records: [DONE.md](DONE.md).

## Install

```
pip install -r pii/requirements.txt
python -m spacy download en_core_web_sm
```

The NER model (GLiNER2-PII, ~1.2 GB) downloads into `models/hf-cache/` on
first use.

## Usage

```
python -m pii strip document.txt -o document.clean.txt --map map.json --report
python -m pii strip scan.png --image -o scan.clean.png --map map.json
python -m pii analyze document.txt            # show detections, change nothing
python -m pii rehydrate cloud_answer.txt --map map.json
```

`strip`/`analyze` accept `-` for stdin. The mapping file accumulates across
runs so placeholders stay consistent over a document set. **It contains the
original PII — it is gitignored and must never leave the machine.**

Flags: `--strip-orgs` (organization names are kept by default — merchant
names carry analytical value), `--threshold` (default 0.4), and the
checksum-invalid identifier controls below.

## Images

`strip --image` OCRs the input (Tesseract, word-level bounding boxes),
runs the full text pipeline on the recognized text, and paints each
detected span's placeholder over its pixels — background-filled boxes
with the placeholder drawn in, so the output image stays pseudonymized
and rehydratable, not blacked out. Detection never sees pixels; painting
happens on the original image (`ocr.py` for the engine adapter and
span→box mapping, `image_mode.py` for the painting).

Requires the Tesseract binary (`winget install UB-Mannheim.TesseractOCR`)
and `pytesseract`. PDFs-as-images and OCR-engine alternatives are on the
roadmap.

## Checksum-invalid identifiers

A value shaped like a TFN whose mod-11 arithmetic fails is a typo, bad OCR,
or forgery — all three worth surfacing rather than silently dropping.
Shadow recognizers (`invalid_recognizers.py`) mirror the checksummed
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
   they are not in Presidio's default registry), credit cards, emails, URLs,
   IPs, AU-region phones; custom recognizers in `recognizers.py` for BSB
   (`AU_BSB`), account numbers (`AU_BANK_ACCOUNT`), PayID (`AU_PAYID`).
2. **Zero-shot NER** — names, addresses, DOB, and bare place names (a
   contextual-identifier LOCATION pass); distinguishes person vs
   organization for bank transaction descriptions. GLiNER2
   (`gliner2_recognizer.py`, Fastino's PII-tuned model, schema
   descriptions). The original GLiNER (v1) backend was evaluated
   side-by-side and removed 2026-07-13 (it's in git history). spaCy
   (`en_core_web_sm`) is Presidio's NLP engine only — its `SpacyRecognizer`
   detector was retired 2026-07-15 (GLiNER2 now owns LOCATION too).
3. **Local-LLM audit pass** — planned; will use llama-server.

Behaviour worth knowing when running the tool: `DATE_TIME` and
`ORGANIZATION` are detected but **kept** by default (transaction dates and
merchant names are the analytical substance of a statement; `DATE_OF_BIRTH`
is stripped); some over-stripping is the accepted recall-first cost — every
ambiguity resolves toward stripping. The design rationale behind all of
this (recall-first span handling, the spaCy detector retirement, GLiNER2
tuning) lives in [ARCHITECTURE.md](ARCHITECTURE.md).

## Performance

The NER model moves itself to CUDA when available (CUDA torch installed
2026-07-12 for the RTX 2080 Ti). On the 9-document eval corpus the NER
share of the run is ~0.7 s (GLiNER2; the removed v1 backend took ~3.3 s,
~15 min on CPU). GLiNER2 always loads now that the patterns-only regime is
retired (2026-07-15); spaCy loads too, as Presidio's NLP engine (still
required — keep the `en_core_web_sm` download above).

## Evaluation

Scored by the Tier-1 synthetic corpus in [pii_eval](../pii_eval/README.md)
(`python -m pii_eval generate` / `score`). Current state:
all pattern entities 100%; PERSON 100%; ADDRESS 83% (the max_width=12
lift closed the one-line fragmentation; the 2 remaining leaks are bare
ALL-CAPS street lines with no state/postcode context, width-independent). Contextual identifiers ("a dentist in
Wagga Wagga") are undetectable by layers 1–2 by nature — a target for the
planned layer-3 LLM audit. Keep presidio ≥ 2.2.363: 2.2.362's ACN
validator rejects every ACN with check digit 0.
