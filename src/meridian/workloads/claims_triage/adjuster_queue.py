"""What the adjuster's queue reads (S016, S060): one page of the claims that wait
for a person, after the row a cursor names. Moved out of ``adjuster.py``, which
imports these names back, so ``adjuster.load_queue`` and the others still
resolve; the page that shows them is ``render_queue`` there. The judgment of a
stored proposal, which the queue's cell and the claim's page share, is here too
(S070): ``_proposal_of`` and the words it answers with."""

import logging
from datetime import datetime
from typing import NamedTuple

from pydantic import ValidationError

from meridian.platform.common.db import connect
from meridian.workloads.claims_triage.lifecycle import SERVICE_NAME
from meridian.workloads.claims_triage.models import invalid_fields
from meridian.workloads.claims_triage.proposal import (
    RESTS_ON_MARKS,
    TriageProposal,
    recommendation_rests_on,
)

logger = logging.getLogger(__name__)

NO_PROPOSAL_TEXT = "no proposal is stored"
NO_STRUCTURED_PROPOSAL_TEXT = "no structured proposal"
UNREADABLE_PROPOSAL_MARK = "could not be read"
UNREADABLE_PROPOSAL_TEXT = f"the stored proposal {UNREADABLE_PROPOSAL_MARK}"

# The queue's two states are literals in QUEUE_SQL: the partial index of
# migration 0011 serves a query only when its WHERE implies the index's.
QUEUE_LIMIT = 100

# The queue: one page after the row named by a cursor (``after_time``,
# ``after_claim``: the last row of the page before), in the order of the index.
# The lateral ``p`` is the latest proposal, a tie in ``created_at`` broken by
# ``proposal_id`` as the decided-claims view does (0013); the lateral ``r`` is the
# move that referred a waiting claim, what REASON_SQL reads for the claim's page,
# and is looked up for a waiting claim only. A page costs at most two lateral
# lookups per row. The same lookup of ``p`` also reads the stored proposal
# itself (S070), so that the page validates it with the one function the claim's
# page uses and marks it only if it can be read; no new index or lookup.
_QUEUE_SELECT = (
    "SELECT c.claim_id, c.state, c.state_changed_at, c.submission ->> 'peril', "
    "c.submission -> 'claimed_amount', p.reason, r.reason, p.proposal "
    "FROM claims.claims AS c "
    "LEFT JOIN LATERAL (SELECT reason, proposal "
    "FROM claims.triage_proposals "
    "WHERE claim_id = c.claim_id ORDER BY created_at DESC, proposal_id LIMIT 1) "
    "AS p ON true "
    "LEFT JOIN LATERAL (SELECT reason FROM audit.claim_trail "
    "WHERE claim_id = c.claim_id AND tenant = c.tenant "
    "AND event = 'claim.awaiting_adjuster' AND c.state = 'awaiting_adjuster' "
    "ORDER BY recorded_at DESC, seq DESC LIMIT 1) AS r ON true "
    "WHERE c.tenant = %s AND c.state IN ('awaiting_adjuster', 'triage_failed') "
)
_QUEUE_AFTER = "AND (c.state_changed_at, c.claim_id) > (%s, %s) "
_QUEUE_ORDER = "ORDER BY c.state_changed_at, c.claim_id LIMIT %s"
QUEUE_SQL = _QUEUE_SELECT + _QUEUE_ORDER
QUEUE_AFTER_SQL = _QUEUE_SELECT + _QUEUE_AFTER + _QUEUE_ORDER


class QueueRow(NamedTuple):
    claim_id: str
    state: str
    since: datetime
    peril: str | None
    claimed_amount: object
    reason: str | None
    # The reason of the move that referred a waiting claim; ``None`` for a claim
    # in another state and for a move with no reason.
    referral_reason: str | None
    # The latest proposal as stored (the decoded JSON, not yet validated: the
    # page does that); ``None`` for a claim with no proposal and for a row of the
    # walking skeleton, which has a draft and no structured proposal.
    proposal: object = None


class QueueCursor(NamedTuple):
    """The last row of a page of the queue: where the next page starts."""

    since: datetime
    claim_id: str


def load_queue(
    dsn: str, tenant: str, after: QueueCursor | None = None
) -> tuple[list[QueueRow], QueueCursor | None]:
    """A page of the queue, after the row ``after`` names (the first page when
    ``None``), and the cursor of the page that follows (``None`` on the last).
    One row more than the page is read: it only says there is a next page."""
    with connect(dsn, SERVICE_NAME) as conn:
        if after is None:
            rows = conn.execute(QUEUE_SQL, (tenant, QUEUE_LIMIT + 1)).fetchall()
        else:
            rows = conn.execute(
                QUEUE_AFTER_SQL, (tenant, *after, QUEUE_LIMIT + 1)
            ).fetchall()
    page = [QueueRow(*row) for row in rows[:QUEUE_LIMIT]]
    has_next = len(rows) > QUEUE_LIMIT
    return page, QueueCursor(page[-1].since, page[-1].claim_id) if has_next else None


def _queue_mark(row: QueueRow) -> str:
    """The queue's cell: the proposal is judged by ``_proposal_of``, as on the
    claim's page, in memory (one validation per row, no database call)."""
    if row.proposal is None:
        return ""
    proposal, _ = _proposal_of(row.claim_id, (row.proposal,), log=False)
    if proposal is None:
        return UNREADABLE_PROPOSAL_MARK
    kind = recommendation_rests_on(proposal.recommendation, proposal.assessment)
    return RESTS_ON_MARKS.get(kind, "")


# The claim's page and the queue's cell judge a stored proposal here. The page
# logs one it cannot read; the queue passes ``log=False`` and logs one line for
# the page (``render_queue``), so that reloading it cannot multiply lines.
def _proposal_of(
    claim_id: str, row: tuple | None, *, log: bool = True
) -> tuple[TriageProposal | None, str | None]:
    """The latest proposal and, when there is none to show, why."""
    if row is None:
        return None, NO_PROPOSAL_TEXT
    if row[0] is None:
        return None, NO_STRUCTURED_PROPOSAL_TEXT
    try:
        return TriageProposal.model_validate(row[0]), None
    except ValidationError as exc:
        # The claim's ID, the class, the fields and the error types only: the
        # document is the model's text.
        if log:
            logger.warning(
                "the stored proposal of claim %s is not valid: %s %s",
                claim_id,
                type(exc).__name__,
                invalid_fields(exc),
            )
        return None, UNREADABLE_PROPOSAL_TEXT
