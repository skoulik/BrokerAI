# BrokerAI Revival Roadmap

Written 2026-07-05 after a review of the abandoned codebase. This is a living document — refine
items and record task status here. Architecture and design decisions (the *why*) are recorded
in [ARCHITECTURE.md](ARCHITECTURE.md).

## Context and constraints

- Input documents are **classified — nothing leaves the machine**. All models run locally.
  Two machines (decided 2026-07-12: **llama.cpp directly, Ollama abandoned**):
  - **MacBook Pro, 64 GB unified memory** (`claude@SERGEI-MACBOOK-PRO`) — primary model host;
    llama.cpp with Metal, rollout by Sergei.
  - **Windows dev box** — RTX 2080 Ti (11 GB, CUDA) + RX 9070 XT (16 GB, RDNA4); for testing
    smaller models locally. Mixed vendors: start with one official **Vulkan** build of
    llama.cpp, which can drive both GPUs in a single `llama-server` (`--split-mode layer`,
    ~27 GB combined VRAM); if Vulkan throughput disappoints, run two servers instead
    (CUDA build for the 2080 Ti, Vulkan/HIP build for the 9070 XT) on separate ports.
- What's worth keeping: the PDF→section-tree design (`rag_tools/pdf.py`), breadcrumb-prefixed
  chunks, page/position metadata enabling jump-to-source in the PDF viewer, ChromaDB, pymupdf.
- What's outdated: chunking parameters, dense-only retrieval, models (nomic-embed-text v1.5),
  frontend toolchain (jQuery UI), no chat, no multi-doc search.

## Phase 1 — PII stripping tool (START HERE; isolated from the rest)

Goal: locally strip personally identifiable information from documents so the stripped version
can be shared with cloud models — **pseudonymization with a consistent local mapping**
(`John Smith → PERSON_1` everywhere) rather than blank redaction, so cloud answers can be
rehydrated locally and analytical utility is preserved. Standalone tool in [`pii/`](pii/),
isolated from the RAG app; eval harness in [`pii_eval/`](pii_eval/).

Status: text and transaction-CSV paths done; detection layers 1–2 (Presidio + custom AU
recognizers, GLiNER2 NER) shipped. Pending: the image/PDF-as-image path, the layer-3 local-LLM
audit pass, and eval tiers 2–3. **Full task list, design decisions, and completed-task
engineering records live in [pii/ROADMAP.md](pii/ROADMAP.md).**

## Phase 2 — Foundation cleanup

- [ ] `pyproject.toml` + uv; pin dependencies
- [ ] **Testbench** (agreed 2026-07-14; pull forward — applies to the Phase 1 pii work
      first, starting with the checksum-invalid eval-generator extension): top-level
      `tests/` with per-feature pytest tests mirroring the package layout (`tests/pii/`,
      later `tests/rag_tools/`, ...). Markers to keep the default run fast and
      model-free (`slow`, `model`/`gpu` for anything loading GLiNER2 or needing CUDA);
      session-scoped fixtures so heavyweight models load once per run. Reporting:
      terminal summary plus a machine-readable artifact (JUnit XML or pytest-html) for
      review. Deterministic unit/regression tests complement — not replace — the
      statistical `pii_eval` corpus harness; expose the tier-1 zero-critical-miss gate
      as a marked slow test so "run everything" is one command. Config lands in
      `pyproject.toml` (ties into the item above; a minimal pyproject now is fine
      without the full uv migration).
- [ ] **llama.cpp (`llama-server`) as the local model server** (chat + embeddings,
      OpenAI-compatible API — the existing `Embedder` needs only config changes). Decided
      2026-07-12, replacing the earlier Ollama plan; see machine topology in
      *Context and constraints*. *Decide later:* MLX on the Mac if llama.cpp Metal speed
      disappoints.
- [ ] Single ChromaDB collection with `docId` metadata instead of per-document collections
      (prerequisite for multi-doc search). *Decide later:* Qdrant if Chroma's hybrid search
      proves too limited.
- [ ] Fix known bugs: query truncated by chars not tokens (`app.py:52`), crash on missing
      collection (`app.py:55`), `text.index(s)` mis-location on repeated chunk text
      (`ingest.py:104`)
- [ ] Split ingestion into a proper CLI; stop calling `app.run()` at import

## Phase 3 — Retrieval quality

- [ ] Build a small eval set of real broker questions first; measure every change against it
- [ ] Re-chunk: embed small (~256–512 tokens), return big (parent section via `walk_tree`) —
      the existing tree is perfectly shaped for parent-document retrieval
- [ ] New embedding model — *decide later:* BGE-M3 (dense+sparse in one model, enables hybrid)
      vs Qwen3-Embedding (0.6B–8B, SOTA dense)
- [ ] Hybrid retrieval (dense + BM25/sparse) — exact terms like "LVR 95%" matter
- [ ] Reranker: bge-reranker-v2-m3 (or Qwen3-Reranker)
- [ ] Unified multi-document search with cross-document ranking
- [ ] *Decide later:* keep the custom PDF tree parser vs Docling/marker — head-to-head test on
      the actual lender PDFs; the custom parser encodes per-document tuning (spec.yaml)

## Phase 4 — Chat with documents

- [ ] Chat LLM: MoE ~30–35B class with ~3B active params (Qwen 3.5/3.6 35B-A3B, ~30 tok/s on
      this hardware). Alternatives: Gemma 3 27B, gpt-oss-20b. Dense 70B fits in 64 GB but too
      slow for chat.
- [ ] v1: retrieve → rerank → stuff top sections with chunk IDs → model emits structured
      citations (`[doc:anz path:0/3/7]`) → frontend renders jump-to-PDF buttons reusing the
      existing page/position plumbing
- [ ] Streaming over SSE
- [ ] v2: agentic retrieval — give the model a `search` tool for multi-hop queries
      ("compare ANZ and AFG on genuine savings"); also covers multi-doc comparison

## Phase 5 — Frontend rebuild

- [ ] Keep PDF.js; rebuild the shell — *decide later:* Vite + React vs Svelte
- [ ] Chat panel alongside the PDF viewer
- [ ] True highlight rectangles instead of the single marker — requires storing block bounding
      boxes in tree nodes during ingestion (add while re-ingesting anyway)

## Reference: local stack (as of mid-2026)

| Role | First choice | Alternatives |
|---|---|---|
| Runtime | llama.cpp (`llama-server`) | MLX (Mac speed) |
| Chat LLM | Qwen 3.5/3.6 35B-A3B | Gemma 3 27B, gpt-oss-20b |
| Embeddings | BGE-M3 | Qwen3-Embedding 0.6B–8B |
| Reranker | bge-reranker-v2-m3 | Qwen3-Reranker |
| PII NER | Presidio + GLiNER2-PII | John Snow Labs (commercial), local VLM |
| Vector DB | ChromaDB (single collection) | Qdrant |
