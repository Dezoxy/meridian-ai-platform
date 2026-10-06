"""What of a brief is in a claim's trail, and what is not (S037, X2: the database
review's M2 and the security review's note on threat f).

The adjuster's page reads the claim's trail (``audit.claim_trail``) and asks it
how the TRIAGE run ended (``ENDED_SQL``). The view's first branch takes every
event of ``claims_api`` or ``claims_sweep`` that names the claim as its
reference, and it has no agent or run column. So:

- the decision's event (``brief.decided``, written by the Claims API about the
  claim) STAYS in the trail on purpose: a person decided something about this
  claim, and the page shows it as plain text;
- the sweep's ``run.failed`` for an abandoned run of the brief's agent is written
  with NO claim as its reference (the run's own ID is on the row), so it is in no
  claim's trail and cannot be taken for the triage run's end; a swept triage run
  still shows.
"""

import uuid

import pytest
from briefsupport import (
    CLAIM_ID,
    TENANT,
    Runtime,
    add_brief,
    add_claim,
    completed,
    make_client,
)
from dbsupport import DatabaseHandle
from servicesupport import owner_rows
from sweepsupport import MINUTE, one_pass, run_events
from workloads.claims_triage.test_adjuster_pages import (
    ABANDONED_REASON,
    CLEAN_UP_LINE,
    MARKUP,
    RESEND_TEXT,
    Page,
    client_for,
    put_decision,
    url_of,
)

from meridian.platform.common.db import connect
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage.lifecycle import AGENT, BRIEF_AGENT

OLD = 2 * RUNNING_LEASE_SECONDS


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds the tenant's claim. Unlike
    ``briefsupport.world`` it does not hold the claim's row still: these tests
    decide the claim's triage, which moves it."""
    add_claim(fresh_database, CLAIM_ID)
    return fresh_database


def add_run_of(
    db: DatabaseHandle, agent: str, status: str = "AwaitingApproval"
) -> uuid.UUID:
    """A run of ``agent`` for the claim, in the app's tenant, idle past the lease."""
    run_id = uuid.uuid4()
    owner_rows(
        db,
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status, updated_at) VALUES (%s, %s, %s, %s, %s, %s, "
        "now() - make_interval(secs => %s)) RETURNING 1",
        (run_id, uuid.uuid4(), agent, TENANT, CLAIM_ID, status, float(OLD)),
    )
    return run_id


def decided_triage(db: DatabaseHandle, run_id: uuid.UUID) -> None:
    """The claim is approved by the adjuster's decision on the triage run."""
    owner_rows(
        db,
        "UPDATE claims.claims SET state = 'approved', run_id = %s, "
        "state_changed_at = now() - make_interval(secs => %s) "
        "WHERE claim_id = %s RETURNING 1",
        (run_id, float(OLD), CLAIM_ID),
    )
    put_decision(db, CLAIM_ID, "approve", run_id)


def trail(db: DatabaseHandle) -> list[tuple]:
    """The claim's trail as the Claims API reads it: event, reason, role."""
    return owner_rows(
        db,
        "SELECT event, reason, db_role FROM audit.claim_trail "
        "WHERE claim_id = %s AND tenant = %s ORDER BY recorded_at, seq",
        (CLAIM_ID, TENANT),
    )


# ── the sweep's event for a brief run ───────────────────────────────────────
def test_the_sweeps_event_for_an_abandoned_brief_run_names_no_claim_as_reference(
    world: DatabaseHandle,
) -> None:
    run_id = add_run_of(world, BRIEF_AGENT)

    result = one_pass(world)

    assert result.runs_ended == 1
    ((service, event, reason, agent, reference, role),) = owner_rows(
        world,
        "SELECT service, event, reason, agent, reference, db_role FROM audit.events "
        "WHERE run_id = %s",
        (run_id,),
    )
    assert (service, event, reason, agent) == (
        "claims-sweep",
        "run.failed",
        ABANDONED_REASON,
        BRIEF_AGENT,
    )
    assert (reference, role) == (None, "claims_sweep")


def test_a_swept_triage_run_keeps_the_claim_as_the_reference_of_its_event(
    world: DatabaseHandle,
) -> None:
    run_id = add_run_of(world, AGENT)

    one_pass(world)

    ((*_, reference, _),) = run_events(world, run_id)
    assert reference == CLAIM_ID


def test_a_swept_brief_run_is_in_no_claims_trail(world: DatabaseHandle) -> None:
    add_run_of(world, BRIEF_AGENT)

    one_pass(world)

    assert trail(world) == []


def test_a_swept_triage_run_is_in_the_claims_trail(world: DatabaseHandle) -> None:
    add_run_of(world, AGENT)

    one_pass(world)

    assert trail(world) == [("run.failed", ABANDONED_REASON, "claims_sweep")]


# ── the adjuster's page reads the triage run's end, and only that ───────────
def test_a_swept_brief_run_is_not_taken_for_the_triage_runs_end(
    world: DatabaseHandle,
) -> None:
    decided_triage(world, add_run_of(world, AGENT, "Running"))
    # The triage run is fresh enough to be kept; the brief run is not.
    owner_rows(
        world,
        "UPDATE runtime.runs SET updated_at = now() - make_interval(secs => %s) "
        "WHERE agent = %s RETURNING 1",
        (float(RUNNING_LEASE_SECONDS - MINUTE), AGENT),
    )
    add_run_of(world, BRIEF_AGENT)

    result = one_pass(world)
    page = Page(client_for(world).get(url_of(CLAIM_ID)).text)

    assert result.runs_ended == 1
    assert CLEAN_UP_LINE not in page.text
    assert RESEND_TEXT in page.text


def test_a_swept_triage_run_is_still_explained_on_the_page(
    world: DatabaseHandle,
) -> None:
    decided_triage(world, add_run_of(world, AGENT))

    one_pass(world)
    page = Page(client_for(world).get(url_of(CLAIM_ID)).text)

    assert CLEAN_UP_LINE in page.text
    assert RESEND_TEXT not in page.text


# ── the decision's event stays in the trail ─────────────────────────────────
def test_the_decision_of_a_brief_is_in_the_claims_trail_and_on_the_page_as_text(
    world: DatabaseHandle,
) -> None:
    run_id = add_brief(world, "awaiting_decision", age_seconds=60)
    assert run_id is not None
    client = make_client(world.dsn("claims_api"), Runtime(completed(run_id, True)))

    posted = client.post(
        f"/claims/{CLAIM_ID}/brief/decision",
        json={"decision": "approve", "run": str(run_id)},
    )
    page = client_for(world).get(url_of(CLAIM_ID))

    assert posted.status_code == 200
    assert trail(world) == [("brief.decided", "adjuster-decision", "claims_api")]
    (row,) = [r for r in Page(page.text).rows if "brief.decided" in r]
    assert "approve" in row
    assert "adjuster-decision" in row
    assert "/brief" not in "".join(Page(page.text).links())


@pytest.mark.parametrize("column", ["event", "outcome", "reason"])
def test_a_brief_event_with_markup_in_it_is_shown_as_text_and_never_as_a_tag(
    world: DatabaseHandle, column: str
) -> None:
    values = {"event": "brief.decided", "outcome": "approve", "reason": "x"}
    values[column] = MARKUP if column != "event" else "brief.decided" + MARKUP
    with connect(world.dsn("claims_api"), "claims-api") as conn:
        conn.execute(
            "INSERT INTO audit.events (service, event, outcome, tenant, reference, "
            "reason) VALUES ('claims-api', %s, %s, %s, %s, %s)",
            (values["event"], values["outcome"], TENANT, CLAIM_ID, values["reason"]),
        )
        conn.commit()

    page = client_for(world).get(url_of(CLAIM_ID))

    assert page.status_code == 200
    assert MARKUP not in page.text
    assert "&lt;script&gt;" in page.text
    assert not [a for a in Page(page.text).attributes("script")]
