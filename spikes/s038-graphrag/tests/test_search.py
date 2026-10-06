"""The platform's own search, asked with each claim's description through the
real gateway app in replay mode (a simulated embedding) and PostgreSQL. These
need a database and skip without one (`make pytest-db`, or the commands in the
README). The first test reproduces the narrative numbers of the platform's
retrieval check, so that a difference found later is the graph's and not a
difference of harness."""

import builtins
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from comparison import rankings, score, search
from dbsupport import OWNER, DatabaseHandle
from knowledge_mcp.test_retrieval import FLOORS
from knowledgesupport import Gateway
from retrievalsupport import (
    CUTOFFS,
    FUSED,
    KEYWORD,
    NARRATIVE,
    RANKINGS,
    VECTOR,
    Measured,
    embed_queries,
    measure,
    narrative_queries,
)

from meridian.platform.common.db import connect

SEARCHES = (KEYWORD, VECTOR, FUSED)


@dataclass(frozen=True, slots=True)
class Run:
    lists: dict[str, dict[str, list[str]]]
    measured: Measured


@pytest.fixture
def run(fresh_database: DatabaseHandle, gateway: Gateway) -> Run:
    lists = search.rank_claims(fresh_database, gateway)
    queries = narrative_queries()
    embeddings = embed_queries(gateway, [q.text for q in queries])
    with connect(fresh_database.dsn(OWNER), "s038-measure") as conn:
        return Run(lists, measure(conn, queries, embeddings))


def test_the_narrative_hits_equal_the_floors_of_the_retrieval_check(run: Run) -> None:
    """The floors are floors (a test of the platform may find more); here the
    numbers are expected to be the floors exactly, so that the harness is the
    platform's own. A difference means the image, the search or a query moved:
    say so before reading anything the graph is compared with."""
    narrative = {k: v for k, v in FLOORS.items() if k[0].startswith(NARRATIVE)}

    found = {key: run.measured.hits[key] for key in narrative}

    assert len(narrative) == 9
    assert found == narrative
    assert all(len(floor) == len(CUTOFFS) for floor in narrative.values())


def test_the_per_claim_lists_give_the_same_narrative_hits_as_the_platform_check(
    run: Run,
) -> None:
    labels = score.load_labels()
    narrative = labels.by_set["narrative"]
    by_ranking = {r: {c: run.lists[c][r] for c in run.lists} for r in SEARCHES}

    for ranking in SEARCHES:
        mine = tuple(
            score.recall(by_ranking[ranking], narrative, k)[0] for k in CUTOFFS
        )
        assert mine == run.measured.hits[(NARRATIVE, ranking)], ranking
    for section in ("2", "3"):
        of_section = {
            c: tuple(x for x in cs if x.startswith(f"{section}."))
            for c, cs in narrative.items()
        }
        group = f"{NARRATIVE} section {section}"
        for ranking in SEARCHES:
            mine = tuple(
                score.recall(by_ranking[ranking], of_section, k)[0] for k in CUTOFFS
            )
            assert mine == run.measured.hits[(group, ranking)], (group, ranking)


def test_chance_is_the_same_as_the_platform_check_computes_it(
    run: Run, data_dir: Path
) -> None:
    narrative = score.load_labels().by_set["narrative"]
    inputs = {i.claim_id: i for i in rankings.claim_inputs(data_dir)}
    sizes = rankings.scope_sizes(data_dir)
    scope = {
        c: sizes[(inputs[c].product, inputs[c].wording_version)] for c in narrative
    }

    mine = tuple(score.chance_hits(narrative, scope, k) for k in CUTOFFS)

    assert mine == pytest.approx(run.measured.chance[NARRATIVE])


def test_each_claim_has_a_list_for_each_search_of_at_most_ten_clauses(run: Run) -> None:
    assert len(run.lists) == 40
    for claim, lists in run.lists.items():
        assert set(lists) == set(SEARCHES), claim
        assert all(len(found) <= max(CUTOFFS) for found in lists.values()), claim
        assert all(len(found) == len(set(found)) for found in lists.values()), claim
    assert set(RANKINGS) == set(SEARCHES)


def test_asking_the_search_opens_no_labels_file(
    fresh_database: DatabaseHandle, gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_open = io.open

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", recording_open)
    monkeypatch.setattr(builtins, "open", recording_open)

    search.rank_claims(fresh_database, gateway)

    assert any(p.endswith("claims.json") for p in opened)
    assert not [p for p in opened if "expected-outcomes" in p or "generator" in p]
