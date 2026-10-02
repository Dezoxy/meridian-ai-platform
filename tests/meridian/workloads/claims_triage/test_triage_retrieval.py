"""Retrieval loses nothing (S014): the terms the graph picks from what the real
``wording_search`` returns for its probes equal the terms of the whole wording.

For each product and each peril of its line (24 pairs) the probes of
``wording.probes`` run through the knowledge server in process (the store
ingested through the replay gateway, PostgreSQL, the default ``top_k``), and the
chunks go to ``select_terms``. The reference is ``select_terms`` over every
clause of the wording (``parse_wording``). The replay embedding is a hashed bag
of words, so this measures the plumbing and the probes, not a model's retrieval.
"""

import uuid
from dataclasses import dataclass, fields
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from generator import catalogue
from knowledgesupport import Gateway
from stacksupport import (
    POLICIES,
    WINDOW_SECONDS,
    replay_gateway,
    seed_and_ingest,
    whole_wording,
)
from toolsupport import World, add_claim, add_run, knowledge_server, run_call

from meridian.workloads.claims_triage.models import Peril
from meridian.workloads.claims_triage.wording import Terms, probes, select_terms

TERM_FIELDS = tuple(field.name for field in fields(Terms))
PAIRS = [
    (product.code, peril)
    for product in catalogue.PRODUCTS.values()
    for peril in catalogue.LINE_PERILS[product.line]
]


@dataclass(frozen=True, slots=True)
class Pair:
    product: str
    peril: Peril
    in_force: Terms
    lapsed: Terms
    whole: Terms
    # The clauses the amounts probe and the timing probe (the last two in-force
    # probes) returned, best first.
    amounts_probe: list[str]
    timing_probe: list[str]

    def lost(self) -> list[str]:
        """The terms the probes get wrong against the whole wording: the fields
        of the in-force probes, and the period and the lapse clause of the one
        probe of a policy that is not in force (named ``not in force: ...``)."""
        names = [
            name
            for name in TERM_FIELDS
            if getattr(self.in_force, name) != getattr(self.whole, name)
        ]
        return names + [
            f"not in force: {name}"
            for name in ("period", "lapse")
            if getattr(self.lapsed, name) != getattr(self.whole, name)
        ]

    def describe(self) -> str:
        """How each lost term differs: the probes' clause, then the whole
        wording's."""
        parts = []
        for name in self.lost():
            field, found = (
                (name.removeprefix("not in force: "), self.lapsed)
                if name.startswith("not in force: ")
                else (name, self.in_force)
            )
            parts.append(
                f"{name}: probes {numbers(getattr(found, field))} "
                f"whole {numbers(getattr(self.whole, field))}"
            )
        return "; ".join(parts)


def numbers(value: Any) -> Any:
    """Clause numbers of a clause, of a tuple of them, or of the flag."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, tuple):
        return [clause.clause for clause in value]
    return value.clause


def flat(answers: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [chunk for chunks in answers for chunk in chunks]


def search(
    gateway: Gateway, server: Any, run_id: uuid.UUID, product: str, query: str
) -> list[dict[str, Any]]:
    gateway.clock.advance(WINDOW_SECONDS)
    result = run_call(
        server, "wording_search", {"query": query, "product": product}, run_id=run_id
    )
    assert result.is_error is False, (product, query)
    return result.structured_content["chunks"]


@pytest.fixture
def found(fresh_database: DatabaseHandle) -> list[Pair]:
    gateway = replay_gateway(fresh_database)
    seed_and_ingest(gateway)
    # A claim and a running run on a policy of each product: the tool is bound
    # to the product of the run's own policy.
    policies = {
        product: next(
            p["policy_number"] for p in POLICIES.values() if p["product"] == product
        )
        for product in catalogue.PRODUCTS
    }
    runs = {}
    for index, (product, policy_number) in enumerate(policies.items(), start=1):
        claim_id = f"CLM-90{index:02d}"
        add_claim(fresh_database, claim_id, policy_number=policy_number)
        runs[product] = add_run(fresh_database, claim_id)
    server = knowledge_server(World(fresh_database, uuid.uuid4()), gateway.http)
    pairs = []
    for product, peril in PAIRS:
        answers: dict[bool, list[list[dict[str, Any]]]] = {
            in_force: [
                search(gateway, server, runs[product], product, query)
                for query in probes(peril, in_force=in_force)  # type: ignore[arg-type]
            ]
            for in_force in (True, False)
        }
        which = {"product": product, "wording_version": catalogue.WORDING_VERSION}
        pairs.append(
            Pair(
                product,
                peril,  # type: ignore[arg-type]
                select_terms(peril, flat(answers[True]), **which),  # type: ignore[arg-type]
                select_terms(peril, flat(answers[False]), **which),  # type: ignore[arg-type]
                select_terms(peril, whole_wording(product), **which),  # type: ignore[arg-type]
                [chunk["clause"] for chunk in answers[True][-2]],
                [chunk["clause"] for chunk in answers[True][-1]],
            )
        )
    return pairs


def test_the_probes_find_every_term_of_the_whole_wording(
    found: list[Pair],
) -> None:
    """Every field of the terms, in force and not in force, in all 24 pairs,
    the limit clause included."""
    agree = [pair for pair in found if not pair.lost()]
    differ = [pair for pair in found if pair.lost()]
    print()
    print(f"pairs that agree: {len(agree)} of {len(found)}")
    print(f"pairs that differ: {len(differ)}")
    for pair in differ:
        print(f"  {pair.product} {pair.peril}: {pair.describe()}")
    for product in catalogue.PRODUCTS:
        pair = next(p for p in found if p.product == product)
        print(f"  {product} amounts probe returned {pair.amounts_probe}")
        print(f"  {product} timing probe returned {pair.timing_probe}")

    assert len(found) == len(PAIRS) == 24
    assert {(p.product, p.peril) for p in found} == set(PAIRS)
    assert [(p.product, p.peril, p.lost()) for p in found] == [
        (p.product, p.peril, []) for p in found
    ]
    # The limit is no longer a clause the probe finds by luck: the amounts probe
    # returns it, in every wording.
    assert all("4.2" in pair.amounts_probe for pair in found)
    assert all(pair.in_force.limit is not None for pair in found)


def test_the_whole_wording_has_what_the_comparison_needs(found: list[Pair]) -> None:
    """The reference is not empty, so equality is not two Nones agreeing: the
    clauses of the amounts, the claim and the period are always there; the cover
    and documents clauses exist exactly for a peril the product covers; and a
    peril the product does not cover has its peril exclusion."""
    for pair in found:
        whole, product = pair.whole, catalogue.PRODUCTS[pair.product]
        covered = pair.peril in product.covered
        where = (pair.product, pair.peril)
        for name in ("deductible", "limit", "reporting", "period", "lapse"):
            assert getattr(whole, name) is not None, (where, name)
        assert (whole.cover is not None) == covered, where
        assert (whole.documents is not None) == covered, where
        assert (whole.peril_exclusion is not None) == (not covered), where
        assert whole.exclusions_complete is True, where
