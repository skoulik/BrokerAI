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
can be shared with cloud models. Prefer **pseudonymization with a consistent local mapping**
(`John Smith → PERSON_1` everywhere) over blank redaction, so cloud answers can be rehydrated
locally and analytical utility is preserved.

Input types to support:
- [x] Plain text *(2026-07-12: `pii/` package — see its README)*
- [ ] Images (scans, screenshots) — OCR with word-level bounding boxes, redact by painting
      over pixel regions
- [ ] PDFs — **treat as images**: render pages → OCR → redact pixels → reassemble PDF.
      Rationale: financial-sector PDFs often have junk/broken text layers, and rebuilding from
      pixels also eliminates the hidden-text-layer leak class entirely.
      *Decide later:* belt-and-braces variant that additionally scans any existing text layer
      to catch text the OCR misses (detection only — output still comes from pixels).
- [x] Bank transaction lists (CSV / statement tables) — column-aware handling.
      *(2026-07-12: CSV mode done — per-cell detection, `--columns` filter; statement tables
      from the image path still pending.)* Descriptions
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
- [x] Standalone module/CLI, separate from the RAG app (shares the local model server)
      *(2026-07-12: `pii/`, layers 1–2 working: Presidio + custom AU recognizers, GLiNER.
      Findings: Presidio's AU recognizers need explicit registration; overlapping PII spans
      must be merged not ranked, or partially-covered spans leak; GLiNER needs per-line and
      de-capitalized passes for all-caps statement lines. LLM audit layer still pending.
      CPU-only torch is slow (~1 min/page-ish) — install CUDA torch for the 2080 Ti when it
      matters.)*
- [x] Consistent pseudonym mapping store + rehydration of cloud responses
      *(2026-07-12: JSON store, document-order numbering, case-insensitive value matching.)*
- [ ] Configurable strip-entity selection — let a run choose which data types to strip
      (e.g. names and addresses only). The pipeline already takes a `strip_entities` set
      internally; needs CLI exposure (`--entities` / named profiles) and documentation.
- [ ] OCR engine choice — *decide later:* Tesseract vs PaddleOCR vs Surya/docTR vs a local VLM
      (e.g. Qwen-VL class) doing OCR+PII detection in one pass. Start by benchmarking on real
      bank statements/scans.
- [ ] Metadata scrubbing on all output formats
- [ ] Barcode masking: mailing barcodes on statements (Australia Post 4-state, and 1-D codes)
      encode the delivery address/customer ref — text-based detection can't see them, so
      detect and paint over barcode regions in the image pass (observed on several of the
      reference examples)
- [ ] Overlaps merging algorithm — define and document. Interesting areas: how the weights are 
      combined (max, average, bayesian/aposteriori), what if winning classes of overlaps
      do not agree, should we merge at all in some cases.
- [ ] Log checksum-invalid identifiers. If an identifier candidate passes the detectors, but
      is rejected by the checksum validator, this should be logged. Evaluate if the output 
      will become too noisy because of this and if so, make the feature optional. Rationale:
      detect typos, wrong OCR output or outright forgery - all three are importans classes.
- [ ] Evaluate GLiNER2 (https://github.com/fastino-ai/GLiNER2) — why it exist, what it adds
      or improves compared to GLiNER, is it maintained, what license/usage terms.

Evaluation (constraint: real documents are classified until stripped — cloud models can only
ever see synthetic/declassified data or aggregate metrics):
- [ ] **Tier 1 — synthetic corpus**: local generator with Faker + custom AU providers (TFN and
      Medicare with valid check digits, BSB/account, ABN/ACN, PayID), fake statement templates
      and transaction CSVs, degradation pipeline (DPI, skew, blur, JPEG artifacts) for OCR
      benchmarking. Ground truth known by construction → automatic precision/recall; the fast
      iteration loop, fully shareable. Sergey will supply a few unclassified-by-construction
      example documents to serve as layout/format references for the generator's templates.
      *(2026-07-12: text tier done — `pii_eval/` package: checksum-valid AU providers, seeded
      persona pool, legacy-statement + loan-application + transaction-CSV templates with exact
      ground-truth spans, recall-first scorer with zero-critical-miss gate. Found and fixed:
      un-hyphenated/hyphenated/labeled account-number forms in transaction descriptions leaked
      (recognizer patterns extended), NER spans crossing CSV cell sentinels crashed csv_mode
      (now clamped per cell), presidio 2.2.362 rejects ACNs with check digit 0 (keep ≥ 2.2.363).
      Current: all pattern entities 100% on two seeds; PERSON 98–100% — GLiNER misses rare
      reversed-caps and "D & D Duncan" joint forms; those plus contextual identifiers are the
      layer-3 LLM-audit backlog. GLiNER now runs on CUDA (~25× faster). PDF/image tier +
      degradation pipeline still pending.)*
      **Received 2026-07-12** — a set of reference documents in `sensitive/statements/`
      (gitignored; never commit, email, or upload — cloud-LLM analysis in-session only).
      Good layout diversity: multiple major-bank statement formats, home-loan and business
      account variants, a plain-text legacy format, and an insurance certificate; at least
      one has a **broken text layer**, confirming the render-as-image rationale.
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
| PII NER | Presidio + GLiNER-PII | John Snow Labs (commercial), local VLM |
| Vector DB | ChromaDB (single collection) | Qdrant |
