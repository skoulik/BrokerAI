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
2. **GLiNER zero-shot NER** (`urchade/gliner_multi_pii-v1`) — names, addresses, DOB, and the
   person-vs-organization distinction that transaction descriptions need.
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

### GLiNER multi-pass prediction (2026-07-12)

Two empirical quirks, found on the synthetic sample and baked into the recognizer
(`pii/gliner_recognizer.py`):

- **ALL-CAPS text tanks recall** — `TRANSFER TO J SMITH ACC 12345678` yields nothing; the
  title-cased form finds both entities. Bank statements are largely upper-case.
- **Context sensitivity** — entities found reliably in a short line are missed when the same
  line sits inside a full document.

Therefore each text is predicted over overlapping document windows (catches multi-line
entities like wrapped addresses) *and* over individual lines, each additionally in a
length-preserving de-capitalized variant; results are unioned (batched inference). Accepted
cost: some over-stripping (e.g. an all-caps merchant line labeled as an address) — a
precision-tuning item for Tier-1 eval, not a leak.

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

### Evaluation (designed 2026-07-05/12, not yet built)

Three tiers, because real documents are classified until stripped: (1) synthetic corpus with
ground truth by construction; (2) PII-transplanted real layouts; (3) metrics-only runs on the
real corpus. Acceptance is recall-first and severity-weighted: zero critical misses (TFN,
account numbers, names), not an F1 number. Details in ROADMAP Phase 1.

## Dependency/runtime notes

- The `pii/` tool keeps its own `requirements.txt`; repo-wide `pyproject.toml` + uv is a
  Phase 2 item.
- torch is currently CPU-only; the NER layer costs ~1 min/page. Install a CUDA build for the
  RTX 2080 Ti when throughput matters.
- GLiNER weights download once into `models/hf-cache/` (gitignored).
