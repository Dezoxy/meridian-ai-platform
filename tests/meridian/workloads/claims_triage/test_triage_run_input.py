"""What a triage run is sent, built by one helper (S067, M1s).

``triaging.triage_run_input`` is the only place that builds a triage's whole run
input: the claim's facts and, beside them, the posted-text flag. ``start_run``
sends it as it is given, so the claim is under ``claim`` once, never twice.
No database: the call sites (``POST /claims``, the three moves) are read in
``test_claims_app.py`` and ``test_claim_moves.py``.
"""

import json
from typing import Any

import httpx
import pytest
from servicesupport import REPO_ROOT, synthetic_claims

from meridian.workloads.claims_triage import triaging
from meridian.workloads.claims_triage.lifecycle import AGENT
from meridian.workloads.claims_triage.models import ClaimFacts, ClaimSubmission
from meridian.workloads.claims_triage.posted_text import POSTED_TEXT_FLAG


def golden_submission() -> ClaimSubmission:
    return ClaimSubmission.model_validate(synthetic_claims()[0])


def name_masked_submission(case_id: str) -> ClaimSubmission:
    path = REPO_ROOT / "data" / "synthetic" / "injection" / "cases.json"
    (case,) = [
        c for c in json.loads(path.read_text(encoding="utf-8")) if c["case"] == case_id
    ]
    return ClaimSubmission.model_validate(case["claim"])


def sent_body(run_input: dict[str, Any]) -> dict[str, Any]:
    """The body ``start_run`` posts for ``run_input``."""
    bodies: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "run_id": "2f1e1f0e-6f1b-4a39-9a55-6b1d7d1d4f10",
                "status": "Completed",
                "output": None,
            },
        )

    http = httpx.Client(
        base_url="http://runtime.invalid", transport=httpx.MockTransport(answer)
    )
    triaging.start_run(http, "claims-triage", "CLM-0001", run_input)
    (body,) = bodies
    return body


def test_the_run_input_is_the_claim_once_and_the_flag() -> None:
    submission = golden_submission()

    run_input = triaging.triage_run_input(submission, ())

    assert set(run_input) == {"claim", POSTED_TEXT_FLAG}
    assert run_input["claim"] == triaging.facts_for_run(submission)
    assert "claim" not in run_input["claim"]
    ClaimFacts.model_validate(run_input["claim"])


def test_the_documents_that_arrived_are_in_the_claim_of_the_run_input() -> None:
    submission = golden_submission()

    run_input = triaging.triage_run_input(submission, ("a-new-document.pdf",))

    assert run_input["claim"]["documents"][-1] == "a-new-document.pdf"
    assert run_input["claim"] == triaging.facts_for_run(
        submission, ("a-new-document.pdf",)
    )


@pytest.mark.parametrize("case_id", ["CLM-1053", "CLM-1054"])
def test_a_name_masked_claim_reaches_the_runtime_with_the_flag_set(
    case_id: str,
) -> None:
    run_input = triaging.triage_run_input(name_masked_submission(case_id), ())

    body = sent_body(run_input)

    assert body["input"] == run_input
    assert body["input"][POSTED_TEXT_FLAG] is True
    assert set(body["input"]["claim"]) == set(run_input["claim"])
    assert "claim" not in body["input"]["claim"]


def test_a_clean_claim_reaches_the_runtime_with_the_flag_false() -> None:
    run_input = triaging.triage_run_input(golden_submission(), ())

    body = sent_body(run_input)

    assert body == {
        "agent": AGENT,
        "tenant": "claims-triage",
        "reference": "CLM-0001",
        "input": {
            "claim": triaging.facts_for_run(golden_submission()),
            POSTED_TEXT_FLAG: False,
        },
    }
