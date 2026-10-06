"""The scorer: three label sets and what is counted against them. The label
counts are the ones the platform's own retrieval check states (S012)."""

import pytest
from comparison import score
from knowledge_mcp.test_retrieval import LABELLED
from retrievalsupport import NARRATIVE, narrative_queries


@pytest.fixture(scope="module")
def labels() -> score.Labels:
    return score.load_labels()


def _count(by_claim: dict[str, tuple[str, ...]]) -> int:
    return sum(len(clauses) for clauses in by_claim.values())


def test_the_narrative_labels_are_the_28_clauses_of_the_retrieval_check(
    labels: score.Labels,
) -> None:
    narrative = labels.by_set["narrative"]

    assert _count(narrative) == LABELLED[NARRATIVE] == 28
    assert (
        sum(c.startswith("2.") for cs in narrative.values() for c in cs)
        == LABELLED[f"{NARRATIVE} section 2"]
        == 20
    )
    assert (
        sum(c.startswith("3.") for cs in narrative.values() for c in cs)
        == LABELLED[f"{NARRATIVE} section 3"]
        == 8
    )


def test_the_narrative_labels_are_what_the_retrieval_check_labels_by_claim(
    labels: score.Labels,
) -> None:
    queries = narrative_queries()

    assert [labels.by_set["narrative"][c] for c in labels.by_set["narrative"]] == [
        q.relevant for q in queries
    ]
    assert len(labels.by_set["narrative"]) == len(queries)


def test_the_exclusion_labels_are_the_eight_section_three_clauses(
    labels: score.Labels,
) -> None:
    exclusion = labels.by_set["exclusion"]
    narrative = labels.by_set["narrative"]

    assert _count(exclusion) == 8
    assert all(c.startswith("3.") for cs in exclusion.values() for c in cs)
    assert {c: cs for c, cs in exclusion.items()} == {
        c: tuple(x for x in cs if x.startswith("3."))
        for c, cs in narrative.items()
        if any(x.startswith("3.") for x in cs)
    }


def test_all_labels_are_every_citation_of_every_claim(labels: score.Labels) -> None:
    everything = labels.by_set["all"]

    assert _count(everything) == 63
    assert len(everything) == 40
    assert all(
        set(labels.by_set["narrative"][c]) <= set(everything[c])
        for c in (labels.by_set["narrative"])
    )


def test_recall_counts_the_labelled_clauses_among_the_first_k_places() -> None:
    ranking = ["2.1", "9.9", "3.2", "4.1", "5.5"]
    labelled = ("2.1", "3.2", "6.1")

    found = [score.hits_at(ranking, labelled, k) for k in (1, 3, 5, 10)]

    assert found == [1, 2, 2, 2]


def test_recall_over_claims_sums_hits_and_labelled_clauses() -> None:
    rankings = {"CLM-1": ["a", "b"], "CLM-2": ["c"]}
    labelled = {"CLM-1": ("a", "z"), "CLM-2": ("c", "b", "q")}

    hits, total = score.recall(rankings, labelled, 5)

    assert (hits, total) == (2, 5)
    assert score.share(hits, total) == pytest.approx(0.4)


def test_a_claim_with_no_list_finds_nothing_and_is_still_counted() -> None:
    hits, total = score.recall({}, {"CLM-1": ("a", "b")}, 5)

    assert (hits, total) == (0, 2)


def test_a_short_list_is_never_padded() -> None:
    assert score.hits_at(["a"], ("a", "b"), 10) == 1
    assert score.hits_at([], ("a",), 10) == 0


def test_a_query_is_won_tied_or_lost_on_the_number_of_labelled_clauses_found() -> None:
    labelled = {
        "WIN": ("a", "b"),
        "TIE": ("a", "b"),
        "LOSS": ("a", "b"),
        "BOTH-NONE": ("a",),
    }
    first = {"WIN": ["a", "b"], "TIE": ["a"], "LOSS": [], "BOTH-NONE": ["z"]}
    second = {"WIN": ["a"], "TIE": ["b"], "LOSS": ["b"], "BOTH-NONE": ["y"]}

    tally = score.tally(first, second, labelled, 5)

    assert tally == {"win": 1, "tie": 2, "loss": 1}


def test_the_cut_off_decides_a_win_at_one_rank_and_a_tie_at_another() -> None:
    labelled = {"CLM-1": ("a", "b")}
    first = {"CLM-1": ["a", "x", "b"]}
    second = {"CLM-1": ["x", "a", "b"]}

    assert score.tally(first, second, labelled, 1) == {"win": 1, "tie": 0, "loss": 0}
    assert score.tally(first, second, labelled, 3) == {"win": 0, "tie": 1, "loss": 0}


def test_chance_is_the_hits_a_random_ranking_of_the_scope_gives() -> None:
    labelled = {"CLM-1": ("a", "b"), "CLM-2": ("c",)}
    scope = {"CLM-1": 20, "CLM-2": 4}

    at_5 = score.chance_hits(labelled, scope, 5)
    at_10 = score.chance_hits(labelled, scope, 10)

    assert at_5 == pytest.approx(2 * 5 / 20 + 1 * 4 / 4)
    assert at_10 == pytest.approx(2 * 10 / 20 + 1 * 4 / 4)
