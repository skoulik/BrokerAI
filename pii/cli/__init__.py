"""CLI for the PII stripping tool.

    python -m pii strip document.txt -o document.clean.txt
    python -m pii strip statement.pdf --pdf -o statement.clean.pdf
    python -m pii analyze document.txt
    python -m pii rehydrate cloud_answer.txt --map statement.pii_map.json
    python -m pii debug ocr statement.pdf --format overlay -o ocr.png

strip/analyze accept '-' to read stdin. The pseudonym map defaults to
per-document — <input>.pii_map.json next to the input file — so each
document gets independent placeholder numbering; pass --map explicitly to
share one map across documents (and always for rehydrate/stdin, where
there is no input document to derive it from). The map contains the
original PII — treat it as sensitive and never share it.
"""

import argparse
import sys
from pathlib import Path

from pii.core import DEFAULT_STRIP_ENTITIES, PiiPipeline, PseudonymMap
from pii.core.ocr import OCR_BACKENDS, OCR_PAGE_BACKENDS


def _read(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8", errors="replace")


def _write(dest: str | None, text: str) -> None:
    if dest is None or dest == "-":
        sys.stdout.write(text)
    else:
        Path(dest).write_text(text, encoding="utf-8")


def _derive_map(input_path: str) -> str:
    """Per-document map default: statement.pdf -> statement.pii_map.json,
    next to the input document."""
    return str(Path(input_path).with_suffix(".pii_map.json"))


def _report(spans, text: str, file=None, prefix: str = "  ") -> None:
    # sys.stderr resolved at call time, not bound at import (capsys).
    file = file if file is not None else sys.stderr
    for r in spans:
        value = text[r.start : r.end].replace("\n", "\\n")
        print(f"{prefix}{r.entity_type:<20} {r.score:.2f}  {value!r}", file=file)


def _report_invalid(findings, file=None) -> None:
    # Near-PII (a typo'd TFN is a real TFN minus a digit) — stderr only,
    # treat any capture of it as a local-only artifact like the map file.
    file = file if file is not None else sys.stderr
    print(
        f"{len(findings)} checksum-invalid identifier candidate(s) "
        "(typo / OCR error / forgery?):",
        file=file,
    )
    for f in findings:
        value = f.value.replace("\n", "\\n")
        print(f"  {f.entity_type:<22} {value!r}  [{f.rule}]", file=file)


def _debug_page_images(path: str, is_pdf: bool, page, dpi: int):
    """Yield (page_number, RGB image) for the selected pages: a single image
    file, one PDF page (`--page N`), or all PDF pages (`--page` unset)."""
    from PIL import Image

    if not is_pdf:
        yield 1, Image.open(path).convert("RGB")
        return
    import pymupdf

    from pii.core.pdf_mode import _render_page

    with pymupdf.open(path) as doc:
        if page is not None:
            if not 1 <= page <= doc.page_count:
                raise SystemExit(
                    f"page {page} out of range (1..{doc.page_count})")
            yield page, _render_page(doc[page - 1], dpi)
        else:
            for number, pg in enumerate(doc, 1):
                yield number, _render_page(pg, dpi)


def _page_progress(number: int, count: int) -> None:
    print(f"page {number}/{count} ...", file=sys.stderr)


def _debug(args) -> int:
    """`pii debug ocr`: OCR the selected page(s) into OcrPage(s) and dump them
    (json/text) or annotate the raster(s) (overlay). PDFs default to all pages;
    overlay to a `.pdf` output reconstructs a fresh image-only PDF like strip."""
    import json
    from dataclasses import replace

    from pii.core import ocr_debug
    from pii.core.ocr import get_ocr_page

    ocr_fn = get_ocr_page(args.ocr_backend)
    is_pdf = args.input.lower().endswith(".pdf")
    out_is_pdf = bool(args.output) and args.output.lower().endswith(".pdf")

    def ocr_of(number, image):
        # The engine can't know the source path / PDF page / render dpi —
        # record them on the frame for the dump.
        page = ocr_fn(image)
        return replace(page, frame=replace(
            page.frame,
            page=number if is_pdf else 1,
            dpi=args.dpi if is_pdf else None,
            source=args.input,
        ))

    if args.format == "overlay":
        if is_pdf and out_is_pdf:
            from pii.core.pdf_mode import rebuild_pdf
            rebuild_pdf(
                args.input, args.output,
                lambda n, im: ocr_debug.draw_overlay(im, ocr_of(n, im)),
                dpi=args.dpi,
                pages=None if args.page is None else {args.page},
                progress=_page_progress,
            )
            print(f"wrote overlay PDF -> {args.output}", file=sys.stderr)
            return 0
        imgs = list(_debug_page_images(args.input, is_pdf, args.page, args.dpi))
        if len(imgs) != 1:
            raise SystemExit(
                "overlay to an image needs a single page — pass --page N, or "
                "give a .pdf output path to annotate all pages")
        number, image = imgs[0]
        ocr_debug.draw_overlay(image, ocr_of(number, image)).save(args.output)
        print(f"wrote overlay -> {args.output}", file=sys.stderr)
        return 0

    pages = [ocr_of(n, im) for n, im
             in _debug_page_images(args.input, is_pdf, args.page, args.dpi)]
    if args.format == "text":
        _write(args.output, "\n".join(ocr_debug.page_to_text(p) for p in pages))
    else:
        payload = (ocr_debug.page_to_dict(pages[0]) if len(pages) == 1
                   else [ocr_debug.page_to_dict(p) for p in pages])
        _write(args.output,
               json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pii", description="Local PII stripping tool"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_strip = sub.add_parser("strip", help="replace PII with placeholders")
    p_strip.add_argument("input", help="input text file, or - for stdin")
    p_strip.add_argument("-o", "--output", help="output file (default stdout)")
    p_strip.add_argument(
        "--map", default=None,
        help="pseudonym mapping store, created/extended (default: "
             "per-document — <input>.pii_map.json next to the input file; "
             "required for stdin input). Pass one path across runs to keep "
             "placeholders consistent over a document set.",
    )
    p_strip.add_argument(
        "--strip-orgs", action="store_true",
        help="also replace organization names (kept by default)",
    )
    p_strip.add_argument("--threshold", type=float, default=0.4)
    p_strip.add_argument(
        "--report", action="store_true",
        help="list applied detections on stderr",
    )
    p_strip.add_argument(
        "--csv", action="store_true",
        help="treat input as CSV: detect per cell, preserve structure",
    )
    p_strip.add_argument(
        "--image", action="store_true",
        help="treat input as an image: OCR, detect on the recognized text, "
             "paint placeholders over the PII pixels (requires -o; output "
             "format follows the file extension)",
    )
    p_strip.add_argument(
        "--pdf", action="store_true",
        help="treat input as a PDF: render each page to pixels, run the "
             "image path on it, and reassemble a fresh image-only PDF — "
             "no text layer, annotations or metadata from the source "
             "survive (requires -o)",
    )
    p_strip.add_argument(
        "--dpi", type=int, default=None,
        help="page render resolution for --pdf mode (default 300)",
    )
    p_strip.add_argument(
        "--ocr-backend", choices=list(OCR_BACKENDS), default="paddle",
        help="OCR engine for --image/--pdf modes (PaddleOCR; default paddle "
             "= the v6_medium tier). Variants name a model tier, downloaded "
             "to models/paddlex on first use. On the GPU paddle wheel the "
             "engine runs in a worker subprocess (it cannot share a process "
             "with the NER model); the CPU wheel runs it in-process.",
    )
    p_strip.add_argument(
        "--columns",
        help="comma-separated column names to process (CSV mode; default all)",
    )
    p_strip.add_argument(
        "--invalid-identifiers",
        choices=["ignore", "all", "likely", "context"], default="likely",
        help="which checksum-rejected identifier candidates to collect: "
             "ignore; likely = evidence inside the span (canonical digit "
             "grouping or an adjacent label); context = also bare digit "
             "runs promoted by nearby context words; all = every failing "
             "pattern match (noisy) (default: likely)",
    )
    p_strip.add_argument(
        "--log-invalid-identifiers", choices=["yes", "no"], default="yes",
        help="list collected checksum-invalid candidates on stderr — the "
             "list is near-PII, keep it local like the map file "
             "(default: yes)",
    )
    p_strip.add_argument(
        "--mask-invalid-identifiers", choices=["yes", "no"], default="no",
        help="also pseudonymize collected candidates (placeholder classes "
             "TFN_INVALID_n, MEDICARE_MALFORMED_n, ...) (default: no)",
    )

    p_analyze = sub.add_parser(
        "analyze", help="show detections without modifying anything"
    )
    p_analyze.add_argument("input", help="input text file, or - for stdin")
    p_analyze.add_argument("--threshold", type=float, default=0.4)
    p_analyze.add_argument(
        "--invalid-identifiers",
        choices=["ignore", "all", "likely", "context"], default="likely",
    )

    p_rehyd = sub.add_parser(
        "rehydrate", help="restore original values in a cloud response"
    )
    p_rehyd.add_argument("input", help="input text file, or - for stdin")
    p_rehyd.add_argument("-o", "--output", help="output file (default stdout)")
    p_rehyd.add_argument(
        "--map", required=True,
        help="the pseudonym map of the document the cloud answer is about "
             "(maps are per-document by default, so there is no safe "
             "default to guess here)",
    )

    p_debug = sub.add_parser(
        "debug", help="diagnostics: inspect the OCR perception layer"
    )
    debug_sub = p_debug.add_subparsers(dest="debug_command", required=True)
    p_debug_ocr = debug_sub.add_parser(
        "ocr", help="OCR a page and dump/annotate the OcrPage "
                    "(blocks, lines, reading order)"
    )
    p_debug_ocr.add_argument("input", help="image or PDF file")
    p_debug_ocr.add_argument(
        "-o", "--output",
        help="output file (default stdout; required for --format overlay)",
    )
    p_debug_ocr.add_argument(
        "--format", choices=["json", "text", "overlay"], default="json",
        help="json (round-trippable OcrPage), text (human summary), or "
             "overlay (annotated raster; requires -o) (default json)",
    )
    p_debug_ocr.add_argument(
        "--ocr-backend", choices=list(OCR_PAGE_BACKENDS), default="ppstructure",
        help="OcrPage backend: ppstructure (PP-StructureV3 — typed blocks + "
             "reading order) or the paddle line-only tiers (default "
             "ppstructure)",
    )
    p_debug_ocr.add_argument(
        "--page", type=int, default=None,
        help="1-based page number for PDF input (default: all pages)",
    )
    p_debug_ocr.add_argument(
        "--dpi", type=int, default=200,
        help="PDF render resolution (default 200)",
    )

    args = parser.parse_args(argv)

    if args.command == "debug":
        if (args.debug_command == "ocr" and args.format == "overlay"
                and (not args.output or args.output == "-")):
            parser.error("--format overlay requires -o OUTPUT (an image path)")
        return _debug(args)

    if args.command == "rehydrate":
        pmap = PseudonymMap(args.map)
        if len(pmap) == 0:
            print(f"warning: mapping {args.map} is empty or missing", file=sys.stderr)
        _write(args.output, pmap.rehydrate(_read(args.input)))
        return 0

    # Validate mode combinations and resolve the map path before any
    # heavy pipeline construction, so bad invocations fail instantly.
    if args.command == "strip":
        if sum([args.csv, args.image, args.pdf]) > 1:
            parser.error("--csv, --image and --pdf are mutually exclusive")
        if args.image and (not args.output or args.output == "-"):
            parser.error("--image requires -o OUTPUT (an image file path)")
        if args.pdf and (not args.output or args.output == "-"):
            parser.error("--pdf requires -o OUTPUT (a PDF file path)")
        if args.map is None:
            if args.input == "-":
                parser.error(
                    "--map is required when reading stdin (no input "
                    "filename to derive the per-document map from)"
                )
            args.map = _derive_map(args.input)

    mask_invalid = getattr(args, "mask_invalid_identifiers", "no") == "yes"
    if mask_invalid and args.invalid_identifiers == "all":
        print(
            "warning: --mask-invalid-identifiers=yes with "
            "--invalid-identifiers=all pseudonymizes most reference/receipt "
            "numbers (~90% of random 9-digit runs fail the TFN checksum) "
            "and guts analytical utility",
            file=sys.stderr,
        )

    strip_entities = set(DEFAULT_STRIP_ENTITIES)
    if getattr(args, "strip_orgs", False):
        strip_entities.add("ORGANIZATION")
    pipeline = PiiPipeline(
        threshold=args.threshold,
        strip_entities=strip_entities,
        invalid_identifiers=args.invalid_identifiers,
        mask_invalid=mask_invalid,
    )
    if getattr(args, "image", False):
        from PIL import Image

        from pii.core.image_mode import strip_image

        pmap = PseudonymMap(args.map)
        result = strip_image(Image.open(args.input), pipeline, pmap,
                             ocr_backend=args.ocr_backend)
        result.image.save(args.output)
        pmap.save()
        if args.report:
            print(f"{len(result.spans)} entities detected:", file=sys.stderr)
            _report(result.spans, result.ocr.text)
        if args.log_invalid_identifiers == "yes" and result.invalid:
            _report_invalid(result.invalid)
        return 0

    if getattr(args, "pdf", False):
        from pii.core.pdf_mode import DEFAULT_DPI, strip_pdf

        def progress(number: int, count: int) -> None:
            print(f"page {number}/{count} ...", file=sys.stderr)

        pmap = PseudonymMap(args.map)
        result = strip_pdf(args.input, pipeline, pmap, args.output,
                           dpi=args.dpi or DEFAULT_DPI,
                           ocr_backend=args.ocr_backend,
                           progress=progress)
        pmap.save()
        if args.report:
            total = sum(len(p.spans) for p in result.pages)
            print(f"{total} entities detected:", file=sys.stderr)
            for p in result.pages:
                _report(p.spans, p.ocr.text, prefix=f"  p{p.number:<3} ")
        invalid = [f for p in result.pages for f in p.invalid]
        if args.log_invalid_identifiers == "yes" and invalid:
            _report_invalid(invalid)
        return 0

    text = _read(args.input)

    if args.command == "analyze":
        spans = pipeline.analyze(text)
        print(f"{len(spans)} entities detected:")
        _report(spans, text, file=sys.stdout)
        return 0

    pmap = PseudonymMap(args.map)
    if args.csv:
        from pii.core.csv_mode import strip_csv

        columns = args.columns.split(",") if args.columns else None
        stripped, spans, invalid = strip_csv(text, pipeline, pmap, columns=columns)
        pmap.save()
        if args.report:
            print(f"{len(spans)} entities replaced", file=sys.stderr)
    else:
        stripped, spans, invalid = pipeline.strip(text, pmap)
        pmap.save()
        if args.report:
            print(f"{len(spans)} entities detected:", file=sys.stderr)
            _report(spans, text)
    if args.log_invalid_identifiers == "yes" and invalid:
        _report_invalid(invalid)
    _write(args.output, stripped)
    return 0
