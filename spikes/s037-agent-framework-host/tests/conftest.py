"""Fixtures of the probes. The PostgreSQL ones read the address of a database of
the caller's own from ``S037_DATABASE_URL`` and fail, not skip, without it: a
probe that silently did not run would read as an answer."""

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg import sql
from s037probe.store import SCHEMA, create_table
from s037probe.support import Kept, StandIn, serve_stand_in

from meridian.platform.common.telemetry import configure_propagation
from meridian.runtime.tool_transport import ToolTransport

DATABASE_URL_ENV = "S037_DATABASE_URL"


@pytest.fixture(autouse=True, scope="session")
def _propagation() -> None:
    """The W3C propagator the runtime's apps set at start; the clients inject
    the trace context with whatever propagator is global."""
    configure_propagation()


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def stand_in() -> StandIn:
    return StandIn()


@pytest.fixture
def stand_in_url(stand_in: StandIn) -> Iterator[str]:
    with serve_stand_in(stand_in) as url:
        yield url


@pytest.fixture
def kept(stand_in_url: str) -> Iterator[Kept]:
    transport = ToolTransport()
    try:
        yield Kept({"policy-mcp": stand_in_url, "claims-mcp": stand_in_url}, transport)
    finally:
        transport.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        pytest.fail(f"{DATABASE_URL_ENV} is not set: see the README, 'How to run'")
    return url


@pytest.fixture
def scratch_schema(database_url: str) -> Iterator[str]:
    """A table of the store in a database of the caller's own, created for one
    test and dropped after it. The schema name is the store's fixed one."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
        )
        create_table(conn)
    yield database_url
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
        )


@pytest.fixture
def thread_id() -> uuid.UUID:
    return uuid.uuid4()
