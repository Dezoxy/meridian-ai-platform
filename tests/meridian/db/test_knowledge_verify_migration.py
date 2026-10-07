"""0026: the ingestion's role may read what ``meridian knowledge verify`` compares
(S067, T-27, T-57).

The migration is found by the end of its name, never by its number: the number
is provisional until the pull request merges.
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import (
    INGEST_ROLE,
    OWNER,
    SEED_ROLE,
    SERVICE_ROLES,
    UPKEEP_ROLE,
    DatabaseHandle,
)
from psycopg import sql
from sweepmigrationsupport import INSUFFICIENT_PRIVILEGE, privileges, refused, run

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

SUFFIX = "_knowledge_verify.sql"
ROLE_CHECK = "required_role text := 'knowledge_ingest'"
# The columns the check reads: the key of a clause, what a search returns of it
# and the hash of the file it came from.
VERIFY_COLUMNS = (
    "product",
    "wording_version",
    "clause",
    "section",
    "title",
    "body",
    "source_sha256",
)
VERIFY_HOLDS = frozenset(
    ("column", "knowledge.chunks", "SELECT", column) for column in VERIFY_COLUMNS
)
OTHER_ROLES = (*SERVICE_ROLES, UPKEEP_ROLE, SEED_ROLE)
PLATFORM_SCHEMAS = ("audit", "claims", "gateway", "knowledge", "policy", "runtime")


def migration_index() -> int:
    (index,) = [
        position
        for position, (name, _) in enumerate(migration_files())
        if name.endswith(SUFFIX)
    ]
    return index


def migration_name() -> str:
    return migration_files()[migration_index()][0]


def migration_text() -> str:
    return dict(migration_files())[migration_name()]


def apply_everything_before(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str]]:
    """Apply the files before this one as the owner; return all of the files."""
    files = migration_files()
    before = files[: migration_index()]
    monkeypatch.setattr(runner, "migration_files", lambda: before)
    with connect(db.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    return files


def apply_with_role_check(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Apply the files before this one, then this one naming ``role`` where it
    names the ingestion's role. Raises what the file raises."""
    files = apply_everything_before(db, monkeypatch)
    index = migration_index()
    name, text = files[index]
    swapped = text.replace(ROLE_CHECK, f"required_role text := '{role}'")
    assert swapped != text
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:index], (name, swapped)]
    )
    with connect(db.dsn(OWNER), "test") as conn:
        try:
            runner.apply_migrations(conn)
        finally:
            conn.rollback()


@contextmanager
def probe_role(db: DatabaseHandle, attributes: str) -> Iterator[str]:
    """A login-less role of its own with ``attributes``, dropped afterwards:
    roles are cluster-wide, so a test never alters ``knowledge_ingest``."""
    name = f"verify_probe_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(db.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN {}").format(
                sql.Identifier(name), sql.SQL(attributes)
            )
        )
        try:
            yield name
        finally:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def test_the_migration_is_recorded(migrated_database: DatabaseHandle) -> None:
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert (migration_name(),) in recorded


def test_the_migration_gives_the_ingestion_the_seven_columns_and_changes_no_other_role(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    ingest_before = privileges(empty_database, INGEST_ROLE)
    others_before = {role: privileges(empty_database, role) for role in OTHER_ROLES}
    monkeypatch.setattr(
        runner, "migration_files", lambda: files[: migration_index() + 1]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == [migration_name()]
    assert privileges(empty_database, INGEST_ROLE) == ingest_before | VERIFY_HOLDS
    assert VERIFY_HOLDS.isdisjoint(ingest_before)
    for role in OTHER_ROLES:
        assert privileges(empty_database, role) == others_before[role], role


def test_the_role_reads_the_columns_the_check_compares(
    migrated_database: DatabaseHandle,
) -> None:
    columns = ", ".join(VERIFY_COLUMNS)

    rows = run(
        migrated_database,
        INGEST_ROLE,
        f"SELECT {columns} FROM knowledge.chunks",  # noqa: S608
    )

    assert rows == []  # no corpus here: the statement ran, which is the point


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM knowledge.chunks",
        "SELECT embedding FROM knowledge.chunks",
        "SELECT lexemes FROM knowledge.chunks",
        "SELECT model FROM knowledge.chunks",
        "SELECT deployment FROM knowledge.chunks",
        "SELECT dimensions FROM knowledge.chunks",
        "SELECT ingested_at FROM knowledge.chunks",
        "UPDATE knowledge.chunks SET body = body",
        "TRUNCATE knowledge.chunks",
    ],
)
def test_the_role_is_refused_every_other_column_and_every_other_change(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    assert refused(migrated_database, INGEST_ROLE, statement) == INSUFFICIENT_PRIVILEGE


def test_the_role_still_deletes_chunks_and_still_cannot_read_the_log(
    migrated_database: DatabaseHandle,
) -> None:
    run(migrated_database, INGEST_ROLE, "DELETE FROM knowledge.chunks")

    statement = "SELECT count(*) FROM audit.events"
    assert refused(migrated_database, INGEST_ROLE, statement) == INSUFFICIENT_PRIVILEGE


def test_a_missing_role_fails_clearly_and_grants_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        psycopg.Error,
        match=(
            "required role role_that_does_not_exist does not exist; "
            "create it out of band before migrating"
        ),
    ):
        apply_with_role_check(empty_database, monkeypatch, "role_that_does_not_exist")

    assert privileges(empty_database, INGEST_ROLE).isdisjoint(VERIFY_HOLDS)


@pytest.mark.parametrize(
    ("attributes", "named"),
    [
        pytest.param("SUPERUSER", "SUPERUSER", id="superuser"),
        pytest.param("BYPASSRLS", "BYPASSRLS", id="bypassrls"),
        pytest.param("CREATEROLE", "CREATEROLE", id="createrole"),
        pytest.param("CREATEDB", "CREATEDB", id="createdb"),
        pytest.param("REPLICATION", "REPLICATION", id="replication"),
    ],
)
def test_a_role_with_more_than_a_plain_login_is_refused_and_nothing_is_granted(
    empty_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    attributes: str,
    named: str,
) -> None:
    with (
        probe_role(empty_database, attributes) as probe,
        pytest.raises(psycopg.Error) as caught,
    ):
        apply_with_role_check(empty_database, monkeypatch, probe)

    message = caught.value.diag.message_primary or ""
    assert f"role {probe} must not hold" in message
    assert named in message
    assert privileges(empty_database, INGEST_ROLE).isdisjoint(VERIFY_HOLDS)


def test_a_plain_role_passes_the_role_check(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control of the refusals above: a role with none of the attributes goes
    # through, so a refusal is about the attribute.
    with probe_role(empty_database, "") as probe:
        apply_with_role_check(empty_database, monkeypatch, probe)


def test_a_migration_run_by_a_role_that_does_not_own_the_schema_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema knowledge"
        ):
            conn.execute(migration_text())
        conn.rollback()

    assert privileges(empty_database, INGEST_ROLE).isdisjoint(VERIFY_HOLDS)


def test_the_file_takes_no_lock_on_a_table_that_exists_before_it(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn(OWNER), "test") as conn:
        existing = [
            oid
            for (oid,) in conn.execute(
                "SELECT c.oid FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname = ANY(%s)",
                (list(PLATFORM_SCHEMAS),),
            )
        ]
        conn.execute(migration_text())
        locked = conn.execute(
            "SELECT l.relation::regclass::text, l.mode FROM pg_locks l "
            "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
            "AND l.relation = ANY(%s::oid[])",
            (existing,),
        ).fetchall()
        conn.rollback()

    assert existing
    assert locked == []


def header_text() -> str:
    head = migration_text().split("DO $$")[0]
    return " ".join(line.removeprefix("--").strip() for line in head.splitlines())


def test_the_header_names_every_column_and_says_why_the_embedding_is_left_out() -> None:
    header = header_text()

    for column in VERIFY_COLUMNS:
        assert column in header, column
    assert "embedding" in header
    assert "no lock" in header
    assert "lock_timeout" in header


def test_the_undo_the_header_gives_takes_the_grant_back(
    fresh_database: DatabaseHandle,
) -> None:
    (statement,) = re.findall(r"REVOKE [^;]+;", header_text())

    assert privileges(fresh_database, INGEST_ROLE) >= VERIFY_HOLDS
    run(fresh_database, OWNER, statement)
    assert privileges(fresh_database, INGEST_ROLE).isdisjoint(VERIFY_HOLDS)
