"""Score the full PDF pipeline against a real-document corpus.

End-to-end over the actual product path: each source PDF goes through
`pii.core.pdf_mode.strip_pdf` (render -> OCR -> detect -> paint ->
reassemble), the stripped PDF's pages are rendered and OCR'd AGAIN, and
every truth entity is scored by value survival with the image tier's
OCR-tolerant matcher (`score_image.find_value`) — same recall-first
asymmetry, same acceptance-gate semantics.

Differences from the synthetic image tier:
- Truth is the hand-authored real-corpus truth.json (pii_eval.realdocs);
  entities carry no `critical` flag, so criticality derives from
  `build.CRITICAL` by type.
- Valueless entities (barcodes — no text to match) are skipped with a
  note; they get scored when barcode masking exists.
- One fresh PseudonymMap per document — the CLI's per-document map
  default (2026-07-18); cross-document consistency belongs to the future
  global/group map layers.
- Stripped PDFs are kept under <corpus>/stripped/ for eyeball review
  (the corpus is gitignored; outputs stay local like the corpus itself).

Expected on first runs: the keep-side table reports institutional
identities (bank names/ABNs/1300 numbers) as over-stripped — that is the
recorded keep-list gap (core/TODO.md), the axis working as designed.
"""

import json
import sys
from pathlib import Path

from pii.core import INVALID_ENTITY_TYPES, PiiPipeline, PseudonymMap
from pii.core.pdf_mode import pdf_to_images, strip_pdf
from pii.core.vlm import DEFAULT_GEOMETRY
from pii_eval.build import CRITICAL
from pii_eval.score_image import (
    _noise,
    _score_invalid,
    _score_survival,
    build_detector,
    reread_engine,
    summarize,
)


def score_pdf(corpus: str, threshold: float = 0.4,
              invalid_identifiers: str = "likely",
              ocr_backend: str = "paddle",
              geometry: str = DEFAULT_GEOMETRY) -> int:
    corpus_path = Path(corpus)
    manifest = json.loads((corpus_path / "manifest.json").read_text("utf-8"))
    truth = json.loads((corpus_path / "truth.json").read_text("utf-8"))
    source_dir = Path(manifest["source"])
    dpi = manifest.get("dpi", 300)
    out_dir = corpus_path / "stripped"
    out_dir.mkdir(parents=True, exist_ok=True)

    ocr = reread_engine()
    vlm = build_detector(geometry)
    pipeline = PiiPipeline(threshold=threshold,
                           invalid_identifiers=invalid_identifiers)

    all_entities = []
    all_invalid = []
    noise = []
    skipped_valueless = 0
    for doc in truth["docs"]:
        out_pdf = out_dir / f"{doc['id']}.clean.pdf"
        result = strip_pdf(
            source_dir / doc["source"], pipeline, PseudonymMap(), out_pdf,
            dpi=dpi, ocr_backend=ocr_backend, detector=vlm,
            geometry=geometry,
            progress=lambda n, c: print(f"  {doc['id']} page {n}/{c} ...",
                                        file=sys.stderr),
        )
        reread = "\n".join(
            ocr(image).text for image in pdf_to_images(out_pdf, dpi=dpi)
        )
        findings = [f for p in result.pages for f in p.invalid]

        entities = [e for e in doc["entities"] if e.get("value")]
        skipped_valueless += len(doc["entities"]) - len(entities)
        for e in entities:
            e["file"] = doc["id"]
            e.setdefault("critical", e["type"] in CRITICAL)
        inv_ents = [e for e in entities if e["type"] in INVALID_ENTITY_TYPES]
        reg_ents = [e for e in entities if e["type"] not in INVALID_ENTITY_TYPES]
        _score_survival(reg_ents, reread)
        _score_invalid(inv_ents, findings, reread)
        all_entities.extend(reg_ents)
        all_invalid.extend(inv_ents)
        noise.extend((doc["id"], f) for f in _noise(findings, inv_ents))
        print(f"  scored {doc['id']} ({len(result.pages)} pages) "
              f"<- {doc['source']}", file=sys.stderr)

    if skipped_valueless:
        print(f"  note: {skipped_valueless} valueless entities (barcodes) "
              "skipped — no value to match until barcode masking lands",
              file=sys.stderr)
    return summarize(all_entities, all_invalid, noise, invalid_identifiers)
