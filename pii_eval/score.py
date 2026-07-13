"""Score the pii pipeline against a generated corpus.

Recall-first, severity-weighted (ROADMAP Tier 1): the headline number per
entity type is coverage — was the PII value fully removed from the output?
Text documents are scored by span coverage against the applied replacement
spans (catches partial leaks); CSV documents by per-cell value survival.
Keep-types (ORGANIZATION) score the opposite direction: over-stripping.

Exit code 1 if any critical-type PII leaked (the acceptance gate).
"""

import csv
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from pii.csv_mode import strip_csv
from pii.mapping import PseudonymMap
from pii.pipeline import PiiPipeline


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _score_text(entities, spans, stripped):
    """Span-coverage verdicts: covered / partial / leaked per truth entity."""
    covered = sorted((s.start, s.end) for s in spans)
    for e in entities:
        if not e["strip_expected"]:
            e["verdict"] = (
                "kept" if _norm(e["value"]) in _norm(stripped) else "over-stripped"
            )
            continue
        overlap = sum(
            min(e["end"], s1) - max(e["start"], s0)
            for s0, s1 in covered
            if s0 < e["end"] and s1 > e["start"]
        )
        span_len = e["end"] - e["start"]
        e["verdict"] = (
            "stripped" if overlap >= span_len
            else "partial" if overlap > 0
            else "leaked"
        )


def _score_csv(entities, stripped):
    rows = list(csv.reader(io.StringIO(stripped)))
    for e in entities:
        try:
            cell = rows[e["row"]][e["col"]]
        except IndexError:
            cell = ""
        survives = _norm(e["value"]) in _norm(cell)
        if not e["strip_expected"]:
            e["verdict"] = "kept" if survives else "over-stripped"
        else:
            e["verdict"] = "leaked" if survives else "stripped"


def score(corpus: str, use_ner: bool = True,
          threshold: float = 0.4) -> int:
    corpus_path = Path(corpus)
    manifest = json.loads((corpus_path / "truth.json").read_text("utf-8"))
    pipeline = PiiPipeline(use_ner=use_ner, threshold=threshold)

    all_entities = []
    for doc in manifest["docs"]:
        text = (corpus_path / doc["file"]).read_text("utf-8")
        pmap = PseudonymMap()
        if doc["kind"] == "csv":
            stripped, _ = strip_csv(text, pipeline, pmap, columns=["Description"])
            _score_csv(doc["entities"], stripped)
        else:
            stripped, spans = pipeline.strip(text, pmap)
            _score_text(doc["entities"], spans, stripped)
        for e in doc["entities"]:
            e["file"] = doc["file"]
        all_entities.extend(doc["entities"])
        print(f"  scored {doc['file']}", file=sys.stderr)

    by_type = defaultdict(lambda: defaultdict(int))
    for e in all_entities:
        by_type[e["type"]][e["verdict"]] += 1

    print(f"\n{'entity type':<20}{'n':>5}{'stripped':>10}{'partial':>9}"
          f"{'leaked':>8}{'recall':>8}")
    strip_types = sorted(
        t for t, c in by_type.items() if c["kept"] + c["over-stripped"] == 0
    )
    for t in strip_types:
        c = by_type[t]
        n = sum(c.values())
        recall = c["stripped"] / n if n else 0.0
        print(f"{t:<20}{n:>5}{c['stripped']:>10}{c['partial']:>9}"
              f"{c['leaked']:>8}{recall:>8.0%}")

    keep_types = sorted(set(by_type) - set(strip_types))
    if keep_types:
        print(f"\n{'keep-type':<20}{'n':>5}{'kept':>10}{'over-stripped':>15}")
        for t in keep_types:
            c = by_type[t]
            print(f"{t:<20}{sum(c.values()):>5}{c['kept']:>10}"
                  f"{c['over-stripped']:>15}")

    failures = [
        e for e in all_entities
        if e["critical"] and e["verdict"] in ("leaked", "partial")
    ]
    if failures:
        print(f"\nCRITICAL LEAKS ({len(failures)}):")
        for e in failures:
            print(f"  {e['file']}: {e['type']} {e['verdict']}: {e['value']!r}")
        return 1
    print("\nAcceptance gate: PASS (zero critical misses)")
    return 0
