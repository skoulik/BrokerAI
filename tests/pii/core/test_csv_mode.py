"""Column-aware CSV stripping.

Layer 0 finds nothing in these (`no_findings`), so the plan comes from layer 1
alone — the structural guarantees under test (column isolation, per-cell
placeholder consistency, untouched date/amount columns) are independent of
which detector produced the spans, which is exactly why they belong here.
"""

import pytest

from pii.core.csv_mode import strip_csv
from pii.core.mapping import PseudonymMap

CSV = (
    "Date,Description,Debit,Credit,Balance\n"
    "01/02/2024,Transfer to olga@example.com,50.00,,1000.00\n"
    "02/02/2024,EFTPOS WOOLWORTHS 4821 AU,12.30,,987.70\n"
)


def test_strip_csv_processes_named_column_only(pipeline, no_findings):
    out = strip_csv(CSV, pipeline, PseudonymMap(), columns=["Description"],
                    detector=no_findings).text
    assert "olga@example.com" not in out
    assert "EMAIL_1" in out
    # untouched columns and structure survive
    assert "01/02/2024,EMAIL_1" not in out.splitlines()[0]  # header intact
    assert out.splitlines()[0] == "Date,Description,Debit,Credit,Balance"
    assert "50.00" in out and "987.70" in out


def test_strip_csv_unknown_column_raises(pipeline, no_findings):
    with pytest.raises(ValueError, match="Nope"):
        strip_csv("A,B\n1,2\n", pipeline, PseudonymMap(), columns=["Nope"],
                  detector=no_findings)


def test_strip_csv_consistent_placeholders_across_rows(pipeline, no_findings):
    text = (
        "Date,Description\n"
        "01/02/2024,PayID olga@example.com\n"
        "05/02/2024,rent from olga@example.com\n"
    )
    out = strip_csv(text, pipeline, PseudonymMap(), columns=["Description"],
                    detector=no_findings).text
    assert out.count("EMAIL_1") == 2


def test_strip_csv_layer0_spans_are_clamped_per_cell(pipeline, stub_detector):
    """A layer-0 value that spans the sentinel must not produce a placeholder
    straddling two cells — the guarantee the per-column batching exists for."""
    text = (
        "Date,Description\n"
        "01/02/2024,Olga Kulik\n"
        "05/02/2024,Sergei Kulik\n"
    )
    detector = stub_detector(("Kulik", "PERSON"))
    out = strip_csv(text, pipeline, PseudonymMap(), columns=["Description"],
                    detector=detector).text
    rows = out.splitlines()
    assert rows[0] == "Date,Description"
    # Both cells keep their own structure; neither swallowed the other.
    assert rows[1].startswith("01/02/2024,")
    assert rows[2].startswith("05/02/2024,")
    assert "Kulik" not in out
