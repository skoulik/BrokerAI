"""Score the pii image path against a rendered corpus.

End-to-end value survival: each rendered page goes through the real image
pipeline (OCR -> detect -> paint), the painted output is OCR'd AGAIN, and
every truth entity is scored by whether its value can still be read out of
the redacted image. Character offsets are meaningless through pixels, so
this scorer matches values (the truth manifest carries them), not spans —
the CSV mode's value-survival semantics, but through the actual painting.

Matching is OCR-tolerant, and deliberately asymmetric — recall-first, a
fuzzy match counts as a LEAK: after exact normalized containment fails, a
confusion-squashed containment (0/O, 1/l/I, 5/S, 8/B ... collapsed) and,
for longer values, a banded edit-distance scan are tried, so a value that
survived with one misread glyph is flagged rather than silently passed.
Values squashing below 4 characters match exactly only (3-letter suburbs
would false-leak everywhere at distance 1). The same matcher decides
keep-types ("kept" if still readable) — there the tolerance works in the
pipeline's favor, which is the correct direction for both.

Checksum-invalid injections are scored on the same axes as the text
scorer (logged/missed against pipeline invalid findings, stripped-anyway,
noise floor), matched by value. Exit code 1 if any critical-type value
survived (same acceptance-gate semantics as the text tier).
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

from pii.core import INVALID_ENTITY_TYPES, PiiPipeline, PseudonymMap
from pii.core.image_mode import strip_image
from pii.core.ocr import get_ocr
from pii_eval.score import _norm

# Classic OCR confusion pairs, collapsed to one representative per class.
# 6/9/g all map together — over-merging digits can only over-report leaks,
# the safe direction.
_CONFUSION = str.maketrans({
    "0": "o", "q": "o",
    "1": "i", "l": "i", "|": "i", "!": "i", "j": "i",
    "5": "s",
    "8": "b",
    "2": "z",
    "6": "g", "9": "g",
    "7": "t",
})


def _squash(s: str) -> str:
    """Casefold, drop non-alphanumerics, collapse OCR confusion classes."""
    return re.sub(r"[^a-z0-9]", "", s.casefold()).translate(_CONFUSION)


def _substring_distance(needle: str, hay: str) -> int:
    """Minimum edit distance between needle and any substring of hay."""
    prev = [0] * (len(hay) + 1)  # a match may start anywhere in hay
    for i, pc in enumerate(needle, 1):
        cur = [i] * (len(hay) + 1)
        for j, hc in enumerate(hay, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (pc != hc))
        prev = cur
    return min(prev)


def find_value(value: str, text: str, squashed_text: str | None = None):
    """None if the value is not readable in text; else 'exact' or 'fuzzy'."""
    v = _norm(value)
    if v and v in _norm(text):
        return "exact"
    vs = _squash(value)
    ts = _squash(text) if squashed_text is None else squashed_text
    if len(vs) < 4:
        return None
    if vs in ts:
        return "fuzzy"
    if len(vs) >= 8:
        k = 1 if len(vs) < 14 else 2
        if _substring_distance(vs, ts) <= k:
            return "fuzzy"
    return None


def _score_survival(entities, reread: str) -> None:
    squashed = _squash(reread)
    for e in entities:
        found = find_value(e["value"], reread, squashed)
        e["match"] = found
        if e["strip_expected"]:
            e["verdict"] = "leaked" if found else "stripped"
        else:
            e["verdict"] = "kept" if found else "over-stripped"


def _score_invalid(entities, findings, reread: str) -> None:
    squashed = _squash(reread)
    for e in entities:
        e["verdict"] = (
            "logged"
            if any(
                f.entity_type == e["type"] and find_value(e["value"], f.value)
                for f in findings
            )
            else "missed"
        )
        e["stripped_anyway"] = not find_value(e["value"], reread, squashed)


def _noise(findings, inv_entities):
    return [
        f for f in findings
        if not any(find_value(e["value"], f.value) for e in inv_entities)
    ]


def score_image(corpus: str, threshold: float = 0.4,
                invalid_identifiers: str = "likely",
                ocr_backend: str = "paddle") -> int:
    ocr = get_ocr(ocr_backend)
    corpus_path = Path(corpus)
    manifest = json.loads((corpus_path / "manifest.json").read_text("utf-8"))
    source = (corpus_path / manifest["source"]).resolve()
    truth = json.loads((source / "truth.json").read_text("utf-8"))
    truth_by_file = {d["file"]: d for d in truth["docs"]}
    pipeline = PiiPipeline(threshold=threshold,
                           invalid_identifiers=invalid_identifiers)

    all_entities = []
    all_invalid = []
    noise = []
    for doc in manifest["docs"]:
        entities = truth_by_file[doc["source"]]["entities"]
        image = Image.open(corpus_path / doc["file"])
        pmap = PseudonymMap()
        result = strip_image(image, pipeline, pmap, ocr_backend=ocr_backend)
        reread = ocr(result.image).text
        inv_ents = [e for e in entities if e["type"] in INVALID_ENTITY_TYPES]
        reg_ents = [e for e in entities if e["type"] not in INVALID_ENTITY_TYPES]
        _score_survival(reg_ents, reread)
        _score_invalid(inv_ents, result.invalid, reread)
        for e in entities:
            e["file"] = doc["file"]
        all_entities.extend(reg_ents)
        all_invalid.extend(inv_ents)
        noise.extend((doc["file"], f) for f in _noise(result.invalid, inv_ents))
        print(f"  scored {doc['file']} [{doc['font']} {doc['size']}px]",
              file=sys.stderr)

    by_type = defaultdict(lambda: defaultdict(int))
    for e in all_entities:
        by_type[e["type"]][e["verdict"]] += 1
        if e["verdict"] == "leaked" and e["match"] == "fuzzy":
            by_type[e["type"]]["fuzzy"] += 1

    print(f"\n{'entity type':<20}{'n':>5}{'stripped':>10}{'leaked':>8}"
          f"{'~ocr':>6}{'recall':>8}")
    strip_types = sorted(
        t for t, c in by_type.items() if c["kept"] + c["over-stripped"] == 0
    )
    for t in strip_types:
        c = by_type[t]
        n = c["stripped"] + c["leaked"]
        recall = c["stripped"] / n if n else 0.0
        print(f"{t:<20}{n:>5}{c['stripped']:>10}{c['leaked']:>8}"
              f"{c['fuzzy']:>6}{recall:>8.0%}")

    keep_types = sorted(set(by_type) - set(strip_types))
    if keep_types:
        print(f"\n{'keep-type':<20}{'n':>5}{'kept':>10}{'over-stripped':>15}")
        for t in keep_types:
            c = by_type[t]
            print(f"{t:<20}{c['kept'] + c['over-stripped']:>5}{c['kept']:>10}"
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
        for e in all_invalid:
            if e["verdict"] == "missed":
                print(f"  missed: {e['file']}: {e['type']} "
                      f"({e['evidence']}): {e['value']!r}")
        print(f"  noise findings (matching no injected entity): {len(noise)}")
        for file, f in noise:
            print(f"    {file}: {f.entity_type} {f.value!r}  [{f.rule}]")

    failures = [
        e for e in all_entities
        if e["critical"] and e["verdict"] == "leaked"
    ]
    if failures:
        print(f"\nCRITICAL LEAKS ({len(failures)}):")
        for e in failures:
            print(f"  {e['file']}: {e['type']} ({e['match']}): "
                  f"{e['value']!r}")
        return 1
    print("\nAcceptance gate: PASS (zero critical misses)")
    return 0
