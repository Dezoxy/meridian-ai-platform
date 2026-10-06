"""0020: the upkeep role, the credits table and what the role may reach (S066).

The first of three files on the migration: the migration itself, the role's
privileges, the credits table and the catalog entries of the functions. The
other two cover the functions: ``test_gateway_upkeep_close.py`` for
``gateway.close_reservation`` and ``test_gateway_upkeep_credit_expire.py`` for
``gateway.credit_tenant`` and ``gateway.expire_ledger``. The constants and
helpers they share are in ``upkeepsupport``.
"""

import re

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from sweepmigrationsupport import INDEX_COLUMNS, privileges
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
    "gateway.upkeep_audit(text, text, text, text, text, text, text, text, text, uuid)",
)
LEDGER_TABLES = ("usage", "budget_counters", "credits")
# The schemas of the platform: pgvector's functions in `public` are executable by
# everyone and are not what this asks about.
PLATFORM_SCHEMAS = ("audit", "claims", "gateway", "knowledge", "policy", "runtime")
OTHER_ROLES = SERVICE_ROLES
INSERT_CREDIT = (
    "INSERT INTO gateway.credits (tenant, kind, period_start, amount, reason) "
    "VALUES (%(tenant)s, %(kind)s, '2026-10-01', %(amount)s, %(reason)s)"
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


# ── the role holds what the contract says and nothing else ──────────────────
def expected_holds(db: DatabaseHandle) -> frozenset[tuple]:
    """The role's rights: SELECT on the three ledger tables (so on each of their
    columns) and USAGE on the schema gateway."""
    columns = run(
        db,
        OWNER,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'gateway' AND table_name = ANY(%s)",
        (list(LEDGER_TABLES),),
    )
    return frozenset(
        {
            ("schema", "gateway", "USAGE", ""),
            *(("table", f"gateway.{table}", "SELECT", "") for table in LEDGER_TABLES),
            *(
                ("column", f"gateway.{table}", "SELECT", column)
                for table, column in columns
            ),
        }
    )


def test_the_role_holds_exactly_three_selects_and_the_schema_usage(
    migrated_database: DatabaseHandle,
) -> None:
    held = privileges(migrated_database, ROLE)

    expected = expected_holds(migrated_database)
    assert held == expected, (held - expected, expected - held)


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


def test_the_reconciliation_index_covers_tenant_kind_and_period(
    migrated_database: DatabaseHandle,
) -> None:
    indexes = run(
        migrated_database,
        OWNER,
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'gateway' AND tablename = 'credits'",
    )
    named = [name for (name,) in indexes if name != "credits_pkey"]

    assert len(named) == 1
    columns = run(
        migrated_database, OWNER, INDEX_COLUMNS, ("gateway", "credits", named[0])
    )
    assert columns == [("tenant",), ("kind",), ("period_start",)]


def test_the_credits_table_is_the_owners_and_only_the_role_reads_it(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT relowner::regrole::text, relacl::text[] FROM pg_class "
        "WHERE oid = 'gateway.credits'::regclass",
    )

    # `m` is MAINTAIN, which PostgreSQL 17 adds to the owner's own rights.
    assert rows == [(OWNER, [f"{OWNER}=arwdDxtm/{OWNER}", f"{ROLE}=r/{OWNER}"])]


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
