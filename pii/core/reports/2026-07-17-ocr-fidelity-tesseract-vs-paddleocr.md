# OCR fidelity: Tesseract 5.4 vs PaddleOCR PP-OCRv5_server vs PP-OCRv6_medium

**Date:** 2026-07-17 · **Instrument:** `python -m pii_eval ocr-report` (font × glyph-size
fidelity sweep; design in the 2026-07-17 DONE record) · **Data:** 1,980 cells per backend,
seeds 42/7/123, identical rendered pages, `pii_eval/reports/ocr_fidelity*.jsonl`
(gitignored; regenerate with `ocr-report --ocr-backend <backend>`) ·
**Stack:** Tesseract 5.4.0/LSTM (CPU, PSM 3 pipeline defaults); paddleocr 3.7.0 /
paddlepaddle-gpu 3.3.1 cu126 on RTX 2080 Ti (adapter defaults: preprocessing stages off,
`enable_mkldnn=False`).

**Verdict:** PP-OCRv6_medium wins decisively — ~25× lower CER than Tesseract and ~6×
lower than v5_server overall — and it effectively *abolishes the x-height cliff* that
defines Tesseract's failure envelope. v5_server beats Tesseract on aggregate but carries a
word-merge pathology that matters specifically for our identifier recognizers.
Recommendation: make `v6_medium` the paddle default tier.

## 1. Headline accuracy (weighted CER/WER, all 3 seeds)

| backend | prose CER | fixed CER | prose WER | fixed WER |
|---|---|---|---|---|
| tesseract | 3.48% | 6.87% | 10.93% | 18.83% |
| paddle v5_server | 1.40% | 1.15% | 11.61% | 8.08% |
| paddle v6_medium | **0.22%** | **0.20%** | **1.56%** | **1.32%** |

Note v5's prose **WER (11.61%) — worse than Tesseract's** despite 2.5× better CER. Not
noise; it's the merge pathology (§4).

## 2. The x-height cliff: Tesseract's defining failure is not universal

CER% by measured x-height band (all docs):

| backend | 4–5 px | 6–7 | 8–9 | 10–13 | 14–17 | 18–26 |
|---|---|---|---|---|---|---|
| tesseract | 44.63 | 3.03 | 1.11 | 0.96 | 1.20 | 0.96 |
| v5_server | 4.81 | 1.48 | 1.06 | 0.70 | 0.62 | 0.54 |
| v6_medium | **0.60** | **0.26** | **0.15** | 0.17 | 0.14 | 0.11 |

At x-height 4–5 px — where Tesseract loses up to 96.5% of characters (Courier) and the
tessdoc says text gets "noise removed" — v6_medium reads at 0.6% CER. The measured
"below ~150 dpi equivalent is unusable" rule is a *Tesseract property*, not a property of
small text. Font sensitivity collapses along with the cliff — em-10 prose CER across the
nine faces:

| font | tesseract | v5_server | v6_medium |
|---|---|---|---|
| arial | 11.4 | 4.5 | 0.3 |
| calibri | 44.9 | 8.9 | 0.8 |
| consola | 33.5 | 5.2 | 1.5 |
| cour | 96.5 | 9.2 | 0.7 |
| georgia | 14.4 | 6.1 | 1.1 |
| lucon | 17.6 | 1.4 | 0.1 |
| segoeui | 8.7 | 3.6 | 0.2 |
| times | 20.0 | 8.1 | 0.7 |
| verdana | 4.3 | 3.6 | 0.0 |

## 3. Structure preservation — the metric that predicts identifier leaks

Totals across all cells:

| metric | tesseract | v5_server | v6_medium |
|---|---|---|---|
| lines lost | 1,649 | 26 | **5** |
| spurious lines | 22 | 0 | 0 |
| resegmented lines (block fragmentation) | 21,960 | 0 | 0 |
| lost chars per 10k | 264.4 | 3.65 | **0.76** |

The s42 image-tier root-causing showed identifiers die of shape/layout damage (stranded
labels, collapsed spacing) — Tesseract's 21,960 fragmented lines and 1,649 lost lines are
that damage at scale. Paddle's detection-based architecture (no page-layout model) doesn't
produce the fragmentation class at all.

## 4. Error taxonomy — v5_server's merge pathology, v6's cleanliness

Errors per 10k truth chars:

| bucket | tesseract | v5_server | v6_medium |
|---|---|---|---|
| **merge** (space lost) | 25.8 | **68.0** ⚠ | **3.3** |
| split (space gained) | 41.0 | 1.4 | 1.1 |
| digit→digit sub | 13.3 | 0.16 | **0.01** |
| digit↔alpha sub | 23.5 | 12.1 | 6.9 |
| digit del/ins | 47.6 | 2.4 | **0.23** |
| alpha sub (non-case) | 24.3 | 5.2 | 2.0 |
| case-only sub | 8.6 | 18.8 | 3.9 |

- **v5_server merges words 2.6× more than Tesseract** (68 vs 26 per 10k). Merges are the
  single worst error class for us — "BSB 013-999" → "BSB013-999" breaks the pattern
  recognizer outright — which is why v5's prose WER is worse than Tesseract's despite far
  better glyph accuracy. Likely tunable (`text_det_unclip_ratio` is the known merge
  lever), but v6 makes tuning mostly moot: 3.3 merges/10k, 20× cleaner than v5.
- A chunk of the paddle error mass is **case flips** (v5: 18.8/10k; top confusions
  `o->O`, `n->N`, `e->E`) — semantically harmless for checksums and case-insensitive
  matching. Discounting case, v6's substantive error rate is ~0.1%.
- Digit integrity (all digit-involving errors per 10k chars): **tesseract 84.4,
  v5 14.7, v6 7.1** — v6's residual is almost entirely the `0↔O/o` family, which the
  image scorer's confusion squash already treats as one class.

## 5. Measured confusion signatures (top pairs)

- **tesseract**: `0->@` 1,674 (Consolas slashed zero — engine+font specific), `0->O`
  1,517, `F->E` 964, `5->S` 568, `J->I`, `1->2`, `0->8` — real digit corruption.
- **v5_server**: `0->O` 1,761, then case flips (`o->O` 1,346, `n->N` 861, `e->E` 786) —
  mostly benign.
- **v6_medium**: `0->O` 909, `W->w`, `S->s`, `0->o` — case + zero/oh only; no
  digit→digit pair in its top 10.

## 6. Confidence calibration — bad news for both engines, differently

| backend | mean conf (correct) | mean conf (erroneous) | erroneous words conf ≥ 80 |
|---|---|---|---|
| tesseract | 91.8 | 64.3 | 41% (n=44,354) |
| v5_server | 96.9 | 94.1 | **99%** (n=20,660) |
| v6_medium | 99.5 | 98.1 | **100%** (n=4,908) |

Tesseract's word-conf is a weak filter; paddle's line-conf is no filter at all — its
errors are as confident as its successes (partly granularity: one line score smeared over
every word). Never gate stripping decisions on OCR confidence; recall must come from the
detection layers. The ban is now backed by ~70k errors across two engines.

## 7. Throughput and operational cost

| backend | mean s/cell | median | p95 | total sweep |
|---|---|---|---|---|
| tesseract (CPU) | 1.44 | 1.03 | 4.25 | 47.6 min |
| v5_server (GPU 2080 Ti) | 2.42 | 1.31 | 7.53 | 79.8 min |
| v6_medium (GPU 2080 Ti) | 2.07 | 1.19 | 5.96 | 68.3 min |

Paddle on an 11 GB GPU is slightly *slower* per page than Tesseract on CPU (and paddle on
CPU is unusable at 30–95 s/page under the oneDNN-bug workaround). Paddle's accuracy comes
with a hard GPU dependency, near-ceiling VRAM (auto_growth high-water mark, WDDM spill on
the largest pages), and the torch mutual-exclusion process rules (2026-07-17 DONE
record). Tesseract remains the zero-infrastructure baseline.

## 8. Reliability

Per-seed CER is essentially constant across the three independent corpora (tesseract
5.28–5.58%, v5 1.18–1.33%, v6 0.21/0.21/0.21%), so nothing above is seed luck. All three
backends scored identical pages through the same alignment instrument.

**Caveats:** clean synthetic renders only — no noise, skew, blur, or JPEG artifacts (the
degradation tier is the next instrument); ~9 Windows system fonts, not scanned-document
typefaces; paddle conf is line-granular; Tesseract ran at pipeline defaults (PSM 3) — a
PSM-tuned Tesseract would close some of the structure gap, not the glyph gap.

## Decisions taken from this report (Sergei, 2026-07-17)

1. **Tesseract is to be retired** from the codebase — clearly inferior, not worth
   supporting as a second backend. Retirement plan with prerequisites in
   pii/core/TODO.md.
2. **v6_medium becomes the paddle default tier** (first step of that plan).
3. **Watch for a PP-OCRv6 server tier** — v6_medium already dominates; a v6_server would
   presumably be better still (none shipped as of paddlex 3.7.2).
4. **Idea recorded: 0↔O post-processing heuristic** — nearly all of v6's residual digit
   risk is one confusion class; a context-aware normalization in identifier-shaped
   tokens could remove it (TODO item).
5. Bake-off continues in a future session: Surya, and possibly a local VLM.
