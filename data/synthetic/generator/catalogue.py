"""The product catalogue: the single source of the wordings and the labels.

The wording renderer writes its headings from the clause functions below and
the oracle cites the same functions, so a clause and the label that cites it
cannot disagree. Nothing here is random.
"""

from dataclasses import dataclass
from datetime import date

# -- parameters ---------------------------------------------------------------
DEFAULT_SEED = 20260929
# The dataset's clock. Nothing is reported after it and no code reads the real
# date, so a rerun never depends on when it runs.
REFERENCE_DATE = date(2026, 9, 1)
CURRENCY = "EUR"
WORDING_VERSION = "2026-01"
AUTO_APPROVAL_LIMIT = (
    2500  # payable euros; payable up to and including this may auto-approve
)
REPORTING_WINDOW_DAYS = 30
EARLY_LOSS_DAYS = 30
FREQUENT_CLAIMS_COUNT = 2
FREQUENT_CLAIMS_WINDOW_DAYS = 365

# -- vocabularies -------------------------------------------------------------
ROUTES = ("auto_approve", "adjuster", "request_documents")
REASONS = (
    "within_threshold",
    "over_threshold",
    "fraud_indicator",
    "excluded",
    "policy_inactive",
    "missing_documents",
)
FRAUD_INDICATORS = ("early_loss", "frequent_claims", "late_report")
KIND_PERIL = "peril"  # the peril itself is not covered by the product
KIND_CIRCUMSTANCE = "circumstance"  # a covered peril under a named circumstance

# -- perils and documents -----------------------------------------------------
LINE_PERILS = {
    "motor": (
        "collision",
        "theft",
        "fire",
        "glass",
        "storm",
        "third_party_liability",
    ),
    "home": (
        "fire",
        "storm",
        "flood",
        "burst_pipe",
        "burglary",
        "accidental_damage",
    ),
}
PERIL_TITLES = {
    "collision": "Collision",
    "theft": "Theft",
    "fire": "Fire",
    "glass": "Glass",
    "storm": "Storm",
    "third_party_liability": "Third-party liability",
    "flood": "Flood",
    "burst_pipe": "Burst pipe",
    "burglary": "Burglary",
    "accidental_damage": "Accidental damage",
}
# The catalogue order of documents. Every per-peril tuple below follows it, so a
# provided or missing list in this order is unambiguous.
DOCUMENT_ORDER = ("police_report", "photos", "repair_estimate", "accident_statement")
DOCUMENT_TITLES = {
    "police_report": "Police report",
    "photos": "Photos",
    "repair_estimate": "Repair estimate",
    "accident_statement": "Accident statement",
}
REQUIRED_DOCUMENTS = {
    "collision": ("photos", "repair_estimate"),
    "theft": ("police_report",),
    "fire": ("photos",),
    "glass": ("photos",),
    "storm": ("photos",),
    "flood": ("photos",),
    "burst_pipe": ("photos", "repair_estimate"),
    "burglary": ("police_report", "photos"),
    "accidental_damage": ("photos",),
    "third_party_liability": ("accident_statement",),
}

# -- fixed clauses (the same in every wording) --------------------------------
DEDUCTIBLE_CLAUSE = "4.1"
LIMIT_CLAUSE = "4.2"
REPORTING_CLAUSE = "5.1"
PERIOD_CLAUSE = "6.1"
LAPSE_CLAUSE = "6.2"


@dataclass(frozen=True)
class Exclusion:
    """An exclusion clause; ``perils`` are the perils it applies to."""

    code: str
    title: str
    kind: str
    perils: tuple[str, ...]


@dataclass(frozen=True)
class Product:
    """A product; ``covered`` and ``exclusions`` keep wording order."""

    code: str
    name: str
    line: str
    covered: tuple[str, ...]
    exclusions: tuple[Exclusion, ...]
    deductible: int
    # A fixed limit, or None when the limit is the policy's sum insured.
    fixed_limit: int | None
    # (low, high, step) of the sum insured drawn for a policy, or None.
    sum_insured_range: tuple[int, int, int] | None


_MOTOR_OWN = ("collision", "theft", "fire", "glass", "storm")
_COLLISION = ("collision",)
_TPL = ("third_party_liability",)
_WEAR_AND_TEAR = ("storm", "burst_pipe")


def _peril(code: str, title: str, perils: tuple[str, ...]) -> Exclusion:
    return Exclusion(code, title, KIND_PERIL, perils)


def _circumstance(code: str, title: str, perils: tuple[str, ...]) -> Exclusion:
    return Exclusion(code, title, KIND_CIRCUMSTANCE, perils)


PRODUCTS = {
    "MOTOR-TPL": Product(
        code="MOTOR-TPL",
        name="Motor Third-Party Liability",
        line="motor",
        covered=_TPL,
        exclusions=(
            _peril("own_vehicle_damage", "Own vehicle damage", _MOTOR_OWN),
            _circumstance("racing", "Racing", _TPL),
        ),
        deductible=0,
        fixed_limit=1_000_000,
        sum_insured_range=None,
    ),
    "MOTOR-COMP": Product(
        code="MOTOR-COMP",
        name="Motor Comprehensive",
        line="motor",
        covered=LINE_PERILS["motor"],
        exclusions=(
            _circumstance("racing", "Racing", _COLLISION),
            _circumstance(
                "driving_under_influence",
                "Driving under the influence",
                _COLLISION,
            ),
            _circumstance("unlicensed_driver", "Unlicensed driver", _COLLISION),
        ),
        deductible=300,
        fixed_limit=None,
        sum_insured_range=(8_000, 45_000, 500),
    ),
    "HOME-STD": Product(
        code="HOME-STD",
        name="Home Standard",
        line="home",
        covered=("fire", "storm", "burst_pipe", "burglary"),
        exclusions=(
            _peril("flood", "Flood", ("flood",)),
            _peril("accidental_damage", "Accidental damage", ("accidental_damage",)),
            _circumstance("wear_and_tear", "Wear and tear", _WEAR_AND_TEAR),
            _circumstance("gradual_leak", "Gradual leaks", ("burst_pipe",)),
        ),
        deductible=250,
        fixed_limit=None,
        sum_insured_range=(80_000, 400_000, 5_000),
    ),
    "HOME-PLUS": Product(
        code="HOME-PLUS",
        name="Home Plus",
        line="home",
        covered=LINE_PERILS["home"],
        exclusions=(
            _circumstance("wear_and_tear", "Wear and tear", _WEAR_AND_TEAR),
            _circumstance("gradual_leak", "Gradual leaks", ("burst_pipe",)),
        ),
        deductible=150,
        fixed_limit=None,
        sum_insured_range=(80_000, 400_000, 5_000),
    ),
}
EXCLUSION_CODES = (
    "own_vehicle_damage",
    "racing",
    "driving_under_influence",
    "unlicensed_driver",
    "flood",
    "accidental_damage",
    "wear_and_tear",
    "gradual_leak",
)


# -- clause numbering ---------------------------------------------------------
def cover_clause(product: Product, peril: str) -> str:
    """Section 2 has one clause per covered peril, in line order."""
    return f"2.{product.covered.index(peril) + 1}"


def exclusion_clause(product: Product, code: str) -> str:
    """Section 3 has one clause per exclusion, in catalogue order."""
    codes = [exclusion.code for exclusion in product.exclusions]
    return f"3.{codes.index(code) + 1}"


def documents_clause(product: Product, peril: str) -> str:
    """Section 5 has 5.1 for reporting, then one documents clause per covered peril."""
    return f"5.{product.covered.index(peril) + 2}"


def peril_label(peril: str) -> str:
    """Lower-case name of a peril, as used in running text."""
    return PERIL_TITLES[peril].lower()


# -- invariants ---------------------------------------------------------------
def validate_catalogue() -> None:
    """Raise ValueError when the catalogue contradicts its own rules."""
    _validate_documents()
    for product in PRODUCTS.values():
        _validate_product(product)


def _validate_documents() -> None:
    for peril, documents in REQUIRED_DOCUMENTS.items():
        ranks = [DOCUMENT_ORDER.index(document) for document in documents]
        if ranks != sorted(ranks) or not documents:
            raise ValueError(f"documents of {peril} do not follow DOCUMENT_ORDER")
    every_peril = {peril for perils in LINE_PERILS.values() for peril in perils}
    if every_peril != set(REQUIRED_DOCUMENTS):
        raise ValueError("REQUIRED_DOCUMENTS must name every peril exactly once")


def _validate_product(product: Product) -> None:
    line = LINE_PERILS[product.line]
    covered_in_line_order = tuple(peril for peril in line if peril in product.covered)
    if covered_in_line_order != product.covered:
        raise ValueError(f"{product.code}: covered perils must keep line order")
    codes = [exclusion.code for exclusion in product.exclusions]
    if len(set(codes)) != len(codes) or not set(codes) <= set(EXCLUSION_CODES):
        raise ValueError(f"{product.code}: exclusion codes are duplicated or unknown")
    named_by_peril_exclusion = set()
    for exclusion in product.exclusions:
        if not set(exclusion.perils) <= set(line):
            raise ValueError(f"{product.code}: {exclusion.code} names a foreign peril")
        if exclusion.kind == KIND_PERIL:
            named_by_peril_exclusion |= set(exclusion.perils)
        elif not set(exclusion.perils) <= set(product.covered):
            raise ValueError(
                f"{product.code}: {exclusion.code} must apply to covered perils"
            )
    for peril in line:
        if (peril in product.covered) == (peril in named_by_peril_exclusion):
            raise ValueError(
                f"{product.code}: {peril} must be covered or peril-excluded, not both"
            )
