# pii — local PII stripping tool

Phase 1 of the BrokerAI revival ([ROADMAP.md](../ROADMAP.md)). Strips
personally identifiable information from documents locally so the stripped
version can be shared with cloud models. Uses **pseudonymization with a
consistent mapping** (`John Smith → PERSON_1` everywhere), not blank
redaction, so cloud answers can be rehydrated and analytical utility is
preserved.

Standalone from the RAG app — nothing here imports `rag_tools` or the web app.

## Install

```
pip install -r pii/requirements.txt
python -m spacy download en_core_web_sm
```

The GLiNER NER model (~600 MB) downloads into `models/hf-cache/` on first use.

## Usage

```
python -m pii strip document.txt -o document.clean.txt --map map.json --report
python -m pii analyze document.txt            # show detections, change nothing
python -m pii rehydrate cloud_answer.txt --map map.json
```

`strip`/`analyze` accept `-` for stdin. The mapping file accumulates across
runs so placeholders stay consistent over a document set. **It contains the
original PII — it is gitignored and must never leave the machine.**

Flags: `--no-ner` (patterns only, fast), `--strip-orgs` (organization names
are kept by default — merchant names carry analytical value), `--threshold`
(default 0.4).

## Detection layers

1. **Presidio patterns/checksums** — built-in `AU_TFN`, `AU_MEDICARE`,
   `AU_ABN`, `AU_ACN` (checksum-validated, explicit registration needed —
   they are not in Presidio's default registry), credit cards, emails, URLs,
   IPs, AU-region phones; custom recognizers in `recognizers.py` for BSB
   (`AU_BSB`), account numbers (`AU_BANK_ACCOUNT`), PayID (`AU_PAYID`).
2. **GLiNER zero-shot NER** (`gliner_recognizer.py`) — names, addresses,
   DOB; distinguishes person vs organization for bank transaction
   descriptions.
3. **Local-LLM audit pass** — planned; will use llama-server.

Design notes:

- Recall-first: in `strip()`, spans are filtered to strip-listed entity types
  *before* overlap resolution, so a kept-type span (e.g. spacy's frequent
  bogus high-score `DATE_TIME` hits) can never shadow real PII.
- `DATE_TIME` and `ORGANIZATION` are detected but kept by default:
  transaction dates and merchant names are the analytical substance of a
  statement. `DATE_OF_BIRTH` (via NER) is stripped.
- Placeholders are allocated in document order and matched case-insensitively
  with whitespace collapsed; rehydration restores the first-seen surface form.
- GLiNER quirks found on the synthetic sample: recall collapses on ALL-CAPS
  text, and entities found in a short line are often missed when the same
  line sits inside a full document. The recognizer therefore predicts over
  document windows *and* individual lines, each also in a de-capitalized
  variant, and unions the results. Over-stripping (e.g. a merchant line
  labeled as an address) is the accepted cost until Tier-1 eval tuning.

## Performance

GLiNER moves itself to CUDA when available (CUDA torch installed
2026-07-12 for the RTX 2080 Ti): a 9-document eval corpus scores in ~35 s
vs ~15 min on CPU. `--no-ner` runs in seconds (patterns only — do not rely
on it for names/addresses).

## Evaluation

Scored by the Tier-1 synthetic corpus in [pii_eval](../pii_eval/README.md)
(`python -m pii_eval generate` / `score`). Current state: all pattern
entities 100%; PERSON 98–100% — GLiNER misses rare reversed-caps
("BURNS EDWIN") and initials-joint ("D & D Duncan") forms, and contextual
identifiers ("a dentist in Wagga Wagga") are undetectable by layers 1–2 by
nature; both are targets for the planned layer-3 LLM audit. Keep presidio
≥ 2.2.363: 2.2.362's ACN validator rejects every ACN with check digit 0.
