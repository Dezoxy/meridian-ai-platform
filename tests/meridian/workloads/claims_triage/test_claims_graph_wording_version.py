"""A wording version the exclusion-clause table does not know (S067): the run
fails before the model is asked, a claim whose route never reads the table
routes as it did, and the log line names the wording only when its product and
version are the catalogue's.
"""

import logging
from collections.abc import Callable
from typing import Any, cast

import pytest
from graphsupport import (
    CANARY,
    CLAIMS,
    WORKERS_LOGGER,
    StubModel,
    StubTools,
    facts,
    logged,
    run_graph,
    triage,
    wording,
)

from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.workloads.claims_triage import workers
from meridian.workloads.claims_triage.models import ClaimFacts
from meridian.workloads.claims_triage.rules import PolicyRecord

# -- a wording version the table does not know (S067) -------------------------

UNKNOWN_VERSION = "2031-07"
NO_COUNT = "the table of exclusion clauses has no count for the wording"


def tools_of_version(
    claim_id: str,
    version: str,
    *,
    dropping: Callable[[dict[str, Any]], bool] = lambda chunk: False,
) -> StubTools:
    """Tools whose policy and search answers carry ``version``; the search
    leaves out the clauses ``dropping`` picks."""
    policy = StubTools._policy({"policy_number": CLAIMS[claim_id]["policy_number"]})
    policy["policy"]["wording_version"] = version

    def versioned(_: int, answer: dict[str, Any]) -> dict[str, Any]:
        kept = [c for c in answer["chunks"] if not dropping(c)]
        return {**answer, "wording_version": version, "chunks": kept}

    return StubTools(answers={"policy_lookup": policy}, tamper=versioned)


def test_an_unknown_wording_version_fails_the_run_before_the_model_is_asked() -> None:
    model = StubModel()

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", model, tools_of_version("CLM-0011", UNKNOWN_VERSION))

    assert failure_reason(raised.value) == "wording-version-unknown"
    assert model.calls == []


def test_an_unknown_wording_version_fails_a_claim_that_needs_no_model() -> None:
    # CLM-0005 is a glass claim on a motor policy: no circumstance exclusion is a
    # candidate, but the rules still read the count in their gaps.
    model = StubModel()

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0005", model, tools_of_version("CLM-0005", UNKNOWN_VERSION))

    assert raised.value.code == "wording-version-unknown"
    assert model.calls == []


def test_the_same_claim_with_a_version_the_table_knows_is_not_failed() -> None:
    output, _, _ = triage("CLM-0005")

    assert (output["route"], output["reason"]) == ("auto_approve", "within_threshold")


@pytest.mark.parametrize(
    "claim_id",
    [
        "CLM-0002",  # a lapsed policy
        "CLM-0014",  # a loss after the period
        "CLM-0020",  # a peril the motor product does not cover
        "CLM-0022",  # a peril the home product does not cover
    ],
    ids=["lapsed", "outside-period", "peril-not-covered-motor", "peril-not-covered"],
)
def test_a_claim_whose_route_never_reads_the_table_routes_as_it_did(
    claim_id: str,
) -> None:
    keys = ("route", "reason", "recommendation", "gaps")
    known, _, _ = triage(claim_id)

    unknown, _, _ = triage(claim_id, tools=tools_of_version(claim_id, UNKNOWN_VERSION))

    assert [unknown[k] for k in keys] == [known[k] for k in keys]


def test_a_claim_with_no_cover_clause_is_unverified_and_not_failed() -> None:
    tools = tools_of_version(
        "CLM-0011", UNKNOWN_VERSION, dropping=lambda c: c["clause"].startswith("2.")
    )

    output, _, _ = triage("CLM-0011", tools=tools)

    assert (output["reason"], output["gaps"]) == ("unverified", ["cover_clause"])


def test_the_log_line_names_the_wording_when_product_and_version_are_the_catalogues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GraphFailure):
        triage("CLM-0011", tools=tools_of_version("CLM-0011", UNKNOWN_VERSION))

    (record,) = [r for r in caplog.records if r.name == WORKERS_LOGGER]
    assert record.getMessage() == f"{NO_COUNT} MOTOR-TPL {UNKNOWN_VERSION}"
    assert record.levelno == logging.ERROR


def terms_of_wording(product: str, version: str) -> None:
    """``terms_of`` for a policy in force that has the cover clause of the home
    wording, whose product and version are the given ones."""
    claim = ClaimFacts.model_validate(facts("CLM-0016"))
    record = StubTools._policy({"policy_number": CLAIMS["CLM-0016"]["policy_number"]})
    policy = PolicyRecord.model_validate(
        {**record["policy"], "product": product, "wording_version": version}
    )
    state = {"chunks": wording("HOME-STD")[1]}
    workers.terms_of(claim, policy, cast(workers.ClaimState, state))


@pytest.mark.parametrize(
    ("product", "version"),
    [
        (f"{CANARY}-P", "2026-01"),
        ("HOME-STD", f"{CANARY}-v"),
        ("HOME-STD", "2026-1"),
        ("HOME-STD", "2026-01\n"),
        ("HOME-STD", "2026-001"),
        ("HOME-STD", ""),
        ("home-std", "2026-02"),
    ],
    ids=[
        "product-canary",
        "version-canary",
        "version-short",
        "version-newline",
        "version-long",
        "version-empty",
        "product-case",
    ],
)
def test_a_product_or_version_outside_the_catalogue_is_not_repeated_in_the_log(
    product: str, version: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GraphFailure) as raised:
        terms_of_wording(product, version)

    assert raised.value.code == "wording-version-unknown"
    assert logged(caplog) == [
        f"{NO_COUNT} of a product or version outside the catalogue"
    ]


def test_a_product_of_the_catalogue_with_a_version_of_its_form_is_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GraphFailure):
        terms_of_wording("HOME-STD", "2026-02")

    assert logged(caplog) == [f"{NO_COUNT} HOME-STD 2026-02"]


RECORDED = (1, 3, 7, 8, 9, 11, 15, 23, 26, 31, 34, 35, 37, 38)


def test_the_model_is_asked_for_the_claims_the_recording_holds() -> None:
    # Every golden policy's pair is in the table, so no claim fails and the 14
    # requests of the recording (the evaluation baseline's triage calls) are
    # still made, and no other.
    asked = []
    for claim_id in CLAIMS:
        model = StubModel()
        run_graph(model, StubTools(), facts(claim_id))
        if model.calls:
            asked.append(claim_id)

    assert asked == [f"CLM-{number:04d}" for number in RECORDED]
