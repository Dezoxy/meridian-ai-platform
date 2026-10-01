"""0002: the route columns on the audit row (S010, T-12)."""

import hashlib
import uuid

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

ROUTE_COLUMNS = ("reason", "data_class", "sku", "region", "residency")
MAX_LENGTH = 128
INSERT_ROUTE = (
    "INSERT INTO audit.events (service, event, outcome, run_id, "
    "reason, data_class, sku, region, residency) "
    "VALUES ('model-gateway', 'model.call', 'completed', %s, "
    "%s, %s, %s, %s, %s)"
)


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def insert_route(db: DatabaseHandle, run_id: uuid.UUID, **values: str) -> None:
    given = {name: values.get(name) for name in ROUTE_COLUMNS}
    run(db, "model_gateway", INSERT_ROUTE, (run_id, *given.values()))


def test_the_ledger_holds_both_migration_files(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert names[:2] == ["0001_schemas.sql", "0002_audit_route.sql"]
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


def test_the_five_route_columns_are_nullable_text(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'audit' AND table_name = 'events' "
        "AND column_name = ANY(%s) ORDER BY column_name",
        (list(ROUTE_COLUMNS),),
    )

    assert rows == [(name, "text", "YES") for name in sorted(ROUTE_COLUMNS)]


def test_the_gateway_role_inserts_the_route_columns(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()

    insert_route(
        migrated_database,
        run_id,
        reason="no-allowed-deployment",
        data_class="personal",
        sku="Standard",
        region="swedencentral",
        residency="eu-region",
    )

    assert run(
        migrated_database,
        OWNER,
        "SELECT reason, data_class, sku, region, residency "
        "FROM audit.events WHERE run_id = %s",
        (run_id,),
    ) == [
        ("no-allowed-deployment", "personal", "Standard", "swedencentral", "eu-region")
    ]


def test_a_row_that_leaves_the_route_columns_out_holds_nulls(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()

    insert_route(migrated_database, run_id)

    assert run(
        migrated_database,
        OWNER,
        "SELECT reason, data_class, sku, region, residency "
        "FROM audit.events WHERE run_id = %s",
        (run_id,),
    ) == [(None, None, None, None, None)]


@pytest.mark.parametrize("column", ROUTE_COLUMNS)
def test_each_route_column_holds_128_characters_and_refuses_129(
    migrated_database: DatabaseHandle, column: str
) -> None:
    at_limit, over_limit = uuid.uuid4(), uuid.uuid4()

    insert_route(migrated_database, at_limit, **{column: "x" * MAX_LENGTH})
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_route(migrated_database, over_limit, **{column: "x" * (MAX_LENGTH + 1)})

    stored = run(
        migrated_database,
        OWNER,
        "SELECT run_id FROM audit.events WHERE run_id = ANY(%s)",
        ([at_limit, over_limit],),
    )
    assert stored == [(at_limit,)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.events SET reason = 'x'",
        "UPDATE audit.events SET residency = 'x'",
        "DELETE FROM audit.events",
    ],
)
def test_the_gateway_role_still_cannot_update_or_delete_a_row(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, "model_gateway", statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.events SET reason = 'x'",
        "DELETE FROM audit.events",
    ],
)
def test_even_the_owner_cannot_change_a_row_with_route_columns(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    insert_route(migrated_database, uuid.uuid4(), reason="timeout")

    with pytest.raises(psycopg.errors.RaiseException):
        run(migrated_database, OWNER, statement)
