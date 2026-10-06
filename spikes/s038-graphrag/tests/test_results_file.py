"""The committed results file against a fresh run. Without a database only the
search's own lists cannot be re-made; they are stored in the file, so every
other number is recomputed from them and the committed data. With a database
the whole file is made again, search included.

To regenerate the file, run the database test with ``S038_WRITE_RESULTS=1``
(the README gives the commands): it writes ``results/comparison.json`` instead
of comparing with it."""

import os
from pathlib import Path

from claimgraph.model import Graph
from comparison import report, search
from dbsupport import DatabaseHandle
from knowledgesupport import Gateway

WRITE_ENV = "S038_WRITE_RESULTS"
SEARCHES = ("keyword", "vector", "fused")


def _stored_search(committed: dict) -> dict[str, dict[str, list[str]]]:
    return {
        c: {s: found[s] for s in SEARCHES} for c, found in committed["lists"].items()
    }


def test_the_committed_file_is_what_the_stored_search_lists_and_the_data_give(
    graph: Graph, data_dir: Path
) -> None:
    committed = report.load()

    again = report.canonical(
        report.build_results(graph, data_dir, _stored_search(committed))
    )

    assert again == committed


def test_the_committed_file_is_what_a_fresh_search_and_the_data_give(
    fresh_database: DatabaseHandle, gateway: Gateway, graph: Graph, data_dir: Path
) -> None:
    lists = search.rank_claims(fresh_database, gateway)
    fresh = report.canonical(report.build_results(graph, data_dir, lists))

    if os.environ.get(WRITE_ENV) == "1":
        report.write(report.RESULTS, fresh)
        return

    assert fresh == report.load()
