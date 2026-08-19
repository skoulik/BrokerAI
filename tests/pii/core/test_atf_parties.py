"""Layer-1 pass 2: the two organizations a trustee clause names (2026-08-19).

The sibling of `test_joint_names.py`, and deliberately the same shape: a value
that names two entities at once is neither of them, so the compound keeps its
own class and the parties are derived beside it.

Layer 0 is the source that carries BOTH halves. `AtfTailRule` matches the
clause from its connector, so it only ever sees the trust — a company name has
no shape and no left edge — which is why the trustee company is recoverable
only from a value a model read whole.

These drive through `PiiPipeline.merge_detections`, the production path.
`KnownValues` is passed only where the point is the document-wide pool; without
it each case is one page that is its own whole document.
"""

from __future__ import annotations

import pytest

from pii.core.derived import KnownValues, parse_atf
from pii.core.detection import Detection
from pii.core.mapping import PseudonymMap
from pii.core.pipeline import apply_plan

COMPOUND = "SK MANAGEMENT VICTORIA PTY LTD ATF SK BUSINESS TRUST"
TRUSTEE = "SK MANAGEMENT VICTORIA PTY LTD"
TRUST = "SK BUSINESS TRUST"


def _org(text: str, value: str) -> Detection:
    start = text.index(value)
    return Detection("ORGANIZATION", start, start + len(value), 1.0)


def _typed(pipeline, text: str, *orgs: str, known: KnownValues | None = None):
    spans, _ = pipeline.merge_detections(
        [_org(text, o) for o in orgs], text, None, known
    )
    return [(s.entity_type, text[s.start : s.end]) for s in spans]


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    "value,trustee,trust",
    [
        (COMPOUND, TRUSTEE, TRUST),
        ("ACME PTY LTD as trustee for THE KULIK FAMILY TRUST",
         "ACME PTY LTD", "THE KULIK FAMILY TRUST"),
        ("ACME PTY LTD as trustees for THE KULIK FAMILY TRUST",
         "ACME PTY LTD", "THE KULIK FAMILY TRUST"),
        # The truncation real statements produce — still a compound, and the
        # trust half is exactly what the document had room for.
        ("SK MANAGEMENT VICTORIA PTY LTD ATF SK BU",
         "SK MANAGEMENT VICTORIA PTY LTD", "SK BU"),
    ],
)
def test_parses_the_trustee_clause(value, trustee, trust):
    parsed = parse_atf(value)
    assert parsed is not None, value
    assert (parsed.trustee, parsed.trust) == (trustee, trust)


@pytest.mark.parametrize(
    "value",
    [
        "SK MANAGEMENT VICTORIA PTY LTD",   # a plain organization
        "ATF SK BUSINESS TRUST",            # the trust with a stray label
        "SK BUSINESS TRUST ATF",            # ...and the mirror of it
    ],
)
def test_rejects_what_is_not_a_compound(value):
    """Both sides must be non-empty. A value that merely starts with the
    connector is the trust alone with a label on it — `AtfTailRule`'s job."""
    assert parse_atf(value) is None


def test_parse_trusts_its_caller_about_organizationhood():
    """Like `parse_joint`, this answers *what form is this value*, never *is
    this an organization* — a detector already decided. It is harmless because
    nothing calls it on a value no layer detected."""
    assert parse_atf("bread and butter atf jam and cream") is not None


# ------------------------------------------------------- classify + derive


def test_the_compound_keeps_its_own_class(pipeline):
    """Not ORGANIZATION: the clause names two organizations, so typing it as
    one asserts a third entity. And mechanically it is the only arrangement
    that works — the parties are SUBSTRINGS of the compound, so as one class
    `_merge_overlaps` would union all three back into the clause."""
    text = f"Account name {COMPOUND}\n"
    assert _typed(pipeline, text, COMPOUND) == [
        ("ORGANIZATION_TRUSTEE", COMPOUND)
    ]


def test_both_parties_are_derived_from_one_compound(pipeline):
    """What layer 1 cannot do. `AtfTailRule` matches from the connector, so the
    trustee company — no shape, no left edge — is reachable only by decomposing
    a value the model read whole."""
    text = (f"Account name {COMPOUND}\n"
            f"Distribution to {TRUST} on 30 June\n"
            f"Managed by {TRUSTEE} since 2019\n")
    found = _typed(pipeline, text, COMPOUND)
    assert ("ORGANIZATION_TRUSTEE", COMPOUND) in found, found
    assert ("ORGANIZATION", TRUST) in found, found
    assert ("ORGANIZATION", TRUSTEE) in found, found


def test_the_compound_is_matched_first(pipeline):
    """Sergei's ordering call (2026-08-19). A party occurrence INSIDE the
    compound is already covered, by the span carrying the more specific label,
    so it must not also be emitted — the same rule `JointNames` applies to a
    surname inside a joint span."""
    text = f"Account name {COMPOUND}\n"
    found = _typed(pipeline, text, COMPOUND)
    assert found.count(("ORGANIZATION", TRUST)) == 0, found
    assert found.count(("ORGANIZATION", TRUSTEE)) == 0, found
    assert found == [("ORGANIZATION_TRUSTEE", COMPOUND)]


def test_a_party_is_derived_on_a_page_that_names_nobody(pipeline):
    """The step-0 payoff. The compound was read on another page; this one
    carries only bare mentions and no detection of its own, so without the
    document-wide pool pass 2 would have nothing to decompose."""
    text = (f"Distribution to {TRUST} on 30 June\n"
            f"Managed by {TRUSTEE} since 2019\n")
    known = KnownValues.of([("ORGANIZATION", COMPOUND)])
    assert _typed(pipeline, text, known=known) == [
        ("ORGANIZATION", TRUST), ("ORGANIZATION", TRUSTEE),
    ]
    # ...and with no document behind it, this page knows nothing.
    assert _typed(pipeline, text) == []


def test_a_party_with_no_word_character_is_never_searched(pipeline):
    """It is searched as a literal with word boundaries, so a punctuation-only
    fragment has nothing to anchor on — the hazard `locator` hit with a
    punctuation-only OCR word, which joined any piece at any position."""
    text = "Account name ACME PTY LTD ATF ---\nRent --- paid\n"
    found = _typed(pipeline, text, "ACME PTY LTD ATF ---")
    assert not [v for kind, v in found if v.strip() == "---"], found


# --------------------------------------------------------------- placeholder


def test_the_compound_and_its_parties_get_distinct_placeholders(pipeline):
    """Three spans, three identities, and the connector survives to say what
    the relationship is. The compound's own prefix is what tells a reader which
    of the three is the whole clause."""
    text = (f"Account name {COMPOUND}\n"
            f"Distribution to {TRUST} on 30 June\n"
            f"Managed by {TRUSTEE} since 2019\n")
    spans, _ = pipeline.merge_detections([_org(text, COMPOUND)], text)
    out = apply_plan(text, spans, PseudonymMap())
    assert "TRUSTEE_1" in out, out
    assert out.count("ORG_1") == 1 and out.count("ORG_2") == 1, out
    for value in (COMPOUND, TRUST, TRUSTEE):
        assert value not in out, out


def test_a_party_named_twice_is_one_identity(pipeline):
    """The reason the parties are values rather than one blob: every mention of
    the trust keys the map on the same string, so it cannot fork."""
    text = (f"Account name {COMPOUND}\n"
            f"Distribution to {TRUST} on 30 June\n"
            f"Final payment to {TRUST} on 31 December\n")
    spans, _ = pipeline.merge_detections([_org(text, COMPOUND)], text)
    out = apply_plan(text, spans, PseudonymMap())
    assert out.count("ORG_1") == 2, out
    assert "ORG_3" not in out, out
