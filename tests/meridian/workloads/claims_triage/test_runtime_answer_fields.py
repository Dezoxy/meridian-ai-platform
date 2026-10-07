"""The Claims API logs which field failed when an answer of the runtime does not
validate (S069, E5, R1).

Three places read what the runtime answered: the run's response
(``runtime_calls._call_runtime``), the triage's proposal
(``triaging.triage_outcome``) and the brief (``briefs._output_of``). Each logs
one line with the failed fields as ``(dotted location, error type)`` pairs and
nothing else: the proposal carries a model's rationale and the brief a model's
text, and neither may reach a log line.
"""

import logging
import uuid
from typing import Any

import httpx
import pytest

from meridian.runtime.models import RunResponse
from meridian.workloads.claims_triage import briefs, models, runtime_calls, triaging
from meridian.workloads.claims_triage.runtime_calls import RuntimeCallError

CANARY = "a-made-up-sentence-about-a-flooded-cellar"
UNAVAILABLE = {
    "route": "adjuster",
    "reason": "unverified",
    "recommendation": None,
    "payable_amount": None,
    "exclusion_clause": None,
    "fraud_indicators": [],
    "missing_documents": [],
    "citations": [],
    "gaps": ["exclusion_assessment"],
    "assessment": "unavailable",
    "unavailable_because": "not-json",
    "rationale": None,
    "drafted_by": None,
}


def runtime_answering(response: httpx.Response) -> httpx.Client:
    return httpx.Client(
        base_url="http://runtime.invalid",
        transport=httpx.MockTransport(lambda request: response),
    )


def start_against(response: httpx.Response) -> RuntimeCallError:
    with pytest.raises(RuntimeCallError) as raised:
        runtime_calls.start_run(
            runtime_answering(response), "claims-triage", "CLM-0001", {}
        )
    return raised.value


def warnings_of(caplog: pytest.LogCaptureFixture, module: Any) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == module.__name__ and record.levelno == logging.WARNING
    ]


def run_with(output: dict[str, Any] | None, status: str = "Completed") -> RunResponse:
    return RunResponse(run_id=uuid.uuid4(), status=status, output=output)  # type: ignore[arg-type]


# ── the run's response ──────────────────────────────────────────────────────
def test_a_body_that_is_not_json_is_logged_as_that_and_the_body_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = httpx.Response(200, text=f"<html>{CANARY}")

    with caplog.at_level(logging.WARNING):
        raised = start_against(body)

    assert (raised.failure, raised.status_code) == ("bad-output", 0)
    assert warnings_of(caplog, runtime_calls) == [
        "the runtime's answer is not JSON (JSONDecodeError)"
    ]
    assert CANARY not in caplog.text


def test_a_body_that_does_not_fit_a_run_logs_the_fields_and_not_their_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = httpx.Response(
        200,
        json={"run_id": CANARY, "status": "Completed", "output": None, CANARY: 1},
    )

    with caplog.at_level(logging.WARNING):
        raised = start_against(body)

    assert raised.failure == "bad-output"
    assert warnings_of(caplog, runtime_calls) == [
        "the runtime's answer is not a run: ValidationError "
        "(('run_id', 'uuid_parsing'), ('*', 'extra_forbidden'))"
    ]
    assert CANARY not in caplog.text


def test_a_body_that_is_not_an_object_logs_an_empty_location(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        raised = start_against(httpx.Response(200, json=[CANARY]))

    assert raised.failure == "bad-output"
    assert warnings_of(caplog, runtime_calls) == [
        "the runtime's answer is not a run: ValidationError (('', 'model_type'),)"
    ]
    assert CANARY not in caplog.text


def test_an_answer_that_fits_a_run_logs_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = httpx.Response(
        200, json={"run_id": str(uuid.uuid4()), "status": "Completed", "output": None}
    )

    with caplog.at_level(logging.WARNING):
        run = runtime_calls.start_run(
            runtime_answering(body), "claims-triage", "CLM-0001", {}
        )

    assert run.status == "Completed"
    assert warnings_of(caplog, runtime_calls) == []


# ── the triage's proposal ───────────────────────────────────────────────────
def test_a_proposal_that_does_not_validate_logs_its_fields_and_none_of_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    output = UNAVAILABLE | {
        "route": CANARY,
        "exclusion_clause": CANARY,
        "rationale": CANARY * 30,
        "citations": [
            {"product": "HOME-STD", "wording_version": "2026-01", "clause": CANARY}
        ],
        CANARY: CANARY,
    }
    run = run_with(output)

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeCallError) as raised:
        triaging.triage_outcome(run)

    assert raised.value.failure == "bad-output"
    assert raised.value.run_id == run.run_id
    (line,) = warnings_of(caplog, triaging)
    assert line == (
        f"the runtime's output of run {run.run_id} is not a triage proposal: "
        "ValidationError (('route', 'literal_error'), "
        "('exclusion_clause', 'string_pattern_mismatch'), "
        "('citations.0.clause', 'string_pattern_mismatch'), "
        "('rationale', 'string_too_long'), ('*', 'extra_forbidden'))"
    )
    assert CANARY not in caplog.text
    assert all(
        CANARY not in str(record.args) and record.exc_info is None
        for record in caplog.records
    )


def test_a_proposal_that_contradicts_itself_logs_an_empty_location(
    caplog: pytest.LogCaptureFixture,
) -> None:
    output = UNAVAILABLE | {"route": "auto_approve"}

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeCallError):
        triaging.triage_outcome(run_with(output))

    (line,) = warnings_of(caplog, triaging)
    assert line.endswith("ValidationError (('', 'value_error'),)")


def test_a_valid_proposal_that_does_not_fit_the_status_logs_no_field_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeCallError):
        triaging.triage_outcome(run_with(UNAVAILABLE, status="Completed"))

    assert warnings_of(caplog, triaging) == []


# ── the brief ───────────────────────────────────────────────────────────────
def test_a_brief_that_does_not_validate_logs_its_fields_and_none_of_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    output = {"brief": CANARY * 1000, "filed": CANARY, CANARY: CANARY}
    run = run_with(output)

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeCallError):
        briefs._output_of(run, resumed=True)

    (line,) = warnings_of(caplog, briefs)
    assert line == (
        f"the runtime's output of run {run.run_id} is not a brief: "
        "ValidationError (('brief', 'string_too_long'), ('filed', 'bool_type'), "
        "('*', 'extra_forbidden'))"
    )
    assert CANARY not in caplog.text


def test_a_brief_of_the_wrong_status_logs_no_field_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeCallError):
        briefs._output_of(run_with({"brief": "text"}, "Failed"), resumed=True)

    assert warnings_of(caplog, briefs) == []


# ── where the helper lives ──────────────────────────────────────────────────
def test_the_helper_lives_beside_the_models_and_triaging_still_offers_it() -> None:
    assert triaging.invalid_fields is models.invalid_fields
    assert triaging.DATA_KEY == models.DATA_KEY == "*"
