"""The policy server's two read tools (S013).

Both are bound to the run's own policy number: the kit has already refused a
call whose argument names another. Neither returns a holder, an address or an
insured object, because the store keeps none (a tool result enters a prompt,
TB-7).

``claim_history`` answers from two sources in one query (S053): the seeded
``policy.claim_history``, and ``claims.decided_claims``, a read-only view of
the platform's own approved and rejected claims. A decided claim's entry has
its claim ID as ``history_id`` and its state as ``status``. The view's rows are
the run's tenant's and never the run's own claim, which the run is deciding;
the view carries no word of the claimant, so neither does an entry. The Claims
API writes nothing to the policy store (the owner's decision, T-66, T-75).
"""

import psycopg

from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolHandler,
)

HISTORY_LIMIT = 100

SELECT_POLICY = """
SELECT policy_number, product, wording_version, start_date, end_date, status,
       lapsed_on, deductible, sum_insured, cover_limit
FROM policy.policies
WHERE policy_number = %s
"""

# Both sources in one order and one cut: %s are the policy number, the policy
# number, the run's tenant, the run's claim and the limit, in that order. One
# row more than the limit, to know whether the answer was cut. A decided claim
# whose submission held no usable loss date or peril is not an entry: the view
# gives NULL there, and an entry needs both.
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
        AND loss_date IS NOT NULL
        AND peril IS NOT NULL
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
            HISTORY_LIMIT + 1,
        ),
    ).fetchall()
    entries = [
        {
            "history_id": history_id,
            "loss_date": loss_date.isoformat(),
            "peril": peril,
            "paid_amount": paid_amount,
            "status": status,
        }
        for history_id, loss_date, peril, paid_amount, status in rows[:HISTORY_LIMIT]
    ]
    return Completed({"entries": entries, "truncated": len(rows) > HISTORY_LIMIT})


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
