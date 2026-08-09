# TODO — PII engine (core)

All open engine tasks, with full working detail. The activity overview and evaluation-tier
plan are in [ROADMAP.md](ROADMAP.md); completed tasks and their engineering records are in
[DONE.md](DONE.md); design decisions (the *why*) in [ARCHITECTURE.md](ARCHITECTURE.md).
Front-end tasks live with their component: [../cli/TODO.md](../cli/TODO.md),
[../gui/TODO.md](../gui/TODO.md).

Grouped by theme. Read the direction note below first — the 2026-08-09 segmenter retirement
closed a whole section of this file, and the ordering that preceded it is history (records in
DONE.md and reports/).

## Direction — 2026-08-09, read before picking anything up below

The segmenter is retired and layer 0 is the default detector (record in
[DONE.md](DONE.md), design in [ARCHITECTURE.md](ARCHITECTURE.md)). What that settled:

- **The layout/perception layer is gone** — with it, the whole "OCR perception / linearization"
  programme (orphan clustering, trial linearizations, table-cell structure, layout thresholds,
  region detection) and the layout half of issue #8a. The VLM reads spatial structure natively.
- **Layer 1 stays**, in the narrowed role it now actually has: classifier, checksum validator,
  and deterministic recall floor over layer 0's findings (`merge_detections`). Whether that
  means presidio or our own code is still open — note it *inverts* the "Stop duplicating
  Presidio's checksum arithmetic" item below, whose proposed fix is to delegate *to* presidio.
- **PaddleOCR stays** and supplies paint geometry: VLM boxes are stochastically unreliable to
  paint, so `--geometry vlm` is a comparison instrument only. They *are* used in production
  (`--geometry hybrid`, the default) as a search constraint, which tolerates the error they
  actually have — see [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0".
- **GLiNER2 (and spaCy with it) are the next retirement candidate** — beaten on this corpus at
  things they structurally cannot do — but the decision has NOT been taken. They still run as
  part of the layer-1 union, which preserves cross-layer disagreement as a signal the tier-3
  metrics plan wants. The "Experiments — GLiNER2 tuning" section below is on hold behind that
  decision, not active work.

**Now the top open risk: throughput.** ~3 min/page is a research profile, so the serving /
quantization item below is what stands between this and a usable product.

## Next up — image/PDF path

- [x] ~~**Hybrids that deliberately use the VLM's own boxes**~~ — **DONE 2026-08-09** as
      `--geometry hybrid` (the new default): two-pass detect-then-localize, `locator.py`
      candidate scoring with the box as a search constraint, `fuzzy.py` confusion-weighted
      edit distance, and the padded model box as tier 3. It landed larger than the sketch
      because the box turned out to be worth more as a *disambiguator* than as a fallback.
      Record in [DONE.md](DONE.md), design in [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0".

- [ ] **A tier-3 paint does not suppress a later identical finding, so the "NOT redacted"
      line can cry wolf** (found in the first hybrid run, 2026-08-09). `locate_findings`
      recognizes a redundant finding by containment in an already-claimed *char span*; a
      box-only placement has no span, so a second finding of the same value with no box of its
      own falls through to `unlocated` and is reported as unredacted even though the pixels
      were painted. Observed on the insurance page: the same address appeared in both the
      tier-3 list and the unlocated list, and re-OCR of the output confirmed **nothing
      leaked**. The error is in the safe direction, but it is in the one line an operator has
      to be able to trust, so it should not stand. The fix needs a semantics decision first:
      should a painted box suppress a later identical value anywhere on the page (wrong if the
      two occurrences are genuinely in different places), or should the report merely
      distinguish "unplaced, but an identical value was painted elsewhere" from "unplaced,
      nothing painted"? The second is honest and cheap; prefer it unless the first is argued.

- [ ] **A value wrapped across lines/columns falls to tier 3 instead of matching** (same run).
      The model returned an address and a vehicle description as single long strings; both
      landed on tier 3, i.e. painted from the model's box rather than exact word boxes, even
      though the text is on the page and OCR read it. The likely cause is that the linearized
      page interleaves other column content between the value's parts, so no *contiguous*
      word window matches — `_fuzzy_windows` only considers contiguous slices of the source
      map, by design (a non-contiguous window would let a span swallow unrelated text). Worth
      confirming against `pii debug ocr` on that page before choosing a fix. Options: allow a
      window to skip a bounded number of intervening words when the skipped text is itself
      part of no other finding; or split a long layer-0 value on its own line breaks and
      locate the pieces independently, which fits the "one box per line" painting model
      already in use. Recall is not lost either way — this is exact geometry vs approximate.

- [ ] **Measure the hybrid against the `ocr` baseline on the 31-page real corpus** — the
      A/B the design was argued from but has NOT been run: `python -m pii_eval score
      --modality pdf -c pii_eval/corpora/real/1 --geometry hybrid` vs `--geometry ocr`, same
      detector and same locator, boxes as the only variable. Three numbers decide whether
      tier 3 earns its complexity or the disambiguation is carrying the whole change:
      (a) the size of the tier-3 residue, (b) how many values change span between the two
      runs (silent mis-locations the box fixed), (c) the throughput cost of pass 2 against
      the predicted ~16 s/page. Until this runs, the hybrid is reasoned-for, not measured.

- [ ] Belt-and-braces text-layer scan (*decide later*, split out of the PDF mode task when it
      shipped 2026-07-18): additionally scan any existing source text layer to catch text the
      OCR misses (detection only — output still comes from pixels). Same machinery as the
      hidden-text report below.
- [ ] Output PDF encoding knobs (deferred from PDF mode, Sergei 2026-07-18): processing is
      lossless end-to-end and only the final embed is lossy (JPEG q90, `pii/core/pdf_mode.py`).
      Make the encoding configurable later — lossless/PNG option, quality, maybe target DPI
      of the embed as distinct from the analysis render.
- [ ] **Layered pseudonym maps** (Sergei, 2026-07-18): maps are per-document by default
      (CLI derives `<input>.pii_map.json`; decision recorded in cli/ARCHITECTURE.md).
      Extension: a per-document map *plus* a global map, and perhaps a per-group map —
      what a "group" is gets defined if/when we get there (a submission bundle is the
      motivating example). Solves cross-document placeholder consistency (today two
      statements of the same person each say PERSON_1 independently); interacts with the
      pseudonym-consistency scoring task in Evaluation below and with entity-variant
      matching (a global map raises the variant-forking stakes).
- [ ] **Hidden-text detection & report** (idea, Sergei 2026-07-18; distant tier): the
      real corpus holds a live specimen (d04.p2) — an account number in ordinary black
      text with a white rectangle drawn over it, glyph fringes peeking past the
      rectangle's edges (and a second, fully visible copy of the same number lower on
      the page). Pixels-first output already destroys hidden source text by
      construction, so the feature is *reporting*, not redaction: detect text in the
      source PDF that does not survive to the render — covered by later-drawn shapes,
      fill matching the background, invisible render mode, clipped or zero-size — and
      report the findings (locations/classes) so the operator knows the source
      carries concealed identifying content. Kin of the belt-and-braces text-layer
      scan in the PDFs-as-images item above (same machinery: diff text-layer strings
      against what OCR reads off the rendered pixels).
- [ ] Statement tables via the image path (the remaining half of the transaction-list task —
      CSV mode shipped 2026-07-12): tabular statements arrive as scans/PDF pages, not CSVs;
      verify the OCR path handles table layouts (row/column integrity, amounts kept intact)
      on the reference documents.
- [ ] Barcode masking: mailing barcodes on statements (Australia Post 4-state, and 1-D codes)
      encode the delivery address/customer ref — text-based detection can't see them, so
      detect and paint over barcode regions in the image pass (observed on several of the
      reference examples)
- [ ] 0↔O post-processing heuristic (idea, Sergei 2026-07-17): nearly all of
      PP-OCRv6_medium's residual digit risk is the single 0↔O/o confusion class (909 of
      its top confusions; digit→digit subs at 0.01/10k chars). A context-aware
      normalization on identifier-shaped tokens — inside digit-dominated runs, map O/o→0
      (and optionally l/I→1) before the pattern recognizers/checksums — could close it
      entirely; the reverse direction (0→O inside alpha words) guards merchant names.
      Measure with the fidelity scorer + leak gate; interacts with the `_CONFUSION`
      refresh task below.
- [ ] Refresh the `_CONFUSION` table in `pii_eval/score_image.py` from the measured
      confusion matrix (ocr-report sweep, 2026-07-17 DONE record): folklore pairs missed
      `0->@` (the top pair, Consolas slashed zero), `J->3`, `1->2`, `4->8`, `W->H`; decide
      per-pair whether to widen the squash classes (over-merging is recall-safe — it can
      only over-report leaks). Re-run the image-tier gate after.
- [x] ~~OCR engine choice — PaddleOCR vs a one-pass VLM~~ — **decided 2026-08-09: both, in
      different roles.** The VLM detects, PaddleOCR supplies geometry (and remains the eval
      harness's independent read-back instrument). Record in [DONE.md](DONE.md).
- [x] ~~**One-pass VLM pipeline**~~ — **DONE 2026-08-08**, shipped as layer 0
      (`--detector vlm`). Record in [DONE.md](DONE.md), design in
      [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0", evidence in
      [reports/2026-08-08-vlm-oneshot-qwen36.md](reports/2026-08-08-vlm-oneshot-qwen36.md).
      Verdict: detection excellent, grounding stochastically unreliable, so **PaddleOCR stays
      and supplies geometry**. The follow-ups it spawned are the next three items.

- [x] ~~**Layer-1 refinement of VLM findings**~~ — **DONE 2026-08-09** as
      `PiiPipeline.merge_detections` + a three-tier `_rank`. Record in [DONE.md](DONE.md),
      design in [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0".
      *Not yet measured on the corpus* — the refinement is unit-tested but no leak-gate run
      has been made against it, so the first `pii_eval score --modality pdf` on the real
      corpus is the outstanding validation.

- [ ] **Serving / llama.cpp tuning job — now also owns page-level concurrency** (scoped
      2026-08-08; re-scoped 2026-08-09 after the first hybrid run measured the two-pass cost
      for real. Sergei: combine with the conveyor idea and postpone.)
      ~176 s/page at Q8_0 was already a research profile rather than a product one — a 50-page
      bundle is ~2.5 h — and **the hybrid default doubles it**. Time splits **~130 s prefill
      (image ingestion, 74%)** and **~45 s decode (11 tok/s against a ~14 tok/s memory-bound
      ceiling)**, so a lever only matters if it attacks the right half. Already tried and
      rejected: `-fa on` and `-ub 2048` gave *no* improvement (73 vs 80 tok/s prefill, decode
      unchanged). Battery throttling halves everything — check `pmset -g ps` before trusting
      any number.

      **Measured 2026-08-09, and it invalidates a premise this design was argued from: the
      image prefill is NOT reused between the two passes.** Four requests over two pages of
      `116832820_7_Insurance_Certificate`, b10326, `-np 1`, `--image-max-tokens 16384`, AC
      power:

      | page / pass | prefill | tokens | decode | out |
      |---|---|---|---|---|
      | p1 detect   | 112.1 s | 8990 | 3.1 s | 35 |
      | p1 localize | 110.6 s | 8790 | 7.2 s | 80 |
      | p2 detect   | 112.1 s | 8990 | 46.0 s | 504 |
      | p2 localize | 112.3 s | 9006 | 74.2 s | 812 |

      The report's "additional passes on the same page cost ~16 s, not 130 s" **did not
      reproduce** — pass 2 re-encodes the image in full, so the hybrid costs ~2× per page
      rather than the ~+9% the design assumed (p2: 345 s against 158 s for values-only).
      No `reusing`/prefix-match line appears in the server log. **Sergei's hypothesis
      (2026-08-09), and the most likely one: it is the changed prompt.** Pass 2 sends the same
      image but different text, and if the multimodal cache path only reuses on a whole-prompt
      match rather than a prefix match, any wording change forces a full re-encode — which is
      exactly what the timings show. One cheap experiment discriminates it: send the same image
      twice with an *identical* prompt. Fast second request ⇒ whole-prompt matching, and the
      fix has to make pass 2 a prefix extension of pass 1 (or drop to `/completion` where the
      token sequence is ours to control) rather than a different instruction after the same
      image. Slow ⇒ image chunks are not cached at all and the lever is elsewhere.
      Also worth trying, cheaply: `--cache-prompt` is enabled by default but `--cache-reuse`
      defaults to **0** (`--cache-reuse 256`); and a newer llama.cpp than b10326. **Until this is settled, `--geometry hybrid`
      buys its correctness at 2× throughput, and whether it deserves to stay the default is an
      open question, not a settled one.** If reuse cannot be recovered, single-pass boxes
      (−7.4% recall, 1× cost) becomes a live option again and the three-way trade should be
      re-argued rather than inherited.

      **Page-level concurrency (Sergei, 2026-08-09).** A conveyor over consecutive pages is
      only worth building against the model, not around it: OCR + locate + layer 1 + paint are
      ~5% of a page, so overlapping just those caps out at single digits. What is worth having
      is running whole pages in parallel — but the standing constraint is that `-np > 1`
      batches sequences into one decode call and thereby breaks the reproducibility the gate
      depends on (item 4 below). Sergei's position is that sessions can run in parallel with
      determinism *and* cache intact, at a cost that is only memory. The version of that which
      is clearly true is **N separate server processes, each `-np 1`** — no shared batch, so
      per-request determinism is untouched, and the cache is per process. That is blocked on
      memory today (2 × 28.6 GB does not fit in 64 GB) which makes it **downstream of the
      quantization item**: at Q4 (~15 GB) two servers fit and page-level parallelism becomes
      free of the determinism objection. Whether one process with `-np N` can also keep
      determinism is the open question — test it the way `-np 1` was qualified in the first
      place: three identical runs of the same page under concurrent load, diffed for
      byte-identical finding sets. Note the prefill-reuse question above interacts: if pass 2
      is ever made cheap by caching, the two passes of a page must stay adjacent on the same
      slot, and a scheduler that interleaves pages would evict exactly the cache that made it
      cheap.

      Run in this order, each measured against the frozen baseline (**445 findings / 350
      distinct values over 31 pages**, values mode, which needs no geometry oracle):

      1. **Lower `--image-max-tokens`** (now 16384). Attacks the dominant 74%; roughly 2×. The
         budget was raised only to fix *box* precision and production takes no boxes, so its
         justification is gone. Risk — the one that made Sergei decline it earlier: image tokens
         set the spatial resolution of the whole perception, not just coordinates, so **recall
         on small print may degrade**. Sweep the budget, watch recall, keep the knee.
      2. **Q5/Q4 instead of Q8_0** (28.6 GB). Decode is memory-bound, so ~2× on that half, plus
         faster load. Q8 was chosen deliberately so a negative capability result could not be
         blamed on quantization; that job is done. Re-score the two hand-verified pages to
         confirm detection quality holds.
      3. **Qwen3.6-35B-A3B** (3B active vs 27B). Structurally the biggest lever and a candidate
         we wanted to try anyway; needs its own capability check, not just a speed check.
      4. **Multi-slot batching** (`-np > 1`, Sergei's question). Decode should scale near
         linearly with batch (weights read once for several sequences); prefill should not, being
         compute-bound — so theory caps the win at ~20–25%. Worth measuring anyway because
         80 tok/s prefill is suspiciously low for an M1 Max and may have headroom the theory
         does not predict. **Cost: it breaks determinism** — precisely what disqualified Surya
         as a gate — so it must be opt-in, with eval/gate runs pinned to `-np 1`. KV cache per
         slot is affordable if `-c` drops (we use ~9k of 32k).
      5. **Recover prefill reuse between the two passes** (see the measurement above). Worth
         up to ~50% of the hybrid's cost on its own, and it is the only item here that is a
         *correction* rather than an optimization — the design's throughput argument assumed
         it already worked.
      6. **N parallel single-slot servers + a page conveyor**, once 2 makes them fit. The
         determinism-safe form of page-level concurrency; scales with however many copies of
         the model fit in RAM.

      Ordering rationale: 1 attacks the biggest slice and costs nothing but a measurement; 2 is
      cheap and independent; 3 changes the model so it needs a quality re-check; 4 is late
      because it buys speed by giving up a property we rely on; 5 can jump the queue if the
      hybrid stays the default, since it is repairing an assumption rather than adding one;
      6 depends on 2 for its memory budget.
- [ ] **PaddleOCR-VL as an OCR backend** (researched 2026-07-25, *postponed* — Sergei: layout
      model first, no VLM for now). The 0.9B VLM ships in the installed `paddleocr` 3.7.0 as
      the `PaddleOCRVL` pipeline (v1.6 default, `vl_rec_backend: native` = paddle, so torch-free
      and worker-compatible; on sm_75 it would run fp32, no bf16). Two modes, only one usable:
      - Default **layout mode** returns `{block_bbox, block_label, block_content, block_order}`
        — block-level geometry ONLY, no line or word boxes. Unusable for painting: hiding one
        account number would mean painting a whole paragraph.
      - **Spotting mode** (`use_layout_detection=False, prompt_label="spotting"`, needs ≥1.5)
        emits text interleaved with `<LOC_n>` quads, post-processed into
        `{"rec_polys", "rec_texts"}` — the SAME keys `_result_lines` already consumes, so the
        adapter would be small. Per-page quantization is 1/1000 of the page (~2.3 px vertically
        on a 300 dpi A4).
      A per-detected-block spotting variant was sketched 2026-07-25 and **died with the layout
      backends** (2026-08-09) — it needed blocks to crop by. If revived, it would have to
      detect its own crops. Keep the Surya round-2 lessons in view either way: silent omission
      is the VLM failure mode that matters for redaction.
- [ ] Watch for **a PP-OCRv6 server tier** (none in paddlex 3.7.2 — tiny/small/medium only);
      if released, benchmark it with the ocr-report sweep against v6_medium — v6_medium
      already dominates, a v6_server should only strengthen it. Add it to `MODEL_TIERS`.
- [ ] Evaluate PaddleOCR knobs — **adapter, review, and clean-render bake-off DONE 2026-07-17**
      (DONE records + reports/2026-07-17-ocr-fidelity-tesseract-vs-paddleocr.md; verdict:
      v6_medium dominates, Tesseract retired). Remaining here: knobs
      tuning round (det thresholds `text_det_thresh`/`text_det_box_thresh`/
      `text_det_unclip_ratio` — the v5 merge lever, moot if v6 stays default;
      `text_det_limit_side_len` — also the VRAM cap; `text_rec_score_thresh`;
      `use_textline_orientation` for skewed scans) against the fidelity metric once the
      degradation tier exists.
- [ ] OCR preprocessing knobs: opt-in preprocessing chain for low-quality scans (bilateral
      filter / contrast stretch / adaptive threshold / rescale — see the harvested
      presidio-image-redactor chain in DONE.md). Preprocessed image feeds OCR only; painting
      stays on original pixels. Needs the eval degradation tier to measure.

- [ ] **Font traceback** (diagnostics-only): fill `OcrLine.font` from the PDF text layer
      (pymupdf `get_text("dict")` spans matched to line boxes) — `None` from any OCR engine.
      engine. Must never feed the strip decision (we deliberately distrust the text layer).
## Detection pipeline

- [ ] **User-editable keep-list ("do not strip") mechanism** (Sergei, 2026-07-18): a
      user-editable configuration file of do-not-strip entries, grouped per entity
      class plus a special class `any` (matches regardless of the detected class);
      entries support regular expressions. Operator workflow is the point: run the
      tool, spot an over-strip, add an entry, rerun — the list grows with use.
      Plugs in as a post-detection filter at the merged-spans level: a span whose
      text matches a keep entry for its class (or `any`) is dropped before painting.
      Keep-listing only ever *reduces* stripping — it is a precision lever whose leak
      risk is operator-owned; log every applied keep in the run output so reviews can
      see what was skipped. Design questions when picked up: match semantics (full
      span vs substring, case folding, whitespace normalization — OCR'd spans may not
      match a cleanly typed entry; consider matching through the OCR-confusion squash
      classes), file format/location, and the core/cli split (core takes a parsed
      keep-list object; the front-ends own loading the file). Dual coverage rule
      applies. Measured by the real-corpus over-strip axis.
- [ ] **Default keep-list content — institutional identities** (real-corpus review,
      Sergei 2026-07-18): the real corpus records bank/insurer identity blocks —
      branded org names, their ABNs, 13/1300/1800 numbers, corporate GPO-box
      addresses — as *keep* truth, and today's pipeline cannot discriminate them from
      customer PII, so the first eval runs will report them all as over-strips (that
      is the axis working, not a truth bug). Recovery = ship starter content for the
      keep-list mechanism above: (1) inbound business numbers as regex entries —
      13 xx xx / 13 xxxx / 1300 xxx xxx / 1800 xxx xxx are ACMA business-only
      allocations, never personal lines, so keep-listing them is zero leak risk;
      (2) major AU financial-institution identities as exact values (names + their
      public ABNs — e.g. ANZ = 11 005 357 522) — keyed by specific values, so the
      customer's own org name/ABN still strips. Mobile-shaped contact numbers inside
      branded blocks (d02's +61 437 968 251) stay syntactically undiscriminable —
      accepted over-strip unless the operator keep-lists the specific number.
- [ ] **Entity-variant identity matching — all classes** (config-toggled; real-corpus
      review, Sergei 2026-07-18, scope widened to all classes same day): the same
      real-world entity appears under variant surface forms within one document set,
      and `PseudonymMap` keys on the exact value, so each variant forks a distinct
      pseudonym — a downstream reader sees several people/addresses where there is
      one. Observed: PERSON — SERGEI KULIK / KULIK SERGEI / S KULIK (and plausibly
      KULIK S); ADDRESS — part forms "24 Stacey Dr" + "Carrickalinga SA 5204" on
      separate lines vs the joined "24 Stacey Dr, Carrickalinga SA 5204" on one line
      (d02). Post-processor: canonicalize values before pseudonym lookup, with
      per-class matching rules — names: case-insensitive token-set match, word-order
      invariance, initial↔full expansion (S ↔ SERGEI); addresses: part/whole
      containment; identifiers: formatting variants (spacing/hyphenation of the same
      digits). Feature requirements deferred — sketch only for now. Idea to keep:
      fuzzy matching should be *configurable and reviewable* — e.g. the tool proposes
      detected matches and the operator can allow some and disallow others, rather
      than silent all-or-nothing merging. Other recorded design questions: ambiguous
      initials (S KULIK when both Sergei and Svetlana Kulik exist), transitive merge
      chains, scope (per document vs per submission bundle — the same scope question
      as pseudonym-consistency scoring in Evaluation below), OCR-damaged variants.
      Ship with a configuration option to turn matching off entirely (privacy-side
      effect: matching *increases* linkability inside the output by design).
- [ ] **Reversed-caps person-name residual** ('REID THOMAS' / 'BROOKS ETHAN') — what
      remains after the 2026-07-15 fixes (full history in DONE.md: JointNameRecognizer
      → interference diagnosis → per-cell NER windows + PERSON coalescing + name-forms
      statistics doc). Current numbers on the fixed-n name-forms corpus: PERSON_REVERSED
      **70/72 across seeds 42+123** (was 20–75% noise on n=5); PERSON_COMMA 32/32,
      PERSON_PARTICLE 20/20, PERSON_MULTIWORD 20/20. The two residual leaks are pure
      **label competition on isolated caps junk lines**: person-only pass finds both
      name words ('REID'@0.86 + 'THOMAS'@0.85), but in the production schema
      ORGANIZATION claims the line ('REID THOMAS RENT'@0.86 org) and person collapses
      to 0.06–0.31 — windowing cannot help. Candidates: (1) labels-per-pass isolation
      (the experiment below owns exactly this; person-only rescues both observed
      leaks), (2) the person-names database layer below (deterministic recall floor),
      (3) LoRA fine-tune on statement-style forms. The known-person permutation pass
      idea is retired as primary (the interference it targeted is fixed at the window
      level) but remains viable belt-and-braces. When the residual closes, promote
      PERSON_REVERSED into pii_eval `build.CRITICAL` (PERSON_JOINT was promoted with
      the joint-form fix).
- [ ] **Person-names database layer** (Sergei, 2026-07-15) — if reversed/varied-name
      recall stays unsatisfactory, integrate a names database as a deterministic
      recall floor: match known given names/surnames (e.g. the `names-dataset`
      package, US SSA + AU census name lists) as tokens and emit PERSON candidates
      for adjacent known-name pairs regardless of word order — 'REID THOMAS' hits
      (Thomas = known given name, Reid = known surname) with no NER involved. Design
      questions when picked up: score/context policy (confident vs context-promoted),
      precision on merchant lines (MCDONALDS, HARVEY NORMAN are surname-shaped —
      probably require a known *given* name in the pair, not just surnames), and the
      overlap policy vs keep-ORGANIZATION spans. Sibling of the AU place-name
      gazetteer task (same trie/set-matching machinery, same fuzzy-budget idea).
- [ ] **Layer-3 local-LLM audit pass** — *contingent, not committed: the plan is to evaluate
      the tool end-to-end with layers 1+2 only, and build layer 3 only if those results prove
      unsatisfactory — see ROADMAP.md and ARCHITECTURE.md.* Design if built: "does this still
      contain anything identifying?" via llama-server; catches contextual identifiers NER
      can't see ("the borrower's wife, a dentist in Wagga Wagga"), including the bare place
      names given up when standalone place-name detection was retired.
- [ ] Overlaps merging algorithm — define and document. Interesting areas: how the weights are
      combined (max, average, bayesian/aposteriori), what if winning classes of overlaps
      do not agree, should we merge at all in some cases. Adjacent-span coalescing for
      fragmented multi-part addresses belongs here too.
      Input (2026-07-14, image-demo wart 2): a strip-type span nested inside a
      kept-type span — GLiNER2 emits both ORGANIZATION 'WOOLWORTHS NEWTOWN' (kept) and
      ADDRESS 'NEWTOWN' (stripped), so the merchant name loses its suburb. Question:
      should a kept ORGANIZATION absorb contained ADDRESS fragments, or is that a leak
      vector (real addresses legitimately appear inside org-labeled spans)?
      *(2026-07-15: the tier-1 corpus now generates suburb-suffixed merchants as
      whole keep-ORGANIZATION spans, so this wart is measured on the over-strip
      axis — a fix here shows up as the ORGANIZATION over-stripped count dropping.)*
      Input (2026-07-14, invalid-identifiers work): invalid-class spans already rank below
      any valid type in `_merge_overlaps` (union extents, valid class wins the placeholder)
      — fold that rule into the general algorithm definition.
- [ ] Loyalty-program ID class (issue #7, 2026-07-22 — design call pending). The Qantas
      frequent-flyer number on the Amplify statement (page 2) is not detected: no current
      class covers it, yet it identifies the customer. Decide: (a) is a loyalty ID
      strip-worthy PII (probably yes — it is a stable customer identifier linkable across
      documents); (b) one generic LOYALTY_ID class or per-program; (c) mechanism — a layer-1
      context pattern ('Frequent Flyer', 'Membership No', 'Rewards number' + digit run,
      the AuAccountNumberRecognizer context-promotion idiom) vs a GLiNER2 label (label
      competition risk — see the labels-per-pass experiment). Dual coverage on landing:
      pytest + a pii_eval probe with a truth type per the established convention.
- [ ] Label/value header columns alias into one span (issue #8a, 2026-07-22; **rescoped
      2026-08-09**). Two-column page headers (ANZ: left 'Postal Address' → address lines,
      right 'Trading Account Number' → '314811') band into single assembled lines by design —
      side-by-side cells ARE one visual row — so the text reads '24 STACEY DRIVE,
      CARRICKALINGA SA 5204 314811' and GLiNER2 emits the WHOLE line as one ADDRESS span
      (0.99). Everything strips, so no leak — the damage is aliasing ('314811' hides in
      ADDRESS_n instead of getting the consistent ACCOUNT_n it gets elsewhere).
      **This is now a `--detector layers` problem only:** the VLM reads the two columns as
      what they are, and layer 1 types the account number from the string. The old fix class
      (detect column structure in the OCR layer and isolate columns as segments) went with the
      segmenter and is not coming back — if this matters on the layers path, it needs a
      cheaper mechanism, and if the layers path is eventually retired it closes by itself.
- [ ] Slim the Presidio NLP engine: exclude `parser` and `ner` from the en_core_web_sm
      pipeline. Presidio loads the model with bare `spacy.load()` (spacy_nlp_engine.py, no
      component exclusions), so every analyzed text pays for the full 6-component pipeline;
      with SpacyRecognizer retired the spaCy NER output is read by nobody, and the parser
      only produces sentence bounds nothing consumes — lemmas need tagger+attribute_ruler
      only. Needs a small SpacyNlpEngine subclass or preloaded-nlp injection; first verify
      no recognizer/enhancer touches `nlp_artifacts.entities`/sents, then measure layer-1+2
      latency on the eval corpus. (spaCy source review finding (m), 2026-07-15 — record in
      [DONE.md](DONE.md).)
- [ ] AU place-name gazetteer as a cheap deterministic place-name layer (spaCy source review
      finding (j)): FlashText/PhraseMatcher-style trie — or plain set matching at our char
      level — over a public AU suburb/town list, case-insensitive, whitespace-normalized.
      Gives recall on bare town names; decide its overlap policy vs the ADDRESS passes when
      the overlaps-merging task above is done. Consider a fuzzy edit budget of
      `max(2, 0.3·len)` for OCR damage (review finding (i)).
      **Contingent:** standalone place-name detection was retired (ARCHITECTURE decision) —
      bare place names pass verbatim, so this is not a live gap unless that stance is reversed
      or layer-3 findings show bare towns must be caught. If revived, the corpus `LOCATION`
      probe (now a KEEP probe) flips back, and a no-context short-suburb surface form should
      be re-added.

## Experiments — GLiNER2 tuning

- [ ] Per-class max_width for GLiNER2 (requested by Sergei 2026-07-14 —
      discomfort with the blanket default max_width=12). Only ADDRESS needs wide
      spans (tier-1: every other class ≤ 4 words), and w16 already showed wide-span
      FP creep, so enumerating 12-word candidate spans for *all* labels buys nothing
      for the narrow classes and may cost precision. Try per-pass widths — the
      recognizer already runs dedicated address passes, and max_width is an
      inference-time attribute that can be set before each pass (both copies:
      `model.max_width` and `model.span_rep.span_rep_layer.max_width`): address
      passes at 12, everything else back at the trained 8. Rerun tier-1 per-class
      P/R + latency. Natural companion to the labels-per-pass experiment below
      (same grid infrastructure); if per-pass mutation proves racy or awkward,
      evaluate two recognizer instances as the fallback.
- [ ] Labels-per-pass (schema partitioning). Label competition
      suppresses sibling scores (documented in pii/core/gliner2_recognizer.py — the same
      span scores 1.0 alone vs 0.49 among siblings); addresses already get dedicated
      passes. New direct evidence (2026-07-15, reversed-caps diagnosis above): on CSV
      column blobs a person-only pass emits 'FULLER CHRISTOPHER'@0.80 where the
      production schema emits 0.33 — isolation rescues real misses, not just points.
      Question: does everything benefit from isolation? Grid to evaluate on
      tier-1, per-class P/R + layer-2 latency:
      (a) all-in-one (current baseline, minus the address passes),
      (b) full isolation — one label per pass (~11 passes; each pass re-encodes the
          3000-char window, so expect roughly linear cost growth),
      (c) themed groups — e.g. semantic {person, org, address, DOB} split from
          numeric IDs {TFN, Medicare, phone, bank account, licence, passport},
      (d) current production config as reference.
      Hypotheses: isolation lifts recall on semantic classes (competition is what we
      pay descriptions to overcome) but hurts precision on confusable numeric IDs,
      where competition doubles as disambiguation — a lone "9-digit number" label
      will claim TFNs, ACNs and phone fragments alike. Numeric precision loss is
      partly tolerable since layer-1 checksum/regex recognizers dominate those
      classes and validation filters impostors. Expected sweet spot: a few themed
      groups, not full isolation. Sequencing: needs at least a provisional
      cross-pass overlap-resolution rule — best run together with (or right after)
      the overlaps-merging task above.
- [ ] Ablation: are the address workarounds still needed at max_width=12?
      Postponed (decision 2026-07-14) until the tier-1 corpus has more and more
      varied address examples — 12 ADDRESS spans from a handful of templates is
      too thin a basis for removing belt-and-braces protections. *(2026-07-15:
      variety widened — PO Box postal lines, `ADDRESS_BARE` bare street lines in
      transaction descriptions; seed 42 now has 18 ADDRESS + 12 ADDRESS_BARE
      spans. Better, but still template-thin; judge again when picked up.)* When picked up,
      fold it into the labels-per-pass experiment above (same mechanics: rerun
      the eval with the extra address passes disabled).
- [ ] LoRA adapter for Australian addresses on GLiNER2 — close the multi-part address
      fragmentation gap at the model level (GLiNER2 ships open training code and
      load_adapter(); pii_eval's generator can produce the training pairs). Revisit after
      the overlaps-merging task lands, which should already close most of the gap.
      *(2026-07-14: priority further reduced — the max_width=12 lift closed the
      one-line-address fragmentation on tier-1; LoRA now only matters if real-world
      wide spans score poorly, or for the '53 MILES SUBWAY'-style bare street-line
      recall misses.)*

## Evaluation

(The tier plan and constraints are described in [ROADMAP.md](ROADMAP.md); the completed
text tier's record is in [DONE.md](DONE.md).)

- [ ] **Pseudonym-consistency scoring** (gap found 2026-07-15; semantics updated
      2026-07-18): the persona pool was built so the same people/accounts recur across a
      corpus, but the scorer creates a fresh `PseudonymMap` per document
      (`pii_eval/score.py`) and asserts nothing about placeholder identity —
      cross-document consistency is prepared for, never checked. *2026-07-18: the product
      story changed — maps are per-document by default, and cross-document consistency
      belongs to the future global/group map layers (see the layered-maps task above), so
      the fresh-map-per-document scorer behaviour is now* correct *for the default. The
      task becomes: when the layered maps land, score the shared-map regime too — same
      canonical value ⇒ same placeholder across a bundle (the truth manifest already
      carries the values).*
- [ ] **Tier 1 — image/degradation tier**: iteration 1 SHIPPED 2026-07-16 (see DONE.md) —
      `pii_eval render` prints the text corpus to page images (Pillow, seeded font
      variety, monospace for fixed-column docs) and `score --modality image` scores the
      real image pipeline by re-OCR value survival with OCR-tolerant matching; paired
      text/image corpora share one truth.json, output at `pii_eval/corpora/image/s<seed>`.
      Remaining: degradation pipeline (DPI, skew, blur, JPEG artifacts) composing on the
      clean renders; realistic reportlab statement templates (mail barcodes) as a second
      layout source; a `partial` axis for the image scorer (token-level survival needs
      occurrence disambiguation — surname stems recur in kept business names, see the
      known-limitation note in pii_eval/README.md); bbox-level ground truth if
      box-placement assertions are ever needed —
      match painted boxes with pixel tolerance from day one, exact-box assertions break
      across Tesseract versions (see the presidio-image-redactor review, DONE.md item (i)).
- [ ] **Tier 2 — PII-transplanted real documents**: Sergey manually replaces real PII with fake
      in 4–6 real documents (one per major bank layout, one bad scan, one transactions CSV),
      keeping layout intact. Real layouts + known ground truth + declassified. One-time effort,
      reusable forever.
- [ ] **Tier 3 — metrics-only runs on the real corpus**: harness emits only aggregates (entity
      counts/type, confidence histograms, layer-disagreement rates, cross-OCR-engine
      disagreement). Local side-by-side review UI so manual acceptance checks are a quick
      click-through; only declassified findings are reported back.

- [ ] **Stop duplicating Presidio's checksum arithmetic** (2026-08-08, from the 2.2.364 ABN
      re-sync — record in [DONE.md](DONE.md)). `pii/core/checksums.py` re-implements the AU
      TFN/Medicare/ABN/ACN rules that Presidio already owns, so every upgrade can silently
      desync a valid/invalid pair and drop values through both sides. `abn_checksum` is now
      version-coupled to Presidio ≥ 2.2.364 and *wrong* against 2.2.363. Options: delegate to
      the recognizers' `validate_result` (they take the matched text, so a thin digits→text
      adapter is needed, and the GLiNER2 post-validation path wants a plain digit-string API),
      or keep the copies but generate the coupling test for *all four* types rather than ABN
      alone. The ABN coupling test is the pattern to extend.

- [ ] **De-flake the tier-1 gate / revisit `build.CRITICAL`** (2026-08-08, incidental finding
      above). The gate passes at seeds 42 and 1 but fails at 2, 3 and 7 on unmodified code —
      always a residual GLiNER2 PERSON miss — so a single-seed gate is partly luck and any
      change perturbing the draw sequence re-enters the lottery. Worth either fixing the PERSON
      residuals or scoring several seeds and gating on the aggregate. Separately,
      `CONTEXTUAL_ID` sits at 0% recall at every seed and is excluded from `CRITICAL`; decide
      whether that exclusion is still intended or is masking a real gap.

## Nice-to-have

- [ ] "Match original font" for painted placeholders (Sergei, 2026-07-14) —
      estimate font size/weight (and maybe family) from the covered words' boxes/pixels so
      placeholders blend into the document instead of the current fixed-Arial
      shrink-to-fit. Also worth considering: match fill to the local background around the
      box rather than the page-wide most-common border color.
