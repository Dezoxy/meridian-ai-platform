"""The simulated variant: a seeded re-linking of the same 50 policies, built in
memory. It differs from the committed graph where it is meant to and nowhere
else, and it is the same on every call."""

from collections import Counter
from pathlib import Path

from claimgraph.census import census
from claimgraph.model import Graph
from claimgraph.variant import VARIANT_SEED, build_variant

KEPT_NODE_KINDS = ("Policy", "Claim", "HistoryEntry", "Product", "Clause", "Peril")
KEPT_EDGE_KINDS = (
    "claimed_on",
    "had",
    "of_product",
    "reported_peril",
    "part_of",
    "applies_to",
    "covers",
    "documents_for",
    "refers_to",
)
RELINKED_EDGE_KINDS = ("holds", "lives_at", "insures")


def _signature(graph: Graph, kinds: tuple[str, ...]) -> set[tuple[str, str, str]]:
    return {(e.kind, e.src, e.dst) for e in graph.edges() if e.kind in kinds}


def test_the_variant_is_the_same_on_every_call_and_for_the_same_seed(
    data_dir: Path, variant: Graph
) -> None:
    again = build_variant(data_dir)

    assert {n.id for n in again.nodes()} == {n.id for n in variant.nodes()}
    assert _signature(again, RELINKED_EDGE_KINDS) == _signature(
        variant, RELINKED_EDGE_KINDS
    )
    assert VARIANT_SEED == 38


def test_another_seed_links_the_same_policies_differently(
    data_dir: Path, variant: Graph
) -> None:
    other = build_variant(data_dir, seed=39)

    assert _signature(other, RELINKED_EDGE_KINDS) != _signature(
        variant, RELINKED_EDGE_KINDS
    )
    assert _signature(other, KEPT_EDGE_KINDS) == _signature(variant, KEPT_EDGE_KINDS)


def test_the_variant_keeps_everything_but_the_links_of_holders_and_assets(
    graph: Graph, variant: Graph
) -> None:
    for kind in KEPT_NODE_KINDS:
        assert {n.id for n in variant.nodes(kind)} == {n.id for n in graph.nodes(kind)}
    assert _signature(variant, KEPT_EDGE_KINDS) == _signature(graph, KEPT_EDGE_KINDS)
    assert variant.notes == graph.notes


def test_the_variant_differs_from_the_committed_graph_in_holders_and_assets(
    graph: Graph, variant: Graph
) -> None:
    assert {n.id for n in variant.nodes("Customer")} != {
        n.id for n in graph.nodes("Customer")
    }
    assert _signature(variant, ("holds",)) != _signature(graph, ("holds",))
    assert _signature(variant, ("insures",)) != _signature(graph, ("insures",))


def test_in_the_variant_some_customers_hold_two_or_three_policies(
    variant: Graph,
) -> None:
    held = Counter(Counter(e.src for e in variant.edges("holds")).values())

    assert set(held) == {1, 2, 3}
    assert sum(k * n for k, n in held.items()) == 50


def test_in_the_variant_some_assets_insure_more_than_one_policy_with_events(
    variant: Graph,
) -> None:
    per_asset = Counter(e.dst for e in variant.edges("insures"))
    shared = [a for a, n in per_asset.items() if n > 1]
    eventful = 0
    for asset in shared:
        policies = [e.src for e in variant.in_edges(asset, {"insures"})]
        with_events = sum(
            1
            for p in policies
            if variant.in_edges(p, {"claimed_on"}) or variant.out_edges(p, {"had"})
        )
        eventful += with_events > 1

    assert len(shared) == 7
    assert eventful == 7


def test_the_variant_gives_the_relational_questions_something_to_return(
    variant: Graph,
) -> None:
    questions = census(variant)["questions"]

    assert questions["policies_of_customer"]["non_trivial"] > 0
    assert questions["claims_on_asset"]["non_trivial"] > 0
    assert questions["claims_of_customer"]["non_trivial"] > 0


def test_every_number_of_the_variant_is_labelled_simulated(variant: Graph) -> None:
    assert census(variant)["label"] == "simulated variant (seed 38)"
    assert variant.label == "simulated variant (seed 38)"


def test_the_variant_writes_nothing_and_keeps_the_data_directory_as_it_was(
    data_dir: Path,
) -> None:
    before = sorted((p.name, p.stat().st_mtime_ns) for p in data_dir.rglob("*"))

    build_variant(data_dir)

    assert sorted((p.name, p.stat().st_mtime_ns) for p in data_dir.rglob("*")) == before
