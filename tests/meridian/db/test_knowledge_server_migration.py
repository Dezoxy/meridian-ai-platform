"""0006: what the knowledge tool server's database role reads and writes (S046)."""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg import sql

from meridian.platform.common.db import connect
from meridian.platform.gateway.replay import replay_embedding
from meridian.platform.knowledge_mcp.search import QueryEmbedding, hybrid_search
from meridian.platform.knowledge_mcp.store import ChunkRow, replace_corpus
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

ROLE = "knowledge_mcp"
OTHER_SERVICE_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
PRODUCT = "HOME-STD"
VERSION = "2026-01"
DEPLOYMENT = "replay-embedding"
DIMENSIONS = 8
SHA = "a" * 64
POLICY_NUMBER = "POL-0006"
CLAIM_ID = "CLM-0006"
GRANTED_CHUNK_COLUMNS = (
    "product",
    "wording_version",
    "clause",
    "section",
    "title",
    "body",
    "lexemes",
    "deployment",
    "dimensions",
    "embedding",
)
WITHHELD_CHUNK_COLUMNS = ("source_sha256", "model", "ingested_at")
GRANTED_POLICY_COLUMNS = "policy_number, product, wording_version"
RUN_COLUMNS = "run_id, agent, tenant, reference, status"
CLAIM_COLUMNS = "claim_id, tenant, policy_number"


def run(
    db: DatabaseHandle, role: str, statement: str | sql.Composable, params: tuple = ()
) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def chunk(clause: str, title: str, body: str) -> ChunkRow:
    return ChunkRow(
        product=PRODUCT,
        wording_version=VERSION,
        clause=clause,
        section="A section",
        title=title,
        body=body,
        source_sha256=SHA,
        deployment=DEPLOYMENT,
        model=DEPLOYMENT,
        dimensions=DIMENSIONS,
        embedding=replay_embedding(f"{title}\n{body}", DIMENSIONS),
    )


@pytest.fixture
def corpus(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own whose store holds three planted clauses
    (the owner writes them, as the ingestion does)."""
    rows = [
        chunk("2.1", "Storm cover", "Damage caused by a storm to the building."),
        chunk("2.2", "Fire cover", "Damage caused by fire or smoke to the building."),
        chunk("4.1", "Deductible", "The deductible is deducted from every claim."),
    ]
    with connect(fresh_database.dsn(OWNER), "test-plant") as writer:
        replace_corpus(writer, rows)
        writer.commit()
    return fresh_database


def seed_policy(db: DatabaseHandle) -> None:
    run(
        db,
        OWNER,
        "INSERT INTO policy.policies (policy_number, product, wording_version, "
        "start_date, end_date, status, deductible, cover_limit) "
        "VALUES (%s, %s, %s, '2026-01-01', '2026-12-31', 'active', 250, 1000)",
        (POLICY_NUMBER, PRODUCT, VERSION),
    )
    run(
        db,
        OWNER,
        "INSERT INTO policy.claim_history (history_id, policy_number, loss_date, "
        "peril, paid_amount, status) "
        "VALUES ('HIST-0006', %s, '2026-02-01', 'storm', 100, 'closed')",
        (POLICY_NUMBER,),
    )


def seed_claim_and_run(db: DatabaseHandle) -> uuid.UUID:
    run(
        db,
        "claims_api",
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES (%s, 'development', %s)",
        (CLAIM_ID, f'{{"policy_number": "{POLICY_NUMBER}"}}'),
    )
    run_id = uuid.uuid4()
    run(
        db,
        "agent_runtime",
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status) VALUES (%s, %s, 'claims-triage', 'development', %s, 'Running')",
        (run_id, uuid.uuid4(), CLAIM_ID),
    )
    return run_id


# ── the migration itself ────────────────────────────────────────────────────
def test_0006_is_the_sixth_migration_file() -> None:
    names = [name for name, _ in migration_files()]

    assert names[5] == "0006_knowledge_server.sql"


def test_a_missing_knowledge_role_fails_clearly_and_changes_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    name, text = files[5]
    broken = text.replace(f"'{ROLE}'", "'role_that_does_not_exist'", 1)
    assert broken != text
    monkeypatch.setattr(runner, "migration_files", lambda: [*files[:5], (name, broken)])

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
        # would have worked: none of them ran.
        assert conn.execute(
            "SELECT has_schema_privilege(%s, 'knowledge', 'USAGE'), "
            "has_schema_privilege(%s, 'policy', 'USAGE'), "
            "has_any_column_privilege(%s, 'knowledge.chunks', 'SELECT')",
            (ROLE, ROLE, ROLE),
        ).fetchone() == (False, False, False)
        assert conn.execute(
            "SELECT count(*) FROM public.meridian_migrations WHERE name = %s", (name,)
        ).fetchone() == (0,)


# ── the search it exists for ────────────────────────────────────────────────
def test_the_knowledge_role_runs_the_real_search(corpus: DatabaseHandle) -> None:
    query = "storm damage"

    with connect(corpus.dsn(ROLE), "test") as conn:
        hits = hybrid_search(
            conn,
            product=PRODUCT,
            wording_version=VERSION,
            query=query,
            embedding=QueryEmbedding(DEPLOYMENT, replay_embedding(query, DIMENSIONS)),
            top_k=3,
        )

    assert {hit.clause for hit in hits} == {"2.1", "2.2", "4.1"}
    assert hits[0].clause == "2.1"


# ── knowledge.chunks ────────────────────────────────────────────────────────
def test_the_knowledge_role_reads_exactly_the_ten_granted_columns(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT a.attname FROM pg_attribute a "
        "WHERE a.attrelid = 'knowledge.chunks'::regclass AND a.attnum > 0 "
        "AND NOT a.attisdropped "
        "AND has_column_privilege(%s, a.attrelid, a.attnum, 'SELECT') "
        "ORDER BY a.attname",
        (ROLE,),
    )

    assert [name for (name,) in rows] == sorted(GRANTED_CHUNK_COLUMNS)


def test_the_columns_listed_in_the_catalogue_are_the_granted_ones(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, privilege_type FROM information_schema.column_privileges "
        "WHERE grantee = %s AND table_schema = 'knowledge' "
        "AND table_name = 'chunks' ORDER BY column_name",
        (ROLE,),
    )

    assert rows == [(name, "SELECT") for name in sorted(GRANTED_CHUNK_COLUMNS)]


@pytest.mark.parametrize("column", GRANTED_CHUNK_COLUMNS)
def test_the_knowledge_role_selects_each_granted_chunk_column(
    corpus: DatabaseHandle, column: str
) -> None:
    rows = run(
        corpus,
        ROLE,
        sql.SQL("SELECT {} FROM knowledge.chunks").format(sql.Identifier(column)),
    )

    assert len(rows) == 3


@pytest.mark.parametrize("column", WITHHELD_CHUNK_COLUMNS)
def test_the_knowledge_role_cannot_select_a_withheld_chunk_column(
    corpus: DatabaseHandle, column: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            corpus,
            ROLE,
            sql.SQL("SELECT {} FROM knowledge.chunks").format(sql.Identifier(column)),
        )


@pytest.mark.parametrize("column", WITHHELD_CHUNK_COLUMNS)
def test_a_withheld_chunk_column_is_no_oracle_in_a_where_clause(
    corpus: DatabaseHandle, column: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            corpus,
            ROLE,
            sql.SQL("SELECT clause FROM knowledge.chunks ORDER BY {}").format(
                sql.Identifier(column)
            ),
        )


def test_the_knowledge_role_cannot_select_star_from_the_chunks(
    corpus: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(corpus, ROLE, "SELECT * FROM knowledge.chunks")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO knowledge.chunks (product) VALUES ('HOME-STD')",
        "UPDATE knowledge.chunks SET body = 'changed'",
        "UPDATE knowledge.chunks SET source_sha256 = repeat('b', 64)",
        "DELETE FROM knowledge.chunks",
        "TRUNCATE knowledge.chunks",
    ],
)
def test_the_knowledge_role_cannot_write_the_chunks(
    corpus: DatabaseHandle, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(corpus, ROLE, statement)


@pytest.mark.parametrize("role", OTHER_SERVICE_ROLES)
def test_no_other_service_role_has_any_privilege_on_the_chunks(
    migrated_database: DatabaseHandle, role: str
) -> None:
    table = [
        privilege
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        )
        if run(
            migrated_database,
            OWNER,
            "SELECT has_table_privilege(%s, 'knowledge.chunks', %s)",
            (role, privilege),
        )[0][0]
    ]
    columns = [
        privilege
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES")
        if run(
            migrated_database,
            OWNER,
            "SELECT has_any_column_privilege(%s, 'knowledge.chunks', %s)",
            (role, privilege),
        )[0][0]
    ]
    schema = run(
        migrated_database,
        OWNER,
        "SELECT has_schema_privilege(%s, 'knowledge', 'USAGE'), "
        "has_schema_privilege(%s, 'knowledge', 'CREATE')",
        (role, role),
    )

    assert table == []
    assert columns == []
    assert schema == [(False, False)]


def test_the_knowledge_role_has_usage_but_not_create_on_the_schema(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        ROLE,
        "SELECT has_schema_privilege('knowledge', 'USAGE'), "
        "has_schema_privilege('knowledge', 'CREATE')",
    )

    assert rows == [(True, False)]


# ── policy.policies, to bind a search to the run's own policy ───────────────
def test_the_knowledge_role_reads_three_columns_of_the_policy(
    fresh_database: DatabaseHandle,
) -> None:
    seed_policy(fresh_database)

    rows = run(
        fresh_database,
        ROLE,
        f"SELECT {GRANTED_POLICY_COLUMNS} FROM policy.policies",  # noqa: S608
    )

    assert rows == [(POLICY_NUMBER, PRODUCT, VERSION)]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT deductible FROM policy.policies",
        "SELECT status FROM policy.policies",
        "SELECT cover_limit FROM policy.policies",
        "SELECT * FROM policy.policies",
        # a column in a WHERE clause is read too: an oracle on the amount
        "SELECT policy_number FROM policy.policies WHERE deductible = 250",
        "SELECT * FROM policy.claim_history",
        "SELECT history_id FROM policy.claim_history",
        "SELECT policy_number FROM policy.claim_history",
    ],
)
def test_the_knowledge_role_cannot_read_more_of_the_policy_store(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    seed_policy(fresh_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(fresh_database, ROLE, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO policy.policies (policy_number, product, wording_version, "
        "start_date, end_date, status, deductible, cover_limit) "
        "VALUES ('POL-7006', 'p', 'v', '2026-01-01', '2026-12-31', 'active', 0, 0)",
        "UPDATE policy.policies SET product = 'x'",
        "DELETE FROM policy.policies",
        "TRUNCATE policy.policies",
        "DELETE FROM policy.claim_history",
    ],
)
def test_the_knowledge_role_cannot_write_the_policy_store(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    seed_policy(fresh_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(fresh_database, ROLE, statement)


# ── the run and the claim it binds to, and the audit log ────────────────────
def test_the_knowledge_role_reads_the_five_run_columns(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = seed_claim_and_run(fresh_database)

    rows = run(
        fresh_database,
        ROLE,
        f"SELECT {RUN_COLUMNS} FROM runtime.runs WHERE run_id = %s",  # noqa: S608
        (run_id,),
    )

    assert rows == [(run_id, "claims-triage", "development", CLAIM_ID, "Running")]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT thread_id FROM runtime.runs",
        "SELECT * FROM runtime.runs",
        "SELECT run_id FROM runtime.runs WHERE thread_id IS NOT NULL",
        "UPDATE runtime.runs SET status = 'Failed'",
        "DELETE FROM runtime.runs",
    ],
)
def test_the_knowledge_role_cannot_go_beyond_the_run_columns(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    seed_claim_and_run(fresh_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(fresh_database, ROLE, statement)


def test_the_knowledge_role_reads_the_three_claim_columns(
    fresh_database: DatabaseHandle,
) -> None:
    seed_claim_and_run(fresh_database)

    rows = run(
        fresh_database,
        ROLE,
        f"SELECT {CLAIM_COLUMNS} FROM claims.claims WHERE claim_id = %s",  # noqa: S608
        (CLAIM_ID,),
    )

    assert rows == [(CLAIM_ID, "development", POLICY_NUMBER)]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT submission FROM claims.claims",
        "SELECT * FROM claims.claims",
        "SELECT claim_id FROM claims.claims WHERE submission IS NOT NULL",
        "UPDATE claims.claims SET tenant = 'x'",
        "DELETE FROM claims.claims",
    ],
)
def test_the_knowledge_role_cannot_go_beyond_the_claim_columns(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    seed_claim_and_run(fresh_database)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(fresh_database, ROLE, statement)


def test_the_knowledge_role_inserts_an_audit_row_and_cannot_read_the_log(
    fresh_database: DatabaseHandle,
) -> None:
    run(
        fresh_database,
        ROLE,
        "INSERT INTO audit.events (service, event, outcome) "
        "VALUES ('knowledge-mcp', 'test', 'ok')",
    )

    rows = run(
        fresh_database,
        OWNER,
        "SELECT db_role, service FROM audit.events WHERE event = 'test'",
    )
    assert rows == [(ROLE, "knowledge-mcp")]
    for statement in (
        "SELECT count(*) FROM audit.events",
        "UPDATE audit.events SET outcome = 'x'",
        "DELETE FROM audit.events",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            run(fresh_database, ROLE, statement)


# ── what it has no part in ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "table",
    [
        "claims.notes",
        "claims.approval_requests",
        "claims.triage_proposals",
        "gateway.budget_counters",
        "gateway.usage",
    ],
)
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM {table}",
        "INSERT INTO {table} DEFAULT VALUES",
        "DELETE FROM {table}",
    ],
)
def test_the_knowledge_role_has_no_part_in_the_other_tables(
    fresh_database: DatabaseHandle, table: str, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(
            fresh_database,
            ROLE,
            sql.SQL(statement).format(table=sql.SQL(table)),
        )


def test_the_knowledge_role_has_no_usage_on_the_gateway_schema(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database, ROLE, "SELECT has_schema_privilege('gateway', 'USAGE')"
    )

    assert rows == [(False,)]
