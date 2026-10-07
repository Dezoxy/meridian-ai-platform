"""The retrieval check of the knowledge search (S012): labelled queries, the
measurement of three rankings and the table that shows it.

The labels come from the generator's output under ``data/synthetic/``, read
only. A query is a text, the product and wording version it is searched in, and
the clauses that answer it. Recall at k is the share of those clauses found
among the first k results of a ranking; the ``chance`` row is the recall a
random ranking of the query's scope would give.

Three families, each with the clauses it can honestly reach:

* ``narrative``: a claim's description is the query, and the relevant clauses
  are the claim's citations in sections 2 and 3 (what is covered, what is
  excluded). A documents clause cannot be reached from a description that says
  nothing about documents, so it is not a label here.
* ``documents``: the question a triage step would ask for a claim that cites a
  documents clause (section 5, not 5.1), built from the claim's peril.
* ``concept``: five fixed phrases per product. They share words with the clause
  titles, so this family checks the plumbing, not meaning.
"""

import json
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
from knowledgesupport import REAL_SOURCE, Gateway

from meridian.platform.knowledge_mcp.embedding_client import EmbeddingClient
from meridian.platform.knowledge_mcp.ingest import BATCH_SIZE
from meridian.platform.knowledge_mcp.search import (
    MAX_TOP_K,
    QueryEmbedding,
    hybrid_search,
    rank_halves,
)

QUERY_TENANT = "evaluation"
QUERY_AGENT = "claims-triage"
EMBEDDING_BATCH = BATCH_SIZE
NARRATIVE_SECTIONS = ("2", "3")
DOCUMENTS_SECTION = "5"
# Clause 5.1 is the deadline to report a claim, not a list of documents.
REPORTING_CLAUSE = "5.1"
# Five questions a triage step asks of every product, each with the one clause
# that answers it. They are fixed: a phrase is not changed to move a number.
CONCEPT_QUESTIONS = (
    ("deductible that applies to a claim", "4.1"),
    ("maximum amount payable for one claim", "4.2"),
    ("deadline for reporting a claim after the loss", "5.1"),
    ("dates when cover starts and ends", "6.1"),
    ("what happens when the premium is unpaid", "6.2"),
)
NARRATIVE = "narrative"
DOCUMENTS = "documents"
CONCEPT = "concept"
KEYWORD = "keyword"
VECTOR = "vector"
FUSED = "fused"
CHANCE = "chance"
RANKINGS = (KEYWORD, VECTOR, FUSED)
CUTOFFS = (1, 3, 5, 10)
GROUPS = (
    NARRATIVE,
    *(f"{NARRATIVE} section {section}" for section in NARRATIVE_SECTIONS),
    DOCUMENTS,
    CONCEPT,
)

# ``(group, ranking)`` to the hits at each cutoff of ``CUTOFFS``, and a group to
# the number of labelled clauses. ``Chance`` is a group to the hits a random
# ranking would give at each cutoff (not whole numbers).
Hits = Mapping[tuple[str, str], tuple[int, ...]]
Chance = Mapping[str, tuple[float, ...]]


@dataclass(frozen=True, slots=True)
class LabelledQuery:
    family: str
    text: str
    product: str
    wording_version: str
    relevant: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Measured:
    labelled: Mapping[str, int]
    hits: Hits
    chance: Chance


def _load(source: Path, name: str) -> list[dict]:
    return json.loads((source / name).read_text(encoding="utf-8"))


def _golden_queries(
    source: Path,
    family: str,
    text: Callable[[dict], str],
    wanted: Callable[[str], bool],
) -> list[LabelledQuery]:
    """One query per golden claim that cites a clause ``wanted`` accepts, in the
    product and wording version of its policy; ``text`` makes the query from the
    claim, and the cited clauses are the labels."""
    policies = {p["policy_number"]: p for p in _load(source, "policies.json")}
    outcomes = {o["claim_id"]: o for o in _load(source, "expected-outcomes.json")}
    queries: list[LabelledQuery] = []
    for claim in _load(source, "claims.json"):
        # None for the claim on a policy number no policy has: it cites nothing,
        # so no query is made from it.
        policy = policies.get(claim["policy_number"])
        relevant = tuple(
            citation["clause"]
            for citation in outcomes[claim["claim_id"]]["citations"]
            if wanted(citation["clause"])
        )
        if relevant:
            queries.append(
                LabelledQuery(
                    family,
                    text(claim),
                    policy["product"],
                    policy["wording_version"],
                    relevant,
                )
            )
    return queries


def narrative_queries(source: Path = REAL_SOURCE) -> list[LabelledQuery]:
    """The claim's description as the query, and its citations in sections 2
    and 3 as the labels."""
    return _golden_queries(
        source,
        NARRATIVE,
        lambda claim: claim["description"],
        lambda clause: clause.split(".")[0] in NARRATIVE_SECTIONS,
    )


def documents_queries(source: Path = REAL_SOURCE) -> list[LabelledQuery]:
    """The question a triage step would ask for a claim that cites a documents
    clause (section 5, not 5.1): ``documents needed for a <peril> claim``."""
    return _golden_queries(
        source,
        DOCUMENTS,
        lambda claim: (
            f"documents needed for a {claim['peril'].replace('_', ' ')} claim"
        ),
        lambda clause: (
            clause.split(".")[0] == DOCUMENTS_SECTION and clause != REPORTING_CLAUSE
        ),
    )


def concept_queries(versions: Mapping[str, str]) -> list[LabelledQuery]:
    """The five fixed questions for each product, in the wording version the
    corpus holds for it."""
    return [
        LabelledQuery(CONCEPT, question, product, version, (clause,))
        for product, version in sorted(versions.items())
        for question, clause in CONCEPT_QUESTIONS
    ]


def corpus_versions(conn: psycopg.Connection) -> dict[str, str]:
    """The wording version of each product in the store; one each, or the
    check cannot say which corpus a concept question is about."""
    rows = conn.execute(
        "SELECT product, array_agg(DISTINCT wording_version) "
        "FROM knowledge.chunks GROUP BY product"
    ).fetchall()
    versions = {product: found for product, found in rows}
    ambiguous = sorted(p for p, found in versions.items() if len(found) != 1)
    if ambiguous:
        raise AssertionError(f"more than one wording version for {ambiguous}")
    return {product: found[0] for product, found in versions.items()}


def require_labels_in_corpus(
    conn: psycopg.Connection, queries: Sequence[LabelledQuery]
) -> None:
    """Fail loudly when a labelled clause is not in the corpus."""
    present = {
        tuple(row)
        for row in conn.execute(
            "SELECT product, wording_version, clause FROM knowledge.chunks"
        ).fetchall()
    }
    missing = sorted(
        {
            (q.product, q.wording_version, clause)
            for q in queries
            for clause in q.relevant
            if (q.product, q.wording_version, clause) not in present
        }
    )
    if missing:
        raise AssertionError(f"labelled clauses are not in the corpus: {missing}")


def embed_queries(gateway: Gateway, texts: Sequence[str]) -> list[QueryEmbedding]:
    """The vectors of ``texts`` through the real gateway app as the evaluation
    tenant running claims-triage, 16 texts to a call."""
    client = EmbeddingClient(
        gateway.http, tenant=QUERY_TENANT, agent=QUERY_AGENT, run_id=uuid.uuid4()
    )
    embeddings: list[QueryEmbedding] = []
    for start in range(0, len(texts), EMBEDDING_BATCH):
        batch = client.embed(texts[start : start + EMBEDDING_BATCH])
        embeddings.extend(QueryEmbedding(batch.deployment, v) for v in batch.vectors)
    return embeddings


def _groups(query: LabelledQuery, clause: str) -> list[str]:
    if query.family == NARRATIVE:
        return [NARRATIVE, f"{NARRATIVE} section {clause.split('.')[0]}"]
    return [query.family]


def scope_sizes(conn: psycopg.Connection) -> dict[tuple[str, str], int]:
    """The number of clauses in each product and wording version."""
    rows = conn.execute(
        "SELECT product, wording_version, count(*) FROM knowledge.chunks "
        "GROUP BY product, wording_version"
    ).fetchall()
    return {(product, version): count for product, version, count in rows}


def measure(
    conn: psycopg.Connection,
    queries: Sequence[LabelledQuery],
    embeddings: Sequence[QueryEmbedding],
) -> Measured:
    """Hits at each cutoff for the keyword half alone, the vector half alone and
    the fusion of the two, per group, and the hits a random ranking of each
    query's scope would give."""
    sizes = scope_sizes(conn)
    labelled: dict[str, int] = defaultdict(int)
    hits: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * len(CUTOFFS))
    chance: dict[str, list[float]] = defaultdict(lambda: [0.0] * len(CUTOFFS))
    for query, embedding in zip(queries, embeddings, strict=True):
        size = sizes[(query.product, query.wording_version)]
        scope = {
            "product": query.product,
            "wording_version": query.wording_version,
            "query": query.text,
            "embedding": embedding,
        }
        halves = rank_halves(conn, **scope)
        rankings = {
            KEYWORD: [c.clause for c in halves.lexical],
            VECTOR: [c.clause for c in halves.vector],
            FUSED: [h.clause for h in hybrid_search(conn, **scope, top_k=MAX_TOP_K)],
        }
        for clause in query.relevant:
            for group in _groups(query, clause):
                labelled[group] += 1
                for index, cutoff in enumerate(CUTOFFS):
                    chance[group][index] += min(cutoff, size) / size
                for ranking, found in rankings.items():
                    for index, cutoff in enumerate(CUTOFFS):
                        if clause in found[:cutoff]:
                            hits[(group, ranking)][index] += 1
    return Measured(
        dict(labelled),
        {key: tuple(counts) for key, counts in hits.items()},
        {group: tuple(expected) for group, expected in chance.items()},
    )


def render_table(measured: Measured) -> str:
    """The measurement as plain text: per group and ranking, the hits over the
    labelled clauses at each cutoff, with the share; ``chance`` is the hits a
    random ranking of the scope gives. A group with no labelled clause shows
    dashes."""
    cutoffs = "".join(f"{f'@{cutoff}':>14}" for cutoff in CUTOFFS)
    lines = [
        "retrieval check: recall = hits / labelled clauses (SIMULATED embedding)",
        "chance: the hits a random ranking of the scope gives, mean of min(k, N) / N",
        "concept: its phrases share words with the clause titles, so it checks the "
        "plumbing, not meaning",
        f"{'group':<20}{'ranking':<9}{'labelled':>9}{cutoffs}",
    ]
    for group in GROUPS:
        total = measured.labelled.get(group, 0)
        rows = [
            (ranking, measured.hits.get((group, ranking), (0,) * len(CUTOFFS)))
            for ranking in RANKINGS
        ]
        rows.append((CHANCE, measured.chance.get(group, (0.0,) * len(CUTOFFS))))
        for ranking, counts in rows:
            if total:
                precision = ".1f" if ranking == CHANCE else "d"
                cells = "".join(
                    f"{f'{n:{precision}}/{total} {n / total:.2f}':>14}" for n in counts
                )
            else:
                cells = "".join(f"{'-':>14}" for _ in CUTOFFS)
            lines.append(f"{group:<20}{ranking:<9}{total:>9}{cells}")
    return "\n".join(lines)
