"""Diagnostics for the OCR perception layer: dump / annotate an OcrPage.

Renderers over an OcrPage for `pii debug ocr` (and a future GUI): a
round-trippable JSON structure, a human-readable text summary, and an
annotated raster overlay (line boxes in assembly order). OCR-only — this
imports no analysis stack, so a debug run stays light and torch-free.

The JSON is faithful enough to round-trip (page_to_dict / page_from_dict),
so a dumped page can later be reloaded and fed straight to painting/
linearization without re-running OCR.

What this shows is the geometry the strip path will actually paint with:
under `--geometry ocr` a value the VLM detected is located in this text and
painted with these word boxes, so a line missing here is a value that cannot
be redacted.
"""

import json

from PIL import Image

from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, OcrLine, OcrPage, OcrWord
from pii.core.paint import Segment, paint_segments

# ---------------------------------------------------------------------------
# JSON (round-trippable)
# ---------------------------------------------------------------------------


def _box_list(box: Box | None):
    return None if box is None else [box.left, box.top, box.width, box.height]


def _box_obj(seq):
    return None if seq is None else Box(*seq)


def page_to_dict(page: OcrPage) -> dict:
    frame = page.frame
    return {
        "frame": {
            "width": frame.width, "height": frame.height, "page": frame.page,
            "dpi": frame.dpi, "source": frame.source,
            "backend": frame.backend, "tier": frame.tier,
        },
        "lines": [
            {
                "text": ln.text, "box": _box_list(ln.box), "conf": ln.conf,
                "words": [
                    {"text": w.text, "box": _box_list(w.box),
                     "region_box": _box_list(w.region_box)}
                    for w in ln.words
                ],
            }
            for ln in page.lines
        ],
    }


def page_from_dict(data: dict) -> OcrPage:
    f = data["frame"]
    frame = OcrFrame(
        width=f["width"], height=f["height"], page=f["page"],
        dpi=f.get("dpi"), source=f.get("source"),
        backend=f.get("backend"), tier=f.get("tier"),
    )
    lines = tuple(
        OcrLine(
            text=ln["text"], box=_box_obj(ln["box"]),
            words=tuple(
                OcrWord(text=w["text"], box=_box_obj(w["box"]),
                        region_box=_box_obj(w["region_box"]))
                for w in ln["words"]
            ),
            conf=ln.get("conf"),
        )
        for ln in data["lines"]
    )
    return OcrPage(frame=frame, lines=lines)


def page_to_json(page: OcrPage, indent: int = 2) -> str:
    return json.dumps(page_to_dict(page), indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------


def page_to_text(page: OcrPage) -> str:
    f = page.frame
    out = [
        f"page {f.page}  {f.width}x{f.height}  backend={f.backend}"
        f"  {len(page.lines)} lines"
    ]
    for i, ln in enumerate(page.lines):
        conf = f"{ln.conf:.0f}" if ln.conf is not None else "-"
        out.append(
            f"  line {i:<4} conf={conf:<4} box={tuple(ln.box)}  {ln.text!r}"
        )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Overlay (reuses the shared paint toolkit — pii.core.paint)
# ---------------------------------------------------------------------------

_WORD_COLOR = (90, 90, 90)  # thin grey — word boxes
_LINE_COLOR = (30, 120, 220)  # blue — assembled line boxes, numbered


def draw_overlay(image: Image.Image, page: OcrPage) -> Image.Image:
    """Annotate a copy of `image` with the page's lines and words via the
    shared frame-style paint (`pii.core.paint.paint_segments`).

    Words get thin grey rectangles; each assembled line a thicker blue
    outline labelled with its index, so the `_rows()` banding is visible —
    two side-by-side detection regions sharing one number is a label and its
    value that WILL reach the recognizer together. The input image is not
    mutated."""
    words = [Segment("", [w.box]) for ln in page.lines for w in ln.words]
    lines = [Segment(str(i), [ln.box]) for i, ln in enumerate(page.lines)]
    out = paint_segments(image, words, margin=0, style="frame",
                         color=_WORD_COLOR, width=1)
    return paint_segments(out, lines, margin=0, style="frame",
                          color=_LINE_COLOR, width=3)
