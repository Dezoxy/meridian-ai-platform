"""A ``CheckpointStorage`` over one PostgreSQL table, keyed by a thread ID the
host chooses. JSON only (``jsonb``), no pickle; a value that cannot be restored
is refused at save.

The store is built per run, for one thread. The framework itself calls only
``save`` and ``load``; ``get_latest``, ``list_*`` and ``delete`` are the host's.
The table is plain SQL in a scratch schema: no migration file.
"""

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from agent_framework import WorkflowCheckpoint
from agent_framework.exceptions import WorkflowCheckpointException
from psycopg import sql
from psycopg.types.json import Jsonb

from s037probe.codec import CheckpointCodec

SCHEMA = "s037_probe"
TABLE = "workflow_checkpoints"
_TABLE = sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(TABLE))


def create_table(conn: psycopg.Connection[Any]) -> None:
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
    conn.execute(
        sql.SQL(
            "CREATE TABLE {} ("
            " seq bigint GENERATED ALWAYS AS IDENTITY,"
            " thread_id uuid NOT NULL,"
            " checkpoint_id text NOT NULL,"
            " workflow_name text NOT NULL,"
            " checkpointed_at timestamptz NOT NULL,"
            " body jsonb NOT NULL,"
            " PRIMARY KEY (thread_id, checkpoint_id))"
        ).format(_TABLE)
    )


class PostgresCheckpointStorage:
    """``failures`` collects the class name of every refused or failed save: the
    framework only logs a failed save, so the host reads this after the leg."""

    # A driver error quotes the failing row (the claim) in its text, and the
    # framework logs the text of what ``save`` raises: with this off the raw
    # error escapes (a probe shows the leak).
    wrap_database_errors = True

    def __init__(self, dsn: str, thread_id: UUID, types: Iterable[type]) -> None:
        self._dsn = dsn
        self._thread_id = thread_id
        self._codec = CheckpointCodec(types)
        self.failures: list[str] = []

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        try:
            document = self._codec.to_document(checkpoint)
            # Refuse now what could not be restored: through JSON text and back.
            self._codec.from_document(json.loads(json.dumps(document)))
            when = datetime.fromisoformat(checkpoint.timestamp)
        except Exception as error:
            self.failures.append(type(error).__name__)
            raise WorkflowCheckpointException(
                f"cannot save a checkpoint: {type(error).__name__}"
            ) from None
        try:
            await self._insert(checkpoint, when, document)
        except psycopg.Error as error:
            self.failures.append(type(error).__name__)
            if not self.wrap_database_errors:
                raise
            raise WorkflowCheckpointException(
                f"cannot save a checkpoint: {type(error).__name__}"
            ) from None
        return checkpoint.checkpoint_id

    async def _insert(
        self, checkpoint: WorkflowCheckpoint, when: datetime, document: Any
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                sql.SQL(
                    "INSERT INTO {} (thread_id, checkpoint_id, workflow_name,"
                    " checkpointed_at, body) VALUES (%s, %s, %s, %s, %s)"
                ).format(_TABLE),
                (
                    self._thread_id,
                    checkpoint.checkpoint_id,
                    checkpoint.workflow_name,
                    when,
                    Jsonb(document),
                ),
            )

    async def _fetch(self, query: sql.Composed, params: tuple[Any, ...]) -> list[Any]:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(query, params)
            return [row[0] for row in await cursor.fetchall()]

    def _read(self, document: Any) -> WorkflowCheckpoint:
        try:
            return self._codec.from_document(document)
        except Exception as error:
            raise WorkflowCheckpointException(
                f"cannot read a checkpoint: {type(error).__name__}"
            ) from error

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        rows = await self._fetch(
            sql.SQL(
                "SELECT body FROM {} WHERE thread_id = %s AND checkpoint_id = %s"
            ).format(_TABLE),
            (self._thread_id, checkpoint_id),
        )
        if not rows:
            raise WorkflowCheckpointException(
                f"No checkpoint found with ID {checkpoint_id}"
            )
        return self._read(rows[0])

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        rows = await self._fetch(
            sql.SQL(
                "SELECT body FROM {} WHERE thread_id = %s AND workflow_name = %s"
                " ORDER BY checkpointed_at, seq"
            ).format(_TABLE),
            (self._thread_id, workflow_name),
        )
        return [self._read(row) for row in rows]

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        return await self._fetch(
            sql.SQL(
                "SELECT checkpoint_id FROM {} WHERE thread_id = %s"
                " AND workflow_name = %s ORDER BY checkpointed_at, seq"
            ).format(_TABLE),
            (self._thread_id, workflow_name),
        )

    async def get_latest(self, *, workflow_name: str) -> WorkflowCheckpoint | None:
        rows = await self._fetch(
            sql.SQL(
                "SELECT body FROM {} WHERE thread_id = %s AND workflow_name = %s"
                " ORDER BY checkpointed_at DESC, seq DESC LIMIT 1"
            ).format(_TABLE),
            (self._thread_id, workflow_name),
        )
        return self._read(rows[0]) if rows else None

    async def delete(self, checkpoint_id: str) -> bool:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                sql.SQL(
                    "DELETE FROM {} WHERE thread_id = %s AND checkpoint_id = %s"
                ).format(_TABLE),
                (self._thread_id, checkpoint_id),
            )
            return cursor.rowcount == 1

    async def delete_run(self) -> int:
        """Not part of the protocol: the host forgets the run's checkpoints."""
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                sql.SQL("DELETE FROM {} WHERE thread_id = %s").format(_TABLE),
                (self._thread_id,),
            )
            return cursor.rowcount
