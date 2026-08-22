# Why the vision tower is slow — head_dim 72 is a second-class citizen in llama.cpp

**Date:** 2026-08-20 · **Status: measurement only, nothing shipped and nothing patched.**
Benchmark source is in the session scratchpad and on the Mac at `~/bench/fa_headdim_bench.cpp`;
no `pii/` code and no llama.cpp code was touched.

Predecessor: [2026-08-12-mac-inference-speed.md](2026-08-12-mac-inference-speed.md). That
report established *where* the time goes (the vision tower) and closed the serving config as a
route to it — "the cost is architectural … no llama.cpp flag reaches it". This report reopens
the *why*, at the kernel level, and finds that about a fifth of the tower is not architectural
at all: it is the Metal flash-attention kernel multiplying by padding.

**Three findings, in ascending order of what they are worth.**

1. **head_dim 72 costs 10.8 s per page** in arithmetic on zero-padding — 22% of the vision
   tower, but only **9% of the 122 s prefill**. Affects Metal and CUDA both.
2. **On the 27B the vision tower is not the dominant term.** LM prefill is 72.6 s against the
   tower's 49.4 s. The predecessor's "the vision tower is the thing worth attacking" was true
   of the 8B and does not survive the move to the 27B.
3. **`ffn_down` ran at 45% of peak while every other GEMM ran at 78–80%** — a cache-locality
   bug in Metal's `mul_mm` launch order. **Found, fixed and confirmed:** a 36-line threadgroup
   swizzle takes end-to-end prefill from **117.92 to 144.68 t/s (+22.7%)** at the shipped
   ubatch, passes `test-backend-ops` 1154/1154, and costs the vision tower nothing. **Or, with
   no patch at all, `-ub 512` recovers +19.3% of it for free.** This one was nearly missed: the
   LM prefill's *aggregate* 64% of peak looked healthy enough that I called it "nothing to
   fix", and only measuring the GEMMs individually showed the aggregate was an average over
   one bad outlier and three good ones.

Finding 3 is worth **12.4 s/page shipped as a flag**, or 15.6 s with the patch. Adding a
head_dim-72 fix would put ~26 s on the table against a 125.6 s prefill (**21%**). None of it
changes the order of magnitude: the page is slow because 8,580 image tokens meet a 27B dense
model.

## The question

The predecessor attributed the tower's cost to unmasked O(N²) attention and stopped there. Two
things it did not check: whether full attention on every layer is even *correct* for this
model, and whether the kernels that run it are on a good path. Both are checkable in the
source, and the second turned out to be measurable.

## What is not wrong (checked first, so the rest is not chasing a phantom)

- **Flash attention is on.** `warmup: flash attention is enabled` in the serve log. The
  non-FA branch of `clip_graph::build_attn` (`tools/mtmd/clip.cpp:745`) materialises `kq` as
  `[N, N, n_head]`; at N = 34,320 and 16 heads that is 75 GB, so this was worth confirming
  rather than assuming. It never runs.
- **The projector is on the GPU.** Re-confirmed by the predecessor, 7.06× against
  `--no-mmproj-offload`. Upstream #22582 does not reproduce here.
- **Full attention on all 27 layers is architecturally correct.** Qwen2.5-VL uses window
  attention and llama.cpp implements it — `conversion/qwenvl.py:70` *asserts*
  `fullatt_block_indexes` is present, and `models/qwen2vl.cpp:104` alternates
  `full_attn`/`window_mask` on `n_wa_pattern`. The Qwen3-VL converter
  (`conversion/qwen3vl.py`) reads no window parameters at all, and the mmproj duly declares
  `n_wa_pattern: 0`. So `models/qwen3vl.cpp:115` passing `nullptr` for `kq_mask` is faithful to
  the architecture, not an omission. **There is no window attention to be missing here.**
- **`ggml_flash_attn_ext_set_prec(cur, GGML_PREC_F32)`, set unconditionally by clip's
  `build_attn`, is a no-op on Metal** — `prec` appears nowhere in
  `ggml/src/ggml-metal/ggml-metal-ops.cpp`. It would matter on CUDA. Not a lever here, but it
  is a lever *there*, so it is recorded.
- **The kernel is the specialised one, not a fallback.** head_dim 72 is in Metal's supported
  list (`ggml-metal-device.m:1279`) and the serve log shows the dedicated pipeline compiled:
  `kernel_flash_attn_ext_f16_dk72_dv72_mask=0_sinks=0_bias=0_scap=0_kvpad=1_bcm=0_ns10=72_ns20=72_nsg=4`.

## The measured shape

From `serve-20260819.log`, Qwen3.8-27B Q8_0 + `mmproj-F16`, llama.cpp `b10499-2-ge90357f06`,
M1 Max, `--image-max-tokens 16384`:

```
clip_encode: copying image 1/1 to input buffer (nx=2496, ny=3520)
clip_encode: output embedding shape [5120, 8580, 1]
```

2496 × 3520 at patch 16 = 156 × 220 = **34,320 patches**, merged 2×2 to 8,580 tokens. Three
encodes in that log: 49.377 s, 49.366 s, 49.370 s — **49.37 s**, spread 11 ms.

The tower is 27 layers, `n_embd` 1152, **16 heads → head_dim 72**, `n_ff` 4304. The attention
therefore runs over 34,320 positions — four times the token count, because the 2×2 merge
happens at the *end*, in the projector. This is the number that matters and it is easy to
misread from the token count.

## The finding: `PAD2(DV, 64)` makes head_dim 72 the worst case in the table

`ggml-metal.metal:6468` sets the output accumulator width for the FA tile kernel:

```c
constexpr short PV = PAD2(DV, 64);   // PAD2(72, 64) == 128
```

and the `O = O + (Q*K^T)*V` loop at `:6842` iterates `NO = PV8/NSG` accumulator tiles — i.e.
over **128 columns, not 72**. `simdgroup_multiply_accumulate` runs on all of them; the last 56
columns are loaded from beyond V's rows, accumulated, and then discarded (only `DV4 = 18`
float4s are written to `dst` at `:7028`). Correctness is fine — 72 is a clean 8×8 tile
boundary, so the garbage never mixes into a live tile, and `extra_pad` reserves the
over-read — but **the P·V half of attention does 128/72 = 1.78× the necessary work.**

The padding is not a careless constant. `NO` must be even (both branches of the loop consume
tiles in pairs, `for ii < NO/2`) and `NSG` is fixed at 4 for DK < 512, so `PV` must be a
multiple of 64. 72 cannot be expressed exactly by this tiling.

### Confirmed by measurement, against a falsifiable prediction

If the padding is real, cost tracks *issued* columns (DK + PV), not useful ones (DK + DV). At
head_dim 72 that predicts 200 units against 64's 136 — a ratio of **1.47** — whereas a kernel
with no padding would predict 144/128 = **1.125**. Those are far enough apart to separate.

`~/bench/fa_headdim_bench.cpp` calls `ggml_flash_attn_ext` alone, at the production shape
(N = 34,320, 16 heads, no mask, F32 Q, F16 K/V, `GGML_PREC_F32`), sweeping head dim. Best of 3:

| head dim | PV (padded) | ms | **useful TFLOPS** | issued TFLOPS | vs hs=64 |
|---|---|---|---|---|---|
| 64 | 64 | 941.2 | 5.13 | 5.13 | 1.000 |
| **72 (this model)** | **128** | **1428.8** | **3.80** | 5.28 | **1.518** |
| 80 | 128 | 1460.9 | 4.13 | 5.37 | 1.552 |
| 96 | 128 | 1588.5 | 4.56 | 5.32 | 1.688 |
| 128 | 128 | 1740.8 | 5.54 | 5.54 | 1.850 |

**The issued-TFLOPS column is flat — 5.13 to 5.54 across a 2× range of head dim — while the
useful column collapses to 3.80 at 72.** The kernel is doing the same amount of arithmetic per
second regardless; head dim 72 just wastes 28% of it. Measured 1.518 against the predicted
1.47 (the small excess is occupancy: the dk72 pipeline reports `th_max = 768` against dk64's
832). The no-padding prediction of 1.125 is refuted.

The same sweep at N = 8,580 and N = 17,160 gives the identical penalty (1.505, 1.518) and
confirms the scaling is exactly quadratic — 89.9 → 357.8 → 1428.8 ms, ratios 3.98 and 3.99 per
doubling. So the effect is scale-invariant and the attention term is textbook N².

## Where the 49.37 s goes

The benchmark call *is* one layer's attention at the production shape, so the tower splits by
measurement rather than by fit: 1.4288 s × 27 layers = **38.58 s**.

| component | time | share of tower |
|---|---|---|
| attention — useful work | 27.78 s | 56.3% |
| attention — **`PAD2(72,64)` padding** | **10.80 s** | **21.9%** |
| projections, FFN, norms, M-RoPE, copies, deepstack, merger | 10.79 s | 21.8% |
| **total** | **49.37 s** | |

The residual is a genuine cross-check, not a plug: the linear part of the graph is
27 × 1045 GFLOP (QKV 273 + O 91 + FFN 681) plus ~3.2 TFLOP of deepstack and merger, which at
the ~5.3 TFLOPS the same GPU delivers is ~5.7 s, leaving ~5 s for norms, rope, `ggml_cont`
copies and softmax. It lands on 10.79 s from the other direction.

**So a fifth of the vision tower — 10.8 s per page — is spent multiplying by padding.**

## But it does not explain production, and the tower is not the biggest term

Asked directly (Sergei, 2026-08-20) whether this accounts for a 1.5–2 minute page, the answer
is **no**. The tower is 49.4 s of a prefill that measures **~122 s**, and the padding is 10.8 s
of that — **9% of prefill, 6-7% of a page.** A real find, not the explanation.

Production prefills, `serve-20260819.log` and `serve-combined.log`, A4 at 300 DPI:

```
task   86 | prompt eval time = 121138.96 ms / 8902 tokens (73.49 t/s)
task 1426 | prompt eval time = 121131.23 ms / 8902 tokens
task 2275 | prompt eval time = 121386.29 ms / 8902 tokens
task 5011 | prompt eval time = 121696.63 ms / 8980 tokens
```

103 of the ~150 image prefills in the current run are 8,980 tokens; this is the norm, not a
sample. Subtracting the measured 49.37 s encode:

| phase | time | share of prefill |
|---|---|---|
| vision encode — useful | 27.8 s | 23% |
| vision encode — **head_dim-72 padding** | **10.8 s** | **9%** |
| vision encode — projections, FFN, norms, rope, copies | 10.8 s | 9% |
| **LM prefill — 8,980 tokens through 27.32B dense** | **72.6 s** | **60%** |
| **total** | **122.0 s** | |

**The LM prefill is the largest single term, and unlike the tower it is not inefficient.**
2 × 27.32e9 × 8,980 = 491 TFLOP in 72.6 s = **6.76 TFLOPS, ~64% of the M1 Max's ~10.6 TFLOPS
peak** (an overestimate in the model's favour — embeddings are not matmul FLOPs and the hybrid
DeltaNet layers are cheaper than dense attention — but the band is right). Against the vision
tower's 3.80 *useful* TFLOPS, 36% of peak.

So the ordering is the opposite of the intuition: **the vision tower is the badly-utilised
part, and the LM prefill is the expensive part.** There is no bug to find in the latter. A
27.32B dense model reading 8,580 image tokens on a 10.6 TFLOPS GPU costs what it costs.

The controlling variable is therefore **image tokens**, which is the one input both terms
depend on — the LM prefill linearly, the tower quadratically (attention) plus linearly.

## Can the head_dim-72 padding be reclaimed? Attempted, and it is not a constants change

Asked directly (Sergei, 2026-08-20). The prize is the 10.8 s/page quantified above. What the
attempt established:

**The obvious move — `PAD2(DV, 32)` = 96 instead of 128 — does not compile.** The kernel is
instantiated for *every* NSG (1, 2, 4, 8) through a function-constant switch, so
`static_assert(PV8 % NSG == 0)` must hold at NSG=8 for every head size. `PAD2(DV, 64)` is not a
careless constant; it is what makes that assert true. DV=72 at PV=96 gives PV8=12, and 12 % 8
fails.

**Tying the padding to NSG does compile**, because NSG is a template parameter right there:
`PV = PAD2(DV, 16*NSG)` keeps `NO = (PV/8)/NSG` even for every instantiation while letting a
small NSG pad less — 128 columns at NSG=4 but 96 at NSG=2. Paired with a host-side
`nsg = (GGML_PAD(ne20,32) % 64 == 0) ? 4 : 2`, DV=72 would take PV=96.

**But the NSG=2 path is broken.** `test-backend-ops -o FLASH_ATTN_EXT` fails at hs=72, and the
benchmark returns 0.4 ms for a 1.4 s workload — the kernel is not doing the work, so it is a
structural break rather than a numerical one, and there was no timing signal to extract. Some
further assumption in the kernel is tied to NSG=4 and I did not find it. **Reverted**; the tree
is clean and FLASH_ATTN_EXT is back to 4792/4792.

**What it would actually take**, in increasing order of payoff:

| approach | padding | recovers | cost |
|---|---|---|---|
| `PV = PAD2(DV, 16*NSG)` + working NSG=2 | 96 | **~6.2 s/page** | find the NSG=4 assumption |
| keep NSG=4, teach the `O = P*V` loop to handle an **odd** `NO` (3 instead of 4) | 96 | ~6.2 s/page | two loops that consume tiles in pairs |
| bounds-checked remainder tile, `NO = ceil(DV8/NSG)` | 72, exact | **10.8 s/page** | real kernel surgery |

None is a constants change; all are kernel work with a correctness burden, against the swizzle's
36 self-contained lines. Worth doing, but it is the second job, not the first.

## CUDA is not an escape hatch

Worth knowing before anyone proposes moving the tower to the Windows testbed:

```c
// ggml/src/ggml-cuda/fattn.cu:461
if (turing_mma_available(cc) && Q->ne[0] != 40 && Q->ne[0] != 72) {
    ...
    return BEST_FATTN_KERNEL_MMA_F16;
}
```

**head_dim 72 is explicitly excluded from the CUDA tensor-core path**, on both the Volta and
the Turing+ branches, and falls through to `BEST_FATTN_KERNEL_TILE`. The MMA dispatch
(`fattn.cu:121`) instantiates 64, 80, 96, 112, 128, 256 — no 72. So Qwen3-VL's vision tower
would run without tensor cores on the 2080 Ti.

Head dim 72 is a second-class citizen on *both* backends. That is the real answer to "is it a
llama.cpp issue": not a bug, not a misconfiguration, but an unusual head dim landing outside
the shapes both backends are tuned for. Qwen3-VL picked 1152/16 = 72; almost nothing else does.

## The LM prefill is not clean either — `ffn_down` runs at 45% of peak

Retracting a claim made earlier in this investigation. On seeing LM prefill at ~64% of peak I
said there was "nothing to fix" there. Challenged on it (Sergei: *"68% utilization looks bad,
perhaps we can get some gains here?"*), I measured instead of asserting, and the aggregate
number was hiding a specific and large deficiency.

`~/bench/mm_bench.cpp` times `ggml_mul_mat` alone, on Metal, at this model's exact prefill
shapes (n_embd 5120, n_ff 17408), q8_0 weights against F32 activations, best of 5:

| ubatch | ffn_up/gate [5120→17408] | **ffn_down [17408→5120]** | attn_q | attn_o |
|---|---|---|---|---|
| 256 | 7.95 (75%) | **7.76 (73%)** | | |
| 512 | 8.23 (78%) | **7.53 (71%)** | 7.91 (75%) | 7.87 (74%) |
| 1024 | 8.38 (79%) | **5.71 (54%)** | | |
| **2048 (shipped)** | 8.43 (80%) | **4.79 (45%)** | 8.29 (78%) | 8.27 (78%) |
| 4096 | 8.42 (79%) | **4.22 (40%)** | | |

**Every GEMM in the model sits at 75–80% of peak except `ffn_down`, which falls to 45% at the
shipped ubatch and keeps falling.** `ffn_up` has *identical* FLOPs and is merely the transposed
shape, and it is flat across the whole range — so this is not the quantization, not the
bandwidth, and not my benchmark: `ffn_up` is the control and it does not move. F16 weights show
the same collapse (87% → 41%), which rules out dequantization as well.

The distinguishing feature is which dimension is the reduction. `ffn_down` reduces over
K = 17408, so its **input activation is [17408, ubatch] — 3.4× the bytes of `ffn_up`'s
[5120, ubatch]** (142 MB against 42 MB at ubatch 2048). The working set scales with K × ubatch,
which is exactly the axis along which the measured efficiency decays. That mechanism is
consistent with every number above but is *not* proven here — the kernel-level confirmation
would be an Xcode GPU capture, which I have not done.

Both kernel-selection conditions are identical for the two shapes (`ne00 % 128 == 0` holds for
5120 and 17408, and both take the same `mul_mm` path), so this is not a dispatch fallback.

### The mechanism, isolated

Holding the FLOP count *exactly* constant — `ne00 × ne01` fixed at 17408 × 5120, only the ratio
moving — at ubatch 2048:

| K | N | src1 MB | TFLOPS | %peak |
|---|---|---|---|---|
| 1024 | 87040 | 8.4 | 8.36 | 79% |
| 2048 | 43520 | 16.8 | 8.41 | 79% |
| 4096 | 21760 | 33.6 | 8.41 | 79% |
| 8192 | 10880 | 67.1 | 7.20 | 68% |
| 17408 | 5120 | 142.6 | 4.83 | 46% |
| 34816 | 2560 | 285.2 | 2.75 | 26% |

**Identical arithmetic throughout, 3× spread in time.** Flat at 79% while src1 fits the M1 Max's
48 MB system-level cache, monotonic collapse once it does not. This is a pure locality effect,
and it named the fix.

`kernel_mul_mm` (`ggml-metal.metal:10193`) takes `r0 = tgpig.y*NR0`, `r1 = tgpig.x*NR1`, and the
dispatch (`ggml-metal-ops.cpp:2484`) launches `grid.x = src1 tiles`, `grid.y = src0 tiles`. x
varies fastest, so **one row tile streams the entire src1 matrix before the next row tile
starts** — the reuse distance for a src0 tile scales with `ne00`. That is the classic
un-swizzled GEMM launch order.

### Fixed, and confirmed

Patch: [`2026-08-20-metal-mulmm-swizzle.patch`](2026-08-20-metal-mulmm-swizzle.patch), 36 lines
in `ggml-metal.metal` as first written, on branch `metal-mulmm-swizzle` in `~/src/llama.cpp` on
the Mac and measured in a scratch build `build-swz/`, leaving production untouched while the
numbers below were taken. It shipped the same day: the branch is gone, the stored `.patch` is
the four-commit series as it landed on `brokerai-serving`, and production `build/` is built
from the last of them (`28b6cb56d`). Walk the grid in groups of 8 row tiles, which bounds the
working set to 8 src0 tiles plus one src1 tile. Below a 32 MB src1 threshold the original
mapping is kept, so small matrices take an unchanged path.

**Correctness:** `test-backend-ops -o MUL_MAT -b MTL0` → **1154/1154 passed**, zero failures.
That was originally written up here as proof that the remap is a bijection over the tile grid.
**It is not, and it never was** (corrected 2026-08-22): none of those 1154 cases exercised the
remap. Of the eval cases, exactly two trip the 32 MB src1 gate — `q4_0 m=1 n=2048 k=8192`
(64 MB) and `q8_0 m=6 n=4096 k=5120` (80 MB) — and both have m ≤ 6 against NR0 = 64, so
`nby = 1`, `gh = 1`, and the mapping degenerates to the identity. The shapes that would have
covered it (`4096 × bs × 14336`) live in `make_test_cases_perf()`, which does not check
correctness at all. The run proved the *unswizzled* path was still intact and that the gated
branch does not misbehave at one row tile. No more than that.

Three cases were added to `make_test_cases_eval()` to close it — large src1 **and** several row
tiles, with the tile counts deliberately not multiples of the group height so the ragged last
group is covered too:

| case | src1 | row tiles | group height |
|---|---|---|---|
| `MUL_MAT q8_0 m=640 n=1056 k=8192` | 34.6 MB | 10 | 8 → 8 + ragged 2 |
| `MUL_MAT f16 m=320 n=512 k=17408` | 35.7 MB | 5 | 4 → 4 + ragged 1 |
| `MUL_MAT_ID q8_0 2/2 m=128 n=1056 k=8192` | 34.6 MB per expert | 2 | 2 |

**1156/1156 MUL_MAT and 800/800 MUL_MAT_ID pass**, on MTL0, BLAS and CPU. That the three
actually reach the grouped path is established by mutation, not by assumption: breaking the
remap into a non-bijection **only where `gh > 1`** leaves every one of the 1154 pre-existing
MUL_MAT cases and 799 MUL_MAT_ID cases passing and fails exactly the three new ones (NMSE
0.0315 and 0.0534 on the two MUL_MAT shapes). The same mutation is what proves the old suite
was blind to it. Cost: ~21 GFLOP added, the whole `-o MUL_MAT` run is 27 s wall on the M1 Max.

This is also the requirement `CONTRIBUTING.md` states outright — *"if you modified a `ggml`
operator or added a new one, add the corresponding test cases to `test-backend-ops`"* — so the
gap was a compliance gap as well as an evidence one.

**Isolated:** `ffn_down [17408→5120]` at ubatch 2048 goes **4.79 → 8.29 TFLOPS (1.73×)**, now
matching `ffn_up`'s 8.33 on the same FLOP count. The whole K sweep above flattens to 78%.

**End to end**, `llama-bench` on the real model — full prefill graph, attention, norms, rope,
DeltaNet, real launches:

| n_ubatch | stock | **patched** | gain |
|---|---|---|---|
| 512 | 140.71 | **148.39** | +5.5% |
| 1024 | 126.66 | **146.67** | +15.8% |
| **2048 (shipped)** | 117.92 | **144.68** | **+22.7%** |

The patch **flattens the ubatch dependence** (144.7–148.4 against stock's 117.9–140.7), which
is the signature the cache explanation predicts and a second confirmation of the mechanism.

**No cost to the vision tower:** encode of the same page measures 49.417 s stock against
49.524 s patched (+0.2%, noise) — expected, since the tower is 78% attention and its GEMMs are
small-K. That run also independently reproduces the production 49.4 s at the same 2496×3520.

### What this is worth per page

LM prefill of 8,980 tokens, vision encode unchanged at 49.4 s:

| configuration | LM prefill | page prefill | vs now |
|---|---|---|---|
| stock, ubatch 2048 (**shipped**) | 76.2 s | **125.6 s** | — |
| stock, ubatch 512 — *flag only, no patch* | 63.8 s | **113.2 s** | **−9.8%** |
| patched, ubatch 2048 | 62.1 s | **111.5 s** | −11.2% |
| **patched, ubatch 512** | 60.5 s | **109.9 s** | **−12.5%** |

The two overlap heavily — they attack the same cache problem — so they are not additive.
**`-ub 512` alone recovers most of it with no patch at all**, which makes it the thing to ship
first.

### Generalized (2026-08-20, at Sergei's request)

**The group size now follows tile bytes, which fixes f16.** A fixed 8 is right for q8_0 and
wrong for f16, whose src0 tile is 2× the bytes. Sweeping it at K=17408, N=5120, ubatch 2048:

| SWZ | 2 | 4 | 8 | 16 |
|---|---|---|---|---|
| q8_0 | 6.66 | 8.16 | **8.29** | 8.22 |
| f16 | 6.47 | **7.37** | 7.28 | 5.22 |

Both peak at **~9 MB of src0 held per group** — f16 wants exactly half the tile count for
exactly double the tile bytes. So the constant became `SWZ = clamp(10MB / (NR0*nb01), 1, 16)`,
which reproduces each dtype's optimum automatically (q8_0 8.29, f16 7.34) and makes the 29%
cliff a fixed 16 would cause on f16 unreachable. Note SWZ=16 being *catastrophic* for f16 while
fine for q8_0 is the same mechanism seen from the other side, and is why a constant was wrong.

**`kernel_mul_mm_id` has the identical grid structure and now carries the identical swizzle.**
Gated on `neh1` (uniform per expert, so the remap stays uniform over the plane it reorders)
with `nbx` from the launched `ne21` extent, so it remains a bijection. **Not measured as a
win:** our MoE (Qwen3.6-35B-A3B — 256 experts, 8 used, n_embd 2048, so `n_ff_exp` ≈ 1024) puts
per-expert src1 at ~0.26 MB at ubatch 2048, three orders under the threshold. The gate can
never fire there. It is reachable for Mixtral-class shapes (few experts, large expert FFN) at
a large ubatch. Included so the two kernels do not drift, and labelled as unexercised.

Verification after generalizing: **MUL_MAT 1154/1154, MUL_MAT_ID 799/799,
FLASH_ATTN_EXT 4792/4792** on MTL0, and end-to-end `pp8980` unchanged at **148.40** (ub 512)
and **144.61** (ub 2048).

### Which other kernels want this — only these two

The pathology needs a 2D tile grid where one operand is re-streamed per tile-row and exceeds
cache. Surveying ggml-metal:

- **`mul_mm`, `mul_mm_id`** — the only two with that structure. Both now patched.
- **`flash_attn_ext`** — the other big tiled kernel, and it is **empirically clean**. Its grid
  is (query tiles, heads), so consecutive threadgroups share a head and therefore the same K/V,
  which is 9.9 MB per head at our vision-tower shape and ~9 MB at LM context depth — both
  comfortably cached. The proof is in the head-dim benchmark already run: issued throughput is
  flat at 5.24/5.27/5.28 TFLOPS across N = 8,580 / 17,160 / 34,320, and the scaling is exactly
  quadratic (×3.98, ×3.99 per doubling). **A cache cliff would have shown up as the largest N
  falling off; it does not.** Reaching one would need N ≈ 170k patches.
- **`mul_mv` / `mul_mv_ext` / `mul_mv_id`** (decode) — the vector is small and shared and the
  weights are streamed once; there is no reuse to lose.
- **norms, softmax, rope, get_rows, im2col** — elementwise or bandwidth-bound, no tile reuse.

### Is the byte budget general across Apple generations? Partly, and here is how far

Sergei's question, 2026-08-20. Two changes were made in response, and the honest answer is
"more robust than it was, still not general".

**Rounding the group down to a power of two.** A group height that does not divide the row-tile
count leaves a ragged last group and costs more than the extra tiles gain: f16 at K=17408 gives
**7.28 TFLOPS at SWZ 8 but 5.50 at SWZ 9**. Before rounding, a 20 MB budget resolved to 9 and
fell into exactly that hole.

**Capping the group at 8 instead of 16.** SWZ=16 did not win a single case measured, and it is
the only setting observed to fall *below* the unswizzled baseline (K=34816 f16: 1.92 against
2.13). Capping removes the only regression case found.

Together these widen the usable budget from roughly one value to a **4× range**:

| budget | 10 MB | 40 MB |
|---|---|---|
| q8_0 K=17408 | 8.28 | 8.29 |
| f16 K=17408 | 7.34 | 7.29 |

**But a 4× tolerance does not cover the hardware spread.** Apple's system-level cache runs from
~8 MB on a base M1/M2/M3/M4 to ~96 MB on an Ultra — 12× — and it is **reported not to be monotonic
across generations** (the M3 Pro is said to have reduced it against the M2 Pro; its memory
bandwidth cut, 200 to 150 GB/s, is well documented, the cache figure less so and it is not
verifiable here). So a formula keyed on generation or GPU core count cannot be trusted either.

**And the size cannot be queried.** Checked directly on this machine: `sysctl` exposes only CPU
cluster L2 (`hw.perflevel0.l2cachesize` = 12 MB), which reads 12 MB on *every* M1 variant
whether the SLC behind it is 8, 24 or 48 MB — useless as a proxy. `hw.l3cachesize` is absent.
Metal has no cache-size API at all.

What saves it from being dangerous is the failure direction: **over-budgeting decays toward the
unswizzled baseline rather than below it** (once capped at 8), while under-budgeting simply
loses the win — 3 MB gives f16 4.51 against a 4.56 baseline. So a machine with a much smaller
cache than the M1 Max should end up near stock, not worse. **That is a reasoned expectation,
not a measurement: this was tested on one chip.**

The upstream-shaped fix is to move the budget host-side, where a device table and an env
override can live, and pass it through `ggml_metal_kargs_mul_mm` instead of hardcoding it in
the shader. Recorded in [../TODO.md](../TODO.md).

### Still open

- **K = 34816 is improved but not solved** (2.70 → 3.77 TFLOPS q8_0, 36%). Grouping only bounds
  the y walk; at that K a single src1 tile is already 4.5 MB, so the x walk needs blocking too.
  **2D grid blocking is the real fix and subsumes the budget problem** — blocking both
  dimensions makes the working set a chosen quantity rather than a consequence of the shape,
  which is what would make the cache-size guess stop mattering.
- Measured on one chip. The mechanism is generic; the numbers are not.

## Prior art: no existing issue covers either finding

Surveyed at Sergei's request (2026-08-20).

- **[#14527](https://github.com/ggml-org/llama.cpp/issues/14527)** "Speed up image encode with
  Metal" — MiniCPM-V on M2, 5000+ ms/slice against 170 ms on a 4090. **Closed stale**, never
  diagnosed: one volunteer could not get an Xcode capture, guessed `rms_norm`, got no gain, and
  the bot closed it. The build in the trace is old enough to log `use bfloat = false` and to be
  skipping every bf16 kernel. **Not our case** — it is a different model, and our tower's
  hot spot is measured, not guessed.
- **[#15426](https://github.com/ggml-org/llama.cpp/issues/15426)** "Image processing on Metal
  takes significant amount of time" — Gemma-3, and a genuine root cause: `IM2COL` was falling
  off Metal to CPU on a src/dst type condition. ngxson fixed the dispatch; the reporter reopened
  because it was *still* slow, and it went stale again. **Not our case either**, and checked
  rather than assumed: our clip warmup prints no "the CLIP graph uses unsupported operators"
  warning, so nothing in the Qwen3-VL graph falls back to CPU.
- **Nothing found** on head_dim 72, on the `PAD2(DV, 64)` accumulator padding, or on large-K
  `mul_mm` degradation. Both findings here appear to be unreported.

The lesson from both issues is that this class of report dies for lack of a reproducer. Both
findings here ship with a standalone benchmark that needs no model and under 1 GB, which is the
thing those two issues never had.

## Levers, ranked against the 122 s prefill (not against the tower)

Ranking against the tower flatters everything that touches the tower. These are page-level.

1. **Fewer image tokens — the only lever that reaches both terms, and it is 3× the next one.**
   The LM prefill is linear in image tokens and the tower's attention is quadratic, so the
   whole prefill moves together. Using the measured law (attention 38.58 s and rest 10.79 s at
   34,320 patches; LM at 123.6 t/s):

   | image tokens | eff. DPI | tower | LM prefill | **prefill** | vs now |
   |---|---|---|---|---|---|
   | 8,580 (now) | 300 | 49.4 s | 72.6 s | **122.0 s** | — |
   | 4,290 | ~212 | 15.0 s | 37.9 s | **53.0 s** | **−57%** |
   | 2,145 | ~150 | 5.1 s | 20.6 s | **25.7 s** | −79% |

   This is the lever ruled out on 2026-08-12 — *"non-negotiable at 16K for qwen models, we
   cannot afford going any lower because of quality degradation"* — and that call stands as
   the operating decision. It is recorded here only because the predecessor also recorded that
   **"the recall half of that question is still open"**: the 16K choice was made on a
   *predicted* quality loss, never a measured one, and the 445-findings/350-values baseline it
   was protecting came from a *different* model. There is now a grounding scorer and a corpus
   gate that could settle it in one run. Sergei's call whether that run is worth making.

2. **Slicing the page — 3× weaker than my first framing.** K slices cost attention N²/K, but
   total image tokens are unchanged, so **the 72.6 s LM prefill does not move at all.** That
   caps the whole idea at the tower's 49.4 s.

   | slices | tower | LM prefill | **prefill** | vs now |
   |---|---|---|---|---|
   | 1 (now) | 49.4 s | 72.6 s | **122.0 s** | — |
   | 2 | 30.1 s | 72.6 s | **102.7 s** | −16% |
   | 4 | 20.4 s | 72.6 s | **93.1 s** | −24% |
   | 8 | 15.6 s | 72.6 s | **88.2 s** | −28% |

   Still worth doing — it needs no llama.cpp change, and the predecessor found slicing did not
   hurt recall on one page and may have helped it (25 of 33 distinct values from one half-page
   slice), with an `"image"` index in the output schema beating the whole-page baseline for
   localization. But it is a recall experiment with a speed dividend, not a speed fix, and the
   instrument is the `pii_eval` scorer.

3. **A smaller LM.** The predecessor measured the 8B's LM prefill at **26.9 s** for 8,975
   tokens against this 27B's 72.6 s for 8,980 — the model choice is worth 45.7 s of prefill,
   more than slicing at K=8. That is a quality decision the corpus eval owns, not a serving
   one, but it belongs on this list because it is the second-largest number in it.

4. **Fix the padding upstream (Metal).** 10.8 s/page — 22% of the tower but **9% of prefill**.
   The cheap partial (`PAD2(DV, 32)` = 96, needs the `O = P*V` loop to handle an odd `NO`) is
   worth about half. Worth an upstream issue regardless of what we do, because it is not
   specific to us: head dims 72, 80, 96 and 112 all pad to 128.

5. **Fix `ffn_down` upstream (Metal), ~9.4 s = 8% of prefill** — and unlike everything else on
   this list it costs us nothing in quality, geometry or model choice. Failing that,
   **`-b 512 -ub 512` is a free flag change worth a predicted ~6.6 s**, pending the end-to-end
   confirmation described above. Both are worth more than the head_dim-72 fix.

6. **Not levers, now confirmed by measurement rather than inherited.**
   - **MTP is free at prefill.** Prefill is 121.4 s with `MTP=off` against 121.1/121.1/121.4 s
     at n-max 2/3/4 — a 0.25% spread. The draft head is not evaluated during prefill, so the
     +77% decode win costs nothing on the other side. (From the existing `serve-mtpoff.log`,
     `serve-nmax3.log`, `serve-nmax4.log` — no new runs needed.)
   - **No CPU fallback in the clip graph** — warmup prints no unsupported-operator warning.
   - Every other serving flag (predecessor: ~1% total); projector precision (2.5% across a 3×
     file-size range); `--mtmd-batch-max-tokens` (structurally inert — `clip_support_batch()`
     is false for Qwen3-VL); LM quantization (every quant within 2% on page cost).
   - **The Metal 4 tensor API** is disabled on this chip by a device-name check
     (`ggml-metal-device.m:753`), overridable with `GGML_METAL_TENSOR_ENABLE`. The upstream
     comment records it as ~5% *slower* on M2 Ultra and neutral on M4, so it is listed as
     known-and-rejected rather than untried, but it is one env var if anyone wants the datum.

## Correcting the predecessor's headline for this model

> "At 16K that inverts — prefill is 68% — so the vision tower is the thing worth attacking."

That was measured on the **8B**, where the tower (50.6 s) genuinely exceeded the LM prefill
(26.9 s). On the **27B now in production the ordering reverses**: tower 49.4 s against LM
prefill 72.6 s. The tower's cost barely changed — it is the same `mmproj` shape and the same
page — while the LM prefill grew 2.7× with the model.

**"The vision tower is the thing worth attacking" is a statement about the 8B and does not
survive the move to the 27B.** Anything quoting that sentence needs to name the language model
it is talking about.

## Reproducing

`~/bench/fa_headdim_bench.cpp` on the Mac, built against the existing tree:

```sh
clang++ -std=c++17 -O2 fa_headdim_bench.cpp -o fa_headdim_bench \
  -I ~/src/llama.cpp/ggml/include -L ~/src/llama.cpp/build/bin \
  -lggml -lggml-base -Wl,-rpath,$HOME/src/llama.cpp/build/bin
./fa_headdim_bench 34320 16 3      # N, n_head, reps
```

It needs no model and under 1 GB, so it runs alongside a live llama-server.
