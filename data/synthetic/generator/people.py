"""Names, contact data, addresses and vehicles: invented by construction.

Names are random pairs from short lists of common names of Austria, Slovakia,
Slovenia, Croatia and Hungary. E-mail addresses use the reserved example.com
domain. There are no phone numbers, dates of birth, IBANs or national IDs, and
a registration has a format that no country issues.
"""

import random
import unicodedata

GIVEN_NAMES = (
    "Anna",
    "Lukas",
    "Katarína",
    "Marek",
    "Petra",
    "Tomáš",
    "Eva",
    "Jakub",
    "Mateja",
    "Luka",
    "Ivana",
    "Marko",
    "Zsófia",
    "Bence",
    "Lena",
    "Stefan",
    "Nina",
    "Dávid",
    "Hana",
    "Matej",
)
SURNAMES = (
    "Gruber",
    "Huber",
    "Bauer",
    "Wagner",
    "Pichler",
    "Müller",
    "Novák",
    "Kováč",
    "Horváth",
    "Šimko",
    "Kovačič",
    "Novak",
    "Horvat",
    "Potočnik",
    "Babić",
    "Marković",
    "Kovács",
    "Nagy",
    "Varga",
    "Tóth",
)
STREETS = (
    "Maple Street",
    "Orchard Lane",
    "Linden Road",
    "Willow Way",
    "Cedar Court",
    "Harbour View",
    "Mill Road",
    "Station Avenue",
    "Meadow Close",
    "Chestnut Row",
    "Riverside Drive",
    "Hillcrest Road",
)
CITIES = (
    ("Vienna", "AT"),
    ("Graz", "AT"),
    ("Linz", "AT"),
    ("Bratislava", "SK"),
    ("Košice", "SK"),
    ("Ljubljana", "SI"),
    ("Maribor", "SI"),
    ("Zagreb", "HR"),
    ("Split", "HR"),
)
VEHICLES = (
    ("Škoda", "Octavia"),
    ("Škoda", "Fabia"),
    ("Volkswagen", "Golf"),
    ("Volkswagen", "Passat"),
    ("Toyota", "Corolla"),
    ("Opel", "Astra"),
    ("Renault", "Clio"),
    ("Ford", "Focus"),
    ("Hyundai", "i30"),
    ("Kia", "Ceed"),
    ("Seat", "Leon"),
    ("Peugeot", "308"),
    ("Dacia", "Duster"),
    ("Suzuki", "Vitara"),
)
MAX_STREET_NUMBER = 120
FIRST_VEHICLE_YEAR = 2012
LAST_VEHICLE_YEAR = 2025
BUILDING_TYPES = ("apartment", "house")
EMAIL_DOMAIN = "example.com"


def ascii_fold(text: str) -> str:
    """Lower-case ASCII form of ``text``: accents are dropped, not transliterated."""
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def make_address(rng: random.Random) -> dict[str, str]:
    city, country = rng.choice(CITIES)
    street = f"{rng.choice(STREETS)} {rng.randint(1, MAX_STREET_NUMBER)}"
    return {"street": street, "city": city, "country": country}


def make_holder(rng: random.Random, number: int) -> dict[str, object]:
    """A policy holder; ``number`` keeps the e-mail address unique."""
    given = rng.choice(GIVEN_NAMES)
    surname = rng.choice(SURNAMES)
    email = f"{ascii_fold(given)}.{ascii_fold(surname)}{number}@{EMAIL_DOMAIN}"
    return {
        "name": f"{given} {surname}",
        "email": email,
        "address": make_address(rng),
    }


def make_vehicle(rng: random.Random, registration: str) -> dict[str, object]:
    make, model = rng.choice(VEHICLES)
    year = rng.randint(FIRST_VEHICLE_YEAR, LAST_VEHICLE_YEAR)
    return {"make": make, "model": model, "year": year, "registration": registration}


def make_building(rng: random.Random, address: dict[str, str]) -> dict[str, object]:
    return {"address": dict(address), "building_type": rng.choice(BUILDING_TYPES)}


def draw_registrations(rng: random.Random, count: int) -> tuple[str, ...]:
    """Unique registrations in the invented ``SYN-`` + 4 digits format."""
    return tuple(
        f"SYN-{number:04d}" for number in rng.sample(range(1000, 10000), count)
    )


def random_place(rng: random.Random) -> dict[str, str]:
    city, country = rng.choice(CITIES)
    return {"city": city, "country": country}
