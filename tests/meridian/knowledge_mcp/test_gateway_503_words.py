"""Which of the gateway's 503s an embedding call met (S073, K6).

The gateway gives a 503 for four reasons and one text each. The client holds the
texts, compares a reply's ``detail`` with them by equality and keeps one fixed
word; the body is the other side's text and is never logged or stored. Nothing
here needs a database or a cluster: the replies are scripted.
"""

import ast
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from knowledgesupport import (
    REAL_SOURCE,
    TENANT,
    RefusalOnlyConnection,
    ScriptedGateway,
    Waits,
)
from servicesupport import REPO_ROOT

from meridian.platform.common.http import AUDIT_UNAVAILABLE, DATABASE_UNAVAILABLE
from meridian.platform.gateway.app import PROVIDER_UNAVAILABLE
from meridian.platform.gateway.refusals import LIMIT_ANSWERS, RATE_STORE_UNAVAILABLE
from meridian.platform.knowledge_mcp import embedding_client, tools
from meridian.platform.knowledge_mcp.embedding_client import (
    GATEWAY_503_WORDS,
    MAX_503_BODY_BYTES,
    UNKNOWN_GATEWAY_WORD,
    EmbeddingCallError,
    EmbeddingClient,
)
from meridian.platform.knowledge_mcp.ingest import IngestError, ingest_wordings
from meridian.platform.registry import Registry
from meridian.platform.toolserver.binding import RunBinding
from meridian.platform.toolserver.handlers import (
    Refused,
    ToolCall,
    ToolFailed,
)

CANARY = "CANARY-gateway-body-4417"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
TEXTS = ("first text",)
# Every text the gateway gives with a 503, and the word each has.
EXPECTED = {
    RATE_STORE_UNAVAILABLE: "rate-store-unavailable",
    DATABASE_UNAVAILABLE: "database-unavailable",
    AUDIT_UNAVAILABLE: "audit-unavailable",
    PROVIDER_UNAVAILABLE: "provider-unavailable",
}
EACH_TEXT = [pytest.param(text, word, id=word) for text, word in EXPECTED.items()]
GATEWAY_DIRECTORIES = ("src/meridian/platform/gateway", "src/meridian/platform/common")


def detail_body(detail: object) -> bytes:
    """The bytes the gateway writes for an error: compact JSON."""
    return json.dumps({"detail": detail}, separators=(",", ":")).encode()


def client_answering(response: httpx.Response) -> EmbeddingClient:
    http = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(lambda _: response),
    )
    return EmbeddingClient(
        http, tenant=TENANT, agent="knowledge-ingestion", run_id=RUN_ID
    )


def error_of(response: httpx.Response) -> EmbeddingCallError:
    with pytest.raises(EmbeddingCallError) as raised:
        client_answering(response).embed(TEXTS)
    return raised.value


def word_of(body: bytes, status: int = 503) -> str | None:
    return error_of(httpx.Response(status, content=body)).gateway_word


# ── 1. the set: the gateway's own texts, held once more and compared ────────
def test_the_words_are_the_closed_set_the_contract_names() -> None:
    assert dict(GATEWAY_503_WORDS) == EXPECTED
    assert UNKNOWN_GATEWAY_WORD == "unknown"
    assert UNKNOWN_GATEWAY_WORD not in EXPECTED.values()


def test_the_rate_store_word_is_the_gateways_own_reason_word() -> None:
    reasons = {
        reason: text
        for reason, (status, text) in LIMIT_ANSWERS.items()
        if status == 503
    }

    assert reasons == {"rate-store-unavailable": RATE_STORE_UNAVAILABLE}
    assert GATEWAY_503_WORDS[RATE_STORE_UNAVAILABLE] == "rate-store-unavailable"


def test_every_503_the_limit_mapping_gives_is_a_text_the_client_names() -> None:
    texts = {text for status, text in LIMIT_ANSWERS.values() if status == 503}

    assert texts <= set(GATEWAY_503_WORDS)


def literal(node: ast.expr, constants: dict[str, str]) -> str | int | None:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def module_constants(tree: ast.Module) -> dict[str, str | int]:
    """Module-level ``NAME = "text"`` and ``NAME = 503`` assignments."""
    found: dict[str, str | int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def texts_answered_with_503(path: Path) -> set[str]:
    """The ``detail`` text of each site in the file that writes a 503: a call
    with ``status_code=503`` and a ``detail=``, ``HTTPException(503, text)``,
    ``error_answer(503, text)`` and ``return 503, text``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = module_constants(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        pair: tuple[ast.expr, ast.expr] | None = None
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            if len(node.value.elts) == 2:
                pair = (node.value.elts[0], node.value.elts[1])
        elif isinstance(node, ast.Call):
            keywords = {k.arg: k.value for k in node.keywords}
            if "status_code" in keywords and "detail" in keywords:
                pair = (keywords["status_code"], keywords["detail"])
            elif len(node.args) >= 2:
                pair = (node.args[0], node.args[1])
        if pair is not None and literal(pair[0], names) == 503:
            text = literal(pair[1], names)
            assert isinstance(text, str), f"{path}:{node.lineno} gives a 503 text"
            found.add(text)
    return found


def test_every_503_text_the_gateways_sources_write_is_a_text_the_client_names() -> None:
    found: set[str] = set()
    for directory in GATEWAY_DIRECTORIES:
        for path in sorted((REPO_ROOT / directory).glob("*.py")):
            found |= texts_answered_with_503(path)

    # Not seen by the scan: a status held in a variable, so the limit mapping's
    # 503s are read from the mapping itself and added here; and a 503 written
    # outside these two directories or built some other way. The certificate's
    # /healthz 503 has no ``detail`` and is not on the embeddings route.
    mapped = {text for status, text in LIMIT_ANSWERS.values() if status == 503}
    assert found | mapped == set(GATEWAY_503_WORDS)


def test_the_scan_sees_a_503_written_in_each_of_the_ways_the_gateway_writes_one(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sites.py"
    source.write_text(
        "HTTP_SERVICE_UNAVAILABLE = 503\n"
        'ONE = "one"\n'
        "def a():\n"
        "    raise HTTPException(status_code=HTTP_SERVICE_UNAVAILABLE, detail=ONE)\n"
        "def b():\n"
        '    return error_answer(503, "two")\n'
        "def c():\n"
        '    return 503, "three"\n'
        "def d():\n"
        '    return error_answer(500, "not this")\n',
        encoding="utf-8",
    )

    assert texts_answered_with_503(source) == {"one", "two", "three"}


# ── 2. the matching ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(("text", "word"), EACH_TEXT)
def test_each_text_the_gateway_gives_with_a_503_has_its_word(
    text: str, word: str
) -> None:
    assert word_of(detail_body(text)) == word


@pytest.mark.parametrize(("text", "word"), EACH_TEXT)
def test_a_known_text_with_other_fields_beside_it_still_has_its_word(
    text: str, word: str
) -> None:
    body = json.dumps({"detail": text, "extra": CANARY}).encode()

    assert word_of(body) == word


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda t: t[:-1], id="one-character-short"),
        pytest.param(lambda t: t + ".", id="one-character-more"),
        pytest.param(lambda t: t + " ", id="trailing-space"),
        pytest.param(lambda t: " " + t, id="leading-space"),
        pytest.param(lambda t: t.capitalize(), id="capital-letter"),
        pytest.param(lambda t: t.upper(), id="upper-case"),
        pytest.param(lambda t: t.replace("is", "was"), id="a-word-changed"),
        pytest.param(lambda t: f"error: {t}", id="the-text-inside-a-longer-one"),
        pytest.param(lambda t: t.replace(" ", "\\u00a0"), id="non-breaking-spaces"),
    ],
)
@pytest.mark.parametrize("text", list(EXPECTED))
def test_a_text_that_is_not_exactly_the_gateways_is_unknown(
    text: str, change: Any
) -> None:
    changed = change(text)
    assert changed != text

    assert word_of(detail_body(changed)) == "unknown"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"<html>503 Service Unavailable</html>", id="html-of-a-proxy"),
        pytest.param(b"not json at all", id="not-json"),
        pytest.param(b"\xff\xfe\x00", id="not-utf-8"),
        pytest.param(b"null", id="null"),
        pytest.param(b'["the rate store is unavailable"]', id="a-list-body"),
        pytest.param(b'"the rate store is unavailable"', id="a-string-body"),
        pytest.param(b"{}", id="no-detail"),
        pytest.param(b'{"detail":null}', id="detail-null"),
        pytest.param(b'{"detail":503}', id="detail-a-number"),
        pytest.param(
            b'{"detail":["the rate store is unavailable"]}', id="detail-a-list"
        ),
        pytest.param(
            b'{"detail":{"the rate store is unavailable":1}}', id="detail-an-object"
        ),
        pytest.param(b'{"status":"certificate-expiring"}', id="the-healthz-body"),
        pytest.param(b'{"detail":"the rate store is unavailable"', id="cut-off"),
        pytest.param(b"[" * 200, id="nested-brackets-inside-the-bound"),
    ],
)
def test_a_503_that_holds_no_known_text_is_unknown_and_does_not_raise_another_error(
    body: bytes,
) -> None:
    error = error_of(httpx.Response(503, content=body))

    assert error.status_code == 503
    assert error.gateway_word == "unknown"


@pytest.mark.parametrize("status", [400, 403, 413, 429, 500, 502, 504])
def test_only_a_503_has_a_word(status: int) -> None:
    body = detail_body(RATE_STORE_UNAVAILABLE)

    assert word_of(body, status) is None


# ── 3. the bound: what is parsed, and what is not ───────────────────────────
@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """The bodies the client's ``json.loads`` was given."""
    seen: list[bytes] = []
    real = json.loads

    def spy(body: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(body)
        return real(body, *args, **kwargs)

    monkeypatch.setattr(embedding_client.json, "loads", spy)
    return seen


def test_a_body_of_a_megabyte_is_unknown_and_is_never_parsed(
    parsed: list[bytes],
) -> None:
    # Nested far past what the parser takes: parsing it would raise
    # RecursionError, which nothing here catches, and the spy would see it.
    body = b"[" * 1_000_000

    error = error_of(httpx.Response(503, content=body))

    assert error.gateway_word == "unknown"
    assert parsed == []


def test_a_megabyte_that_starts_as_a_known_reply_is_unknown_and_not_parsed(
    parsed: list[bytes],
) -> None:
    body = detail_body(DATABASE_UNAVAILABLE) + b" " * 1_000_000

    error = error_of(httpx.Response(503, content=body))

    assert error.gateway_word == "unknown"
    assert parsed == []


def test_a_body_of_exactly_the_bound_is_parsed_and_one_byte_more_is_not(
    parsed: list[bytes],
) -> None:
    exact = detail_body(DATABASE_UNAVAILABLE).ljust(MAX_503_BODY_BYTES)
    over = exact + b" "

    assert len(exact) == MAX_503_BODY_BYTES
    assert word_of(exact) == "database-unavailable"
    assert len(parsed) == 1
    assert word_of(over) == "unknown"
    assert len(parsed) == 1


def test_the_bound_holds_the_longest_text_as_the_gateway_writes_it_with_room() -> None:
    longest = max(len(detail_body(text)) for text in EXPECTED)

    assert longest < MAX_503_BODY_BYTES
    assert MAX_503_BODY_BYTES // longest >= 4
    assert MAX_503_BODY_BYTES <= 1024


# ── 4. the body is the other side's text: it is kept nowhere ────────────────
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(detail_body(f"down {CANARY}"), id="a-text-with-the-canary"),
        pytest.param(detail_body(CANARY), id="the-canary-alone"),
        pytest.param(detail_body([CANARY]), id="a-list-of-the-canary"),
        pytest.param(f"<html>{CANARY}</html>".encode(), id="not-json"),
        pytest.param(
            json.dumps({"detail": RATE_STORE_UNAVAILABLE, "echo": CANARY}).encode(),
            id="a-known-text-with-the-canary-beside-it",
        ),
        pytest.param(detail_body(CANARY).ljust(5000, b" "), id="over-the-bound"),
    ],
)
def test_a_canary_in_a_503_body_is_in_no_log_record_and_no_exception_text(
    body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    error = error_of(httpx.Response(503, content=body))

    seen = f"{error!s} {error!r} {error.args!r} {error.gateway_word}"
    assert CANARY not in seen
    assert error.__cause__ is None
    assert error.__context__ is None
    assert CANARY not in caplog.text
    assert all(CANARY not in str(r.args) + r.getMessage() for r in caplog.records)


def test_the_message_of_the_error_says_the_status_and_not_the_word() -> None:
    error = error_of(httpx.Response(503, content=detail_body(RATE_STORE_UNAVAILABLE)))

    assert str(error) == "model gateway answered 503"


# ── 5. a 503 is not retried, here or in the ingestion ───────────────────────
def test_a_503_is_one_request_and_no_wait_in_the_ingestion(registry: Registry) -> None:
    gateway = ScriptedGateway(
        script=lambda i, inputs: httpx.Response(
            503, content=detail_body(RATE_STORE_UNAVAILABLE)
        )
    )
    waits = Waits()

    with pytest.raises(IngestError):
        ingest_for(gateway, registry, waits)

    assert len(gateway.requests) == 1
    assert waits.seconds == []


def ingest_for(gateway: ScriptedGateway, registry: Registry, waits: Waits) -> None:
    client = EmbeddingClient(
        gateway.http(), tenant=TENANT, agent="knowledge-ingestion", run_id=RUN_ID
    )
    ingest_wordings(RefusalOnlyConnection(), REAL_SOURCE, client, registry, sleep=waits)


# ── 6. where the word shows: the ingestion's ending message ─────────────────
def ingest_ending(registry: Registry, response: httpx.Response) -> IngestError:
    gateway = ScriptedGateway(script=lambda index, inputs: response)
    with pytest.raises(IngestError) as raised:
        ingest_for(gateway, registry, Waits())
    return raised.value


@pytest.mark.parametrize(("text", "word"), EACH_TEXT)
def test_the_ingestions_ending_message_names_the_word_of_a_503(
    registry: Registry, text: str, word: str
) -> None:
    ended = ingest_ending(registry, httpx.Response(503, content=detail_body(text)))

    assert str(ended) == (
        "the model gateway refused the embedding call "
        f"(model gateway answered 503; kind {word})"
    )
    assert ended.reason == "gateway-failed"


def test_the_ingestions_ending_message_says_unknown_for_a_503_it_cannot_tell(
    registry: Registry,
) -> None:
    ended = ingest_ending(registry, httpx.Response(503, content=b"<html></html>"))

    assert str(ended).endswith("(model gateway answered 503; kind unknown)")


@pytest.mark.parametrize("status", [403, 500, 502, 504])
def test_a_refusal_that_is_not_a_503_has_the_message_it_had_and_no_kind(
    registry: Registry, status: int
) -> None:
    body = detail_body(RATE_STORE_UNAVAILABLE)

    ended = ingest_ending(registry, httpx.Response(status, content=body))

    assert str(ended) == (
        "the model gateway refused the embedding call "
        f"(model gateway answered {status})"
    )


def test_a_canary_in_a_503_body_is_not_in_the_ingestions_ending_message(
    registry: Registry,
) -> None:
    ended = ingest_ending(
        registry, httpx.Response(503, content=detail_body(f"down {CANARY}"))
    )

    assert CANARY not in f"{ended!s} {ended!r} {ended.args!r}"
    assert ended.__cause__ is None


# ── 7. where it shows for the search tool: its log, not its answer ──────────
def embed_query_call(response: httpx.Response) -> tuple[ScriptedGateway, Any]:
    gateway = ScriptedGateway(script=lambda index, inputs: response)
    binding = RunBinding(uuid.uuid4(), TENANT, "claims-triage", "CLM-1", "POL-1")
    call = ToolCall(binding, {}, None, "0" * 64)
    return gateway, lambda: tools._embed_query(gateway.http(), call, "a query")


def answer_of(response: httpx.Response) -> str:
    """The word the tool gives an agent for this reply: a refusal's or a
    failure's."""
    gateway, run = embed_query_call(response)
    try:
        result = run()
    except ToolFailed as failed:
        assert str(failed) == failed.reason
        assert len(gateway.requests) == 1
        return failed.reason
    assert isinstance(result, Refused)
    return result.reason


@pytest.mark.parametrize(("text", "word"), EACH_TEXT)
def test_the_tools_answer_to_an_agent_for_each_503_is_what_it_was(
    text: str, word: str
) -> None:
    answer = answer_of(httpx.Response(503, content=detail_body(text)))

    assert answer == "gateway-unavailable"
    assert word not in answer


@pytest.mark.parametrize(
    ("response", "answer"),
    [
        pytest.param(
            httpx.Response(429, content=detail_body(RATE_STORE_UNAVAILABLE)),
            "gateway-busy",
            id="busy",
        ),
        pytest.param(
            httpx.Response(403, content=detail_body(RATE_STORE_UNAVAILABLE)),
            "gateway-refused",
            id="refused",
        ),
        pytest.param(
            httpx.Response(500, content=b"{}"), "gateway-unavailable", id="500"
        ),
    ],
)
def test_the_tools_other_answers_are_what_they_were(
    response: httpx.Response, answer: str
) -> None:
    assert answer_of(response) == answer


@pytest.mark.parametrize(("text", "word"), EACH_TEXT)
def test_the_tools_log_line_names_the_word_of_a_503(
    text: str, word: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    answer_of(httpx.Response(503, content=detail_body(text)))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"embedding call failed: status 503, kind {word}"]


def test_the_tools_log_line_is_what_it_was_for_a_status_that_is_not_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    answer_of(httpx.Response(502, content=detail_body(RATE_STORE_UNAVAILABLE)))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["embedding call failed: status 502"]


def test_a_canary_in_a_503_body_is_in_no_log_record_of_the_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    answer_of(httpx.Response(503, content=detail_body(f"down {CANARY}")))

    assert CANARY not in caplog.text
    assert "kind unknown" in caplog.text
