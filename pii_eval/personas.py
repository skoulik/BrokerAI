"""Persona pool shared across a corpus run.

One pool per run so the same people/accounts recur across documents —
that is what lets the eval later check pseudonym consistency across a
document set. Merchant organizations are drawn from a fixed plausible-AU
list; they are ground-truthed as ORGANIZATION with strip_expected=False
(the pipeline keeps merchants by default).
"""

import random
from dataclasses import dataclass, field

from faker import Faker

from pii_eval import au


@dataclass
class Person:
    first: str
    last: str
    title: str
    dob: str
    mobile: str
    email: str
    street: str
    suburb: str
    state: str
    postcode: str

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def caps(self) -> str:
        return self.full.upper()

    @property
    def reversed_caps(self) -> str:  # "KULIK OLGA" style
        return f"{self.last} {self.first}".upper()

    @property
    def address_oneline(self) -> str:
        return f"{self.street}, {self.suburb} {self.state} {self.postcode}"


@dataclass
class Business:
    name: str            # "OAKFIELD CONSULTING PTY LTD"
    abn: str
    acn: str
    trust: str | None    # "OAKFIELD FAMILY TRUST"


@dataclass
class Account:
    bank: str
    bsb: str
    number: str
    holder: str          # display name on the statement


@dataclass
class Pool:
    rng: random.Random
    people: list[Person]
    businesses: list[Business]
    accounts: list[Account]
    merchants: list[str] = field(default_factory=list)

    def person(self) -> Person:
        return self.rng.choice(self.people)

    def couple(self) -> tuple[Person, Person]:
        """Two people sharing a surname — joint-account name forms
        ('S & O Kulik', 'Olga and Sergei Kulik') are the hard NER cases."""
        a, b = self.rng.sample(self.people, 2)
        b = Person(**{**b.__dict__, "last": a.last})
        return a, b

    def business(self) -> Business:
        return self.rng.choice(self.businesses)

    def account(self) -> Account:
        return self.rng.choice(self.accounts)

    def merchant(self) -> str:
        return self.rng.choice(self.merchants)


MERCHANTS = [
    "WOOLWORTHS", "COLES EXPRESS", "BUNNINGS WAREHOUSE", "KMART",
    "AMAZON AU", "XERO AU", "OFFICEWORKS", "TELSTRA", "AGL ENERGY",
    "SHELL COLES EX", "MCDONALDS", "CHEMIST WAREHOUSE", "UBER *TRIP",
    "NETFLIX.COM", "SPOTIFY", "JB HI-FI", "AUSTRALIA POST", "ALDI STORES",
]

_TRUST_KIND = ["FAMILY", "BUSINESS", "PROPERTY", "INVESTMENT"]


def make_pool(seed: int, n_people: int = 8, n_businesses: int = 3) -> Pool:
    rng = random.Random(seed)
    fake = Faker("en_AU")
    fake.seed_instance(seed)

    people = []
    for _ in range(n_people):
        first, last = fake.first_name(), fake.last_name()
        people.append(
            Person(
                first=first,
                last=last,
                title=rng.choice(["Mr", "Mrs", "Ms", "Dr"]),
                dob=fake.date_of_birth(minimum_age=21, maximum_age=75)
                    .strftime(rng.choice(["%d/%m/%Y", "%d %b %Y"])),
                mobile=au.mobile(rng),
                email=f"{first}.{last}@{rng.choice(['gmail.com', 'outlook.com', 'bigpond.com'])}".lower(),
                street=fake.street_address(),
                suburb=fake.city(),
                state=fake.state_abbr(),
                postcode=fake.postcode(),
            )
        )

    businesses = []
    for _ in range(n_businesses):
        stem = f"{fake.last_name().upper()} {rng.choice(['CONSULTING', 'HOLDINGS', 'MANAGEMENT', 'SERVICES'])}"
        businesses.append(
            Business(
                name=f"{stem} PTY LTD",
                abn=au.abn(rng),
                acn=au.acn(rng),
                trust=f"{stem.split()[0]} {rng.choice(_TRUST_KIND)} TRUST"
                if rng.random() < 0.7 else None,
            )
        )

    accounts = []
    for holder in [p.full for p in people[:4]] + [b.name for b in businesses]:
        bank, _ = rng.choice(au.BANKS)
        accounts.append(
            Account(
                bank=bank,
                bsb=au.bsb(rng, bank),
                number=au.account_number(rng),
                holder=holder,
            )
        )

    return Pool(
        rng=rng,
        people=people,
        businesses=businesses,
        accounts=accounts,
        merchants=list(MERCHANTS),
    )
