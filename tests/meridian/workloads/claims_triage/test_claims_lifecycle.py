"""The claim lifecycle: its table against the diagram, and the one function that
changes a claim's state."""

import re
import uuid
from datetime import datetime
from typing import Any, get_args

import pytest
from dbsupport import DatabaseHandle
from servicesupport import REPO_ROOT, owner_rows

from meridian.platform.common.db import connect
from meridian.workloads.claims_triage import lifecycle
from meridian.workloads.claims_triage.lifecycle import (
    ADJUSTER_APPROVED,
    DOCUMENTS_ARRIVED,
    DOCUMENTS_AT_CAP,
    DOCUMENTS_DEADLINE_DAYS,
    DOCUMENTS_OVERDUE,
    MAX_TRIAGES_PER_CLAIM,
    MOVE_CLAIM,
    RULES_APPROVED,
    TRANSITIONS,
    TRIAGE_ABANDONED,
    TRIAGE_FAILED,
    TRIAGE_NOT_STARTED,
    TRIAGE_RECLAIMED,
    TRIAGE_STARTED,
    LifecycleState,
    Transition,
    move_claim,
)

OVERVIEW = REPO_ROOT / "docs/architecture/overview/01-meridian-ai-platform.md"
# The diagram's names, and the words the claim's state is stored as.
DIAGRAM_NAMES = {
    "Submitted": "submitted",
    "Triaging": "triaging",
    "TriageFailed": "triage_failed",
    "AwaitingAdjuster": "awaiting_adjuster",
    "DocumentsRequested": "documents_requested",
    "Approved": "approved",
    "Rejected": "rejected",
    "Withdrawn": "withdrawn",
}
# The diagram's edges that are designed, not implemented (the overview says
# which step builds each); [*] --> Submitted is the claim's creation. None is
# left: the deadline of S052 refers a claim to an adjuster and rejects none.
DESIGNED_EDGES: set[tuple[str, str]] = set()
START = "[*]"
WITHDRAWAL = "claimant-withdrew"  # the one trigger two transitions share
CLAIM_ID = "CLM-9301"
TENANT = "claims-triage"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000009301")


def diagram_edges() -> set[tuple[str, str]]:
    """The edges of the ``stateDiagram-v2`` block under "### Claim lifecycle"."""
    text = OVERVIEW.read_text(encoding="utf-8")
    section = text.split("### Claim lifecycle", 1)[1].split("\n### ", 1)[0]
    block = re.search(r"```mermaid\n(stateDiagram-v2\n.*?)```", section, re.DOTALL)
    assert block, "no stateDiagram-v2 block under 'Claim lifecycle'"
    edges = re.findall(r"^\s*(\[\*\]|\w+) --> (\w+)", block.group(1), re.MULTILINE)
    assert edges, "the block has no edge"
    return set(edges)


# ── the table against the diagram ───────────────────────────────────────────
def test_the_diagram_names_exactly_the_states_of_the_lifecycle() -> None:
    names = {name for edge in diagram_edges() for name in edge} - {START}

    assert names == set(DIAGRAM_NAMES)
    assert set(DIAGRAM_NAMES.values()) == set(get_args(LifecycleState))


def test_every_implemented_transition_is_an_edge_of_the_diagram() -> None:
    by_word = {word: name for name, word in DIAGRAM_NAMES.items()}
    # The lease takeover keeps a claim in Triaging: the diagram has no edge for it.
    implemented = {
        (by_word[t.source], by_word[t.target])
        for t in TRANSITIONS
        if t != TRIAGE_RECLAIMED
    }

    assert implemented <= diagram_edges()


def test_the_edges_of_the_diagram_that_are_not_implemented_are_the_designed_ones() -> (
    None
):
    by_word = {word: name for name, word in DIAGRAM_NAMES.items()}
    implemented = {
        (by_word[t.source], by_word[t.target])
        for t in TRANSITIONS
        if t != TRIAGE_RECLAIMED
    }

    assert diagram_edges() - implemented - {(START, "Submitted")} == DESIGNED_EDGES


def test_the_diagram_has_no_edge_for_the_lease_takeover() -> None:
    assert ("Triaging", "Triaging") not in diagram_edges()
    assert TRIAGE_RECLAIMED in TRANSITIONS


def test_the_sweep_has_three_transitions_and_each_has_its_own_word() -> None:
    assert [
        (t.source, t.target, t.trigger)
        for t in (DOCUMENTS_OVERDUE, TRIAGE_NOT_STARTED, TRIAGE_ABANDONED)
    ] == [
        ("documents_requested", "awaiting_adjuster", "documents-overdue"),
        ("submitted", "triage_failed", "triage-not-started"),
        ("triaging", "triage_failed", "triage-abandoned"),
    ]
    assert {DOCUMENTS_OVERDUE, TRIAGE_NOT_STARTED, TRIAGE_ABANDONED} <= TRANSITIONS
    # Two words for one pair of states are allowed: the audit event's reason says
    # which of them moved the claim.
    assert (TRIAGE_FAILED.source, TRIAGE_FAILED.target) == (
        TRIAGE_ABANDONED.source,
        TRIAGE_ABANDONED.target,
    )


def test_the_deadline_for_documents_is_fourteen_days() -> None:
    # The owner's decision of 2026-10-03.
    assert DOCUMENTS_DEADLINE_DAYS == 14


def test_a_trigger_word_belongs_to_one_transition_out_of_a_state() -> None:
    # Withdrawal is one trigger from two states (S048), so the word names the
    # transition together with its source: the audit event's reason and its
    # outcome (the target) say the rest.
    keys = [(t.source, t.trigger) for t in TRANSITIONS]

    assert len(set(keys)) == len(keys)
    assert all(re.fullmatch(r"[a-z]+(-[a-z]+)+", t.trigger) for t in TRANSITIONS)
    triggers = [t.trigger for t in TRANSITIONS if t.trigger != WITHDRAWAL]
    assert len(set(triggers)) == len(triggers)
    assert {t.target for t in TRANSITIONS if t.trigger == WITHDRAWAL} == {"withdrawn"}


# ── the function that changes a state ───────────────────────────────────────
class NoSql:
    """A connection that fails the test if any SQL is run on it."""

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("SQL ran")


@pytest.mark.parametrize(
    "transition",
    [
        Transition("approved", "triaging", "triage-started"),
        Transition("submitted", "approved", "rules-approved"),
        Transition("submitted", "triaging", "anything"),
        Transition("withdrawn", "submitted", "triage-started"),
        Transition("triaging", "withdrawn", "claimant-withdrew"),
    ],
    ids=[
        "no-such-edge",
        "edge-skipped",
        "unknown-trigger",
        "out-of-a-final-state",
        "designed-not-implemented",
    ],
)
def test_a_transition_that_is_not_in_the_table_raises_before_any_sql(
    transition: Transition,
) -> None:
    with pytest.raises(ValueError, match="not a transition"):
        move_claim(NoSql(), transition, claim_id=CLAIM_ID, tenant=TENANT)  # type: ignore[arg-type]


@pytest.fixture
def claim(fresh_database: DatabaseHandle) -> DatabaseHandle:
    owner_rows(
        fresh_database,
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES (%s, %s, '{}') RETURNING 1",
        (CLAIM_ID, TENANT),
    )
    return fresh_database


def move(db: DatabaseHandle, transition: Transition, **kwargs: Any) -> datetime | None:
    """``move_claim`` as the Claims API's role, committed."""
    with connect(db.dsn("claims_api"), "test") as conn:
        moved = move_claim(conn, transition, claim_id=CLAIM_ID, tenant=TENANT, **kwargs)
        conn.commit()
    return moved


def state_of(db: DatabaseHandle) -> tuple[str, uuid.UUID | None, datetime]:
    ((state, run_id, changed_at),) = owner_rows(
        db,
        "SELECT state, run_id, state_changed_at FROM claims.claims WHERE claim_id = %s",
        (CLAIM_ID,),
    )
    return state, run_id, changed_at


def audit_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT service, event, outcome, reason, tenant, agent, run_id, reference, "
        "db_role FROM audit.events ORDER BY recorded_at",
    )


@pytest.mark.parametrize("transition", sorted(TRANSITIONS), ids=lambda t: t.trigger)
def test_every_listed_transition_moves_the_claim_and_audits_it(
    claim: DatabaseHandle, transition: Transition
) -> None:
    owner_rows(
        claim,
        "UPDATE claims.claims SET state = %s RETURNING 1",
        (transition.source,),
    )

    moved_at = move(claim, transition, run_id=RUN_ID)

    assert state_of(claim) == (transition.target, RUN_ID, moved_at)
    assert audit_rows(claim) == [
        (
            "claims-api",
            f"claim.{transition.target}",
            transition.target,
            transition.trigger,
            TENANT,
            None,
            RUN_ID,
            CLAIM_ID,
            "claims_api",
        )
    ]


def test_the_audit_event_carries_the_service_the_caller_names(
    claim: DatabaseHandle,
) -> None:
    with connect(claim.dsn("claims_api"), "test") as conn:
        moved = move_claim(
            conn,
            TRIAGE_STARTED,
            claim_id=CLAIM_ID,
            tenant=TENANT,
            service="claims-sweep",
        )
        conn.commit()

    assert moved is not None
    assert [row[0] for row in audit_rows(claim)] == ["claims-sweep"]


def test_a_claim_in_another_state_does_not_move_and_nothing_is_audited(
    claim: DatabaseHandle,
) -> None:
    moved_at = move(claim, RULES_APPROVED)  # the claim is submitted, not triaging

    assert moved_at is None
    assert state_of(claim)[0] == "submitted"
    assert audit_rows(claim) == []


def test_a_claim_of_another_tenant_does_not_move_and_nothing_is_audited(
    claim: DatabaseHandle,
) -> None:
    owner_rows(claim, "UPDATE claims.claims SET state = 'triaging' RETURNING 1")

    with connect(claim.dsn("claims_api"), "test") as conn:
        moved_at = move_claim(
            conn, RULES_APPROVED, claim_id=CLAIM_ID, tenant="another-tenant"
        )
        conn.commit()

    assert moved_at is None
    assert state_of(claim)[0] == "triaging"
    assert audit_rows(claim) == []


def test_a_claim_that_changed_since_the_given_moment_does_not_move(
    claim: DatabaseHandle,
) -> None:
    taken_at = move(claim, TRIAGE_STARTED)
    assert taken_at is not None

    other = move(claim, TRIAGE_RECLAIMED)  # another request took the triage over
    assert other is not None
    stale = move(claim, RULES_APPROVED, run_id=RUN_ID, changed_at=taken_at)

    assert stale is None
    assert state_of(claim) == ("triaging", None, other)
    assert [event for _, event, *_ in audit_rows(claim)] == [
        "claim.triaging",
        "claim.triaging",
    ]


def test_a_claim_that_has_not_changed_since_the_given_moment_moves(
    claim: DatabaseHandle,
) -> None:
    taken_at = move(claim, TRIAGE_STARTED)

    closed = move(claim, RULES_APPROVED, run_id=RUN_ID, changed_at=taken_at)

    assert closed is not None
    assert closed > taken_at
    assert state_of(claim) == ("approved", RUN_ID, closed)


def test_the_run_the_move_names_replaces_the_claims_and_none_clears_it(
    claim: DatabaseHandle,
) -> None:
    move(claim, TRIAGE_STARTED, run_id=RUN_ID)
    assert state_of(claim)[1] == RUN_ID

    move(claim, TRIAGE_RECLAIMED)

    assert state_of(claim)[1] is None


def test_a_move_and_its_audit_event_are_one_transaction(
    claim: DatabaseHandle,
) -> None:
    with connect(claim.dsn("claims_api"), "test") as conn:
        assert move_claim(conn, TRIAGE_STARTED, claim_id=CLAIM_ID, tenant=TENANT)
        conn.rollback()

    assert state_of(claim)[0] == "submitted"
    assert audit_rows(claim) == []


def test_a_decision_move_keeps_the_run_it_is_given(claim: DatabaseHandle) -> None:
    owner_rows(
        claim, "UPDATE claims.claims SET state = 'awaiting_adjuster' RETURNING 1"
    )

    move(claim, ADJUSTER_APPROVED, run_id=RUN_ID)

    assert state_of(claim)[:2] == ("approved", RUN_ID)


# ── the triage cap (T-38) ───────────────────────────────────────────────────
INTO_TRIAGING = sorted(t for t in TRANSITIONS if t.target == "triaging")
ELSEWHERE = sorted(t for t in TRANSITIONS if t.target != "triaging")
TRIAGES_SO_FAR = 2


def put_claim_in(db: DatabaseHandle, state: str, triages: int) -> None:
    owner_rows(
        db,
        "UPDATE claims.claims SET state = %s, triages = %s RETURNING 1",
        (state, triages),
    )


def triages_of(db: DatabaseHandle) -> int:
    ((triages,),) = owner_rows(
        db, "SELECT triages FROM claims.claims WHERE claim_id = %s", (CLAIM_ID,)
    )
    return triages


def test_the_cap_is_five_and_every_start_of_a_triage_is_under_it() -> None:
    assert MAX_TRIAGES_PER_CLAIM == 5
    assert {t.trigger for t in INTO_TRIAGING} == {
        "triage-started",
        "triage-retried",
        "triage-reclaimed",
        "adjuster-sent-back",
        "documents-arrived",
    }


@pytest.mark.parametrize("transition", INTO_TRIAGING, ids=lambda t: t.trigger)
def test_a_move_into_triaging_raises_the_count_by_one(
    claim: DatabaseHandle, transition: Transition
) -> None:
    put_claim_in(claim, transition.source, TRIAGES_SO_FAR)

    moved = move(claim, transition)

    assert moved is not None
    assert triages_of(claim) == TRIAGES_SO_FAR + 1


def test_the_lease_takeover_counts_as_a_triage(claim: DatabaseHandle) -> None:
    move(claim, TRIAGE_STARTED)
    assert triages_of(claim) == 1

    assert move(claim, TRIAGE_RECLAIMED) is not None

    assert triages_of(claim) == 2


@pytest.mark.parametrize("transition", ELSEWHERE, ids=lambda t: t.trigger + t.source)
def test_a_move_elsewhere_leaves_the_count_alone(
    claim: DatabaseHandle, transition: Transition
) -> None:
    put_claim_in(claim, transition.source, TRIAGES_SO_FAR)

    moved = move(claim, transition)

    assert moved is not None
    assert triages_of(claim) == TRIAGES_SO_FAR


@pytest.mark.parametrize("transition", INTO_TRIAGING, ids=lambda t: t.trigger)
def test_the_last_triage_under_the_cap_is_allowed(
    claim: DatabaseHandle, transition: Transition
) -> None:
    put_claim_in(claim, transition.source, MAX_TRIAGES_PER_CLAIM - 1)

    moved = move(claim, transition)

    assert moved is not None
    assert triages_of(claim) == MAX_TRIAGES_PER_CLAIM


@pytest.mark.parametrize("triages", [MAX_TRIAGES_PER_CLAIM, MAX_TRIAGES_PER_CLAIM + 3])
@pytest.mark.parametrize("transition", INTO_TRIAGING, ids=lambda t: t.trigger)
def test_at_the_cap_a_move_into_triaging_is_refused_and_leaves_the_claim_as_it_was(
    claim: DatabaseHandle, transition: Transition, triages: int
) -> None:
    put_claim_in(claim, transition.source, triages)
    before = state_of(claim)

    moved = move(claim, transition, run_id=RUN_ID)

    assert moved is None
    assert state_of(claim) == before
    assert triages_of(claim) == triages
    assert audit_rows(claim) == []


def test_a_claim_at_the_cap_still_moves_to_every_other_state(
    claim: DatabaseHandle,
) -> None:
    put_claim_in(claim, "documents_requested", MAX_TRIAGES_PER_CLAIM)

    refused_at_the_cap = move(claim, DOCUMENTS_ARRIVED)
    referred = move(claim, DOCUMENTS_AT_CAP)

    assert refused_at_the_cap is None
    assert referred is not None
    assert state_of(claim)[0] == "awaiting_adjuster"
    assert triages_of(claim) == MAX_TRIAGES_PER_CLAIM


def test_the_cap_the_sql_uses_is_the_constant(
    claim: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "%(max_triages)s" in MOVE_CLAIM
    assert str(MAX_TRIAGES_PER_CLAIM) not in MOVE_CLAIM
    put_claim_in(claim, "triage_failed", 1)

    monkeypatch.setattr(lifecycle, "MAX_TRIAGES_PER_CLAIM", 1)
    assert move(claim, lifecycle.TRIAGE_RETRIED) is None
    monkeypatch.setattr(lifecycle, "MAX_TRIAGES_PER_CLAIM", 2)

    assert move(claim, lifecycle.TRIAGE_RETRIED) is not None
    assert triages_of(claim) == 2
