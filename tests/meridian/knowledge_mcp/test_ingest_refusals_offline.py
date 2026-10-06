"""What ``ingest_wordings`` refuses or fails on before it writes (S012): the
registry (T-60), the manifest (T-57), the wordings, the gateway.

These tests need no database (S065). The connection they pass in fails the
test when anything is executed on it, so "nothing was written" is true by
construction. A test that needs the real gateway app, or asserts what a run
stored, is in ``test_ingest_refusals.py``.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from knowledgesupport import (
    CANARY,
    REAL_SOURCE,
    TENANT,
    WORDING,
    RefusalOnlyConnection,
    ScriptedGateway,
    Source,
    Waits,
    canary_source,
    embedding_reply,
    ingest_without_database,
    too_many,
)

from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp.embedding_client import EmbeddingClient
from meridian.platform.knowledge_mcp.ingest import (
    DEFAULT_WAIT_SECONDS,
    MAX_UNANNOUNCED_RETRIES,
    IngestError,
    ingest_wordings,
)
from meridian.platform.registry import Registry, load_registry


def assert_refused_before_any_call(
    directory: Path,
    registry: Registry,
    *reasons: str,
    tenant: str = TENANT,
) -> IngestError:
    """The ingestion is refused with a message that holds each of ``reasons``
    and the gateway was never called (nothing was written: the connection
    would have failed the test)."""
    gateway = ScriptedGateway()

    with pytest.raises(IngestError) as raised:
        ingest_without_database(
            gateway.http(), registry, tenant=tenant, source=directory
        )

    for reason in reasons:
        assert reason in str(raised.value)
    assert gateway.requests == []
    return raised.value


# ── 1. the registry: before any file is read or call made ───────────────────
@pytest.mark.parametrize(
    ("tenant", "reason"),
    [
        pytest.param("ghost", "not in the registry", id="unknown-tenant"),
        pytest.param("evaluation", "may not run agent", id="tenant-without-the-agent"),
    ],
)
def test_a_tenant_that_is_unknown_or_may_not_run_the_agent_is_refused_first(
    registry: Registry,
    tmp_path: Path,
    tenant: str,
    reason: str,
) -> None:
    error = assert_refused_before_any_call(
        tmp_path / "does-not-exist", registry, reason, tenant=tenant
    )

    assert "manifest" not in str(error)  # no file was read


def test_a_client_that_is_not_the_ingestion_agent_is_refused_before_anything_else(
    registry: Registry, tmp_path: Path
) -> None:
    gateway = ScriptedGateway()

    with pytest.raises(IngestError) as raised:
        # claims-triage may run claims-triage, so only the agent check refuses.
        ingest_without_database(
            gateway.http(),
            registry,
            agent="claims-triage",
            source=tmp_path / "does-not-exist",
        )

    assert "knowledge-ingestion" in str(raised.value)
    assert "manifest" not in str(raised.value)  # no file was read
    assert gateway.requests == []


def test_a_tenant_of_class_synthetic_is_refused_even_when_it_runs_the_agent(
    plant: Callable[..., Path], tmp_path: Path
) -> None:
    directory = plant(
        (
            "tenants.yaml",
            "data_class: synthetic\n    agents: [claims-triage]",
            "data_class: synthetic\n    agents: [claims-triage, knowledge-ingestion]",
        )
    )
    registry = load_registry(directory)
    assert registry.tenant_may_run("development", "knowledge-ingestion")

    error = assert_refused_before_any_call(
        tmp_path / "does-not-exist",
        registry,
        "synthetic",
        "global",
        tenant="development",
    )

    assert "manifest" not in str(error)


def test_the_real_registry_lets_claims_triage_ingest_and_no_other_tenant(
    registry: Registry,
) -> None:
    assert [t.id for t in registry.tenants if "knowledge-ingestion" in t.agents] == [
        TENANT
    ]
    agent = registry.agent("knowledge-ingestion")
    assert agent is not None
    assert agent.tools == ()


# ── 2. the manifest ─────────────────────────────────────────────────────────
def test_a_manifest_that_is_not_synthetic_is_refused(
    source: Source, registry: Registry
) -> None:
    directory = source(manifest_edit=lambda m: m.update(synthetic=False))

    assert_refused_before_any_call(directory, registry, "synthetic")


@pytest.mark.parametrize("value", ["true", 1, None])
def test_only_the_boolean_true_counts_as_synthetic(
    source: Source, registry: Registry, value: object
) -> None:
    directory = source(manifest_edit=lambda m: m.update(synthetic=value))

    assert_refused_before_any_call(directory, registry, "synthetic")


def test_a_manifest_without_the_synthetic_key_is_refused(
    source: Source, registry: Registry
) -> None:
    directory = source(manifest_edit=lambda m: m.pop("synthetic"))

    assert_refused_before_any_call(directory, registry, "synthetic")


def test_a_missing_manifest_is_refused(source: Source, registry: Registry) -> None:
    directory = source()
    (directory / "manifest.json").unlink()

    assert_refused_before_any_call(directory, registry, "manifest.json")


@pytest.mark.parametrize("text", ["{", "[]", '"text"', "null"])
def test_a_manifest_that_is_not_a_json_object_is_refused(
    source: Source, registry: Registry, text: str
) -> None:
    directory = source()
    (directory / "manifest.json").write_text(text, encoding="utf-8")

    assert_refused_before_any_call(directory, registry, "manifest.json")


def test_a_manifest_without_a_file_list_is_refused(
    source: Source, registry: Registry
) -> None:
    directory = source(manifest_edit=lambda m: m.pop("files"))

    assert_refused_before_any_call(directory, registry, "manifest.json")


def test_a_wording_the_manifest_does_not_list_is_refused(
    source: Source, registry: Registry
) -> None:
    directory = source(manifest_edit=lambda m: m["files"].pop("wordings/MOTOR-COMP.md"))

    assert_refused_before_any_call(
        directory, registry, "wordings/MOTOR-COMP.md", "not listed"
    )


def test_a_listed_wording_that_is_missing_is_refused(
    source: Source, registry: Registry
) -> None:
    directory = source()
    (directory / WORDING).unlink()

    assert_refused_before_any_call(directory, registry, WORDING, "missing")


@pytest.mark.parametrize("name", ["HOME-PLUS", "HOME-STD", "MOTOR-COMP", "MOTOR-TPL"])
def test_a_wording_whose_hash_differs_is_refused(
    source: Source, registry: Registry, name: str
) -> None:
    key = f"wordings/{name}.md"
    directory = source(manifest_edit=lambda m: m["files"].update({key: "0" * 64}))

    assert_refused_before_any_call(directory, registry, key, "hash")


def test_a_wording_changed_after_the_generator_is_refused(
    source: Source, registry: Registry
) -> None:
    def tamper(content: dict[str, str]) -> None:
        content[WORDING] += "\nAn extra line.\n"

    directory = source(tamper, rehash=False)

    assert_refused_before_any_call(directory, registry, WORDING, "hash")


def test_a_source_with_no_wording_is_refused(
    source: Source, registry: Registry
) -> None:
    directory = source(lambda content: content.clear())

    assert_refused_before_any_call(directory, registry, "no wording")


def test_a_source_without_a_wordings_directory_is_refused(
    tmp_path: Path, registry: Registry
) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"synthetic": True, "files": {}}), encoding="utf-8"
    )

    assert_refused_before_any_call(tmp_path, registry, "no wording")


def test_a_wording_that_is_not_utf8_is_refused(
    source: Source, registry: Registry
) -> None:
    directory = source()
    data = b"\xff\xfe not text"
    (directory / WORDING).write_bytes(data)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][WORDING] = hashlib.sha256(data).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert_refused_before_any_call(directory, registry, "UTF-8")


# ── 3. the parse ────────────────────────────────────────────────────────────
def test_a_wording_that_breaks_a_rule_is_refused_naming_its_file_and_line(
    source: Source, registry: Registry
) -> None:
    def break_a_clause(content: dict[str, str]) -> None:
        content[WORDING] = content[WORDING].replace("### 1.2", "### 2.2", 1)

    assert_refused_before_any_call(source(break_a_clause), registry, WORDING, "line ")


def test_two_wordings_of_the_same_product_and_version_are_refused(
    source: Source, registry: Registry
) -> None:
    def copy_one(content: dict[str, str]) -> None:
        content["wordings/ZZ-COPY.md"] = content[WORDING]

    assert_refused_before_any_call(
        source(copy_one),
        registry,
        "wordings/ZZ-COPY.md",
        "same product code and wording version",
    )


# ── 4. the embedding: waits and failures that end the run ───────────────────
def test_waits_that_would_pass_the_bound_give_up_before_the_wait_that_would(
    registry: Registry,
) -> None:
    gateway = ScriptedGateway(script=lambda index, inputs: too_many("60"))
    waits = Waits()

    with pytest.raises(IngestError, match="429"):
        ingest_without_database(gateway.http(), registry, sleep=waits)

    assert waits.seconds == [60.0] * 5


def test_the_bound_is_for_the_whole_ingestion_not_for_each_batch(
    registry: Registry,
) -> None:
    # Each batch is refused once, for 60 s: the sixth wait passes 300 s.
    refused_once: set[tuple[str, ...]] = set()

    def script(index: int, inputs: list[str]) -> httpx.Response | None:
        if tuple(inputs) in refused_once:
            return None
        refused_once.add(tuple(inputs))
        return too_many("60")

    waits = Waits()

    with pytest.raises(IngestError, match="429"):
        ingest_without_database(
            ScriptedGateway(script=script).http(), registry, sleep=waits
        )

    assert waits.seconds == [60.0] * 5


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(500, json={"detail": "x"}), id="server-error"),
        pytest.param(httpx.Response(403, json={"detail": "x"}), id="policy-refusal"),
        pytest.param(httpx.Response(413, json={"detail": "x"}), id="too-large"),
        pytest.param(httpx.Response(200, json={"unexpected": "shape"}), id="bad-shape"),
        pytest.param(httpx.Response(200, content=b"not json"), id="not-json"),
    ],
)
def test_any_other_failure_is_refused_at_once_without_a_wait(
    registry: Registry, response: httpx.Response
) -> None:
    gateway = ScriptedGateway(script=lambda index, inputs: response)
    waits = Waits()

    with pytest.raises(IngestError):
        ingest_without_database(gateway.http(), registry, sleep=waits)

    assert len(gateway.requests) == 1
    assert waits.seconds == []


@pytest.mark.parametrize(
    "different",
    [
        pytest.param({"deployment": "other-embedding"}, id="deployment"),
        pytest.param({"model": "other-model"}, id="model"),
        pytest.param({"dimensions": 4}, id="dimensions"),
    ],
)
def test_batches_from_two_deployments_models_or_lengths_are_refused(
    registry: Registry, different: dict[str, Any]
) -> None:
    gateway = ScriptedGateway(
        script=lambda index, inputs: (
            httpx.Response(200, json=embedding_reply(len(inputs), **different))
            if index == 3
            else None
        )
    )

    with pytest.raises(IngestError, match="not comparable"):
        ingest_without_database(gateway.http(), registry)


# ── no wording text outside the table ───────────────────────────────────────
@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(500, json={"detail": f"echo {CANARY}"}), id="500"),
        pytest.param(
            httpx.Response(429, json={"detail": f"echo {CANARY}"}), id="429-forever"
        ),
        pytest.param(
            httpx.Response(200, json={"detail": f"echo {CANARY}"}), id="bad-shape"
        ),
    ],
)
def test_canary_text_is_in_no_exception_message_or_log_when_the_gateway_fails(
    source: Source,
    registry: Registry,
    caplog: pytest.LogCaptureFixture,
    response: httpx.Response,
) -> None:
    directory = canary_source(source)
    gateway = ScriptedGateway(script=lambda index, inputs: response)

    with caplog.at_level(logging.DEBUG), pytest.raises(IngestError) as raised:
        ingest_without_database(
            gateway.http(), registry, source=directory, sleep=Waits()
        )

    assert CANARY not in str(raised.value)
    assert CANARY not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert CANARY not in caplog.text


def test_canary_text_is_in_no_message_of_a_wording_that_breaks_a_rule(
    source: Source, registry: Registry
) -> None:
    def break_with_canary(content: dict[str, str]) -> None:
        content[WORDING] = content[WORDING].replace("### 1.2 ", f"### 2.2 {CANARY} ", 1)

    error = assert_refused_before_any_call(source(break_with_canary), registry, "line ")

    assert CANARY not in str(error)
    assert error.__cause__ is not None  # the WordingError, which has no text
    assert CANARY not in str(error.__cause__)
    assert CANARY not in repr(error.__cause__)


# ── a 429 that carries no Retry-After ───────────────────────────────────────
def test_a_429_without_retry_after_is_retried_twice_and_then_given_up(
    registry: Registry,
) -> None:
    # The gateway sends no Retry-After when a budget is used up, and waiting
    # does not help: three answers in all, two waits, then the ingestion stops.
    gateway = ScriptedGateway(script=lambda index, inputs: too_many())
    waits = Waits()

    with pytest.raises(IngestError, match="429") as raised:
        ingest_without_database(gateway.http(), registry, sleep=waits)

    assert MAX_UNANNOUNCED_RETRIES == 2
    assert len(gateway.requests) == MAX_UNANNOUNCED_RETRIES + 1
    assert waits.seconds == [DEFAULT_WAIT_SECONDS] * MAX_UNANNOUNCED_RETRIES
    assert raised.value.reason == "gateway-busy"


# ── a connection that commits by itself ─────────────────────────────────────
def test_an_autocommit_connection_is_refused_before_anything_else(
    registry: Registry, tmp_path: Path
) -> None:
    gateway = ScriptedGateway()
    # The wrong agent and a source that does not exist: the replace must be one
    # transaction (T-58), and that is the first thing that is checked.
    client = EmbeddingClient(
        gateway.http(), tenant="ghost", agent="claims-triage", run_id=uuid.uuid4()
    )

    with pytest.raises(IngestError, match="transaction") as raised:
        ingest_wordings(
            RefusalOnlyConnection(autocommit=True),
            tmp_path / "does-not-exist",
            client,
            registry,
        )

    assert raised.value.reason == "not-transactional"
    assert gateway.requests == []


# ── every refusal says why, in a word ───────────────────────────────────────
def run_with_reason(
    registry: Registry,
    *,
    source: Path = REAL_SOURCE,
    gateway: ScriptedGateway | None = None,
    tenant: str = TENANT,
    agent: str = INGESTION_AGENT,
) -> str:
    gateway = gateway or ScriptedGateway()
    with pytest.raises(IngestError) as raised:
        ingest_without_database(
            gateway.http(),
            registry,
            tenant=tenant,
            agent=agent,
            source=source,
            sleep=Waits(),
        )
    return raised.value.reason


def test_a_registry_refusal_has_the_reason_registry_refused(
    registry: Registry,
) -> None:
    for tenant in ("ghost", "evaluation"):
        assert run_with_reason(registry, tenant=tenant) == "registry-refused"
    assert run_with_reason(registry, agent="claims-triage") == "registry-refused"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda source, tmp_path: tmp_path, id="no-manifest"),
        pytest.param(
            lambda source, tmp_path: source(
                manifest_edit=lambda m: m.update(synthetic=False)
            ),
            id="not-synthetic",
        ),
        pytest.param(
            lambda source, tmp_path: source(
                lambda content: content.update({WORDING: content[WORDING] + "x\n"}),
                rehash=False,
            ),
            id="hash-differs",
        ),
        pytest.param(
            lambda source, tmp_path: source(
                manifest_edit=lambda m: m["files"].pop("wordings/MOTOR-COMP.md")
            ),
            id="wording-not-listed",
        ),
    ],
)
def test_a_manifest_refusal_has_the_reason_manifest_refused(
    registry: Registry,
    source: Source,
    tmp_path: Path,
    build: Callable[[Source, Path], Path],
) -> None:
    directory = build(source, tmp_path)

    assert run_with_reason(registry, source=directory) == "manifest-refused"


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda content: content.update(
                {WORDING: content[WORDING].replace("### 1.2", "### 2.2", 1)}
            ),
            id="a-rule-is-broken",
        ),
        pytest.param(
            lambda content: content.update({"wordings/ZZ-COPY.md": content[WORDING]}),
            id="same-product-and-version-twice",
        ),
    ],
)
def test_a_wording_refusal_has_the_reason_wording_refused(
    registry: Registry,
    source: Source,
    edit: Callable[[dict[str, str]], None],
) -> None:
    directory = source(edit)

    assert run_with_reason(registry, source=directory) == "wording-refused"


def test_a_gateway_failure_has_the_reason_gateway_failed(registry: Registry) -> None:
    refusals = [
        httpx.Response(500, json={}),
        httpx.Response(403, json={}),
        httpx.Response(200, json={"unexpected": "shape"}),
        httpx.Response(200, content=b"not json"),
    ]
    for response in refusals:
        gateway = ScriptedGateway(script=lambda index, inputs, r=response: r)
        assert run_with_reason(registry, gateway=gateway) == "gateway-failed"


def test_batches_that_cannot_be_compared_have_the_reason_gateway_failed(
    registry: Registry,
) -> None:
    gateway = ScriptedGateway(
        script=lambda index, inputs: (
            httpx.Response(200, json=embedding_reply(len(inputs), model="other"))
            if index == 3
            else None
        )
    )

    assert run_with_reason(registry, gateway=gateway) == "gateway-failed"


def test_a_gateway_that_stays_busy_has_the_reason_gateway_busy(
    registry: Registry,
) -> None:
    gateway = ScriptedGateway(script=lambda index, inputs: too_many("60"))

    assert run_with_reason(registry, gateway=gateway) == "gateway-busy"


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            httpx.ConnectError("refused"), id="the-transport-fails-before-an-answer"
        ),
        pytest.param(httpx.ReadTimeout("timed out"), id="the-call-times-out"),
    ],
)
def test_a_transport_failure_or_timeout_is_not_called_a_refusal(
    registry: Registry, response: Exception
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise response

    client = httpx.Client(
        base_url="http://gateway.invalid", transport=httpx.MockTransport(fail)
    )

    with pytest.raises(IngestError) as raised:
        ingest_without_database(client, registry)

    assert "refused" not in str(raised.value)
    assert "failed" in str(raised.value)
    assert raised.value.reason == "gateway-failed"


def test_a_status_the_gateway_answered_is_still_called_a_refusal(
    registry: Registry,
) -> None:
    gateway = ScriptedGateway(script=lambda index, inputs: httpx.Response(403, json={}))

    with pytest.raises(IngestError, match=r"refused the embedding call.*403"):
        ingest_without_database(gateway.http(), registry)
