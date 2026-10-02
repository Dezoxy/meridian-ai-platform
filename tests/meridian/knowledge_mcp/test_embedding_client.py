"""The ingestion's client of the gateway's ``POST /v1/embeddings`` (S012)."""

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from meridian.platform.common.telemetry import (
    configure_propagation,
    make_tracer_provider,
)
from meridian.platform.knowledge_mcp.embedding_client import (
    EmbeddingBatch,
    EmbeddingCallError,
    EmbeddingClient,
)

RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
CANARY = "CANARY-input-text-7783"
TEXTS = ("first text", "second text")


def good_reply(count: int = 2, dimensions: int = 3, **changes: Any) -> dict[str, Any]:
    return {
        "call_id": str(uuid.uuid4()),
        "mode": "replay",
        "deployment": "replay-embedding",
        "provider": "replay",
        "model": "replay-embedding",
        "dimensions": dimensions,
        "embeddings": [[0.5] * dimensions for _ in range(count)],
        "usage": {"input_tokens": 7},
    } | changes


def client_for(
    respond: Callable[[httpx.Request], httpx.Response],
) -> tuple[EmbeddingClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    http = httpx.Client(
        base_url="http://gateway.invalid", transport=httpx.MockTransport(record)
    )
    client = EmbeddingClient(
        http, tenant="claims-triage", agent="knowledge-ingestion", run_id=RUN_ID
    )
    return client, seen


def answering(body: object, status: int = 200, **kwargs: Any) -> EmbeddingClient:
    client, _ = client_for(lambda _: httpx.Response(status, json=body, **kwargs))
    return client


def raising(error: Exception) -> EmbeddingClient:
    def fail(_: httpx.Request) -> httpx.Response:
        raise error

    client, _ = client_for(fail)
    return client


def test_embed_posts_the_texts_with_the_three_identity_headers() -> None:
    client, seen = client_for(lambda _: httpx.Response(200, json=good_reply()))

    client.embed(TEXTS)

    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/v1/embeddings")
    assert request.headers["X-Meridian-Tenant"] == "claims-triage"
    assert request.headers["X-Meridian-Agent"] == "knowledge-ingestion"
    assert request.headers["X-Meridian-Run"] == str(RUN_ID)
    assert json.loads(request.content) == {"inputs": list(TEXTS)}


def test_the_client_exposes_the_tenant_and_the_agent_it_was_given() -> None:
    client, _ = client_for(lambda _: httpx.Response(200, json=good_reply()))

    assert (client.tenant, client.agent) == ("claims-triage", "knowledge-ingestion")
    with pytest.raises(AttributeError):
        client.tenant = "evaluation"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        client.agent = "claims-triage"  # type: ignore[misc]


def test_the_client_keeps_the_run_id_it_was_given() -> None:
    client, _ = client_for(lambda _: httpx.Response(200, json=good_reply()))

    assert client.run_id == RUN_ID


def test_the_current_trace_context_travels_in_the_request() -> None:
    configure_propagation()
    tracer = make_tracer_provider("ingestion", InMemorySpanExporter()).get_tracer("t")
    client, seen = client_for(lambda _: httpx.Response(200, json=good_reply()))

    with tracer.start_as_current_span("ingest") as span:
        client.embed(TEXTS)

    carried = propagate.extract({"traceparent": seen[0].headers["traceparent"]})
    assert trace.get_current_span(carried).get_span_context().trace_id == (
        span.get_span_context().trace_id
    )


def test_a_good_answer_becomes_a_batch() -> None:
    reply = good_reply(embeddings=[[0.25, 0.5, 1], [0, -0.5, 0.125]])

    batch = answering(reply).embed(TEXTS)

    assert batch == EmbeddingBatch(
        deployment="replay-embedding",
        model="replay-embedding",
        dimensions=3,
        vectors=((0.25, 0.5, 1.0), (0.0, -0.5, 0.125)),
        input_tokens=7,
    )
    assert isinstance(batch.vectors, tuple)
    assert all(isinstance(v, tuple) for v in batch.vectors)


def test_a_field_the_client_does_not_know_is_ignored() -> None:
    batch = answering(good_reply(finish_reason="stop", extra={"a": 1})).embed(TEXTS)

    assert batch.dimensions == 3


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(good_reply(count=1), id="fewer-vectors-than-texts"),
        pytest.param(good_reply(count=3), id="more-vectors-than-texts"),
        pytest.param(good_reply(count=0), id="no-vectors"),
        pytest.param(
            good_reply(embeddings=[[0.5] * 3, [0.5] * 2]), id="one-vector-too-short"
        ),
        pytest.param(
            good_reply(embeddings=[[0.5] * 3, [0.5] * 4]), id="one-vector-too-long"
        ),
        pytest.param(
            good_reply() | {"dimensions": 4}, id="dimensions-not-the-vectors-length"
        ),
        pytest.param(good_reply(dimensions=0, embeddings=[[], []]), id="no-dimension"),
        pytest.param(good_reply(embeddings=[["a", "b", "c"]] * 2), id="not-numbers"),
        pytest.param(good_reply(embeddings=[[True, False, True]] * 2), id="booleans"),
        pytest.param(
            good_reply(embeddings=[[0.5] * 3, [0.0] * 3]), id="an-all-zero-vector"
        ),
        pytest.param(
            good_reply(embeddings=[[0.5] * 3, [1e39, 0.5, 0.5]]),
            id="a-component-beyond-float32",
        ),
        pytest.param(
            good_reply(embeddings=[[0.5] * 3, [1e-50] * 3]),
            id="components-that-underflow-to-zero",
        ),
        pytest.param(
            good_reply(embeddings=[[0.5] * 3, [1e-23] * 3]), id="a-norm-that-underflows"
        ),
        pytest.param(
            good_reply(embeddings=[[0.5] * 3, [2e19] * 3]), id="a-norm-that-overflows"
        ),
        pytest.param(good_reply(embeddings="oops"), id="embeddings-not-a-list"),
        pytest.param(good_reply(embeddings=[0.5, 0.5]), id="a-flat-list"),
        pytest.param(good_reply(deployment=""), id="empty-deployment"),
        pytest.param(good_reply(deployment=7), id="deployment-not-text"),
        pytest.param(good_reply(model="m" * 129), id="model-too-long"),
        pytest.param(good_reply(usage={"input_tokens": -1}), id="negative-tokens"),
        pytest.param(good_reply(usage={}), id="no-tokens"),
        pytest.param({"unexpected": "shape"}, id="unknown-shape"),
        pytest.param([1, 2, 3], id="a-list"),
        pytest.param("text", id="a-string"),
        pytest.param(None, id="null"),
    ],
)
def test_an_answer_that_is_not_the_contract_is_refused_with_status_zero(
    reply: object,
) -> None:
    with pytest.raises(EmbeddingCallError) as raised:
        answering(reply).embed(TEXTS)

    assert raised.value.status_code == 0
    assert raised.value.retry_after_seconds is None


def test_an_answer_that_is_not_json_is_refused_with_status_zero() -> None:
    client, _ = client_for(lambda _: httpx.Response(200, content=b"not json at all"))

    with pytest.raises(EmbeddingCallError) as raised:
        client.embed(TEXTS)

    assert raised.value.status_code == 0


def test_a_nan_in_a_vector_is_refused() -> None:
    client, _ = client_for(
        lambda _: httpx.Response(
            200,
            content=json.dumps(good_reply()).replace("0.5", "NaN", 1).encode(),
        )
    )

    with pytest.raises(EmbeddingCallError) as raised:
        client.embed(TEXTS)

    assert raised.value.status_code == 0


@pytest.mark.parametrize("status", [400, 403, 413, 422, 500, 502, 503, 504])
def test_a_refusal_or_a_failure_carries_the_status_and_no_wait(status: int) -> None:
    with pytest.raises(EmbeddingCallError) as raised:
        answering({"detail": "refused"}, status).embed(TEXTS)

    assert raised.value.status_code == status
    assert raised.value.retry_after_seconds is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param("7", 7.0, id="whole-seconds"),
        pytest.param("2.5", 2.5, id="fractional-seconds"),
        pytest.param("0", 0.0, id="zero"),
        pytest.param(None, None, id="missing"),
        pytest.param("-3", None, id="negative"),
        pytest.param("soon", None, id="not-a-number"),
        pytest.param("nan", None, id="nan"),
        pytest.param("inf", None, id="infinite"),
        pytest.param("Wed, 21 Oct 2026 07:28:00 GMT", None, id="a-date"),
        pytest.param("", None, id="empty"),
    ],
)
def test_a_429_carries_its_retry_after_when_it_is_a_non_negative_number(
    header: str | None, expected: float | None
) -> None:
    headers = {} if header is None else {"Retry-After": header}

    with pytest.raises(EmbeddingCallError) as raised:
        answering({}, 429, headers=headers).embed(TEXTS)

    assert raised.value.status_code == 429
    assert raised.value.retry_after_seconds == expected


def test_a_retry_after_on_another_status_is_not_read() -> None:
    with pytest.raises(EmbeddingCallError) as raised:
        answering({}, 503, headers={"Retry-After": "9"}).embed(TEXTS)

    assert raised.value.retry_after_seconds is None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(httpx.ReadTimeout(f"timed out {CANARY}"), id="read-timeout"),
        pytest.param(httpx.ConnectTimeout(f"timed out {CANARY}"), id="connect-timeout"),
        pytest.param(httpx.ConnectError(f"refused {CANARY}"), id="connect-error"),
        pytest.param(httpx.ReadError(f"reset {CANARY}"), id="read-error"),
    ],
)
def test_a_transport_failure_is_status_zero_without_its_message_or_cause(
    error: httpx.HTTPError,
) -> None:
    with pytest.raises(EmbeddingCallError) as raised:
        raising(error).embed(TEXTS)

    assert raised.value.status_code == 0
    assert raised.value.retry_after_seconds is None
    assert CANARY not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_no_input_text_reaches_any_exception_text() -> None:
    texts = (f"{CANARY} one", f"{CANARY} two")
    clients = [
        answering({"detail": f"echo {CANARY}"}, 500),
        answering({"detail": f"echo {CANARY}"}, 429, headers={"Retry-After": "1"}),
        answering(good_reply(count=1, model=CANARY)),
        answering(good_reply(embeddings=[[0.5] * 3, [0.5] * 2], deployment=CANARY)),
        answering({"detail": CANARY}),
        raising(httpx.ReadTimeout(CANARY)),
        raising(httpx.ConnectError(CANARY)),
    ]

    for client in clients:
        with pytest.raises(EmbeddingCallError) as raised:
            client.embed(texts)
        text = f"{raised.value!s} {raised.value!r} {raised.value.args!r}"
        assert CANARY not in text


def test_the_error_message_says_the_status() -> None:
    with pytest.raises(EmbeddingCallError, match="429"):
        answering({}, 429).embed(TEXTS)
    with pytest.raises(EmbeddingCallError, match="no usable answer"):
        answering({"unexpected": "shape"}).embed(TEXTS)
