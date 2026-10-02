"""The terms of a policy wording, picked from the clauses a search returned.

Pure: no I/O. This is the only module that knows how a Meridian wording is laid
out (``data/synthetic/wordings/``): the section a clause sits in, the titles it
carries and the sentence that closes an exclusion. ``wording_search`` returns
clauses (``clause``, ``section``, ``title``, ``body``, ``keyword_match``); the
section is read from the number of ``clause``, not from the ``section`` title.

The probes are built from the peril alone, never from the claim's text: a claim
description is data that a customer wrote and does not reach a search query.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .models import Peril

# The catalogue's titles (data/synthetic/generator/catalogue.py); a test keeps
# the two equal. The lower-case form is the peril's label in running text.
PERIL_TITLES: Mapping[Peril, str] = MappingProxyType(
    {
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
)

# Two probes, not one: a probe of five topics returns ten clauses and the Limit
# clause, which matches by its title only, was not among them.
AMOUNTS_PROBE = "Deductible. Limit."
TIMING_PROBE = "Reporting a claim. Period of cover. Lapse for non-payment."

# The number of section-3 clauses of each wording, by (product, wording
# version). Clauses 3.1 to 3.k that are all retrieved are not the whole section
# when the search missed the last one, and nothing in the clauses shows it, so
# the count is known. A pair that is not here is never complete. A test keeps
# the table equal to the wordings under data/synthetic/wordings/.
EXCLUSION_CLAUSES: Mapping[tuple[str, str], int] = MappingProxyType(
    {
        ("HOME-STD", "2026-01"): 4,
        ("HOME-PLUS", "2026-01"): 2,
        ("MOTOR-COMP", "2026-01"): 3,
        ("MOTOR-TPL", "2026-01"): 2,
    }
)

COVER_SECTION = "2"
EXCLUSION_SECTION = "3"
AMOUNTS_SECTION = "4"
CLAIM_SECTION = "5"
PERIOD_SECTION = "6"

CLAUSE_NUMBER = re.compile(r"[0-9]+\.[0-9]+")
# The closing sentence of an exclusion, read from a body whose whitespace is
# normalised. It must open the body or follow the end of another sentence.
APPLIES_TO = re.compile(
    r"(?:\A|(?<=[.!?] ))This exclusion applies to claims for "
    r"([^.]+?)(, which this product does not cover)?\.\Z"
)
NAME_SEPARATOR = re.compile(r", | and ")
KNOWN_LABELS = frozenset(title.lower() for title in PERIL_TITLES.values())


@dataclass(frozen=True, slots=True)
class Clause:
    clause: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class Terms:
    cover: Clause | None
    documents: Clause | None
    peril_exclusion: Clause | None
    candidates: tuple[Clause, ...]
    exclusions_complete: bool
    deductible: Clause | None
    limit: Clause | None
    reporting: Clause | None
    period: Clause | None
    lapse: Clause | None


@dataclass(frozen=True, slots=True)
class _Exclusion:
    clause: Clause
    labels: frozenset[str]
    peril_not_covered: bool


def _label(peril: Peril) -> str:
    return PERIL_TITLES[peril].lower()


def probes(peril: Peril, *, in_force: bool) -> tuple[str, ...]:
    """The fixed search queries for ``peril``. The three that look for the peril's
    cover, its exclusions and the amounts (deductible and limit) are asked only
    when the policy was in force; the timing probe is always asked."""
    if not in_force:
        return (TIMING_PROBE,)
    return (
        PERIL_TITLES[peril],
        f"This exclusion applies to claims for {_label(peril)}.",
        AMOUNTS_PROBE,
        TIMING_PROBE,
    )


def _number(clause: str) -> tuple[int, ...]:
    return tuple(int(part) for part in clause.split("."))


def _section(clause: str) -> str:
    return clause.partition(".")[0]


def _unique_clauses(chunks: Iterable[Mapping[str, Any]]) -> list[Clause]:
    """Each clause once, the first chunk that names it, in numeric order."""
    found: dict[str, Clause] = {}
    for chunk in chunks:
        number = chunk["clause"]
        if not isinstance(number, str) or not CLAUSE_NUMBER.fullmatch(number):
            raise ValueError("a chunk's clause is not a number of the form n.m")
        found.setdefault(number, Clause(number, chunk["title"], chunk["body"]))
    return sorted(found.values(), key=lambda clause: _number(clause.clause))


def _read_exclusion(clause: Clause) -> _Exclusion | None:
    """The exclusion's perils, or None when its closing sentence cannot be read
    or names something that is not a known peril."""
    found = APPLIES_TO.search(" ".join(clause.body.split()))
    if found is None:
        return None
    labels = frozenset(name.strip() for name in NAME_SEPARATOR.split(found[1]))
    if not labels <= KNOWN_LABELS:
        return None
    return _Exclusion(clause, labels, peril_not_covered=found[2] is not None)


def _exclusions_are_numbered_without_gap(clauses: list[Clause]) -> bool:
    return [_number(clause.clause) for clause in clauses] == [
        (int(EXCLUSION_SECTION), position) for position in range(1, len(clauses) + 1)
    ]


def _titled(clauses: list[Clause], section: str, title: str) -> Clause | None:
    return next(
        (c for c in clauses if _section(c.clause) == section and c.title == title),
        None,
    )


def select_terms(
    peril: Peril,
    chunks: Iterable[Mapping[str, Any]],
    *,
    product: str,
    wording_version: str,
) -> Terms:
    """The terms that bear on a claim for ``peril``, from the clauses retrieved
    of the wording ``product`` at ``wording_version``.

    Chunks may repeat a clause and come in any order. The exclusions are
    complete only when every section-3 clause is readable, numbered without a
    gap, and as many as ``EXCLUSION_CLAUSES`` says. Raises ``ValueError`` for a
    chunk whose clause number is not of the form ``n.m``."""
    clauses = _unique_clauses(chunks)
    label = _label(peril)
    in_exclusions = [c for c in clauses if _section(c.clause) == EXCLUSION_SECTION]
    readable = [
        exclusion
        for exclusion in map(_read_exclusion, in_exclusions)
        if exclusion is not None
    ]
    naming_peril = [e for e in readable if label in e.labels]
    return Terms(
        cover=_titled(clauses, COVER_SECTION, PERIL_TITLES[peril]),
        documents=_titled(clauses, CLAIM_SECTION, f"Documents for {label}"),
        peril_exclusion=next(
            (e.clause for e in naming_peril if e.peril_not_covered), None
        ),
        candidates=tuple(e.clause for e in naming_peril if not e.peril_not_covered),
        # No section-3 clause at all is not complete: a wording without
        # exclusions is not credible, so the search missed them.
        exclusions_complete=len(in_exclusions) > 0
        and len(in_exclusions) == EXCLUSION_CLAUSES.get((product, wording_version))
        and len(readable) == len(in_exclusions)
        and _exclusions_are_numbered_without_gap(in_exclusions),
        deductible=_titled(clauses, AMOUNTS_SECTION, "Deductible"),
        limit=_titled(clauses, AMOUNTS_SECTION, "Limit"),
        reporting=_titled(clauses, CLAIM_SECTION, "Reporting a claim"),
        period=_titled(clauses, PERIOD_SECTION, "Period of cover"),
        lapse=_titled(clauses, PERIOD_SECTION, "Lapse for non-payment"),
    )
