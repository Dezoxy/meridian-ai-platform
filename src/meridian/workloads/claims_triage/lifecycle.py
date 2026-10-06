"""The claim's lifecycle as the Claims API keeps it (S015, S048).

``TRANSITIONS`` is the implemented half of the lifecycle: the edges of the
"Claim lifecycle" diagram in ``docs/architecture/overview/
01-meridian-ai-platform.md`` that the API moves a claim along today, each with
the trigger word its audit event carries. S015's edges are the triage's and the
adjuster's decision; S048's are the ones a person drives: sending a claim back
to triage, referring a claim whose triage failed, withdrawing a claim and the
arrival of documents. The diagram is in the overview, and a test
(``test_claims_lifecycle.py``) keeps the two equal: every implemented edge is in
the diagram, and the edges not implemented are listed there by name.

Every change of state goes through ``move_claim``, in the caller's transaction.
"""

import re
from datetime import datetime, timedelta
from typing import Literal, NamedTuple
from uuid import UUID

import psycopg

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.common.env import SettingsError

SERVICE_NAME = "claims-api"
# How many times a claim may be taken for triage, whatever starts one (a post, a
# retry, a send-back, documents, a lease taken over): each is a model call, and a
# loop of them is a bill (T-38).
MAX_TRIAGES_PER_CLAIM = 5
# How long a claim waits for the claimant's documents before the sweep refers it
# to an adjuster (S052): the owner's decision of 2026-10-03, counted from the
# claim's last move to ``documents_requested``. The sweep's setting may change it.
DOCUMENTS_DEADLINE_DAYS = 14
# The variable that sets those days, for the sweep (which acts on it) and for the
# Claims API (whose status page tells the claimant the day): one name, one parser
# and one sum, here, so the two agree when both are given the same value. The
# chart sets it on the sweep's CronJob and on the Claims API's Deployment from
# one value (``sweep.documentsDeadlineDays``), and a test holds the two rendered
# values equal. Whole days, ASCII digits.
DOCUMENTS_DEADLINE_ENV = "MERIDIAN_SWEEP_DOCUMENTS_DEADLINE_DAYS"
MIN_DEADLINE_DAYS = 1
MAX_DEADLINE_DAYS = 365
SECONDS_PER_DAY = 24 * 60 * 60
# The agent a claim's triage runs. Here, not in ``triaging.py``, with the lease
# below: the sweep imports them, and ``triaging.py`` loads FastAPI, httpx and
# OpenTelemetry, which a job that needs only PostgreSQL does not.
AGENT = "claims-triage"
# The agent of the claim brief (S037), here for the same reason: the sweep lists
# its runs, and ``briefs.py`` loads FastAPI.
BRIEF_AGENT = "claim-brief"
# What a call to the Agent Runtime may take, per phase (httpx's four timeouts;
# ``triaging.runtime_timeout`` builds them). Each value bounds one phase, not the
# call: the connect value is given to the TCP connect and again to the TLS
# handshake, and a read is each wait for bytes, so a runtime that sends its answer
# in pieces can take longer. One that answers in one piece is waited for at most
# pool + 2 x connect + write + read.
RUNTIME_TIMEOUT_SECONDS = 60.0
RUNTIME_CONNECT_TIMEOUT_SECONDS = 3.0
RUNTIME_WRITE_TIMEOUT_SECONDS = 5.0
RUNTIME_POOL_TIMEOUT_SECONDS = 3.0
# How long a claim may stay ``triaging`` before another post takes the triage
# over: twice the read timeout, so a live request is not robbed. The lease has to
# cover two calls one after the other: the end of the old run (``end_run``, a
# shorter read) and the new triage, in ``add_documents`` and ``triage_again``.
# With the values above that is 29 s + 74 s = 103 s of 120 s (a test holds it).
TRIAGE_LEASE_SECONDS = 2 * RUNTIME_TIMEOUT_SECONDS

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
# A decision on a claim whose triage failed refers it first (S048).
TRIAGE_REFERRED = Transition("triage_failed", "awaiting_adjuster", "triage-referred")
ADJUSTER_SENT_BACK = Transition("awaiting_adjuster", "triaging", "adjuster-sent-back")
CLAIMANT_WITHDREW_WAITING = Transition(
    "awaiting_adjuster", "withdrawn", "claimant-withdrew"
)
CLAIMANT_WITHDREW_DOCUMENTS = Transition(
    "documents_requested", "withdrawn", "claimant-withdrew"
)
DOCUMENTS_ARRIVED = Transition("documents_requested", "triaging", "documents-arrived")
# Documents that arrive after the last triage the cap allows refer the claim.
DOCUMENTS_AT_CAP = Transition(
    "documents_requested", "awaiting_adjuster", "triage-cap-reached"
)
# The scheduled sweep's moves (S052). It refers a claim to an adjuster when the
# documents never came (no claim is rejected without a person), and fails a claim
# whose triage never started or never ended, so that a person sees it. TRIAGE_FAILED
# is the triage's own word for the second source; the reason tells them apart.
DOCUMENTS_OVERDUE = Transition(
    "documents_requested", "awaiting_adjuster", "documents-overdue"
)
TRIAGE_NOT_STARTED = Transition("submitted", "triage_failed", "triage-not-started")
TRIAGE_ABANDONED = Transition("triaging", "triage_failed", "triage-abandoned")

# The one audit event that is no move (S060): documents posted for a claim that was
# referred because its documents did not arrive. The post is refused (409) and
# the claim stays where it is; the event says that documents were tried, so that the
# adjuster's page can. Not ``claim.awaiting_adjuster``, which is the referral the
# page reads its reason from. Closed vocabulary: the reason is a fixed word, and the
# event holds no document's name, count or content.
DOCUMENTS_REFUSED_EVENT = "claim.documents_refused"
DOCUMENTS_REFUSED_OUTCOME = "refused"
AFTER_DEADLINE_REASON = "after-deadline"

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
        TRIAGE_REFERRED,
        ADJUSTER_SENT_BACK,
        CLAIMANT_WITHDREW_WAITING,
        CLAIMANT_WITHDREW_DOCUMENTS,
        DOCUMENTS_ARRIVED,
        DOCUMENTS_AT_CAP,
        DOCUMENTS_OVERDUE,
        TRIAGE_NOT_STARTED,
        TRIAGE_ABANDONED,
    }
)


def deadline_days_of(raw: str | None) -> int:
    """The days a claim waits for documents from the variable's value: the
    default when it is unset (``None``), else a whole number from
    ``MIN_DEADLINE_DAYS`` to ``MAX_DEADLINE_DAYS``. Raises ``SettingsError``
    naming the variable, never its value."""
    if raw is None:
        return DOCUMENTS_DEADLINE_DAYS
    # ASCII digits only: ``\d`` and ``int`` accept other scripts' digits.
    if re.fullmatch(r"[0-9]{1,4}", raw):
        days = int(raw)
        if MIN_DEADLINE_DAYS <= days <= MAX_DEADLINE_DAYS:
            return days
    raise SettingsError(
        f"{DOCUMENTS_DEADLINE_ENV} must be a whole number of days "
        f"from {MIN_DEADLINE_DAYS} to {MAX_DEADLINE_DAYS}"
    )


def deadline_seconds(days: int) -> float:
    """How long a claim may wait for documents, in seconds: what the sweep tests
    a claim's age against."""
    return float(days * SECONDS_PER_DAY)


def documents_due(requested_at: datetime, days: int) -> datetime:
    """The moment the sweep refers a claim whose documents were asked for at
    ``requested_at`` (its ``state_changed_at``): the moment its age passes
    ``deadline_seconds``."""
    return requested_at + timedelta(seconds=deadline_seconds(days))


MOVE_CLAIM = """
UPDATE claims.claims
SET state = %(target)s, state_changed_at = clock_timestamp(), run_id = %(run_id)s,
    triages = triages + (%(target)s::text = 'triaging')::int
WHERE claim_id = %(claim_id)s AND tenant = %(tenant)s AND state = %(source)s
    AND (%(changed_at)s::timestamptz IS NULL OR state_changed_at = %(changed_at)s)
    AND (%(target)s::text <> 'triaging' OR triages < %(max_triages)s)
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
    service: str = SERVICE_NAME,
) -> datetime | None:
    """Move a claim along one listed transition, in the caller's transaction.

    A compare-and-set: the claim moves only if it is the tenant's, is in the
    transition's source state and, when ``changed_at`` is given, has not changed
    since then (a request that holds a claim's triage passes the moment it took
    it, so that a triage taken over by another request is not overwritten).
    Returns when the claim changed, or ``None`` if it did not move. When it
    moved, the audit event is written in the same transaction. ``run_id`` is the
    run the change is about, ``None`` while none is known; it replaces the
    claim's. ``service`` is the one the audit event names, the Claims API unless
    the sweep moves the claim.

    A move whose target is ``triaging`` raises the claim's ``triages`` by one in
    the same ``UPDATE``, and is refused (``None``, like any other compare-and-set
    that does not match) when the claim has been triaged ``MAX_TRIAGES_PER_CLAIM``
    times already, so a claim cannot exceed the cap (T-38) even for a caller that
    does not check it first; callers check under their lock to choose an answer.
    A move to any other state leaves ``triages`` alone.

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
            "max_triages": MAX_TRIAGES_PER_CLAIM,
        },
    ).fetchone()
    if row is None:
        return None
    record_event(
        conn,
        AuditEvent(
            service=service,
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
