"""0017: the audit rows of one transaction can be ordered (S065, T-25, T-71).

``audit.events`` gains ``seq``, stamped by the insert trigger from a sequence
the caller cannot touch, so two events written in one transaction (one
``recorded_at``) have an order. The two properties that must hold: no service
role chooses a row's ``seq`` by any form of INSERT, and nothing updates, deletes
or truncates the table, the owner included.
"""

import hashlib

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

NAME = "0017_audit_order.sql"
FORGED_SEQ = 10**12
ROWS_PER_TRANSACTION = 12
TENANT = "development"
CLAIM_ID = "CLM-0170"
TRAIL_COLUMNS = (
    "claim_id",
    "tenant",
    "recorded_at",
    "db_role",
    "service",
    "event",
    "outcome",
    "reason",
    "seq",  # appended by 0017
)
PLANT = (
    "INSERT INTO audit.events (service, event, outcome) "
    "SELECT %s, 'planted-' || n, 'ok' FROM generate_series(1, %s) AS n"
)
# Every form of INSERT that can carry a value for seq, as (statement, rows
# written); %s is the event name, and the forged value is in the statement.
INSERT_HEAD = "INSERT INTO audit.events (seq, service, event, outcome) "
ROW = "(%(seq)s, 'test', %(event)s, 'ok')"
FORMS = {
    "named": (INSERT_HEAD + f"VALUES {ROW}", 1),
    "overriding_system_value": (
        INSERT_HEAD + f"OVERRIDING SYSTEM VALUE VALUES {ROW}",
        1,
    ),
    "overriding_user_value": (INSERT_HEAD + f"OVERRIDING USER VALUE VALUES {ROW}", 1),
    "default": (INSERT_HEAD + "VALUES (DEFAULT, 'test', %(event)s, 'ok')", 1),
    "null": (INSERT_HEAD + "VALUES (NULL, 'test', %(event)s, 'ok')", 1),
    "several_rows": (INSERT_HEAD + f"VALUES {ROW}, {ROW}", 2),
    "select": (INSERT_HEAD + "SELECT %(seq)s, 'test', %(event)s, 'ok'", 1),
    "on_conflict": (INSERT_HEAD + f"VALUES {ROW} ON CONFLICT DO NOTHING", 1),
}


def run(
    db: DatabaseHandle, role: str, statement: str, params: tuple | dict = ()
) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def highest_seq(db: DatabaseHandle) -> int:
    ((highest,),) = run(db, OWNER, "SELECT coalesce(max(seq), 0) FROM audit.events")
    return highest


def stored(db: DatabaseHandle, event: str) -> list[tuple[int, str]]:
    """The ``(seq, db_role)`` of the rows of one event name, in ``seq`` order."""
    return run(
        db,
        OWNER,
        "SELECT seq, db_role FROM audit.events WHERE event = %s ORDER BY seq",
        (event,),
    )


@pytest.fixture
def planted_database(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> DatabaseHandle:
    """Two transactions of twelve rows written before 0017, then 0017 applied
    and nothing after it: a later migration must not change what these tests
    see."""
    files = migration_files()
    monkeypatch.setattr(
        runner, "migration_files", lambda: [(n, t) for n, t in files if n < NAME]
    )
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
        for service in ("first", "second"):
            conn.execute(PLANT, (service, ROWS_PER_TRANSACTION))
            conn.commit()
        monkeypatch.setattr(
            runner, "migration_files", lambda: [(n, t) for n, t in files if n <= NAME]
        )
        assert runner.apply_migrations(conn) == [NAME]
    return empty_database


# ── the migration and the rows that existed ─────────────────────────────────
def test_the_migration_is_the_seventeenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert names[16] == NAME
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


def test_rows_written_before_the_migration_get_distinct_seq_in_time_then_id_order(
    planted_database: DatabaseHandle,
) -> None:
    rows = run(
        planted_database,
        OWNER,
        "SELECT seq, service, recorded_at, event_id FROM audit.events ORDER BY seq",
    )

    assert [seq for seq, *_ in rows] == list(range(1, 2 * ROWS_PER_TRANSACTION + 1))
    # The first transaction started first: its rows come first.
    assert [service for _, service, *_ in rows] == (
        ["first"] * ROWS_PER_TRANSACTION + ["second"] * ROWS_PER_TRANSACTION
    )
    # Inside a transaction the stamp is one time and the ids are random, so the
    # order is the ids': the only thing recorded that tells the rows apart.
    keys = [(recorded_at, event_id) for _, _, recorded_at, event_id in rows]
    assert keys == sorted(keys)


def test_the_first_row_written_after_the_migration_follows_the_last_one_before(
    planted_database: DatabaseHandle,
) -> None:
    run(planted_database, "claims_api", FORMS["default"][0], {"event": "after"})

    last_before = 2 * ROWS_PER_TRANSACTION
    assert stored(planted_database, "after") == [(last_before + 1, "claims_api")]


def test_the_column_is_a_required_bigint_with_no_default_of_its_own(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT data_type, is_nullable, column_default, is_identity "
        "FROM information_schema.columns "
        "WHERE table_schema = 'audit' AND table_name = 'events' "
        "AND column_name = 'seq'",
    )

    # No default: a default is evaluated as the inserting role, which would need
    # the sequence's USAGE. The trigger gives the value, and a row the trigger
    # missed would be refused by NOT NULL, not given a number.
    assert rows == [("bigint", "NO", None, "NO")]


def test_the_sequence_is_owned_by_the_column(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_serial_sequence('audit.events', 'seq')",
    )

    assert rows == [("audit.events_seq",)]


# ── two events of one transaction ───────────────────────────────────────────
def test_two_events_of_one_transaction_share_a_time_and_are_ordered_as_written(
    migrated_database: DatabaseHandle,
) -> None:
    # Named so that the alphabetical order is the wrong one.
    with connect(migrated_database.dsn("claims_api"), "test") as conn:
        for event in ("order.zulu", "order.alpha", "order.mike"):
            conn.execute(
                "INSERT INTO audit.events (service, event, outcome) "
                "VALUES ('test', %s, 'ok')",
                (event,),
            )
        conn.commit()

    rows = run(
        migrated_database,
        OWNER,
        "SELECT event, recorded_at, seq FROM audit.events "
        "WHERE event LIKE 'order.%%' ORDER BY seq",
    )

    events = [event for event, _, _ in rows]
    assert events == ["order.zulu", "order.alpha", "order.mike"]
    assert len({recorded_at for _, recorded_at, _ in rows}) == 1
    seqs = [seq for _, _, seq in rows]
    assert seqs == sorted(set(seqs))


def test_a_row_written_later_in_another_session_has_the_greater_seq(
    migrated_database: DatabaseHandle,
) -> None:
    default_form = FORMS["default"][0]
    run(migrated_database, "agent_runtime", default_form, {"event": "session.first"})
    run(migrated_database, "model_gateway", default_form, {"event": "session.second"})

    ((first, _),) = stored(migrated_database, "session.first")
    ((second, _),) = stored(migrated_database, "session.second")
    assert first < second


# ── no service role chooses a seq ───────────────────────────────────────────
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_a_service_role_cannot_choose_the_seq_by_any_form_of_insert(
    migrated_database: DatabaseHandle, role: str, form: str
) -> None:
    statement, count = FORMS[form]
    event = f"forge.{form}.{role}"
    before = highest_seq(migrated_database)

    run(migrated_database, role, statement, {"seq": FORGED_SEQ, "event": event})

    rows = stored(migrated_database, event)
    assert len(rows) == count
    seqs = [seq for seq, _ in rows]
    assert all(before < seq < FORGED_SEQ for seq in seqs)
    assert seqs == sorted(set(seqs))
    # The identity of the writer is still the session user's.
    assert {db_role for _, db_role in rows} == {role}


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_a_service_role_cannot_choose_the_seq_by_copy(
    migrated_database: DatabaseHandle, role: str
) -> None:
    event = f"forge.copy.{role}"
    before = highest_seq(migrated_database)

    with connect(migrated_database.dsn(role), "test") as conn:
        with conn.cursor().copy(
            "COPY audit.events (seq, service, event, outcome) FROM STDIN"
        ) as copy:
            copy.write_row((FORGED_SEQ, "test", event, "ok"))
        conn.commit()

    ((seq, db_role),) = stored(migrated_database, event)
    assert before < seq < FORGED_SEQ
    assert db_role == role


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_a_service_role_holds_no_privilege_on_the_sequence(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_sequence_privilege(%s, 'audit.events_seq', 'USAGE'), "
        "has_sequence_privilege(%s, 'audit.events_seq', 'SELECT'), "
        "has_sequence_privilege(%s, 'audit.events_seq', 'UPDATE')",
        (role, role, role),
    )

    assert rows == [(False, False, False)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, "SELECT nextval('audit.events_seq')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, "SELECT setval('audit.events_seq', 1)")


def test_nothing_is_granted_on_the_sequence_to_anyone_but_its_owner(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT relacl, relowner::regrole::text FROM pg_class "
        "WHERE oid = 'audit.events_seq'::regclass",
    )

    assert rows == [(None, OWNER)]


def test_the_stamp_function_is_the_owners_with_a_fixed_search_path(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT prosecdef, proowner::regrole::text, proconfig, proacl::text[] "
        "FROM pg_proc WHERE oid = 'audit.stamp_event'::regproc",
    )

    # SECURITY DEFINER so that the owner, not the writer, takes the nextval;
    # the grant list is the owner's alone, as 0001 left it.
    assert rows == [(True, OWNER, ["search_path=pg_catalog"], [f"{OWNER}=X/{OWNER}"])]


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_a_service_role_cannot_call_the_stamp_function(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_function_privilege(%s, 'audit.stamp_event()', 'EXECUTE')",
        (role,),
    )

    assert rows == [(False,)]


# ── still insert-only, for the owner too ────────────────────────────────────
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.events SET seq = 1",
        "UPDATE audit.events SET seq = seq + 1000000",
        "UPDATE audit.events SET outcome = 'changed'",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
    ],
)
def test_the_owner_still_cannot_update_delete_or_truncate_the_audit_log(
    planted_database: DatabaseHandle, statement: str
) -> None:
    before = run(
        planted_database, OWNER, "SELECT seq, event_id FROM audit.events ORDER BY seq"
    )

    with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
        run(planted_database, OWNER, statement)

    after = run(
        planted_database, OWNER, "SELECT seq, event_id FROM audit.events ORDER BY seq"
    )
    assert after == before
    assert len(before) == 2 * ROWS_PER_TRANSACTION


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_a_service_role_cannot_update_the_seq(
    migrated_database: DatabaseHandle, role: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(migrated_database, role, "UPDATE audit.events SET seq = 1")


# ── audit.claim_trail ───────────────────────────────────────────────────────
def test_the_view_has_the_columns_it_had_and_seq_last(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'audit' AND table_name = 'claim_trail' "
        "ORDER BY ordinal_position",
    )

    assert tuple(name for (name,) in rows) == TRAIL_COLUMNS


def test_the_view_is_still_a_security_barrier_that_only_the_claims_api_reads(
    migrated_database: DatabaseHandle,
) -> None:
    options = run(
        migrated_database,
        OWNER,
        "SELECT reloptions FROM pg_class WHERE oid = 'audit.claim_trail'::regclass",
    )
    grants = run(
        migrated_database,
        OWNER,
        "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'audit' AND table_name = 'claim_trail' "
        "AND grantee <> %s ORDER BY grantee, privilege_type",
        (OWNER,),
    )
    others = run(
        migrated_database,
        OWNER,
        "SELECT r, has_any_column_privilege(r, 'audit.claim_trail', 'SELECT') "
        "FROM unnest(%s::text[]) AS r",
        ([role for role in SERVICE_ROLES if role != "claims_api"],),
    )

    assert options == [(["security_barrier=true"],)]
    assert grants == [("claims_api", "SELECT")]
    assert all(not can_read for _, can_read in others)


def test_the_trail_shows_the_seq_of_the_event_and_orders_two_events_of_a_transaction(
    migrated_database: DatabaseHandle,
) -> None:
    run(
        migrated_database,
        "claims_api",
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES (%s, %s, '{}')",
        (CLAIM_ID, TENANT),
    )
    with connect(migrated_database.dsn("claims_api"), "test") as conn:
        for event in ("claim.zulu", "claim.alpha"):
            conn.execute(
                "INSERT INTO audit.events (service, event, outcome, tenant, reference) "
                "VALUES ('claims-api', %s, 'ok', %s, %s)",
                (event, TENANT, CLAIM_ID),
            )
        conn.commit()

    trail = run(
        migrated_database,
        "claims_api",
        "SELECT event, seq FROM audit.claim_trail WHERE claim_id = %s "
        "ORDER BY recorded_at, seq",
        (CLAIM_ID,),
    )
    events = run(
        migrated_database,
        OWNER,
        "SELECT event, seq FROM audit.events WHERE reference = %s ORDER BY seq",
        (CLAIM_ID,),
    )

    assert [event for event, _ in trail] == ["claim.zulu", "claim.alpha"]
    assert trail == events
