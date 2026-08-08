# One-pass VLM experiment — Qwen3.6-27B on the real corpus

**Date:** 2026-08-08 · **Status: exploratory, nothing shipped.** Harness lives in the session
scratchpad; no `pii/` code was touched. This report records what was measured so the
engine-shaped decision in [TODO.md](../TODO.md) ("One-pass VLM pipeline") can be taken on
numbers rather than expectation.

Predecessor: [2026-07-17-ocr-bakeoff-round2-surya.md](2026-07-17-ocr-bakeoff-round2-surya.md),
whose operational findings drove most of the setup choices below.

## Landscape — the TODO's premise was stale

The TODO item assumed a "Qwen-VL class" grounding model. That branch no longer exists: Qwen
folded vision into the main line. **Qwen3.5** (Feb–Mar 2026) and **Qwen3.6** (Apr 2026) are
natively multimodal via early fusion, Apache 2.0, and Qwen claims they beat the old Qwen3-VL
series specifically on spatial grounding, document analysis and OCR.

| model | params | note |
|---|---|---|
| Qwen3.6-27B | 27B dense | MMMU 82.9, RefCOCO avg 92.5, 262K ctx |
| Qwen3.6-35B-A3B | 35B MoE, 3B active | untried; the obvious speed lever |
| Qwen3.5-{0.8,2,4,9,27}B | dense | small end, could fit the Windows box |

Other grounding-capable candidates surveyed and not pursued: **Moondream 3** (9B MoE / 2B
active, dedicated grounding tokens, MLX-only so it needs a Python toolchain the Mac lacks) and
**Molmo 2 8B** (Apache 2.0, but its documented output is *points*, not boxes — no rectangle to
paint).

## Setup

- **Qwen3.6-27B Q8_0** (28.6 GB) + `mmproj-F16` (0.93 GB), unsloth GGUFs, on the Mac M1 Max
  (64 GB). Q8 chosen deliberately so a negative capability result could not be blamed on
  quantization; Q4/Q5 untested.
- **llama.cpp b10326** (Sergei upgraded from b9968 for this). The compatibility was predicted
  from GGUF headers before downloading: the mmproj declares `clip.projector_type=qwen3vl_merger`
  and the model declares `general.architecture=qwen35`, so Qwen3.6 reuses the Qwen3-VL vision
  tower and loads on any build with `clip_graph_qwen3vl`. **b9968 does not have it**; b10326 does.
- Serving flags, each chosen against a Surya finding: `-np 1` (single slot — see determinism
  below), `--image-min-tokens 1024`, `--image-max-tokens 16384`, `--jinja` (needed for
  `chat_template_kwargs`, since Qwen3.6 is hybrid-thinking and a `<think>` trace over a page of
  numbers would wreck JSON extraction).
- **Corpus:** the 31 real pages of `sensitive/statements/1/` (11 documents, 5+ bank/insurer
  layouts) rendered at the pipeline's own 300 dpi.
- **Review oracle:** the PDFs' text layers. Not used for detection — the pipeline reads pixels
  on purpose — but as a review reference it is exact, and all 31 pages have one.

## Determinism — the Surya blocker is answered

Surya 2 was disqualified because three temperature-0 runs gave 6/3/5 critical leaks; the report
recorded single-slot serving as an untried lever. **It works.** With `-np 1`, three identical
runs produced byte-identical finding sets on both the 2B and the 27B. A VLM pipeline can be
gated.

## Detection quality

Prompt evolution mattered more than anything else measured here. Three versions, each change
earning its place on evidence:

| version | design | result on the insurance page |
|---|---|---|
| v1 | 14 classes mirroring `PLACEHOLDER_PREFIXES`, plus "do not report the issuer" | 11 findings, **3 misses** |
| v2 | 5 coarse classes, no institutional exclusions | 19 findings, 1 miss |
| v5 | + `policy, reference and claim numbers`, + identifiers-live-in-headings | **20 findings, clean** |

Three findings from that progression:

1. **Recall is bounded by the vocabulary you name.** v1 found every class it was given and
   nothing it wasn't — it missed a vehicle registration and a policy number, neither of which
   appeared in the prompt.
2. **Coarse classes generalize.** Collapsing 14 → 5 cost no recall and gained coverage: generic
   `PII_IDENTIFIER` caught `S745CHU` with no mention of vehicles anywhere in the prompt.
3. **A structural hint did not substitute for a concrete noun.** The sentence "identifiers can
   appear inside headings" *alone* did not recover the policy number; it came back only once
   `policy numbers` was named explicitly. Worth remembering before assuming principle-shaped
   instructions generalize.

### Class design (Sergei, this session)

The VLM detects; **layer-1 recognizers classify what they can**. The cut follows one test —
*can a deterministic recognizer re-derive this class from the string alone?* Identifiers: yes
(regex + checksum), so the VLM need not try, and measurably should not — it drifted
`CREDIT_CARD` ↔ `AU_BANK_ACCOUNT` on the same value between two runs. Names/addresses/
companies/DOB: no, only semantics decides, so they stay separate. DOB earns its own class for
exactly that reason — nothing but context distinguishes a birth date from a transaction date.

```
PII_NAME  PII_ADDRESS  PII_COMPANY  PII_DOB  PII_IDENTIFIER
```

Unclassifiable identifiers keep a generic `IDENTIFIER_GENERIC` placeholder — distinct from the
existing `*_INVALID` classes, which mean "matched a pattern, failed its checksum".

**Institutional exclusions were removed from the prompt on purpose.** Over-strip is recoverable
by the planned operator keep-list; under-strip is a breach. The exclusion belongs in a
deterministic, auditable, logged layer — not in a prompt where a model applies it silently and
per-page.

### Corpus sweep (values mode, v5, 31 pages, 445 findings, 74 min)

Zero parse failures. After triaging every flagged candidate, the residue:

- **One coherent recall gap — mailing-house control codes.** Two banks, same pattern, directly
  under the address block: `*#* 1584.3694.1.2 ZZ258R3 0303 SL.R3.S912.D170.O` (CommBank, all 3
  pages) and `234/484280500 / E-395 S-806 I-1611395` + `/000395` (NAB). Plausibly keyed to the
  customer's mail record, so a real linkability risk, and missed systematically.
- **Not a model failure — barcodes.** The long digit runs
  (`1320021111102112203002333…`) are Australia Post 4-state barcodes. They exist as digits in
  the *text layer* but on the page they are a graphic; a pixels-first reader cannot see them as
  digits at all. This is the existing barcode-masking TODO and no prompt will fix it.
- **Minor inconsistency:** `13 22 65` / `13 10 12` skipped, though v5 has no exclusions and the
  model did report `1300 306 560` elsewhere.

Two results worth singling out. On the known-hard pages the model caught **the account number
that leaks in the shipping default** (`162-097111-4` on `d11.p2`), the **Qantas frequent-flyer
number no current class covers** (issue #7), a **card number buried mid-sentence in prose**, and
the cross-column `VIC 3810` — while correctly leaving Westpac's ABN, `13 22 66`, AFSL 233714 and
the merchants alone. On the densest page (35 findings) it caught BSB `083-064`, account
`32-151-6825` and `30-743-3257` (the issue-#11 account).

**Caveat on that dense page, recorded because it nearly became a false finding:** its output
looks mangled (`Sk Busines`, `Olga and Sergei Kuli L2724656893`). Those strings are **verbatim
in the source** — the bank truncates its narrative fields to a fixed width. The model was
faithful.

## Grounding (boxes mode)

Detection ≠ grounding, and the first measurement was alarming: at the *default* vision-token
budget, the box for `162-097111-4` covered only `97111-4`, leaving `162-0` legible. Raising
`--image-max-tokens` to 16384 fixed it. **Vision-token budget is a correctness knob** — the
central Surya lesson, reconfirmed.

With the budget raised, boxes are reliable modulo a systematic, one-directional error:

| pad | fully covered (dense 19-finding page) | worst |
|---|---|---|
| 0 px | 6/19 | 95.8% |
| 4 px | 17/19 | 99.2% |
| **8 px** | **19/19** | **100%** |

Per-edge slack shows left/top/bottom carry safe overhang while the **right edge clips by ~2 px
median**. An 8 px pad at 300 dpi (0.68 mm) closes it completely. Over-painting is harmless for
redaction; under-painting leaks — so pad generously.

**Coverage alone is not sufficient** (Sergei's point: a single page-sized box leaks nothing and
is equally unusable), so tightness is scored alongside it — the fraction of the painted
rectangle that is actually the value, and the box-area ÷ ink-area ratio:

| | no pad | 8 px pad |
|---|---|---|
| fully covered | 6/20 | 20/20 |
| worst coverage | 91.4% | 100% |
| **box area ÷ ink area** | **1.39× median, 1.9× max** | 1.97× median, 2.9× max |

The boxes are genuinely snug — a halo of roughly half a character. The trade is explicit: 8 px
of pad buys guaranteed coverage for ~0.6× extra painted area. Whether a ~2× halo starts
colliding with neighbouring values on dense transaction tables is the open risk.

A capability the OCR path does not have: the model boxed a **logo** (`Budget Direct`) correctly.
There is no text layer there at all, so this is only reachable from pixels.

**Oracle limitation:** findings that are graphics (logos, barcodes, signatures) cannot be
validated against the text layer and are excluded from the box metric — they need visual
review. An earlier version of the matcher scored the logo box against `budgetdirect.com.au`
elsewhere on the page and reported 0% coverage and 8.4× bloat for what is in fact a near-perfect
box.

### Measuring this correctly took three attempts (all scorer bugs, all initially misread as model failures)

Recorded because each produced a confident wrong answer:

1. **Unanchored match window** — accepting "target appears anywhere in the accumulation"
   returned the union of every word from the page start, so correct boxes scored ~3%.
2. **Font boxes instead of glyph ink** — a PDF word rect spans ascender-to-descender plus
   leading, so a perfectly tight box around CAPS covers only ~⅔ of it. Reported ~65% coverage on
   visually perfect boxes. The reference must be *ink*, measured from the render. (Same concern
   as the `A line box contains its glyph ink` invariant, inverted: there engine boxes were too
   small, here PDF font boxes are too large.)
3. **Wrong occurrence of a repeated value** — `24 Stacey Dr` appears twice on the insurance
   page; scoring the box against the smallest match rather than the best-covered one reported
   0.0% for three correct boxes, which read convincingly as "16% catastrophic failures".

The metric is now validated against two pages independently verified by eye.

## Performance

Server-side, on AC power (battery throttling roughly doubled everything — the same request took
280 s on battery vs 148 s on mains):

| phase | rate | cost per A4 page |
|---|---|---|
| image ingestion | — | **~130 s** |
| prompt eval | ~80 tok/s | included above |
| decode | ~11 tok/s | ~20–45 s |

Decode is near the hardware ceiling (~14 tok/s = 400 GB/s ÷ 28.6 GB); **image ingestion is ~75%
of the cost** and scales with `--image-max-tokens`. Levers tried and rejected: `-fa on` and
`-ub 2048` gave *no* improvement (73 vs 80 tok/s prefill, decode unchanged). Untried: Q4/Q5
(decode is memory-bound, so ~2×) and **Qwen3.6-35B-A3B** (3B active — the biggest structural
lever). Image prefill is cached per image, so *additional passes on the same page cost ~16 s,
not 130 s* — which makes multi-pass schemes (themed groups, an audit pass) far cheaper than a
naive estimate suggests.

## Architectural direction agreed this session (Sergei)

On these results the one-pass VLM is "the most promising result so far and the avenue worth
developing". The agreed shape:

- **The layout/perception layer is not needed.** This is the best-supported deletion: the whole
  segmenter exists to *reconstruct* what the VLM sees natively. `d11.p2`'s account number leaks
  in today's default precisely because lines sort `(top, left)` and the value precedes its
  label; the VLM read it correctly with no layout machinery. Retires `ocr_page.py`,
  `linearization.py`, both layout backends, per-block feeding, orphan clustering, multiple trial
  linearizations, and the entire table-structure programme (`TableCellsDetection`/SLANeXt) —
  most of the open image-path TODO, including a known live leak.
- **GLiNER2 is not needed.** Beaten on this corpus at things it structurally cannot do (loyalty
  ID, policy number, vehicle registration, truncated narrative names). Its whole tuning surface
  goes with it. spaCy goes too — it is loaded only for Presidio's context enhancer.
- **Layer-1 validation stays**, in a narrowed role: classifier, checksum validator, and
  deterministic recall floor. Reasons it should not be collapsed into the VLM: checksums are a
  signal a VLM cannot produce (the `*_INVALID` classes are a product feature); the VLM is
  measurably unreliable at *classifying* (same value typed `CREDIT_CARD` in one run and
  `AU_BANK_ACCOUNT` in another); and a gate needs at least one provable, unit-testable layer.
  *Open, deliberately postponed:* whether that means keeping **presidio** or re-implementing the
  parts we need ourselves — `checksums.py` already owns the AU arithmetic. Note this **inverts**
  the TODO item "Stop duplicating Presidio's checksum arithmetic", whose proposed fix is to
  delegate *to* presidio.
- **PaddleOCR stays for now** — the verdict is not yet earned. Boxes cost recall (below), degrade
  on dense transaction rows, and nothing is tested on degraded input. Separately, the image-tier
  gate scores by *re-OCR*ing output pixels, so an independent OCR engine must be retained as the
  **instrument** even if it leaves the pipeline: the model under test cannot be its own scorer.

### Boxes cost recall — the finding that shapes the design

Same page, same prompt, same model: **values mode returns 20 findings including the policy
number; boxes mode returns 19 and drops it.** Emitting `bbox_2d` for every finding displaces
detection, and what it dropped was the hardest-won detection on the page. This argues against
end-to-end grounding in a single pass.

**Confirmed at corpus scale** (both sweeps, 31 pages, values compared on an alphanumeric squash):

| | values mode | boxes mode |
|---|---|---|
| distinct values | **350** | 324 |
| found only *without* boxes | — | **63** |
| found only *with* boxes | 37 | — |
| net | — | **−26 (−7.4%)** |

The 37 found only with boxes show this is churn rather than pure loss — the same stochastic
behaviour seen in box placement — but the net cost is real. Combined with the coverage numbers
below, `geometry="vlm"` is penalised twice: it detects ~7% fewer values *and* fully covers only
~65% of the ones it finds.

**Two-pass detect-then-localize — tested, result mixed (2026-08-08).** Pass 1 detects values
only; pass 2 is handed that list back and asked only *where* each one is. Measured on the
insurance page (19 distinct strings):

| | single-pass boxes | two-pass locate |
|---|---|---|
| recall | dropped the policy number | **all 19 located** |
| tightness (no pad) | 1.41× ink | **1.24× ink** |
| fully covered | **19/19 at 8 px pad** | 17/19 at *any* pad |
| worst coverage | 100% | 85% → 95.7% only at 2.7× bloat |

Two-pass recovers the recall *and* boxes more tightly, but leaves a residual **one-character
shift** on small print — visually confirmed: the AFS licence boxes cover `85571`/`41411`,
missing the leading `2` in both, while the phone numbers beside them are boxed correctly. That
is a *displacement*, not a shrink, so padding only fixes it by exceeding the whole shift, at
2.7× ink area and still short of full coverage.

### Box placement is stochastically unreliable — the decisive result

Full 31-page boxes sweep, 416 findings located against the text-layer oracle, scored at an 8 px
pad: **64.9% fully covered, 74.3% at ≥90%, median 100%, worst 0%**, box area 2.02× ink at the
median. So a quarter of all painted boxes would leave part of their value legible.

Measured over 227 findings on the subset available when the analysis was run (gross
wrong-occurrence matches excluded), the *worst inward intrusion on any edge* per finding:

| percentile | clip |
|---|---|
| p50 | +3.0 px |
| p75 | +9.2 px |
| **p90** | **+63.9 px** |
| p99 | +131.7 px |
| max | +237.8 px |

**16% of boxes clip by more than 20 px.** The median box is excellent; the distribution is
bimodal, and the tail contains real customer PII — not only institutional strings.

Position was investigated as a possible systematic cause (Sergei's hypothesis: cumulative drift
down the page). **It is not.** Median edge error is flat across five vertical bands (left −3.2 /
−3.5 / −1.9 / +1.0 / −1.6 px; top ≈ −7 px throughout), and small print is not systematically
worse than large (median left −2.9 vs −2.3). So there is no calibration to apply.

The clinching case: `162-097111-4` on `Statements_1114` **p4** is boxed as `97111-4`, leaving
`162-0` legible — while **p2 of the same document, identical layout, same token budget** boxes
the same value correctly. The failure is *stochastic*, not systematic, so it cannot be padded,
calibrated or predicted away.

**Consequence: do not paint VLM boxes.** The hybrid stands — **VLM supplies the values, OCR
supplies the geometry** — and PaddleOCR stays in the pipeline proper, not merely as the scoring
instrument. This is the opposite of the hoped-for simplification, and it is the single result
that most constrains the design.

### Risks named against this direction

- **Throughput**: ~3 min/page at Q8 is a research profile, not a product one (a 50-page bundle
  is ~2.5 h). Q4/Q5 and the 35B-A3B MoE become load-bearing rather than curiosities.
- **Single point of failure**: one model, one failure mode, no cross-layer disagreement — which
  the tier-3 metrics plan currently intends to use as a signal.

## Shipped this session — step 1 of the integration

`--detector vlm` is wired into the CLI for `--image`/`--pdf`, with **both geometry sources kept
deliberately** (Sergei's call — it keeps the comparison live and leaves the door open if boxes
improve with a different quant or model):

| flags | behaviour |
|---|---|
| `--detector layers` (default) | unchanged |
| `--detector vlm --geometry ocr` | VLM finds the values, each is located in the OCR text, painted with exact word boxes |
| `--detector vlm --geometry vlm` | the model's own boxes; **OCR never runs** |

New `pii/core/vlm.py` holds the transport (stdlib `urllib`, so `core` gains no dependency and
tests inject a fake), the tuned prompt, parsing, and `locate()`. Boxes are only *requested* when
`--geometry vlm` will use them, since asking for coordinates costs recall. `IDENTIFIER_GENERIC`
is now a real entity type (`ID_n` placeholder) in `DEFAULT_STRIP_ENTITIES` and
`PLACEHOLDER_PREFIXES`. 21 model-free tests; fast suite 316 green.

`locate()` is tiered — exact, then an alphanumeric squash ignoring spacing/hyphens/case — and
goes no fuzzier on purpose: an edit-distance match risks painting the *wrong* region, which is
worse than declaring the value unlocatable. Unlocatable values **warn loudly** and are counted,
because a detected value we cannot place is a value we cannot redact.

Verified on a real page, same input through both paths — the difference is visible in the
output pixels:

- `--geometry vlm` → `Account Number : 162-0` **still legible** beside the `ID_1` placeholder
- `--geometry ocr` → `Account Number : ID_1`, clean

The locator handled every finding on that page including `PAKENHAM\nVIC 3810`, which the VLM
returned as one span and which matched **across a newline** through the squash.

## Decisions taken after hands-on use (Sergei, 2026-08-08)

- **`--geometry vlm` is substandard; production uses `--geometry ocr`.** Confirmed by driving
  both paths on real documents, and consistent with everything measured above. The VLM-geometry
  path stays in the tool as a comparison instrument, not a production option.
- **Consequence — production never asks for boxes**, which recovers the measured **7.4% recall**
  that requesting `bbox_2d` costs. The production path is therefore better on *both* axes, not
  a safety-vs-quality trade. `_build_detector` already ties `want_boxes` to the geometry choice,
  so this follows automatically.
- **Consequence — refinement always has context.** With OCR text guaranteed present, layer-1
  refinement has one mechanism rather than two (a context-free per-value variant would have
  been needed for `geometry="vlm"`).

## Next: re-run the sweep at a lower `--image-max-tokens`

Deferred until the rest is settled (Sergei, 2026-08-08). The budget was raised to 16384 to fix
*box* precision; production takes no boxes, so its main justification is gone — and image
ingestion is ~130 s of the ~176 s per page, so halving it roughly doubles throughput.

The risk is the one Sergei raised when he first declined the reduction: image tokens set the
spatial resolution of the whole perception, not just coordinate precision, so **recall on small
print may degrade**. That is exactly what the re-run measures.

Clean A/B, because values mode needs no geometry oracle — just compare finding sets against
the established baseline: **445 findings / 350 distinct values over 31 pages at 16384**.

## Open questions

- Over-strip is **unmeasured**. With exclusions removed the model reports institutional data by
  design, so the number only becomes meaningful once the keep-list exists.
- The prompt was tuned on one corpus page (insurance p2), so that page's score is not
  independent.
- The review oracle only sees the text layer — anything purely visual (barcodes, signatures)
  is invisible to it.
- Tier choice is still open: values-mode + our own OCR geometry, versus boxes-mode end to end.
  The boxes result above makes the second viable, which would remove the string→box resolution
  step entirely.
