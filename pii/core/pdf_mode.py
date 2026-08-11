"""PDF stripping: PDF -> page images -> image pipeline -> fresh PDF.

Two sweeps, not one: every page is READ (detect + localize + OCR) before
any page is REDACTED, so the findings can be grouped across the whole
document first and each page stripped against what all of them know.
The rendered pages live in a temporary on-disk cache in between — see
`strip_pdf` for why they are cached rather than rendered twice.


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
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Iterator

import pymupdf
from PIL import Image

from pii.core.mapping import PseudonymMap
from pii.core.ocr import get_ocr_page

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
    # A page-level RecognizerInput; None when the VLM supplied geometry
    # directly and OCR never ran.
    ocr: object | None
    spans: list  # applied detections; offsets into ocr.text
    invalid: list
    # What was painted — the only record on the VLM-geometry path (no spans).
    segments: list = dataclasses.field(default_factory=list)
    # Findings painted from the model's own box, and findings not painted at
    # all. Carried per page so a document-level report can total them; see
    # ImageStripResult for why they are counted rather than only warned about.
    box_geometry: list = dataclasses.field(default_factory=list)
    unlocated: list = dataclasses.field(default_factory=list)
    # Subset of `unlocated` whose value was painted elsewhere on the page —
    # see ImageStripResult for why the two are reported apart.
    unlocated_painted_elsewhere: list = dataclasses.field(default_factory=list)
    # Spans this page owes to detections made on OTHER pages — the values that
    # used to leak here. Counted per page for the same reason as the two above.
    borrowed: list = dataclasses.field(default_factory=list)


@dataclass
class PdfStripResult:
    pages: list[PdfPageResult]
    # The document-wide entity groups every page was stripped against, with
    # the vote each class was elected from (pii.core.grouping).
    groups: tuple = ()


def strip_pdf(
    path: str | Path,
    pipeline,
    pmap: PseudonymMap,
    out_path: str | Path,
    dpi: int = DEFAULT_DPI,
    ocr_backend: str = "paddle",
    progress: Callable[[int, int, str], None] | None = None,
    *,
    detector,
    geometry: str = "hybrid",
) -> PdfStripResult:
    """Strip a PDF in two sweeps and write a fresh, image-only PDF.

    Sweep 1 READS every page — render at `dpi`, detect, localize, OCR — and
    keeps the findings. Sweep 2 REDACTS every page against what the whole
    document then knows: values are grouped across pages
    (`pii.core.grouping`), and each page is searched for every value any page
    reported. That is the point of the split — a value layer 0 names on page 1
    and misses on page 4 used to be redacted on page 1 and leak on page 4,
    because a per-page pipeline had nothing that could notice.

    **Rendered pages are cached to disk between the sweeps, never re-rendered.**
    The model's boxes live in the coordinate space of the pixels it saw; a
    second render only *assumes* it reproduces the first (dpi rounding, a
    library bump, page rotation), while a cache makes it identical by
    construction. PNG, because processing stays lossless until the final embed.
    The cache holds full unredacted pages — near-PII of the strongest kind — so
    it lives in a temporary directory that is emptied as the run proceeds (each
    page's file is unlinked the moment that page is embedded) and removed on
    the way out, including on an exception.

    One pipeline, one OCR engine and one shared `pmap` serve all pages, so
    placeholders are consistent across the document.

    `progress(page_number, page_count, phase)` is called before each page,
    with phase "read" or "redact" — two sweeps really are two passes over the
    document, and a run measured in minutes per page needs to say which.
    """
    # heavy: the analysis stack
    from pii.core.grouping import group_findings
    from pii.core.image_mode import read_page, strip_from_vlm

    # Resolved ONCE for the document, not per page — and skipped entirely on
    # the one path that never reads pixels through OCR (geometry="vlm").
    page_engine = get_ocr_page(ocr_backend) if geometry != "vlm" else None
    pages: list[PdfPageResult] = []
    with TemporaryDirectory(prefix="pii-pages-") as cache_dir:
        cache = Path(cache_dir)
        reads = []
        sizes: list[tuple[float, float]] = []
        with pymupdf.open(path) as doc:
            count = doc.page_count
            for number, page in enumerate(doc, 1):
                if progress:
                    progress(number, count, "read")
                image = _render_page(page, dpi)
                image.save(_cache_path(cache, number), "PNG")
                reads.append(
                    read_page(
                        image, page_engine,
                        detector=detector, geometry=geometry,
                    )
                )
                # Captured here so sweep 2 never reopens the source document.
                sizes.append((page.rect.width, page.rect.height))

        grouping = group_findings([read.findings for read in reads])

        out_doc = pymupdf.open()
        for index, read in enumerate(reads):
            number = index + 1
            if progress:
                progress(number, count, "redact")
            cached = _cache_path(cache, number)
            # Closed before unlinking — an open handle blocks removal on
            # Windows, and Pillow reads lazily.
            with Image.open(cached) as stored:
                image = stored.convert("RGB")
            result = strip_from_vlm(
                image, read.findings, pipeline, pmap,
                ocr=read.ocr, grouping=grouping,
            )
            cached.unlink()
            buf = io.BytesIO()
            result.image.save(buf, "JPEG", quality=_JPEG_QUALITY)
            width, height = sizes[index]
            out_page = out_doc.new_page(width=width, height=height)
            out_page.insert_image(out_page.rect, stream=buf.getvalue())
            pages.append(
                PdfPageResult(
                    number=number,
                    ocr=result.ocr,
                    spans=result.spans,
                    invalid=result.invalid,
                    segments=result.segments,
                    box_geometry=result.box_geometry,
                    unlocated=result.unlocated,
                    unlocated_painted_elsewhere=(
                        result.unlocated_painted_elsewhere
                    ),
                    borrowed=result.borrowed,
                )
            )
        # A fresh document carries nothing from the source; empty the
        # metadata dict too so not even library defaults land in the output.
        out_doc.set_metadata({})
        out_doc.save(out_path, garbage=4, deflate=True)
        out_doc.close()
    return PdfStripResult(pages=pages, groups=grouping.groups)


def _cache_path(cache: Path, number: int) -> Path:
    return cache / f"page-{number:04d}.png"


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
