"""Transaction-CSV template with per-cell ground truth.

Each description is rendered through a throwaway Doc so its annotations come
out with cell-relative offsets; the truth records carry (row, col) plus the
value, which is what the leak-based scorer needs (placeholders never straddle
cells in pii's CSV mode, so cell-level truth is sufficient).
"""

import csv
import io
import random
from dataclasses import dataclass, asdict

from pii_eval import txbank
from pii_eval.build import CRITICAL, Doc
from pii_eval.personas import Pool

HEADER = ["Date", "Description", "Debit", "Credit", "Balance"]


@dataclass
class CellAnn:
    type: str
    value: str
    row: int  # 1-based data row (0 is the header)
    col: int
    strip_expected: bool = True

    @property
    def critical(self) -> bool:
        return self.type in CRITICAL

    def to_json(self) -> dict:
        return asdict(self) | {"critical": self.critical}


def transactions_csv(pool: Pool, n_rows: int | None = None) -> tuple[str, list[CellAnn]]:
    rng = pool.rng
    n_rows = n_rows or rng.randrange(15, 40)
    year = rng.choice([2023, 2024, 2025])
    balance = round(rng.uniform(100, 50000), 2)

    rows: list[list[str]] = [HEADER]
    anns: list[CellAnn] = []
    for i in range(1, n_rows + 1):
        cell = Doc()
        for part in txbank.description(pool):
            cell.raw(part) if isinstance(part, str) else cell.pii(*part)
        anns.extend(
            CellAnn(a.type, a.value, i, 1, a.strip_expected) for a in cell.anns
        )
        debit, credit, balance = txbank.amounts(rng, balance)
        rows.append(
            [
                f"{rng.randrange(1, 29):02d}/{rng.randrange(1, 13):02d}/{year}",
                cell.text,
                debit,
                credit,
                f"{balance:,.2f}",
            ]
        )

    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue(), anns
