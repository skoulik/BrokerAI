# BrokerAI Architecture & Design Decisions

Living document, started 2026-07-12. Every non-obvious architecture or design decision lands
here with its rationale and date, so future changes argue against the *reason*, not just the
code. Plans and task status live in [ROADMAP.md](ROADMAP.md); this file records *why things
are the way they are*. The Phase 1 PII tool keeps its own set of these documents under
[pii/](pii/) — see below.

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

Strips PII from documents locally so the stripped versions can be shared with cloud models —
**pseudonymization with a consistent, rehydratable local mapping** (`John Smith → PERSON_1`
everywhere), not blank redaction. Layered detection (Presidio patterns/checksums with custom
AU recognizers, GLiNER2 zero-shot NER, a planned local-LLM audit pass) over text, CSV, image
and (planned) PDF inputs, with a recall-first scoring philosophy: every ambiguity resolves
toward stripping.

Fully documented in its own directory (moved out of this file 2026-07-14):

- [pii/ARCHITECTURE.md](pii/ARCHITECTURE.md) — module map, pipelines, all design decisions
- [pii/ROADMAP.md](pii/ROADMAP.md) — activity overview and evaluation tiers
- [pii/TODO.md](pii/TODO.md) — open tasks
- [pii/DONE.md](pii/DONE.md) — completed tasks with engineering records
- [pii/README.md](pii/README.md) — usage

## Dependency/runtime notes

- The `pii/` tool keeps its own `requirements.txt`; repo-wide `pyproject.toml` + uv is a
  Phase 2 item. PII-specific runtime notes (CUDA torch, model caches, Tesseract) live in
  [pii/ARCHITECTURE.md](pii/ARCHITECTURE.md).
