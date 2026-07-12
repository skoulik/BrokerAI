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

The NER model (GLiNER2-PII, ~1.2 GB) downloads into `models/hf-cache/` on
first use (as does the legacy GLiNER model, ~600 MB, if selected).

## Usage

```
python -m pii strip document.txt -o document.clean.txt --map map.json --report
python -m pii analyze document.txt            # show detections, change nothing
python -m pii rehydrate cloud_answer.txt --map map.json
```

`strip`/`analyze` accept `-` for stdin. The mapping file accumulates across
runs so placeholders stay consistent over a document set. **It contains the
original PII — it is gitignored and must never leave the machine.**

Flags: `--no-ner` (patterns only, fast), `--ner-backend gliner|gliner2`
(default gliner2), `--strip-orgs` (organization names are kept by default —
merchant names carry analytical value), `--threshold` (default 0.4).

## Detection layers

1. **Presidio patterns/checksums** — built-in `AU_TFN`, `AU_MEDICARE`,
   `AU_ABN`, `AU_ACN` (checksum-validated, explicit registration needed —
   they are not in Presidio's default registry), credit cards, emails, URLs,
   IPs, AU-region phones; custom recognizers in `recognizers.py` for BSB
   (`AU_BSB`), account numbers (`AU_BANK_ACCOUNT`), PayID (`AU_PAYID`).
2. **Zero-shot NER** — names, addresses, DOB; distinguishes person vs
   organization for bank transaction descriptions. Default backend is
   GLiNER2 (`gliner2_recognizer.py`, Fastino's PII-tuned model, schema
   descriptions, ~5x faster); the original GLiNER (`gliner_recognizer.py`)
   remains available via `--ner-backend gliner`.
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
- Each NER backend is tuned around its own measured failure modes (details
  in the recognizers' docstrings). GLiNER: recall collapses on ALL-CAPS text
  and on lines embedded in context, so it predicts over windows *and*
  individual lines, each also de-capitalized. GLiNER2 has neither weakness
  but needs windowing for memory (quadratic attention), re-finding of
  repeated mentions (its formatter returns one span per unique entity
  text), separate schema passes for addresses (labels compete inside a
  schema), and honorific span extension. Over-stripping (e.g. a merchant
  line labeled as an address) is the accepted recall-first cost.
- Known GLiNER2 gap: multi-part AU addresses come out fragmented
  (street/suburb spans with `', '`/`' NSW '` connectors uncovered — 4
  partials on Tier-1 vs GLiNER's single full spans). Planned fixes:
  adjacent-span merging in the pipeline (ROADMAP overlaps task) and a LoRA
  adapter trained on synthetic AU addresses.

## Performance

Both NER backends move themselves to CUDA when available (CUDA torch
installed 2026-07-12 for the RTX 2080 Ti). On the 9-document eval corpus
the NER share of the run is ~0.7 s for GLiNER2 vs ~3.3 s for GLiNER
(~15 min on CPU). `--no-ner` runs in seconds (patterns only — do not rely
on it for names/addresses).

## Evaluation

Scored by the Tier-1 synthetic corpus in [pii_eval](../pii_eval/README.md)
(`python -m pii_eval generate` / `score`). Current state (GLiNER2 backend):
all pattern entities 100%; PERSON 100%; ADDRESS is the weak spot (50% —
see the fragmentation note above; GLiNER scores 83%, both leak the same
two odd synthetic street lines). Contextual identifiers ("a dentist in
Wagga Wagga") are undetectable by layers 1–2 by nature — a target for the
planned layer-3 LLM audit. Keep presidio ≥ 2.2.363: 2.2.362's ACN
validator rejects every ACN with check digit 0.
