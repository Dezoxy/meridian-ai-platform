"""The policy server's two read tools (S013).

Both are bound to the run's own policy number: the kit has already refused a
call whose argument names another. Neither returns a holder, an address or an
insured object, because the store keeps none (a tool result enters a prompt,
TB-7).

``claim_history`` answers from three sources in one query (S053, S067): the
seeded ``policy.claim_history``, ``claims.decided_claims``, a read-only view of
the platform's own approved and rejected claims, and ``claims.open_claims``,
one of the claims still open (submitted, triaging, failed triage, waiting for
an adjuster or for documents; never a withdrawn one). A claim's entry has its
claim ID as ``history_id`` and its state as ``status``. The views' rows are the
run's tenant's and never the run's own claim, which the run is deciding; the
views carry no word of the claimant, so neither does an entry. The Claims API
writes nothing to the policy store (the owner's decision, T-66, T-76).
"""

import logging

import psycopg

from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolHandler,
)

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 100

SELECT_POLICY = """
SELECT policy_number, product, wording_version, start_date, end_date, status,
       lapsed_on, deductible, sum_insured, cover_limit
FROM policy.policies
WHERE policy_number = %s
"""

# The three sources in one order and one cut: %s are the policy number (the
# seeded history), the policy number, the run's tenant and the run's claim (the
# decided claims), the same three again (the open claims, S067), and the limit,
# in that order. One row more than the limit, to know whether the answer was
# cut. A claim of the claims store whose submission held no usable loss date or
# peril comes with a NULL there: it is not an entry, and the answer is
# `truncated` (see claim_history). The query's conditions on the views must stay
# plain comparisons of the views' columns (leakproof operators), or PostgreSQL
# evaluates them above the security_barrier and scans every claim of the view.
SELECT_HISTORY = """
SELECT history_id, loss_date, peril, paid_amount, status
FROM (
    SELECT history_id, loss_date, peril, paid_amount, status
    FROM policy.claim_history
    WHERE policy_number = %s
    UNION ALL
    SELECT claim_id, loss_date, peril, paid_amount, state
    FROM claims.decided_claims
    WHERE policy_number = %s
        AND tenant = %s
        AND claim_id <> %s
    UNION ALL
    SELECT claim_id, loss_date, peril, paid_amount, state
    FROM claims.open_claims
    WHERE policy_number = %s
        AND tenant = %s
        AND claim_id <> %s
) AS entries
ORDER BY loss_date DESC, history_id
LIMIT %s
"""


def policy_lookup(conn: psycopg.Connection, call: ToolCall) -> Completed | Refused:
    row = conn.execute(SELECT_POLICY, (call.arguments["policy_number"],)).fetchone()
    if row is None:
        return Completed({"found": False})
    (
        number,
        product,
        wording,
        start,
        end,
        status,
        lapsed_on,
        deductible,
        sum_insured,
        cover_limit,
    ) = row
    policy: dict[str, str | int] = {
        "policy_number": number,
        "product": product,
        "wording_version": wording,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "status": status,
        "deductible": deductible,
        "limit": cover_limit,
    }
    if lapsed_on is not None:
        policy["lapsed_on"] = lapsed_on.isoformat()
    if sum_insured is not None:
        policy["sum_insured"] = sum_insured
    return Completed({"found": True, "policy": policy})


def claim_history(conn: psycopg.Connection, call: ToolCall) -> Completed | Refused:
    policy_number = call.arguments["policy_number"]
    rows = conn.execute(
        SELECT_HISTORY,
        (
            policy_number,
            policy_number,
            call.binding.tenant,
            call.binding.claim_id,
            policy_number,
            call.binding.tenant,
            call.binding.claim_id,
            HISTORY_LIMIT + 1,
        ),
    ).fetchall()
    kept = rows[:HISTORY_LIMIT]
    # A claim the views could not read (a NULL date or peril) is no
    # entry, and the rules must know they count less than there is. A NULL date
    # sorts first under DESC, so such a row is always inside the rows read. A
    # row with a date and a NULL peril sorts by its date and may be behind the
    # cut, where it is neither counted nor logged; then the rows read are more
    # than the limit and the answer is `truncated` all the same.
    unreadable = sum(1 for row in rows if row[1] is None or row[2] is None)
    if unreadable:
        logger.warning(
            "claim_history for run %s left out %d claims of the claims store that "
            "could not be read",
            call.binding.run_id,
            unreadable,
        )
    entries = [
        {
            "history_id": history_id,
            "loss_date": loss_date.isoformat(),
            "peril": peril,
            "paid_amount": paid_amount,
            "status": status,
        }
        for history_id, loss_date, peril, paid_amount, status in kept
        if loss_date is not None and peril is not None
    ]
    return Completed(
        {"entries": entries, "truncated": len(rows) > HISTORY_LIMIT or unreadable > 0}
    )


HANDLERS = (
    ToolHandler(
        tool="policy_lookup",
        scope="policy:read",
        bound_argument="policy_number",
        bound_to="policy_number",
        run=policy_lookup,
    ),
    ToolHandler(
        tool="claim_history",
        scope="claims:history:read",
        bound_argument="policy_number",
        bound_to="policy_number",
        run=claim_history,
    ),
)
