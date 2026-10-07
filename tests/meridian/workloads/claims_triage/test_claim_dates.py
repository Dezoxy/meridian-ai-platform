"""S070: the claim's page shows the loss date as a statement, with its gaps.

The loss date is the claimant's word; the report date is the one the API
received with the claim (the form stamps it, the JSON route keeps the caller's,
and nothing stored says which). The page labels both for what they are and
shows two numbers of days: from the loss to the report, and from the report to
when the API received the claim. Nothing is checked, stored or sent to the
model.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from pagesupport import CLAIM, Definitions
from servicesupport import owner_rows
from workloads.claims_triage.test_adjuster_pages import (
    ESCAPED_MARKUP,
    MARKUP,
    RICH_PROPOSAL,
    client_for,
    put_claim,
    put_proposal,
    url_of,
)

from meridian.workloads.claims_triage import adjuster, claim_dates, claimant

RECEIVED = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
GAP_LABELS = (
    claim_dates.LOSS_TO_REPORT_LABEL,
    claim_dates.REPORT_TO_RECEIVED_LABEL,
)


def rows_for(claim: object, received_at: datetime | None = RECEIVED) -> dict[str, str]:
    return claim_dates.day_gaps(claim, received_at)


# ── the arithmetic ──────────────────────────────────────────────────────────
def test_the_gaps_are_the_days_from_the_loss_to_the_report_and_to_receipt() -> None:
    claim = {"loss_date": "2026-07-01", "reported_on": "2026-07-13"}

    rows = rows_for(claim)

    assert rows == {
        claim_dates.LOSS_TO_REPORT_LABEL: "12 days",
        claim_dates.REPORT_TO_RECEIVED_LABEL: "2 days",
    }


@pytest.mark.parametrize(
    ("loss", "report", "first", "second"),
    [
        ("2026-07-15", "2026-07-15", "0 days", "0 days"),
        ("2026-07-14", "2026-07-14", "0 days", "1 day"),
        ("2026-07-14", "2026-07-15", "1 day", "0 days"),
        # a report dated after the API received it (the JSON route keeps the
        # caller's date): a negative number, not hidden
        ("2026-07-15", "2026-07-20", "5 days", "-5 days"),
        ("2026-07-15", "2026-07-16", "1 day", "-1 day"),
    ],
)
def test_a_gap_of_one_day_is_singular_and_none_is_zero(
    loss: str, report: str, first: str, second: str
) -> None:
    rows = rows_for({"loss_date": loss, "reported_on": report})

    assert list(rows.values()) == [first, second]


def test_the_day_of_receipt_is_the_day_in_the_insurers_time_zone() -> None:
    # 22:30 UTC on the 13th is 00:30 on the 14th in Vienna: a form sent then is
    # stamped the 14th, so its gap to receipt is 0, not -1.
    received = datetime(2026, 7, 13, 22, 30, tzinfo=UTC)
    claim = {"loss_date": "2026-07-13", "reported_on": "2026-07-14"}

    rows = rows_for(claim, received)

    assert rows[claim_dates.REPORT_TO_RECEIVED_LABEL] == "0 days"


def test_the_time_zone_is_the_one_the_claimants_form_stamps_in() -> None:
    assert claim_dates.REPORT_TIME_ZONE is claimant.REPORT_TIME_ZONE


def test_a_claim_with_no_received_moment_has_only_the_first_gap() -> None:
    claim = {"loss_date": "2026-07-01", "reported_on": "2026-07-13"}

    rows = rows_for(claim, None)

    assert list(rows) == [claim_dates.LOSS_TO_REPORT_LABEL]


@pytest.mark.parametrize(
    "claim",
    [
        None,
        "text",
        [],
        {},
        {"loss_date": None, "reported_on": None},
        {"loss_date": "2026-07-01"},
        {"reported_on": "2026-07-13"},
        {"loss_date": "garbage", "reported_on": "also garbage"},
        {"loss_date": "2026-13-45", "reported_on": "2026-02-30"},
        {"loss_date": 20260701, "reported_on": 20260713},
        {"loss_date": ["2026-07-01"], "reported_on": {"day": "2026-07-13"}},
        {"loss_date": "20260701", "reported_on": "2026-W28-1"},
        {"loss_date": "2026-07-01T10:00:00", "reported_on": "2026-07-13 10:00"},
        # Arabic-Indic digits: a date to no one's parser here
        {"loss_date": "٢٠٢٦-07-01", "reported_on": "2026-07-13"},
    ],
    ids=lambda claim: type(claim).__name__ + "-" + str(len(str(claim))),
)
def test_a_submission_with_no_usable_dates_gives_no_gap_and_never_raises(
    claim: object,
) -> None:
    rows = rows_for(claim)

    assert rows == {} or list(rows) == [claim_dates.REPORT_TO_RECEIVED_LABEL]


def test_a_missing_loss_date_leaves_out_only_the_gap_that_needs_it() -> None:
    rows = rows_for({"reported_on": "2026-07-13"})

    assert rows == {claim_dates.REPORT_TO_RECEIVED_LABEL: "2 days"}


def test_a_missing_report_date_leaves_out_both_gaps() -> None:
    rows = rows_for({"loss_date": "2026-07-01"})

    assert rows == {}


def test_the_labels_name_neither_the_claimant_nor_a_prompt() -> None:
    # The claim's page names no claimant (an existing test asserts it).
    labels = (
        claim_dates.LOSS_DATE_LABEL,
        claim_dates.REPORTED_ON_LABEL,
        *GAP_LABELS,
    )

    for label in labels:
        assert "claimant" not in label.lower(), label
        assert "prompt" not in label.lower(), label
    assert "stated" in claim_dates.LOSS_DATE_LABEL
    assert "as received" in claim_dates.REPORTED_ON_LABEL


def test_the_facts_hold_the_labelled_dates_and_the_gaps_in_order() -> None:
    claim = {"loss_date": "2026-07-01", "reported_on": "2026-07-13"}

    facts = adjuster.facts_of(claim, received_at=RECEIVED)

    labels = list(facts)
    assert facts[claim_dates.LOSS_DATE_LABEL] == "2026-07-01"
    assert facts[claim_dates.REPORTED_ON_LABEL] == "2026-07-13"
    assert labels[labels.index(claim_dates.REPORTED_ON_LABEL) + 1 :][:2] == list(
        GAP_LABELS
    )


def test_facts_without_a_received_moment_still_read_as_before() -> None:
    claim = {"loss_date": "2026-07-01", "reported_on": "2026-07-13"}

    facts = adjuster.facts_of(claim)

    assert claim_dates.REPORT_TO_RECEIVED_LABEL not in facts
    assert facts[claim_dates.LOSS_TO_REPORT_LABEL] == "12 days"


# ── the page ────────────────────────────────────────────────────────────────
def put_received(db: DatabaseHandle, claim_id: str, moment: datetime) -> None:
    owner_rows(
        db,
        "UPDATE claims.claims SET received_at = %s WHERE claim_id = %s RETURNING 1",
        (moment, claim_id),
    )


def shown_on_page(db: DatabaseHandle, **submission: Any) -> Definitions:
    put_claim(db, CLAIM, **submission)
    put_received(db, CLAIM, RECEIVED)
    put_proposal(db, CLAIM, RICH_PROPOSAL)
    response = client_for(db).get(url_of(CLAIM))
    assert response.status_code == 200
    return Definitions(response.text)


def test_the_page_labels_the_dates_and_shows_the_two_gaps(
    fresh_database: DatabaseHandle,
) -> None:
    shown = shown_on_page(
        fresh_database, loss_date="2026-07-01", reported_on="2026-07-13"
    )

    assert shown.value(claim_dates.LOSS_DATE_LABEL) == "2026-07-01"
    assert shown.value(claim_dates.REPORTED_ON_LABEL) == "2026-07-13"
    assert shown.value(claim_dates.LOSS_TO_REPORT_LABEL) == "12 days"
    assert shown.value(claim_dates.REPORT_TO_RECEIVED_LABEL) == "2 days"
    assert "Loss date" not in shown.labels()


def test_the_new_rows_hold_only_fixed_words_and_whole_numbers(
    fresh_database: DatabaseHandle,
) -> None:
    shown = shown_on_page(
        fresh_database, loss_date="2026-07-01", reported_on="2026-07-13"
    )

    for label in GAP_LABELS:
        assert shown.value(label).split(" ")[1] in ("day", "days")
        assert int(shown.value(label).split(" ")[0]) >= 0


@pytest.mark.parametrize(
    "dates",
    [
        {},
        {"loss_date": "garbage", "reported_on": "2026-07-13"},
        {"loss_date": "2026-07-01", "reported_on": 20260713},
        {"loss_date": None, "reported_on": None},
    ],
    ids=["missing", "loss-malformed", "report-a-number", "both-null"],
)
def test_a_stored_submission_with_unusable_dates_renders_without_the_gap(
    fresh_database: DatabaseHandle, dates: dict[str, Any]
) -> None:
    put_claim(fresh_database, CLAIM)
    owner_rows(
        fresh_database,
        "UPDATE claims.claims SET submission = "
        "(submission - 'loss_date' - 'reported_on') || %s::jsonb "
        "WHERE claim_id = %s RETURNING 1",
        (json.dumps(dates), CLAIM),
    )
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    assert claim_dates.LOSS_TO_REPORT_LABEL not in Definitions(response.text).labels()


def test_markup_in_the_dates_is_escaped_beside_the_gaps(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, loss_date=MARKUP, reported_on="2026-07-13")
    put_received(fresh_database, CLAIM, RECEIVED)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    assert MARKUP not in response.text
    assert ESCAPED_MARKUP in response.text
    shown = Definitions(response.text)
    assert claim_dates.LOSS_TO_REPORT_LABEL not in shown.labels()
    assert shown.value(claim_dates.REPORT_TO_RECEIVED_LABEL) == "2 days"
