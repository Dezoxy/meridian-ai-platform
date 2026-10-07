"""0020: what the upkeep role holds and the catalog entries of its functions (S066).

The second of three files on the migration: the role's rights, the role
connected as itself, and the owner, the ACL and the search path of each
function. The migration's own checks are in
``test_gateway_upkeep_migration_checks.py``, the credits table in
``test_gateway_upkeep_credits_table.py``. The constants and helpers they share
are in ``upkeepsupport``.
"""

import re

import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from sweepmigrationsupport import privileges
from upkeepsupport import (
    INSUFFICIENT_PRIVILEGE,
    MIGRATION,
    READABLE_COLUMNS,
    ROLE,
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
# 0030 took the upkeep role's EXECUTE on the last of them back (the expiry in
# batches replaced it): the owner's alone on a migrated database.
OWNERS_ALONE_SINCE_0030 = "gateway.expire_ledger(date, text)"
ROLE_FUNCTIONS = tuple(f for f in PUBLIC_FUNCTIONS if f != OWNERS_ALONE_SINCE_0030)
HELPER_FUNCTIONS = (
    "gateway.upkeep_check_reason(text)",
    "gateway.upkeep_lower_counter(text, text, date, bigint)",
    "gateway.upkeep_open_charge(text, text, date)",
    "gateway.upkeep_audit(text, text, text, text, text, text, text, text, text, uuid)",
)
LEDGER_TABLES = ("usage", "budget_counters", "credits")
# The schemas of the platform: pgvector's functions in `public` are executable by
# everyone and are not what this asks about.
PLATFORM_SCHEMAS = ("audit", "claims", "gateway", "knowledge", "policy", "runtime")
OTHER_ROLES = SERVICE_ROLES
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


def test_the_role_may_execute_five_functions_and_no_other(
    migrated_database: DatabaseHandle,
) -> None:
    # The catalog prints a signature without the spaces after its commas. 0020's
    # close and credit; 0028's audit expiry and its count; 0030's expiry in
    # batches, which took the place of 0020's expire_ledger for this role.
    since_0028_and_0030 = [
        "gateway.expire_audit_events(timestamp with time zone,text,integer)",
        "gateway.count_audit_events_before(timestamp with time zone)",
        "gateway.expire_ledger_batch(date,text,integer)",
    ]
    expected = sorted(
        [name.replace(", ", ",") for name in ROLE_FUNCTIONS] + since_0028_and_0030
    )
    assert executable_by(migrated_database, ROLE) == expected


@pytest.mark.parametrize("function", ROLE_FUNCTIONS)
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
