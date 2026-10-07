"""What each Claims API caller sends the runtime, through the real services
(``build_stack``): the triage's whole run input and the brief's (S067, M1s).

``start_run`` sends the input as it is given. The triage passes the claim and the
posted-text flag; the brief passes the claim only, as its workflow reads it. A
triage whose input was wrapped twice fails on its first node, and the brief next
to it is never started, so each test here reads a result only a run that read
its input can have.
"""

import json
from typing import Any

import httpx
import pytest
from dbsupport import DatabaseHandle
from servicesupport import REPO_ROOT, owner_rows
from stacksupport import Stack, build_stack, stored_proposals
from test_claim_brief_stack import Models, start_brief, triage_paused
from test_claim_brief_stack import models as models  # a fixture, found here
from test_claim_brief_stack import stack as stack  # a fixture, found here

NAME_MASKED = ("CLM-1053", "CLM-1054")


def name_masked_claim(case_id: str) -> dict[str, Any]:
    path = REPO_ROOT / "data" / "synthetic" / "injection" / "cases.json"
    (case,) = [
        c for c in json.loads(path.read_text(encoding="utf-8")) if c["case"] == case_id
    ]
    claim: dict[str, Any] = case["claim"]
    return claim


class Refusing:
    """A model that fails the test when it is asked, and counts the asks."""

    def __init__(self) -> None:
        self.asked = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked += 1
        return httpx.Response(500, json={})

    def http(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(self)
        )


@pytest.mark.parametrize("case_id", NAME_MASKED)
def test_a_name_masked_claim_is_stopped_by_the_flag_the_run_reads_from_its_input(
    fresh_database: DatabaseHandle, case_id: str
) -> None:
    model = Refusing()
    built = build_stack(fresh_database, runtime_http=model.http())

    posted = built.post(name_masked_claim(case_id))

    assert posted.status_code == 201, posted.text
    proposal = stored_proposals(fresh_database)[case_id]
    assert proposal.assessment == "unavailable"
    assert proposal.unavailable_because == "injection-suspected"
    assert model.asked == 0


def test_a_brief_is_started_with_the_claim_its_workflow_reads_and_drafts_it(
    stack: Stack, models: Models, fresh_database: DatabaseHandle
) -> None:
    triage_paused(stack)

    started = start_brief(stack)

    assert started["state"] == "awaiting_decision"
    assert models.briefs == 1
    assert started["brief"]
    ((agent,),) = owner_rows(
        fresh_database,
        "SELECT agent FROM runtime.runs WHERE run_id = %s",
        (started["run_id"],),
    )
    assert agent == "claim-brief"
