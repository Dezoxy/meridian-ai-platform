"""0020: the upkeep role, the credits table and what the role may reach (S066).

The first of three files on the migration: the migration itself, the role's
privileges, the credits table and the catalog entries of the functions. The
other two cover the functions: ``test_gateway_upkeep_close.py`` for
``gateway.close_reservation`` and ``test_gateway_upkeep_credit_expire.py`` for
``gateway.credit_tenant`` and ``gateway.expire_ledger``. The constants and
helpers they share are in ``upkeepsupport``.
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sweepmigrationsupport import privileges
from upkeepsupport import (
    INSUFFICIENT_PRIVILEGE,
    MIGRATION,
    ROLE,
    TENANT,
    run,
    sqlstate,
)

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

PUBLIC_FUNCTIONS = (
    "gateway.close_reservation(uuid, boolean, text)",
    "gateway.credit_tenant(text, text, bigint, text)",
    "gateway.expire_ledger(date, text)",
)
HELPER_FUNCTIONS = (
    "gateway.upkeep_check_reason(text)",
    "gateway.upkeep_lower_counter(text, text, date, bigint)",
    "gateway.upkeep_open_charge(text, text, date)",
    "gateway.upkeep_audit(text, text, text, text, text, text, text, text, text, uuid)",
)
LEDGER_TABLES = ("usage", "budget_counters", "credits")
# The columns `meridian gateway` reads, by table: LIST_RESERVED (usage) and the
# dry run's counts of rows by month (usage.month, the two period_start columns).
READABLE_COLUMNS = {
    "usage": (
        "attempt_id",
        "tenant",
        "deployment",
        "state",
        "reserved_at",
        "reserved_tokens",
        "reserved_micro_eur",
        "month",
    ),
    "budget_counters": ("period_start",),
    "credits": ("period_start",),
}
# The schemas of the platform: pgvector's functions in `public` are executable by
# everyone and are not what this asks about.
PLATFORM_SCHEMAS = ("audit", "claims", "gateway", "knowledge", "policy", "runtime")
OTHER_ROLES = SERVICE_ROLES
INSERT_CREDIT = (
    "INSERT INTO gateway.credits (tenant, kind, period_start, amount, reason) "
    "VALUES (%(tenant)s, %(kind)s, '2026-10-01', %(amount)s, %(reason)s)"
)
INSERT_CREDIT_AT = (
    "INSERT INTO gateway.credits (tenant, kind, period_start, amount, reason) "
    "VALUES (%(tenant)s, %(kind)s, %(period)s, %(amount)s, %(reason)s)"
)
CREDIT_DEFAULTS = {
    "tenant": TENANT,
    "kind": "tokens-day",
    "amount": 5,
    "reason": "goodwill",
}
EXECUTORS = """
SELECT p.oid::regprocedure::text
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = ANY(%(schemas)s)
    AND has_function_privilege(%(role)s, p.oid, 'EXECUTE')
ORDER BY 1
"""


def owner_scalar(db: DatabaseHandle, statement: str, params: object = ()) -> object:
    ((value,),) = run(db, OWNER, statement, params)
    return value


def executable_by(db: DatabaseHandle, role: str) -> list[str]:
    rows = run(db, OWNER, EXECUTORS, {"schemas": list(PLATFORM_SCHEMAS), "role": role})
    return [name for (name,) in rows]


def acl(db: DatabaseHandle, function: str) -> list[str]:
    return owner_scalar(  # type: ignore[return-value]
        db,
        "SELECT proacl::text[] FROM pg_proc WHERE oid = %s::regprocedure",
        (function,),
    )


# ── the migration ───────────────────────────────────────────────────────────
def test_the_migration_is_the_twentieth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[19] == MIGRATION
    assert (MIGRATION,) in recorded


def test_a_missing_upkeep_role_fails_clearly_and_changes_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    name, text = files[19]
    broken = text.replace(f"ARRAY['{ROLE}']", "ARRAY['role_that_does_not_exist']")
    assert broken != text
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:19], (name, broken)]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match=(
                "required role role_that_does_not_exist does not exist; "
                "create it out of band before migrating"
            ),
        ):
            runner.apply_migrations(conn)
        conn.rollback()

        # The role exists in the cluster, so the grants that follow the check
        # would have worked: none of them ran, and no 0020 object exists.
        assert conn.execute(
            "SELECT has_schema_privilege(%s, 'gateway', 'USAGE')", (ROLE,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT count(*) FROM pg_class WHERE relname = 'credits'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'close_reservation'"
        ).fetchone() == (0,)


@contextmanager
def probe_role(
    db: DatabaseHandle, attributes: str, member_of: str | None = None
) -> Iterator[str]:
    """A login-less role of its own, with ``attributes``, that is a member of
    ``member_of``; dropped when the block ends. Roles are cluster-wide and the
    workers share one server, so a test never alters ``gateway_upkeep``: it
    makes a role like it, under a name of its own, and swaps the name into the
    migration's text."""
    name = f"gateway_upkeep_probe_{uuid.uuid4().hex[:12]}"
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


def apply_0020_naming_the_role(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Apply 0001 to 0019 as the owner, then 0020 with ``role`` in place of the
    upkeep role in its role check, as the owner. Raises what the file raises."""
    files = migration_files()
    name, text = files[19]
    swapped = text.replace(f"ARRAY['{ROLE}']", f"ARRAY['{role}']")
    assert swapped != text
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(db.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:19], (name, swapped)]
    )
    with connect(db.dsn(OWNER), "test") as conn:
        try:
            runner.apply_migrations(conn)
        finally:
            conn.rollback()


def assert_nothing_of_0020_exists(db: DatabaseHandle) -> None:
    for relation in ("pg_class", "pg_proc"):
        column = "relname" if relation == "pg_class" else "proname"
        assert run(
            db,
            OWNER,
            f"SELECT count(*) FROM {relation} WHERE {column} IN "  # noqa: S608
            "('credits', 'close_reservation', 'credit_tenant', 'expire_ledger')",
        ) == [(0,)]


@pytest.mark.parametrize(
    ("attributes", "member_of", "named"),
    [
        pytest.param("SUPERUSER", None, "SUPERUSER", id="superuser"),
        pytest.param("BYPASSRLS", None, "BYPASSRLS", id="bypassrls"),
        pytest.param("CREATEROLE", None, "CREATEROLE", id="createrole"),
        pytest.param("CREATEDB", None, "CREATEDB", id="createdb"),
        pytest.param("REPLICATION", None, "REPLICATION", id="replication"),
        pytest.param("", "pg_write_all_data", "membership", id="builtin-group"),
        pytest.param("", "pg_monitor", "membership", id="another-builtin-group"),
    ],
)
def test_a_role_with_more_than_a_plain_login_is_refused_and_nothing_is_created(
    empty_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    attributes: str,
    member_of: str | None,
    named: str,
) -> None:
    with (
        probe_role(empty_database, attributes, member_of) as role,
        pytest.raises(psycopg.Error) as caught,
    ):
        apply_0020_naming_the_role(empty_database, monkeypatch, role)

    message = caught.value.diag.message_primary or ""
    assert f"role {role} must not hold" in message
    assert named in message
    assert_nothing_of_0020_exists(empty_database)


def test_a_role_that_is_a_member_of_a_throwaway_group_is_refused_too(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        probe_role(empty_database, "") as group,
        probe_role(empty_database, "", member_of=group) as role,
        pytest.raises(psycopg.Error, match="membership"),
    ):
        apply_0020_naming_the_role(empty_database, monkeypatch, role)

    assert_nothing_of_0020_exists(empty_database)


def test_a_plain_role_passes_the_role_check(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control of the refusals above: the same swap with a role that has none
    # of the attributes goes through, so a refusal is about the attribute.
    with probe_role(empty_database, "") as role:
        apply_0020_naming_the_role(empty_database, monkeypatch, role)


def test_the_upkeep_role_as_the_tests_and_the_cluster_make_it_has_no_attribute_of_note(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, "
        "(SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) "
        "FROM pg_roles r WHERE rolname = %s",
        (ROLE,),
    )

    assert rows == [(False, False, False, False, False, 0)]


def test_a_migration_run_by_a_superuser_is_refused_and_creates_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    monkeypatch.setattr(runner, "migration_files", lambda: files[:20])
    as_admin = make_conninfo(empty_database.admin_dsn, dbname=empty_database.name)

    with connect(as_admin, "test") as conn:
        is_superuser = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        conn.rollback()  # the runner wants an idle connection
        assert is_superuser == (True,)
        with pytest.raises(
            psycopg.Error,
            match="must be run by the owner of the schema gateway",
        ):
            runner.apply_migrations(conn)
        conn.rollback()

    assert_nothing_of_0020_exists(empty_database)


def test_a_migration_run_by_a_role_that_does_not_own_the_schema_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    text = dict(migration_files())[MIGRATION]

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema gateway"
        ):
            conn.execute(text)
        conn.rollback()

    assert_nothing_of_0020_exists(empty_database)


# ── the role holds what the contract says and nothing else ──────────────────
def expected_holds(db: DatabaseHandle) -> frozenset[tuple]:
    """The role's rights: SELECT on the columns the command reads and USAGE on the
    schema gateway. No table-level right at all (``db`` is kept so that callers
    read as before)."""
    return frozenset(
        {
            ("schema", "gateway", "USAGE", ""),
            *(
                ("column", f"gateway.{table}", "SELECT", column)
                for table, columns in READABLE_COLUMNS.items()
                for column in columns
            ),
        }
    )


def test_the_role_holds_exactly_the_columns_the_command_reads_and_the_schema_usage(
    migrated_database: DatabaseHandle,
) -> None:
    held = privileges(migrated_database, ROLE)

    expected = expected_holds(migrated_database)
    assert held == expected, (held - expected, expected - held)
    assert not [right for right in held if right[0] == "table"]


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("usage", "run_id"),
        ("usage", "call_id"),
        ("usage", "agent"),
        ("usage", "model"),
        ("usage", "provider"),
        ("usage", "input_tokens"),
        ("usage", "charged_tokens"),
        ("usage", "closed_at"),
        ("budget_counters", "amount"),
        ("budget_counters", "tenant"),
        ("credits", "amount"),
        ("credits", "tenant"),
        ("credits", "reason"),
        ("credits", "db_role"),
    ],
)
def test_the_role_cannot_read_a_column_the_command_does_not_need(
    migrated_database: DatabaseHandle, table: str, column: str
) -> None:
    state = sqlstate(
        migrated_database,
        ROLE,
        f"SELECT {column} FROM gateway.{table}",  # noqa: S608
    )

    assert state == INSUFFICIENT_PRIVILEGE


def test_the_role_cannot_read_every_column_of_a_ledger_table_with_a_star(
    migrated_database: DatabaseHandle,
) -> None:
    for table in LEDGER_TABLES:
        state = sqlstate(
            migrated_database,
            ROLE,
            f"SELECT * FROM gateway.{table}",  # noqa: S608
        )

        assert state == INSUFFICIENT_PRIVILEGE, table


def test_the_role_has_no_privilege_on_the_audit_schema_or_any_other_schema(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT s, has_schema_privilege(%s, s, 'USAGE'), "
        "has_schema_privilege(%s, s, 'CREATE') "
        "FROM unnest(%s::text[]) AS s ORDER BY s",
        (ROLE, ROLE, list(PLATFORM_SCHEMAS)),
    )

    assert rows == [
        (schema, schema == "gateway", False) for schema in sorted(PLATFORM_SCHEMAS)
    ]


def test_the_role_may_execute_the_three_functions_and_no_other_function(
    migrated_database: DatabaseHandle,
) -> None:
    # The catalog prints a signature without the spaces after its commas.
    expected = sorted(name.replace(", ", ",") for name in PUBLIC_FUNCTIONS)
    assert executable_by(migrated_database, ROLE) == expected


@pytest.mark.parametrize("function", PUBLIC_FUNCTIONS)
def test_execute_on_each_function_is_the_owners_and_the_roles_alone(
    migrated_database: DatabaseHandle, function: str
) -> None:
    # No `=X/...` entry for PUBLIC: it was revoked.
    assert acl(migrated_database, function) == [
        f"{OWNER}=X/{OWNER}",
        f"{ROLE}=X/{OWNER}",
    ]


@pytest.mark.parametrize("function", HELPER_FUNCTIONS)
def test_a_helper_function_is_the_owners_alone(
    migrated_database: DatabaseHandle, function: str
) -> None:
    assert acl(migrated_database, function) == [f"{OWNER}=X/{OWNER}"]


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_service_role_may_execute_any_function_of_the_upkeep(
    migrated_database: DatabaseHandle, role: str
) -> None:
    held = executable_by(migrated_database, role)

    assert not {*PUBLIC_FUNCTIONS, *HELPER_FUNCTIONS} & set(held)


@pytest.mark.parametrize("function", PUBLIC_FUNCTIONS)
def test_the_model_gateway_cannot_call_a_function_of_the_upkeep(
    migrated_database: DatabaseHandle, function: str
) -> None:
    name = function.split("(")[0]
    arguments = {
        "gateway.close_reservation": "gen_random_uuid(), false, 'a-reason'",
        "gateway.credit_tenant": "'t', 'tokens-day', 1, 'a-reason'",
        "gateway.expire_ledger": "'2020-01-01', 'a-reason'",
    }[name]

    state = sqlstate(
        migrated_database,
        "model_gateway",
        f"SELECT * FROM {name}({arguments})",  # noqa: S608
    )

    assert state == INSUFFICIENT_PRIVILEGE


def test_the_migration_gives_the_role_everything_it_holds_and_changes_no_other_role(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    assert files[19][0] == MIGRATION
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    before = {role: privileges(empty_database, role) for role in SERVICE_ROLES}
    before_functions = {
        role: executable_by(empty_database, role) for role in OTHER_ROLES
    }
    monkeypatch.setattr(runner, "migration_files", lambda: files[:20])

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == [MIGRATION]
    assert privileges(empty_database, ROLE) == expected_holds(empty_database)
    for role in SERVICE_ROLES:
        assert privileges(empty_database, role) == before[role], role
    for role in OTHER_ROLES:
        assert executable_by(empty_database, role) == before_functions[role], role


def test_the_file_takes_no_lock_on_a_table_that_exists_before_it(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)

    with connect(empty_database.dsn(OWNER), "test") as conn:
        existing = [
            oid
            for (oid,) in conn.execute(
                "SELECT c.oid FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname = ANY(%s)",
                (list(PLATFORM_SCHEMAS),),
            )
        ]
        conn.execute(dict(files)[MIGRATION])
        locked = conn.execute(
            "SELECT l.relation::regclass::text, l.mode FROM pg_locks l "
            "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
            "AND l.relation = ANY(%s::oid[])",
            (existing,),
        ).fetchall()
        conn.rollback()

    assert locked == []


# ── the role, connected as itself ───────────────────────────────────────────
@pytest.mark.parametrize(
    "table", ["gateway.usage", "gateway.budget_counters", "gateway.credits"]
)
def test_the_role_reads_the_ledger_tables(
    migrated_database: DatabaseHandle, table: str
) -> None:
    rows = run(migrated_database, ROLE, f"SELECT count(*) FROM {table}")  # noqa: S608

    assert len(rows) == 1


@pytest.mark.parametrize("table", ["usage", "budget_counters", "credits"])
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO gateway.{table} DEFAULT VALUES",
        "UPDATE gateway.{table} SET tenant = 'x'",
        "DELETE FROM gateway.{table}",
        "TRUNCATE gateway.{table}",
    ],
)
def test_the_role_cannot_write_a_ledger_table(
    migrated_database: DatabaseHandle, table: str, statement: str
) -> None:
    state = sqlstate(migrated_database, ROLE, statement.format(table=table))

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO audit.events (service, event, outcome) VALUES ('x', 'y', 'z')",
        "UPDATE audit.events SET outcome = 'x'",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
        "SELECT count(*) FROM audit.events",
        "SELECT count(*) FROM audit.claim_trail",
        "SELECT nextval('audit.events_seq')",
        "SELECT audit.stamp_event()",
    ],
)
def test_the_role_cannot_touch_the_audit_log_directly(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    state = sqlstate(migrated_database, ROLE, statement)

    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE gateway.extra (id int)",
        "SELECT * FROM claims.claims",
        "SELECT * FROM runtime.runs",
        "UPDATE claims.claims SET tenant = 'x'",
    ],
)
def test_the_role_reaches_nothing_outside_the_ledger(
    migrated_database: DatabaseHandle, statement: str
) -> None:
    state = sqlstate(migrated_database, ROLE, statement)

    assert state == INSUFFICIENT_PRIVILEGE


def test_the_role_cannot_call_a_helper_directly(
    migrated_database: DatabaseHandle,
) -> None:
    state = sqlstate(
        migrated_database, ROLE, "SELECT gateway.upkeep_check_reason('a-reason')"
    )

    assert state == INSUFFICIENT_PRIVILEGE


# ── the credits table ───────────────────────────────────────────────────────
def insert_credit(db: DatabaseHandle, **values: object) -> list:
    return run(db, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | values)


def test_the_credits_table_has_its_columns(migrated_database: DatabaseHandle) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'gateway' AND table_name = 'credits' "
        "ORDER BY column_name",
    )

    assert rows == [
        ("amount", "bigint", "NO"),
        ("credit_id", "uuid", "NO"),
        ("db_role", "name", "NO"),
        ("kind", "text", "NO"),
        ("period_start", "date", "NO"),
        ("reason", "text", "NO"),
        ("recorded_at", "timestamp with time zone", "NO"),
        ("tenant", "text", "NO"),
    ]


def test_a_credit_row_gets_an_id_a_time_and_the_session_user(
    fresh_database: DatabaseHandle,
) -> None:
    insert_credit(fresh_database)

    rows = run(
        fresh_database,
        OWNER,
        "SELECT credit_id IS NOT NULL, recorded_at <= now(), "
        "db_role = session_user FROM gateway.credits",
    )

    assert rows == [(True, True, True)]


@pytest.mark.parametrize("amount", [0, -1])
def test_a_credit_of_nothing_or_less_is_refused(
    fresh_database: DatabaseHandle, amount: int
) -> None:
    assert (
        sqlstate(
            fresh_database, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | {"amount": amount}
        )
        == "23514"
    )


def test_a_credit_kind_outside_the_two_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    assert (
        sqlstate(
            fresh_database, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | {"kind": "month"}
        )
        == "23514"
    )


@pytest.mark.parametrize(
    "reason",
    ["a", "a-b-9", "x" * 64, "0"],
)
def test_a_reason_that_is_a_slug_is_stored(
    fresh_database: DatabaseHandle, reason: str
) -> None:
    insert_credit(fresh_database, reason=reason)

    assert run(fresh_database, OWNER, "SELECT reason FROM gateway.credits") == [
        (reason,)
    ]


@pytest.mark.parametrize(
    "reason",
    ["", "x" * 65, "Upper", "under_score", "with space", "end\n", "né", "a.b"],
)
def test_a_reason_that_is_not_a_slug_is_refused(
    fresh_database: DatabaseHandle, reason: str
) -> None:
    assert (
        sqlstate(
            fresh_database, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | {"reason": reason}
        )
        == "23514"
    )


def test_a_credit_tenant_holds_128_characters_and_refuses_129(
    fresh_database: DatabaseHandle,
) -> None:
    insert_credit(fresh_database, tenant="t" * 128)

    assert (
        sqlstate(
            fresh_database,
            OWNER,
            INSERT_CREDIT,
            CREDIT_DEFAULTS | {"tenant": "t" * 129},
        )
        == "23514"
    )


def test_the_credits_table_has_no_index_but_its_key(
    migrated_database: DatabaseHandle,
) -> None:
    # The reconciliation reads the whole table (a hash aggregate: an index on
    # (tenant, kind, period_start) was measured never used) and the expiry's
    # DELETE filters on period_start alone, which that index cannot serve.
    indexes = run(
        migrated_database,
        OWNER,
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'gateway' AND tablename = 'credits'",
    )

    assert indexes == [("credits_pkey",)]


@pytest.mark.parametrize("period", ["2026-10-01", "2026-01-01"])
def test_a_cost_credit_may_name_the_first_of_a_month(
    fresh_database: DatabaseHandle, period: str
) -> None:
    values = CREDIT_DEFAULTS | {"kind": "cost-month", "period": period}

    run(fresh_database, OWNER, INSERT_CREDIT_AT, values)

    assert run(fresh_database, OWNER, "SELECT count(*) FROM gateway.credits") == [(1,)]


@pytest.mark.parametrize("period", ["2026-10-02", "2026-10-15", "2026-10-31"])
def test_a_cost_credit_that_names_another_day_than_the_first_is_refused(
    fresh_database: DatabaseHandle, period: str
) -> None:
    values = CREDIT_DEFAULTS | {"kind": "cost-month", "period": period}

    state = sqlstate(fresh_database, OWNER, INSERT_CREDIT_AT, values)

    assert state == "23514"


def test_a_token_credit_may_name_any_day(fresh_database: DatabaseHandle) -> None:
    values = CREDIT_DEFAULTS | {"kind": "tokens-day", "period": "2026-10-17"}

    run(fresh_database, OWNER, INSERT_CREDIT_AT, values)

    assert run(fresh_database, OWNER, "SELECT count(*) FROM gateway.credits") == [(1,)]


def test_the_credits_table_is_the_owners_and_no_table_right_is_granted(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT relowner::regrole::text, relacl::text[] FROM pg_class "
        "WHERE oid = 'gateway.credits'::regclass",
    )

    # No ACL at all: the owner's default rights, and the role holds a column right
    # only, which lives in pg_attribute (next test).
    assert rows == [(OWNER, None)]


def test_the_column_rights_of_the_role_are_in_the_catalog_as_granted_by_the_owner(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT c.relname, a.attname, "
        "array_agg(i::text) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "CROSS JOIN LATERAL unnest(a.attacl) AS i "
        "WHERE c.relnamespace = 'gateway'::regnamespace AND i::text LIKE %s "
        "GROUP BY 1, 2 ORDER BY 1, 2",
        (f"{ROLE}=%",),
    )

    expected = sorted(
        (table, column, [f"{ROLE}=r/{OWNER}"])
        for table, columns in READABLE_COLUMNS.items()
        for column in columns
    )
    assert rows == expected


# ── the functions in the catalog ────────────────────────────────────────────
@pytest.mark.parametrize("function", [*PUBLIC_FUNCTIONS, *HELPER_FUNCTIONS])
def test_each_function_is_the_owners_with_a_pinned_search_path(
    migrated_database: DatabaseHandle, function: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT proowner::regrole::text, proconfig FROM pg_proc "
        "WHERE oid = %s::regprocedure",
        (function,),
    )

    # pg_temp last: the session's temporary schema is searched first for
    # relations and types unless the path names it (see upkeepsupport).
    assert rows == [(OWNER, ["search_path=pg_catalog, pg_temp"])]


@pytest.mark.parametrize("function", PUBLIC_FUNCTIONS)
def test_each_function_the_role_calls_runs_with_the_owners_rights(
    migrated_database: DatabaseHandle, function: str
) -> None:
    secdef = owner_scalar(
        migrated_database,
        "SELECT prosecdef FROM pg_proc WHERE oid = %s::regprocedure",
        (function,),
    )

    assert secdef is True


def test_the_floor_of_a_closing_is_at_least_twenty_call_deadlines() -> None:
    from meridian.platform.gateway.resilience import CALL_DEADLINE_SECONDS

    text = dict(migration_files())[MIGRATION]
    (minutes,) = re.findall(
        r"floor_age constant interval := interval '(\d+) minutes'", text
    )

    assert int(minutes) * 60 >= 20 * CALL_DEADLINE_SECONDS
