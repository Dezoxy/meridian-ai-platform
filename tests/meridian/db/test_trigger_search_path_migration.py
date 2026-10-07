"""0031, second half: the older trigger functions pin ``pg_temp`` last (S068).

Four trigger functions of the first thirty files pinned ``pg_catalog`` alone:
``audit.stamp_event`` (0001, replaced by 0017), ``gateway.forbid_reopen`` (0003)
and the sweep's two (0014). A path that does not name ``pg_temp`` searches the
session's temporary schema FIRST for relations and types, so a later edit of a
body that names an unqualified table could be shadowed by a temporary one; naming
it last makes it the last place looked in. The file alters the four functions'
configuration and nothing else. These tests read the configuration from the
catalog, probe each function with look-alikes in the firing session's temporary
schema, and read the whole migrated catalog so that a later trigger function or
SECURITY DEFINER function without the pin fails the suite.

The probes are a regression guard, not a proof that the file fixed something:
none of the four bodies names an unqualified relation or type (the only thing a
temporary schema shadows implicitly), so they pass before the file and after it.
The control shows what the file does change, on a name no function of ours uses.
The tests that fail first are the catalog ones. The migration is found by the end
of its name, never by its number.
"""

import datetime
import uuid

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from sweepmigrationsupport import (
    CLAIM_ID,
    CLAIM_MESSAGE,
    INSERT_EVENT,
    RUN_MESSAGE,
    TENANT,
    run,
    start_run,
)
from sweepmigrationsupport import (
    claim as claim,  # a fixture: pytest finds it in this module's namespace
)

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

SUFFIX = "_idle_timeout_pg_temp.sql"
PINNED = ["search_path=pg_catalog, pg_temp"]
FOUR = (
    "audit.stamp_event()",
    "gateway.forbid_reopen()",
    "claims.confine_sweep_claim_moves()",
    "runtime.confine_sweep_run_moves()",
)
FUNCTIONS_THAT_NEED_THE_PIN = (
    "SELECT n.nspname || '.' || p.proname, p.proconfig "
    "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace "
    "CROSS JOIN (SELECT oid FROM pg_roles WHERE rolname = %s) AS owner "
    "WHERE (p.proowner = owner.oid OR n.nspowner = owner.oid) "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND (p.prorettype = 'pg_catalog.trigger'::regtype OR p.prosecdef "
    "OR p.oid IN (SELECT tgfoid FROM pg_trigger WHERE NOT tgisinternal)) "
    "ORDER BY 1"
)
HIJACKED_TIME = datetime.datetime(1999, 1, 1, tzinfo=datetime.UTC)
HIJACKED_EVENT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000ff")
HIJACKED_SEQUENCE = -1
CALL_ID = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
INSERT_USAGE = (
    "INSERT INTO gateway.usage (call_id, tenant, agent, run_id, deployment, "
    "provider, model, day, month, reserved_tokens, reserved_micro_eur, "
    "charged_tokens, charged_micro_eur) "
    "VALUES (%s, 'development', 'claims-triage', %s, 'aoai-sdc-gpt-4o', "
    "'azure-openai', 'gpt-4o', '2026-10-01', '2026-10-01', 100, 5, 100, 5)"
)
CLOSE_USAGE = (
    "UPDATE gateway.usage SET state = 'settled', closed_at = now() WHERE call_id = %s"
)
CHANGE_CLOSED_USAGE = "UPDATE gateway.usage SET charged_tokens = 0 WHERE call_id = %s"
REOPEN_MESSAGE = "gateway.usage: only a reserved row can be closed, once"

# Look-alikes made in the firing session's own temporary schema: a table of the
# name of each table a guard sits on (and of the stamp's sequence), so a body that
# named one without its schema would read the temporary one.
LOOK_ALIKE_TABLES = (
    "CREATE TEMP TABLE events (event_id uuid, recorded_at timestamptz)",
    "CREATE TEMP TABLE events_seq (last_value bigint)",
    "CREATE TEMP TABLE usage (state text)",
    "CREATE TEMP TABLE claims (state text)",
    "CREATE TEMP TABLE runs (status text)",
)
# The control: a temporary table named like a catalog the session reads, and
# what a count of it gives under each path.
SHADOW = "CREATE TEMP TABLE pg_database (datname name)"
COUNT_DATABASES = "SELECT count(*) FROM pg_database"
LOOK_ALIKE_STAMPS = (
    "CREATE FUNCTION pg_temp.now() RETURNS timestamptz LANGUAGE sql AS "
    "$$ SELECT '1999-01-01 00:00:00+00'::timestamptz $$",
    "CREATE FUNCTION pg_temp.gen_random_uuid() RETURNS uuid LANGUAGE sql AS "
    f"$$ SELECT '{HIJACKED_EVENT_ID}'::uuid $$",
    "CREATE FUNCTION pg_temp.nextval(regclass) RETURNS bigint LANGUAGE sql AS "
    f"$$ SELECT {HIJACKED_SEQUENCE}::bigint $$",
)


def migration_text() -> str:
    (text,) = [text for name, text in migration_files() if name.endswith(SUFFIX)]
    return text


def pinned_path_ends_in_pg_temp(config: list[str] | None) -> bool:
    """Whether a function's stored configuration pins a search path whose last
    schema is ``pg_temp``."""
    for entry in config or []:
        name, _, value = entry.partition("=")
        if name == "search_path":
            return [part.strip() for part in value.split(",")][-1] == "pg_temp"
    return False


def refusal_among(
    db: DatabaseHandle,
    role: str,
    look_alikes: tuple[str, ...],
    statement: str,
    params: tuple = (),
) -> str | None:
    """Make ``look_alikes`` in the session's temporary schema, run ``statement``
    in that session and return a trigger's refusal, or ``None`` if it ran."""
    try:
        with connect(db.dsn(role), "test") as conn:
            for look_alike in look_alikes:
                conn.execute(look_alike)
            conn.execute(statement, params)
            conn.commit()
    except psycopg.errors.RaiseException as exc:
        return exc.diag.message_primary
    return None


# ── the helper that reads a stored path ─────────────────────────────────────
@pytest.mark.parametrize(
    "config",
    [["search_path=pg_catalog, pg_temp"], ["a=b", "search_path=app,pg_temp"]],
)
def test_a_path_that_ends_in_pg_temp_is_pinned(config: list[str]) -> None:
    assert pinned_path_ends_in_pg_temp(config)


@pytest.mark.parametrize(
    "config",
    [
        None,
        [],
        ["search_path=pg_catalog"],
        ["search_path=pg_temp, pg_catalog"],
        ["statement_timeout=10s"],
    ],
)
def test_a_path_that_does_not_end_in_pg_temp_is_not_pinned(
    config: list[str] | None,
) -> None:
    assert not pinned_path_ends_in_pg_temp(config)


# ── the catalog ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("function", FOUR)
def test_each_older_trigger_function_names_pg_temp_last(
    migrated_database: DatabaseHandle, function: str
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT proconfig FROM pg_proc WHERE oid = %s::regprocedure",
        (function,),
    )

    assert rows == [(PINNED,)]


def test_the_alter_changes_neither_the_definer_flag_nor_the_grants(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT p.oid::regprocedure::text, p.prosecdef, p.proowner::regrole::text, "
        "p.proacl::text[] FROM pg_proc AS p "
        "WHERE p.oid = ANY (%s::regprocedure[]) ORDER BY 1",
        (list(FOUR),),
    )

    # Only the stamp function is SECURITY DEFINER (0017), as before; every one is
    # the owner's and executable by the owner alone (0001, 0003, 0014).
    assert rows == [
        (function, function == "audit.stamp_event()", OWNER, [f"{OWNER}=X/{OWNER}"])
        for function in sorted(FOUR)
    ]


def test_every_trigger_and_definer_function_of_the_platform_pins_pg_temp_last(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(migrated_database, OWNER, FUNCTIONS_THAT_NEED_THE_PIN, (OWNER,))

    unpinned = [
        name for name, config in rows if not pinned_path_ends_in_pg_temp(config)
    ]
    assert {name + "()" for name, _ in rows} >= set(FOUR)
    assert unpinned == []


def test_the_migration_says_what_it_does_not_build() -> None:
    text = migration_text().lower()
    statements = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )

    # Decision 8: the right to make temporary tables stays with PUBLIC; the
    # header says so, and the file takes nothing from it.
    assert "not built" in text
    assert "temporary tables" in text
    assert "revoke" not in statements


# ── the probes: a look-alike in the firing session's temporary schema ──────
def test_a_session_whose_temporary_schema_holds_look_alikes_is_still_stamped(
    fresh_database: DatabaseHandle,
) -> None:
    reference = "CLM-0031"

    refusal = refusal_among(
        fresh_database,
        "claims_api",
        (*LOOK_ALIKE_TABLES, *LOOK_ALIKE_STAMPS),
        INSERT_EVENT,
        ("claims-api", "claim.received", TENANT, None, reference, "probe"),
    )

    ((event_id, recorded_at, role, sequence),) = run(
        fresh_database,
        OWNER,
        "SELECT event_id, recorded_at, db_role, seq FROM audit.events "
        "WHERE reference = %s",
        (reference,),
    )
    assert refusal is None
    assert event_id != HIJACKED_EVENT_ID
    assert recorded_at > HIJACKED_TIME + datetime.timedelta(days=365)
    assert (role, sequence > 0) == ("claims_api", True)


def test_a_path_that_names_pg_temp_last_is_what_keeps_a_temporary_table_from_shadowing(
    fresh_database: DatabaseHandle,
) -> None:
    # The control of the probes: the old pin (pg_catalog alone) leaves the
    # temporary schema first for relations, so a temporary table named like a
    # catalog wins; the new one puts it last, so the catalog does. This is what
    # the file changes, shown on a name no function of ours uses.
    with connect(fresh_database.dsn("claims_sweep"), "test") as conn:
        conn.execute(SHADOW)
        conn.execute("SET LOCAL search_path = pg_catalog")
        old_pin = conn.execute(COUNT_DATABASES).fetchone()
        conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")
        new_pin = conn.execute(COUNT_DATABASES).fetchone()

    assert old_pin == (0,)
    assert new_pin is not None
    assert new_pin[0] > 0


def test_a_closed_usage_row_stays_closed_among_look_alike_tables(
    fresh_database: DatabaseHandle,
) -> None:
    run(fresh_database, "model_gateway", INSERT_USAGE, (CALL_ID, RUN_ID))
    run(fresh_database, "model_gateway", CLOSE_USAGE, (CALL_ID,))

    refusal = refusal_among(
        fresh_database,
        "model_gateway",
        LOOK_ALIKE_TABLES,
        CHANGE_CLOSED_USAGE,
        (CALL_ID,),
    )

    assert refusal == REOPEN_MESSAGE


def test_the_sweep_still_cannot_decide_a_claim_among_look_alike_tables(
    claim: DatabaseHandle,
) -> None:
    refusal = refusal_among(
        claim,
        "claims_sweep",
        LOOK_ALIKE_TABLES,
        "UPDATE claims.claims SET state = 'approved' WHERE claim_id = %s",
        (CLAIM_ID,),
    )

    assert refusal == CLAIM_MESSAGE


def test_the_sweep_still_cannot_complete_a_run_among_look_alike_tables(
    claim: DatabaseHandle,
) -> None:
    run_id, _ = start_run(claim, "Running")

    refusal = refusal_among(
        claim,
        "claims_sweep",
        LOOK_ALIKE_TABLES,
        "UPDATE runtime.runs SET status = 'Completed', updated_at = now() "
        "WHERE run_id = %s",
        (run_id,),
    )

    assert refusal == RUN_MESSAGE
