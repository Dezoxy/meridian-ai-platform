"""S070: the queue marks a proposal only when the claim's page could read it.

The claim's page validates the stored proposal and, when that fails, says it
"could not be read". The queue uses the same judgment
(``adjuster_queue._proposal_of``, one function), so a stored proposal that fails
validation carries no mark in the queue and the short form of the page's words
instead. A page of the queue validates each shown row once, from the row it
already read: no second call to the database. The claim's page logs a proposal
it cannot read; the queue logs one line per page with the count, never one per
row.
"""

import logging
from datetime import timedelta
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from pagesupport import CLAIM, Definitions, Table
from workloads.claims_triage.test_adjuster_page_marks import (
    MARK_HEADER,
    MODEL_MARK,
    NOTE_LABEL,
    RULES_MARK,
    proposal_with,
)
from workloads.claims_triage.test_adjuster_pages import (
    LONG_AGO,
    QUEUE_URL,
    RICH_PROPOSAL,
    client_for,
    put_claim,
    put_proposal,
    url_of,
)

from meridian.workloads.claims_triage import adjuster, adjuster_queue
from meridian.workloads.claims_triage.adjuster_queue import QueueRow
from meridian.workloads.claims_triage.proposal import TriageProposal

REASON_HEADER = "Reason of the latest proposal"
UNREADABLE_MARK = adjuster_queue.UNREADABLE_PROPOSAL_MARK
# Each fails the validator (or the model's shape) and keeps the two columns the
# insert reads, ``route`` and ``reason``. The first two keep a recommendation
# and an assessment that, read without validating, would be marked "model".
UNREADABLE = {
    "an-exclusion-that-recommends-approving": RICH_PROPOSAL
    | {"recommendation": "approve"},
    "a-key-the-model-does-not-have": RICH_PROPOSAL | {"canary_key": "x"},
    "a-missing-key": {k: v for k, v in RICH_PROPOSAL.items() if k != "gaps"},
}


def queue_row(claim_id: str, proposal: object) -> QueueRow:
    return QueueRow(
        claim_id,
        "awaiting_adjuster",
        LONG_AGO,
        "fire",
        10_000,
        "excluded",
        None,
        proposal,
    )


def marks_of(html: str) -> dict[str, str]:
    return {r["Claim"]: r[MARK_HEADER] for r in Table(html).rows}


# ── one stored row that fails validation, in both pages ─────────────────────
@pytest.mark.parametrize("stored", UNREADABLE.values(), ids=UNREADABLE)
def test_a_stored_proposal_that_cannot_be_read_has_no_mark_in_the_queue(
    fresh_database: DatabaseHandle, stored: dict[str, Any]
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, stored)
    client = client_for(fresh_database)

    queue = client.get(QUEUE_URL)
    page = client.get(url_of(CLAIM))

    assert queue.status_code == page.status_code == 200
    cell = Table(queue.text).row(CLAIM)[MARK_HEADER]
    assert cell == UNREADABLE_MARK
    assert cell not in (MODEL_MARK, RULES_MARK)
    # The page says it in the long form and has no sentence of what it rests on.
    assert adjuster_queue.UNREADABLE_PROPOSAL_TEXT in page.text
    assert NOTE_LABEL not in Definitions(page.text).labels()


def test_the_cell_for_an_unreadable_proposal_is_the_pages_words_short() -> None:
    long_form = adjuster_queue.UNREADABLE_PROPOSAL_TEXT

    short_form = adjuster_queue.UNREADABLE_PROPOSAL_MARK

    assert short_form == "could not be read"
    assert long_form == f"the stored proposal {short_form}"


def test_the_queue_and_the_page_agree_on_which_stored_proposals_can_be_read(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    stored = {f"CLM-93{i:02d}": p for i, p in enumerate(UNREADABLE.values(), 10)}
    stored |= {
        "CLM-9320": RICH_PROPOSAL,
        "CLM-9321": proposal_with("reject", "not_needed"),
    }
    for claim_id, proposal in stored.items():
        put_claim(db, claim_id)
        put_proposal(db, claim_id, proposal)
    client = client_for(db)

    marks = marks_of(client.get(QUEUE_URL).text)

    for claim_id in stored:
        page = client.get(url_of(claim_id)).text
        assert (marks[claim_id] == UNREADABLE_MARK) == (
            adjuster_queue.UNREADABLE_PROPOSAL_TEXT in page
        )
    assert marks["CLM-9320"] == MODEL_MARK
    assert marks["CLM-9321"] == RULES_MARK


# ── what a page of the queue costs ──────────────────────────────────────────
@pytest.fixture
def validations(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The values ``TriageProposal`` validates while a test renders the queue,
    which may not open a connection."""

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("the rendering of the queue must not use a connection")

    for module in (adjuster, adjuster_queue):
        monkeypatch.setattr(module, "connect", refuse)
    validated: list[object] = []
    validate = TriageProposal.model_validate

    def counting(value: object) -> TriageProposal:
        validated.append(value)
        return validate(value)

    monkeypatch.setattr(TriageProposal, "model_validate", counting)
    return validated


def test_a_page_validates_each_row_with_a_proposal_once_and_asks_no_database(
    validations: list[object],
) -> None:
    rows = [
        queue_row("CLM-9401", RICH_PROPOSAL),
        queue_row("CLM-9402", None),
        queue_row("CLM-9403", UNREADABLE["a-missing-key"]),
        queue_row("CLM-9404", proposal_with("reject", "not_needed")),
    ]

    html = adjuster.render_queue(rows)

    # A row with no stored proposal costs nothing.
    assert len(validations) == 3
    assert marks_of(html) == {
        "CLM-9401": MODEL_MARK,
        "CLM-9402": "",
        "CLM-9403": UNREADABLE_MARK,
        "CLM-9404": RULES_MARK,
    }


def test_a_full_page_of_the_queue_validates_one_proposal_per_row(
    validations: list[object],
) -> None:
    rows = [
        queue_row(f"CLM-{i:05d}", RICH_PROPOSAL)
        for i in range(adjuster_queue.QUEUE_LIMIT)
    ]

    html = adjuster.render_queue(rows)

    assert len(Table(html).rows) == adjuster_queue.QUEUE_LIMIT
    assert len(validations) == adjuster_queue.QUEUE_LIMIT
    assert set(marks_of(html).values()) == {MODEL_MARK}


@pytest.mark.parametrize("stored", ["a string", 5, [], [RICH_PROPOSAL]])
def test_a_stored_proposal_that_is_not_an_object_has_no_mark_and_never_raises(
    stored: object,
) -> None:
    html = adjuster.render_queue([queue_row("CLM-9401", stored)])

    assert marks_of(html) == {"CLM-9401": UNREADABLE_MARK}


# ── what a page of the queue logs ───────────────────────────────────────────
def warnings_of(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_page_with_three_unreadable_proposals_logs_one_record_with_the_count_3(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [queue_row(f"CLM-940{i}", UNREADABLE["a-missing-key"]) for i in range(3)]
    rows += [queue_row("CLM-9409", RICH_PROPOSAL), queue_row("CLM-9410", None)]

    with caplog.at_level(logging.DEBUG):
        adjuster.render_queue(rows)

    (record,) = warnings_of(caplog)
    assert record.name == adjuster.__name__
    assert record.getMessage() == (
        "rows of the queue page whose stored proposal could not be read: 3"
    )
    assert record.args == (3,)
    # No claim, no field and no stored value in the line.
    assert "CLM-" not in record.getMessage()
    assert [r for r in caplog.records if r.levelno < logging.WARNING] == []


def test_a_page_with_one_unreadable_proposal_logs_a_line_that_reads_right(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [queue_row("CLM-9401", UNREADABLE["a-missing-key"])]

    with caplog.at_level(logging.DEBUG):
        adjuster.render_queue(rows)

    (record,) = warnings_of(caplog)
    assert record.getMessage() == (
        "rows of the queue page whose stored proposal could not be read: 1"
    )
    assert record.args == (1,)


def test_a_page_with_no_unreadable_proposal_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [queue_row("CLM-9401", RICH_PROPOSAL), queue_row("CLM-9402", None)]

    with caplog.at_level(logging.DEBUG):
        adjuster.render_queue(rows)

    assert caplog.records == []


def test_a_full_page_of_unreadable_proposals_still_logs_one_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [
        queue_row(f"CLM-{i:05d}", UNREADABLE["a-key-the-model-does-not-have"])
        for i in range(adjuster_queue.QUEUE_LIMIT)
    ]

    with caplog.at_level(logging.DEBUG):
        adjuster.render_queue(rows)

    (record,) = caplog.records
    assert record.args == (adjuster_queue.QUEUE_LIMIT,)


def test_the_claims_own_page_still_logs_the_proposal_it_cannot_read(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, UNREADABLE["a-key-the-model-does-not-have"])

    with caplog.at_level(logging.DEBUG):
        response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    (record,) = warnings_of(caplog)
    assert record.getMessage() == (
        f"the stored proposal of claim {CLAIM} is not valid: "
        "ValidationError (('*', 'extra_forbidden'),)"
    )
    assert "canary_key" not in record.getMessage()


def test_a_page_of_the_queue_logs_one_line_however_often_it_is_loaded(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_database
    for i, stored in enumerate(UNREADABLE.values()):
        put_claim(db, f"CLM-930{i}")
        put_proposal(db, f"CLM-930{i}", stored)
    client = client_for(db)

    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            assert client.get(QUEUE_URL).status_code == 200

    records = warnings_of(caplog)
    assert [r.args for r in records] == [(3,), (3,)]


# ── pinned, not changed ─────────────────────────────────────────────────────
def test_a_failed_last_triage_shows_the_latest_stored_proposal_in_both_columns(
    fresh_database: DatabaseHandle,
) -> None:
    # The failed triage stored nothing, so the latest STORED proposal is the one
    # of an earlier triage. The queue describes that one, in the Reason column
    # (the proposal's ``reason`` column) and in the mark, so the two agree. This
    # is deliberate, not an accident: the row's state says the last triage
    # failed, and the claim's page shows the same stored proposal.
    db = fresh_database
    put_claim(db, CLAIM, "triage_failed", triages=2)
    put_proposal(db, CLAIM, RICH_PROPOSAL, created_at=LONG_AGO)
    client = client_for(db)

    row = Table(client.get(QUEUE_URL).text).row(CLAIM)
    page = Definitions(client.get(url_of(CLAIM)).text)

    assert row["State"] == "triage_failed"
    assert row[REASON_HEADER] == "excluded"
    assert row[MARK_HEADER] == MODEL_MARK
    assert NOTE_LABEL in page.labels()


@pytest.mark.parametrize(
    ("recommendation", "status"), [("reject", "none_applies"), ("approve", "applies")]
)
def test_the_pairs_the_graph_never_writes_but_the_validator_admits_are_marked_model(
    fresh_database: DatabaseHandle, recommendation: str, status: str
) -> None:
    # Only a hand-written row holds these: the graph writes a rejection with an
    # exclusion that applies, and an approval with none that applies. They are
    # marked "model" because a model answered and a drafter is stored; that is
    # the safe side: the mark reads as "look at this" to an adjuster, and
    # leaving it off would say the rules alone decided.
    stored = proposal_with(recommendation, status)
    assert TriageProposal.model_validate(stored).drafted_by is not None
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, stored)

    response = client_for(fresh_database).get(QUEUE_URL)

    assert Table(response.text).row(CLAIM)[MARK_HEADER] == MODEL_MARK


def test_the_latest_proposal_is_the_one_whose_readability_the_queue_judges(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM)
    put_proposal(db, CLAIM, UNREADABLE["a-key-the-model-does-not-have"])
    put_proposal(db, CLAIM, RICH_PROPOSAL, created_at=LONG_AGO + timedelta(hours=1))

    response = client_for(db).get(QUEUE_URL)

    assert Table(response.text).row(CLAIM)[MARK_HEADER] == MODEL_MARK
