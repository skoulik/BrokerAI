# Qwen3.6 is hybrid SSM+attention, and that breaks llama-server's image prompt cache

**Date:** 2026-08-13 · **Status: measured, patched, and DEPLOYED on the Mac** (Sergei,
2026-08-13 — see "Deployment" below). The fix is entirely in the serving layer; no `pii/` code
changed. Measurements taken against the live 35B server on the Mac
(`llama.cpp b10326 / 3653e6d6d`), with a CUDA control run on
`D:\code\llama.cpp` b10380 (`0b1bad14f`).

Corrects a scope assumption in
[2026-08-12-mac-inference-speed.md](2026-08-12-mac-inference-speed.md): that report's
"the image prefill cache works" holds for **Qwen3-VL only**. It does not survive the move to
Qwen3.6.

---

## Headline

**On Qwen3.6 (both the 27B dense and the 35B MoE) the image prefill cache only works when the
new prompt is a strict EXTENSION of the previous one. Any prompt that diverges mid-sequence
re-projects the whole image - about 50 s per page at the 16K image budget.**

The two-pass regime is exactly the losing shape. `vlm.py:_ask` builds a fresh standalone
`[image, prompt]` message for each pass, so pass 2 diverges from pass 1 immediately after the
image and pays the vision tower a second time.

This is **not** a Mac or Metal problem. It is a property of the model architecture and would
reproduce on any backend.

## How it presents, and why it is easy to misread

The server reports a cache hit and then throws it away:

```
slot get_availabl: selected slot by LCP similarity, f_sim_best = 0.998 (> 0.100 thold), f_keep = 0.991
...
W find_slot: non-consecutive token position 4 after 3 for sequence 0 with 2048 new tokens
slot print_timing: prompt eval time = 59152.92 ms /  8604 tokens
```

`f_sim_best = 0.998` is the "it found the common prefix" line. `prompt_n = 8604` on the next
line is the whole prompt being reprocessed anyway. Wall-clock is the only instrument that
catches it, which is how it was originally noticed.

## Measurements

M1 Max 64 GB, `Qwen3.6-35B-A3B-UD-Q4_K_XL` + `mmproj-F16`, `-c 24576 -fa on -np 1 -b 2048
-ub 2048`, KV f16, `--image-min-tokens 1024 --image-max-tokens 16384`. Page is
`bench_p0.png`, A4 at 300 DPI (2480x3508), 8603 prompt tokens. A cold first request is
guaranteed by appending 8 random bytes after the PNG `IEND` - pixel-identical, different
FNV id.

`[<image><q1>]` then `[<image><q2>]`:

| | request 1 (cold) | request 2 (same image, new question) |
|---|---|---|
| **projection** | **~50.4 s** | **~49.9 s - paid again** |
| **decode** | 1306 ms (64 tok) | 336 ms (17 tok) |
| **wall total** | **61228 ms** | **59600 ms** |
| prefill total | 59719 ms | 59153 ms |
| `prompt_n` | 8603 | 8604 |

Request 2 saves nothing.

**Projection is derived, not instrumented.** A text-only prompt of 7852 tokens prefills in
8517 ms (922 t/s), so 8603 tokens cost ~9.3 s of LM work and the remaining ~50 s is the vision
tower. This is the same subtraction the predecessor report validated to 3 ms against
`llama-mtmd-cli`, and it lands on the 50.6 s that report measured for the same mmproj and
image budget. The direct instrument (`"%s slice encoded in %ld ms"`,
`tools/mtmd/mtmd-helper.cpp:251`) did not appear in the log - the server is not running at a
verbosity that emits it.

### The contrast that identifies the mechanism

Same model, same image, same server session - only the request shape differs:

| shape | `prompt_n` | prefill | wall |
|---|---|---|---|
| **true extension** (multi-turn: user+image, assistant reply, new user turn) | **43** | **304 ms** | **885 ms** |
| **new question** (fresh `[image, q2]`) | 8604 | 59153 ms | 59600 ms |

**67x.** Extending forward is free; diverging costs a full re-projection.

Note a trap in constructing this test: `[img, q1]` -> `[img, q1, q2]` inside a *single* user
message is **not** an extension. The chat template puts `<|im_end|>...` after `q1` in the
first and after `q2` in the second, so they diverge and it re-projects. The extension must be
a real multi-turn continuation.

## Root cause

`ssm.*` keys in the GGUF sort every model on the Mac into two groups:

| model | arch prefix | SSM keys | case B |
|---|---|---|---|
| Qwen3-VL-2B | `qwen3vl` | none | works |
| Qwen3-VL-8B | `qwen3vl` | none | works |
| **Qwen3.6-27B dense** | `qwen35` | `conv_kernel, group_count, inner_size, state_size, time_step_rank` | **breaks** |
| **Qwen3.6-35B-A3B** | `qwen35moe` | same | **breaks** |

The whole Qwen3.6 family is hybrid SSM + attention, dense and MoE alike. Recurrent state is a
running summary: it can be extended, but it cannot be truncated back to an arbitrary earlier
position. So when the longest common prefix ends mid-sequence, the server cannot roll the
state back to that point and gives up on the entire prefix:

```cpp
// tools/server/server-context.cpp:3372
if (do_reset) {
    SLT_TRC(slot, "forcing full prompt re-processing due to lack of cache data "
                  "(likely due to SWA or hybrid/recurrent memory, ...)");
    pos_next = 0;
    n_past   = 0;
}
```

The `find_slot: non-consecutive token position` warnings come from
`src/llama-memory-recurrent.cpp:641` - the recurrent memory objecting to a position that
moved backwards. Prefix matching itself is fine: `server_tokens::get_common_prefix`
(`tools/server/server-common.cpp:471`) compares media chunks by id and token count and finds
the image correctly. The failure is downstream, in what the memory can do with that answer.

**Why the predecessor report missed it:** it benchmarked Qwen3-VL-8B, which is pure attention.
Its "pass 2 costs 1.2 s instead of 24.4 s" is correct for that model and does not transfer.

**CUDA control:** on Qwen3-VL-2B, b10380, RTX 2080 Ti, the identical case B reuses 8586 of
8596 tokens (14 ms prefill). Same server code, same request shape, no SSM - cache works. This
confirms the discriminator is the architecture, not the platform.

## Request shapes: what caches and what does not

Measured on the CUDA control (Qwen3-VL, all shapes) and on the Mac (extension vs divergence).
The rule is the same on both; on Qwen3.6 only the first row survives.

| shape | pure attention | hybrid SSM |
|---|---|---|
| `[img,q1]` -> multi-turn continuation | reuses | **reuses** |
| `[img,q1]` -> `[img,q2]` | reuses image | **re-projects** |
| `[q1,img]` -> `[q2,img]` (text before image changes) | re-projects | re-projects |
| varying system prompt ahead of the image | re-projects | re-projects |
| same picture, re-encoded bytes (or sliced vs whole) | re-projects | re-projects |

The general rule for the last three is unchanged and applies everywhere: the cache is a
prefix, so an image survives only if **every token in front of it is byte-identical**. The
image chunk id is `fnv_hash(buf, len)` over the raw encoded file bytes
(`tools/mtmd/mtmd-helper.cpp:377`), so any re-encode or reslice is a guaranteed miss.

## Context checkpoints do not rescue it - tested, and the reason is exact

The server can snapshot state and roll back to it, which is the designed escape for models
that cannot do partial removal. It does not work here. Tested on the Mac with the server
restarted as `--verbose -ctxcp 32 -cms 0` (checkpoint cap at the default 32, **minimum
spacing dropped to 0** so nothing is suppressed for being too close). Request 2 still
reported `prompt_n = 8604` and 59.1 s of prefill - unchanged.

A checkpoint *is* created, then rejected:

```
task 0  | main/do_checkpoint = no,  pos_min = -1     <- image iterations: suppressed
task 0  | main/do_checkpoint = no,  pos_min = 4
task 0  | main/do_checkpoint = yes, pos_min = 128    <- only after ALL the text
        | created context checkpoint 1 of 32 (pos_min = 128, pos_max = 128, n_tokens = 8599, 62.813 MiB)

task 67 | checking checkpoint with [128, 128] against 115...
task 67 | forcing full prompt re-processing due to lack of cache data
task 67 | erased invalidated context checkpoint (pos_min = 128, pos_max = 128, n_swa = 0, pos_next = 0)
```

The rejection is the first clause of the search predicate at `server-context.cpp:3351`,
`if (cur.pos_max > pos_next) return false;`. The checkpoint sits at position **128**; the
divergence is at position **115**. It is past the point we need, so it is discarded and
erased.

The gap between `n_tokens = 8599` and `pos = 128` is M-RoPE: the image spans ~8500 *tokens*
but only ~110 *positions*, so the whole page compresses into positions 4 - 115.

**The deadlock, precisely.** The only useful checkpoint position is immediately after the
image (~pos 115). Checkpoint creation is suppressed on exactly that iteration by
`server-context.cpp:3602`:

```cpp
// do not checkpoint after mtmd chunks
do_checkpoint = do_checkpoint && !has_mtmd;
```

The next opportunity comes after the trailing text tokens are batched, by which time the
position has advanced to 128 - past the divergence. **The checkpoint always lands too late to
be usable**, on every request, regardless of `-ctxcp` or `-cms`.

`n_swa = 0` is confirmed in the model load, so SWA plays no part; this is purely the
recurrent path.

## The one-line fix, implemented and measured - 95x

Removing the `!has_mtmd` suppression lets a checkpoint be created at the position that is
currently unreachable. Because `has_mtmd` is true only on the iteration that drains the image
chunks, dropping it lands exactly one extra checkpoint - immediately after the image, before
the text - and changes nothing else.

```diff
-                    // do not checkpoint after mtmd chunks
-                    do_checkpoint = do_checkpoint && !has_mtmd;
+                    // checkpoint after mtmd chunks is allowed. for memory that cannot roll back
+                    // (hybrid/recurrent), this is the only point to resume from without re-encoding the media
```

Built and measured on the Mac against **current master `8e7f22b67` (2026-08-12)**, which
already contains #26640. Baseline was re-measured on the same commit, so the comparison is
patch-only. Both runs used `-ctxcp 4 -cms 0`, identical image bytes, greedy with fixed seed.

| | baseline (master) | **patched** |
|---|---|---|
| request 1, cold | 61492 ms, `prompt_n` 8603 | 60637 ms, `prompt_n` 8603 |
| **request 2** | **59604 ms, `prompt_n` 8604** | **627 ms, `prompt_n` 20** |
| reply 1 | `Here are all the account numbers...2880-95701...` | **byte-identical** |
| reply 2 | `The statement period ... 01JUL22.` | **byte-identical** |
| peak wired (pages) | 1706391 | 1703470 |

**95x on the second request**, and the cold path is unaffected.

The log shows the mechanism working end to end:

```
task 0  | do_checkpoint = yes, pos_min = 4       <- NEW, right after the image
        | created context checkpoint 1 of 4 (pos_min = 4, n_tokens = 8584, 62.813 MiB)
        | created context checkpoint 2 of 4 (pos_min = 128, n_tokens = 8599)
task 67 | checking checkpoint with [128, 128] against 115...   <- rejected, too late
task 67 | checking checkpoint with [4, 4] against 115...       <- accepted
task 67 | restored context checkpoint (pos_min = 4, n_tokens = 8584, n_past = 8584)
```

`prompt_n = 20` is the new question's tokens; all 8584 image tokens are reused.

**Memory.** Each checkpoint is 62.8 MiB. The test used `-ctxcp 4` (~251 MiB cap); the default
32 would allow ~2 GB, which is plausibly why the suppression exists. An image workload wants a
low `-ctxcp` rather than the default.

**What this does NOT establish.** Correctness was checked on two prompts and one page, by
string equality of the replies. That is a smoke test, not a gate - a restored checkpoint that
was subtly wrong could still produce identical output on short greedy answers. Before this is
trusted, it wants the `pii_eval` scorer over the corpus. The reason the suppression was added
in #20726 remains undocumented and unknown; the possibility that it guards a real failure this
test does not exercise is open.

Patched tree is on the Mac at `~/src/llama.cpp` (master + the 2 lines above), built to
`~/src/llama.cpp/build/bin/llama-server` — which is now what production runs, see below.

## Deployment — what actually shipped, 2026-08-13

Production on the Mac now runs the patched build. `/opt/llama.cpp` is retired (Sergei is
deleting it), so **all four** `~/models/*/serve.sh` were repointed at
`$HOME/src/llama.cpp/build/bin/llama-server`, and the tree was rebuilt with every target so
nothing from `/opt` is lost — `llama-mtmd-cli`, `llama-quantize`, `llama-tokenize`,
`llama-perplexity` and the rest. Two carry-overs worth knowing: `ggml-rpc-server` is NOT built
(needs `-DGGML_RPC=ON`, unused here), and the web-UI bundle failed to download at configure
time, so this server serves the API but not the browser UI.

**The flag recipe is one flag: `-ctxcp 4`** on the two Qwen3.6 scripts.

- `-cms 0` is **not** required, contrary to what the experiments above ran with. It was a
  control to prove nothing was suppressed for spacing. The post-image checkpoint is the first
  in the slot, so it passes on `checkpoints.empty()` at `server-context.cpp:3605` before the
  spacing test is reached. Verified live: `min spacing = 8192` (the default) and the pos-4
  checkpoint is still created on every page.
- `4` caps checkpoint memory at ~251 MiB. Each request creates two checkpoints (one after the
  image, one after the text), so 4 holds a whole detect+localize pair with room to spare.
- The Qwen3-VL scripts deliberately did **not** get it: pure-attention models fail the
  `seq_rm_type` test at `server-context.cpp:3461` and never create a checkpoint at all, so the
  flag would be inert.

The same edit also reverted `--cache-type-k/v` from `q8_0` to `f16` on the 27B and MoE scripts,
which was an approved 2026-08-12 finding that had never been applied to those two files.

### Smoke test: 4 pages through the real pipeline

`sensitive/statements/1/1.pdf`, `strip --pdf` at the default `--geometry hybrid`, MoE
`UD-Q4_K_XL`, exit 0, 4-page image-only PDF out with a 0-character text layer.

| page | detect prefill | localize prefill |
|---|---:|---:|
| 1 | 59,734 ms / 9,036 tok | **565 ms / 281 tok** |
| 2 | 59,727 ms / 9,036 tok | **521 ms / 251 tok** |
| 3 | 60,126 ms / 9,036 tok | **650 ms / 364 tok** |
| 4 | 59,698 ms / 9,036 tok | **483 ms / 213 tok** |

All four localize calls logged `restored context checkpoint (pos_min = 4, n_tokens = 8584)`.
Pass 2 went from ~59 s to ~0.5 s, saving ~237 s on this document — and this is with the real
`_LOCATE_PROMPT` carrying its value list, which the synthetic follow-up above did not test.

Cross-page behaviour is correct and worth stating because it looks alarming in the log: each
new page's detect logs `forcing full prompt re-processing due to lack of cache data`. That is
right — a different page is a different image, the prefix genuinely does not match, and the
checkpoint from the previous page is correctly discarded. No contamination between pages.

Server RSS after the run was 25.1 GB, against 19.8 GB previously reported for this model. The
checkpoints account for ~251 MiB of that; the rest is the earlier figure having been read at
startup under `--no-warmup`, before the KV cache and compute buffers were touched. Not a
like-for-like pair, and not close to binding on 64 GB.

**What the smoke test does not establish:** it was not compared against the stock server, so it
shows the pipeline runs correctly and fast, not that output is identical to unpatched. One page
hit the known truncation path (`max_tokens`, salvaged as designed) — pre-existing behaviour,
unrelated to the patch.

## The alternative we did not take

The losing shape can also be fixed in our own code, by sending pass 2 as a continuation instead
of a fresh request:

```
[user: image + detect_prompt] [assistant: pass-1 reply] [user: locate_prompt]
```

That converts divergence into extension, and the measurement above says the winning shape costs
885 ms instead of 59600 ms. It was rejected in favour of the server patch, and the reason is
quality, not speed: **the patch is transparent to the model.** Same request, same prompt, same
context, replies byte-identical — a pure speedup that needs no re-gating. The continuation
carries the detection prompt and pass 1's own reply into pass 2's context, which changes what
the model sees and would need `pii_eval` to re-gate localization quality; the earlier finding
that prompt scoping failed asymmetrically (15 hallucinations on the second image) is a concrete
reason to expect context changes to matter here.

Keep it as the fallback if upstream rejects the patch and carrying a local build stops being
acceptable.

## Other things not verified

- Qwen3.6 was **not** reproduced on CUDA; no Qwen3.6 GGUF is on the Windows box and there is
  ~13 GB free. The claim that it is platform-independent rests on the mechanism plus the
  Qwen3-VL control, not on a direct CUDA run of Qwen3.6.
- Whether `--swa-full`-style "keep everything" has a recurrent analogue was not investigated.
- No upstream issue search was done for this specific interaction of mtmd + recurrent memory.
- Why `!has_mtmd` blocks checkpointing was not established. Removing it works and is measured
  above, but the suppression may still guard a real failure this test does not exercise -
  memory growth at the default `-ctxcp 32` is the most likely intent.
- **The patch is not gated with `pii_eval`.** Byte-equality of replies was checked on one page
  and two prompts; the smoke test above adds 4 real pages and the real `_LOCATE_PROMPT`, but
  against the patched server only. The outstanding check is cheap and is a *determinism diff*,
  not a recall score: run the corpus against patched and stock servers and compare the raw
  detect/locate JSON byte for byte. Equal output proves it is a pure speedup and no scoring is
  needed — the model's inputs never changed.

## Reproduction

Harness is on the Mac at `~/bench/casebtest.py` (divergence) and `~/bench/caseat2.py`
(extension). Both append random bytes after the PNG `IEND` to force a cold first request, read
`timings` from the response, and print projection / decode / wall. The CUDA-side equivalents
are `cachetest.py` and `structures.py` in the llama.cpp session scratchpad.
