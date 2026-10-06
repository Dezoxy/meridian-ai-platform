"""The graph's schema and its container: typed nodes, typed directed edges,
each with the file and field it came from. Plain Python, no dependency.

The kinds are named here and only here. A node prints its kind and its ID
and nothing else, so that a log line or a test failure never carries a field
of the data.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

NODE_KINDS = (
    "Customer",
    "Address",
    "Policy",
    "Asset",
    "Claim",
    "HistoryEntry",
    "Product",
    "Clause",
    "Peril",
)


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """Which kinds an edge kind joins, and where the edge comes from."""

    sources: tuple[str, ...]
    target: str
    origin: str


# Every edge kind, in one place. The first seven come from the data files; the
# last five from the wording text alone.
EDGE_KINDS: dict[str, EdgeSpec] = {
    "holds": EdgeSpec(("Customer",), "Policy", "policies.json holder.email"),
    "lives_at": EdgeSpec(("Customer",), "Address", "policies.json holder.address"),
    "insures": EdgeSpec(
        ("Policy",), "Asset", "policies.json insured_object.registration or address"
    ),
    "claimed_on": EdgeSpec(("Claim",), "Policy", "claims.json policy_number"),
    "had": EdgeSpec(("Policy",), "HistoryEntry", "claim-history.json policy_number"),
    "of_product": EdgeSpec(("Policy",), "Product", "policies.json product"),
    "reported_peril": EdgeSpec(
        ("Claim", "HistoryEntry"), "Peril", "claims.json or claim-history.json peril"
    ),
    "part_of": EdgeSpec(("Clause",), "Product", "the wording's clause heading"),
    "applies_to": EdgeSpec(
        ("Clause",), "Peril", "an exclusion's sentence 'applies to claims for'"
    ),
    "covers": EdgeSpec(
        ("Clause",), "Peril", "the title of a clause in 'What is covered'"
    ),
    "documents_for": EdgeSpec(
        ("Clause",), "Peril", "the title of a clause 'Documents for ...'"
    ),
    "refers_to": EdgeSpec(("Clause",), "Clause", "'clause n.m' in a clause body"),
}


@dataclass(frozen=True, slots=True, repr=False)
class Node:
    """``attrs`` holds only values that are not personal data: dates, amounts,
    codes, numbers. Treat it as read-only."""

    kind: str
    id: str
    source_file: str = field(compare=False)
    source_field: str = field(compare=False)
    attrs: Mapping[str, object] = field(default_factory=dict, compare=False)

    def __repr__(self) -> str:
        return f"{self.kind}({self.id})"


@dataclass(frozen=True, slots=True)
class Edge:
    kind: str
    src: str
    dst: str
    source_file: str = field(compare=False)
    source_field: str = field(compare=False)

    def __repr__(self) -> str:
        return f"{self.kind}({self.src} -> {self.dst})"


class Graph:
    """Nodes by ID (one namespace for all kinds) and directed edges. Built by
    ``add_node`` and ``add_edge``, which refuse a duplicate ID, a missing end
    and an edge whose ends are not of the kinds its kind names. ``notes`` holds
    counts the builder keeps about what it could not read. ``label`` says what
    the graph is, and every number reported from it carries it."""

    def __init__(self, label: str = "committed data") -> None:
        self.label = label
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._out: defaultdict[str, list[Edge]] = defaultdict(list)
        self._in: defaultdict[str, list[Edge]] = defaultdict(list)
        self.notes: dict[str, int] = {}

    def add_node(self, node: Node) -> None:
        if node.kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind {node.kind}")
        if node.id in self._nodes:
            raise ValueError(f"node {node.id} exists")
        self._nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        spec = EDGE_KINDS[edge.kind]
        src, dst = self.node(edge.src), self.node(edge.dst)
        if src.kind not in spec.sources or dst.kind != spec.target:
            raise ValueError(f"{edge.kind} does not join {edge.src} and {edge.dst}")
        self._edges.append(edge)
        self._out[edge.src].append(edge)
        self._in[edge.dst].append(edge)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def node(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def nodes(self, kind: str | None = None) -> Iterator[Node]:
        return (n for n in self._nodes.values() if kind is None or n.kind == kind)

    def edges(self, kind: str | None = None) -> Iterator[Edge]:
        return (e for e in self._edges if kind is None or e.kind == kind)

    def out_edges(self, node_id: str, kinds: Iterable[str]) -> list[Edge]:
        wanted = set(kinds)
        return [e for e in self._out.get(node_id, ()) if e.kind in wanted]

    def in_edges(self, node_id: str, kinds: Iterable[str]) -> list[Edge]:
        wanted = set(kinds)
        return [e for e in self._in.get(node_id, ()) if e.kind in wanted]
