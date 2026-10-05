"""0015: the call's purpose on the refusal row of the Model Gateway (S058, T-03)."""

import hashlib
import uuid

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

MAX_LENGTH = 128
INSERT_PURPOSE = (
    "INSERT INTO audit.events (service, event, outcome, run_id, purpose) "
    "VALUES ('model-gateway', 'model.call', 'refused', %s, %s)"
)
INSERT_WITHOUT_PURPOSE = (
    "INSERT INTO audit.events (service, event, outcome, run_id) "
    "VALUES ('model-gateway', 'model.call', 'refused', %s)"
)


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def stored_purpose(db: DatabaseHandle, run_id: uuid.UUID) -> list:
    return run(
        db,
        OWNER,
        "SELECT purpose FROM audit.events WHERE run_id = %s",
        (run_id,),
    )


def test_the_migration_is_the_fifteenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert names[14] == "0015_audit_purpose.sql"
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


def test_the_purpose_column_is_nullable_text(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'audit' AND table_name = 'events' "
        "AND column_name = 'purpose'",
    )

    assert rows == [("purpose", "text", "YES")]


def test_the_gateway_role_inserts_the_purpose(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()

    run(migrated_database, "model_gateway", INSERT_PURPOSE, (run_id, "embedding"))

    assert stored_purpose(migrated_database, run_id) == [("embedding",)]


def test_a_row_that_leaves_the_purpose_out_holds_null(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()

    run(migrated_database, "model_gateway", INSERT_WITHOUT_PURPOSE, (run_id,))

    assert stored_purpose(migrated_database, run_id) == [(None,)]


def test_the_purpose_holds_128_characters_and_refuses_129(
    migrated_database: DatabaseHandle,
) -> None:
    at_limit, over_limit = uuid.uuid4(), uuid.uuid4()

    run(migrated_database, "model_gateway", INSERT_PURPOSE, (at_limit, "x" * 128))
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            "model_gateway",
            INSERT_PURPOSE,
            (over_limit, "x" * (MAX_LENGTH + 1)),
        )

    stored = run(
        migrated_database,
        OWNER,
        "SELECT run_id FROM audit.events WHERE run_id = ANY(%s)",
        ([at_limit, over_limit],),
    )
    assert stored == [(at_limit,)]
