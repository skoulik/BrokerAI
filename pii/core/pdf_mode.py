"""PDF stripping: PDF -> page images -> image pipeline -> fresh PDF.

PDFs are treated as images (core/TODO.md): render pages to pixels, run
the image path on each page, reassemble. Rationale for pixels-first:
financial-sector PDFs often carry junk or broken text layers, and
rebuilding the output from pixels eliminates the hidden-text-layer leak
class entirely — nothing from the source PDF's internal structure
survives into the output. The reassembled document is built from scratch
(`strip_pdf`), so text layers, annotations, attachments and source
metadata are absent *by construction*, not by scrubbing.

The renderer is pymupdf (decision 2026-07-17, over poppler/pdftoppm and
pypdfium2): pip-installable self-contained wheel, in-process rendering,
and the same library covers reassembly, the future belt-and-braces
text-layer scan, and metadata scrubbing. It is AGPL-licensed — fine for
internal use, revisit before any commercial distribution (the seam below
keeps a renderer swap contained).

Processing is lossless end-to-end (raw RGB renders through OCR, detection
and painting); only the final embed into the output PDF is lossy — JPEG,
because a 300 DPI A4 page is ~26 MB of raw pixels and a lossless embed
makes multi-page statements balloon to tens of MB. The eval scorer
re-OCRs output pixels, so any OCR-visible encoding damage is measured,
not hidden. Encoding choice/quality is fixed for now (configurability is
a recorded TODO).

Module import stays light (pymupdf + Pillow + pii.core.ocr) per the
pii.core lazy-import policy — OCR-only/torch-free processes may import
this module freely; `strip_pdf` pulls in the analysis stack on call.
"""

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import pymupdf
from PIL import Image

from pii.core.mapping import PseudonymMap
from pii.core.ocr import OcrResult, get_ocr

# 300 DPI is the scanning-industry default for OCR of small print;
# statements ship 7-9pt body text, which at the synthetic tier's 150 DPI
# falls below the glyph sizes the OCR fidelity sweep validated.
DEFAULT_DPI = 300

# Final-embed encoding (see module docstring: lossless until the embed).
_JPEG_QUALITY = 90


def pdf_page_count(path: str | Path) -> int:
    with pymupdf.open(path) as doc:
        return doc.page_count


def _render_page(page: pymupdf.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


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
            yield _render_page(page, dpi)


@dataclass
class PdfPageResult:
    """One page's detection record. Like ImageStripResult, `ocr` holds
    the recognized plaintext INCLUDING the PII — local-only artifact."""

    number: int  # 1-based page number
    ocr: OcrResult
    spans: list  # applied detections; offsets into ocr.text
    invalid: list


@dataclass
class PdfStripResult:
    pages: list[PdfPageResult]


def strip_pdf(
    path: str | Path,
    pipeline,
    pmap: PseudonymMap,
    out_path: str | Path,
    dpi: int = DEFAULT_DPI,
    ocr_backend: str = "paddle",
    progress: Callable[[int, int], None] | None = None,
) -> PdfStripResult:
    """Strip a PDF page by page and write a fresh, image-only PDF.

    Each page: render at `dpi` -> OCR -> full text pipeline -> paint
    placeholders on the pixels -> embed into a new page of the output
    document at the source page's physical size (points). Pages stream
    through one pipeline/OCR engine and one shared `pmap`, so memory
    stays flat and placeholders are consistent across the document.

    `progress(page_number, page_count)` is called before each page is
    processed (OCR + NER make pages slow enough to want a heartbeat).
    """
    from pii.core.image_mode import strip_from_ocr  # heavy: analysis stack

    ocr_engine = get_ocr(ocr_backend)
    pages: list[PdfPageResult] = []
    out_doc = pymupdf.open()
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, 1):
            if progress:
                progress(number, doc.page_count)
            image = _render_page(page, dpi)
            result = strip_from_ocr(image, ocr_engine(image), pipeline, pmap)
            buf = io.BytesIO()
            result.image.save(buf, "JPEG", quality=_JPEG_QUALITY)
            out_page = out_doc.new_page(
                width=page.rect.width, height=page.rect.height
            )
            out_page.insert_image(out_page.rect, stream=buf.getvalue())
            pages.append(
                PdfPageResult(
                    number=number,
                    ocr=result.ocr,
                    spans=result.spans,
                    invalid=result.invalid,
                )
            )
    # A fresh document carries nothing from the source; empty the
    # metadata dict too so not even library defaults land in the output.
    out_doc.set_metadata({})
    out_doc.save(out_path, garbage=4, deflate=True)
    out_doc.close()
    return PdfStripResult(pages=pages)


def rebuild_pdf(
    src_path: str | Path,
    out_path: str | Path,
    transform: Callable[[int, Image.Image], Image.Image],
    dpi: int = DEFAULT_DPI,
    pages: set[int] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Render each source page to `dpi`, run `transform(page_number, image)`
    -> RGB image, and embed the result into a fresh image-only PDF at the
    source page's physical size.

    Same fresh-document discipline as strip_pdf — no text layer, annotations
    or metadata from the source survive — but generic in the per-page image
    transform, so the debug overlay (and any other page-image annotation)
    reassembles a PDF through one code path. `pages` (a set of 1-based page
    numbers, None = all) selects which pages to include."""
    out_doc = pymupdf.open()
    with pymupdf.open(src_path) as doc:
        for number, page in enumerate(doc, 1):
            if pages is not None and number not in pages:
                continue
            if progress:
                progress(number, doc.page_count)
            annotated = transform(number, _render_page(page, dpi)).convert("RGB")
            buf = io.BytesIO()
            annotated.save(buf, "JPEG", quality=_JPEG_QUALITY)
            out_page = out_doc.new_page(
                width=page.rect.width, height=page.rect.height
            )
            out_page.insert_image(out_page.rect, stream=buf.getvalue())
    out_doc.set_metadata({})
    out_doc.save(out_path, garbage=4, deflate=True)
    out_doc.close()
