"""Name-form statistics document: many DISTINCT names per surface form.

The realistic templates draw joint/reversed forms from an 8-person pool a
handful of times per corpus — enough to detect a gap, too few to measure
it (PERSON_REVERSED swung 20-75% between seeds on n=5). This document
gives every documented-hard person surface form a real sample: each bank
entry appears once per form, so per-form n is fixed by construction and
independent of seed (seeds only shuffle frames/casing).

Truth types (per-form convention, none gated until fixed):

- PERSON_REVERSED — 'SCHAEFER JOSEPH' statement order, drawn across all
  three name classes below (reversed particle surnames are the measured
  worst case).
- PERSON_COMMA — 'SCHAEFER, JOSEPH' (common on real statements; fails in
  junk context per the 2026-07-15 probes).
- PERSON_PARTICLE — canonical-order particle surnames ('Jan van den
  Berg'); robust in probes, watched for regression.
- PERSON_MULTIWORD — canonical-order multi-token non-Anglo names
  ('Maria Garcia Lopez', 'Rajesh Kumar Sharma'); same.
- Plain-Anglo canonical mentions ride the gated PERSON type (established
  100%); they double as the interference source: the same person appears
  canonically AND reversed in one document, the exact condition of the
  same-window interference diagnosis (pii/DONE.md).
"""

import csv
import io
import random

from pii_eval.templates_csv import HEADER, CellAnn
from pii_eval.personas import Pool
from pii_eval import txbank

ANGLO_NAMES = [
    ("Emily", "Watson"), ("Jack", "Turner"), ("Oliver", "Harris"),
    ("Charlotte", "Bennett"), ("Henry", "Dawson"), ("Amelia", "Foster"),
    ("Thomas", "Reid"), ("Grace", "Mitchell"), ("Samuel", "Clarke"),
    ("Isla", "Ferguson"), ("Ethan", "Brooks"), ("Ruby", "Coleman"),
]
PARTICLE_NAMES = [
    ("Jan", "van den Berg"), ("Sophie", "de Jong"), ("Marco", "Di Stefano"),
    ("Liam", "O'Connor"), ("Heidi", "von Schulz"), ("Lucas", "van der Meer"),
    ("Pieter", "de Villiers"), ("Elena", "Della Rosa"),
    ("Sean", "Mac Namara"), ("Anouk", "van Dijk"),
]
MULTIWORD_NAMES = [
    ("Maria", "Garcia Lopez"), ("Carlos", "Mendoza Ruiz"),
    ("Ana", "Silva Santos"), ("Rajesh", "Kumar Sharma"),
    ("Venkata", "Subramanian Iyer"), ("Priya", "Ramachandran"),
    ("Lakshmi", "Narayanan"), ("Wei", "Zhang"),
    ("Yuki", "Tanaka"), ("Amara", "Okafor"),
]


def _canonical_frames(rng: random.Random, ref):
    return rng.choice([
        lambda name: ["PAYID PAYMENT FROM ", name],
        lambda name: ["Transfer to other Bank NetBank To ", name],
        lambda name: ["DIRECT CREDIT ", name, f" {ref()}"],
    ])


def _reversed_frames(rng: random.Random, ref):
    return rng.choice([
        lambda name: ["OSKO ", ref(), " ", name, " RENT"],
        lambda name: ["TFR ", name, " - rent"],
        lambda name: ["DIRECT DEBIT ", name],
    ])


def name_forms_csv(pool: Pool) -> tuple[str, list[CellAnn]]:
    """One transaction CSV carrying the full name-form battery."""
    rng = pool.rng

    def ref():
        return txbank._ref(rng, rng.choice("PWR"), 9)

    entries = []  # (description parts, value, truth type)

    def add(parts_fn, value, ttype):
        entries.append((parts_fn(value), value, ttype))

    def caps_half(s):
        # statements mix ALL-CAPS and title case about evenly
        return s.upper() if rng.random() < 0.5 else s

    for first, last in ANGLO_NAMES:
        add(_canonical_frames(rng, ref),
            caps_half(f"{first} {last}"), "PERSON")
    for first, last in PARTICLE_NAMES:
        add(_canonical_frames(rng, ref),
            caps_half(f"{first} {last}"), "PERSON_PARTICLE")
    for first, last in MULTIWORD_NAMES:
        add(_canonical_frames(rng, ref),
            caps_half(f"{first} {last}"), "PERSON_MULTIWORD")
    for first, last in ANGLO_NAMES + PARTICLE_NAMES + MULTIWORD_NAMES:
        add(_reversed_frames(rng, ref), f"{last} {first}".upper(),
            "PERSON_REVERSED")
    for first, last in ANGLO_NAMES[::2] + PARTICLE_NAMES[::2] \
            + MULTIWORD_NAMES[::2]:
        add(_reversed_frames(rng, ref), f"{last}, {first}".upper(),
            "PERSON_COMMA")

    rng.shuffle(entries)

    rows: list[list[str]] = [HEADER]
    anns: list[CellAnn] = []
    balance = round(rng.uniform(100, 50000), 2)
    year = rng.choice([2023, 2024, 2025])
    for parts, value, ttype in entries:
        cell = "".join(parts)
        row = len(rows)
        anns.append(CellAnn(ttype, value, row, 1))
        debit, credit, balance = txbank.amounts(rng, balance)
        rows.append([
            f"{rng.randrange(1, 29):02d}/{rng.randrange(1, 13):02d}/{year}",
            cell, debit, credit, f"{balance:,.2f}",
        ])

    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue(), anns
