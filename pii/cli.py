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
        "--no-ner", action="store_true",
        help="skip the NER layer (patterns only, much faster)",
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
        "--columns",
        help="comma-separated column names to process (CSV mode; default all)",
    )

    p_analyze = sub.add_parser(
        "analyze", help="show detections without modifying anything"
    )
    p_analyze.add_argument("input", help="input text file, or - for stdin")
    p_analyze.add_argument("--no-ner", action="store_true")
    p_analyze.add_argument("--threshold", type=float, default=0.4)

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

    strip_entities = set(DEFAULT_STRIP_ENTITIES)
    if getattr(args, "strip_orgs", False):
        strip_entities.add("ORGANIZATION")
    pipeline = PiiPipeline(
        use_ner=not args.no_ner,
        threshold=args.threshold,
        strip_entities=strip_entities,
    )
    text = _read(args.input)

    if args.command == "analyze":
        _report(pipeline.analyze(text), text, file=sys.stdout)
        return 0

    pmap = PseudonymMap(args.map)
    if args.csv:
        from pii.csv_mode import strip_csv

        columns = args.columns.split(",") if args.columns else None
        stripped, spans = strip_csv(text, pipeline, pmap, columns=columns)
        pmap.save()
        if args.report:
            print(f"{len(spans)} entities replaced", file=sys.stderr)
    else:
        stripped, spans = pipeline.strip(text, pmap)
        pmap.save()
        if args.report:
            _report(spans, text)
    _write(args.output, stripped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
