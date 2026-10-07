"""What a triage run is sent, built by one helper (S067, M1s).

``triaging.triage_run_input`` is the only place that builds a triage's whole run
input: the claim's facts and, beside them, the posted-text flag. ``start_run``
sends it as it is given, so the claim is under ``claim`` once, never twice.
No database: the call sites (``POST /claims``, the three moves) are read in
``test_claims_app.py`` and ``test_claim_moves.py``.
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from servicesupport import injection_case_claim, synthetic_claims

from meridian.workloads.claims_triage import claimant_name, triaging
from meridian.workloads.claims_triage.lifecycle import AGENT
from meridian.workloads.claims_triage.models import (
    MAX_DOCUMENTS,
    ClaimFacts,
    ClaimSubmission,
)
from meridian.workloads.claims_triage.posted_text import (
    POSTED_TEXT_FLAG,
    input_for_run,
)

GOLDEN_RUN_INPUT = Path(__file__).parent / "golden_run_input.json"


def golden_submission() -> ClaimSubmission:
    return ClaimSubmission.model_validate(synthetic_claims()[0])


def name_masked_submission(case_id: str) -> ClaimSubmission:
    return ClaimSubmission.model_validate(injection_case_claim(case_id))


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


# ── the two parts (S070): what the submission alone gives, then the documents ─
def submission_named(name: str, description: str) -> ClaimSubmission:
    return ClaimSubmission.model_validate(
        {
            **synthetic_claims()[0],
            "claimant": {"name": name, "email": "who.ever@example.net"},
            "description": description,
        }
    )


SUBMISSIONS = {
    "golden": golden_submission,
    "name-replaced": lambda: submission_named(
        "Bence Novak", "Bence Novak (who.ever@example.net) reports a storm."
    ),
    "name-in-a-form": lambda: submission_named(
        "Kovács János", "Kovács Jánosnak a tetőt vitte a vihar."
    ),
    "no-part-long-enough": lambda: submission_named("Mr Wu", "Mr Wu reports a storm."),
    "posted-text-addresses-the-model": lambda: name_masked_submission("CLM-1053"),
    "posted-text-of-the-second-case": lambda: name_masked_submission("CLM-1054"),
}
ARRIVALS = {
    "none": (),
    "two-new": ("invoice.pdf", "estimate.pdf"),
    "one-the-submission-has": ("photos", "invoice.pdf", "invoice.pdf"),
    # the merged names pass the bound: the facts do not validate, and the run is
    # sent them as they are, as it always was
    "past-the-bound": tuple(f"extra-{n}.pdf" for n in range(MAX_DOCUMENTS)),
}


@pytest.mark.parametrize("arrived", ARRIVALS.values(), ids=ARRIVALS.keys())
@pytest.mark.parametrize("submission", SUBMISSIONS.values(), ids=SUBMISSIONS.keys())
def test_the_two_parts_give_the_run_input_the_one_step_gave_byte_for_byte(
    submission: Any, arrived: tuple[str, ...]
) -> None:
    claim = submission()
    # ``input_for_run`` of ``facts_for_run`` is the one step as it was.
    one_step = input_for_run(claim, triaging.facts_for_run(claim, arrived))

    prepared = triaging.prepare_run_input(claim)
    two_steps = triaging.run_input_with_documents(prepared, arrived)

    assert json.dumps(two_steps) == json.dumps(one_step)
    assert json.dumps(triaging.triage_run_input(claim, arrived)) == json.dumps(one_step)


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == triaging.logger.name and r.levelno == logging.WARNING
    ]


def test_arrivals_past_the_bound_log_one_warning_in_each_path_and_the_same_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    claim = golden_submission()
    arrived = ARRIVALS["past-the-bound"]
    caplog.set_level(logging.DEBUG)

    prepared = triaging.prepare_run_input(claim)
    after_the_first_part = warnings_of(caplog)
    two_steps = triaging.run_input_with_documents(prepared, arrived)
    in_two_parts = warnings_of(caplog)
    caplog.clear()
    one_step = triaging.triage_run_input(claim, arrived)
    in_one_step = warnings_of(caplog)

    assert after_the_first_part == []
    assert len(in_two_parts) == len(in_one_step) == 1
    assert in_two_parts == in_one_step
    assert "documents" in in_one_step[0]
    assert "too_long" in in_one_step[0]
    assert len(one_step["claim"]["documents"]) > MAX_DOCUMENTS
    assert json.dumps(two_steps) == json.dumps(one_step)


def test_both_paths_give_the_bytes_the_commit_before_the_two_parts_gave() -> None:
    # ``golden_run_input.json`` is the run input of CLM-1053 with two arrivals
    # as ``triage_run_input`` of commit 09c0f52 built it (before S070 split it in
    # two), made from that commit's own code, not from today's. Same call, same
    # separators: the file is compared as text.
    golden = GOLDEN_RUN_INPUT.read_text(encoding="utf-8")
    claim = name_masked_submission("CLM-1053")
    arrived = ("invoice.pdf", "photos")

    one_step = triaging.triage_run_input(claim, arrived)
    prepared = triaging.prepare_run_input(claim)
    two_steps = triaging.run_input_with_documents(prepared, arrived)

    assert json.dumps(one_step) == golden
    assert json.dumps(two_steps) == golden


def test_the_part_that_needs_the_documents_compiles_no_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled: list[str] = []
    real = claimant_name._compile_uncached

    def spy(pattern: str, flags: Any) -> Any:
        compiled.append(pattern)
        return real(pattern, flags)

    monkeypatch.setattr(claimant_name, "_compile_uncached", spy)
    claim = submission_named("Bence Novak", "Bence Novak reports a storm.")

    prepared = triaging.prepare_run_input(claim)
    after_the_first_part = len(compiled)
    triaging.run_input_with_documents(prepared, ("invoice.pdf",))

    assert after_the_first_part == 1
    assert len(compiled) == after_the_first_part


def test_the_first_part_can_be_finished_for_different_documents_without_changing() -> (
    None
):
    claim = golden_submission()
    prepared = triaging.prepare_run_input(claim)
    before = json.dumps(prepared.facts)

    first = triaging.run_input_with_documents(prepared, ("first.pdf",))
    second = triaging.run_input_with_documents(prepared, ())

    assert json.dumps(prepared.facts) == before
    assert first["claim"]["documents"] == ["photos", "first.pdf"]
    assert second["claim"]["documents"] == ["photos"]


def test_the_first_part_holds_the_posted_text_flag_the_run_is_sent() -> None:
    clean = triaging.prepare_run_input(golden_submission())
    masked = triaging.prepare_run_input(name_masked_submission("CLM-1053"))

    assert clean.posted_text_flag is False
    assert masked.posted_text_flag is True
    assert triaging.run_input_with_documents(masked, ())[POSTED_TEXT_FLAG] is True
