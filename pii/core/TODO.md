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

      - **Both off must be refused at the front-end**, loudly. It is the `--no-ner` patterns-only
        regime retired 2026-07-15 as unsafe, and the standing rule is that a strip entry point
        always takes a detector — it must not become reachable by turning two flags off.
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

- [ ] **Font traceback** (diagnostics-only): fill `OcrLine.font` from the PDF text layer
      (pymupdf `get_text("dict")` spans matched to line boxes) — it is `None` from any OCR
      engine. Must never feed the strip decision (we deliberately distrust the text layer).

## Detection pipeline

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
- [ ] **Promote `PERSON_REVERSED` into `pii_eval` `build.CRITICAL`** — all that remains of the
      reversed-caps person-name residual *(2026-07-15 → closed 2026-08-09)*. The residual was a
      GLiNER2 label-competition effect (ORGANIZATION claiming an isolated caps line and person
      collapsing to 0.06–0.31, which windowing could not help) and it **died with the
      detector**: layer 0 scores 100% on seeds 42/123/7 where GLiNER2 scored 89/95/95
      ([reports/2026-08-09-text-layer0-vs-gliner2.md](reports/2026-08-09-text-layer0-vs-gliner2.md)).
      The candidate fixes are moot with it — labels-per-pass isolation, the names database
      below, a LoRA fine-tune. The promotion is the last step, and PERSON_JOINT is the
      precedent. Do it together with the gate work in Evaluation below, since promoting a probe
      into CRITICAL changes what the gate fails on. History in DONE.md.
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

      Two things to settle in the same pass. `CONTEXTUAL_ID` sits at 0% recall at every seed and
      is excluded from `CRITICAL` — decide whether that exclusion is still intended or is
      masking a real gap; layer 3 is nominally its owner and layer 3 is contingent. And
      `PERSON_REVERSED` is due for promotion into `CRITICAL` (item in Detection pipeline above),
      which changes what the gate fails on.

      Note the gate now needs a llama-server, which changes its character: it is no longer a
      cheap model-free check, and `-np 1` is required for the reproducibility it depends on.

## Nice-to-have

- [ ] "Match original font" for painted placeholders (Sergei, 2026-07-14) —
      estimate font size/weight (and maybe family) from the covered words' boxes/pixels so
      placeholders blend into the document instead of the current fixed-Arial
      shrink-to-fit. Also worth considering: match fill to the local background around the
      box rather than the page-wide most-common border color.
