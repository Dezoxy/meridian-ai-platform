"""One explicit traversal: follow these edge kinds from this node, up to n
hops, and return the nodes reached with the path that reached each.

Breadth first, so a node is reached once, by a shortest path; edges leave a
node in the order they were added, so the result is the same on every call.
``out`` kinds are followed from source to target, ``in_`` kinds from target to
source. The cost of a traversal is a count, not a time.
"""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from .model import Edge, Graph, Node


@dataclass(slots=True)
class Cost:
    """Counts a traversal adds up: distinct nodes reached (the start included)
    and the edges examined that match a kind to follow."""

    nodes_visited: int = 0
    edges_followed: int = 0


@dataclass(frozen=True, slots=True)
class Reach:
    """A node reached, the hops it took and the edges walked."""

    node: Node
    hops: int
    path: tuple[Edge, ...]


def follow(
    graph: Graph,
    start: str,
    *,
    out: Iterable[str] = (),
    in_: Iterable[str] = (),
    max_hops: int,
    cost: Cost | None = None,
) -> list[Reach]:
    """The nodes within ``max_hops`` of ``start`` over the named edge kinds,
    nearest first, each with its path. The start is not in the result. Every
    edge examined that matches a kind counts as followed."""
    graph.node(start)  # a start that is not in the graph is an error
    out_kinds, in_kinds = tuple(out), tuple(in_)
    seen = {start}
    reached: list[Reach] = []
    queue: deque[tuple[str, int, tuple[Edge, ...]]] = deque([(start, 0, ())])
    followed = 0
    while queue:
        here, hops, path = queue.popleft()
        if hops == max_hops:
            continue
        steps = [(e, e.dst) for e in graph.out_edges(here, out_kinds)]
        steps += [(e, e.src) for e in graph.in_edges(here, in_kinds)]
        for edge, there in steps:
            followed += 1
            if there in seen:
                continue
            seen.add(there)
            reach = Reach(graph.node(there), hops + 1, (*path, edge))
            reached.append(reach)
            queue.append((there, hops + 1, reach.path))
    if cost is not None:
        cost.nodes_visited += len(seen)
        cost.edges_followed += followed
    return reached
