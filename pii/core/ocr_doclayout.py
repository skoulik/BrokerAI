"""PP-DocLayoutV3 adapter: layout blocks from PP-DocLayoutV3 + text lines from
PaddleOCR -> OcrPage.

The second layout-aware perception backend (after PP-StructureV3), built to
answer one question: does a newer layout MODEL see financial-document
structure — statement tables above all — better than the one PP-StructureV3
ships with? The two backends differ ONLY in where blocks come from:

- ``ppstructure`` runs the PP-StructureV3 *pipeline*, whose layout submodule
  is **PP-DocLayout_plus-L** (an RT-DETR detector: boxes + labels, no order).
  Reading order is then computed by the pipeline's own xycut pass.
- ``doclayout:v3`` runs **PP-DocLayoutV3** standalone (``paddleocr.
  LayoutDetection``) and pairs it with a direct ``PaddleOCR`` call for lines.
  V3 is a transformer with a reading-order head, so the ORDER IS THE MODEL'S.

Text lines come from the same pinned PP-OCR tier either way (``ocr_paddle.
DEFAULT_TIER``), so switching backends cannot change a single recognized
character — only blocks and reading order move. That is the whole point: the
fidelity numbers (CER, leak gate) are held constant by construction while the
structure question is isolated.

Three facts about V3's raw result, measured 2026-07-25 (they drive the
conversion below and are NOT guesses from the docs):

- **The box list is already in model reading order.** paddlex sorts by the
  reading-order column before returning (``layout_analysis/processors.py``
  ``np.argsort(boxes[:, 6])``), so LIST POSITION is the reading order.
- **The ``order`` field is a filtered enumeration, not the order.**
  ``update_order_index`` numbers 1..N over that sorted list but blanks every
  label in ``SKIP_ORDER_LABELS`` — which includes ``table``, ``image``,
  ``header``, ``footer``. Ranking by that field the way PP-Structure's
  ``order_index`` is ranked would push every statement table to the END of
  the page, i.e. exactly the wrong answer for our documents. So we take list
  position and ignore ``order``.
- **Blocks carry ``polygon_points``** (a 4-point quad per block) alongside the
  axis-aligned ``coordinate``. Deliberately not stored yet: nothing consumes
  block polygons (paint works off boxes) and ``ocr_debug.page_to_dict`` does
  not serialize them, so filling ``OcrBlock.polygon`` would silently break the
  round-trip. It is the lever to reach for if skewed scans need it.

This module owns the PURE conversion (``doclayout_result_to_page``) over
normalized plain-dict inputs; engine construction + the GPU/torch-stub dance
live below it with a lazy paddle import (mirrors ocr_ppstructure.py).
"""

from functools import lru_cache

from PIL import Image

from pii.core.ocr import _to_box
from pii.core.ocr_page import OcrBlock, OcrFrame, OcrPage, build_layout_page
from pii.core.ocr_paddle import DEFAULT_TIER, _predict, _result_lines

# Layout models behind this backend, selected by the backend string
# ("doclayout:v3"). V2 is the same architecture one generation back — also
# reading-order-capable, also reachable by name — and can be registered here
# if a comparison ever needs it; unevaluated names stay out of the seam.
LAYOUT_MODELS = {"v3": "PP-DocLayoutV3"}
DEFAULT_LAYOUT = "v3"


def doclayout_result_to_page(
    layout: list[dict], ocr: dict, frame: OcrFrame
) -> OcrPage:
    """Convert a normalized (layout result, PaddleOCR result) pair into an
    OcrPage.

    `layout` is the list of detected blocks IN MODEL READING ORDER (see the
    module docstring — list position is the order, the `order` field is not),
    each `{"label", "score", "coordinate": [x1, y1, x2, y2]}`. `ocr` is a raw
    PaddleOCR page result. The line->block linkage, orphan handling and line
    emission order are the shared `build_layout_page` discipline."""
    blocks = [
        OcrBlock(
            id=r,
            kind=b.get("label") or "text",
            origin="detected",
            box=_to_box(b["coordinate"]),
            reading_order=r,
            page_id=frame.page,
            conf=float(b["score"]) * 100 if b.get("score") is not None else None,
        )
        for r, b in enumerate(layout)
    ]
    return build_layout_page(blocks, _result_lines(ocr), frame)


# --------------------------------------------------------------------------
# Engine entry: raw image -> OcrPage. Lazy paddle import; the GPU/torch-stub
# dance is ocr_paddle._engine's, reached by constructing the OCR engine first.
# Kept below the pure adapter so importing this module stays torch-free (the
# worker child imports it before stubbing torch). On the GPU wheel this runs
# only inside the worker subprocess; get_ocr_page picks worker vs in-process.
# --------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _shipped_knobs(model_name: str) -> dict:
    """The detection operating point PaddleX ships FOR THIS MODEL, lifted from
    the pipeline config that uses it (`threshold`, `layout_nms`,
    `layout_unclip_ratio`, `layout_merge_bboxes_mode`).

    Standalone `LayoutDetection` does NOT use it: with no explicit threshold it
    falls back to `draw_threshold: 0.5` out of the model's inference.yml — a
    visualization default, not an operating point. Measured 2026-07-25 on the
    real corpus, running V3 at 0.5 detects NOTHING on whole pages (d11: zero
    blocks, every line orphaned), while PP-StructureV3 hands plus-L a tuned
    per-class dict. Comparing the two backends therefore meant comparing a
    tuned model against an untuned one — the config below is what makes the
    comparison honest, and it is Baidu's own shipped answer for this model.

    Matched BY MODEL NAME, newest pipeline first, and only the block that
    actually names our model is used — so the index-keyed dicts inside it
    (merge modes are keyed by class id) are guaranteed to be keyed by OUR
    model's taxonomy. That is precisely the trap `ocr_ppstructure.
    _layout_thresholds` warns about: PP-StructureV3's dict is keyed by
    PP-DocLayout_plus-L's classes and would silently retarget on V3's
    25-class list. Nothing matched -> {} -> shipped defaults, never a guess.
    """
    import importlib.util
    from pathlib import Path

    import yaml

    spec = importlib.util.find_spec("paddlex")
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    if not locations:
        return {}
    configs = Path(locations[0]) / "configs" / "pipelines"
    for path in sorted(configs.glob("PaddleOCR-VL*.yaml"), reverse=True):
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        layout = (config.get("SubModules") or {}).get("LayoutDetection") or {}
        if layout.get("model_name") != model_name:
            continue
        return {
            key: layout[key]
            for key in ("threshold", "layout_nms", "layout_unclip_ratio",
                        "layout_merge_bboxes_mode")
            if layout.get(key) is not None
        }
    return {}


@lru_cache(maxsize=None)
def _layout_engine(layout: str = DEFAULT_LAYOUT, tier: str = DEFAULT_TIER):
    """Construct the standalone layout detector at its shipped operating point
    (`_shipped_knobs`).

    The OCR engine is built FIRST and deliberately: `ocr_paddle._engine` owns
    the wheel/DLL/torch-stub dance (and its RuntimeError carries the story
    when torch is already loaded), so going through it means this module
    repeats none of it. Both engines then live in this process — the worker
    subprocess on the GPU wheel — sharing the paddle runtime."""
    from pii.core.ocr_paddle import _engine, _gpu_wheel

    _engine(tier)  # wheel/DLL/torch-stub dance + the OCR models
    from paddleocr import LayoutDetection

    model_name = LAYOUT_MODELS[layout]
    return LayoutDetection(
        model_name=model_name,
        device="gpu" if _gpu_wheel() else "cpu",
        **_shipped_knobs(model_name),
    )


def _as_list(x):
    return x.tolist() if hasattr(x, "tolist") else x


def _layout_predict(image: Image.Image, layout: str, tier: str) -> list[dict]:
    """Run the layout model on one image; return its blocks as plain dicts, in
    the model's reading order (the result list's own order)."""
    import numpy as np

    bgr = np.asarray(image.convert("RGB"))[:, :, ::-1]
    raw = list(_layout_engine(layout, tier).predict(bgr))[0]
    return [
        {
            "label": b.get("label"),
            "score": float(b["score"]) if b.get("score") is not None else None,
            "coordinate": [int(v) for v in _as_list(b["coordinate"])],
        }
        for b in (dict(raw).get("boxes") or [])
    ]


def doclayout_page(
    image: Image.Image,
    lang: str = "eng",
    layout: str = DEFAULT_LAYOUT,
    tier: str = DEFAULT_TIER,
) -> OcrPage:
    """Detect layout with PP-DocLayoutV3 and OCR with PaddleOCR into an
    OcrPage (typed blocks + model reading order). `lang` is accepted for
    OCR-seam parity and ignored (PaddleOCR selects language by model)."""
    frame = OcrFrame(
        width=image.width, height=image.height, page=1,
        backend=f"doclayout:{layout}", tier=tier,
    )
    return doclayout_result_to_page(
        _layout_predict(image, layout, tier), _predict(image, tier), frame
    )
