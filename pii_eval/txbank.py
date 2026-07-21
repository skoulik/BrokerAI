"""Transaction-description patterns modeled on the reference statements.

Each pattern returns a list of parts consumable by build.Doc: plain strings,
or (value, entity_type) / (value, entity_type, strip_expected) tuples.
Receipt/reference codes (FT..., W10..., REF...) are left un-annotated on
purpose: they are neither required strips nor protected keeps, so the scorer
ignores whatever the pipeline does with them.

The two documented-hard person surface forms are annotated with distinct
truth types — PERSON_JOINT ("E & J Moore") and PERSON_REVERSED ("MOORE
OLGA") — following the CONTEXTUAL_ID precedent. PERSON_JOINT entered
build.CRITICAL 2026-07-15: the layer-1 JointNameRecognizer owns the
mechanical joint forms now (GLiNER2 lost their span boundaries inside
transaction junk — diagnostic in pii/DONE.md). PERSON_REVERSED still
reports per-form without tripping the gate: reversed caps has no
mechanical pattern, GLiNER2 covers it only intermittently via glue spans;
promote it when its fix lands (candidates in pii/TODO.md). Two more
per-form probes (2026-07-15):

- ADDRESS_BARE — a street line with no suburb/state context ("RENT 53
  MILES ST"), the documented '53 MILES SUBWAY'-class recall miss.
- suburb-suffixed merchants ("EFTPOS WOOLWORTHS NEWTOWN 4821 AU") — the
  whole value is a keep-ORGANIZATION; GLiNER2 also emitting ADDRESS for
  the suburb (the 2026-07-14 image-demo wart) shows up as over-stripped
  on the keep axis. Input to the overlaps-merging task in pii/TODO.md.

The layer-1 JointNameRecognizer's precision trade-offs are probed per-form
too (2026-07-15 review round):

- ORGANIZATION_AND — 'X and Y Z' org names with a corporate word but no
  legal-form marker ('HARVEY AND MILLER HOLDINGS'); the recognizer's
  surname-slot guard must keep them from being mis-split into joint PERSON
  names. Marker-bearing joint-org names ('... PTY LTD') moved to
  AND_ORGS_PRIVATE / ORGANIZATION_PRIVATE (org_policy strips them, 2026-07-21):
  the recognizer's corporate-tail guard still keeps them off the PERSON path,
  but the strip decision is now org_policy's, not a keep.
- ORGANIZATION_AND_BARE — the documented recall-first sacrifice: org
  names in the joint-name shape with no corporate marker anywhere
  ("P & O CRUISES") get stripped by the person patterns; expected
  over-strips, watched so the loss never silently grows.
- colliding surnames (Fee, Card — surnames that are also statement
  vocabulary) are drawn as PERSON_COLLIDING: the full-name form is now
  GLiNER2-driven (connector-merge, issue #4) and the model does not
  recognise a word-like surname, so the given names strip but the surname
  leaks. Accepted 2026-07-21 — a non-gated per-form probe, not the critical
  PERSON gate.
"""

import random

from pii_eval import au
from pii_eval.personas import TOWNS, Pool

# Joint-name-shaped org names carrying a legal-form marker (PTY LTD):
# org_policy strips them as private entities, and the JointNameRecognizer's
# corporate-tail guard must still keep them from being mis-split into joint
# PERSON names. Ground-truthed ORGANIZATION_PRIVATE (strip) since 2026-07-21.
AND_ORGS_PRIVATE = [
    "TAYLOR AND SCOTT LAWYERS PTY LTD",
    "ANGUS AND ROBERTSON PTY LTD",
]
# Joint-name-shaped org whose corporate word (HOLDINGS) is NOT a legal-form
# marker — no private-entity strip, so it stays the ORGANIZATION_AND keep
# probe for the JointNameRecognizer surname-slot guard.
AND_ORGS_GUARDED = [
    "HARVEY AND MILLER HOLDINGS",
]
AND_ORGS_BARE = [
    "P & O CRUISES",
    "ANGUS AND ROBERTSON BOOKSHOP",
]
COLLIDING_SURNAMES = ["Fee", "Card"]


def _ref(rng: random.Random, prefix: str, n: int = 10) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
    return prefix + "".join(rng.choice(alphabet) for _ in range(n))


def _amount(rng: random.Random, lo=10, hi=20000) -> float:
    return round(rng.uniform(lo, hi), 2)


def description(pool: Pool) -> list:
    """One random transaction description as Doc parts."""
    rng = pool.rng
    p = pool.person()
    couple_a, couple_b = pool.couple()
    acct = pool.account()
    biz = pool.business()
    joint, joint_type = rng.choice(
        [
            (f"{couple_a.first[0]} & {couple_b.first[0]} {couple_a.last}",
             "PERSON_JOINT"),
            (f"{couple_a.first} and {couple_b.first} {couple_a.last}",
             "PERSON"),
            (couple_a.reversed_caps, "PERSON_REVERSED"),
            # Colliding surname: a surname that is also statement vocabulary
            # (Fee/Card). The full-name form is now GLiNER2-driven (connector-
            # merge, issue #4) and the model doesn't recognise a word-like
            # surname, so the given names strip but the surname leaks —
            # accepted (2026-07-21). Non-gated PERSON_COLLIDING probe.
            (f"{couple_a.first} and {couple_b.first} "
             f"{rng.choice(COLLIDING_SURNAMES)}",
             "PERSON_COLLIDING"),
        ]
    )
    patterns = [
        lambda: [
            "Repayment (from ",
            (au.digits(acct.bsb), "AU_BSB"),
            " ",
            (au.digits(acct.number), "AU_BANK_ACCOUNT"),
            f", {_ref(rng, 'FT')})",
        ],
        lambda: ["Transfer to other Bank NetBank To ", (p.full, "PERSON")],
        lambda: [
            "PAYID PAYMENT FROM ",
            (p.caps, "PERSON"),
            " ",
            (p.email, "AU_PAYID"),
        ],
        lambda: ["OSKO ", _ref(rng, "P", 9), " ", (joint, joint_type), " RENT"],
        lambda: ["SALARY ", (biz.name, "ORGANIZATION_PRIVATE", True), f" {_ref(rng, '', 6)}"],
        lambda: [
            "DD ",
            ("BUDGET DIRECT INSURANCE", "ORGANIZATION", False),
            f" POLICY {rng.randrange(10**8, 10**9)}",
        ],
        lambda: [f"ATO ATO{rng.randrange(10**12, 10**13)} ACTIVITY STMT"],
        lambda: ["TFR ", (p.full, "PERSON"), f" - {rng.choice(['for taranga', 'loan', 'birthday', 'rent'])}"],
        lambda: [
            "EFTPOS ",
            (pool.merchant(), "ORGANIZATION", False),
            f" {rng.randrange(1000, 9999)} AU",
        ],
        lambda: [
            "EFTPOS ",
            (f"{pool.merchant()} {rng.choice(TOWNS).upper()}",
             "ORGANIZATION", False),
            f" {rng.randrange(1000, 9999)} AU",
        ],
        lambda: ["RENT ", (p.street.upper(), "ADDRESS_BARE"),
                 " ", _ref(rng, "RW")],
        lambda: [
            "#",
            (pool.merchant(), "ORGANIZATION", False),
            f" AU INV-{rng.randrange(10**7, 10**8)} AU",
        ],
        lambda: [
            "PAYMENT TO ",
            (rng.choice(AND_ORGS_PRIVATE), "ORGANIZATION_PRIVATE", True),
            f" INV {rng.randrange(10**5, 10**6)}",
        ],
        lambda: [
            "TFR ",
            (rng.choice(AND_ORGS_GUARDED), "ORGANIZATION_AND", False),
            f" {_ref(rng, 'R')}",
        ],
        lambda: [
            "EFTPOS ",
            (rng.choice(AND_ORGS_BARE), "ORGANIZATION_AND_BARE", False),
            f" {rng.randrange(1000, 9999)} AU",
        ],
        lambda: ["Loan Repayment ", (joint, joint_type)],
        lambda: [
            "Interest Charged From A/C ",
            (acct.number, "AU_BANK_ACCOUNT"),
        ],
        lambda: [
            "Online ",
            _ref(rng, "W", 9),
            " Loan to ",
            (biz.name, "ORGANIZATION_PRIVATE", True),
            " ",
            (joint, joint_type),
        ],
        lambda: [
            "DIRECT CREDIT ",
            (couple_a.caps, "PERSON"),
            " MOB ",
            (couple_a.mobile, "PHONE_NUMBER"),
        ],
    ]
    return rng.choice(patterns)()


def amounts(rng: random.Random, balance: float) -> tuple[str, str, float]:
    """(debit, credit, new_balance) — one of debit/credit is empty."""
    amt = _amount(rng)
    if rng.random() < 0.5:
        return f"{amt:,.2f}", "", round(balance - amt, 2)
    return "", f"{amt:,.2f}", round(balance + amt, 2)
