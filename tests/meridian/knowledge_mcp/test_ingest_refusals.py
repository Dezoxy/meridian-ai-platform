"""What ``ingest_wordings`` does with the gateway's answers, and what it
leaves in the database (S012).

The tests that expect a refusal before any write and need no database are in
``test_ingest_refusals_offline.py`` (S065). What stays here needs the real
gateway app (its ledger is in the database) or asserts what a run stored.
"""

import json
import logging

import httpx
import pytest
from dbsupport import DatabaseHandle
from knowledgesupport import (
    CANARY,
    REAL_SOURCE,
    TENANT,
    Gateway,
    ScriptedGateway,
    Source,
    Waits,
    audit_row_count,
    canary_source,
    chunk_count,
    chunk_rows,
    embedding_reply,
    ingest,
    too_many,
)
from servicesupport import owner_rows

from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.platform.knowledge_mcp.ingest import (
    BATCH_SIZE,
    DEFAULT_WAIT_SECONDS,
    MAX_TOTAL_WAIT_SECONDS,
    MAX_UNANNOUNCED_RETRIES,
    MAX_WAIT_SECONDS,
    MIN_WAIT_SECONDS,
    IngestError,
)
from meridian.platform.registry import Registry

CHUNKS = 85
BATCHES = 6


# ── 2. the manifest ─────────────────────────────────────────────────────────
def test_the_manifests_entries_for_other_files_are_not_wordings(
    fresh_database: DatabaseHandle, gateway: Gateway, source: Source
) -> None:
    directory = source()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert "policies.json" in manifest["files"]  # listed, and not in the directory

    counts, _ = ingest(fresh_database, gateway.http, gateway.registry, source=directory)

    assert counts.chunks == CHUNKS


# ── 4. the embedding: order, batches, waits and failures ────────────────────
def test_the_texts_go_in_batches_of_sixteen_in_file_then_clause_order(
    fresh_database: DatabaseHandle, registry: Registry
) -> None:
    gateway = ScriptedGateway()

    ingest(fresh_database, gateway.http(), registry)

    expected = [
        f"{chunk.title}\n{chunk.body}"
        for path in sorted((REAL_SOURCE / "wordings").glob("*.md"))
        for chunk in parse_wording(path.read_text(encoding="utf-8")).chunks
    ]
    assert [len(batch) for batch in gateway.requests] == [BATCH_SIZE] * 5 + [5]
    assert [text for batch in gateway.requests for text in batch] == expected


def test_every_call_carries_the_tenant_agent_and_one_run(
    fresh_database: DatabaseHandle, registry: Registry
) -> None:
    gateway = ScriptedGateway()

    _, run_id = ingest(fresh_database, gateway.http(), registry)

    assert len(gateway.headers) == BATCHES
    for headers in gateway.headers:
        assert headers["X-Meridian-Tenant"] == TENANT
        assert headers["X-Meridian-Agent"] == "knowledge-ingestion"
        assert headers["X-Meridian-Run"] == str(run_id)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        pytest.param("7", 7.0, id="the-gateways-own-wait"),
        pytest.param(None, DEFAULT_WAIT_SECONDS, id="no-retry-after"),
        pytest.param("soon", DEFAULT_WAIT_SECONDS, id="retry-after-not-a-number"),
        pytest.param("90", MAX_WAIT_SECONDS, id="capped-at-a-minute"),
        pytest.param("60", 60.0, id="a-minute-exactly"),
        pytest.param("0", MIN_WAIT_SECONDS, id="zero-is-raised-to-the-minimum"),
    ],
)
def test_a_429_is_waited_out_through_the_injected_sleep_and_retried(
    fresh_database: DatabaseHandle,
    registry: Registry,
    retry_after: str | None,
    expected: float,
) -> None:
    gateway = ScriptedGateway(
        script=lambda index, inputs: too_many(retry_after) if index == 0 else None
    )
    waits = Waits()

    counts, _ = ingest(fresh_database, gateway.http(), registry, sleep=waits)

    assert waits.seconds == [expected]
    assert counts.chunks == CHUNKS
    assert chunk_count(fresh_database) == CHUNKS
    # The refused call and its retry carried the same texts.
    assert gateway.requests[0] == gateway.requests[1]
    assert len(gateway.requests) == BATCHES + 1


def test_waits_that_add_up_to_exactly_the_bound_are_allowed(
    fresh_database: DatabaseHandle, registry: Registry
) -> None:
    refusals = int(MAX_TOTAL_WAIT_SECONDS // MAX_WAIT_SECONDS)
    gateway = ScriptedGateway(
        script=lambda index, inputs: too_many("60") if index < refusals else None
    )
    waits = Waits()

    ingest(fresh_database, gateway.http(), registry, sleep=waits)

    assert sum(waits.seconds) == MAX_TOTAL_WAIT_SECONDS
    assert chunk_count(fresh_database) == CHUNKS


def test_a_failure_on_the_last_batch_leaves_the_table_as_it_was(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry)
    before = chunk_rows(fresh_database)
    audit_before = audit_row_count(fresh_database)
    failing = ScriptedGateway(
        script=lambda index, inputs: (
            httpx.Response(500, json={}) if index == BATCHES - 1 else None
        )
    )

    with pytest.raises(IngestError):
        ingest(fresh_database, failing.http(), gateway.registry)

    assert len(failing.requests) == BATCHES
    assert chunk_rows(fresh_database) == before
    assert audit_row_count(fresh_database) == audit_before


def test_the_run_stores_the_deployment_the_batches_agreed_on(
    fresh_database: DatabaseHandle, registry: Registry
) -> None:
    gateway = ScriptedGateway(
        script=lambda index, inputs: httpx.Response(
            200,
            json=embedding_reply(
                len(inputs), deployment="aoai-x", model="text-x", dimensions=5
            ),
        )
    )

    counts, _ = ingest(fresh_database, gateway.http(), registry)

    assert (counts.deployment, counts.model, counts.dimensions) == (
        "aoai-x",
        "text-x",
        5,
    )
    stored = {tuple(row[7:10]) for row in chunk_rows(fresh_database)}
    assert stored == {("aoai-x", "text-x", 5)}


# ── no wording text outside the table ───────────────────────────────────────
def test_canary_text_in_a_wording_is_in_the_table_and_in_no_audit_row_or_log(
    fresh_database: DatabaseHandle,
    gateway: Gateway,
    source: Source,
    caplog: pytest.LogCaptureFixture,
) -> None:
    directory = canary_source(source)

    with caplog.at_level(logging.DEBUG):
        ingest(fresh_database, gateway.http, gateway.registry, source=directory)

    ((in_table,),) = owner_rows(
        fresh_database,
        "SELECT count(*) FROM knowledge.chunks WHERE title LIKE %s AND body LIKE %s",
        (f"%{CANARY}%", f"%{CANARY}%"),
    )
    assert in_table == 1
    assert CANARY not in str(owner_rows(fresh_database, "SELECT * FROM audit.events"))
    assert CANARY not in caplog.text
    assert all(CANARY not in str(record.__dict__) for record in caplog.records)


# ── a 429 that carries no Retry-After ───────────────────────────────────────
def test_a_429_without_retry_after_twice_and_then_an_answer_goes_through(
    fresh_database: DatabaseHandle, registry: Registry
) -> None:
    gateway = ScriptedGateway(
        script=lambda index, inputs: too_many() if index < 2 else None
    )
    waits = Waits()

    counts, _ = ingest(fresh_database, gateway.http(), registry, sleep=waits)

    assert counts.chunks == CHUNKS
    assert waits.seconds == [DEFAULT_WAIT_SECONDS] * 2
    assert len(gateway.requests) == BATCHES + 2


def test_the_count_of_429s_without_retry_after_is_for_each_batch(
    fresh_database: DatabaseHandle, registry: Registry
) -> None:
    # Each batch is refused twice without a Retry-After and then answered: no
    # batch passes its two, though the whole run holds twelve.
    seen: dict[tuple[str, ...], int] = {}

    def script(index: int, inputs: list[str]) -> httpx.Response | None:
        key = tuple(inputs)
        seen[key] = seen.get(key, 0) + 1
        return too_many() if seen[key] <= 2 else None

    waits = Waits()

    counts, _ = ingest(
        fresh_database, ScriptedGateway(script=script).http(), registry, sleep=waits
    )

    assert counts.chunks == CHUNKS
    assert waits.seconds == [DEFAULT_WAIT_SECONDS] * (2 * BATCHES)


def test_a_429_that_names_its_wait_keeps_the_existing_bounds(
    fresh_database: DatabaseHandle, registry: Registry
) -> None:
    refusals = MAX_UNANNOUNCED_RETRIES + 3  # more than the unannounced limit
    gateway = ScriptedGateway(
        script=lambda index, inputs: too_many("1") if index < refusals else None
    )
    waits = Waits()

    counts, _ = ingest(fresh_database, gateway.http(), registry, sleep=waits)

    assert counts.chunks == CHUNKS
    assert waits.seconds == [1.0] * refusals
