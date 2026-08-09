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

Three geometry regimes exist, and all three are deliberately kept:

- ``geometry="hybrid"`` — production. Two passes: `detect` names the values,
  `localize` hands them back and asks only where they are. The boxes are then
  used as a **search constraint** by `locator.py`, which paints OCR word boxes
  wherever the value can be matched and falls back to the model's box only for
  the residue that has no OCR text at all. Rationale for the split lives on
  `_LOCATE_PROMPT`; rationale for boxes-as-constraint lives in `locator.py`.
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

The transport is injectable so the testbench never needs a model server.
"""

from __future__ import annotations

import json
import os
import re
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

# The three geometry regimes described above. "hybrid" is production; the
# other two are kept so the comparison that produced that verdict stays
# runnable. Declared here rather than in image_mode because the CLI resolves
# the flag before it is willing to import the analysis stack.
GEOMETRIES = ("hybrid", "ocr", "vlm")
DEFAULT_GEOMETRY = "hybrid"

# Kept verbatim from the tuned probe prompt. Three properties are load-bearing
# and should not be edited casually - each was established by measurement:
#  - coarse classes: collapsing 14 -> 5 cost no recall and GAINED generalization
#    (a vehicle registration was caught with no mention of vehicles);
#  - no institutional carve-outs: over-strip is recoverable by the keep-list,
#    under-strip is a breach, and a prompt is the wrong place for a silent,
#    per-page, unauditable keep decision;
#  - identifiers-live-in-headings + naming "policy, reference and claim numbers":
#    a policy number rendered as a bold heading was missed until BOTH were
#    present. The structural hint alone was not enough.
PROMPT = """You are auditing a scanned Australian financial document for personally \
identifying information, so that it can be pseudonymized before leaving a secure network.

Report EVERY span of text on this page that could identify a person or an organization, or \
that ties the document to a particular customer. Be exhaustive: a missed identifier is a \
privacy breach. When in doubt, report it - reporting too much is harmless and corrected \
later, missing something is not. Include every occurrence, even when the same value appears \
more than once on the page.

Use these types:
  - PII_NAME        a person's name, full or partial
  - PII_ADDRESS     a postal address or any part of one (street line, suburb/state/postcode)
  - PII_COMPANY     the name of a company or organization
  - PII_DOB         a person's date of birth
  - PII_IDENTIFIER  any number or code identifying a person, organization or account -
                    account and customer numbers, BSB, card numbers, tax file numbers,
                    Medicare, ABN/ACN, membership and loyalty numbers, policy, reference
                    and claim numbers, phone numbers, email addresses, licence and
                    passport numbers

Monetary amounts, transaction dates, interest rates and balances are NOT identifiers - do \
not report them.

Identifiers appear anywhere on the page, not only as the value of a labelled field. A \
heading, title, footer, prose sentence or table cell may itself contain an identifier, with \
no separate label next to it - read those as carefully as you read labelled fields.

Transcribe each value EXACTLY as printed, preserving spacing, hyphens and punctuation. Do \
not normalize, reformat or correct it."""

_OUTPUT_VALUES = """

Output a JSON array only, no prose, no markdown fence:
[{"text": "<exact text as printed>", "type": "<TYPE>"}]
If the page contains none, output []."""

_OUTPUT_BOXES = """

Output a JSON array only, no prose, no markdown fence:
[{"text": "<exact text as printed>", "type": "<TYPE>", "bbox_2d": [x1, y1, x2, y2]}]
bbox_2d is the tight box around that text: (x1,y1) top-left, (x2,y2) bottom-right, in \
normalized relative coordinates scaled to 1000. Make the box enclose the whole string \
including its first and last characters.
If the page contains none, output []."""

# Pass 2 of the two-pass regime. Detection and grounding are separated because
# asking for both at once measurably costs recall — 350 -> 324 distinct values
# over 31 pages, and the page that lost its policy number lost the hardest-won
# detection on it. Splitting them recovers that in full (pass 1 below is
# byte-identical to the single-pass values prompt) and boxes MORE tightly
# (1.24x vs 1.41x ink). It is affordable because llama.cpp caches image
# prefill per image: a second pass on a page already seen costs ~16 s against
# the ~130 s the image itself cost. Evidence:
# reports/2026-08-08-vlm-oneshot-qwen36.md.
_LOCATE_PROMPT = """This page has already been read. Below is the list of text values found \
on it. Your only job now is to say WHERE each one is printed.

Values:
{values}

For every value in the list, output one entry per place it appears on the page. A value \
printed twice gets two entries. If you cannot find a value on the page, omit it — do not \
guess a location.

Output a JSON array only, no prose, no markdown fence:
[{{"text": "<the value, copied from the list>", "bbox_2d": [x1, y1, x2, y2]}}]
bbox_2d is the tight box around that text: (x1,y1) top-left, (x2,y2) bottom-right, in \
normalized relative coordinates scaled to 1000. Make the box enclose the whole string \
including its first and last characters."""


@dataclass(frozen=True)
class VlmFinding:
    """One detection. `box` is the model's own normalized-to-1000 rectangle and
    is present only when the model was asked for geometry."""

    text: str
    entity_type: str
    box: tuple[int, int, int, int] | None = None


class Transport(Protocol):
    def __call__(self, url: str, payload: dict, timeout: int) -> dict: ...


def http_transport(url: str, payload: dict, timeout: int) -> dict:
    """Default transport: stdlib only, so `pii.core` gains no dependency.

    Connection failures are translated into `VlmUnavailable` with the URL and a
    hint. The model server is usually on another machine, so "wrong --vlm-url"
    is the single most likely failure and a raw URLError traceback is a poor way
    to say it."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode(errors="replace")
        raise VlmUnavailable(
            f"model server at {url} returned HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise VlmUnavailable(
            f"cannot reach the model server at {url} ({reason}). Start "
            f"llama-server with a vision model, or pass --vlm-url if it runs "
            f"on another host (e.g. --vlm-url http://192.168.1.55:8080)."
        ) from exc


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
    """Drop <think> blocks — hybrid-thinking models emit them, and a reasoning
    trace over a page of numbers contains '[', which would capture the JSON
    scanner below."""
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()


def parse_findings(raw: str) -> list[VlmFinding]:
    """Parse the model's JSON array. Unparseable output yields no findings, and
    the caller is expected to treat that as a failure rather than an empty page."""
    payload = _extract_array(strip_thinking(fold_digits(raw)))
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


def _extract_array(body: str):
    """Find the first balanced top-level JSON array, tolerating a code fence."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1)
    start = body.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
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
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class VlmDetector:
    """Detects PII directly from a page image.

    `want_boxes` makes the ONE-pass boxes prompt (`geometry="vlm"`). It stays
    off everywhere else because asking for coordinates alongside detection
    measurably costs recall — 350 -> 324 distinct values over 31 pages. The
    production route to geometry is `localize`, a second pass, which pays no
    such price.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        transport: Transport | None = None,
        timeout: int = 1800,
        want_boxes: bool = False,
        encode_image: Callable[[object], str] | None = None,
    ) -> None:
        self.url = url
        self.transport = transport or http_transport
        self.timeout = timeout
        self.want_boxes = want_boxes
        self._encode = encode_image or _encode_png

    @property
    def prompt(self) -> str:
        return PROMPT + (_OUTPUT_BOXES if self.want_boxes else _OUTPUT_VALUES)

    def detect(self, image) -> list[VlmFinding]:
        return parse_findings(self._ask(image, self.prompt))

    def localize(
        self, image, findings: list[VlmFinding]
    ) -> list[VlmFinding]:
        """Pass 2: hand the already-detected values back and ask only where
        they are, returning the findings with `box` filled in where the model
        placed them.

        The model's answer is treated as a POOL of hints rather than a
        one-to-one reply: it routinely returns a different number of boxes
        than there were findings (a value printed twice, a value it declines
        to place), and pairing by position would then silently attach one
        value's box to another. Matching is by squashed text, assigned in
        order, and a finding that draws no hint simply keeps `box=None` and
        falls back to unconstrained search."""
        if not findings:
            return findings
        listing = "\n".join(
            f"- {value}" for value in dict.fromkeys(f.text for f in findings)
        )
        raw = self._ask(image, _LOCATE_PROMPT.format(values=listing))
        return attach_boxes(findings, parse_findings(raw))

    def _ask(self, image, prompt: str) -> str:
        payload = {
            # Hybrid-thinking models honour this via the chat template; it needs
            # llama-server --jinja to take effect.
            "chat_template_kwargs": {"enable_thinking": False},
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
            "max_tokens": 4096,
            "stream": False,
        }
        response = self.transport(self.url, payload, self.timeout)
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VlmError(f"unexpected response shape: {response!r}") from exc


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
