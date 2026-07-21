"""Account-holder private-entity policy (org_policy.is_private_entity).

Strip an ORGANIZATION when it carries a legal-form marker (PTY LTD / TRUST /
ATF / SUPER FUND / SMSF) and is not a known public institution; keep it
otherwise. The keep-list wins ties. Pure/lexical — no model."""

import pytest

from pii.core.org_policy import is_private_entity


@pytest.mark.parametrize("name", [
    "SK BUSINESS TRUST",                       # issue #2
    "SK MANAGEMENT VICTORIA PTY LTD",          # issue #5
    "OAKFIELD CONSULTING PTY LTD",
    "OAKFIELD FAMILY TRUST",
    "Smith Superannuation Fund",
    "JONES SMSF",
    "THE TRUSTEE FOR SK BUSINESS TRUST",
    "TAYLOR AND SCOTT LAWYERS PTY LTD",        # joint-shaped, still private
])
def test_private_entities_stripped(name):
    assert is_private_entity(name) is True


@pytest.mark.parametrize("name", [
    "ANZ",                                     # institution
    "QBE Insurance (Australia) Limited",       # keep-listed despite 'Limited'
    "Australia and New Zealand Banking Group Limited",
    "CGU Insurance",
    "WOOLWORTHS",                              # merchant, no marker
    "WOOLWORTHS NEWTOWN",
    "BUDGET DIRECT INSURANCE",
    "SK MGMT",                                 # markerless customer entity
    "HARVEY AND MILLER HOLDINGS",              # 'HOLDINGS' is not a marker
])
def test_kept_organizations(name):
    assert is_private_entity(name) is False


def test_keeplist_wins_over_marker():
    # A bank's own super fund carries a marker but is kept; the customer's is
    # stripped (the keep-list-wins-ties rule).
    assert is_private_entity("AMP Superannuation Fund") is False
    assert is_private_entity("Kulik Family Superannuation Fund") is True
