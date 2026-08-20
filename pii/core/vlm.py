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
TYPE_MAP = {
    "PII_NAME": "PERSON",
    "PII_ADDRESS": "ADDRESS",
    "PII_COMPANY": "ORGANIZATION",
    "PII_DOB": "DATE_OF_BIRTH",
    "PII_IDENTIFIER": "IDENTIFIER_GENERIC",
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

# Reasoning. Layer 0 is a thinking model as of 2026-08-19, and thinking is ON:
# the levels come from the chat template, which validates them and raises on
# anything else. `medium` is NOT a midpoint - the template sets an instruction
# for `xhigh` and `low` only, so `medium` injects nothing and is the model's
# unmodified behaviour. "off" is a COMPARISON INSTRUMENT, never a production
# value, for the same reason `geometry="vlm"` is one: it is kept so the
# measurement that chose the default stays runnable.
REASONING_EFFORTS = ("low", "medium", "xhigh", "off")
DEFAULT_EFFORT = "medium"

# A cap on the thinking block, not a shaping knob. At 4096 nothing in the
# 2026-08-19 sweep was cut (the longest trace observed was ~2077 tokens), which
# is the point: it exists to bound a runaway repetition loop, which greedy
# decode on a reasoning model makes a live risk, and greedy is not negotiable
# because the gate needs determinism.
DEFAULT_REASONING_BUDGET = 4096

# The answer's own allowance. This was the whole of `max_tokens` before thinking
# existed, and what sized it - a dense page of findings - has not changed.
ANSWER_TOKENS = 4096

# Injected immediately before the forced end-of-thinking tag when the budget
# runs out, so a cut-off trace lands on the answer rather than stopping
# mid-sentence. It biases toward completeness because this tool's asymmetry
# does: over-strip is recoverable, under-strip is a breach.
REASONING_CUTOFF = "\n\nEnough thinking. I will now output every identifier found.\n"

# Engage the grammar at the array and NOT before it. llama.cpp replays into the
# grammar everything from the first non-empty CAPTURE GROUP onward, falling back
# to the whole match when the pattern has none - so a bare "</think>" trigger
# would replay `</think>` into a grammar whose root starts with `[` and reject
# every continuation. Capturing the bracket is what makes the handoff exact.
GRAMMAR_TRIGGER = r"</think>[\s\S]*?(\[)"

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

# The tuned probe prompt, plus the value-not-label sentence added 2026-08-12.
# Four properties are load-bearing and should not be edited casually - each was
# established by measurement:
#  - coarse classes: collapsing 14 -> 5 cost no recall and GAINED generalization
#    (a vehicle registration was caught with no mention of vehicles);
#  - no institutional carve-outs: over-strip is recoverable by the keep-list,
#    under-strip is a breach, and a prompt is the wrong place for a silent,
#    per-page, unauditable keep decision;
#  - identifiers-live-in-headings + naming "policy, reference and claim numbers":
#    a policy number rendered as a bold heading was missed until BOTH were
#    present. The structural hint alone was not enough;
#  - value-not-label: the sentence states an invariant the tool already holds
#    ("a label is evidence, not part of the value") and that layer 0 was
#    breaking on every page, keying the map on "Account number 6874-72521".
#    Its SCOPE is what matters, not its phrasing. Three phrasings that named
#    only the label produced byte-identical findings; widening it by one phrase
#    ("never the label, heading or caption") left two pages untouched and made
#    the model grab whole transaction-narrative rows on the third
#    ("FROM THE TRUSTEE FOR TO ANZ ACCT LN"). Keep it narrow, and do not try to
#    aim its large precision side-effect by rewording - per-value keep
#    decisions belong in entity_keep.txt where they are auditable.
#    Measurements in DONE.md.
#
# Two sentences were REMOVED 2026-08-19, when layer 0 became a reasoning model.
# Both dated from the non-thinking regime and both turned out to control
# something other than what they said:
#  - "Do not explain your reasoning." SUPPRESSED THINKING. With it present the
#    model emitted ZERO thinking tokens on the combined pass at xhigh; removing
#    it alone restored 1379. It was silently defeating the thing it was being
#    asked to do.
#  - "Stop immediately after the closing ]." is redundant under a grammar - the
#    root reaches an accepting state after `]`, so only EOG stays legal and the
#    model cannot continue. Its ONLY real effect was suppressing a LEADING code
#    fence, which it never mentions. The replacement says that directly.
#    Removing it without a replacement brings the fence back.
# Together the replacement scored 15 findings where the old prompt scored 14 and
# deleting the prohibition alone scored 13 (one page, counts only).
# Measurements: reports/2026-08-19-qwen38-bringup.md.
PROMPT = """Find all occurrences of Personally Identifiable Information (PII) identifiers in \
this page. Look for them anywhere: main text, titles, headers, footers, tables.

Identifier TYPE is one of:
* PII_NAME : a person's name, full or partial, including when used in account names;
* PII_DOB : a person's date of birth;
* PII_ADDRESS : a postal address, full or partial;
* PII_COMPANY : a name of a company or an organization, full or partial, including when \
used in account names;
* PII_IDENTIFIER : any other PII identifier - a number or a code identifying a person, \
an organization or an account, such as:
  - account number, credit card, driving licence, TFN, medicare or passport number;
  - insurance policy, reference or claim identifier;
  - membership or loyalty card number;
  - ABN, ACN or TFN number;
  - phone number, email address;
  - vehicle plate number.

Use an appropriate TYPE for each PII that you find, if unsure, fallback to PII_IDENTIFIER.
Do not output monetary amounts, transaction dates, interest rates, balances, percentages - they \
are NOT identifiers.
Output only the JSON array, with no code fence and no other text."""

_OUTPUT_VALUES = """
Output in this JSON format:
[{"type": "<TYPE>", "text": "<exact text as printed>"}]
If the page contains none, output []."""

_OUTPUT_BOXES = """
Output in this JSON format:
[{"type": "<TYPE>", "text": "<exact text as printed>", "bbox_2d": [x1, y1, x2, y2]}]
bbox_2d is the tight box around that text: (x1,y1) top-left, (x2,y2) bottom-right, in \
normalized relative coordinates scaled to 1000. Make the box enclose the whole string \
including its first and last characters.
If the page contains none, output []."""

# Pass 2 of the two-pass regime. Detection and grounding are separated because
# asking for both at once measurably costs recall — 350 -> 324 distinct values
# over 31 pages, and the page that lost its policy number lost the hardest-won
# detection on it. Splitting them recovers that in full (pass 1 below is
# byte-identical to the single-pass values prompt) and boxes MORE tightly
# (1.24x vs 1.41x ink). Evidence: reports/2026-08-08-vlm-oneshot-qwen36.md.
#
# OPERATIONAL, and it is not visible from this file: pass 2 is cheap only
# because the server restores a context checkpoint taken right after the image,
# which needs the patched llama-server and -ctxcp > 0. Two edits here would
# silently forfeit that and double the prefill of every page — putting anything
# ahead of the image in _ask's message list, and re-encoding the page to
# different PNG bytes between the two calls, since the server keys the image
# chunk on a hash of the encoded bytes. Neither fails loudly; both just get
# slow. See reports/2026-08-13-qwen36-ssm-prompt-cache.md.
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
    """One detection. `box` is the model's own normalized-to-1000 rectangle and
    is present only when the model was asked for geometry."""

    text: str
    entity_type: str
    box: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class Incomplete:
    """How many model responses in one unit of work came back unusable.

    COUNTED, not merely warned about, for the same reason as
    `ImageStripResult.unlocated`: Python's default warning filter shows one
    instance per code location, so the second looped page of a run is silent.

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
    """

    truncated: int = 0
    malformed: int = 0

    @property
    def total(self) -> int:
        return self.truncated + self.malformed

    def __bool__(self) -> bool:
        return bool(self.total)

    def __add__(self, other: "Incomplete") -> "Incomplete":
        return Incomplete(
            self.truncated + other.truncated,
            self.malformed + other.malformed,
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
    to learn anything. Retrying is safe here specifically because the request is
    idempotent — greedy, `seed` pinned — so a second ask returns the same
    answer and a retry can only recover a result, never change one.

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
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    # Forced open: an unmatched close means everything ahead of it is reasoning.
    # The FIRST close is the real one, which is also how llama.cpp's own parser
    # splits reasoning from content.
    _, closed, tail = raw.partition("</think>")
    return (tail if closed else raw).strip()


def read_response(response: dict) -> DetectorResult:
    """One server reply -> findings plus the failure counters.

    Shared by both layer-0 detectors, so the three-way split (clean page / cut
    off / not JSON) is decided in exactly one place.

    `finish_reason` is the whole point of this function. llama-server reports
    `"length"` when the generation was truncated, and reading it is what
    separates an empty page from an answer that never finished — without it a
    repetition loop, where the model emits the same entry until the budget runs
    out, is indistinguishable from a clean page. An array that DID close is
    complete regardless of why generation stopped: everything meaningful
    arrived, and whatever was cut was trailing.
    """
    try:
        choice = response["choices"][0]
        raw = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VlmError(f"unexpected response shape: {response!r}") from exc
    payload, complete = _extract_array(strip_thinking(fold_digits(raw)))
    findings = _findings_from(payload)
    if complete:
        return DetectorResult(findings)
    # The completed elements are kept either way (see _extract_array); which
    # counter this lands in is decided by WHY the array is still open — a
    # budget the generation ran into, or a body that was never JSON at all.
    if choice.get("finish_reason") == "length":
        return DetectorResult(findings, Incomplete(truncated=1))
    return DetectorResult(findings, Incomplete(malformed=1))


def parse_findings(raw: str) -> list[VlmFinding]:
    """Parse a response BODY into findings, ignoring completeness.

    The body-level seam: it shares every defence with `read_response` (fence,
    `<think>` block, folded digits, salvage) but has no envelope to read
    `finish_reason` from, so it cannot tell an empty page from an answer that
    never finished. Detectors therefore go through `read_response`; this stays
    as the entry point for anything holding only the text — the testbench, and
    a caller parsing a body it did not fetch itself."""
    payload, _ = _extract_array(strip_thinking(fold_digits(raw)))
    return _findings_from(payload)


def _findings_from(payload) -> list[VlmFinding]:
    """Turn parsed JSON items into findings, skipping what makes no sense."""
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
                x1, y1, x2, y2 = (int(v) for v in raw_box)
            except (TypeError, ValueError):
                box = None
            else:
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


class VlmDetector:
    """Detects PII directly from a page image.

    `want_boxes` makes the ONE-pass boxes prompt (`geometry="vlm"`). It stays
    off everywhere else because asking for coordinates alongside detection
    measurably costs recall — 350 -> 324 distinct values over 31 pages. The
    production route to geometry is `localize`, a second pass, which pays no
    such price.

    `grammar` constrains the output shape at the sampler. It is on by default
    and exists as a switch because constrained decoding alters the sampled
    distribution, so it is an A/B axis rather than a serialization detail.
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
        reasoning_budget: int = DEFAULT_REASONING_BUDGET,
    ) -> None:
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"unknown reasoning effort: {reasoning_effort!r}")
        self.url = url
        self.transport = transport or http_transport
        self.timeout = timeout
        self.want_boxes = want_boxes
        self._encode = encode_image or _encode_png
        self.grammar = grammar
        self.reasoning_effort = reasoning_effort
        self.reasoning_budget = reasoning_budget

    @property
    def thinking(self) -> bool:
        return self.reasoning_effort != "off"

    @property
    def max_tokens(self) -> int:
        """Answer allowance PLUS thinking allowance — they share one budget.

        Sized this way because of which limit bites first. Reaching
        `max_tokens` truncates the array mid-entry: a redaction failure that
        `Incomplete.truncated` can report but not undo, and on a page whose
        names and addresses are layer 0's alone to find. Reaching the reasoning
        budget instead closes the trace cleanly and still yields a whole answer.
        So the reasoning budget must be able to bite FIRST, which it can only do
        if `max_tokens` leaves the answer its own room on top."""
        return ANSWER_TOKENS + (self.reasoning_budget if self.thinking else 0)

    @property
    def prompt(self) -> str:
        return PROMPT + (_OUTPUT_BOXES if self.want_boxes else _OUTPUT_VALUES)

    @property
    def _detect_grammar(self) -> str | None:
        if not self.grammar:
            return None
        return GRAMMAR_VALUES_BOXES if self.want_boxes else GRAMMAR_VALUES

    def detect(self, image) -> DetectorResult:
        return read_response(self._ask(image, self.prompt, self._detect_grammar))

    def localize(self, image, findings: list[VlmFinding]) -> DetectorResult:
        """Pass 2: hand the already-detected values back and ask only where
        they are, returning the findings with `box` filled in where the model
        placed them.

        The model's answer is treated as a POOL of hints rather than a
        one-to-one reply: it routinely returns a different number of boxes
        than there were findings (a value printed twice, a value it declines
        to place), and pairing by position would then silently attach one
        value's box to another. Matching is by squashed text, assigned in
        order, and a finding that draws no hint simply keeps `box=None` and
        falls back to unconstrained search.

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
        hints = read_response(
            self._ask(
                image,
                _LOCATE_PROMPT.format(values=listing),
                GRAMMAR_LOCATE if self.grammar else None,
            )
        )
        return replace(
            hints, findings=attach_boxes(findings, hints.findings)
        )

    def _ask(self, image, prompt: str, grammar: str | None = None) -> dict:
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
            # serving (-np 1) makes greedy decode reproducible; parallel batching
            # does not, and a gate you can pass by re-rolling is not a gate.
            "temperature": 0.0,
            "top_k": 1,
            "top_p": 1.0,
            "seed": 42,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        payload.update(self._reasoning_fields())
        if grammar:
            # Per-request rather than a server flag, so the shape we enforce is
            # versioned with the code that parses it — the same reasoning that
            # keeps the sampling parameters above out of a launch script.
            payload["grammar"] = grammar
            payload.update(self._lazy_fields())
        return self.transport(self.url, payload, self.timeout)

    def _reasoning_fields(self) -> dict:
        """Thinking on (effort + budget + cut-off), or the pre-2026-08-19 off.

        All per-request, for the reason `grammar` is: a server flag would apply
        to every caller of that server and could not be versioned with the code
        that reads the reply. Needs `llama-server --jinja` for
        `chat_template_kwargs` to reach the template at all."""
        if not self.thinking:
            # The template then writes a PRE-CLOSED think block into the prompt,
            # so the budget sampler sees start-and-end among the prefill tokens
            # and the grammar applies from the first generated token. That is
            # exactly the old behaviour, which is what makes "off" a usable
            # baseline rather than a third thing.
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {
            "chat_template_kwargs": {"reasoning_effort": self.reasoning_effort},
            "reasoning_budget_tokens": self.reasoning_budget,
            "reasoning_budget_message": REASONING_CUTOFF,
        }

    def _lazy_fields(self) -> dict:
        """Make the grammar engage only after the thinking block.

        llama.cpp does the hard part: with a lazy grammar AND a reasoning-budget
        sampler, `grammar_should_apply()` is false for the whole thinking block,
        so the GBNF cannot constrain the trace by construction rather than by a
        trigger that happens to avoid it. The trigger then engages it at the
        array.

        **Requires a llama-server carrying the grammar_lazy passthrough fix.**
        Upstream's OAI layer overwrites `grammar_lazy` and `grammar_triggers`
        from the chat template unconditionally, and its copy-remaining loop only
        fills absent keys — so on a stock server these two are silently dropped,
        the grammar applies from token 0, and the model does not think at all.
        The failure is quiet: replies still parse, they are just unreasoned. See
        reports/2026-08-19-qwen38-bringup.md."""
        if not self.thinking:
            return {}
        return {
            "grammar_lazy": True,
            "grammar_triggers": [
                {"type": _TRIGGER_PATTERN, "value": GRAMMAR_TRIGGER}
            ],
        }


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
    keep `box=None`; surplus hints are discarded."""
    pool: dict[str, list[tuple[int, int, int, int]]] = {}
    for hint in hints:
        if hint.box is None:
            continue
        key, _ = squash_map(hint.text)
        pool.setdefault(key, []).append(hint.box)

    out = []
    for finding in findings:
        key, _ = squash_map(finding.text)
        boxes = pool.get(key)
        box = boxes.pop(0) if boxes else None
        out.append(replace(finding, box=box) if box is not None else finding)
    return out


class VlmError(RuntimeError):
    """The model server returned something unusable."""


class VlmUnavailable(VlmError):
    """The model server could not be reached at all."""


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
