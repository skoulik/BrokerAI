"""Column-aware CSV stripping."""

import pytest

from pii.core.csv_mode import strip_csv
from pii.core.mapping import PseudonymMap

CSV = (
    "Date,Description,Debit,Credit,Balance\n"
    "01/02/2024,Transfer to olga@example.com,50.00,,1000.00\n"
    "02/02/2024,EFTPOS WOOLWORTHS 4821 AU,12.30,,987.70\n"
)


def test_strip_csv_processes_named_column_only(pipeline):
    out, spans, _ = strip_csv(CSV, pipeline, PseudonymMap(), columns=["Description"])
    assert "olga@example.com" not in out
    assert "EMAIL_1" in out
    # untouched columns and structure survive
    assert "01/02/2024,EMAIL_1" not in out.splitlines()[0]  # header intact
    assert out.splitlines()[0] == "Date,Description,Debit,Credit,Balance"
    assert "50.00" in out and "987.70" in out


def test_strip_csv_unknown_column_raises(pipeline):
    with pytest.raises(ValueError, match="Nope"):
        strip_csv("A,B\n1,2\n", pipeline, PseudonymMap(), columns=["Nope"])


def test_strip_csv_consistent_placeholders_across_rows(pipeline):
    text = (
        "Date,Description\n"
        "01/02/2024,PayID olga@example.com\n"
        "05/02/2024,rent from olga@example.com\n"
    )
    out, _, _ = strip_csv(text, pipeline, PseudonymMap(), columns=["Description"])
    assert out.count("EMAIL_1") == 2
