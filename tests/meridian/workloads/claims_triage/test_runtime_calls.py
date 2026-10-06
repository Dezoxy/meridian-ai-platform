"""The Claims API's calls to the Agent Runtime (S037, R1): a run is started for
an agent, the claims-triage agent when none is named."""

import json
import uuid

import httpx

from meridian.workloads.claims_triage import runtime_calls, triaging
from meridian.workloads.claims_triage.lifecycle import AGENT, BRIEF_AGENT

FACTS = {"claim_id": "CLM-0001", "peril": "water"}
# A run's whole input, as a caller builds it: ``start_run`` sends it as it is
# given, so a field beside the claim reaches the runtime (S067).
RUN_INPUT = {"claim": FACTS, "posted_text_addresses_the_model": True}


def runtime_that_records(bodies: list[dict]) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"run_id": str(uuid.uuid4()), "status": "Completed", "output": None},
        )

    return httpx.Client(
        base_url="http://runtime.invalid", transport=httpx.MockTransport(answer)
    )


def test_a_run_started_with_no_agent_named_is_a_run_of_the_triage_agent() -> None:
    bodies: list[dict] = []

    run = triaging.start_run(
        runtime_that_records(bodies), "claims-triage", "CLM-0001", RUN_INPUT
    )

    assert run.status == "Completed"
    assert bodies == [
        {
            "agent": AGENT,
            "tenant": "claims-triage",
            "reference": "CLM-0001",
            "input": RUN_INPUT,
        }
    ]


def test_a_run_started_for_the_brief_agent_names_it_and_sends_the_same_input() -> None:
    bodies: list[dict] = []

    triaging.start_run(
        runtime_that_records(bodies),
        "claims-triage",
        "CLM-0001",
        RUN_INPUT,
        agent=BRIEF_AGENT,
    )

    assert bodies == [
        {
            "agent": BRIEF_AGENT,
            "tenant": "claims-triage",
            "reference": "CLM-0001",
            "input": RUN_INPUT,
        }
    ]


def test_a_run_input_that_names_no_claim_is_not_given_one() -> None:
    bodies: list[dict] = []

    triaging.start_run(
        runtime_that_records(bodies), "claims-triage", "CLM-0001", {"other": 1}
    )

    assert [body["input"] for body in bodies] == [{"other": 1}]


def test_the_calls_triaging_offered_before_are_the_new_modules_own() -> None:
    for name in (
        "END_RUN_TIMEOUT_SECONDS",
        "HTTP_GATEWAY_TIMEOUT",
        "RuntimeCallError",
        "start_run",
        "resume_run",
        "end_run",
        "runtime_timeout",
    ):
        assert getattr(triaging, name) is getattr(runtime_calls, name)
