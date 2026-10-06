"""The scheduled sweep and the claim brief (S037, A1): a run of ``claim-brief``
is ended as an abandoned one of ``claims-triage`` is, and it is kept while a
brief of its own tenant points at it and waits for its decision, or has changed
within the lease. The sweep reads no brief text and writes nothing to the table.
The rest of the sweep is in ``test_sweep.py``."""

import subprocess
import sys
import uuid

import pytest
from dbsupport import DatabaseHandle
from servicesupport import owner_rows
from sweepsupport import (
    AGENT,
    CHECKPOINT_TABLES,
    MINUTE,
    add_claim,
    add_run,
    checkpoint_rows,
    one_pass,
    status_of,
)

from meridian.platform.common.db import connect
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage import lifecycle, sweep
from meridian.workloads.claims_triage.sweep import (
    SERVICE_NAME,
    PassResult,
    end_run_unless_kept,
    list_abandoned_runs,
)

BRIEF_AGENT = "claim-brief"
OTHER_TENANT = "another-tenant"
CLAIM = "CLM-5501"
OLD = 2 * RUNNING_LEASE_SECONDS
JUST_INSIDE = RUNNING_LEASE_SECONDS - MINUTE
JUST_OUTSIDE = RUNNING_LEASE_SECONDS + MINUTE


def add_brief(
    db: DatabaseHandle,
    claim_id: str,
    state: str,
    run_id: uuid.UUID | None,
    *,
    age_seconds: float,
    tenant: str = "development",
) -> None:
    """A brief in ``state`` since ``age_seconds`` ago, naming ``run_id``; a state
    that needs a text (the table's CHECK) holds one."""
    text = "a brief" if state in ("awaiting_decision", "filed", "rejected") else None
    owner_rows(
        db,
        "INSERT INTO claims.briefs (claim_id, tenant, run_id, state, brief, "
        "state_changed_at) "
        "VALUES (%s, %s, %s, %s, %s, now() - make_interval(secs => %s)) RETURNING 1",
        (claim_id, tenant, run_id, state, text, float(age_seconds)),
    )


def brief_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(db, "SELECT * FROM claims.briefs ORDER BY brief_id")


def a_brief_run(
    db: DatabaseHandle, status: str = "AwaitingApproval", idle: float = OLD
) -> tuple[uuid.UUID, str]:
    return add_run(db, status, idle_seconds=idle, agent=BRIEF_AGENT, reference=CLAIM)


# ── the agent the sweep lists ───────────────────────────────────────────────
def test_the_sweep_names_the_brief_agent_from_the_module_that_loads_no_web_stack() -> (
    None
):
    assert lifecycle.BRIEF_AGENT == "claim-brief"
    assert sweep.BRIEF_AGENT is lifecycle.BRIEF_AGENT


def test_importing_the_briefs_module_gives_the_same_agent_constant() -> None:
    from meridian.workloads.claims_triage import briefs

    assert briefs.BRIEF_AGENT is lifecycle.BRIEF_AGENT


def test_the_sweep_still_loads_no_web_stack_with_the_brief_agent_in_it() -> None:
    code = (
        "import sys; import meridian.workloads.claims_triage.sweep; "
        "print('fastapi' in sys.modules or "
        "'meridian.workloads.claims_triage.briefs' in sys.modules)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == "False"


# ── a paused brief keeps its run ────────────────────────────────────────────
def test_a_paused_briefs_run_older_than_the_lease_is_kept(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "awaiting_adjuster", age_seconds=OLD)
    run_id, thread = a_brief_run(fresh_database)
    add_brief(fresh_database, CLAIM, "awaiting_decision", run_id, age_seconds=OLD)

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 0, 0, 0, 0)
    assert status_of(fresh_database, run_id) == "AwaitingApproval"
    # One row in each checkpoint table, the second host's included: all kept.
    assert checkpoint_rows(fresh_database, thread) == len(CHECKPOINT_TABLES)


def test_a_paused_brief_keeps_its_run_however_long_it_has_waited(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "approved", age_seconds=OLD)
    run_id, _ = a_brief_run(fresh_database, idle=100 * RUNNING_LEASE_SECONDS)
    add_brief(
        fresh_database,
        CLAIM,
        "awaiting_decision",
        run_id,
        age_seconds=100 * RUNNING_LEASE_SECONDS,
    )

    result = one_pass(fresh_database)

    assert result.runs_ended == 0
    assert status_of(fresh_database, run_id) == "AwaitingApproval"


@pytest.mark.parametrize("state", ["filed", "rejected", "failed", "drafting"])
def test_a_brief_that_changed_within_the_lease_keeps_its_run_in_any_state(
    fresh_database: DatabaseHandle, state: str
) -> None:
    add_claim(fresh_database, CLAIM, "approved", age_seconds=OLD)
    run_id, _ = a_brief_run(fresh_database)
    add_brief(fresh_database, CLAIM, state, run_id, age_seconds=JUST_INSIDE)

    result = one_pass(fresh_database)

    assert result.runs_ended == 0
    assert status_of(fresh_database, run_id) == "AwaitingApproval"


# ── an abandoned brief run is ended ─────────────────────────────────────────
@pytest.mark.parametrize("state", ["filed", "rejected", "failed", "drafting"])
def test_a_run_of_a_brief_that_is_not_waiting_and_has_not_changed_lately_is_ended(
    fresh_database: DatabaseHandle, state: str
) -> None:
    add_claim(fresh_database, CLAIM, "approved", age_seconds=OLD)
    run_id, thread = a_brief_run(fresh_database)
    add_brief(fresh_database, CLAIM, state, run_id, age_seconds=JUST_OUTSIDE)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_rows(fresh_database, thread) == 0


def test_a_drafting_briefs_run_older_than_the_lease_with_no_fresh_change_is_ended(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "awaiting_adjuster", age_seconds=OLD)
    run_id, _ = a_brief_run(fresh_database, "Running")
    add_brief(fresh_database, CLAIM, "drafting", run_id, age_seconds=OLD)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, run_id) == "Failed"


@pytest.mark.parametrize("status", ["Running", "AwaitingApproval"])
def test_a_brief_run_that_no_brief_names_is_ended(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = a_brief_run(fresh_database, status)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_rows(fresh_database, thread) == 0
    ((reason, service),) = owner_rows(
        fresh_database,
        "SELECT reason, service FROM audit.events "
        "WHERE run_id = %s AND event = 'run.failed'",
        (run_id,),
    )
    assert (reason, service) == ("abandoned", SERVICE_NAME)


def test_a_brief_run_idle_for_less_than_the_lease_is_left_alone(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = a_brief_run(fresh_database, "Running", idle=JUST_INSIDE)

    result = one_pass(fresh_database)

    assert result.runs_ended == 0
    assert status_of(fresh_database, run_id) == "Running"


def test_a_brief_changed_just_outside_the_lease_keeps_nothing_and_one_just_inside_does(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, "CLM-5502", "approved", age_seconds=OLD)
    add_claim(fresh_database, "CLM-5503", "approved", age_seconds=OLD)
    outside, _ = a_brief_run(fresh_database)
    inside, _ = a_brief_run(fresh_database)
    add_brief(fresh_database, "CLM-5502", "failed", outside, age_seconds=JUST_OUTSIDE)
    add_brief(fresh_database, "CLM-5503", "failed", inside, age_seconds=JUST_INSIDE)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, outside) == "Failed"
    assert status_of(fresh_database, inside) == "AwaitingApproval"


def test_a_brief_of_another_tenant_keeps_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "awaiting_adjuster", age_seconds=OLD)
    add_claim(
        fresh_database,
        "CLM-5504",
        "awaiting_adjuster",
        age_seconds=OLD,
        tenant=OTHER_TENANT,
    )
    run_id, _ = a_brief_run(fresh_database)
    add_brief(
        fresh_database,
        "CLM-5504",
        "awaiting_decision",
        run_id,
        age_seconds=OLD,
        tenant=OTHER_TENANT,
    )

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, run_id) == "Failed"


def test_a_brief_keeps_only_the_run_it_names(fresh_database: DatabaseHandle) -> None:
    add_claim(fresh_database, CLAIM, "awaiting_adjuster", age_seconds=OLD)
    named, _ = a_brief_run(fresh_database)
    stranger, _ = a_brief_run(fresh_database)
    add_brief(fresh_database, CLAIM, "awaiting_decision", named, age_seconds=OLD)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, named) == "AwaitingApproval"
    assert status_of(fresh_database, stranger) == "Failed"


# ── triage's runs are as they were ──────────────────────────────────────────
def test_a_triage_run_that_no_claim_keeps_is_still_ended_beside_a_kept_brief(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, CLAIM, "awaiting_adjuster", age_seconds=OLD)
    kept, _ = a_brief_run(fresh_database)
    add_brief(fresh_database, CLAIM, "awaiting_decision", kept, age_seconds=OLD)
    triage, _ = add_run(fresh_database, "AwaitingApproval", agent=AGENT)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, kept) == "AwaitingApproval"
    assert status_of(fresh_database, triage) == "Failed"


def test_a_triage_run_a_waiting_claim_keeps_is_still_kept(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = add_run(fresh_database, "AwaitingApproval", agent=AGENT)
    add_claim(
        fresh_database, "CLM-5505", "awaiting_adjuster", age_seconds=OLD, run_id=run_id
    )

    result = one_pass(fresh_database)

    assert result.runs_ended == 0
    assert status_of(fresh_database, run_id) == "AwaitingApproval"


def test_a_run_of_another_agent_is_not_the_sweeps_to_end(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = add_run(fresh_database, "Running", agent="some-other-agent")

    result = one_pass(fresh_database)

    assert result.runs_ended == 0
    assert status_of(fresh_database, run_id) == "Running"


# ── what the sweep reads and writes ─────────────────────────────────────────
def test_a_pass_writes_nothing_to_the_briefs_table(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, "CLM-5506", "approved", age_seconds=OLD)
    add_claim(fresh_database, "CLM-5507", "approved", age_seconds=OLD)
    ended, _ = a_brief_run(fresh_database)
    kept, _ = a_brief_run(fresh_database)
    add_brief(fresh_database, "CLM-5506", "drafting", ended, age_seconds=OLD)
    add_brief(fresh_database, "CLM-5507", "awaiting_decision", kept, age_seconds=OLD)
    before = brief_rows(fresh_database)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert brief_rows(fresh_database) == before


def test_a_run_a_brief_started_to_keep_after_it_was_listed_is_not_ended(
    fresh_database: DatabaseHandle,
) -> None:
    # The keep rule is read again under the run's lock, so a brief that pauses
    # between the listing and the end keeps its run.
    add_claim(fresh_database, CLAIM, "awaiting_adjuster", age_seconds=OLD)
    run_id, _ = a_brief_run(fresh_database)
    with connect(fresh_database.dsn("claims_sweep"), SERVICE_NAME) as conn:
        assert list_abandoned_runs(conn) == [run_id]
        add_brief(fresh_database, CLAIM, "awaiting_decision", run_id, age_seconds=OLD)

        ended = end_run_unless_kept(conn, run_id)
        conn.rollback()

    assert ended is False
    assert status_of(fresh_database, run_id) == "AwaitingApproval"


def test_a_brief_of_another_tenant_does_not_keep_the_run_when_it_is_asked_again(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(
        fresh_database,
        "CLM-5508",
        "awaiting_adjuster",
        age_seconds=OLD,
        tenant=OTHER_TENANT,
    )
    run_id, _ = a_brief_run(fresh_database)
    add_brief(
        fresh_database,
        "CLM-5508",
        "awaiting_decision",
        run_id,
        age_seconds=OLD,
        tenant=OTHER_TENANT,
    )

    with connect(fresh_database.dsn("claims_sweep"), SERVICE_NAME) as conn:
        ended = end_run_unless_kept(conn, run_id)
        conn.commit()

    assert ended is True
    assert status_of(fresh_database, run_id) == "Failed"
