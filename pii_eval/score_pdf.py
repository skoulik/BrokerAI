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
from pii_eval.build import CORPUS_KEEP_FILE, CRITICAL
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
    documents = list(_documents(corpus_path, manifest))
    dpi = manifest.get("dpi", 300)
    out_dir = corpus_path / "stripped"
    out_dir.mkdir(parents=True, exist_ok=True)

    ocr = reread_engine()
    vlm = build_detector(geometry)
    # The corpus's own keep list, not the shipped one: the keep axis must
    # measure the tool against what this generator emits (see
    # pii_eval/entity_keep.txt).
    pipeline = PiiPipeline(threshold=threshold,
                           invalid_identifiers=invalid_identifiers,
                           entity_keep=CORPUS_KEEP_FILE)

    all_entities = []
    all_invalid = []
    noise = []
    skipped_valueless = 0
    borrowed = 0
    for doc_id, source_pdf, truth_entities in documents:
        out_pdf = out_dir / f"{doc_id}.clean.pdf"
        result = strip_pdf(
            source_pdf, pipeline, PseudonymMap(), out_pdf,
            dpi=dpi, ocr_backend=ocr_backend, detector=vlm,
            geometry=geometry,
            progress=lambda n, c, phase, _id=doc_id: print(
                f"  {_id} page {n}/{c} {phase} ...", file=sys.stderr
            ),
        )
        reread = "\n".join(
            ocr(image).text for image in pdf_to_images(out_pdf, dpi=dpi)
        )
        findings = [f for p in result.pages for f in p.invalid]
        borrowed += sum(len(p.borrowed) for p in result.pages)

        entities = [e for e in truth_entities if e.get("value")]
        skipped_valueless += len(truth_entities) - len(entities)
        for e in entities:
            e["file"] = doc_id
            e.setdefault("critical", e["type"] in CRITICAL)
        inv_ents = [e for e in entities if e["type"] in INVALID_ENTITY_TYPES]
        reg_ents = [e for e in entities if e["type"] not in INVALID_ENTITY_TYPES]
        _score_survival(reg_ents, reread)
        _score_invalid(inv_ents, findings, reread)
        all_entities.extend(reg_ents)
        all_invalid.extend(inv_ents)
        noise.extend((doc_id, f) for f in _noise(findings, inv_ents))
        print(f"  scored {doc_id} ({len(result.pages)} pages, "
              f"{len(result.groups)} entity groups) <- {source_pdf.name}",
              file=sys.stderr)

    if skipped_valueless:
        print(f"  note: {skipped_valueless} valueless entities (barcodes) "
              "skipped — no value to match until barcode masking lands",
              file=sys.stderr)
    if borrowed:
        # The cross-page axis: spans a page owed to detections made on other
        # pages. Against --modality image over the same rendered pages (which
        # strips each one in isolation) this is the difference the two-sweep
        # pipeline makes, and it should show up as recall in the table above.
        print(f"  {borrowed} span(s) redacted from detections made elsewhere "
              f"in their document", file=sys.stderr)
    return summarize(all_entities, all_invalid, noise, invalid_identifiers)


def _documents(corpus_path: Path, manifest: dict):
    """Yield `(doc_id, source_pdf, entities)` for either corpus shape.

    A REAL corpus carries its own hand-authored truth.json beside the manifest
    and points `source` at the directory of original PDFs. A rendered
    synthetic corpus has no truth of its own — it belongs to the text corpus
    the pages were generated from — and its PDFs are the assembled page
    renders sitting in the corpus folder. That difference is the discriminator
    rather than a flag: whoever owns the truth owns the corpus shape.
    """
    own_truth = corpus_path / "truth.json"
    if own_truth.exists():
        truth = json.loads(own_truth.read_text("utf-8"))
        source_dir = Path(manifest["source"])
        for doc in truth["docs"]:
            yield doc["id"], source_dir / doc["source"], doc["entities"]
        return

    source = (corpus_path / manifest["source"]).resolve()
    truth = json.loads((source / "truth.json").read_text("utf-8"))
    by_file = {doc["file"]: doc for doc in truth["docs"]}
    for doc in manifest["docs"]:
        yield (
            Path(doc["source"]).stem,
            corpus_path / doc["pdf"],
            by_file[doc["source"]]["entities"],
        )
