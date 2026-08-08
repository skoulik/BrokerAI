# TODO — PII engine (core)

All open engine tasks, with full working detail. The activity overview and evaluation-tier
plan are in [ROADMAP.md](ROADMAP.md); completed tasks and their engineering records are in
[DONE.md](DONE.md); design decisions (the *why*) in [ARCHITECTURE.md](ARCHITECTURE.md).
Front-end tasks live with their component: [../cli/TODO.md](../cli/TODO.md),
[../gui/TODO.md](../gui/TODO.md).

Grouped by theme. Suggested order on the image/PDF track (2026-07-14, amended 2026-07-18;
fidelity sweep + bake-off rounds 1 AND 2 + Tesseract retirement + **PDF mode (2026-07-18)**
done — see DONE.md and reports/; round 2 evaluated and retired Surya 2 same-day, docTR
dropped unevaluated): demo on the reference documents → degradation tier → one-pass VLM
experiment (future session; owns the next engine-shaped decision).

## Direction change — 2026-08-08, read before picking anything up below

The one-pass VLM landed and changed what is worth building. On the evidence in
[reports/2026-08-08-vlm-oneshot-qwen36.md](reports/2026-08-08-vlm-oneshot-qwen36.md), Sergei's
verdict is that this is "the most promising result so far and the avenue worth developing", and
the agreed shape is:

- **The layout/perception layer is not needed** — the VLM reads spatial structure natively,
  which is why it got `d11.p2`'s account number right where the shipping default leaks it. That
  makes most of the "OCR perception / linearization" section below, the 2026-07-25 session plan,
  and the whole table-structure programme **expected retirements rather than planned work**.
- **GLiNER2 (and spaCy with it) are not needed** — beaten on this corpus at things they
  structurally cannot do. The "Experiments — GLiNER2 tuning" section is in the same position.
- **Layer-1 validation stays**, narrowed to classifier / checksum / recall floor. Whether that
  means presidio or our own code is postponed — note it *inverts* the "Stop duplicating
  Presidio's checksum arithmetic" item below, whose proposed fix is to delegate *to* presidio.
- **PaddleOCR stays.** VLM boxes are stochastically unreliable, so OCR supplies geometry;
  `--geometry ocr` is production and `--geometry vlm` is a comparison instrument only.

Nothing is deleted yet, because the VLM path is opt-in and layer-1 refinement is not built —
if it stalls, the items below are still the plan. Retire them only once the VLM path is the
default and measured on the leak gate.

## Session plan — set by Sergei 2026-07-25, in this order (SUPERSEDED — see above)

After eyeballing the whole sensitive corpus on the new `doclayout:v3` default ("almost there
in terms of layout understanding"). Each step below has a fuller entry further down; this is
the running order, and it is deliberately layout-first-then-back-to-e2e:

1. ~~**Root-cause the remaining orphans.**~~ First cluster classified 2026-07-27 (record kept
   in the orphan item below); the rest is folded into steps 3/4.
2. ~~**Back to e2e: teach the GLiNER feeder about blocks — feed lines per block.**~~ **DONE
   2026-07-27** (`--feed blocks`; record in DONE.md, evidence in
   reports/2026-07-27-per-block-feed-bakeoff.md). −4 critical leaks against its own backend
   control at no time cost. It also moved step 4 to the front of the queue: see below.
3. **Multiple trial linearizations with overlaps.** Several assemblies of the same page,
   windows overlapping so an entity split across a boundary is caught by at least one trial —
   the reason offsets live per-linearization in the source map, never on perception. Now also
   the mitigation for the per-block feed's known cost (a label in one block can no longer
   promote a value in the next): a whole-page trial unioned with the per-block trial gets both.
   Needs a merge rule for spans living in different offset spaces — union in *word* space via
   the source maps, not in character space.
4. **Table structure and other heuristics — now the top item, and it carries a known leak.**
   Cell/row/column structure for the 45 detected `table` blocks (`TableCellsDetection` first —
   non-generative geometry — with SLANeXt structure only if logical spans/headers turn out to
   be needed), plus the remaining statement-row heuristics. Promoted because the 2026-07-27
   feed bake-off traced a real leak to it: lines inside a block are emitted in `(top, left)`
   order, so a multi-column header panel emits a value before its own label and context
   promotion never fires (`d11.p2`'s account number, evidence in the report). That leak is
   **live in the shipping default** since `doclayout:v3` + `--feed blocks` was adopted — it
   was accepted knowingly (net 8 leaks vs the old default's 9), and this item is the repayment.
   Same defect as issue #8a below.

## Next up — image/PDF path

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
- [ ] OCR engine choice — *decide later:* PaddleOCR (current, v6_medium default; the
      last classic-OCR candidate standing after rounds 1–2) vs the one-pass VLM pipeline
      below. Decide on benchmark numbers from real bank statements/scans (needs the image
      eval tier for ground truth). The engine seam is the parallel-lists word-box dict in
      `pii/core/ocr.py` (each backend is an adapter normalizing into it).
- [x] ~~**One-pass VLM pipeline**~~ — **DONE 2026-08-08**, shipped as layer 0
      (`--detector vlm`). Record in [DONE.md](DONE.md), design in
      [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0", evidence in
      [reports/2026-08-08-vlm-oneshot-qwen36.md](reports/2026-08-08-vlm-oneshot-qwen36.md).
      Verdict: detection excellent, grounding stochastically unreliable, so **PaddleOCR stays
      and supplies geometry**. The follow-ups it spawned are the next three items.

- [ ] **Layer-1 refinement of VLM findings** (step 2, designed 2026-08-08, not built — Sergei
      paused implementation). Today every VLM-detected identifier strips as
      `IDENTIFIER_GENERIC` (`ID_n`), so the VLM path forks placeholder identity against the
      layers path (the same account number is `ACCOUNT_1` under `--detector layers` and `ID_2`
      under `--detector vlm`) and the `*_INVALID` classes are absent. Layer 1 should run
      alongside and do three jobs: **refine** (adopt layer 1's precise type where a layer-1
      span overlaps a VLM span), **validate** (restore the checksum-invalid shadows — the
      signal a VLM structurally cannot produce), and **union** (add layer-1 findings the VLM
      missed, as a deterministic recall floor). Needs an overlap rule rather than trusting
      layer 1 blindly: it classified the AFSL number `237502` as a phone. Production is
      `--geometry ocr`, so OCR text — and therefore recognizer *context* — is always
      available, which is why one refinement mechanism suffices.

- [ ] **Serving / llama.cpp tuning job** (scoped 2026-08-08, deferred to its own session).
      ~176 s/page at Q8_0 is a research profile, not a product one — a 50-page submission
      bundle is ~2.5 h. Time splits **~130 s prefill (image ingestion, 74%)** and **~45 s
      decode (11 tok/s against a ~14 tok/s memory-bound ceiling)**, so a lever only matters if
      it attacks the right half. Already tried and rejected: `-fa on` and `-ub 2048` gave *no*
      improvement (73 vs 80 tok/s prefill, decode unchanged). Battery throttling halves
      everything — check `pmset -g ps` before trusting any number.

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

      Ordering rationale: 1 attacks the biggest slice and costs nothing but a measurement; 2 is
      cheap and independent; 3 changes the model so it needs a quality re-check; 4 is last
      because it is the only one that buys speed by giving up a property we rely on.
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
      Better idea than either, if picked up (Sergei's, 2026-07-25): run spotting **per detected
      block** using the layout blocks we now have — quantization becomes 1/1000 of the crop,
      sub-1500px crops get a free 2× upscale before the encoder (attacking the vision-token
      starvation that killed Surya), generations stay short (page-wide runs risk
      `truncate_repetitive_content` silently dropping repeated statement lines), and the
      line→block linkage becomes exact instead of reconstructed. The pipeline will not do this
      itself — prompt choice is hardcoded per block label — so we would crop and batch the
      crops through `predict([...])` ourselves. Two risks to design for: text outside every
      detected block never gets read (mitigate with a det-only PP-OCR coverage sweep), and
      glyphs flush against a crop edge read worse (pad crops; painting stays on original
      pixels). Keep the Surya round-2 lessons in view — silent omission is the VLM failure mode
      that matters for redaction.
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

## OCR perception / linearization (2026-07-24)

The OcrPage / linearization / PP-StructureV3 backend / `debug ocr` layer shipped (record in
DONE.md; design in ARCHITECTURE.md "OCR perception layer"); it runs alongside the untouched
`OcrResult` strip path. Open follow-ups:

- [ ] **Root-cause the remaining orphan lines** (Sergei, 2026-07-25 — step 1 of the session
      plan; he eyeballed the whole sensitive corpus on `doclayout:v3` and some orphans "look
      suspicious"): 20 orphan lines survive corpus-wide (down from 117 under `ppstructure`).
      An orphan means PP-DocLayoutV3 emitted no block covering that line — `_assign` already
      falls back to largest-overlap, so it is never a linkage bug. Per-page counts from the
      bake-off: d02.p5 6, d11.p4 4, d02.p4 3, d03.p1 2, d09.p2 2, then singles on d02.p2,
      d03.p2, d10.p1. Classify each before fixing: page furniture the model deliberately
      ignores vs a real detection miss vs a line whose box straddles two blocks. Candidate
      fixes, in increasing order of commitment — the orphan-clustering item below (adapter-side,
      no model change), a threshold nudge (0.2 halves orphans to 2 corpus-wide but merges
      blocks larger — sweep in reports/2026-07-25-layout-bakeoff-doclayoutv3.md), or nothing if
      they are all furniture. Note orphans are *not* a leak by themselves (every line still
      reaches the recognizer in its own synthetic block) — they are a structure-quality signal,
      and they matter more once feeding is per-block (step 2), because an orphan then becomes a
      one-line context-free window. **Since 2026-07-27 that "once" is now** — under
      `--feed blocks` an orphan line IS a one-line analyzer call with no context whatsoever,
      which is exactly the regime the `BSB`-alone unit test shows detects nothing. Orphan
      clustering therefore stopped being a tidiness item and became a recall item.

      **First cluster classified (2026-07-27, `Statements - 1114.pdf` p2 at 200 dpi — a real
      detection miss, not furniture):** 16 of 53 lines orphan, and they are exactly the top
      header panel — the addressee block (`THE DIRECTOR` / `25 OAKLANDS WAY` / `PAKENHAM` /
      `VIC 3810`) and the account panel (`Account Number : 162-097111-4`, statement period,
      statement number, page). V3 *does* see the panel: running the model directly on the same
      raster, it emits `table [6,67,1593,292]` covering the whole thing — at score **0.296**,
      against the shipped `threshold: 0.3`. It misses by 0.004. The whole page sits on the cut
      (the seven surviving blocks score 0.38–0.46; at the un-overridden 0.5 default V3 detects
      *nothing* here). Page-local sweep: 0.3 → 7 blocks, 0.2 → 8 (panel recovered), 0.1 → 8,
      **0.05 → 2 page-sized blobs** — `layout_merge_bboxes_mode: union` chaining low-score
      boxes. So the cliff below the useful range is real but is not where a nudge would sit.

      **The cost is reading order, not the orphaning as such.** Orphan blocks are appended
      *after* the detected run and `linearize` walks lines in emission order, so the address and
      account number are emitted at the very END of the page string, after the whole transaction
      table, and interleaved between the two columns in paddle's detection order (`THE DIRECTOR`
      / `Account Number` / `: 162-097111-4` / `25 OAKLANDS WAY` / …). The multi-line address is
      shredded. **A threshold nudge alone does not fix this** — the panel comes back as one
      full-width block and lines within a block sort by `(top, left)`, so the two columns still
      alternate. Whatever the fix, it has to reach column structure, which points at the
      orphan-clustering item and step 3 rather than at the threshold. (For contrast,
      `ppstructure` reports 0 orphans on this page only because it emits 2 blocks total, one
      `table [0,67,1587,931]` swallowing the panel *and* the transaction table — no structure to
      be orphaned from.)
- [ ] **Multiple trial linearizations with overlaps** (Sergei, 2026-07-25 — step 3 of the
      session plan): run several assemblies of the same page and union the findings, with the
      windows overlapping so an entity broken by one trial's boundary is intact in another.
      This is what the source map was designed for (offsets per linearization, never on
      perception); relates to the existing GLiNER2 cell-isolation windows and person-fragment
      coalescing, which are the same problem at a smaller scale. Since the per-block feed
      landed (2026-07-27) this is also its safety net — a whole-page trial recovers the
      cross-block context per-block feeding gives up. Open design question: spans from two
      trials live in different offset spaces, so the union has to happen in *word* space
      through the source maps.
- [ ] **Finish the strip migration onto `OcrPage`/`RecognizerInput`** and retire
      `OcrResult`/`assemble`. Landed 2026-07-27: `image_mode.strip_from_page` +
      `strip_image`/`strip_pdf` routing, so both feeds run over `OcrPage` today. Remaining:
      make it the *only* path — the `OcrPage` path is the default since 2026-07-27, but the
      flat `OcrResult` path stays reachable (`--ocr-backend paddle --feed page`) and is worth
      keeping until intra-block column structure repays the `d11` regression (step 4), since
      it is the only configuration that still bands columns into visual rows; the worker's
      strip path still speaks the bare-tier spec; and the harness still resolves through
      `get_ocr`
      (`OCR_BACKENDS`, line-only) in `ocr_report` and in the scorers' *read-back*
      (`score_image.reread_engine` — deliberately pinned there, so a backend/feed comparison
      measures with a constant instrument; only `ocr_report` is a real migration debt).
- [ ] **Font traceback** (diagnostics-only): fill `OcrLine.font` / `OcrBlock.font` from the PDF
      text layer (pymupdf `get_text("dict")` spans matched to line boxes) — `None` from any OCR
      engine. Must never feed the strip decision (we deliberately distrust the text layer).
- [ ] **`kind="table"` blocks** (parked 2026-07-24; more urgent since 2026-07-25 — the
      `doclayout:v3` default returns ~2× as many table blocks, 45 vs 24 corpus-wide): with
      table-structure recognition off, table text arrives as ordinary lines under a `table`
      block — verify that's enough for PII on real table-heavy statements (interacts with the
      "statement tables" item above); only reach for `child_blocks`/cell structure if it isn't.
      Observed on the real ANZ statement (2026-07-24): the balance-summary `table` block was
      detected cleanly, but its lines interleave label/value in reading order (a stray `$0.00`
      mid-run) — within-table line ordering may need work if per-block feeding relies on it.
      Re-check on V3's blocks, whose table boxes are larger (the BSB/account label-value panel
      is now one `table` block, so the interleaving question applies to it too).

      **Measured cost, 2026-07-27 (feed bake-off): a leak that is live in the shipping
      default.** Lines inside a block sort by `(top, left)`, so a
      three-column header panel interleaves — `d11.p2` emits `': 162-097111-4'` *before*
      `'THE DIRECTOR'` and `'Account Number'`, all 15 lines inside one correctly detected
      `table` block — and the account number's context promotion never fires, leaking it under
      both feeds (the flat `paddle` path survives only because `_rows` bands side-by-side
      regions into one visual line). Cell geometry gives line→(row, column), which is the
      fix; it is the same defect as issue #8a in the Detection pipeline section below.

      **Model inventory for internal table structure** (researched 2026-07-25, nothing built —
      step 4 of the session plan). PP-DocLayoutV3 itself gives none of this: its 25 classes
      include exactly one table-related label (`table`), no cell/row/column/header. paddlex
      ships it as three separate models, all reachable standalone the way `LayoutDetection` is:
      `TableClassification` (`PP-LCNet_x1_0_table_cls`, wired vs wireless router),
      **`TableCellsDetection`** (`RT-DETR-L_wired_table_cell_det` / `…_wireless_…`) → per-cell
      BOXES, non-generative — the one to try first, since cell geometry alone gives
      line→(row, column) by the same containment discipline we already own; and
      `TableStructureRecognition` (`SLANet`, `SLANet_plus`, `SLANeXt_wired`, `SLANeXt_wireless`)
      → an HTML token sequence with `<thead>`/`colspan`/`rowspan` plus per-token boxes, i.e.
      logical spans and header/body, but *generated* (hallucination/truncation risk on the long
      many-row tables statements are full of, and its training distribution is scientific
      tables). `TableRecognitionPipelineV2` orchestrates all three and exposes `cell_box_list`,
      `pred_html`, `table_ocr_pred` (OCR split per cell) and `split_ocr_bboxes_by_table_cells`
      (splits a line box spanning ≥k cells — exactly the statement-row operation). **Consume it
      ourselves, not via PP-Structure**: the 2026-07-25 finding that table recognition "would
      not change blocks" was about switching it on *inside* PP-StructureV3, which only writes
      `block.content = pred_html` (never read by our adapter) and blinds the `num_of_lines`
      cross-check. Open design question it forces: cells need a level between block and line
      (or a parent/child relation on `OcrBlock`) — a perception-hierarchy change, and per-block
      feeding then becomes per-*cell* feeding.
- [ ] **Decide the layout `text` threshold** — *moot on the default path since 2026-07-25*,
      kept for the `ppstructure` backend only. PaddleX's shipped per-class cut drops
      label/value header panels whole, so their lines arrive as one-line synthetic blocks —
      59 orphan lines over the 31-page real corpus, 22 on one statement page.
      `_layout_thresholds({"text": 0.33})` cuts that to 19 while *raising* the block count
      (393 → 398), and turns the panel into two ordinary `text` blocks. The `doclayout:v3`
      backend (now default) returns that panel as a detected `table` block with no tuning at
      all — 20 orphan lines corpus-wide — so this only matters if `ppstructure` is revived.
      Still open on the adapter side, and it applies to BOTH backends: cluster adjacent orphan
      lines into one synthetic block and insert them in reading order by geometry, rather than
      one-per-line appended after every detected block.
- [ ] **CPU-wheel PP-Structure**: `_structure_engine` sets `device="cpu"` off the GPU wheel but
      is untested there; check the paddle 3.3.x oneDNN PIR-executor crash (the `enable_mkldnn`
      lever plain PaddleOCR needs) doesn't bite PP-Structure.
- [ ] **`use_region_detection`** defaults on (downloads `PP-DocBlockLayout`); evaluate whether
      the coarser region grouping helps reading order on multi-column statements or can be
      dropped to save a model.

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
- [ ] OCR column segmentation for label/value header blocks (issue #8a, 2026-07-22).
      Two-column page headers (ANZ: left 'Postal Address' → address lines, right 'Trading
      Account Number' → '314811') band into single assembled lines by design — side-by-side
      cells ARE one visual row — so the text reads '24 STACEY DRIVE, CARRICKALINGA SA 5204
      314811' and GLiNER2 emits the WHOLE line as one ADDRESS span (0.99; its addr-split pass
      even scores the bare '314811' as a locality line at 0.45). Everything strips, so no
      leak — the damage is aliasing ('314811' hides in ADDRESS_n instead of getting the
      consistent ACCOUNT_n it gets elsewhere) and label/value association (the account's
      label sits one row up in its own column, out of pattern/context reach). Fix class:
      detect column structure in the OCR layer and isolate columns as separate segments —
      the RECORD_SEPARATOR cell-isolation precedent (csv_mode → GLiNER2 windows) is the
      mechanism to reuse. Scope decision needed: header blocks only, or general multi-column
      handling (interacts with _rows and the transaction-table banding that MUST stay
      row-wise).
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
