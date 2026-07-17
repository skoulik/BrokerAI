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


def _is_fixed_column(filename: str) -> bool:
    """Docs whose layout is carried by whitespace must render monospace."""
    return filename.endswith(".csv") or filename.startswith("legacy")


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
        "docs": [],
    }
    for doc in truth["docs"]:
        text = (corpus_path / doc["file"]).read_text("utf-8")
        if _is_fixed_column(doc["file"]):
            pool = MONO_FONTS
            if doc["file"].endswith(".csv"):
                text = format_csv_table(text)
        else:
            pool = MONO_FONTS + PROPORTIONAL_FONTS
        font_name = rng.choice(pool)
        size = rng.choice(FONT_SIZES)
        page = render_page(text, font_name, size)
        name = Path(doc["file"]).stem + ".png"
        page.save(out / name, dpi=(150, 150))
        manifest["docs"].append(
            {"file": name, "source": doc["file"], "font": font_name,
             "size": size}
        )
        print(f"  rendered {name} [{font_name} {size}px "
              f"{page.width}x{page.height}]")

    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8"
    )
    print(f"{len(manifest['docs'])} pages -> {out}")
    return out
