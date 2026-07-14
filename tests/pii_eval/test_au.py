"""AU identifier generators: checksum validity, and the checksum-invalid
injectors (typos / malformed) that feed the invalid-identifier eval docs."""

import random
import re

import pytest

from pii_eval import au

N = 50  # draws per property check


def _rngs():
    return [random.Random(seed) for seed in range(3)]


@pytest.mark.parametrize(
    "gen,valid",
    [(au.tfn, au.tfn_valid), (au.abn, au.abn_valid), (au.acn, au.acn_valid)],
)
def test_generators_pass_their_checksum(gen, valid):
    for rng in _rngs():
        for _ in range(N):
            assert valid(au.digits(gen(rng)))


def test_medicare_generator_valid_with_and_without_irn():
    rng = random.Random(0)
    for _ in range(N):
        assert au.medicare_valid(au.digits(au.medicare(rng)))
        assert au.medicare_valid(au.digits(au.medicare(rng, irn=False)))


def test_card_generator_passes_luhn():
    rng = random.Random(0)
    for _ in range(N):
        assert au.luhn_valid(au.digits(au.card_number(rng)))


@pytest.mark.parametrize(
    "inject,valid",
    [
        (au.invalid_tfn, au.tfn_valid),
        (au.invalid_medicare, au.medicare_valid),
        (au.invalid_abn, au.abn_valid),
        (au.invalid_acn, au.acn_valid),
        (au.invalid_card, au.luhn_valid),
    ],
)
def test_injectors_fail_their_checksum(inject, valid):
    for rng in _rngs():
        for _ in range(N):
            assert not valid(au.digits(inject(rng)))


def test_invalid_tfn_also_fails_acn_and_keeps_format():
    # 9-digit shadow patterns overlap; a typo'd TFN validating as an ACN
    # would be indistinguishable from a real ACN (flaky eval by seed).
    rng = random.Random(0)
    for _ in range(N):
        v = au.invalid_tfn(rng)
        assert re.fullmatch(r"\d{3} \d{3} \d{3}", v)
        assert not au.acn_valid(au.digits(v))


def test_invalid_medicare_keeps_structure():
    rng = random.Random(0)
    for _ in range(N):
        d = au.digits(au.invalid_medicare(rng))
        assert d[0] in "23456"  # structure intact — checksum is what fails


def test_malformed_medicare_breaks_structure_only():
    rng = random.Random(0)
    for _ in range(N):
        v = au.malformed_medicare(rng)
        assert v[0] in "01789"
        assert re.fullmatch(r"\d{4} \d{5} \d( \d)?", v)
