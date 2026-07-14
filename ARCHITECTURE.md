# BrokerAI Architecture & Design Decisions

Living document, started 2026-07-12. Every non-obvious architecture or design decision lands
here with its rationale and date, so future changes argue against the *reason*, not just the
code. Plans and task status live in [ROADMAP.md](ROADMAP.md); this file records *why things
are the way they are*.

## System overview

Two deliberately independent parts:

1. **RAG app** — semantic search (later: chat) over mortgage lender policy PDFs.
   Ingestion (`ingest.py`, `rag_tools/`) → ChromaDB → Quart API (`app.py`) → PDF.js frontend.
2. **PII stripping tool** (`pii/`) — strips personally identifiable information from documents
   locally so the stripped versions can be shared with cloud models. Shares nothing with the
   RAG app except (in the future) the local model server.

### Cross-cutting constraints

- **Local-only processing (2026-07-05).** Input documents are classified — nothing leaves the
  machine. All models run locally. Cloud models may only ever see synthetic data, PII-stripped
  output, or aggregate metrics.
- **llama.cpp as the model runtime (2026-07-12, replacing an earlier Ollama plan).** One
  runtime for chat + embeddings with an OpenAI-compatible API (`llama-server`). Hosts: MacBook
  Pro 64 GB (Metal, primary) and the Windows dev box (RTX 2080 Ti + RX 9070 XT; one Vulkan
  build can drive both GPUs). Fallbacks if performance disappoints: MLX on the Mac, split
  CUDA/Vulkan servers on Windows. Details in ROADMAP.

## RAG pipeline (current state, pre-Phase-2)

PDF → header-detected section tree (`rag_tools/pdf.py`, font-statistics heuristics, cached as
JSON) → breadcrumb-prefixed token chunks → nomic embeddings → per-document ChromaDB
collections → `/search` API → jQuery UI + PDF.js frontend with jump-to-source.

Decisions that survive the planned rebuild (per the 2026-07-05 review):

- **Section tree with page/position metadata.** The tree shape enables parent-document
  retrieval (embed small, return the enclosing section) and jump-to-spot-in-PDF; both are
  planned features, so the tree stays.
- **Breadcrumb header prefixes on chunks** preserve hierarchical context in embeddings.
- **Per-document tuning manifest** (`db/pdfs/spec.yaml`): lender PDFs are messy in
  document-specific ways (columns, colored headers, page furniture); a per-document spec beats
  a universal parser. Open question (Phase 3): custom parser vs Docling/marker — decide by
  head-to-head on the actual corpus.

Known-outdated parts (dense-only retrieval, 8K/2K chunking, nomic-embed v1.5, per-document
collections, jQuery frontend) are scheduled for replacement — see ROADMAP Phases 2–5.

## PII stripping tool (`pii/`)

Goal: output safe to hand to cloud models, with enough analytical utility left to be worth
handing over. Usage in `pii/README.md`.

### Pseudonymization over redaction (2026-07-05)

PII is replaced with stable placeholders (`John Smith → PERSON_1` everywhere, across a whole
document set), not blanks. Rationale: the cloud model can still reason about "PERSON_1's
recurring rent payments", and its answers are **rehydratable** — a local reverse pass restores
the real values. The mapping store (JSON) contains the original PII: it is gitignored and must
never leave the machine.

- Placeholders are allocated in document order (readable mappings) and matched
  case-insensitively with whitespace collapsed; rehydration restores the first-seen surface
  form.

### Layered detection (2026-07-05; layers 1–2 built 2026-07-12)

No single detector catches everything, so layers are unioned:

1. **Presidio pattern/checksum recognizers** — deterministic, high precision: AU TFN /
   Medicare / ABN / ACN (checksum-validated), credit cards, emails, IBAN, IPs, URLs, AU-region
   phones, plus custom recognizers for BSB, bank account numbers, and PayID
   (`pii/recognizers.py`).
2. **GLiNER2 zero-shot NER** (`fastino/gliner2-privacy-filter-PII-multi`) — names, addresses,
   DOB, and the person-vs-organization distinction that transaction descriptions need.
3. **Local-LLM audit pass** (planned) — "does this still contain anything identifying?" for
   contextual identifiers NER can't see ("the borrower's wife, a dentist in Wagga Wagga").

### Presidio AU recognizers require explicit registration (2026-07-12)

Presidio *ships* AU_TFN/AU_MEDICARE/AU_ABN/AU_ACN implementations (open source, MIT — no paid
tier involved), but its default registry config
(`presidio_analyzer/conf/default_recognizers.yaml`) lists every country-specific recognizer
with `enabled: false`; only generic + US recognizers are on by default. Consequence: they
silently never run unless registered. `pii/pipeline.py` registers the four AU classes
explicitly. The checksum logic is ordinary local Python in the library
(`predefined_recognizers/country_specific/australia/`) and verified working: a valid-checksum
TFN scores 1.00, a digit-swapped one is rejected entirely.

### Recall-first span handling (2026-07-12 — two leak classes found and designed out)

Scoring philosophy: a false positive costs some analytical utility; a false negative leaks
classified PII. Every ambiguity resolves toward stripping.

- **Filter before overlap resolution.** Detected spans are filtered to strip-listed entity
  types *before* overlaps are resolved. Found the hard way: spacy emits bogus high-score
  `DATE_TIME` spans over account/phone numbers; if kept-type spans compete, they shadow real
  PII which then leaks.
- **Merge overlapping PII spans; never rank them.** Highest-score-wins let a small `AU_BSB`
  span (0.55) evict a wider account-number span (0.52) that covered it, exposing the
  remainder. Overlapping strip-listed spans are unioned into one replacement (entity type of
  the highest-scored member).

### NER backend: GLiNER v1 → GLiNER2 (2026-07-12; v1 removed 2026-07-13)

The original backend (`urchade/gliner_multi_pii-v1`) had two empirical quirks found on the
synthetic sample — ALL-CAPS text tanked recall, and entities found reliably in a short line
were missed when the same line sat inside a full document — worked around with multi-pass
prediction (document windows + individual lines, each also de-capitalized, unioned).

GLiNER2 (Fastino, Apache 2.0, PII-tuned model with schema descriptions) has neither weakness,
matched v1 on Tier-1 PERSON (100%) and ran ~4.7× faster, so it became the sole backend; the
v1 recognizer and its `--ner-backend` switch were removed (recoverable from git history).
GLiNER2's own quirks and tuning live in `pii/gliner2_recognizer.py`'s docstring; its known
gap — multi-part AU addresses fragmented into street/suburb spans — is targeted by the
adjacent-span-merging and LoRA-adapter ROADMAP tasks. Accepted cost either way: some
over-stripping (e.g. a merchant line labeled as an address) — a precision-tuning item, not a
leak.

### What is deliberately kept (2026-07-12)

`ORGANIZATION` (merchant names — the analytical substance of spending data) and `DATE_TIME`
(transaction dates) are detected but not stripped by default; `DATE_OF_BIRTH` is stripped.
Overrides: `--strip-orgs` now; full per-run entity-type selection is a planned feature
(ROADMAP Phase 1).

### CSV handling (2026-07-12)

Bank transaction lists are processed **per cell**, optionally restricted to named columns:
placeholders can never straddle cell boundaries, and date/amount columns pass through
byte-identical. Cells of a column are batched into one analyzer call, joined by a sentinel
(`␞`) no recognizer can match across, with a hard alignment check afterwards. Side benefit
observed: cell-level context avoids some of the over-stripping seen in whole-text mode.

### PDFs will be processed as rendered images (decided 2026-07-05, not yet built)

Financial-sector PDFs often carry junk/broken text layers (confirmed: one reference statement
has one), and rebuilding output from pixels eliminates the hidden-text-layer leak class
entirely. Corollary requirement from the reference docs: mailing barcodes (Australia Post
4-state) encode the delivery address and are invisible to text-based detection — the image
pass must detect and mask barcode regions.

### Image path is orthogonal to presidio-image-redactor (2026-07-14)

The OCR/image pipeline is built around our own `PiiPipeline`, not Microsoft's
`presidio-image-redactor` package. Presidio stays exactly where it is today — as the engine
inside the *text-analysis* layer — and the image path is a front-end (render → OCR → assembled
text with offset↔word-box bookkeeping) plus a back-end (span → boxes → paint → reassemble PDF)
around the unchanged text pipeline. Reasons:

- **Wrong hook point.** `ImageAnalyzerEngine` plugs in at the bare `AnalyzerEngine` level, but
  our value-add lives above it in `pii/pipeline.py`: recall-first union overlap merging (theirs
  drops overlaps by score rank — the leaky approach rejected 2026-07-12), invalid-identifier
  collection/reporting, strip planning, pseudonym mapping. Adopting it means bypassing or
  forking all of that.
- **Wrong output model.** `ImageRedactorEngine` draws filled boxes — blank redaction. Our core
  requirement is pseudonymization: paint the region and draw the placeholder (`PERSON_1`) into
  it, emitting the same rehydratable `map.json`.
- **No home for roadmap items.** Barcode masking is not text-driven (no OCR span to map);
  the OCR bake-off needs an engine interface we own (theirs is shaped like Tesseract's TSV, so
  wiring PaddleOCR/Surya is the same work either way); a future local-VLM path does OCR+detection
  in one pass, which an OCR-then-analyze frame can't express; PDF reassembly and the
  belt-and-braces text-layer scan are ours to build regardless.
- **The eval needs to own the mapping.** pii_eval's planned degradation tier and the Tier-3
  cross-OCR-engine disagreement metric both require control over the assembled-text/offset/box
  contract — that must not be buried in a third-party engine.

What we do reuse: the entire eval-gated text pipeline verbatim on OCR output, and their
span→bbox mapping logic as a *reference* (the one solved piece — small, MIT; see the
source-review task in `pii/ROADMAP.md`). `presidio-image-redactor` is not installed as a
dependency; only `presidio-analyzer` remains.

### Evaluation (designed 2026-07-05/12, not yet built)

Three tiers, because real documents are classified until stripped: (1) synthetic corpus with
ground truth by construction; (2) PII-transplanted real layouts; (3) metrics-only runs on the
real corpus. Acceptance is recall-first and severity-weighted: zero critical misses (TFN,
account numbers, names), not an F1 number. Details in ROADMAP Phase 1.

## Dependency/runtime notes

- The `pii/` tool keeps its own `requirements.txt`; repo-wide `pyproject.toml` + uv is a
  Phase 2 item.
- CUDA torch installed 2026-07-12 for the RTX 2080 Ti (CPU-only NER cost ~1 min/page).
- GLiNER2 weights download once into `models/hf-cache/` (gitignored).
