# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

BrokerAI is a RAG (retrieval-augmented generation) semantic search tool over mortgage lender policy PDFs (Australian lenders: ANZ, AFG, Advantedge, etc.). It has an offline ingestion pipeline and a web app that searches the resulting vector store and displays hits alongside the source PDF.

The project is being revived in phases (see `ROADMAP.md`). Phase 1 is a standalone PII-stripping tool in `pii/` (eval harness in `pii_eval/`), isolated from the RAG code and documented by its own `pii/README.md`, `pii/ROADMAP.md`, and `pii/CLAUDE.md`.

## Commands

There is no requirements.txt, lint, or test suite. Key deps: quart, chromadb, pymupdf, anytree, httpx, python-box, transformers, langchain-text-splitters, aiofiles, aiopath.

- `python ingest.py` — build trees from PDFs and embed them into ChromaDB. Idempotent: skips a document if `db/trees/<id>.json` exists (tree step) or if its Chroma collection exists (embedding step). To re-ingest a document, delete its tree JSON and its Chroma collection.
- `python app.py` — run the Quart web app (binds 0.0.0.0, default port 5000). Serves the frontend from `frontend/` and expects ingestion to have been run already (it calls `get_collection`, which fails on missing collections).
- `python layout-analyzer.py <file.pdf>` — debugging utility; renders a PDF's text-block/table layout rectangles into a new PDF to help tune per-document spec parameters.
- `python embed.py` / `python embed_test.py` — ad-hoc scripts to smoke-test the embedding server and measure its latency vs input length.

Both ingestion and the app's `/search` require an **external OpenAI-compatible embeddings server** running at `http://localhost:8081/embeddings` (nomic-embed-text-v1.5, 768 dims) — configured in `config.yaml`. Nothing in this repo starts that server.

## Architecture

Data flow: PDF → header-detected section tree → token chunks → embeddings → ChromaDB → search API → frontend.

1. **PDF → tree** (`rag_tools/pdf.py`, `pdf_to_tree`): uses pymupdf. Detects headers by font size/bold/color statistics (the most common font size is body text; larger sizes map to header levels), optionally detects multi-column layout by clustering block left edges, extracts tables as markdown, and builds an `anytree` hierarchy of sections. Each node has `name` (numeric id), `level`, `header`, `text`, `page`, `position`. Trees are cached as JSON in `db/trees/<docId>.json`.
2. **Chunk + embed** (`ingest.py`): each tree node's text is prefixed with its breadcrumb header path ("A > B > C\n" + text), split with `TokenTextSplitter` using the HF nomic tokenizer (`models/tokenizer/...`), chunk size/overlap from `config.yaml` (8000/2000 tokens). Chunks are embedded with the nomic **document prefix** (`"search_document: "`) and stored in a per-document ChromaDB collection (cosine space) at `db/embeddings/`. Chunk IDs are `<tree-node-path>#<chunkIdx>` (e.g. `0/3/7#1`); metadata `pos`/`len` are character offsets into the header+text string used to recover the chunk text at query time.
3. **Search** (`app.py` `/search`): embeds the query with the nomic **query prefix** (`"search_query: "`), queries the document's collection, then uses `rag_tools.walk_tree` to resolve each chunk ID's node path back into breadcrumb headers and slices the node text by `pos`/`len`. Returns crumbs, text, page, position, and relevance (1 − cosine distance).
4. **Frontend** (`frontend/index.html` + `js/simpleviewer.mjs`): single-page jQuery UI app with a PDF.js viewer on the left and a per-document accordion of search results on the right; "goto" buttons scroll the PDF viewer to the hit's page/position and place a marker. It fans out one `/search` request per document.

## Configuration

- `config.yaml` — paths, chunker size/overlap, embedding server URL, nomic prefixes, tokenizer model path. Note the embedding prefixes matter: documents and queries must use their respective prefixes or relevance degrades.
- `db/pdfs/spec.yaml` — the per-document ingestion manifest, keyed by PDF filename. Each entry sets `id`, `title`, page range (`page_from`/`page_to`, 0-based pymupdf page numbers), `detect_columns`, `color_headers` (treat colored text as header signal), and `header`/`footer` (y-coordinates cropping page furniture). Only PDFs listed in spec.yaml are ingested/served; `db/pdfs_all/` holds the unprocessed corpus. Adding a document = copy the PDF to `db/pdfs/`, add a spec entry (tune crop/columns with layout-analyzer.py), rerun `python ingest.py`.

## Notes

- `rag_tools/tokenizer.py` is a vendored Llama 3 tiktoken tokenizer that is currently unused by the pipeline (the HF AutoTokenizer is used instead).
- `frontend/js` and `frontend/css` contain vendored jQuery UI and PDF.js builds; jQuery itself is loaded from Google's CDN in `index.html`.
