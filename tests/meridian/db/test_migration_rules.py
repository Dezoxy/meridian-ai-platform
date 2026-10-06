"""The rule for adding a column to a table that is read and written (S065).

A migration file is one transaction, so a file that does ``ALTER TABLE ... ADD
COLUMN`` and then ``UPDATE`` on the same table holds the ``ALTER``'s ACCESS
EXCLUSIVE lock through the whole backfill. The rule is in
``src/meridian/platform/migrations/README.md``: the column goes in one file, the
backfill in the next. The first half of this file checks the packaged files
(text only, no database); the second half shows by construction, on a scratch
table, why the rule holds.
"""

import re

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

# Files 0001 to 0016 are history: 0009 and 0012 backfill claims.claims in the
# same file that adds the column ("fine at this size": at most 10,000 claims).
LAST_EXEMPT_NUMBER = 16
README = "src/meridian/platform/migrations/README.md"

# What is removed before the statements are read: a comment, a string literal
# and a dollar-quoted body (a function's text). One pass, left to right, so an
# apostrophe in a comment or a ``--`` in a string cannot confuse the next one.
# Not handled: a nested /* */ comment, an E'...' literal with a backslash
# escape.
NOT_STATEMENTS = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\$(\w*)\$.*?\$\1\$", re.DOTALL
)
TABLE = r'((?:"[^"]+"|\w+)(?:\s*\.\s*(?:"[^"]+"|\w+))?)'
ALTER_ADD_COLUMN = re.compile(
    rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{TABLE}\s.*?\bADD\s+COLUMN\b",
    re.IGNORECASE | re.DOTALL,
)
UPDATE_TABLE = re.compile(rf"UPDATE\s+(?:ONLY\s+)?{TABLE}", re.IGNORECASE)

PLANTED_BOTH = """
-- an UPDATE in a comment does not count
ALTER TABLE claims.claims ADD COLUMN flag integer NOT NULL DEFAULT 0;
UPDATE claims.claims SET flag = 1 WHERE claim_id = 'CLM-0001';
"""
PLANTED_ALTER_ONLY = """
ALTER TABLE claims.claims ADD COLUMN flag integer NOT NULL DEFAULT 0;
"""
PLANTED_UPDATE_ONLY = """
UPDATE claims.claims SET flag = 1 WHERE claim_id = 'CLM-0001';
"""


def tables(pattern: re.Pattern[str], text: str) -> set[str]:
    """The tables whose statements start with ``pattern`` (the first group)."""
    stripped = NOT_STATEMENTS.sub(" ", text)
    found = set()
    for statement in stripped.split(";"):
        match = pattern.match(statement.strip())
        if match:
            found.add(re.sub(r'\s|"', "", match.group(1)).lower())
    return found


def same_table(left: str, right: str) -> bool:
    """Equal names, or one name without a schema and the same table in the
    other (the search path may complete it)."""
    if left == right:
        return True
    if "." in left and "." in right:
        return False
    return left.rsplit(".", 1)[-1] == right.rsplit(".", 1)[-1]


def refusal(name: str, text: str) -> str | None:
    """The message for a file that adds a column to a table and updates it in
    the same file, or None when the file is clear or exempt by its number."""
    if int(name[:4]) <= LAST_EXEMPT_NUMBER:
        return None
    for added in sorted(tables(ALTER_ADD_COLUMN, text)):
        for updated in sorted(tables(UPDATE_TABLE, text)):
            if same_table(added, updated):
                return (
                    f"{name} adds a column to {added} and runs UPDATE on "
                    f"{updated} in one file: the ALTER's ACCESS EXCLUSIVE lock "
                    "is held through the backfill, because a file is one "
                    "transaction. Add the column in one file and backfill in "
                    f"the next; see {README}"
                )
    return None


def packaged(name: str) -> str:
    return dict(migration_files())[name]


NEW_FILES = [
    name for name, _ in migration_files() if int(name[:4]) > LAST_EXEMPT_NUMBER
]


@pytest.mark.parametrize("name", NEW_FILES)
def test_a_new_migration_does_not_add_a_column_and_update_the_table(
    name: str,
) -> None:
    message = refusal(name, packaged(name))

    assert message is None, message


def test_the_check_has_files_to_read() -> None:
    assert "0017_audit_order.sql" in NEW_FILES


def test_the_check_sees_the_add_column_of_0017_and_no_update() -> None:
    text = packaged("0017_audit_order.sql")

    assert tables(ALTER_ADD_COLUMN, text) == {"audit.events"}
    assert tables(UPDATE_TABLE, text) == set()


@pytest.mark.parametrize(
    "name",
    ["0009_claim_states.sql", "0012_claim_lifecycle.sql"],
)
def test_the_check_would_refuse_the_two_files_it_exempts(name: str) -> None:
    text = packaged(name)

    refused = refusal(f"0099_{name[5:]}", text)

    assert refused is not None
    assert "claims.claims" in refused


def test_a_file_that_adds_a_column_and_updates_the_table_is_refused() -> None:
    message = refusal("0099_x.sql", PLANTED_BOTH)

    assert message is not None
    assert "0099_x.sql" in message
    assert "claims.claims" in message
    assert "ACCESS EXCLUSIVE" in message
    assert README in message


@pytest.mark.parametrize(
    ("text"),
    [PLANTED_ALTER_ONLY, PLANTED_UPDATE_ONLY],
    ids=["alter-only", "update-only"],
)
def test_a_file_with_only_one_of_the_two_is_accepted(text: str) -> None:
    assert refusal("0099_x.sql", text) is None


def test_the_same_text_in_an_exempt_file_is_accepted() -> None:
    assert refusal("0016_x.sql", PLANTED_BOTH) is None
    assert refusal("0017_x.sql", PLANTED_BOTH) is not None


def test_two_different_tables_are_accepted() -> None:
    text = (
        "ALTER TABLE claims.claims ADD COLUMN flag integer;\n"
        "UPDATE claims.decisions SET reason = 'x';\n"
    )

    assert refusal("0099_x.sql", text) is None


def test_a_grant_or_a_trigger_clause_or_a_conflict_clause_is_not_an_update() -> None:
    text = (
        "ALTER TABLE claims.claims ADD COLUMN flag integer;\n"
        "GRANT UPDATE (flag) ON claims.claims TO claims_api;\n"
        "CREATE TRIGGER t BEFORE UPDATE OR DELETE ON claims.claims\n"
        "    FOR EACH ROW EXECUTE FUNCTION f();\n"
        "INSERT INTO claims.claims (claim_id) VALUES ('x')\n"
        "    ON CONFLICT (claim_id) DO UPDATE SET flag = 1;\n"
    )

    assert refusal("0099_x.sql", text) is None


def test_a_trigger_only_file_is_accepted() -> None:
    text = (
        "DROP TRIGGER events_no_update ON audit.events;\n"
        "CREATE TRIGGER events_no_update BEFORE UPDATE ON audit.events\n"
        "    FOR EACH ROW WHEN (OLD.event IS NOT NULL)\n"
        "    EXECUTE FUNCTION audit.refuse_change();\n"
    )

    assert refusal("0099_x.sql", text) is None


def test_quoted_and_unqualified_names_are_compared_as_the_same_table() -> None:
    text = (
        'ALTER TABLE ONLY "Claims"."Claims" ADD COLUMN flag integer;\n'
        "UPDATE claims SET flag = 1;\n"
    )

    assert refusal("0099_x.sql", text) is not None


# What the check does not see: an UPDATE inside a function body (a DO block, a
# CREATE FUNCTION) or in dynamic SQL (EXECUTE), a data-modifying CTE (WITH ...
# UPDATE), an ALTER TABLE ... ADD without the word COLUMN, a column added by
# CREATE TABLE ... LIKE or by a view, and the same table reached through a
# different search_path or a rename earlier in the file.
def test_an_update_inside_a_function_body_is_not_seen() -> None:
    text = (
        "ALTER TABLE claims.claims ADD COLUMN flag integer;\n"
        "DO $body$ BEGIN UPDATE claims.claims SET flag = 1; END $body$;\n"
    )

    assert tables(UPDATE_TABLE, text) == set()
    assert refusal("0099_x.sql", text) is None


# ── why the rule holds, on a scratch table ──────────────────────────────────
# Every wait below is the server's: a second connection that asks for a lock
# the first holds is refused after 200 ms. No test sleeps.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '200ms'"
CREATE_SCRATCH = "CREATE TABLE public.rule_scratch (id integer PRIMARY KEY, label text)"
FILL_SCRATCH = (
    "INSERT INTO public.rule_scratch (id, label) "
    "SELECT i, 'row' FROM generate_series(1, 4) AS i"
)
ADD_FLAG = "ALTER TABLE public.rule_scratch ADD COLUMN flag integer NOT NULL DEFAULT 0"
BACKFILL_HALF = "UPDATE public.rule_scratch SET flag = 1 WHERE id <= 2"
ADD_CHECK_NOT_VALID = (
    "ALTER TABLE public.rule_scratch "
    "ADD CONSTRAINT rule_scratch_flag_check CHECK (flag >= 0) NOT VALID"
)
VALIDATE_CHECK = (
    "ALTER TABLE public.rule_scratch VALIDATE CONSTRAINT rule_scratch_flag_check"
)
READ_ALL = "SELECT id, flag FROM public.rule_scratch ORDER BY id"


def scratch_table(db: DatabaseHandle, *, with_flag: bool) -> None:
    """A table of four rows, with the column already committed (the first file
    of the rule) or without it."""
    with connect(db.dsn(OWNER), "test-set-up") as conn:
        conn.execute(CREATE_SCRATCH)
        conn.execute(FILL_SCRATCH)
        if with_flag:
            conn.execute(ADD_FLAG)
        conn.commit()


def test_a_column_added_and_backfilled_in_one_transaction_blocks_readers(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=False)

    with (
        connect(empty_database.dsn(OWNER), "test-migration") as migration,
        connect(empty_database.dsn(OWNER), "test-reader") as reader,
    ):
        migration.execute(ADD_FLAG)
        migration.execute(BACKFILL_HALF)
        reader.execute(LOCK_TIMEOUT)

        with pytest.raises(psycopg.errors.LockNotAvailable):
            reader.execute(READ_ALL)

        migration.rollback()


def test_a_backfill_after_the_column_is_committed_blocks_no_reader_and_no_other_row(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with (
        connect(empty_database.dsn(OWNER), "test-backfill") as backfill,
        connect(empty_database.dsn(OWNER), "test-other") as other,
    ):
        backfill.execute(BACKFILL_HALF)
        other.execute(LOCK_TIMEOUT)

        seen = other.execute(READ_ALL).fetchall()
        written = other.execute(
            "UPDATE public.rule_scratch SET label = 'new' WHERE id = 3"
        )
        backfill.rollback()
        other.rollback()

    assert seen == [(1, 0), (2, 0), (3, 0), (4, 0)]
    assert written.rowcount == 1


def test_a_writer_of_a_row_the_backfill_has_changed_waits_for_that_row_only(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with (
        connect(empty_database.dsn(OWNER), "test-backfill") as backfill,
        connect(empty_database.dsn(OWNER), "test-other") as other,
    ):
        backfill.execute(BACKFILL_HALF)
        other.execute(LOCK_TIMEOUT)

        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.execute("UPDATE public.rule_scratch SET label = 'new' WHERE id = 1")

        backfill.rollback()


def test_a_constraint_added_and_validated_in_one_transaction_blocks_readers(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)

    with (
        connect(empty_database.dsn(OWNER), "test-migration") as migration,
        connect(empty_database.dsn(OWNER), "test-reader") as reader,
    ):
        migration.execute(ADD_CHECK_NOT_VALID)
        migration.execute(VALIDATE_CHECK)
        reader.execute(LOCK_TIMEOUT)

        with pytest.raises(psycopg.errors.LockNotAvailable):
            reader.execute(READ_ALL)

        migration.rollback()


def test_validating_a_constraint_added_not_valid_blocks_no_reader_and_no_writer(
    empty_database: DatabaseHandle,
) -> None:
    scratch_table(empty_database, with_flag=True)
    with connect(empty_database.dsn(OWNER), "test-not-valid-file") as not_valid_file:
        not_valid_file.execute(ADD_CHECK_NOT_VALID)
        not_valid_file.commit()

    with (
        connect(empty_database.dsn(OWNER), "test-validate") as validate,
        connect(empty_database.dsn(OWNER), "test-other") as other,
    ):
        validate.execute(VALIDATE_CHECK)
        other.execute(LOCK_TIMEOUT)

        seen = other.execute(READ_ALL).fetchall()
        written = other.execute(
            "UPDATE public.rule_scratch SET label = 'new' WHERE id = 3"
        )
        validate.rollback()
        other.rollback()

    assert seen == [(1, 0), (2, 0), (3, 0), (4, 0)]
    assert written.rowcount == 1
