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
  from BEFORE the match only. Every sub-threshold pattern (bare account numbers, PayID digit
  runs, the `context` invalid tier) exists only because that promotion exists, so changing a
  constant silently re-tunes all of them. Pinned in `tests/pii/core/test_engine.py`.
- **A label reaches a value by being near it ON THE PAGE, never by character distance.** A
  `Layout` supplies the candidate's neighbourhoods and the engine knows nothing else about where
  text sits; 60 characters of assembled string modelled no field, line, column or ownership, and
  typed a print-batch reference as a bank account off a credit card's label (2026-08-14).
- **The left band is a word COUNT, not a distance.** Measured: the true label sat 462 px from
  its value and the false promoter 748 px from its own, so no threshold separates them — word
  counts do. It is derived per rule from that rule's own longest label; never make it global.
- **Every BSB grouping needs BOTH a lookahead in `AuBsbRule` and its mirrored lookbehind in
  `AuAccountNumberRule`.** A value's own leading words count against its band, so with no
  combined-form match the account half is further from the label than the BSB half and the
  field strips in HALF — which reads as redacted. CommBank's `06 3118 10587788` leaked its
  account on all three pages of a reference statement (2026-08-18). Widen the grouping
  vocabulary, never `FILLER_ALLOWANCE`: that is a global precision trade paid for one form.
- **Between bands the NEAREST label wins**, edge to edge in line heights — the same rule already
  used within a band, where the closest label is the one that introduces the value. A fixed
  left-then-above order lets a bogus left label outrank a good one directly overhead.
- **A match that straddles a column is not one value.** `linearize` joins words with ONE space,
  so every separator class in `recognizers.py` is blind to a column gap on the OCR path;
  `Layout.contiguous` is what sees it. Never re-derive that guard from the assembled string.
- **The `above` band selects detection REGIONS, not words.** Per-word x-overlap contributes only
  the word directly overhead, so `Account Number` above a short value degrades to `Account` and
  a wrapped licence label never assembles.
- **STRICT gates, NEAR boosts.** A STRICT pattern is dropped when unattached and keeps its
  declared score when attached — that is what made converting nine label lookbeheads
  score-neutral, and it means a STRICT pattern must declare a score above threshold.
- **A label spelling is a stem that must start at a word boundary**, and the gap to the value is
  measured from the end of the label's own WORD. Wrap a spelling too short to be a safe stem in
  `labels.Exact` (`ac` would otherwise match inside `across`).
- **`rule.detect()` is half a labelled rule.** The label is `context` and attachment is the
  Analyzer's, so test labelled patterns through `Analyzer.analyze`, never against `detect`
  alone — the latter reports a labelled candidate with no label anywhere near it.
- **Layer 1 now depends on OCR geometry**, where it used to be pure text: a bad line box is a
  *detection* bug and not only a paint bug. Accepted as the price of the model being right.
- **Keeping takes positive evidence; stripping takes none.** A detected value is replaced
  unless the keep list matches it (`entity_keep.py`, `data/entity_keep.txt`). Never invert this
  back into "strip only what looks private": the old rule needed a legal-form marker as
  evidence, and a fixed-width statement field printed `SK BUSINESS TRUST` as `SK BUSINESS TRUS`,
  destroying the evidence while keeping the identifying name — kept three times on one page
  (2026-08-11). A mangled fragment cannot fake presence on a list someone wrote down.
- **A label is evidence, not part of the value.** A labelled pattern matches the value's SHAPE
  and nothing else; the label lives in `context` and is attached by the engine (it was a regex
  lookbehind until 2026-08-14, same rule). A span covering "TFN: 123 456 782" keys the pseudonym
  map on a different string than a bare occurrence, forking one identifier into TFN_1 and TFN_2.
- **Layer 1 must not import torch — nor anything that does.** Nothing in the strip path may
  pull in spaCy/thinc/presidio (thinc imports real torch eagerly). This is not hygiene: the
  paddle-GPU wheel cannot share a Windows process with torch, and OCR runs in-process now that
  it does not have to. `ocr_paddle._engine` raises if torch is present, and
  `test_registry_policy.py` checks the import graph in a subprocess.
- **There is no layer 2, and no detector switch.** GLiNER2 was retired 2026-08-09 (GLiNER v1
  before it, 2026-07-13); both are in git history. Layer 0 — a local LLM over HTTP — is the
  only semantic detector, in every input mode. Do not re-add an NER model to the registry:
  `tests/pii/core/test_registry_policy.py` fails if anything there claims ADDRESS or
  DATE_OF_BIRTH or PERSON at all — a joint name is derived from people already known
  (`pii/core/derived.py`, layer 1 pass 2), not guessed from a shape.
- **A strip entry point always takes a detector.** `strip_text` / `strip_csv` / `strip_image` /
  `strip_pdf` require one, with no default. `PiiPipeline.detect` stays public as a *layer*,
  which is what `merge_detections` consumes.
- **Patterns-only is reachable only by asking for it, and never silently.** `--layer0 off`
  passes a `NullDetector`; that is the ONLY route. Layer 1 alone leaves names and addresses on
  the page, so what made the retired `--no-ner` unsafe (2026-07-15) was its silence, not its
  existence — keep both guards: the entry points still demand a detector object, and the run
  warns ungated by `--report`. Never add a `layer0=False` parameter or a defaulted detector:
  that is the omission the rule forbids.
- **A debug artifact that would be blank is not written.** The `layer-0` and `locate` overlays
  and the findings listing all come from `PageDebug.placements`, so `--layer0 off` would render
  them as unannotated copies of the ORIGINAL page — and the whole debug set is near-PII. Two
  extra unredacted copies of the document carrying no diagnostics is a liability, so
  `_debug_spec` drops them and says so. Not the same as the empty `layer-0` overlay under
  `--geometry ocr`, where layer 0 ran, `locate` is populated, and the emptiness is information.
- **No standalone place-name detection.** A lone city/town name passes verbatim; `LOCATION` is
  not in `DEFAULT_STRIP_ENTITIES` or the placeholder map. Full addresses and suburb-postcode
  lines still strip, as layer-0 ADDRESS. Rationale in
  [core/ARCHITECTURE.md](core/ARCHITECTURE.md).
- **Detection and grounding are two model passes, never one.** `detect` names the values,
  `localize` asks where they are. Asking for both at once costs 7.4% recall (measured, 31
  pages); the split is near-free only while the server can restore a post-image context
  checkpoint (patched llama-server, `-ctxcp > 0`), and doubles prefill per page when it cannot.
  Never add `bbox_2d` to the detection prompt.
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
- **One value is not always one span, and the page string is why.** `_rows` bands a page
  VISUALLY, so two cards side by side share every band and a value that WRAPS inside one
  column has the other card's row-mate spliced between its halves — 40 characters of an expiry
  date between `24 Stacey Dr` and `Carrickalinga SA 5204`, reachable by no contiguous search
  (2026-08-13). Keep `Placement.spans` a tuple and `Detection.full_value` as the pseudonym key:
  without the second, one address forks into ADDRESS_1 and ADDRESS_2.
- **The wrapped search is driven by the NEEDLE, never by a scan of the covered words.** A run
  only ever starts where the needle's next character does, which is what lets a line be offered
  whole and picked from safely. A flat box-local assembly was built first and cannot work: with
  outer-only slack a box that clips the word ending the value's FIRST line leaves it interior
  and unreachable, and with per-line slack the neighbouring column lands in the seam. Measured
  — the flat version resolved a clipped box to `24 Stacey` + `Carrickalinga SA`, a partial
  paint, which is a leak where tier 3 had at least over-painted.
- **A wrapped borrowed match is guarded by the COLUMN, and stays squash-exact.** No box means
  no anchor, so pieces must sit on consecutive lines and share an x-column — remove that guard
  and `24 Stacey Dr` in the left card joins `Carrickalinga SA 5204` in the right. Do not extend
  the fuzzy tier across a wrap there: unanchored plus wrapped plus fuzzy is three liberties at
  once.
- **Every piece of a wrapped match must earn a character of the needle.** A punctuation-only
  OCR word squashes to nothing and `startswith("")` is true everywhere, so it joins any piece
  at any position for free — and a piece of one such word is a "proper prefix" that carries the
  walk to the next line, where the real value finishes the match. Shipped for a few hours in
  the first cut: every needle claimed the stray `-` or `?` on the line above it, painted with
  its own placeholder (2026-08-13).
- **Being in the box is the positional agreement; how MUCH of it a candidate fills is not.**
  `_place` ranks free candidates by kind, then edit distance, then overlap. Ranking by overlap
  magnitude first hands a clipped box to whichever candidate fits inside it — a truncation of
  the value beating the whole of it.
- **A page is not the unit of truth.** Every page is READ before any page is REDACTED, so a
  value layer 0 names on page 1 and misses on page 4 strips on both. Do not restore a
  streaming per-page loop in `strip_pdf`: it is what made that leak invisible.
- **BOTH layers are read in sweep 1, and the needle list is complete before any page is
  painted.** Layer 1 scores per OCCURRENCE — the boost comes from the neighbourhood that
  occurrence sits in — so one printing of a value clears the threshold and the next does not:
  four `Pc 432103` on one page, one painted (2026-08-18). Its detections are needles now
  (`image_mode.layer1_needles`). Do not move that call back into sweep 2: there it runs per
  page, after the needle list is frozen, which is why no layer-1 hit could propagate.
- **A layer-1 needle may ADD coverage and must never re-classify or reach the fuzzy tier.**
  Two separate guards, both load-bearing. It carries `TEXTUAL_TIERS` because a layer-1 span's
  extent is often an artifact (`AtfTailRule` matches the rest of the line) — as a fuzzy needle
  `ATF SK MANAGEMENT` ate `Name\nSK MANAGEMENT` across a line break. And it is dropped before
  the merge wherever layer 1 already spoke on that page, or its score of 1.0 wins
  `_merge_overlaps` and re-types the page's own verdict — one licence number labelled `AFSL`
  at one printing and `Credit Licence` at the other collapsed onto one class. Layer-0 needles
  get neither guard, deliberately: every tier, and their class outranks layer 1's.
- **Layer-1 needles are not group members.** Grouping decides the class and the report, never
  recall, so a pattern hit casts no vote and adds no group row — the same number is emitted as
  both `AU_AFSL` and `AU_CREDIT_LICENCE` on purpose. But both needle sets go through ONE
  `locate_borrowed` call: the tiers run in phases across all needles, and splitting the call
  would let a layer-0 fuzzy match take a span from a layer-1 exact one.
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
- **The text layer is a repair source constrained by OCR geometry, never a detection source.**
  Only a word the OCR already saw is repaired; a word it missed is never added. Same shape as
  "a model box is a search constraint, not paint geometry" — an untrusted source is admissible
  exactly where a trusted one pins it down, which is what keeps this from reopening the
  hidden-text-layer leak class that made PDFs be treated as images.
- **A repair keeps the same words in the same order**, with the same `region_box`, and runs
  BEFORE `linearize` — that is what lets offsets, painting and the pseudonym map be built from
  repaired words with no remapping anywhere. A gate that lets a token GROW (the text layer's
  hundred leader dots) is changing extent, not reading, and is rejected.
- **Characters and boxes are corrected against DIFFERENT evidence, on separate gates.** A
  paddle word box is an estimate and a text-layer box is where the renderer drew the glyphs, so
  a confirmed pair lends its box too (`_lendable`) — measured, that took the reference corpus
  from six partly-painted values to none. Never gate lending on horizontal overlap: that is the
  identity evidence for a CHARACTER substitution, and on the words worth relocating it fails
  (`244616.` overlaps its own true box by 0.25). Geometry cannot be both the evidence and the
  thing being corrected; identity comes from the alignment and the reading, and the guards are
  the two axes the drift does not break — vertical agreement, and staying inside the word's own
  detection region.
- **A merge lends its union box although it repairs no characters.** Which characters belong
  where is unestablished, the extent is not. Leaving merges on drifted coordinates while their
  neighbours move puts two coordinate systems on one line, and a lent box starting inside an
  unlent one over-painted a neighbouring word by 217 px (2026-08-18).
- **The correspondence is an ALIGNMENT, never a nearest-box pairing.** Independent per-word
  overlap drifts by one across a whole line wherever OCR boxes are interpolated — eight
  consecutive wrong pairs between two IDENTICAL word sequences on the first page measured
  (2026-08-18). Geometry buckets text words onto a line; order-preserving alignment picks the
  partner within it.
- **The page-level repair guard counts READING agreement alone.** A pair refused for its
  geometry or its extent is a good correspondence we decline to act on; counting those against
  the text layer disables repair on a page whose only problem is an interpolated box.
- **A font is render-only and must never reach a detection decision** — we deliberately
  distrust the text layer, and its idea of the typeface is the least load-bearing thing it
  carries. Do not resolve the document's EMBEDDED font either: 8 of 11 fonts on one reference
  page are Identity-H CID subsets that render a placeholder as zero-height nothing, which is a
  filled box with an invisible label.
- **`_rows` visual banding is load-bearing, now for a different reason.** It defines what "the
  same line" MEANS, and the left attachment band is a line — so a label beside its value in
  another column of the same printed row is still reachable, while the neighbouring column is
  held off by the word count rather than by the banding. Keep the x-overlap guard (two regions
  sharing an x-column are stacked lines, not one row).
- **A line box contains its glyph ink.** Build `OcrLine.box` only through
  `ocr_page._line_box` (word boxes ∪ their region boxes) — engine word boxes are inset from the
  ink, so a word-box union slices the first and last glyph.
- **A rotated line is never banded with anything.** A page-edge stripe is 275-865 px tall, so a
  y-centre band around it reaches a third of the page: the enquiries phone and the stripe of the
  reference statement assembled as one line inside one 1488x275 rectangle (2026-08-18). Band the
  rotated regions apart and merge them back by y-centre — skipping them in the one pass lets a
  stripe split the row it crosses.
- **Rotation is decided by GEOMETRY, direction by RECOGNITION** — a region twice as tall as it is
  wide is a rotated line (measured: no upright region in the corpus exceeds 1.5:1), and which way
  it reads is settled by reading the crop both ways and keeping the better score. Never make the
  banding wait on a recognizer: a stripe that reads badly still wrecks the row it lands in.
- **`rotation` is degrees COUNTER-CLOCKWISE the text is turned from upright**, everywhere it
  appears — `OcrLine`/`OcrWord`, `PlacedWord`, `TextWord`, `Segment`. 90 is the left margin
  reading bottom-to-top, 270 the right reading top-to-bottom.
- **Measure along the line, never along x.** `ocr.reading_extent` / `cross_extent` /
  `_oriented_box` are the only place that knows what those axes are; a gap, a paint run, a
  neighbour midpoint, a line "height" and a bucket all go through them. Two words of a stripe
  share `left` exactly, so an x-gap between them is always 0 — `contiguous` waved every stripe
  span through before this.
- **A text word may only pair with an OCR line of the same rotation.** A stripe crosses the
  y-range of a third of the page's lines; without the match it takes their text words and leaves
  those lines unrepaired. The same match keeps a page-wide footer from being `above` every stripe
  it crosses and lending it a label.
- **Reach OCR only through `get_ocr_page`** — in-process on either paddle wheel since the
  worker subprocess went (2026-08-09). Never import torch into a paddle-GPU process; that is
  what the worker used to isolate and what `ocr_paddle._engine`'s guard now enforces alone.
  Models live under `models/paddlex`.
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
