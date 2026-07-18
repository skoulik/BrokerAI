"""Real-document image corpus: import PDFs, render ground-truth markup.

Unlike the synthetic tiers there is no generated text source: pages are
rendered straight from real PDFs (pii.core.pdf_mode) and ground truth is
*authored* — a human/Claude reads every page and records each entity as
type + value + pixel boxes in truth.json — rather than emitted by a
builder. Documents get neutral ids (d01, d02, ...) so no original
filename (some embed account numbers) leaks into page names; the mapping
back to sources lives in manifest.json. The corpus sits under
pii_eval/corpora/real/<set>/ — gitignored, sources are sensitive.

truth.json format:
    {"docs": [{"id": "d01", "source": "....pdf",
               "entities": [{"type": "PERSON", "value": "J CITIZEN",
                             "strip_expected": true,
                             "boxes": [{"page": "d01.p1.png", "left": ...,
                                        "top": ..., "width": ...,
                                        "height": ...}]}]}]}
One entry per (type, value) per document, its boxes covering every
occurrence on every page. Non-text regions carrying identity (barcodes)
use a valueless entry: value null, boxes only.

`render_gt` paints the markup itself through the production painting
seam (pii.core.image_mode.paint_segments) with one shared PseudonymMap
across the whole corpus — the output is what a *perfect* pipeline run
would produce. Two uses: eyeball review of markup completeness (anything
identifying still readable is a markup gap), and later the reference
against the real pipeline's output.
"""

import json
from collections import defaultdict
from pathlib import Path

from PIL import Image

from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box
from pii.core.pdf_mode import DEFAULT_DPI, pdf_to_images


def import_pdfs(src: str, out: str, dpi: int = DEFAULT_DPI) -> Path:
    """Render every PDF page of a source folder into <out>/pages; write
    manifest.json. Idempotent overwrite: same sources -> same page files."""
    src_path = Path(src)
    out_path = Path(out)
    pages_dir = out_path / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    manifest = {"source": src_path.as_posix(), "dpi": dpi, "docs": []}
    pdfs = sorted(src_path.glob("*.pdf"), key=lambda p: p.name.lower())
    for i, pdf in enumerate(pdfs, 1):
        doc_id = f"d{i:02}"
        pages = []
        for pno, image in enumerate(pdf_to_images(pdf, dpi=dpi), 1):
            name = f"{doc_id}.p{pno}.png"
            image.save(pages_dir / name)
            pages.append(name)
        manifest["docs"].append(
            {"id": doc_id, "source": pdf.name, "pages": pages}
        )
        print(f"  {doc_id}: {len(pages)} pages <- {pdf.name}")
    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8"
    )
    print(f"{len(manifest['docs'])} documents -> {out_path}")
    return out_path


def render_gt(corpus: str) -> Path:
    """Paint the ground-truth markup onto the corpus pages -> <corpus>/
    gt_render. One PseudonymMap across all documents (a submission bundle
    shares pseudonym identity); valueless entities (barcodes) are painted
    with their bare type as the label. Rendered in the reviewable "frame"
    style — outline + label chip, content readable underneath — so markup
    gaps and box errors are visible (Sergei, 2026-07-17)."""
    from pii.core.image_mode import Segment, paint_segments

    corpus_path = Path(corpus)
    truth = json.loads((corpus_path / "truth.json").read_text("utf-8"))
    manifest = json.loads((corpus_path / "manifest.json").read_text("utf-8"))
    pages_of = {d["id"]: d["pages"] for d in manifest["docs"]}
    out = corpus_path / "gt_render"
    out.mkdir(parents=True, exist_ok=True)

    pmap = PseudonymMap()
    for doc in truth["docs"]:
        by_page: dict[str, list[Segment]] = defaultdict(list)
        for ent in doc["entities"]:
            if not ent.get("strip_expected", True):
                continue
            label = (
                pmap.placeholder_for(ent["type"], ent["value"])
                if ent.get("value")
                else ent["type"]
            )
            for b in ent["boxes"]:
                by_page[b["page"]].append(
                    Segment(
                        label,
                        [Box(b["left"], b["top"], b["width"], b["height"])],
                    )
                )
        # Every page lands in gt_render — untouched pages pass through as
        # copies, so the render is a complete reviewable document set.
        for page_name in pages_of[doc["id"]]:
            page = Image.open(corpus_path / "pages" / page_name)
            segments = by_page.get(page_name, [])
            paint_segments(page, segments, style="frame").save(out / page_name)
            print(f"  {page_name}: {len(segments)} boxes framed")
    pmap.save(out / "pseudonyms.json")
    print(f"gt render -> {out}")
    return out
