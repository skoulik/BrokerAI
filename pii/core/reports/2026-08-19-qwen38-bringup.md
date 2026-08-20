# Qwen3.8-27B bringup: MTP, context checkpoints, and thinking with a lazy grammar

**Date:** 2026-08-19 · **Status: mechanisms verified and measured on ONE page. Nothing in
`pii/` changed yet, and no quality claim is made.** Serving layer is deployed on the Mac; the
engine changes and the `pii_eval` comparison are open (see "Next").

Qwen3.8-27B was released 2026-08-14 (Apache 2.0, 27.8B dense, text+image+video, 262K native
context). Sergei asked to try it in place of Qwen3.6, with three requirements: make the MTP
path work, keep context checkpoints working (the location pass depends on them), and turn
thinking ON — with a reasoning budget, a cut-off message, and a grammar applied lazily so the
GBNF never runs on the model's thinking text.

All three work. Two findings turned out to matter more than the bringup itself: **our own prompt
was suppressing the thinking we were trying to enable**, and a **single combined detect+localize
pass** (Sergei's suggestion) is both cheaper and, on this page, better than the two-pass shape
production runs today.

---

## The fact that carried the most weight

**Its GGUF declares `general.architecture = qwen35`** — the same arch as Qwen3.6, with the same
`qwen35.ssm.*` keys. So it is the same hybrid recurrent family, and everything in
[2026-08-13-qwen36-ssm-prompt-cache.md](2026-08-13-qwen36-ssm-prompt-cache.md) transfers
unchanged: memory that cannot roll back to an arbitrary position, an image prefill cache that
only survives a strict extension, and a post-image context checkpoint as the only escape.
Upstream still suppresses exactly that checkpoint (`do_checkpoint = do_checkpoint && !has_mtmd`
is present on master b10499), so **our patch is still required and still applies.**

It also carries `qwen35.nextn_predict_layers` and `blk.64.nextn.*` tensors, at `type = q8_0` in
bartowski's Q8_0 — the MTP draft head is inside the main GGUF, not a separate file.

## Serving layer

`~/src/llama.cpp` branch **`brokerai-serving`**, two commits on top of **b10499**, built to
`build 10501`. The checkpoint patch was an uncommitted working-tree edit before this; it is now
a real commit, and the dead `has_mtmd` variable it left behind (a compiler warning) is removed.

The second commit is new and is the one that made the whole thinking design reachable:

**`oaicompat_chat_params_parse()` silently dropped a request's `grammar_lazy` and
`grammar_triggers`.** Both were set unconditionally from `chat_params`, while `grammar` itself
is set only when the chat template produced one; the copy-remaining-properties loop then fills
only keys that are *absent*. So on `/v1/chat/completions` a client supplying its own grammar
plus its own lazy configuration got the grammar honoured and the triggers discarded, and the
grammar applied from the first token. Moving both inside the same guard as `grammar` fixes it
and changes nothing else: with no template grammar there is nothing to be lazy about. Written
to upstream standard; worth offering upstream.

Model dir `~/models/qwen3.8-27b/` with `dl.sh` and `serve.sh` in the house style
(`Q=` quant, `MTP=on|off`, `NMAX=` draft width). Two deliberate deviations from
`../qwen3.6-27b/serve.sh`, both documented in the file: `-c 32768` (thinking makes generation
long) and `--spec-type draft-mtp`. Reasoning-budget flags are deliberately NOT in `serve.sh` —
they are per-request, so the output shape stays versioned with the code that parses it.

## How thinking and a GBNF coexist — the mechanism, confirmed

llama.cpp already implements this and we did not have to invent it. In `common/sampling.cpp`,
`grammar_should_apply()` returns false while the reasoning-budget sampler is inside the
thinking block, *provided the grammar is lazy*. So the GBNF is suppressed for the whole trace
by construction, not by a trigger regex that happens to avoid it.

Three details had to line up, and each was verified rather than assumed:

1. **The budget sampler must arm even though the model never emits `<think>`.** Qwen3.8's
   template ends the generation prompt with `<think>\n`, so thinking is *forced open* and only
   the closing tag is generated. `common_sampler_init` feeds `prefill_tokens` — the tokenized
   generation prompt — into the budget sampler, so it enters COUNTING before token 0. Log
   confirms: `cmn common_reaso: activated, budget=...`.
2. **The trigger must not feed `</think>` into the grammar.** llama.cpp replays into the
   grammar everything from the first non-empty *capture group*, falling back to the whole match
   when there is none. A bare `</think>` word trigger would therefore replay `</think>` into a
   grammar whose root starts with `[`, and reject everything. The trigger used is
   `</think>[\s\S]*?(\[)`, capturing the bracket.
3. **The trigger `type` is an int on the wire** (`PATTERN` = 2), not a string. A string throws
   a JSON type error.

The server's debug output shows the handoff exactly:

```
Grammar still awaiting trigger after token 248069 (`</think>`)
Grammar still awaiting trigger after token 71093 (```)
Grammar still awaiting trigger after token 2164 (`json`)
Grammar triggered on regex: '['
```

**A consequence to be honest about:** with a lazy grammar the model may emit an unconstrained
preamble before the array — a ```` ```json ```` fence, in practice. `_extract_array` handles it
(including the *unclosed* fence the grammar produces, via its plain `find("[")` fallback), so
it parses. But `Incomplete.malformed`'s docstring claim that malformed output is "unreachable"
with a grammar in force is no longer strictly true, and should be corrected when the engine
changes land.

## Throughput

M1 Max 64 GB, Q8_0, `-c 32768 -ctxcp 4 -np 1`, page 0 of `sensitive/statements/1/1.pdf` at
300 DPI, greedy with seed 42. Prefill is 8902 image tokens at ~73.5 t/s = **~121 s, and is
independent of every knob below**. Only the first run of a page pays it: consecutive requests
on the same image restore the post-image checkpoint (`prompt_n` 8902 → 336), which is the patch
working, so **decode tokens are the comparable quantity here, not wall time**.

### Reasoning effort (two-pass, budget 4096, which never fired)

| effort | detect think / answer | localize think / answer | total decode | findings |
|---|---|---|---|---|
| **off** | 0 / 298 | 0 / 550 | **848** | 14 |
| low | — / — | — / — | 2317 | 14 |
| medium | **455** / 289 | **1515** / 540 | 2805 | 14 |
| xhigh | 880 / 301 | 2077 / 606 | 3870 | 15 |

Projected page cost at ~19.3 tok/s decode plus ~121 s prefill: **~167 s** (off), ~217 s
(medium + a budget that bites localize), ~268 s (medium), ~322 s (xhigh). For scale, Qwen3.6-27B
is recorded at ~176 s/page (2026-08-18).

Three observations:

- **Localize is where thinking is wasted.** It spends 1515 thinking tokens placing 14 strings it
  was handed — 3.3x what detect spends actually reading the page.
- **`medium` is not a midpoint — it is the absence of an instruction.** The chat template sets
  `reasoning_instructions = ''` and then has branches for `xhigh` and `low` only; there is **no
  `medium` branch**. So `medium` injects nothing and is the model's unmodified behaviour, while
  `low` and `xhigh` each prepend a system sentence. That is why `low` produced a *longer* detect
  trace than `medium` (998 vs 747 predicted tokens): it is not the model ignoring a brevity
  instruction, it is a brevity instruction added to a baseline that had none.

  It also explains a cache result that looked wrong: the `off` run reported `prompt_n` 320
  rather than re-projecting, because `off` and `medium` both inject no system text and therefore
  share a byte-identical prefix ahead of the image. `low` and `xhigh` change that prefix and
  force a full re-projection — which matters for eval timing, since switching effort costs a
  cold page.
- **These numbers understate thinking's cost, and cannot answer whether it helps.** Our own
  prompt was suppressing thinking while they were taken — see the next section.

### The reasoning budget

At `reasoning_budget_tokens: 512` the localize trace halved (1515 → ~512 thinking tokens, total
2058 → 1067) with **identical findings**, and the cut-off message appeared at the end of the
trace as designed. Detect was untouched, its trace being already under the cap. At 4096 nothing
in the sweep was cut — so 4096 is a pure runaway-loop safety net rather than a shaping knob,
which is what it is wanted for while the quality question is open.

Cut-off message in use: `"\n\nEnough thinking. I will now output every identifier found.\n"`
(Sergei's wording). It is injected before the forced end tag, so it becomes the last line of
the trace. It is effectively prompt text and biases toward completeness, which is the right
asymmetry for a redaction tool — but like every other prompt scope decision here, its effect is
unmeasured.

### MTP

Output is **identical** across all of these (same `predicted_n`, same findings), so speculative
decoding is a pure speedup and greedy determinism — which the gate depends on — survives.

| `--spec-draft-n-max` | decode tok/s | acceptance | mean accepted len |
|---|---|---|---|
| MTP off | **10.90** | — | — |
| **2** | **19.3** | **86%** | 2.77 |
| 3 | 18.7 | 78.5% | 3.35 |
| 4 | 17.3 | 70% | 3.81 |

**+77% from MTP**, well above the +33-39% reported for consumer GPUs. n-max 2 is the optimum and
the decline past it is monotonic: acceptance falls faster than the extra draft length pays.

### One pass instead of two (Sergei, 2026-08-19)

Asking for values and boxes in a single call, with the boxes used as locator *search
constraints* rather than painted:

| | values | boxed | thinking | decode tokens | page wall |
|---|---|---|---|---|---|
| two-pass, medium | 14 | 14 | 455 + 1515 | 2805 | 270 s |
| **combined, medium** | **14** | **14 / 14** | 946 | **1598** | **205 s** |
| combined, xhigh | 14 | 14 / 14 | **0** | 664 | 156 s |

43% fewer decoded tokens for the same values and full box coverage. This deliberately re-opens
the 2026-08-08 decision that split the passes (350 → 324 distinct values over 31 pages when
asked for both at once) — that measurement was taken on Qwen3.6 with thinking OFF, so a
reasoning model invalidates its premise. **One page cannot refute a 31-page result**; this says
only that no loss is visible here.

Note what it would mean if it holds: pass 2 disappears, and with it production's dependence on
the context-checkpoint patch.

## Our own prompt was suppressing thinking

The `combined, xhigh` row above — **zero thinking tokens** — is what exposed this. Greedy with a
fixed seed, so not sampling noise, and the contrast rules out "xhigh suppresses thinking":
two-pass detect at xhigh *did* think (880 tokens) with the values output spec. Same effort,
different output spec, opposite behaviour.

`vlm.PROMPT` carries two sentences written for a non-thinking regime. Isolating them (identical
image, identical system instruction at xhigh, identical grammar, only the user text varying — and
because the user text sits *after* the image, all of these reuse the post-image checkpoint and
are cheap):

| variant | thinking tok | decode | findings | leading fence |
|---|---|---|---|---|
| as-is (production) | **0** | 664 | 14 | no |
| no "Do not explain your reasoning." | 1379 | 1989 | 13 | no |
| no "Stop immediately after the closing ]." | 0 | 667 | 14 | **yes** |
| neither | 2185 | 2798 | 13 | **yes** |
| **proposed** (see below) | **1185** | 1885 | **15** | no |

Each sentence turns out to control something other than what it says:

- **"Do not explain your reasoning." is what suppresses thinking.** Removing the other sentence
  alone changes nothing.
- **"Stop immediately after the closing ]." is what suppresses the leading fence** — and its
  stated job is redundant under a grammar, since the root reaches an accepting state after `]`
  and only EOG stays legal. The fence is the only thing it was actually buying, and it never
  mentions it.

**Consequence for the effort numbers above: they were measured against a prompt that fights
thinking.** Some configurations thought anyway (medium+boxes 946 tokens, xhigh+values 880) and
this one did not, so the suppression is a conflict the model resolves inconsistently. Their cost
figures are therefore an underestimate, and "no evidence thinking finds more" was confounded —
it was partly not thinking.

### The wording adopted (Sergei, 2026-08-19)

Drop `Do not explain your reasoning.` outright, and replace `Stop immediately after the closing
].` with:

> Output only the JSON array, with no code fence and no other text.

Measured on the combined pass:

| effort | thinking | answer | decode | findings | fence |
|---|---|---|---|---|---|
| medium | 836 | 697 | **1536** | **15** | no |
| xhigh | 1185 | 697 | 1885 | **15** | no |

It beats both the current prompt (14 findings) and the naive deletion (13), thinks at both
efforts, emits no fence, and reports nothing malformed or truncated. Note the *answer* is the
same size at both efforts (697 tokens) while thinking differs by 349 — so xhigh is paying ~19%
more decode for what may be an identical answer, which points at `medium` as the default.

Best configuration measured today — combined pass, medium, this prompt — is **1536 decode tokens
and 15 findings**, against production's current two-pass shape at **2805 tokens and 14 findings**:
cheaper and finding more, projecting to ~199 s/page against the incumbent's ~176 s.

`_LOCATE_PROMPT` carries the same "Stop immediately" sentence and needs the same treatment for as
long as the two-pass regime exists.

## What this does NOT establish

- **Any quality claim whatsoever.** One page, finding *counts* only — nothing about precision,
  span granularity, or the values themselves. A count going 14 → 15 is not evidence the 15th is
  real, and the prompt comparison above is decided on counts alone.
- **That thinking is worth its cost.** The one page where thinking was genuinely enabled found
  one more value than the page where it was suppressed. That is a hint, not a result.
- **That `medium` and `xhigh` produce the same answer.** Their answers are the same *size*
  (697 tokens); they were not diffed.
- **That the combined pass is safe**, for the reason stated above.
- **Box quality as constraints.** The combined pass boxed 14/14, but coverage is not accuracy;
  only running the real `locator` over them shows whether they constrain to the right text.
- **Anything about other pages, other documents, or the 4-bit quant.**
- The `low`-effort row was measured before exact token counting was added, so its split between
  thinking and answer is unknown.

## A live bug found on the way

`vlm.strip_thinking()` matches `<think>.*?</think>`. With forced-open thinking the opening tag
is in the *prompt*, so a reply carries only the trailing `</think>` and nothing is stripped.
`parse_findings` then scans the unstripped reasoning, latches onto a `[` inside it, and returns
**`[]` — an empty list, indistinguishable from a clean page.** Reproduced:

```python
raw = 'Let me look. I see [account] numbers.</think>\n\n[{"text": "John Smith", "type": "PII_NAME"}]'
parse_findings(raw)   # -> []   (the real finding is lost)
```

Production does not currently hit this, because llama.cpp's default `reasoning_format: deepseek`
splits the trace into `message.reasoning_content` and leaves `content` clean. But
`parse_findings` is documented as the entry point for callers holding only a body, so this is a
live defect in a public function, and it is exactly the silent-failure shape `DetectorResult`
exists to prevent. Fix plus a pytest test plus a corpus probe, per the dual-coverage rule.

## Next

1. Engine changes in `pii/core` (thinking on, budget, lazy grammar, the two prompt sentences,
   the `strip_thinking` fix, `max_tokens` sized to budget + answer, a `--reasoning-effort` flag).
2. `pii_eval` over the corpus: the incumbent vs Qwen3.8 at medium and xhigh, plus the combined
   pass. This is where the quality question is finally asked. The prompt change must be scored
   here too — it is the one change so far that altered a finding count in either direction, and
   the four load-bearing prompt properties give ample precedent for small edits having large,
   non-obvious effects.
3. The 4-bit quant of the same model, for the throughput/quality attribution.
4. Decide production model AND quant, and write it in the dependency table — the open TODO from
   2026-08-19 that this work makes newly urgent, since there are now three candidates.

## Reproduction

`phase2_probe.py` and `combined_probe.py` in the session scratchpad. They run on WINDOWS and
**import `pii.core.vlm`** rather than copying from it — deliberately: `~/bench/bench_client.py`
on the Mac carries a hand-copied `PROMPT` that has since drifted from `vlm.py`, so its numbers
were measured against a prompt production no longer sends. That harness should be retired or
made to import.
