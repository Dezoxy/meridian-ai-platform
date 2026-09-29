"""The claim flow, once per framework. Every test here runs for both."""

from pathlib import Path
from types import ModuleType

import pytest
from claimflow import rules
from support import (
    AUTO_APPROVE_CLAIM,
    BOUNDARY_CLAIM,
    LAPSED_POLICY_CLAIM,
    OUT_OF_DATE_CLAIM,
    OVER_THRESHOLD_CLAIM,
    run_cli,
)

ADJUSTER = "ADJ-0042"


def test_auto_approve_claim_completes_without_pausing(
    flow: ModuleType, store_dir: Path
) -> None:
    assert rules.golden_route(AUTO_APPROVE_CLAIM) == "auto_approve"  # cross-check

    result = flow.start(AUTO_APPROVE_CLAIM, store_dir)

    assert result.status == "completed"
    assert result.claim_id == AUTO_APPROVE_CLAIM
    assert result.proposal == "auto_approve"
    assert result.outcome == "auto_approve"
    assert result.adjuster_id is None
    assert result.run_ref


@pytest.mark.parametrize(
    ("claim_id", "proposal"),
    [
        (OVER_THRESHOLD_CLAIM, "approve"),
        (BOUNDARY_CLAIM, "approve"),
        (LAPSED_POLICY_CLAIM, "reject"),
        (OUT_OF_DATE_CLAIM, "reject"),
    ],
)
def test_claim_needing_a_decision_pauses_awaiting_approval(
    flow: ModuleType, store_dir: Path, claim_id: str, proposal: str
) -> None:
    result = flow.start(claim_id, store_dir)

    assert result.status == "awaiting_approval"
    assert result.proposal == proposal
    assert result.outcome is None
    assert result.adjuster_id is None
    assert result.run_ref


def test_resume_records_the_adjuster_decision_and_completes(
    flow: ModuleType, store_dir: Path
) -> None:
    paused = flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    done = flow.resume(
        paused.run_ref, {"decision": "reject", "adjuster_id": ADJUSTER}, store_dir
    )

    assert done.status == "completed"
    assert done.claim_id == OVER_THRESHOLD_CLAIM
    assert done.proposal == "approve"  # the proposal is kept, the decision differs
    assert done.outcome == "reject"
    assert done.adjuster_id == ADJUSTER


def test_pause_and_resume_in_two_processes_sharing_only_the_store(
    framework: str, tmp_path: Path
) -> None:
    store = str(tmp_path / "store")

    paused = run_cli(
        "start", "--framework", framework, "--claim-id", OVER_THRESHOLD_CLAIM,
        "--store-dir", store,
    )  # fmt: skip
    assert paused["status"] == "awaiting_approval"

    done = run_cli(
        "resume", "--framework", framework, "--run-ref", paused["run_ref"],
        "--decision", "approve", "--adjuster-id", ADJUSTER, "--store-dir", store,
    )  # fmt: skip

    assert done["status"] == "completed"
    assert done["outcome"] == "approve"
    assert done["adjuster_id"] == ADJUSTER
    assert done["claim_id"] == OVER_THRESHOLD_CLAIM


def test_unknown_claim_id_surfaces_the_rules_error_from_the_framework(
    flow: ModuleType, store_dir: Path
) -> None:
    # An exception in a step reaches the caller with its own type in both.
    with pytest.raises(rules.UnknownClaimError):
        flow.start("CLM-9999", store_dir)
