"""S070: an answer that cannot be built after the request stored something is
one defined failure, on every route that builds it (G3).

The proposal is stored and counted before the answer is built. A model that
refuses its own parts (a pydantic ``ValidationError``, and nothing else is
caught) used to leave the claims route with a bare 500 from the outermost
middleware, and the documents route the same, with the names and the proposal
stored and nothing to tell the claimant's page that they were. Now it is a
``DecisionFailure`` of 500 with the fixed text, marked ``stored`` where names
were stored, one log line with the claim's ID and the error's class, no second
triage and no second count; the state stands, so a retry is a 409.
"""

import logging
import uuid
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from fastapi import HTTPException
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from servicesupport import claim_with_id, owner_rows
from workloads.claims_triage.test_claim_moves import (
    MOVE_ID,
    NOT_AWAITING_DOCUMENTS_DETAIL,
    MoveRuntime,
    add_documents_directly,
    arrived_names,
    documents_url,
    triages_of,
    waiting_for_documents,
)
from workloads.claims_triage.test_claims_app import CANARY, claim_state
from workloads.claims_triage.test_claims_meters import (
    UNAVAILABLE,
    Runtime,
    post_json_claim,
    proposals_stored,
    triage_count,
    triages,
)
from workloads.claims_triage.test_claims_meters import (
    make_client as metered_client,
)

from meridian.workloads.claims_triage import moves, triaging
from meridian.workloads.claims_triage.models import (
    ClaimMoveResponse,
    ClaimResponse,
    DecisionFailure,
)

CLAIM = "CLM-9101"
BUILT_NOTHING = "the answer for claim {} could not be built: ValidationError"
INTERNAL = "internal error"
HAS_PROPOSAL_DETAIL = "the claim already has a triage proposal"


def refuse_to_build(model: type, **inputs: object) -> Any:
    """A stand-in for a response model that refuses its parts: the real
    ``ValidationError`` of the real model, whose text quotes the input."""

    def build(**_: object) -> None:
        model.model_validate(inputs)

    return build


def errors_of(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def starts(runtime: MoveRuntime) -> int:
    return len(runtime.starts)


# ── the documents route ─────────────────────────────────────────────────────
def test_the_documents_route_answers_a_500_when_the_answer_cannot_be_built(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    waiting_for_documents(fresh_database, [])
    monkeypatch.setattr(
        triaging, "ClaimResponse", refuse_to_build(ClaimResponse, claim_id=CANARY)
    )
    runtime = MoveRuntime()
    reader = InMemoryMetricReader()
    client = metered_client(fresh_database, runtime, reader)  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG):
        response = client.post(documents_url(), json={"documents": ["photos"]})

    assert response.status_code == 500
    assert response.json() == {
        "detail": INTERNAL,
        "claim_id": MOVE_ID,
        "run_id": str(runtime.run_id),
    }
    # One line: the claim's ID and the error's class, no field and no value.
    (record,) = errors_of(caplog)
    assert record.getMessage() == BUILT_NOTHING.format(MOVE_ID)
    assert CANARY not in record.getMessage() + response.text
    # The names and the proposal are stored, and counted once.
    assert arrived_names(fresh_database, MOVE_ID) == ["photos"]
    assert proposals_stored(fresh_database) == 1
    assert triages(reader) == [triage_count("stored")]


def test_a_retry_of_the_same_documents_after_that_is_a_409_and_changes_nothing(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting_for_documents(fresh_database, [], triages=1)
    monkeypatch.setattr(
        triaging, "ClaimResponse", refuse_to_build(ClaimResponse, claim_id=CANARY)
    )
    runtime = MoveRuntime()
    client = metered_client(fresh_database, runtime, InMemoryMetricReader())  # type: ignore[arg-type]
    assert (
        client.post(documents_url(), json={"documents": ["photos"]}).status_code == 500
    )
    stored = (
        claim_state(fresh_database, MOVE_ID),
        triages_of(fresh_database, MOVE_ID),
        arrived_names(fresh_database, MOVE_ID),
        proposals_stored(fresh_database),
    )

    retry = client.post(documents_url(), json={"documents": ["photos"]})

    assert retry.status_code == 409
    assert retry.json() == {"detail": NOT_AWAITING_DOCUMENTS_DETAIL}
    assert stored[0][0] == "awaiting_adjuster"
    assert (
        claim_state(fresh_database, MOVE_ID),
        triages_of(fresh_database, MOVE_ID),
        arrived_names(fresh_database, MOVE_ID),
        proposals_stored(fresh_database),
    ) == stored
    # One triage started: the retry started none.
    assert starts(runtime) == 1


def test_the_claimants_page_learns_that_the_names_were_stored(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting_for_documents(fresh_database, [])
    monkeypatch.setattr(
        triaging, "ClaimResponse", refuse_to_build(ClaimResponse, claim_id=CANARY)
    )

    answer = add_documents_directly(fresh_database, MoveRuntime(), ["photos"])

    assert isinstance(answer, DecisionFailure)
    assert (answer.status, answer.detail, answer.stored) == (500, INTERNAL, True)
    assert answer.run_id is not None


def test_a_move_response_that_cannot_be_built_is_a_stored_500_too(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The cap referral builds its answer from two literals after storing no
    # proposal: a ``try`` there could not fire, and there is none.
    waiting_for_documents(fresh_database, [])
    monkeypatch.setattr(
        moves,
        "ClaimMoveResponse",
        refuse_to_build(ClaimMoveResponse, claim_id=CANARY),
    )

    with caplog.at_level(logging.DEBUG):
        answer = add_documents_directly(fresh_database, MoveRuntime(), ["photos"])

    assert isinstance(answer, DecisionFailure)
    assert (answer.status, answer.detail, answer.stored) == (500, INTERNAL, True)
    (record,) = errors_of(caplog)
    assert record.getMessage() == BUILT_NOTHING.format(MOVE_ID)
    assert arrived_names(fresh_database, MOVE_ID) == ["photos"]


def test_an_error_that_is_not_a_validation_error_is_still_not_caught(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pin, green before and after: the handling names ``ValidationError``; it
    # is not an ``except Exception``.
    waiting_for_documents(fresh_database, [])

    def broken(**_: object) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(triaging, "ClaimResponse", broken)

    with pytest.raises(RuntimeError):
        add_documents_directly(fresh_database, MoveRuntime(), ["photos"])


def test_a_refusal_after_the_names_were_stored_is_still_a_refused_after_storing(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pin, green before and after. The 409 is raised inside the triage, before
    # the answer is built, so the failing stand-in is never reached here: what
    # this holds is that the handling of the build swallows no ``HTTPException``
    # and the route still marks one raised after the store.
    waiting_for_documents(fresh_database, [])
    monkeypatch.setattr(
        triaging, "ClaimResponse", refuse_to_build(ClaimResponse, claim_id=CANARY)
    )
    runtime = MoveRuntime(
        during_start=lambda: owner_rows(
            fresh_database,
            "UPDATE claims.claims SET state = 'approved', run_id = %s, "
            "state_changed_at = clock_timestamp() WHERE claim_id = %s RETURNING 1",
            (uuid.uuid4(), MOVE_ID),
        )
    )

    with pytest.raises(HTTPException) as refused:
        add_documents_directly(fresh_database, runtime, ["photos"])

    assert isinstance(refused.value, moves.RefusedAfterStoring)
    assert refused.value.status_code == 409
    assert arrived_names(fresh_database, MOVE_ID) == ["photos"]


# ── the claims route ────────────────────────────────────────────────────────
# New behaviour, not a pin: the claims route gave the same bare 500 as the
# documents route (no claim, no run, a line from the outermost middleware);
# its body now carries what ``ClaimErrorBody`` declares for it. The pin is
# ``test_an_answer_that_cannot_be_built_after_the_proposal_is_stored_is_a_stored_
# triage`` in ``test_claims_meters.py``, whose stand-in is a ``RuntimeError``.
def test_the_claims_route_answers_the_same_500_with_the_claim_and_one_log_line(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        triaging, "ClaimResponse", refuse_to_build(ClaimResponse, claim_id=CANARY)
    )
    reader = InMemoryMetricReader()
    client = metered_client(fresh_database, Runtime(UNAVAILABLE), reader)

    with caplog.at_level(logging.DEBUG):
        response = post_json_claim(client, claim_with_id(CLAIM))

    assert response.status_code == 500
    body = response.json()
    assert (body["detail"], body["claim_id"]) == (INTERNAL, CLAIM)
    assert set(body) == {"detail", "claim_id", "run_id"}
    (record,) = errors_of(caplog)
    assert record.getMessage() == BUILT_NOTHING.format(CLAIM)
    assert CANARY not in record.getMessage() + response.text
    assert proposals_stored(fresh_database) == 1
    # Counted once, as stored: not a second time as unexpected.
    assert triages(reader) == [triage_count("stored")]


def test_a_retry_of_the_claim_after_that_answers_409_and_triages_nothing_again(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        triaging, "ClaimResponse", refuse_to_build(ClaimResponse, claim_id=CANARY)
    )
    reader = InMemoryMetricReader()
    runtime = Runtime(UNAVAILABLE)
    client = metered_client(fresh_database, runtime, reader)
    assert post_json_claim(client, claim_with_id(CLAIM)).status_code == 500

    retry = post_json_claim(client, claim_with_id(CLAIM))

    assert retry.status_code == 409
    assert retry.json() == {"detail": HAS_PROPOSAL_DETAIL}
    assert proposals_stored(fresh_database) == 1
    assert triages(reader) == [triage_count("stored")]
