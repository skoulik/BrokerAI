"""Layer 0: a local vision LLM reads the page image and names the PII.

This is NOT an OCR backend. An OCR adapter feeds text into the analyzer; this
reads pixels and produces detections directly, joining at the same seam
`PiiPipeline.detect` does. It does NOT replace layer 1: each value it finds is
located in the OCR text and then refined, validated and extended by a layer-1
pass over that same text (`PiiPipeline.merge_detections`) — checksums are a
signal a VLM structurally cannot produce, and it is measurably unreliable at
*typing* an identifier even when it reads one correctly. The design decision
is recorded in ARCHITECTURE.md; the measurements behind it are in
[reports/2026-08-08-vlm-oneshot-qwen36.md](reports/2026-08-08-vlm-oneshot-qwen36.md).

Four geometry regimes exist, and all four are deliberately kept:

- ``geometry="hybrid"`` — production. Two passes: `detect` names the values,
  `localize` hands them back and asks only where they are. The boxes are then
  used as a **search constraint** by `locator.py`, which paints OCR word boxes
  wherever the value can be matched and falls back to the model's box only for
  the residue that has no OCR text at all. Rationale for the split lives on
  `_LOCATE_PROMPT`; rationale for boxes-as-constraint lives in `locator.py`.
- ``geometry="combined"`` — ONE pass asking for values and boxes together, whose
  boxes are then used exactly as hybrid's are: a search constraint, never paint.
  Note what this is NOT: `vlm` below also asks one pass for boxes, but *paints*
  them, which is the part measured unsafe. Introduced 2026-08-19 (Sergei) when
  layer 0 became a reasoning model, because the split makes the model think
  twice per page and the second trace is spent placing strings it was handed —
  1515 thinking tokens against detect's 455. It deliberately re-opens the
  2026-08-08 decision that created the split (350 -> 324 distinct values over 31
  pages when both were asked at once), on the grounds that that measurement was
  taken with thinking OFF and a reasoning model invalidates its premise.

  **Measured and REJECTED for production (Sergei, 2026-08-20): a comparison
  instrument, like `vlm` below.** It is genuinely cheaper — 43% fewer decoded
  tokens than the two-pass shape on one page, and it thinks once instead of
  twice — but its boxes are unreliable in a way that reaches the output. On a
  disclosure page carrying 21 occurrences of one short token, 12 of the 21
  boxes enclosed no instance of it, landing on paragraph-initial words instead.
  A wrong box makes `locator` claim the wrong text, and the keep list is then
  consulted on the CLAIMED text: an `ANZ` detection that claimed the adjacent
  word `any` was pseudonymized, because `anz` is on the keep list and `any` is
  not. The 2026-08-08 report had already measured one-pass boxes as looser
  (1.41x ink against the two-pass 1.24x); this is that finding arriving again
  from the other side. Details: reports/2026-08-20-qwen38-corpus-eval.md.
- ``geometry="ocr"``    — one pass, values only. The same locator runs, but
  with no boxes to constrain it, it degrades to page-wide exact-or-squash
  matching: the pre-box behaviour, kept as the comparison baseline, with the
  presence of boxes as the only variable between it and hybrid.
- ``geometry="vlm"``    — the model's own ``bbox_2d`` is painted directly and
  OCR never runs. Measured **unsafe**: 16% of boxes clip by more than 20 px,
  the tail includes real account numbers, and the failure is *stochastic* (the
  same value on the same layout is boxed correctly on one page and wrongly on
  the next), so no padding or calibration fixes it. A comparison instrument,
  never a production option.

Output is constrained at the sampler by a GBNF grammar (see the grammars below)
and every reply goes through `read_response`, which reads `finish_reason` — an
empty result must never be confused with a clean page.

The transport is injectable so the testbench never needs a model server, which
also means the grammar can be ignored by whatever is on the other end: the
defences in `parse_findings` stay regardless.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable, Protocol

# VLM class -> pipeline entity type. The VLM deliberately emits COARSE classes:
# the split follows "can a deterministic recognizer re-derive this class from the
# string alone?" - identifiers can (regex + checksum, layer 1's job, and the VLM
# is measurably unreliable at it), names/addresses/companies/dates cannot.
#
# The class names never say "PII" (2026-09-14): Gemma 4 read "PII" as personal
# information and argued organizations, their addresses and web addresses out of it
# page by page. Both prompts use these names.
TYPE_MAP = {
    "NAME": "PERSON",
    "ADDRESS": "ADDRESS",
    "COMPANY": "ORGANIZATION",
    "DOB": "DATE_OF_BIRTH",
    "IDENTIFIER": "IDENTIFIER_GENERIC",
}

# The model server usually runs on another machine (a Mac with enough unified
# memory), so the localhost default is rarely right. PII_VLM_URL saves passing
# --vlm-url on every invocation — same idea as the retired Surya adapter's
# SURYA_INFERENCE_URL.
DEFAULT_URL = os.environ.get("PII_VLM_URL") or "http://localhost:8080"
DEFAULT_PAD = 8  # px, at the analysis DPI

# How many times `http_transport` asks before giving up, and the base delay
# between attempts (multiplied by the attempt number). Small on purpose: this
# recovers a dropped connection, it is not a queue for a server that is down —
# three attempts over ~6 s distinguishes a blip from an absence without making
# "wrong --vlm-url" take a minute to report.
TRANSPORT_ATTEMPTS = 3
TRANSPORT_BACKOFF = 2.0  # seconds

# Reasoning. Layer 0 is a thinking model as of 2026-08-19, and thinking is ON.
# How a level reaches the model is the model FAMILY's (see `ModelFamily`): Qwen's
# template reads the level and raises on anything else - and there `medium` is NOT
# a midpoint, since the template sets an instruction for `xhigh` and `low` only,
# so `medium` injects nothing and is the model's unmodified behaviour - while
# Gemma's has no levels at all, only thinking on (`medium`) or off. "off" is a
# COMPARISON INSTRUMENT on the detection pass, for the same reason
# `geometry="vlm"` is one: it keeps the measurement that chose the default runnable.
REASONING_EFFORTS = ("low", "medium", "xhigh", "off")
DEFAULT_EFFORT = "medium"

# The GROUNDING pass (`localize`, pass 2 of `hybrid`) has its own effort, OFF by
# default for every model (Sergei, 2026-09-13). It is handed the values and asked
# only where they are, and both bring-ups measured its thinking as spent on that:
# Qwen3.8 thought 1515 tokens placing 14 given values against 455 finding them,
# Gemma 4 spends 3-4k tokens for the same boxes it draws without thinking.
# "same" copies the detection effort.
#
# The cost of OFF is the image cache, and it is not uniform. Pass 2 reuses pass
# 1's image only when everything ahead of the image is byte-identical: Qwen's
# `medium` and `off` inject nothing there, so they share it, while `low`/`xhigh`
# prepend a system line and pay a full image prefill again (~120 s on a Qwen3.8
# page). Gemma's thinking switch adds a system turn, so pass 2 re-reads its image
# (~9 s). Pass-2 image reuse after a long pass-1 trace was also seen to FAIL with
# a byte-identical prefix (2026-09-13, cause not established), so on Gemma the
# prefill is not reliably saved by keeping thinking on either.
GROUNDING_REASONING_EFFORTS = REASONING_EFFORTS + ("same",)
DEFAULT_GROUNDING_EFFORT = "off"

# A cap on the thinking block. It exists to bound a runaway repetition loop,
# which greedy decode on a reasoning model makes a live risk, and greedy is not
# negotiable because the gate needs determinism. On Qwen3.8 nothing in the
# 2026-08-19 sweep reached 4096. Gemma 4 thinks longer and reached it on about a
# third of `real/1`'s pages (2026-09-13), so a run can set its own
# (`--reasoning-budget`), and each reply that reaches it is counted
# (`Incomplete.reasoning_budget_hit`).
DEFAULT_REASONING_BUDGET = 4096

# The answer's own allowance. This was the whole of `max_tokens` before thinking
# existed, and what sized it - a dense page of findings - has not changed.
ANSWER_TOKENS = 4096

# Injected immediately before the forced end-of-thinking tag when the budget
# runs out, so a cut-off trace lands on the answer rather than stopping
# mid-sentence. It biases toward completeness because this tool's asymmetry
# does: over-strip is recoverable, under-strip is a breach.
REASONING_CUTOFF = "\n\nEnough thinking. I will now output every identifier found.\n"

# Trigger types are ints on the wire - llama.cpp's server reads
# `in.at("type").get<int>()`, so a string is an HTTP 400, not a fallback. 2 is
# PATTERN.
_TRIGGER_PATTERN = 2

# The three geometry regimes described above. "hybrid" is production; the
# other two are kept so the comparison that produced that verdict stays
# runnable. Declared here rather than in image_mode because the CLI resolves
# the flag before it is willing to import the analysis stack.
GEOMETRIES = ("hybrid", "combined", "ocr", "vlm")
DEFAULT_GEOMETRY = "hybrid"

# The order the four `bbox_2d` integers are ASKED for, and so read back in. A model
# asked against its native order does not reliably comply: Gemma 4, told x first,
# answered y first on 27 of 31 real pages, x first on three and a mix on one - one
# page flipping between runs (2026-09-13). A wrong order raises nothing, so each
# model is asked in its OWN order: "auto" takes the family from the served model's
# name before the first boxed request, and refuses a model it cannot place rather
# than guess. The explicit orders are the override.
BOX_ORDERS = ("auto", "xyxy", "yxyx")
DEFAULT_BOX_ORDER = "auto"


@dataclass(frozen=True)
class ModelFamily:
    """Everything about talking to layer 0 that differs between model families:
    the box order to ask in, how thinking is switched on and read back, and
    whether the server may reuse cached prompts.

    Keyed on the FAMILY name in the served model's file name
    (`family_for_model`), so every Gemma is assumed to speak like Gemma 4
    26B-A4B, the only one measured, and every Qwen like Qwen3.6/3.8."""

    name: str
    box_order: str
    # The effort levels the chat template reads. Empty means thinking is a plain
    # switch, turned on by `medium` (the default) and nothing else.
    efforts: tuple[str, ...]
    # Engages the lazy grammar at the answer's "[" once the trace has CLOSED.
    # llama.cpp replays into the grammar everything from the first non-empty
    # capture group, falling back to the whole match when there is none - so a
    # bare end-of-trace trigger would replay the end tag into a grammar whose
    # root starts with `[` and reject every continuation. Capturing the bracket
    # is what makes the handoff exact.
    trigger: str
    # Sent as `cache_prompt`. Off makes greedy output reproducible: llama-server
    # reuses cached prompts - including ones kept in host memory from EARLIER
    # requests (`--cache-ram`) - and a reused prefix changes how the rest of the
    # prompt is batched. A full hit re-evaluates just the last token, in a batch
    # of one, whose logits differ from the same position inside the full batch;
    # greedy then takes another path - other findings, on some pages no thinking
    # at all - and even a 2-token reuse flipped grounding answers. Off, two runs
    # of `real/1` matched request for request (2026-09-15, pii/core/DONE.md).
    # On is a trade of that for prefix reuse, worth making only where the
    # serving setup is built on it.
    prompt_cache: bool

    def supports(self, effort: str) -> bool:
        return effort == "off" or effort in (self.efforts or ("medium",))

    def thinking_kwargs(self, effort: str) -> dict:
        """The `chat_template_kwargs` for `effort`. "off" is the same for every
        family - both templates then write a pre-closed trace - which is what
        lets a thinking-off request go out before the family is known."""
        if effort == "off":
            return {"enable_thinking": False}
        if self.efforts:
            return {"reasoning_effort": effort}
        return {"enable_thinking": True}


# Qwen closes its trace with `</think>`; its template opens the trace itself.
# Prompt cache ON, knowingly giving up run-to-run reproducibility (Sergei,
# 2026-09-15): Qwen3.6/3.8 is hybrid SSM+attention, so pass 2 over a page is
# cheap only by restoring the checkpoint taken right after the image (patched
# llama-server, -ctxcp > 0) - ~0.5 s against ~60 s of image prefill. Not
# re-measured for determinism; Qwen may be retired. A gate run on Qwen that must
# reproduce wants this off.
QWEN = ModelFamily(
    "qwen", "xyxy", ("low", "medium", "xhigh"), r"</think>[\s\S]*?(\[)", prompt_cache=True
)
# Gemma writes `<|channel>thought ... <channel|>`, from a `<|think|>` system turn
# that `enable_thinking` adds; llama.cpp's gemma4 parser registers those tags, so
# the reasoning budget and the `reasoning_content` split work unchanged (2026-09-13).
# Prompt cache off: with detection thinking (the default) reuse bought nothing -
# pass 2's system turn differs from pass 1's, so it reused 2 tokens, and `real/1`
# ran 42.4 min off against 43 on. With thinking off in both passes it would reuse
# the image, ~9 s a page.
GEMMA = ModelFamily("gemma", "yxyx", (), r"<channel\|>[\s\S]*?(\[)", prompt_cache=False)
FAMILIES = (QWEN, GEMMA)

# The detection prompt (pass 1). What each part is for, and which parts were
# established by measurement - edit those only with a run to show for it:
#  - coarse classes (measured): collapsing 14 -> 5 cost no recall and GAINED
#    generalization (a vehicle registration was caught with no mention of vehicles);
#  - identifiers-live-anywhere + naming "insurance policy, reference or claim"
#    (measured): a policy number rendered as a bold heading was missed until both
#    were present;
#  - no "PII" anywhere in the wording (2026-09-14, see TYPE_MAP): the word
#    carries the model's own idea of personal information, which is narrower than
#    what this tool strips;
#  - no exceptions for organizations, and "when unsure, include it": over-strip is
#    recoverable by the keep list and under-strip is a breach, so the prompt must
#    not leave the model a keep decision of its own. Without the sentence, Gemma 4
#    made one per page and made it differently on each - it left a bank's address
#    out "to be safe" on one page and put another in on the next, and did the same
#    with web addresses (2026-09-14). Filtering organizations is the keep list's job;
#  - value-not-label: a label is evidence, not part of the value, and a labelled
#    value keys the pseudonym map on a different string than a bare one. Without
#    it the output format's "exactly as printed" was read as licence to copy the
#    label. Keep its scope narrow: widening it to "never the label, heading or
#    caption" once made the model grab whole transaction-narrative rows;
#  - one line for a multi-line value: the locator matches either way, and without
#    the sentence the model spent its thinking choosing between a line break and a
#    space;
#  - decisions the traces showed the model re-deciding on every page, settled in the
#    definitions rather than as extra rules (a rule is one more thing it re-checks):
#    a trust, fund or brand is a COMPANY (Sergei; "brand or service" moved the argument
#    to brand-versus-product, and adding "product" cost recall on real/1); page and
#    statement numbers are not identifiers (Sergei: out); the label example is an
#    ABN, the label it hesitated over (2026-09-14);
#  - distinct values (`_OUTPUT_VALUES`) against every printing (`_OUTPUT_BOXES`):
#    see those two.
# The 2026-09-14 changes were measured together on real/1 (DONE.md). Older
# measurements: DONE.md, reports/2026-08-19-qwen38-bringup.md.
#
# Two sentences must stay OUT, both removed 2026-08-19 when layer 0 became a
# reasoning model, because each controlled something other than what it said:
#  - "Do not explain your reasoning." SUPPRESSED THINKING: with it, zero thinking
#    tokens on the combined pass at xhigh; without it, 1379.
#  - "Stop immediately after the closing ]." is redundant under a grammar, and its
#    only real effect was suppressing a leading code fence - which the "no code
#    fence" sentence now says directly.
PROMPT = """Find the names, dates of birth, addresses, organizations and identifiers printed on \
this page. Look for them anywhere: main text, titles, headers, footers, tables.

TYPE is one of:
* NAME : a person's name, full or partial, including when used in account names;
* DOB : a person's date of birth;
* ADDRESS : a postal address, full or partial;
* COMPANY : a name of a company, an organization, a trust or a fund, or of a brand, full or \
partial, including when used in account names;
* IDENTIFIER : any other identifier - a number or a code identifying a person, \
an organization or an account, such as:
  - account number, credit card, driving licence, TFN, medicare or passport number;
  - insurance policy, reference or claim identifier;
  - membership or loyalty card number;
  - ABN, ACN or TFN number;
  - phone number, email address, web address;
  - vehicle plate number.

Use an appropriate TYPE for each value that you find, if unsure, fallback to IDENTIFIER.
When unsure whether to include something, include it: reporting too much is corrected later, \
missing something is not. This applies to banks, insurers and every other organization \
too - include their names, numbers, addresses and web addresses.
Report the value, never the label that introduces it: in "ABN 12 345 678 901" the value is \
"12 345 678 901".
A value printed across several lines may be written on one line.
Do not output monetary amounts, transaction dates, interest rates, balances, percentages, page \
numbers or statement numbers - they are NOT identifiers.
Output only the JSON array, with no code fence and no other text."""

# Pass 1 without boxes (`hybrid`, `ocr`) needs each value ONCE. Pass 2 returns a
# box for every printing and `attach_boxes` gives each its own finding, and
# `locator.locate_borrowed` finds every occurrence of a known value anyway. Asked
# for "all occurrences", Gemma 4 argued on nearly every page over whether that
# meant printings or values, answered both ways, and the repeats it did list
# lost their boxes in pass 2 (2026-09-14).
_OUTPUT_VALUES = """
List each distinct value once, however many times it is printed on the page.
Output in this JSON format:
[{"type": "<TYPE>", "text": "<the value, exactly as printed>"}]
If the page contains none, output []"""

# The one-pass box prompt (`combined`, `vlm`) is the only source of boxes, so it
# still needs every printing.
_OUTPUT_BOXES = """
List every place a value is printed: a value printed twice gets two entries.
Output in this JSON format:
[{"type": "<TYPE>", "text": "<the value, exactly as printed>", "bbox_2d": [x1, y1, x2, y2]}]
bbox_2d is the tight box around that text: (x1,y1) top-left, (x2,y2) bottom-right, in \
normalized relative coordinates scaled to 1000. Make the box enclose the whole string \
including its first and last characters.
If the page contains none, output []"""

# Pass 2 of the two-pass regime. Detection and grounding are separated because
# asking for both at once measurably costs recall — 350 -> 324 distinct values
# over 31 pages, and the page that lost its policy number lost the hardest-won
# detection on it. Splitting them recovers that in full (pass 1 below is
# byte-identical to the single-pass values prompt) and boxes MORE tightly
# (1.24x vs 1.41x ink). Evidence: reports/2026-08-08-vlm-oneshot-qwen36.md.
#
# OPERATIONAL, and it is not visible from this file: on a family with the
# prompt cache on (`ModelFamily.prompt_cache`, Qwen), pass 2 is cheap only
# because the server restores a context checkpoint taken right after the image,
# which needs the patched llama-server and -ctxcp > 0. Two edits here would
# silently forfeit that and double the prefill of every page — putting anything
# ahead of the image in _ask's message list, and re-encoding the page to
# different PNG bytes between the two calls, since the server keys the image
# chunk on a hash of the encoded bytes. Neither fails loudly; both just get
# slow. See reports/2026-08-13-qwen36-ssm-prompt-cache.md. With the cache off
# (Gemma), pass 2 prefills the page again, for reproducibility (2026-09-15).
_LOCATE_PROMPT = """This page has already been read. Below is the list of text values found \
on it. Your only job now is to say WHERE each one is printed.

Values:
{values}

For every value in the list, output one entry per place it appears on the page. A value \
printed twice gets two entries. If you cannot find a value on the page, omit it — do not \
guess a location.

Output in this JSON format:
[{{"text": "<the value, copied from the list>", "bbox_2d": [x1, y1, x2, y2]}}]
bbox_2d is the tight box around that text: (x1,y1) top-left, (x2,y2) bottom-right, in \
normalized relative coordinates scaled to 1000. Make the box enclose the whole string \
including its first and last characters.
Output only the JSON array, with no code fence and no other text."""

# The two box prompts above spell boxes x first: the measured wording, and what an
# x-first model is sent byte for byte. A y-first model gets the same prompts with
# only the coordinate NAMES swapped — the smallest edit that stops it being asked
# against its own convention. Every phrase must stay in both prompts, or the
# y-first prompt would silently go on asking for x first (pinned by a test).
_XY_PHRASES = (
    ("[x1, y1, x2, y2]", "[y1, x1, y2, x2]"),
    ("(x1,y1) top-left, (x2,y2) bottom-right", "(y1,x1) top-left, (y2,x2) bottom-right"),
)


def in_box_order(prompt: str, order: str) -> str:
    """`prompt` asking for boxes in `order` — a concrete order, never "auto"."""
    if order == "xyxy":
        return prompt
    if order != "yxyx":
        raise ValueError(f"box order must be resolved before prompting: {order!r}")
    for x_first, y_first in _XY_PHRASES:
        prompt = prompt.replace(x_first, y_first)
    return prompt

# GBNF grammars — the output SHAPE, enforced at the sampler instead of parsed
# out of whatever comes back. One per prompt, and the prompts are unchanged:
# they still describe the shape in words, which costs nothing and keeps the
# model's intent aligned with the constraint.
#
# Two things this buys beyond dropping fences and preambles. The class
# vocabulary becomes ENFORCED rather than mapped — `TYPE_MAP.get(...,
# "IDENTIFIER_GENERIC")` silently collapses a class the model invents, and the
# enum below is DERIVED from TYPE_MAP so it cannot drift from it. And an
# unparseable body stops being reachable by malformed output, which is what
# makes `Incomplete.malformed` meaningful as a signal rather than noise.
#
# It constrains FORM, not LENGTH: a grammar-guided answer truncates exactly as
# unparseably as a free one. That is `read_response`'s job, not this one.
#
# Three notes for anyone editing these:
#  - `\\` inside a character class is REJECTED by llama.cpp b10326 ("failed to
#    parse grammar"), so a literal backslash is written `\x5C`. Do not
#    "restore" json.gbnf's spelling; hex escapes work on every build that
#    accepts character classes at all.
#  - EVERY repetition is bounded except the transcribed value itself.
#    Whitespace is pinned rather than given a `ws ::= [ \t\n]*` rule, and an
#    integer is capped at five digits, because an unbounded repetition is a
#    legal place for a greedy decode to spin forever — and the one thing that
#    cannot be bounded is the one thing that must stay verbatim.
#  - the integers are deliberately NOT range-checked to 0..1000. Clamping would
#    turn a model emitting pixel coordinates from a visibly off-page box into a
#    silently plausible wrong one; a grammar should remove ambiguity, not
#    evidence.
_G_ROOT = 'root ::= "[" (item (", " item)*)? "]"'

_G_ITEM_VALUES = r'''item ::= "{\"text\": " string ", \"type\": " type "}"'''
_G_ITEM_VALUES_BOXES = (
    r'''item ::= "{\"text\": " string ", \"type\": " type '''
    r'''", \"bbox_2d\": " bbox "}"'''
)
_G_ITEM_BOXES = r'''item ::= "{\"text\": " string ", \"bbox_2d\": " bbox "}"'''

_G_BBOX = r'''bbox ::= "[" int ", " int ", " int ", " int "]"
int ::= "0" | [1-9] [0-9]? [0-9]? [0-9]? [0-9]?'''

# JSON's string production, transcribed from llama.cpp's json.gbnf with the
# backslash spelled \x5C (see above). The only unbounded repetition here.
_G_STRING = r'''string ::= "\"" char* "\""
char ::= [^"\x5C\x7F\x00-\x1F] | "\\" (["\x5Cbfnrt/] | "u" hex hex hex hex)
hex ::= [0-9a-fA-F]'''


def _type_rule() -> str:
    """The class enum, derived from TYPE_MAP so the two cannot drift.

    Double-encoded on purpose: the inner dump quotes the class name as JSON,
    the outer one wraps it as a GBNF string literal."""
    return "type ::= " + " | ".join(
        json.dumps(json.dumps(name)) for name in TYPE_MAP
    )


GRAMMAR_VALUES = "\n".join((_G_ROOT, _G_ITEM_VALUES, _type_rule(), _G_STRING))
GRAMMAR_VALUES_BOXES = "\n".join(
    (_G_ROOT, _G_ITEM_VALUES_BOXES, _type_rule(), _G_BBOX, _G_STRING)
)
GRAMMAR_LOCATE = "\n".join((_G_ROOT, _G_ITEM_BOXES, _G_BBOX, _G_STRING))


@dataclass(frozen=True)
class VlmFinding:
    """One detection. `box` is the model's own normalized-to-1000 rectangle,
    ALWAYS (x1, y1, x2, y2) whatever order the model wrote it in, and is present
    only when the model was asked for geometry."""

    text: str
    entity_type: str
    box: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class Incomplete:
    """How many model responses in one unit of work did not finish.

    COUNTED, not merely warned about, for the same reason as
    `ImageStripResult.unlocated`: Python's default warning filter shows one
    instance per code location, so the second looped page of a run is silent.

    The first two counters are ANSWERS that did not finish, and they are all
    that `total` and truthiness see — each is a hole in the redaction:

    - `truncated` — the generation ran into the token budget mid-array
      (`finish_reason == "length"`, and the array never closed). Layer 0 is the
      only detector for PERSON / ADDRESS / ORGANIZATION, so a page read this
      way carries no name, address or company redaction at all, while layer 1
      still finds the checksummed identifiers and makes the output look
      plausibly redacted. Measured at ~1 in 70 real pages
      ([reports/2026-08-12-mac-inference-speed.md](reports/2026-08-12-mac-inference-speed.md)).
    - `malformed` — the generation ENDED normally but carried no usable JSON
      array. Under a NON-lazy grammar this is unreachable, which is what made
      it worth counting separately: it was the canary that the server ignored
      the grammar field. A lazy grammar weakens that (2026-08-19): everything
      before the opening `[` is unconstrained, so a reply that never reaches an
      array is now reachable without the server having ignored anything. It
      remains the right counter — an answer that is not an array is not an
      empty page — but it no longer proves what it used to.

    The third is a TRACE that did not finish, and is deliberately outside
    `total`:

    - `reasoning_budget_hit` — the thinking ran into `reasoning_budget_tokens`
      and was closed by `REASONING_CUTOFF`. The answer after it is whole, so the
      page is not a hole. What it measures is the budget: the model stopped
      thinking before it was done, and a larger budget might have found more.
      Folded into `total`, every Gemma page that thinks long would read as an
      unfinished page and be reported as missing names.
    """

    truncated: int = 0
    malformed: int = 0
    reasoning_budget_hit: int = 0

    @property
    def total(self) -> int:
        return self.truncated + self.malformed

    def __bool__(self) -> bool:
        return bool(self.total)

    def __add__(self, other: "Incomplete") -> "Incomplete":
        return Incomplete(
            self.truncated + other.truncated,
            self.malformed + other.malformed,
            self.reasoning_budget_hit + other.reasoning_budget_hit,
        )

    def __radd__(self, other):
        # So sum() over pages/windows works without a start= value.
        return self if other == 0 else self.__add__(other)


@dataclass(frozen=True)
class DetectorResult:
    """What one detection pass produced, and what went wrong producing it.

    `detect` returns this rather than a bare list because an empty list is
    THREE situations — a genuinely clean page, an answer that was cut off, and
    an answer that was never JSON — and only the first may be redacted against.
    The `vlm` contract used to say so in prose ("the caller is expected to treat
    that as a failure rather than an empty page") while giving the caller
    nothing to tell them apart with.
    """

    findings: list[VlmFinding]
    incomplete: Incomplete = Incomplete()
    # What the model thought on the way to `findings`, one entry per reply that
    # thought at all. Kept for the debug output (`--debug` writes it beside the
    # findings listing); nothing in detection reads it.
    reasoning: tuple[ReasoningTrace, ...] = ()


@dataclass(frozen=True)
class ReasoningTrace:
    """One reply's thinking, verbatim.

    Near-PII like the findings listing: a trace over a page quotes the page.
    `stage` is the pass that asked, in the flags' own words — "detection"
    (`--reasoning-effort`) or "grounding" (`--grounding-reasoning-effort`)."""

    stage: str
    text: str
    budget_hit: bool = False


class Transport(Protocol):
    def __call__(self, url: str, payload: dict, timeout: int) -> dict: ...


def http_transport(url: str, payload: dict, timeout: int) -> dict:
    """Default transport: stdlib only, so `pii.core` gains no dependency.

    Connection failures are translated into `VlmUnavailable` with the URL and a
    hint. The model server is usually on another machine, so "wrong --vlm-url"
    is the single most likely failure and a raw URLError traceback is a poor way
    to say it.

    A connection-level failure is RETRIED; an HTTP status is not. The split is
    the whole point: a status code means the server read the request and
    answered it, so retrying would hide a bad request behind a delay, while a
    reset pipe means the answer never arrived and asking again is the only way
    to learn anything. Retrying is safe here because the request is idempotent —
    greedy, `seed` pinned, and the prompt cache off — so a second ask returns the
    same answer and a retry can only recover a result, never change one. For a
    family with `ModelFamily.prompt_cache` on that does not hold: a re-send hits
    the prompt cached by the first attempt and re-evaluates differently, so a
    retry can change the answer (it still cannot lose one).

    Why it exists (2026-08-19): a 56-minute corpus run died on a single TCP
    reset while the server sat healthy and had already generated the reply.
    Without a retry, one blip destroys an arbitrarily long job that has already
    paid for every page before it — and a long production document has exactly
    the same exposure, at higher stakes."""
    data = json.dumps(payload).encode()
    for attempt in range(1, TRANSPORT_ATTEMPTS + 1):
        req = urllib.request.Request(
            f"{url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # Note this precedes the URLError clause deliberately: HTTPError is
            # a subclass of it, and catching it second would make every 4xx/5xx
            # retryable.
            detail = exc.read()[:400].decode(errors="replace")
            raise VlmUnavailable(
                f"model server at {url} returned HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if attempt == TRANSPORT_ATTEMPTS:
                raise VlmUnavailable(
                    f"cannot reach the model server at {url} ({reason}) after "
                    f"{TRANSPORT_ATTEMPTS} attempts. Start llama-server (with "
                    f"a VISION model for --image/--pdf), or pass --vlm-url if "
                    f"it runs on another host "
                    f"(e.g. --vlm-url http://192.168.1.55:8080)."
                ) from exc
            # Printed rather than warned. `warnings` shows one instance per
            # code location, so a link that resets on every tenth page would
            # announce itself once and then go quiet — the same trap that made
            # `Incomplete` a counter instead of a warning. A retry is an
            # operational fact and each one is worth seeing.
            print(
                f"pii: model server at {url} dropped the connection "
                f"({reason}); retrying {attempt}/{TRANSPORT_ATTEMPTS - 1}",
                file=sys.stderr,
            )
            time.sleep(TRANSPORT_BACKOFF * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def served_model_name(url: str, timeout: int) -> str | None:
    """The model llama-server reports serving (`GET /v1/models`), or None.

    Asked once per detector, and only when a request that depends on the model
    family (thinking on, or boxes under "auto") must be built before any reply
    has named the model — with thinking on, that is the very first request. A
    failure is `VlmUnavailable` with the same hint as a failed request, since
    it is the same wrong --vlm-url."""
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise VlmUnavailable(
            f"cannot ask the model server at {url} what it is serving ({reason}). "
            f"Start llama-server, or pass --vlm-url if it runs on another host "
            f"(e.g. --vlm-url http://192.168.1.55:8080)."
        ) from exc
    for key, field in (("data", "id"), ("models", "name")):
        entries = body.get(key) if isinstance(body, dict) else None
        if entries and isinstance(entries[0], dict) and entries[0].get(field):
            return str(entries[0][field])
    return None


def fold_digits(text: str) -> str:
    """Fold non-ASCII decimal digits to ASCII.

    A VLM once decoded U+06F5 (Extended Arabic-Indic five) for an ASCII '5' on a
    CLEAN render - visually identical, but it breaks value matching and checksums
    by string identity while looking correct. Classic OCR engines cannot produce
    this class of error; generative ones can.
    """
    return "".join(
        str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch
        for ch in text
    )


def strip_thinking(raw: str) -> str:
    """Drop the model's reasoning trace, whether it is closed or forced open.

    A reasoning trace over a page of numbers contains '[', so failing to strip
    one does not merely leave noise: the JSON scanner below latches onto the
    trace's brackets and `parse_findings` returns the wrong list — or, measured
    2026-08-19, an EMPTY one, which is indistinguishable from a clean page.

    Two shapes, and the second is the one that bites:

    - a model that emits its own opening tag produces a matched `<think>` pair;
    - a model whose chat template opens the block FOR it emits only the CLOSING
      tag (Qwen3.8's generation prompt ends with an open `<think>`), so there is
      no pair to match and everything ahead of `</think>` is reasoning.

    Production does not normally see either, because llama.cpp's default
    `reasoning_format: deepseek` splits the trace into `message.reasoning_content`
    and leaves `content` clean. This function is what stands behind that for
    `parse_findings`, whose contract is to take a body from a caller that did
    not fetch it — where neither shape can be ruled out.
    """
    for opening, closing in _TRACE_TAGS:
        raw = re.sub(re.escape(opening) + r".*?" + re.escape(closing), "", raw, flags=re.S)
        # Forced open: an unmatched close means everything ahead of it is
        # reasoning. The FIRST close is the real one, which is also how
        # llama.cpp's own parser splits reasoning from content.
        _, closed, tail = raw.partition(closing)
        if closed:
            raw = tail
    return raw.strip()


# Every family's trace delimiters (see `strip_thinking`): Qwen's, and Gemma's
# thought channel. Stripped whichever model a body came from, since a body-only
# caller cannot say.
_TRACE_TAGS = (("<think>", "</think>"), ("<|channel>thought", "<channel|>"))


def family_for_model(name: str | None) -> ModelFamily | None:
    """The `ModelFamily` a served model's name implies.

    Matched on the file name alone, case-insensitively — llama-server reports
    the model PATH by default, and a directory called `qwen-vs-gemma` must not
    decide it. None when no known family matches, and also when more than one
    does: a name that says both is not evidence for either."""
    if not name:
        return None
    base = _model_file(name).lower()
    matches = [family for family in FAMILIES if family.name in base]
    return matches[0] if len(matches) == 1 else None


def _model_file(name: str) -> str:
    """The file-name part of the model name llama-server reports (a path)."""
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def read_response(
    response: dict, box_order: str = "xyxy", stage: str = "detection"
) -> DetectorResult:
    """One server reply -> findings, the failure counters, and the trace.

    Shared by both layer-0 detectors, so the three-way split (clean page / cut
    off / not JSON) is decided in exactly one place, and so is whether the
    thinking ran out of budget (`_reasoning_of`). `stage` only labels the trace.

    `finish_reason` is the whole point of this function. llama-server reports
    `"length"` when the generation was truncated, and reading it is what
    separates an empty page from an answer that never finished — without it a
    repetition loop, where the model emits the same entry until the budget runs
    out, is indistinguishable from a clean page. An array that DID close is
    complete regardless of why generation stopped: everything meaningful
    arrived, and whatever was cut was trailing.

    `box_order` must be a concrete order; resolving "auto" needs the detector,
    which is what knows whether the override was given.
    """
    try:
        choice = response["choices"][0]
        message = choice["message"]
        raw = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VlmError(f"unexpected response shape: {response!r}") from exc
    payload, complete = _extract_array(strip_thinking(fold_digits(raw)))
    findings = _findings_from(payload, box_order)
    trace = _reasoning_of(message, stage)
    hit = int(trace is not None and trace.budget_hit)
    reasoning = (trace,) if trace is not None else ()
    if complete:
        return DetectorResult(findings, Incomplete(reasoning_budget_hit=hit), reasoning)
    # The completed elements are kept either way (see _extract_array); which
    # counter this lands in is decided by WHY the array is still open — a
    # budget the generation ran into, or a body that was never JSON at all.
    if choice.get("finish_reason") == "length":
        return DetectorResult(
            findings, Incomplete(truncated=1, reasoning_budget_hit=hit), reasoning
        )
    return DetectorResult(
        findings, Incomplete(malformed=1, reasoning_budget_hit=hit), reasoning
    )


def _reasoning_of(message: dict, stage: str) -> ReasoningTrace | None:
    """The reply's thinking, or None if it did not think.

    llama-server puts the trace in `reasoning_content` by default. A server
    that leaves it inline in `content` is read too, as far as the first closing
    tag, which is where `strip_thinking` ends it.

    A trace that ran out of budget is recognised by `REASONING_CUTOFF`, which
    the server writes into the trace at the point it closes it. Matched without
    its surrounding newlines, which a server may trim."""
    text = message.get("reasoning_content") or ""
    if not text.strip():
        content = message.get("content") or ""
        for opening, closing in _TRACE_TAGS:
            head, closed, _ = content.partition(closing)
            if closed:
                text = head.split(opening, 1)[-1]
                break
    text = text.strip()
    if not text:
        return None
    return ReasoningTrace(stage, text, REASONING_CUTOFF.strip() in text)


def parse_findings(raw: str, box_order: str = "xyxy") -> list[VlmFinding]:
    """Parse a response BODY into findings, ignoring completeness.

    The body-level seam: it shares every defence with `read_response` (fence,
    `<think>` block, folded digits, salvage) but has no envelope to read
    `finish_reason` from, so it cannot tell an empty page from an answer that
    never finished. Detectors therefore go through `read_response`; this stays
    as the entry point for anything holding only the text — the testbench, and
    a caller parsing a body it did not fetch itself. With no envelope there is
    no model name either, so the box order is the caller's to state."""
    payload, _ = _extract_array(strip_thinking(fold_digits(raw)))
    return _findings_from(payload, box_order)


def _findings_from(payload, box_order: str = "xyxy") -> list[VlmFinding]:
    """Turn parsed JSON items into findings, skipping what makes no sense.

    Boxes leave here as (x1, y1, x2, y2) whatever `box_order` they arrived in,
    so nothing downstream — the locator, the overlays, the grounding scorer —
    ever learns that models disagree."""
    if box_order not in ("xyxy", "yxyx"):
        # "auto" reaching here would silently parse as xyxy — the one outcome
        # the setting exists to prevent.
        raise ValueError(f"box order must be resolved before parsing: {box_order!r}")
    if payload is None:
        return []
    out = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        entity = TYPE_MAP.get(str(item.get("type", "")), "IDENTIFIER_GENERIC")
        raw_box = item.get("bbox_2d")
        box = None
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            try:
                a, b, c, d = (int(v) for v in raw_box)
            except (TypeError, ValueError):
                box = None
            else:
                x1, y1, x2, y2 = (b, a, d, c) if box_order == "yxyx" else (a, b, c, d)
                box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        out.append(VlmFinding(text=text, entity_type=entity, box=box))
    return out


def _extract_array(body: str) -> tuple[list | None, bool]:
    """Find the first top-level JSON array, tolerating a code fence.

    Returns `(payload, complete)`. An array that never closes is SALVAGED: the
    elements that did complete come back with `complete=False`. A truncated
    answer is mostly a good answer — a dense page that hit the token budget
    after 250 findings used to contribute none of them — and the caller learns
    from `complete` that it must not treat what it got as the whole page.
    """
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1)
    start = body.find("[")
    if start == -1:
        return None, False
    depth = 0  # bracket nesting; braces are tracked apart, see `cut`
    braces = 0
    in_str = False
    esc = False
    cut = None  # the last comma SEPARATING two top-level elements
    for i, ch in enumerate(body[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            braces += 1
        elif ch == "}":
            braces -= 1
        elif ch == "," and depth == 1 and braces == 0:
            # Braces matter here and nowhere else: the commas inside an entry
            # sit at bracket depth 1 too, and cutting at one would truncate an
            # object rather than the array.
            cut = i
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start : i + 1]), True
                except json.JSONDecodeError:
                    return None, False
    return _salvage(body, start, cut), False


def _salvage(body: str, start: int, cut: int | None) -> list | None:
    """The completed elements of an array that never closed, deduplicated.

    Identical entries are collapsed HERE and nowhere else. An unterminated
    array is the signature of a repetition loop, so its occurrence counts
    cannot be trusted — and they no longer need to be: `locator.locate_borrowed`
    finds every occurrence of a known value mechanically. Without the collapse,
    one looped value that is not on the page arrives as hundreds of separate
    "unredacted detection" warnings and buries the report it should be raising.
    Entries that differ in ANY field, a box included, are separate occurrences
    and survive.
    """
    if cut is None:
        return None
    try:
        payload = json.loads(body[start:cut] + "]")
    except json.JSONDecodeError:
        return None
    return list(
        {json.dumps(item, sort_keys=True): item for item in payload}.values()
    )


def _check_budget(tokens: int) -> None:
    """Refuse a reasoning budget below 1 token. llama.cpp reads -1 as unlimited,
    which would take away the room `_max_tokens` keeps for the answer on top of
    the budget — the property that lets the budget bite first."""
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 1:
        raise ValueError(f"reasoning budget must be at least 1 token: {tokens!r}")


class _ServedModel:
    """What both layer-0 detectors need to know about the model behind the URL.

    Two things about a request depend on the model FAMILY (`ModelFamily`): the
    order boxes are asked for in, and how thinking is switched on and where its
    trace ends. Both must be known BEFORE the request goes out, so the family
    is resolved on first need — learned from an earlier reply's `model` field
    when there was one, else asked for once (`served_model`, injectable) — and
    an unplaceable model is refused there, before a request is spent on it.
    Requests that depend on neither (thinking off, no boxes) run against any
    model. Every reply is checked against the family its request was built for.

    A mixin rather than borrowed methods, because the state is shared too: the
    text detector sends the same thinking protocol to the same server."""

    url: str
    timeout: int
    reasoning_budget: int
    box_order: str = "auto"
    # The answer's own allowance; see `_max_tokens`.
    _answer_tokens: int = ANSWER_TOKENS

    def _init_served_model(self, served_model: Callable[[str, int], str | None] | None) -> None:
        self._served_model = served_model or served_model_name
        # The (served model name, its family) last seen. `_unplaceable` keeps a
        # name no family matched, so a refusal can name it without asking the
        # server again.
        self._model: tuple[str, ModelFamily] | None = None
        self._unplaceable: str | None = None

    def _family(self, needed_for: str) -> ModelFamily:
        if self._model is None:
            name = self._unplaceable or self._served_model(self.url, self.timeout)
            family = family_for_model(name)
            if family is None:
                raise ModelFamilyUnknown(
                    f"cannot tell which model family layer-0 model {name!r} is, "
                    f"and this run needs it {needed_for}. Known families: "
                    + ", ".join(f.name for f in FAMILIES)
                )
            self._learn(name, family)
        return self._model[1]

    def _learn(self, name: str, family: ModelFamily) -> None:
        # Printed, not warned, and again whenever the model changes: which family
        # a run spoke to is an operational fact worth seeing, and `warnings`
        # would show it once per code location.
        if self._model == (name, family):
            return
        self._model = (name, family)
        self._unplaceable = None
        print(
            f"pii: layer-0 model {_model_file(name)} -> {family.name} family",
            file=sys.stderr,
        )

    def _check_reply(self, response, built_for: ModelFamily | None) -> None:
        """Learn from the model a reply names, and refuse one that is not the
        family its request was built for — a server that changed model between
        building the request and answering it."""
        name = response.get("model") if isinstance(response, dict) else None
        if not name:
            return
        family = family_for_model(name)
        if built_for is not None and family is not built_for:
            self._model = None
            raise ModelFamilyUnknown(
                f"the request was built for the {built_for.name} family, but the "
                f"reply came from {name!r}"
                + (f" ({family.name})" if family else "")
                + ": did the server change model mid-run? Re-run"
            )
        if family is None:
            self._unplaceable = name
        else:
            self._learn(name, family)

    def _thinking_family(self, effort: str) -> ModelFamily | None:
        """The family a request at `effort` is built for, or None if thinking is
        off and so the request is the same for every family."""
        if effort == "off":
            return None
        family = self._family("to switch its thinking on (or pass --reasoning-effort off)")
        if not family.supports(effort):
            name = _model_file(self._model[0])
            raise ReasoningEffortUnsupported(
                f"layer-0 model {name} is {family.name}, whose chat template has "
                f"no reasoning effort levels: thinking is on (medium) or off. "
                f"{effort!r} would be ignored silently, so it is refused"
            )
        return family

    def _cache_fields(self) -> dict:
        """`cache_prompt` as the served model's family sets it, and off while the
        family is not known yet: a request that needs no family (thinking off, box
        order given) must still run reproducibly against any model. Not resolved
        here, so it never costs a request or refuses an unplaceable model."""
        family = self._model[1] if self._model else None
        return {"cache_prompt": bool(family and family.prompt_cache)}

    def _max_tokens(self, effort: str) -> int:
        """Answer allowance PLUS thinking allowance — they share one budget.

        Sized this way because of which limit bites first. Reaching
        `max_tokens` truncates the array mid-entry: a redaction failure that
        `Incomplete.truncated` can report but not undo, and on a page whose
        names and addresses are layer 0's alone to find. Reaching the reasoning
        budget instead closes the trace cleanly and still yields a whole answer.
        So the reasoning budget must be able to bite FIRST, which it can only do
        if `max_tokens` leaves the answer its own room on top."""
        return self._answer_tokens + (0 if effort == "off" else self.reasoning_budget)

    def _reasoning_fields(self, effort: str) -> dict:
        """Thinking on (the family's switch + budget + cut-off), or off.

        All per-request, for the reason `grammar` is: a server flag would apply
        to every caller of that server and could not be versioned with the code
        that reads the reply. Needs `llama-server --jinja` for
        `chat_template_kwargs` to reach the template at all."""
        family = self._thinking_family(effort)
        if family is None:
            # Every template then writes a PRE-CLOSED trace into the prompt, so
            # the budget sampler sees start-and-end among the prefill tokens and
            # the grammar applies from the first generated token. That is
            # exactly the pre-thinking behaviour, which is what makes "off" a
            # usable baseline rather than a third thing.
            return {"chat_template_kwargs": QWEN.thinking_kwargs("off")}
        return {
            "chat_template_kwargs": family.thinking_kwargs(effort),
            "reasoning_budget_tokens": self.reasoning_budget,
            "reasoning_budget_message": REASONING_CUTOFF,
        }

    def _lazy_fields(self, effort: str) -> dict:
        """Make the grammar engage only after the thinking trace, at the
        family's own end-of-trace marker (`ModelFamily.trigger`).

        llama.cpp does the hard part: with a lazy grammar AND a reasoning-budget
        sampler, `grammar_should_apply()` is false for the whole thinking block,
        so the GBNF cannot constrain the trace by construction rather than by a
        trigger that happens to avoid it. The trigger then engages it at the
        array. A trigger for the WRONG family never fires, and the answer goes
        out unconstrained — which is why the family is resolved, never assumed.

        **Requires a llama-server carrying the grammar_lazy passthrough fix.**
        Upstream's OAI layer overwrites `grammar_lazy` and `grammar_triggers`
        from the chat template unconditionally, and its copy-remaining loop only
        fills absent keys — so on a stock server these two are silently dropped,
        the grammar applies from token 0, and the model does not think at all.
        The failure is quiet: replies still parse, they are just unreasoned. See
        reports/2026-08-19-qwen38-bringup.md."""
        family = self._thinking_family(effort)
        if family is None:
            return {}
        return {
            "grammar_lazy": True,
            "grammar_triggers": [{"type": _TRIGGER_PATTERN, "value": family.trigger}],
        }


class VlmDetector(_ServedModel):
    """Detects PII directly from a page image.

    `want_boxes` makes the ONE-pass boxes prompt (`geometry="vlm"`). It stays
    off everywhere else because asking for coordinates alongside detection
    measurably costs recall — 350 -> 324 distinct values over 31 pages. The
    production route to geometry is `localize`, a second pass, which pays no
    such price.

    `grammar` constrains the output shape at the sampler. It is on by default
    and exists as a switch because constrained decoding alters the sampled
    distribution, so it is an A/B axis rather than a serialization detail.

    `reasoning_effort` is the detection pass's — `detect`, the only pass outside
    `hybrid` — and `grounding_reasoning_effort` is `localize`'s, "off" by
    default (see `DEFAULT_GROUNDING_EFFORT`), or "same" to copy the first.

    `box_order` is the order boxes are ASKED for in, and so read back in (see
    `BOX_ORDERS`). Under "auto" it is the served model family's, resolved as
    `_ServedModel` describes; an explicit order overrides only that.
    """

    # Which layer-0 modality this detector IS, for the run to describe itself
    # with (the front-end banner, the debug findings listing). A plain string
    # rather than a type check, so the vision/text switches planned in
    # core/TODO.md can extend the vocabulary without touching its readers.
    layer0 = "vision"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        transport: Transport | None = None,
        timeout: int = 1800,
        want_boxes: bool = False,
        encode_image: Callable[[object], str] | None = None,
        grammar: bool = True,
        reasoning_effort: str = DEFAULT_EFFORT,
        grounding_reasoning_effort: str = DEFAULT_GROUNDING_EFFORT,
        reasoning_budget: int = DEFAULT_REASONING_BUDGET,
        box_order: str = DEFAULT_BOX_ORDER,
        served_model: Callable[[str, int], str | None] | None = None,
    ) -> None:
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"unknown reasoning effort: {reasoning_effort!r}")
        if grounding_reasoning_effort not in GROUNDING_REASONING_EFFORTS:
            raise ValueError(
                f"unknown grounding reasoning effort: {grounding_reasoning_effort!r}"
            )
        if box_order not in BOX_ORDERS:
            raise ValueError(f"unknown box order: {box_order!r}")
        _check_budget(reasoning_budget)
        self.url = url
        self.transport = transport or http_transport
        self.timeout = timeout
        self.want_boxes = want_boxes
        self._encode = encode_image or _encode_png
        self.grammar = grammar
        self.reasoning_effort = reasoning_effort
        self.grounding_reasoning_effort = grounding_reasoning_effort
        self.reasoning_budget = reasoning_budget
        self.box_order = box_order
        self._init_served_model(served_model)

    @property
    def grounding_effort(self) -> str:
        """`grounding_reasoning_effort` with "same" resolved."""
        if self.grounding_reasoning_effort == "same":
            return self.reasoning_effort
        return self.grounding_reasoning_effort

    @property
    def prompt(self) -> str:
        """The pass-1 prompt. With boxes, this resolves the box order, which
        under "auto" may ask the server what it is serving."""
        if not self.want_boxes:
            return PROMPT + _OUTPUT_VALUES
        return PROMPT + in_box_order(_OUTPUT_BOXES, self._request_order())

    @property
    def _detect_grammar(self) -> str | None:
        if not self.grammar:
            return None
        return GRAMMAR_VALUES_BOXES if self.want_boxes else GRAMMAR_VALUES

    def detect(self, image) -> DetectorResult:
        effort = self.reasoning_effort
        order = self._request_order() if self.want_boxes else None
        prompt = PROMPT + (
            in_box_order(_OUTPUT_BOXES, order) if order else _OUTPUT_VALUES
        )
        response = self._ask(image, prompt, self._detect_grammar, effort)
        return self._read(
            response, order, self._built_for(effort, boxed=self.want_boxes), "detection"
        )

    def _request_order(self) -> str:
        """The order to ASK for boxes in, resolved before the request is sent."""
        if self.box_order != "auto":
            return self.box_order
        return self._family(
            "to ask for its boxes in its own order (or pass --box-order xyxy "
            "for x first, as Qwen, or yxyx for y first, as Gemma)"
        ).box_order

    def _built_for(self, effort: str, *, boxed: bool) -> ModelFamily | None:
        """The family a request was built for, if anything in it depended on
        one: thinking on, or a box order chosen by "auto"."""
        if effort != "off" or (boxed and self.box_order == "auto"):
            return self._model[1] if self._model else None
        return None

    def _read(
        self,
        response: dict,
        asked: str | None = None,
        built_for: ModelFamily | None = None,
        stage: str = "detection",
    ) -> DetectorResult:
        """`read_response` in the order the boxes were `asked` for, after
        checking the reply came from the family the request was built for."""
        self._check_reply(response, built_for)
        # A boxless prompt's reply carries no box anything will use, so which
        # order it is parsed in is moot.
        return read_response(response, asked or "xyxy", stage)

    def localize(self, image, findings: list[VlmFinding]) -> DetectorResult:
        """Pass 2: hand the already-detected values back and ask only where
        they are, returning the findings with `box` filled in where the model
        placed them. Thinks at `grounding_effort`, "off" by default.

        The model's answer is treated as a POOL of hints rather than a
        one-to-one reply: it routinely returns a different number of boxes
        than there were findings (a value printed twice, a value it declines
        to place), and pairing by position would then silently attach one
        value's box to another. Matching is by squashed text, assigned in
        order; a finding that draws no hint simply keeps `box=None` and falls
        back to unconstrained search, and a surplus box for a value becomes a
        finding of its own (`attach_boxes`).

        A truncated pass 2 is a milder failure than a truncated pass 1 — the
        values are already known and simply lose their search constraint, which
        is the `--geometry ocr` baseline — but it is still counted, because
        nothing downstream can tell a box the model declined to give from one
        it never got to."""
        if not findings:
            return DetectorResult(list(findings))
        listing = "\n".join(
            f"- {value}" for value in dict.fromkeys(f.text for f in findings)
        )
        effort = self.grounding_effort
        order = self._request_order()
        response = self._ask(
            image,
            # Re-spelled BEFORE the values go in, so a value that happens to
            # contain a coordinate phrase is never rewritten.
            in_box_order(_LOCATE_PROMPT, order).format(values=listing),
            GRAMMAR_LOCATE if self.grammar else None,
            effort,
        )
        hints = self._read(response, order, self._built_for(effort, boxed=True), "grounding")
        return replace(
            hints, findings=attach_boxes(findings, hints.findings)
        )

    def _ask(self, image, prompt: str, grammar: str | None, effort: str) -> dict:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{self._encode(image)}"
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            # Greedy and pinned. Determinism is a gate requirement: single-slot
            # serving (-np 1) and the prompt cache off (`ModelFamily.prompt_cache`)
            # make greedy decode reproducible; parallel batching does not, and a
            # gate you can pass by re-rolling is not a gate.
            "temperature": 0.0,
            "top_k": 1,
            "top_p": 1.0,
            "seed": 42,
            "max_tokens": self._max_tokens(effort),
            "stream": False,
        }
        payload.update(self._reasoning_fields(effort))
        # After the reasoning fields, which resolve the family when thinking is on.
        payload.update(self._cache_fields())
        if grammar:
            # Per-request rather than a server flag, so the shape we enforce is
            # versioned with the code that parses it — the same reasoning that
            # keeps the sampling parameters above out of a launch script.
            payload["grammar"] = grammar
            payload.update(self._lazy_fields(effort))
        return self.transport(self.url, payload, self.timeout)


class NullDetector:
    """Layer 0 turned OFF by request (`--layer0 off`): detects nothing.

    A strip entry point still REQUIRES a detector and always will — a
    patterns-only run must not be reachable by forgetting an argument
    (2026-07-15) — so skipping layer 0 is done by passing a detector that
    answers nothing, not by making the argument optional. Every mode then runs
    unchanged: `merge_detections` folds an empty layer-0 set and degenerates to
    layer 1 alone, and the image path still OCRs, linearizes and paints.

    **This is a knowingly reduced redaction, not a free speedup.** Layer 1 owns
    no PERSON of its own (its joint-name rule derives from people another
    layer detected) and no ADDRESS,
    ORGANIZATION or DATE_OF_BIRTH at all, so a run under this detector redacts
    identifiers and leaves names and addresses on the page. The front-end says
    so on every run, and the debug findings listing records the regime, because
    zero findings must never be mistakable for a clean document — the same
    reasoning that made `DetectorResult` carry `incomplete`.

    `incomplete` is always empty, and that is a claim rather than a default:
    nothing was asked, so nothing was cut off. A page whose answer was LOST is
    a different fact and must not report the same way.
    """

    layer0 = "off"

    def detect(self, subject) -> DetectorResult:
        return DetectorResult([])

    def localize(self, image, findings: list[VlmFinding]) -> DetectorResult:
        # Reached under the default hybrid geometry, where `read_page` calls
        # pass 2 unconditionally. There is nothing to place and no request is
        # made, which is why this detector needs no server at all.
        return DetectorResult(list(findings))


def attach_boxes(
    findings: list[VlmFinding], hints: list[VlmFinding]
) -> list[VlmFinding]:
    """Pair pass-2 boxes onto pass-1 findings by squashed text.

    Squashed rather than exact because the two passes need not agree on
    separators — pass 2 re-transcribes the value as it copies it back — and
    the box is a positional hint, so a hint attached on slightly loose text
    equality costs nothing: `locator` re-derives the real span itself and
    treats the box only as a search constraint.

    Findings and hints are both consumed in order, so N occurrences of one
    value draw the model's N boxes for it in page order. Surplus findings
    keep `box=None`.

    **A surplus box for a value pass 1 named becomes a finding of its own**,
    placed right after that value's last finding. Pass 1 lists each value once
    and pass 2 boxes every printing, so dropping the extra boxes would leave
    each repeat to the box-free document-wide search, which cannot reach a
    printing with no OCR text (a second logo) or one damaged past exact
    matching. A box for a value pass 1 never named is still discarded: pass 2
    places values, it does not detect them."""
    pool: dict[str, list[tuple[int, int, int, int]]] = {}
    for hint in hints:
        if hint.box is None:
            continue
        key, _ = squash_map(hint.text)
        pool.setdefault(key, []).append(hint.box)

    keys = [squash_map(finding.text)[0] for finding in findings]
    last = {key: i for i, key in enumerate(keys)}
    out = []
    for i, (finding, key) in enumerate(zip(findings, keys)):
        boxes = pool.get(key)
        box = boxes.pop(0) if boxes else None
        out.append(replace(finding, box=box) if box is not None else finding)
        if last[key] == i and boxes:
            out.extend(replace(finding, box=extra) for extra in boxes)
            boxes.clear()
    return out


class VlmError(RuntimeError):
    """The model server returned something unusable."""


class VlmUnavailable(VlmError):
    """The model server could not be reached at all."""


class ModelFamilyUnknown(VlmError):
    """A request needs the served model's family (`ModelFamily`) and none can be
    had: its name matches no known family, or the model answering is not the
    family the request was built for.

    A VlmError so every front-end already reports it as a message rather than
    a traceback: like a missing server, it is the operator's to fix."""


class ReasoningEffortUnsupported(VlmError):
    """An effort level the served model's chat template does not read — Gemma's
    has none. Refused rather than sent, since the template would ignore it and
    two runs differing only in it would look like a comparison."""


def squash_map(text: str) -> tuple[str, list[int]]:
    """Alphanumeric squash plus an index from each squashed char back to its
    offset in the original, so a match found in squashed space can be reported
    as real offsets."""
    chars, index = [], []
    for i, ch in enumerate(fold_digits(text)):
        if ch.isalnum():
            chars.append(ch.lower())
            index.append(i)
    return "".join(chars), index


def _encode_png(image) -> str:
    import base64
    import io

    buf = io.BytesIO()
    image.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()
