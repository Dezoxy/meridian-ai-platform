"""S070: the adjuster's pages say what a recommendation rests on.

A recommendation is sometimes the rules' alone and sometimes rests on a model's
reading of the policy's exclusion clauses. ``recommendation_rests_on`` derives
which from two stored fields; the claim's page prints one fixed sentence beside
the recommendation and the queue one fixed marker in a column of its own.
Nothing here asserts on the loss date's labels (``test_claim_dates.py``).
"""

import re
from datetime import timedelta
from itertools import product
from typing import Any, get_args

import pytest
from dbsupport import DatabaseHandle
from pagesupport import CLAIM, Definitions, Table
from workloads.claims_triage.test_adjuster_pages import (
    CLAIMANT_NAME,
    ESCAPED_MARKUP,
    LONG_AGO,
    MARKUP,
    MARKUP_IN_ATTRIBUTE,
    QUEUE_URL,
    RICH_PROPOSAL,
    Page,
    client_for,
    put_claim,
    put_proposal,
    url_of,
)
from workloads.claims_triage.test_claims_app import DRAFTED_BY, OUTPUT

from meridian.workloads.claims_triage import proposal as proposal_module
from meridian.workloads.claims_triage.proposal import (
    ASSESSED,
    AssessmentStatus,
    TriageProposal,
)

RECOMMENDATIONS = ("approve", "reject", None)
STATUSES = ("not_needed", "none_applies", "applies", "unavailable")
# What each (recommendation, assessment) rests on: a recommendation the model
# answered for is the model's; one with no model asked is the rules'; and
# there is nothing to mark without a recommendation or when the assessment
# was unavailable.
EXPECTED = {
    (recommendation, status): (
        "neither"
        if recommendation is None or status == "unavailable"
        else "model"
        if status in ASSESSED
        else "rules"
    )
    for recommendation, status in product(RECOMMENDATIONS, STATUSES)
}


def combination_id(combination: tuple[str | None, str]) -> str:
    return f"{combination[0] or 'no-recommendation'}-{combination[1]}"


def proposal_with(recommendation: str | None, status: str) -> dict[str, Any]:
    """A stored proposal the validator accepts, for one combination. A rejection
    the model's reading found an exclusion for has the shape the graph writes:
    ``excluded``, with the clause."""
    called = status != "not_needed"
    reason, extra = {
        "approve": ("over_threshold", {"payable_amount": 5000}),
        "reject": ("policy_inactive", {}),
        None: ("unverified", {"gaps": ["exclusion_assessment"]}),
    }[recommendation]
    if recommendation == "reject" and status == "applies":
        reason, extra = "excluded", {"exclusion_clause": "3.2"}
    return (
        OUTPUT
        | {
            "reason": reason,
            "recommendation": recommendation,
            "assessment": status,
            "unavailable_because": "not-json" if status == "unavailable" else None,
            "rationale": "A fixed rationale." if status in ASSESSED else None,
            "drafted_by": DRAFTED_BY if called else None,
        }
        | extra
    )


MODEL_NOTE = proposal_module.RESTS_ON_NOTES["model"]
RULES_NOTE = proposal_module.RESTS_ON_NOTES["rules"]
MODEL_MARK = proposal_module.RESTS_ON_MARKS["model"]
RULES_MARK = proposal_module.RESTS_ON_MARKS["rules"]
MARK_HEADER = "Recommendation rests on"
NOTE_LABEL = "Recommendation rests on"


# ── the function ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("combination", EXPECTED, ids=combination_id)
def test_a_stored_proposal_is_marked_by_its_recommendation_and_its_assessment(
    combination: tuple[str | None, str],
) -> None:
    recommendation, status = combination
    stored = TriageProposal.model_validate(proposal_with(recommendation, status))

    rests_on = proposal_module.recommendation_rests_on(
        stored.recommendation, stored.assessment
    )

    assert rests_on == EXPECTED[combination]


def test_the_table_covers_every_status_the_proposal_allows() -> None:
    assert set(STATUSES) == set(get_args(AssessmentStatus))
    assert set(ASSESSED) <= set(STATUSES)


def test_each_sentence_and_marker_belongs_to_its_key() -> None:
    # The words are typed here, not read from the dictionaries: swapping the
    # two values of either one must fail this.
    notes = proposal_module.RESTS_ON_NOTES
    marks = proposal_module.RESTS_ON_MARKS

    assert "a model's reading" in notes["model"]
    assert "no model was asked" not in notes["model"]
    assert "no model was asked" in notes["rules"]
    assert "a model's reading" not in notes["rules"]
    assert "model reading" in marks["model"]
    assert "rules only" not in marks["model"]
    assert "rules only" in marks["rules"]
    assert "model reading" not in marks["rules"]


# What the graph can write with a recommendation: an assessment that is
# unavailable never carries one (the recommendation is withheld), and is marked
# "neither" even where a model was called, so it is not in the invariant.
WITH_A_RECOMMENDATION = [
    c for c in EXPECTED if c[0] is not None and c[1] != "unavailable"
]


@pytest.mark.parametrize("combination", WITH_A_RECOMMENDATION, ids=combination_id)
def test_a_recommendation_rests_on_the_model_exactly_when_a_model_was_called(
    combination: tuple[str | None, str],
) -> None:
    # Independent of ``EXPECTED``: it reads ``drafted_by`` of the validated
    # proposal, the field that says a model answered.
    stored = TriageProposal.model_validate(proposal_with(*combination))

    rests_on = proposal_module.recommendation_rests_on(
        stored.recommendation, stored.assessment
    )

    assert stored.recommendation is not None
    assert (rests_on == "model") == (stored.drafted_by is not None)
    assert rests_on in ("model", "rules")


def test_a_rejection_the_models_reading_excluded_has_the_graphs_shape() -> None:
    stored = TriageProposal.model_validate(proposal_with("reject", "applies"))

    assert (stored.reason, stored.exclusion_clause) == ("excluded", "3.2")
    assert (
        proposal_module.recommendation_rests_on(
            stored.recommendation, stored.assessment
        )
        == "model"
    )


def test_a_proposal_stored_before_s017_is_marked_without_error() -> None:
    old = {k: v for k, v in RICH_PROPOSAL["drafted_by"].items() if k != "prompt"}
    stored = TriageProposal.model_validate(RICH_PROPOSAL | {"drafted_by": old})

    rests_on = proposal_module.recommendation_rests_on(
        stored.recommendation, stored.assessment
    )

    assert stored.drafted_by is not None
    assert stored.drafted_by.prompt is None
    assert rests_on == "model"


@pytest.mark.parametrize(
    ("recommendation", "assessment", "expected"),
    [
        (None, None, "neither"),
        ("approve", None, "neither"),
        ("approve", "", "neither"),
        ("reject", "something-new", "neither"),
        ("approve", "NONE_APPLIES", "neither"),
        ("maybe", "applies", "neither"),
        ("", "none_applies", "neither"),
        # the boundary: the same words, spelled as the validator spells them
        ("approve", "none_applies", "model"),
        ("reject", "not_needed", "rules"),
    ],
    ids=lambda value: "none" if value is None else value or "empty",
)
def test_a_stored_value_the_function_does_not_know_is_not_marked_and_never_raises(
    recommendation: str | None, assessment: str | None, expected: str
) -> None:
    # The queue reads two ``->>`` texts of a row it cannot validate: a claim
    # with no proposal gives two NULLs, an older or newer row any word.
    rests_on = proposal_module.recommendation_rests_on(recommendation, assessment)

    assert rests_on == expected


def test_the_fixed_words_name_neither_the_claimant_nor_the_prompt() -> None:
    # The claim's page names no claimant and, for a proposal stored before
    # S017, no prompt: two existing tests assert both absences on the page.
    words = [
        *proposal_module.RESTS_ON_NOTES.values(),
        *proposal_module.RESTS_ON_MARKS.values(),
    ]

    assert set(proposal_module.RESTS_ON_NOTES) == {"model", "rules"}
    assert set(proposal_module.RESTS_ON_MARKS) == {"model", "rules"}
    for word in words:
        assert "claimant" not in word.lower(), word
        assert "prompt" not in word.lower(), word
        assert not re.search(r"[<>&\"]", word), word


# ── the claim's page ────────────────────────────────────────────────────────
def test_a_recommendation_that_rests_on_the_model_is_said_beside_it(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    shown = Definitions(response.text)
    labels = shown.labels()
    assert shown.value(NOTE_LABEL) == MODEL_NOTE
    assert labels[labels.index("Recommendation") + 1] == NOTE_LABEL
    assert RULES_NOTE not in Page(response.text).text


def test_a_recommendation_the_rules_alone_made_says_no_model_was_asked(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, proposal_with("reject", "not_needed"))

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    assert Definitions(response.text).value(NOTE_LABEL) == RULES_NOTE
    assert MODEL_NOTE not in Page(response.text).text


@pytest.mark.parametrize(
    "combination",
    [c for c, rests_on in EXPECTED.items() if rests_on == "neither"],
    ids=combination_id,
)
def test_a_proposal_with_nothing_to_mark_has_no_sentence(
    fresh_database: DatabaseHandle, combination: tuple[str | None, str]
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, proposal_with(*combination))

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    assert NOTE_LABEL not in Definitions(response.text).labels()
    assert MODEL_NOTE not in Page(response.text).text
    assert RULES_NOTE not in Page(response.text).text


def test_a_claim_with_no_proposal_has_no_sentence(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    assert NOTE_LABEL not in Definitions(response.text).labels()


def test_the_page_of_a_proposal_stored_before_s017_is_marked_and_names_no_prompt(
    fresh_database: DatabaseHandle,
) -> None:
    old = {k: v for k, v in RICH_PROPOSAL["drafted_by"].items() if k != "prompt"}
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL | {"drafted_by": old})

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    assert Definitions(response.text).value(NOTE_LABEL) == MODEL_NOTE
    assert "prompt" not in Page(response.text).text


def test_the_page_prints_no_stored_value_it_did_not_print_before(
    fresh_database: DatabaseHandle,
) -> None:
    # The sentence is a fixed word of the code: a stored key that no page
    # showed before, and the claimant's name, still do not appear.
    put_claim(
        fresh_database,
        CLAIM,
        rests_on="stored-canary-91",
        claimant={"name": CLAIMANT_NAME, "email": "a@example.com"},
    )
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    assert "stored-canary-91" not in response.text
    assert CLAIMANT_NAME not in response.text
    shown = Definitions(response.text)
    assert shown.value(NOTE_LABEL) in proposal_module.RESTS_ON_NOTES.values()


def test_markup_is_still_escaped_with_the_sentence_present(
    fresh_database: DatabaseHandle,
) -> None:
    hostile = MARKUP + MARKUP_IN_ATTRIBUTE
    put_claim(
        fresh_database,
        CLAIM,
        description=hostile,
        loss_location={"city": hostile, "country": "Austria"},
    )
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL | {"rationale": hostile})

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert response.status_code == 200
    html = response.text
    assert Definitions(html).value(NOTE_LABEL) == MODEL_NOTE
    assert MARKUP not in html
    assert "<img" not in html
    assert html.count(ESCAPED_MARKUP) == 3  # description, city and rationale
    assert not [t for t, _ in Page(html).tags if t in ("script", "img")]


# ── the queue ───────────────────────────────────────────────────────────────
def test_the_queue_marks_each_row_by_the_latest_proposal(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301")
    put_proposal(db, "CLM-9301", RICH_PROPOSAL)
    put_claim(db, "CLM-9302")
    put_proposal(db, "CLM-9302", proposal_with("reject", "not_needed"))
    put_claim(db, "CLM-9303")
    put_proposal(db, "CLM-9303", OUTPUT)
    put_claim(db, "CLM-9304", "triage_failed")
    put_claim(db, "CLM-9305")
    put_proposal(db, "CLM-9305", proposal_with("reject", "not_needed"))
    put_proposal(
        db, "CLM-9305", RICH_PROPOSAL, created_at=LONG_AGO + timedelta(hours=1)
    )

    response = client_for(db).get(QUEUE_URL)

    assert response.status_code == 200
    table = Table(response.text)
    assert MARK_HEADER in table.headers
    marks = {r["Claim"]: r[MARK_HEADER] for r in table.rows}
    assert marks == {
        "CLM-9301": MODEL_MARK,
        "CLM-9302": RULES_MARK,
        # nothing to mark: no recommendation, or no proposal
        "CLM-9303": "",
        "CLM-9304": "",
        # the latest proposal's, not the older one's
        "CLM-9305": MODEL_MARK,
    }


def test_a_queue_row_with_a_proposal_of_the_walking_skeleton_has_no_mark(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, None)

    response = client_for(fresh_database).get(QUEUE_URL)

    assert response.status_code == 200
    assert Table(response.text).row(CLAIM)[MARK_HEADER] == ""


def test_the_queue_prints_the_fixed_marks_only(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL | {"rationale": MARKUP})

    response = client_for(fresh_database).get(QUEUE_URL)

    assert response.status_code == 200
    assert MARKUP not in response.text
    assert "The roof was rotten" not in response.text
    assert Table(response.text).row(CLAIM)[MARK_HEADER] == MODEL_MARK


def test_the_queue_reads_the_two_fields_in_the_query_it_already_made() -> None:
    from meridian.workloads.claims_triage import adjuster_queue

    sql = adjuster_queue.QUEUE_SQL

    assert sql.count("proposal ->> 'assessment'") == 1
    assert sql.count("proposal ->> 'recommendation'") == 1
    # One lookup of the latest proposal per row, as before.
    assert sql.count("FROM claims.triage_proposals") == 1
