"""The text columns of ``runtime.runs`` are bounded to what the HTTP edge admits
(S059)."""

import hashlib
import uuid

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

MAX_LENGTH = 64
BOUNDED_COLUMNS = ("agent", "tenant", "reference")
INSERT_RUN = (
    "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, status) "
    "VALUES (%s, %s, %s, %s, %s, 'Running')"
)


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def insert_run(db: DatabaseHandle, run_id: uuid.UUID, **values: str) -> None:
    columns = dict.fromkeys(BOUNDED_COLUMNS, "x") | values
    run(
        db,
        "agent_runtime",
        INSERT_RUN,
        (run_id, uuid.uuid4(), *(columns[name] for name in BOUNDED_COLUMNS)),
    )


def test_the_migration_is_recorded(migrated_database: DatabaseHandle) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert "0016_runs_text_bounds.sql" in names
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


@pytest.mark.parametrize("column", BOUNDED_COLUMNS)
def test_a_text_column_of_a_run_holds_64_characters_and_refuses_65(
    migrated_database: DatabaseHandle, column: str
) -> None:
    at_limit, over_limit = uuid.uuid4(), uuid.uuid4()

    insert_run(migrated_database, at_limit, **{column: "x" * MAX_LENGTH})
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_run(migrated_database, over_limit, **{column: "x" * (MAX_LENGTH + 1)})

    stored = run(
        migrated_database,
        OWNER,
        "SELECT run_id FROM runtime.runs WHERE run_id = ANY(%s)",
        ([at_limit, over_limit],),
    )
    assert stored == [(at_limit,)]


@pytest.mark.parametrize("column", BOUNDED_COLUMNS)
def test_the_bound_counts_characters_not_bytes(
    migrated_database: DatabaseHandle, column: str
) -> None:
    run_id = uuid.uuid4()

    insert_run(migrated_database, run_id, **{column: "é" * MAX_LENGTH})

    stored = run(
        migrated_database,
        OWNER,
        f"SELECT {column} FROM runtime.runs WHERE run_id = %s",  # noqa: S608
        (run_id,),
    )
    assert stored == [("é" * MAX_LENGTH,)]
