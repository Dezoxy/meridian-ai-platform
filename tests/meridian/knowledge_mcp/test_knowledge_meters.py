"""The knowledge server's series (S064): the empty store, the stale vectors and a
gateway that does not answer, each counted by the kit's one counter of calls.

None of them has code of its own: the server's reasons are series because they
pass the place where every call of the kit ends. Real PostgreSQL
(``make pytest-db``), the store ingested through the replay gateway, and a
stand-in for the gateway the server embeds a query through.
"""

from contextlib import suppress
from typing import Any

import httpx
import pytest
from dbsupport import OWNER, DatabaseHandle
from knowledgesupport import (
    Gateway,
    Script,
    ScriptedGateway,
    embedding_reply,
    ingest,
    too_many,
)
from mcp.shared.exceptions import MCPError
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from servicesupport import FakeClock, metric_points
from toolsupport import (
    AGENT,
    TENANT,
    World,
    knowledge_settings_for,
    run_call,
    seed_world,
)

from meridian.platform.common.db import connect
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.knowledge_mcp.app import create_app
from meridian.platform.toolserver.meters import CALLS
from meridian.platform.toolserver.wire import META_TIMEOUT_MS

PRODUCT = "HOME-PLUS"
DEPLOYMENT = "replay-embedding"
DIMENSIONS = 1024
QUERY = "storm damage to the roof of the house"
CANARY = "CANARY-query-text-4417"
LATE_SECONDS = 100.0  # past any budget a call is given
TOOL = "wording_search"
RUN_LABELS = {"meridian.tenant": TENANT, "meridian.agent": AGENT}


@pytest.fixture
def world(fresh_database: DatabaseHandle, gateway: Gateway) -> World:
    """The four wordings ingested, a claim on a HOME-PLUS policy, a run."""
    ingest(fresh_database, gateway.http, gateway.registry)
    return seed_world(fresh_database)


def good_reply(index: int, inputs: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json=embedding_reply(len(inputs), deployment=DEPLOYMENT, dimensions=DIMENSIONS),
    )


def answering(response: httpx.Response) -> Script:
    return lambda index, inputs: response


def raising(error: Exception) -> Script:
    def script(index: int, inputs: list[str]) -> httpx.Response:
        raise error

    return script


def search(world: World, script: Script, query: str = QUERY) -> InMemoryMetricReader:
    """One search over a gateway that answers as ``script`` says; the reader of
    the server's meter provider. A call that fails raises, and is ignored."""
    reader = InMemoryMetricReader()
    server = create_app(
        knowledge_settings_for(world.db),
        http=ScriptedGateway(script=script).http(),
        meter_provider=make_meter_provider("knowledge-mcp", reader),
    ).server
    with suppress(MCPError):
        run_call(
            server, TOOL, {"query": query, "product": PRODUCT}, run_id=world.run_id
        )
    return reader


def counted(reader: InMemoryMetricReader) -> list[tuple[dict[str, Any], float]]:
    return metric_points(reader, CALLS)


def one_series(
    outcome: str, reason: str | None = None
) -> list[tuple[dict[str, Any], float]]:
    labels = {"meridian.tool": TOOL, "meridian.outcome": outcome} | RUN_LABELS
    if reason is not None:
        labels["meridian.reason"] = reason
    return [(labels, 1)]


def test_a_search_that_answers_is_one_completed_count(world: World) -> None:
    assert counted(search(world, good_reply)) == one_series("completed")


def test_a_store_without_the_products_rows_is_a_no_corpus_series(
    world: World,
) -> None:
    with connect(world.db.dsn(OWNER), "test-delete") as conn:
        conn.execute("DELETE FROM knowledge.chunks WHERE product = %s", (PRODUCT,))
        conn.commit()

    reader = search(world, good_reply)

    assert counted(reader) == one_series("refused", "no-corpus")


def test_a_store_emptied_while_the_server_waits_is_the_same_series(
    world: World,
) -> None:
    def empty_then_answer(index: int, inputs: list[str]) -> httpx.Response:
        with connect(world.db.dsn(OWNER), "test-delete") as conn:
            conn.execute("DELETE FROM knowledge.chunks WHERE product = %s", (PRODUCT,))
            conn.commit()
        return good_reply(index, inputs)

    reader = search(world, empty_then_answer)

    assert counted(reader) == one_series("refused", "no-corpus")


@pytest.mark.parametrize(
    "reply",
    [
        embedding_reply(1, deployment="other-embedding", dimensions=DIMENSIONS),
        embedding_reply(1, deployment=DEPLOYMENT, dimensions=3),
    ],
    ids=["another deployment", "another length"],
)
def test_vectors_of_another_deployment_or_length_are_a_stale_vectors_series(
    world: World, reply: dict[str, Any]
) -> None:
    reader = search(world, answering(httpx.Response(200, json=reply)))

    assert counted(reader) == one_series("refused", "stale-vectors")


GATEWAY_THAT_DOES_NOT_ANSWER = {
    "transport error": raising(httpx.ConnectError("no route")),
    "timeout": raising(httpx.ReadTimeout("too slow")),
    "500": answering(httpx.Response(500, json={})),
    "not JSON": answering(httpx.Response(200, content=b"<html>not json</html>")),
}


@pytest.mark.parametrize("how", GATEWAY_THAT_DOES_NOT_ANSWER)
def test_a_gateway_that_gives_no_vector_is_a_gateway_unavailable_series(
    world: World, how: str
) -> None:
    reader = search(world, GATEWAY_THAT_DOES_NOT_ANSWER[how])

    assert counted(reader) == one_series("failed", "gateway-unavailable")


def test_a_busy_gateway_is_a_gateway_busy_series(world: World) -> None:
    reader = search(world, answering(too_many("1")))

    assert counted(reader) == one_series("refused", "gateway-busy")


def test_a_gateway_that_says_no_is_a_gateway_refused_series(world: World) -> None:
    reader = search(world, answering(httpx.Response(403, json={"detail": "no"})))

    assert counted(reader) == one_series("refused", "gateway-refused")


def test_a_wait_cut_by_the_calls_own_deadline_is_a_timed_out_series(
    world: World,
) -> None:
    clock = FakeClock()

    def times_out_when_the_call_has_no_time_left(
        index: int, inputs: list[str]
    ) -> httpx.Response:
        clock.advance(LATE_SECONDS)
        raise httpx.ReadTimeout("too slow")

    reader = InMemoryMetricReader()
    server = create_app(
        knowledge_settings_for(world.db),
        http=ScriptedGateway(script=times_out_when_the_call_has_no_time_left).http(),
        meter_provider=make_meter_provider("knowledge-mcp", reader),
        clock=clock,
    ).server

    with pytest.raises(MCPError):
        run_call(
            server,
            TOOL,
            {"query": QUERY, "product": PRODUCT},
            run_id=world.run_id,
            meta={META_TIMEOUT_MS: 250},
        )

    assert counted(reader) == one_series("failed", "timed-out")


def test_the_text_of_a_query_is_in_no_label(world: World) -> None:
    reader = search(world, raising(httpx.ConnectError(CANARY)), query=CANARY)

    assert CANARY not in repr(counted(reader))
