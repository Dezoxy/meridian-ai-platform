"""The knowledge tool server (S046): ``wording_search`` over the real wordings,
ingested through the in-process replay gateway, with the server running as the
database role ``knowledge_mcp``. The gateway is the real app in replay mode or a
stand-in that records what it was asked and answers as the test says."""

import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Any, cast

import httpx
import psycopg
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
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from retrievalsupport import embed_queries
from servicesupport import (
    REGISTRY_DIR,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    owner_rows,
)
from toolsupport import (
    AGENT,
    CLAIM,
    TENANT,
    World,
    add_claim,
    add_run,
    application_log,
    audit_rows,
    holds,
    knowledge_server,
    knowledge_settings_for,
    run_call,
    seed_world,
    serve,
    text_of,
    tracer_of,
)

from meridian.platform.common.db import connect
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.knowledge_mcp.app import create_app
from meridian.platform.knowledge_mcp.search import hybrid_search
from meridian.platform.knowledge_mcp.store import (
    INSERT_CHUNK,
    ChunkRow,
    vector_literal,
)
from meridian.platform.knowledge_mcp.tools import DEFAULT_TOP_K, handlers
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.binding import RunBinding
from meridian.platform.toolserver.handlers import ToolCall
from meridian.platform.toolserver.validation import build_validator, fits
from meridian.platform.toolserver.wire import META_REFUSAL
from meridian.runtime.tool_client import ToolClient, ToolRefused

# POL-0049, the policy of the seeded claim, is a HOME-PLUS policy.
PRODUCT = "HOME-PLUS"
OTHER_PRODUCT = "HOME-STD"
VERSION = "2026-01"
DEPLOYMENT = "replay-embedding"
DIMENSIONS = 1024
QUERY = "storm damage to the roof of the house"
# A word only a few clauses hold: the rest of the top ten is the vector half's.
RARE_WORD_QUERY = "burglary"
CANARY = "CANARY-query-text-5530"
BODY_CANARY = "CANARY-wording-body-8821"
APPLICATION = "knowledge-mcp"
DELETE_PRODUCT = "DELETE FROM knowledge.chunks WHERE product = %s"


# ── the world ───────────────────────────────────────────────────────────────
@pytest.fixture
def world(fresh_database: DatabaseHandle, gateway: Gateway) -> World:
    """The policies seeded, the four wordings ingested, a claim on POL-0049 and
    a Running run."""
    ingest(fresh_database, gateway.http, gateway.registry)
    return seed_world(fresh_database)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def server(world: World, gateway: Gateway, exporter: InMemorySpanExporter) -> Any:
    """The server over the real replay gateway."""
    return knowledge_server(world, gateway.http, exporter)


def search(
    server: Any,
    world: World,
    query: str = QUERY,
    *,
    product: str = PRODUCT,
    top_k: int | None = None,
    run_id: uuid.UUID | None = None,
) -> Any:
    arguments: dict[str, Any] = {"query": query, "product": product}
    if top_k is not None:
        arguments["top_k"] = top_k
    return run_call(server, "wording_search", arguments, run_id=run_id or world.run_id)


def assert_refused(result: Any, reason: str) -> None:
    assert result.is_error is True
    assert result.meta[META_REFUSAL] == reason
    assert text_of(result) == f"refused: {reason}"
    assert result.structured_content is None


def direct_hits(
    world: World, gateway: Gateway, query: str, top_k: int = DEFAULT_TOP_K
) -> Any:
    """What ``hybrid_search`` answers for the same query, vector and scope."""
    (embedding,) = embed_queries(gateway, [query])
    with connect(world.db.dsn(OWNER), "test-search") as conn:
        return hybrid_search(
            conn,
            product=PRODUCT,
            wording_version=VERSION,
            query=query,
            embedding=embedding,
            top_k=top_k,
        )


def stored_chunks(db: DatabaseHandle, product: str) -> set[tuple[str, str, str, str]]:
    rows = owner_rows(
        db,
        "SELECT clause, section, title, body FROM knowledge.chunks WHERE product = %s",
        (product,),
    )
    return {tuple(row) for row in rows}  # type: ignore[misc]


def as_tuple(chunk: dict[str, Any]) -> tuple[str, str, str, str]:
    return chunk["clause"], chunk["section"], chunk["title"], chunk["body"]


def delete_product(db: DatabaseHandle, product: str = PRODUCT) -> None:
    with connect(db.dsn(OWNER), "test-delete") as conn:
        conn.execute(DELETE_PRODUCT, (product,))
        conn.commit()


def good_reply(index: int, inputs: list[str]) -> httpx.Response:
    """A reply the stored vectors are comparable with."""
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


def server_over(
    world: World,
    scripted: ScriptedGateway,
    exporter: InMemorySpanExporter | None = None,
) -> Any:
    return knowledge_server(world, scripted.http(), exporter)


def server_connections(db: DatabaseHandle) -> list[tuple[str, str | None]]:
    """The role and the state of each connection the knowledge server holds.

    Read as the administrator the fixtures connect with: PostgreSQL shows the
    state of another role's session only to a superuser or a member of
    ``pg_read_all_stats``, and the owner is neither (its rows say ``NULL``)."""
    with connect(db.admin_dsn, "test-activity") as conn:
        rows = conn.execute(
            "SELECT usename, state FROM pg_stat_activity "
            "WHERE datname = %s AND application_name = %s",
            (db.name, APPLICATION),
        ).fetchall()
    return [(usename, state) for usename, state in rows]


# ── 1. the answer ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(("top_k", "count"), [(None, 5), (1, 1), (10, 10)])
def test_a_search_answers_the_policys_product_and_version_and_that_products_chunks(
    world: World, server: Any, top_k: int | None, count: int
) -> None:
    result = search(server, world, top_k=top_k)

    assert result.is_error is False
    answer = result.structured_content
    assert (answer["product"], answer["wording_version"]) == (PRODUCT, VERSION)
    assert len(answer["chunks"]) == count
    own = stored_chunks(world.db, PRODUCT)
    assert len(own) >= count
    assert {as_tuple(chunk) for chunk in answer["chunks"]} <= own


def test_the_answer_fits_the_registrys_output_schema(world: World, server: Any) -> None:
    tool = load_registry(REGISTRY_DIR).tool("wording_search")
    assert tool is not None and tool.output_schema is not None

    result = search(server, world, top_k=10)

    assert fits(build_validator(tool.output_schema), result.structured_content)
    assert text_of(result) == json.dumps(
        result.structured_content, separators=(",", ":"), ensure_ascii=False
    )


# ── 2 and 3. the same answer as the search itself ───────────────────────────
@pytest.mark.parametrize("top_k", [1, 5, 10])
def test_the_clauses_and_their_order_equal_the_search_called_directly(
    world: World, gateway: Gateway, server: Any, top_k: int
) -> None:
    hits = direct_hits(world, gateway, QUERY, top_k)

    result = search(server, world, top_k=top_k)

    chunks = result.structured_content["chunks"]
    assert [chunk["clause"] for chunk in chunks] == [hit.clause for hit in hits]
    assert chunks == [
        {
            "clause": hit.clause,
            "section": hit.section,
            "title": hit.title,
            "body": hit.body,
            "keyword_match": hit.lexical_rank is not None,
        }
        for hit in hits
    ]


def test_keyword_match_is_true_for_a_shared_word_and_false_for_a_vector_only_chunk(
    world: World, gateway: Gateway, server: Any
) -> None:
    hits = direct_hits(world, gateway, RARE_WORD_QUERY, 10)
    assert {hit.lexical_rank is None for hit in hits} == {True, False}, (
        "the query must list chunks of both halves for this test to mean anything"
    )

    result = search(server, world, RARE_WORD_QUERY, top_k=10)

    matched = {
        c["clause"]: c["keyword_match"] for c in result.structured_content["chunks"]
    }
    assert matched == {hit.clause: hit.lexical_rank is not None for hit in hits}
    assert True in matched.values()
    assert False in matched.values()


# ── 4. the wording version is the policy's ──────────────────────────────────
def test_chunks_of_the_same_product_under_another_version_are_never_returned(
    world: World, gateway: Gateway, server: Any
) -> None:
    word = "zyxquorbble"
    (as_query,) = embed_queries(gateway, [word])
    text = f"{word} {word} storm damage to the roof"
    planted = [
        ChunkRow(
            product=PRODUCT,
            wording_version="2025-01",
            clause=f"9.{number}",
            section="An older wording",
            title=f"Old {word}",
            body=text,
            source_sha256="0" * 64,
            deployment=DEPLOYMENT,
            model=DEPLOYMENT,
            dimensions=DIMENSIONS,
            # The very vector of the query: the vector half would list them
            # first if the scope let them in.
            embedding=as_query.vector,
        )
        for number in (1, 2, 3)
    ]
    with connect(world.db.dsn(OWNER), "test-plant") as conn:
        conn.cursor().executemany(
            INSERT_CHUNK,
            [asdict(r) | {"embedding": vector_literal(r.embedding)} for r in planted],
        )
        conn.commit()
    keyword_only = hybrid_search_scope_count(world.db, word)
    assert keyword_only == 3, "the planted word must be in the planted rows"

    for query in (word, f"{word} storm damage"):
        result = search(server, world, query, top_k=10)

        assert result.is_error is False
        answer = result.structured_content
        assert answer["wording_version"] == VERSION
        for chunk in answer["chunks"]:
            assert word not in chunk["title"] + chunk["body"]
            assert chunk["section"] != "An older wording"


def hybrid_search_scope_count(db: DatabaseHandle, word: str) -> int:
    ((count,),) = owner_rows(
        db,
        "SELECT count(*) FROM knowledge.chunks "
        "WHERE product = %s AND body LIKE %s AND wording_version <> %s",
        (PRODUCT, f"%{word}%", VERSION),
    )
    return count


# ── 5. refusals that call nothing ───────────────────────────────────────────
def test_another_product_is_outside_the_claim_and_the_gateway_is_not_called(
    world: World,
) -> None:
    scripted = ScriptedGateway()
    server = server_over(world, scripted)

    result = search(server, world, product=OTHER_PRODUCT)

    assert_refused(result, "outside-claim")
    assert scripted.requests == []


def test_a_policy_without_a_row_is_refused_and_the_gateway_is_not_called(
    world: World,
) -> None:
    add_claim(world.db, "CLM-0004", policy_number="POL-9999")
    run_id = add_run(world.db, "CLM-0004")
    scripted = ScriptedGateway()
    server = server_over(world, scripted)

    result = search(server, world, run_id=run_id)

    assert_refused(result, "policy-not-found")
    assert scripted.requests == []


@pytest.mark.parametrize("query", [" ", "   ", "\t\n", "  "])  # noqa: RUF001
def test_a_query_of_only_whitespace_is_refused_and_the_gateway_is_not_called(
    world: World, query: str
) -> None:
    scripted = ScriptedGateway()
    server = server_over(world, scripted)

    result = search(server, world, query)

    assert_refused(result, "invalid-arguments")
    assert scripted.requests == []


def test_a_query_with_spaces_around_words_is_sent_as_it_came(world: World) -> None:
    scripted = ScriptedGateway(script=good_reply)
    server = server_over(world, scripted)

    result = search(server, world, "  storm  ")

    assert result.is_error is False
    assert scripted.requests == [["  storm  "]]


def test_a_binding_without_a_policy_scope_is_a_bug_that_raises_before_any_call() -> (
    None
):
    scripted = ScriptedGateway()
    (handler,) = handlers(scripted.http())
    binding = RunBinding(uuid.uuid4(), TENANT, AGENT, CLAIM, "POL-0049")
    call = ToolCall(binding, {"query": QUERY, "product": PRODUCT}, None, "0" * 64)

    with pytest.raises(RuntimeError):
        handler.run(cast(psycopg.Connection, None), call)

    assert scripted.requests == []


def test_the_handler_is_the_registrys_tool_bound_to_the_product() -> None:
    (handler,) = handlers(ScriptedGateway().http())

    assert (handler.tool, handler.scope) == ("wording_search", "knowledge:search")
    assert (handler.bound_argument, handler.bound_to) == ("product", "product")


# ── 6. the gateway call ─────────────────────────────────────────────────────
def test_the_gateway_call_carries_the_run_rows_identity_and_only_the_query(
    world: World, exporter: InMemorySpanExporter
) -> None:
    scripted = ScriptedGateway(script=good_reply)
    server = server_over(world, scripted, exporter)

    result = search(server, world)

    assert result.is_error is False
    assert scripted.requests == [[QUERY]]
    (headers,) = scripted.headers
    assert headers["X-Meridian-Tenant"] == world.tenant
    assert headers["X-Meridian-Agent"] == world.agent
    assert headers["X-Meridian-Run"] == str(world.run_id)


def test_the_gateway_call_carries_the_trace_of_the_tool_calls_span(
    world: World, exporter: InMemorySpanExporter
) -> None:
    scripted = ScriptedGateway(script=good_reply)
    server = server_over(world, scripted, exporter)

    search(server, world)

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    _, trace_id, span_id, _ = scripted.headers[0]["traceparent"].split("-")
    assert trace_id == format(span.context.trace_id, "032x")
    assert span_id == format(span.context.span_id, "016x")


def test_the_gateways_own_audit_row_names_the_runs_tenant_agent_and_run(
    world: World, server: Any
) -> None:
    result = search(server, world)

    assert result.is_error is False
    (row,) = [
        row
        for row in audit_events(world.db, world.run_id)
        if row["service"] == "model-gateway"
    ]
    assert (row["event"], row["outcome"]) == ("model.call", "completed")
    assert (row["tenant"], row["agent"], row["run_id"]) == (
        TENANT,
        AGENT,
        world.run_id,
    )


# ── 7. what the gateway can answer ──────────────────────────────────────────
def a_reply_of_the_wrong_length() -> httpx.Response:
    # One vector of 3 components under a ``dimensions`` that says 4.
    return httpx.Response(200, json={**embedding_reply(1), "dimensions": 4})


GATEWAY_ANSWERS = {
    "429": (answering(too_many("1")), "gateway-busy"),
    "429 without a wait": (answering(too_many()), "gateway-busy"),
    "403": (
        answering(httpx.Response(403, json={"detail": "refused"})),
        "gateway-refused",
    ),
    "500": (answering(httpx.Response(500, json={})), "gateway-unavailable"),
    "502": (answering(httpx.Response(502, text="bad gateway")), "gateway-unavailable"),
    "transport error": (raising(httpx.ConnectError("no route")), "gateway-unavailable"),
    "timeout": (raising(httpx.ReadTimeout("too slow")), "gateway-unavailable"),
    "not JSON": (
        answering(httpx.Response(200, content=b"<html>not json</html>")),
        "gateway-unavailable",
    ),
    "wrong length": (answering(a_reply_of_the_wrong_length()), "gateway-unavailable"),
    "two vectors": (
        answering(httpx.Response(200, json=embedding_reply(2))),
        "gateway-unavailable",
    ),
}


@pytest.mark.parametrize("answer", GATEWAY_ANSWERS)
def test_each_gateway_answer_has_its_refusal_one_request_and_one_audit_row(
    world: World, answer: str
) -> None:
    script, reason = GATEWAY_ANSWERS[answer]
    scripted = ScriptedGateway(script=script)
    server = server_over(world, scripted)

    result = search(server, world)

    assert_refused(result, reason)
    assert len(scripted.requests) == 1  # no retry
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"], row["tool"]) == (
        "refused",
        reason,
        "wording_search",
    )
    assert (row["tenant"], row["agent"], row["run_id"]) == (
        world.tenant,
        world.agent,
        world.run_id,
    )


# ── 8. the store ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("emptied", ["the product", "the whole store"])
def test_a_store_without_the_products_rows_is_no_corpus(
    world: World, emptied: str
) -> None:
    if emptied == "the product":
        delete_product(world.db)
    else:
        with connect(world.db.dsn(OWNER), "test-delete") as conn:
            conn.execute("DELETE FROM knowledge.chunks")
            conn.commit()
    server = server_over(world, ScriptedGateway(script=good_reply))

    result = search(server, world)

    assert_refused(result, "no-corpus")
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "no-corpus")


STALE_ANSWERS = {
    "another deployment": embedding_reply(
        1, deployment="other-embedding", dimensions=DIMENSIONS
    ),
    "another length": embedding_reply(1, deployment=DEPLOYMENT, dimensions=3),
}


@pytest.mark.parametrize("answer", STALE_ANSWERS)
def test_rows_of_another_deployment_or_length_than_the_query_are_stale_vectors(
    world: World, answer: str
) -> None:
    scripted = ScriptedGateway(
        script=answering(httpx.Response(200, json=STALE_ANSWERS[answer]))
    )
    server = server_over(world, scripted)

    result = search(server, world)

    assert_refused(result, "stale-vectors")
    assert len(scripted.requests) == 1
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "stale-vectors")


# ── 9. no transaction while the server waits ────────────────────────────────
def test_no_transaction_is_open_while_the_server_waits_for_the_gateway(
    world: World,
) -> None:
    seen: list[list[tuple[str, str | None]]] = []

    def look(index: int, inputs: list[str]) -> httpx.Response:
        seen.append(server_connections(world.db))
        return good_reply(index, inputs)

    server = server_over(world, ScriptedGateway(script=look))

    result = search(server, world)

    assert result.is_error is False
    # Found, and as the role the migration grants: not vacuously empty.
    assert seen == [[("knowledge_mcp", "idle")]]


# ── 10. the audit row ───────────────────────────────────────────────────────
def test_a_completed_search_writes_one_completed_audit_row(
    world: World, server: Any
) -> None:
    result = search(server, world)

    assert result.is_error is False
    (row,) = audit_rows(world.db)
    assert (row["service"], row["event"], row["outcome"]) == (
        APPLICATION,
        "tool.call",
        "completed",
    )
    assert row["tool"] == "wording_search"
    assert (row["tenant"], row["agent"], row["run_id"], row["reference"]) == (
        world.tenant,
        world.agent,
        world.run_id,
        world.claim_id,
    )
    assert row["reason"] is None


# ── 11. nothing of a query or a chunk is kept ───────────────────────────────
def plant_body_canary(db: DatabaseHandle) -> None:
    with connect(db.dsn(OWNER), "test-plant") as conn:
        conn.execute(
            "UPDATE knowledge.chunks SET body = body || ' ' || %s", (BODY_CANARY,)
        )
        conn.commit()


@pytest.mark.parametrize("outcome", ["completed", "gateway-busy", "no-corpus"])
@pytest.mark.parametrize("planted", ["query", "body"])
def test_a_canary_in_a_query_or_a_chunk_is_in_no_audit_row_span_or_log_record(
    world: World,
    gateway: Gateway,
    exporter: InMemorySpanExporter,
    planted: str,
    outcome: str,
) -> None:
    canary = CANARY if planted == "query" else BODY_CANARY
    query = f"{CANARY} {QUERY}" if planted == "query" else QUERY
    if planted == "body":
        plant_body_canary(world.db)
    if outcome == "no-corpus":
        delete_product(world.db)
    http = gateway.http
    if outcome == "gateway-busy":
        http = ScriptedGateway(script=lambda index, inputs: too_many()).http()
    server = knowledge_server(world, http, exporter)

    with application_log() as records:
        result = search(server, world, query)

    if outcome == "completed":
        assert result.is_error is False
        if planted == "body":  # the answer holds it; nothing else may
            assert canary in text_of(result)
    else:
        assert_refused(result, outcome)
    trail = owner_rows(world.db, "SELECT row_to_json(e)::text FROM audit.events e")
    assert trail, "no audit row, so nothing was checked"
    assert canary not in repr(trail)
    assert_spans_hold_no_exception_and_no_canary(exporter, canary)
    assert records, "no log record, so nothing was checked"
    assert not holds(records, canary)


# ── 12. through the runtime's tool client ───────────────────────────────────
@dataclass(frozen=True)
class Live:
    base: str
    tools: ToolClient
    world: World


@pytest.fixture
def live(
    world: World, gateway: Gateway, exporter: InMemorySpanExporter
) -> Iterator[Live]:
    settings = knowledge_settings_for(world.db, hosts=("127.0.0.1:*",))
    app = create_app(
        settings,
        http=gateway.http,
        tracer_provider=make_tracer_provider(APPLICATION, exporter),
    )
    with serve(app.app) as base:
        tools = ToolClient(
            {APPLICATION: base},
            registry=load_registry(REGISTRY_DIR),
            agent=world.agent,
            run_id=world.run_id,
            tracer=tracer_of(exporter),
            on_refusal=lambda tool: None,
        )
        yield Live(base, tools, world)


def test_the_runtimes_tool_client_gets_a_result_that_fits_the_schema(
    live: Live,
) -> None:
    tool = load_registry(REGISTRY_DIR).tool("wording_search")
    assert tool is not None and tool.output_schema is not None

    result = live.tools.call("wording_search", {"query": QUERY, "product": PRODUCT})

    assert fits(build_validator(tool.output_schema), result.data)
    assert result.data["product"] == PRODUCT
    assert len(result.data["chunks"]) == DEFAULT_TOP_K
    assert result.replayed is False
    assert result.call_id is not None


def test_a_no_corpus_refusal_reaches_the_runtimes_tool_client_as_tool_refused(
    live: Live,
) -> None:
    delete_product(live.world.db)

    with pytest.raises(ToolRefused) as raised:
        live.tools.call("wording_search", {"query": QUERY, "product": PRODUCT})

    assert raised.value.reason == "no-corpus"
    assert raised.value.tool == "wording_search"


def test_a_search_for_another_product_reaches_the_tool_client_as_outside_claim(
    live: Live,
) -> None:
    with pytest.raises(ToolRefused) as raised:
        live.tools.call("wording_search", {"query": QUERY, "product": OTHER_PRODUCT})

    assert raised.value.reason == "outside-claim"
