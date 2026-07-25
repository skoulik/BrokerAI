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
lost line is unredacted PII). That reconstruction is not PP-Structure-
specific and lives in ``ocr_page.build_layout_page``, shared with the
PP-DocLayoutV3 backend.

This module owns the PURE conversion (``ppstructure_result_to_page``), given
a NORMALIZED plain-dict result (the engine entry flattens LayoutBlock
objects to dicts). Engine construction + the GPU/torch-stub dance live in a
separate entry with a lazy paddle import.
"""

from functools import lru_cache

from pii.core.ocr import _to_box
from pii.core.ocr_page import OcrBlock, OcrFrame, OcrPage, build_layout_page
from pii.core.ocr_paddle import _result_lines


def _block_rank(block: dict) -> tuple:
    """Reading-order sort key: order_index ascending, None last (footers and
    the like carry no order_index), ties broken by raw layout index."""
    order_index = block.get("order_index")
    return (order_index is None, order_index if order_index is not None else 0,
            block.get("index") or 0)


def ppstructure_result_to_page(result: dict, frame: OcrFrame) -> OcrPage:
    """Convert a normalized PP-StructureV3 result into an OcrPage.

    Blocks come from parsing_res_list (sorted into reading order); lines come
    from overall_ocr_res. The line->block linkage, orphan handling and line
    emission order are the shared `build_layout_page` discipline."""
    # Detected blocks in PP-Structure reading order.
    raw = sorted(result.get("parsing_res_list") or [], key=_block_rank)
    blocks = [
        OcrBlock(id=r, kind=b.get("label") or "text", origin="detected",
                 box=_to_box(b["bbox"]), reading_order=r, page_id=frame.page)
        for r, b in enumerate(raw)
    ]
    return build_layout_page(
        blocks, _result_lines(result.get("overall_ocr_res") or {}), frame
    )


# --------------------------------------------------------------------------
# Engine entry: raw image -> OcrPage. Lazy paddle import; the GPU/torch-stub
# dance mirrors ocr_paddle._engine. Kept below the pure adapter so importing
# this module stays torch-free (the worker child imports it before stubbing
# torch). On the GPU wheel this runs only inside the worker subprocess;
# get_ocr_page picks worker vs in-process by wheel.
# --------------------------------------------------------------------------

def _paddlex_pipeline_config() -> dict | None:
    """PaddleX's shipped PP-StructureV3 pipeline config as a plain dict.

    Located with `find_spec` and read as data — deliberately NOT via `import
    paddlex`, so this stays callable from the torch-free half of the process."""
    import importlib.util
    from pathlib import Path

    import yaml

    spec = importlib.util.find_spec("paddlex")
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    if not locations:
        return None
    path = Path(locations[0]) / "configs" / "pipelines" / "PP-StructureV3.yaml"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _layout_labels(model_name: str) -> list | None:
    """The layout model's ordered class list, from its cached inference.yml."""
    from pathlib import Path

    import yaml

    from pii.core.ocr_paddle import CACHE_DIR

    path = Path(CACHE_DIR) / "official_models" / model_name / "inference.yml"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("label_list")


def _layout_thresholds(overrides: dict | None = None) -> dict | None:
    """PaddleX's shipped per-class layout thresholds with `overrides` applied
    — keyed by LABEL name, e.g. `{"text": 0.33}`. None means "leave the shipped
    config alone", which is what an empty `overrides` yields.

    **We currently override nothing.** This is the seam for tuning layout
    recall, not a tuning: to relax a class, pass it from `_structure_engine`,

        PPStructureV3(..., layout_threshold=_layout_thresholds({"text": 0.33}))

    and nothing else changes. Why you might want to: PaddleX ships a PER-CLASS
    threshold dict (text 0.4, table 0.5, paragraph_title 0.3, seal 0.45, rest
    0.5) — NOT the flat 0.5 the API's float knob implies. Label/value header
    panels on real statements score just under the `text` cut, so the panel is
    detected as nothing and every line in it arrives orphaned. Measurements and
    the alternatives (flat float, table class, merge modes) are in DONE.md.

    Three rules make the result safe to hand back to paddlex:

    - **Prefer this over the float knob.** Passing `layout_threshold` as a
      float REPLACES the whole per-class dict — it would silently raise
      `paragraph_title` from 0.3 to the float as well as lowering what you meant
      to lower. A dict keeps every other class untouched.
    - **The dict must be COMPLETE.** paddlex resolves a class as
      `threshold.get(cat_id, 0.5)`, so a partial dict silently resets every
      unlisted class to 0.5. We therefore start from the shipped dict and edit
      only the named entries.
    - **Class indices are resolved from the model's own `label_list`**, never
      hardcoded, so a reordered label list on a paddlex upgrade cannot retarget
      an override onto some other class.

    Anything missing (config, model dir, an unknown label) returns None rather
    than guessing: shipped behaviour, orphans and all, beats silently
    thresholding the wrong class. A None is skipped by paddleocr's config merge
    (`_pipelines/utils.py`), so it genuinely means "leave it alone"."""
    if not overrides:
        return None
    config = _paddlex_pipeline_config() or {}
    layout = (config.get("SubModules") or {}).get("LayoutDetection") or {}
    shipped, model_name = layout.get("threshold"), layout.get("model_name")
    if not isinstance(shipped, dict) or not model_name:
        return None
    labels = _layout_labels(model_name) or []
    if not all(label in labels for label in overrides):
        return None
    return {**shipped,
            **{labels.index(label): value for label, value in overrides.items()}}


@lru_cache(maxsize=None)
def _structure_engine():
    """Construct a lean PPStructureV3: layout detection + reading order + OCR
    only (table/formula/seal/chart/orientation all off — financial-doc PII
    needs text+boxes, not cell structure). OCR sub-models pinned to the paddle
    default tier so they reuse the already-cached OCR models. Layout thresholds
    are PaddleX's shipped per-class defaults — `_layout_thresholds` documents
    the seam for relaxing one, and why the float knob is the wrong way in.

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
        layout_threshold=_layout_thresholds(),
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
