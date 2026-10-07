"""The table for a claim's uploaded files, claims.claim_files (S070, F1).

The migration is found by the end of its name, never by its number or its place
in the list: the number is provisional until the pull request merges, and the
file is the last only until another one lands after it.
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import get_args

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
    TENANT,
    privileges,
    refused,
    run,
)

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files
from meridian.workloads.claims_triage.rules import Document

SUFFIX = "_claim_files.sql"
TABLE = "claims.claim_files"
API = "claims_api"
OTHER_ROLES = tuple(
    role for role in (*SERVICE_ROLES, UPKEEP_ROLE, *JOB_ROLES) if role != API
)
COLUMNS = (
    "file_id",
    "claim_id",
    "kind",
    "media_type",
    "size_bytes",
    "sha256",
    "content",
    "received_at",
)
KINDS = (*get_args(Document), "other")
MEDIA_TYPES = ("application/pdf", "image/jpeg", "image/png")
MAX_BYTES = 1024 * 1024
CHECK_VIOLATION = "23514"
FOREIGN_KEY_VIOLATION = "23503"
NOT_NULL_VIOLATION = "23502"
UNIQUE_VIOLATION = "23505"
INSERT_FILE = (
    "INSERT INTO claims.claim_files "
    "(file_id, claim_id, kind, media_type, size_bytes, sha256, content) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
# What the one role that writes the table holds on it: the privilege snapshot's
# rows. Nothing is held on a column beyond the read: no UPDATE of any.
API_HOLDS = frozenset(
    {
        ("table", TABLE, "SELECT", ""),
        ("table", TABLE, "INSERT", ""),
        *(("column", TABLE, "SELECT", column) for column in COLUMNS),
    }
)
# The relations a transaction of this file holds a lock on, other than the new
# table and its own indexes: the existing relations the file touches.
LOCKED_EXISTING_RELATIONS = (
    "SELECT c.oid::regclass::text, l.mode FROM pg_locks AS l "
    "JOIN pg_class AS c ON c.oid = l.relation "
    "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
    "AND c.relkind IN ('r', 'p', 'v', 'm') "
    "AND c.relnamespace NOT IN ('pg_catalog'::regnamespace, "
    "'information_schema'::regnamespace) "
    "AND c.relname <> 'claim_files' ORDER BY 1, 2"
)


def migration_index() -> int:
    (index,) = [
        position
        for position, (name, _) in enumerate(migration_files())
        if name.endswith(SUFFIX)
    ]
    return index


def migration_name() -> str:
    return migration_files()[migration_index()][0]


def migration_text() -> str:
    return migration_files()[migration_index()][1]


def on_files(db: DatabaseHandle, role: str) -> frozenset[tuple]:
    return frozenset(row for row in privileges(db, role) if row[1] == TABLE)


def file_row(
    claim_id: str = CLAIM_ID,
    kind: str = "photos",
    media_type: str = "application/pdf",
    content: bytes = b"%PDF-synthetic",
    size_bytes: int | None = None,
    sha256: bytes | None = None,
    file_id: uuid.UUID | None = None,
) -> tuple:
    """The parameters of ``INSERT_FILE``: a valid file unless an argument says."""
    return (
        file_id or uuid.uuid4(),
        claim_id,
        kind,
        media_type,
        len(content) if size_bytes is None else size_bytes,
        hashlib.sha256(content).digest() if sha256 is None else sha256,
        content,
    )


def add_file(db: DatabaseHandle, role: str = API, **overrides: object) -> uuid.UUID:
    row = file_row(**overrides)  # type: ignore[arg-type]
    run(db, role, INSERT_FILE, row)
    return row[0]


def constraint_of(db: DatabaseHandle, row: tuple) -> str | None:
    """The name of the constraint that refuses ``row``, as ``claims_api``.

    PostgreSQL checks a table's CHECKs in the order of their names and names the
    first that fails, so a row must break exactly one for the name to mean it.
    """
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        run(db, API, INSERT_FILE, row)
    return caught.value.diag.constraint_name


@pytest.fixture
def claim(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds one claim."""
    run(fresh_database, API, INSERT_CLAIM, (CLAIM_ID, TENANT))
    return fresh_database


# ── the migration ───────────────────────────────────────────────────────────
def test_the_migration_is_recorded(migrated_database: DatabaseHandle) -> None:
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert (migration_name(),) in recorded


def test_the_table_has_exactly_the_columns_of_the_contract(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'claim_files' "
        "ORDER BY ordinal_position",
    )

    assert rows == [
        ("file_id", "uuid", "NO", None),
        ("claim_id", "text", "NO", None),
        ("kind", "text", "NO", None),
        ("media_type", "text", "NO", None),
        ("size_bytes", "integer", "NO", None),
        ("sha256", "bytea", "NO", None),
        ("content", "bytea", "NO", None),
        ("received_at", "timestamp with time zone", "NO", "now()"),
    ]


def test_a_file_has_no_name_column(migrated_database: DatabaseHandle) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'claims' AND table_name = 'claim_files' "
        "AND column_name ~* '(name|filename|path)'",
    )

    assert rows == []


# ── what a stored file is ───────────────────────────────────────────────────
def test_claims_api_stores_and_reads_back_a_file_byte_for_byte(
    claim: DatabaseHandle,
) -> None:
    content = bytes(range(256)) * 4  # every byte value, NUL included
    file_id = add_file(claim, content=content, media_type="image/png", kind="other")

    rows = run(
        claim,
        API,
        "SELECT claim_id, kind, media_type, size_bytes, sha256, content, "
        "received_at IS NOT NULL FROM claims.claim_files WHERE file_id = %s",
        (file_id,),
    )

    assert rows == [
        (
            CLAIM_ID,
            "other",
            "image/png",
            len(content),
            hashlib.sha256(content).digest(),
            content,
            True,
        )
    ]


def test_the_time_a_file_was_received_is_the_databases(claim: DatabaseHandle) -> None:
    file_id = add_file(claim)

    ((same_transaction_clock,),) = run(
        claim,
        OWNER,
        "SELECT received_at BETWEEN now() - interval '1 minute' AND now() "
        "FROM claims.claim_files WHERE file_id = %s",
        (file_id,),
    )

    assert same_transaction_clock is True


def test_a_claim_holds_several_files_and_one_identifier_names_one(
    claim: DatabaseHandle,
) -> None:
    first = add_file(claim)
    add_file(claim)
    add_file(claim, content=b"the same bytes twice", kind="other")
    add_file(claim, content=b"the same bytes twice", kind="other")

    assert run(claim, OWNER, "SELECT count(*) FROM claims.claim_files") == [(4,)]
    assert refused(claim, API, INSERT_FILE, file_row(file_id=first)) == (
        UNIQUE_VIOLATION
    )


def test_the_app_supplies_the_identifier(claim: DatabaseHandle) -> None:
    row = file_row()

    sqlstate = refused(
        claim,
        API,
        "INSERT INTO claims.claim_files "
        "(claim_id, kind, media_type, size_bytes, sha256, content) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        row[1:],
    )

    assert sqlstate == NOT_NULL_VIOLATION


# ── what the table refuses ──────────────────────────────────────────────────
@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_the_rules_know_and_other_is_accepted(
    claim: DatabaseHandle, kind: str
) -> None:
    add_file(claim, kind=kind)

    assert run(claim, OWNER, "SELECT kind FROM claims.claim_files") == [(kind,)]


def test_the_kinds_are_the_four_document_codes_of_the_rules_and_other() -> None:
    # The CHECK lists the codes as text; the rules' type is the source. This
    # fails the day one gains a code the other has not.
    listed = re.search(r"kind IN \((.*?)\)", migration_text(), re.DOTALL)
    assert listed is not None
    check = re.findall(r"'([a-z_]+)'", listed.group(1))

    assert len(get_args(Document)) == 4
    assert sorted(check) == sorted(KINDS)


@pytest.mark.parametrize(
    "kind", ["invoice", "Photos", "police-report", "", "photos ", "OTHER"]
)
def test_a_sixth_kind_is_refused(claim: DatabaseHandle, kind: str) -> None:
    constraint = constraint_of(claim, file_row(kind=kind))

    assert constraint == "claim_files_kind_is_known"


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_each_of_the_three_media_types_is_accepted(
    claim: DatabaseHandle, media_type: str
) -> None:
    add_file(claim, media_type=media_type)

    assert run(claim, OWNER, "SELECT media_type FROM claims.claim_files") == [
        (media_type,)
    ]


@pytest.mark.parametrize(
    "media_type",
    [
        "image/gif",
        "image/jpg",
        "application/zip",
        "text/html",
        "application/pdf; charset=utf-8",
        "Application/PDF",
        "",
    ],
)
def test_a_fourth_media_type_is_refused(claim: DatabaseHandle, media_type: str) -> None:
    constraint = constraint_of(claim, file_row(media_type=media_type))

    assert constraint == "claim_files_media_type_is_known"


@pytest.mark.parametrize("size", [1, MAX_BYTES])
def test_a_file_of_one_byte_and_one_of_one_mebibyte_are_accepted(
    claim: DatabaseHandle, size: int
) -> None:
    file_id = add_file(claim, content=b"\x01" * size)

    assert run(
        claim,
        OWNER,
        "SELECT size_bytes, octet_length(content) FROM claims.claim_files "
        "WHERE file_id = %s",
        (file_id,),
    ) == [(size, size)]


def test_zero_bytes_are_refused(claim: DatabaseHandle) -> None:
    constraint = constraint_of(claim, file_row(content=b""))

    assert constraint == "claim_files_size_is_bounded"


def test_one_mebibyte_and_one_byte_are_refused(claim: DatabaseHandle) -> None:
    constraint = constraint_of(claim, file_row(content=b"\x01" * (MAX_BYTES + 1)))

    assert constraint == "claim_files_size_is_bounded"


@pytest.mark.parametrize("size", [-1, 0, MAX_BYTES + 1, 2**31 - 1])
def test_a_size_outside_the_bounds_is_refused_whatever_the_content_says(
    claim: DatabaseHandle, size: int
) -> None:
    # The content is 14 bytes, so the size also disagrees with it: which of the
    # two CHECKs is named is the order of their names, so only the kind of
    # refusal is asserted. Each CHECK alone is shown by the tests around this.
    assert refused(claim, API, INSERT_FILE, file_row(size_bytes=size)) == (
        CHECK_VIOLATION
    )


@pytest.mark.parametrize("length", [0, 1, 31, 33, 64])
def test_a_hash_that_is_not_32_bytes_is_refused(
    claim: DatabaseHandle, length: int
) -> None:
    constraint = constraint_of(claim, file_row(sha256=b"\xab" * length))

    assert constraint == "claim_files_sha256_is_32_bytes"


def test_a_hash_of_32_bytes_is_accepted_even_if_it_is_not_the_content_s(
    claim: DatabaseHandle,
) -> None:
    # The table holds the length of a hash, not the hash of the content: the
    # app computes it, and the database cannot (no extension is installed to do
    # it here). The test says so, so nobody takes the column for a guarantee.
    add_file(claim, sha256=b"\x00" * 32)

    assert run(claim, OWNER, "SELECT octet_length(sha256) FROM claims.claim_files") == [
        (32,)
    ]


@pytest.mark.parametrize(
    ("content", "size"),
    [
        (b"12345", 6),  # one byte shorter than it says
        (b"12345", 100),  # far shorter
        (b"12345", 4),  # one byte longer than it says
        (b"12345", 1),
    ],
)
def test_a_content_whose_length_is_not_its_size_is_refused(
    claim: DatabaseHandle, content: bytes, size: int
) -> None:
    constraint = constraint_of(claim, file_row(content=content, size_bytes=size))

    assert constraint == "claim_files_content_is_its_size"


@pytest.mark.parametrize("column", COLUMNS)
def test_no_column_may_be_null(claim: DatabaseHandle, column: str) -> None:
    values = dict(zip(COLUMNS, (*file_row(), datetime.now(UTC)), strict=True))
    values[column] = None
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))

    sqlstate = refused(
        claim,
        API,
        f"INSERT INTO claims.claim_files ({names}) VALUES ({marks})",  # noqa: S608
        tuple(values.values()),
    )

    assert sqlstate == NOT_NULL_VIOLATION


def test_a_file_for_a_claim_that_does_not_exist_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    sqlstate = refused(fresh_database, API, INSERT_FILE, file_row(claim_id="CLM-9999"))

    assert sqlstate == FOREIGN_KEY_VIOLATION
    assert run(fresh_database, OWNER, "SELECT count(*) FROM claims.claim_files") == [
        (0,)
    ]


def test_a_refused_file_leaves_nothing_behind(claim: DatabaseHandle) -> None:
    add_file(claim)
    refused(claim, API, INSERT_FILE, file_row(content=b"x", size_bytes=2))

    assert run(claim, OWNER, "SELECT count(*) FROM claims.claim_files") == [(1,)]


# ── the index ───────────────────────────────────────────────────────────────
def test_the_files_of_one_claim_are_read_through_an_index_on_the_claim(
    claim: DatabaseHandle,
) -> None:
    indexes = run(
        claim,
        OWNER,
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'claims' AND tablename = 'claim_files' "
        "AND indexname <> 'claim_files_pkey'",
    )
    with connect(claim.dsn(OWNER), "test") as conn:
        # An empty table is read in full whatever indexes it has, so the
        # planner is told a scan of the table is not an option, and a bitmap
        # scan (which would sort afterwards) is not either.
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET enable_bitmapscan = off")
        plan = "\n".join(
            row[0]
            for row in conn.execute(
                "EXPLAIN SELECT file_id, kind, size_bytes FROM claims.claim_files "
                "WHERE claim_id = 'CLM-0013' ORDER BY received_at"
            )
        )

    assert len(indexes) == 1
    assert indexes[0][0].endswith("(claim_id, received_at)")
    assert "claim_files_claim_idx" in plan
    assert "Sort" not in plan


# ── what each role holds ────────────────────────────────────────────────────
def test_claims_api_holds_select_and_insert_and_nothing_else(
    migrated_database: DatabaseHandle,
) -> None:
    held = on_files(migrated_database, API)

    assert held == API_HOLDS, (held - API_HOLDS, API_HOLDS - held)


@pytest.mark.parametrize("column", COLUMNS)
def test_claims_api_cannot_change_any_column_of_a_file(
    claim: DatabaseHandle, column: str
) -> None:
    add_file(claim)

    sqlstate = refused(claim, API, f"UPDATE claims.claim_files SET {column} = {column}")  # noqa: S608

    assert sqlstate == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE claims.claim_files SET kind = 'other'",
        "UPDATE claims.claim_files SET content = '\\x00', size_bytes = 1",
        "DELETE FROM claims.claim_files",
        "TRUNCATE claims.claim_files",
    ],
)
def test_claims_api_may_neither_change_nor_delete_a_file(
    claim: DatabaseHandle, statement: str
) -> None:
    file_id = add_file(claim)

    assert refused(claim, API, statement) == INSUFFICIENT_PRIVILEGE
    assert run(claim, OWNER, "SELECT file_id FROM claims.claim_files") == [(file_id,)]


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_no_other_role_holds_anything_on_the_table(
    migrated_database: DatabaseHandle, role: str
) -> None:
    held = on_files(migrated_database, role)
    rows = run(
        migrated_database,
        OWNER,
        "SELECT has_table_privilege(%s, 'claims.claim_files', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'), "
        "has_any_column_privilege(%s, 'claims.claim_files', "
        "'SELECT, INSERT, UPDATE, REFERENCES')",
        (role, role),
    )

    assert held == frozenset()
    assert rows == [(False, False)]
    # A role with no right on the schema is refused by the schema's USAGE first;
    # either way it cannot read a byte.
    sqlstate = refused(
        migrated_database, role, "SELECT content FROM claims.claim_files"
    )
    assert sqlstate == INSUFFICIENT_PRIVILEGE


def test_nothing_on_the_table_is_granted_to_public(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM information_schema.role_table_grants "
        "WHERE table_schema = 'claims' AND table_name = 'claim_files' "
        "AND grantee = 'PUBLIC'",
    )
    columns = run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM information_schema.column_privileges "
        "WHERE table_schema = 'claims' AND table_name = 'claim_files' "
        "AND grantee = 'PUBLIC'",
    )

    assert rows == [(0,)]
    assert columns == [(0,)]


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


def test_the_migration_applies_on_the_database_before_it_and_gives_one_role_one_right(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    roles = (API, *OTHER_ROLES)
    before = {role: privileges(empty_database, role) for role in roles}
    monkeypatch.setattr(
        runner, "migration_files", lambda: files[: migration_index() + 1]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        applied = runner.apply_migrations(conn)

    assert applied == [migration_name()]
    assert privileges(empty_database, API) - before[API] == API_HOLDS
    assert before[API] - privileges(empty_database, API) == frozenset()
    for role in OTHER_ROLES:
        assert privileges(empty_database, role) == before[role], role


def test_no_other_file_makes_the_table() -> None:
    # The suffix finds the file whatever its number; a second file that made the
    # table (a renumbered copy left beside it) would be seen here.
    creating = [
        name
        for name, text in migration_files()
        if "CREATE TABLE claims.claim_files" in text
    ]

    assert creating == [migration_name()]


def test_a_migration_run_by_a_role_that_does_not_own_the_schema_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema claims"
        ):
            conn.execute(migration_text())
        conn.rollback()

    ((exists,),) = run(
        empty_database, OWNER, "SELECT to_regclass('claims.claim_files') IS NOT NULL"
    )
    assert exists is False


def test_a_missing_role_fails_clearly_and_creates_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = apply_everything_before(empty_database, monkeypatch)
    index = migration_index()
    name, text = files[index]
    swapped = text.replace("'claims_api'", "'role_that_does_not_exist'")
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
        empty_database, OWNER, "SELECT to_regclass('claims.claim_files') IS NOT NULL"
    )
    assert exists is False


def test_the_file_starts_with_a_lock_timeout_and_says_which_lock_it_takes() -> None:
    text = migration_text()

    body = "\n".join(
        line for line in text.splitlines() if not line.startswith("--") and line.strip()
    )
    assert body.startswith("SET LOCAL lock_timeout = '3s';")
    assert "SHARE ROW EXCLUSIVE" in text
    assert "retention" in text


def test_the_file_takes_share_row_exclusive_on_the_claims_and_no_other_lock(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn(OWNER), "test") as conn:
        conn.execute(migration_text())
        held = conn.execute(LOCKED_EXISTING_RELATIONS).fetchall()
        conn.rollback()

    # One existing relation, claims.claims (the foreign key's). The strongest lock
    # on it is SHARE ROW EXCLUSIVE, which blocks its writers and DDL but not its
    # readers; the ACCESS SHARE beside it conflicts only with ACCESS EXCLUSIVE.
    assert held == [
        ("claims.claims", "AccessShareLock"),
        ("claims.claims", "ShareRowExclusiveLock"),
    ]


def test_a_writer_of_the_claims_holds_the_file_up_for_three_seconds_and_no_longer(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)
    text = migration_text()

    with connect(empty_database.dsn(OWNER), "test") as writer:
        # An open write on claims.claims: ROW EXCLUSIVE, which the file's lock
        # conflicts with.
        writer.execute(INSERT_CLAIM, (CLAIM_ID, TENANT))
        with connect(empty_database.dsn(OWNER), "test") as applier:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                applier.execute(text)
            applier.rollback()
        writer.rollback()

    ((exists,),) = run(
        empty_database, OWNER, "SELECT to_regclass('claims.claim_files') IS NOT NULL"
    )
    assert exists is False
