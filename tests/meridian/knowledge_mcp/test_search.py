"""The hybrid search over the knowledge store (S012): the fusion on its own, the
refusals, and the search against a real PostgreSQL with pgvector.

The rows are planted by hand, with the replay vector of each clause's title and
body (the text the ingestion embeds), so a test chooses exactly what is in the
scope. The real wordings are measured in ``test_retrieval.py``.
"""

import dataclasses
import hashlib
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from servicesupport import REGISTRY_DIR, REPO_ROOT, owner_rows

from meridian.platform.common.db import connect
from meridian.platform.gateway.replay import replay_embedding
from meridian.platform.knowledge_mcp.search import (
    CANDIDATES_PER_HALF,
    MAX_QUERY_CHARACTERS,
    MAX_TOP_K,
    RRF_K,
    SEARCH,
    Hit,
    QueryEmbedding,
    SearchRefused,
    fuse,
    hybrid_search,
    rank_halves,
)
from meridian.platform.knowledge_mcp.store import (
    ChunkRow,
    replace_corpus,
    vector_literal,
)
from meridian.platform.registry import load_registry

WORDING_PRODUCTS = {"HOME-PLUS", "HOME-STD", "MOTOR-COMP", "MOTOR-TPL"}
PRODUCT = "HOME-STD"
VERSION = "2026-01"
DEPLOYMENT = "replay-embedding"
DIMENSIONS = 32
CANARY = "CANARY-query-text-4471"
SHA = hashlib.sha256(b"a planted wording").hexdigest()


# ── helpers ─────────────────────────────────────────────────────────────────
def chunk(
    clause: str,
    title: str,
    body: str,
    *,
    product: str = PRODUCT,
    version: str = VERSION,
    deployment: str = DEPLOYMENT,
    dimensions: int = DIMENSIONS,
) -> ChunkRow:
    return ChunkRow(
        product=product,
        wording_version=version,
        clause=clause,
        section="A section",
        title=title,
        body=body,
        source_sha256=SHA,
        deployment=deployment,
        model=deployment,
        dimensions=dimensions,
        embedding=replay_embedding(f"{title}\n{body}", dimensions),
    )


def embedding_of(text: str, *, dimensions: int = DIMENSIONS) -> QueryEmbedding:
    return QueryEmbedding(DEPLOYMENT, replay_embedding(text, dimensions))


def default_rows() -> list[ChunkRow]:
    return [
        chunk("2.1", "Storm cover", "Damage caused by a storm to the building."),
        chunk("2.2", "Fire cover", "Damage caused by fire or smoke to the building."),
        chunk("3.1", "Flood exclusion", "Damage caused by a flood is not covered."),
        chunk("4.1", "Deductible", "The deductible is deducted from every claim."),
        chunk("5.1", "Reporting a claim", "Report the loss within thirty days."),
    ]


Plant = Callable[..., psycopg.Connection]


@pytest.fixture
def plant_corpus(fresh_database: DatabaseHandle) -> Iterator[Plant]:
    """Replace the corpus with the rows given and return a connection to search
    over; the connections are closed afterwards."""
    opened: list[psycopg.Connection] = []

    def make(rows: list[ChunkRow] | None = None) -> psycopg.Connection:
        with connect(fresh_database.dsn(OWNER), "test-plant") as writer:
            replace_corpus(writer, default_rows() if rows is None else rows)
            writer.commit()
        conn = connect(fresh_database.dsn(OWNER), "test-search")
        opened.append(conn)
        return conn

    yield make
    for conn in opened:
        conn.close()


def search(
    conn: psycopg.Connection,
    query: str,
    *,
    top_k: int = 5,
    product: str = PRODUCT,
    version: str = VERSION,
    embedding: QueryEmbedding | None = None,
) -> tuple[Hit, ...]:
    return hybrid_search(
        conn,
        product=product,
        wording_version=version,
        query=query,
        embedding=embedding or embedding_of(query),
        top_k=top_k,
    )


def halves(
    conn: psycopg.Connection,
    query: str,
    *,
    product: str = PRODUCT,
    version: str = VERSION,
    embedding: QueryEmbedding | None = None,
) -> Any:
    return rank_halves(
        conn,
        product=product,
        wording_version=version,
        query=query,
        embedding=embedding or embedding_of(query),
    )


def drop_check(db: DatabaseHandle, containing: str) -> None:
    """Drop the one CHECK constraint of the chunk table whose text holds
    ``containing``, as the owner."""
    ((name,),) = owner_rows(
        db,
        "SELECT conname FROM pg_constraint WHERE contype = 'c' "
        "AND conrelid = 'knowledge.chunks'::regclass "
        "AND pg_get_constraintdef(oid) LIKE %s",
        (f"%{containing}%",),
    )
    with connect(db.dsn(OWNER), "test-drop-check") as conn:
        conn.execute(f'ALTER TABLE knowledge.chunks DROP CONSTRAINT "{name}"')
        conn.commit()


def lexical_clauses(candidates: Any) -> list[str]:
    return [c.clause for c in candidates.lexical]


def vector_clauses(candidates: Any) -> list[str]:
    return [c.clause for c in candidates.vector]


# ── fuse: pure ──────────────────────────────────────────────────────────────
def test_fuse_of_one_list_scores_each_clause_by_its_rank() -> None:
    fused = fuse([["2.1", "2.2", "3.1"]])

    assert fused == [
        ("2.1", 1 / (RRF_K + 1)),
        ("2.2", 1 / (RRF_K + 2)),
        ("3.1", 1 / (RRF_K + 3)),
    ]


def test_fuse_of_two_lists_adds_the_scores_and_lifts_a_clause_in_both() -> None:
    fused = fuse([["2.1", "2.2", "3.1"], ["3.1", "2.2"]])

    scores = dict(fused)
    assert scores["3.1"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["2.2"] == pytest.approx(1 / 62 + 1 / 62)
    assert scores["2.1"] == pytest.approx(1 / 61)
    assert [clause for clause, _ in fused] == ["3.1", "2.2", "2.1"]


def test_a_clause_in_one_list_only_keeps_its_single_term() -> None:
    fused = dict(fuse([["2.1"], ["4.2"]]))

    assert fused == {"2.1": 1 / (RRF_K + 1), "4.2": 1 / (RRF_K + 1)}


def test_fuse_ties_break_by_clause_in_numeric_order() -> None:
    # 2.10 sorts before 2.9 as text; the order is the clause's number.
    tied = fuse([["2.10"], ["2.9"]])
    assert [clause for clause, _ in tied] == ["2.9", "2.10"]
    assert tied[0][1] == tied[1][1]

    assert [clause for clause, _ in fuse([["3.1", "2.10", "2.9"]])] == [
        "3.1",
        "2.10",
        "2.9",
    ]
    assert [c for c, _ in fuse([["10.1"], ["9.2"], ["9.10"]])] == [
        "9.2",
        "9.10",
        "10.1",
    ]


def test_fuse_takes_the_constant_as_a_parameter() -> None:
    assert fuse([["2.1"]], k=0) == [("2.1", 1.0)]
    assert fuse([["2.1", "2.2"]], k=9) == [("2.1", 1 / 10), ("2.2", 1 / 11)]
    assert RRF_K == 60


def test_fuse_of_nothing_is_nothing() -> None:
    assert fuse([]) == []
    assert fuse([[], []]) == []


def test_fuse_counts_a_clause_once_per_list_even_if_a_list_repeats_it() -> None:
    assert dict(fuse([["2.1", "2.1"]])) == {"2.1": 1 / (RRF_K + 1)}


# ── refusals before the database ────────────────────────────────────────────
def refused(**changes: Any) -> SearchRefused:
    """Search with no connection at all: a refusal that comes before the
    database never touches it, so an answer from the database would be an
    AttributeError and not a ``SearchRefused``."""
    arguments: dict[str, Any] = {
        "product": PRODUCT,
        "wording_version": VERSION,
        "query": "storm damage",
        "embedding": embedding_of("storm damage"),
        "top_k": 5,
    } | changes
    with pytest.raises(SearchRefused) as raised:
        hybrid_search(None, **arguments)  # type: ignore[arg-type]
    return raised.value


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("", id="empty"),
        pytest.param("   \t\n ", id="whitespace-only"),
        pytest.param("x" * (MAX_QUERY_CHARACTERS + 1), id="501-characters"),
        pytest.param("storm\x00damage", id="nul"),
        pytest.param("storm \ud800 damage", id="lone-surrogate"),
    ],
)
def test_a_query_that_is_empty_too_long_or_unreadable_is_refused_first(
    query: str,
) -> None:
    error = refused(query=query)

    assert error.reason == "invalid-query"
    assert str(error) == "invalid-query"


def test_a_vector_that_is_empty_not_finite_or_not_a_direction_is_refused_first() -> (
    None
):
    for vector in [
        (),
        (1.0, math.nan, 0.0),
        (1.0, math.inf, 0.0),
        (1.0, -math.inf, 0.0),
        (0.0, 0.0, 0.0),
        (1.0, 1e39, 0.0),  # beyond the 4-byte floats pgvector keeps
        (1e-50, 1e-50, 1e-50),  # stored as all zero
        (1e-23, 1e-23, 1e-23),  # the norm underflows
        (2e19, 2e19, 2e19),  # the norm overflows
    ]:
        error = refused(embedding=QueryEmbedding(DEPLOYMENT, vector))

        assert (error.reason, str(error)) == ("invalid-query", "invalid-query"), vector


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"product": "HOME\x00STD"}, id="nul-in-the-product"),
        pytest.param({"wording_version": "2026\x00-01"}, id="nul-in-the-version"),
    ],
)
def test_a_nul_in_the_scope_is_refused_first_not_a_database_error(
    changes: dict[str, str],
) -> None:
    error = refused(**changes)

    assert (error.reason, str(error)) == ("invalid-query", "invalid-query")


@pytest.mark.parametrize("top_k", [0, MAX_TOP_K + 1, -1])
def test_a_top_k_outside_one_to_ten_is_refused_first(top_k: int) -> None:
    error = refused(top_k=top_k)

    assert (error.reason, str(error)) == ("invalid-top-k", "invalid-top-k")


def test_the_limits_are_the_tools_schema() -> None:
    tool = load_registry(REGISTRY_DIR).tool("wording_search")
    assert tool is not None
    properties = tool.input_schema["properties"]
    products = {
        path.stem
        for path in (REPO_ROOT / "data" / "synthetic" / "wordings").glob("*.md")
    }

    assert properties["query"]["maxLength"] == MAX_QUERY_CHARACTERS
    assert properties["top_k"]["maximum"] == MAX_TOP_K
    assert properties["top_k"]["minimum"] == 1
    assert set(properties["product"]["enum"]) == products == WORDING_PRODUCTS
    # Not in the schema: how many rows each half lists before the fusion.
    assert CANDIDATES_PER_HALF == 20


# ── refusals that need the corpus ───────────────────────────────────────────
def test_a_scope_with_no_row_is_refused_as_no_corpus(plant_corpus: Plant) -> None:
    conn = plant_corpus()

    for product, version in [("MOTOR-TPL", VERSION), (PRODUCT, "2025-01")]:
        with pytest.raises(SearchRefused) as raised:
            search(conn, "storm", product=product, version=version)

        assert (raised.value.reason, str(raised.value)) == ("no-corpus", "no-corpus")


def test_an_empty_store_is_no_corpus(plant_corpus: Plant) -> None:
    conn = plant_corpus([])

    with pytest.raises(SearchRefused) as raised:
        search(conn, "storm")

    assert raised.value.reason == "no-corpus"


def test_a_scope_of_another_deployment_is_stale(plant_corpus: Plant) -> None:
    conn = plant_corpus(
        [
            chunk(c.clause, c.title, c.body, deployment="other-model")
            for c in default_rows()
        ]
    )

    with pytest.raises(SearchRefused) as raised:
        search(conn, "storm")

    assert (raised.value.reason, str(raised.value)) == (
        "stale-vectors",
        "stale-vectors",
    )


def test_one_row_of_another_length_among_good_rows_is_stale_not_a_database_error(
    plant_corpus: Plant,
) -> None:
    rows = [*default_rows(), chunk("5.2", "Documents", "Send photos.", dimensions=16)]
    conn = plant_corpus(rows)

    # pgvector refuses to compare two lengths; the search must never ask it to.
    with pytest.raises(SearchRefused) as raised:
        search(conn, "storm")

    assert raised.value.reason == "stale-vectors"
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param((0.0,) * DIMENSIONS, id="all-zero"),
        pytest.param((1e-50,) * DIMENSIONS, id="underflows-to-all-zero"),
    ],
)
def test_a_stored_vector_the_database_cannot_measure_is_stale_not_dropped(
    plant_corpus: Plant, unreadable: tuple[float, ...]
) -> None:
    rows = default_rows()
    rows[1] = dataclasses.replace(rows[1], embedding=unreadable)
    conn = plant_corpus(rows)

    # Its distance is NaN: the vector half would list four rows of five and
    # nothing would say the fifth was lost (T-54).
    with pytest.raises(SearchRefused) as raised:
        search(conn, "storm")

    assert (raised.value.reason, str(raised.value)) == (
        "stale-vectors",
        "stale-vectors",
    )


def test_the_safety_of_the_distance_does_not_rest_on_the_check_constraint(
    fresh_database: DatabaseHandle, plant_corpus: Plant
) -> None:
    conn = plant_corpus()
    drop_check(fresh_database, "vector_dims")
    # A row whose column says 32 and whose vector has 16 components.
    liar = dataclasses.replace(
        chunk("5.2", "Documents", "Send photos."),
        embedding=replay_embedding("Send photos.", 16),
    )
    with connect(fresh_database.dsn(OWNER), "test-liar") as writer:
        writer.execute(
            "INSERT INTO knowledge.chunks (product, wording_version, clause, "
            "section, title, body, source_sha256, deployment, model, dimensions, "
            "embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
            (
                liar.product,
                liar.wording_version,
                liar.clause,
                liar.section,
                liar.title,
                liar.body,
                liar.source_sha256,
                liar.deployment,
                liar.model,
                liar.dimensions,
                vector_literal(liar.embedding),
            ),
        )
        writer.commit()

    # pgvector refuses to compare two lengths, and the column's word cannot be
    # trusted: the row is stale, not a database error.
    with pytest.raises(SearchRefused) as raised:
        search(conn, "storm")

    assert raised.value.reason == "stale-vectors"


def test_a_query_vector_of_another_length_than_the_rows_is_stale(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()

    with pytest.raises(SearchRefused) as raised:
        search(conn, "storm", embedding=embedding_of("storm", dimensions=16))

    assert raised.value.reason == "stale-vectors"


def test_a_query_vector_of_another_deployment_is_stale(plant_corpus: Plant) -> None:
    conn = plant_corpus()
    other = QueryEmbedding("another-deployment", embedding_of("storm").vector)

    with pytest.raises(SearchRefused) as raised:
        search(conn, "storm", embedding=other)

    assert raised.value.reason == "stale-vectors"


def test_a_stale_row_outside_the_scope_does_not_refuse_the_scope(
    plant_corpus: Plant,
) -> None:
    stale = chunk("2.1", "Storm cover", "Other.", product="HOME-PLUS", dimensions=16)
    conn = plant_corpus([*default_rows(), stale])

    hits = search(conn, "storm")

    assert hits
    assert all(hit.product == PRODUCT for hit in hits)


def test_a_vector_longer_than_pgvector_allows_is_refused_not_a_database_error() -> None:
    error = refused(embedding=QueryEmbedding(DEPLOYMENT, (1.0,) * 16_001))

    assert error.reason == "invalid-query"


# ── the answer ──────────────────────────────────────────────────────────────
def test_a_hit_carries_the_clause_its_text_its_score_and_both_ranks(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()

    hits = search(conn, "storm damage to the building")

    top = hits[0]
    assert top == Hit(
        product=PRODUCT,
        wording_version=VERSION,
        clause="2.1",
        section="A section",
        title="Storm cover",
        body="Damage caused by a storm to the building.",
        score=top.score,
        lexical_rank=1,
        vector_rank=1,
        keyword_score=top.keyword_score,
        distance=top.distance,
    )
    assert top.score == pytest.approx(2 / (RRF_K + 1))  # the fused score
    assert top.keyword_score is not None
    assert top.keyword_score > 0
    assert top.distance is not None
    assert 0 <= top.distance < 2


def test_the_hits_are_the_fusion_of_the_two_halves_cut_to_top_k(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()
    query = "damage caused by a flood or a storm"

    candidates = halves(conn, query)
    fused = fuse([lexical_clauses(candidates), vector_clauses(candidates)])
    hits = search(conn, query, top_k=3)

    assert [(hit.clause, hit.score) for hit in hits] == fused[:3]
    assert len(search(conn, query, top_k=1)) == 1
    assert len(search(conn, query, top_k=MAX_TOP_K)) == len(fused)


def test_a_clause_one_half_did_not_list_has_no_rank_there(plant_corpus: Plant) -> None:
    conn = plant_corpus()

    # "deductible" is in one clause's words only; the vector half lists them all.
    hits = search(conn, "deductible", top_k=MAX_TOP_K)

    by_clause = {hit.clause: hit for hit in hits}
    assert by_clause["4.1"].lexical_rank == 1
    assert by_clause["4.1"].keyword_score is not None
    assert by_clause["2.1"].lexical_rank is None
    assert by_clause["2.1"].keyword_score is None
    assert by_clause["2.1"].vector_rank is not None
    assert by_clause["2.1"].distance is not None
    assert len(hits) == 5


def test_a_hit_carries_the_signals_of_the_halves_that_listed_it(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()
    query = "damage caused by a flood or a storm"

    candidates = halves(conn, query)
    hits = search(conn, query, top_k=MAX_TOP_K)

    keyword = {c.clause: c.signal for c in candidates.lexical}
    distance = {c.clause: c.signal for c in candidates.vector}
    assert {h.clause: h.keyword_score for h in hits if h.lexical_rank} == keyword
    assert {h.clause: h.distance for h in hits if h.vector_rank} == distance
    # Best first: the keyword score falls and the distance rises down a list.
    assert list(keyword.values()) == sorted(keyword.values(), reverse=True)
    assert list(distance.values()) == sorted(distance.values())
    assert all(score > 0 for score in keyword.values())
    assert all(0 <= d <= 2 for d in distance.values())


# ── the two halves ──────────────────────────────────────────────────────────
def test_a_clauses_exact_title_ranks_that_clause_first_in_the_keyword_half(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()

    for row in default_rows():
        assert lexical_clauses(halves(conn, row.title))[0] == row.clause, row.title


def test_a_query_equal_to_a_chunks_embedded_text_ranks_it_first_in_the_vector_half(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()

    for row in default_rows():
        text = f"{row.title}\n{row.body}"
        assert vector_clauses(halves(conn, text))[0] == row.clause, row.clause
        top = search(conn, text)[0]
        assert top.vector_rank == 1
        assert top.distance == pytest.approx(0, abs=1e-6)


def test_the_vector_half_lists_every_comparable_row_the_keyword_half_only_matches(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()

    candidates = halves(conn, "deductible")

    assert lexical_clauses(candidates) == ["4.1"]
    assert sorted(vector_clauses(candidates)) == ["2.1", "2.2", "3.1", "4.1", "5.1"]


def test_each_half_is_cut_to_twenty_candidates(
    plant_corpus: Plant,
) -> None:
    rows = [chunk(f"2.{n}", f"Cover {n}", f"Storm damage {n}.") for n in range(1, 26)]
    conn = plant_corpus(rows)

    candidates = halves(conn, "storm damage")

    assert len(candidates.lexical) == CANDIDATES_PER_HALF
    assert len(candidates.vector) == CANDIDATES_PER_HALF


def test_ties_in_both_halves_and_in_the_fusion_break_in_numeric_clause_order(
    plant_corpus: Plant,
) -> None:
    # Identical text, so identical keyword scores and identical vectors.
    rows = [
        chunk("2.10", "Storm cover", "Damage caused by a storm."),
        chunk("2.9", "Storm cover", "Damage caused by a storm."),
        chunk("2.2", "Storm cover", "Damage caused by a storm."),
        chunk("3.1", "Fire cover", "Damage caused by fire."),
    ]
    conn = plant_corpus(rows)

    candidates = halves(conn, "Storm cover. Damage caused by a storm.")
    hits = search(conn, "Storm cover. Damage caused by a storm.")

    assert lexical_clauses(candidates)[:3] == ["2.2", "2.9", "2.10"]
    assert vector_clauses(candidates)[:3] == ["2.2", "2.9", "2.10"]
    assert [hit.clause for hit in hits][:3] == ["2.2", "2.9", "2.10"]


def test_both_windows_end_their_sort_keys_with_the_clause_text() -> None:
    # Not observable through the table: the primary key's index hands the rows
    # to the sort in clause-text order, so a tie comes out the same with or
    # without the key. The text of the statement is what pins it.
    assert "DESC, clause_order, clause\n" in SEARCH
    assert "ORDER BY distance, clause_order, clause)" in SEARCH


# ── scope ───────────────────────────────────────────────────────────────────
def test_a_query_that_matches_another_products_clause_never_returns_it(
    plant_corpus: Plant,
) -> None:
    rows = [
        *default_rows(),
        chunk(
            "2.7",
            "Flood cover",
            "Damage caused by a flood is covered.",
            product="HOME-PLUS",
        ),
        chunk(
            "2.8",
            "Flood cover",
            "Damage caused by a flood is covered.",
            version="2025-01",
        ),
    ]
    conn = plant_corpus(rows)
    query = "Flood cover. Damage caused by a flood is covered."

    candidates = halves(conn, query)
    hits = search(conn, query, top_k=MAX_TOP_K)

    assert "2.7" not in lexical_clauses(candidates) + vector_clauses(candidates)
    assert "2.8" not in lexical_clauses(candidates) + vector_clauses(candidates)
    assert {(hit.product, hit.wording_version) for hit in hits} == {(PRODUCT, VERSION)}
    assert {hit.clause for hit in hits} == {"2.1", "2.2", "3.1", "4.1", "5.1"}
    assert hits[0].clause == "3.1"


# ── the query is data ───────────────────────────────────────────────────────
HOSTILE = [
    "storm's \"damage\" 'to' the roof",
    "back\\slash \\ storm \\'",
    "storm & flood | fire ! ( ) : * <-> damage",
    "!storm & !(flood | fire):* <2> damage",
    "a.claimant@example.com wrote about a storm",
    "C:\\Users\\claimant\\storm.txt and /var/tmp/storm/photo.jpg",
    "https://example.com/claims?id=1&q=storm#top",
    "'a' & 'b'",
    "well-known storm-damaged roof, high-rise flood-proof",
    "the and of to a in it",
    "?!.,;:-()[]{}<>@#$%^&*",
    "storm " * 83 + "x",
    "s" * MAX_QUERY_CHARACTERS,
    "Ünïcödé storm 暴风 \u2603",
]


def expected_keyword_matches(db: DatabaseHandle, query: str) -> set[str]:
    """The clauses whose stored words share a word with the query's, by the
    database's own parser and in Python: no tsquery is built, so the search's
    quoting is not what is being trusted."""
    ((wanted,),) = owner_rows(
        db,
        "SELECT coalesce(array_agg(lexeme), '{}') "
        "FROM unnest(to_tsvector('english', %s::text))",
        (query,),
    )
    stored = owner_rows(
        db,
        "SELECT clause, (SELECT array_agg(lexeme) FROM unnest(lexemes)) "
        "FROM knowledge.chunks",
    )
    return {clause for clause, words in stored if set(words) & set(wanted)}


def test_hostile_query_text_is_read_as_words_by_the_keyword_half(
    fresh_database: DatabaseHandle, plant_corpus: Plant
) -> None:
    conn = plant_corpus()
    matched_something = 0

    for query in HOSTILE:
        assert len(query) <= MAX_QUERY_CHARACTERS, query
        expected = expected_keyword_matches(fresh_database, query)
        candidates = halves(conn, query)
        hits = search(conn, query, top_k=MAX_TOP_K)

        # The statement ran, the keyword half lists what the text's own words
        # match and nothing else, and the vector half lists every row.
        assert set(lexical_clauses(candidates)) == expected, query
        assert {h.clause for h in hits if h.lexical_rank} == expected, query
        assert len(candidates.vector) == 5, query
        assert len(hits) == 5, query
        matched_something += bool(expected)

    # Not vacuous: most of the hostile texts do hold a word of a clause.
    assert matched_something >= 6


def test_a_query_with_no_lexeme_has_no_keyword_half_and_the_vector_half_ranks_alone(
    plant_corpus: Plant,
) -> None:
    conn = plant_corpus()

    for query in ["the and of to a in it", "?!.,;:-()[]{}<>@#$%^&*", "'' \"\" \\"]:
        candidates = halves(conn, query)
        hits = search(conn, query, top_k=MAX_TOP_K)

        assert candidates.lexical == (), query
        assert len(candidates.vector) == 5, query
        assert {hit.lexical_rank for hit in hits} == {None}, query
        assert sorted(hit.vector_rank for hit in hits) == [1, 2, 3, 4, 5], query


def test_query_syntax_is_read_as_words_never_as_operators(plant_corpus: Plant) -> None:
    conn = plant_corpus()

    # Read as syntax, "!flood" would exclude the flood clause and "&" would
    # demand both words; read as words, each lexeme matches its own clause.
    both = lexical_clauses(halves(conn, "storm & !flood"))
    prefix = lexical_clauses(halves(conn, "stor:* flo:*"))

    assert sorted(both) == ["2.1", "3.1"]
    assert prefix == []  # a prefix is not a word of any clause


def test_a_stem_is_not_stemmed_twice(plant_corpus: Plant) -> None:
    # PostgreSQL's English stem of "lapse" is "laps", and the stem of "laps" is
    # "lap": a query lexeme run through the dictionaries again would match the
    # "lap" clause and miss the "lapse" one.
    conn = plant_corpus(
        [
            chunk("6.2", "Lapse", "Cover lapses when the premium is unpaid."),
            chunk("6.3", "Seat belt", "A lap belt is worn across the lap."),
        ]
    )

    assert lexical_clauses(halves(conn, "lapse")) == ["6.2"]
    assert lexical_clauses(halves(conn, "the lapses")) == ["6.2"]
    assert lexical_clauses(halves(conn, "lap")) == ["6.3"]


def test_a_query_of_the_full_length_is_accepted(plant_corpus: Plant) -> None:
    conn = plant_corpus()

    assert search(conn, "x" * MAX_QUERY_CHARACTERS)


# ── one statement ───────────────────────────────────────────────────────────
@contextmanager
def counted(
    fresh_database: DatabaseHandle,
) -> Iterator[tuple[psycopg.Connection, list[Any]]]:
    """A connection and the statements it executes through cursors."""
    statements: list[Any] = []

    class CountingCursor(psycopg.Cursor):
        def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(query)
            return super().execute(query, *args, **kwargs)

    with connect(fresh_database.dsn(OWNER), "test-count") as conn:
        conn.cursor_factory = CountingCursor
        yield conn, statements


def test_rank_halves_executes_exactly_one_statement(
    fresh_database: DatabaseHandle, plant_corpus: Plant
) -> None:
    plant_corpus()

    with counted(fresh_database) as (conn, statements):
        candidates = halves(conn, "storm damage")

    assert len(statements) == 1
    assert candidates.lexical
    assert candidates.vector


def test_hybrid_search_executes_exactly_one_statement(
    fresh_database: DatabaseHandle, plant_corpus: Plant
) -> None:
    plant_corpus()

    with counted(fresh_database) as (conn, statements):
        search(conn, "storm damage")

    assert len(statements) == 1


def test_a_refusal_for_the_scope_is_decided_by_the_same_statement(
    fresh_database: DatabaseHandle, plant_corpus: Plant
) -> None:
    plant_corpus()

    with (
        counted(fresh_database) as (conn, statements),
        pytest.raises(SearchRefused),
    ):
        search(conn, "storm", product="MOTOR-TPL")

    assert len(statements) == 1


# ── the transaction a search reads in (T-58) ────────────────────────────────
def test_a_replace_open_on_one_connection_is_invisible_to_a_search_on_another(
    fresh_database: DatabaseHandle, plant_corpus: Plant
) -> None:
    searcher = plant_corpus()  # the default rows, committed
    new_rows = [chunk("2.1", "Hail cover", "Damage caused by hail to the roof.")]

    with connect(fresh_database.dsn(OWNER), "test-replace") as writer:
        replace_corpus(writer, new_rows)  # open, not committed
        during = search(searcher, "hail", top_k=MAX_TOP_K)
        writer.commit()
    after = search(searcher, "hail", top_k=MAX_TOP_K)

    assert {hit.clause for hit in during} == {"2.1", "2.2", "3.1", "4.1", "5.1"}
    assert {hit.title for hit in during} == {
        "Storm cover",
        "Fire cover",
        "Flood exclusion",
        "Deductible",
        "Reporting a claim",
    }
    assert [(hit.clause, hit.title) for hit in after] == [("2.1", "Hail cover")]


# ── nothing of the query in a message ───────────────────────────────────────
def test_no_refusal_quotes_the_query_text(plant_corpus: Plant) -> None:
    conn = plant_corpus()
    wrong_length = embedding_of(CANARY, dimensions=16)
    cases: list[tuple[str, dict[str, Any]]] = [
        ("too long", {"query": CANARY + "x" * MAX_QUERY_CHARACTERS}),
        ("nul", {"query": CANARY + "\x00"}),
        ("bad top_k", {"query": CANARY, "top_k": 0}),
        ("no corpus", {"query": CANARY, "product": "MOTOR-TPL"}),
        ("stale", {"query": CANARY, "embedding": wrong_length}),
        (
            "bad vector",
            {"query": CANARY, "embedding": QueryEmbedding(DEPLOYMENT, (math.nan,))},
        ),
    ]

    for label, changes in cases:
        arguments: dict[str, Any] = {
            "product": PRODUCT,
            "wording_version": VERSION,
            "embedding": embedding_of("storm"),
            "top_k": 5,
        } | changes
        with pytest.raises(SearchRefused) as raised:
            hybrid_search(conn, **arguments)

        error = raised.value
        assert CANARY not in str(error), label
        assert CANARY not in repr(error), label
        assert CANARY not in repr(error.args), label
        assert error.__cause__ is None, label
        assert str(error) == error.reason, label
