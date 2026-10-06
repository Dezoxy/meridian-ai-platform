"""The smoke check of the stores (S062).

``check_stores`` in ``infra/kind/smoke.sh`` is the second half of the database
check: through the database's primary pod it reads that the policy store holds
policies, that the knowledge store holds chunks and that the migrations ledger's
newest file is the newest one of the checkout the script runs from. These tests
run the function in bash against a stub ``kctl`` (the harness is the sweep
check's, in test_kind_manifests.py); the stub answers each query by its text.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from test_kind_manifests import (
    COMMON_SH,
    DEPLOY_SH,
    KIND_DIR,
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
)

from meridian.platform.migrations.runner import migration_files

MIGRATIONS = KIND_DIR.parent.parent / "src" / "meridian" / "platform" / "migrations"
# The newest file of the made-up tree the stub tests build, not of the real one.
STUB_TREE_NEWEST = "0016_runs_text_bounds.sql"
TREE = ["0001_schemas.sql", "0002_audit_route.sql", STUB_TREE_NEWEST]
SQL_NAMES = "STORES_READY_SQL|POLICY_COUNT_SQL|LEDGER_NEWEST_SQL"
CHUNKS_SQL = re.search(r"^readonly CHUNK_COUNT_SQL=(.*)$", COMMON_SH, re.M)


def run_stores_check(
    tmp_path: Path,
    *,
    tree: list[str] | None = None,
    ready: str = "1,1,1",
    policies: str = "12",
    chunks: str = "85",
    ledger: str = STUB_TREE_NEWEST,
    error: str = "psql failed",
) -> tuple[list[str], str]:
    """``check_stores`` of smoke.sh in bash against a stub ``kctl`` and a
    migrations folder holding the files ``tree`` names (default: three, the
    newest ``STUB_TREE_NEWEST``). ``ready`` is the answer of the probe for the
    stores' tables (the ledger's, the policy store's and the knowledge store's,
    each 1 or 0, separated by commas; ``FAIL``: it fails); the three others are
    the answers of the policy count, the chunk count and the ledger's newest
    name (``FAIL``: that query fails). A failing query writes ``error`` to
    stderr. Returns the output lines and what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    folder = tmp_path / "migrations"
    folder.mkdir()
    for name in TREE if tree is None else tree:
        (folder / name).touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            f'readonly MIGRATIONS_DIR="{folder}"',
            *re.findall(r"^readonly CHUNK_COUNT_SQL=.*$", COMMON_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            '    *"get pod"*) echo platform-db-1 ;;',
            '    *"to_regclass"*) answer="${READY}" ;;',
            '    *"count(*) FROM policy.policies"*) answer="${POLICIES}" ;;',
            '    *"count(*) FROM knowledge.chunks"*) answer="${CHUNKS}" ;;',
            '    *"meridian_migrations"*) answer="${LEDGER}" ;;',
            '    *) echo "unexpected: $*" >&2; return 2 ;;',
            "  esac",
            '  [[ "$*" == *"get pod"* ]] && return 0',
            '  [[ "${answer}" != FAIL ]] || { echo "${ERROR}" >&2; return 1; }',
            '  printf "%s\\n" "${answer}"',
            "}",
            function_definition(SMOKE_SH, "meridian_query"),
            function_definition(SMOKE_SH, "store_count"),
            function_definition(SMOKE_SH, "newest_migration"),
            function_definition(SMOKE_SH, "store_line"),
            function_definition(SMOKE_SH, "check_stores"),
            *re.findall(rf"^readonly (?:{SQL_NAMES})=.*$", SMOKE_SH, re.M),
            *re.findall(r"^readonly QUERY_ERROR_LENGTH=.*$", SMOKE_SH, re.M),
            "check_stores platform-db-1",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "READY": ready,
            "POLICIES": policies,
            "CHUNKS": chunks,
            "LEDGER": ledger,
            "ERROR": error,
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


def verdicts(lines: list[str]) -> list[str]:
    return [line.split()[0] for line in lines]


def test_the_stores_check_prints_three_pass_lines_for_a_seeded_current_cluster(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path)

    assert verdicts(lines) == ["PASS"] * 3
    assert lines == [
        "PASS  database: the policy store holds 12 policies (policy.policies)",
        "PASS  database: the knowledge store holds 85 chunks (knowledge.chunks)",
        f"PASS  database: the migrations ledger's newest file is {STUB_TREE_NEWEST}, "
        "the newest of this checkout",
    ]


def test_the_stores_check_fails_the_policy_store_when_it_holds_no_policy(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, policies="0")

    assert verdicts(lines) == ["FAIL", "PASS", "PASS"]
    assert lines[0].startswith("FAIL  database: the policy store holds no policies")
    assert "seed" in lines[0]


def test_the_stores_check_fails_the_knowledge_store_when_it_holds_no_chunk(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, chunks="0")

    assert verdicts(lines) == ["PASS", "FAIL", "PASS"]
    assert lines[1].startswith("FAIL  database: the knowledge store holds no chunks")
    assert "ingest" in lines[1]


def test_the_stores_check_passes_with_exactly_one_policy_and_one_chunk(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, policies="1", chunks="1")

    assert verdicts(lines) == ["PASS"] * 3


def test_the_stores_check_fails_when_the_ledger_is_behind_the_checkout(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, ledger="0015_audit_purpose.sql")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "0015_audit_purpose.sql" in lines[2]
    assert STUB_TREE_NEWEST in lines[2]
    assert "another checkout" in lines[2]


def test_the_stores_check_fails_when_the_ledger_is_ahead_of_the_checkout(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, ledger="0017_something_newer.sql")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "0017_something_newer.sql" in lines[2]


def test_the_stores_check_fails_an_empty_ledger(tmp_path: Path) -> None:
    lines, _ = run_stores_check(tmp_path, ledger="")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "ledger is empty" in lines[2]


def test_the_stores_check_takes_the_newest_file_by_name_not_by_listing(
    tmp_path: Path,
) -> None:
    tree = ["0010_b.sql", "0009_z.sql", "0002_a.sql"]

    lines, _ = run_stores_check(tmp_path, tree=tree, ledger="0010_b.sql")

    assert lines[2].startswith("PASS  database: the migrations ledger's newest file")


def test_the_stores_check_fails_the_ledger_when_the_checkout_holds_no_migration(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, tree=["README.md", "__init__.py"])

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "no migration file" in lines[2]


@pytest.mark.parametrize("ready", ["0,0,0", "0,1,1"])
def test_the_stores_check_skips_once_when_the_migrations_ledger_is_not_there_yet(
    tmp_path: Path, ready: str
) -> None:
    # Nothing was ever migrated: the ledger table is the one thing that says so.
    lines, asked = run_stores_check(tmp_path, ready=ready)

    assert lines == [
        "SKIP  database: the meridian database holds no migrated schemas yet "
        "(make deploy), so its stores are not read"
    ]
    assert "count(*)" not in asked
    assert "ORDER BY" not in asked


def test_the_stores_check_fails_when_the_schemas_cannot_be_probed(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, ready="FAIL")

    (line,) = lines
    assert line.startswith("FAIL  database: could not read the meridian database")
    assert "psql failed" in line


def test_the_stores_check_fails_a_missing_policy_table_and_reads_the_rest(
    tmp_path: Path,
) -> None:
    lines, asked = run_stores_check(tmp_path, ready="1,0,1")

    assert verdicts(lines) == ["FAIL", "PASS", "PASS"]
    assert "policy.policies does not exist" in lines[0]
    assert "make deploy" in lines[0]
    assert "count(*) FROM policy.policies" not in asked
    assert "count(*) FROM knowledge.chunks" in asked


def test_the_stores_check_fails_a_missing_knowledge_table_and_reads_the_rest(
    tmp_path: Path,
) -> None:
    lines, asked = run_stores_check(tmp_path, ready="1,1,0")

    assert verdicts(lines) == ["PASS", "FAIL", "PASS"]
    assert "knowledge.chunks does not exist" in lines[1]
    assert "count(*) FROM knowledge.chunks" not in asked
    assert "count(*) FROM policy.policies" in asked


def test_the_stores_check_names_both_tables_when_both_are_missing(
    tmp_path: Path,
) -> None:
    lines, _ = run_stores_check(tmp_path, ready="1,0,0")

    assert verdicts(lines) == ["FAIL", "FAIL", "PASS"]
    assert "policy.policies does not exist" in lines[0]
    assert "knowledge.chunks does not exist" in lines[1]


@pytest.mark.parametrize(
    "ready", ["", "t", "f", "true,true,true", "1,1", "1,1,1,1", "2,1,1", "1,x,1"]
)
def test_the_stores_check_fails_an_answer_that_is_not_in_the_probes_form(
    tmp_path: Path, ready: str
) -> None:
    lines, asked = run_stores_check(tmp_path, ready=ready)

    (line,) = lines
    assert line.startswith("FAIL  database: the probe for the stores' tables")
    assert "not in its form" in line
    assert "count(*)" not in asked


def test_a_failed_read_of_the_database_keeps_its_message_cleaned_and_cut(
    tmp_path: Path,
) -> None:
    error = "\x1b[31mFATAL: role gone\x1b[0m\n" + "x" * 400

    (tmp_path / "again").mkdir()

    lines, _ = run_stores_check(tmp_path, ready="FAIL", error=error)
    counted, _ = run_stores_check(tmp_path / "again", chunks="FAIL", error=error)

    for line in (lines[0], counted[1]):
        assert "FATAL: role gone" in line
        assert "\x1b" not in line
        assert "x" * 40 in line  # cut, not dropped
        assert "x" * 161 not in line
    assert verdicts(counted) == ["PASS", "FAIL", "PASS"]


def test_the_stores_check_fails_one_line_when_one_query_fails(tmp_path: Path) -> None:
    lines, _ = run_stores_check(tmp_path, chunks="FAIL")

    assert verdicts(lines) == ["PASS", "FAIL", "PASS"]
    assert "could not be read" in lines[1]


def test_the_stores_check_fails_a_count_that_is_not_a_number(tmp_path: Path) -> None:
    lines, _ = run_stores_check(tmp_path, policies="many")

    assert verdicts(lines) == ["FAIL", "PASS", "PASS"]
    assert "could not be read" in lines[0]


def test_the_stores_check_prints_no_row_only_counts_and_a_file_name(
    tmp_path: Path,
) -> None:
    body = function_body(SMOKE_SH, "check_stores")
    queries = re.findall(rf"^readonly (?:{SQL_NAMES})=(.*)$", SMOKE_SH, re.M)

    # Every query is a probe, a count or the ledger's newest name.
    assert len(queries) == 3
    probe, policy_count, ledger_newest = (q.strip("'\"") for q in queries)
    assert re.findall(r"to_regclass\('([a-z_.]+)'\)", probe) == [
        "public.meridian_migrations",
        "policy.policies",
        "knowledge.chunks",
    ]
    assert " FROM " not in probe
    assert policy_count.split()[1] == "count(*)"
    assert ledger_newest.split()[1] == "name"
    assert "SELECT count(*) FROM knowledge" in COMMON_SH
    assert not re.search(r"kctl[^\n]*\b(create|apply|delete|patch|replace)\b", body)


def test_the_chunk_count_is_one_query_that_deploy_and_smoke_both_use() -> None:
    assert CHUNKS_SQL is not None
    sql = CHUNKS_SQL.group(1).strip("'\"")

    assert sql == "SELECT count(*) FROM knowledge.chunks"
    assert "${CHUNK_COUNT_SQL}" in function_body(DEPLOY_SH, "stored_chunk_count")
    assert "${CHUNK_COUNT_SQL}" in function_body(SMOKE_SH, "check_stores")
    assert "FROM knowledge.chunks" not in DEPLOY_SH
    assert "FROM knowledge.chunks" not in SMOKE_SH


def test_the_newest_migration_of_the_real_tree_is_the_one_smoke_reads(
    tmp_path: Path,
) -> None:
    script = "\n".join(
        [
            "set -euo pipefail",
            f'KIND_DIR="{KIND_DIR}"',
            *re.findall(r"^readonly MIGRATIONS_DIR=.*$", SMOKE_SH, re.M),
            function_definition(SMOKE_SH, "newest_migration"),
            "newest_migration",
        ]
    )

    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )

    # The shell's byte sort agrees with the newest packaged file read from the
    # directory in Python and with the one the runner applies last: no file
    # name is written here, so the next migration does not break this test.
    packaged = sorted(p.name for p in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    applied_last = migration_files()[-1][0]
    assert packaged
    assert done.stdout.strip() == packaged[-1]
    assert done.stdout.strip() == applied_last


def test_the_readme_says_what_the_stores_lines_read() -> None:
    readme = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())

    # The count of lines the documents state is pinned in
    # test_smoke_alert_rules.py, the latest check to add lines.
    assert "**Database.** Five lines" in readme
    assert "`policy.policies` holds policies" in readme
    assert "the database's clock" in readme
    assert "not this laptop's `date`" in readme
    assert "read the time against" not in readme


def test_the_stores_check_runs_inside_the_database_check_and_is_documented() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]
    header = SMOKE_SH.split("set -euo pipefail")[0]

    # The database check is still the second, and the stores are read by it
    # with the primary pod it found: no further numbered check.
    assert calls[1] == "check_database"
    assert 'check_stores "${primary}"' in function_body(SMOKE_SH, "check_database")
    assert "policy.policies" in header
    assert "knowledge.chunks" in header
    assert "meridian_migrations" in header
    assert "not prove" in header.split("2. database:")[1].split("3. tools:")[0]
