"""Column-aware CSV handling for bank transaction lists.

Runs detection per cell so dates/amounts columns pass through untouched and
placeholders never straddle cell boundaries. `columns` restricts processing
to named columns (header row required); default is every column.

Cells are batched into one analyzer call per column (rows joined by a
sentinel) — per-cell calls would pay the NER model's per-invocation cost hundreds
of times on a big statement. The sentinel keeps pattern recognizers from
matching across cells, AND (2026-07-15) its RECORD_SEPARATOR char is a hard
NER window boundary — the GLiNER2 recognizer predicts each cell in its own
window, because cross-cell context is pure noise (spans are clamped per
cell anyway) and same-person mentions in different word orders interfere
inside one attention window (records in pii/DONE.md). NER can still emit a
span crossing a cell boundary within a window, so detected spans are
clamped to cell boundaries before replacement (the fragment in each cell
is replaced independently — recall-first).
"""

import csv
import io

from pii.core.constants import RECORD_SEPARATOR
from pii.core.mapping import PseudonymMap
from pii.core.pipeline import PiiPipeline

# Never appears in bank data; blocks patterns from spanning two cells and
# splits NER prediction windows (see module docstring).
_SENTINEL = f"\n{RECORD_SEPARATOR}\n"


def strip_csv(
    text: str,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    columns: list[str] | None = None,
) -> tuple[str, list, list]:
    """Returns (stripped_csv, applied_detections, invalid_findings).

    Invalid-finding offsets are relative to the per-column joined blob —
    only their value/type/rule are meaningful to callers. Pattern matches
    cannot cross the sentinel (it contains non-pattern characters), so
    findings never straddle cells; when masking is on, the invalid spans
    are part of the plan and get the same per-cell clamping as every
    other span.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text, [], []

    header = rows[0]
    if columns:
        missing = [c for c in columns if c not in header]
        if missing:
            raise ValueError(
                f"columns not in CSV header {header}: {missing}"
            )
        wanted = {header.index(c) for c in columns}
    else:
        wanted = set(range(max(len(r) for r in rows)))

    all_spans = []
    all_invalid = []
    for col in sorted(wanted):
        # Data rows only — the header row is column names, not PII.
        cells = [row[col] if col < len(row) else "" for row in rows[1:]]
        if not any(c.strip() for c in cells):
            continue
        joined = _SENTINEL.join(cells)
        spans, invalid = pipeline.detect(joined)
        all_spans.extend(spans)
        all_invalid.extend(invalid)

        # Cell offset ranges within `joined`.
        bounds = []
        pos = 0
        for c in cells:
            bounds.append((pos, pos + len(c)))
            pos += len(c) + len(_SENTINEL)

        # Clamp each span to the cells it touches; replace fragments
        # right-to-left per cell so earlier offsets stay valid. Placeholders
        # are allocated in document order (pmap is idempotent, so a fragment
        # seen twice gets the same placeholder).
        replaced = list(cells)
        for i, (cs, ce) in enumerate(bounds):
            frags = []
            for s in spans:
                lo, hi = max(s.start, cs), min(s.end, ce)
                if lo < hi:
                    frags.append((lo - cs, hi - cs, s.entity_type))
            # Forward pre-pass so numbering follows document order, then
            # splice in reverse.
            for lo, hi, etype in sorted(frags):
                pmap.placeholder_for(etype, cells[i][lo:hi])
            for lo, hi, etype in sorted(frags, reverse=True):
                placeholder = pmap.placeholder_for(etype, cells[i][lo:hi])
                replaced[i] = replaced[i][:lo] + placeholder + replaced[i][hi:]

        for row, new_value in zip(rows[1:], replaced):
            if col < len(row):
                row[col] = new_value

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(rows)
    return out.getvalue(), all_spans, all_invalid
