"""Every number of the results file, pinned, and the checks that the file and the
README hold nothing they should not. The numbers are read from the file the
database run wrote (`test_results_file.py` shows the file is what a fresh run
gives); a number that moves is a decision for a person, not a regeneration."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from comparison import score, tables

SPIKE = Path(__file__).resolve().parents[1]
README = SPIKE / "README.md"
RESULTS = SPIKE / "results" / "comparison.json"
RULE_HEADING = "## The decision rule, set before anything was compared"
# The rule's section as it is on the step branch when this part began: from its
# heading to the next "## " heading, trailing blank lines dropped.
RULE_SHA256 = "982e9408c9833a828e0749ead759a049661fbd21720cd5509b0128a2071f75be"
RULE_LINES = 48
RANKED = ("graph", "keyword", "vector", "fused")


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    return json.loads(RESULTS.read_text(encoding="utf-8"))


def test_the_label_sets_hold_the_counted_clauses(results: dict[str, Any]) -> None:
    recall = results["recall"]

    assert {s: (recall[s]["claims"], recall[s]["labelled"]) for s in recall} == {
        "narrative": (28, 28),
        "exclusion": (8, 8),
        "all": (40, 63),
    }


def test_recall_of_each_ranking_on_each_label_set_at_1_3_5_and_10(
    results: dict[str, Any],
) -> None:
    hits = {
        s: {r: results["recall"][s][r]["hits"] for r in (*RANKED, "chance")}
        for s in score.LABEL_SETS
    }

    assert hits["narrative"] == {
        "graph": [23, 27, 28, 28],
        "keyword": [14, 19, 21, 22],
        "vector": [3, 9, 13, 19],
        "fused": [11, 19, 20, 23],
        "chance": [1.33, 3.99, 6.65, 13.31],
    }
    assert hits["exclusion"] == {
        "graph": [3, 7, 8, 8],
        "keyword": [3, 4, 5, 5],
        "vector": [0, 0, 1, 1],
        "fused": [1, 2, 3, 5],
        "chance": [0.4, 1.2, 2.0, 4.01],
    }
    assert hits["all"] == {
        "graph": [23, 31, 34, 34],
        "keyword": [14, 23, 26, 30],
        "vector": [3, 14, 28, 41],
        "fused": [11, 20, 28, 36],
        "chance": [2.98, 8.93, 14.89, 29.78],
    }


def test_the_shares_are_the_hits_over_the_labelled_clauses(
    results: dict[str, Any],
) -> None:
    for table in results["recall"].values():
        for ranking in (*RANKED, "chance"):
            for hit, share in zip(
                table[ranking]["hits"], table[ranking]["share"], strict=True
            ):
                # chance shows its hits to 2 places, its share from the exact
                # value, so a share may differ by half a hundredth of a clause
                slack = 0.005 / table["labelled"] + 5e-5
                assert share == pytest.approx(hit / table["labelled"], abs=slack)


def test_the_production_set_holds_every_labelled_clause(
    results: dict[str, Any],
) -> None:
    held = {s: results["recall"][s]["production"]["hits"] for s in score.LABEL_SETS}

    assert held == {"narrative": 28, "exclusion": 8, "all": 63}
    assert results["list_sizes"]["production"] == {
        "claims": 40,
        "sizes": {"6": 3, "7": 18, "8": 9, "9": 4, "10": 6},
    }


def test_the_list_sizes_and_how_often_a_list_is_shorter_than_the_cut_off(
    results: dict[str, Any],
) -> None:
    sizes = results["list_sizes"]

    assert sizes["graph"]["sizes"] == {"1": 3, "2": 15, "3": 6, "4": 10, "5": 6}
    assert sizes["graph"]["shorter_than_k"] == {"1": 0, "3": 18, "5": 34, "10": 40}
    assert sizes["keyword"]["sizes"] == {
        "2": 3,
        "3": 1,
        "4": 3,
        "5": 1,
        "6": 2,
        "7": 2,
        "8": 1,
        "9": 4,
        "10": 23,
    }
    assert sizes["keyword"]["shorter_than_k"] == {"1": 0, "3": 3, "5": 7, "10": 17}
    assert sizes["vector"]["sizes"] == sizes["fused"]["sizes"] == {"10": 40}


def test_a_graph_list_in_the_file_is_never_padded(results: dict[str, Any]) -> None:
    for claim, lists in results["lists"].items():
        assert len(lists["graph"]) == len(set(lists["graph"])), claim
        assert 1 <= len(lists["graph"]) <= 5, claim


def test_wins_ties_and_losses_of_the_graph_against_fused_and_keyword(
    results: dict[str, Any],
) -> None:
    def tally(label_set: str) -> dict[str, dict[str, list[int]]]:
        return {
            pair: {rank: [t["win"], t["tie"], t["loss"]] for rank, t in by_rank.items()}
            for pair, by_rank in results["wins"][label_set].items()
        }

    assert tally("narrative") == {
        "graph_vs_fused": {"5": [8, 20, 0], "10": [5, 23, 0]},
        "graph_vs_keyword": {"5": [7, 21, 0], "10": [6, 22, 0]},
    }
    assert tally("exclusion") == {
        "graph_vs_fused": {"5": [5, 3, 0], "10": [3, 5, 0]},
        "graph_vs_keyword": {"5": [3, 5, 0], "10": [3, 5, 0]},
    }
    assert tally("all") == {
        "graph_vs_fused": {"5": [10, 27, 3], "10": [6, 27, 7]},
        "graph_vs_keyword": {"5": [9, 30, 1], "10": [7, 31, 2]},
    }


def test_wins_ties_and_losses_add_up_to_the_queries_of_the_label_set(
    results: dict[str, Any],
) -> None:
    for label_set, queries in (("narrative", 28), ("exclusion", 8), ("all", 40)):
        for by_rank in results["wins"][label_set].values():
            for t in by_rank.values():
                assert t["win"] + t["tie"] + t["loss"] == queries, label_set


def test_point_two_the_graph_against_the_production_lookup_claim_by_claim(
    results: dict[str, Any],
) -> None:
    on_all = results["point2"]["all"]
    differences = {
        claim: (p["graph_only"], p["production_only"])
        for claim, p in on_all["per_claim"].items()
        if p["graph_only"] or p["production_only"]
    }

    assert {k: v for k, v in on_all.items() if k != "per_claim"} == {
        "claims": 40,
        "identical": 14,
        "graph_finds_more": 0,
        "graph_finds_fewer": 26,
        "graph_holds": 34,
        "production_holds": 63,
    }
    assert len(on_all["per_claim"]) == 40
    assert all(not graph_only for graph_only, _ in differences.values())
    assert differences["CLM-0012"] == ([], ["4.1", "5.1"])
    assert differences["CLM-0024"] == ([], ["4.1", "4.2"])
    assert differences["CLM-0014"] == ([], ["6.1"])
    assert sum(len(p) for _, p in differences.values()) == 63 - 34
    on_narrative = results["point2"]["narrative"]
    assert on_narrative["identical"] == on_narrative["claims"] == 28
    assert on_narrative["graph_finds_more"] == on_narrative["graph_finds_fewer"] == 0


def test_the_labelled_clauses_no_ranking_finds_at_ten(results: dict[str, Any]) -> None:
    found = [(u["claim"], u["clause"]) for u in results["unfound_at_10"]]

    assert found == [
        ("CLM-0002", "6.2"),
        ("CLM-0016", "4.1"),
        ("CLM-0017", "6.2"),
        ("CLM-0018", "6.1"),
        ("CLM-0025", "4.1"),
        ("CLM-0028", "4.1"),
        ("CLM-0034", "4.1"),
        ("CLM-0039", "6.1"),
    ]
    assert not [u for u in results["unfound_at_10"] if u["in_sets"]]


def test_no_unfound_clause_is_in_a_list_in_the_file(results: dict[str, Any]) -> None:
    for u in results["unfound_at_10"]:
        for ranking in RANKED:
            assert u["clause"] not in results["lists"][u["claim"]][ranking][:10]


def test_the_costs_as_counts(results: dict[str, Any]) -> None:
    graph, search = results["cost"]["graph"], results["cost"]["search"]

    assert (graph["nodes_held"], graph["edges_held"]) == (382, 521)
    assert graph["per_query_nodes_visited"] == {"median": 36.0, "max": 42}
    assert graph["per_query_edges_followed"] == {"median": 31.0, "max": 37}
    assert graph["source_lines_total"] == sum(graph["source_lines"].values()) == 921
    assert graph["parser_notes"]["cover_clauses_without_a_known_peril"] == 0
    assert graph["parser_notes"]["documents_clauses_without_a_known_peril"] == 0
    assert graph["parser_notes"]["exclusion_clauses_without_a_peril_sentence"] == 0
    assert search == {
        "queries": 40,
        "rows_in_store": 85,
        "rows_in_scope_per_query": {"median": 24.0, "max": 25, "min": 14},
        "embeddings_per_query": 1,
        "gateway_calls_for_all_queries": 3,
    }


def test_the_rule_applied_to_the_numbers(results: dict[str, Any]) -> None:
    rule = results["rule"]

    assert {r: rule["point1"][r]["holds"] for r in rule["point1"]} == {
        "all": False,
        "narrative": True,
    }
    failed = [
        (c["label_set"], c["rank"], c["against"])
        for c in rule["point1"]["all"]["checks"]
        if not (c["graph_finds_more"] and c["wins_at_least_losses"])
    ]
    assert failed == [("all", 10, "fused")]
    assert {r: rule["point2"][r]["holds"] for r in rule["point2"]} == {
        "all": False,
        "narrative": False,
    }
    assert rule["point3"] == {"counted_needs": [], "holds": False}
    assert rule["outcome"] == {"all": "no", "narrative": "not now"}
    assert rule["readings_agree"] is False


def test_the_needs_are_the_four_the_documents_write_down(
    results: dict[str, Any],
) -> None:
    found = {
        n["need"]: (n["question"], n["hops"], n["non_trivial"], n["starts"])
        for n in results["needs"]
    }

    assert found == {
        "frequent_claims": ("events_before_loss", 1, 10, 40),
        "open_at_the_same_time": (
            "events_before_loss (the claims it returns)",
            1,
            0,
            40,
        ),
        "holder_of_a_policy": ("policies_of_customer", 1, 0, 50),
        "terms_of_a_claim": ("clauses_bearing_on_claim", 3, 37, 40),
    }


def test_each_need_is_written_where_the_file_says() -> None:
    repo = SPIKE.parents[1]
    results = json.loads(RESULTS.read_text(encoding="utf-8"))

    for need in results["needs"]:
        for where in need["where"]:
            text = (repo / where["file"]).read_text(encoding="utf-8")
            assert where["phrase"] in text, (need["need"], where["file"])


def test_the_vector_and_fused_rows_say_simulated_embedding(
    results: dict[str, Any],
) -> None:
    assert "simulated embedding" in results["embedding"]
    assert "simulated embedding" in tables.SEARCH_NAMES["vector"]
    assert "simulated embedding" in tables.SEARCH_NAMES["fused"]
    assert "simulated embedding" not in tables.SEARCH_NAMES["graph"]


def _personal_strings(data_dir: Path) -> set[str]:
    policies = json.loads((data_dir / "policies.json").read_text(encoding="utf-8"))
    claims = json.loads((data_dir / "claims.json").read_text(encoding="utf-8"))
    found: set[str] = set()
    for policy in policies:
        holder = policy["holder"]
        found |= {holder["name"], holder["email"], holder["address"]["street"]}
    for claim in claims:
        found |= {claim["claimant"]["name"], claim["claimant"]["email"]}
        found |= {claim["description"], claim["description"][:40]}
    return {s for s in found if s}


def test_the_results_and_the_readme_hold_no_description_and_no_name(
    data_dir: Path,
) -> None:
    strings = _personal_strings(data_dir)
    blob = RESULTS.read_text(encoding="utf-8") + README.read_text(encoding="utf-8")

    offenders = sum(1 for s in strings if s in blob)

    assert len(strings) > 100
    assert offenders == 0


def _rule_section(text: str) -> str:
    lines = text.split("\n")
    start = lines.index(RULE_HEADING)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end]).rstrip("\n")


def test_the_decision_rule_section_is_the_one_set_down_before_the_comparison() -> None:
    section = _rule_section(README.read_text(encoding="utf-8"))

    assert len(section.split("\n")) == RULE_LINES
    assert hashlib.sha256(section.encode()).hexdigest() == RULE_SHA256


def test_the_rest_of_the_readme_comes_after_the_rule_in_the_order_asked() -> None:
    headings = [
        line
        for line in README.read_text(encoding="utf-8").split("\n")
        if line.startswith("## ")
    ]

    tail = headings[headings.index(RULE_HEADING) :]

    assert tail == [
        RULE_HEADING,
        "## The comparison",
        "## What the rule says",
        "## What this does not show",
        "## Review",
    ]


def test_the_review_section_is_empty_but_for_one_line() -> None:
    text = README.read_text(encoding="utf-8")

    review = text.split("\n## Review\n", 1)[1].strip()

    assert review == "Filled in by the main session after the review."


def _parse_tables(text: str) -> dict[str, list[list[str]]]:
    lines = text.split("\n")
    found: dict[str, list[list[str]]] = {}
    for index, line in enumerate(lines):
        if not line.startswith("Table: "):
            continue
        rows: list[list[str]] = []
        for row in lines[index + 2 :]:
            if not row.startswith("|"):
                break
            rows.append([cell.strip() for cell in row.strip().strip("|").split("|")])
        found[line.removeprefix("Table: ")] = [r for r in rows if set(r[0]) != {"-"}]
    return found


def test_the_readme_tables_equal_the_results_file(results: dict[str, Any]) -> None:
    parsed = _parse_tables(README.read_text(encoding="utf-8"))

    expected = tables.all_tables(results)

    assert set(parsed) == set(expected)
    for caption, rows in expected.items():
        assert parsed[caption] == rows, caption


def test_the_table_parser_reads_a_table_and_ignores_the_rule_line() -> None:
    text = "Table: T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nafter"

    assert _parse_tables(text) == {"T": [["A", "B"], ["1", "2"]]}
