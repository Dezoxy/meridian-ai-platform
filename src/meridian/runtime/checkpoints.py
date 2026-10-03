"""The PostgreSQL checkpointer of the Agent Runtime (S015).

LangGraph's saver has its own connection, not one from ``common/db.connect``,
for three reasons. It must be opened with ``autocommit=True``: the saver
issues its statements one by one and never commits, so under ``connect``'s
``autocommit=False`` nothing it wrote would persist. It must use
``prepare_threshold=0`` and ``dict_row``, which its queries rely on. And it
finds its tables by unqualified name, so the connection's ``search_path`` is
the ``runtime`` schema, where migration 0008 created them. The timeouts are
the ones every service connection has.

The saver never calls ``setup()``: the tables come from the migration, and the
runtime's role cannot create tables.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from meridian.platform.common.db import CONNECT_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_MS
from meridian.runtime import SERVICE_NAME

# libpq splits ``options`` on spaces, as in ``connect``; this value has none.
SEARCH_PATH = "runtime"


@contextmanager
def open_saver(dsn: str) -> Iterator[PostgresSaver]:
    """Yield a ``PostgresSaver`` on its own connection; close it on exit.

    Raises ``psycopg.Error`` when the connection cannot be opened.
    """
    conn = psycopg.connect(
        dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        application_name=SERVICE_NAME,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=(
            f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c search_path={SEARCH_PATH}"
        ),
    )
    try:
        yield PostgresSaver(conn)
    finally:
        conn.close()
