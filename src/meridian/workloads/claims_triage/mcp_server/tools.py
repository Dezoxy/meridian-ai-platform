"""The claims server's two write tools (S013).

Each stores one row under the idempotency key the runtime derived (T-23). A key
is unique per run. The insert is ``ON CONFLICT (run_id, idempotency_key) DO
NOTHING``, so two calls of one run with one key cannot both insert, however
they interleave: at READ COMMITTED, which ``connect()`` pins, the second waits
for the first's transaction and then finds the row. At a stricter level it
would fail with a serialization error instead. Reading first and inserting
after would let both pass the read. A repeated call with the same payload
answers the stored ID; the same key and run with another claim or payload is
refused. The same key from another run is another row, and no answer says that
a key exists in another run.
"""

from dataclasses import dataclass

import psycopg
from psycopg import sql

from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolHandler,
)


@dataclass(frozen=True, slots=True)
class Store:
    """An append-only table with an idempotency key. Names are constants of
    this module, never input."""

    table: str
    id_column: str
    text_column: str
    text_argument: str


NOTES = Store("notes", "note_id", "note", "note")
APPROVAL_REQUESTS = Store("approval_requests", "request_id", "reason", "reason")

INSERT = """
INSERT INTO claims.{table}
    (claim_id, run_id, agent, {text}, idempotency_key, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id, idempotency_key) DO NOTHING
RETURNING {id}
"""

# Only the columns the role may read: the replay columns, never the text.
SELECT_BY_KEY = """
SELECT {id}, claim_id, payload_hash
FROM claims.{table}
WHERE run_id = %s AND idempotency_key = %s
"""


def _statement(template: str, store: Store) -> sql.Composed:
    return sql.SQL(template).format(
        table=sql.Identifier(store.table),
        text=sql.Identifier(store.text_column),
        id=sql.Identifier(store.id_column),
    )


def insert_or_replay(
    conn: psycopg.Connection, call: ToolCall, store: Store
) -> Completed | Refused:
    key = call.idempotency_key
    if key is None:  # the kit refuses a write without one; this is a backstop
        return Refused("idempotency-key-missing")
    binding = call.binding
    inserted = conn.execute(
        _statement(INSERT, store),
        (
            binding.claim_id,
            binding.run_id,
            binding.agent,
            call.arguments[store.text_argument],
            key,
            call.payload_hash,
        ),
    ).fetchone()
    if inserted is not None:
        return Completed({store.id_column: str(inserted[0]), "replayed": False})
    existing = conn.execute(
        _statement(SELECT_BY_KEY, store), (binding.run_id, key)
    ).fetchone()
    if existing is None:
        # The conflicting row cannot be gone: nothing deletes from these tables.
        raise RuntimeError("an idempotency conflict without a stored row")
    stored_id, claim_id, payload_hash = existing
    if (claim_id, payload_hash) != (binding.claim_id, call.payload_hash):
        return Refused("idempotency-key-reused")
    return Completed({store.id_column: str(stored_id), "replayed": True}, replayed=True)


def add_claim_note(conn: psycopg.Connection, call: ToolCall) -> Completed | Refused:
    return insert_or_replay(conn, call, NOTES)


def request_approval(conn: psycopg.Connection, call: ToolCall) -> Completed | Refused:
    return insert_or_replay(conn, call, APPROVAL_REQUESTS)


HANDLERS = (
    ToolHandler(
        tool="add_claim_note",
        scope="claims:note:write",
        bound_argument="claim_id",
        bound_to="claim_id",
        run=add_claim_note,
    ),
    ToolHandler(
        tool="request_approval",
        scope="claims:approval:request",
        bound_argument="claim_id",
        bound_to="claim_id",
        run=request_approval,
    ),
)
