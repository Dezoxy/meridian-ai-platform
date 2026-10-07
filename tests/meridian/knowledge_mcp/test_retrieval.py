"""The retrieval check (S012): the real wordings, ingested through the real
gateway app in replay mode, searched with labelled queries, and what the search
finds with word overlap only. The floors are plumbing regression floors, not a
measurement of retrieval quality (see FLOORS)."""

import json
from collections import Counter
from dataclasses import dataclass

import pytest
from dbsupport import OWNER, DatabaseHandle
from knowledgesupport import Gateway, ingest
from retrievalsupport import (
    CONCEPT,
    CONCEPT_QUESTIONS,
    CUTOFFS,
    DOCUMENTS,
    FUSED,
    GROUPS,
    KEYWORD,
    NARRATIVE,
    RANKINGS,
    VECTOR,
    LabelledQuery,
    Measured,
    concept_queries,
    corpus_versions,
    documents_queries,
    embed_queries,
    measure,
    narrative_queries,
    render_table,
    require_labels_in_corpus,
)
from servicesupport import REPO_ROOT, owner_rows

from meridian.platform.common.db import connect

# Labelled clauses per group: the generator's citations of the claims, in
# sections 2 and 3 for the narrative queries (34 clauses: 28 of the first forty
# claims and six of the claims on a fraud indicator's boundary, each a cover
# clause), in section 5 except 5.1 for the documents queries (6), and five
# questions for each of four products (20). The floors below were measured
# before those six queries existed and still hold.
LABELLED = {
    NARRATIVE: 34,
    f"{NARRATIVE} section 2": 26,
    f"{NARRATIVE} section 3": 8,
    DOCUMENTS: 6,
    CONCEPT: 20,
}

# FLOORS: plumbing regression floors on a simulated embedding (a hashed bag of
# words), PostgreSQL 17.11 with pgvector 0.8.6, measured 2026-10-02; not a
# measurement of retrieval quality. Each tuple is the hits (not a share) at the
# cutoffs 1, 3, 5 and 10; a test fails when the search finds fewer. Re-measure
# when the image, the sort keys of the search or a query changes; a floor is
# lowered only with a reason, and raised never to flatter a number.
FLOORS = {
    (NARRATIVE, KEYWORD): (14, 19, 21, 22),
    (NARRATIVE, VECTOR): (3, 9, 13, 19),
    (NARRATIVE, FUSED): (11, 19, 20, 23),
    (f"{NARRATIVE} section 2", KEYWORD): (11, 15, 16, 17),
    (f"{NARRATIVE} section 2", VECTOR): (3, 9, 12, 18),
    (f"{NARRATIVE} section 2", FUSED): (10, 17, 17, 18),
    (f"{NARRATIVE} section 3", KEYWORD): (3, 4, 5, 5),
    (f"{NARRATIVE} section 3", VECTOR): (0, 0, 1, 1),
    (f"{NARRATIVE} section 3", FUSED): (1, 2, 3, 5),
    (DOCUMENTS, KEYWORD): (6, 6, 6, 6),
    (DOCUMENTS, VECTOR): (6, 6, 6, 6),
    (DOCUMENTS, FUSED): (6, 6, 6, 6),
    (CONCEPT, KEYWORD): (15, 17, 19, 20),
    (CONCEPT, VECTOR): (5, 5, 9, 15),
    (CONCEPT, FUSED): (7, 10, 13, 19),
}


@dataclass(frozen=True, slots=True)
class Run:
    queries: list[LabelledQuery]
    measured: Measured
    database: DatabaseHandle


@pytest.fixture
def run(fresh_database: DatabaseHandle, gateway: Gateway) -> Run:
    ingest(fresh_database, gateway.http, gateway.registry)
    with connect(fresh_database.dsn(OWNER), "test-retrieval") as conn:
        queries = [
            *narrative_queries(),
            *documents_queries(),
            *concept_queries(corpus_versions(conn)),
        ]
        # Fails loudly when a labelled clause is not in the corpus.
        require_labels_in_corpus(conn, queries)
        embeddings = embed_queries(gateway, [q.text for q in queries])
        measured = measure(conn, queries, embeddings)
    return Run(queries, measured, fresh_database)


def golden_pairs() -> list[tuple[str, str]]:
    """``(claim ID, clause)`` for every citation of the generator, read here
    without the helpers under test."""
    outcomes = json.loads(
        (REPO_ROOT / "data/synthetic/expected-outcomes.json").read_text("utf-8")
    )
    return [(o["claim_id"], c["clause"]) for o in outcomes for c in o["citations"]]


def test_the_labels_are_the_generators_citations(run: Run) -> None:
    pairs = golden_pairs()
    narrative = [p for p in pairs if p[1].split(".")[0] in ("2", "3")]
    documents = [p for p in pairs if p[1].startswith("5.") and p[1] != "5.1"]

    assert run.measured.labelled == LABELLED
    assert LABELLED[NARRATIVE] == len(narrative)
    assert LABELLED[f"{NARRATIVE} section 2"] == sum(
        1 for _, clause in narrative if clause.startswith("2.")
    )
    assert LABELLED[f"{NARRATIVE} section 3"] == sum(
        1 for _, clause in narrative if clause.startswith("3.")
    )
    assert LABELLED[DOCUMENTS] == len(documents)
    by_family = Counter(q.family for q in run.queries)
    assert by_family == {
        NARRATIVE: len({claim for claim, _ in narrative}),
        DOCUMENTS: len({claim for claim, _ in documents}),
        CONCEPT: 20,
    }
    assert {q.product for q in run.queries if q.family == CONCEPT} == {
        "HOME-PLUS",
        "HOME-STD",
        "MOTOR-COMP",
        "MOTOR-TPL",
    }
    assert [q.text for q in run.queries if q.family == CONCEPT][:5] == [
        question for question, _ in CONCEPT_QUESTIONS
    ]


def test_a_documents_query_is_the_question_for_the_claims_peril(run: Run) -> None:
    claims = json.loads((REPO_ROOT / "data/synthetic/claims.json").read_text("utf-8"))
    perils = {c["peril"] for c in claims}
    queries = [q for q in run.queries if q.family == DOCUMENTS]

    assert len(queries) == LABELLED[DOCUMENTS]
    assert {q.text for q in queries} <= {
        f"documents needed for a {peril.replace('_', ' ')} claim" for peril in perils
    }
    assert all("_" not in q.text for q in queries)
    assert all(
        clause.startswith("5.") and clause != "5.1"
        for q in queries
        for clause in q.relevant
    )


def test_a_narrative_query_is_a_description_with_no_documents_clause_as_a_label(
    run: Run,
) -> None:
    descriptions = {
        c["description"]
        for c in json.loads(
            (REPO_ROOT / "data/synthetic/claims.json").read_text("utf-8")
        )
    }
    queries = [q for q in run.queries if q.family == NARRATIVE]

    assert {q.text for q in queries} <= descriptions
    assert all(
        clause.split(".")[0] in ("2", "3") for q in queries for clause in q.relevant
    )


def test_chance_is_the_recall_of_a_random_ranking_of_each_scope(run: Run) -> None:
    sizes = {
        (product, version): count
        for product, version, count in owner_rows(
            run.database,
            "SELECT product, wording_version, count(*) FROM knowledge.chunks "
            "GROUP BY product, wording_version",
        )
    }
    expected: dict[str, list[float]] = {group: [0.0] * len(CUTOFFS) for group in GROUPS}
    for query in run.queries:
        size = sizes[(query.product, query.wording_version)]
        for clause in query.relevant:
            groups = (
                [NARRATIVE, f"{NARRATIVE} section {clause.split('.')[0]}"]
                if query.family == NARRATIVE
                else [query.family]
            )
            for group in groups:
                for index, cutoff in enumerate(CUTOFFS):
                    expected[group][index] += min(cutoff, size) / size

    for group in GROUPS:
        assert run.measured.chance[group] == pytest.approx(expected[group]), group
        shares = [h / LABELLED[group] for h in run.measured.chance[group]]
        assert shares == sorted(shares), group  # more results, more chance
        assert 0 < shares[0] < shares[-1] < 1, group


def test_the_table_has_a_chance_row_for_each_group_and_says_what_concept_is(
    run: Run,
) -> None:
    table = render_table(run.measured)

    for group in GROUPS:
        assert any(
            line.startswith(group.ljust(20) + "chance") for line in table.splitlines()
        ), group
    assert "checks the plumbing, not meaning" in table
    assert "SIMULATED" in table


def test_a_group_with_no_labelled_clause_is_shown_without_dividing_by_zero() -> None:
    empty = Measured(labelled={}, hits={}, chance={})

    table = render_table(empty)

    for group in GROUPS:
        assert group in table
    assert "-" in table
    assert "nan" not in table.lower()


def test_the_floors_cover_every_group_and_ranking() -> None:
    assert set(FLOORS) == {(group, ranking) for group in GROUPS for ranking in RANKINGS}
    assert all(len(floor) == len(CUTOFFS) for floor in FLOORS.values())
    assert set(LABELLED) == set(GROUPS)


def test_the_search_finds_at_least_the_floors_and_prints_the_table(run: Run) -> None:
    print()
    print(render_table(run.measured))

    for key, floor in FLOORS.items():
        found = run.measured.hits.get(key, (0,) * len(CUTOFFS))
        for cutoff, hits, least in zip(CUTOFFS, found, floor, strict=True):
            assert hits >= least, (key, f"@{cutoff}", hits, least)


def test_no_description_or_question_reaches_an_audit_row(run: Run) -> None:
    audit = str(owner_rows(run.database, "SELECT * FROM audit.events"))

    ((own,),) = owner_rows(
        run.database, "SELECT count(*) FROM audit.events WHERE tenant = 'evaluation'"
    )
    assert own > 0  # the queries' own calls were audited, without their text
    for query in run.queries:
        assert query.text not in audit
        assert query.text[:40] not in audit
