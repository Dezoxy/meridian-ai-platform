"""The claims tool server (S013): ``add_claim_note`` and ``request_approval``
write once per idempotency key, against real PostgreSQL."""

import logging
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from dbsupport import OWNER, DatabaseHandle
from jsonschema import Draft202012Validator
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, assert_spans_hold_no_exception_and_no_canary
from toolsupport import (
    AGENT,
    CANARY,
    CLAIM,
    KEY,
    OTHER_KEY,
    World,
    add_claim,
    add_run,
    audit_rows,
    payload_hash,
    run_call,
    seed_world,
    settings_for,
    table_rows,
    text_of,
)

from meridian.platform.common.db import connect
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.validation import build_validator, fits
from meridian.platform.toolserver.wire import META_CALL_ID, META_REFUSAL
from meridian.workloads.claims_triage.mcp_server.app import create_app

THREADS = 8
ROUNDS = 25
HOLD_SECONDS = 1.0


@dataclass(frozen=True)
class Store:
    tool: str
    table: str
    id_field: str
    text_field: str


STORES = [
    pytest.param(
        Store("add_claim_note", "claims.notes", "note_id", "note"), id="add_claim_note"
    ),
    pytest.param(
        Store("request_approval", "claims.approval_requests", "request_id", "reason"),
        id="request_approval",
    ),
]


def arguments(store: Store, text: str = "The first text.") -> dict[str, str]:
    return {"claim_id": CLAIM, store.text_field: text}


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def server(world: World, exporter: InMemorySpanExporter) -> Any:
    provider = make_tracer_provider("claims-mcp", exporter)
    return create_app(
        settings_for(world.db, "claims_mcp"), tracer_provider=provider
    ).server


@pytest.fixture
def fast_switching() -> Iterator[None]:
    """Switch threads about a million times a second, so a race shows up within
    a few rounds and not once in a million."""
    before = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(before)


def rows(world: World, store: Store) -> list[tuple]:
    return table_rows(world.db, store.table)


def columns(world: World, store: Store) -> dict[str, Any]:
    """The one stored row, by column name."""
    with connect(world.db.dsn(OWNER), "test-read") as conn:
        cursor = conn.execute(f"SELECT * FROM {store.table}")  # noqa: S608
        names = [column.name for column in cursor.description]
        (row,) = cursor.fetchall()
    return dict(zip(names, row, strict=True))


@pytest.mark.parametrize("store", STORES)
def test_a_call_stores_one_row_with_the_claim_the_run_and_the_agent(
    world: World, server: Any, store: Store
) -> None:
    given = arguments(store)

    result = run_call(server, store.tool, given, run_id=world.run_id, key=KEY)

    stored = columns(world, store)
    assert result.is_error is False
    assert result.structured_content == {
        store.id_field: str(stored[store.id_field]),
        "replayed": False,
    }
    assert text_of(result) == (
        f'{{"{store.id_field}":"{stored[store.id_field]}","replayed":false}}'
    )
    assert (stored["claim_id"], stored["run_id"], stored["agent"]) == (
        CLAIM,
        world.run_id,
        AGENT,
    )
    assert stored[store.text_field] == given[store.text_field]
    assert (stored["idempotency_key"], stored["payload_hash"]) == (
        KEY,
        payload_hash(given),
    )


@pytest.mark.parametrize("store", STORES)
def test_the_same_key_and_payload_again_answers_the_same_id_and_stores_nothing(
    world: World, server: Any, store: Store
) -> None:
    given = arguments(store)
    first = run_call(server, store.tool, given, run_id=world.run_id, key=KEY)

    again = run_call(server, store.tool, given, run_id=world.run_id, key=KEY)

    assert again.is_error is False
    assert again.structured_content == {
        store.id_field: first.structured_content[store.id_field],
        "replayed": True,
    }
    assert len(rows(world, store)) == 1
    assert [r["outcome"] for r in audit_rows(world.db)] == ["completed", "replayed"]
    assert audit_rows(world.db)[1]["call_id"] == uuid.UUID(again.meta[META_CALL_ID])


@pytest.mark.parametrize("store", STORES)
def test_the_same_key_with_another_text_is_refused_and_stores_nothing(
    world: World, server: Any, store: Store
) -> None:
    run_call(server, store.tool, arguments(store), run_id=world.run_id, key=KEY)

    result = run_call(
        server,
        store.tool,
        arguments(store, "Another text."),
        run_id=world.run_id,
        key=KEY,
    )

    assert result.is_error is True
    assert result.meta[META_REFUSAL] == "idempotency-key-reused"
    assert len(rows(world, store)) == 1
    assert columns(world, store)[store.text_field] == "The first text."
    last = audit_rows(world.db)[-1]
    assert (last["outcome"], last["reason"]) == ("refused", "idempotency-key-reused")


@pytest.mark.parametrize("store", STORES)
def test_the_same_key_from_another_run_is_another_row_and_says_nothing_of_the_first(
    world: World, server: Any, store: Store
) -> None:
    first = run_call(server, store.tool, arguments(store), run_id=world.run_id, key=KEY)
    other_run = add_run(world.db, CLAIM)

    result = run_call(server, store.tool, arguments(store), run_id=other_run, key=KEY)

    assert result.is_error is False
    assert result.structured_content["replayed"] is False
    assert (
        result.structured_content[store.id_field]
        != first.structured_content[store.id_field]
    )
    assert len(rows(world, store)) == 2


@pytest.mark.parametrize("store", STORES)
def test_another_text_under_the_key_of_another_run_is_another_row_too(
    world: World, server: Any, store: Store
) -> None:
    other_run = add_run(world.db, CLAIM)
    run_call(server, store.tool, arguments(store), run_id=world.run_id, key=KEY)

    # Not a refusal: that would tell the caller the key exists in another run.
    result = run_call(
        server,
        store.tool,
        arguments(store, "Another text."),
        run_id=other_run,
        key=KEY,
    )

    assert result.is_error is False
    assert len(rows(world, store)) == 2


@pytest.mark.parametrize("store", STORES)
def test_another_key_with_the_same_payload_stores_a_second_row(
    world: World, server: Any, store: Store
) -> None:
    run_call(server, store.tool, arguments(store), run_id=world.run_id, key=KEY)

    result = run_call(
        server, store.tool, arguments(store), run_id=world.run_id, key=OTHER_KEY
    )

    assert result.structured_content["replayed"] is False
    assert len(rows(world, store)) == 2


def race(
    server: Any, world: World, store: Store, given: dict[str, str], key: str
) -> tuple[list[Any], list[BaseException]]:
    """Send one call from each of eight threads at the same moment. A thread
    that is still alive after the join or died with any exception fails the
    test: it is not an answer that is missing, it is a result nobody saw."""
    answers: list[Any] = []
    errors: list[BaseException] = []
    start = threading.Barrier(THREADS)

    def send() -> None:
        start.wait(timeout=30)
        try:
            answers.append(
                run_call(server, store.tool, given, run_id=world.run_id, key=key)
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=send) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not [t for t in threads if t.is_alive()], "a racing thread did not finish"
    return answers, errors


@pytest.mark.parametrize("store", STORES)
@pytest.mark.usefixtures("fast_switching")
def test_eight_threads_sending_one_key_leave_one_row_and_all_get_its_id(
    world: World, server: Any, store: Store
) -> None:
    given = arguments(store)
    # A first use of the SDK's client builds its models; do that before the race.
    run_call(server, store.tool, {}, run_id=world.run_id)
    for round_number in range(ROUNDS):
        answers, errors = race(server, world, store, given, f"{round_number:064x}")

        assert errors == []
        assert len(answers) == THREADS
        assert len({a.structured_content[store.id_field] for a in answers}) == 1
        replayed = [a.structured_content["replayed"] for a in answers]
        assert sorted(replayed) == [False] + [True] * (THREADS - 1)
    assert len(rows(world, store)) == ROUNDS


@pytest.mark.parametrize("store", STORES)
def test_a_call_that_waits_for_an_uncommitted_row_answers_its_id_when_it_commits(
    world: World, server: Any, store: Store
) -> None:
    given = arguments(store)
    answers: list[Any] = []
    errors: list[BaseException] = []

    def send() -> None:
        try:
            answers.append(
                run_call(server, store.tool, given, run_id=world.run_id, key=KEY)
            )
        except BaseException as exc:
            errors.append(exc)

    # Build the SDK's models before the wait, so it is the row that holds the
    # call and not a first use.
    run_call(server, store.tool, {}, run_id=world.run_id)
    with connect(world.db.dsn(OWNER), "test-hold") as holder:
        (held,) = holder.execute(
            f"INSERT INTO {store.table} "  # noqa: S608
            f"(claim_id, run_id, agent, {store.text_field}, idempotency_key, "
            f"payload_hash) VALUES (%s, %s, %s, %s, %s, %s) RETURNING {store.id_field}",
            (
                CLAIM,
                world.run_id,
                AGENT,
                given[store.text_field],
                KEY,
                payload_hash(given),
            ),
        ).fetchone()
        caller = threading.Thread(target=send)
        caller.start()
        caller.join(timeout=HOLD_SECONDS)

        assert caller.is_alive(), "the call answered while the row was uncommitted"
        assert answers == [] and errors == []
        holder.commit()

    caller.join(timeout=30)
    assert not caller.is_alive()
    assert errors == []
    (answer,) = answers
    assert answer.structured_content == {store.id_field: str(held), "replayed": True}
    assert len(rows(world, store)) == 1
    assert audit_rows(world.db)[-1]["outcome"] == "replayed"


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a note\x00 with a NUL", id="nul"),
        pytest.param("a note \ud800 with a lone surrogate", id="lone-surrogate"),
    ],
)
def test_a_text_the_database_cannot_hold_is_refused_and_stores_nothing(
    world: World, server: Any, store: Store, text: str
) -> None:
    results = [
        run_call(
            server, store.tool, arguments(store, text), run_id=world.run_id, key=KEY
        )
        for _ in range(30)
    ]

    assert {r.meta[META_REFUSAL] for r in results} == {"invalid-arguments"}
    assert rows(world, store) == []
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "invalid-arguments")


# ── approval_outcome (S015): a read of the run's own recorded decision ──────
def record_decision(world: World, run_id: uuid.UUID, decision: str) -> None:
    with connect(world.db.dsn(OWNER), "test-seed") as conn:
        conn.execute(
            "INSERT INTO claims.decisions (claim_id, run_id, decision) "
            "VALUES (%s, %s, %s)",
            (CLAIM, run_id, decision),
        )


def outcome_of(server: Any, run_id: uuid.UUID, claim_id: str = CLAIM) -> Any:
    return run_call(server, "approval_outcome", {"claim_id": claim_id}, run_id=run_id)


OUTCOMES = ["approve", "reject", "request_documents", "send_back", "withdrawn"]


@pytest.mark.parametrize("decision", OUTCOMES)
def test_a_run_reads_the_decision_recorded_for_it(
    world: World, server: Any, decision: str
) -> None:
    record_decision(world, world.run_id, decision)

    result = outcome_of(server, world.run_id)

    assert result.is_error is False
    assert result.structured_content == {"outcome": decision}
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("completed", None)


def outcome_validator() -> Draft202012Validator:
    """The validator of ``approval_outcome``'s output schema, from the registry."""
    tool = load_registry(REGISTRY_DIR).tool("approval_outcome")
    assert tool is not None
    assert tool.output_schema is not None
    return build_validator(tool.output_schema)


@pytest.mark.parametrize("word", OUTCOMES)
def test_the_answer_for_each_outcome_fits_the_registry_s_output_schema(
    world: World, server: Any, word: str
) -> None:
    record_decision(world, world.run_id, word)

    result = outcome_of(server, world.run_id)

    assert result.structured_content == {"outcome": word}
    assert fits(outcome_validator(), result.structured_content)


def test_an_outcome_word_the_schema_does_not_list_is_not_an_answer() -> None:
    assert not fits(outcome_validator(), {"outcome": "cancelled"})


def test_a_run_with_no_recorded_decision_reads_no_outcome(
    world: World, server: Any
) -> None:
    result = outcome_of(server, world.run_id)

    assert result.is_error is False
    assert result.structured_content == {}
    assert [r["outcome"] for r in audit_rows(world.db)] == ["completed"]


def test_the_decision_recorded_for_another_run_of_the_claim_is_not_visible(
    world: World, server: Any
) -> None:
    record_decision(world, add_run(world.db, CLAIM), "reject")

    result = outcome_of(server, world.run_id)

    assert result.structured_content == {}


def test_a_call_naming_another_claim_is_refused_by_the_binding(
    world: World, server: Any
) -> None:
    add_claim(world.db, "CLM-0002")
    record_decision(world, world.run_id, "approve")

    result = outcome_of(server, world.run_id, claim_id="CLM-0002")

    assert result.is_error is True
    assert result.meta[META_REFUSAL] == "outside-claim"
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "outside-claim")


def test_a_read_needs_no_idempotency_key_and_writes_one_audit_row_per_call(
    world: World, server: Any
) -> None:
    outcome_of(server, world.run_id)
    outcome_of(server, world.run_id)

    assert [r["outcome"] for r in audit_rows(world.db)] == ["completed", "completed"]


@pytest.mark.parametrize("store", STORES)
def test_the_text_is_in_no_audit_row_span_or_log_line(
    world: World,
    server: Any,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
    store: Store,
) -> None:
    caplog.set_level(logging.DEBUG)
    given = arguments(store, CANARY)

    run_call(server, store.tool, given, run_id=world.run_id, key=KEY)
    run_call(server, store.tool, given, run_id=world.run_id, key=KEY)
    run_call(
        server,
        store.tool,
        arguments(store, CANARY + "!"),
        run_id=world.run_id,
        key=KEY,
    )

    assert len(audit_rows(world.db)) == 3
    assert CANARY not in repr(audit_rows(world.db))
    assert CANARY not in caplog.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)
