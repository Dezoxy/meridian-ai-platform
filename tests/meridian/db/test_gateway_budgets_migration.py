"""0003: the gateway's counters and usage table, and four audit columns (S011)."""

import hashlib
import uuid
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import apply_migrations, migration_files

MAX_LENGTH = 128
CALL_ID = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
TEXT_COLUMNS = ("tenant", "agent", "deployment", "provider", "model")

INSERT_USAGE = (
    "INSERT INTO gateway.usage (call_id, tenant, agent, run_id, deployment, "
    "provider, model, day, month, reserved_tokens, reserved_micro_eur, "
    "charged_tokens, charged_micro_eur) "
    "VALUES (%(call_id)s, %(tenant)s, %(agent)s, %(run_id)s, %(deployment)s, "
    "%(provider)s, %(model)s, '2026-10-01', '2026-10-01', %(reserved_tokens)s, "
    "%(reserved_micro_eur)s, %(charged_tokens)s, %(charged_micro_eur)s)"
)
USAGE_DEFAULTS = {
    "tenant": "development",
    "agent": "claims-triage",
    "run_id": RUN_ID,
    "deployment": "aoai-sdc-gpt-4o",
    "provider": "azure-openai",
    "model": "gpt-4o",
    "reserved_tokens": 100,
    "reserved_micro_eur": 5,
    "charged_tokens": 100,
    "charged_micro_eur": 5,
}


def run(db: DatabaseHandle, role: str, statement: str, params: Any = ()) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def insert_usage(db: DatabaseHandle, **values: object) -> None:
    row = {"call_id": uuid.uuid4()} | USAGE_DEFAULTS | values
    run(db, "model_gateway", INSERT_USAGE, row)


def insert_counter(db: DatabaseHandle, **values: object) -> None:
    row = {
        "tenant": "development",
        "kind": "tokens-day",
        "period_start": "2026-10-01",
    } | values
    run(
        db,
        "model_gateway",
        "INSERT INTO gateway.budget_counters (tenant, kind, period_start) "
        "VALUES (%(tenant)s, %(kind)s, %(period_start)s)",
        row,
    )


def columns(db: DatabaseHandle, schema: str, table: str) -> list[tuple]:
    return run(
        db,
        OWNER,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
        (schema, table),
    )


def test_the_ledger_holds_the_third_migration_file(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert names[:3] == [
        "0001_schemas.sql",
        "0002_audit_route.sql",
        "0003_gateway_budgets.sql",
    ]
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


def test_the_counters_table_has_its_four_columns(
    migrated_database: DatabaseHandle,
) -> None:
    assert columns(migrated_database, "gateway", "budget_counters") == [
        ("amount", "bigint", "NO"),
        ("kind", "text", "NO"),
        ("period_start", "date", "NO"),
        ("tenant", "text", "NO"),
    ]


def test_the_usage_table_has_its_columns(migrated_database: DatabaseHandle) -> None:
    assert columns(migrated_database, "gateway", "usage") == [
        ("agent", "text", "NO"),
        ("attempt_id", "uuid", "NO"),
        ("call_id", "uuid", "NO"),
        ("charged_micro_eur", "bigint", "NO"),
        ("charged_tokens", "bigint", "NO"),
        ("closed_at", "timestamp with time zone", "YES"),
        ("day", "date", "NO"),
        ("deployment", "text", "NO"),
        ("input_tokens", "integer", "YES"),
        ("model", "text", "NO"),
        ("month", "date", "NO"),
        ("output_tokens", "integer", "YES"),
        ("provider", "text", "NO"),
        ("reserved_at", "timestamp with time zone", "NO"),
        ("reserved_micro_eur", "bigint", "NO"),
        ("reserved_tokens", "bigint", "NO"),
        ("run_id", "uuid", "NO"),
        ("state", "text", "NO"),
        ("tenant", "text", "NO"),
    ]


def test_the_four_audit_columns_are_nullable(
    migrated_database: DatabaseHandle,
) -> None:
    rows = [
        row
        for row in columns(migrated_database, "audit", "events")
        if row[0] in {"call_id", "http_status", "provider_model", "suppressed"}
    ]

    assert rows == [
        ("call_id", "uuid", "YES"),
        ("http_status", "integer", "YES"),
        ("provider_model", "text", "YES"),
        ("suppressed", "integer", "YES"),
    ]


def test_the_counter_row_is_keyed_by_tenant_kind_and_period(
    migrated_database: DatabaseHandle,
) -> None:
    insert_counter(migrated_database, tenant="key-test")
    insert_counter(migrated_database, tenant="key-test", kind="cost-month")
    insert_counter(migrated_database, tenant="key-test", period_start="2026-10-02")

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_counter(migrated_database, tenant="key-test")


def test_a_counter_starts_at_zero(migrated_database: DatabaseHandle) -> None:
    insert_counter(migrated_database, tenant="zero-test")

    assert run(
        migrated_database,
        OWNER,
        "SELECT amount FROM gateway.budget_counters WHERE tenant = 'zero-test'",
    ) == [(0,)]


def test_a_counter_kind_outside_the_two_is_refused(
    migrated_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_counter(migrated_database, kind="requests-day")


def test_a_negative_counter_is_refused(migrated_database: DatabaseHandle) -> None:
    insert_counter(migrated_database, tenant="negative-test")

    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            "model_gateway",
            "UPDATE gateway.budget_counters SET amount = amount - 1 "
            "WHERE tenant = 'negative-test'",
        )


def test_a_counter_tenant_holds_128_characters_and_refuses_129(
    migrated_database: DatabaseHandle,
) -> None:
    insert_counter(migrated_database, tenant="x" * MAX_LENGTH)

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_counter(migrated_database, tenant="x" * (MAX_LENGTH + 1))


def test_a_usage_row_gets_an_attempt_id_and_starts_reserved(
    migrated_database: DatabaseHandle,
) -> None:
    call_id = uuid.uuid4()

    insert_usage(migrated_database, call_id=call_id)

    ((attempt_id, state, reserved_at, closed_at, input_tokens),) = run(
        migrated_database,
        OWNER,
        "SELECT attempt_id, state, reserved_at, closed_at, input_tokens "
        "FROM gateway.usage WHERE call_id = %s",
        (call_id,),
    )
    assert isinstance(attempt_id, uuid.UUID)
    assert state == "reserved"
    assert reserved_at.year >= 2026
    assert (closed_at, input_tokens) == (None, None)


def test_a_call_can_be_reserved_once_per_deployment(
    migrated_database: DatabaseHandle,
) -> None:
    call_id = uuid.uuid4()
    insert_usage(migrated_database, call_id=call_id)
    insert_usage(migrated_database, call_id=call_id, deployment="aoai-sdc-gpt-4o-b")

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_usage(migrated_database, call_id=call_id)


@pytest.mark.parametrize("column", TEXT_COLUMNS)
def test_each_usage_text_column_holds_128_characters_and_refuses_129(
    migrated_database: DatabaseHandle, column: str
) -> None:
    insert_usage(migrated_database, **{column: "x" * MAX_LENGTH})

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_usage(migrated_database, **{column: "x" * (MAX_LENGTH + 1)})


@pytest.mark.parametrize(
    "column",
    ["reserved_tokens", "reserved_micro_eur", "charged_tokens", "charged_micro_eur"],
)
def test_a_negative_usage_amount_is_refused(
    migrated_database: DatabaseHandle, column: str
) -> None:
    insert_usage(migrated_database, **{column: 0})

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_usage(migrated_database, **{column: -1})


def test_a_usage_state_outside_the_four_is_refused(
    migrated_database: DatabaseHandle,
) -> None:
    insert_usage(migrated_database)
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            OWNER,
            "UPDATE gateway.usage SET state = 'lost' WHERE tenant = 'development'",
        )


def index_definitions(db: DatabaseHandle) -> dict[str, str]:
    rows = run(
        db,
        OWNER,
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'gateway' AND tablename = 'usage'",
    )
    return dict(rows)


def test_the_usage_table_indexes_the_open_reservations_and_not_the_tenant_day(
    migrated_database: DatabaseHandle,
) -> None:
    definitions = index_definitions(migrated_database)

    assert "usage_tenant_day_idx" not in definitions
    assert "(reserved_at)" in definitions["usage_open_idx"]
    assert "state = 'reserved'" in definitions["usage_open_idx"]


CLOSE = (
    "UPDATE gateway.usage SET state = %(state)s, closed_at = now() "
    "WHERE attempt_id = %(attempt_id)s"
)


def reserved_attempt(db: DatabaseHandle) -> uuid.UUID:
    call_id = uuid.uuid4()
    insert_usage(db, call_id=call_id)
    ((attempt_id,),) = run(
        db, OWNER, "SELECT attempt_id FROM gateway.usage WHERE call_id = %s", (call_id,)
    )
    return attempt_id


@pytest.mark.parametrize("state", ["settled", "released", "kept"])
def test_a_reserved_row_closes_into_each_closing_state(
    migrated_database: DatabaseHandle, state: str
) -> None:
    attempt_id = reserved_attempt(migrated_database)

    run(
        migrated_database,
        "model_gateway",
        CLOSE,
        {"state": state, "attempt_id": attempt_id},
    )

    assert run(
        migrated_database,
        OWNER,
        "SELECT state, closed_at IS NOT NULL FROM gateway.usage WHERE attempt_id = %s",
        (attempt_id,),
    ) == [(state, True)]


@pytest.mark.parametrize("role", ["model_gateway", OWNER])
@pytest.mark.parametrize("closed_as", ["settled", "released", "kept"])
def test_a_closed_row_cannot_be_updated_by_the_gateway_or_the_owner(
    migrated_database: DatabaseHandle, role: str, closed_as: str
) -> None:
    attempt_id = reserved_attempt(migrated_database)
    run(
        migrated_database,
        "model_gateway",
        CLOSE,
        {"state": closed_as, "attempt_id": attempt_id},
    )

    with pytest.raises(psycopg.errors.RaiseException, match="only a reserved row"):
        run(
            migrated_database,
            role,
            "UPDATE gateway.usage SET charged_tokens = 0 WHERE attempt_id = %s",
            (attempt_id,),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="only a reserved row"):
        run(
            migrated_database,
            role,
            CLOSE,
            {"state": "settled", "attempt_id": attempt_id},
        )

    assert run(
        migrated_database,
        OWNER,
        "SELECT state, charged_tokens FROM gateway.usage WHERE attempt_id = %s",
        (attempt_id,),
    ) == [(closed_as, 100)]


@pytest.mark.parametrize("role", ["model_gateway", OWNER])
def test_a_closed_row_cannot_be_reopened(
    migrated_database: DatabaseHandle, role: str
) -> None:
    attempt_id = reserved_attempt(migrated_database)
    run(
        migrated_database,
        "model_gateway",
        CLOSE,
        {"state": "kept", "attempt_id": attempt_id},
    )

    with pytest.raises(psycopg.errors.RaiseException):
        run(
            migrated_database,
            role,
            "UPDATE gateway.usage SET state = 'reserved', closed_at = NULL "
            "WHERE attempt_id = %s",
            (attempt_id,),
        )

    assert run(
        migrated_database,
        OWNER,
        "SELECT state FROM gateway.usage WHERE attempt_id = %s",
        (attempt_id,),
    ) == [("kept",)]


def test_a_reserved_row_is_not_changed_without_being_closed(
    migrated_database: DatabaseHandle,
) -> None:
    attempt_id = reserved_attempt(migrated_database)

    with pytest.raises(psycopg.errors.RaiseException):
        run(
            migrated_database,
            "model_gateway",
            "UPDATE gateway.usage SET charged_tokens = 1 WHERE attempt_id = %s",
            (attempt_id,),
        )


def test_the_trigger_function_is_not_callable_by_public(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        "model_gateway",
        "SELECT has_function_privilege('gateway.forbid_reopen()', 'EXECUTE')",
    )

    assert rows == [(False,)]


def test_closing_without_a_closing_time_is_refused(
    migrated_database: DatabaseHandle,
) -> None:
    attempt_id = reserved_attempt(migrated_database)

    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            "model_gateway",
            "UPDATE gateway.usage SET state = 'settled' WHERE attempt_id = %s",
            (attempt_id,),
        )


@pytest.mark.parametrize(
    ("state", "closed", "accepted"),
    [
        ("reserved", False, True),
        ("settled", True, True),
        ("released", True, True),
        ("kept", True, True),
        ("reserved", True, False),
        ("settled", False, False),
        ("released", False, False),
        ("kept", False, False),
    ],
)
def test_closed_at_is_set_exactly_when_the_state_is_not_reserved(
    migrated_database: DatabaseHandle, state: str, closed: bool, accepted: bool
) -> None:
    def insert() -> None:
        run(
            migrated_database,
            OWNER,
            "INSERT INTO gateway.usage (call_id, tenant, agent, run_id, deployment, "
            "provider, model, day, month, reserved_tokens, reserved_micro_eur, "
            "charged_tokens, charged_micro_eur, state, closed_at) VALUES "
            "(gen_random_uuid(), 't', 'a', gen_random_uuid(), 'd', 'p', 'm', "
            "'2026-10-01', '2026-10-01', 1, 1, 1, 1, %s, "
            "CASE WHEN %s THEN now() END)",
            (state, closed),
        )

    if accepted:
        insert()
    else:
        with pytest.raises(psycopg.errors.CheckViolation):
            insert()


def test_the_suppressed_count_is_a_nullable_integer_that_is_not_negative(
    migrated_database: DatabaseHandle,
) -> None:
    def insert(suppressed: int | None) -> None:
        run(
            migrated_database,
            "model_gateway",
            "INSERT INTO audit.events (service, event, outcome, suppressed) "
            "VALUES ('model-gateway', 'model.call', 'refused', %s)",
            (suppressed,),
        )

    assert ("suppressed", "integer", "YES") in columns(
        migrated_database, "audit", "events"
    )
    insert(None)
    insert(0)
    insert(19)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(-1)


@pytest.mark.parametrize("status", [100, 599])
def test_an_http_status_inside_100_to_599_is_stored(
    migrated_database: DatabaseHandle, status: int
) -> None:
    run_id = uuid.uuid4()

    run(
        migrated_database,
        "model_gateway",
        "INSERT INTO audit.events (service, event, outcome, run_id, call_id, "
        "http_status, provider_model) VALUES ('model-gateway', 'model.call', "
        "'failed', %s, %s, %s, 'gpt-4o-2024-11-20')",
        (run_id, CALL_ID, status),
    )

    assert run(
        migrated_database,
        OWNER,
        "SELECT call_id, http_status, provider_model FROM audit.events "
        "WHERE run_id = %s",
        (run_id,),
    ) == [(CALL_ID, status, "gpt-4o-2024-11-20")]


@pytest.mark.parametrize("status", [99, 600, 0, -1])
def test_an_http_status_outside_100_to_599_is_refused(
    migrated_database: DatabaseHandle, status: int
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            "model_gateway",
            "INSERT INTO audit.events (service, event, outcome, http_status) "
            "VALUES ('model-gateway', 'model.call', 'failed', %s)",
            (status,),
        )


def test_the_provider_model_holds_128_characters_and_refuses_129(
    migrated_database: DatabaseHandle,
) -> None:
    def insert(length: int) -> None:
        run(
            migrated_database,
            "model_gateway",
            "INSERT INTO audit.events (service, event, outcome, provider_model) "
            "VALUES ('model-gateway', 'model.call', 'completed', %s)",
            ("x" * length,),
        )

    insert(MAX_LENGTH)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(MAX_LENGTH + 1)


def test_0003_applies_on_a_database_that_has_0001_and_0002_with_audit_rows(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    assert [name for name, _ in files[:3]] == [
        "0001_schemas.sql",
        "0002_audit_route.sql",
        "0003_gateway_budgets.sql",
    ]
    monkeypatch.setattr(runner, "migration_files", lambda: files[:2])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        assert len(apply_migrations(conn)) == 2
    old_run = uuid.uuid4()
    run(
        empty_database,
        "model_gateway",
        "INSERT INTO audit.events (service, event, outcome, run_id, reason) "
        "VALUES ('model-gateway', 'model.call', 'refused', %s, 'timeout')",
        (old_run,),
    )
    monkeypatch.undo()

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = apply_migrations(conn)

    assert applied == [name for name, _ in migration_files()[2:]]
    assert applied[0] == "0003_gateway_budgets.sql"
    assert run(
        empty_database,
        OWNER,
        "SELECT reason, call_id, http_status, provider_model, suppressed "
        "FROM audit.events WHERE run_id = %s",
        (old_run,),
    ) == [("timeout", None, None, None, None)]
    assert run(empty_database, OWNER, "SELECT count(*) FROM gateway.usage") == [(0,)]
