"""S048's edges a person drives, through the real services (``build_stack``).

The Claims API's unit tests use a fake runtime for each route. These tests run
what only the real runtime, graph and claims tool server can show: that the
paused run a send-back or a withdrawal ends really ends (status ``Completed``,
its fixed note, no checkpoint row left), that the run a send-back starts is a
real one, that the documents a claimant reports change the rules' answer, that
the cap on triages holds, and that an adjuster can decide a claim whose triage
failed.

The claims are the golden set's, in the replay gateway's mode. The claim on
the lapsed policy (``LAPSED_POLICY``) is referred by the rules without asking
the model, so its triage calls no gateway and repeats the same way every time.
"""

import uuid
from datetime import date
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from servicesupport import audit_events, owner_rows
from stacksupport import (
    CLAIMS,
    Stack,
    build_stack,
    claims_that_ask_the_model,
)
from test_triage_stack import (
    AFTER_TRIAGE,
    CHECKPOINT_TABLES,
    LAPSED_POLICY,
    run_status,
    states,
    table_count,
)

from meridian.workloads.claims_triage.models import DECISION_NOTES
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.triaging import TRIAGE_CAP_DETAIL

# The golden claims whose only reason to ask for documents is that some are
# missing (all six of the golden set's ``request_documents`` claims are), and
# that the rules never ask the model about, so that the route the rules give
# once the documents are there is the rules' alone and does not wait on a
# model. CLM-0030 is a burglary on a HOME-STD policy: it came without its
# police report. With the report present the rules approve it automatically
# (``within_threshold``), which ends its run.
DOCUMENTS_REQUESTED = "CLM-0030"
ROUTE_WITH_THE_DOCUMENTS = "auto_approve"
CLAIM_EVENTS_SQL = (
    "SELECT recorded_at, event, reason FROM audit.events "
    "WHERE service = 'claims-api' AND reference = %s ORDER BY recorded_at, event"
)


@pytest.fixture
def stack(fresh_database: DatabaseHandle) -> Stack:
    return build_stack(fresh_database)


def run_of(db: DatabaseHandle, claim_id: str) -> uuid.UUID:
    """The run the claim points to."""
    ((run_id,),) = owner_rows(
        db, "SELECT run_id FROM claims.claims WHERE claim_id = %s", (claim_id,)
    )
    assert isinstance(run_id, uuid.UUID)
    return run_id


def runs_of(db: DatabaseHandle, claim_id: str) -> int:
    """How many runs the runtime has started for the claim."""
    ((count,),) = owner_rows(
        db, "SELECT count(*) FROM runtime.runs WHERE reference = %s", (claim_id,)
    )
    return int(count)


def thread_of(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    ((thread,),) = owner_rows(
        db, "SELECT thread_id::text FROM runtime.runs WHERE run_id = %s", (run_id,)
    )
    return str(thread)


def checkpoint_threads(db: DatabaseHandle) -> set[str]:
    """The threads that hold a row in any checkpoint table."""
    return {
        str(thread)
        for table in CHECKPOINT_TABLES
        for (thread,) in owner_rows(db, f"SELECT thread_id FROM {table}")  # noqa: S608
    }


def claim_trail(db: DatabaseHandle, claim_id: str) -> list[tuple[str, str]]:
    """The Claims API's audit events for the claim, oldest first, as
    ``(event, reason)``. Events of one transaction share a moment (``now()``),
    so their order among themselves is not the order they were written in."""
    return [
        (event, reason)
        for _, event, reason in owner_rows(db, CLAIM_EVENTS_SQL, (claim_id,))
    ]


def run_events(db: DatabaseHandle, run_id: uuid.UUID) -> list[str]:
    """The Agent Runtime's own audit events for the run, in order."""
    return [
        e["event"] for e in audit_events(db, run_id) if e["service"] == "agent-runtime"
    ]


def notes(db: DatabaseHandle) -> list[tuple[uuid.UUID, str]]:
    return owner_rows(db, "SELECT run_id, note FROM claims.notes ORDER BY created_at")


def decisions(db: DatabaseHandle) -> list[tuple[str, uuid.UUID | None, str]]:
    return owner_rows(
        db,
        "SELECT claim_id, run_id, decision FROM claims.decisions ORDER BY decided_at",
    )


def proposals_of(
    db: DatabaseHandle, claim_id: str
) -> list[tuple[uuid.UUID, TriageProposal]]:
    """Every proposal stored for the claim, oldest first, with its run."""
    rows = owner_rows(
        db,
        "SELECT run_id, proposal FROM claims.triage_proposals "
        "WHERE claim_id = %s ORDER BY created_at",
        (claim_id,),
    )
    return [(run_id, TriageProposal.model_validate(doc)) for run_id, doc in rows]


def no_checkpoint_left(db: DatabaseHandle) -> None:
    for table in CHECKPOINT_TABLES:
        assert table_count(db, table) == 0, table


def post_paused(stack: Stack, claim_id: str) -> uuid.UUID:
    """Post a claim the rules refer to an adjuster; its run is paused."""
    posted = stack.post(CLAIMS[claim_id])
    assert posted.status_code == 201, posted.text
    assert (posted.json()["run_status"], posted.json()["state"]) == AFTER_TRIAGE[
        "adjuster"
    ]
    return uuid.UUID(posted.json()["run_id"])


# ── 1. send back ────────────────────────────────────────────────────────────
def test_a_claim_sent_back_ends_its_old_run_and_is_triaged_by_a_new_one(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    old_run = post_paused(stack, LAPSED_POLICY)
    old_thread = thread_of(fresh_database, old_run)
    assert old_thread in checkpoint_threads(fresh_database)

    answer = stack.triage_again(LAPSED_POLICY)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    new_run = uuid.UUID(body["run_id"])
    assert new_run != old_run
    assert (body["claim_id"], body["state"], body["run_status"]) == (
        LAPSED_POLICY,
        "awaiting_adjuster",
        "AwaitingApproval",
    )
    assert body["proposal"]["route"] == "adjuster"
    # The old run ended: it read the word recorded for it, wrote its fixed note
    # once and completed.
    assert run_status(fresh_database, old_run) == "Completed"
    assert notes(fresh_database) == [(old_run, DECISION_NOTES["send_back"])]
    assert decisions(fresh_database) == [(LAPSED_POLICY, old_run, "send_back")]
    assert run_events(fresh_database, old_run) == [
        "run.started",
        "run.awaiting_approval",
        "run.resumed",
        "run.completed",
    ]
    # The claim's run is the new one, which paused as the first did: the same
    # facts, the same rules.
    assert run_of(fresh_database, LAPSED_POLICY) == new_run
    assert run_status(fresh_database, new_run) == "AwaitingApproval"
    assert run_events(fresh_database, new_run) == [
        "run.started",
        "run.awaiting_approval",
    ]
    assert [run for run, _ in proposals_of(fresh_database, LAPSED_POLICY)] == [
        old_run,
        new_run,
    ]
    # The checkpoint rows that remain are the new run's thread's alone.
    assert checkpoint_threads(fresh_database) == {thread_of(fresh_database, new_run)}
    assert claim_trail(fresh_database, LAPSED_POLICY) == [
        ("claim.triaging", "triage-started"),
        ("claim.awaiting_adjuster", "rules-referred"),
        ("claim.triaging", "adjuster-sent-back"),
        ("claim.awaiting_adjuster", "rules-referred"),
    ]

    decided = stack.decide(LAPSED_POLICY, "reject")

    assert decided.status_code == 200, decided.text
    assert decided.json() == {
        "claim_id": LAPSED_POLICY,
        "state": "rejected",
        "run_id": str(new_run),
        "run_status": "Completed",
    }
    assert run_status(fresh_database, new_run) == "Completed"
    assert notes(fresh_database) == [
        (old_run, DECISION_NOTES["send_back"]),
        (new_run, DECISION_NOTES["reject"]),
    ]
    assert decisions(fresh_database) == [
        (LAPSED_POLICY, old_run, "send_back"),
        (LAPSED_POLICY, new_run, "reject"),
    ]
    no_checkpoint_left(fresh_database)
    assert claim_trail(fresh_database, LAPSED_POLICY)[-1] == (
        "claim.rejected",
        "adjuster-rejected",
    )


# ── 3. withdrawal of a paused claim ─────────────────────────────────────────
def test_a_withdrawn_claim_ends_its_paused_run_once_however_often_it_is_posted(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    run_id = post_paused(stack, LAPSED_POLICY)

    first = stack.withdraw(LAPSED_POLICY)

    assert first.status_code == 200, first.text
    assert first.json() == {
        "claim_id": LAPSED_POLICY,
        "state": "withdrawn",
        "run_id": str(run_id),
        "run_status": "Completed",
        "proposal": None,
    }
    assert states(fresh_database)[LAPSED_POLICY] == "withdrawn"
    assert run_status(fresh_database, run_id) == "Completed"
    assert notes(fresh_database) == [(run_id, DECISION_NOTES["withdrawn"])]
    assert decisions(fresh_database) == [(LAPSED_POLICY, run_id, "withdrawn")]
    no_checkpoint_left(fresh_database)
    assert claim_trail(fresh_database, LAPSED_POLICY) == [
        ("claim.triaging", "triage-started"),
        ("claim.awaiting_adjuster", "rules-referred"),
        ("claim.withdrawn", "claimant-withdrew"),
    ]
    events = run_events(fresh_database, run_id)
    assert events == [
        "run.started",
        "run.awaiting_approval",
        "run.resumed",
        "run.completed",
    ]

    again = stack.withdraw(LAPSED_POLICY)

    # The same answer, and nothing more written: the resume of an ended run
    # answers its status and runs nothing.
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert notes(fresh_database) == [(run_id, DECISION_NOTES["withdrawn"])]
    assert decisions(fresh_database) == [(LAPSED_POLICY, run_id, "withdrawn")]
    assert run_events(fresh_database, run_id) == events
    assert len(claim_trail(fresh_database, LAPSED_POLICY)) == 3


# ── 4. documents ────────────────────────────────────────────────────────────
def test_the_documents_reported_for_a_claim_change_the_rules_answer(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    posted = stack.post(CLAIMS[DOCUMENTS_REQUESTED])
    assert posted.status_code == 201, posted.text
    assert (posted.json()["run_status"], posted.json()["state"]) == AFTER_TRIAGE[
        "request_documents"
    ]
    first_run = uuid.UUID(posted.json()["run_id"])
    ((_, first),) = proposals_of(fresh_database, DOCUMENTS_REQUESTED)
    assert first.route == "request_documents"
    assert first.reason == "missing_documents"
    missing = list(first.missing_documents)
    assert missing

    answer = stack.report_documents(DOCUMENTS_REQUESTED, missing)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    second_run = uuid.UUID(body["run_id"])
    assert second_run != first_run
    assert body["proposal"]["route"] == ROUTE_WITH_THE_DOCUMENTS
    assert (body["run_status"], body["state"]) == AFTER_TRIAGE[ROUTE_WITH_THE_DOCUMENTS]
    # A second proposal for the claim, of a new run: the documents are there,
    # so nothing is missing and the rules' answer is no longer a request.
    stored = proposals_of(fresh_database, DOCUMENTS_REQUESTED)
    assert [run for run, _ in stored] == [first_run, second_run]
    ((_, _), (_, second)) = stored
    assert second.route == ROUTE_WITH_THE_DOCUMENTS
    assert second.route != "request_documents"
    assert second.missing_documents == ()
    assert run_status(fresh_database, second_run) == "Completed"
    # The first run had ended and stays ended; nothing was written for it.
    assert run_status(fresh_database, first_run) == "Completed"
    assert table_count(fresh_database, "claims.notes") == 0
    assert table_count(fresh_database, "claims.decisions") == 0
    assert run_of(fresh_database, DOCUMENTS_REQUESTED) == second_run
    assert owner_rows(
        fresh_database,
        "SELECT name FROM claims.claim_documents WHERE claim_id = %s ORDER BY name",
        (DOCUMENTS_REQUESTED,),
    ) == [(name,) for name in sorted(missing)]
    # The submission stays as the claimant wrote it.
    ((submission,),) = owner_rows(
        fresh_database,
        "SELECT submission FROM claims.claims WHERE claim_id = %s",
        (DOCUMENTS_REQUESTED,),
    )
    assert submission["documents"] == CLAIMS[DOCUMENTS_REQUESTED]["documents"]
    assert claim_trail(fresh_database, DOCUMENTS_REQUESTED) == [
        ("claim.triaging", "triage-started"),
        ("claim.documents_requested", "rules-requested-documents"),
        ("claim.triaging", "documents-arrived"),
        ("claim.approved", "rules-approved"),
    ]


# ── 5. withdrawal while documents are requested ─────────────────────────────
def test_a_claim_withdrawn_while_documents_are_requested_writes_nothing_for_its_run(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    posted = stack.post(CLAIMS[DOCUMENTS_REQUESTED])
    assert posted.json()["state"] == "documents_requested"
    run_id = uuid.UUID(posted.json()["run_id"])

    answer = stack.withdraw(DOCUMENTS_REQUESTED)

    assert answer.status_code == 200, answer.text
    assert answer.json()["claim_id"] == DOCUMENTS_REQUESTED
    assert answer.json()["state"] == "withdrawn"
    assert answer.json()["proposal"] is None
    assert states(fresh_database)[DOCUMENTS_REQUESTED] == "withdrawn"
    # Its run had ended and stays ended: no word recorded, no note written. (A
    # best-effort resume of an ended run may be called; it runs nothing.)
    assert run_status(fresh_database, run_id) == "Completed"
    assert table_count(fresh_database, "claims.notes") == 0
    assert table_count(fresh_database, "claims.decisions") == 0
    assert claim_trail(fresh_database, DOCUMENTS_REQUESTED) == [
        ("claim.triaging", "triage-started"),
        ("claim.documents_requested", "rules-requested-documents"),
        ("claim.withdrawn", "claimant-withdrew"),
    ]


# ── 6. the cap ──────────────────────────────────────────────────────────────
def test_a_claim_triaged_five_times_refuses_a_sixth_and_can_still_be_decided(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    post_paused(stack, LAPSED_POLICY)
    for _ in range(4):
        sent_back = stack.triage_again(LAPSED_POLICY)
        assert sent_back.status_code == 200, sent_back.text
        assert sent_back.json()["state"] == "awaiting_adjuster"
    assert owner_rows(fresh_database, "SELECT triages FROM claims.claims") == [(5,)]
    assert runs_of(fresh_database, LAPSED_POLICY) == 5
    paused_run = run_of(fresh_database, LAPSED_POLICY)

    refused = stack.triage_again(LAPSED_POLICY)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == TRIAGE_CAP_DETAIL
    # No run started, and the claim kept its paused run.
    assert runs_of(fresh_database, LAPSED_POLICY) == 5
    assert states(fresh_database)[LAPSED_POLICY] == "awaiting_adjuster"
    assert run_of(fresh_database, LAPSED_POLICY) == paused_run
    assert run_status(fresh_database, paused_run) == "AwaitingApproval"
    assert owner_rows(fresh_database, "SELECT triages FROM claims.claims") == [(5,)]

    decided = stack.decide(LAPSED_POLICY, "reject")

    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "rejected"
    assert decided.json()["run_status"] == "Completed"
    assert run_status(fresh_database, paused_run) == "Completed"
    no_checkpoint_left(fresh_database)


# ── 7. a failed triage, decided ─────────────────────────────────────────────
def test_an_adjuster_decides_a_claim_whose_triage_failed_with_no_run_to_resume(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    """The gateway's window is not cleared between claims (as in
    ``test_triage_stack``): the third claim whose run asks the model is refused
    at its first search, its run fails and the claim is ``triage_failed``."""
    first, second, third = sorted(claims_that_ask_the_model())[:3]
    assert stack.post(CLAIMS[first], advance=False).status_code == 201
    assert stack.post(CLAIMS[second], advance=False).status_code == 201
    refused = stack.post(CLAIMS[third], advance=False)
    assert refused.status_code == 502
    failed_run = uuid.UUID(refused.json()["run_id"])
    assert states(fresh_database)[third] == "triage_failed"
    assert run_status(fresh_database, failed_run) == "Failed"
    runs_before = runs_of(fresh_database, third)

    answer = stack.decide(third, "approve")

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "claim_id": third,
        "state": "approved",
        "run_id": None,
        "run_status": None,
    }
    assert states(fresh_database)[third] == "approved"
    # The decision is recorded with no run; nothing was resumed or written.
    assert owner_rows(
        fresh_database,
        "SELECT claim_id, run_id, decision FROM claims.decisions WHERE claim_id = %s",
        (third,),
    ) == [(third, None, "approve")]
    assert runs_of(fresh_database, third) == runs_before
    assert run_status(fresh_database, failed_run) == "Failed"
    assert "run.resumed" not in run_events(fresh_database, failed_run)
    # No run in the whole database was resumed, and no note was written (the
    # two claims posted before are paused, and nobody decided them).
    assert owner_rows(
        fresh_database, "SELECT count(*) FROM audit.events WHERE event = 'run.resumed'"
    ) == [(0,)]
    assert table_count(fresh_database, "claims.notes") == 0
    # The claim's trail: the referral and the decision are one transaction, so
    # they share a moment and are compared as a pair after the two before them.
    trail = claim_trail(fresh_database, third)
    assert trail[:2] == [
        ("claim.triaging", "triage-started"),
        ("claim.triage_failed", "triage-failed"),
    ]
    assert len(trail) == 4
    assert set(trail[2:]) == {
        ("claim.awaiting_adjuster", "triage-referred"),
        ("claim.approved", "adjuster-approved"),
    }
    moments = {
        moment
        for moment, event, _ in owner_rows(fresh_database, CLAIM_EVENTS_SQL, (third,))
        if event in {"claim.awaiting_adjuster", "claim.approved"}
    }
    assert len(moments) == 1


# ── the claims a policy has had: seeded history and the claims decided here ──
# CLM-0019 is a flood on a policy whose seeded history holds one claim, three
# months before its loss (2026-03-12); the rules approve it. A claim on the
# same policy with a loss date after it, still inside the year, has two claims
# behind it once CLM-0019 is decided, and the rules' count is two. The claims
# built here come without their documents, so they wait for them and are
# decided by nobody: only CLM-0019 is a decided claim of the policy.
DECIDED_BY_THE_RULES = "CLM-0019"
LATER_LOSS = date(2026, 7, 10)


def later_claim_on_the_policy(claim_id: str) -> dict[str, Any]:
    """A golden claim's synthetic values under a new ID, lost and reported
    after the golden claim's loss, with no documents."""
    return CLAIMS[DECIDED_BY_THE_RULES] | {
        "claim_id": claim_id,
        "loss_date": LATER_LOSS.isoformat(),
        "reported_on": LATER_LOSS.isoformat(),
        "documents": [],
    }


def indicators_of(db: DatabaseHandle, claim_id: str) -> list[str]:
    ((_, proposal),) = proposals_of(db, claim_id)
    return list(proposal.fraud_indicators)


def test_a_claim_decided_here_counts_towards_the_next_claims_frequent_claims(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    # The seeded history alone is one claim in the year: no indicator.
    without = stack.post(later_claim_on_the_policy("CLM-9101"))
    assert without.status_code == 201, without.text
    assert indicators_of(fresh_database, "CLM-9101") == []

    # The golden claim on the policy is approved by the rules.
    decided = stack.post(CLAIMS[DECIDED_BY_THE_RULES])
    assert decided.status_code == 201, decided.text
    assert states(fresh_database)[DECIDED_BY_THE_RULES] == "approved"

    # The seeded claim and the decided one make two: the indicator. The claim
    # that waited for its documents (not decided) did not count.
    with_it = stack.post(later_claim_on_the_policy("CLM-9102"))
    assert with_it.status_code == 201, with_it.text
    assert indicators_of(fresh_database, "CLM-9102") == ["frequent_claims"]
    assert states(fresh_database)["CLM-9101"] == "documents_requested"
    assert states(fresh_database)["CLM-9102"] == "documents_requested"
