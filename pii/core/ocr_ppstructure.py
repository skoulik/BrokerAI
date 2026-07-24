"""PP-StructureV3 adapter: a raw layout+OCR result -> OcrPage.

Layout-aware perception. PP-StructureV3 returns, per page:

- ``overall_ocr_res`` — the flat per-line OCR (rec_texts / rec_boxes /
  rec_scores, optional word fragments), the same shape plain PaddleOCR
  emits. This is the source of LINES (text + boxes + conf).
- ``parsing_res_list`` — typed layout blocks (``label``, ``bbox``,
  reading-order ``order_index``, a line ``num_of_lines`` count). This is the
  source of BLOCKS.

Critically, PP-Structure exposes NO line->block linkage (measured
2026-07-24: ``child_blocks`` is empty for text blocks, a block carries only
its concatenated content + bbox + a line COUNT). So we reconstruct the
linkage ourselves by geometric containment of each line box in a block box
— cross-checked against ``num_of_lines`` (a count, not identities). A line
that lands in no block becomes its own synthetic block (never dropped: a
lost line is unredacted PII).

This module owns the PURE conversion (``ppstructure_result_to_page``), given
a NORMALIZED plain-dict result (the engine entry flattens LayoutBlock
objects to dicts). Engine construction + the GPU/torch-stub dance live in a
separate entry with a lazy paddle import.
"""

from functools import lru_cache

from pii.core.ocr import Box, _to_box, _union
from pii.core.ocr_page import OcrBlock, OcrFrame, OcrLine, OcrPage, OcrWord
from pii.core.ocr_paddle import _region_words


def _block_rank(block: dict) -> tuple:
    """Reading-order sort key: order_index ascending, None last (footers and
    the like carry no order_index), ties broken by raw layout index."""
    order_index = block.get("order_index")
    return (order_index is None, order_index if order_index is not None else 0,
            block.get("index") or 0)


def _assign(line_box: Box, block_boxes: list[Box]) -> int | None:
    """Index of the block whose bbox contains the line's centre; failing
    that, the block with the largest overlap area; None if the line overlaps
    no block at all (an orphan → its own synthetic block)."""
    cx = line_box.left + line_box.width / 2
    cy = line_box.top + line_box.height / 2
    for j, bb in enumerate(block_boxes):
        if bb.left <= cx <= bb.right and bb.top <= cy <= bb.bottom:
            return j
    best, best_area = None, 0
    for j, bb in enumerate(block_boxes):
        ox = max(0, min(line_box.right, bb.right) - max(line_box.left, bb.left))
        oy = max(0, min(line_box.bottom, bb.bottom) - max(line_box.top, bb.top))
        if ox * oy > best_area:
            best, best_area = j, ox * oy
    return best


def ppstructure_result_to_page(result: dict, frame: OcrFrame) -> OcrPage:
    """Convert a normalized PP-StructureV3 result into an OcrPage.

    Blocks come from parsing_res_list (sorted into reading order); lines come
    from overall_ocr_res and are assigned to a containing block by geometry;
    orphan lines get one synthetic block each. Lines are emitted in
    (block reading order, top, left) order — the layout model's reading order
    replacing the geometric row-banding of the line-only path."""
    ocr = result.get("overall_ocr_res") or {}
    texts = ocr.get("rec_texts") or []
    scores = ocr.get("rec_scores") or []
    rec_boxes = ocr.get("rec_boxes")
    polys = ocr.get("rec_polys")
    frag_texts = ocr.get("text_word") or []
    frag_boxes = ocr.get("text_word_boxes") or []

    # Detected blocks in PP-Structure reading order.
    raw = sorted(result.get("parsing_res_list") or [], key=_block_rank)
    det_boxes = [_to_box(b["bbox"]) for b in raw]
    det_kinds = [b.get("label") or "text" for b in raw]

    # Each OCR line: word geometry (shared paddle normalization) + its
    # containing detected block (or None -> orphan).
    lines_data = []  # (line_box, words[(word, box)], conf, det_index_or_None)
    for i, text in enumerate(texts):
        if not text.strip():
            continue
        src = (rec_boxes[i]
               if rec_boxes is not None and len(rec_boxes) > i else polys[i])
        line_box = _to_box(src)
        conf = float(scores[i]) * 100 if len(scores) > i else 0.0
        frags = (
            list(zip(frag_texts[i], frag_boxes[i]))
            if len(frag_texts) > i and len(frag_boxes) > i
            else None
        )
        words = _region_words(text, line_box, frags)
        lines_data.append((line_box, words, conf, _assign(line_box, det_boxes)))

    # Blocks: detected first (reading_order = sorted rank), then one synthetic
    # block per orphan line, appended after the detected run.
    blocks = [
        OcrBlock(id=r, kind=det_kinds[r], origin="detected", box=box,
                 reading_order=r, page_id=frame.page)
        for r, box in enumerate(det_boxes)
    ]
    line_block = []
    next_ro = len(det_boxes)
    for line_box, _words, _conf, det in lines_data:
        if det is not None:
            line_block.append(det)
        else:
            blocks.append(OcrBlock(
                id=len(blocks), kind="unassigned", origin="synthetic",
                box=line_box, reading_order=next_ro, page_id=frame.page))
            line_block.append(len(blocks) - 1)
            next_ro += 1

    # Emit lines in (block reading order, top, left) order.
    order = sorted(
        range(len(lines_data)),
        key=lambda k: (blocks[line_block[k]].reading_order,
                       lines_data[k][0].top, lines_data[k][0].left),
    )
    lines = []
    for k in order:
        line_box, words, conf, _ = lines_data[k]
        lines.append(OcrLine(
            text=" ".join(w for w, _b in words),
            box=_union([b for _w, b in words]) if words else line_box,
            words=tuple(
                OcrWord(text=w, box=b, region_box=line_box) for w, b in words
            ),
            block_id=line_block[k],
            conf=conf,
        ))
    return OcrPage(frame=frame, blocks=tuple(blocks), lines=tuple(lines))


# --------------------------------------------------------------------------
# Engine entry: raw image -> OcrPage. Lazy paddle import; the GPU/torch-stub
# dance mirrors ocr_paddle._engine. Kept below the pure adapter so importing
# this module stays torch-free (the worker child imports it before stubbing
# torch). On the GPU wheel this runs only inside the worker subprocess;
# get_ocr_page picks worker vs in-process by wheel.
# --------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _structure_engine():
    """Construct a lean PPStructureV3: layout detection + reading order + OCR
    only (table/formula/seal/chart/orientation all off — financial-doc PII
    needs text+boxes, not cell structure). OCR sub-models pinned to the paddle
    default tier so they reuse the already-cached OCR models.

    Mirrors ocr_paddle._engine's wheel/DLL dance: on the GPU wheel paddle's
    DLLs load first, then a torch stub (with the Tensor shim) so paddlex,
    modelscope and scipy don't drag real torch into this process."""
    import os
    import sys
    from pathlib import Path

    from pii.core.ocr_paddle import (
        CACHE_DIR, DEFAULT_TIER, MODEL_TIERS, _gpu_wheel, _stub_torch,
    )

    os.environ.setdefault(
        "PADDLE_PDX_CACHE_HOME", str(Path(CACHE_DIR).resolve())
    )
    if _gpu_wheel():
        if "torch" in sys.modules and not getattr(
            sys.modules["torch"], "__pii_stub__", False
        ):
            raise RuntimeError(
                "paddlepaddle-gpu and torch cannot share a process on Windows "
                "(conflicting bundled cudnn DLLs). Run PP-Structure through "
                "the worker subprocess (get_ocr_page uses it on the GPU wheel)."
            )
        import paddle  # noqa: F401  (GPU DLLs must load before the torch stub)

        _stub_torch()
        device = "gpu"
    else:
        import torch  # noqa: F401  (CPU wheel: torch first or paddle breaks)

        device = "cpu"

    from paddleocr import PPStructureV3

    det_model, rec_model = MODEL_TIERS[DEFAULT_TIER]
    return PPStructureV3(
        device=device,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_seal_recognition=False,
        use_table_recognition=False,
        use_formula_recognition=False,
        use_chart_recognition=False,
        text_detection_model_name=det_model,
        text_recognition_model_name=rec_model,
    )


def _as_list(x):
    return x.tolist() if hasattr(x, "tolist") else x


def _block_field(block, name):
    """Read a field off a live LayoutBlock (attributes in __dict__, some are
    properties) — safe for both."""
    fields = getattr(block, "__dict__", {})
    return fields[name] if name in fields else getattr(block, name, None)


def _normalize(raw: dict) -> dict:
    """Flatten a live PPStructureV3 result into the plain-dict shape the pure
    adapter consumes: LayoutBlock objects -> dicts, numpy arrays -> lists."""
    ocr = raw.get("overall_ocr_res") or {}
    rec_boxes = ocr.get("rec_boxes")
    rec_polys = ocr.get("rec_polys")
    blocks = []
    for blk in raw.get("parsing_res_list") or []:
        blocks.append({
            "label": _block_field(blk, "label"),
            "bbox": [int(v) for v in _as_list(_block_field(blk, "bbox"))],
            "num_of_lines": _block_field(blk, "num_of_lines"),
            "order_index": _block_field(blk, "order_index"),
            "index": _block_field(blk, "index"),
        })
    return {
        "overall_ocr_res": {
            "rec_texts": list(ocr.get("rec_texts") or []),
            "rec_scores": [float(s) for s in (ocr.get("rec_scores") or [])],
            "rec_boxes": (
                [[int(v) for v in b] for b in _as_list(rec_boxes)]
                if rec_boxes is not None else None
            ),
            "rec_polys": _as_list(rec_polys) if rec_polys is not None else None,
        },
        "parsing_res_list": blocks,
    }


def _structure_predict(image) -> dict:
    import numpy as np

    bgr = np.asarray(image.convert("RGB"))[:, :, ::-1]
    raw = dict(list(_structure_engine().predict(bgr))[0])
    return _normalize(raw)


def ppstructure_page(image, lang: str = "eng") -> OcrPage:
    """OCR + layout-parse a PIL image into an OcrPage (typed blocks + reading
    order). `lang` is accepted for OCR-seam parity and ignored."""
    frame = OcrFrame(
        width=image.width, height=image.height, page=1,
        backend="paddle:structure",
    )
    return ppstructure_result_to_page(_structure_predict(image), frame)
