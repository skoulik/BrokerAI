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
- **One rule owns a checksummed identifier, both halves of it.** `ChecksumRule` matches once,
  calls the checksum once, and emits either the valid class or its `*_INVALID` shadow. Never
  split them again: they are supposed to *partition* the digit space, and when Presidio owned
  one half and our shadows the other they silently disagreed — a hyphen-grouped VALID
  TFN/ABN/ACN/Medicare matched neither and was detected by nothing (2026-08-09).
  `pii/core/checksums.py` is the single source of truth; `pii_eval/au.py` mirrors it so the
  corpus generator and the detector agree.
- **The context boost's constants are load-bearing.** +0.35, floored to 0.4, capped at 1.0,
  from a window BEFORE the match only. Every sub-threshold pattern (bare account numbers, PayID
  digit runs, the `context` invalid tier) exists only because that promotion exists, so changing
  a constant silently re-tunes all of them. Pinned in `tests/pii/core/test_engine.py`.
- **Keeping takes positive evidence; stripping takes none.** A detected value is replaced
  unless the keep list matches it (`entity_keep.py`, `data/entity_keep.txt`). Never invert this
  back into "strip only what looks private": the old rule needed a legal-form marker as
  evidence, and a fixed-width statement field printed `SK BUSINESS TRUST` as `SK BUSINESS TRUS`,
  destroying the evidence while keeping the identifying name — kept three times on one page
  (2026-08-11). A mangled fragment cannot fake presence on a list someone wrote down.
- **A label is evidence, not part of the value.** Labeled identifier patterns match the label
  as a LOOKBEHIND. A span covering "TFN: 123 456 782" keys the pseudonym map on a different
  string than a bare occurrence of the same TFN, forking one identifier into TFN_1 and TFN_2.
- **Layer 1 must not import torch — nor anything that does.** Nothing in the strip path may
  pull in spaCy/thinc/presidio (thinc imports real torch eagerly). This is not hygiene: the
  paddle-GPU wheel cannot share a Windows process with torch, and OCR runs in-process now that
  it does not have to. `ocr_paddle._engine` raises if torch is present, and
  `test_registry_policy.py` checks the import graph in a subprocess.
- **There is no layer 2, and no detector switch.** GLiNER2 was retired 2026-08-09 (GLiNER v1
  before it, 2026-07-13); both are in git history. Layer 0 — a local LLM over HTTP — is the
  only semantic detector, in every input mode. Do not re-add an NER model to the registry:
  `tests/pii/core/test_registry_policy.py` fails if anything there claims ADDRESS or
  DATE_OF_BIRTH, or claims PERSON without being the mechanical `JointNameRule`.
- **A strip entry point always takes a detector.** `strip_text` / `strip_csv` / `strip_image` /
  `strip_pdf` require one — layer 1 alone is the `--no-ner` regime retired 2026-07-15 as unsafe
  (its name leaks), and it must not be reachable by omitting an argument. `PiiPipeline.detect`
  stays public as a *layer*, which is what `merge_detections` consumes.
- **No standalone place-name detection.** A lone city/town name passes verbatim; `LOCATION` is
  not in `DEFAULT_STRIP_ENTITIES` or the placeholder map. Full addresses and suburb-postcode
  lines still strip, as layer-0 ADDRESS. Rationale in
  [core/ARCHITECTURE.md](core/ARCHITECTURE.md).
- **Detection and grounding are two model passes, never one.** `detect` names the values,
  `localize` asks where they are. Asking for both at once costs 7.4% recall (measured, 31
  pages); the split costs ~16 s/page because image prefill is cached. Never add `bbox_2d` to
  the detection prompt.
- **A model box is a search constraint, not paint geometry.** Layer 0's boxes are
  stochastically unsafe to paint (16% clip by >20 px) but reliable enough to say *which*
  occurrence a value is — painting tolerance is zero pixels, localization tolerance is half a
  word. `locator.py` paints OCR word boxes; the model's own box is painted only for the
  residue that matches no OCR text at all (a logo, a barcode), padded and counted separately.
  `--geometry vlm` exists as a comparison instrument only.
- **Fuzzy matching is permitted exactly where a box constrains the candidate set.** Inside a
  box, edit distance can only pick something in the right place; page-wide it would paint the
  WRONG region, so `--geometry ocr` stays at exact-or-squash. The confusion table in
  `fuzzy.py` is a *discount inside* the edit distance, never a gate in front of it — folding
  both sides through confusion classes fails on unlisted damage and on dropped characters.
- **A page is not the unit of truth.** Every page is READ before any page is REDACTED, so a
  value layer 0 names on page 1 and misses on page 4 strips on both. Do not restore a
  streaming per-page loop in `strip_pdf`: it is what made that leak invisible.
- **Cache the raster the model saw; never render a page twice.** The model's `bbox_2d` lives
  in the coordinate space of those exact pixels, and a second render only assumes it
  reproduces the first. The cache is PNG (lossless until the final embed), holds full
  unredacted pages, and must be unlinked per page and removed on the way out.
- **Grouping decides the class and the report, never recall.** Every constituent is searched
  independently, so a mis-grouping can only mislabel. Keep it that way — the moment a group's
  canonical form becomes the needle, a clustering bug becomes a mis-paint.
- **A group compares normalized and stores verbatim.** Distance runs case- and
  separator-folded (`SMITH JOHN` vs `Smith John` is 8 raw edits); the constituent's original
  text is what `locate_borrowed` searches for. Never let a normalized form become a needle.
- **The identifier confusion table is DERIVED, not listed.** `IDENTIFIER_CONFUSION_PAIRS` is
  the cross-class subset of `CONFUSION_PAIRS`. A digit read as a letter is damage; a digit
  read as another digit is a different account, and `1↔2`/`4↔8` are in the measured table.
  Deriving it is what stops the pending confusion-matrix refresh leaving a stale copy.
- **The group vote can un-redact, so it must stay auditable.** The elected class replaces
  every member's own in both directions (deliberate — a 10-to-1 majority for a company is a
  company). `EntityGroup.votes` must keep reaching the CLI report; a silent election is the
  failure mode.
- **A borrowed needle is bounded, and its fuzzy tier is guarded by a LENGTH FLOOR.** Both ends
  must fall on a word edge (exact matching has no length floor, so an unguarded `Wu` paints
  inside `Would`), and edit distance is admissible only from 8 squashed characters up — below
  that any budget of 1 matches a large fraction of a page. Fuzzy is allowed here, unlike in
  `locate_findings`, because borrowed placements do not COMPETE: nothing is consumed, so a
  spurious match is additive over-strip rather than a leak plus an over-strip. Do not carry
  that licence back to `locate_findings`.
- **Fuzzy borrowed matching is ADDITIVE, never a fallback tier.** A page carrying a value's
  full form exactly *and* a truncated form would otherwise find the exact one, skip fuzzy and
  leak the truncation — a real specimen, not a hypothesis (`SK BUSINESS TRUS`, 2026-08-11).
- **An identifier's budget must stay below 2.0.** `fuzzy.identifier_substitution_cost` prices
  a digit read as another digit at infinity, but edit distance routes around that with a
  delete plus an insert for exactly 2.0 — so the prohibition only bites if no budget can pay
  the detour. Raise `_BORROWED_FUZZY_IDENTIFIER_CAP` and one account number starts matching
  another that differs by a single digit.
- **An empty layer-0 answer is three situations, and only one is a clean page.** Every reply
  goes through `vlm.read_response`, which reads `finish_reason`: a closed array is clean, an
  open one is `truncated` or `malformed`, and both are carried to the caller on `Incomplete`
  (never merged — with a grammar on, `malformed` means the server ignored it). Do not restore
  a `detect()` that returns a bare list: layer 0 is the only detector for PERSON / ADDRESS /
  ORGANIZATION, so a page read from a cut-off answer loses exactly those and still looks
  plausibly redacted.
- **A truncated answer is salvaged, and only there do identical entries collapse.** The
  elements before the cut are real detections — 38 of them on the specimen, against 0 before.
  The collapse is confined to that path because a loop's occurrence counts are worthless while
  a normal page's are how repeats get boxed; genuine repeats survive anywhere because
  `locate_borrowed` finds them mechanically.
- **A grammar constrains FORM, not LENGTH.** Do not close the truncation path on the grounds
  that output is now grammar-guided: measured, the loop specimen still hits `max_tokens` with
  the grammar on. And write a literal backslash in a GBNF character class as `\x5C` —
  llama.cpp b10326 rejects `\\` there, so json.gbnf's `string` rule cannot be pasted verbatim.
- **A detected value that cannot be located is a leak.** Unlocatable findings must keep
  warning loudly AND stay counted on `ImageStripResult.unlocated` / `PdfPageResult.unlocated`
  / `TextStripResult.unlocated` — a warning alone is deduplicated by Python's default filter
  when a later page or document repeats it. Same for `box_geometry`, which is a weaker
  redaction rather than none.
- **On the text path the model names values; WE find the occurrences.** `text_llm`'s prompt
  asks for each DISTINCT value once, and `locator.locate_in_text` marks every occurrence of
  it. Do not "fix" the prompt to enumerate occurrences: finding a known string in a known
  string is exact and free, while a model's enumeration costs output budget and decays with
  document length. The vision prompt asks for every occurrence only because each one needs
  its own box.
- **The two layer-0 prompts are separate strings but ONE class vocabulary.** `vlm.PROMPT` is
  frozen at the wording that was measured, so `text_llm.PROMPT` is a copy rather than a
  splice — but both must name exactly the keys of `vlm.TYPE_MAP`, or a class the model emits
  silently collapses to `IDENTIFIER_GENERIC`. Pinned by a test in `test_text_llm.py`.
- **Squash matching has a length floor; exact matching must not.** Squash collapses
  separators, so a short needle matches across word boundaries — tolerable on a page where
  the model's box constrains it, unbounded in page-wide text. Exact matching keeps no floor:
  real 2-char surnames (Wu, Ng) and 3-char organizations (NAB, ANZ) exist.
- **The OCR perception layer (`OcrPage`) carries no character offsets.** Offsets live only in
  the linearization source map (`RecognizerInput` / `linearize`) — an offset is a
  (page, assembly) property; baking one onto a line ties perception to one assembly. Keep the
  anti-silent-leak rule (record `(start,end,box)` at construction, never re-derive from
  lengths) on the source map.
- **An OCR line is never dropped.** Every row carrying words becomes an `OcrLine`, with no
  filtering or scoring step in between. A dropped line is unredacted PII.
- **`_rows` visual banding is load-bearing.** It is what puts a label and its value from two
  side-by-side detection regions onto ONE assembled line, which is how context promotion reaches
  a value in a column beside its own label. Keep the x-overlap guard (two regions sharing an
  x-column are stacked lines, not one row).
- **A line box contains its glyph ink.** Build `OcrLine.box` only through
  `ocr_page._line_box` (word boxes ∪ their region boxes) — engine word boxes are inset from the
  ink, so a word-box union slices the first and last glyph.
- **Reach OCR only through `get_ocr_page`** (worker on the GPU paddle wheel, in-process on CPU)
  — never import torch into a paddle-GPU process. Models live under `models/paddlex`.
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
