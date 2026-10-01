"""The policy server's two read tools (S013).

Both are bound to the run's own policy number: the kit has already refused a
call whose argument names another. Neither returns a holder, an address or an
insured object, because the store keeps none (a tool result enters a prompt,
TB-7).
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

# One row more than the limit, to know whether the answer was cut.
SELECT_HISTORY = """
SELECT history_id, loss_date, peril, paid_amount, status
FROM policy.claim_history
WHERE policy_number = %s
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
    rows = conn.execute(
        SELECT_HISTORY, (call.arguments["policy_number"], HISTORY_LIMIT + 1)
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
