"""Render a generated text corpus into images — iteration 1 of the image tier.

Prints each text-corpus document onto a white page image (Pillow, system
TTF fonts), producing a paired corpus: same content, same truth.json, two
modalities. Score deltas between the text and image runs are then
attributable to exactly two causes — OCR errors and the loss of structure
the text path exploits (e.g. CSV cell isolation). The degradation pipeline
(DPI/skew/blur/JPEG) from the image-tier task composes on top of these
renders later; the reportlab statement templates are a separate, second
*layout* source feeding the same machinery.

Font variety (2026-07-16): fonts are drawn per document from a seeded RNG
so the OCR engine sees more than one glyph profile. Fixed-column documents
(legacy statements, rendered CSV tables) only stay faithful in monospace —
their layout IS the whitespace — so they draw from the monospace pool;
prose-shaped loan documents draw from the full pool (a proportional font
un-aligns the value column, which is realistic for printed forms). The
choice is recorded per doc in manifest.json so score deltas stay
attributable.

CSV sources are rendered as column-aligned monospace tables (the
"tabular statements arrive as scans" scenario) — cell ground truth
coordinates don't apply to the image path; the image scorer matches
values, not offsets.

Pagination (2026-08-11): documents span 1-3 pages, because a one-page
corpus cannot see the failure the two-sweep pipeline exists for — an
entity detected on one page and missed on another. Text documents carry
their own page breaks as form feeds emitted by the templates
(`build.Doc.page_break`), so pagination is described once, in the source
text, and the truth offsets are unaffected. CSVs cannot: a form feed
inside a CSV would break the parse, so their tables are split by row
count here with the COLUMN HEADER row repeated — page furniture carrying
no PII, which leaves the paired-corpus attribution intact.

Each document renders to one PNG per page and is also assembled into a
PDF, so the same rendered pixels feed both scoring modalities: `--modality
image` strips page by page (no cross-page knowledge — the control) and
`--modality pdf` runs the real two-sweep `strip_pdf` over them.
"""

import csv
import io
import json
import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Pools are Windows system fonts (bare names resolve via C:/Windows/Fonts).
MONO_FONTS = ["consola.ttf", "cour.ttf", "lucon.ttf"]
PROPORTIONAL_FONTS = [
    "arial.ttf", "calibri.ttf", "times.ttf",
    "verdana.ttf", "segoeui.ttf", "georgia.ttf",
]
FONT_SIZES = [20, 22, 24, 26]  # px; a readable range for the image tier

_PAD = 48  # page margin, px
_LINE_SPACING = 0.35  # extra leading as a fraction of the font's line height

# CSV tables have no form feeds to split on (see the module docstring), so
# they are cut by row count. Chosen to put a 15-40 row transaction table on
# 1-2 pages, matching the 1-3 page range the text templates produce.
_CSV_ROWS_PER_PAGE = 28

# Embedded into the assembled PDF at the DPI the pages were drawn for, so a
# page comes out at its natural physical size and score_pdf's re-render at
# the same DPI reproduces these pixels.
_RENDER_DPI = 150


def _is_fixed_column(filename: str) -> bool:
    """Docs whose layout is carried by whitespace must render monospace."""
    return filename.endswith(".csv") or filename.startswith("legacy")


def _paginates(filename: str) -> bool:
    """Whether a doc is worth splitting across pages.

    The name-forms statistics doc is not: every row is a DIFFERENT person by
    construction, so it has no entity to carry across a page break and
    splitting it would only spend model time (minutes per page) on pages that
    can teach the cross-page path nothing.
    """
    return not Path(filename).name.startswith("names")


def format_csv_table(text: str) -> str:
    """Column-align a CSV as a printable table (2-space gutters)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    ncols = max(len(r) for r in rows)
    widths = [
        max((len(r[c]) for r in rows if c < len(r)), default=0)
        for c in range(ncols)
    ]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip()
        for r in rows
    )


def paginate(text: str, is_csv: bool, enabled: bool = True) -> list[str]:
    """Split a document's text into pages.

    Text documents carry their own breaks (form feeds from
    `build.Doc.page_break`) — pagination is a property of the document, not of
    the renderer, so the text tier and the image tier agree by construction.
    CSV tables are cut by row count with the column header repeated, because a
    form feed inside a CSV would break the parse.
    """
    if not enabled:
        return [text.replace("\f", "")]
    if not is_csv:
        return text.split("\f")
    lines = text.splitlines()
    if len(lines) <= _CSV_ROWS_PER_PAGE + 1:
        return ["\n".join(lines)]
    header, rows = lines[0], lines[1:]
    return [
        "\n".join([header, *rows[at : at + _CSV_ROWS_PER_PAGE]])
        for at in range(0, len(rows), _CSV_ROWS_PER_PAGE)
    ]


def write_pdf(pages: list[Image.Image], path: Path) -> None:
    """Assemble rendered pages into a PDF at their natural physical size.

    Pillow's own PDF writer, not pymupdf: this is corpus construction, and
    building the input with the same library the pipeline uses to read it
    would let a bug in that library hide itself.
    """
    first, *rest = pages
    first.save(
        path, "PDF", resolution=_RENDER_DPI, save_all=True, append_images=rest
    )


def render_page(text: str, font_name: str, size: int) -> Image.Image:
    """Draw the text line-by-line onto a white content-sized page."""
    font = ImageFont.truetype(font_name, size)
    lines = text.splitlines() or [""]
    ascent, descent = font.getmetrics()
    step = round((ascent + descent) * (1 + _LINE_SPACING))
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    width = max(
        (int(probe.textlength(line, font=font)) for line in lines if line),
        default=0,
    )
    page = Image.new(
        "RGB", (width + 2 * _PAD, step * len(lines) + 2 * _PAD), "white"
    )
    draw = ImageDraw.Draw(page)
    for i, line in enumerate(lines):
        draw.text((_PAD, _PAD + i * step), line, font=font, fill="black")
    return page


def render(corpus: str, outdir: str) -> Path:
    """Render every doc of a text corpus to PNG; write manifest.json."""
    corpus_path = Path(corpus)
    truth = json.loads((corpus_path / "truth.json").read_text("utf-8"))
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    # Seeded from the corpus seed (not the CLI flags), so a corpus always
    # renders identically no matter how the paths were spelled.
    rng = random.Random(f"render-{truth['seed']}")

    manifest = {
        "seed": truth["seed"],
        "source": Path(os.path.relpath(corpus_path, out)).as_posix(),
        # The DPI the pages were drawn for. score_pdf re-renders the assembled
        # PDFs at this value, which reproduces exactly these pixels.
        "dpi": _RENDER_DPI,
        "docs": [],
    }
    total_pages = 0
    for doc in truth["docs"]:
        text = (corpus_path / doc["file"]).read_text("utf-8")
        is_csv = doc["file"].endswith(".csv")
        if _is_fixed_column(doc["file"]):
            pool = MONO_FONTS
            if is_csv:
                text = format_csv_table(text)
        else:
            pool = MONO_FONTS + PROPORTIONAL_FONTS
        font_name = rng.choice(pool)
        size = rng.choice(FONT_SIZES)

        stem = Path(doc["file"]).stem
        page_texts = paginate(text, is_csv, _paginates(doc["file"]))
        images = [render_page(t, font_name, size) for t in page_texts]
        # Pages of one document share a raster size — a PDF page whose height
        # follows its content would make the analysis DPI differ per page.
        width = max(im.width for im in images)
        height = max(im.height for im in images)
        images = [_on_canvas(im, width, height) for im in images]

        names = []
        for number, image in enumerate(images, 1):
            name = f"{stem}.p{number}.png"
            image.save(out / name, dpi=(_RENDER_DPI, _RENDER_DPI))
            names.append(name)
        pdf_name = f"{stem}.pdf"
        write_pdf(images, out / pdf_name)
        total_pages += len(images)

        manifest["docs"].append(
            {"pages": names, "pdf": pdf_name, "source": doc["file"],
             "font": font_name, "size": size}
        )
        print(f"  rendered {stem} [{len(images)} page(s), {font_name} "
              f"{size}px {width}x{height}]")

    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8"
    )
    print(f"{len(manifest['docs'])} docs / {total_pages} pages -> {out}")
    return out


def _on_canvas(image: Image.Image, width: int, height: int) -> Image.Image:
    """Place a page on a uniform white canvas, top-left."""
    if image.size == (width, height):
        return image
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(image, (0, 0))
    return canvas
