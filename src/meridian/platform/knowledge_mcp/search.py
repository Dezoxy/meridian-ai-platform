"""Hybrid search over the knowledge store (S012): a keyword half and a vector
half, fused by reciprocal rank. A connection in, hits out; no HTTP, no registry
and no logging, so nothing of a query, a hit or a vector leaves through here.

Everything a search reads comes from ONE statement, so an ingestion that
commits meanwhile cannot give the two halves different corpora (T-58). The
statement counts the rows of the scope, the rows whose vectors are comparable
with the query's and the rows the vector half measured, and the search refuses
when they differ: vectors of two deployments or two lengths are not in one
space, a stored vector that has no distance (all zero) cannot be ranked, and an
answer without such rows would lose them silently (T-54). The comparable rows
are materialised before any distance is computed, and are those whose vector
really has the query's length (not only those whose ``dimensions`` column says
so), so pgvector is never asked to compare two lengths whatever plan the
database picks and whatever constraint the table still has. ``model`` is stored
with each row but not compared: two models of one deployment name and length
would pass as one space, the residual T-54 names.

The caller ends the read transaction: the functions here leave it open, as the
tool kit's handlers do (``toolserver``), because the caller decides whether the
answer and its audit row share a transaction. The statement timeout is the
connection's (``common/db.py``), not this module's.

The query text is a statement parameter. Its keyword terms are the lexemes
PostgreSQL's own parser gives for it, each one quoted and joined with OR, so no
character of it is read as ``tsquery`` syntax (T-59). An error names a reason
word and nothing else.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from meridian.platform.knowledge_mcp.store import usable_vector, vector_literal

# The limits of the wording_search tool's schema.
MAX_QUERY_CHARACTERS = 500
MAX_TOP_K = 10
CANDIDATES_PER_HALF = 20
RRF_K = 60

RefusalReason = Literal["invalid-query", "invalid-top-k", "no-corpus", "stale-vectors"]

# The lexemes of the query come out of ``to_tsvector`` already stemmed, so the
# text is cast to a tsquery as it is: ``to_tsquery`` would stem them again. A
# lexeme is quoted with its quote and backslash doubled; no lexeme at all leaves
# the text NULL, and ``lexemes @@ NULL`` matches nothing. Clauses sort by their
# numbers, so 2.9 comes before 2.10, and by their text when the numbers are equal.
# ``model`` is stored with each row and not compared here: the residual T-54 names.
SEARCH = r"""
WITH scope AS MATERIALIZED (
    SELECT clause, section, title, body, lexemes, deployment, dimensions,
           embedding, string_to_array(clause, '.')::int[] AS clause_order
    FROM knowledge.chunks
    WHERE product = %(product)s AND wording_version = %(wording_version)s
),
comparable AS MATERIALIZED (
    SELECT * FROM scope
    WHERE deployment = %(deployment)s AND dimensions = %(dimensions)s
      AND vector_dims(embedding) = %(dimensions)s
),
query AS (
    SELECT string_agg(
        '''' || replace(replace(lexeme, '\', '\\'), '''', '''''') || '''', ' | '
    )::tsquery AS tsq
    FROM unnest(to_tsvector('english', %(query)s::text))
),
lexical AS (
    SELECT clause, section, title, body,
           ts_rank_cd(lexemes, tsq) AS signal,
           row_number() OVER (
               ORDER BY ts_rank_cd(lexemes, tsq) DESC, clause_order, clause
           ) AS rank
    FROM comparable, query
    WHERE lexemes @@ tsq
),
measured AS (
    SELECT *, embedding <=> %(vector)s::vector AS distance FROM comparable
),
semantic AS (
    SELECT clause, section, title, body, distance AS signal,
           row_number() OVER (ORDER BY distance, clause_order, clause) AS rank
    FROM measured
    WHERE distance <> 'NaN'
)
SELECT
    (SELECT count(*) FROM scope),
    (SELECT count(*) FROM comparable),
    (SELECT count(*) FROM semantic),
    (SELECT json_agg(
        json_build_object(
            'clause', clause, 'section', section, 'title', title, 'body', body,
            'signal', signal
        ) ORDER BY rank
    ) FROM lexical WHERE rank <= %(limit)s),
    (SELECT json_agg(
        json_build_object(
            'clause', clause, 'section', section, 'title', title, 'body', body,
            'signal', signal
        ) ORDER BY rank
    ) FROM semantic WHERE rank <= %(limit)s)
"""


class SearchRefused(Exception):
    """The search will not answer. ``reason`` says why; the message is the
    reason word and holds nothing of the query."""

    def __init__(self, reason: RefusalReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class QueryEmbedding:
    """The query's vector and the registry ID of the deployment that made it."""

    deployment: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Candidate:
    """A row one half listed, in the order that half ranked it. ``signal`` is
    that half's own measure: ``ts_rank_cd`` for the keyword half (higher is
    better), the cosine distance for the vector half (lower is better, 0 to 2)."""

    clause: str
    section: str
    title: str
    body: str
    signal: float


@dataclass(frozen=True, slots=True)
class Candidates:
    """Both halves of one search, each best first and at most
    ``CANDIDATES_PER_HALF`` long, from one snapshot of the store."""

    lexical: tuple[Candidate, ...]
    vector: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class Hit:
    product: str
    wording_version: str
    clause: str
    section: str
    title: str
    body: str
    score: float  # the fused score: a rank, not a measure of a match
    lexical_rank: int | None  # 1-based; None when the half did not list it
    vector_rank: int | None
    keyword_score: float | None  # ts_rank_cd; None when the keyword half did not
    distance: float | None  # cosine distance; None when the vector half did not


def _clause_order(clause: str) -> tuple[int, ...]:
    return tuple(int(part) for part in clause.split("."))


def fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: a clause's score is the sum, over the lists that
    hold it, of ``1 / (k + rank)`` with ranks from 1. Best first; equal scores
    break by clause number."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        # A clause is counted once per list, at its first place.
        for rank, clause in enumerate(dict.fromkeys(ranking), start=1):
            scores[clause] = scores.get(clause, 0.0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], _clause_order(item[0])))


def _check_query(query: str, product: str, wording_version: str) -> None:
    if not query.strip() or len(query) > MAX_QUERY_CHARACTERS:
        raise SearchRefused("invalid-query")
    if "\x00" in query or "\x00" in product or "\x00" in wording_version:
        # PostgreSQL text holds no NUL: the driver would raise DataError.
        raise SearchRefused("invalid-query")
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate: the driver could not send it.
        raise SearchRefused("invalid-query") from None


def _candidates(rows: list[dict[str, Any]] | None) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            row["clause"], row["section"], row["title"], row["body"], row["signal"]
        )
        for row in rows or ()
    )


def rank_halves(
    conn: psycopg.Connection,
    *,
    product: str,
    wording_version: str,
    query: str,
    embedding: QueryEmbedding,
) -> Candidates:
    """Both ranked halves for ``query`` in one product and wording version, from
    one statement. Raises ``SearchRefused`` for a query or a vector that is not
    usable, a scope with no row, and a scope with a row whose vector is not
    comparable with the query's or has no distance from it."""
    _check_query(query, product, wording_version)
    if not usable_vector(embedding.vector):
        raise SearchRefused("invalid-query")
    parameters = {
        "product": product,
        "wording_version": wording_version,
        "deployment": embedding.deployment,
        "dimensions": len(embedding.vector),
        "query": query,
        "vector": vector_literal(embedding.vector),
        "limit": CANDIDATES_PER_HALF,
    }
    with conn.cursor() as cursor:
        cursor.execute(SEARCH, parameters)
        # The statement always answers with one row, the counts and the halves.
        ((in_scope, comparable, measured, lexical, vector),) = cursor.fetchall()
    if in_scope == 0:
        raise SearchRefused("no-corpus")
    if comparable != in_scope or measured != comparable:
        raise SearchRefused("stale-vectors")
    return Candidates(_candidates(lexical), _candidates(vector))


def hybrid_search(
    conn: psycopg.Connection,
    *,
    product: str,
    wording_version: str,
    query: str,
    embedding: QueryEmbedding,
    top_k: int,
) -> tuple[Hit, ...]:
    """The best ``top_k`` clauses of the scope by the fusion of both halves."""
    if not 1 <= top_k <= MAX_TOP_K:
        raise SearchRefused("invalid-top-k")
    candidates = rank_halves(
        conn,
        product=product,
        wording_version=wording_version,
        query=query,
        embedding=embedding,
    )
    rows = {c.clause: c for c in (*candidates.vector, *candidates.lexical)}
    lexical_ranks = {c.clause: n for n, c in enumerate(candidates.lexical, start=1)}
    vector_ranks = {c.clause: n for n, c in enumerate(candidates.vector, start=1)}
    keyword_scores = {c.clause: c.signal for c in candidates.lexical}
    distances = {c.clause: c.signal for c in candidates.vector}
    fused = fuse(
        [[c.clause for c in candidates.lexical], [c.clause for c in candidates.vector]]
    )
    return tuple(
        Hit(
            product=product,
            wording_version=wording_version,
            clause=clause,
            section=rows[clause].section,
            title=rows[clause].title,
            body=rows[clause].body,
            score=score,
            lexical_rank=lexical_ranks.get(clause),
            vector_rank=vector_ranks.get(clause),
            keyword_score=keyword_scores.get(clause),
            distance=distances.get(clause),
        )
        for clause, score in fused[:top_k]
    )
