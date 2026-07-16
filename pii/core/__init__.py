"""BrokerAI PII engine (core).

The detection/pseudonymization engine, independent of any front-end. Both the
CLI (``pii.cli``) and the planned GUI (``pii.gui``) build on this package's
public API; the two front-ends must never import each other. If a front-end
needs assembly logic that only one of them has today, push it down here rather
than importing sideways. Design decisions live in pii/core/ARCHITECTURE.md.

The names below are the top-level public API. ``csv_mode``, ``image_mode`` and
``ocr`` are also public, imported from their submodules — ``image_mode``/``ocr``
pull in Pillow/pytesseract, so they are kept out of this eager import to keep
``import pii.core`` free of the image stack.
"""

from pii.core.constants import RECORD_SEPARATOR
from pii.core.invalid_recognizers import INVALID_ENTITY_TYPES
from pii.core.mapping import PseudonymMap
from pii.core.pipeline import DEFAULT_STRIP_ENTITIES, InvalidFinding, PiiPipeline

__all__ = [
    "PiiPipeline",
    "PseudonymMap",
    "RECORD_SEPARATOR",
    "DEFAULT_STRIP_ENTITIES",
    "InvalidFinding",
    "INVALID_ENTITY_TYPES",
]
