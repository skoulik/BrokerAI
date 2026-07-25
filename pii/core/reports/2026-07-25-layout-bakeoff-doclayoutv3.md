# Layout bake-off: PP-DocLayout_plus-L (PP-StructureV3) vs PP-DocLayoutV3

**Date:** 2026-07-25 · **Corpus:** `pii_eval/corpora/real/1` — 31 pages of real bank
statements/certificates at 300 dpi · **Question (Sergei):** does a newer layout model
detect statement **tables** better than the one PP-StructureV3 ships with?

## What was compared

Only the **block source** changes. Both backends read text lines with the same pinned
PP-OCR tier (`PP-OCRv6_medium`), so recognized characters — and therefore CER, the leak
gate and every fidelity number — are held constant by construction.

| | `ppstructure` (existing) | `doclayout:v3` (new) |
|---|---|---|
| blocks from | PP-StructureV3 *pipeline*, layout submodule **PP-DocLayout_plus-L** | **PP-DocLayoutV3** standalone (`paddleocr.LayoutDetection`) |
| architecture | RT-DETR detector: boxes + labels | transformer with a **reading-order head** |
| reading order | pipeline's `xycut_enhanced` pass | the model's own prediction |
| taxonomy | 23 classes | 25 classes |
| lines from | pipeline-internal PP-OCR (`overall_ocr_res`) | direct `PaddleOCR` call |

## Headline result

| metric | plus-L | **V3 @ 0.3** | V3 @ 0.5 (untuned) |
|---|---|---|---|
| `table` blocks | 24 | **45** | 29 |
| orphan lines (in no detected block) | 117 (6.2%) | **20 (1.1%)** | 383 (20.4%) |
| detected blocks | 405 | 537 | 403 |
| lines | 1884 | 1878 | 1878 |
| wall time | 4.7 s/page | **4.5 s/page** | 4.5 s/page |

V3 nearly doubles table detection and cuts orphaned lines ~6×, at no time cost — the
layout model is ~0.1 s/page either way; PP-OCR dominates both.

Per-page swings on the statement pages that matter (orphans, tables):

| page | plus-L | V3 |
|---|---|---|
| `d04.p1` (ANZ cash account) | 6 orphans, **0 tables** | **0 orphans, 4 tables** |
| `d03.p2` | 32 orphans, 2 tables | 1 orphan, 3 tables |
| `d02.p4` | 19 orphans, 0 tables | 3 orphans, 2 tables |
| `d03.p1` | 20 orphans, 2 tables | 2 orphans, 2 tables |

The overlays make the difference plain on `d04.p1`: plus-L finds **no table at all** and
shreds each statement table into per-column `text` blocks (the summary values orphan
outright, as does the addressee name "SERGEI KULIK"), while V3 returns the transaction
table, the summary table, the account-details table — **and the BSB / Cash Account Number
label-value panel as a `table` block**. That panel is exactly the known failure the
`_layout_thresholds` seam was documented for (TODO "Decide the layout `text` threshold");
V3 solves it without touching any threshold.

## The trap: `draw_threshold` is not an operating point

The first run of this bake-off had V3 **losing** (383 orphan lines, and zero blocks on
four pages of `d11` — every line orphaned). Root cause: standalone `LayoutDetection` with
no explicit threshold falls back to `draw_threshold: 0.5` from the model's exported
`inference.yml` (`paddlex/inference/models/layout_analysis/predictor.py:164`) — a
**visualization** default PaddleDetection stamps into every exported model, not a tuned
operating point.

Every shipped pipeline overrides it, and each with the config written for *its* model:

- PP-StructureV3 → plus-L: per-class dict (`text` 0.4, `paragraph_title` 0.3, rest 0.5).
- PaddleOCR-VL-1.6 → PP-DocLayoutV3: flat **0.3**, `layout_nms: True`,
  `layout_unclip_ratio: [1.0, 1.0]`, per-class `layout_merge_bboxes_mode`.

So the original comparison was tuned-vs-untuned. `ocr_doclayout._shipped_knobs` now lifts
the operating point out of the pipeline config that **names our model**, newest first —
which also guarantees the index-keyed dicts inside it are keyed by our model's own
taxonomy (the retargeting hazard `ocr_ppstructure._layout_thresholds` warns about).

## Threshold sweep (V3, all other knobs shipped)

| threshold | blocks | tables | orphan lines |
|---|---|---|---|
| 0.1 | 447 | 54 | 2 (0.1%) |
| 0.2 | 501 | 48 | 2 (0.1%) |
| **0.3 (shipped)** | **537** | **45** | **20 (1.1%)** |
| 0.4 | 484 | 35 | 205 (10.9%) |
| 0.5 (`draw_threshold`) | 403 | 29 | 385 (20.5%) |

Block count is **not monotonic**: below 0.3 more overlapping candidates survive to be
merged by `layout_nms` + the `union`/`large` merge modes, so blocks get *bigger and fewer*
while coverage still improves. The recall cliff sits between 0.3 and 0.4, so the shipped
0.3 is just on the safe side of it. Going lower buys the last ~18 orphan lines at the risk
of over-merging — which these proxy metrics cannot see (no block-level ground truth
exists); only overlay inspection can judge it. **Kept at the shipped 0.3.**

## Secondary findings

- **Line counts differ by 6 across the corpus** (1884 vs 1878) — the two backends do not
  share a line source after all: PP-Structure OCRs through its own pipeline feed, which
  *fragments* some lines. Measured on `d08.p1`: PP-Structure yields `'Pa'` + `'Page 1 of'`
  where the direct `PaddleOCR` call yields `'Page 1 of 1'`; same on a barcode line in
  `d10.p1`. Every difference found favoured the direct call. Fidelity is therefore not
  *exactly* constant across backends, but it moves in the right direction.
- **V3 emits `polygon_points` per block** (4-point quads) alongside the axis-aligned
  `coordinate`. Not stored: nothing consumes block polygons and `ocr_debug.page_to_dict`
  does not serialize them, so filling `OcrBlock.polygon` would silently break the JSON
  round-trip. It is the lever for skewed scans.
- **Reading order comes from list position, not the `order` field.** paddlex sorts the
  result list by the model's reading-order column (`processors.py:938`), then
  `update_order_index` numbers 1..N over it while **blanking every label in
  `SKIP_ORDER_LABELS`** — which includes `table`, `image`, `header`, `footer`. Ranking by
  that field the way PP-Structure's `order_index` is ranked would push every statement
  table to the end of the page. Covered by a regression test.
- **Per-block confidence** is recorded on `OcrBlock.conf` (score × 100); plus-L blocks
  reach us through PP-Structure without one.

## Recommendation

Adopt `doclayout:v3` as the default `OcrPage` backend. It dominates on every measured
axis (tables, orphans, speed), needs no tuning of ours, and comes with model-predicted
reading order. It also makes the pending TODO "Decide the layout `text` threshold" moot
for the default path — the panel that motivated it is now a detected block.

**Adopted** (Sergei, on these numbers, same day): `get_ocr_page` and `--ocr-backend`
default to `doclayout:v3`. `ppstructure` stays selectable and unchanged as the comparison
baseline. The strip path still runs `get_ocr`/`OcrResult` and is untouched by the switch.

## Reproducing

Comparison and sweep scripts are throwaway (session scratchpad, not committed); the
measurements come through the public seam:

```
python -m pii debug ocr <page.png> --ocr-backend doclayout:v3 --format overlay -o out.png
python -m pii debug ocr <page.png> --ocr-backend ppstructure   --format overlay -o out.png
```
