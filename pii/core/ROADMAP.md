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

1. **Patterns/checksums** (Presidio + custom AU recognizers: TFN, Medicare, ABN/ACN,
   BSB/account, PayID; checksum-invalid identifiers surfaced, not silently dropped) — shipped.
2. **Zero-shot NER** (GLiNER2 — names, addresses, DOB, person-vs-organization) — shipped.
3. **Local-LLM audit pass** ("does this still contain anything identifying?" — contextual
   identifiers, via llama-server) — **contingent, not committed** (expectation set
   2026-07-15): the plan is to evaluate the tool end-to-end with layers 1+2 only; layer 3
   gets built only if those results prove unsatisfactory. Known layer-1/2 gaps therefore
   need owners that don't assume layer 3 (see TODO.md).

## Evaluation tiers

Constraint: real documents are classified until stripped — cloud models can only ever see
synthetic/declassified data or aggregate metrics.

- **Tier 1 — synthetic corpus** (ground truth by construction; the fast iteration loop):
  text tier shipped 2026-07-12; image/degradation tier pending.
- **Tier 2 — PII-transplanted real documents** (real layouts, known ground truth,
  declassified; one-time manual effort): pending.
- **Tier 3 — metrics-only runs on the real corpus** (aggregates out, local review UI):
  pending.

Scoring is recall-first and severity-weighted: acceptance = zero critical misses (TFN,
account numbers, names) on the Tier 3 review set, not a single F1 number.

## Where things stand (2026-07-18)

Text, CSV, image and PDF paths work end-to-end behind one CLI (`python -m pii`); detection
layers 1–2 are eval-gated on the Tier-1 text and image corpora, and `pii_eval score
--modality pdf` runs the full PDF pipeline against the real-document corpus. The current
front: demo on the reference documents → degradation tier → one-pass VLM experiment;
after that the end-to-end evaluation decides whether layer 3 is needed at all — see
[TODO.md](TODO.md) for the ordered list.
