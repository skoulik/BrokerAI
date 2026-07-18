"""PDF page rendering: PDF -> page images.

PDFs are treated as images (core/TODO.md): render pages to pixels, run
the image path on each page, reassemble. This module owns the first leg.
Rationale for pixels-first: financial-sector PDFs often carry junk or
broken text layers, and rebuilding the output from pixels eliminates the
hidden-text-layer leak class entirely — nothing from the source PDF's
internal structure survives into the output.

The renderer is pymupdf (decision 2026-07-17, over poppler/pdftoppm and
pypdfium2): pip-installable self-contained wheel, in-process rendering,
and the same library later covers reassembly, the belt-and-braces
text-layer scan, and metadata scrubbing. It is AGPL-licensed — fine for
internal use, revisit before any commercial distribution (the seam below
keeps a renderer swap contained).

Import stays light (pymupdf + Pillow only) per the pii.core lazy-import
policy — OCR-only/torch-free processes may import this module freely.
"""

from pathlib import Path
from typing import Iterator

import pymupdf
from PIL import Image

# 300 DPI is the scanning-industry default for OCR of small print;
# statements ship 7-9pt body text, which at the synthetic tier's 150 DPI
# falls below the glyph sizes the OCR fidelity sweep validated.
DEFAULT_DPI = 300


def pdf_page_count(path: str | Path) -> int:
    with pymupdf.open(path) as doc:
        return doc.page_count


def pdf_to_images(
    path: str | Path, dpi: int = DEFAULT_DPI
) -> Iterator[Image.Image]:
    """Render each page of a PDF to an RGB Pillow image, in page order.

    A generator: a 300 DPI A4 page is ~26 MB of pixels, so callers
    stream pages through the per-page pipeline instead of holding a
    whole document in memory.
    """
    with pymupdf.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
