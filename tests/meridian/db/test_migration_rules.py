"""The rules for a migration that locks a table that is read and written (S065).

A migration file is one transaction, so a file that does ``ALTER TABLE ... ADD
COLUMN`` and then ``UPDATE`` on the same table holds the ``ALTER``'s ACCESS
EXCLUSIVE lock through the whole backfill, and a statement that takes ACCESS
EXCLUSIVE queues every new reader and writer behind it until it gets the lock.
The rules are in ``src/meridian/platform/migrations/README.md``: the column goes
in one file and the backfill in the next, and a file that takes ACCESS EXCLUSIVE
starts with ``SET LOCAL lock_timeout``. This file checks the packaged files, on
text only (no database); ``test_migration_rules_on_a_scratch_table.py`` shows
against PostgreSQL why the rules hold and what each kind of change locks.
"""

import re

import pytest

from meridian.platform.migrations.runner import migration_files

# Files 0001 to 0016 are history: 0009 and 0012 backfill claims.claims in the
# same file that adds the column ("fine at this size": at most 10,000 claims).
LAST_EXEMPT_NUMBER = 16
README = "src/meridian/platform/migrations/README.md"
# \d and int() accept the digits of other scripts: a file name is read with
# these ASCII digits only.
NUMBER = re.compile(r"[0-9]{4}")

# What is removed before the statements are read: a comment, a string literal
# and a dollar-quoted body (a function's text). One pass, left to right, so an
# apostrophe in a comment or a ``--`` in a string cannot confuse the next one.
# Not handled: a nested /* */ comment, an E'...' literal with a backslash
# escape.
NOT_STATEMENTS = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\$(\w*)\$.*?\$\1\$", re.DOTALL
)
TABLE = r'((?:"[^"]+"|\w+)(?:\s*\.\s*(?:"[^"]+"|\w+))?)(?![\w."])'
ALTER_ADD_COLUMN = re.compile(
    rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{TABLE}\s.*?\bADD\s+COLUMN\b",
    re.IGNORECASE | re.DOTALL,
)
UPDATE_TABLE = re.compile(rf"UPDATE\s+(?:ONLY\s+)?{TABLE}", re.IGNORECASE)
# The other ways a file can change the rows of a table it has just altered. The
# first two start a statement; the third is searched in a WITH or a DO.
MERGE_UPDATE = re.compile(
    rf"MERGE\s+INTO\s+(?:ONLY\s+)?{TABLE}\s.*?\bTHEN\s+UPDATE\b",
    re.IGNORECASE | re.DOTALL,
)
UPSERT_UPDATE = re.compile(
    rf"INSERT\s+INTO\s+{TABLE}\s.*?\bON\s+CONFLICT\b.*?\bDO\s+UPDATE\b",
    re.IGNORECASE | re.DOTALL,
)
UPDATE_ANYWHERE = re.compile(rf"\bUPDATE\s+(?:ONLY\s+)?(?!SET\b){TABLE}", re.IGNORECASE)
WITH_OR_DO = re.compile(r"(WITH|DO)\b", re.IGNORECASE)
STARTING_FORMS = {
    "UPDATE": UPDATE_TABLE,
    "MERGE ... THEN UPDATE": MERGE_UPDATE,
    "INSERT ... ON CONFLICT ... DO UPDATE": UPSERT_UPDATE,
}

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


def number_of(name: str) -> int:
    """The four ASCII digits a migration's name starts with."""
    head = name[:4]
    if not NUMBER.fullmatch(head):
        raise ValueError(f"{name} does not start with four ASCII digits")
    return int(head)


def strip(text: str, *, bodies: bool = False, literals: bool = False) -> str:
    """``text`` without comments, string literals and dollar-quoted bodies.

    ``bodies`` keeps the body of a dollar-quoted string (a DO block runs in the
    file; a function's text does not, but is kept with it) as text of the
    statement that holds it, with its own semicolons turned into spaces so that
    the statement stays one. ``literals`` keeps the string literals, for the
    one caller that reads a value out of one.
    """

    def blank(match: re.Match[str]) -> str:
        tag = match.group(1)
        if tag is None:
            return match.group(0) if literals and match.group(0)[0] == "'" else " "
        if not bodies:
            return " "
        body = match.group(0)[len(tag) + 2 : -(len(tag) + 2)]
        return " " + strip(body, bodies=True).replace(";", " ") + " "

    return NOT_STATEMENTS.sub(blank, text)


def tables(pattern: re.Pattern[str], text: str) -> set[str]:
    """The tables whose statements start with ``pattern`` (the first group)."""
    found = set()
    for statement in strip(text).split(";"):
        match = pattern.match(statement.strip())
        if match:
            found.add(clean(match.group(1)))
    return found


def clean(name: str) -> str:
    return re.sub(r'\s|"', "", name).lower()


def backfills(text: str) -> list[tuple[str, str]]:
    """``(form, table)`` for every statement that can change the rows of a table:
    an UPDATE, a MERGE that updates, an upsert that updates, and an UPDATE
    inside a WITH or a DO block (a function's text only defines, so it does not
    count)."""
    found = []
    for statement in strip(text, bodies=True).split(";"):
        statement = statement.strip()
        for form, pattern in STARTING_FORMS.items():
            match = pattern.match(statement)
            if match:
                found.append((form, clean(match.group(1))))
        nesting = WITH_OR_DO.match(statement)
        if nesting:
            where = "a WITH" if nesting.group(1).upper() == "WITH" else "a DO block"
            for match in UPDATE_ANYWHERE.finditer(statement):
                found.append((f"UPDATE inside {where}", clean(match.group(1))))
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
    if number_of(name) <= LAST_EXEMPT_NUMBER:
        return None
    changed = backfills(text)
    for added in sorted(tables(ALTER_ADD_COLUMN, text)):
        for form, updated in changed:
            if same_table(added, updated):
                return (
                    f"{name} adds a column to {added} and runs {form} on "
                    f"{updated} in one file: the ALTER's ACCESS EXCLUSIVE lock "
                    "is held through the backfill, because a file is one "
                    "transaction. Add the column in one file and backfill in "
                    f"the next; see {README}"
                )
    return None


# A statement that takes ACCESS EXCLUSIVE on a table or a view that is read
# and written. A statement of another mode (a CREATE INDEX, a CREATE TRIGGER, a
# LOCK in SHARE mode) queues too, but the rule is the one the README states.
# CREATE OR REPLACE VIEW is the view's own ACCESS EXCLUSIVE lock (measured in
# pg_locks below): readers of the view queue behind it as readers of a table
# queue behind an ALTER TABLE, so the rule includes it. Not handled: the same
# statements inside a DO block or in dynamic SQL.
OTHER_LOCK_MODE = (
    r"\bIN\s+(?:ACCESS\s+SHARE|ROW\s+SHARE|ROW\s+EXCLUSIVE|SHARE\s+UPDATE\s+EXCLUSIVE"
    r"|SHARE\s+ROW\s+EXCLUSIVE|SHARE|EXCLUSIVE)\s+MODE\b"
)
ACCESS_EXCLUSIVE_STATEMENT = re.compile(
    r"ALTER\s+TABLE\b|DROP\s+TRIGGER\b|CLUSTER\b|CREATE\s+OR\s+REPLACE\s+VIEW\b"
    rf"|LOCK\s+(?:TABLE\s+)?(?!.*{OTHER_LOCK_MODE})",
    re.IGNORECASE | re.DOTALL,
)
# A positive timeout: SET LOCAL ends with the transaction, 0 turns it off.
SET_LOCK_TIMEOUT = re.compile(
    r"SET\s+LOCAL\s+lock_timeout\s*(?:=|TO)\s*'?(?P<value>[0-9]+(?:\.[0-9]+)?)"
    r"\s*(?:ms|s|min|h|d)?'?",
    re.IGNORECASE,
)


def describe(statement: str, match: re.Match[str]) -> str:
    """The statement's keywords and the name after them: ``ALTER TABLE x``."""
    after = statement[match.end() :].split()
    return " ".join([*match.group(0).split(), *after[:1]])


def access_exclusive_statements(text: str) -> list[str]:
    """Each statement that takes ACCESS EXCLUSIVE, as ``ALTER TABLE x``."""
    found = []
    for statement in strip(text).split(";"):
        statement = statement.strip()
        match = ACCESS_EXCLUSIVE_STATEMENT.match(statement)
        if match:
            found.append(describe(statement, match))
    return found


def lock_timeout_refusal(name: str, text: str) -> str | None:
    """The message for a file that takes ACCESS EXCLUSIVE before it has set a
    positive ``lock_timeout`` for its transaction, or None."""
    if number_of(name) <= LAST_EXEMPT_NUMBER:
        return None
    timeout_set = False
    for statement in strip(text, literals=True).split(";"):
        statement = statement.strip()
        match = SET_LOCK_TIMEOUT.match(statement)
        if match and float(match.group("value")) > 0:
            timeout_set = True
            continue
        locking = ACCESS_EXCLUSIVE_STATEMENT.match(statement)
        if locking and not timeout_set:
            return (
                f"{name} runs {describe(statement, locking)!r}, which takes "
                "an ACCESS EXCLUSIVE lock, before a SET LOCAL lock_timeout: "
                "while the statement waits for the lock, every new reader and "
                "writer of the table queues behind it. Start the file with "
                f"SET LOCAL lock_timeout = '3s'; see {README}"
            )
    return None


def packaged(name: str) -> str:
    return dict(migration_files())[name]


NEW_FILES = [
    name for name, _ in migration_files() if number_of(name) > LAST_EXEMPT_NUMBER
]


@pytest.mark.parametrize("name", NEW_FILES)
def test_a_new_migration_does_not_add_a_column_and_update_the_table(
    name: str,
) -> None:
    message = refusal(name, packaged(name))

    assert message is None, message


@pytest.mark.parametrize("name", NEW_FILES)
def test_a_new_migration_that_takes_access_exclusive_sets_lock_timeout_first(
    name: str,
) -> None:
    message = lock_timeout_refusal(name, packaged(name))

    assert message is None, message


def test_the_check_has_files_to_read() -> None:
    assert "0017_audit_order.sql" in NEW_FILES
    assert "0018_sweep_trigger_when.sql" in NEW_FILES


@pytest.mark.parametrize(
    ("name", "statements"),
    [
        (
            "0017_audit_order.sql",
            ["LOCK TABLE audit.events", "ALTER TABLE audit.events"],
        ),
        ("0018_sweep_trigger_when.sql", ["DROP TRIGGER claims_confine_sweep"]),
    ],
)
def test_the_check_sees_the_access_exclusive_statements_of_the_packaged_files(
    name: str, statements: list[str]
) -> None:
    found = access_exclusive_statements(packaged(name))

    for statement in statements:
        assert statement in found


def test_the_check_sees_the_add_column_of_0017_and_no_update() -> None:
    text = packaged("0017_audit_order.sql")

    assert tables(ALTER_ADD_COLUMN, text) == {"audit.events"}
    assert tables(UPDATE_TABLE, text) == set()
    assert backfills(text) == []


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


def test_a_grant_a_trigger_clause_or_a_do_nothing_conflict_is_not_an_update() -> None:
    text = (
        "ALTER TABLE claims.claims ADD COLUMN flag integer;\n"
        "GRANT UPDATE (flag) ON claims.claims TO claims_api;\n"
        "CREATE TRIGGER t BEFORE UPDATE OR DELETE ON claims.claims\n"
        "    FOR EACH ROW EXECUTE FUNCTION f();\n"
        "INSERT INTO claims.claims (claim_id) VALUES ('x')\n"
        "    ON CONFLICT (claim_id) DO NOTHING;\n"
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


# The forms a backfill takes besides a plain UPDATE, each as a planted text the
# check refuses (with the name it gives the form) and, below, a near miss that
# it accepts. Every text runs after ADD_TO_CLAIMS, on the table that file
# altered.
ADD_TO_CLAIMS = "ALTER TABLE claims.claims ADD COLUMN flag integer;\n"
MATCHED_MERGE = (
    "MERGE INTO claims.claims AS c USING staged AS s ON s.claim_id = c.claim_id\n"
    "    WHEN MATCHED THEN UPDATE SET flag = 1;\n"
)
REFUSED_BACKFILLS = {
    "merge": (MATCHED_MERGE, "MERGE ... THEN UPDATE"),
    "with-update-in-a-cte": (
        "WITH moved AS (UPDATE claims.claims SET flag = 1 RETURNING claim_id)\n"
        "    SELECT count(*) FROM moved;\n",
        "UPDATE inside a WITH",
    ),
    "with-then-update": (
        "WITH ids AS (SELECT claim_id FROM claims.claims)\n"
        "    UPDATE claims.claims SET flag = 1\n"
        "    WHERE claim_id IN (SELECT claim_id FROM ids);\n",
        "UPDATE inside a WITH",
    ),
    "do-block": (
        "DO $body$ BEGIN UPDATE claims.claims SET flag = 1; END $body$;\n",
        "UPDATE inside a DO block",
    ),
    "do-block-second-statement": (
        "DO $$ BEGIN PERFORM 1; UPDATE ONLY claims.claims SET flag = 1; END $$;\n",
        "UPDATE inside a DO block",
    ),
    "upsert": (
        "INSERT INTO claims.claims (claim_id) VALUES ('x')\n"
        "    ON CONFLICT (claim_id) DO UPDATE SET flag = 1;\n",
        "INSERT ... ON CONFLICT ... DO UPDATE",
    ),
}
ACCEPTED_NEAR_MISSES = {
    "merge-that-only-inserts": (
        "MERGE INTO claims.claims AS c USING staged AS s ON s.claim_id = c.claim_id\n"
        "    WHEN NOT MATCHED THEN INSERT (claim_id) VALUES (s.claim_id);\n"
    ),
    "merge-into-another-table": MATCHED_MERGE.replace("claims.claims", "claims.notes"),
    "with-that-only-reads": (
        "WITH ids AS (SELECT claim_id FROM claims.claims)\n"
        "    SELECT count(*) FROM ids;\n"
    ),
    "with-update-of-another-table": (
        "WITH moved AS (UPDATE claims.notes SET flag = 1 RETURNING note_id)\n"
        "    SELECT count(*) FROM moved;\n"
    ),
    "do-block-that-only-reads": "DO $$ BEGIN PERFORM 1; END $$;\n",
    "do-block-that-updates-another-table": (
        "DO $$ BEGIN UPDATE claims.notes SET flag = 1; END $$;\n"
    ),
    "do-block-that-names-the-update-in-a-literal": (
        "DO $$ BEGIN RAISE NOTICE 'UPDATE claims.claims SET flag = 1'; END $$;\n"
    ),
    "function-that-only-defines-the-update": (
        "CREATE FUNCTION claims.set_flag() RETURNS void LANGUAGE sql AS\n"
        "    $$ UPDATE claims.claims SET flag = 1 $$;\n"
    ),
    "upsert-that-does-nothing": (
        "INSERT INTO claims.claims (claim_id) VALUES ('x')\n"
        "    ON CONFLICT (claim_id) DO NOTHING;\n"
    ),
    "upsert-into-another-table": (
        "INSERT INTO claims.notes (note_id) VALUES ('x')\n"
        "    ON CONFLICT (note_id) DO UPDATE SET flag = 1;\n"
    ),
}


@pytest.mark.parametrize("form", REFUSED_BACKFILLS)
def test_a_backfill_of_another_form_on_the_altered_table_is_refused(
    form: str,
) -> None:
    statement, named = REFUSED_BACKFILLS[form]

    message = refusal("0099_x.sql", ADD_TO_CLAIMS + statement)

    assert message is not None
    assert named in message
    assert "claims.claims" in message
    assert README in message


@pytest.mark.parametrize("form", ACCEPTED_NEAR_MISSES)
def test_a_near_miss_of_a_backfill_form_is_accepted(form: str) -> None:
    assert refusal("0099_x.sql", ADD_TO_CLAIMS + ACCEPTED_NEAR_MISSES[form]) is None


@pytest.mark.parametrize("form", REFUSED_BACKFILLS)
def test_a_backfill_form_without_the_added_column_is_accepted(form: str) -> None:
    statement, _ = REFUSED_BACKFILLS[form]

    assert refusal("0099_x.sql", statement) is None


# What the check does not see: an UPDATE in dynamic SQL (EXECUTE in a DO block
# or a function), a MERGE or an upsert inside a WITH or a DO block, an ALTER
# TABLE ... ADD without the word COLUMN, a column added by CREATE TABLE ... LIKE
# or by a view, and the same table reached through a different search_path or a
# rename earlier in the file.
def test_an_update_in_dynamic_sql_is_not_seen() -> None:
    dynamic = "DO $$ BEGIN EXECUTE 'UPDATE claims.claims SET flag = 1'; END $$;\n"

    assert backfills(dynamic) == []
    assert refusal("0099_x.sql", ADD_TO_CLAIMS + dynamic) is None


def test_a_name_with_digits_of_another_script_is_not_read_as_a_number() -> None:
    def spelled_with(first_zero: int, first_one: int) -> str:
        digits = (first_zero, first_zero, first_one, first_one + 6)
        return "".join(chr(code) for code in digits) + "_x.sql"

    arabic_indic = spelled_with(0x660, 0x661)
    fullwidth = spelled_with(0xFF10, 0xFF11)
    assert number_of("0017_x.sql") == 17
    assert int(arabic_indic[:4]) == 17
    for name in (arabic_indic, fullwidth, "001_x.sql"):
        with pytest.raises(ValueError, match="four ASCII digits"):
            number_of(name)


# ── a file that takes ACCESS EXCLUSIVE sets a lock_timeout first ────────────
LOCKS_WITHOUT_A_TIMEOUT = {
    "alter-table": "ALTER TABLE audit.events ADD COLUMN flag integer;\n",
    "drop-trigger": "DROP TRIGGER events_stamp ON audit.events;\n",
    "cluster": "CLUSTER audit.events USING events_pkey;\n",
    "lock-table": "LOCK TABLE audit.events IN ACCESS EXCLUSIVE MODE;\n",
    "lock-table-without-a-mode": "LOCK TABLE audit.events;\n",
    "lock-without-the-word-table": "LOCK audit.events, claims.claims;\n",
    "create-or-replace-view": (
        "CREATE OR REPLACE VIEW audit.claim_trail AS SELECT 1 AS claim_id;\n"
    ),
}
TIMEOUT = "SET LOCAL lock_timeout = '3s';\n"
LOCKS_THAT_ARE_NOT_ACCESS_EXCLUSIVE = {
    "create-index": "CREATE INDEX events_flag_idx ON audit.events (event);\n",
    "select": "SELECT 1;\n",
    "lock-in-share-mode": "LOCK TABLE audit.events IN SHARE MODE;\n",
    "lock-in-share-row-exclusive-mode": (
        "LOCK TABLE audit.events IN SHARE ROW EXCLUSIVE MODE;\n"
    ),
    "plain-create-view": "CREATE VIEW audit.flags AS SELECT 1 AS flag;\n",
    "alter-table-in-a-comment": "-- ALTER TABLE audit.events ADD COLUMN x integer;\n",
    "alter-table-in-a-literal": "SELECT 'ALTER TABLE audit.events';\n",
    "alter-table-in-a-function": (
        "CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS\n"
        "    $$ BEGIN ALTER TABLE audit.events ADD COLUMN x integer; END $$;\n"
    ),
}


@pytest.mark.parametrize("form", LOCKS_WITHOUT_A_TIMEOUT)
def test_a_file_that_takes_access_exclusive_without_a_timeout_is_refused(
    form: str,
) -> None:
    message = lock_timeout_refusal("0099_x.sql", LOCKS_WITHOUT_A_TIMEOUT[form])

    assert message is not None
    assert "0099_x.sql" in message
    assert "lock_timeout" in message
    assert README in message


@pytest.mark.parametrize("form", LOCKS_WITHOUT_A_TIMEOUT)
def test_a_file_that_sets_a_timeout_first_and_then_locks_is_accepted(
    form: str,
) -> None:
    text = TIMEOUT + LOCKS_WITHOUT_A_TIMEOUT[form]

    assert lock_timeout_refusal("0099_x.sql", text) is None


@pytest.mark.parametrize("form", LOCKS_THAT_ARE_NOT_ACCESS_EXCLUSIVE)
def test_a_file_without_an_access_exclusive_statement_needs_no_timeout(
    form: str,
) -> None:
    text = LOCKS_THAT_ARE_NOT_ACCESS_EXCLUSIVE[form]

    assert lock_timeout_refusal("0099_x.sql", text) is None


@pytest.mark.parametrize(
    "timeout",
    [
        "SET LOCAL lock_timeout = 0;\n",
        "SET LOCAL lock_timeout = '0';\n",
        "SET LOCAL lock_timeout = '0ms';\n",
        "SET lock_timeout = '3s';\n",
        "SET LOCAL statement_timeout = '3s';\n",
        "-- SET LOCAL lock_timeout = '3s';\n",
        "SELECT 'SET LOCAL lock_timeout = 3s';\n",
        "RESET lock_timeout;\n",
    ],
    ids=[
        "zero",
        "zero-quoted",
        "zero-with-a-unit",
        "not-local",
        "another-setting",
        "in-a-comment",
        "in-a-literal",
        "reset",
    ],
)
def test_a_timeout_that_is_off_or_is_not_one_does_not_count(timeout: str) -> None:
    text = timeout + LOCKS_WITHOUT_A_TIMEOUT["alter-table"]

    assert lock_timeout_refusal("0099_x.sql", text) is not None


@pytest.mark.parametrize(
    "timeout",
    [
        "SET LOCAL lock_timeout = '3s'",
        "SET LOCAL lock_timeout TO '3s'",
        "SET LOCAL lock_timeout = 3000",
        "set local lock_timeout = '500ms'",
        "SET LOCAL lock_timeout = '1min'",
    ],
)
def test_a_positive_timeout_in_any_of_its_spellings_counts(timeout: str) -> None:
    text = timeout + ";\n" + LOCKS_WITHOUT_A_TIMEOUT["alter-table"]

    assert lock_timeout_refusal("0099_x.sql", text) is None


def test_a_timeout_set_after_the_lock_statement_is_too_late() -> None:
    text = LOCKS_WITHOUT_A_TIMEOUT["alter-table"] + TIMEOUT

    message = lock_timeout_refusal("0099_x.sql", text)

    assert message is not None
    assert "ALTER TABLE audit.events" in message


def test_the_timeout_rule_does_not_apply_to_the_files_it_exempts() -> None:
    text = LOCKS_WITHOUT_A_TIMEOUT["alter-table"]

    assert lock_timeout_refusal("0016_x.sql", text) is None
    assert lock_timeout_refusal("0017_x.sql", text) is not None
