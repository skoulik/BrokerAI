"""Score the pii pipeline against a generated corpus.

Recall-first, severity-weighted (pii/ROADMAP.md, Tier 1): the headline number per
entity type is coverage — was the PII value fully removed from the output?
Text documents are scored by span coverage against the applied replacement
spans (catches partial leaks); CSV documents by per-cell value survival.
Keep-types (ORGANIZATION) score the opposite direction: over-stripping.

Injected checksum-invalid identifiers (types in
pii.invalid_recognizers.INVALID_ENTITY_TYPES) are scored on their own
axes: collection ("logged"/"missed" against the pipeline's invalid
findings, broken down by the annotation's evidence tier), leak risk at
mask=no ("stripped-anyway" — did another layer remove the mangled value?),
and the noise floor (findings matching no injected entity).

Exit code 1 if any critical-type PII leaked (the acceptance gate).
"""

import csv
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from pii.core import INVALID_ENTITY_TYPES, PiiPipeline, PseudonymMap
from pii.core.csv_mode import strip_csv


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


def _score_invalid_text(entities, findings, spans):
    covered = sorted((s.start, s.end) for s in spans)
    for e in entities:
        e["verdict"] = (
            "logged"
            if any(
                f.entity_type == e["type"]
                and f.start < e["end"] and e["start"] < f.end
                for f in findings
            )
            else "missed"
        )
        overlap = sum(
            min(e["end"], s1) - max(e["start"], s0)
            for s0, s1 in covered
            if s0 < e["end"] and s1 > e["start"]
        )
        e["stripped_anyway"] = overlap >= e["end"] - e["start"]


def _score_invalid_csv(entities, findings, stripped):
    rows = list(csv.reader(io.StringIO(stripped)))
    for e in entities:
        e["verdict"] = (
            "logged"
            if any(
                f.entity_type == e["type"] and _norm(e["value"]) in _norm(f.value)
                for f in findings
            )
            else "missed"
        )
        try:
            cell = rows[e["row"]][e["col"]]
        except IndexError:
            cell = ""
        e["stripped_anyway"] = _norm(e["value"]) not in _norm(cell)


def _noise(findings, inv_entities, kind):
    """Findings that match no injected invalid entity — the log noise."""
    out = []
    for f in findings:
        if kind == "csv":
            matched = any(
                _norm(e["value"]) in _norm(f.value) for e in inv_entities
            )
        else:
            matched = any(
                f.start < e["end"] and e["start"] < f.end for e in inv_entities
            )
        if not matched:
            out.append(f)
    return out


def score(corpus: str, threshold: float = 0.4,
          invalid_identifiers: str = "likely") -> int:
    corpus_path = Path(corpus)
    manifest = json.loads((corpus_path / "truth.json").read_text("utf-8"))
    pipeline = PiiPipeline(threshold=threshold,
                           invalid_identifiers=invalid_identifiers)

    all_entities = []
    all_invalid = []
    noise = []
    for doc in manifest["docs"]:
        text = (corpus_path / doc["file"]).read_text("utf-8")
        pmap = PseudonymMap()
        inv_ents = [e for e in doc["entities"]
                    if e["type"] in INVALID_ENTITY_TYPES]
        reg_ents = [e for e in doc["entities"]
                    if e["type"] not in INVALID_ENTITY_TYPES]
        if doc["kind"] == "csv":
            stripped, _, findings = strip_csv(
                text, pipeline, pmap, columns=["Description"]
            )
            _score_csv(reg_ents, stripped)
            _score_invalid_csv(inv_ents, findings, stripped)
        else:
            stripped, spans, findings = pipeline.strip(text, pmap)
            _score_text(reg_ents, spans, stripped)
            _score_invalid_text(inv_ents, findings, spans)
        for e in doc["entities"]:
            e["file"] = doc["file"]
        all_entities.extend(reg_ents)
        all_invalid.extend(inv_ents)
        noise.extend((doc["file"], f) for f in
                     _noise(findings, inv_ents, doc["kind"]))
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

    if invalid_identifiers != "ignore" and (all_invalid or noise):
        print(f"\nchecksum-invalid identifiers "
              f"(collection tier: {invalid_identifiers})")
        by_inv = defaultdict(lambda: defaultdict(int))
        for e in all_invalid:
            c = by_inv[e["type"]]
            c["n"] += 1
            c[e["verdict"]] += 1
            c["sa"] += e["stripped_anyway"]
        print(f"{'type':<24}{'n':>4}{'logged':>8}{'missed':>8}"
              f"{'stripped-anyway':>17}")
        for t in sorted(by_inv):
            c = by_inv[t]
            print(f"{t:<24}{c['n']:>4}{c['logged']:>8}{c['missed']:>8}"
                  f"{c['sa']:>17}")
        by_ev = defaultdict(lambda: [0, 0])
        for e in all_invalid:
            by_ev[e["evidence"]][0] += e["verdict"] == "logged"
            by_ev[e["evidence"]][1] += 1
        order = {"in-span": 0, "context": 1, "none": 2}
        print("  by evidence: " + "; ".join(
            f"{ev}: {c[0]}/{c[1]} logged"
            for ev, c in sorted(by_ev.items(),
                                key=lambda kv: order.get(kv[0], 9))
        ))
        for e in all_invalid:
            if e["verdict"] == "missed":
                print(f"  missed: {e['file']}: {e['type']} "
                      f"({e['evidence']}): {e['value']!r}")
        print(f"  noise findings (matching no injected entity): {len(noise)}")
        for file, f in noise:
            print(f"    {file}: {f.entity_type} {f.value!r}  [{f.rule}]")

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
