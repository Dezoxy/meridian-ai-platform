"""Assemble the dataset: build every scenario, then check it against the oracle.

The plan is shuffled with the seed, and policy numbers are a random permutation,
so neither a claim number nor a policy number tells the outcome. Every built
scenario is judged by the oracle; if the derived outcome differs from the one
the builder intended, the generator stops.
"""

import random
from typing import NamedTuple

from . import catalogue, people
from .builders import BUILDERS, build_background
from .oracle import derive_outcome
from .plan import PLAN, POLICY_COUNT
from .records import Context, Record, Scenario


class ScenarioError(RuntimeError):
    """A built scenario does not produce the outcome it was built for."""


class Dataset(NamedTuple):
    policies: list[Record]
    history: list[Record]
    claims: list[Record]
    outcomes: list[Record]


def build_dataset(seed: int) -> Dataset:
    """The whole dataset for ``seed``; identical on every call."""
    catalogue.validate_catalogue()
    rng = random.Random(seed)
    ctx = Context(rng, people.draw_registrations(rng, POLICY_COUNT))
    plan = rng.sample(PLAN, len(PLAN))
    numbers = rng.sample(range(1, POLICY_COUNT + 1), POLICY_COUNT)
    scenarios = [
        BUILDERS[spec.reason](ctx, spec, numbers[index])
        for index, spec in enumerate(plan)
    ]
    background = [
        build_background(ctx, number, index)
        for index, number in enumerate(numbers[len(plan) :])
    ]
    return _assemble(scenarios, background)


def _assemble(
    scenarios: list[Scenario], background: list[tuple[Record, list[Record]]]
) -> Dataset:
    policies = [s.policy for s in scenarios] + [policy for policy, _ in background]
    entries = [e for s in scenarios for e in s.history]
    entries += [e for _, history in background for e in history]
    entries.sort(key=lambda e: (e["policy_number"], e["loss_date"], e["peril"]))
    claims, outcomes = [], []
    for index, scenario in enumerate(scenarios, start=1):
        claim = {"claim_id": f"CLM-{index:04d}", **scenario.claim}
        outcomes.append(_judged(scenario, claim, entries))
        claims.append(claim)
    history = [
        {"history_id": f"HIST-{index:04d}", **entry}
        for index, entry in enumerate(entries, start=1)
    ]
    return Dataset(
        policies=sorted(policies, key=lambda p: p["policy_number"]),
        history=history,
        claims=claims,
        outcomes=outcomes,
    )


def _judged(scenario: Scenario, claim: Record, entries: list[Record]) -> Record:
    """The oracle's outcome for ``claim``; raises when it is not the intended one."""
    outcome = derive_outcome(claim, scenario.policy, entries, scenario.circumstance)
    derived = (
        outcome["reason"],
        tuple(outcome["fraud_indicators"]),
        outcome["exclusion"],
        tuple(outcome["missing_documents"]),
    )
    intended = (
        scenario.reason,
        scenario.indicators,
        scenario.exclusion,
        scenario.missing,
    )
    if derived != intended:
        raise ScenarioError(
            f"{claim['claim_id']}: built for {intended}, the oracle derives {derived}"
        )
    return outcome
