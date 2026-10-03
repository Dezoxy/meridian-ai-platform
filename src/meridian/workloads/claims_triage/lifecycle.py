"""The claim's lifecycle as the Claims API keeps it (S015).

``TRANSITIONS`` is the implemented half of the lifecycle: the edges of the
"Claim lifecycle" diagram in ``docs/architecture/overview/
01-meridian-ai-platform.md`` that the API moves a claim along today, each with
the trigger word its audit event carries. The diagram is in the overview, and a
test (``test_claims_lifecycle.py``) keeps the two equal: every implemented edge
is in the diagram, and the edges not implemented are listed there by name.

Every change of state goes through ``move_claim``, in the caller's transaction.
"""

from datetime import datetime
from typing import Literal, NamedTuple
from uuid import UUID

import psycopg

from meridian.platform.common.audit import AuditEvent, record_event

SERVICE_NAME = "claims-api"

LifecycleState = Literal[
    "submitted",
    "triaging",
    "triage_failed",
    "awaiting_adjuster",
    "documents_requested",
    "approved",
    "rejected",
    "withdrawn",
]


class Transition(NamedTuple):
    source: LifecycleState
    target: LifecycleState
    trigger: str


TRIAGE_STARTED = Transition("submitted", "triaging", "triage-started")
TRIAGE_RETRIED = Transition("triage_failed", "triaging", "triage-retried")
# A triage that an API died in the middle of must not lock its claim forever,
# so a claim that has been ``triaging`` longer than the lease is triaged again.
# The diagram has no edge for it: the claim stays in Triaging.
TRIAGE_RECLAIMED = Transition("triaging", "triaging", "triage-reclaimed")
RULES_APPROVED = Transition("triaging", "approved", "rules-approved")
RULES_REFERRED = Transition("triaging", "awaiting_adjuster", "rules-referred")
RULES_REQUESTED_DOCUMENTS = Transition(
    "triaging", "documents_requested", "rules-requested-documents"
)
TRIAGE_FAILED = Transition("triaging", "triage_failed", "triage-failed")
ADJUSTER_APPROVED = Transition("awaiting_adjuster", "approved", "adjuster-approved")
ADJUSTER_REJECTED = Transition("awaiting_adjuster", "rejected", "adjuster-rejected")
ADJUSTER_REQUESTED_DOCUMENTS = Transition(
    "awaiting_adjuster", "documents_requested", "adjuster-requested-documents"
)

TRANSITIONS: frozenset[Transition] = frozenset(
    {
        TRIAGE_STARTED,
        TRIAGE_RETRIED,
        TRIAGE_RECLAIMED,
        RULES_APPROVED,
        RULES_REFERRED,
        RULES_REQUESTED_DOCUMENTS,
        TRIAGE_FAILED,
        ADJUSTER_APPROVED,
        ADJUSTER_REJECTED,
        ADJUSTER_REQUESTED_DOCUMENTS,
    }
)

MOVE_CLAIM = """
UPDATE claims.claims
SET state = %(target)s, state_changed_at = clock_timestamp(), run_id = %(run_id)s
WHERE claim_id = %(claim_id)s AND tenant = %(tenant)s AND state = %(source)s
    AND (%(changed_at)s::timestamptz IS NULL OR state_changed_at = %(changed_at)s)
RETURNING state_changed_at
"""


def move_claim(
    conn: psycopg.Connection,
    transition: Transition,
    *,
    claim_id: str,
    tenant: str,
    run_id: UUID | None = None,
    changed_at: datetime | None = None,
) -> datetime | None:
    """Move a claim along one listed transition, in the caller's transaction.

    A compare-and-set: the claim moves only if it is the tenant's, is in the
    transition's source state and, when ``changed_at`` is given, has not changed
    since then (a request that holds a claim's triage passes the moment it took
    it, so that a triage taken over by another request is not overwritten).
    Returns when the claim changed, or ``None`` if it did not move. When it
    moved, the audit event is written in the same transaction. ``run_id`` is the
    run the change is about, ``None`` while none is known; it replaces the
    claim's.

    Raises ``ValueError`` before any SQL for a transition not in the table.
    """
    if transition not in TRANSITIONS:
        raise ValueError(f"not a transition of the claim lifecycle: {transition}")
    row = conn.execute(
        MOVE_CLAIM,
        {
            "target": transition.target,
            "run_id": run_id,
            "claim_id": claim_id,
            "tenant": tenant,
            "source": transition.source,
            "changed_at": changed_at,
        },
    ).fetchone()
    if row is None:
        return None
    record_event(
        conn,
        AuditEvent(
            service=SERVICE_NAME,
            event=f"claim.{transition.target}",
            outcome=transition.target,
            reason=transition.trigger,
            tenant=tenant,
            run_id=run_id,
            reference=claim_id,
        ),
    )
    moved_at: datetime = row[0]
    return moved_at
