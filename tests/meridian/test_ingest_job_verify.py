"""The ingestion Job on kind runs ``meridian knowledge verify`` after the write
(S067, T-57).

Tested without a cluster: the chart is rendered with ``helm template``, and the
Job's command is run in a shell with a stand-in for ``meridian`` that records
what it was asked and exits as the test says. Nothing here reaches a cluster; the
next step that holds the cluster proves the Job there.
"""

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
import typer.main
from chartsupport import TEST_TAG, rendered_chart
from servicesupport import REPO_ROOT

from meridian.platform.cli import app as meridian_cli

DEPLOY_SH = (REPO_ROOT / "infra" / "kind" / "deploy.sh").read_text(encoding="utf-8")
TENANT = "claims-triage"
SOURCE = "/opt/meridian/synthetic"
# Stands in for ``meridian``: logs its words, then exits with the status the
# test set for that subcommand (``FAIL_<word>=<status>``).
STAND_IN = """#!/bin/sh
echo "$2" >> "$STAND_IN_LOG"
eval "status=\\${FAIL_$2:-0}"
exit "$status"
"""


def ingest_job() -> dict:
    (found,) = [
        d
        for d in rendered_chart()
        if d["kind"] == "Job" and d["metadata"]["name"] == f"meridian-ingest-{TEST_TAG}"
    ]
    return found


def the_command() -> list[str]:
    (container,) = ingest_job()["spec"]["template"]["spec"]["containers"]
    return container["command"]


def the_script() -> str:
    command = the_command()
    assert command[:2] == ["sh", "-c"], command
    assert len(command) == 3, command
    return command[2]


def cli_words(words: list[str]) -> list[str]:
    """The leading words that name commands of the ``meridian`` CLI, resolved
    through the Typer tree (a renamed command stops matching)."""
    command = typer.main.get_command(meridian_cli)
    found: list[str] = []
    for word in words:
        subcommands = getattr(command, "commands", None)
        if not subcommands or word not in subcommands:
            break
        found.append(word)
        command = subcommands[word]
    return found


def the_two_commands() -> tuple[list[str], list[str]]:
    words = shlex.split(the_script())
    assert words.count("&&") == 1, words
    cut = words.index("&&")
    return words[:cut], words[cut + 1 :]


def run_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **statuses: int
) -> tuple[int, list[str]]:
    """Run the Job's command with ``meridian`` replaced by the stand-in; the
    status each subcommand exits with is ``statuses`` (default 0). Returns the
    shell's status and the subcommands that ran, in order."""
    stand_in = tmp_path / "meridian"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STAND_IN_LOG", str(log))
    for word, status in statuses.items():
        monkeypatch.setenv(f"FAIL_{word}", str(status))
    command = the_command()
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    return done.returncode, log.read_text(encoding="utf-8").split()


def test_the_job_runs_the_ingestion_and_then_the_verification() -> None:
    ingest, verify = the_two_commands()

    assert ingest == [
        "meridian",
        "knowledge",
        "ingest",
        "--tenant",
        TENANT,
        "--from",
        SOURCE,
    ]
    assert verify == ["meridian", "knowledge", "verify", "--from", SOURCE]
    assert cli_words(ingest[1:]) == ["knowledge", "ingest"]
    assert cli_words(verify[1:]) == ["knowledge", "verify"]


def test_the_verification_reads_the_same_wordings_the_ingestion_wrote() -> None:
    ingest, verify = the_two_commands()

    assert ingest[ingest.index("--from") + 1] == verify[verify.index("--from") + 1]


def test_a_clean_ingestion_and_verification_end_the_job_with_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, ran = run_script(tmp_path, monkeypatch)

    assert status == 0
    assert ran == ["ingest", "verify"]


@pytest.mark.parametrize("difference_status", [1, 2])
def test_a_difference_or_an_unverifiable_store_fails_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, difference_status: int
) -> None:
    status, ran = run_script(tmp_path, monkeypatch, verify=difference_status)

    assert status == difference_status
    assert ran == ["ingest", "verify"]


def test_a_failed_ingestion_fails_the_job_and_the_verification_does_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, ran = run_script(tmp_path, monkeypatch, ingest=1)

    assert status == 1
    assert ran == ["ingest"]


def test_the_deploy_script_stops_on_a_failed_job_and_so_on_a_failed_verification() -> (
    None
):
    start = DEPLOY_SH.index("run_job() {")
    body = DEPLOY_SH[start : DEPLOY_SH.index("\n}\n", start)]

    failed = body[body.index("failed)") :]
    assert 'die "the job ${job} failed' in failed.split(";;")[0]
