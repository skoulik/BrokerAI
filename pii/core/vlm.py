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

Geometry has two sources and they are deliberately both kept:

- ``geometry="ocr"``  — the model supplies only the *values*; each is located in
  the OCR text and painted through `RecognizerInput.painted_boxes_for_span`.
  Safest: OCR word boxes are exact. This is the production path, and it is
  also what makes layer-1 refinement possible (there is text to refine
  against — see `PiiPipeline.merge_detections`).
- ``geometry="vlm"``  — the model's own ``bbox_2d`` is used and OCR never runs.
  Faster and simpler, but measured **unsafe**: 16% of boxes clip by more than
  20 px, the tail includes real account numbers, and the failure is *stochastic*
  (the same value on the same layout is boxed correctly on one page and wrongly
  on the next), so no padding or calibration fixes it.

The transport is injectable so the testbench never needs a model server.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
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

    `want_boxes` follows the geometry choice: asking for coordinates measurably
    COSTS recall (on one page, 20 findings including a policy number without
    boxes vs 19 without it with boxes), so the OCR-geometry path deliberately
    does not ask for them.
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
                        {"type": "text", "text": self.prompt},
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
            raw = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VlmError(f"unexpected response shape: {response!r}") from exc
        return parse_findings(raw)


class VlmError(RuntimeError):
    """The model server returned something unusable."""


class VlmUnavailable(VlmError):
    """The model server could not be reached at all."""


def _squash_map(text: str) -> tuple[str, list[int]]:
    """Alphanumeric squash plus an index from each squashed char back to its
    offset in the original, so a match found in squashed space can be reported
    as real offsets."""
    chars, index = [], []
    for i, ch in enumerate(fold_digits(text)):
        if ch.isalnum():
            chars.append(ch.lower())
            index.append(i)
    return "".join(chars), index


def locate(
    haystack: str, needle: str, taken: list[tuple[int, int]] | None = None
) -> tuple[int, int] | None:
    """Find `needle` in `haystack`, returning character offsets.

    Two transcriptions of the same pixels rarely agree exactly - the VLM may
    normalize spacing or punctuation the OCR kept, or vice versa - so matching
    is tiered: exact, then on an alphanumeric squash that ignores spacing,
    hyphens and case. Nothing fuzzier: an edit-distance match risks painting the
    WRONG region, which is worse than reporting the value as unlocatable, and
    unlocatable values are surfaced to the caller rather than dropped.

    `taken` lists already-claimed ranges so repeated values (an address printed
    twice on a page) map to successive occurrences instead of all collapsing
    onto the first.
    """
    taken = taken or []

    def free(start: int, end: int) -> bool:
        return not any(start < t_end and t_start < end for t_start, t_end in taken)

    at = haystack.find(needle)
    while at != -1:
        if free(at, at + len(needle)):
            return at, at + len(needle)
        at = haystack.find(needle, at + 1)

    hay_sq, index = _squash_map(haystack)
    need_sq, _ = _squash_map(needle)
    if not need_sq:
        return None
    at = hay_sq.find(need_sq)
    while at != -1:
        start = index[at]
        end = index[at + len(need_sq) - 1] + 1
        if free(start, end):
            return start, end
        at = hay_sq.find(need_sq, at + 1)
    return None


def _encode_png(image) -> str:
    import base64
    import io

    buf = io.BytesIO()
    image.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()
