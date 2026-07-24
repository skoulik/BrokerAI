"""Diagnostics for the OCR perception layer: dump / annotate an OcrPage.

Renderers over an OcrPage for `pii debug ocr` (and a future GUI): a
round-trippable JSON structure, a human-readable text summary, and an
annotated raster overlay (blocks + lines + reading order; synthetic blocks
marked). OCR-only — this imports no analysis stack, so a debug run stays
light and torch-free.

The JSON is faithful enough to round-trip (page_to_dict / page_from_dict),
so a dumped page can later be reloaded and fed straight to painting/
linearization without re-running OCR.
"""

import json

from PIL import Image

from pii.core.ocr import Box
from pii.core.ocr_page import OcrBlock, OcrFrame, OcrLine, OcrPage, OcrWord
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
        "blocks": [
            {
                "id": b.id, "kind": b.kind, "origin": b.origin,
                "box": _box_list(b.box), "reading_order": b.reading_order,
                "page_id": b.page_id, "conf": b.conf,
            }
            for b in page.blocks
        ],
        "lines": [
            {
                "text": ln.text, "box": _box_list(ln.box),
                "block_id": ln.block_id, "conf": ln.conf,
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
    blocks = tuple(
        OcrBlock(
            id=b["id"], kind=b["kind"], origin=b["origin"],
            box=_box_obj(b["box"]), reading_order=b["reading_order"],
            page_id=b["page_id"], conf=b.get("conf"),
        )
        for b in data["blocks"]
    )
    lines = tuple(
        OcrLine(
            text=ln["text"], box=_box_obj(ln["box"]),
            words=tuple(
                OcrWord(text=w["text"], box=_box_obj(w["box"]),
                        region_box=_box_obj(w["region_box"]))
                for w in ln["words"]
            ),
            block_id=ln["block_id"], conf=ln.get("conf"),
        )
        for ln in data["lines"]
    )
    return OcrPage(frame=frame, blocks=blocks, lines=lines)


def page_to_json(page: OcrPage, indent: int = 2) -> str:
    return json.dumps(page_to_dict(page), indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------


def page_to_text(page: OcrPage) -> str:
    f = page.frame
    lines_by_block: dict[int, list[OcrLine]] = {}
    for ln in page.lines:
        lines_by_block.setdefault(ln.block_id, []).append(ln)
    out = [
        f"page {f.page}  {f.width}x{f.height}  backend={f.backend}"
        f"  {len(page.blocks)} blocks, {len(page.lines)} lines"
    ]
    for b in sorted(page.blocks, key=lambda b: b.reading_order):
        blk_lines = lines_by_block.get(b.id, [])
        conf = f"{b.conf:.0f}" if b.conf is not None else "-"
        out.append(
            f"  block {b.id:<3} {b.kind:<11} {b.origin:<9} ro={b.reading_order}"
            f"  conf={conf}  box={tuple(b.box)}  [{len(blk_lines)} lines]"
        )
        for ln in blk_lines:
            lconf = f"{ln.conf:.0f}" if ln.conf is not None else "-"
            out.append(f"      line conf={lconf:<4} {ln.text!r}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Overlay (reuses the shared paint toolkit — pii.core.paint)
# ---------------------------------------------------------------------------

_LINE_COLOR = (90, 90, 90)  # thin grey — line boxes
_DETECTED_COLOR = (30, 120, 220)  # blue — detected layout blocks
_SYNTHETIC_COLOR = (230, 140, 20)  # amber — synthetic (fabricated) blocks


def draw_overlay(image: Image.Image, page: OcrPage) -> Image.Image:
    """Annotate a copy of `image` with the page's blocks and lines via the
    shared frame-style paint (`pii.core.paint.paint_segments`).

    Lines get thin grey rectangles; blocks a thicker outline labelled
    ``<reading_order>:<kind>`` — blue for detected, amber + a "synthetic" tag
    for synthetic. On a line-only page every block is synthetic, so the labels
    number the lines in assembly order — the `_rows()` grouping made visible.
    The input image is not mutated."""
    lines = [Segment("", [ln.box]) for ln in page.lines]
    detected = [Segment(f"{b.reading_order}:{b.kind}", [b.box])
                for b in page.blocks if b.origin == "detected"]
    synthetic = [Segment(f"{b.reading_order}:{b.kind} synthetic", [b.box])
                 for b in page.blocks if b.origin == "synthetic"]
    out = paint_segments(image, lines, margin=0, style="frame",
                         color=_LINE_COLOR, width=1)
    out = paint_segments(out, detected, margin=0, style="frame",
                         color=_DETECTED_COLOR, width=3)
    out = paint_segments(out, synthetic, margin=0, style="frame",
                         color=_SYNTHETIC_COLOR, width=3)
    return out
