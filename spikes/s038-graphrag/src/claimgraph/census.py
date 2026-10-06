"""What a graph holds, as plain numbers: nodes and edges by kind, the degree
distribution of each node kind for each edge kind, and for each named question
the number of start nodes whose answer holds more than the trivial one. The
numbers of a graph carry its label. Nothing here reads a file.

The trivial answer of each question (the start nodes are all the nodes of the
kind the question starts from):

- clauses_bearing_on_claim (starts: claims): one clause, or none. The one is
  what the peril's own name gives without any graph.
- events_before_loss (starts: claims, with the claim's policy and loss date):
  no earlier event.
- policies_of_customer (starts: customers): one policy.
- claims_on_asset (starts: assets): events through one policy only, which is
  what that policy's own record already says. More than trivial is events
  through two or more policies.
- customers_sharing_address (starts: customers): nobody else.
- claims_of_customer (starts: customers): claims on one policy only. More than
  trivial is claims on two or more policies.
"""

from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any

from .model import EDGE_KINDS, Graph
from .questions import (
    claims_of_customer,
    claims_on_asset,
    clauses_bearing_on_claim,
    customers_sharing_address,
    events_before_loss,
    policies_of_customer,
)
from .traverse import Cost


def _degrees(graph: Graph) -> dict[str, dict[str, dict[int, int]]]:
    """For each node kind and each edge kind it can be an end of: how many
    nodes have 0, 1, 2 ... edges of that kind, leaving ('out') or arriving
    ('in')."""
    result: dict[str, dict[str, dict[int, int]]] = {}
    for node in graph.nodes():
        result.setdefault(node.kind, {})
    for kind in result:
        ids = [n.id for n in graph.nodes(kind)]
        for edge_kind, spec in EDGE_KINDS.items():
            if kind in spec.sources:
                counts = Counter(len(graph.out_edges(i, (edge_kind,))) for i in ids)
                result[kind][f"{edge_kind}.out"] = dict(sorted(counts.items()))
            if kind == spec.target:
                counts = Counter(len(graph.in_edges(i, (edge_kind,))) for i in ids)
                result[kind][f"{edge_kind}.in"] = dict(sorted(counts.items()))
    return result


def _measure(
    graph: Graph,
    start_kind: str,
    ask: Callable[[str, Cost], Any],
    size: Callable[[Any], int],
    non_trivial: Callable[[Any], bool],
) -> dict[str, Any]:
    cost = Cost()
    sizes: Counter[int] = Counter()
    more = 0
    starts = 0
    for node in graph.nodes(start_kind):
        answer = ask(node.id, cost)
        starts += 1
        sizes[size(answer)] += 1
        more += non_trivial(answer)
    return {
        "starts": starts,
        "non_trivial": more,
        "answer_sizes": dict(sorted(sizes.items())),
        "cost": {
            "nodes_visited": cost.nodes_visited,
            "edges_followed": cost.edges_followed,
        },
    }


def _claim_policy(graph: Graph, claim_id: str) -> str:
    return graph.out_edges(claim_id, ("claimed_on",))[0].dst


def _events_of_claim(graph: Graph, claim_id: str, cost: Cost) -> Any:
    loss = date.fromisoformat(str(graph.node(claim_id).attrs["loss_date"]))
    return events_before_loss(graph, _claim_policy(graph, claim_id), loss, cost=cost)


def _claim_questions(graph: Graph) -> dict[str, dict[str, Any]]:
    return {
        "clauses_bearing_on_claim": _measure(
            graph,
            "Claim",
            lambda i, c: clauses_bearing_on_claim(graph, i, cost=c),
            len,
            lambda a: len(a) > 1,
        ),
        "events_before_loss": _measure(
            graph,
            "Claim",
            lambda i, c: _events_of_claim(graph, i, c),
            lambda a: len(a.claims) + len(a.history),
            lambda a: bool(a.claims or a.history),
        ),
    }


def _questions(graph: Graph) -> dict[str, dict[str, Any]]:
    def through(answer: list, position: int, end: str) -> int:
        edges = {r.path[position] for r in answer}
        return len({e.src if end == "src" else e.dst for e in edges})

    return {
        **_claim_questions(graph),
        "policies_of_customer": _measure(
            graph,
            "Customer",
            lambda i, c: policies_of_customer(graph, i, cost=c),
            len,
            lambda a: len(a) > 1,
        ),
        "claims_on_asset": _measure(
            graph,
            "Asset",
            lambda i, c: claims_on_asset(graph, i, cost=c),
            len,
            lambda a: through(a, 0, "src") > 1,
        ),
        "customers_sharing_address": _measure(
            graph,
            "Customer",
            lambda i, c: customers_sharing_address(graph, i, cost=c),
            len,
            lambda a: len(a) > 0,
        ),
        "claims_of_customer": _measure(
            graph,
            "Customer",
            lambda i, c: claims_of_customer(graph, i, cost=c),
            len,
            lambda a: through(a, 0, "dst") > 1,
        ),
    }


def census(graph: Graph) -> dict[str, Any]:
    """The census of one graph: counts of nodes and edges by kind, degrees,
    the six questions and the builder's notes on what it could not read."""
    return {
        "label": graph.label,
        "nodes": dict(Counter(n.kind for n in graph.nodes())),
        "edges": dict(Counter(e.kind for e in graph.edges())),
        "degrees": _degrees(graph),
        "questions": _questions(graph),
        "notes": dict(graph.notes),
    }
