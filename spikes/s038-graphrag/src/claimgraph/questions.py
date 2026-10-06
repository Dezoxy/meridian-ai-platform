"""The named questions the graph is built to answer, each one a short
traversal. The answer to each is a list of nodes with the path that reached
them; none reads a file, and none looks at a name, an e-mail or an address.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from .model import Edge, Graph, Node
from .traverse import Cost, Reach, follow

FREQUENT_CLAIMS_WINDOW_DAYS = 365  # the platform's own window (rules.py)

# Where a clause stands among the ones of equal hop count: the cover for the
# peril first, then an exclusion that names it, then its documents, then what
# the others refer to.
RELATION_ORDER = ("covers", "applies_to", "documents_for", "refers_to")
PERIL_RELATIONS = ("covers", "applies_to", "documents_for")
TIE_BREAK = "ties by relation order, then clause number"


@dataclass(frozen=True, slots=True)
class BearingClause:
    """One clause in the ordered answer: how far from the claim it is, by which
    relation it was reached and the edges walked."""

    clause: Node
    hops: int
    relation: str
    via: tuple[Edge, ...]

    @property
    def reason(self) -> str:
        kinds = ", ".join(edge.kind for edge in self.via)
        return f"{self.hops} hops via {kinds}; {TIE_BREAK}"


@dataclass(frozen=True, slots=True)
class PriorEvents:
    """Claims and history entries of one policy inside a window."""

    claims: list[Node] = field(default_factory=list)
    history: list[Node] = field(default_factory=list)


def _require(graph: Graph, node_id: str, kind: str) -> None:
    if graph.node(node_id).kind != kind:
        raise ValueError(f"{node_id} is not a {kind}")


def _clause_order(clause: Node) -> tuple[int, int]:
    section, _, number = str(clause.attrs["number"]).partition(".")
    return int(section), int(number)


def _rank(found: BearingClause) -> tuple[int, int, tuple[int, int]]:
    return (
        found.hops,
        RELATION_ORDER.index(found.relation),
        _clause_order(found.clause),
    )


def clauses_bearing_on_claim(
    graph: Graph, claim_id: str, *, top: int | None = None, cost: Cost | None = None
) -> list[BearingClause]:
    """Which clauses of the claim's policy wording bear on this claim, nearest
    first? A clause bears on it when it covers the claim's peril, is an
    exclusion that names it, or lists the documents for it, and it belongs to
    the product of the claim's policy (two hops from the claim); and when one
    of those clauses refers to it (three hops). Equal hops are ordered by
    relation (covers, applies_to, documents_for, refers_to), then clause
    number. ``top`` keeps that many places from the front."""
    _require(graph, claim_id, "Claim")
    to_product = follow(
        graph, claim_id, out=("claimed_on", "of_product"), max_hops=2, cost=cost
    )
    product = next(r.node.id for r in to_product if r.node.kind == "Product")
    of_product = {
        r.node.id
        for r in follow(graph, product, in_=("part_of",), max_hops=1, cost=cost)
    }
    by_peril = follow(
        graph,
        claim_id,
        out=("reported_peril",),
        in_=PERIL_RELATIONS,
        max_hops=2,
        cost=cost,
    )
    direct = [
        BearingClause(r.node, r.hops, r.path[-1].kind, r.path)
        for r in by_peril
        if r.node.kind == "Clause" and r.node.id in of_product
    ]
    found = {b.clause.id: b for b in direct}
    for b in sorted(direct, key=_rank):
        for ref in follow(
            graph, b.clause.id, out=("refers_to",), max_hops=1, cost=cost
        ):
            found.setdefault(
                ref.node.id,
                BearingClause(ref.node, b.hops + 1, "refers_to", (*b.via, *ref.path)),
            )
    ranked = sorted(found.values(), key=_rank)
    return ranked if top is None else ranked[:top]


def events_before_loss(
    graph: Graph,
    policy_id: str,
    loss_date: date,
    *,
    window_days: int = FREQUENT_CLAIMS_WINDOW_DAYS,
    cost: Cost | None = None,
) -> PriorEvents:
    """Which claims and history entries does this policy have in the 365 days
    before this loss date (the first day included, the loss date itself not)?
    One hop from the policy; the rule T-76's `frequent_claims` reads."""
    _require(graph, policy_id, "Policy")
    first = loss_date - timedelta(days=window_days)
    events = follow(
        graph, policy_id, out=("had",), in_=("claimed_on",), max_hops=1, cost=cost
    )
    inside = [
        r.node
        for r in events
        if first <= date.fromisoformat(str(r.node.attrs["loss_date"])) < loss_date
    ]
    inside.sort(key=lambda n: (str(n.attrs["loss_date"]), n.id))
    return PriorEvents(
        claims=[n for n in inside if n.kind == "Claim"],
        history=[n for n in inside if n.kind == "HistoryEntry"],
    )


def policies_of_customer(
    graph: Graph, customer_id: str, *, cost: Cost | None = None
) -> list[Node]:
    """Which policies does this customer hold? One hop from the customer."""
    _require(graph, customer_id, "Customer")
    reached = follow(graph, customer_id, out=("holds",), max_hops=1, cost=cost)
    return [r.node for r in reached]


def claims_on_asset(
    graph: Graph, asset_id: str, *, cost: Cost | None = None
) -> list[Reach]:
    """Which claims and history entries are there on this asset, through every
    policy that insures it? Two hops: asset to policy to the event."""
    _require(graph, asset_id, "Asset")
    reached = follow(
        graph,
        asset_id,
        out=("had",),
        in_=("insures", "claimed_on"),
        max_hops=2,
        cost=cost,
    )
    return [r for r in reached if r.node.kind in ("Claim", "HistoryEntry")]


def customers_sharing_address(
    graph: Graph, customer_id: str, *, cost: Cost | None = None
) -> list[Node]:
    """Which other customers live at this customer's address? Two hops: to the
    address and back to its other customers."""
    _require(graph, customer_id, "Customer")
    reached = follow(
        graph,
        customer_id,
        out=("lives_at",),
        in_=("lives_at",),
        max_hops=2,
        cost=cost,
    )
    return [r.node for r in reached if r.node.kind == "Customer"]


def claims_of_customer(
    graph: Graph, customer_id: str, *, cost: Cost | None = None
) -> list[Reach]:
    """Which claims does this customer have, across all of their policies? Two
    hops: customer to policy to claim."""
    _require(graph, customer_id, "Customer")
    reached = follow(
        graph,
        customer_id,
        out=("holds",),
        in_=("claimed_on",),
        max_hops=2,
        cost=cost,
    )
    return [r for r in reached if r.node.kind == "Claim"]
