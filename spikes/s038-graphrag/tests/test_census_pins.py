"""Every number of the two censuses, pinned. A regenerated data set, a changed
wording or a changed re-linking changes a number here and fails this test; the
fix is to read the diff and say why the number moved, not to paste the new one.
"""

from claimgraph.census import census
from claimgraph.model import Graph

COMMITTED = {
    "label": "committed data",
    "nodes": {
        "Peril": 10,
        "Product": 4,
        "Clause": 85,
        "Policy": 50,
        "Address": 49,
        "Customer": 50,
        "Asset": 50,
        "Claim": 40,
        "HistoryEntry": 44,
    },
    "edges": {
        "part_of": 85,
        "refers_to": 17,
        "covers": 17,
        "applies_to": 17,
        "documents_for": 17,
        "lives_at": 50,
        "holds": 50,
        "insures": 50,
        "of_product": 50,
        "claimed_on": 40,
        "reported_peril": 84,
        "had": 44,
    },
    "degrees": {
        "Peril": {
            "reported_peril.in": {3: 2, 6: 2, 8: 1, 9: 1, 10: 2, 11: 1, 18: 1},
            "applies_to.in": {0: 1, 1: 6, 3: 1, 4: 2},
            "covers.in": {1: 5, 2: 3, 3: 2},
            "documents_for.in": {1: 5, 2: 3, 3: 2},
        },
        "Product": {
            "of_product.in": {10: 1, 12: 2, 16: 1},
            "part_of.in": {14: 1, 22: 1, 24: 1, 25: 1},
        },
        "Clause": {
            "part_of.out": {1: 85},
            "applies_to.out": {0: 74, 1: 8, 2: 2, 5: 1},
            "covers.out": {0: 68, 1: 17},
            "documents_for.out": {0: 68, 1: 17},
            "refers_to.out": {0: 68, 1: 17},
            "refers_to.in": {0: 73, 1: 8, 2: 3, 3: 1},
        },
        "Policy": {
            "holds.in": {1: 50},
            "insures.out": {1: 50},
            "claimed_on.in": {0: 10, 1: 40},
            "had.out": {0: 24, 1: 10, 2: 14, 3: 2},
            "of_product.out": {1: 50},
        },
        "Address": {"lives_at.in": {1: 48, 2: 1}},
        "Customer": {"holds.out": {1: 50}, "lives_at.out": {1: 50}},
        "Asset": {"insures.in": {1: 50}},
        "Claim": {"claimed_on.out": {1: 40}, "reported_peril.out": {1: 40}},
        "HistoryEntry": {"had.in": {1: 44}, "reported_peril.out": {1: 44}},
    },
    "questions": {
        "clauses_bearing_on_claim": {
            "starts": 40,
            "non_trivial": 37,
            "answer_sizes": {1: 3, 2: 15, 3: 6, 4: 10, 5: 6},
            "cost": {"nodes_visited": 1457, "edges_followed": 1225},
        },
        "events_before_loss": {
            "starts": 40,
            "non_trivial": 10,
            "answer_sizes": {0: 30, 1: 8, 3: 2},
            "cost": {"nodes_visited": 112, "edges_followed": 72},
        },
        "policies_of_customer": {
            "starts": 50,
            "non_trivial": 0,
            "answer_sizes": {1: 50},
            "cost": {"nodes_visited": 100, "edges_followed": 50},
        },
        "claims_on_asset": {
            "starts": 50,
            "non_trivial": 0,
            "answer_sizes": {0: 3, 1: 23, 2: 13, 3: 9, 4: 2},
            "cost": {"nodes_visited": 184, "edges_followed": 134},
        },
        "customers_sharing_address": {
            "starts": 50,
            "non_trivial": 2,
            "answer_sizes": {0: 48, 1: 2},
            "cost": {"nodes_visited": 102, "edges_followed": 102},
        },
        "claims_of_customer": {
            "starts": 50,
            "non_trivial": 0,
            "answer_sizes": {0: 10, 1: 40},
            "cost": {"nodes_visited": 140, "edges_followed": 90},
        },
    },
    "notes": {
        "cover_clauses": 17,
        "cover_clauses_without_a_known_peril": 0,
        "cover_clauses_naming_another_peril": 2,
        "exclusion_clauses": 11,
        "exclusion_clauses_without_a_peril_sentence": 0,
        "documents_clauses": 17,
        "documents_clauses_without_a_known_peril": 0,
        "references_without_a_target": 0,
        "references_in_section_introductions": 8,
        "policies_on_another_wording_version": 0,
    },
}

SIMULATED_VARIANT = {
    "label": "simulated variant (seed 38)",
    "nodes": {
        "Peril": 10,
        "Product": 4,
        "Clause": 85,
        "Policy": 50,
        "Address": 39,
        "Customer": 40,
        "Asset": 43,
        "Claim": 40,
        "HistoryEntry": 44,
    },
    "edges": {
        "part_of": 85,
        "refers_to": 17,
        "covers": 17,
        "applies_to": 17,
        "documents_for": 17,
        "lives_at": 40,
        "holds": 50,
        "insures": 50,
        "of_product": 50,
        "claimed_on": 40,
        "reported_peril": 84,
        "had": 44,
    },
    "degrees": {
        "Peril": COMMITTED["degrees"]["Peril"],
        "Product": COMMITTED["degrees"]["Product"],
        "Clause": COMMITTED["degrees"]["Clause"],
        "Policy": COMMITTED["degrees"]["Policy"],
        "Address": {"lives_at.in": {1: 38, 2: 1}},
        "Customer": {"holds.out": {1: 32, 2: 6, 3: 2}, "lives_at.out": {1: 40}},
        "Asset": {"insures.in": {1: 36, 2: 7}},
        "Claim": COMMITTED["degrees"]["Claim"],
        "HistoryEntry": COMMITTED["degrees"]["HistoryEntry"],
    },
    "questions": {
        "clauses_bearing_on_claim": {
            "starts": 40,
            "non_trivial": 37,
            "answer_sizes": {1: 3, 2: 15, 3: 6, 4: 10, 5: 6},
            "cost": {"nodes_visited": 1457, "edges_followed": 1225},
        },
        "events_before_loss": {
            "starts": 40,
            "non_trivial": 10,
            "answer_sizes": {0: 30, 1: 8, 3: 2},
            "cost": {"nodes_visited": 112, "edges_followed": 72},
        },
        "policies_of_customer": {
            "starts": 40,
            "non_trivial": 8,
            "answer_sizes": {1: 32, 2: 6, 3: 2},
            "cost": {"nodes_visited": 90, "edges_followed": 50},
        },
        "claims_on_asset": {
            "starts": 43,
            "non_trivial": 7,
            "answer_sizes": {0: 3, 1: 13, 2: 13, 3: 11, 4: 3},
            "cost": {"nodes_visited": 177, "edges_followed": 134},
        },
        "customers_sharing_address": {
            "starts": 40,
            "non_trivial": 2,
            "answer_sizes": {0: 38, 1: 2},
            "cost": {"nodes_visited": 82, "edges_followed": 82},
        },
        "claims_of_customer": {
            "starts": 40,
            "non_trivial": 7,
            "answer_sizes": {0: 9, 1: 24, 2: 5, 3: 2},
            "cost": {"nodes_visited": 130, "edges_followed": 90},
        },
    },
    "notes": COMMITTED["notes"],
}


def test_every_number_of_the_committed_census_is_pinned(graph: Graph) -> None:
    assert census(graph) == COMMITTED


def test_every_number_of_the_simulated_variant_census_is_pinned(
    variant: Graph,
) -> None:
    assert census(variant) == SIMULATED_VARIANT
