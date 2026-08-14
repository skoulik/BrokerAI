"""CLI for the PII stripping tool.

    python -m pii strip document.txt -o document.clean.txt
    python -m pii strip statement.pdf --pdf -o statement.clean.pdf
    python -m pii strip statement.pdf --pdf -o statement.clean.pdf \
        --debug=ocr,layer-0,layer-1   # + statement.clean.debug.pdf
    python -m pii analyze document.txt
    python -m pii rehydrate cloud_answer.txt --map statement.pii_map.json

strip/analyze accept '-' to read stdin. The pseudonym map defaults to
per-document — <input>.pii_map.json next to the input file — so each
document gets independent placeholder numbering; pass --map explicitly to
share one map across documents (and always for rehydrate/stdin, where
there is no input document to derive it from). The map contains the
original PII — treat it as sensitive and never share it.
"""

import argparse
import os
import sys
from pathlib import Path

from pii.core import DEFAULT_STRIP_ENTITIES, PiiPipeline, PseudonymMap
from pii.core.debug_overlay import DEBUG_LAYERS, parse_layers
from pii.core.entity_keep import load_keep
from pii.core.ocr import OCR_PAGE_BACKENDS
# stdlib-only module, so importing it here costs nothing on the default path
from pii.core.vlm import (
    DEFAULT_GEOMETRY,
    DEFAULT_URL,
    GEOMETRIES,
    Incomplete,
    VlmError,
)


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


def _derive_debug_out(output_path: str) -> str:
    """Debug overlay base default: statement.clean.pdf ->
    statement.clean.debug.pdf, which `DebugSpec.paths` then turns into one file
    per layer (statement.clean.debug.locate.pdf, ...).

    Derived from the OUTPUT, not the input: the operator already chose where
    this run's artifacts land, and the set belongs together — a run of the
    same document with different flags overwrites its own overlays instead of
    the previous run's."""
    out = Path(output_path)
    return str(out.with_suffix(f".debug{out.suffix}"))


def _build_detector(args):
    """Construct the layer-0 detector for this run.

    There is no detector CHOICE any more — layer 0 is the only detector since
    GLiNER2 was retired (2026-08-09). What varies is the MODALITY, and that
    follows the input rather than a flag: --image/--pdf read page pixels,
    text and CSV read the string.

    `--layer0 off` turns the layer off entirely and is the one way to reach a
    patterns-only run. It is deliberately a detector OBJECT rather than a
    missing argument: the strip entry points still require one, so the regime
    retired 2026-07-15 stays unreachable by omission, and choosing it is an
    explicit, reported act. See `pii.core.vlm.NullDetector`.

    Imported lazily, so the import cost and the model-server dependency land
    only when a run actually reaches for one."""
    media = getattr(args, "image", False) or getattr(args, "pdf", False)
    geometry = getattr(args, "geometry", DEFAULT_GEOMETRY)
    url = getattr(args, "vlm_url", None) or DEFAULT_URL
    grammar = getattr(args, "grammar", True)

    if not media and geometry != DEFAULT_GEOMETRY:
        # Text and CSV have no page, so there is nothing for --geometry to
        # mean: values are located by finding them in the string itself.
        raise SystemExit(
            "--geometry applies to --image/--pdf only: text input has no "
            "page, so detected values are located in the text itself"
        )

    if getattr(args, "layer0", "auto") == "off":
        if geometry == "vlm":
            # That path never runs OCR, so with layer 0 silent there is no
            # text for layer 1 to read either: the run would detect nothing
            # at all and write out a copy of the input, which is the one
            # failure an operator could not see. Refuse rather than quietly
            # force a geometry they did not ask for.
            raise SystemExit(
                "--layer0 off cannot be combined with --geometry vlm: that "
                "path never runs OCR, so with no semantic detector there is "
                "no text for layer 1 either and the output would be an "
                "unredacted copy of the input"
            )
        from pii.core.vlm import NullDetector

        return NullDetector()

    if not media:
        from pii.core.text_llm import TextDetector

        return TextDetector(url=url, grammar=grammar)

    from pii.core.vlm import VlmDetector

    return VlmDetector(
        url=url,
        # The one-pass boxes prompt is only used where its boxes are painted
        # directly. Everywhere else geometry comes from the second pass
        # (VlmDetector.localize), which costs no recall.
        want_boxes=geometry == "vlm",
        grammar=grammar,
    )


def _warn_layer0_off(file=None) -> None:
    """Say that no semantic detector ran, before the run produces anything.

    NOT gated behind --report: what a run did not look for is not a reporting
    detail. Printed once at the top rather than beside the results, so an
    operator reading a plausible-looking list of redacted identifiers has
    already been told what is missing from it."""
    print(
        "WARNING: --layer0 off — no semantic detector ran. Only layer 1 "
        "(patterns and checksums) was applied, so identifiers are redacted "
        "but PERSON, ADDRESS, ORGANIZATION and DATE_OF_BIRTH are NOT "
        "detected. This output is a reduced redaction; do not treat it as "
        "safe to share.",
        file=file or sys.stderr,
    )


def _report(spans, text: str, file=None, prefix: str = "  ") -> None:
    # sys.stderr resolved at call time, not bound at import (capsys).
    file = file if file is not None else sys.stderr
    for r in spans:
        value = text[r.start : r.end].replace("\n", "\\n")
        print(f"{prefix}{r.entity_type:<20} {r.score:.2f}  {value!r}", file=file)


def _report_geometry(box_geometry, unlocated, file=None, prefix: str = "",
                     painted_elsewhere=()) -> None:
    """Report the lower-confidence outcomes of value location.

    Always printed when non-empty, independently of --report: these are a
    weaker redaction and no redaction at all, so neither may depend on the
    operator having asked for a detection listing.

    `painted_elsewhere` is the subset of `unlocated` whose value was painted
    somewhere on its page, and it gets its own line because the flat "NOT
    redacted" wording was wrong for it — the pixels were painted, by a
    first finding that had only the model's box and so left no char span to
    mark this one redundant. It is NOT reported as safe: two occurrences of a
    value can genuinely sit in two places, and only the operator can tell."""
    file = file if file is not None else sys.stderr
    if box_geometry:
        print(
            f"{prefix}{len(box_geometry)} value(s) painted from the model's "
            f"own box (no OCR text matched — logo/barcode/graphic?); geometry "
            f"is approximate and layer 1 never saw them",
            file=file,
        )
    unplaced = len(unlocated) - len(painted_elsewhere)
    if unplaced > 0:
        print(
            f"{prefix}WARNING: {unplaced} detected value(s) could not be "
            f"placed on the page and were NOT redacted",
            file=file,
        )
    if painted_elsewhere:
        print(
            f"{prefix}WARNING: {len(painted_elsewhere)} detected value(s) "
            f"could not be placed, but an identical value WAS painted "
            f"elsewhere on the page — check whether these are the same "
            f"printing or a second, still legible one",
            file=file,
        )


def _report_incomplete(incomplete, file=None, prefix: str = "") -> None:
    """Report reads where the model's answer did not finish.

    Always printed when non-zero, independently of --report, and worded
    differently from every other warning here on purpose: the others name a
    value that was not redacted, and this one cannot. What a cut-off answer
    would have gone on to say is unknowable, so the only honest report is that
    this run has a hole in it and where.

    Layer 0 is the sole detector for PERSON / ADDRESS / ORGANIZATION, so an
    affected page keeps its checksummed identifiers redacted by layer 1 and
    looks plausible while missing exactly the classes a reader notices."""
    file = file if file is not None else sys.stderr
    if incomplete.truncated:
        print(
            f"{prefix}WARNING: {incomplete.truncated} model response(s) were "
            f"cut off at the token budget. What had been reported by then was "
            f"kept, but names, addresses or organizations may be MISSING and "
            f"cannot be listed — re-run, or split the affected input",
            file=file,
        )
    if incomplete.malformed:
        print(
            f"{prefix}WARNING: {incomplete.malformed} model response(s) "
            f"carried no usable JSON array — same consequence as being cut "
            f"off. Unless --no-grammar was passed, this means the server "
            f"ignored the grammar; check that it is llama.cpp",
            file=file,
        )


def _report_groups(groups, file=None, prefix: str = "  ") -> None:
    """Print the document-wide entity groups and every constituent detection.

    Not decoration: the group's class is elected by majority vote across the
    document and replaces every member's own class, so the election can KEEP a
    value that some page reported as PII. This listing — the tally, then each
    surface form with the pages it was seen on — is what lets an operator see
    that decision and disagree with it. Near-PII like every other listing
    here, so stderr only."""
    file = file if file is not None else sys.stderr
    print(
        f"{len(groups)} entity group(s), class elected by majority vote "
        f"across the document:",
        file=file,
    )
    for group in groups:
        votes = " / ".join(f"{etype} {n}" for etype, n in group.votes)
        print(f"{prefix}{group.entity_type:<20} [{votes}]", file=file)
        for variant in group.variants:
            pages = " ".join(f"p{p}" for p in variant.pages)
            print(
                f"{prefix}  {variant.text!r:<44} x{variant.count}  {pages}",
                file=file,
            )


def _report_borrowed(count: int, file=None) -> None:
    """Spans a page owed to detections made elsewhere in the document.

    Always printed when non-zero, independently of --report: on a multi-page
    document these are the values that would have leaked before, which is the
    same class of fact as the two lines in _report_geometry."""
    if not count:
        return
    file = file if file is not None else sys.stderr
    print(
        f"{count} value(s) redacted from detections made elsewhere in the "
        f"document",
        file=file,
    )


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


def _debug_spec(args, detector):
    """Build the overlay request, minus anything this run cannot draw.

    Under `--layer0 off` the layer-0 and locate overlays have no placements to
    draw and would come out as unannotated copies of the original page — extra
    NEAR-PII files (that is what the debug warning is about) carrying nothing.
    Dropped rather than rendered blank, and said out loud rather than dropped
    quietly, so a shorter file list is explained instead of puzzling."""
    if not args.debug:
        return None
    from pii.core.debug_overlay import DebugSpec, drop_layer0_layers

    if getattr(detector, "layer0", None) != "off":
        return DebugSpec(layers=args.debug, path=args.debug_out)

    layers, dropped = drop_layer0_layers(args.debug)
    if dropped:
        print(
            f"note: no semantic detector ran, so the "
            f"{', '.join(dropped)} overlay(s) and the findings listing have "
            f"nothing to show and were not written",
            file=sys.stderr,
        )
    if not layers:
        # Every layer asked for needs layer 0. Writing nothing beats writing a
        # blank page, and the note above already said why.
        return None
    return DebugSpec(layers=layers, path=args.debug_out, findings=False)


def _debug_note(spec) -> None:
    """List the debug artifacts — and the warning that goes with all of them.

    The overlays are drawn on the ORIGINAL page, so they carry the very text
    the output does not. Warned once per run rather than once per file (four
    identical warnings train an operator to skip them), but every path is
    named: an operator who forgets which of these files is the safe one has a
    breach, not an inconvenience."""
    paths = spec.paths()
    listing = " + 1 findings listing" if spec.findings else ""
    print(
        f"wrote {len(paths)} debug overlay(s){listing} — NOT "
        f"redacted, they show the original page; keep them local, like the "
        f"map file:",
        file=sys.stderr,
    )
    for layer, path in paths:
        print(f"  {layer:<8} -> {path}", file=sys.stderr)
    if spec.findings:
        # Named apart from the layers because it is not one: it carries every
        # layer-0 finding, including the ones with no box, which no overlay can
        # draw. See pii.core.debug_overlay.findings_record.
        print(f"  {'findings':<8} -> {spec.findings_path()}", file=sys.stderr)


def _strip_media(args, pipeline, detector):
    """Handle --image / --pdf. Returns an exit code."""
    # Resolved before the run, so a dropped layer is reported with the other
    # start-of-run warnings rather than after minutes of detection.
    debug_spec = _debug_spec(args, detector)
    if getattr(args, "image", False):
        from PIL import Image

        from pii.core.image_mode import strip_image

        pmap = PseudonymMap(args.map)
        image = Image.open(args.input).convert("RGB")
        result = strip_image(image, pipeline, pmap,
                             ocr_backend=args.ocr_backend,
                             detector=detector,
                             geometry=getattr(args, "geometry", DEFAULT_GEOMETRY))
        result.image.save(args.output)
        pmap.save()
        if debug_spec is not None:
            # Drawn here rather than inside the core call: on a single page the
            # front-end still holds the pixels the run used, so there is no
            # cache to reach into (unlike --pdf, where strip_pdf owns them).
            from pii.core.debug_overlay import (
                draw_layers,
                findings_record,
                page_debug,
                write_findings,
            )

            record = page_debug(result)
            for layer, path in debug_spec.paths():
                draw_layers(image, record, [layer]).save(path)
            if debug_spec.findings:
                write_findings(
                    debug_spec.findings_path(), [findings_record(record)],
                    layer0=getattr(detector, "layer0", "on"),
                )
            _debug_note(debug_spec)
        if args.report:
            if result.ocr is not None:
                print(f"{len(result.spans)} entities detected:", file=sys.stderr)
                _report(result.spans, result.ocr.text)
            else:
                # --geometry vlm skips OCR: no text, no offsets, so the painted
                # segments are the only record of what was redacted.
                print(f"{len(result.segments)} entities detected:",
                      file=sys.stderr)
                for seg in result.segments:
                    print(f"  {seg.label}", file=sys.stderr)
            _report_groups(result.groups)
        _report_geometry(
            result.box_geometry, result.unlocated,
            painted_elsewhere=result.unlocated_painted_elsewhere,
        )
        _report_incomplete(result.incomplete)
        _report_borrowed(len(result.borrowed))
        if args.log_invalid_identifiers == "yes" and result.invalid:
            _report_invalid(result.invalid)
        return 0

    if getattr(args, "pdf", False):
        from pii.core.pdf_mode import DEFAULT_DPI, strip_pdf

        # Two sweeps over the document (see strip_pdf): the operator of a
        # minutes-per-page run needs to know which one is running.
        phases = {"read": "reading", "redact": "redacting"}

        def progress(number: int, count: int, phase: str) -> None:
            print(f"page {number}/{count} {phases.get(phase, phase)} ...",
                  file=sys.stderr)

        pmap = PseudonymMap(args.map)
        result = strip_pdf(args.input, pipeline, pmap, args.output,
                           dpi=args.dpi or DEFAULT_DPI,
                           ocr_backend=args.ocr_backend,
                           progress=progress,
                           detector=detector,
                           geometry=getattr(args, "geometry", DEFAULT_GEOMETRY),
                           debug=debug_spec)
        pmap.save()
        if debug_spec is not None:
            _debug_note(debug_spec)
        if args.report:
            total = sum(len(p.spans) for p in result.pages)
            print(f"{total} entities detected:", file=sys.stderr)
            for p in result.pages:
                if p.ocr is not None:
                    _report(p.spans, p.ocr.text, prefix=f"  p{p.number:<3} ")
                else:
                    for seg in p.segments:
                        print(f"  p{p.number:<3} {seg.label}", file=sys.stderr)
            _report_groups(result.groups)
        _report_geometry(
            [f for p in result.pages for f in p.box_geometry],
            [f for p in result.pages for f in p.unlocated],
            painted_elsewhere=[
                f for p in result.pages
                for f in p.unlocated_painted_elsewhere
            ],
        )
        # Per page as well as in total: the count says how much is missing,
        # the page numbers say where to look.
        affected = [p.number for p in result.pages if p.incomplete]
        if affected:
            print(
                f"pages with an unfinished model response: "
                f"{', '.join(str(n) for n in affected)}",
                file=sys.stderr,
            )
        # start= matters: an empty document would otherwise sum to the int 0.
        _report_incomplete(
            sum((p.incomplete for p in result.pages), Incomplete())
        )
        _report_borrowed(sum(len(p.borrowed) for p in result.pages))
        invalid = [f for p in result.pages for f in p.invalid]
        if args.log_invalid_identifiers == "yes" and invalid:
            _report_invalid(invalid)
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
        help="replace every organization name, ignoring the keep list",
    )
    p_strip.add_argument(
        "--entity-keep", metavar="FILE", default=None,
        help="values to keep unredacted: a file of one regex per line, in "
             "optional [ENTITY_TYPE] sections (default: $PII_ENTITY_KEEP, else "
             "the shipped list of institutions and common merchants). What is "
             "NOT matched is stripped — keeping is opt-in, because an unknown "
             "name may be the account holder's own company or trust",
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
        "--ocr-backend", choices=list(OCR_PAGE_BACKENDS), default="paddle",
        help="PaddleOCR model tier supplying the GEOMETRY in --image/--pdf "
             "modes (default paddle = PP-OCRv6_medium). Models download to "
             "models/paddlex on first use. On the GPU paddle wheel the engine "
             "runs in a worker subprocess (it cannot share a process with "
             "torch); the CPU wheel runs it in-process.",
    )
    p_strip.add_argument(
        "--geometry", choices=list(GEOMETRIES), default=DEFAULT_GEOMETRY,
        help="how detected values are placed on the page (--image/--pdf "
             "only): "
             "hybrid (default; a second model pass boxes each value, and those "
             "boxes constrain the search for it in the OCR text — painting "
             "still uses exact OCR word boxes, falling back to the model's own "
             "padded box only where there is no OCR text at all, as for a logo "
             "or a barcode), ocr (no second pass; search the whole page string "
             "for each value — the pre-box baseline), or vlm (paint the "
             "model's own boxes, OCR never runs; faster but measured UNSAFE — "
             "16%% of boxes clip by >20px, stochastically, so it is a "
             "comparison instrument, not a production option)",
    )
    p_strip.add_argument(
        "--layer0", choices=["auto", "off"], default="auto",
        help="whether the semantic detector runs. auto = yes, in the modality "
             "the input implies (pixels for --image/--pdf, text otherwise). "
             "off = skip it entirely and run layer 1 alone — no model server "
             "is contacted, which is one to two orders of magnitude faster "
             "and is meant for dry runs, low-sensitivity documents and "
             "debugging layer 1 in isolation. UNSAFE AS A DEFAULT: layer 1 is "
             "patterns and checksums, so identifiers are redacted but PERSON, "
             "ADDRESS, ORGANIZATION and DATE_OF_BIRTH are not detected at all "
             "(default: auto)",
    )
    p_strip.add_argument(
        "--vlm-url", default=None,
        help="llama-server base URL (default http://localhost:8080, or "
             "$PII_VLM_URL). Required by every strip mode — the local LLM is "
             "the detector — unless --layer0 off",
    )
    p_strip.add_argument(
        "--no-grammar", dest="grammar", action="store_false",
        help="do not constrain the model's output shape with a GBNF grammar. "
             "The grammar is on by default: it makes a markdown fence, a "
             "preamble or an invented entity class unrepresentable rather "
             "than something to parse around. Turn it off to compare "
             "detection quality, or for a server that does not support the "
             "grammar field",
    )
    p_strip.add_argument(
        "--debug", metavar="LAYERS", default=None,
        help="also write ANNOTATED copies of the page(s) beside the output, "
             "ONE FILE PER LAYER (--image/--pdf only): a comma-separated list "
             f"of {', '.join(DEBUG_LAYERS)}, or 'all' — one layer per pipeline "
             "stage. ocr = word and assembled line boxes; layer-0 = what the "
             "model named, its class on its own box (empty under --geometry "
             "ocr, which asks for no boxes); locate = where each finding was "
             "placed and by which tier (exact/squash/fuzzy/box/dup — a layer-0 "
             "box with nothing over it was placed by nothing, i.e. not "
             "redacted); layer-1 = the spans actually painted, their refined "
             "class and where each came from (L0/DOC/L1). The overlay is drawn "
             "on the ORIGINAL page and is NOT redacted — keep it local, like "
             "the map file",
    )
    p_strip.add_argument(
        "--debug-out", metavar="BASE", default=None,
        help="base path for the annotated copies; the layer name is inserted "
             "before the extension (default: the output path with '.debug' "
             "before its extension, e.g. statement.clean.debug.locate.pdf)",
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
        "analyze",
        help="show what strip would replace, without modifying anything",
    )
    p_analyze.add_argument("input", help="input text file, or - for stdin")
    p_analyze.add_argument("--threshold", type=float, default=0.4)
    p_analyze.add_argument(
        "--invalid-identifiers",
        choices=["ignore", "all", "likely", "context"], default="likely",
    )
    p_analyze.add_argument(
        "--layer0", choices=["auto", "off"], default="auto",
        help="whether the semantic detector runs; 'off' reports layer 1 alone "
             "and contacts no model server (see `strip --layer0`)",
    )
    p_analyze.add_argument(
        "--vlm-url", default=None,
        help="llama-server base URL (default http://localhost:8080, or "
             "$PII_VLM_URL)",
    )
    p_analyze.add_argument(
        "--no-grammar", dest="grammar", action="store_false",
        help="do not constrain the model's output shape with a GBNF grammar "
             "(see `strip --no-grammar`)",
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

    args = parser.parse_args(argv)

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
        if args.debug is not None:
            # Layers are parsed and the destination resolved here, before the
            # model server is touched: a typo'd layer name must not surface
            # after minutes of detection with the artifact already unwritable.
            if not (args.image or args.pdf):
                parser.error(
                    "--debug applies to --image/--pdf only: the overlay is "
                    "drawn on page pixels, and text input has no page"
                )
            try:
                args.debug = parse_layers(args.debug)
            except ValueError as exc:
                parser.error(str(exc))
            if not args.debug_out:
                args.debug_out = _derive_debug_out(args.output)
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

    # Where the keep list comes from is a front-end decision (pii.core reads no
    # environment); a bad path or a bad pattern in it must stop the run here,
    # before a document is processed against a list that is not what the
    # operator thinks it is.
    keep_path = (
        getattr(args, "entity_keep", None) or os.environ.get("PII_ENTITY_KEEP")
    )
    try:
        entity_keep = load_keep(keep_path)
    except ValueError as exc:
        raise SystemExit(f"pii: {exc}") from None
    if getattr(args, "strip_orgs", False):
        # Expressed as data: --strip-orgs simply drops that section, so
        # "ignore the keep list for organizations" needs no second code path.
        entity_keep = entity_keep.without("ORGANIZATION")
    pipeline = PiiPipeline(
        threshold=args.threshold,
        strip_entities=set(DEFAULT_STRIP_ENTITIES),
        invalid_identifiers=args.invalid_identifiers,
        mask_invalid=mask_invalid,
        entity_keep=entity_keep,
    )
    detector = _build_detector(args)
    if getattr(detector, "layer0", None) == "off":
        _warn_layer0_off()
    if getattr(args, "image", False) or getattr(args, "pdf", False):
        try:
            return _strip_media(args, pipeline, detector)
        except VlmError as exc:
            # A missing model server is an operator problem, not a bug —
            # report it as a message, not a traceback.
            raise SystemExit(f"pii: {exc}") from None

    text = _read(args.input)

    if args.command == "analyze":
        # The same detection strip runs, reported instead of applied — an
        # analyze that showed only layer 1 would understate what strip does
        # now that the semantic detector is layer 0.
        from pii.core.text_mode import detect_text

        try:
            spans, invalid, unlocated, incomplete = detect_text(
                text, pipeline, detector
            )
        except VlmError as exc:
            raise SystemExit(f"pii: {exc}") from None
        print(f"{len(spans)} entities would be replaced:")
        _report(spans, text, file=sys.stdout)
        if unlocated:
            print(
                f"WARNING: {len(unlocated)} detected value(s) were not found "
                f"in the text and could NOT be redacted", file=sys.stderr,
            )
        _report_incomplete(incomplete)
        # analyze has no --log-invalid-identifiers of its own; the findings
        # are the point of the command, so they always print.
        if invalid:
            _report_invalid(invalid)
        return 0

    pmap = PseudonymMap(args.map)
    try:
        if args.csv:
            from pii.core.csv_mode import strip_csv

            columns = args.columns.split(",") if args.columns else None
            result = strip_csv(
                text, pipeline, pmap, columns=columns, detector=detector
            )
        else:
            from pii.core.text_mode import strip_text

            result = strip_text(text, pipeline, pmap, detector=detector)
    except VlmError as exc:
        raise SystemExit(f"pii: {exc}") from None
    pmap.save()
    if args.report:
        if args.csv:
            print(f"{len(result.spans)} entities replaced", file=sys.stderr)
        else:
            print(f"{len(result.spans)} entities detected:", file=sys.stderr)
            _report(result.spans, text)
    if result.unlocated:
        # Always reported, independently of --report: these are detections
        # that were NOT redacted.
        print(
            f"WARNING: {len(result.unlocated)} detected value(s) were not "
            f"found in the text and were NOT redacted",
            file=sys.stderr,
        )
    _report_incomplete(result.incomplete)
    if args.log_invalid_identifiers == "yes" and result.invalid:
        _report_invalid(result.invalid)
    _write(args.output, result.text)
    return 0
