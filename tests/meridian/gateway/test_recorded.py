"""The gateway answers chat from a recording (S050, T-76).

The key, the file, the two providers, and the app in recorded mode on the
throwaway PostgreSQL. Nothing here reaches a network.
"""

import contextlib
import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR, audit_events, owner_rows

from meridian.platform.common.env import SettingsError
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.evaluation.report import MAX_REPORT_BYTES
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway.app import NOT_RECORDED, create_app
from meridian.platform.gateway.budget import cost_micro_eur
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest, Message
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.providers.recorded import (
    RecordedAnswer,
    RecordedProvider,
    Recording,
    RecordingError,
    RecordingProvider,
    dump_recording,
    load_recording,
    request_key,
    write_recording,
)
from meridian.platform.gateway.settings import RECORDINGS_ENV, GatewaySettings
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.models import Deployment

UNUSED_DSN = "postgresql://model_gateway@db.invalid/meridian"
CLAIM_TEXT = "claimant-secret-text-42"
CANARY = "CANARY-recording-content-7741"
PROVIDER_MODEL = "gpt-4o-2024-11-20"
ANSWER_TEXT = "a recorded answer"
INPUT_TOKENS = 11
OUTPUT_TOKENS = 7
BODY = {"messages": [{"role": "user", "content": CLAIM_TEXT}]}
OTHER_BODY = {"messages": [{"role": "user", "content": "a prompt nobody recorded"}]}
REQUEST = ChatRequest.model_validate(BODY)
HEX_A = "a" * 64
HEX_B = "b" * 64
RECORDED_DEPLOYMENT = "recorded-chat"
NOT_RECORDED_DETAIL = (
    "no recording answers this request; record again (make eval-record)"
)


def headers(run_id: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "X-Meridian-Tenant": "claims-triage",
        "X-Meridian-Agent": "claims-triage",
        "X-Meridian-Run": str(run_id or uuid.uuid4()),
    }


def answer(**overrides: object) -> RecordedAnswer:
    values: dict[str, object] = {
        "text": ANSWER_TEXT,
        "finish_reason": "stop",
        "model": PROVIDER_MODEL,
        "input_tokens": INPUT_TOKENS,
        "output_tokens": OUTPUT_TOKENS,
        "latency_ms": 1234,
    } | overrides
    return RecordedAnswer.model_validate(values)


def recording_of(entries: dict[str, RecordedAnswer]) -> Recording:
    return Recording(format=1, recorded_for={"prompt-v1": HEX_A}, entries=entries)


def document(**overrides: object) -> dict:
    """A valid recording as the JSON object a file holds."""
    return {
        "format": 1,
        "recorded_for": {"prompt-v1": HEX_A},
        "entries": {HEX_A: answer().model_dump(mode="json")},
    } | overrides


def write_document(path: Path, content: object) -> Path:
    path.write_text(
        content if isinstance(content, str) else json.dumps(content), encoding="utf-8"
    )
    return path


def deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment(RECORDED_DEPLOYMENT)
    assert found is not None
    return found


def ask(provider: RecordedProvider, request: ChatRequest = REQUEST) -> ProviderReply:
    return provider.chat(deployment(), request, timeout_seconds=5.0)


# ── the key ─────────────────────────────────────────────────────────────────
def test_the_key_is_the_sha256_of_the_canonical_json_of_the_request() -> None:
    canonical = (
        '{"max_output_tokens":1024,"messages":[{"content":"héllo","role":"user"}]}'
    )
    request = ChatRequest(messages=(Message(role="user", content="héllo"),))

    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert request_key(request) == expected
    assert len(expected) == 64


def test_equal_requests_have_equal_keys() -> None:
    assert request_key(REQUEST) == request_key(ChatRequest.model_validate(BODY))


def request_with(content: str, role: str = "user", tokens: int = 1024) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "messages": [{"role": role, "content": content}],
            "max_output_tokens": tokens,
        }
    )


@pytest.mark.parametrize(
    "changed",
    [
        request_with("claimant-secret-text-43"),
        request_with(CLAIM_TEXT, role="assistant"),
        request_with(CLAIM_TEXT, tokens=512),
    ],
    ids=["one character", "role", "max_output_tokens"],
)
def test_one_changed_thing_changes_the_key(changed: ChatRequest) -> None:
    assert request_key(changed) != request_key(request_with(CLAIM_TEXT))


class RequestWithOptionalField(ChatRequest):
    note: str | None = None


def test_a_field_that_is_none_does_not_change_the_key() -> None:
    plain = request_key(request_with(CLAIM_TEXT))
    base = request_with(CLAIM_TEXT).model_dump()

    assert request_key(RequestWithOptionalField(**base)) == plain
    assert request_key(RequestWithOptionalField(**base, note="x")) != plain


# ── the answer and the file ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": 1},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"latency_ms": -1},
        {"input_tokens": 1.0},
        {"input_tokens": True},
        {"input_tokens": "3"},
        {"finish_reason": "content_filter"},
    ],
)
def test_an_answer_refuses_an_extra_field_and_a_count_that_is_not_a_whole_number(
    overrides: dict,
) -> None:
    with pytest.raises(ValidationError):
        answer(**overrides)


def test_a_valid_recording_loads(tmp_path: Path) -> None:
    path = write_document(tmp_path / "r.json", document())

    loaded = load_recording(path)

    assert loaded.format == 1
    assert loaded.recorded_for == {"prompt-v1": HEX_A}
    assert loaded.entries == {HEX_A: answer()}


def with_entry(**overrides: object) -> dict:
    entry = answer().model_dump(mode="json") | overrides
    return document(entries={HEX_A: entry})


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(with_entry(**{"CANARY-extra-7741": CANARY}), id="extra in entry"),
        pytest.param(document(**{"CANARY-extra-7741": CANARY}), id="extra at top"),
        pytest.param(with_entry(text=CANARY, input_tokens=-1), id="negative count"),
        pytest.param(
            with_entry(text=CANARY, latency_ms=1.5), id="latency not an integer"
        ),
        pytest.param(
            document(entries={CANARY: answer().model_dump(mode="json")}),
            id="key not 64 hex",
        ),
        pytest.param(
            document(entries={"A" * 64: answer().model_dump(mode="json")}),
            id="key in capitals",
        ),
        pytest.param(
            document(recorded_for={"prompt-v1": CANARY}), id="label hash not hex"
        ),
        pytest.param(
            document(recorded_for={CANARY: HEX_A}), id="label out of the pattern"
        ),
        pytest.param(document(format=2), id="format 2"),
        pytest.param(document(format=True), id="format true"),
        pytest.param(document(format=1.0), id="format a float"),
        pytest.param(
            '{"format": 1, "format": 1, "recorded_for": {}, "entries": {}}'
            f' "{CANARY}"',
            id="not json",
        ),
        pytest.param(
            '{"format": 1, "recorded_for": {}, "entries": {},'
            f' "entries": {{"{CANARY}": 1}}}}',
            id="duplicate key",
        ),
        pytest.param([CANARY], id="not an object"),
    ],
)
def test_a_defective_file_is_refused_without_quoting_it(
    tmp_path: Path, content: object
) -> None:
    path = write_document(tmp_path / "r.json", content)

    with pytest.raises(RecordingError) as raised:
        load_recording(path)

    assert isinstance(raised.value, ValueError)
    assert CANARY not in str(raised.value)
    assert "7741" not in str(raised.value)


def test_a_label_is_at_most_64_characters(tmp_path: Path) -> None:
    ok = write_document(tmp_path / "ok.json", document(recorded_for={"a" * 64: HEX_A}))
    too_long = write_document(
        tmp_path / "long.json", document(recorded_for={"a" * 65: HEX_A})
    )

    assert load_recording(ok).recorded_for == {"a" * 64: HEX_A}
    with pytest.raises(RecordingError):
        load_recording(too_long)


def test_a_file_over_the_size_limit_is_refused(tmp_path: Path) -> None:
    path = write_document(tmp_path / "big.json", " " * (MAX_REPORT_BYTES + 1))

    with pytest.raises(RecordingError, match="too large"):
        load_recording(path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RecordingError, match="not found"):
        load_recording(tmp_path / "absent.json")


def test_dump_then_load_is_byte_stable(tmp_path: Path) -> None:
    first = recording_of({HEX_B: answer(text="é b"), HEX_A: answer()})
    path = tmp_path / "r.json"

    write_recording(first, path)
    loaded = load_recording(path)

    text = path.read_text(encoding="utf-8")
    assert text == dump_recording(first)
    assert dump_recording(loaded) == text
    assert text.endswith("}\n")
    assert "é b" in text  # not escaped
    assert text.index(HEX_A) < text.index(HEX_B)  # sorted keys
    assert text.startswith('{\n  "entries": {')  # indent 2, sorted keys
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]  # no temporary file


def test_write_replaces_a_file_and_a_failed_write_leaves_it(tmp_path: Path) -> None:
    path = write_document(tmp_path / "r.json", "old")

    write_recording(recording_of({HEX_A: answer()}), path)
    assert load_recording(path).entries == {HEX_A: answer()}

    with pytest.raises(OSError):
        write_recording(recording_of({}), tmp_path / "no-such-dir" / "r.json")
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]


# ── RecordedProvider ────────────────────────────────────────────────────────
def test_a_recorded_request_gets_the_entrys_reply() -> None:
    provider = RecordedProvider(recording_of({request_key(REQUEST): answer()}))

    reply = ask(provider)

    assert reply == ProviderReply(
        text=ANSWER_TEXT,
        finish_reason="stop",
        model=PROVIDER_MODEL,
        input_tokens=INPUT_TOKENS,
        output_tokens=OUTPUT_TOKENS,
    )


def test_an_unrecorded_request_is_refused_before_anything_is_sent() -> None:
    provider = RecordedProvider(recording_of({request_key(REQUEST): answer()}))

    with pytest.raises(ProviderError) as raised:
        ask(provider, request_with("another prompt"))

    assert (raised.value.kind, raised.value.sent) == ("not-recorded", False)
    assert "another prompt" not in str(raised.value)


def test_the_misses_are_remembered_in_order_and_the_unused_entries_sorted() -> None:
    hit, spare = request_key(REQUEST), HEX_A
    provider = RecordedProvider(recording_of({hit: answer(), spare: answer()}))
    first, second = request_with("first"), request_with("second")

    assert provider.missed == ()
    assert provider.unused() == tuple(sorted((hit, spare)))
    for request in (first, REQUEST, second, first):
        with contextlib.suppress(ProviderError):
            ask(provider, request)

    assert provider.missed == tuple(request_key(r) for r in (first, second, first))
    assert provider.unused() == (spare,)


def test_a_recorded_provider_answers_no_embedding() -> None:
    provider = RecordedProvider(recording_of({}))

    with pytest.raises(ProviderError) as raised:
        provider.embed(
            deployment(),
            EmbeddingRequest(inputs=("text",)),
            timeout_seconds=5.0,
        )

    assert (raised.value.kind, raised.value.sent) == ("not-recorded", False)


def run_in_threads(work: Callable[[int], None], count: int = 8) -> None:
    threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_misses_and_hits_are_counted_exactly_under_threads() -> None:
    provider = RecordedProvider(recording_of({request_key(REQUEST): answer()}))

    def work(index: int) -> None:
        for n in range(50):
            with contextlib.suppress(ProviderError):
                ask(provider, request_with(f"{index}-{n}"))
            ask(provider)

    run_in_threads(work)

    assert len(provider.missed) == 8 * 50
    assert provider.unused() == ()


# ── RecordingProvider ───────────────────────────────────────────────────────
class Inner:
    """Answers or raises as told; remembers what it was asked."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.seen: list[ChatRequest] = []

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.seen.append(request)
        if self.error is not None:
            raise self.error
        return ProviderReply(
            text=ANSWER_TEXT,
            finish_reason="length",
            model=PROVIDER_MODEL,
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
        )

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        return EmbeddingReply(embeddings=((0.5,),), model="m", input_tokens=1)


def ticks(*values: float) -> Callable[[], float]:
    iterator: Iterator[float] = iter(values)
    return lambda: next(iterator)


def test_a_reply_is_returned_unchanged_and_stored_with_its_six_fields() -> None:
    inner = Inner()
    provider = RecordingProvider(inner, now=ticks(10.0, 10.25))

    reply = provider.chat(deployment(), REQUEST, timeout_seconds=5.0)

    assert reply == ProviderReply(
        text=ANSWER_TEXT,
        finish_reason="length",
        model=PROVIDER_MODEL,
        input_tokens=INPUT_TOKENS,
        output_tokens=OUTPUT_TOKENS,
    )
    recorded = provider.recording({"prompt-v1": HEX_A})
    assert recorded.recorded_for == {"prompt-v1": HEX_A}
    assert list(recorded.entries) == [request_key(REQUEST)]
    (stored,) = recorded.entries.values()
    assert stored.model_dump() == {
        "text": ANSWER_TEXT,
        "finish_reason": "length",
        "model": PROVIDER_MODEL,
        "input_tokens": INPUT_TOKENS,
        "output_tokens": OUTPUT_TOKENS,
        "latency_ms": 250,
    }
    text = dump_recording(recorded)
    assert CLAIM_TEXT not in text  # the request is not stored


def test_the_latency_is_whole_milliseconds() -> None:
    provider = RecordingProvider(Inner(), now=ticks(1.0, 1.0004))

    provider.chat(deployment(), REQUEST, timeout_seconds=5.0)

    (stored,) = provider.recording({}).entries.values()
    assert stored.latency_ms == 0


def test_a_failed_call_stores_nothing_and_its_error_propagates_unchanged() -> None:
    error = ProviderError("unavailable", 503)
    provider = RecordingProvider(Inner(error), now=ticks(1.0, 2.0))

    with pytest.raises(ProviderError) as raised:
        provider.chat(deployment(), REQUEST, timeout_seconds=5.0)

    assert raised.value is error
    assert provider.recording({}).entries == {}


def test_the_same_key_asked_twice_keeps_the_last_answer() -> None:
    inner = Inner()
    provider = RecordingProvider(inner, now=ticks(0.0, 0.1, 0.0, 0.3))

    provider.chat(deployment(), REQUEST, timeout_seconds=5.0)
    provider.chat(deployment(), REQUEST, timeout_seconds=5.0)

    (stored,) = provider.recording({}).entries.values()
    assert stored.latency_ms == 300
    assert len(inner.seen) == 2


def test_embed_delegates_and_records_nothing() -> None:
    provider = RecordingProvider(Inner())

    reply = provider.embed(
        deployment(), EmbeddingRequest(inputs=("text",)), timeout_seconds=5.0
    )

    assert reply.embeddings == ((0.5,),)
    assert provider.recording({}).entries == {}


def test_the_returned_recording_is_a_copy() -> None:
    provider = RecordingProvider(Inner())
    before = provider.recording({})

    provider.chat(deployment(), REQUEST, timeout_seconds=5.0)

    assert before.entries == {}
    assert len(provider.recording({}).entries) == 1


def test_a_recording_made_under_threads_holds_every_key() -> None:
    provider = RecordingProvider(Inner())

    def work(index: int) -> None:
        for n in range(25):
            provider.chat(
                deployment(), request_with(f"{index}-{n}"), timeout_seconds=5.0
            )

    run_in_threads(work)

    assert len(provider.recording({}).entries) == 8 * 25


# ── the gateway in recorded mode ────────────────────────────────────────────
def registry_without_recorded() -> Registry:
    registry = load_registry(REGISTRY_DIR)
    return registry.model_copy(update={"recorded": ()})


def recorded_client(
    database_url: str,
    recordings: Path,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    settings = GatewaySettings(
        registry_dir=REGISTRY_DIR,
        mode="recorded",
        environment="test",
        database_url=database_url,
        recordings=recordings,
    )
    tracing = make_tracer_provider("model-gateway", exporter)
    return TestClient(create_app(settings, tracer_provider=tracing))


@pytest.fixture
def recordings(tmp_path: Path) -> Path:
    """A file that answers BODY only."""
    path = tmp_path / "recording.json"
    write_recording(recording_of({request_key(REQUEST): answer()}), path)
    return path


def usage_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT deployment, state, reserved_tokens, charged_tokens, "
        "charged_micro_eur, input_tokens, output_tokens "
        "FROM gateway.usage ORDER BY reserved_at",
    )


def test_a_recorded_chat_is_answered_charged_audited_and_traced(
    fresh_database: DatabaseHandle, recordings: Path
) -> None:
    exporter = InMemorySpanExporter()
    client = recorded_client(fresh_database.dsn("model_gateway"), recordings, exporter)
    run_id = uuid.uuid4()

    response = client.post("/v1/chat", json=BODY, headers=headers(run_id))

    assert response.status_code == 200
    body = response.json()
    assert (body["mode"], body["deployment"], body["provider"], body["model"]) == (
        "recorded",
        RECORDED_DEPLOYMENT,
        "recorded",
        RECORDED_DEPLOYMENT,
    )
    assert body["output"] == {"text": ANSWER_TEXT, "finish_reason": "stop"}
    assert body["usage"] == {
        "input_tokens": INPUT_TOKENS,
        "output_tokens": OUTPUT_TOKENS,
    }
    registry = load_registry(REGISTRY_DIR)
    live = registry.deployment("aoai-sdc-gpt-4o")
    assert live is not None
    charge = cost_micro_eur(
        deployment().price, registry.exchange, INPUT_TOKENS, OUTPUT_TOKENS
    )
    assert charge > 0
    assert charge == cost_micro_eur(
        live.price, registry.exchange, INPUT_TOKENS, OUTPUT_TOKENS
    )
    ((name, state, _, charged_tokens, micro_eur, tokens_in, tokens_out),) = usage_rows(
        fresh_database
    )
    assert (name, state, micro_eur) == (RECORDED_DEPLOYMENT, "settled", charge)
    assert (tokens_in, tokens_out) == (INPUT_TOKENS, OUTPUT_TOKENS)
    assert charged_tokens == INPUT_TOKENS + OUTPUT_TOKENS
    (event,) = audit_events(fresh_database, run_id)
    assert (event["event"], event["outcome"], event["call_id"]) == (
        "model.call",
        "completed",
        uuid.UUID(body["call_id"]),
    )
    assert (event["deployment"], event["provider"], event["model"]) == (
        RECORDED_DEPLOYMENT,
        "recorded",
        RECORDED_DEPLOYMENT,
    )
    assert (event["input_tokens"], event["output_tokens"]) == (
        INPUT_TOKENS,
        OUTPUT_TOKENS,
    )
    assert event["provider_model"] == PROVIDER_MODEL
    (chat_span,) = [
        s for s in exporter.get_finished_spans() if s.name == "gateway.chat"
    ]
    attributes = dict(chat_span.attributes)
    assert attributes["meridian.mode"] == "recorded"
    assert attributes["meridian.deployment"] == RECORDED_DEPLOYMENT
    assert attributes["meridian.cost_micro_eur"] == charge


def test_an_unrecorded_chat_is_a_502_with_the_fixed_detail_and_nothing_is_billed(
    fresh_database: DatabaseHandle, recordings: Path
) -> None:
    client = recorded_client(fresh_database.dsn("model_gateway"), recordings)
    run_id = uuid.uuid4()
    miss = {"messages": [{"role": "user", "content": CANARY}]}

    response = client.post("/v1/chat", json=miss, headers=headers(run_id))

    assert response.status_code == 502
    assert response.json() == {"detail": NOT_RECORDED_DETAIL}
    assert NOT_RECORDED == NOT_RECORDED_DETAIL
    assert CANARY not in response.text
    ((name, state, _, charged_tokens, micro_eur, _, _),) = usage_rows(fresh_database)
    assert (name, state, charged_tokens, micro_eur) == (
        RECORDED_DEPLOYMENT,
        "released",
        0,
        0,
    )
    (event,) = audit_events(fresh_database, run_id)
    assert (event["outcome"], event["reason"], event["http_status"]) == (
        "failed",
        "not-recorded",
        None,
    )
    assert event["deployment"] == RECORDED_DEPLOYMENT
    counters = owner_rows(fresh_database, "SELECT amount FROM gateway.budget_counters")
    assert [amount for (amount,) in counters] == [0] * len(counters)


def test_misses_open_no_circuit_and_a_recorded_request_after_them_is_answered(
    fresh_database: DatabaseHandle, recordings: Path
) -> None:
    client = recorded_client(fresh_database.dsn("model_gateway"), recordings)

    for number in range(5):  # more than the circuit's threshold of failures
        miss = {"messages": [{"role": "user", "content": f"unrecorded {number}"}]}
        assert client.post("/v1/chat", json=miss, headers=headers()).status_code == 502
    after = client.post("/v1/chat", json=BODY, headers=headers())

    assert after.status_code == 200
    assert after.json()["output"]["text"] == ANSWER_TEXT


def test_an_embedding_is_answered_by_the_replay_embedding_deployment(
    fresh_database: DatabaseHandle, recordings: Path
) -> None:
    client = recorded_client(fresh_database.dsn("model_gateway"), recordings)

    response = client.post(
        "/v1/embeddings", json={"inputs": [CLAIM_TEXT]}, headers=headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["mode"], body["deployment"], body["provider"]) == (
        "recorded",
        "replay-embedding",
        "replay",
    )


def test_a_purpose_with_no_recorded_deployment_goes_to_its_replay_deployment(
    fresh_database: DatabaseHandle,
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gateway_app, "load_registry", lambda _: registry_without_recorded()
    )
    client = recorded_client(fresh_database.dsn("model_gateway"), recordings)

    response = client.post("/v1/chat", json=BODY, headers=headers())

    assert response.status_code == 200
    body = response.json()
    assert (body["mode"], body["deployment"], body["provider"]) == (
        "recorded",
        "replay-chat",
        "replay",
    )
    assert body["output"]["text"].startswith("Replay response (simulated;")


# ── the start rules ─────────────────────────────────────────────────────────
def recorded_settings(
    environment: str = "test", recordings: Path | None = None
) -> GatewaySettings:
    return GatewaySettings(
        registry_dir=REGISTRY_DIR,
        mode="recorded",
        environment=environment,  # type: ignore[arg-type]
        database_url=UNUSED_DSN,
        recordings=recordings,
    )


class Silent:
    def chat(self, *args: object, **kwargs: object) -> ProviderReply:
        raise AssertionError("not called")

    def embed(self, *args: object, **kwargs: object) -> EmbeddingReply:
        raise AssertionError("not called")


@pytest.mark.parametrize("environment", ["kind", "azure"])
@pytest.mark.parametrize("brings_providers", [False, True])
def test_recorded_mode_is_refused_outside_test_ci_and_local(
    recordings: Path, environment: str, brings_providers: bool
) -> None:
    providers = {"recorded": Silent(), "replay": Silent()} if brings_providers else None

    with pytest.raises(
        SettingsError,
        match=r"recorded mode is refused outside the test, ci and local "
        r"environments \(T-76\)",
    ):
        create_app(recorded_settings(environment, recordings), providers=providers)


@pytest.mark.parametrize("environment", ["test", "ci", "local"])
def test_recorded_mode_starts_in_test_ci_and_local(
    recordings: Path, environment: str
) -> None:
    app = create_app(recorded_settings(environment, recordings))

    assert app.title == "Meridian Model Gateway"


def test_recorded_mode_without_recordings_and_without_providers_is_refused() -> None:
    with pytest.raises(SettingsError, match=RECORDINGS_ENV):
        create_app(recorded_settings())


def test_recorded_mode_with_providers_needs_no_recordings() -> None:
    create_app(
        recorded_settings(), providers={"recorded": Silent(), "replay": Silent()}
    )


@pytest.mark.parametrize(
    "content",
    [None, "not json " + CANARY, json.dumps({"format": 2, "note": CANARY})],
    ids=["missing", "not json", "invalid"],
)
def test_an_unreadable_recording_stops_the_start_and_names_the_variable_only(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "recording.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(SettingsError) as raised:
        create_app(recorded_settings(recordings=path))

    assert RECORDINGS_ENV in str(raised.value)
    assert CANARY not in str(raised.value)
    assert "7741" not in str(raised.value)
    assert str(path) not in str(raised.value)


def test_the_variable_is_read_from_the_environment(tmp_path: Path) -> None:
    environ = {
        "MERIDIAN_GATEWAY_MODE": "recorded",
        "MERIDIAN_ENVIRONMENT": "ci",
        "MERIDIAN_DATABASE_URL": UNUSED_DSN,
        RECORDINGS_ENV: str(tmp_path / "r.json"),
    }

    built = GatewaySettings.from_env(environ)
    without = GatewaySettings.from_env({**environ, RECORDINGS_ENV: ""})

    assert RECORDINGS_ENV == "MERIDIAN_GATEWAY_RECORDINGS"
    assert built.mode == "recorded"
    assert built.recordings == tmp_path / "r.json"
    assert without.recordings is None


def test_a_live_gateway_refuses_a_chat_route_candidate_of_provider_kind_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = load_registry(REGISTRY_DIR)
    routes = tuple(
        route.model_copy(
            update={"candidates": ("aoai-sdc-gpt-4o", RECORDED_DEPLOYMENT)}
        )
        if route.purpose == "chat"
        else route
        for route in registry.routes
    )
    monkeypatch.setattr(
        gateway_app,
        "load_registry",
        lambda _: registry.model_copy(update={"routes": routes}),
    )
    settings = GatewaySettings(
        registry_dir=REGISTRY_DIR,
        mode="live",
        environment="test",
        database_url=UNUSED_DSN,
    )

    with pytest.raises(SettingsError, match="the chat route has a recorded candidate"):
        create_app(settings, providers={"azure-openai": Silent(), "recorded": Silent()})
