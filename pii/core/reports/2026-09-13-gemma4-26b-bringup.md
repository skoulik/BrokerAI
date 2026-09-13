# Gemma 4 26B-A4B bringup: a page in 25 seconds, and three things the engine assumes about Qwen

**Date:** 2026-09-13 · **Status: serving layer deployed and measured; one engine change (box
order, asked in each model's native order) made; one corpus configuration run twice, thinking
off.** Sergei asked to try Gemma 4 as a
layer-0 candidate, starting with the 26B MoE variant at Q8_0 and with MTP.

**Corpus result (`real/1`, hybrid, thinking off):** recall **91.2%** (93/102) and **gate FAIL**
on one critical leak. Qwen3.8's recorded run scored 94.1% and passed, but under a different
configuration. **Every one of the 9 leaks is the truncated-own-entity class Qwen3.8 already
leaked on**, and 6 of them are the identical values. The run took **15 min** for survival and
**9 min** for grounding, against Qwen3.8's 2h20m and 2h18m. See "The corpus run" below.

**Headline:** detect + localize for one statement page takes **~25 s**. Qwen3.8-27B takes
**~167–270 s** for the same page, depending on reasoning effort (2026-08-19), and Qwen3.6-27B
~176 s. The gap comes from both ends of the request. The image is 1120 tokens instead of
~8900, and 4B active parameters decode at 65 tok/s with MTP, against 19 tok/s. The first of
those is also the main quality risk (below).

---

## Serving layer

**Model.** Everything is from `ggml-org/gemma-4-26B-A4B-it-GGUF`, all three files out of one
conversion. It is pinned to `google/gemma-4-26B-A4B-it` at 4d7ae4984b, which carries the fixed
chat template. SHA-256 of each file was verified against Hugging Face.

| file | size |
|---|---|
| `gemma-4-26B-A4B-it-Q8_0.gguf` | 26.9 GB |
| `mmproj-gemma-4-26B-A4B-it-BF16.gguf` | 1.19 GB |
| `mtp-gemma-4-26B-A4B-it-Q8_0.gguf` (MTP drafter) | 0.46 GB |

**The MTP head is a separate model here**, unlike Qwen3.8's `blk.*.nextn.*` tensors inside the
main GGUF. It is a small `gemma4-assistant` model that shares the target's embeddings and KV
cache, and it loads with `-md <file> --spec-type draft-mtp`. ggml-org also ships a DFlash
drafter for the same model; it is untried.

**llama.cpp had to be upgraded first.** The Mac's `brokerai-serving` sat on an upstream from
2026-08-25, 324 commits behind. Upstream has since fixed three things this model needs:

- Gemma 4's image attention mask, plus an image-token cap of 280 (#28335).
- The drafter's loader (#28183).
- Speculation after an image (#28715). Every request we send starts with an image.

The feature branches were rebased onto upstream b10939 and `brokerai-serving` rebuilt from
them. It is built to a **separate** `build-b10939/`, so `build/` still holds the binary
Qwen3.8 was measured on.

- Conflicts: none. `metal/language-version` was dropped, because upstream fixed the same thing
  independently (#27461).
- `local/mtmd-checkpoint` and `server/grammar-triggers` are both still needed: upstream still
  carries the guard and the shadowing.
- Upstream #28301 also edited `kernel_mul_mm_id`, the kernel the swizzle patches. The merge
  was automatic.
- test-backend-ops on MTL0 passes: MUL_MAT 1267/1267, and MUL_MAT_ID 844/844 including
  upstream's new token-tile boundary cases.

**Launch script.** `~/models/gemma-4-26b-a4b/serve.sh` follows the house style (`Q=`,
`MTP=on|off`, `NMAX=`). Three flags deviate from Qwen3.8's, each for an architectural reason
recorded in the script:

1. **`--image-min-tokens 1120 --image-max-tokens 1120`.** 1120 is the model's largest trained
   visual budget, and the one Google recommends for OCR and documents.
2. **`-ub 2048`**, up from 512. Gemma 4 attends to image tokens non-causally, so the whole
   image must fit in one ubatch, and 512 < 1120 would abort. Qwen3.8's `-ub 512` was a Metal
   cache-locality tuning for a dense FFN shape and does not transfer.
3. **`--swa-full` and no `-ctxcp`.** 25 of the 30 layers are sliding-window (1024 tokens). A
   full-size SWA cache makes any shared prefix reusable without checkpoints, so this model
   does not depend on the checkpoint patch.

The server's RSS is 33.8 GB with MTP on.

## Throughput

Setup: M1 Max 64 GB, Q8_0, `-c 32768 -np 1`. The page is page 0 of `statements/1/1.pdf`,
rendered at 300 DPI (2480x3505). Decoding is greedy, and each configuration was measured on a
freshly started server. The client is `pii.core.vlm.VlmDetector` itself with
`reasoning_effort="off"`, the production two-pass shape (detect, then localize).

| step | prompt_n / cached | prompt eval | decoded | wall |
|---|---|---|---|---|
| detect (cold) | 1427 / 0 | 9.0 s | 349 | 14.6 s |
| localize | 369 / **1098** | 0.5 s | 594 | 10.2 s |

- **Pass 2 reuses the image.** 1098 tokens are restored and only the new text is evaluated,
  with no checkpoint involved.
- **Nearly all of the 9 s prefill is the image**, since text evaluates at ~700 tok/s.
- **Page total ≈ 25 s.**

### MTP

Output was **identical** with MTP off and at every n-max, in values, token counts and
findings.

| `--spec-draft-n-max` | decode tok/s (detect / localize) | acceptance |
|---|---|---|
| MTP off | 48.2 / 48.1 | — |
| **2** (kept) | **64.8 / 63.7** | 97% / 93% |
| 3 | 65.9 / 63.2 | 95% / 89% |
| 4 | 64.6 / 64.2 | 92% / 90% |
| 6 | 58.8 / 58.0 | 81% / 79% |

That is +34% from MTP, flat from n-max 2 to 4. Qwen3.8 gained +77%; why this model gains less
was not investigated.

## Three things the engine assumes about Qwen

### 1. Gemma must be asked for boxes in its own order, y first

On the bring-up page Gemma answered **y first to a prompt that asks x first.** The localize
boxes were scored against Qwen3.8's boxes for the same values on the same page (the
2026-08-20 xhigh two-pass run, both normalized to 1000):

| boxes read as | median IoU vs Qwen3.8 (9 shared values) |
|---|---|
| printed (x first) | **0.000** |
| x/y swapped | **0.854** |

**The first fix was wrong, and the corpus showed it.** The first version of `--box-order`
kept the x-first prompt and swapped every Gemma box on the way back. Sergei's own sweep over
the reference folder then came back with 1.pdf's boxes out of place. Checked per page for
whether its boxes are wider than tall, as text lines are:
- **27 of 31 pages** were read y first, as expected, and the insurance certificate's page 6
  was mixed.
- **1.pdf pages 1, 3 and 4** came back x first. Swapping those made them wrong.
- **Page 1 itself had been y first** in the bring-up probe and in the eval run earlier the
  same day.

So Gemma *mostly* answers in its own convention against the prompt, but not always, and not
even stably for one page. No rule for reading the boxes back can be right.

**The shipped fix asks each model in its native order.** The x-first prompts are unchanged,
byte for byte, and are what Qwen is sent. Gemma gets the same prompts with only the
coordinate names swapped (`[y1, x1, y2, x2]`, `(y1,x1) top-left`). Asked y first, it answered y
first on **all 17 pages checked**:
- 1.pdf, all 4 pages, run twice, including the three that had flipped;
- a 3-page statement;
- the 6-page insurance certificate, whose page 6 had also been mixed (2 of 4).

Design and refusal rules: ARCHITECTURE "Layer 0"; flag usage: README.

### 2. A re-sent request is not guaranteed the same answer

The same detect request produced **15 findings (349 tokens) or 13 findings (298 tokens)**,
depending on what the server had cached:

- **Prompt evaluated whole (1427), or as a cached image plus 329 text tokens:** 15. The two
  agreed.
- **Prompt fully cached (`prompt_n` = 1, the last token re-evaluated alone):** 13.

It reproduces identically with MTP off, so it is not speculative decoding. The likely cause is
that the final prompt token's logits differ numerically between a 1-token batch and a
329-token batch (different Metal kernel paths), and this page sits on a near-tie. That was
inferred, not isolated.

Two consequences:

- **`vlm.http_transport`'s docstring says a retry "returns the same answer".** On this stack
  that is false exactly when a retry follows a reply that arrived at the server but not the
  client. The re-sent request then hits the full-prompt cache and can answer differently.
- **Gate runs must start from a fresh server**, or at least never re-send an identical page
  first. The ordinary pass sequence (fresh image, then pass 2 on a cached prefix) was
  deterministic across all five cold runs.

### 3. Thinking is a different protocol

Gemma 4 is switched into thinking by `enable_thinking`, or `<|think|>` in the system prompt. It
does not use Qwen's `reasoning_effort`. Its trace does not end in `</think>`, so `vlm.py`'s lazy
grammar trigger would never fire and the grammar would never engage. This run used
`reasoning_effort="off"`, which `vlm.py` already maps to `enable_thinking: false`, and that works
unchanged. **Thinking on is untested.**

## What one page says about quality, which is little

Gemma found 15 values and Qwen3.8 found 14. They share 9 exactly, with no class disagreement on
any of them. The rest, compared by class, length and nearest counterpart:

- **Same value, different extent (4 cases).**
  - Qwen's two ADDRESS lines are one ADDRESS for Gemma.
  - A Qwen 6-character identifier sits inside an 11-character one.
  - A 47-character ORGANIZATION sits inside a 72-character string that Gemma typed
    IDENTIFIER_GENERIC, so the two models also **disagree on its class**.
- **Qwen only:** one 14-character identifier has no counterpart on Gemma's side, so it is a
  probable miss.
- **Gemma only:** a 3-character ORGANIZATION, and 7- and 36-character identifiers, all
  unmatched.

Whether the unmatched values are real is not established. **The 1120-token image budget is the
open risk.** Qwen reads this page from ~8900 image tokens, so Gemma sees it at roughly an eighth
of the token count. A miss on a short identifier is the failure that budget would predict, but
one page cannot separate that from an ordinary model difference.

## The corpus run

**This run used the first, read-back version of the box-order fix**, the one that swapped
every Gemma reply. Its three flipped pages are therefore misread in the grounding numbers
below. The native-order re-run follows in "Re-run with native-order prompts". A one-page
`ground --limit 1` had looked right before the full run: model boxes contained 93% of their
values' ink, and everything was painted.

| | Gemma 4 26B-A4B | Qwen3.8-27B (2026-08-20) |
|---|---|---|
| quant | Q8_0 + MTP drafter | Q8_0, MTP head |
| geometry | **hybrid** (production) | combined (since rejected) |
| reasoning | **off** | xhigh |
| corpus | `real/1`: 11 docs, 31 pages | same |
| survival run | **15 min** | 2 h 20 m |
| grounding run | **9 min** | 2 h 18 m |

**The two configurations differ in geometry and in thinking, not only in the model.** No Qwen3.8
run exists under hybrid with thinking off. So this is a comparison against the only corpus
measurement there is, and not a controlled one.

### Recall — 91.2%, gate FAIL

| type | n | Gemma | Qwen3.8 |
|---|---|---|---|
| ORGANIZATION | 22 | **73%** (16) | 82% (18) |
| PERSON_JOINT | 7 | **86%** (6) | 100% |
| LOCATION | 2 | 0% | 0% |
| every other type (ADDRESS 22, AU_BANK_ACCOUNT 13, PERSON 14, AU_BSB 6, CREDIT_CARD 4, …) | 71 | 100% | 100% |
| **total** | **102** | **91.2%** (9 leaks) | 94.1% (6 leaks) |

The stripped outputs were re-read with the scorer's own instrument, and each leaked value was
checked against both runs:

| leak | Qwen3.8 | Gemma |
|---|---|---|
| the two LOCATION values (d01, d09) | leaked | leaked |
| `Sk Busines`, `Sk Ma` (d05), `SK` (d06, d09) | leaked | leaked |
| a 12-character truncation of the customer's management company (d05) | stripped | **leaked** |
| the joint holders' printed initials, `PERSON_JOINT`, **critical** (d10) | stripped | **leaked** |
| `SK` (d10) | stripped | **leaked** |

**All three extra leaks sit in the class the 2026-08-20 report already diagnosed.** Each is an
abbreviated or clipped rendering of the customer's own names inside a narrative line, which
`locate_borrowed` cannot connect back to the full name it holds, because none of its tiers
matches a prefix. Qwen3.8 covered those three only by detecting each rendering directly. The
same document, d10, also has the four partial paints below.

The two LOCATION leaks are by design: there is no standalone place-name detection. So nothing
in this run leaked because the model misread an ordinary, fully printed value.

**Over-strip** is within one or two of Qwen3.8 on every keep-type. ORGANIZATION: 7 kept and 17
over-stripped, against 8 and 16. ADDRESS: 1 kept and 3, against 0 and 4. PHONE_NUMBER, AU_ABN
and EMAIL are identical. So this is the same keep-list gap and not a precision change. 82 spans
were redacted from detections made elsewhere in their document, against 58.

### Grounding

| | Gemma: model boxes | Qwen3.8: model boxes | Gemma: painted | Qwen3.8: painted |
|---|---|---|---|---|
| occurrences boxed / fully covered | 136 / 209 | 173 / 209 | **181 / 209** | 185 / 209 |
| ink contained / mean ink painted | 58% | 71% | **89%** | 90% |
| partial paints | — | — | 8 | 7 |

- **Gemma boxes fewer occurrences and boxes them more loosely**, yet what reaches the page is
  within four occurrences of Qwen3.8. The identifiers carry the difference in boxing: AU_BSB
  is boxed 10/23 against 23/23, and AU_BANK_ACCOUNT 26/44 against 42/44. Their paint is
  nonetheless 20/23 and 42/44, recovered by layer 1 and the document-wide borrow.
- **PERSON_JOINT is the one real grounding loss**, painted 12/22 against 20/22. Four of the
  eight partial paints are one joint-name printing on d10 page 2, the same document as the
  critical leak.
- LOCATION paints 3/5 where Qwen3.8 painted 0/5.

### Verdict

Quality is **close to, not at**, Qwen3.8's recorded run: three more leaks and a failed gate.
The gap sits in a leak class both models share, and Gemma gets there at roughly a tenth of the
wall time. It was also measured with thinking off, against a Qwen3.8 run at xhigh. That confound
is exactly what the thinking-on follow-up would remove, which is the argument for pursuing it.

**Sergei's read, after his own sweep of the reference folder with native-order prompts
(2026-09-13):** *"the quality is really good keeping in mind how much faster it runs compared
to Qwen-3.8."*

## Re-run with native-order prompts

The same configuration (hybrid, thinking off, `real/1`) was run again after Gemma started
being asked for boxes y first. The detect prompt is identical between the two runs; only
pass 2's box wording differs. The survival run took 11.5 min and grounding 9.5 min.

| | Gemma, box read-back (run 1) | **Gemma, native order (run 2)** | Qwen3.8 (2026-08-20) |
|---|---|---|---|
| model boxes: occurrences boxed / usable | 136 / 130 | **158 / 155** | 173 / 151 |
| model boxes: ink contained / IoU | 58% / 54% | **69% / 54%** | 71% / 57% |
| model boxes matching no truth | 170 | 158 | — |
| painted: fully covered / mean / partial | 181 / 89% / 8 | 180 / 89% / 9 | 185 / 90% / 7 |
| recall (leaks) | 91.2% (9) | 90.2% (10) | 94.1% (6) |
| gate | FAIL (1 critical) | FAIL (same critical) | PASS |

**The model boxes are now close to Qwen3.8's.** The gain is where run 1's misread pages
landed:

| type | boxed, run 1 → run 2 | ink contained, run 1 → run 2 |
|---|---|---|
| AU_BSB | 10 → 20 of 23 | 41% → 82% |
| AU_BANK_ACCOUNT | 26 → 38 of 44 | 53% → 76% |
| PERSON | 18 → 19 | 70% → 82% |

**What reaches the page did not move,** because the locator had been absorbing the bad
boxes: 181 against 180 fully covered.

**The one extra leak is not caused by the prompt change.** It is `SK MANAGEMENT VICTORIA PTY
LTD`, printed on d10 page 1 inside a transaction line as `1/SK MANAGEMENT VICTORIA PTY LTD`.
Re-stripping d10 twice settled it:
- once as shipped;
- once emulating run 1 (x-first prompt, y-first read-back).

Both produce **identical findings and placements** on every page, and **both leak it**. Layer
0 detects the header form `SK MANAGEMENT VICTORIA PTY LTD AS TRUSTEES FOR SK BUSINESS TRUST`.
The transaction line prints only a prefix of that, which no borrowed-search tier matches: the
same prefix gap as every other leak here.

That run 1 covered it anyway is **run-to-run variance in layer 0**, and the most likely cause
is the re-sent-request divergence in `TODO.md`. That is a second piece of evidence it matters:
it moved a corpus leak, not only a one-page finding count.

## What this does NOT establish

- **A controlled comparison.** Geometry and thinking both differ from the Qwen3.8 run.
- **Whether thinking closes the gap.** Thinking-on is not implemented for Gemma.
- **Whether the 1120-token budget costs recall.** No leak here points at a misread, but the
  truncated-entity class would hide one.
- **Precision.** The truth's deliberate gaps still conflate false positives with policy.
- **The combined one-pass shape**, **DFlash**, and **Q4**.
- **Swizzle speed after the rebase.** Correctness was tested, speed was not re-measured.
- **The cause of the cache-state flip.** It is inferred, as stated above.

## Next

Scheduled in [TODO.md](../TODO.md), on Sergei's condition that the corpus quality be
promising:

1. **Debug the re-sent-request divergence.** Also correct `http_transport`'s idempotency claim.
2. **Support thinking for Gemma and run the corpus with it on.** This is the run that makes
   the Qwen3.8 comparison fair.

## Reproduction

`gemma4_probe.py` in the session scratchpad. It imports `pii.core.vlm` and runs detect,
localize and detect again. It prints no PII values, only counts, classes, timings, box IoU
against the Qwen3.8 run, and a value-free diff. Server restarts between configurations used
`serve.sh` with `MTP=` and `NMAX=`.

The corpus run used `serve.sh` defaults (MTP on, n-max 2) and `PII_VLM_URL` pointing at the
Mac:

```
python -m pii_eval score  -c pii_eval/corpora/real/1 --modality pdf --reasoning-effort off
python -m pii_eval ground -c pii_eval/corpora/real/1 --reasoning-effort off
```

Stripped outputs are kept beside the corpus: `stripped.gemma4_26b_hybrid_off`, and the Qwen3.8
run's output copied to `stripped.qwen38_combined_xhigh` before this run overwrote `stripped/`.
The side-by-side leak table came from re-reading both with `reread_engine` and `find_value`,
the scorer's own instrument.
