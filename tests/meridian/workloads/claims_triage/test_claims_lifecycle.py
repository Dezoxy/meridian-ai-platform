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
from meridian.workloads.claims_triage.lifecycle import (
    ADJUSTER_APPROVED,
    RULES_APPROVED,
    TRANSITIONS,
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
# which step builds each); [*] --> Submitted is the claim's creation.
DESIGNED_EDGES = {
    ("AwaitingAdjuster", "Triaging"),
    ("AwaitingAdjuster", "Withdrawn"),
    ("DocumentsRequested", "Triaging"),
    ("DocumentsRequested", "Rejected"),
    ("DocumentsRequested", "Withdrawn"),
    ("TriageFailed", "AwaitingAdjuster"),  # S048
}
START = "[*]"
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


def test_a_trigger_word_belongs_to_one_transition() -> None:
    triggers = [t.trigger for t in TRANSITIONS]

    assert len(set(triggers)) == len(triggers)
    assert all(re.fullmatch(r"[a-z]+(-[a-z]+)+", word) for word in triggers)


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
