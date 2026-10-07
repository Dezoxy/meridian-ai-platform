"""0028: the dry run's count, ``gateway.count_audit_events_before`` (S068, T-25).

The upkeep role has no right on ``audit.events``, so the command's dry run cannot
count the rows an expiry would take; this owner's function counts them and
changes nothing. It holds the expiry's predicate (``recorded_at`` before the
cutoff), its refusals of a NULL and of a cutoff in the future, and its grant (the
upkeep role alone). Its tests plant rows as the expiry's do: the old group, the
clock, the young group.

The migration is found by the end of its name, never by its number.
"""

import pytest
from dbsupport import (
    INGEST_ROLE,
    OWNER,
    SEED_ROLE,
    SERVICE_ROLES,
    UPKEEP_ROLE,
    DatabaseHandle,
)
from upkeepsupport import (
    INSUFFICIENT_PRIVILEGE,
    NULL_ARGUMENT,
    REASON,
    audit_rows,
    run,
    sqlstate,
)

COUNT = "SELECT gateway.count_audit_events_before(%s)"
EXPIRE = "SELECT gateway.expire_audit_events(%s, %s, %s)"
FUTURE_CUTOFF = "GU401"
PROBE = "count-probe"
YOUNG = "count-young"
PLANT = (
    "INSERT INTO audit.events (service, event, outcome) "
    "SELECT %s, 'probe', 'completed' FROM generate_series(1, %s)"
)
READERS = (
    "SELECT n.nspname || '.' || p.proname FROM pg_proc AS p "
    "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
    "WHERE p.prosecdef AND p.prosrc ~* %s "
    "AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = %s) ORDER BY 1"
)
READS_THE_AUDIT_TABLE = r"(from|join)\s+(only\s+)?audit\.events"


def plant(db: DatabaseHandle, service: str, rows: int) -> None:
    run(db, OWNER, PLANT, (service, rows))


def clock(db: DatabaseHandle):
    return run(db, OWNER, "SELECT clock_timestamp()")[0][0]


def old_and_young(db: DatabaseHandle, old: int = 5, young: int = 3):
    plant(db, PROBE, old)
    cutoff = clock(db)
    plant(db, YOUNG, young)
    return cutoff


def count(db: DatabaseHandle, cutoff) -> int:
    return run(db, UPKEEP_ROLE, COUNT, (cutoff,))[0][0]


def total(db: DatabaseHandle) -> int:
    return run(db, OWNER, "SELECT count(*) FROM audit.events")[0][0]


def test_the_function_is_the_owners_definer_in_the_schema_gateway(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT p.prosecdef, r.rolname, p.proconfig, n.nspname, p.prorettype::regtype, "
        "pg_get_function_identity_arguments(p.oid) "
        "FROM pg_proc AS p JOIN pg_roles AS r ON r.oid = p.proowner "
        "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE p.proname = 'count_audit_events_before'",
    )

    assert rows == [
        (
            True,
            OWNER,
            ["search_path=pg_catalog, pg_temp"],
            "gateway",
            "bigint",
            "p_before timestamp with time zone",
        )
    ]


def test_only_the_upkeep_role_may_execute_the_function(
    migrated_database: DatabaseHandle,
) -> None:
    roles = (UPKEEP_ROLE, *SERVICE_ROLES, SEED_ROLE, INGEST_ROLE)
    holders = {
        role: run(
            migrated_database,
            OWNER,
            "SELECT has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc AS p "
            "WHERE p.proname = 'count_audit_events_before'",
            (role,),
        )[0][0]
        for role in roles
    }
    public = run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM pg_proc AS p, aclexplode(p.proacl) AS a "
        "WHERE p.proname = 'count_audit_events_before' AND a.grantee = 0",
    )

    assert holders == {role: role == UPKEEP_ROLE for role in roles}
    assert public == [(0,)]


def test_a_service_role_cannot_call_the_function(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database)

    code = sqlstate(fresh_database, "model_gateway", COUNT, (cutoff,))

    assert code == INSUFFICIENT_PRIVILEGE


def test_the_owners_definer_functions_that_read_the_audit_table_are_these_two(
    migrated_database: DatabaseHandle,
) -> None:
    readers = run(migrated_database, OWNER, READERS, (READS_THE_AUDIT_TABLE, OWNER))

    assert readers == [
        ("gateway.count_audit_events_before",),
        ("gateway.expire_audit_events",),
    ]


def test_the_count_is_what_the_batches_then_remove(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database, old=5, young=3)

    counted = count(fresh_database, cutoff)
    removed = [
        run(fresh_database, UPKEEP_ROLE, EXPIRE, (cutoff, REASON, 2))[0][0]
        for _ in range(4)
    ]

    assert counted == 5
    assert sum(removed) == counted
    assert count(fresh_database, cutoff) == 0


def test_the_count_of_a_cutoff_before_every_row_is_zero_and_after_them_all_of_them(
    fresh_database: DatabaseHandle,
) -> None:
    before_all = clock(fresh_database)
    plant(fresh_database, PROBE, 4)
    after_all = clock(fresh_database)

    assert count(fresh_database, before_all) == 0
    assert count(fresh_database, after_all) == 4


def test_the_count_changes_nothing_and_writes_no_audit_row(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database)

    count(fresh_database, cutoff)

    assert total(fresh_database) == 8
    assert audit_rows(fresh_database) == []


@pytest.mark.parametrize(
    ("cutoff_sql", "code"),
    [
        ("NULL", NULL_ARGUMENT),
        ("now() + interval '1 microsecond'", FUTURE_CUTOFF),
        ("now() + interval '1 day'", FUTURE_CUTOFF),
    ],
)
def test_a_null_and_a_future_cutoff_are_refused_with_the_expirys_codes(
    fresh_database: DatabaseHandle, cutoff_sql: str, code: str
) -> None:
    plant(fresh_database, PROBE, 3)

    statement = f"SELECT gateway.count_audit_events_before({cutoff_sql})"

    assert sqlstate(fresh_database, UPKEEP_ROLE, statement) == code


def test_a_cutoff_of_now_is_accepted(fresh_database: DatabaseHandle) -> None:
    plant(fresh_database, PROBE, 3)

    rows = run(
        fresh_database,
        UPKEEP_ROLE,
        "SELECT gateway.count_audit_events_before(now())",
    )

    assert rows == [(3,)]
