"""The claimant's pages (S049) through the real services (``build_stack``).

The pages' own tests run on fakes. These tests run what only the real runtime,
graph and claims tool server can show: that a claim submitted from the form is
triaged as through ``POST /claims`` (the state and the run's status are the
database's), that the status page tells the claimant what happens next without
a word of the stored proposal, and that the page's two other forms end a run and
change the rules' answer as S048's JSON routes do.

The claims are the golden set's: CLM-0002 (the lapsed policy, referred to an
adjuster by the rules without asking the model) and CLM-0030 (a burglary that
came without its police report).
"""

from typing import get_args

import httpx
import pytest
from dbsupport import DatabaseHandle
from stacksupport import CLAIMS, Stack, build_stack
from test_lifecycle_stack import (
    DOCUMENTS_REQUESTED,
    ROUTE_WITH_THE_DOCUMENTS,
    notes,
    proposals_of,
    run_of,
)
from test_triage_stack import AFTER_TRIAGE, LAPSED_POLICY, run_status, states

from meridian.workloads.claims_triage.models import DECISION_NOTES
from meridian.workloads.claims_triage.rules import FraudIndicator

STATUS_PATH = "/claimant/claims/{}"
REVIEWING = "An adjuster is reviewing your claim."
APPROVED = "Your claim is approved."
ASKED_FOR = "Documents asked for:"
ARRIVED = "Documents that arrived"


@pytest.fixture
def stack(fresh_database: DatabaseHandle) -> Stack:
    return build_stack(fresh_database)


def assert_redirects_to_status(response: httpx.Response, claim_id: str) -> None:
    assert response.status_code == 303, response.text
    assert response.headers["location"] == STATUS_PATH.format(claim_id)


def status_page(stack: Stack, claim_id: str) -> str:
    page = stack.client.get(STATUS_PATH.format(claim_id))
    assert page.status_code == 200, page.text
    return page.text


# ── 1. a claim waiting for an adjuster ──────────────────────────────────────
def test_a_claim_submitted_from_the_page_waits_for_an_adjuster_and_is_withdrawn(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    claim = CLAIMS[LAPSED_POLICY]

    submitted = stack.submit_in_page(claim)

    assert_redirects_to_status(submitted, LAPSED_POLICY)
    # As through POST /claims: the rules refer it, and its run is paused.
    assert states(fresh_database)[LAPSED_POLICY] == "awaiting_adjuster"
    run_id = run_of(fresh_database, LAPSED_POLICY)
    assert (
        run_status(fresh_database, run_id),
        states(fresh_database)[LAPSED_POLICY],
    ) == AFTER_TRIAGE["adjuster"]
    ((_, proposal),) = proposals_of(fresh_database, LAPSED_POLICY)
    assert proposal.route == "adjuster"
    # The page says what happens next and nothing of the proposal or the claim.
    text = status_page(stack, LAPSED_POLICY)
    assert REVIEWING in text
    assert proposal.reason not in text
    # This proposal holds no amount and no indicator; every indicator the rules
    # know is checked all the same, and an amount would be if it held one.
    if proposal.payable_amount is not None:
        assert str(proposal.payable_amount) not in text
    for indicator in (*proposal.fraud_indicators, *get_args(FraudIndicator)):
        assert indicator not in text
    assert claim["claimant"]["name"] not in text
    assert claim["claimant"]["email"] not in text
    assert claim["description"] not in text

    withdrawn = stack.withdraw_in_page(LAPSED_POLICY)

    assert_redirects_to_status(withdrawn, LAPSED_POLICY)
    assert states(fresh_database)[LAPSED_POLICY] == "withdrawn"
    # The run ended as the JSON withdrawal ends it: the word read, the fixed
    # note written once, status Completed.
    assert run_status(fresh_database, run_id) == "Completed"
    assert notes(fresh_database) == [(run_id, DECISION_NOTES["withdrawn"])]
    assert "You withdrew this claim." in status_page(stack, LAPSED_POLICY)


# ── 2. a claim that asks for documents ──────────────────────────────────────
def test_the_documents_reported_from_the_page_change_the_rules_answer(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    submitted = stack.submit_in_page(CLAIMS[DOCUMENTS_REQUESTED])

    assert_redirects_to_status(submitted, DOCUMENTS_REQUESTED)
    assert states(fresh_database)[DOCUMENTS_REQUESTED] == "documents_requested"
    ((first_run, first),) = proposals_of(fresh_database, DOCUMENTS_REQUESTED)
    assert first.route == "request_documents"
    missing = list(first.missing_documents)
    assert missing
    before = status_page(stack, DOCUMENTS_REQUESTED)
    assert ASKED_FOR in before
    for name in missing:
        assert f"<li>{name}</li>" in before.split(ASKED_FOR)[1]
    assert ARRIVED not in before

    reported = stack.documents_in_page(DOCUMENTS_REQUESTED, missing)

    assert_redirects_to_status(reported, DOCUMENTS_REQUESTED)
    # Approved by the rules, as through POST /claims/{id}/documents: a second
    # proposal of a new run, and nothing missing.
    assert states(fresh_database)[DOCUMENTS_REQUESTED] == "approved"
    stored = proposals_of(fresh_database, DOCUMENTS_REQUESTED)
    ((_, _), (second_run, second)) = stored
    assert second.route == ROUTE_WITH_THE_DOCUMENTS
    assert second_run != first_run
    assert run_of(fresh_database, DOCUMENTS_REQUESTED) == second_run
    after = status_page(stack, DOCUMENTS_REQUESTED)
    assert APPROVED in after
    assert ASKED_FOR not in after
    for name in missing:
        assert f"<li>{name}</li>" in after.split(ARRIVED)[1]
