# CLAUDE.md — pii/

Guidance for Claude Code sessions working in this directory. Doc layout (reorganized
2026-07-14): [README.md](README.md) — usage; [ARCHITECTURE.md](ARCHITECTURE.md) — module
map, pipelines, and dated design decisions; [ROADMAP.md](ROADMAP.md) — activity overview
and eval tiers; [TODO.md](TODO.md) — open tasks with full detail; [DONE.md](DONE.md) —
completed tasks with their engineering records, verbatim. New decisions go to
ARCHITECTURE.md; finished TODO items move to DONE.md with their records.

## Working agreements

- **Standalone from the RAG app.** Nothing here may import `rag_tools`, `app.py`, `ingest.py`,
  or other RAG-pipeline code; the PII tool only shares the local model server. Keep it that way.
- **presidio must be ≥ 2.2.363.** 2.2.362's ACN validator rejects every ACN with check digit 0.
- **AU recognizers need explicit registration.** Presidio ships its Australian entities
  (`AU_TFN`, `AU_MEDICARE`, `AU_ABN`, `AU_ACN`) disabled/absent from the default registry —
  they must be registered explicitly, alongside the custom BSB/account/PayID recognizers in
  `recognizers.py`.
- **GLiNER2 is the sole layer-2 NER backend.** GLiNER v1 was removed 2026-07-13 (in git
  history). Its tuning quirks — windowing, repeated-mention re-finding, per-label schema
  passes, honorific extension, `max_width` — are documented in `gliner2_recognizer.py`;
  read that docstring before touching NER behaviour. Default `max_width=12`; do not raise
  past ~12 (wide-span false-positive creep starts around 16).
- **spaCy is the NLP engine only, not a detector (retired 2026-07-15).** `SpacyRecognizer` is
  removed from the registry and the `--no-ner` patterns-only regime is gone; GLiNER2 owns
  PERSON/ORG/dates and LOCATION (its location pass defaults on). spaCy stays loaded solely for
  Presidio's context enhancer (tokens/lemmas). Its old PERSON/DATE_TIME detector emissions were
  glue/FP-prone on OCR text; see `tests/pii/test_registry_policy.py`, ARCHITECTURE.md and the
  DONE.md record.
- **Eval harness is in [`../pii_eval/`](../pii_eval/)** (`python -m pii_eval generate` / `score`).
  Run it to check for regressions; the scorer gates on zero critical misses. Generated
  synthetic corpora (text and image alike) live under `pii_eval/corpora/<modality>/s<seed>`
  (gitignored) — the CLI defaults resolve there; never write corpora to session scratchpads.
- **Reference documents in [`../sensitive/`](../sensitive/) are classified.** They are
  gitignored — never commit, email, or upload them; cloud-LLM analysis of them is in-session
  only. Anything a cloud model sees must be synthetic or declassified.
