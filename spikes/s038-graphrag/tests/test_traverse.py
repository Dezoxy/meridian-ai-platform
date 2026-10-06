"""The traversal API: edge kinds followed, hops, paths, and the cost counts."""

from claimgraph.model import Graph
from claimgraph.traverse import Cost, follow


def _ids(reached: list) -> list[str]:
    return [r.node.id for r in reached]


def test_follow_goes_out_along_the_named_kinds_and_returns_the_path(
    graph: Graph,
) -> None:
    reached = follow(graph, "CLM-0006", out={"claimed_on", "of_product"}, max_hops=2)

    assert _ids(reached) == ["POL-0004", "HOME-STD"]
    assert [r.hops for r in reached] == [1, 2]
    assert [e.kind for e in reached[1].path] == ["claimed_on", "of_product"]
    assert [repr(e) for e in reached[1].path] == [
        "claimed_on(CLM-0006 -> POL-0004)",
        "of_product(POL-0004 -> HOME-STD)",
    ]


def test_follow_goes_against_the_direction_of_the_kinds_named_as_incoming(
    graph: Graph,
) -> None:
    reached = follow(graph, "POL-0004", in_={"claimed_on"}, max_hops=1)

    assert _ids(reached) == ["CLM-0006"]


def test_follow_stops_at_the_hop_limit_and_ignores_other_kinds(graph: Graph) -> None:
    one_hop = follow(graph, "CLM-0006", out={"claimed_on", "of_product"}, max_hops=1)
    other_kind = follow(graph, "CLM-0006", out={"had"}, max_hops=3)

    assert _ids(one_hop) == ["POL-0004"]
    assert other_kind == []


def test_follow_reaches_a_node_once_by_its_shortest_path(graph: Graph) -> None:
    reached = follow(graph, "HOME-STD/2.1", out={"refers_to", "part_of"}, max_hops=3)

    ids = _ids(reached)
    assert len(ids) == len(set(ids))
    assert dict(zip(ids, (r.hops for r in reached), strict=True))["HOME-STD"] == 1


def test_follow_counts_the_nodes_it_visits_and_the_edges_it_follows(
    graph: Graph,
) -> None:
    cost = Cost()

    follow(graph, "CLM-0006", out={"claimed_on", "of_product"}, max_hops=2, cost=cost)

    assert cost == Cost(nodes_visited=3, edges_followed=2)


def test_follow_from_an_unknown_node_is_an_error_not_an_empty_answer(
    graph: Graph,
) -> None:
    import pytest

    with pytest.raises(KeyError):
        follow(graph, "CLM-9999", out={"claimed_on"}, max_hops=1)
