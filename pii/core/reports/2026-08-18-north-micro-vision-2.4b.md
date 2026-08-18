# North Micro Vision 2.4B — ad-hoc capability probe

**Date:** 2026-08-18 · **Status: exploratory, nothing shipped.** Harness lives in the session
scratchpad; no `pii/` code was touched. Requested as an opportunistic look at whether a
*small* VLM could replace the 27B on the image path, since throughput is the top open risk in
[TODO.md](../TODO.md).

Predecessor: [2026-08-08-vlm-oneshot-qwen36.md](2026-08-08-vlm-oneshot-qwen36.md), whose
corpus, review oracle and clip metric this reuses so the two are directly comparable.

**Verdict: not a candidate for layer 0.** It reads documents genuinely well and it is ~7x
faster on hardware we already own, but its ceiling on this corpus is **75% recall taking the
union of three prompt formats** — and what it misses is bank account numbers. Under-strip is a
breach, so a 25% miss rate at 3x the compute is not a prompt-tuning gap. Recorded because the
*deployment* findings and the `[]` failure mode are reusable, and because two of them are
properties of small VLMs generally, not of this model.

## The model

[`CohereLabs/North-Micro-Vision-Instruct`](https://huggingface.co/CohereLabs/North-Micro-Vision-Instruct),
Apache 2.0, 2.4B total: a 400M native-resolution vision encoder (SigLIP 2 SO400M lineage) and a
2B "Command A+" LLM with interleaved sliding-window and global attention. Trained
document-first — 40-50% OCR/document data per stage, 17.8% charts/tables, 13.3% visual
grounding. DocVQA 0.921, ChartQA 0.808, RefCOCO 0.732; MMMU 0.329.

Two properties made it worth the time. It is built around **A4 at 200 dpi** (1654x2339) with no
tiling, which is exactly this pipeline's workload; and it emits boxes as `[x1,y1,x2,y2]`
**normalized to 0-1000**, the same convention `_LOCATE_PROMPT` and `_G_ITEM_BOXES` already
speak, so a grounding probe needed no coordinate translation.

## Setup

- **transformers 5.16.0.dev0, installed from git** (`uv pip install --system
  "git+https://github.com/huggingface/transformers.git"`). `config.json` declares
  `model_type: cohere_compass`; PyPI's newest stable is 5.15.0, which does not have it. The
  full testbench (695 tests) passes on the upgraded install.
- **RTX 2080 Ti (11 GB), fp16** — see below, the dtype is forced, not chosen. 4.63 GiB
  weights, 7.44 GiB peak at 200 dpi, 13.7 s to load.
- **Corpus:** 11 pages of `pii_eval/corpora/real/1`, one per document so all 11 lender layouts
  are represented, each the richest page in its document. 188 scored PII occurrences.
- **Oracle:** the existing `truth.json` (text-layer ruler, 300 dpi pixel boxes).
- Greedy decode (`do_sample=False`) throughout.

## Deployment findings

**fp16 is mandatory on this card, and the reason generalises.** The vision tower runs *global*
attention over every patch at native resolution — ~15k patches for A4 at 200 dpi. The Windows
torch 2.13+cu130 wheel is **not compiled with flash attention**, and SDPA's memory-efficient
kernel **refuses bf16** (`Expected query, key and value to all be of dtype: {Half, Float}`), so
a bf16 load falls back to the math path and tries to allocate **13.74 GiB for one attention
matrix**. The same op in fp16 under `EFFICIENT_ATTENTION` takes **0.06 GiB**. Any
native-resolution encoder will hit this on a pre-Ampere card; the fix is the dtype, not the
image size.

Second, `cohere_compass` derives RoPE positions from the token-type layout
(`get_rope_index`), so **an assistant prefill must go through
`apply_chat_template(continue_final_message=True)`**. Appending token ids to `input_ids` by
hand desyncs the attention mask by exactly the prefill length and crashes.

## The `[]` collapse — the finding that changes the measurement

**Every JSON-shaped prompt returns exactly `[]`**, on every page and every class, including
this repo's own production `PROMPT`. Taken at face value the model detects nothing.

It is a decoding-start artifact. **Prefilling the assistant turn with `["` unlocks it**, prompt
unchanged:

| prefill | reply |
|---|---|
| *(none)* | `[]` |
| `["` | `["<the correct org name>"]` |

Two consequences worth carrying forward:

- Removing the `If the page contains none, output []` sentence does **not** fix it — the model
  still answers `[]` on most pages, and where it does answer it becomes unstable.
- **The prefill that fixes the collapse also removes the model's ability to report a clean
  page.** `["` is an instruction to emit at least one item. Asked for dates of birth across 11
  pages containing **zero**, the prefilled form produced **53 predictions, 50 matching
  nothing** — splitting a lender's published contact number into its three digit-groups and
  returning them as three separate dates of birth, and inventing plausible dates outright. In free-form prose it correctly declines ("the dates of birth are
  not provided"). This is a direct coupling, not a tuning knob: for a class that is often
  absent, the prefill converts silence into confabulation.

A sampler-level grammar (the transformers analogue of the GBNF this repo already uses) would
likely dominate the prefill hack, since it constrains form without mandating content. Untested
— it needs `xgrammar`, which is not installed.

## Detection — one class per call

Five classes, three output formats, 11 pages, 165 calls, 20 min wall. Recall is scored on the
raw reply (did the model read the value at all); predictions are split three ways — `on` matches
a truth value of the class **asked for**, `off` matches a **different** class's value (a
filtering failure), `noise` matches nothing.

| class | format | recall | precision | note |
|---|---|---|---|---|
| ADDRESS | lines | **21/21 · 100%** | 14% | `json_pre` 81% at 61% precision |
| COMPANY | lines | 51/64 · 80% | 23% | `json_pre` 73% at 64% — the better trade |
| NAME | json_pre | 18/25 · 72% | 55% | `bare` gets 28% — format moves recall **2.6x** |
| IDENTIFIER | lines | 47/78 · **60%** | 23% | the class that matters most, and the worst |
| DOB | — | n/a (no truth) | 0% | fabricates; see above |

Format is the dominant variable and the trade is clean:

| format | recall | precision | s/call | truncated |
|---|---|---|---|---|
| `lines` (one per line) | **70%** | 27% | 11.2 | 11/55 |
| `json_pre` (`["` prefill) | 59% | **40%** | 5.1 | 1/55 |
| `bare` (plain question) | 49% | 32% | 5.5 | 0/55 |

The mechanism behind that trade is visible in the raw replies. Asked a **question**, it
*summarizes* — short, well-typed, and incomplete. Told to **list**, it stops filtering by class
and starts *transcribing the page*: asked for person names on one page it returned 118 items,
**83 of which were other classes' values**. Neither behaviour is "find every occurrence".

### The ceiling

Union of all three formats — best case, 3x the compute:

| class | union recall |
|---|---|
| ADDRESS | 90.5% |
| COMPANY | 79.7% |
| NAME | 72.0% |
| IDENTIFIER | 67.9% |
| **TOTAL** | **141/188 = 75.0%** |

The residue is the wrong tail: **17 bank account occurrences** (one 9-digit account never read
on any page in any format), 2 ATO reference numbers, 2 ABNs, 4 phone numbers, one person's
name. Layer 1 cannot rescue these — it refines and validates what layer 0 names, and these were
never named.

## Detection — this repo's full `PROMPT`

Same 11 pages, the production prompt verbatim:

| variant | recall |
|---|---|
| `PROMPT + _OUTPUT_VALUES`, as-is | **0/193 · 0.0%** — `[]` on every page |
| same + `[{"type": "` prefill | **48/193 · 24.9%** |

Against 75% for per-class decomposition, the five-class taxonomy in one prompt **costs two
thirds of the recall**. Four of 11 pages ran the full 1024-token budget (~49 s) without ever
closing the JSON array. This is the clearest single result in the probe: at 2.4B, one prompt
per class is not a stylistic choice, it is the only regime that works.

## Grounding

Values are taken from the oracle, so this measures *only* localisation. Scored per distinct
value against its best-matching occurrence, since one call returns one box.

| regime | usable box | s/call | clip <= 0 | clip > 0 |
|---|---|---|---|---|
| **one call per value** | **29/32 · 91%** | 4.6 | 38% | **62%** |
| all values in one call | 4/32 · 12% | 33.8 | 0% | 100% |

**Per-detection locate beats batch locate by ~7x on usable output** — this was the specific
question asked, and the answer is unambiguous. Batch fails for a structural reason: told to
locate a list of values, the model **ignores the list and transcribes the page in reading
order**, so the returned text rarely matches what was asked for.

Batch also exposed a third format failure — it emits **one JSON array per line** rather than one
array:

```
[{"text": "<value>", "bbox": [44, 30, 267, 70]}]
[{"text": "<value>", "bbox": [95, 182, 130, 189]}]
```

Harmless once parsed line-wise, but a whole-text `json.loads` gets nothing. Worth knowing for
any small model: it cannot hold a single multi-item array open.

**The boxes remain unsafe to paint**, which independently reconfirms the 2026-08-08 verdict on
different weights. 62% clip the true ink by more than zero pixels and 10% land in an entirely
wrong region (>100 px); median IoU is 0.079. Painting tolerance is zero pixels, so
`locator.py`'s design — model boxes as a **search constraint** over OCR word boxes, never as
paint geometry — holds here too, and more strongly than it did for the 27B.

## Transcription — it reads better than it selects

The same pages, asked only to transcribe. Recall here is the ceiling for any regime that feeds
this model's text to a downstream text detector (the text-only regime in TODO.md).

| dpi | transcription recall | s/page | image tokens |
|---|---|---|---|
| **200** (native design point) | **124/145 · 85.5%** | 33-50 | ~3.8K |
| 300 (this pipeline's) | 119/145 · 82.1% | 67-89 | ~8.5K |

Two results here matter more than the headline.

**Transcription (85.5%) beats detection (75% union, 60% for identifiers).** The model reads
these pages better than it selects from them — on two of six pages it transcribed every truth
value. The bottleneck is instruction-following and exhaustive enumeration, not vision. That is
consistent with everything above: it summarizes when questioned and transcribes when listed,
because transcription is what it is best at.

**More resolution is worse.** 300 dpi lost 3.4 points of recall and cost **2.1x the time**. The
model card's "validated operating range up to 8K tokens" is real — A4 at 300 dpi is ~8.5K image
tokens and steps outside it, while 200 dpi sits at ~3.8K. Anything built on this model should
render at 200 dpi, which is also the cheaper option; the usual assumption that more pixels help
OCR is inverted here.

It is still not an OCR candidate. 85.5% is far below what the existing PaddleOCR path delivers,
one dense transaction page transcribed only 14/29 (dropping account numbers), and the failure
is silent — the transcript reads as clean prose whether or not it dropped a row.

## Throughput — the one place it delivers

| | this model (2080 Ti) | Qwen3.6-27B (M1 Max) |
|---|---|---|
| per class-call | 4.3-5.5 s | — |
| 5-class sweep | **~25 s/page** | ~176 s/page |
| weights resident | **4.63 GiB** | 15.8-28.6 GiB |

Roughly **7x faster at a fifth of the memory**, on the Windows box, leaving the Mac free. That
is the prize, and it is why the negative result is worth recording precisely: the throughput
item in TODO.md is not solved by dropping to a 2.4B detector, because recall collapses well
before the speed is banked.

## What to carry forward

1. **Small-VLM prompts must be one class per call.** Confirmed at the extreme: 75% vs 25%.
2. **Any JSON prompt needs a decode-start guarantee** — prefill or, better, a sampler grammar.
   Without one, a capable model looks like a broken one.
3. **Forcing non-empty output forces confabulation.** Whatever supplies the structure must
   still permit the empty answer, or clean pages become false positives.
4. **Boxes-as-search-constraint is the right call across model scales** — now measured on a
   second, unrelated architecture.
5. **Render at the model's native design point, not the highest available.** 200 dpi beat
   300 dpi on accuracy *and* was 2.1x faster. Worth re-checking on the 27B, where this
   pipeline's 300 dpi was chosen for OCR, not for the VLM.
6. **This model's ceiling is selection, not reading** — transcription 85.5% vs detection 75%.
   If a small VLM is ever revisited, the promising shape is transcribe-then-detect-in-text,
   not detect-in-pixels.
7. Untested and the obvious next step if this is ever revisited: **constrained decoding via
   `xgrammar`**, which would replace both the prefill hack and the line-wise parsing, and is
   the closest analogue to the GBNF the llama.cpp path already relies on.
