# CLAUDE.md — pii/

Guidance for Claude Code sessions working anywhere under `pii/`. The tool is three components
(2026-07-16 split; rules in [ARCHITECTURE.md](ARCHITECTURE.md)): **`pii.core`** the engine,
**`pii.cli`** the command-line front-end, **`pii.gui`** a planned GUI (stubs).

Docs: each component has its own `ARCHITECTURE`/`ROADMAP`/`TODO`/`DONE` — engine detail in
[core/](core/ARCHITECTURE.md), CLI in [cli/](cli/ARCHITECTURE.md), GUI stubs in
[gui/](gui/ARCHITECTURE.md). Root [README.md](README.md) is usage; the root
`ARCHITECTURE`/`ROADMAP`/`TODO`/`DONE` are the **umbrella** (component map + cross-cutting only).

## Documentation ownership — one home per kind of fact

A decision belongs in **exactly one** place. The recurring failure mode is narrating the same
decision into six files (ARCHITECTURE + README + this file + TODO + a source docstring + DONE);
when it later changes, most copies go stale. Keep each kind of fact in its home:

| File | Owns | Must NOT carry |
|---|---|---|
| `core/ARCHITECTURE.md` | The **current** design + its rationale (the "why"). One date-stamp per decision for provenance. | A changelog of reversals. |
| `core/DONE.md` | The **raw history** — experiments, eval numbers, head-to-heads, retired approaches. Append-only. | Distilled current design (that's ARCHITECTURE's job). |
| `core/TODO.md` | Open work only. | Re-explanations of closed decisions. |
| `README.md` | **Usage** — flags, behaviour a user running the tool needs. | Dates, issue numbers, eval numbers, design rationale (→ link ARCHITECTURE). |
| this file (`pii/CLAUDE.md`) | **Invariants** a future session must not break, stated imperatively + a one-line why. | Multi-sentence re-derived rationale. |
| source docstrings | What the code **does** + non-obvious **operational** constraints for editing *that file*. | Eval numbers, dated history, head-to-heads (→ one pointer). |

Two habits that keep it from drifting back:

- **When a decision is superseded, rewrite it** to state today's design — do not stack a
  "superseded in part" note on top of the old entry. The before/after story goes to DONE.md; a
  reader of ARCHITECTURE should never have to diff two dated entries to learn the current state.
- **Move, don't delete, real history.** If you strip a dated experiment or eval numbers out of
  README/ARCHITECTURE/a docstring, make sure DONE.md carries it first.

New engine decisions go to [core/ARCHITECTURE.md](core/ARCHITECTURE.md); finished engine TODO
items move to [core/DONE.md](core/DONE.md) with their records.

## Working agreements

- **Three components, one rule set (2026-07-16).** `pii.core` is the engine — a library with a
  deliberate public API in `pii/core/__init__.py`. `pii.cli` and `pii.gui` both build on it and
  **must never import each other**; `core` depends on no front-end. If a front-end needs logic
  the other already has, push it **down into `core`**. `python -m pii` stays the canonical CLI
  entry.
- **Standalone from the RAG app.** Nothing here may import `rag_tools`, `app.py`, `ingest.py`,
  or other RAG-pipeline code; the PII tool only shares the local model server. Keep it that way.
- **presidio must be ≥ 2.2.363.** 2.2.362's ACN validator rejects every ACN with check digit 0.
- **AU recognizers need explicit registration.** Presidio ships its Australian entities
  (`AU_TFN`, `AU_MEDICARE`, `AU_ABN`, `AU_ACN`) disabled/absent from the default registry —
  they must be registered explicitly, alongside the custom BSB/account/PayID recognizers in
  `pii/core/recognizers.py`.
- **GLiNER2 is the sole layer-2 NER backend.** GLiNER v1 was removed 2026-07-13 (in git
  history). Its tuning quirks — windowing, repeated-mention re-finding, per-label schema
  passes, honorific extension, `max_width` — are documented in `pii/core/gliner2_recognizer.py`;
  read that docstring before touching NER behaviour. Default `max_width=12`; do not raise
  past ~12 (wide-span false-positive creep starts around 16).
- **spaCy is the NLP engine only, not a detector.** `SpacyRecognizer` is not in the registry
  and the `--no-ner` patterns-only regime is gone; GLiNER2 owns PERSON/ORG/dates. spaCy stays
  loaded solely for Presidio's context enhancer (tokens/lemmas). Regression-tested in
  `tests/pii/core/test_registry_policy.py`; rationale in [core/ARCHITECTURE.md](core/ARCHITECTURE.md).
- **No standalone place-name detection.** A lone city/town name passes verbatim; `LOCATION` is
  not in `DEFAULT_STRIP_ENTITIES`, the placeholder map, or the recognizer's supported entities.
  The ADDRESS passes still strip full addresses and suburb-postcode lines. Rationale in
  [core/ARCHITECTURE.md](core/ARCHITECTURE.md).
- **The OCR perception layer (`OcrPage`) carries no character offsets.** Offsets live only in
  the linearization source map (`RecognizerInput` / `linearize`) — an offset is a
  (page, assembly) property and we run multiple trial linearizations; baking one onto a line
  ties perception to one assembly. Keep the anti-silent-leak rule (record `(start,end,box)` at
  construction, never re-derive from lengths) on the source map.
- **An OCR line is never dropped.** `OcrLine.block_id` is total: line-only backends synthesize
  one block per line; a layout model's orphan line gets its own synthetic block. A dropped line
  is unredacted PII.
- **PP-StructureV3 needs the `paddlex[ocr]` extras and the `_stub_torch` `Tensor`-as-class
  shim** (scipy, pulled in by `paddlex[ocr]`, probes `issubclass(x, torch.Tensor)`). Its models
  live under `models/paddlex`. Reach OCR that yields an `OcrPage` only through `get_ocr_page`
  (worker on the GPU wheel, in-process on CPU) — never import torch into a paddle-GPU process.
- **Edge cases get dual coverage (2026-07-15).** Every newly identified corner case or fail
  mode gets BOTH a pytest test (model-free via the fake-model/stub patterns where possible,
  `model`-marked otherwise) AND a pii_eval corpus probe (distinct truth type per the
  PERSON_REVERSED convention for known-hard forms). The harness measures trends but runs
  manually; the testbench runs on every change — one without the other is a blind spot.
- **Eval harness is in [`../pii_eval/`](../pii_eval/)** (`python -m pii_eval generate` / `score`).
  Run it to check for regressions; the scorer gates on zero critical misses. Generated
  synthetic corpora (text and image alike) live under `pii_eval/corpora/<modality>/s<seed>`
  (gitignored) — the CLI defaults resolve there; never write corpora to session scratchpads.
- **Reference documents in [`../sensitive/`](../sensitive/) are classified.** They are
  gitignored — never commit, email, or upload them; cloud-LLM analysis of them is in-session
  only. Anything a cloud model sees must be synthetic or declassified.
