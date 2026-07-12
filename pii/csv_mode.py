"""Column-aware CSV handling for bank transaction lists.

Runs detection per cell so dates/amounts columns pass through untouched and
placeholders never straddle cell boundaries. `columns` restricts processing
to named columns (header row required); default is every column.

Cells are batched into one analyzer call per column (rows joined by a
sentinel the recognizers can't match across) — per-cell calls would pay
GLiNER's per-invocation cost hundreds of times on a big statement.
"""

import csv
import io

from pii.mapping import PseudonymMap
from pii.pipeline import PiiPipeline

# Never appears in bank data; blocks patterns/NER from spanning two cells.
_SENTINEL = "\n␞\n"


def strip_csv(
    text: str,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    columns: list[str] | None = None,
) -> tuple[str, list]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text, []

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
    for col in sorted(wanted):
        # Data rows only — the header row is column names, not PII.
        cells = [row[col] if col < len(row) else "" for row in rows[1:]]
        if not any(c.strip() for c in cells):
            continue
        joined = _SENTINEL.join(cells)
        stripped, spans = pipeline.strip(joined, pmap)
        all_spans.extend(spans)
        replaced = stripped.split(_SENTINEL)
        if len(replaced) != len(cells):  # a replacement ate a sentinel
            raise RuntimeError(
                f"cell alignment lost in column {header[col]!r}"
            )
        for row, new_value in zip(rows[1:], replaced):
            if col < len(row):
                row[col] = new_value

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(rows)
    return out.getvalue(), all_spans
