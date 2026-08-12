"""Column-aware CSV handling for bank transaction lists.

Runs detection per cell so dates/amounts columns pass through untouched and
placeholders never straddle cell boundaries. `columns` restricts processing
to named columns (header row required); default is every column.

Cells are batched into one detection call per column (rows joined by a
sentinel) — per-cell calls would pay the model's per-invocation cost hundreds
of times on a big statement. The sentinel keeps pattern recognizers from
matching across cells. A detector can still return a value spanning a cell
boundary, so detected spans are clamped to cell boundaries before replacement
(the fragment in each cell is replaced independently — recall-first).

Detection is delegated to `pii.core.text_mode.detect_text`, so the layer-0
`TextDetector` serves CSV exactly as it serves plain text. The per-column
batching is unchanged by that and is the reason it stays: the guarantees it
buys — placeholders never straddling a cell, date/amount columns passing
through byte-identical — are structural, independent of which detector runs.
(Whether a language model would do better seeing whole ROWS than a column of
values in isolation is an open question, not answered here.)

Historical note: the sentinel's RECORD_SEPARATOR char was ALSO a hard NER
window boundary, isolating each cell from GLiNER2's global attention
(2026-07-15, records in pii/core/DONE.md). That half died with the recognizer
on 2026-08-09; the pattern-isolation half above is why the sentinel remains.
"""

import csv
import io

from pii.core.constants import RECORD_SEPARATOR
from pii.core.mapping import PseudonymMap
from pii.core.pipeline import PiiPipeline
from pii.core.text_mode import TextStripResult, detect_text
from pii.core.vlm import Incomplete

# Never appears in bank data; blocks patterns from spanning two cells (see
# module docstring).
_SENTINEL = f"\n{RECORD_SEPARATOR}\n"


def strip_csv(
    text: str,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    columns: list[str] | None = None,
    *,
    detector,
) -> TextStripResult:
    """Strip a CSV per cell through the layer-0 `detector` (required — see
    the module docstring).

    Span offsets on the result are relative to the per-column joined blob, as
    are invalid-finding offsets —
    only their value/type/rule are meaningful to callers. Pattern matches
    cannot cross the sentinel (it contains non-pattern characters), so
    findings never straddle cells; when masking is on, the invalid spans
    are part of the plan and get the same per-cell clamping as every
    other span.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return TextStripResult(text=text, spans=[], invalid=[])

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
    all_unlocated = []
    # Summed over columns: each is its own detection pass, so a column whose
    # answer was cut off is a column that may be under-redacted.
    all_incomplete = Incomplete()
    for col in sorted(wanted):
        # Data rows only — the header row is column names, not PII.
        cells = [row[col] if col < len(row) else "" for row in rows[1:]]
        if not any(c.strip() for c in cells):
            continue
        joined = _SENTINEL.join(cells)
        spans, invalid, unlocated, incomplete = detect_text(
            joined, pipeline, detector
        )
        all_spans.extend(spans)
        all_invalid.extend(invalid)
        all_unlocated.extend(unlocated)
        all_incomplete += incomplete

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
    return TextStripResult(
        text=out.getvalue(),
        spans=all_spans,
        invalid=all_invalid,
        unlocated=all_unlocated,
        incomplete=all_incomplete,
    )
