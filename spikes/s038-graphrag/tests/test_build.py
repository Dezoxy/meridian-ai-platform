"""What the builder makes of the committed files: counts per kind, endpoints,
the identity keys and the wording edges."""

import json
from collections import Counter
from pathlib import Path

import pytest
from claimgraph.build import load_records, names_another_peril
from claimgraph.model import EDGE_KINDS, NODE_KINDS, Edge, Graph, Node
from claimgraph.wording import slug


def test_the_builder_makes_the_node_counts_the_files_hold(graph: Graph) -> None:
    counts = Counter(node.kind for node in graph.nodes())

    assert dict(counts) == {
        "Customer": 50,
        "Address": 49,
        "Policy": 50,
        "Asset": 50,
        "Claim": 40,
        "HistoryEntry": 44,
        "Product": 4,
        "Clause": 85,
        "Peril": 10,
    }
    assert set(counts) == set(NODE_KINDS)


def test_the_builder_makes_the_edge_counts_the_files_and_wordings_hold(
    graph: Graph,
) -> None:
    counts = Counter(edge.kind for edge in graph.edges())

    assert dict(counts) == {
        "holds": 50,
        "lives_at": 50,
        "insures": 50,
        "claimed_on": 40,
        "had": 44,
        "of_product": 50,
        "reported_peril": 84,
        "part_of": 85,
        "applies_to": 17,
        "covers": 17,
        "documents_for": 17,
        "refers_to": 17,
    }
    assert set(counts) == set(EDGE_KINDS)


def test_every_edge_joins_the_kinds_its_kind_names(graph: Graph) -> None:
    wrong = 0
    for edge in graph.edges():
        spec = EDGE_KINDS[edge.kind]
        wrong += graph.node(edge.src).kind not in spec.sources
        wrong += graph.node(edge.dst).kind != spec.target

    assert wrong == 0


def test_the_graph_refuses_an_edge_between_the_wrong_kinds() -> None:
    small = Graph()
    small.add_node(Node("Policy", "POL-0001", "policies.json", "policy_number"))
    small.add_node(Node("Claim", "CLM-0001", "claims.json", "claim_id"))
    wrong_way_round = Edge("claimed_on", "POL-0001", "CLM-0001", "claims.json", "x")

    with pytest.raises(ValueError, match="does not join"):
        small.add_edge(wrong_way_round)


def test_every_node_and_edge_names_its_file_and_field(graph: Graph) -> None:
    nameless_nodes = sum(
        1 for n in graph.nodes() if not (n.source_file and n.source_field)
    )
    nameless_edges = sum(
        1 for e in graph.edges() if not (e.source_file and e.source_field)
    )

    assert nameless_nodes == 0
    assert nameless_edges == 0
    assert graph.node("POL-0001").source_file == "policies.json"
    assert graph.node("CLM-0001").source_file == "claims.json"
    assert graph.node("HIST-0001").source_file == "claim-history.json"
    assert graph.node("HOME-STD/2.2").source_file == "wordings/HOME-STD.md"


def test_every_refers_to_target_exists_and_is_in_the_same_product(
    graph: Graph,
) -> None:
    dangling = 0
    crossing = 0
    for edge in graph.edges("refers_to"):
        dangling += not graph.has_node(edge.dst)
        crossing += graph.has_node(edge.dst) and (
            graph.node(edge.dst).attrs["product"]
            != graph.node(edge.src).attrs["product"]
        )

    assert dangling == 0
    assert crossing == 0
    assert graph.notes["references_without_a_target"] == 0
    assert graph.notes["references_in_section_introductions"] == 8


def test_a_clause_refers_to_the_clause_its_text_names(graph: Graph) -> None:
    targets = {e.dst for e in graph.out_edges("HOME-STD/2.1", {"refers_to"})}
    capitalised = {e.dst for e in graph.out_edges("HOME-STD/1.5", {"refers_to"})}

    assert targets == {"HOME-STD/4.2"}
    assert capitalised == {"HOME-STD/6.1"}


def test_every_clause_is_part_of_exactly_one_product(graph: Graph) -> None:
    per_clause = Counter(e.src for e in graph.edges("part_of"))

    assert set(per_clause.values()) == {1}
    assert {e.dst for e in graph.out_edges("MOTOR-TPL/2.1", {"part_of"})} == {
        "MOTOR-TPL"
    }
    assert graph.node("HOME-STD").attrs["wording_version"] == "2026-01"
    assert graph.notes["policies_on_another_wording_version"] == 0


def test_an_exclusion_applies_to_the_perils_its_own_sentence_names(
    graph: Graph,
) -> None:
    def perils(clause: str) -> set[str]:
        return {e.dst for e in graph.out_edges(clause, {"applies_to"})}

    assert perils("HOME-STD/3.3") == {"storm", "burst_pipe"}
    assert perils("MOTOR-TPL/3.1") == {
        "collision",
        "theft",
        "fire",
        "glass",
        "storm",
    }
    assert perils("MOTOR-TPL/3.2") == {"third_party_liability"}
    assert perils("HOME-STD/2.2") == set()


def test_a_cover_clause_covers_the_peril_its_title_names(graph: Graph) -> None:
    def covers(clause: str) -> set[str]:
        return {e.dst for e in graph.out_edges(clause, {"covers"})}

    assert covers("HOME-STD/2.3") == {"burst_pipe"}
    assert covers("MOTOR-COMP/2.6") == {"third_party_liability"}
    assert covers("HOME-PLUS/2.6") == {"accidental_damage"}
    assert covers("HOME-STD/3.1") == set()
    assert {e.dst for e in graph.out_edges("HOME-STD/5.4", {"documents_for"})} == {
        "burst_pipe"
    }


def test_the_wording_recognisers_report_what_they_could_not_read(
    graph: Graph,
) -> None:
    assert graph.notes["cover_clauses"] == 17
    assert graph.notes["cover_clauses_without_a_known_peril"] == 0
    assert graph.notes["exclusion_clauses"] == 11
    assert graph.notes["exclusion_clauses_without_a_peril_sentence"] == 0
    assert graph.notes["documents_clauses"] == 17
    assert graph.notes["documents_clauses_without_a_known_peril"] == 0


def test_the_two_burglary_clauses_are_the_cover_clauses_that_name_another_peril(
    data_dir: Path, graph: Graph
) -> None:
    perils = {n.id for n in graph.nodes("Peril")}
    named = {
        f"{wording.product}/{clause.number}"
        for _, wording in load_records(data_dir).wordings
        for clause in wording.clauses
        if clause.is_cover
        and names_another_peril(clause.body, slug(clause.title), perils)
    }

    assert named == {"HOME-STD/2.4", "HOME-PLUS/2.5"}
    assert graph.notes["cover_clauses_naming_another_peril"] == 2


def test_the_customer_key_is_a_digest_of_the_normalised_email(graph: Graph) -> None:
    ids = [n.id for n in graph.nodes("Customer")]
    unlike_digest = sum(
        1
        for i in ids
        if not (
            i.startswith("CUS-")
            and len(i) == 16
            and all(c in "0123456789abcdef" for c in i[4:])
        )
    )

    assert unlike_digest == 0
    assert len(set(ids)) == 50
    holds_per_customer = Counter(e.src for e in graph.edges("holds"))
    assert set(holds_per_customer.values()) == {1}


def test_an_asset_is_a_vehicle_by_registration_or_a_home_by_address(
    graph: Graph,
) -> None:
    families = Counter(n.attrs["family"] for n in graph.nodes("Asset"))
    insured_per_asset = Counter(e.dst for e in graph.edges("insures"))

    assert dict(families) == {"vehicle": 26, "home": 24}
    assert set(insured_per_asset.values()) == {1}


def test_the_only_shared_address_is_the_holder_address_of_two_motor_policies(
    graph: Graph,
) -> None:
    holders_per_address = Counter(e.dst for e in graph.edges("lives_at"))
    shared = [a for a, n in holders_per_address.items() if n > 1]

    assert len(shared) == 1
    customers = {e.src for e in graph.in_edges(shared[0], {"lives_at"})}
    policies = {e.dst for c in customers for e in graph.out_edges(c, {"holds"})}
    assert policies == {"POL-0016", "POL-0024"}


def test_how_often_policies_share_each_candidate_key_in_the_raw_files(
    data_dir: Path,
) -> None:
    rows = json.loads((data_dir / "policies.json").read_text(encoding="utf-8"))
    claims = json.loads((data_dir / "claims.json").read_text(encoding="utf-8"))
    by_policy = {p["policy_number"]: p for p in rows}

    def shared(values: list[object]) -> tuple[int, int]:
        """(distinct values, policies on a value that more than one policy has)"""
        counts = Counter(values)
        return len(counts), sum(n for n in counts.values() if n > 1)

    holder = [p["holder"] for p in rows]
    insured = [p["insured_object"] for p in rows]
    assert shared([h["email"].strip().lower() for h in holder]) == (50, 0)
    assert shared([" ".join(h["name"].lower().split()) for h in holder]) == (50, 0)
    assert shared([(h["name"].lower(), repr(h["address"])) for h in holder]) == (50, 0)
    assert shared([repr(h["address"]) for h in holder]) == (49, 2)
    assert shared([i["registration"] for i in insured if "registration" in i]) == (
        26,
        0,
    )
    assert shared([repr(i["address"]) for i in insured if "address" in i]) == (24, 0)
    at_home = sum(
        1 for p in rows if p["insured_object"].get("address") == p["holder"]["address"]
    )
    assert at_home == 24
    same_person = sum(
        1
        for c in claims
        if c["claimant"]["email"] == by_policy[c["policy_number"]]["holder"]["email"]
    )
    assert same_person == len(claims) == 40
