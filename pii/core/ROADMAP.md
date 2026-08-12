# PII Engine (core) Roadmap

The roadmap for the **core PII engine** — input types, detection layers, and evaluation tiers.
Part of the Phase 1 [BrokerAI revival](../../ROADMAP.md); the component-level overview
(core / cli / gui) is the umbrella [../ROADMAP.md](../ROADMAP.md), and the eval harness is
[`../../pii_eval/`](../../pii_eval/). Engine details live next door:

- **[TODO.md](TODO.md)** — all open engine tasks, grouped, with full working detail
- **[DONE.md](DONE.md)** — completed tasks with their engineering records, verbatim
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — module map, pipelines, and dated design decisions
- **[../README.md](../README.md)** — installation and usage

Goal: locally strip personally identifiable information from documents so the stripped version
can be shared with cloud models. Prefer **pseudonymization with a consistent local mapping**
(`John Smith → PERSON_1` everywhere) over blank redaction, so cloud answers can be rehydrated
locally and analytical utility is preserved.

## Input types

- [x] Plain text *(2026-07-12)*
- [x] Bank transaction CSVs — per-cell, column-aware *(2026-07-12)*
- [x] Images (scans, screenshots) — OCR, placeholders painted onto pixels *(2026-07-14)*
- [x] PDFs — treated as images: render → OCR → paint → reassemble a fresh image-only PDF
  *(2026-07-18)*
- [ ] Statement tables via the image path

## Detection layers

0. **Local LLM** — `vlm.py` reading the page image, `text_llm.py` reading the document text.
   Names, addresses, organizations, dates of birth and identifiers, semantically. Shipped
   2026-08-08 (pixels) / 2026-08-09 (text), and **the only semantic detector** in every input
   mode since.
1. **Patterns/checksums** (our own engine since 2026-08-09: TFN, Medicare, ABN/ACN,
   BSB/account, PayID, cards, email, phone, IBAN; checksum-invalid identifiers surfaced, not
   silently dropped) — shipped. Its role is to refine, validate and extend layer 0, and to be
   a deterministic recall floor under it — not to run alone.
2. ~~**Zero-shot NER** (GLiNER2 — names, addresses, DOB, person-vs-organization)~~ —
   **retired 2026-08-09**: layer 0 matched or beat it on every semantic class and every seed.
3. **Local-LLM audit pass** over the *already stripped* text ("does this still contain
   anything identifying?" — contextual identifiers, via llama-server) — **contingent, not
   committed** (expectation set 2026-07-15): the plan is to evaluate the tool end-to-end on
   layers 0+1, and build layer 3 only if those results prove unsatisfactory. Known gaps
   therefore need owners that don't assume layer 3 (see TODO.md).

Consequence of the 2026-08-09 flip, accepted knowingly: **every input mode requires a running
llama-server** (`--vlm-url` / `$PII_VLM_URL`), including the tier-1 acceptance gate. There is
no offline path, and no torch, spaCy or Presidio anywhere in the stack.

## Evaluation tiers

Constraint: real documents are classified until stripped — cloud models can only ever see
synthetic/declassified data or aggregate metrics.

- **Tier 1 — synthetic corpus** (ground truth by construction; the fast iteration loop):
  text tier shipped 2026-07-12, image tier iteration 1 shipped 2026-07-16 (paired renders
  scored by re-OCR survival), multi-page corpus 2026-08-11. Degradation pipeline pending.
- **Tier 2 — PII-transplanted real documents** (real layouts, known ground truth,
  declassified; one-time manual effort): pending.
- **Tier 3 — metrics-only runs on the real corpus** (aggregates out, local review UI):
  pending.

Scoring is recall-first and severity-weighted: acceptance = zero critical misses (TFN,
account numbers, names) on the Tier 3 review set, not a single F1 number.

## Where things stand (2026-08-12)

Text, CSV, image and PDF paths work end-to-end behind one CLI (`python -m pii`), with a local
LLM as the semantic detector on all of them and our own pattern/checksum engine underneath it.
PDFs are read in **two sweeps** — every page detected and grouped before any page is redacted —
so a value the model names on one page is redacted on every page that prints it. What survives
unredacted is reported rather than swallowed: unlocatable findings, model boxes painted without
OCR corroboration, and truncated or malformed model answers are all counted and surfaced.

The front, in order:

1. **Throughput.** ~3 min/page makes the image/PDF path a research profile, not a product one.
   The serving/quantization work is what stands between the two, and the proposed text-only
   regime (layer 0 over the OCR'd page text instead of the page image) could settle it outright
   — one to two orders of magnitude, at a cost that is known and measurable.
2. **Measurement.** Two shipped designs are reasoned-for rather than measured on the real
   corpus — the hybrid geometry and layer 1's refinement of layer-0 findings — and the tier-1
   gate's seed numbers predate the detector swap.
3. **Then** the end-to-end evaluation decides whether layer 3 is needed at all.

See [TODO.md](TODO.md) for the ordered list.
