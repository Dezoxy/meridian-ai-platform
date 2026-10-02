"""0005: the knowledge store, which needs pgvector from a superuser (S012)."""

import hashlib
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg.conninfo import make_conninfo

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import apply_migrations, migration_files

HASH = "a" * 64
INSERT = (
    "INSERT INTO knowledge.chunks (product, wording_version, clause, section, "
    "title, body, source_sha256, deployment, model, dimensions, embedding) "
    "VALUES (%(product)s, %(wording_version)s, %(clause)s, %(section)s, "
    "%(title)s, %(body)s, %(source_sha256)s, %(deployment)s, %(model)s, "
    "%(dimensions)s, %(embedding)s::vector)"
)
CHUNK: dict[str, Any] = {
    "product": "MOTOR-TPL",
    "wording_version": "2026-01",
    "clause": "1.1",
    "section": "Definitions",
    "title": "You and we",
    "body": "In this wording you means the policyholder.",
    "source_sha256": HASH,
    "deployment": "replay-embedding",
    "model": "replay-embedding",
    "dimensions": 3,
    "embedding": "[0.25,0.5,1.0]",
}
EXPECTED_COLUMNS = [
    ("body", "text", "NO"),
    ("clause", "text", "NO"),
    ("deployment", "text", "NO"),
    ("dimensions", "integer", "NO"),
    ("embedding", "USER-DEFINED", "NO"),
    ("ingested_at", "timestamp with time zone", "NO"),
    ("lexemes", "tsvector", "NO"),
    ("model", "text", "NO"),
    ("product", "text", "NO"),
    ("section", "text", "NO"),
    ("source_sha256", "text", "NO"),
    ("title", "text", "NO"),
    ("wording_version", "text", "NO"),
]


def owner_run(db: DatabaseHandle, statement: str, params: Any = ()) -> list[tuple]:
    with connect(db.dsn(OWNER), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def insert_chunk(db: DatabaseHandle, **values: Any) -> None:
    owner_run(db, INSERT, CHUNK | values)


@pytest.fixture
def chunks_database(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database whose chunk table is empty."""
    owner_run(fresh_database, "DELETE FROM knowledge.chunks")
    return fresh_database


def test_the_ledger_holds_the_fifth_migration_file(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = owner_run(
        migrated_database,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert names[4] == "0005_knowledge.sql"
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


def test_a_database_without_pgvector_is_refused_and_leaves_nothing(
    empty_database: DatabaseHandle,
) -> None:
    with psycopg.connect(
        make_conninfo(empty_database.admin_dsn, dbname=empty_database.name),
        autocommit=True,
    ) as admin:
        admin.execute("DROP EXTENSION vector")

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error, match=r'extension "vector" is not installed.*superuser'
        ):
            apply_migrations(conn)
        conn.rollback()

        assert conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname = 'knowledge'"
        ).fetchone() == (0,)


def test_a_vector_type_the_owner_cannot_see_is_refused_and_leaves_nothing(
    empty_database: DatabaseHandle,
) -> None:
    # The extension exists, in a schema that is not on the migrating role's
    # search path: pg_extension says it is there and the type cannot be named.
    with psycopg.connect(
        make_conninfo(empty_database.admin_dsn, dbname=empty_database.name),
        autocommit=True,
    ) as admin:
        admin.execute("DROP EXTENSION vector")
        admin.execute("CREATE SCHEMA hidden")
        admin.execute("CREATE EXTENSION vector SCHEMA hidden")

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(psycopg.Error) as raised:
            apply_migrations(conn)
        conn.rollback()

        assert conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname = 'knowledge'"
        ).fetchone() == (0,)
    message = raised.value.diag.message_primary or ""
    assert 'extension "vector" is not installed' in message
    assert "search path" in message
    assert "superuser" in message
    assert "azure_pg_admin" in message
    assert "allow-listed" in message


def test_the_owner_cannot_create_pgvector_which_is_why_a_superuser_does(
    empty_database: DatabaseHandle,
) -> None:
    with psycopg.connect(
        make_conninfo(empty_database.admin_dsn, dbname=empty_database.name),
        autocommit=True,
    ) as admin:
        admin.execute("DROP EXTENSION vector")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        owner_run(empty_database, "CREATE EXTENSION vector")


def test_the_chunk_table_has_these_columns_all_not_null(
    migrated_database: DatabaseHandle,
) -> None:
    rows = owner_run(
        migrated_database,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'knowledge' AND table_name = 'chunks' "
        "ORDER BY column_name",
    )

    assert rows == EXPECTED_COLUMNS


def test_the_primary_key_is_product_version_and_clause(
    migrated_database: DatabaseHandle,
) -> None:
    rows = owner_run(
        migrated_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'knowledge.chunks'::regclass AND contype = 'p'",
    )

    assert rows == [("PRIMARY KEY (product, wording_version, clause)",)]


def test_a_chunk_is_stored_with_its_generated_lexemes(
    chunks_database: DatabaseHandle,
) -> None:
    insert_chunk(chunks_database, title="Deductible rule", body="You pay storms.")

    ((lexemes, vector, ingested_year),) = owner_run(
        chunks_database,
        "SELECT lexemes::text, embedding::text, "
        "extract(year FROM ingested_at)::int FROM knowledge.chunks",
    )

    # The title's words carry weight A, the body's weight B, both stemmed.
    assert "'deduct':1A" in lexemes
    assert "'storm':" in lexemes
    assert "B" in lexemes.split("'storm':")[1]
    assert vector == "[0.25,0.5,1]"
    assert ingested_year >= 2026


def test_a_title_word_outranks_a_body_word_in_the_generated_lexemes(
    chunks_database: DatabaseHandle,
) -> None:
    insert_chunk(chunks_database, title="Excess", body="Deductible applies.")

    ((title_rank, body_rank),) = owner_run(
        chunks_database,
        "SELECT ts_rank(lexemes, to_tsquery('english', 'excess')), "
        "ts_rank(lexemes, to_tsquery('english', 'deductible')) "
        "FROM knowledge.chunks",
    )

    assert title_rank > body_rank > 0


def test_neither_the_lexemes_nor_the_vectors_are_indexed(
    migrated_database: DatabaseHandle,
) -> None:
    # The only query reads the table through a materialised CTE, so a GIN index
    # on the lexemes could never be used; the vectors are compared exactly.
    rows = owner_run(
        migrated_database,
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'knowledge' AND tablename = 'chunks'",
    )

    definitions = [definition for (definition,) in rows]
    assert not any("embedding" in d for d in definitions)
    assert not any("gin" in d.lower() for d in definitions)
    assert not any("lexemes" in d for d in definitions)
    assert definitions == [
        "CREATE UNIQUE INDEX chunks_pkey ON knowledge.chunks USING btree "
        "(product, wording_version, clause)"
    ]


def test_a_vector_whose_length_is_not_the_dimensions_is_refused(
    chunks_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_chunk(chunks_database, dimensions=4)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_chunk(chunks_database, dimensions=2)

    insert_chunk(chunks_database, dimensions=3)


def test_vectors_of_two_lengths_can_live_in_the_one_table(
    chunks_database: DatabaseHandle,
) -> None:
    insert_chunk(chunks_database, clause="1.1")
    insert_chunk(chunks_database, clause="1.2", dimensions=2, embedding="[1.0,2.0]")

    assert owner_run(
        chunks_database,
        "SELECT dimensions FROM knowledge.chunks ORDER BY clause",
    ) == [(3,), (2,)]


@pytest.mark.parametrize("clause", ["1.1", "12.34", "1.10", "2.10", "99.99", "10.1"])
def test_a_clause_number_of_one_or_two_digits_either_side_is_accepted(
    chunks_database: DatabaseHandle, clause: str
) -> None:
    insert_chunk(chunks_database, clause=clause)


@pytest.mark.parametrize(
    "clause",
    [
        "1",
        "1.",
        ".1",
        "1.1.1",
        "123.1",
        "1.123",
        "a.1",
        "1.1\n",
        "1,1",
        "",
        "0.1",
        "1.0",
        "0.0",
        "01.1",
        "1.01",
        "02.10",
        "00.1",
    ],
)
def test_a_clause_number_that_is_not_n_dot_m_is_refused(
    chunks_database: DatabaseHandle, clause: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_chunk(chunks_database, clause=clause)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"product": ""}, id="empty-product"),
        pytest.param({"product": "x" * 33}, id="long-product"),
        pytest.param({"wording_version": ""}, id="empty-version"),
        pytest.param({"wording_version": "x" * 17}, id="long-version"),
        pytest.param({"section": ""}, id="empty-section"),
        pytest.param({"section": "x" * 201}, id="long-section"),
        pytest.param({"title": ""}, id="empty-title"),
        pytest.param({"title": "x" * 201}, id="long-title"),
        pytest.param({"body": ""}, id="empty-body"),
        pytest.param({"body": "x" * 4001}, id="long-body"),
        pytest.param({"source_sha256": "A" * 64}, id="upper-case-hash"),
        pytest.param({"source_sha256": "a" * 63}, id="short-hash"),
        pytest.param({"source_sha256": "a" * 63 + "\n"}, id="newline-hash"),
        pytest.param({"deployment": ""}, id="empty-deployment"),
        pytest.param({"deployment": "x" * 129}, id="long-deployment"),
        pytest.param({"model": ""}, id="empty-model"),
        pytest.param({"model": "x" * 129}, id="long-model"),
        pytest.param({"dimensions": 0, "embedding": "[]"}, id="zero-dimensions"),
    ],
)
def test_a_bad_chunk_row_is_refused(
    chunks_database: DatabaseHandle, values: dict[str, Any]
) -> None:
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.DataException)):
        insert_chunk(chunks_database, **values)


def test_a_chunk_at_every_cap_is_accepted(chunks_database: DatabaseHandle) -> None:
    insert_chunk(
        chunks_database,
        product="p" * 32,
        wording_version="v" * 16,
        clause="99.99",
        section="s" * 200,
        title="t" * 200,
        body="b" * 4000,
        deployment="d" * 128,
        model="m" * 128,
    )


def test_a_dimension_over_the_2000_cap_is_refused(
    chunks_database: DatabaseHandle,
) -> None:
    vector = "[" + ",".join(["0"] * 2001) + "]"

    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.DataException)):
        insert_chunk(chunks_database, dimensions=2001, embedding=vector)


def test_a_second_chunk_with_the_same_product_version_and_clause_is_refused(
    chunks_database: DatabaseHandle,
) -> None:
    insert_chunk(chunks_database)

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_chunk(chunks_database)


def test_the_same_clause_in_another_version_or_product_is_another_row(
    chunks_database: DatabaseHandle,
) -> None:
    insert_chunk(chunks_database)
    insert_chunk(chunks_database, wording_version="2026-02")
    insert_chunk(chunks_database, product="HOME-STD")

    assert owner_run(chunks_database, "SELECT count(*) FROM knowledge.chunks") == [(3,)]


@pytest.mark.parametrize("column", sorted(CHUNK))
def test_a_null_in_a_column_the_caller_writes_is_refused(
    chunks_database: DatabaseHandle, column: str
) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        insert_chunk(chunks_database, **{column: None})


def test_the_lexemes_cannot_be_written_by_hand(
    chunks_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.GeneratedAlways):
        owner_run(
            chunks_database,
            INSERT.replace(
                "dimensions, embedding)", "dimensions, embedding, lexemes)"
            ).replace(
                "%(embedding)s::vector)", "%(embedding)s::vector, 'x'::tsvector)"
            ),
            CHUNK,
        )


# ── who may touch it ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "role", [role for role in SERVICE_ROLES if role != "knowledge_mcp"]
)
def test_a_service_role_but_the_knowledge_server_can_neither_read_nor_write_the_chunks(
    migrated_database: DatabaseHandle, role: str
) -> None:
    with connect(migrated_database.dsn(role), "test") as conn:
        for statement in (
            "SELECT count(*) FROM knowledge.chunks",
            "DELETE FROM knowledge.chunks",
            "TRUNCATE knowledge.chunks",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(INSERT, CHUNK)
        conn.rollback()
        assert conn.execute(
            "SELECT has_schema_privilege('knowledge', 'USAGE')"
        ).fetchone() == (False,)


def test_public_has_no_privilege_on_the_knowledge_schema_or_its_table(
    migrated_database: DatabaseHandle,
) -> None:
    schemas = owner_run(
        migrated_database,
        "SELECT n.nspname FROM pg_namespace n, aclexplode(n.nspacl) a "
        "WHERE a.grantee = 0 AND n.nspname = 'knowledge'",
    )
    tables = owner_run(
        migrated_database,
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
        "WHERE a.grantee = 0 AND n.nspname = 'knowledge'",
    )

    assert schemas == []
    assert tables == []


def test_only_the_owner_and_the_knowledge_servers_column_grant_touch_the_chunks(
    migrated_database: DatabaseHandle,
) -> None:
    # Migration 0006 (S046) gave knowledge_mcp a SELECT on ten columns; the
    # table's own ACL still names the owner alone.
    table_grantees = owner_run(
        migrated_database,
        "SELECT DISTINCT a.grantee::regrole::text "
        "FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = 'knowledge.chunks'::regclass",
    )
    column_grants = owner_run(
        migrated_database,
        "SELECT DISTINCT a.grantee::regrole::text, a.privilege_type "
        "FROM pg_attribute t, aclexplode(t.attacl) a "
        "WHERE t.attrelid = 'knowledge.chunks'::regclass",
    )

    assert table_grantees in ([], [(OWNER,)])
    assert column_grants == [("knowledge_mcp", "SELECT")]
