"""The retrieval check measured through the tool (S046): what an agent gets from
``wording_search`` for the narrative family of the golden set (a claim's
description as the query, its citations in sections 2 and 3 as the labels),
called through the server with the product of the claim's policy and no
``top_k``. ``test_retrieval.py`` measures the library only."""

import uuid
from dataclasses import dataclass

import pytest
from dbsupport import OWNER, DatabaseHandle
from knowledgesupport import Gateway, ingest
from retrievalsupport import narrative_queries
from servicesupport import synthetic_claims
from toolsupport import (
    SYNTHETIC_DIR,
    World,
    add_claim,
    add_run,
    knowledge_server,
    run_call,
)

from meridian.platform.common.db import connect
from meridian.platform.knowledge_mcp.tools import DEFAULT_TOP_K
from meridian.platform.policy_mcp.seed import seed_policies

# Seconds that clear the gateway's windows (10 requests per 10 seconds and 10,000
# tokens per minute for the claims-triage tenant) before the next call.
WINDOW_SECONDS = 61

ALL = "all"
COVER = "2"
EXCLUSIONS = "3"
# 28, 20 and 8 for the first forty claims, and six cover clauses more for the
# claims on a fraud indicator's boundary; the floors below were measured before
# those six queries existed and still hold.
LABELLED = {ALL: 34, COVER: 26, EXCLUSIONS: 8}
# FLOORS: the labelled clauses found among the chunks of one call, in the groups
# of LABELLED (section 2 is cover, section 3 exclusions). Measured in replay
# mode (a simulated embedding, a hashed bag of words) on PostgreSQL 17.11 with
# pgvector 0.8.6, 2026-10-02: plumbing floors, not a measure of retrieval
# quality. A floor is lowered only with a reason, and raised never to flatter a
# number.
FLOORS = {ALL: 23, COVER: 18, EXCLUSIONS: 5}


@dataclass(frozen=True, slots=True)
class Found:
    labelled: dict[str, int]
    hits: dict[str, int]
    chunk_counts: list[int]


def seed_claims_and_runs(db: DatabaseHandle) -> dict[str, uuid.UUID]:
    """The policies seeded, and for each narrative claim a claim row on its own
    policy and a Running run; the claim's description to the run's ID."""
    with connect(db.dsn(OWNER), "test-seed") as conn:
        seed_policies(conn, SYNTHETIC_DIR)
    claims = {claim["description"]: claim for claim in synthetic_claims()}
    runs = {}
    for query in narrative_queries():
        claim = claims[query.text]
        add_claim(db, claim["claim_id"], policy_number=claim["policy_number"])
        runs[query.text] = add_run(db, claim["claim_id"])
    return runs


@pytest.fixture
def found(fresh_database: DatabaseHandle, gateway: Gateway) -> Found:
    ingest(fresh_database, gateway.http, gateway.registry)
    runs = seed_claims_and_runs(fresh_database)
    # The server reads the run of each call from its metadata; the world only
    # names the database.
    server = knowledge_server(World(fresh_database, uuid.uuid4()), gateway.http)
    labelled = {ALL: 0, COVER: 0, EXCLUSIONS: 0}
    hits = {ALL: 0, COVER: 0, EXCLUSIONS: 0}
    chunk_counts = []
    for query in narrative_queries():
        gateway.clock.advance(WINDOW_SECONDS)
        result = run_call(
            server,
            "wording_search",
            {"query": query.text, "product": query.product},
            run_id=runs[query.text],
        )
        assert result.is_error is False, query.text
        chunks = result.structured_content["chunks"]
        chunk_counts.append(len(chunks))
        clauses = {chunk["clause"] for chunk in chunks}
        for label in query.relevant:
            section = label.split(".")[0]
            labelled[ALL] += 1
            labelled[section] += 1
            if label in clauses:
                hits[ALL] += 1
                hits[section] += 1
    return Found(labelled, hits, chunk_counts)


def test_the_tool_finds_at_least_the_floors_of_the_labelled_clauses(
    found: Found,
) -> None:
    print()
    print(f"through the tool, no top_k: found {found.hits} of {found.labelled}")

    assert found.labelled == LABELLED
    assert found.chunk_counts == [DEFAULT_TOP_K] * len(found.chunk_counts)
    for group, floor in FLOORS.items():
        assert found.hits[group] >= floor, (group, found.hits[group], floor)
