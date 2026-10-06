"""The platform's own search, asked with each claim's description.

The real wordings are ingested through the real gateway app in replay mode
(the way the platform's retrieval check does it) and each claim's description
is the query, in its policy's product and wording version. The embedding is the
platform's simulated one: a hashed bag of words, which carries no meaning of
the text. Three lists per claim, the first ten clauses of each:

- ``keyword``: the keyword half alone (it may list fewer than ten);
- ``vector``: the vector half alone (simulated embedding);
- ``fused``: the reciprocal rank fusion of the two (``hybrid_search``).

This module opens no labels and does not import the scorer.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from claimgraph import files
from dbsupport import OWNER, DatabaseHandle
from knowledgesupport import Gateway, ingest
from retrievalsupport import CUTOFFS, FUSED, KEYWORD, VECTOR, embed_queries
from servicesupport import REPO_ROOT

from meridian.platform.common.db import connect
from meridian.platform.knowledge_mcp.search import (
    MAX_TOP_K,
    hybrid_search,
    rank_halves,
)

DATA_DIR = REPO_ROOT / "data" / "synthetic"
DEPTH = max(CUTOFFS)
SEARCHES = (KEYWORD, VECTOR, FUSED)


@dataclass(frozen=True, slots=True)
class SearchQuery:
    claim_id: str
    product: str
    wording_version: str
    text: str


def search_queries(data_dir: Path = DATA_DIR) -> list[SearchQuery]:
    """One query per claim: its description, in its policy's wording."""
    policies = {
        p["policy_number"]: p
        for p in json.loads(files.read_text(data_dir / "policies.json"))
    }
    return [
        SearchQuery(
            c["claim_id"],
            policies[c["policy_number"]]["product"],
            policies[c["policy_number"]]["wording_version"],
            c["description"],
        )
        for c in json.loads(files.read_text(data_dir / "claims.json"))
    ]


def rank_claims(
    database: DatabaseHandle, gateway: Gateway, data_dir: Path = DATA_DIR
) -> dict[str, dict[str, list[str]]]:
    """Ingest the wordings, then the first ``DEPTH`` clauses of each search for
    each claim's description, by claim ID."""
    ingest(database, gateway.http, gateway.registry, source=data_dir)
    queries = search_queries(data_dir)
    embeddings = embed_queries(gateway, [q.text for q in queries])
    found: dict[str, dict[str, list[str]]] = {}
    with connect(database.dsn(OWNER), "s038-search") as conn:
        for query, embedding in zip(queries, embeddings, strict=True):
            scope = {
                "product": query.product,
                "wording_version": query.wording_version,
                "query": query.text,
                "embedding": embedding,
            }
            halves = rank_halves(conn, **scope)
            fused = hybrid_search(conn, **scope, top_k=MAX_TOP_K)
            found[query.claim_id] = {
                KEYWORD: [c.clause for c in halves.lexical][:DEPTH],
                VECTOR: [c.clause for c in halves.vector][:DEPTH],
                FUSED: [h.clause for h in fused][:DEPTH],
            }
    return found
