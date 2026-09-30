"""The command line: no malformed resume unless it is asked for by name."""

from pathlib import Path

import pytest
from support import OVER_THRESHOLD_CLAIM, run_cli, run_cli_process

ADJUSTER = "ADJ-0042"


def _start(framework: str, store: str) -> dict:
    return run_cli(
        "start", "--framework", framework, "--claim-id", OVER_THRESHOLD_CLAIM,
        "--store-dir", store,
    )  # fmt: skip


def _resume_args(framework: str, run_ref: str, store: str) -> list[str]:
    return [
        "resume", "--framework", framework, "--run-ref", run_ref, "--store-dir", store,
    ]  # fmt: skip


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--decision", "approve"],
        ["--adjuster-id", ADJUSTER],
    ],
    ids=["no-flags", "decision-only", "adjuster-only"],
)
def test_resume_without_both_decision_flags_is_refused_before_any_framework_runs(
    framework: str, tmp_path: Path, flags: list[str]
) -> None:
    store = str(tmp_path / "store")
    paused = _start(framework, store)

    refused = run_cli_process(
        *_resume_args(framework, paused["run_ref"], store), *flags
    )

    assert refused.returncode == 2
    assert "--decision and --adjuster-id" in refused.stderr
    assert refused.stdout == ""
    # The pause is untouched: a complete resume still works.
    done = run_cli(
        *_resume_args(framework, paused["run_ref"], store),
        "--decision", "approve", "--adjuster-id", ADJUSTER,
    )  # fmt: skip
    assert done["status"] == "completed"


def test_raw_payload_sends_an_empty_object_to_langgraph_which_re_pauses(
    tmp_path: Path,
) -> None:
    store = str(tmp_path / "store")
    paused = _start("langgraph", store)

    again = run_cli(*_resume_args("langgraph", paused["run_ref"], store),
                    "--raw-payload", "{}")  # fmt: skip

    assert again["status"] == "awaiting_approval"


def test_raw_payload_sends_an_empty_object_to_maf_which_refuses_it(
    tmp_path: Path,
) -> None:
    store = str(tmp_path / "store")
    paused = _start("maf", store)

    refused = run_cli_process(*_resume_args("maf", paused["run_ref"], store),
                              "--raw-payload", "{}")  # fmt: skip

    assert refused.returncode != 0
    assert "Response type mismatch" in refused.stderr


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--raw-payload", "{}", "--decision", "approve"], "excludes"),
        (["--raw-payload", "{not json"], "not JSON"),
    ],
    ids=["mixed", "not-json"],
)
def test_raw_payload_is_refused_when_mixed_with_flags_or_not_json(
    tmp_path: Path, flags: list[str], message: str
) -> None:
    refused = run_cli_process(
        *_resume_args("langgraph", "any-run", str(tmp_path / "store")), *flags
    )

    assert refused.returncode == 2
    assert message in refused.stderr


def test_maf_store_option_is_refused_for_langgraph(tmp_path: Path) -> None:
    refused = run_cli_process(
        "start", "--framework", "langgraph", "--maf-store", "json",
        "--claim-id", OVER_THRESHOLD_CLAIM, "--store-dir", str(tmp_path / "store"),
    )  # fmt: skip

    assert refused.returncode == 2
    assert "--maf-store applies to --framework maf only" in refused.stderr
