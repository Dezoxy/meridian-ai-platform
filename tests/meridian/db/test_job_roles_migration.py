"""0022: a role of its own for the seed and for the ingestion (S063, T-25).

The migration is found by the end of its name, never by its number or its place
in the list: the number is provisional until the pull request merges, and the
file is the last only until another one lands after it.
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import (
    INGEST_ROLE,
    JOB_ROLES,
    OWNER,
    SEED_ROLE,
    SERVICE_ROLES,
    UPKEEP_ROLE,
    DatabaseHandle,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sweepmigrationsupport import INSUFFICIENT_PRIVILEGE, privileges, run

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files
from meridian.platform.policy_mcp.seed import UPSERT_HISTORY, UPSERT_POLICY

SUFFIX = "_job_roles.sql"
ROLE_CHECK = "ARRAY['policy_seed', 'knowledge_ingest']"
PLATFORM_SCHEMAS = ("audit", "claims", "gateway", "knowledge", "policy", "runtime")
POLICY_COLUMNS = (
    "policy_number",
    "product",
    "wording_version",
    "start_date",
    "end_date",
    "status",
    "lapsed_on",
    "deductible",
    "sum_insured",
    "cover_limit",
)
HISTORY_COLUMNS = (
    "history_id",
    "policy_number",
    "loss_date",
    "peril",
    "paid_amount",
    "status",
)
# The columns each upsert's SET names: all but the key.
POLICY_UPDATABLE = POLICY_COLUMNS[1:]
HISTORY_UPDATABLE = HISTORY_COLUMNS[1:]
OTHER_ROLES = (*SERVICE_ROLES, UPKEEP_ROLE)


def table_holds(table: str, privileges_held: tuple[str, ...]) -> set[tuple]:
    return {("table", table, privilege, "") for privilege in privileges_held}


def column_holds(table: str, privilege: str, columns: tuple[str, ...]) -> set[tuple]:
    return {("column", table, privilege, column) for column in columns}


# What each role holds: the table-level rights, the column rights that follow
# from them (a column is readable when its table is), and the update of columns.
SEED_HOLDS = frozenset(
    {
        ("schema", "policy", "USAGE", ""),
        *table_holds("policy.policies", ("SELECT", "INSERT", "DELETE")),
        *table_holds("policy.claim_history", ("SELECT", "INSERT", "DELETE")),
        *column_holds("policy.policies", "SELECT", POLICY_COLUMNS),
        *column_holds("policy.claim_history", "SELECT", HISTORY_COLUMNS),
        *column_holds("policy.policies", "UPDATE", POLICY_UPDATABLE),
        *column_holds("policy.claim_history", "UPDATE", HISTORY_UPDATABLE),
    }
)
INGEST_HOLDS = frozenset(
    {
        ("schema", "audit", "USAGE", ""),
        ("schema", "knowledge", "USAGE", ""),
        *table_holds("knowledge.chunks", ("INSERT", "DELETE")),
        *table_holds("audit.events", ("INSERT",)),
    }
)
HOLDS = {SEED_ROLE: SEED_HOLDS, INGEST_ROLE: INGEST_HOLDS}
# What a fully migrated database gives the ingestion: 0022's rights and the read
# of seven columns that 0026 added for `meridian knowledge verify`
# (test_knowledge_verify_migration.py holds that file's own grant).
VERIFY_COLUMNS = (
    "product",
    "wording_version",
    "clause",
    "section",
    "title",
    "body",
    "source_sha256",
)
MIGRATED_HOLDS = {
    SEED_ROLE: SEED_HOLDS,
    INGEST_ROLE: INGEST_HOLDS
    | column_holds("knowledge.chunks", "SELECT", VERIFY_COLUMNS),
}
TABLES = """
SELECT n.nspname || '.' || c.relname, c.relkind::text,
    (SELECT a.attname FROM pg_attribute AS a
     WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
     ORDER BY a.attnum LIMIT 1)
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v')
ORDER BY 1
"""
EXECUTORS = """
SELECT p.oid::regprocedure::text
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = ANY(%(schemas)s)
    AND has_function_privilege(%(role)s, p.oid, 'EXECUTE')
ORDER BY 1
"""


def migration_index() -> int:
    (index,) = [
        position
        for position, (name, _) in enumerate(migration_files())
        if name.endswith(SUFFIX)
    ]
    return index


def migration_name() -> str:
    return migration_files()[migration_index()][0]


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
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    """Apply the files before this one, then this one with ``check`` in place of
    its role check, as the owner. Raises what the file raises."""
    files = apply_everything_before(db, monkeypatch)
    index = migration_index()
    name, text = files[index]
    swapped = text.replace(ROLE_CHECK, check)
    assert swapped != text
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:index], (name, swapped)]
    )
    with connect(db.dsn(OWNER), "test") as conn:
        try:
            runner.apply_migrations(conn)
        finally:
            conn.rollback()


def assert_nothing_was_granted(db: DatabaseHandle) -> None:
    for role in JOB_ROLES:
        assert privileges(db, role) == frozenset(), role


@contextmanager
def probe_role(
    db: DatabaseHandle, attributes: str, member_of: str | None = None
) -> Iterator[str]:
    """A login-less role of its own, with ``attributes``, that is a member of
    ``member_of``; dropped when the block ends. Roles are cluster-wide and the
    workers share one server, so a test never alters ``policy_seed``: it makes a
    role like it, under a name of its own, and swaps the name into the check."""
    name = f"job_roles_probe_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(db.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN {}").format(
                sql.Identifier(name), sql.SQL(attributes)
            )
        )
        try:
            if member_of is not None:
                admin.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(member_of), sql.Identifier(name)
                    )
                )
            yield name
        finally:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


@contextmanager
def owned_table(db: DatabaseHandle, owner: str) -> Iterator[str]:
    """A table in ``public`` of the test database that ``owner`` owns; dropped
    when the block ends. Done as the administrator, in the test database itself:
    ``public`` exists before any migration, and an object is owned in one
    database only, so it must go before the role can be dropped."""
    name = f"public.owned_by_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(
        make_conninfo(db.admin_dsn, dbname=db.name), autocommit=True
    ) as admin:
        admin.execute(sql.SQL("CREATE TABLE {} (x int)").format(sql.SQL(name)))
        try:
            admin.execute(
                sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    sql.SQL(name), sql.Identifier(owner)
                )
            )
            yield name
        finally:
            admin.execute(sql.SQL("DROP TABLE {}").format(sql.SQL(name)))


def checks_naming(probe: str, at: int) -> str:
    """The role check with ``probe`` in the place of the role at ``at``."""
    roles = ["policy_seed", "knowledge_ingest"]
    roles[at] = probe
    return "ARRAY[" + ", ".join(f"'{role}'" for role in roles) + "]"


# ── the migration ───────────────────────────────────────────────────────────
def test_the_migration_is_recorded(migrated_database: DatabaseHandle) -> None:
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert (migration_name(),) in recorded


@pytest.mark.parametrize("at", [0, 1], ids=["seed", "ingest"])
def test_a_missing_role_fails_clearly_and_grants_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, at: int
) -> None:
    with pytest.raises(
        psycopg.Error,
        match=(
            "required role role_that_does_not_exist does not exist; "
            "create it out of band before migrating"
        ),
    ):
        apply_with_role_check(
            empty_database,
            monkeypatch,
            checks_naming("role_that_does_not_exist", at),
        )

    # Both roles exist in the cluster, so the grants that follow the check would
    # have worked: none of them ran.
    assert_nothing_was_granted(empty_database)


@pytest.mark.parametrize("at", [0, 1], ids=["seed", "ingest"])
@pytest.mark.parametrize(
    ("attributes", "member_of", "named"),
    [
        pytest.param("SUPERUSER", None, "SUPERUSER", id="superuser"),
        pytest.param("BYPASSRLS", None, "BYPASSRLS", id="bypassrls"),
        pytest.param("CREATEROLE", None, "CREATEROLE", id="createrole"),
        pytest.param("CREATEDB", None, "CREATEDB", id="createdb"),
        pytest.param("REPLICATION", None, "REPLICATION", id="replication"),
        pytest.param("", "pg_write_all_data", "membership", id="builtin-group"),
    ],
)
def test_a_role_with_more_than_a_plain_login_is_refused_and_nothing_is_granted(
    empty_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    at: int,
    attributes: str,
    member_of: str | None,
    named: str,
) -> None:
    with (
        probe_role(empty_database, attributes, member_of) as probe,
        pytest.raises(psycopg.Error) as caught,
    ):
        apply_with_role_check(empty_database, monkeypatch, checks_naming(probe, at))

    message = caught.value.diag.message_primary or ""
    assert f"role {probe} must not hold" in message
    assert named in message
    assert_nothing_was_granted(empty_database)


@pytest.mark.parametrize("at", [0, 1], ids=["seed", "ingest"])
def test_a_role_that_owns_an_object_is_refused_and_nothing_is_granted(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, at: int
) -> None:
    # A role that owns a table may grant on it, drop it and change its triggers,
    # whatever this file grants: the check refuses it (the database review).
    with (
        probe_role(empty_database, "") as probe,
        owned_table(empty_database, probe),
        pytest.raises(psycopg.Error) as caught,
    ):
        apply_with_role_check(empty_database, monkeypatch, checks_naming(probe, at))

    message = caught.value.diag.message_primary or ""
    assert f"role {probe} must not hold" in message
    assert "an owned object" in message
    assert_nothing_was_granted(empty_database)


@pytest.mark.parametrize("at", [0, 1], ids=["seed", "ingest"])
def test_a_plain_role_passes_the_role_check(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, at: int
) -> None:
    # The control of the refusals above: the same swap with a role that has none
    # of the attributes goes through, so a refusal is about the attribute.
    with probe_role(empty_database, "") as probe:
        apply_with_role_check(empty_database, monkeypatch, checks_naming(probe, at))


@pytest.mark.parametrize("at", [0, 1], ids=["seed", "ingest"])
def test_a_role_that_owns_nothing_passes_beside_one_that_owns_a_table(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, at: int
) -> None:
    # The ownership check is about the role it is given: another role's table in
    # the same database does not make a plain role fail.
    with (
        probe_role(empty_database, "") as plain,
        probe_role(empty_database, "") as owner,
        owned_table(empty_database, owner),
    ):
        apply_with_role_check(empty_database, monkeypatch, checks_naming(plain, at))


@pytest.mark.parametrize("role", JOB_ROLES)
def test_a_role_as_the_tests_and_the_cluster_make_it_has_no_attribute_of_note(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, "
        "(SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid), "
        "(SELECT count(*) FROM pg_shdepend d "
        "WHERE d.refclassid = 'pg_authid'::regclass "
        "AND d.refobjid = r.oid AND d.deptype = 'o') "
        "FROM pg_roles r WHERE rolname = %s",
        (role,),
    )

    assert rows == [(False, False, False, False, False, 0, 0)]


def test_a_migration_run_by_a_superuser_is_refused_and_grants_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    monkeypatch.setattr(
        runner, "migration_files", lambda: files[: migration_index() + 1]
    )
    as_admin = make_conninfo(empty_database.admin_dsn, dbname=empty_database.name)

    with connect(as_admin, "test") as conn:
        is_superuser = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        conn.rollback()  # the runner wants an idle connection
        assert is_superuser == (True,)
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema audit"
        ):
            runner.apply_migrations(conn)
        conn.rollback()

    assert_nothing_was_granted(empty_database)


def test_a_migration_run_by_a_role_that_does_not_own_the_schemas_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    text = dict(files)[migration_name()]

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema audit"
        ):
            conn.execute(text)
        conn.rollback()

    assert_nothing_was_granted(empty_database)


def test_the_migration_gives_each_role_what_it_holds_and_changes_no_other_role(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    before = {role: privileges(empty_database, role) for role in OTHER_ROLES}
    monkeypatch.setattr(
        runner, "migration_files", lambda: files[: migration_index() + 1]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == [migration_name()]
    for role in JOB_ROLES:
        assert privileges(empty_database, role) == HOLDS[role], role
    for role in OTHER_ROLES:
        assert privileges(empty_database, role) == before[role], role


def test_the_file_takes_no_lock_on_a_table_that_exists_before_it(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn(OWNER), "test") as conn:
        existing = [
            oid
            for (oid,) in conn.execute(
                "SELECT c.oid FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname = ANY(%s)",
                (list(PLATFORM_SCHEMAS),),
            )
        ]
        conn.execute(files[migration_index()][1])
        locked = conn.execute(
            "SELECT l.relation::regclass::text, l.mode FROM pg_locks l "
            "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
            "AND l.relation = ANY(%s::oid[])",
            (existing,),
        ).fetchall()
        conn.rollback()

    assert existing
    assert locked == []


# ── what each role holds ────────────────────────────────────────────────────
@pytest.mark.parametrize("role", JOB_ROLES)
def test_each_role_holds_exactly_the_grants_of_the_contract(
    migrated_database: DatabaseHandle, role: str
) -> None:
    held = privileges(migrated_database, role)

    assert held == MIGRATED_HOLDS[role], (
        held - MIGRATED_HOLDS[role],
        MIGRATED_HOLDS[role] - held,
    )


def set_columns(statement: str) -> frozenset[str]:
    """The columns an upsert's ``DO UPDATE SET`` names: ``col = EXCLUDED.col``."""
    return frozenset(re.findall(r"(\w+) = EXCLUDED", statement))


def test_the_set_reader_finds_every_column_of_an_upsert_and_nothing_else() -> None:
    statement = "ON CONFLICT (k) DO UPDATE SET\n    a = EXCLUDED.a,\n    b = EXCLUDED.b"

    assert set_columns(statement) == {"a", "b"}
    assert set_columns("INSERT INTO t (k, a) VALUES (1, 2)") == frozenset()


def test_the_update_grants_are_the_columns_the_seeds_upserts_set_and_no_others(
    migrated_database: DatabaseHandle,
) -> None:
    held = privileges(migrated_database, SEED_ROLE)

    updatable = {
        table: frozenset(
            column
            for kind, name, privilege, column in held
            if kind == "column" and privilege == "UPDATE" and name == table
        )
        for table in ("policy.policies", "policy.claim_history")
    }

    # The source of truth is the seed's own statements, not this file's lists:
    # a column granted that no upsert sets, or set that is not granted, differs.
    assert updatable == {
        "policy.policies": set_columns(UPSERT_POLICY),
        "policy.claim_history": set_columns(UPSERT_HISTORY),
    }
    assert all(updatable.values())
    assert "policy_number" not in updatable["policy.policies"]
    assert "history_id" not in updatable["policy.claim_history"]


@pytest.mark.parametrize("role", JOB_ROLES)
def test_no_role_holds_a_function_or_a_sequence_of_the_platform(
    migrated_database: DatabaseHandle, role: str
) -> None:
    functions = run(
        migrated_database,
        OWNER,
        EXECUTORS,
        {"schemas": list(PLATFORM_SCHEMAS), "role": role},  # type: ignore[arg-type]
    )
    sequence = run(
        migrated_database,
        OWNER,
        "SELECT has_sequence_privilege(%s, 'audit.events_seq', 'USAGE'), "
        "has_sequence_privilege(%s, 'audit.events_seq', 'SELECT'), "
        "has_sequence_privilege(%s, 'audit.events_seq', 'UPDATE')",
        (role, role, role),
    )

    assert functions == []
    assert sequence == [(False, False, False)]


# ── refused, connected as the role ──────────────────────────────────────────
def own_tables(role: str) -> set[str]:
    return {
        object_name
        for kind, object_name, _, _ in HOLDS[role]
        if kind in ("table", "column")
    }


def statements_for(table: str, kind: str, column: str) -> list[str]:
    """Every statement that reads or writes ``table``. A view is only read: its
    writes are refused for what it is (a union under a derived table cannot be
    written, SQLSTATE 55000), not for a privilege, and the table under it is
    covered by its own statements."""
    statements = [f"SELECT 1 FROM {table} LIMIT 1"]  # noqa: S608
    if kind != "v":
        statements += [
            f"INSERT INTO {table} DEFAULT VALUES",
            f"UPDATE {table} SET {column} = {column}",  # noqa: S608
            f"DELETE FROM {table}",  # noqa: S608
            f"TRUNCATE {table}",
        ]
    return statements


def sqlstates(db: DatabaseHandle, role: str, statements: list[str]) -> dict[str, str]:
    """The SQLSTATE each statement ends in as ``role``, one connection, each in
    a transaction that is rolled back."""
    outcome: dict[str, str] = {}
    with connect(db.dsn(role), "test") as conn:
        for statement in statements:
            try:
                conn.execute(statement)
                outcome[statement] = "ran"
            except psycopg.Error as exc:
                outcome[statement] = exc.sqlstate or "none"
            conn.rollback()
    return outcome


@pytest.mark.parametrize("role", JOB_ROLES)
def test_a_role_is_refused_every_statement_on_every_table_it_has_no_right_on(
    migrated_database: DatabaseHandle, role: str
) -> None:
    tables = run(migrated_database, OWNER, TABLES, (list(PLATFORM_SCHEMAS),))
    others = [t for t in tables if t[0] not in own_tables(role)]
    statements = [
        s for table, kind, column in others for s in statements_for(table, kind, column)
    ]

    outcome = sqlstates(migrated_database, role, statements)

    schemas = {table.split(".")[0] for table, _, _ in others}
    assert {"claims", "gateway", "runtime"} <= schemas
    assert {
        s for s, state in outcome.items() if state != INSUFFICIENT_PRIVILEGE
    } == set()


def test_the_seed_cannot_touch_the_audit_log_or_the_knowledge_store(
    migrated_database: DatabaseHandle,
) -> None:
    statements = [
        "INSERT INTO audit.events (service, event, outcome) VALUES ('x', 'x', 'x')",
        "SELECT count(*) FROM audit.events",
        "SELECT count(*) FROM knowledge.chunks",
        "DELETE FROM knowledge.chunks",
        "TRUNCATE knowledge.chunks",
    ]

    outcome = sqlstates(migrated_database, SEED_ROLE, statements)

    assert set(outcome.values()) == {INSUFFICIENT_PRIVILEGE}, outcome


@pytest.mark.parametrize(
    "statement",
    [
        "TRUNCATE policy.policies, policy.claim_history",
        "UPDATE policy.policies SET policy_number = 'POL-9999'",
        "UPDATE policy.claim_history SET history_id = 'HIST-9999'",
    ],
)
def test_the_seed_cannot_truncate_the_policy_tables_or_update_a_key(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    outcome = sqlstates(migrated_database, SEED_ROLE, [statement])

    assert outcome == {statement: INSUFFICIENT_PRIVILEGE}


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM policy.policies",
        "SELECT count(*) FROM policy.claim_history",
        "INSERT INTO policy.policies DEFAULT VALUES",
        "DELETE FROM policy.claim_history",
        "TRUNCATE policy.policies",
    ],
)
def test_the_ingestion_can_neither_read_nor_write_the_policy_tables(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    outcome = sqlstates(migrated_database, INGEST_ROLE, [statement])

    assert outcome == {statement: INSUFFICIENT_PRIVILEGE}


@pytest.mark.parametrize(
    "statement",
    [
        # Since 0026 the role reads seven columns, so a count of rows is allowed;
        # the vector is still out of reach.
        "SELECT embedding FROM knowledge.chunks",
        "UPDATE knowledge.chunks SET title = title",
        "TRUNCATE knowledge.chunks",
        "SELECT count(*) FROM audit.events",
        "UPDATE audit.events SET outcome = 'x'",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
    ],
)
def test_the_ingestion_cannot_read_the_log_or_change_a_row_of_it_or_the_chunks(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    outcome = sqlstates(migrated_database, INGEST_ROLE, [statement])

    assert outcome == {statement: INSUFFICIENT_PRIVILEGE}


def test_an_audit_row_of_the_ingestion_names_its_role_whatever_it_sends(
    migrated_database: DatabaseHandle,
) -> None:
    marker = f"job-roles-{uuid.uuid4()}"

    run(
        migrated_database,
        INGEST_ROLE,
        "INSERT INTO audit.events (db_role, service, event, outcome) "
        "VALUES ('meridian_owner', 'knowledge-ingest', %s, 'ok')",
        (marker,),
    )

    rows = run(
        migrated_database,
        OWNER,
        "SELECT db_role FROM audit.events WHERE event = %s",
        (marker,),
    )
    assert rows == [(INGEST_ROLE,)]


# ── the header ──────────────────────────────────────────────────────────────
def migration_text() -> str:
    return dict(migration_files())[migration_name()]


def header_text() -> str:
    head = migration_text().split("DO $$")[0]
    return " ".join(line.removeprefix("--").strip() for line in head.splitlines())


def test_the_header_names_the_guards_ownership_check_and_what_it_cannot_see() -> None:
    header = header_text()

    assert "owns an object" in header
    assert "cannot see" in header
    assert "granted to one of these roles after the file ran" in header
    assert "inRoles" in header
    assert "infra/kind/values/platform-db.yaml" in header


def test_the_header_says_what_is_true_of_the_key_columns() -> None:
    header = header_text()

    assert "cannot rename a row" not in header
    assert "not updatable" in header
    assert "a delete and an insert still replace a row" in header


def test_the_header_says_the_cluster_sets_a_limit_of_two_connections() -> None:
    header = header_text()

    assert "Neither role gets a connection limit here" not in header
    assert "connection limit of 2" in header
    assert "platform-db.yaml" in header


def test_the_undo_the_header_gives_takes_every_grant_back(
    fresh_database: DatabaseHandle,
) -> None:
    statements = re.findall(r"REVOKE [^;]+;", header_text())

    assert len(statements) == 4
    assert {SEED_ROLE, INGEST_ROLE} == {s.split()[-1].rstrip(";") for s in statements}
    assert all(privileges(fresh_database, role) for role in JOB_ROLES)
    for statement in statements:
        run(fresh_database, OWNER, statement)
    assert [privileges(fresh_database, role) for role in JOB_ROLES] == [
        frozenset(),
        frozenset(),
    ]
