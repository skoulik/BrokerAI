# TODO — PII engine (core)

All open engine tasks, with full working detail. The activity overview and evaluation-tier
plan are in [ROADMAP.md](ROADMAP.md); completed tasks and their engineering records are in
[DONE.md](DONE.md); design decisions (the *why*) in [ARCHITECTURE.md](ARCHITECTURE.md).
Front-end tasks live with their component: [../cli/TODO.md](../cli/TODO.md),
[../gui/TODO.md](../gui/TODO.md).

Grouped by theme, not by priority — with one exception: **throughput is the top open risk.**
At ~3 min/page the image/PDF path is a research profile rather than a product one, so the
serving/quantization item below is what stands between this tool and something usable on a real
submission bundle.

## What the 2026-08-09 rebuild settled — standing facts

Four retirements landed that day and they close off whole classes of work. Records in
[DONE.md](DONE.md), designs in [ARCHITECTURE.md](ARCHITECTURE.md); this list exists so nothing
below gets picked up against the old shape of the tool.

- **Layer 0 — a local LLM — is the only semantic detector**, in every input mode. GLiNER2,
  spaCy's NER and the `--detector` flag are all gone, so there is nothing left to choose
  between, and a strip entry point always *requires* a detector — patterns-only is the
  `--no-ner` regime retired 2026-07-15 as unsafe. Accepted knowingly: every input mode now
  needs a llama-server, including the tier-1 gate. There is no offline path.
- **The engine is ours** — no Presidio, no spaCy, no torch — and one rule owns both halves of
  each checksummed identifier class.
- **The layout/segmenter layer is gone**, and with it the whole OCR perception programme
  (orphan clustering, trial linearizations, table-cell structure, layout thresholds, region
  detection) and the layout half of issue #8a. The VLM reads spatial structure natively.
- **Layer 1 stays**, in the narrowed role it now actually has: classifier, checksum validator,
  and deterministic recall floor under a stochastic detector (`merge_detections`).
- **PaddleOCR stays** and supplies paint geometry. VLM boxes are stochastically unsafe to
  paint, so they serve as a *search constraint* instead (`--geometry hybrid`, the default);
  `--geometry vlm` is a comparison instrument only.

## Plan — layer 0 (Gemma 4), as agreed with Sergei on 2026-09-15

Where things stand: detection prompt tuning is concluded (`fbc040f`: 96.1% (4 leaks) on
`real/1` once the scorer stopped double-counting a truncation, gate PASS, DONE.md). Runs are
reproducible (`ModelFamily.prompt_cache`, and the llama.cpp SWA restore fix serving from
`brokerai-serving` `40e3f3b3b`). MTP n-max 2 stays. The keep list covers legal names
(`952bd10`). The items below are in the order agreed. Each has its own entry further down with
the details. Item 1, the d05 `Sk Ma` leak, is closed: it was painted, and the scorer counted it
inside another value (DONE.md). Item 2, `real/1` with MTP on against off, is closed: the outputs
differ by near-tie flips from two batch-variant Metal kernels, recall is level, MTP stays on
(DONE.md).

3. **After prompt tuning** (Sergei's list):
   - DFlash;
   - Google's QAT Q4_0, with one K-quant as the quality comparison;
   - a speed check of Gemma 4 12B Unified.
   DiffusionGemma is postponed.
4. **Loose model boxes:**
   1. measure the harm;
   2. an anisotropic search box;
   3. crop blank margins;
   4. coarse-to-fine crops.
5. **Remaining thinking ideas:** the truncated-text rule (d05 dot loop); grounding thinking on,
   or the combined single pass, with the new prompt; aligning the text prompt with the vision one
   (see "Layer-0 thinking: ideas raised 2026-09-14").
6. **Lower priority:**
   - the layer-1 `URL` class;
   - brands against products (open);
   - titles (postponed);
   - llama.cpp: an upstream report of the SWA restore bug (Sergei is commenting on #28873
     first). MTP's batch variance on Metal is closed: reported on #25618, switches kept for
     sweeps only (2026-09-16).

Working rules from these sessions:
- Judge a prompt change on a `real/1` run, never on a leak or two: every edit flips near-tie
  values.
- Sergei runs his own sweeps from `main`: never edit `main` while one of his or a queued chain
  is running.
- The Mac server must run under `caffeinate -is`.

## Next up — image/PDF path

- [ ] **Run the TEXT layer-0 pass over the OCR'd page text as well** (Sergei, 2026-08-11,
      raised while reviewing the limits of borrowed fuzzy matching — *"an independent text-only
      detection pass to run on the OCR-ed text... could fix the reverse failure where the OCR
      finds the text but the VLM does not flag it"*; that work shipped the same day, record in
      [DONE.md](DONE.md)). Written down, not designed.

      **The two gaps are mirror images.** Grouping propagates what the VLM found SOMEWHERE in
      the document; this would catch what the VLM found NOWHERE, on text OCR read perfectly
      well. Two detectors reading the same page through different senses fail differently — the
      vision pass can lose small print or a dense table to its own token resolution, where a
      language model reading the characters cannot. And the two compose: a value the text pass
      catches on page 4 joins the same entity group, so it propagates back to pages 1-3 for
      free.

      **Most of the plumbing already exists.** `text_llm.TextDetector` is the same model and the
      same five-class vocabulary, `linearize(OcrPage).text` is the string layer 1 already runs
      on, and its findings would land in that same offset space — so `locate_in_text` places
      them exactly (the model quotes from the string it was handed), the source map turns those
      spans into pixel boxes by interval intersection, and `merge_detections` unions them like
      any other layer-0 source. **No geometry problem at all**, which is the striking part: this
      pass needs no box, no locator tier and no fallback.

      **It is cheap as an addition, and potentially transformative as a REPLACEMENT** (Sergei:
      *"it will also work faster — if used instead of image pass"*). The measured ~300 s/page is
      dominated by image prefill (~130 s per vision call, 74%, and the two vision passes do not
      share it — see the serving item); a page of text is a few thousand tokens with no image to
      ingest.

      **The shape of the feature is two INDEPENDENT switches, not a three-way mode** (Sergei,
      2026-08-11): vision and text are each on or off, and the operator balances speed against
      quality by choosing. Three legal combinations:

      1. **Vision only** — today. Best on layout and on anything without a text layer; ~300 s/page.
      2. **Vision + text** — the union above. Best recall, most expensive.
      3. **Text only** — OCR → linearize → text layer 0 + layer 1 → paint. Structurally the
         pre-2026-08-08 architecture with a far better semantic detector in place of GLiNER2,
         and it would be **one to two orders of magnitude faster**, which would settle the
         serving/throughput item outright rather than adding to it.

      Guardrails that must be built with the switches, not after them:

      - **Both off is `--layer0 off`, which now exists** (2026-08-14, decision in
        [ARCHITECTURE.md](ARCHITECTURE.md)) — so the requirement is no longer "refuse it" but
        "route to it": turning both modalities off must land on the same explicit, warned,
        `NullDetector` path rather than becoming a second, quieter way to reach patterns-only.
        The `layer0` string these switches extend (`vision`/`text`/`off`) is already carried by
        each detector and read by the warning and the debug listing.
      - **Turning vision off is a knowingly reduced redaction, not a free speedup**, and the
        run output has to say so. What is given up is listed above (no-OCR-text content, native
        layout reading); an operator choosing speed should see that in the report, and a
        stripped document's trustworthiness now depends on which modalities ran.
      - **`--geometry vlm` and text-only are mutually exclusive** — that path never runs OCR, so
        there is no text for the text pass to read. Reject the combination rather than silently
        forcing one.
      - Open: the CLI surface (two boolean flags vs one `--layer0 vision,text` list), and
        whether the default is both (recall-first) or vision-only (today's behaviour preserved).

      What regime 3 gives up is specific and known, which is what makes it measurable: content
      with **no OCR text at all** (logos, barcodes, handwriting — locator tier 3 today, and the
      only geometry that exists for it comes from the model's own box), and the VLM's **native
      reading of spatial structure**, which is why the segmenter was retired — a linearized page
      bands side-by-side columns into one line, and issue #8a is exactly what that costs.

      **The A/B that decides it has a precedent to copy**: hold layer 1 constant, vary only the
      semantic detector over the same pages, score per class — the shape of
      [reports/2026-08-09-text-layer0-vs-gliner2.md](reports/2026-08-09-text-layer0-vs-gliner2.md),
      which retired layer 2 on exactly that evidence. The multi-page rendered corpus can run all
      three regimes over identical pixels.

      Known risks to design against when it is picked up:
      - **It inherits OCR damage and OCR omissions.** Anything OCR dropped or mangled is
        invisible to it, and this is precisely what regime 3 is betting against: under vision +
        text the vision pass covers that residue, under text only nothing does. So the OCR
        fidelity numbers stop being a background quality metric and become the floor under
        detection itself.
      - **`_rows` banding is load-bearing but visually false.** The linearized page interleaves
        side-by-side columns into one line on purpose (it is how context promotion reaches a
        value in a column beside its own label), so this pass would read lines that do not
        exist visually — exactly the aliasing in issue #8a below, which the VLM reading pixels
        does not have. Whether that costs precision is measurable.
      - **Over-strip.** Two semantic detectors unioned is more false positives by construction;
        recall-first accepts that, and the real-corpus over-strip axis is where it shows.
      - **The vote.** Its findings would vote in the entity groups. Whether a text-modality
        opinion should weigh the same as a vision one is a real question, and it interacts with
        the vote's ability to un-redact.
      - Whether it runs on every page or only as a backstop where the vision pass found little
        — cheaper, but unpredictable in exactly the cases that matter.

- [ ] **Layer 0 names values with no alphanumeric character, and they propagate document-wide**
      *(2026-08-13, `116832820_7_Insurance_Certificate.pdf`)*. The model returned `-` — the
      hyphen in the heading `Policy number - 116832820 07` — as PII_COMPANY, and `?`, a card's
      help icon, as a name. Both came back with no `bbox_2d`, so neither is visible on ANY debug
      layer: the layer-0 overlay draws the model's own box and there is none. They surface only
      downstream, where the damage compounds — grouping turns each into a document-wide needle;
      `locate_borrowed`'s exact tier has no length floor (deliberate: `Wu`, `Ng`, `NAB`, `ANZ`)
      and its word-edge guard does not apply to a needle whose edge characters are not
      alphanumeric; so every occurrence of that character in the document is painted and given a
      placeholder of its own (`ORG_3 = "-"`, and the hyphen INSIDE `Gt-Line`). Grouping also
      fuses all punctuation-only values into one entity, since `_related` short-circuits on an
      empty squash and calls two of them the same — contained to junk-with-junk, because a real
      value's squash is never empty, but they can re-type each other. Cost is over-strip and a
      polluted map, not a leak.

      Proposed fix: drop a finding carrying no alphanumeric character where layer-0 responses
      are PARSED (`vlm.py`), so nothing downstream ever sees it — placement, grouping, needles,
      placeholders, map. It cannot cost recall: a name, address, organization, DOB or identifier
      always carries at least one alphanumeric, which is also why the guard cannot be argued
      into a length floor and does not need measuring. Two open questions: whether a
      single-alphanumeric finding (`A`) should go the same way — the no-floor rule was argued
      for two- and three-character values, not one — and whether the drops should be counted
      rather than silent.

- [ ] **Text and CSV have no borrowed pass, so layer 1's per-occurrence scoring is unguarded
      there** *(2026-08-18, scoped out of the layer-1-needle change — record in
      [DONE.md](DONE.md))*. On the page path a value layer 1 detects once is now searched for
      everywhere (`image_mode.layer1_needles`). `text_mode` has no equivalent: layer 0's
      findings reach every occurrence through `locate_in_text`, but layer 1's do not propagate
      at all, and `TextLayout` gives each occurrence its own left band — so the same asymmetry
      exists in miniature. It bites only where layer 0 misses the value, which is why it was
      not bundled: on the page path the boost is the *only* thing standing between a bare digit
      run and the threshold, while in text mode layer 0 reads the same characters we do.

      Not obviously the same fix. The page path collects needles in sweep 1 because it already
      has two sweeps; text mode is a single pass over a string, and giving it one needs a reason
      beyond symmetry. Cheapest honest version: after the layer-1 pass, re-search the document
      for each distinct value it detected — exact and squash only, the same tier restriction —
      and union the extra spans in. Worth measuring against the corpus before building, since a
      document-wide regex sweep may already reach most of them.

- [ ] **A painted box started INSIDE its word and left two characters legible** *(seen
      2026-08-11 on the first `--debug` run, `pii_eval/corpora/image/s123/loan_04.png` at 150 dpi,
      Qwen3-VL-8B)*. The page ends "previously resided in Kew." and the output reads
      "resided in Ke" followed by a narrow `ADDRESS_9` — the paint box covers only the tail of
      the word. The layer-1 overlay shows the red box beginning mid-word, so this is geometry,
      not detection: the value WAS located and painted, just not over all of its pixels.
      `boxes_for_span` is supposed to make this impossible (a word partially covered by a span
      still yields its whole box), so the suspect is either the word/region split OCR produced
      for that token or the neighbour-midpoint pull-back in `painted_boxes_for_span`. Reproduce
      with `strip --debug=ocr,layer-1` and read the word boxes on that line. Note the value is a
      bare suburb, which layer 0 typed ADDRESS — worth deciding separately whether that should
      strip at all (see "No standalone place-name detection" in ARCHITECTURE.md).

- [ ] **Measure the hybrid against the `ocr` baseline on the 31-page real corpus** — the
      A/B the design was argued from but has NOT been run: `python -m pii_eval score
      --modality pdf -c pii_eval/corpora/real/1 --geometry hybrid` vs `--geometry ocr`, same
      detector and same locator, boxes as the only variable. Three numbers decide whether
      tier 3 earns its complexity or the disambiguation is carrying the whole change:
      (a) the size of the tier-3 residue, (b) how many values change span between the two
      runs (silent mis-locations the box fixed), (c) the throughput cost of pass 2 against
      the predicted ~16 s/page. Until this runs, the hybrid is reasoned-for, not measured.

      **The same run is the outstanding validation of `merge_detections`** *(folded in here
      2026-08-12 from the layer-1-refinement item, which was otherwise complete)*. Layer 1's
      refine/validate/extend pass over layer-0 findings is unit-tested but has never faced the
      leak gate, so the first `pii_eval score --modality pdf` on the real corpus measures both
      at once. It cannot be checked against the frozen baseline (445 findings / 350 distinct
      values, 31 pages) — that was taken in values mode with no layer 1 at all.

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
      scan above (same machinery: diff text-layer strings against what OCR reads off
      the rendered pixels).
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

         **Partial answer, unplanned (2026-08-19).** The tier-1 gate was run at Q4_0 and the
         capability check came back with a real, reproducible difference: a spelled-out joint
         name (`ERIC SMITH AND CASSANDRA SMITH`) is returned as TWO PERSON findings,
         deterministically over 3/3 repeat calls, where the 2026-08-14 corpus decision recorded
         it as one span. Not a leak — both names strip — but it is a change in span
         GRANULARITY, which is the kind of thing "detection quality holds" has to mean here,
         and it cost `JointNames.CLASSIFY` its input. Whether Q8_0 still returns the compound
         is untested and is the cheapest way to attribute it; if it does, quant selection is a
         detection decision and not only a throughput one. Record in DONE.md.
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
- [ ] **Investigate serving PaddleOCR-VL through llama.cpp instead of the paddle wheel**
      (Sergei, 2026-08-09; the serving axis of the backend item above — read that one first
      for the model's two modes and why layout mode is unusable). Note the production OCR
      today, PP-OCRv6_medium, is a classic det/rec CNN pipeline and can **not** run on
      llama.cpp; this is specifically about swapping the OCR engine to the 0.9B VLM *and*
      moving it onto the server we already run.

      **The model side is settled — the harness is the open question.** Verified 2026-08-09:
      llama.cpp has PaddleOCR-VL support (ggml-org/llama.cpp#18825, mtmd with
      `mtmd_decode_use_mrope`), and PaddlePaddle publishes official GGUFs for 1.5 and 1.6.
      There is an open eval bug against 1.6 (ggml-org/llama.cpp#25339) — check its state
      before trusting that tier. What llama.cpp gives back is a token stream; everything the
      `paddleocr` Python pipeline does around the model is ours to rebuild:
      1. spotting-mode prompt + decoding the `<LOC_n>` quads interleaved with the text into
         `{"rec_polys", "rec_texts"}` (the keys `_result_lines` already consumes — the
         adapter itself stays small, the decoder is the new code);
      2. coordinate dequantization from the per-page 1/1000 grid (~2.3 px vertically on a
         300 dpi A4 — noted in the item above);
      3. whether mtmd preserves the model's native tiling/resolution handling. A VLM OCR fed
         at the wrong input resolution drops small print *silently*, which is the Surya
         round-2 lesson and the failure mode that matters most here;
      4. determinism under `-np 1`, the same gate requirement layer 0 carries;
      5. **the make-or-break: are word- or at least line-level boxes obtainable this way?**
         Block-level geometry is useless for painting — that is what killed layout mode.

      What it buys, and why it is worth the investigation now: one serving stack for
      everything (layer-0 detection and OCR geometry on the same llama-server), and it drops
      `paddleocr` + the `paddlepaddle` wheel + `models/paddlex` — the most fragile dependency in
      the project (Windows DLL conflicts, per-machine wheel choice, and the torch guard that
      exists to police them). The worker subprocess those DLL rules once forced is already gone
      (2026-08-09); this would remove the rules themselves.

      Two costs to weigh. **Memory/process budget:** llama-server serves one model per
      process, so this is a second server alongside Qwen3.6 — it lands directly on the
      constraint the serving/quantization item above is already fighting. **A generative
      geometry source:** `ocr_page.py`'s "an OCR line is never dropped" invariant would then
      rest on a model that can omit, and OCR is what supplies the pixels we paint. That is a
      strictly weaker guarantee than a det/rec pipeline gives, and it needs measuring against
      the fidelity sweep and the leak gate before it could ever be a default.
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


## Detection pipeline

- [ ] **A vertical band for TEXT mode, and then CSV** *(the two follow-ons left over from the
      attachment work; the character window itself was retired 2026-08-18 — record in
      [DONE.md](DONE.md))*. Text attachment is left-only by Sergei's call ("Might reconsider
      later"), so a label directly ABOVE its value strips on the image tier and is a scored MISS
      on the text tier — which is what the non-gated `ACCOUNT_LABELLED_ABOVE` probe measures on
      every run. Monospaced statement text has real columns, so the same band is expressible
      over (line, column) without geometry. **CSV** follows it: cells are already the detection
      unit, so a column HEADER as the label of every cell below it is the natural next step, and
      it was postponed with the vertical band.

      Accepted residuals, all needing a specific layout rather than being hypothetical: a
      two-column line whose LEFT column ends in a label word, beside a right-column value with
      fewer than `word_floor` words before it, still attaches; a column header still labels only
      the first row under it (Sergei: "we have to sacrifice this, do not chase").

- [ ] **The corpus cannot see a separator bug, and has now hidden the same one twice**
      *(2026-08-12)*. `pii_eval/au.py` emits every identifier in ONE canonical form —
      single-space groups. Both the 2026-08-09 split-ownership leak and the 2026-08-12
      separator leak (records in [DONE.md](DONE.md)) were invisible to every corpus run ever
      made, and both were found by hand on a real document. The generator should vary the
      surface form the way a scanned statement does — single/double space, tab, hyphen and
      dash variants, NBSP — while keeping the truth value canonical, so a value that survives
      only in one spelling is a scored miss. Note the eval's `au.py` mirrors the checksum
      arithmetic and a coupling test pins the two together, so the change has to keep that
      seam honest.

      **Third instance, different axis (2026-08-14): LABEL spellings.** `AFS Licence No 285571`
      matched nothing because the corpus, the pytest case and the regex all exercised the single
      spelling `AFSL <digits>` — three artefacts agreeing with each other, so no run could
      disagree with any of them (record in [DONE.md](DONE.md)). The generalization is that a
      probe exercising ONE surface form of a labelled or separated pattern measures the regex
      against itself, so the varied-surface-form work above must cover label spellings as well as
      separators. The attachment work has since moved label spellings out of the regexes into
      data (`Rule.context`, 2026-08-14), which is what makes varying them in the generator worth
      doing: one list to extend, not nine alternations.

      **Fourth instance, and this one is the SCORER rather than the generator (2026-08-19).**
      Every text/image/pdf verdict is span COVERAGE — `stripped` / `partial` / `leaked` — so a
      value covered under the *wrong entity class* scores `stripped` and no run can disagree.
      Measured on seed 42 against the pre-fix pattern set: the six new bare-BSB probes all
      scored `stripped` while being detected as `AU_BANK_ACCOUNT`, which is precisely the bug
      (one BSB pseudonymizing as `BSB_n` on some rows and `ACCOUNT_n` on others; record in
      [DONE.md](DONE.md)). A class axis is not free — the truth types deliberately do not map
      1:1 onto entity types (`ADDRESS_WRAPPED`, `PERSON_REVERSED`, `AU_BANK_ACCOUNT_BSB_2_4`
      are forms, not classes) — so it wants a declared expected-class per truth type, scored
      as its own column rather than folded into the strip verdict. Until then, class is pinned
      by pytest only, and a mis-typing is a blind spot on every corpus run.

- [ ] **The ACN inside an ABN can still capture it when OCR damages a DIGIT rather than a
      separator** *(remainder of the 2026-08-12 separator fix)*. The separator class fixed the
      spacing cases, but `ABN I1 005 357 522` — the `1`/`I` confusion, which is the single most
      common OCR error on this corpus — still drops the ABN pattern, leaves `AuAcnRule` matching
      the 9-digit tail, and **loses the leading two digits from the span**. The remaining fix is
      the one not taken: have the ACN rule decline a 9-digit run whose two preceding characters
      complete a valid ABN. `fuzzy.py`'s confusion table already knows `I`/`1`, so the same
      question applies to the digit patterns generally — deliberately not widened, because a
      character class that admits letters into a digit run is a much larger change than a
      separator class and wants its own measurement.

      Also deliberately left on the narrow `[ -]`: `AuAccountNumberRule`'s grouped forms and the
      IBAN pattern. Neither is checksum-gated the way the identifiers above are (the account
      rule is *"hopelessly ambiguous without context"* by its own docstring), so widening them
      buys recall against a much weaker guard. Measure before touching.

      The general answer to that damage is one mechanism per half of the corpus: repair from
      the document's own text layer where there is one — shipped 2026-08-18, design in
      [ARCHITECTURE.md](ARCHITECTURE.md) — and folding where there is not, which is the item
      below.

- [ ] **The eval corpus has no text layer, so it cannot see OCR repair** *(2026-08-18, found
      while shipping it — the closed item claimed the opposite)*. `pii_eval/render.py` builds
      corpus PDFs with **Pillow's** PDF writer from page images, so every generated PDF is
      pixels only and `--text-repair` is a no-op on the whole harness. Repair is covered by
      pytest (`tests/pii/core/test_text_layer.py`, one case per gate) and measured by hand on
      the reference corpus (records in [DONE.md](DONE.md)), which leaves it outside the thing
      that runs on every change — the standing dual-coverage rule.

      The fix is to render the image tier's PDFs through pymupdf instead, so the page carries
      the text it was drawn from. That also makes the metric free and exact: "repaired OCR text
      vs the text the page was rendered from" needs no new ground truth. Worth pairing with the
      damage-injection probe the folding item below wants, since the two measure opposite
      halves of the same corpus — repair where the document carries the truth, folding where it
      does not.

- [ ] **Fold OCR-confusable letters into digit runs, for content with no text layer**
      *(2026-08-14; kept on Sergei's instruction — "yours still makes sense for non text
      content"). Complementary to text-layer repair (shipped 2026-08-18), not an alternative
      to it*: repair where the document carries the truth, fold where it does not — scans, and
      the parts of a text PDF the text layer does not cover, of which the Amplify p3 footer is a live specimen
      (`ABN 33 O07 457 141`, and in `ServletRetrieve (6).pdf` layer 0 read the same ABN as
      `32 o09 656 74o`, so the glyphs really are ambiguous rather than the OCR being careless).

      **The mechanism, and why it is not "widen the digit patterns".** Widening every regex to
      admit letters changes every rule at once with no guard. Instead apply a strict **1:1
      character substitution** (`O`->`0`, `I`/`l`->`1`, `S`->`5`, `B`->`8`, ...) to produce a
      DETECTION VIEW of the page text, and run layer 1 on that. 1:1 is the whole point: offsets
      are preserved exactly, so a span found on the folded view is that span on the real text
      and nothing downstream — source map, boxes, painting, pseudonym map — is touched.

      **The checksum is an oracle for the fold**, which no regex-widening approach can match:
      `32 O09 656 74O` folds to `32 009 656 740` and PASSES the ABN checksum, which is about as
      strong a confirmation as a guess can get. Fold speculatively, keep what validates, discard
      what does not. For the classes with no checksum — accounts, the ones that actually leaked
      here — the guard is an ATTACHED LABEL, which the 2026-08-14 attachment work made cheap to
      express: `Account Number` sits directly left of `O18057571`.

      **The unit is the separator-grouped numeric FIELD, not the token.** `O07` carries two
      digits and one letter, so any token-level digit-ratio floor high enough to be safe misses
      it; it is only obviously numeric as part of `33 O07 457 141`.

      The real risk is folding a genuine alphanumeric reference, and the same documents are full
      of them — `RHL-155634`, `FT231832FXL2`, `me0240.v02/202002/216436` must all survive.

      **The corpus cannot see any of this**, which is now the fourth instance of that lesson
      (separator forms, label spellings, and this): the generator emits clean text, so no eval
      run could ever catch an OCR-damage bug. The probe comes first — damage injection into
      truth-bearing values on the image tier. **Repair landed first (2026-08-18), so
      re-measure before tuning this**: on the reference corpus it fixed 21 readings including
      both the `O18057571` account number and the `32 O09 656 74O` ABN, so what is left for
      folding is the content the text layer does not cover — the Amplify p3 footer, scans, and
      any page whose text layer is refused. That is a much smaller and differently-shaped
      target than the one this item was written against.

- [ ] **A keep entry is filed under a class the pipeline may not settle on** *(found
      2026-08-12 while looking at the 1.pdf false positives)*. `entity_keep.txt` is sectioned by
      entity type and the match runs against the class the value ENDS UP with, which need not
      be the obvious one: on a real statement `13 25 99`, a published bank support line, arrived
      as `IDENTIFIER_GENERIC` rather than `PHONE_NUMBER`, and an institution's ABN as `AU_ACN`
      (see the separator bug above, which is most of why). An entry filed under the intuitive
      class keeps **nothing**, and the failure is invisible — a keep that never fires looks
      exactly like a value nobody listed.

      No entries were added: institution-specific numbers were tried in the shipped default and
      REVERTED (Sergei, 2026-08-12) because that file's scope is institution *names* that hold
      for any Australian financial document, and a value harvested from one statement is a
      per-document-set decision. `--entity-keep` REPLACES the default rather than composing with
      it, so "the shipped list plus my institutions" is not currently expressible — which is the
      real gap, and the reason the wrong place looked attractive.

      Options, none designed: composition (`--entity-keep` extending rather than replacing, or
      an include directive); a class-independent section for values that are never customer data
      whatever they are typed as; or matching against the layer-0 class as well as the final one.
      Whichever is chosen, the silent-failure property is the thing to fix — a keep entry that
      matches no class in the file should probably be a configuration error, the way a broken
      pattern already is.

- [ ] **A span the keep list splits produces fragments that never rejoin the group they came
      from** *(Sergei, 2026-08-11, on seeing `FROM SK BUSINESS TRUS ANZ HIGHETT LOAN` strip to
      `FROM ORG_5 ANZ ORG_6`: "I think we should run re-grouping after splits. Highett is not an
      organization, it is an address...")*. To think about, not yet designed.

      Grouping runs in sweep 1, on layer-0 findings; `apply_keep` splits spans in sweep 2. So a
      fragment inherits its parent's class and its own placeholder, and nothing reconsiders
      either. Two symptoms on one real page, both measured:

      - **Wrong class.** `HIGHETT LOAN` is the tail of a narrative field naming a suburb, kept
        as ORGANIZATION because the span it was cut from was one. Re-running the existing
        grouping would NOT fix it: `type_for('HIGHETT LOAN')` is None, because nothing in the
        document types Highett as an address — layer 0 called the whole line an organization,
        and layer 1 has no place-name detection *by design* (see "No standalone place-name
        detection" in ARCHITECTURE.md). Fixing this needs either knowledge the tool deliberately
        refuses (a gazetteer) or a fresh layer-0 call per fragment. Note the class decides a
        placeholder label here, not whether anything is redacted.
      - **Split placeholder.** `sk business trust -> ORG_2` and `sk business trus -> ORG_5` in
        the same map: one entity, two placeholders. `type_for('SK BUSINESS TRUS')` is also None,
        and the reason is a constant — `GROUP_BUDGET` is 0.9 while a single deletion costs 1.0,
        so a document-truncated form is not considered the same entity as its full form.

      **The sharp version of that second symptom, and the part worth thinking about first: a
      value MATCHED as a borrowed occurrence does not join the group of the needle that matched
      it** *(Sergei, 2026-08-11)*. Two components already disagree about whether these are one
      entity, measured on the same pair of strings:

          needle 'skbusinesstrust' (15)  vs  page 'skbusinesstrus'   edit distance 1.0
            locator.borrowed_budget  = 3.0  -> MATCHES, and paints it
            grouping.GROUP_BUDGET    = 0.9  -> NOT the same entity, so a new placeholder

      So `locate_borrowed` redacts the truncated printing *because it is the known value*, and
      the map then records it as a different value. Whichever way it is resolved, the two
      budgets answering one question ("is this the same entity?") with different numbers is the
      thing to look at — `grouping.py` already carries the same warning about
      `fuzzy.identifier_shaped`, which is deliberately shared with the locator "because the
      locator asks the same question when it matches a borrowed value and the two must not
      disagree". Note the budgets are tuned against different risks (a wrong group election
      mislabels a whole document-wide entity; a wrong borrowed match is additive over-strip), so
      the fix is probably not simply one constant — a borrowed match could instead CARRY its
      needle's identity to the span it produced, which is the information the map is missing.

      The second symptom is not really about splits at all, which is why this needs thought
      before code: the map keys on the SURFACE FORM, so `olga kulik -> PERSON_1` and
      `kulik olga -> PERSON_3` are already two placeholders for one person today. Grouping
      elects the class and never unifies the placeholder. Levers: re-key fragments against the
      grouping after splitting; key the pseudonym map on the group rather than the string, which
      unifies placeholders across every variant but makes rehydration restore one canonical form
      where the document printed several.

      **Whatever the fix, it must treat borrowed items GENTLY — no greedy group expansion**
      *(Sergei, 2026-08-11)*. `_cluster` is single-link union-find, so a group is transitive by
      construction: every member's matching surface is the group's. Two consequences bound the
      design space, and they rule out the lever that looks smallest.

      - **Do not make a borrowed match a member.** A matched variant would become a needle, that
        needle would match the next mangled printing, and so on — a feedback loop where each hop
        is inside budget while the endpoints are arbitrarily far apart. The member set would stop
        being "what the model actually saw" and become "everything anything matched". A borrowed
        occurrence should attach as a SATELLITE instead: it takes the group's placeholder and
        class, contributes no needle, casts no vote (it is a consequence of the needle, not an
        independent observation), and never moves the canonical form. Its distance must always
        be measured against that canonical form, never against another satellite.
      - **Do not raise `GROUP_BUDGET` past 1.0 to unify the placeholders.** That was the obvious
        lever and it is the dangerous one. At 0.9 a single deletion (1.0) cannot join anything,
        which is exactly what keeps truncation chains apart today — measured on the four
        progressive truncations of one name:

              GROUP_BUDGET=0.9  ->  4 groups, sizes [1, 1, 1, 1]
              GROUP_BUDGET=1.0  ->  1 group,  size  [4]      # SK BUSINESS TR joins TRUST
              GROUP_BUDGET=1.5  ->  1 group,  size  [4]

        One hop at a time under single link, so the group's canonical form can drift to an
        arbitrary prefix. Identifiers are protected against digit-for-digit drift by
        `IDENTIFIER_COSTS` pricing that at infinity, but truncation is deletions — they would
        chain too.

- [ ] **Invalid identifiers lost their context-tier coverage with GLiNER2** (measured
      2026-08-09, [reports/2026-08-09-text-layer0-vs-gliner2.md](reports/2026-08-09-text-layer0-vs-gliner2.md);
      Sergei: log and proceed). `AU_TFN_INVALID` logged drops 3 → 2 per seed on every seed, and
      the lost candidate is the *context*-tier one every time. The shadow recognizers do not
      collect that at the default `likely` tier — it was GLiNER2's identifier post-validation
      demoting a shape-correct checksum failure, and that path died with the recognizer. Nothing
      replaces it today. Cheapest candidate fix: raise the shadow default tier to `context`,
      which is a deliberate noise/coverage trade (bare digit runs promoted by nearby label
      words) and therefore a decision, not a patch. Measure the noise column before adopting it.

- [ ] **Layer 0 strips invalid identifiers regardless of `--mask-invalid-identifiers`** (same
      measurement; Sergei: log and proceed). Layer 0 reports a checksum-failed identifier as
      `PII_IDENTIFIER`, so it strips under `IDENTIFIER_GENERIC` whatever the mask setting says —
      `stripped-anyway` went 0 → 4 for TFN on s42. The direction is safe (a typo'd TFN is a real
      TFN minus a digit) but it breaks the feature's documented contract, which separates
      *reporting* a candidate from *masking* it, and an operator cannot review a value that has
      already been replaced. Options when picked up: exempt spans whose only detection is a
      layer-0 generic identifier overlapping an `*_INVALID` shadow from the strip plan when
      `mask_invalid` is off (keeps the contract, costs a knowingly-unredacted near-PII value in
      the output — probably wrong), or restate the contract as "layer 0 strips what it sees; the
      mask flag governs layer 1 only" and fix the docs instead. Decide which before coding.

- [ ] **What the keep list still owes** *(the mechanism SHIPPED 2026-08-11 — `entity_keep.py`,
      `data/entity_keep.txt`, `--entity-keep` / `$PII_ENTITY_KEEP` / `--strip-orgs`; record in
      [DONE.md](DONE.md), design in [ARCHITECTURE.md](ARCHITECTURE.md) "What is deliberately
      kept". This is the residue of the 2026-07-18 sketch that it did not cover.)*

      - **Applied keeps are not logged.** The original ask was that a run report every keep it
        applied, so a review can see what was deliberately left readable. Nothing prints today.
        This is the one item with a leak-adjacent argument behind it: keeping is the only
        operator-owned precision lever in the tool, and an unlogged one is unauditable.
      - **No `any` section** — a value kept regardless of the class it ends up with. The
        motivation got sharper after it was written: see the class-mismatch item above, where a
        bank's published `13 25 99` arrived as `IDENTIFIER_GENERIC` rather than `PHONE_NUMBER`.
        `any` is one of the three candidate fixes there; decide it in that item, not this one.
      - **Matching does not go through the OCR-confusion squash classes**, so a keep entry
        typed cleanly can miss an OCR-damaged printing of the same name. Untested and unmeasured
        — the borrowed matcher's fuzzy tier shows the shape a fix would take, and the same
        length-floor guard would be needed.

      Starter content shipped with it (232 lines: banks, insurers, card networks, lenders,
      utilities, telcos, major merchants). Two 2026-07-18 recommendations were deliberately NOT
      taken, and the file says so inline: the 13 xx xx / 1300 / 1800 ranges are present but
      **commented out**, because on a business account the holder's own service line is as
      identifying as their company name and this corpus is full of business and trust accounts;
      and institution ABNs are not listed, per the class-mismatch item above. Mobile-shaped
      numbers inside branded blocks (d02's +61 437 968 251) stay syntactically undiscriminable —
      an accepted over-strip unless the operator lists the specific number.
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
- [ ] **Person-names database layer** (Sergei, 2026-07-15) — **contingent, and its trigger has
      not fired**: it was the deterministic recall floor to build *if* reversed/varied-name
      recall stayed unsatisfactory, and layer 0 took it to 100% (item above). Kept because the
      argument survives the detector — a stochastic detector wants a mechanical floor under it,
      which is layer 1's standing job. If built: match known given names/surnames (e.g. the
      `names-dataset` package, US SSA + AU census lists) as tokens and emit PERSON candidates
      for adjacent known-name pairs regardless of word order — 'REID THOMAS' hits with no model
      involved. Design questions when picked up: score/context policy (confident vs
      context-promoted), precision on merchant lines (MCDONALDS, HARVEY NORMAN are
      surname-shaped — probably require a known *given* name in the pair, not just surnames),
      and the overlap policy against keep-listed ORGANIZATION spans. Sibling of the AU
      place-name gazetteer task (same trie/set-matching machinery, same fuzzy-budget idea).
- [ ] **Layer-3 local-LLM audit pass** — *contingent, not committed: the plan is to evaluate the
      tool end-to-end on the layers it has (0 and 1) and build layer 3 only if those results
      prove unsatisfactory — see ROADMAP.md and ARCHITECTURE.md.* Design if built: a second pass
      over the **already stripped** text — "does this still contain anything identifying?" — via
      the same llama-server. It catches what neither live layer can see by nature: contextual
      identifiers ("the borrower's wife, a dentist in Wagga Wagga"), including the bare place
      names given up when standalone place-name detection was retired. Note what makes it a
      separate layer rather than a longer layer-0 prompt: layer 0 reads the original and names
      values, layer 3 reads the output and judges the residue.
- [ ] Overlaps merging algorithm — define and document. Interesting areas: how the weights are
      combined (max, average, bayesian/aposteriori), what if winning classes of overlaps
      do not agree, should we merge at all in some cases. Adjacent-span coalescing for
      fragmented multi-part addresses belongs here too.
      Input (2026-07-14, image-demo wart 2): a strip-type span nested inside a kept-type
      span — a detector emits both ORGANIZATION 'WOOLWORTHS NEWTOWN' (kept) and ADDRESS
      'NEWTOWN' (stripped), so the merchant name loses its suburb. Question: should a kept
      ORGANIZATION absorb contained ADDRESS fragments, or is that a leak vector (real addresses
      legitimately appear inside org-labeled spans)?
      *(2026-07-15: the tier-1 corpus generates suburb-suffixed merchants as whole
      keep-ORGANIZATION spans, so this wart is measured on the over-strip axis — a fix shows up
      as the ORGANIZATION over-stripped count dropping. 2026-08-11: `apply_keep` answered a
      NEARBY question in the opposite direction — a keep match now exempts only what it covers
      and the rest of the span strips around it — so the nesting rule here has to be argued
      against that, not in a vacuum.)*
      Input (2026-07-14, invalid-identifiers work): invalid-class spans already rank below
      any valid type in `_merge_overlaps` (union extents, valid class wins the placeholder)
      — fold that rule into the general algorithm definition.
- [ ] **A layer-1 `URL` class for web addresses** *(Sergei, 2026-09-15)*. Web addresses reach
      the output only through layer 0, as `IDENTIFIER_GENERIC` (`ID_n`), or as `ORGANIZATION`
      when the model reads a domain as a brand. Gemma weighed `budgetdirect.com.au` against
      COMPANY 14 times on `real/1` and put `MEBANK.COM.AU` under COMPANY in a draft. The prompt
      now says a web address is an IDENTIFIER "even when it spells a company's name"; a
      deterministic class would settle it regardless of the model.
      - **What it buys:** a stable class and placeholder (`URL_n`); a keep-list section of its
        own, so an institution's domain can be kept without a keep entry under
        `IDENTIFIER_GENERIC` (see "A keep entry is filed under a class the pipeline may not
        settle on" above); and a recall floor for domains the model misses.
      - **Shape:** an `EmailRule`-style rule, Presidio's URL pattern harvested, validated by
        the public-suffix check `EmailRule` already does with `tldextract`. It covers bare
        domains (`budgetdirect.com.au`), `www.` forms and full URLs with a path.
      - **Must be decided:**
        - Ranking. `_rank` puts every specific class in one tier, so a layer-0
          `ORGANIZATION` on the same span can outscore `URL`. A domain-shaped span should
          take `URL`, which is a ranking rule and not just a new pattern.
        - E-mail overlap. The domain inside an e-mail address must not become a separate
          `URL` member that forks the address.
        - OCR damage. A space or `,` read inside a domain must not split it.
      - Dual coverage on landing: pytest, plus a pii_eval probe with a `URL` truth type.

- [ ] Loyalty-program ID class (issue #7, 2026-07-22 — **re-check before designing anything**).
      The Qantas frequent-flyer number on the Amplify statement (page 2) was not detected: no
      layer-1 class covers it, yet it identifies the customer. What changed since: layer 0's
      prompt names "membership and loyalty numbers" explicitly, so it most likely strips as
      `IDENTIFIER_GENERIC` today. **Step one is therefore to re-run that page**, not to pick a
      mechanism. If it is detected, what remains is only whether a stable customer identifier
      deserves its own class for report legibility (`LOYALTY_ID` vs `ID_n`) — a much smaller
      question. If it is still missed, the layer-1 route is a context pattern ('Frequent Flyer',
      'Membership No', 'Rewards number' + digit run, the `AuAccountNumberRule`
      context-promotion idiom). Dual coverage on landing: pytest + a pii_eval probe with a truth
      type per the established convention.
- [ ] Label/value header columns alias into one span (issue #8a, 2026-07-22; rescoped
      2026-08-09, **rescoped again 2026-08-12**). Two-column page headers (ANZ: left 'Postal
      Address' → address lines, right 'Trading Account Number' → '314811') band into one
      assembled line by design — side-by-side cells ARE one visual row — so the linearized text
      reads '24 STACEY DRIVE, CARRICKALINGA SA 5204 314811' and a detector reading that string
      emits the whole line as one ADDRESS span. Everything strips, so there is no leak; the
      damage is aliasing ('314811' hides inside ADDRESS_n instead of getting the ACCOUNT_n it
      gets elsewhere).
      **On the shipping path this is closed**: layer 0 reads the two columns as what they are,
      and layer 1 types the account number from the string. It survives here as a **known cost
      of the proposed OCR-text layer-0 pass** (first item in this file), which would read
      exactly that aliased line — that item lists it among its risks, and this entry is the
      detail behind it. The old fix class (detect column structure and isolate columns as
      segments) went with the segmenter and is not coming back; a text pass that wants this
      fixed needs a cheaper mechanism.
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

- [ ] **Pin which layer-0 model AND quant is production, in the docs** *(2026-08-19, found
      while starting the server for the gate)*. ARCHITECTURE's dependency table names
      "Qwen3.6-27B" and no quant; the Mac carries four `~/models/*/serve.sh`, and the last real
      run on it (Aug 17, ~38k tasks) was the **MoE 35B-A3B UD-Q4_K_XL**, a switch nothing in
      the repo records. So the two sources disagree and neither pins the quant — which matters
      more than it looks, because every recorded eval number is only interpretable against the
      model that produced it: the 2026-08-09 text baseline is Q8_0, the 2026-08-19 gate run is
      Q4_0, and those two are now known to differ in span granularity (item above). Decide what
      production is, write it in the dependency table with its quant, and say so in the eval
      reports' headers. Cheap, and it stops the next comparison being between unknowns.

- [ ] **Gemma 4: the reasoning budget — lower, not higher** *(Sergei, 2026-09-13; direction
      reversed 2026-09-14)*. The budget is reached by REPETITION, not by pages that need more
      thinking: the first complete list is usually the answer and the rest restates it (DONE.md,
      "Gemma 4 detection traces"). Prompt wording and a repeat penalty did not remove that
      without damage.
      - **Next (Sergei):** a budget roughly big enough for the first complete list, with a
        softer cut-off message that tells the model it is done rather than stopped. Run it on
        d01–d03 + d05 against V0 first, then on `real/1`.
      - **The trade to watch:** every probe variant that shortened thinking misread a long
        reference code that full-length traces read correctly. Part of the repetition is
        re-reading.
      - **A larger budget** (8192) stays untried; nothing now suggests it would help.
      - **Measured 2026-09-14:** budget 2048 with a soft cut-off message on `real/1` cost recall,
        96.1% → 92.2% (4 → 8 leaks, the fragile truncated names and place names), for 23% less
        survival time. Keep 4096 until thinking itself improves. Temperature sampling is out
        too (DONE.md).
      - **Also run thinking OFF with the 2026-09-14 prompt** *(Sergei)*. The 90.2% / gate FAIL
        number is from the old prompt, and the prompt fixes (distinct values, labels, no
        "PII") do not depend on thinking.

- [ ] **Layer-0 thinking: ideas raised 2026-09-14 and not yet scheduled.** Evidence for each is
      in DONE.md ("Gemma 4 detection traces") unless stated.
      - **Grounding with thinking on, and the combined single pass, with the new prompt**
        *(Sergei: neither is ruled out)*.
      - **A repeat penalty inside the thinking only.** Needs a llama.cpp patch keyed on the
        reasoning-budget sampler's in-trace state; applied to the whole reply it damaged
        transcription.
      - **Suppressing "Wait"/"Hmm" tokens** (NoWait, arXiv 2506.08343). Held back: a logit
        bias also hits the answer, e.g. a surname like "Waite".
      - **Cutting the trace client-side once a complete list appears in it** (stream and
        abort). Fragile, since draft lists are not always final; last resort.
      - **The truncated-text rule** ("If a value is cut short on the page, copy only the
        characters that are printed") removed the d05 dot loop in one document run. Not in the
        prompt; needs its own run.
      - **Bring the text prompt in line with the vision one**: the explicit "organizations'
        names, numbers, addresses and web addresses" sentence and value-not-label. Text input
        is unmeasured since the 2026-09-14 changes.
      - The DFlash drafter moved to the item below (Sergei, 2026-09-15).

- [ ] **Titles in names - postponed** *(Sergei, 2026-09-15)*. The model deliberates over "Mr Sergei
      Kulik" against "Sergei Kulik" as distinct values (4 trace lines on `real/1`). Listing both may
      key one person under two pseudonyms - not yet checked in d02/d09's maps. The prompt route
      failed: "NAME … without a title such as Mr or Mrs" made the model drop the joint initials
      `SK OK` ("I'll leave it out to be safe"). A detection-only bisect on the leak pages showed
      the title clause alone loses `SK OK`, and no other edit did; `real/1` failed its gate twice
      with it. If the fork is real, handle it deterministically: a NAME finding that starts with
      a title also contributes its untitled form. Not a prompt sentence.

- [ ] **Brands: the COMPANY boundary the model still argues most** *(Sergei, 2026-09-15: "something
      has to be done and I don't see an obvious solution")*. With brands in COMPANY (Sergei's call),
      the largest remaining debate in the traces is brand against product, model or statement type:
      "Kia is a brand, but it's part of the vehicle description", "AMPLIFY BUSINESS … statement
      type", "Gold Car Insurance Policy", NetBank. That is 25-29 doubting lines per `real/1` run.
      Adding "product or service" to COMPANY cost three leaks and painted product names as ORG
      (DONE.md). Options to think through, none decided:
      1. **Measure the harm first.** So far it costs tokens, not leaks: none of the 4-5 leaks is a
         brand case. Check whether any truth value was ever dropped as "a product/brand".
      2. **An extent rule:** report the brand word itself, not the product or model phrase ("Kia",
         not "2019 Kia Sportage"). It is a boundary inside a definition, which tends to become the
         next argument.
      3. **Take brands out of the model's job:** known brands already sit in the keep list, so the
         model could report every capitalized name and let the keep list decide. That changes the
         over-strip balance, and the prompt must not end up making keep decisions.
      4. **Accept the residual argument**, per the lesson that the narrowest wording plus some
         argument beats widening a class (memory: prompt-category-word-imports-model-definition).
      - Tried 2026-09-15 and rejected: "public companies and brands do not need to be reported" (DONE.md).

- [ ] **After prompt tuning: DFlash, a Q4 quant, Diffusiongemma** *(Sergei, 2026-09-15)*. Each is
      a `real/1` survival + grounding run against the committed prompt, with the cache off so
      the comparison is exact.
      - **The DFlash drafter** (`dflash-gemma-4-26B-A4B-it-Q8_0.gguf`, `--spec-type
        draft-dflash`). It drafts a whole block per forward pass, which may suit repetitive
        traces better than one-token MTP. Caveats: gains are reported lower on MoE targets,
        it was tested on CUDA/Vulkan (Metal unconfirmed), and speculation after an image may
        need a fix, as MTP did ([PR #22105](https://github.com/ggml-org/llama.cpp/pull/22105)).
        Measure decode tok/s and acceptance against MTP n-max 2 (61.1 tok/s), and whether the
        answers match. Every drafter setting so far has been its own set of outputs.
      - **A 4-bit quant of Gemma 4 26B-A4B.** Plain Q4_0 is the weakest 4-bit format (one scale
        per 32 weights, no importance matrix), so it is not the candidate. In order:
        1. **Google's QAT Q4_0** (`google/gemma-4-26B-A4B-it-qat-q4_0-gguf`, 14.4 GB + a 1.19 GB
           mmproj). It was trained for that format, which usually beats post-training K-quants.
           Caveat: the repo was last modified 2026-07-17, before the fixed chat template (#47,
           2026-07-20) that the ggml-org conversion carries, so pass that template with
           `--chat-template-file`. Check that the ggml-org MTP drafter pairs with it.
        2. **One post-training K-quant, as a quality comparison only:** bartowski `Q4_K_M`
           (17.0 GB, imatrix) or unsloth `UD-Q4_K_XL` (17.0 GB). On the M1 Max, K-quants lack the
           fast Metal decode kernel (`mul_mv_ext` covers the legacy quants and `iq4_nl` only).
           Measured on Qwen3-VL-8B (reports/2026-08-12-mac-inference-speed.md), tg512 was Q4_0
           58.4, IQ4_NL 49.6, Q4_K_M 43.7 and Q8_0 38.1 tok/s. So a K-quant would decode ~25%
           slower than Q4_0, and the i-quants are out: IQ4_NL is 15% slower, and IQ4_XS is not in
           the fast-kernel list at all (unmeasured). That was a dense model; the MoE experts use
           the `_id` kernels, whose coverage is unchecked. The same session also had UD-Q4_K_XL
           return 21 values against Q8_0's 70.
        Why it may pay here when it did not for Qwen: Q4_0 cost Qwen3.6-27B 11% of prefill for no
        gain, but that workload was image prefill. Gemma's is decode, 1,658 s of decode against
        563 s of prefill on a `real/1` survival run, and decode is limited by memory bandwidth.
        The risk is transcription, since shorter traces already misread long reference codes, so
        compare identifiers character by character, not just recall. The swizzle was tuned on
        q8_0 and f16 tiles only.
      - **Gemma 4 12B "Unified"** (`google/gemma-4-12B-it`, raised by Sergei 2026-09-15).
        Preliminary, from the model card:
        - **Architecture:** dense, 11.95B parameters, 48 layers, SWA window 1024. It is
          encoder-free: image patches (and audio) are linearly projected straight into the LLM,
          with no ~550M vision tower. Image budgets are the same as the 26B's (70…1120 tokens).
          It has a thinking mode.
        - **Quality against 26B-A4B:** OmniDocBench 1.5 edit distance 0.164 vs 0.149 (slightly
          worse, far better than DiffusionGemma's 0.319); MMMU Pro 69.1% vs 73.8%; MMLU Pro 77.2%
          vs 82.6%.
        - **Serving:** `ggml-org/gemma-4-12B-it-GGUF` has Q8_0 12.7 GB, Q4_0 7.2 GB (its `.src_sha`
          lists the QAT checkpoint), a 0.16 GB mmproj projector and MTP drafters. Google also
          publishes `gemma-4-12B-it-qat-q4_0-gguf`. Open llama.cpp issues: draft-mtp memory
          fault (#26782), and garbled output on large prompts on Intel Arc (#26206). Metal is
          unreported.
        - **The catch is speed, and it is estimated, not measured.** It is dense, so every token
          reads ~12B parameters against the 26B-A4B's 3.8B active. Our runs are decode-dominated
          (1,658 s of decode against 563 s of prefill), so expect roughly 2–3x slower decode at
          Q8_0. The encoder-free prefill also runs image tokens through the full 12B, and saves
          only the vision tower. Its draws are the memory footprint (7–13 GB) and a different
          way of reading fine print.
        - **First step if pursued:** a speed check before any corpus run — `llama-bench` tg at
          Q8_0 and Q4_0, then d01 through the strip path with thinking on — since speed is what
          would rule it out.
      - **DiffusionGemma** (`google/diffusiongemma-26B-A4B-it`, released 2026-06-10). Preliminary
        research 2026-09-15: **not usable for layer 0 yet, and weaker at documents. Postponed
        (Sergei, 2026-09-15).**
        - **What it is:** Gemma 4 26B-A4B turned into a discrete text-diffusion model. An
          autoregressive encoder caches the prompt, and a bidirectional decoder denoises
          256-token canvases, 15–20 tokens per forward pass. It takes images and has a thinking
          mode. The recommended sampler is entropy-bounded with a 0.8 → 0.4 temperature
          schedule and up to 48 steps, so greedy reproducibility is an open question.
        - **Quality, from Google's model card against Gemma 4 26B-A4B:** OmniDocBench 1.5 edit
          distance 0.319 against 0.149, twice the document-parsing error; MMMU Pro 54.3% against
          73.8%. Our task is verbatim transcription of statements, so this is the headline risk.
        - **llama.cpp:** only draft PRs. #24423 (unsloth, updated 2026-09-10) has a
          `llama-diffusion-cli` and example servers. #24427 is another. Neither touches mtmd,
          grammars or llama-server, and device-side sampling is CUDA only: on Metal it falls
          back to the host.
        - **Apple Silicon speed:** issue #24529 measured an M3 Max at Q4_K_M at 6–28 tok/s as
          shipped, and 48–61 tok/s after local optimisation. That is no better than our
          autoregressive Gemma 4 with MTP on the M1 Max (61 tok/s).
        - **Other route:** mlx-community publishes 4/8-bit MLX conversions. Whether mlx-vlm serves
          it with images is unverified.
        - **Revisit when** llama.cpp support merges with image input and Metal sampling. A quick
          quality spot check through MLX on a few pages is possible sooner if wanted.

- [ ] **Loose model boxes: exact vertically, off horizontally** *(Sergei, 2026-09-15; after the
      work above)*. On 1.pdf page 4 (his sweep, `gemma-4-div-fix-prompt-arg-fixes-15.09`), the
      grounding boxes, compared with OCR positions of the same words in 0-1000 units, were within
      ±2 vertically but off by 8-55 horizontally (20-136 px of 2,480), with a varying sign. Not a
      scaling or box-order bug. The likely floor is image resolution: 1,120 tokens over an A4
      page is roughly one token column per ~90 px, about the width of "ANZ" (estimate). Why
      vertical does better is a hypothesis: lines are separated by whitespace, while a word
      inside a line is not, and a y unit is 3.5 px against an x unit's 2.5 px. Every finding on
      that page was still placed on the right OCR word, since a model box only constrains the
      search.
      1. **Measure the harm first**, across `real/1`: placements that took the wrong occurrence
         of a value repeated close to itself, and findings painted from the model's own box
         (the "matched no OCR text" warnings). Two ANZ boxes on page 4 have no matching OCR word
         nearby - check them in the `locate` overlay.
      2. **An anisotropic search box, no model calls.** Tier 3 pads the model box by
         `FALLBACK_PAD_RATIO * box.height` (min 8 px) on every side, ~24 px on body text, far
         below the horizontal error seen. Widen it in x (about one token column), and consider
         the same asymmetry wherever box overlap ranks placement candidates.
      3. **Crop the page's blank margins before both passes** (Sergei). Resolution per token rises
         with the square root of the area removed, since the image budget is fixed. On the 31
         `real/1` pages the ink covers median 88.5% of the width (79.1-93.5%) and 92.1% of the
         height (66.4-96.9%). That is ~1.11x linear resolution on the median page and 1.23-1.29x on
         pages with blank bottoms (d04 p1, d09 p1, d10 p2/p3); d01 p4 ~1.12x. Take the extent from
         the RASTER (an ink threshold plus a minimum per row and column, padded), not from PDF
         objects. The PDF union of text, images and non-white drawings matched on most pages, but
         a full-page background put d03 at 99.6% width against 93% of ink, and scans have no
         objects at all. Keep page-edge rotated stripes, which are content. Boxes then translate
         back by the crop offset, and the cached raster must be the cropped one both passes saw.
      4. **Coarse-to-fine crops, if 1-3 are not enough.** Crop around each whole-page box, padded
         a couple of token widths in x and a line or two in y, and ask for the value's box in the
         crop at the full 1,120 tokens. Blind tiling is not the design: tiles need overlaps, cut
         words and absent values handled, and a model asked about one image reports values from
         others (Qwen, 2026-08-12). A crop around a known box removes the absent-value case,
         padding covers cut words, and a value wider than its crop keeps the whole-page box. Cost:
         ~9 s of prefill per crop cluster.

- [ ] **llama.cpp: find where the batch shape changes the logits** *(Sergei, 2026-09-15: worth
      doing anyway, and it also bears on the MTP setting)*. Greedy output on the Mac changes
      with how a prompt or a draft is batched. A full cache hit re-evaluates the last token
      alone and thinks differently, a 2-token prefix reuse changed grounding answers, and MTP's
      `--spec-draft-n-max` changed 3 of 4 d01 detections between 2 and 3 (DONE.md,
      2026-09-15). `ModelFamily.prompt_cache` sidesteps the cache case for Gemma. MTP depth
      cannot be sidestepped that way: every setting is one particular set of outputs. What
      remains after the restore bug below is the batch-shape noise itself (slot hit, 2-token
      reuse, draft depth).
      - **The restore discrepancy is a llama.cpp bug, found and fixed (2026-09-15).**
        A full hit on the slot's own KV is noise: logprobs within 0.016, and the whole
        755-token output identical. A full hit RESTORED from host memory (`--cache-ram`) was
        not, because `llama_kv_cache::state_write` drops sliding-window cells that are masked
        relative to the sequence's END. That was added upstream for checkpoints (`236531595`,
        #23981) but applies to every per-sequence save. The prompt cache saves the slot WITH
        its generated reply and restores only the prompt prefix, whose own window needs those
        cells. `--swa-full` makes the server treat the model as non-SWA (`n_swa = 0`), so
        nothing notices. Token-1 deviation after a restore grew with the tokens generated
        before the save: 0.015 / 0.11 / 0.88 / 1.17 / 8.4 for 1 / 8 / 64 / 256 / 755. At 755
        the model closed its thought at once, the "no thinking" pages; MTP was not involved.
        - **Fix, deployed 2026-09-15:** drop masked cells only for
          `LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY` (checkpoints). Fork branch
          `kv/swa-mask-partial-only` (`e422ce962`), merged into `brokerai-serving` (`b162d127d`,
          pushed); `build-b10939` rebuilt (build 10952) and serving. The real request restored
          byte-identical to a full evaluation, MTP on (unpatched: 61 tokens). The fix makes
          host-memory entries larger for `--swa-full` models, since SWA cells beyond the window
          are now kept.
        - **Minimal repro, reproduced on upstream master `7cf1c54a9`:** text-only, Gemma 3 270M,
          `--swa-full -np 1`, curl. Evaluate prompt A; generate 400 tokens on A; send prompt B
          (A's state is saved to `--cache-ram`); send A again (restored). Unpatched, the restored
          first-token logprobs differ from both a full evaluation and a slot reuse (`Alpha` −9.47
          against −9.76, `α` −10.28 against −10.87). Patched, they are identical to the slot
          reuse. Script: Mac `~/models/gemma-3-270m/repro_swa_restore.sh`; a master build is
          in `~/src/llama.cpp-swafix-test`.
        - **Upstream:** no existing report found (2026-09-15). #28873 (open) changes the same
          function for `PARTIAL_ONLY` saves of non-SWA caches, a checkpoint speed-up, and does
          not fix this. #27148 (RAM cache restoring an unrelated conversation under concurrent
          load), #21769 and #25751 are different mechanisms. Report or PR: Sergei's call.
        - Probes: scratch `first_token_probe.py`, `hit_replay.py`, `reuse_replay.py`,
          `restore_logprobs.py`, `swa_window_probe.py`, `restore_replay.py`; results in
          `sensitive/statements/1/exp-2026-09-15-determinism/`.
      - **The draft-depth part is localized and fixed on a diagnostic build (2026-09-16,
        DONE.md).** Two Metal kernels make a verify batch's row 0 differ from a one-token decode:
        `mul_mv_ext` (2–8 rows, whose `nxpsg` also depends on the row count, which is why each
        n-max is its own set of outputs) against `mul_mv`, and flash attention's `nsg`, which
        changes past a padded KV length of 2048 and 4096. Running 1–8 rows through
        `mul_mv_ext` with a fixed `nxpsg`, plus a pinned `nsg`, made MTP on and off identical on
        `real/1` (62/62) at full MTP speed. Build and switches: Mac `~/src/llama.cpp-mtp-debug`
        (uncommitted diff at `40e3f3b3b`); the probe is `batch-inv/`.
      - **Still open: prefill shape.** The cache-hit and 2-token-reuse cases compare a
        many-row prefill (`mul_mm` above 8 rows) with a one-token evaluation, which the fix
        above does not touch; n-max 8 and up would hit the same wall. `ModelFamily.prompt_cache`
        keeps Gemma off that path, so this matters only if the prompt cache is wanted back.
      - **Decided 2026-09-16 (Sergei): the invariance switches are a measurement instrument, not a
        serving change.** Use them for sweeps where two arms must be comparable across a serving
        difference; production stays on the stock kernels, where recall is level anyway. The
        diagnostic build is the Mac's `~/src/llama.cpp-mtp-debug` (switches
        `GGML_METAL_MUL_MV_EXT_INVARIANT=1 GGML_METAL_FA_VEC_NSG=4`, `serve-debug.sh`,
        `restart.sh`, diff saved as `batch-inv/metal-batch-invariance-diag.patch`). Cost there:
        plain decode 46.4 against 47.5 tok/s, MTP decode unchanged at 61.
      - **Reported upstream** as a comment on #25618 (2026-09-16,
        [#25618 comment](https://github.com/ggml-org/llama.cpp/issues/25618#issuecomment-5690103249)):
        the two kernels, the measurements, and the near-tie nature. No PR offered.

- [ ] **De-flake the tier-1 gate / revisit `build.CRITICAL`** (2026-08-08; **re-measure before
      acting, 2026-08-12**). Under GLiNER2 the gate passed at seeds 42 and 1 and failed at 2, 3
      and 7 on unmodified code — always a residual PERSON miss — so a single-seed gate was
      partly luck and any change perturbing the draw sequence re-entered the lottery. **Those
      numbers are stale**: the detector was replaced on 2026-08-09, seeds 42/123/7 were
      re-measured, and seed 7's failure is now a *recorded accepted loss* rather than a flake
      (a shared surname that is also a banking word — `LOAN REPAYMENT PERSON_5 FEE`; see the
      joint-name decision in ARCHITECTURE.md). Seeds 1, 2 and 3 have not been re-run under
      layer 0. So: re-measure first, then decide between scoring several seeds and gating on the
      aggregate, or keeping a single seed and listing the accepted losses.

      One thing to settle in the same pass: `CONTEXTUAL_ID` sits at 0% recall at every seed and
      is excluded from `CRITICAL` — decide whether that exclusion is still intended or is
      masking a real gap; layer 3 is nominally its owner and layer 3 is contingent.
      (`PERSON_REVERSED` used to be listed here as due for promotion into `CRITICAL`. It was
      promoted on 2026-08-09 with the rest of that work and the item was never closed; found
      2026-08-19.)

      Note the gate now needs a llama-server, which changes its character: it is no longer a
      cheap model-free check, and `-np 1` is required for the reproducibility it depends on.

## Serving — local llama.cpp patches

Local commits live on the **`brokerai-serving`** branch of `~/src/llama.cpp` on the Mac. Since
2026-09-13 it sits on upstream b10939 and is built to `build-b10939/`, which serves Gemma 4;
`build/` keeps the b10499-based binary Qwen3.8 was measured on. Full engineering record and every number quoted
below: [reports/2026-08-20-vision-tower-head-dim-72.md](reports/2026-08-20-vision-tower-head-dim-72.md).
Shipped 2026-08-20: four `metal:` commits (mul_mm threadgroup swizzle + its generalizations),
worth pp8980 117.92 → 144.60 t/s, plus `-ub 512` in `serve.sh`. Added 2026-08-22: `1bcb1ed48`
`test-backend-ops: cover the mul_mm threadgroup swizzle` — the four above shipped with **no
correctness coverage of the reordered path at all** (every gate-tripping eval case had one row
tile, so the remap was the identity); three cases close it, and a mutation proves they cover
what the old 1154 did not. Details in the report's "Fixed, and confirmed" section.

- [ ] **Why pass 2 lost Gemma's cached image after a long pass-1 trace** *(2026-09-13, found
      while choosing how to switch thinking off for the grounding pass)*.
      - **Setup:** pass 1 thinking on (a 2.8–4.5k-token reply), then pass 2 with the
        IDENTICAL prefix (thinking kwargs unchanged, `reasoning_budget_tokens: 0`).
      - **Result:** pass 2 re-read the whole prompt on both pages tried (`prompt_n` 1472 and
        1371, `cache_n` 0). The very first thinking-on probe had reused it (`prompt_n` 343)
        after an equally long pass 1.
      - **What the log shows:** "selected slot by LCP similarity, f_sim_best 0.75, f_keep
        0.19". In `server-context.cpp`, `f_keep < 0.5` saves the slot to the RAM prompt cache
        and then tries a better cached prompt. The save copies without clearing, and
        `server_prompt_cache::load` returns true when nothing better is found, so that path
        alone does not explain a full re-read.
      - **Ruled out:** cache size; the prompt prefix.
      - **Next step:** a server run at trace verbosity to see what clears the slot.
      - **Parked 2026-09-15:** moot for Gemma, which now sends `cache_prompt: false` and so
        re-reads pass 2 on purpose. Still relevant to Qwen, whose family keeps the cache on.
      - **Matters because:** it costs ~9 s/page on Gemma whichever way pass 2 thinks, and on a
        Qwen-sized image it would be ~120 s.

- [ ] **Reclaim the head_dim-72 flash-attention padding — ~10.8 s/page.** *(Deferred by Sergei
      2026-08-20: "let's postpone the odd padding, but write it down".)* Qwen3-VL's vision
      tower has head_dim 72; Metal's FA tile kernel accumulates `O = P*V` over
      `PV = PAD2(DV, 64) = 128` columns, so 56 of every 128 are multiplied and discarded. It is
      **22% of the vision tower**, measured: at N=34,320/16 heads the kernel delivers 3.80
      *useful* TFLOPS at hs=72 against 5.54 at hs=128, while *issued* throughput is flat
      (5.13–5.54) across every head dim — i.e. cost tracks padded columns, not real ones.
      Attempted and reverted 2026-08-20; it is **not** a constants change:
      - `PAD2(DV, 32)` alone does not compile — the kernel is instantiated for every NSG
        (1/2/4/8) via a function-constant switch and `static_assert(PV8 % NSG == 0)` must hold
        at NSG=8. That assert is *why* the 64 is there.
      - `PV = PAD2(DV, 16*NSG)` does compile (NSG is a template parameter), and with a host-side
        `nsg = 2` for DV∈{72,80,96} would give PV=96. But **the NSG=2 path is broken**:
        FLASH_ATTN_EXT fails at hs=72 and the kernel returns in 0.4 ms for a 1.4 s workload, so
        it is structural, not numerical. Some assumption is still tied to NSG=4; not found.
      - Preferred route instead: keep NSG=4 and teach the two `O = P*V` loops to handle an
        **odd** `NO` (3 rather than 4) — they currently consume accumulator tiles in pairs
        (`for ii < NO/2`, `lo[2*ii+0]`, `lo[2*ii+1]`). Worth ~6.2 s/page at PV=96.
      - Full prize (10.8 s) needs a bounds-checked remainder tile, `NO = ceil(DV8/NSG)`, so
        DV=72 is exact.
      Also affects head dims 80/96/112, and **CUDA is not an escape** — `fattn.cu:461` excludes
      head dim 72 from the tensor-core MMA path outright.

- [ ] **Validate the swizzle constants on newer Apple silicon when one is available.** The
      *design* is settled — **a generic constant plus clipping, not a per-device table**
      (Sergei, 2026-08-20: a table has no automatic source and no maintainer, so it "would not
      be a feasible solution"; the host-side plumbing built toward one was reverted). What is
      open is only confirming the constants behave on an M2/M3/M4/M5. Established 2026-08-20:
      - **Why it should be safe on a bigger cache, structurally:** the budget sets the working
        set the *group* creates (`SWZ = 10MB / tile_bytes`), which is a property of the shape,
        not the machine. A group holding ≤10 MB of src0 that was comfortable in a 48 MB cache
        is comfortable in a 96 MB one — the working set does not grow with the hardware, so a
        larger cache cannot turn a safe choice unsafe.
      - **And the swizzle only reorders threadgroups; it never changes the work done.** There
        is no mechanism for a large regression, only for worse locality than the default order
        — and the default order is the pathological one for these shapes.
      - **The exposure is the 32 MB gate, not the budget.** That constant does ask a
        machine-dependent question ("is src1 too big for cache"). On a bigger cache a shape just
        over 32 MB may no longer be pathological and would be swizzled needlessly. Measured cost
        of swizzling a shape that does not need it: **~1.2%** (ffn_up, 8.43 → 8.33). Bounded, no
        cliff. Note the asymmetry favours firing: ~1% when needless, ~70% when needed — if
        anything the gate should be *lowered*, not raised.
      - **The benefit saturates**, so clipping at 8 likely costs nothing on a bigger cache: SWZ
        8 vs 16 is 8.29 vs 8.22 here (16 already not better), and budgets of 10 MB and 40 MB
        measure identically. A src0 tile only has to survive one src1 sweep; grouping past that
        buys nothing.
      - **macOS exposes no GPU/SLC size.** `sysctl` gives only CPU cluster L2
        (`hw.perflevel0.l2cachesize` = 12 MB here), and that reads 12 MB on *every* M1 variant
        whether the SLC is 8, 24 or 48 MB — useless as a proxy. Metal has no cache query either.
      - **Apple's SLC spans ~8 MB (base) to ~96 MB (Ultra).** It is also *reported* not to be
        monotonic across generations (the M3 Pro is said to have reduced it against the M2 Pro;
        its bandwidth cut, 200 → 150 GB/s, is well documented, the cache figure is not, and
        neither is verifiable here). Treat the non-monotonicity as unconfirmed — it does not
        change the design, which is safe in the cache-*larger* direction regardless.
      - Mitigated, not solved: rounding the group down to a power of two and capping it at 8
        widened the usable budget from roughly one value to a **4× range** (10 MB and 40 MB both
        give 8.28/8.29 q8_0 and 7.34/7.29 f16). A 4× tolerance against a 12× hardware spread
        still leaves the extremes uncovered.
      - Failure direction is the safe one *as far as it was measured*: over-budgeting decays
        toward the unswizzled baseline, under-budgeting (3 MB) loses the win outright (f16 4.51
        against a 4.56 baseline). The one case observed *below* baseline was SWZ=16, which the
        cap to 8 removed. **Untestable here — every number is from one chip.**
      - To validate on a new machine: `llama-bench -p 8980` against a build with and without
        the four `metal:` commits. `~/bench/mm_sweep.cpp` and `mm_dt.cpp` reproduce the kernel
        behaviour with no model and under 1 GB.

- [ ] **2D grid blocking for `mul_mm` — the real fix, and it subsumes the item above.** The
      current swizzle groups only the row-tile walk, so each group still streams the full width
      of src1; that is why K=34816 is improved but not solved (2.70 → 3.77 TFLOPS, 36% of peak)
      and why the absolute byte budget is load-bearing at all. Blocking both grid dimensions
      bounds *both* operands, which makes the working set a chosen quantity rather than a
      consequence of the shape, and should make the cache-size guess far less critical.

- [ ] **`mul_mm_id` swizzle is committed but unexercised.** Our MoE (Qwen3.6-35B-A3B: 256
      experts, 8 used, n_embd 2048 → `n_ff_exp` ≈ 1024) puts per-expert src1 at ~0.26 MB, three
      orders under the threshold, so the gate can never fire. Correct (MUL_MAT_ID 799/799) but
      unmeasured. If a Mixtral-class MoE (few experts, large expert FFN) ever enters the
      picture, measure it there.

- [ ] **Decide what to do with the patches upstream.** Four commits, self-contained, with a
      model-free reproducer (`~/bench/mm_sweep.cpp`, `mm_dt.cpp`, `fa_headdim_bench.cpp`).
      Sergei has not decided whether to file; the two nearest existing issues
      ([#14527](https://github.com/ggml-org/llama.cpp/issues/14527),
      [#15426](https://github.com/ggml-org/llama.cpp/issues/15426)) both died stale for want of
      exactly such a reproducer. Blocked on the generality items above being at least honest
      about their limits.

## Nice-to-have

- [ ] "Match original font" for painted placeholders (Sergei, 2026-07-14) —
      estimate font size/weight (and maybe family) from the covered words' boxes/pixels so
      placeholders blend into the document instead of the current fixed-Arial
      shrink-to-fit. Also worth considering: match fill to the local background around the
      box rather than the page-wide most-common border color.
