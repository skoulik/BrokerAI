"""Checksum-valid Australian identifier generators.

Presidio's AU recognizers validate check digits (TFN mod-11, ABN mod-89,
ACN complement, Medicare mod-10), so the corpus must contain arithmetically
valid values or layer 1 would legitimately reject them.

All functions take a random.Random for reproducible corpora and return
formatted surface strings; use digits() to get the bare value.
"""

import random

DIGITS = "0123456789"


def digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def _rand_digits(rng: random.Random, n: int) -> list[int]:
    return [rng.randrange(10) for _ in range(n)]


def tfn(rng: random.Random) -> str:
    # weights (1,4,3,7,5,8,6,9,10); weighted sum divisible by 11
    weights = (1, 4, 3, 7, 5, 8, 6, 9, 10)
    while True:
        d = _rand_digits(rng, 8)
        check = sum(w * x for w, x in zip(weights, d)) % 11
        # last weight is 10 ≡ -1 (mod 11), so digit == check closes the sum
        if check <= 9:
            d.append(check)
            s = "".join(map(str, d))
            return f"{s[:3]} {s[3:6]} {s[6:]}"


def medicare(rng: random.Random, irn: bool = True) -> str:
    # d1 in 2..6; d9 = weighted sum of d1..d8 with (1,3,7,9,1,3,7,9) mod 10;
    # d10 is the card issue number
    weights = (1, 3, 7, 9, 1, 3, 7, 9)
    d = [rng.randrange(2, 7)] + _rand_digits(rng, 7)
    d.append(sum(w * x for w, x in zip(weights, d)) % 10)
    d.append(rng.randrange(1, 10))
    s = "".join(map(str, d))
    out = f"{s[:4]} {s[4:9]} {s[9]}"
    return f"{out} {rng.randrange(1, 10)}" if irn else out


def abn(rng: random.Random) -> str:
    # weights (10,1,3,5,7,9,11,13,15,17,19) with 1 subtracted from the first
    # digit; weighted sum divisible by 89. The first two digits' weighted
    # contribution (d1-1)*10 + d2 covers 0..89, so they can always be solved
    # for any random tail.
    tail_weights = (3, 5, 7, 9, 11, 13, 15, 17, 19)
    tail = _rand_digits(rng, 9)
    need = -sum(w * x for w, x in zip(tail_weights, tail)) % 89
    s = str(need // 10 + 1) + str(need % 10) + "".join(map(str, tail))
    return f"{s[:2]} {s[2:5]} {s[5:8]} {s[8:]}"


def acn(rng: random.Random) -> str:
    weights = (8, 7, 6, 5, 4, 3, 2, 1)
    d = _rand_digits(rng, 8)
    d.append((10 - sum(w * x for w, x in zip(weights, d)) % 10) % 10)
    s = "".join(map(str, d))
    return f"{s[:3]} {s[3:6]} {s[6:]}"


# (bank, BSB prefix) pools mirroring real allocations so statements look
# plausible; the remaining four digits are random.
BANKS = [
    ("ANZ", "01"),
    ("Westpac", "03"),
    ("Commonwealth Bank", "06"),
    ("NAB", "08"),
    ("St George", "11"),
    ("Macquarie", "18"),
    ("Bankwest", "30"),
    ("Bendigo Bank", "63"),
    ("ME Bank", "94"),
]


def bsb(rng: random.Random, bank: str | None = None) -> str:
    prefix = dict(BANKS).get(bank) or rng.choice(BANKS)[1]
    rest = "".join(str(rng.randrange(10)) for _ in range(4))
    return f"{prefix}{rest[0]}-{rest[1:]}"


def account_number(rng: random.Random) -> str:
    styles = [
        lambda s: s,                        # 018057571
        lambda s: f"{s[:2]}-{s[2:5]}-{s[5:]}",  # 32-151-6825
        lambda s: f"{s[:4]}-{s[4:]}",       # 6874-72521
        lambda s: f"{s[:6]}-{s[6:]}",       # 162097-1114 style
    ]
    n = rng.choice([8, 9, 9, 10])
    s = "".join(str(rng.randrange(10)) for _ in range(n))
    return rng.choice(styles)(s)


def card_number(rng: random.Random) -> str:
    d = [4] + _rand_digits(rng, 14)  # Visa-style
    # Luhn check digit
    total = 0
    for i, x in enumerate(reversed(d)):
        x = x * 2 if i % 2 == 0 else x  # positions counted before the check digit
        total += x - 9 if x > 9 else x
    d.append((10 - total % 10) % 10)
    s = "".join(map(str, d))
    return " ".join(s[i : i + 4] for i in range(0, 16, 4))


def mobile(rng: random.Random) -> str:
    s = "04" + "".join(str(rng.randrange(10)) for _ in range(8))
    return f"{s[:4]} {s[4:7]} {s[7:]}"


def landline(rng: random.Random) -> str:
    area = rng.choice(["02", "03", "07", "08"])
    s = "".join(str(rng.randrange(10)) for _ in range(8))
    return f"({area}) {s[:4]} {s[4:]}"


def rego(rng: random.Random) -> str:
    letters = lambda n: "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(n))
    nums = lambda n: "".join(str(rng.randrange(10)) for _ in range(n))
    return rng.choice(
        [
            lambda: letters(3) + nums(3),        # VIC ABC123
            lambda: nums(3) + letters(3),        # QLD 123ABC
            lambda: letters(2) + nums(2) + letters(2),  # NSW AB12CD
            lambda: "S" + nums(3) + letters(3),  # SA S123ABC
        ]
    )()


def drivers_licence(rng: random.Random) -> str:
    return "".join(str(rng.randrange(10)) for _ in range(rng.choice([8, 9])))
