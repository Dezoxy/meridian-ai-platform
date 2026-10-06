"""Probe 3: a resume in a new process, and the response type it must carry.

The second process is a second interpreter (``python -m s037probe.rig``) that
shares only the PostgreSQL table with the first: it builds its workflow, its
clients and its store from nothing.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from s037probe.flow import Marker
from s037probe.rig import Rig, all_checkpoints, describe, latest_sync, resume, start

SPIKE_ROOT = Path(__file__).resolve().parents[1]
PROCESS_SECONDS = 120


def process(action: str, dsn: str, thread_id: uuid.UUID, *extra: str) -> dict[str, Any]:
    """One leg in a process of its own; its one line of JSON."""
    done = subprocess.run(
        [sys.executable, "-m", "s037probe.rig", action, dsn, str(thread_id), *extra],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=True,
        cwd=SPIKE_ROOT,
        env={**os.environ, "PYTHONPATH": str(SPIKE_ROOT / "src")},
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_pause_is_resumed_by_a_process_that_shares_only_the_store(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    started = process("start", scratch_schema, thread_id)

    resumed = process("resume", scratch_schema, thread_id, "{}")

    assert started["paused"] is True
    assert started["counts"] == {"gather": 1, "draft": 1, "ask": 1}
    assert resumed["paused"] is False
    assert resumed["outputs"] == [{"brief": "a synthetic brief", "filed": True}]
    # Only the steps after the pause ran in the second process: nothing before
    # it ran again, and the brief came out of the checkpoint, not out of a call.
    assert resumed["counts"] == {"answered": 1, "file": 1}


def test_the_runtimes_empty_object_is_enough_when_the_response_type_is_a_marker(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)

    deps, result = resume(rig, {})

    assert describe(result)["outputs"] == [
        {"brief": "a synthetic brief", "filed": True}
    ]
    assert dict(deps.counts) == {"answered": 1, "file": 1}


def test_the_hosts_own_marker_instance_is_enough_too(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)

    _, result = resume(rig, Marker())

    assert describe(result)["paused"] is False


@pytest.mark.parametrize(
    "wrong", [{"extra": "CANARY-9c2e"}, "CANARY-9c2e", 42, None, ["CANARY-9c2e"]]
)
def test_a_response_that_is_not_a_marker_raises_a_value_error_before_anything_runs(
    scratch_schema: str, thread_id: uuid.UUID, wrong: Any
) -> None:
    rig = Rig(scratch_schema, thread_id)
    start(rig)
    before = latest_sync(rig)

    with pytest.raises(ValueError, match="Response type mismatch") as raised:
        resume(rig, wrong)

    # The message names the types, never the value that was sent.
    assert "CANARY-9c2e" not in str(raised.value)
    # Refused before the response-entry checkpoint: the pause is as it was, and
    # a good response still completes the run.
    assert latest_sync(rig).checkpoint_id == before.checkpoint_id
    assert len(all_checkpoints(rig)) == 4
    _, result = resume(rig, {})
    assert describe(result)["paused"] is False


def test_a_mismatch_in_a_second_process_is_reported_by_the_process_not_hidden(
    scratch_schema: str, thread_id: uuid.UUID
) -> None:
    process("start", scratch_schema, thread_id)

    refused = process("resume", scratch_schema, thread_id, '"text"')
    accepted = process("resume", scratch_schema, thread_id, "{}")

    assert refused["raised"] == "ValueError"
    assert refused["message"].startswith("Response type mismatch")
    assert accepted["outputs"] == [{"brief": "a synthetic brief", "filed": True}]
