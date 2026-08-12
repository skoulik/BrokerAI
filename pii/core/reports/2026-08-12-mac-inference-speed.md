# Mac inference-speed sweep — Qwen3-VL-8B on the M1 Max

**Date:** 2026-08-12 · **Status: measurement only, nothing shipped.** Harness lives in the
session scratchpad and on the Mac under `~/bench`; no `pii/` code was touched. This records
what the serving layer actually costs, so the "make it faster" decisions can be taken on
numbers.

Predecessor: [2026-08-08-vlm-oneshot-qwen36.md](2026-08-08-vlm-oneshot-qwen36.md), whose
performance section this supersedes for the 8B and corrects for the 27B.

## Machine

MacBook Pro 18,2 — **M1 Max, 10 CPU (8P/2E), 32-core GPU, 64 GB unified**, macOS 26.1,
`recommendedMaxWorkingSetSize = 55662 MB`, `iogpu.wired_limit_mb = 0` (kernel default).
llama.cpp **b10326** (`3653e6d6d`). All runs on **AC power** — the predecessor measured
battery roughly doubling everything.

Metal reports `has_bfloat = true`, `simdgroup matrix mul = true`, and
**`has tensor = false`** — llama.cpp disables the Metal 4 tensor API on pre-M5 silicon
because it measured *slower* there, so that axis is closed on this machine by upstream
policy, not by our configuration.

## The headline: the serving config is not what the quality baseline was measured on

`--image-max-tokens 16384` is **absent from both `serve.sh` files**. It was used for the
2026-08-08 experiment and dropped when the serve scripts were written, so every run since has
used the model default. The logs show the factor of two directly:

| run | config | tokens/page | prefill |
|---|---|---|---|
| `qwen3.6-27b/serve_hires.log` (Aug 8 — the run behind the predecessor report) | `--image-max-tokens 16384` | 8933 | 111.5 s @ 80 t/s |
| `qwen3.6-27b/serve.log` (Aug 11 — current `serve.sh`) | *flag absent* | 4438 | 43.9 s @ 101 t/s |
| `qwen3-vl-8b/serve.log` (Aug 11 — current `serve.sh`) | *flag absent* | 4419 | 24.4 s @ 181 t/s |

**What the default budget means physically.** llama.cpp caps a Qwen3-VL image at
`image_max_tokens × 32 × 32` px. The default resolves to 4096 tokens = 4,194,304 px. An A4
page at the pipeline's own 300 DPI is 2480×3508 = 8.70 Mpx, so it is downscaled by
√(4.19/8.70) = 0.694 — **the model reads the page at ~208 effective DPI, not 300**. At 16384
the cap (16.8 Mpx) exceeds the page, so it is encoded natively.

**The cap is PER ATTACHMENT, not per request.**
`mtmd_image_preprocessor_dyn_size::preprocess(const clip_image_u8 & img)` takes a single image
and applies `calc_size_preserved_ratio(..., max_pixels: hparams.image_max_pixels, ...)` to that
image alone (`tools/mtmd/mtmd-image.cpp:929`). There is no accumulator across a request and no
awareness of how many images it carries. Consequence for any multi-image scheme: K slices each
get the full budget, so the cap stops binding as soon as the pieces are smaller than it, and
**context** — not the cap — becomes the bound (K × tokens_per_slice + prompt + output).

The predecessor's quality baseline — *445 findings / 350 distinct values over 31 pages* — was
measured at native 300 DPI. The "Next: re-run the sweep at a lower `--image-max-tokens`"
experiment it proposed therefore happened by accident, unmeasured, and is the live config.
The recall half of that question is **still open**; see the image-budget section.

## Method

Two instruments, because one number cannot separate the three cost centres:

- **End-to-end** (`~/bench/bench_client.py`) fires the real `vlm.PROMPT` payload at
  llama-server over HTTP and records the server's own `timings`. This is the only instrument
  that sees the vision tower.
- **Model-only** (`llama-bench`) runs `pp4419`/`tg512` with no projector loaded at all. The
  difference between the two is the vision tower's cost, measured rather than estimated.

Three properties of the harness are load-bearing and were each forced by a failed first
attempt:

- **A different page per rep.** The first run repeated one page and measured nothing:
  llama-server matches a slot's cached prompt by longest common prefix, the image IS the
  prefix, and every rep after warm-up reported `prompt_n: 1, cache_n: 4422`. Six distinct
  A4/300 pages now rotate, which is also what production does — page N+1 is never page N.
  Pass 2 deliberately reuses its own rep's page, because `localize` is *supposed* to hit that
  cache.
- **A pinned decode length** (512 tokens). Decode rate decays with `n_past` and output length
  is content-dependent, so an unpinned run compares two different workloads. 512 sits inside
  production's observed 400–900 band.
- **A fixed pass-2 value list.** Otherwise a config that detects one value fewer gets a
  shorter pass-2 prompt and looks faster.

Benchmark pages are synthetic (`pii_eval` legacy statements re-rendered to A4/300), so
nothing from `sensitive/` is copied to the Mac. Reps are reported as the **median of 3** with
the spread printed — Q8_0's three prefills spanned 6 ms on 24 s, so the machine is quiet and
the measurements are trustworthy.

## Where a page's time actually goes

**Decision, 2026-08-12 (Sergei): the image budget stays at 16384** — *"non-negotiable at 16K
for qwen models, we cannot afford going any lower because of quality degradation"*. Both
`serve.sh` files were corrected to carry `--image-max-tokens 16384` again, with `-c` cut from
32768 to 24576 to match the real workload. Everything below is reported at BOTH operating
points, because the sweep began before the decision and the two are different machines:

| | default budget (4423 tok) | **16K budget (8975 tok)** |
|---|---|---|
| vision tower | 13.7 s | **50.6 s** |
| LM prefill | 10.6 s | 26.9 s |
| prefill total | 24.5 s | **77.5 s** |
| vision share of prefill | 56% | **65%** |
| prefill share of page | 40% | **68%** |

**The vision tower scales at n^1.8 — nearly quadratic.** 2.07× the image tokens costs 3.7× the
encode. The LM prefill over the same range costs only 2.5×, because it has a causal mask and a
KV cache.

The mechanism is confirmed in the graph, not inferred: `tools/mtmd/models/qwen3vl.cpp:115`
calls `build_attn(..., nullptr, kq_scale, il)` — **`kq_mask` is `nullptr`**, so the vision
tower runs *unmasked full attention over every patch*, with no causal mask, no windowing and no
KV cache (one forward pass over the whole image, not autoregressive). Attention is therefore
O(N²) in patch count, and mixing that with the linear MLP term gives the measured n^1.8. This
is why the 16K budget costs what it does, and it is a property of the model architecture rather
than anything tunable in the serving config. (LM figure from `llama-bench pp8975` = 333.21 t/s
with no projector loaded; the tower is the remainder.)

The practical consequence: at the default budget decode was 58% of a page and prefill
optimisation could not reach most of the cost. **At 16K that inverts — prefill is 68%** — so
the vision tower is the thing worth attacking, and speculative decoding drops to secondary.

### The default-budget breakdown (kept for reference)

At Q8_0 with the model-default image budget, per page:

| phase | cost | share |
|---|---|---|
| vision encode (mmproj) | **13.7 s** | 22% |
| LM prefill (4419 tok) | 10.6 s | 17% |
| decode, pass 1 (512 tok) | 18.0 s | 29% |
| pass 2 prefill (image cached, 385 tok) | 1.2 s | 2% |
| decode, pass 2 (512 tok) | 18.0 s | 29% |
| **total** | **~61.5 s** | |

The image prefill cache works exactly as the predecessor claimed: pass 2 costs **1.2 s** of
prefill instead of 24.4 s.

**Decode is 58% of a page** once both passes are counted — more than prefill. That reframes
the problem: prefill optimisation alone cannot reach most of the cost.

## Quantization is not the lever (and the reason is kernel-level)

Five quants, every other flag identical to the current `serve.sh`:

| quant | size | prefill | pp t/s | tg t/s | page_s | quality vs Q8_0 |
|---|---|---|---|---|---|---|
| **Q8_0** (current) | 8.71 GB | 24350 ms | **181.5** | 28.49 | 61.5 | 70 values (baseline) |
| Q6_K | 6.73 GB | 25696 ms | 172.0 | **30.89** | **60.2** | 75 · lost 3 real values |
| Q5_K_M | 5.85 GB | 26357 ms | 167.7 | 26.30 | 66.7 | 81 · diverges heavily |
| UD-Q4_K_XL | 5.15 GB | 25555 ms | 172.9 | 29.54 | 61.5 | **21 · lost 50** ⚠ |
| Q4_K_M | 5.03 GB | 25480 ms | 173.4 | 29.89 | 61.0 | 72 · lost 4 real values |

**Every quant lands within 2% of Q8_0 on page cost.** Halving the weights bought nothing, and
Q8_0 — the largest — has the fastest prefill. The expectation going in was "decode is
memory-bound, so Q8→Q4 is ~2×"; that is wrong on this hardware.

⚠ **UD-Q4_K_XL returned 21 values against Q8_0's 70** on the quality page. One page, so not a
verdict — but it is the quant reputation would have picked, and the collapse is large enough
that it must be re-tested before anyone adopts it.

**Caveat on the quality column:** it measures *divergence from Q8_0, not degradation*. Q8_0
itself over-reports (it emits transaction dates like `01AUG22` as identifiers, which the
prompt explicitly excludes), so many "missing" entries are Q8_0's own false positives
disappearing. Only the named real values matter.

### Why — measured with the projector removed

`llama-bench`, same flags, no vision:

| quant | pp4419 t/s | tg512 t/s |
|---|---|---|
| **Q8_0** | **415.76 ± 0.14** | 38.02 |
| Q6_K | 370.66 | 42.72 |
| Q5_K_M | **349.77** | **34.83** |
| Q4_K_M | 374.58 | 42.44 |
| UD-Q4_K_XL | 375.66 | **43.74** |

Q5_K_M is slower than Q8_0 at *both* prefill and decode despite being 2.67 GiB smaller. That
cannot be a transfer-size effect. The Metal kernels do **not** operate on quantized data
directly — `mul_mm`/`mul_mv` call a per-block `dequantize_*` into registers and then run the
simdgroup matmul, so every type pays dequantization ALU *inside* the matmul:

```
Q8_0:  reg[i] = qs[i] * d;                                    // 1 op, 1 stream
Q4_K:  reg[i] = dl * (q[i] & mask) - ml;                      // ~3 ops + 6-bit scale unpack
Q5_K:  reg[i] = dl * ((q[i]&mask) + (qh[i]&ul ? 16:0)) - ml;  // ~6 ops + 2nd stream (qh)
```

Q5_K carries a separate `qh` high-bit array *and* `get_scale_min_k4_just2`, which is why it
loses on both axes.

**And the structural finding:** the specialised `mul_mv_ext` matrix-vector kernels exist for
`f32, f16, bf16, q4_0, q4_1, q5_0, q5_1, q8_0, iq4_nl, mxfp4, q1_0, q2_0` — **the legacy
quants. There is no `mul_mv_ext` for `q4_K`, `q5_K` or `q6_K`.** Q8_0 has a fast decode path
the K-quants structurally lack. This predicts Q4_0 and IQ4_NL should beat every K-quant at
decode, which is measured separately below.

### The vision tower, isolated

Subtracting `llama-bench`'s LM-only prefill from the end-to-end prefill:

| quant | e2e prefill | LM prefill | **vision tower** |
|---|---|---|---|
| Q8_0 | 24350 | 10629 | **13721** |
| Q6_K | 25696 | 11922 | **13774** |
| Q5_K_M | 26357 | 12634 | **13723** |
| Q4_K_M | 25480 | 11797 | **13683** |
| UD-Q4_K_XL | 25555 | 11763 | **13792** |

**13.7 s, constant to within 0.8% across all five** — as it must be, since all five run the
same `mmproj-F16`. It is 56% of prefill, and **no LM quantization can touch it**. Only the
image budget and the projector itself reach it.

### Why the decode win shrinks in situ

`llama-bench` says UD-Q4_K_XL decodes 15% faster than Q8_0 (43.74 vs 38.02); end to end it is
only 3.7% faster (29.54 vs 28.49). `llama-bench`'s `tg512` starts from an empty context,
while production decodes at `n_past ≈ 4419`, where a **quant-independent KV-attention term**
(the KV cache is `q8_0` in every config) dilutes the weight-bandwidth advantage. Any decode
optimisation must be judged at realistic context depth, not at `tg`.

## Context sizing

Workload high-water, from 70 real requests on the live 8B server: **max 8518** tokens, p99
5294, p50 4603. The maximum is a generation that hit `max_tokens` — 4099 output tokens on one
page, i.e. the model looped. Rare (1 in 70) but it is what sets the requirement.

| | prefill | + output (max 4096) | worst case |
|---|---|---|---|
| default image budget | 4419 | 4096 | **8,515** |
| `--image-max-tokens 16384` | 8933 | 4096 | **13,029** |

The text path is not binding: `WINDOW_CHARS = 4000` (~1.1k tokens) + prompt + `MAX_TOKENS`
→ ~5.6k.

**`-c 32768` is 2.5–4× larger than the workload can use.** At the default budget `-c 16384`
is right (2× headroom); `-c 8192` is *not* safe, because the observed runaway already exceeded
it. Adopting the 16384 image budget leaves only 25% headroom at `-c 16384` and an oversized
page would blow it, so that combination wants `-c 24576`.

## The image budget — the real lever, and a genuinely hard trade

| budget | prompt_n | eff. DPI | prefill | pp t/s | values found | decode_n |
|---|---|---|---|---|---|---|
| 1024 | 1383 | ~104 | **4534 ms** | 305.0 | **0 — total failure** | 4096 (ran to cap) |
| 2048 | 2409 | ~147 | 10197 ms | 236.3 | 24 | 491 |
| **4096 (current default)** | 4423 | ~208 | 24514 ms | 180.4 | 70 | 1528 |
| 8192 | 8527 | ~295 | 71040 ms | 120.0 | 50 | 1013 |
| 16384 | 8975 | ~300 (native) | **77526 ms** | 115.8 | 33 | 666 |

**The cost curve is superlinear.** 2× the image tokens (4423 → 8975) costs **3.2×** the
prefill (24.5 → 77.5 s), because attention is quadratic in sequence length — in the vision
tower's patch self-attention *and* in the LM prefill. Per-token throughput falls from 305 to
116 t/s across the range. Any reasoning that treats this axis as linear (including the
predecessor report's "halving it roughly doubles throughput") understates the cost of going
up and understates the saving of coming down.

**Value COUNT moves opposite to value QUALITY.** More resolution produces *fewer* reported
values (70 → 50 → 33) but *more real entities*. The 13 values `img-16384` finds that the
current `img-4096` misses are not fragments:

    ANGUS AND ROBERTSON PTY LTD · HARVEY AND MILLER HOLDINGS · NORMAN CHAVEZ
    ERIC SMITH · ERIC WADE · R & E ROCHA RENT · EFTPOS COLES EXPRESS
    NETFLIX.COM AU · BUNNINGS WAREHOUSE AU · AT06667873802666 · PSMSCG3YW3

Real names, companies and reference codes. Most of `img-4096`'s 50 "extra" values are the
over-reports the prompt explicitly forbids (`01JUL22`, `17`, `1058 AU`). So the low budget is
not trading recall for noise-free output — it is **losing named entities while gaining
noise**, which is the worst direction for a tool whose failure mode is an unredacted name.

**There is a cliff, not a gradient.** At 1024 tokens (~104 DPI) the model produced no
parseable output at all and ran to `max_tokens`. This axis cannot be tuned by extrapolation.

**Caveat, stated plainly:** this is ONE synthetic page with no scored ground truth. What is
measured is *divergence* between budgets plus a manual reading of which values are real. The
instrument that would settle it is the `pii_eval` scorer over the 31-page corpus at both
budgets — this section is enough to show the trade exists and is expensive, not enough to
pick a number.

## Slicing a page into overlapping horizontal bands — measured, and it does not pay

Proposed by Sergei on seeing the n^1.8 vision scaling: if the tower is nearly quadratic, two
half-pages should beat one whole page. Constraint he set, and it is the right one:
**horizontal cuts only, never vertical** — a vertical cut severs text lines mid-line and would
separate a label from its value on the same line, which is exactly the adjacency layer 1's
context promotion runs on. A horizontal cut only ever splits between lines.

Two slices of `bench_p0`, full width, at the 16K budget:

| scheme | tokens/slice | prefill | decode | **total** | values |
|---|---|---|---|---|---|
| whole page | 8975 | 77.9 s | 28.6 s | **106.5 s** | 33 |
| 2 slices, 10% overlap | 5071 | 60.4 s (**1.29×**) | 161.6 s ⚠ | 222.0 s | 1 (loop, see below) |
| 2 slices, 20% overlap | 5539 | 69.4 s (1.12×) | 44.7 s | **114.1 s** | 56 |

**The prefill saving is real and predicted well** (1.29× measured against 1.32× predicted from
the n^1.8 fit; 1.12× against 1.16×). **It is also smaller than the decode penalty it creates.**
Each slice runs its own decode pass and re-emits the overlap band's findings, and decode was
already the larger half of the page — so slicing multiplies the half that does not amortise in
order to attack the half that does. More slices makes this worse, not better. Overlap is
expensive for the same n^1.8 reason: the duplicated band is paid at nearly its square, which is
why 20% overlap gives back most of the 1.29×.

**The interesting result is not the speed one.** At 20% overlap the two slices found **56
distinct values against the whole page's 33, missing only 2**, at identical native resolution.
The plausible mechanism is that a slice gives the model half as much to enumerate, so it reads
more exhaustively — which would make slicing a **recall lever costing ~7% wall time**, not a
speed optimisation at all. Unverified: one page, and the 25 extra values were not checked
against ground truth. If it is worth pursuing, the instrument is the `pii_eval` scorer, not
this harness.

## A repetition loop is silently indistinguishable from a clean page

Found while diagnosing the ov10 slice above, which under greedy decode emits

    ..."AT06667873802666"..."AT06667873802666"..."AT06667873802666"... (to max_tokens)

The array never closes, `parse_findings` returns `[]`, and `image_mode.read_page` treats that
as "no findings on this page". Since layer 0 is the only detector for PERSON / ADDRESS /
ORGANIZATION, such a page emits **no name, address or company redaction at all** while layer 1
still finds checksummed identifiers, so the output looks plausibly redacted.

Rate: **~1 in 70 on real pages** — the `n_tokens = 8518` entry in the 8B `serve.log` of
2026-08-11 is one, a 4099-token decode on a real statement. Three further reproductions in this
session (the first dense benchmark page, `img-1024`, the ov10 slice).

This is a correctness bug, not a performance finding, and it is written up with its fix in
[TODO.md](../TODO.md) — `finish_reason` is already on the wire (`"length"` on truncation) and
`VlmDetector._ask` discards it. Also recorded there: which repetition-penalty options are
admissible (`dry_*` only; token-level penalties would corrupt the verbatim transcription the
prompt requires).

## `--mtmd-batch-max-tokens` does nothing here, and the code says why

Six values at the 16K budget, Q8_0, everything else fixed:

| `--mtmd-batch-max-tokens` | 512 | 1024 (default) | 2048 | 4096 | 8192 | 16384 |
|---|---|---|---|---|---|---|
| prefill (ms) | 76894 | 76948 | 76896 | 76896 | 76887 | 76945 |
| page (s) | 122.1 | 122.2 | 122.2 | 122.2 | 122.1 | 122.2 |

**61 ms of spread across a 32× range of the flag — 0.08%.** A clean null, and it is structural
rather than a tuning result:

- `mtmd_batch_add_chunk` (`tools/mtmd/mtmd.cpp:1998`) admits the **first chunk
  unconditionally** and only consults `batch_max_tokens` for *subsequent* chunks. One image is
  one chunk, so with one page per request the limit is never reached.
- It would not help even with several images: `clip_support_batch()` gates that path, and
  `support_batch()` is overridden to `true` only for `gemma4v`, `internvl`, `deepseekocr` and
  `deepseekocr2` (`tools/mtmd/models/models.h`). **Qwen3-VL inherits the base `false`**, so a
  second chunk is rejected before the token limit is examined. The one exception,
  `clip_model_n_temporal_merge() == 2`, is the Qwen-VL *video* frame-pair path and treats the
  batch dimension as temporal — wrong semantics for two slices of one page.

This flag is therefore **not an intra-image tiling knob**, which is how it reads from the
`--help` text ("maximum number of image tokens per batch when encoding images"). It is a
multi-image batching knob, and it is inert for this workload.

Together with `kq_mask = nullptr` above, that closes the serving config as a route to the
vision tower: **no llama.cpp flag reaches it.** The only levers on the tower are patch count
(the image budget) and slicing.

## The projector precision bracket — F16 was already right

All at the 16K budget, Q8_0 language model, only the `--mmproj` file varying:

| projector | file | prefill | vs F16 | page |
|---|---|---|---|---|
| **F16 (current)** | 1.16 GB | **76893 ms** | — | 122.1 s |
| Q8_0 (Qwen official) | 0.75 GB | 77119 ms | +0.3% | 122.3 s |
| BF16 | 1.16 GB | 77366 ms | +0.6% | 122.6 s |
| F32 | 2.31 GB | 78833 ms | +2.5% | 124.0 s |

The prediction recorded before the run held: **Q8_0 does not help.** The whole bracket spans
2.5% across a 3× range of file size, which is itself the result — the projector is compute-
bound (~1.16 GB streamed against a 50.6 s encode, ~3 ms of it), so precision barely moves the
number, and the quantized variant is fractionally *slower* for the same dequantize-inside-the-
matmul reason that made Q5_K lose on the language model. BF16 is slower than F16 despite
`has_bfloat = true` on this chip.

## `-b` / `-ub` — a small real win, opposite to the published advice

| `-b`/`-ub` | 256 | 512 | **1024** | 2048 (current) | 4096 | 2048/512 |
|---|---|---|---|---|---|---|
| prefill (ms) | 77578 | 76333 | **76197** | 76888 | 77673 | 76355 |
| page (s) | 122.8 | 121.5 | 121.5 | 122.1 | 122.8 | 121.5 |

A shallow optimum at 512-1024, with both extremes worse. **`-b 1024 -ub 1024` is 0.9% faster
than the current 2048** — 0.7 s/page. Consistent with the same direction measured at the
default budget (512 beat 2048 by 1.6%), and *contrary* to the widely-cited Apple Silicon
tuning advice of `-b 2048 -ub 2048`; it matches instead the "smaller batches can help" thread
in upstream discussion #21112. The 27B report's "no improvement from -ub 2048" was right to be
sceptical.

## Context size — no speed cost at all, pure memory

| `-c` | prefill | page | **peak wired** |
|---|---|---|---|
| 16384 | 76878 ms | 122.1 s | 16.54 GB |
| **24576 (now shipped)** | 76882 ms | 122.1 s | 17.18 GB |
| 32768 (previous) | 76882 ms | 122.1 s | 17.82 GB |
| 65536 | 76965 ms | 122.2 s | 20.39 GB |

**0.1% spread on time** across a 4× range — exactly what `-fa on` predicts, since attention
cost tracks actual `n_past` rather than the allocation. So the `-c` effect observed on the text
path was allocation pressure, not compute.

Memory scales linearly at **78 KB/token**, which confirms empirically the 36 layers × 1024 KV
dim × 2 × q8_0 arithmetic that was flagged as unverified earlier in this report. The 32768 →
24576 change saves 0.64 GB on the 8B and would save **~1.14 GB on the 27B** (64 layers), which
is where it matters — that model was observed paging.

## What all of this adds up to

**Across every serving flag tested — LM quantization, projector precision,
`--mtmd-batch-max-tokens`, `-b`/`-ub`, `-c`, KV cache type — the total available gain is ~1%**
(the `-b`/`-ub` move, 0.9%). Everything else is neutral, structurally inert, or worse.

The serving configuration was not misconfigured for speed. The two changes that *did* matter
were both corrections of lost/oversized settings rather than tuning wins: restoring
`--image-max-tokens 16384` (a recall bug) and cutting `-c` to 24576 (a memory win, no speed
change).

The cost is architectural: 50.6 s of the 77.5 s prefill is a vision tower running unmasked
O(N²) attention over 8495 patches. No llama.cpp flag reaches it. The only levers on it are
patch count — which the 16K decision fixes — and partitioning the attention, i.e. slicing.

## KV cache type — the single largest win, and it inverts the received advice

| KV type | prefill | pp t/s | tg t/s | **page** | peak wired |
|---|---|---|---|---|---|
| **f16** | **70405 ms** | 127.4 | **32.77** | **102.8 s** | 18.84 GB |
| q8_0 (was shipped) | 76870 ms | 116.7 | 23.51 | 122.1 s | 17.33 GB |
| q4_0 | 76928 ms | 116.6 | 26.59 | 117.1 s | 16.27 GB |
| q8_0 K / f16 V | **325873 ms** | 27.6 | 5.33 | **547 s** | 17.74 GB |

**f16 cuts page cost 15.8% and decode 39%, for +1.5 GB** — larger than every other serving
flag in this report combined, and **uniquely free of quality risk, because f16 is *higher*
precision than the q8_0 it replaces.** Adopted 2026-08-12 (Sergei: "f16 KV it is") on the 8B;
the 27B is left on q8_0 pending its memory check, where +3 GB could matter on a model already
observed paging.

The mechanism, quantified against measured decode (weights 8.7 GB + KV at ~400 GB/s,
`n_past ≈ 9000`):

| KV type | bytes/token | KV read | theoretical tg | measured tg | **kernel efficiency** |
|---|---|---|---|---|---|
| q8_0 | 78 KB | 0.70 GB | 42.4 t/s | 23.51 | **55%** |
| f16 | 147 KB | 1.32 GB | 39.7 t/s | 32.77 | **82%** |

q8_0 has the *best* bandwidth ceiling of the two and still loses, reaching only 55% of it. The
deficit is dequantisation ALU inside the attention kernel on every decode step — the same
mechanism that makes the weight K-quants lose. **This contradicts the widely-cited Apple
Silicon tuning guidance**, which recommends `--cache-type-k q8_0 --cache-type-v q8_0`.

**Never set K and V to different types.** Every Metal fused-attention kernel is instantiated
with one dtype for both (`kernel_flash_attn_ext<..., block_q8_0, dequantize_q8_0, block_q8_0,
dequantize_q8_0, ...>`), so a mismatch has no instantiation and falls off fused attention
entirely: 547 s/page against ~110, a **4.5× penalty**. This is a live footgun in a combination
users are actively advised to try ("quantize K harder than V").

## Legacy quants — the kernel prediction, confirmed

`llama-bench`, `pp8975`/`tg512`, no vision:

| quant | size | pp8975 | **tg512** |
|---|---|---|---|
| Q8_0 | 8.11 GiB | 333.18 | 38.05 |
| **Q4_0** | 4.45 GiB | **334.67** | **58.35** |
| IQ4_NL | 4.46 GiB | 327.78 | 49.58 |
| Q4_K_M | 4.68 GiB | 307.14 | 43.73 |

**Q4_0 matches Q8_0's prefill and decodes 53% faster, at half the size** — while Q4_K_M, the
conventional choice, is worse on both axes. This is exactly what the `mul_mv_ext` kernel
coverage predicted: legacy quants get the specialised matvec path, K-quants do not.

**Not adopted.** Q4_0 is the crudest 4-bit format (one f16 scale per 32 weights, symmetric, no
min offset) against Q4_K's 6-bit scales *and* mins in super-blocks, and quantization damage on
this workload surfaces as digit errors in values the prompt requires verbatim — a wrong digit
is a different account, which `fuzzy.py` prices at infinity precisely because it is
unrecoverable. This session also produced a cautionary specimen: UD-Q4_K_XL returned 21 values
against Q8_0's 70. Q4_0 needs the `pii_eval` gate before it goes near `serve.sh` (Sergei,
2026-08-12: "I'd be cautious as the quality may degrade").

## Slice-scoped localization — the index schema wins, prompt scoping fails

Three arms on one page, all fed the same fixed 15-value list so only localization varies,
scored against ground-truth glyph boxes computed from the render arithmetic (verified: ink
fills 96-99% of every computed box width).

| arm | returned | recall | **hallucinated** | mean IoU | clip>20px | looseness |
|---|---|---|---|---|---|---|
| (a) whole page | 15 | 0.824 | 1 | 0.705 | 1/14 | 1.05 |
| **(b) index in schema** | 15 | **0.882** | **0** | 0.717 | 3/15 | 1.11 |
| (c) prompt-scoped | 26 | 0.647 | **15** | 0.713 | 0/11 | 1.39 |

**The feared degradation did not occur.** Adding an `"image"` index to the output schema beat
the whole-page baseline on recall, hallucinated nothing, and got every index right. The 7.4%
recall cost that adding `bbox_2d` to *detection* incurred did not repeat when adding an index
to *localization*.

**Prompt scoping failed, asymmetrically.** Arm (c)'s 15 hallucinations were all on the SECOND
image (`out_of_band: [0, 15]`) — asked about image 1 it complied, asked about image 2 it
reported values from the pair regardless, despite emphatic wording that most listed values
would be absent. Telling a model to *label* its output is reliable; telling it to *suppress*
most of its input is not.

Caveats: one page, 17 truth boxes, so the clip>20px column is inside noise; and this isolates
localization rather than measuring end-to-end recall.

## The vision tower is on the GPU — upstream #22582 does not reproduce here

Upstream [#22582](https://github.com/ggml-org/llama.cpp/issues/22582) claims llama-server runs
the full vision tower on CPU per image slice while llama-cli is "almost instant" (closed as not
planned, different backend), and [#14527](https://github.com/ggml-org/llama.cpp/issues/14527)
measures Metal image encode at ~45× CUDA on an M2. Either would dominate every flag in this
report, so both were checked directly with `llama-mtmd-cli` on the same model and page:

| projector | vision encode | user CPU time |
|---|---|---|
| GPU (default) | **13718 ms** | 0.75 s |
| CPU (`--no-mmproj-offload`) | 96794 ms | 771.74 s |

**GPU offload is worth 7.06×**, and the user-CPU figures confirm the work genuinely moves. The
projector is on the GPU in llama-server on this build; there is no bug to chase.

**This also validates the attribution method used throughout this report.** `llama-mtmd-cli`
reports the encode directly as **13718 ms**; the figure derived for the same quantity by
subtracting `llama-bench`'s LM-only prefill from the end-to-end server prefill was **13721 ms**
— a 3 ms agreement between an instrumented value and an inferred one. The 50.6 s tower figure
at the 16K budget rests on the same subtraction and inherits that confidence.

## KV precision, completed — f16 is the optimum and f32 is a cliff

| KV type | prefill | tg | page |
|---|---|---|---|
| **f16** | 70402 ms | **32.78** | **102.8 s** |
| q4_0 | 76928 ms | 26.59 | 117.1 s |
| bf16 | 71138 ms | 24.01 | 115.0 s |
| q8_0 | 76870 ms | 23.51 | 122.1 s |
| **f32** | 71061 ms | **5.09** | **273.4 s** |

**f32 decodes 6.4× slower than f16.** A bandwidth argument predicts ~13% (f32 doubles KV
traffic); the prediction was wrong by 5.6×, because f32 falls off the optimised decode path
entirely — the same class of failure as the mismatched-dtype cliff, not a bandwidth effect.
Third time in this investigation that a bandwidth-shaped prediction lost to a kernel-shaped
reality on this backend.

## Speculative decoding does not engage on multimodal prompts

| config | prefill | tg | page | draft telemetry |
|---|---|---|---|---|
| off | 76890 ms | 23.51 | 122.1 s | — |
| `--spec-draft-n-max 4` | 76807 ms | 23.57 | 121.9 s | **absent** |
| `--spec-draft-n-max 16` | 76842 ms | 23.57 | 122.0 s | **absent** |

Qwen3-VL-2B-Q4_K_M as draft (same family, so the vocabulary matches). `llama-server` emits
`draft_n`/`draft_n_accepted` only when speculation actually ran; **zero occurrences across
every response**, and decode is identical to spec-off. The server loaded the draft, accepted
the flags, and never speculated — consistent with the draft path consuming TOKEN batches while
an image chunk is an EMBEDDING batch. A negative result with positive evidence, not an absence
of measurement.

(First attempt failed on a flag rename: `--draft-max`/`--draft-min` are removed in b10326 in
favour of `--spec-draft-n-max`/`--spec-draft-n-min`; the old names remain as stubs that emit a
removal error.)

## A kernel optimisation, implemented and falsified

The measured deficit is that quantized KV reaches only ~55% of its bandwidth ceiling where f16
reaches ~82%. The decode kernel `kernel_flash_attn_ext_vec_q8_0_dk128_dv128` calls
`dequantize_q8_0_t4` once per 4 KV elements, and that function issued **four scalar int8 device
loads** for four contiguous addresses. `char4` is illegal there — `block_q8_0` is 34 bytes with
`qs` at offset 2, so the address is never 4-byte aligned — but Metal's `packed_char4`
(alignment 1) permits exactly this load.

Built b10326 from source on the Mac and verified parity with the shipped binary first
(pp512 565.76 vs 565.78, tg256 38.37 vs 38.34 — identical). Benchmarked at
**`-d 8975`**, the real context depth, because from an empty context the q8_0-vs-f16 gap is
only 5% while at production depth it is 39%: the dequantisation cost scales with how much KV
there is to dequantize, so benchmarking from empty under-measures any fix ~8×.

| build | tg128 @ d8975 |
|---|---|
| unpatched | 23.90 / 23.91 |
| **`packed_char4` vectorised load** | **20.40 / 20.36 (−15%)** |
| reverted | 23.85 |

**The optimisation is a pessimisation, reproducibly.** The loads were never the bottleneck: the
Metal compiler already generates a better sequence from the scalar loop than an explicit
unaligned 4-byte load plus `char4→float4` conversion. The deficit is the intrinsic
dequantisation *arithmetic* — a multiply by the block scale per element on every KV element on
every decode step — which is not removable by better memory access.

Closing it would mean restructuring the FA inner loop (dequantizing a whole 32-element block
once into threadgroup memory rather than eight 4-element calls that each re-read the block
scale), touching shared-memory budget and barriers. That needs profiler data rather than
intuition — this experiment is evidence that intuition about this compiler is unreliable. Patch
reverted; the tree is clean.

**Practical impact: none.** The 8B now runs f16 KV, which sidesteps the deficit entirely.
Optimising quantized KV only matters where the memory saving is needed — i.e. the 27B.

## Levers by measured value

| lever | effect | status |
|---|---|---|
| **KV cache f16 (was q8_0)** | **−15.8% page, +39% decode**, no quality risk | **applied to the 8B** |
| Q4_0 weights | +53% decode, half size | **held** — quality ungated |
| `-b`/`-ub` 1024 | −0.9% | not applied |
| everything else | ≤2%, inert, or worse | — |
| `--image-max-tokens 16384` | +3.2× prefill | **restored** — a recall fix, not a speed one |
| `-c` 24576 (was 32768) | 0% speed, −0.64 GB | **applied** |

<!-- SECTIONS PENDING: K-way slice budget test (one-request multi-image), 27B memory check. -->

## Open, and needing a decision

- **The image budget is a recall trade, not a free win.** Numbers pending; the decision is
  Sergei's, and it needs its own A/B against the 350-distinct baseline rather than a speed
  argument.
- **`iogpu.wired_limit_mb` is untested** — `sudo` on the Mac requires a password. It does not
  matter for the 8B (peak wired 16.9 GB of 64) but it is the lever for the 27B, which was
  observed paging.
