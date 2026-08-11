"""PaddleOCR adapter: image -> OcrPage (the perception layer).

The OCR engine behind the `pii.core.ocr.get_ocr_page` seam (Tesseract was the
first backend, retired 2026-07-17). PaddleOCR is line-oriented: detection finds text-line regions
anywhere on the page (no page-layout model), recognition returns one
string + one confidence per region. Normalization into per-word
geometry follows the 2026-07-16 review findings (record in DONE.md):

- `rec_texts` line strings are authoritative for the assembled text.
  The `return_word_box` fragments have unreliable boundaries (merged
  tokens like "TFN123", "013-999Acct"), so they are NEVER used as
  tokens — only as geometry: each line word is mapped onto the
  fragment char stream (whitespace squeezed out on both sides) and its
  box is the union of overlapping fragment boxes. If the streams
  disagree, the whole line falls back to proportional interpolation
  over the line box. Recall-first: mapping ambiguity inflates boxes.
- Confidence is per line (0-1); every word of a line carries the line
  conf scaled to 0-100. Coarser than Tesseract's per-word conf — a
  documented semantic difference.
- Detected regions carry no reading order; they are banded into visual
  rows by y-center (same geometry discipline as the eval harness) and
  each row becomes one assembled line, left-to-right — so statement
  rows reach the recognizers as single lines even when detection split
  them into separate regions.
- Windows DLL rules (verified 2026-07-16/17): with the CPU wheel,
  torch must be imported BEFORE paddle or torch's shm.dll breaks —
  handled here. With the GPU wheel (paddlepaddle-gpu, cu126, sm_75
  verified on the 2080 Ti), torch and paddle are MUTUALLY EXCLUSIVE in
  one process: both bundle cudnn_cnn64_9.dll from different CUDA
  families and the second loader gets WinError 127, whichever the
  order. Every path is torch-free since 2026-08-09, so the GPU wheel
  serves all of them at full speed.
- `enable_mkldnn=False` avoids the paddle 3.3.x oneDNN PIR-executor
  crash on PP-OCRv5 server models (upstream bug; CPU path only, the
  flag is inert on GPU).
- Models: two tiers registered (PP-OCRv5_server, PP-OCRv6_medium);
  default is v6_medium after the round-1 bake-off (DEFAULT_TIER below).
  Downloads land under PADDLE_PDX_CACHE_HOME, defaulted here to the
  repo-convention `models/paddlex` (same cwd-relative pattern as
  the repo model-cache convention).
- The worker subprocess that used to isolate GPU paddle from torch was
  retired 2026-08-09: nothing in the strip path imports torch since Presidio
  and spaCy went, so OCR runs in-process on either wheel. `_engine` below
  still refuses to start when torch IS loaded — that guard is what turns a
  future re-introduction into a clear error instead of a DLL crash, and it
  is the only thing standing between the two libraries now.
"""

import os
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image

from pii.core.ocr import (
    Box,
    _interpolate,
    _rows,
    _to_box,
    _union,
)
from pii.core.ocr_page import OcrFrame, OcrPage, build_page

CACHE_DIR = "models/paddlex"
# Two tiers from the bake-off (Sergei, 2026-07-17): v5's top tier is
# server; PP-OCRv6 ships no server tier, so its top is medium. Selected
# via the backend string ("paddle:v5_server"). Default is v6_medium — the
# round-1 fidelity verdict (reports/2026-07-17-ocr-fidelity-*.md): ~25×
# lower CER than Tesseract, ~6× lower than v5_server, no x-height cliff,
# and none of v5's word-merge pathology.
DEFAULT_TIER = "v6_medium"
MODEL_TIERS = {
    "v5_server": ("PP-OCRv5_server_det", "PP-OCRv5_server_rec"),
    "v6_medium": ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
}


def _gpu_wheel() -> bool:
    """Which paddle wheel is installed, decided WITHOUT importing paddle
    (importing is exactly what the DLL rules below gate on)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        version("paddlepaddle-gpu")
        return True
    except PackageNotFoundError:
        return False


class _Anything:
    """Inert attribute/call sink for the torch stub below."""

    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *args, **kwargs):
        return _Anything()


def _stub_torch() -> None:
    """Install a fake `torch` so paddleocr can import in a GPU-wheel
    process where the real torch must never load.

    paddlex hard-imports `modelscope` (official_models.py), and
    modelscope hard-imports torch at import time — which would load
    torch's cudnn DLLs and break paddle-GPU (the mutual exclusion in
    the module docstring). The stub satisfies modelscope's import-time
    needs (verified empirically 2026-07-17: __spec__ probing, package
    shape, torch.multiprocessing, torch.distributed.is_available/
    is_initialized, annotation chains like torch.nn.Module) and
    answers everything else with inert dummies. Anything that later
    tries REAL torch work in this process gets the stub
    and fails — by design: a GPU-paddle process is OCR-only.
    """
    import importlib.machinery
    import types

    if "torch" in sys.modules:
        return

    def _sub(name):
        m = types.ModuleType(name)
        m.__spec__ = importlib.machinery.ModuleSpec(name, None)
        m.__getattr__ = lambda attr: _Anything()
        sys.modules[name] = m
        return m

    stub = _sub("torch")
    stub.__pii_stub__ = True
    stub.__version__ = "2.0.0+pii.stub"
    stub.__path__ = []
    # scipy/sklearn probe `issubclass(x, torch.Tensor)`; the __getattr__ sink
    # returns an _Anything instance, not a class -> TypeError. Present Tensor
    # as a real empty class so the check cleanly returns False (no tensors
    # live in a torch-stubbed process). Verified 2026-07-24. Added for the
    # paddlex[ocr] extras PP-StructureV3 needed; kept after that backend was
    # retired (2026-08-09) because the extras may still be installed and a
    # stub that answers one more probe costs nothing.
    stub.Tensor = type("Tensor", (), {})
    dist = _sub("torch.distributed")
    dist.is_available = lambda: False
    dist.is_initialized = lambda: False
    stub.distributed = dist
    mp = _sub("torch.multiprocessing")
    mp.get_start_method = lambda allow_none=True: "spawn"
    stub.multiprocessing = mp


@lru_cache(maxsize=None)
def _engine(tier: str = DEFAULT_TIER):
    det_model, rec_model = MODEL_TIERS[tier]
    os.environ.setdefault(
        "PADDLE_PDX_CACHE_HOME", str(Path(CACHE_DIR).resolve())
    )
    if _gpu_wheel():
        # Mutual exclusion (see docstring): fail with the story instead
        # of the WinError 127 the cudnn clash would produce downstream.
        if "torch" in sys.modules and not getattr(
            sys.modules["torch"], "__pii_stub__", False
        ):
            raise RuntimeError(
                "paddlepaddle-gpu and torch cannot share a process on "
                "Windows (conflicting bundled cudnn DLLs). This process "
                "already imported torch. Nothing in the strip path should "
                "— that is what let the worker subprocess be retired "
                "(2026-08-09) — so find the import and drop it, or install "
                "the CPU paddle wheel."
            )
        import paddle  # noqa: F401  (GPU DLLs must load first)

        _stub_torch()
        device = "gpu"
    else:
        import torch  # noqa: F401  (CPU wheel: torch first or it breaks)

        device = "cpu"
    from paddleocr import PaddleOCR

    return PaddleOCR(
        text_detection_model_name=det_model,
        text_recognition_model_name=rec_model,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device=device,
        enable_mkldnn=False,
    )


def _predict(image: Image.Image, tier: str) -> dict:
    """Run the engine on one image; return the raw PaddleOCR page dict.

    `lang` is not a parameter: PaddleOCR selects languages via its model
    choice, and the pinned models cover Latin text — callers accept `lang`
    only for OCR-seam signature parity and ignore it."""
    import numpy as np

    bgr = np.asarray(image.convert("RGB"))[:, :, ::-1]
    return dict(_engine(tier).predict(bgr, return_word_box=True)[0])


def ocr_page_paddle(
    image: Image.Image, lang: str = "eng", tier: str = DEFAULT_TIER
) -> OcrPage:
    """OCR a PIL image with PaddleOCR into an OcrPage. The frame records the
    raster size and which model produced it."""
    frame = OcrFrame(
        width=image.width, height=image.height, page=1,
        backend="paddle", tier=tier,
    )
    return result_to_page(_predict(image, tier), frame)


def _result_lines(result: dict):
    """Paddle OCR result -> flat `(line_box, words[(word, box)], conf)` list,
    one entry per recognized region, in the engine's own order.

    The line/word normalization (`_region_words`) in exactly one place;
    `_result_to_rows` then bands these regions into visual rows."""
    texts = result.get("rec_texts") or []
    scores = result.get("rec_scores") or []
    boxes = result.get("rec_boxes")
    polys = result.get("rec_polys")
    frag_texts = result.get("text_word") or []
    frag_boxes = result.get("text_word_boxes") or []

    lines = []
    for i, text in enumerate(texts):
        if not text.strip():
            continue
        line_box = _to_box(
            boxes[i] if boxes is not None and len(boxes) > i else polys[i]
        )
        conf = float(scores[i]) * 100 if len(scores) > i else 0.0
        frags = (
            list(zip(frag_texts[i], frag_boxes[i]))
            if len(frag_texts) > i and len(frag_boxes) > i
            else None
        )
        lines.append((line_box, _region_words(text, line_box, frags), conf))
    return lines


def _result_to_rows(result: dict):
    """Paddle result -> assembled visual rows: `_result_lines` plus y-center
    banding. Each word carries its region (line) box: the fragment boxes are
    inset from the glyphs, so painting grows out to it.

    The banding is load-bearing, not cosmetic — it is what puts a label and
    its value from two side-by-side detection regions onto ONE assembled
    line, which is how context promotion reaches an account number sitting in
    a column beside its own label."""
    return _rows([
        (line_box, [(w, b, conf, line_box) for w, b in words])
        for line_box, words, conf in _result_lines(result)
    ])


def result_to_page(result: dict, frame: OcrFrame) -> OcrPage:
    """Pure conversion of one PaddleOCR page result into an OcrPage.
    `frame` supplies the raster/provenance the raw result lacks."""
    return build_page(_result_to_rows(result), frame)


def _region_words(text, line_box, frags):
    """(word, Box) list for one recognized line; see module docstring."""
    words = []
    pos = 0
    for word in text.split():
        words.append((word, pos, pos + len(word)))
        pos += len(word)
    if not words:
        return []
    if frags is not None:
        spans = []
        fpos = 0
        for ftext, fbox in frags:
            squeezed = "".join(str(ftext).split())
            if not squeezed:
                continue
            spans.append((fpos, fpos + len(squeezed), _to_box(fbox)))
            fpos += len(squeezed)
        if spans and fpos == pos:
            return [
                (word, _union([b for fs, fe, b in spans
                               if max(s, fs) < min(e, fe)]))
                for word, s, e in words
            ]
    return _interpolate(text, line_box)
