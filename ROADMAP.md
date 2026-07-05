# BrokerAI Revival Roadmap

Written 2026-07-05 after a review of the abandoned codebase. This is a living document — refine
items and record decisions here as they are made.

## Context and constraints

- Input documents are **classified — nothing leaves the machine**. All models run locally
  (MacBook Pro, 64 GB unified memory; Ollama / llama.cpp / MLX).
- What's worth keeping: the PDF→section-tree design (`rag_tools/pdf.py`), breadcrumb-prefixed
  chunks, page/position metadata enabling jump-to-source in the PDF viewer, ChromaDB, pymupdf.
- What's outdated: chunking parameters, dense-only retrieval, models (nomic-embed-text v1.5),
  frontend toolchain (jQuery UI), no chat, no multi-doc search.

## Phase 1 — PII stripping tool (START HERE; isolated from the rest)

Goal: locally strip personally identifiable information from documents so the stripped version
can be shared with cloud models. Prefer **pseudonymization with a consistent local mapping**
(`John Smith → PERSON_1` everywhere) over blank redaction, so cloud answers can be rehydrated
locally and analytical utility is preserved.

Input types to support:
- [ ] Plain text
- [ ] Images (scans, screenshots) — OCR with word-level bounding boxes, redact by painting
      over pixel regions
- [ ] PDFs — **treat as images**: render pages → OCR → redact pixels → reassemble PDF.
      Rationale: financial-sector PDFs often have junk/broken text layers, and rebuilding from
      pixels also eliminates the hidden-text-layer leak class entirely.
      *Decide later:* belt-and-braces variant that additionally scans any existing text layer
      to catch text the OCR misses (detection only — output still comes from pixels).
- [ ] Bank transaction lists (CSV / statement tables) — column-aware handling. Descriptions
      contain personal names, PayID emails/phones, BSB/account refs; these reveal spending
      patterns and allow re-identification. Keep merchant names (analytical value), strip
      person names — zero-shot NER labels (GLiNER) distinguish person vs organization.
      Consistent pseudonyms per counterparty so patterns survive but identity doesn't.

Detection pipeline (layered — no single layer catches everything):
1. Pattern recognizers via Presidio, with **custom Australian entities**: TFN, Medicare number,
   BSB + account number, ABN/ACN, AU phone/address formats, PayID.
2. NER: GLiNER-PII (~600 MB, runs anywhere) as Presidio engine.
3. Local-LLM audit pass: "does this still contain anything identifying?" — catches contextual
   identifiers NER misses ("the borrower's wife, a dentist in Wagga Wagga").

Tasks:
- [ ] Standalone module/CLI, separate from the RAG app (shares the local model server)
- [ ] Consistent pseudonym mapping store + rehydration of cloud responses
- [ ] OCR engine choice — *decide later:* Tesseract vs PaddleOCR vs Surya/docTR vs a local VLM
      (e.g. Qwen-VL class) doing OCR+PII detection in one pass. Start by benchmarking on real
      bank statements/scans.
- [ ] Metadata scrubbing on all output formats

Evaluation (constraint: real documents are classified until stripped — cloud models can only
ever see synthetic/declassified data or aggregate metrics):
- [ ] **Tier 1 — synthetic corpus**: local generator with Faker + custom AU providers (TFN and
      Medicare with valid check digits, BSB/account, ABN/ACN, PayID), fake statement templates
      and transaction CSVs, degradation pipeline (DPI, skew, blur, JPEG artifacts) for OCR
      benchmarking. Ground truth known by construction → automatic precision/recall; the fast
      iteration loop, fully shareable. Sergey will supply a few unclassified-by-construction
      example documents to serve as layout/format references for the generator's templates.
- [ ] **Tier 2 — PII-transplanted real documents**: Sergey manually replaces real PII with fake
      in 4–6 real documents (one per major bank layout, one bad scan, one transactions CSV),
      keeping layout intact. Real layouts + known ground truth + declassified. One-time effort,
      reusable forever.
- [ ] **Tier 3 — metrics-only runs on the real corpus**: harness emits only aggregates (entity
      counts/type, confidence histograms, layer-disagreement rates, cross-OCR-engine
      disagreement). Local side-by-side review UI so manual acceptance checks are a quick
      click-through; only declassified findings are reported back.
- [ ] Scoring is recall-first and severity-weighted: acceptance = zero critical misses (TFN,
      account numbers, names) on the Tier 3 review set, not a single F1 number.

## Phase 2 — Foundation cleanup

- [ ] `pyproject.toml` + uv; pin dependencies
- [ ] Ollama as the single local model server (chat + embeddings, OpenAI-compatible API — the
      existing `Embedder` needs only config changes). *Decide later:* llama.cpp server or MLX
      if Ollama speed disappoints.
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
| Runtime | Ollama | llama.cpp (control), MLX (Mac speed) |
| Chat LLM | Qwen 3.5/3.6 35B-A3B | Gemma 3 27B, gpt-oss-20b |
| Embeddings | BGE-M3 | Qwen3-Embedding 0.6B–8B |
| Reranker | bge-reranker-v2-m3 | Qwen3-Reranker |
| PII NER | Presidio + GLiNER-PII | John Snow Labs (commercial), local VLM |
| Vector DB | ChromaDB (single collection) | Qdrant |
