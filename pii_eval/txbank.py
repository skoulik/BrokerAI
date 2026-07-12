"""Transaction-description patterns modeled on the reference statements.

Each pattern returns a list of parts consumable by build.Doc: plain strings,
or (value, entity_type) / (value, entity_type, strip_expected) tuples.
Receipt/reference codes (FT..., W10..., REF...) are left un-annotated on
purpose: they are neither required strips nor protected keeps, so the scorer
ignores whatever the pipeline does with them.
"""

import random

from pii_eval import au
from pii_eval.personas import Pool


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
    joint = rng.choice(
        [
            f"{couple_a.first[0]} & {couple_b.first[0]} {couple_a.last}",
            f"{couple_a.first} and {couple_b.first} {couple_a.last}",
            couple_a.reversed_caps,
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
        lambda: ["OSKO ", _ref(rng, "P", 9), " ", (joint, "PERSON"), " RENT"],
        lambda: ["SALARY ", (biz.name, "ORGANIZATION", False), f" {_ref(rng, '', 6)}"],
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
            "#",
            (pool.merchant(), "ORGANIZATION", False),
            f" AU INV-{rng.randrange(10**7, 10**8)} AU",
        ],
        lambda: ["Loan Repayment ", (joint, "PERSON")],
        lambda: [
            "Interest Charged From A/C ",
            (acct.number, "AU_BANK_ACCOUNT"),
        ],
        lambda: [
            "Online ",
            _ref(rng, "W", 9),
            " Loan to ",
            (biz.name, "ORGANIZATION", False),
            " ",
            (joint, "PERSON"),
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
