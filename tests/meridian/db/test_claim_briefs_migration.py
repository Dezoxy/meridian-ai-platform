"""The claim brief's table, claims.briefs (S037, A1).

The migration is found by the end of its name, never by its number or its place
in the list: the number is provisional until the pull request merges, and the
file is the last only until another one lands after it.
"""

import uuid

import psycopg
import pytest
from dbsupport import (
    JOB_ROLES,
    OWNER,
    SERVICE_ROLES,
    UPKEEP_ROLE,
    DatabaseHandle,
)
from sweepmigrationsupport import (
    CLAIM_ID,
    INSERT_CLAIM,
    INSUFFICIENT_PRIVILEGE,
    OTHER_CLAIM_ID,
    TENANT,
    privileges,
    refused,
    run,
)

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

SUFFIX = "_claim_briefs.sql"
TABLE = "claims.briefs"
API = "claims_api"
SWEEP = "claims_sweep"
OTHER_ROLES = tuple(
    role
    for role in (*SERVICE_ROLES, UPKEEP_ROLE, *JOB_ROLES)
    if role not in (API, SWEEP)
)
COLUMNS = (
    "brief_id",
    "claim_id",
    "tenant",
    "run_id",
    "state",
    "brief",
    "created_at",
    "state_changed_at",
)
API_UPDATABLE = ("run_id", "state", "state_changed_at", "brief")
SWEEP_READABLE = ("run_id", "tenant", "state", "state_changed_at")
STATES = ("drafting", "awaiting_decision", "filed", "rejected", "failed")
OPEN_STATES = ("drafting", "awaiting_decision")
CLOSED_STATES = ("filed", "rejected", "failed")
MAX_BRIEF_CHARS = 4000
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
INSERT_BRIEF = (
    "INSERT INTO claims.briefs (brief_id, claim_id, tenant, state) "
    "VALUES (%s, %s, %s, %s)"
)

# What each role holds on the table: the rows the privilege snapshot reports.
API_HOLDS = frozenset(
    {
        ("table", TABLE, "SELECT", ""),
        ("table", TABLE, "INSERT", ""),
        *(("column", TABLE, "SELECT", column) for column in COLUMNS),
        *(("column", TABLE, "UPDATE", column) for column in API_UPDATABLE),
    }
)
SWEEP_HOLDS_ON_BRIEFS = frozenset(
    ("column", TABLE, "SELECT", column) for column in SWEEP_READABLE
)
HOLDS = {API: API_HOLDS, SWEEP: SWEEP_HOLDS_ON_BRIEFS}


def migration_index() -> int:
    (index,) = [
        position
        for position, (name, _) in enumerate(migration_files())
        if name.endswith(SUFFIX)
    ]
    return index


def migration_name() -> str:
    return migration_files()[migration_index()][0]


def on_briefs(db: DatabaseHandle, role: str) -> frozenset[tuple]:
    return frozenset(row for row in privileges(db, role) if row[1] == TABLE)


def add_brief(
    db: DatabaseHandle,
    state: str = "drafting",
    claim_id: str = CLAIM_ID,
    role: str = API,
) -> uuid.UUID:
    brief_id = uuid.uuid4()
    run(db, role, INSERT_BRIEF, (brief_id, claim_id, TENANT, state))
    return brief_id


@pytest.fixture
def claims(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds two claims."""
    run(fresh_database, API, INSERT_CLAIM, (CLAIM_ID, TENANT))
    run(fresh_database, API, INSERT_CLAIM, (OTHER_CLAIM_ID, TENANT))
    return fresh_database


# ── the migration ───────────────────────────────────────────────────────────
def test_the_migration_is_recorded(migrated_database: DatabaseHandle) -> None:
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert (migration_name(),) in recorded


def test_the_table_has_the_columns_of_the_contract(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'briefs' "
        "ORDER BY ordinal_position",
    )

    assert rows == [
        ("brief_id", "uuid", "NO"),
        ("claim_id", "text", "NO"),
        ("tenant", "text", "NO"),
        ("run_id", "uuid", "YES"),
        ("state", "text", "NO"),
        ("brief", "text", "YES"),
        ("created_at", "timestamp with time zone", "NO"),
        ("state_changed_at", "timestamp with time zone", "NO"),
    ]


def test_a_new_brief_has_no_run_no_text_and_both_times_set(
    claims: DatabaseHandle,
) -> None:
    add_brief(claims)

    ((run_id, text, created, changed),) = run(
        claims,
        OWNER,
        "SELECT run_id, brief, created_at IS NOT NULL, state_changed_at IS NOT NULL "
        "FROM claims.briefs",
    )

    assert (run_id, text, created, changed) == (None, None, True, True)


# ── what the table refuses ──────────────────────────────────────────────────
@pytest.mark.parametrize("state", STATES)
def test_each_state_of_the_closed_set_is_accepted(
    claims: DatabaseHandle, state: str
) -> None:
    add_brief(claims, state)

    assert run(claims, OWNER, "SELECT state FROM claims.briefs") == [(state,)]


@pytest.mark.parametrize("state", ["", "Drafting", "done", "approved"])
def test_a_state_outside_the_closed_set_is_refused(
    claims: DatabaseHandle, state: str
) -> None:
    assert refused(
        claims, API, INSERT_BRIEF, (uuid.uuid4(), CLAIM_ID, TENANT, state)
    ) == (CHECK_VIOLATION)


def test_a_brief_of_the_longest_length_is_stored_and_one_character_more_is_refused(
    claims: DatabaseHandle,
) -> None:
    brief_id = add_brief(claims)
    update = "UPDATE claims.briefs SET brief = %s WHERE brief_id = %s"

    run(claims, API, update, ("x" * MAX_BRIEF_CHARS, brief_id))
    sqlstate = refused(claims, API, update, ("x" * (MAX_BRIEF_CHARS + 1), brief_id))

    assert sqlstate == CHECK_VIOLATION
    ((stored,),) = run(claims, OWNER, "SELECT char_length(brief) FROM claims.briefs")
    assert stored == MAX_BRIEF_CHARS


def test_the_length_is_counted_in_characters_and_not_bytes(
    claims: DatabaseHandle,
) -> None:
    brief_id = add_brief(claims)

    run(
        claims,
        API,
        "UPDATE claims.briefs SET brief = %s WHERE brief_id = %s",
        ("é" * MAX_BRIEF_CHARS, brief_id),
    )

    ((characters,),) = run(
        claims, OWNER, "SELECT char_length(brief) FROM claims.briefs"
    )
    assert characters == MAX_BRIEF_CHARS


def test_a_brief_for_a_claim_that_does_not_exist_is_refused(
    claims: DatabaseHandle,
) -> None:
    sqlstate = refused(
        claims, API, INSERT_BRIEF, (uuid.uuid4(), "CLM-9999", TENANT, "drafting")
    )

    assert sqlstate == FOREIGN_KEY_VIOLATION


def test_a_brief_has_no_tenant_less_form(claims: DatabaseHandle) -> None:
    sqlstate = refused(
        claims,
        API,
        "INSERT INTO claims.briefs (brief_id, claim_id, state) VALUES (%s, %s, %s)",
        (uuid.uuid4(), CLAIM_ID, "drafting"),
    )

    assert sqlstate == "23502"


def test_two_briefs_cannot_name_one_run(claims: DatabaseHandle) -> None:
    first = add_brief(claims, "failed")
    second = add_brief(claims, "failed")
    run_id = uuid.uuid4()
    update = "UPDATE claims.briefs SET run_id = %s WHERE brief_id = %s"

    run(claims, API, update, (run_id, first))

    assert refused(claims, API, update, (run_id, second)) == UNIQUE_VIOLATION


@pytest.mark.parametrize("first", OPEN_STATES)
@pytest.mark.parametrize("second", OPEN_STATES)
def test_a_claim_cannot_have_a_second_open_brief(
    claims: DatabaseHandle, first: str, second: str
) -> None:
    add_brief(claims, first)

    sqlstate = refused(
        claims, API, INSERT_BRIEF, (uuid.uuid4(), CLAIM_ID, TENANT, second)
    )

    assert sqlstate == UNIQUE_VIOLATION


@pytest.mark.parametrize("closed", CLOSED_STATES)
@pytest.mark.parametrize("opened", OPEN_STATES)
def test_a_claim_may_have_an_open_brief_beside_closed_ones(
    claims: DatabaseHandle, closed: str, opened: str
) -> None:
    add_brief(claims, closed)
    add_brief(claims, closed)

    add_brief(claims, opened)

    states = run(claims, OWNER, "SELECT state FROM claims.briefs ORDER BY state")
    assert sorted(state for (state,) in states) == sorted([closed, closed, opened])


def test_two_claims_may_each_have_an_open_brief(claims: DatabaseHandle) -> None:
    add_brief(claims, "awaiting_decision", CLAIM_ID)

    add_brief(claims, "awaiting_decision", OTHER_CLAIM_ID)

    assert run(claims, OWNER, "SELECT count(*) FROM claims.briefs") == [(2,)]


def test_an_open_brief_that_closes_lets_a_new_one_open(claims: DatabaseHandle) -> None:
    first = add_brief(claims, "awaiting_decision")

    run(
        claims,
        API,
        "UPDATE claims.briefs SET state = 'filed' WHERE brief_id = %s",
        (first,),
    )
    add_brief(claims, "drafting")

    assert run(claims, OWNER, "SELECT count(*) FROM claims.briefs") == [(2,)]


def test_the_open_briefs_index_is_partial_and_unique_and_the_latest_one_is_indexed(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'claims' AND tablename = 'briefs'",
    )
    definitions = [definition for (definition,) in rows]

    open_only = [d for d in definitions if "UNIQUE" in d and "WHERE" in d]
    assert len(open_only) == 1
    assert "(claim_id)" in open_only[0]
    assert "drafting" in open_only[0]
    assert "awaiting_decision" in open_only[0]
    latest = [d for d in definitions if "claim_id, created_at" in d]
    assert len(latest) == 1


# ── what each role holds ────────────────────────────────────────────────────
@pytest.mark.parametrize("role", [API, SWEEP])
def test_each_role_holds_exactly_the_grants_of_the_contract(
    migrated_database: DatabaseHandle, role: str
) -> None:
    held = on_briefs(migrated_database, role)

    assert held == HOLDS[role], (held - HOLDS[role], HOLDS[role] - held)


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_role_holds_anything_on_the_table(
    migrated_database: DatabaseHandle, role: str
) -> None:
    held = on_briefs(migrated_database, role)

    assert held == frozenset()
    # A role with no right on the schema is refused too, by the schema's USAGE.
    sqlstate = refused(migrated_database, role, "SELECT run_id FROM claims.briefs")
    assert sqlstate == INSUFFICIENT_PRIVILEGE


def test_the_sweep_cannot_read_the_text_of_a_brief(claims: DatabaseHandle) -> None:
    add_brief(claims)

    sqlstates = {
        column: refused(claims, SWEEP, f"SELECT {column} FROM claims.briefs")  # noqa: S608
        for column in ("brief", "brief_id", "claim_id", "created_at")
    }
    star = refused(claims, SWEEP, "SELECT * FROM claims.briefs")

    assert set(sqlstates.values()) == {INSUFFICIENT_PRIVILEGE}
    assert star == INSUFFICIENT_PRIVILEGE


def test_the_sweep_reads_the_four_columns_it_is_granted(
    claims: DatabaseHandle,
) -> None:
    add_brief(claims, "awaiting_decision")

    rows = run(
        claims,
        SWEEP,
        "SELECT run_id, tenant, state, state_changed_at FROM claims.briefs",
    )

    assert [(row[1], row[2]) for row in rows] == [(TENANT, "awaiting_decision")]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE claims.briefs SET state = 'failed'",
        "UPDATE claims.briefs SET run_id = gen_random_uuid()",
        "DELETE FROM claims.briefs",
        "INSERT INTO claims.briefs (brief_id, claim_id, tenant, state) "
        "VALUES (gen_random_uuid(), 'CLM-0013', 'development', 'failed')",
    ],
)
def test_the_sweep_writes_nothing_to_the_table(
    claims: DatabaseHandle, statement: str
) -> None:
    add_brief(claims)

    assert refused(claims, SWEEP, statement) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("column", ["brief_id", "claim_id", "tenant", "created_at"])
def test_the_claims_api_cannot_change_what_names_a_brief(
    claims: DatabaseHandle, column: str
) -> None:
    add_brief(claims)

    sqlstate = refused(claims, API, f"UPDATE claims.briefs SET {column} = {column}")  # noqa: S608

    assert sqlstate == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement", ["DELETE FROM claims.briefs", "TRUNCATE claims.briefs"]
)
def test_the_claims_api_cannot_delete_a_brief(
    claims: DatabaseHandle, statement: str
) -> None:
    add_brief(claims)

    assert refused(claims, API, statement) == INSUFFICIENT_PRIVILEGE


def test_the_claims_api_changes_the_four_columns_it_is_granted(
    claims: DatabaseHandle,
) -> None:
    brief_id = add_brief(claims)
    run_id = uuid.uuid4()

    run(
        claims,
        API,
        "UPDATE claims.briefs SET run_id = %s, state = 'awaiting_decision', "
        "brief = 'a brief', state_changed_at = clock_timestamp() "
        "WHERE brief_id = %s",
        (run_id, brief_id),
    )

    assert run(claims, OWNER, "SELECT run_id, state, brief FROM claims.briefs") == [
        (run_id, "awaiting_decision", "a brief")
    ]


# ── the file ────────────────────────────────────────────────────────────────
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


def test_the_migration_gives_the_two_roles_what_they_hold_and_changes_no_other_role(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    roles = (API, SWEEP, *OTHER_ROLES)
    before = {role: privileges(empty_database, role) for role in roles}
    monkeypatch.setattr(
        runner, "migration_files", lambda: files[: migration_index() + 1]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == [migration_name()]
    for role in (API, SWEEP):
        gained = privileges(empty_database, role) - before[role]
        assert gained == HOLDS[role], role
        assert before[role] - privileges(empty_database, role) == frozenset(), role
    for role in OTHER_ROLES:
        assert privileges(empty_database, role) == before[role], role


def test_a_migration_run_by_a_role_that_does_not_own_the_schema_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    text = dict(files)[migration_name()]

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema claims"
        ):
            conn.execute(text)
        conn.rollback()

    ((exists,),) = run(
        empty_database, OWNER, "SELECT to_regclass('claims.briefs') IS NOT NULL"
    )
    assert exists is False


@pytest.mark.parametrize("role", [API, SWEEP])
def test_a_missing_role_fails_clearly_and_creates_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    index = migration_index()
    name, text = files[index]
    swapped = text.replace(f"'{role}'", "'role_that_does_not_exist'")
    assert swapped != text
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:index], (name, swapped)]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match="required role role_that_does_not_exist does not exist",
        ):
            runner.apply_migrations(conn)
        conn.rollback()

    ((exists,),) = run(
        empty_database, OWNER, "SELECT to_regclass('claims.briefs') IS NOT NULL"
    )
    assert exists is False


def test_the_file_starts_with_a_lock_timeout_and_says_which_lock_it_takes() -> None:
    text = dict(migration_files())[migration_name()]

    body = "\n".join(
        line for line in text.splitlines() if not line.startswith("--") and line.strip()
    )
    assert body.startswith("SET LOCAL lock_timeout = '3s';")
    assert "SHARE ROW EXCLUSIVE" in text
