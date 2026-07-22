"""Account-holder private-entity policy for ORGANIZATION spans.

ORGANIZATION is kept by default — merchant and institution names carry
analytical value in bank statements (pii/core/pipeline.py). But the account
holder's OWN business entity — a company, trust or fund that identifies them
— is PII. This module draws the line the way Sergei chose (2026-07-21): an
organization is a *private entity* to be stripped when it carries an
Australian legal-form marker (PTY LTD, TRUST, ATF, SUPER FUND, ...) AND is
NOT a known public institution on the keep-list. Both lists are lexical and
extensible; the keep-list wins ties (a bank's own super fund stays kept, the
customer's SMSF is stripped).

Deliberate limits (the low-hanging fruit — mangled transaction spans are a
separate pass): does NOT catch markerless customer entities ('SK MGMT'), and
an OCR-fused span carrying an institution token ('SK ... TRUS ANZ HIGHETT')
is kept by the keep-list even though it hides a private entity.
"""

import re

# Legal-form markers of a private company / trust / fund. Bare LTD/LIMITED is
# excluded on purpose: too many public companies use it ('QBE ... Limited'),
# and the keep-list can't be exhaustive. Matched case-insensitively.
# trustee takes the plural too ('as trustees for', joint trustees —
# issue #9, 2026-07-22).
_MARKERS = re.compile(
    r"\b(?:pty|trust(?:ees?)?|atf|super(?:annuation)?\s+fund|smsf)\b",
    re.IGNORECASE,
)

# Known public institutions to KEEP even when they carry a marker. Grow as
# needed; matched case-insensitively on word boundaries. The keep-list wins:
# an institution is never a "private entity" regardless of its legal form.
_KEEPLIST = re.compile(
    r"\b(?:"
    r"anz|nab|cba|commonwealth\s+bank|westpac|st\.?\s*george|bankwest|"
    r"bendigo|suncorp|macquarie|bank\s+of\s+queensland|boq|ing|amp|"
    r"afg|advantedge|connective|"
    r"qbe|cgu|iag|allianz|aami|insurance\s+australia|"
    r"australia\s+and\s+new\s+zealand\s+banking|new\s+zealand\s+banking\s+group|"
    r"visa|mastercard|american\s+express"
    r")\b",
    re.IGNORECASE,
)


def is_private_entity(name: str) -> bool:
    """True if an ORGANIZATION name is an account-holder private entity that
    should be stripped: a legal-form marker is present and it is not a
    keep-listed public institution."""
    if _KEEPLIST.search(name):
        return False
    return bool(_MARKERS.search(name))
