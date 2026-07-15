"""CLI for the PII stripping tool.

    python -m pii strip document.txt -o document.clean.txt --map map.json
    python -m pii analyze document.txt
    python -m pii rehydrate cloud_answer.txt --map map.json

strip/analyze accept '-' to read stdin. The mapping file accumulates across
runs, keeping placeholders consistent over a whole document set. It contains
the original PII — treat it as sensitive and never share it.
"""

import argparse
import sys
from pathlib import Path

from pii.mapping import PseudonymMap
from pii.pipeline import DEFAULT_STRIP_ENTITIES, PiiPipeline


def _read(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8", errors="replace")


def _write(dest: str | None, text: str) -> None:
    if dest is None or dest == "-":
        sys.stdout.write(text)
    else:
        Path(dest).write_text(text, encoding="utf-8")


def _report(spans, text: str, file=sys.stderr) -> None:
    print(f"{len(spans)} entities detected:", file=file)
    for r in spans:
        value = text[r.start : r.end].replace("\n", "\\n")
        print(f"  {r.entity_type:<20} {r.score:.2f}  {value!r}", file=file)


def _report_invalid(findings, file=sys.stderr) -> None:
    # Near-PII (a typo'd TFN is a real TFN minus a digit) — stderr only,
    # treat any capture of it as a local-only artifact like the map file.
    print(
        f"{len(findings)} checksum-invalid identifier candidate(s) "
        "(typo / OCR error / forgery?):",
        file=file,
    )
    for f in findings:
        value = f.value.replace("\n", "\\n")
        print(f"  {f.entity_type:<22} {value!r}  [{f.rule}]", file=file)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pii", description="Local PII stripping tool"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_strip = sub.add_parser("strip", help="replace PII with placeholders")
    p_strip.add_argument("input", help="input text file, or - for stdin")
    p_strip.add_argument("-o", "--output", help="output file (default stdout)")
    p_strip.add_argument(
        "--map", default="pii_map.json",
        help="pseudonym mapping store, created/extended (default pii_map.json)",
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
    p_rehyd.add_argument("--map", default="pii_map.json")

    args = parser.parse_args(argv)

    if args.command == "rehydrate":
        pmap = PseudonymMap(args.map)
        if len(pmap) == 0:
            print(f"warning: mapping {args.map} is empty or missing", file=sys.stderr)
        _write(args.output, pmap.rehydrate(_read(args.input)))
        return 0

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
        if args.csv:
            parser.error("--image and --csv are mutually exclusive")
        if not args.output or args.output == "-":
            parser.error("--image requires -o OUTPUT (an image file path)")
        from PIL import Image

        from pii.image_mode import strip_image

        pmap = PseudonymMap(args.map)
        result = strip_image(Image.open(args.input), pipeline, pmap)
        result.image.save(args.output)
        pmap.save()
        if args.report:
            _report(result.spans, result.ocr.text)
        if args.log_invalid_identifiers == "yes" and result.invalid:
            _report_invalid(result.invalid)
        return 0

    text = _read(args.input)

    if args.command == "analyze":
        _report(pipeline.analyze(text), text, file=sys.stdout)
        return 0

    pmap = PseudonymMap(args.map)
    if args.csv:
        from pii.csv_mode import strip_csv

        columns = args.columns.split(",") if args.columns else None
        stripped, spans, invalid = strip_csv(text, pipeline, pmap, columns=columns)
        pmap.save()
        if args.report:
            print(f"{len(spans)} entities replaced", file=sys.stderr)
    else:
        stripped, spans, invalid = pipeline.strip(text, pmap)
        pmap.save()
        if args.report:
            _report(spans, text)
    if args.log_invalid_identifiers == "yes" and invalid:
        _report_invalid(invalid)
    _write(args.output, stripped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
