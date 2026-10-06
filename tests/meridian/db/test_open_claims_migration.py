"""The view of open claims the policy server reads (S067, T-76).

0013's view holds the decided claims; this file's view holds the claims that
are still open, so that claims filed against one policy before any is decided
count towards ``frequent_claims`` too. The migration is found by the end of its
name, never by its number or its place in the list: the number is provisional
until the pull request merges.
"""

import datetime
import re
from typing import get_args

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg.types.json import Jsonb

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files
from meridian.platform.policy_mcp.tools import SELECT_HISTORY
from meridian.workloads.claims_triage.lifecycle import LifecycleState

SUFFIX = "_open_claims.sql"
INDEX = "claims_open_idx"
ROLE = "policy_mcp"
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
TENANT = "development"
CLAIM = "CLM-0013"
INSUFFICIENT_PRIVILEGE = "42501"
OPEN_STATES = (
    "submitted",
    "triaging",
    "triage_failed",
    "awaiting_adjuster",
    "documents_requested",
)
CLOSED_STATES = ("approved", "rejected", "withdrawn")
SELECT_VIEW = (
    "SELECT claim_id, tenant, policy_number, loss_date, peril, paid_amount, state "
    "FROM claims.open_claims ORDER BY claim_id"
)
INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission, state) "
    "VALUES (%s, %s, %s, %s)"
)
INSERT_PROPOSAL = (
    "INSERT INTO claims.triage_proposals (proposal_id, claim_id, run_id, route, "
    "reason, proposal) VALUES (gen_random_uuid(), %s, gen_random_uuid(), "
    "'adjuster', 'r', %s)"
)


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def refused(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> str:
    """The SQLSTATE of the error ``statement`` raises as ``role``."""
    with pytest.raises(psycopg.Error) as caught:
        run(db, role, statement, params)
    return caught.value.sqlstate or "none"


def submission(**fields: object) -> Jsonb:
    base = {"policy_number": "POL-0001", "loss_date": "2026-07-13", "peril": "storm"}
    return Jsonb({**base, **fields})


def add_claim(
    db: DatabaseHandle, claim_id: str, state: str, body: Jsonb | None = None
) -> None:
    run(db, OWNER, INSERT_CLAIM, (claim_id, TENANT, body or submission(), state))


def migration_index() -> int:
    (index,) = [
        position
        for position, (name, _) in enumerate(migration_files())
        if name.endswith(SUFFIX)
    ]
    return index


def migration_name() -> str:
    return migration_files()[migration_index()][0]


def test_the_migration_is_recorded_after_the_decided_claims_view(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names.index("0013_decided_claims.sql") < migration_index()
    assert (migration_name(),) in recorded


def test_the_view_has_exactly_the_seven_columns_of_the_decided_view_in_order(
    migrated_database: DatabaseHandle,
) -> None:
    query = (
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = %s ORDER BY ordinal_position"
    )

    open_columns = run(migrated_database, OWNER, query, ("open_claims",))
    decided_columns = run(migrated_database, OWNER, query, ("decided_claims",))

    assert [name for name, _ in open_columns] == [
        "claim_id",
        "tenant",
        "policy_number",
        "loss_date",
        "peril",
        "paid_amount",
        "state",
    ]
    assert open_columns == decided_columns


def test_the_view_is_a_security_barrier(migrated_database: DatabaseHandle) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT reloptions FROM pg_class WHERE oid = 'claims.open_claims'::regclass",
    )

    assert rows == [(["security_barrier=true"],)]


def test_the_view_holds_the_five_open_states_and_no_other(
    fresh_database: DatabaseHandle,
) -> None:
    for number, state in enumerate((*OPEN_STATES, *CLOSED_STATES), start=1):
        add_claim(fresh_database, f"CLM-{number:04d}", state)

    rows = run(fresh_database, ROLE, "SELECT claim_id, state FROM claims.open_claims")

    assert sorted(rows) == [
        (f"CLM-{number:04d}", state)
        for number, state in enumerate(OPEN_STATES, start=1)
    ]


def test_a_row_carries_what_the_submission_says(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(
        fresh_database,
        CLAIM,
        "documents_requested",
        submission(policy_number="POL-0042", loss_date="2026-05-01", peril="fire"),
    )

    assert run(fresh_database, ROLE, SELECT_VIEW) == [
        (
            CLAIM,
            TENANT,
            "POL-0042",
            datetime.date(2026, 5, 1),
            "fire",
            0,
            "documents_requested",
        )
    ]


def test_an_open_claim_is_paid_nothing_whatever_its_proposal_says(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "awaiting_adjuster")
    run(
        fresh_database,
        OWNER,
        INSERT_PROPOSAL,
        (CLAIM, Jsonb({"route": "adjuster", "reason": "r", "payable_amount": 700})),
    )

    rows = run(fresh_database, ROLE, "SELECT paid_amount FROM claims.open_claims")

    assert rows == [(0,)]


@pytest.mark.parametrize(
    "body",
    [
        Jsonb({}),
        Jsonb({"loss_date": "not a date"}),
        Jsonb({"loss_date": "2026-02-31"}),
        Jsonb({"loss_date": "today"}),
        Jsonb({"loss_date": 20260713}),
    ],
    ids=["empty", "text", "impossible", "keyword", "number"],
)
def test_a_submission_without_a_usable_loss_date_does_not_break_the_view(
    fresh_database: DatabaseHandle, body: Jsonb
) -> None:
    add_claim(fresh_database, "CLM-0001", "submitted", body)
    add_claim(fresh_database, "CLM-0002", "submitted")

    rows = run(
        fresh_database,
        ROLE,
        "SELECT claim_id, loss_date FROM claims.open_claims ORDER BY claim_id",
    )

    assert [claim_id for claim_id, _ in rows] == ["CLM-0001", "CLM-0002"]
    assert rows[0][1] is None
    assert rows[1][1] is not None


@pytest.mark.parametrize(
    ("peril", "kept"),
    [
        ("storm", True),
        ("x" * 64, True),
        ("", False),
        ("x" * 65, False),
        (["storm"], False),
        (7, False),
        (None, False),
    ],
    ids=["word", "at-bound", "empty", "over-bound", "array", "number", "null"],
)
def test_peril_is_null_unless_the_submission_has_a_string_of_1_to_64_characters(
    fresh_database: DatabaseHandle, peril: object, kept: bool
) -> None:
    add_claim(fresh_database, CLAIM, "submitted", submission(peril=peril))

    rows = run(fresh_database, ROLE, "SELECT peril FROM claims.open_claims")

    assert rows == [(peril if kept else None,)]


# The tool's parameters: for each of the three sources the policy number, and
# for the two views the tenant and the run's claim, then the limit.
HISTORY_PARAMETERS = (
    *("POL-0001", "POL-0001", TENANT, "CLM-9999"),
    *("POL-0001", TENANT, "CLM-9999"),
    101,
)
LEAKY_FUNCTION = """
CREATE FUNCTION claims.leaky_open(text) RETURNS boolean LANGUAGE plpgsql
COST 0.0001 AS $$
BEGIN
    IF $1 = 'CLM-6666' THEN
        RAISE EXCEPTION 'leaked';
    END IF;
    RETURN true;
END
$$
"""


def test_a_condition_a_caller_adds_is_not_evaluated_on_a_row_the_view_leaves_out(
    fresh_database: DatabaseHandle,
) -> None:
    # A function that is cheap and not leakproof: without the security barrier
    # PostgreSQL would run it on every claim, a decided one included, ahead of
    # the view's own condition, and it would raise. The scan is a sequential
    # one: through the partial index the decided claim is never read, barrier or
    # not.
    run(fresh_database, OWNER, LEAKY_FUNCTION)
    run(
        fresh_database,
        OWNER,
        f"GRANT EXECUTE ON FUNCTION claims.leaky_open(text) TO {ROLE}",
    )
    add_claim(fresh_database, "CLM-6666", "approved")
    add_claim(fresh_database, "CLM-0002", "submitted")

    with connect(fresh_database.dsn(ROLE), "test") as conn:
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        rows = conn.execute(
            "SELECT claim_id FROM claims.open_claims WHERE claims.leaky_open(claim_id)"
        ).fetchall()

    assert rows == [("CLM-0002",)]


def test_the_tool_s_query_reads_both_views_through_their_partial_indexes(
    fresh_database: DatabaseHandle,
) -> None:
    for number in range(1, 6):
        add_claim(fresh_database, f"CLM-{number:04d}", "submitted")
        add_claim(fresh_database, f"CLM-{number + 10:04d}", "approved")
    with connect(fresh_database.dsn(ROLE), "test") as conn:
        # Whatever the planner's costs at this size, an index it may use is used.
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            line
            for (line,) in conn.execute(
                "EXPLAIN " + SELECT_HISTORY,
                HISTORY_PARAMETERS,
            ).fetchall()
        )

    assert INDEX in plan, plan
    assert "claims_decided_idx" in plan, plan
    conditions = [line for line in plan.splitlines() if "Index Cond" in line]
    assert any("tenant" in c and "policy_number" in c for c in conditions), plan


def test_the_partial_index_predicate_is_exactly_the_five_open_states(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_indexdef(indexrelid), pg_get_expr(indpred, indrelid) "
        "FROM pg_index WHERE indexrelid = 'claims.claims_open_idx'::regclass",
    )

    ((definition, predicate),) = rows
    assert "(tenant, policy_number)" in definition
    assert predicate == (
        "(state = ANY (ARRAY['submitted'::text, 'triaging'::text, "
        "'triage_failed'::text, 'awaiting_adjuster'::text, "
        "'documents_requested'::text]))"
    )


def listed_states(definition: str) -> set[str]:
    """The state literals after the last WHERE of a view's or an index's
    definition, as the catalogue prints it."""
    condition = definition.rsplit("WHERE", 1)[-1]
    return set(re.findall(r"'([a-z_]+)'::text", condition))


def test_the_two_views_and_the_index_list_the_states_of_the_lifecycle_between_them(
    migrated_database: DatabaseHandle,
) -> None:
    # A ninth state added to the lifecycle is in neither view until a file puts
    # it in one; this fails the day the lifecycle and the views part, so that it
    # is decided, not missed.
    ((open_view, decided_view, predicate),) = run(
        migrated_database,
        OWNER,
        "SELECT pg_get_viewdef('claims.open_claims'::regclass), "
        "pg_get_viewdef('claims.decided_claims'::regclass), "
        "pg_get_expr(indpred, indrelid) FROM pg_index "
        "WHERE indexrelid = 'claims.claims_open_idx'::regclass",
    )

    opened, decided = listed_states(open_view), listed_states(decided_view)
    assert opened == set(OPEN_STATES)
    assert decided == {"approved", "rejected"}
    assert opened.isdisjoint(decided)
    assert opened | decided | {"withdrawn"} == set(get_args(LifecycleState))
    assert listed_states(predicate) == opened


def test_the_decided_view_still_holds_only_the_decided_states(
    fresh_database: DatabaseHandle,
) -> None:
    # 0013 is applied and never changes: the old view and its index are as they
    # were, and an open claim is not in them.
    for number, state in enumerate((*OPEN_STATES, *CLOSED_STATES), start=1):
        add_claim(fresh_database, f"CLM-{number:04d}", state)

    rows = run(
        fresh_database, ROLE, "SELECT claim_id, state FROM claims.decided_claims"
    )

    assert sorted(rows) == [("CLM-0006", "approved"), ("CLM-0007", "rejected")]


def test_policy_mcp_may_select_the_view_and_still_not_read_the_submission(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "submitted")

    viewed = run(fresh_database, ROLE, "SELECT claim_id FROM claims.open_claims")
    privileges = run(
        fresh_database,
        OWNER,
        "SELECT has_table_privilege('policy_mcp', 'claims.open_claims', 'SELECT'), "
        "has_column_privilege('policy_mcp', 'claims.claims', 'submission', 'SELECT'), "
        "has_column_privilege('policy_mcp', 'claims.claims', 'state', 'SELECT')",
    )

    assert viewed == [(CLAIM,)]
    assert privileges == [(True, False, False)]
    assert (
        refused(fresh_database, ROLE, "SELECT submission FROM claims.claims")
        == INSUFFICIENT_PRIVILEGE
    )


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_service_role_has_any_privilege_on_the_view(
    migrated_database: DatabaseHandle, role: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'claims.open_claims', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'), "
        "has_any_column_privilege(%s, 'claims.open_claims', "
        "'SELECT, INSERT, UPDATE, REFERENCES')",
        (role, role),
    )

    assert rows == [(False, False)]


def test_the_view_is_granted_to_policy_mcp_alone_and_to_public_not_at_all(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'claims' AND table_name = 'open_claims' "
        "AND grantee <> %s ORDER BY grantee, privilege_type",
        (OWNER,),
    )
    # A grant on one column does not show in role_table_grants: ask for each.
    writes = run(
        migrated_database,
        OWNER,
        "SELECT has_any_column_privilege('policy_mcp', 'claims.open_claims', "
        "'INSERT, UPDATE, REFERENCES')",
    )

    assert rows == [("policy_mcp", "SELECT")]
    assert writes == [(False,)]


# Each statement names plain columns only (the view's literal and computed ones
# cannot be written), so PostgreSQL checks the privilege and nothing else comes
# first: the one SQLSTATE that can come out is a missing privilege.
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO claims.open_claims (claim_id, tenant, state) "
        "VALUES ('CLM-0099', 'development', 'submitted')",
        "UPDATE claims.open_claims SET state = 'approved'",
        "DELETE FROM claims.open_claims",
    ],
)
def test_policy_mcp_cannot_write_through_the_view(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    add_claim(fresh_database, CLAIM, "submitted")

    sqlstate = refused(fresh_database, ROLE, statement)

    assert sqlstate == INSUFFICIENT_PRIVILEGE
    assert run(fresh_database, OWNER, "SELECT count(*) FROM claims.claims") == [(1,)]
    assert run(fresh_database, OWNER, "SELECT state FROM claims.claims") == [
        ("submitted",)
    ]


def test_the_view_is_writable_by_its_shape_so_the_grants_alone_stop_a_write(
    fresh_database: DatabaseHandle,
) -> None:
    # The view is a plain one over one table: PostgreSQL would pass an update of
    # its plain columns to the table. Only the missing grant stops it, which is
    # why the tests above and the grant test below read the privileges.
    add_claim(fresh_database, CLAIM, "submitted")
    run(fresh_database, OWNER, f"GRANT UPDATE (state) ON claims.open_claims TO {ROLE}")

    run(fresh_database, ROLE, "UPDATE claims.open_claims SET state = 'approved'")

    assert run(fresh_database, OWNER, "SELECT state FROM claims.claims") == [
        ("approved",)
    ]


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
        empty_database, OWNER, "SELECT to_regclass('claims.open_claims') IS NOT NULL"
    )
    assert exists is False


def test_a_missing_role_fails_clearly_and_creates_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    text = dict(files)[migration_name()].replace(ROLE, "role_that_does_not_exist")

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match=(
                "required role role_that_does_not_exist does not exist; "
                "create it out of band before migrating"
            ),
        ):
            conn.execute(text)
        conn.rollback()

    assert run(
        empty_database,
        OWNER,
        "SELECT to_regclass('claims.open_claims') IS NOT NULL, "
        "to_regclass('claims.claims_open_idx') IS NOT NULL",
    ) == [(False, False)]


def test_the_migration_gives_policy_mcp_the_one_view_and_changes_no_other_role(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    snapshot = (
        "SELECT c.relname, p.priv FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS p(priv) "
        "WHERE n.nspname = 'claims' AND c.relkind IN ('r', 'v') "
        "AND has_table_privilege(%s, c.oid, p.priv)"
    )
    before = {
        role: set(run(empty_database, OWNER, snapshot, (role,)))
        for role in (ROLE, *OTHER_ROLES)
    }
    monkeypatch.setattr(
        runner, "migration_files", lambda: files[: migration_index() + 1]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == [migration_name()]
    for role in (ROLE, *OTHER_ROLES):
        after = set(run(empty_database, OWNER, snapshot, (role,)))
        gained = {("open_claims", "SELECT")} if role == ROLE else set()
        assert after - before[role] == gained, role
        assert before[role] - after == set(), role
