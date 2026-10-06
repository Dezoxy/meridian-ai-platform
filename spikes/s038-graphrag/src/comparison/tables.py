"""The README's tables, made from the results file and nothing else: each is a
caption and rows of text, and a test parses the README's tables back and
compares them with these. Imports nothing of the platform, so that
``python -m comparison`` prints them with the spike's own path only."""

import json
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parents[2] / "results" / "comparison.json"
SET_TITLES = {
    "narrative": "narrative labels (sections 2 and 3)",
    "exclusion": "exclusion labels (section 3)",
    "all": "all labels (every citation)",
}
SEARCH_NAMES = {
    "graph": "graph",
    "keyword": "keyword half",
    "vector": "vector half (simulated embedding)",
    "fused": "fused (simulated embedding)",
}
Rows = list[list[str]]


def load(path: Path = RESULTS) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell(hits: int | float, total: int, share: float) -> str:
    shown = f"{hits:.1f}" if isinstance(hits, float) else str(hits)
    return f"{shown}/{total} {share:.2f}"


def recall_rows(results: dict[str, Any], label_set: str) -> Rows:
    table = results["recall"][label_set]
    total = table["labelled"]
    rows = [["Ranking", *(f"@{k}" for k in results["cutoffs"])]]
    for name, title in SEARCH_NAMES.items():
        rows.append(
            [
                title,
                *(
                    _cell(h, total, s)
                    for h, s in zip(
                        table[name]["hits"], table[name]["share"], strict=True
                    )
                ),
            ]
        )
    chance = table["chance"]
    rows.append(
        [
            "chance",
            *(
                _cell(float(h), total, s)
                for h, s in zip(chance["hits"], chance["share"], strict=True)
            ),
        ]
    )
    held = table["production"]
    rows.append(
        ["production (a set, no cut-off)", _cell(held["hits"], total, held["share"])]
        + [""] * 3
    )
    return rows


def size_rows(results: dict[str, Any]) -> Rows:
    rows = [["List", "Claims", "Size: claims", "Shorter than 1, 3, 5, 10"]]
    for name, sized in results["list_sizes"].items():
        counts = ", ".join(f"{s}: {n}" for s, n in sized["sizes"].items())
        shorter = sized.get("shorter_than_k")
        rows.append(
            [
                name,
                str(sized["claims"]),
                counts,
                ", ".join(str(shorter[str(k)]) for k in results["cutoffs"])
                if shorter
                else "a set",
            ]
        )
    return rows


def win_rows(results: dict[str, Any]) -> Rows:
    rows = [["Labels", "Graph against", "Rank", "Wins", "Ties", "Losses"]]
    for label_set in ("narrative", "exclusion", "all"):
        for pair, by_rank in results["wins"][label_set].items():
            for rank, tally in by_rank.items():
                rows.append(
                    [
                        label_set,
                        pair.removeprefix("graph_vs_"),
                        rank,
                        str(tally["win"]),
                        str(tally["tie"]),
                        str(tally["loss"]),
                    ]
                )
    return rows


def point1_rows(results: dict[str, Any], reading: str) -> Rows:
    rows = [["Labels", "Rank", "Against", "Graph", "Other", "More", "W/T/L", "W>=L"]]
    for c in results["rule"]["point1"][reading]["checks"]:
        rows.append(
            [
                c["label_set"],
                str(c["rank"]),
                c["against"],
                str(c["graph_hits"]),
                str(c["other_hits"]),
                "yes" if c["graph_finds_more"] else "no",
                f"{c['wins']}/{c['ties']}/{c['losses']}",
                "yes" if c["wins_at_least_losses"] else "no",
            ]
        )
    return rows


def point2_summary_rows(results: dict[str, Any]) -> Rows:
    rows = [
        [
            "Labels",
            "Claims",
            "Identical",
            "Graph finds more",
            "Graph finds fewer",
            "Held by graph",
            "Held by production",
        ]
    ]
    for label_set in ("all", "narrative"):
        p = results["point2"][label_set]
        rows.append(
            [
                label_set,
                str(p["claims"]),
                str(p["identical"]),
                str(p["graph_finds_more"]),
                str(p["graph_finds_fewer"]),
                str(p["graph_holds"]),
                str(p["production_holds"]),
            ]
        )
    return rows


def point2_difference_rows(results: dict[str, Any]) -> Rows:
    rows = [["Claim", "Only the graph holds", "Only production holds"]]
    for claim, p in results["point2"]["all"]["per_claim"].items():
        if p["graph_only"] or p["production_only"]:
            rows.append(
                [
                    claim,
                    ", ".join(p["graph_only"]) or "none",
                    ", ".join(p["production_only"]) or "none",
                ]
            )
    return rows


def unfound_rows(results: dict[str, Any]) -> Rows:
    rows = [["Claim", "Clause", "In narrative labels"]]
    for u in results["unfound_at_10"]:
        rows.append([u["claim"], u["clause"], "yes" if u["in_sets"] else "no"])
    return rows


def need_rows(results: dict[str, Any]) -> Rows:
    rows = [["Need", "Where written", "Question", "Hops", "Non-trivial", "Counted"]]
    for n in results["needs"]:
        where = "; ".join(
            f"{w['file'].rsplit('/', 1)[-1]}:{w['line']}" for w in n["where"]
        )
        rows.append(
            [
                n["need"],
                where,
                n["question"],
                str(n["hops"]),
                f"{n['non_trivial']} of {n['starts']}",
                "yes" if n["counted"] else "no",
            ]
        )
    return rows


def cost_rows(results: dict[str, Any]) -> Rows:
    g, s = results["cost"]["graph"], results["cost"]["search"]
    return [
        ["Cost", "Graph", "Search (keyword, vector, fused)"],
        [
            "Held",
            f"{g['nodes_held']} nodes, {g['edges_held']} edges",
            f"{s['rows_in_store']} rows",
        ],
        [
            "Per query, looked at",
            f"nodes visited: median {g['per_query_nodes_visited']['median']:g}, "
            f"max {g['per_query_nodes_visited']['max']}; edges followed: median "
            f"{g['per_query_edges_followed']['median']:g}, "
            f"max {g['per_query_edges_followed']['max']}",
            f"rows in scope: min {s['rows_in_scope_per_query']['min']}, median "
            f"{s['rows_in_scope_per_query']['median']:g}, "
            f"max {s['rows_in_scope_per_query']['max']}",
        ],
        [
            "Per query, calls",
            "none",
            f"{s['embeddings_per_query']} embedding "
            f"({s['gateway_calls_for_all_queries']} gateway calls for "
            f"{s['queries']} queries, 16 texts to a call)",
        ],
        [
            "Lines of source",
            f"{g['source_lines_total']} (model, keys, wording, build, traverse, "
            "questions)",
            "the platform's own, not counted here",
        ],
    ]


def all_tables(results: dict[str, Any]) -> dict[str, Rows]:
    tables = {
        f"Recall, {SET_TITLES[s]}": recall_rows(results, s)
        for s in ("narrative", "exclusion", "all")
    }
    tables["List sizes"] = size_rows(results)
    tables["Wins, ties and losses of the graph, query by query"] = win_rows(results)
    tables["Point 1, all labels read as every citation"] = point1_rows(results, "all")
    tables["Point 1, all labels read as the narrative labels"] = point1_rows(
        results, "narrative"
    )
    tables["Point 2, graph against production"] = point2_summary_rows(results)
    tables["Point 2, every claim on which the two differ (all labels)"] = (
        point2_difference_rows(results)
    )
    tables["Labelled clauses no ranking finds at 10"] = unfound_rows(results)
    tables["Point 3, the written needs"] = need_rows(results)
    tables["Costs, as counts"] = cost_rows(results)
    return tables


def to_markdown(rows: Rows) -> str:
    width = len(rows[0])
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def render(results: dict[str, Any]) -> str:
    return "\n\n".join(
        f"Table: {caption}\n\n{to_markdown(rows)}"
        for caption, rows in all_tables(results).items()
    )
