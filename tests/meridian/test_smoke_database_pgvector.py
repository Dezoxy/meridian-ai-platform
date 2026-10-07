"""The database check's two pgvector lines keep psql's message (S073 K4).

``check_database`` in ``infra/kind/smoke.sh`` reads ``pg_extension`` in the
primary's pod, in the ``app`` and the ``meridian`` database. The read ended in
``2>/dev/null || true``, so a read that failed (a pod that could not be reached,
a statement that hit its deadline) read as an empty answer and the line said the
extension was "not installed": a wrong cause. Now a read that fails says so,
with the first line of what it wrote, cleaned and cut. The harness runs the
function in bash against a stub ``kctl``; the API server line and the stores
have harnesses of their own (``test_smoke_stores.py``, ``test_kind_database_*``).
"""

import os
import re
import subprocess
from pathlib import Path

from test_kind_manifests import (
    SMOKE_SH,
    function_definition,
    one_line_function,
    requires_jq,
)

PRIMARY = "platform-db-1"
CUT = int(re.findall(r"^readonly QUERY_ERROR_LENGTH=(\d+)$", SMOKE_SH, re.M)[0])
STUB = r"""
kctl() {
  printf '%s\n' "$*" >>"${ASKED}"
  case "$*" in
    *" get pod "*) printf '%s' "${PRIMARY}" ;;
    *" exec "*)
      all="$*"; database="${all#* -d }"; database="${database%% *}"
      if [[ -e "${STATE}/error-${database}" ]]; then
        cat "${STATE}/error-${database}" >&2
        return 1
      fi
      cat "${STATE}/answer-${database}" ;;
  esac
}
"""


def run_pgvector(
    tmp_path: Path,
    *,
    answers: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> tuple[list[str], str]:
    """The lines of ``check_database`` (the API server line and the stores are
    stubbed out) and the calls ``kctl`` got. ``answers`` maps a database to what
    psql prints (the version by default); ``errors`` maps one to what the call
    writes on stderr before it exits 1."""
    state = tmp_path / "state"
    state.mkdir()
    for database in ("app", "meridian"):
        text = (answers or {}).get(database, "0.8.1\n")
        (state / f"answer-{database}").write_text(text)
    for database, message in (errors or {}).items():
        (state / f"error-{database}").write_text(message)
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            *re.findall(
                r"^readonly (?:PSQL_OPTIONS|QUERY_ERROR_LENGTH)=.*$", SMOKE_SH, re.M
            ),
            one_line_function(SMOKE_SH, "clean_lines"),
            STUB,
            "check_database_api_server() { :; }",
            "check_stores() { :; }",
            function_definition(SMOKE_SH, "check_database"),
            "check_database",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "ASKED": str(asked),
            "STATE": str(state),
            "PRIMARY": PRIMARY,
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


@requires_jq
def test_two_installed_extensions_print_the_two_lines_they_printed_before(
    tmp_path: Path,
) -> None:
    lines, _ = run_pgvector(tmp_path)

    assert lines == [
        f"PASS  database: pgvector 0.8.1 installed in {PRIMARY}, database app",
        f"PASS  database: pgvector 0.8.1 installed in {PRIMARY}, database meridian",
    ]


@requires_jq
def test_a_read_that_answers_nothing_is_still_extension_not_installed(
    tmp_path: Path,
) -> None:
    lines, _ = run_pgvector(tmp_path, answers={"app": ""})

    assert lines[0] == (
        f"FAIL  database: extension vector is not installed in {PRIMARY}, database app"
    )
    assert lines[1].startswith("PASS")


@requires_jq
def test_a_read_that_fails_says_it_failed_with_the_first_line_of_the_error(
    tmp_path: Path,
) -> None:
    first = "psql: error: connection to server failed: FATAL: the database is starting"

    lines, _ = run_pgvector(tmp_path, errors={"meridian": f"{first}\nsecond line\n"})

    assert lines[0].startswith("PASS")
    assert lines[1] == (
        f"FAIL  database: could not read pg_extension in {PRIMARY}, database "
        f"meridian: {first}"
    )
    assert "not installed" not in lines[1]
    assert "second line" not in lines[1]


@requires_jq
def test_a_failed_read_of_one_database_does_not_stop_the_read_of_the_other(
    tmp_path: Path,
) -> None:
    lines, asked = run_pgvector(tmp_path, errors={"app": "boom\n"})

    assert lines[0].startswith("FAIL  database: could not read pg_extension")
    assert lines[1].startswith("PASS  database: pgvector 0.8.1")
    assert len([c for c in asked.splitlines() if " exec " in c]) == 2


@requires_jq
def test_the_message_is_cleaned_of_bytes_that_are_not_printable_ascii(
    tmp_path: Path,
) -> None:
    lines, _ = run_pgvector(tmp_path, errors={"app": "\x1b[31merror\x1b[0m é here\n"})

    assert "\x1b" not in lines[0] and "é" not in lines[0]
    assert "[31merror[0m  here" in lines[0]


@requires_jq
def test_the_message_is_cut_to_the_length_the_other_failed_reads_use(
    tmp_path: Path,
) -> None:
    lines, _ = run_pgvector(tmp_path, errors={"app": "x" * (CUT + 100) + "\n"})

    assert "x" * CUT in lines[0]
    assert "x" * (CUT + 1) not in lines[0]


@requires_jq
def test_a_failed_read_with_no_message_says_so(tmp_path: Path) -> None:
    lines, _ = run_pgvector(tmp_path, errors={"app": ""})

    assert lines[0].endswith("database app: no message")


@requires_jq
def test_the_read_still_carries_its_deadline(tmp_path: Path) -> None:
    _, asked = run_pgvector(tmp_path)

    execs = [call for call in asked.splitlines() if " exec " in call]
    assert len(execs) == 2
    options = "PGOPTIONS=-c statement_timeout=5s -c lock_timeout=3s"
    assert all(options in call for call in execs)
