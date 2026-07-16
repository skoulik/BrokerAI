"""BrokerAI PII engine (core).

The detection/pseudonymization engine, independent of any front-end. Both the
CLI (``pii.cli``) and the planned GUI (``pii.gui``) build on this package's
public API; the two front-ends must never import each other. If a front-end
needs assembly logic that only one of them has today, push it down here rather
than importing sideways. Design decisions live in pii/core/ARCHITECTURE.md.

The public names below resolve lazily (PEP 562): importing ``pii.core`` — or
any light submodule like ``pii.core.ocr`` — must not drag in the analysis
stack (presidio -> spaCy -> thinc, which opportunistically loads torch).
That laziness is load-bearing, not an optimization: the paddlepaddle-gpu
wheel cannot share a process with torch on Windows (conflicting bundled
cudnn DLLs — see pii/core/ocr_paddle.py), so OCR-only processes such as the
pii_eval fidelity sweep must be able to reach ``pii.core.ocr`` while staying
torch-free. ``csv_mode``, ``image_mode`` and ``ocr`` stay submodule imports
for the same reason.
"""

from pii.core.constants import RECORD_SEPARATOR

_LAZY = {
    "PseudonymMap": ("pii.core.mapping", "PseudonymMap"),
    "INVALID_ENTITY_TYPES": ("pii.core.invalid_recognizers",
                             "INVALID_ENTITY_TYPES"),
    "DEFAULT_STRIP_ENTITIES": ("pii.core.pipeline", "DEFAULT_STRIP_ENTITIES"),
    "InvalidFinding": ("pii.core.pipeline", "InvalidFinding"),
    "PiiPipeline": ("pii.core.pipeline", "PiiPipeline"),
}

__all__ = ["RECORD_SEPARATOR", *_LAZY]


def __getattr__(name: str):
    try:
        module, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    import importlib

    return getattr(importlib.import_module(module), attr)


def __dir__():
    return sorted(__all__)
