"""The keep list (pii.core.entity_keep) — pure/lexical, no model.

A value is STRIPPED unless the keep list matches it (inverted 2026-08-11).
These tests pin the inversion, the file format including sections, and the two
real failures that caused it — both cases where the old marker rule kept an
account holder's own trust because the page had mangled the evidence."""

import pytest

from pii.core.entity_keep import (
    DEFAULT_KEEP_FILE,
    EntityKeep,
    KeepList,
    load_keep,
    parse_keep,
)


@pytest.fixture(scope="module")
def shipped():
    return load_keep()


def _orgs(keep):
    """The ORGANIZATION section as a callable — most tests are about it."""
    return lambda name: keep.keeps("ORGANIZATION", name)


# --- the inversion: keeping takes positive evidence ----------------------


@pytest.mark.parametrize("name", [
    "SK BUSINESS TRUST",                       # issue #2
    "SK BUSINESS TRUS",                        # doc-truncated (2026-08-11)
    "SK BUSINESS TRU",                         # truncated further
    "SK MGMT",                                 # markerless customer entity
    "SK MANAGEMENT VICTORIA PTY LTD",          # issue #5
    "OAKFIELD FAMILY TRUST",
    "Smith Superannuation Fund",
    "JONES SMSF",
    "THE TRUSTEE FOR SK BUSINESS TRUST",
    "ATF SK BU",                               # truncated trustee clause
    "HARVEY AND MILLER HOLDINGS",
])
def test_unknown_organizations_are_stripped(shipped, name):
    assert _orgs(shipped)(name) is False


def test_the_truncation_that_caused_the_inversion(shipped):
    """A statement printed 'SK BUSINESS TRUST' as 'SK BUSINESS TRUS' in a
    fixed-width field. The old rule needed the legal-form marker as evidence to
    strip, and the page had truncated exactly that — so the holder's own trust
    was kept, three times on one page, while the same value stripped in full
    elsewhere. Neither form may be kept now."""
    keeps = _orgs(shipped)
    assert keeps("SK BUSINESS TRUST") is False
    assert keeps("SK BUSINESS TRUS") is False


def test_a_keep_match_covers_only_itself(shipped):
    """A fused span must not ride a keep-listed token to safety.

    Layer 0 reads a whole statement narrative as one organization, and
    'SK BUSINESS TRUS ANZ HIGHETT LOAN' contains ANZ. `keeps` is True — a
    pattern does match in there — but `matches` reports WHERE, and that is what
    the pipeline subtracts: ANZ survives, the trust name does not. Measured on
    a real page, where exempting the span wholesale kept the account holder's
    own trust three times (2026-08-11)."""
    fused = "SK BUSINESS TRUS ANZ HIGHETT LOAN"
    assert _orgs(shipped)(fused) is True
    (start, end), = shipped.matches("ORGANIZATION", fused)
    assert fused[start:end] == "ANZ"


def test_matches_reports_every_keep_range_merged(shipped):
    value = "ANZ TO WOOLWORTHS NEWTOWN"
    spans = shipped.matches("ORGANIZATION", value)
    assert [value[a:b] for a, b in spans] == ["ANZ", "WOOLWORTHS"]


def test_matches_is_empty_for_an_unlisted_value(shipped):
    assert shipped.matches("ORGANIZATION", "SK BUSINESS TRUS") == []


# --- what the shipped list keeps -----------------------------------------


@pytest.mark.parametrize("name", [
    "ANZ",
    "ANZ HIGHETT LOAN",
    "QBE Insurance (Australia) Limited",
    "Australia and New Zealand Banking Group Limited",
    "CGU Insurance",
    "WOOLWORTHS",
    "WOOLWORTHS NEWTOWN 4821 AU",               # statement narrative form
    "EFTPOS COLES EXPRESS 1234",
    "BUDGET DIRECT INSURANCE",
    "UBER *TRIP",
    "JB HI-FI",
    "NETFLIX.COM",
    "St George Bank",
    "ST. GEORGE",
])
def test_shipped_list_keeps_institutions_and_merchants(shipped, name):
    assert _orgs(shipped)(name) is True


def test_word_boundaries_are_enforced(shipped):
    # 'anz' must not keep 'ANZAC PARADE TRADING' — patterns are wrapped in \b.
    assert _orgs(shipped)("ANZAC PARADE TRADING") is False


def test_shipped_file_parses_and_is_not_empty(shipped):
    assert DEFAULT_KEEP_FILE.exists()
    assert len(shipped) > 50


def test_shipped_phone_ranges_are_disabled(shipped):
    """The 1300/1800 patterns ship COMMENTED OUT: on a business account the
    holder's own service line is as identifying as their company name, so
    enabling them is a per-document-set decision."""
    assert shipped.keeps("PHONE_NUMBER", "1300 975 000") is False
    assert shipped.keeps("PHONE_NUMBER", "13 22 65") is False


# --- sections -------------------------------------------------------------


def test_unsectioned_lines_are_organizations():
    keep = parse_keep("acme\nwidgets\n")
    assert keep.keeps("ORGANIZATION", "ACME PTY LTD") is True
    assert keep.keeps("PHONE_NUMBER", "ACME PTY LTD") is False


def test_sections_scope_patterns_to_one_class():
    keep = parse_keep(
        "acme\n"
        "\n"
        "[PHONE_NUMBER]\n"
        "1300[\\s-]*\\d{3}[\\s-]*\\d{3}\n"
    )
    # The motivating case: an institution's support line, detected as
    # PHONE_NUMBER exactly like a customer's mobile.
    assert keep.keeps("PHONE_NUMBER", "1300 975 000") is True
    assert keep.keeps("PHONE_NUMBER", "0412 345 678") is False
    # ...and the pattern does not leak into another class.
    assert keep.keeps("ORGANIZATION", "1300 975 000") is False


def test_a_class_with_no_section_keeps_nothing():
    keep = parse_keep("acme\n")
    assert keep.keeps("PERSON", "acme") is False
    assert keep.keeps("ADDRESS", "anything") is False


def test_without_drops_one_sections_exemptions():
    # What --strip-orgs does, expressed as data rather than a flag.
    keep = parse_keep("acme\n[PHONE_NUMBER]\n1300\\d+\n")
    stripped_orgs = keep.without("ORGANIZATION")
    assert stripped_orgs.keeps("ORGANIZATION", "ACME") is False
    assert stripped_orgs.keeps("PHONE_NUMBER", "1300999") is True


# --- file format ----------------------------------------------------------


def test_parse_ignores_comments_and_blanks():
    keep = parse_keep(
        "# a comment\n"
        "\n"
        "acme\\s+bank   # trailing comment\n"
        "  \n"
        "widgets\n"
    )
    assert len(keep) == 2
    assert keep.keeps("ORGANIZATION", "ACME  BANK LTD") is True
    assert keep.keeps("ORGANIZATION", "WIDGETS R US") is True
    assert keep.keeps("ORGANIZATION", "SOMETHING ELSE") is False


def test_a_broken_pattern_is_a_configuration_error():
    # Silently dropping it would silently start stripping what it was meant to
    # keep — the operator must hear about it.
    with pytest.raises(ValueError, match="line 2"):
        parse_keep("good\n(unclosed\n")


def test_a_malformed_section_header_is_an_error():
    # '[phone_number]' would otherwise be read as a pattern and keep nothing.
    with pytest.raises(ValueError, match="section header"):
        parse_keep("acme\n[phone_number]\n1300\\d+\n")


def test_a_missing_file_is_reported_not_ignored():
    with pytest.raises(ValueError, match="cannot read"):
        load_keep("no/such/keep/list.txt")


def test_an_empty_list_keeps_nothing():
    # The all-strip regime: legitimate, and it must not accidentally match.
    assert EntityKeep({}).keeps("ORGANIZATION", "ANZ") is False
    assert KeepList([]).keeps("ANZ") is False
