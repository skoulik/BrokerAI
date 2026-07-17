"""Surya 2 adapter: image -> OcrResult via line detection + per-line VLM OCR.

Surya 2 (surya-ocr 0.20+) is a 650M VLM served OUT-OF-PROCESS through an
OpenAI-compatible server (llama-server with the F16 GGUFs here; vllm on
Linux), plus a small in-process torch line-DETECTION model. v1's native
word/char boxes are gone — block-level HTML is all the VLM returns — so
this adapter composes the upstream-intended line path (their docstring:
block mode exists "for downstream merging with text-line detection"):

  DetectionPredictor (torch) -> line polygons
  -> synthesized one-box-per-line LayoutResult (label "Text", per-line
     token budget from the line's pixel width)
  -> RecognitionPredictor block mode -> per-line HTML
  -> HTML flattened to plain text (entities unescaped, tags dropped;
     no separator between inline fragments — a mid-word <b> split must
     not break an identifier for value matching)
  -> proportional line->word interpolation (`pii.core.ocr._interpolate`;
     surya emits no word fragments at all, unlike paddle)
  -> `_rows` banding -> `assemble`.

Operational profile (2026-07-17 review + smoke test, records in DONE.md):

- surya reads env AT IMPORT TIME (pydantic-settings), so `_set_env`
  runs before the first surya import — `get_ocr`'s lazy import
  guarantees the ordering. Caches follow the repo convention:
  HF_HUB_CACHE -> models/hf-cache (GGUF + mmproj), MODEL_CACHE_DIR ->
  models/surya (detection weights, their S3).
- Windows: the backend is forced to llamacpp unless SURYA_INFERENCE_URL
  is set (autodetect picks vllm on any NVIDIA box; vllm has no Windows
  support and its bfloat16 default won't start on Turing anyway).
  SURYA_INFERENCE_URL attaches to an externally managed llama-server
  and skips spawning — the remote/Mac path.
- Auto-spawn works on Windows but surya's own cleanup does NOT
  (WinError 5 -> orphaned server holding ~1.5 GB VRAM), so this adapter
  registers its own atexit kill using the PID from surya's sentinel
  file (~/.cache/datalab/surya/<backend>_server.json). Deliberately
  honored: SURYA_INFERENCE_KEEP_ALIVE=1 keeps the server up for
  back-to-back sweep runs.
- Placement (smoke-tested, llama-server b9968 Vulkan): 2080 Ti ~0.5
  s/line warm (260 tok/s decode); RX 9070 XT pathologically slow (12
  tok/s — RDNA4 Vulkan immaturity), so NVIDIA until a build fixes it.
- Confidence is the block's mean token probability (0-1) scaled to
  0-100 for every word of the line — paddle-grade coarseness; treat
  with the same "not an error filter" caveat until measured.
- A block the server ERRORED on raises RuntimeError: a failed VLM call
  is an infra fault, and failing loud beats silently leaving a line's
  pixels unexamined (leak-first). A blank block (empty HTML) is a
  content property and is skipped, like paddle's empty lines.
"""

import atexit
import json
import os
import signal
import subprocess
import unicodedata
from pathlib import Path

from PIL import Image

from pii.core.ocr import Box, OcrResult, _interpolate, _rows, _to_box, assemble

HF_CACHE_DIR = "models/hf-cache"
DET_CACHE_DIR = "models/surya"

# Rounded-to-50 token estimate for a line `width_px` wide: ~10 px/glyph
# at our render sizes, ~3 chars/token, then headroom. Their budget adds
# +100 slack on top (surya.inference.util.image_token_budget); a short
# real line decodes ~40 tokens, so the 50 floor is already generous.
_TOKENS_PER_PX = 1 / 30


def _line_budget(width_px: float) -> int:
    return max(50, round(width_px * _TOKENS_PER_PX / 50 + 0.5) * 50)


def _set_env() -> None:
    os.environ.setdefault("HF_HUB_CACHE", str(Path(HF_CACHE_DIR).resolve()))
    os.environ.setdefault("MODEL_CACHE_DIR", str(Path(DET_CACHE_DIR).resolve()))
    os.environ.setdefault("DISABLE_TQDM", "1")
    if not os.environ.get("SURYA_INFERENCE_URL"):
        os.environ.setdefault("SURYA_INFERENCE_BACKEND", "llamacpp")
    # LOAD-BEARING: without a vision-token floor, small line crops get so
    # few image tokens that the VLM fabricates ("Lady Marian Perochenko"
    # from an account-number crop), loops, and table-izes — the s42 gate
    # went 6-leak catastrophic to near-clean on this one flag (2026-07-17
    # diagnosis; llama.cpp itself warns Qwen-VL wants >=1024). If you set
    # LLAMA_CPP_EXTRA_ARGS yourself (e.g. --device), re-include it.
    os.environ.setdefault("LLAMA_CPP_EXTRA_ARGS", "--image-min-tokens 1024")


_STATE: dict = {}


def _predictors():
    if "rec" not in _STATE:
        _set_env()
        from surya.detection import DetectionPredictor
        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor

        manager = SuryaInferenceManager()
        manager.start()
        _register_shutdown(manager)
        _STATE["det"] = DetectionPredictor()
        _STATE["rec"] = RecognitionPredictor(manager)
    return _STATE["det"], _STATE["rec"]


def _register_shutdown(manager) -> None:
    """Kill the auto-spawned inference server at exit — surya's own atexit
    cleanup fails on Windows (WinError 5), orphaning the server. PID comes
    from surya's sentinel file; external (SURYA_INFERENCE_URL) and
    keep-alive servers are left alone."""
    handle = manager.backend.handle
    if handle is None or not handle.spawned_by_us:
        return
    if os.environ.get("SURYA_INFERENCE_KEEP_ALIVE", "") not in ("", "0", "false"):
        return
    sentinel = (
        Path("~/.cache/datalab/surya").expanduser() / f"{manager.method}_server.json"
    )

    def _kill() -> None:
        try:
            pid = int(json.loads(sentinel.read_text())["pid"])
        except (OSError, ValueError, KeyError):
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    atexit.register(_kill)


def ocr_image_surya(image: Image.Image, lang: str = "eng") -> OcrResult:
    """OCR a PIL image with Surya 2 into an OcrResult.

    `lang` is accepted for seam-signature parity and ignored (the VLM is
    multilingual; there is no language knob on this path).
    """
    det, rec = _predictors()
    rgb = image.convert("RGB")
    detection = det([rgb])[0]
    segments = []
    for line in detection.bboxes:
        segments.extend(_split_line(rgb, _to_box(line.polygon)))
    layout = _segments_to_layout(segments, rgb.size)
    if not layout.bboxes:
        return assemble([])
    page = rec([rgb], [layout], full_page=False)[0]
    return page_to_ocr(page)


# Gap splitting: a whitespace run inside a line counts as a column gutter
# when it is wider than this fraction of the line's height (word gaps run
# ~0.2-0.5x height even in monospace; statement gutters measure several
# heights). Pixels within `_BG_TOLERANCE` luminance of the line's border
# background count as blank.
_GAP_FRAC = 1.5
_BG_TOLERANCE = 32


def _split_line(image: Image.Image, box) -> list:
    """Split one detected line box at wide horizontal whitespace gaps.

    WHY: the VLM re-interprets a full-width statement row crop as a
    *table* — literal ' | ' separators, phantom header cells, duplicated
    words (observed on legacy_00 s42; the s42 critical leaks traced to
    exactly this). Narrow single-phrase crops come back as clean <p>
    text, so wide gutters are split BEFORE recognition; `_rows` re-bands
    the segments into one assembled line afterwards. Splitting also
    tightens each segment to its inked columns, which shrinks the
    proportional-interpolation error on word boxes.

    Returns a list of Box segments (possibly just the trimmed input).
    """
    import numpy as np

    crop = image.convert("L").crop(
        (box.left, box.top, box.right, box.bottom)
    )
    cols = np.asarray(crop)
    if cols.size == 0:
        return []
    # Background = the line's own border median (statement stripes can
    # differ from the page background).
    border = np.concatenate([cols[0, :], cols[-1, :]])
    bg = float(np.median(border))
    blank = (np.abs(cols.astype(float) - bg) <= _BG_TOLERANCE).all(axis=0)
    inked = np.flatnonzero(~blank)
    if inked.size == 0:
        return []
    min_gap = max(int(box.height * _GAP_FRAC), 4)
    segments = []
    start = inked[0]
    prev = inked[0]
    for x in inked[1:]:
        if x - prev > min_gap:
            segments.append((start, prev))
            start = x
        prev = x
    segments.append((start, prev))
    pad = 2
    out = []
    for s, e in segments:
        left = box.left + int(s) - pad
        right = box.left + int(e) + pad
        out.append(
            Box(
                left=max(left, box.left),
                top=box.top,
                width=max(min(right, box.right) - max(left, box.left), 1),
                height=box.height,
            )
        )
    return out


def _segments_to_layout(segments, size):
    """Segment boxes -> a synthetic LayoutResult whose 'blocks' are the
    individual line segments (label Text, reading order as given, token
    budget from pixel width)."""
    from surya.layout.schema import LayoutBox, LayoutResult

    w, h = size
    boxes = []
    for i, seg in enumerate(segments):
        polygon = [
            [seg.left, seg.top],
            [seg.right, seg.top],
            [seg.right, seg.bottom],
            [seg.left, seg.bottom],
        ]
        boxes.append(
            LayoutBox(
                polygon=polygon,
                label="Text",
                raw_label="Text",
                position=i,
                count=_line_budget(seg.width),
            )
        )
    return LayoutResult(bboxes=boxes, image_bbox=[0, 0, float(w), float(h)])


def page_to_ocr(page) -> OcrResult:
    """Pure conversion of a per-line PageOCRResult into OcrResult.

    Duck-typed on purpose (needs `.blocks` with `.polygon`/`.html`/
    `.skipped`/`.error`/`.confidence`) so the conversion is testable
    without importing surya.
    """
    regions = []
    errors = []
    for block in page.blocks:
        if getattr(block, "skipped", False):
            continue
        if getattr(block, "error", False):
            errors.append(block)
            continue
        text = _flatten_html(block.html)
        if not text:
            continue
        line_box = _to_box(block.polygon)
        conf = max(0.0, min(float(block.confidence or 0.0), 1.0)) * 100
        words = _interpolate(text, line_box)
        regions.append((line_box, [(w, b, conf) for w, b in words]))
    if errors:
        boxes = [_to_box(b.polygon) for b in errors]
        raise RuntimeError(
            f"surya OCR failed on {len(errors)} line(s) (infra fault, "
            f"failing loud rather than leaving pixels unexamined): "
            f"first at {boxes[0]}"
        )
    return assemble(_rows(regions))


def _flatten_html(html: str) -> str:
    """Model-emitted line HTML -> plain text.

    Inline tags are concatenated with NO separator (a mid-word `<b>`
    must not split an identifier and break value matching); block-level
    boundaries and <br> become spaces; entities are unescaped by the
    parser; whitespace is collapsed.
    """
    if not html:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with(" ")
    for tag in soup.find_all(
        ["p", "div", "td", "th", "tr", "li", "pre", "h1", "h2", "h3", "h4", "h5"]
    ):
        tag.insert_after(" ")
    text = " ".join(soup.get_text("").split())
    # Belt-and-braces against residual table-izing: the VLM separates
    # perceived columns with literal pipes; our documents never draw '|'
    # glyphs, so standalone pipes are model artifacts, not content.
    text = " ".join(t for t in text.split() if t != "|")
    return _fold_digits(text)


def _fold_digits(text: str) -> str:
    """Fold every non-ASCII Unicode digit to its ASCII value.

    A multilingual VLM emits visually identical cross-script homoglyphs —
    observed on a CLEAN 32 px render: U+06F5 ۵ (Extended Arabic-Indic
    five) for '5', a confusion class no classic OCR engine has. In our
    Latin-script documents any non-ASCII digit is per se a recognition
    artifact, and unfolded it breaks value matching / checksums by string
    identity while LOOKING correct — a silent-leak shape. Letter
    homoglyphs (Cyrillic А etc.) are a watch item for the fidelity sweep,
    not folded here.
    """
    if text.isascii():
        return text
    out = []
    for ch in text:
        d = None if ch.isascii() else unicodedata.digit(ch, None)
        out.append(str(d) if d is not None else ch)
    return "".join(out)
