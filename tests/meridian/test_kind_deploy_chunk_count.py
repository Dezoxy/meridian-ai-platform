"""A chunk count that is not a number does not start an ingestion (S073, K9, K13).

``deploy.sh`` skips the ingestion when its Job succeeded and the store holds
rows. Ingesting again removes the kept ingest Jobs and embeds the corpus again:
model calls, which cost money. K9 stopped the deploy when the count could not be
read at all (the pod not found, the call ended at its outer bound, the pod not
reached) but let psql's own error and an empty or garbled answer ingest again.
K13 narrowed that: ``migrate`` runs before ``ingest_corpus``, so the table
exists and an error never means "no rows". The only answer that ingests again is
exit 0 with the text ``0``; a positive number skips; anything else stops the
deploy with nothing removed.

The functions run in bash, from the script's text, against a stand-in ``kctl``
that logs what it was asked and answers as the case says.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR, SECONDS

DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")

# What kubectl prints when the command it ran in the pod ended with a status.
PSQL_FAILED = "error:command terminated with exit code {}"

# The answers of the stand-in ``kctl`` to the lookup of the pod and to the count.
# ``pod`` is what the lookup does, ``count`` what the exec does.
READABLE = {
    "rows": {"pod": "found", "count": "answer:12", "status": 0},
    "17 rows": {"pod": "found", "count": "answer:17", "status": 0},
    "no rows": {"pod": "found", "count": "answer:0", "status": 0},
}
# Answers that are not "a number, with exit 0": psql's own errors (kubectl exits
# with psql's status and says so), a remote status outside 1 to 3, and exit 0
# with an answer that is no number.
UNREADABLE = {
    "psql exits 1 (a statement or lock timeout, or any error)": {
        "pod": "found",
        "count": PSQL_FAILED.format(1),
        "status": 1,
    },
    "psql exits 2 (could not connect)": {
        "pod": "found",
        "count": PSQL_FAILED.format(2),
        "status": 2,
    },
    "psql exits 3 (an error in a script)": {
        "pod": "found",
        "count": PSQL_FAILED.format(3),
        "status": 3,
    },
    "the command in the pod was ended by TERM (143)": {
        "pod": "found",
        "count": PSQL_FAILED.format(143),
        "status": 143,
    },
    "an empty answer with exit 0": {"pod": "found", "count": "answer:", "status": 0},
    "an answer that is no number, with exit 0": {
        "pod": "found",
        "count": "answer:ERROR: relation does not exist",
        "status": 0,
    },
    "a negative number, with exit 0": {
        "pod": "found",
        "count": "answer:-1",
        "status": 0,
    },
    "two lines, with exit 0": {"pod": "found", "count": "answer:0\n0", "status": 0},
}
UNREADABLE |= {
    "the lookup of the pod fails": {"pod": "fails", "count": "", "status": 0},
    "the pod is not found": {"pod": "empty", "count": "", "status": 0},
    "the count ends at the outer bound (124)": {
        "pod": "found",
        "count": "error:kctl: kubectl exec ended with status 124",
        "status": 124,
    },
    "the count ends at the outer bound (137)": {
        "pod": "found",
        "count": "error:kctl: kubectl exec ended with status 137",
        "status": 137,
    },
    "kubectl cannot reach the pod": {
        "pod": "found",
        "count": "error:error: unable to upgrade connection: pod does not exist",
        "status": 1,
    },
    "kubectl cannot reach the server": {
        "pod": "found",
        "count": "error:Unable to connect to the server: net/http: timeout",
        "status": 1,
    },
}


def definition(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", script, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return f"{name}() {{\n{match.group(1)}}}\n"


def run_ingest_corpus(
    tmp_path: Path, case: dict[str, object]
) -> tuple[int, list[str], str]:
    """``stored_chunk_count`` and ``ingest_corpus`` of deploy.sh with the Job of
    this tag succeeded. Returns the exit status, the lines the stand-ins wrote
    to standard output and what went to standard error. The lines start with
    ``LOG``, ``DIE`` (the script stopped), ``DELETE`` (a ``kctl delete``) or
    ``RUN`` (``run_job``, the ingestion)."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "NAMESPACE=meridian; tag=abc; image=meridian:abc; ingested_at=''",
            *re.findall(r"^readonly CHUNK_COUNT_SQL=.*$", COMMON_SH, re.M),
            *re.findall(
                r"^readonly (?:PSQL_OPTIONS|DELETE_TIMEOUT)=.*$", DEPLOY_SH, re.M
            ),
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*"; exit 1; }',
            definition(COMMON_SH, "printable_ascii"),
            "job_state() { echo succeeded; }",
            'run_job() { echo "RUN $*"; }',
            "kctl() {",
            '  case "$*" in',
            # The script sends a delete's output to /dev/null: it is logged to a file.
            '    *"delete "*) echo "DELETE $*" >>"${DELETES}" ;;',
            '    *" exec "*)',
            '      case "${COUNT}" in',
            '        answer:*) echo "${COUNT#answer:}" ;;',
            '        error:*) echo "${COUNT#error:}" >&2 ;;',
            "      esac",
            '      return "${COUNT_STATUS}" ;;',
            '    *"get pod"*)',
            '      case "${POD}" in',
            "        found) echo platform-db-1 ;;",
            "        fails) echo 'Unable to connect to the server' >&2; return 1 ;;",
            "      esac ;;",
            '    *"get job"*) echo job.batch/meridian-ingest-abc ;;',
            "  esac",
            "}",
            definition(DEPLOY_SH, "stored_chunk_count"),
            definition(DEPLOY_SH, "ingest_corpus"),
            "ingest_corpus",
            'echo "ingested_at=${ingested_at}"',
        ]
    )
    deletes = tmp_path / "deletes"
    deletes.touch()
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "TMPDIR": str(tmp_path),
            "DELETES": str(deletes),
            "POD": str(case["pod"]),
            "COUNT": str(case["count"]),
            "COUNT_STATUS": str(case["status"]),
        },
        check=False,
        timeout=SECONDS,
    )
    lines = done.stdout.splitlines() + deletes.read_text(encoding="utf-8").splitlines()
    return done.returncode, lines, done.stderr


def of_kind(lines: list[str], *prefixes: str) -> list[str]:
    return [line for line in lines if line.startswith(prefixes)]


@pytest.mark.parametrize("label", list(UNREADABLE))
def test_a_count_that_cannot_be_read_stops_the_deploy_and_removes_nothing(
    tmp_path: Path, label: str
) -> None:
    status, lines, _ = run_ingest_corpus(tmp_path, UNREADABLE[label])

    assert status == 1
    (stop,) = of_kind(lines, "DIE")
    assert "could not read" in stop
    assert "knowledge.chunks" in stop
    assert "Nothing was removed" in stop
    # No delete of the ingest Jobs, no Job applied, no embedding: the stand-ins
    # for both would have written a line.
    assert of_kind(lines, "DELETE", "RUN") == []
    assert not [line for line in lines if "ingesting again" in line]


def test_the_answer_zero_with_exit_0_is_the_one_answer_that_ingests_again(
    tmp_path: Path,
) -> None:
    status, lines, _ = run_ingest_corpus(tmp_path, READABLE["no rows"])

    assert status == 0
    assert of_kind(lines, "DIE") == []
    assert len([line for line in lines if "ingesting again" in line]) == 1
    assert len(of_kind(lines, "DELETE")) == 1
    assert "meridian-ingest" in of_kind(lines, "DELETE")[0]
    assert of_kind(lines, "RUN") == ["RUN meridian-ingest-abc ingest"]


@pytest.mark.parametrize("label", ["rows", "17 rows"])
def test_a_store_that_holds_rows_is_not_ingested_again(
    tmp_path: Path, label: str
) -> None:
    status, lines, _ = run_ingest_corpus(tmp_path, READABLE[label])

    assert status == 0
    assert [line for line in lines if "already in the store" in line]
    assert of_kind(lines, "DIE", "DELETE", "RUN") == []


def test_what_kubectl_said_about_an_unreadable_count_is_shown_before_the_stop(
    tmp_path: Path,
) -> None:
    case = UNREADABLE["the count ends at the outer bound (124)"]

    status, _, shown = run_ingest_corpus(tmp_path, case)

    assert status == 1
    assert "kubectl exec ended with status 124" in shown
