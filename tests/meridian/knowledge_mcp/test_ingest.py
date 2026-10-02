"""``ingest_wordings`` against the real gateway app in replay mode and a real
PostgreSQL: what it stores, what it audits and how it replaces (S012)."""

import hashlib
import json
from array import array
from collections.abc import Callable
from pathlib import Path

from dbsupport import DatabaseHandle
from knowledgesupport import (
    REAL_SOURCE,
    TENANT,
    WORDING_NAMES,
    Gateway,
    Source,
    Waits,
    chunk_count,
    chunk_rows,
    ingest,
)
from servicesupport import audit_events, owner_rows

from meridian.platform.gateway.replay import replay_embedding
from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.platform.knowledge_mcp.ingest import IngestCounts
from meridian.platform.knowledge_mcp.store import vector_literal

DIMENSIONS = 1024
CHUNKS = 85
BATCHES = 6  # 85 chunks, 16 to a call
MANIFEST = json.loads((REAL_SOURCE / "manifest.json").read_text(encoding="utf-8"))
COLUMNS = (
    "product",
    "wording_version",
    "clause",
    "section",
    "title",
    "body",
    "source_sha256",
    "deployment",
    "model",
    "dimensions",
    "embedding",
    "ingested_at",
)


def stored(db: DatabaseHandle) -> list[dict]:
    return [dict(zip(COLUMNS, row, strict=True)) for row in chunk_rows(db)]


def without_times(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k != "ingested_at"} for row in rows]


def float32(vector: list[float] | tuple[float, ...]) -> list[float]:
    return array("f", vector).tolist()


def parsed(text: str) -> list[float]:
    return [float(component) for component in text.strip("[]").split(",")]


def replay_tokens(text: str) -> int:
    """The gateway's replay count: characters over four, rounded up."""
    return -(-len(text) // 4)


# ── a run against the real wordings ─────────────────────────────────────────
def test_a_run_stores_the_eighty_five_clauses_with_their_replay_vectors(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    counts, _ = ingest(fresh_database, gateway.http, gateway.registry)

    rows = stored(fresh_database)
    assert len(rows) == CHUNKS
    assert counts == IngestCounts(
        documents=4,
        chunks=CHUNKS,
        input_tokens=counts.input_tokens,
        deployment="replay-embedding",
        model="replay-embedding",
        dimensions=DIMENSIONS,
    )
    assert counts.input_tokens > 0
    assert {r["deployment"] for r in rows} == {"replay-embedding"}
    assert {r["model"] for r in rows} == {"replay-embedding"}
    assert {r["dimensions"] for r in rows} == {DIMENSIONS}
    assert {r["product"] for r in rows} == set(WORDING_NAMES)
    assert {r["wording_version"] for r in rows} == {"2026-01"}


def test_each_row_carries_the_hash_the_manifest_lists_for_its_wording(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry)

    for product in WORDING_NAMES:
        listed = MANIFEST["files"][f"wordings/{product}.md"]
        digests = {
            r["source_sha256"]
            for r in stored(fresh_database)
            if r["product"] == product
        }
        assert digests == {listed}
        data = (REAL_SOURCE / "wordings" / f"{product}.md").read_bytes()
        assert hashlib.sha256(data).hexdigest() == listed


def test_each_row_holds_the_clause_as_the_chunking_cuts_it(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry)

    rows = {(r["product"], r["clause"]): r for r in stored(fresh_database)}
    for product in WORDING_NAMES:
        text = (REAL_SOURCE / "wordings" / f"{product}.md").read_text(encoding="utf-8")
        for chunk in parse_wording(text).chunks:
            row = rows[(product, chunk.clause)]
            assert (row["section"], row["title"], row["body"]) == (
                chunk.section,
                chunk.title,
                chunk.body,
            )


def test_a_stored_vector_is_the_replay_vector_of_its_title_and_body(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry)

    rows = stored(fresh_database)
    for row in rows:
        expected = replay_embedding(f"{row['title']}\n{row['body']}", DIMENSIONS)
        # pgvector keeps 4-byte floats: equal at that precision, not before.
        assert float32(parsed(row["embedding"])) == float32(expected), (
            row["product"],
            row["clause"],
        )
    assert len(rows) == CHUNKS


def test_the_vector_text_reads_back_as_the_computed_number() -> None:
    vector = replay_embedding("a storm damaged the roof", DIMENSIONS)

    literal = vector_literal(vector)

    assert [float(c) for c in literal.strip("[]").split(",")] == list(vector)


# ── what the run leaves in the audit log ────────────────────────────────────
def test_the_gateway_rows_and_the_ingestions_own_row_carry_the_run(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    counts, run_id = ingest(fresh_database, gateway.http, gateway.registry)

    events = audit_events(fresh_database, run_id)

    calls = [e for e in events if e["event"] == "model.call"]
    (own,) = [e for e in events if e["event"] == "knowledge.ingest"]
    assert len(calls) == BATCHES
    for call in calls:
        assert (call["service"], call["outcome"]) == ("model-gateway", "completed")
        assert (call["tenant"], call["agent"]) == (TENANT, INGESTION_AGENT)
        assert call["deployment"] == "replay-embedding"
    assert sum(c["input_tokens"] for c in calls) == counts.input_tokens
    assert own["service"] == "knowledge-ingestion"
    assert own["outcome"] == "completed"
    assert (own["tenant"], own["agent"], own["run_id"]) == (
        TENANT,
        INGESTION_AGENT,
        run_id,
    )
    assert own["reference"] == "wordings"
    assert (own["deployment"], own["model"]) == ("replay-embedding",) * 2
    assert own["input_tokens"] == counts.input_tokens
    assert own["output_tokens"] is None
    assert len(events) == BATCHES + 1


def test_the_token_count_is_the_sum_the_gateway_reported_per_text(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    counts, _ = ingest(fresh_database, gateway.http, gateway.registry)

    expected = sum(
        replay_tokens(f"{r['title']}\n{r['body']}") for r in stored(fresh_database)
    )
    assert counts.input_tokens == expected


def test_no_chunk_text_title_or_vector_is_in_any_audit_row(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry)

    audit = str(owner_rows(fresh_database, "SELECT * FROM audit.events"))
    for row in stored(fresh_database):
        assert row["title"] not in audit
        assert row["body"][:40] not in audit
        assert row["embedding"][:30] not in audit


def test_a_rollback_takes_the_rows_and_the_ingestions_audit_row_together(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry, commit=False)

    assert chunk_count(fresh_database) == 0
    ((own,),) = owner_rows(
        fresh_database,
        "SELECT count(*) FROM audit.events WHERE event = 'knowledge.ingest'",
    )
    assert own == 0


# ── replacing what is there ─────────────────────────────────────────────────
def test_a_second_run_leaves_eighty_five_rows_not_a_hundred_and_seventy(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    first, _ = ingest(fresh_database, gateway.http, gateway.registry)
    waits = Waits(also=gateway.clock.advance)

    second, _ = ingest(fresh_database, gateway.http, gateway.registry, sleep=waits)

    assert chunk_count(fresh_database) == CHUNKS
    assert second == first
    # The tenant's token window was spent by the first run, so the gateway told
    # the second to wait: the wait is the gateway's own Retry-After.
    assert waits.seconds
    assert all(0 < wait <= 60 for wait in waits.seconds)


def test_a_second_run_gives_the_rows_the_same_content(
    fresh_database: DatabaseHandle, gateway: Gateway
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry)
    before = without_times(stored(fresh_database))

    ingest(
        fresh_database,
        gateway.http,
        gateway.registry,
        sleep=Waits(also=gateway.clock.advance),
    )

    assert without_times(stored(fresh_database)) == before


def test_a_wording_removed_from_the_source_takes_its_rows_with_it(
    fresh_database: DatabaseHandle, gateway: Gateway, source: Source
) -> None:
    ingest(fresh_database, gateway.http, gateway.registry)
    reduced = source(lambda content: content.pop("wordings/MOTOR-TPL.md"))

    counts, _ = ingest(
        fresh_database,
        gateway.http,
        gateway.registry,
        source=reduced,
        sleep=Waits(also=gateway.clock.advance),
    )

    products = {r["product"] for r in stored(fresh_database)}
    assert products == {"HOME-PLUS", "HOME-STD", "MOTOR-COMP"}
    assert counts.documents == 3
    assert counts.chunks == chunk_count(fresh_database) < CHUNKS


def test_the_counts_are_the_documents_the_chunks_and_the_vectors_shape(
    fresh_database: DatabaseHandle, gateway: Gateway, source: Source
) -> None:
    one = source(
        lambda content: [content.pop(k) for k in list(content) if "HOME-STD" not in k]
    )

    counts, _ = ingest(fresh_database, gateway.http, gateway.registry, source=one)

    assert counts.documents == 1
    assert counts.chunks == chunk_count(fresh_database)
    assert (counts.deployment, counts.model, counts.dimensions) == (
        "replay-embedding",
        "replay-embedding",
        DIMENSIONS,
    )


def test_a_tenant_of_class_internal_may_ingest(
    fresh_database: DatabaseHandle,
    plant: Callable[..., Path],
    build_gateway: Callable[..., Gateway],
) -> None:
    directory = plant(
        (
            "tenants.yaml",
            "data_class: synthetic\n    agents: [claims-triage]",
            "data_class: internal\n    agents: [claims-triage, knowledge-ingestion]",
        )
    )
    gateway = build_gateway(directory)

    # The tenant's own token window is smaller than one ingestion, so the
    # gateway makes it wait; the fake clock moves instead of the test.
    counts, _ = ingest(
        fresh_database,
        gateway.http,
        gateway.registry,
        tenant="development",
        sleep=Waits(also=gateway.clock.advance),
    )

    assert counts.chunks == CHUNKS
