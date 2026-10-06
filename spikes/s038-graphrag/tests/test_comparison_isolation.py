"""No oracle leakage, for the modules of the comparison. `score.py` is the only
module that opens the labels. The modules that build or ask a ranking (the
graph's list, the production set, the search, the needs) neither import the
scorer nor call a function of the platform's retrieval check that reads
labels. Three checks, as for the graph's own modules: the source text, the
imports and a fresh process that records what it opened."""

import ast
import subprocess
import sys
from pathlib import Path

SPIKE = Path(__file__).resolve().parents[1]
COMPARISON = sorted((SPIKE / "src" / "comparison").glob("*.py"))
SCORER = "score.py"
# Modules that score, and so may import the scorer.
SCORING = {"score.py", "report.py", "rule.py"}
ASKING = [p for p in COMPARISON if p.name not in SCORING]
LABEL_READERS = (
    "narrative_queries",
    "documents_queries",
    "_golden_queries",
    "expected-outcomes",
    "expected_outcomes",
)
GENERATOR = ("generator/", "/generator", "import generator", "from generator")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _imports(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(_text(path))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append(("." * node.level) + (node.module or ""))
            found += [("." * node.level) + a.name for a in node.names]
    return found


def test_the_comparison_package_has_the_modules_the_contract_names() -> None:
    assert {p.name for p in COMPARISON} >= {
        "score.py",
        "rankings.py",
        "search.py",
        "needs.py",
        "report.py",
        "rule.py",
        "tables.py",
    }


def test_only_the_scorer_names_the_labels_file() -> None:
    naming = sorted(p.name for p in COMPARISON if "expected-outcomes" in _text(p))

    assert naming == [SCORER]


def test_no_module_of_the_comparison_names_the_generator() -> None:
    offenders = [p.name for p in COMPARISON for g in GENERATOR if g in _text(p)]

    assert COMPARISON
    assert offenders == []


def test_the_modules_that_ask_a_ranking_read_no_labels_and_import_no_scorer() -> None:
    offenders = []
    for path in ASKING:
        text = _text(path)
        offenders += [(path.name, r) for r in LABEL_READERS if r in text]
        offenders += [
            (path.name, name)
            for name in _imports(path)
            if name.endswith(("score", "report", "rule")) or name.endswith(".score")
        ]

    assert [p.name for p in ASKING] == [
        "__init__.py",
        "__main__.py",
        "needs.py",
        "rankings.py",
        "search.py",
        "tables.py",
    ]
    assert offenders == []


def test_the_graph_modules_do_not_import_the_comparison() -> None:
    for path in (SPIKE / "src" / "claimgraph").glob("*.py"):
        assert "comparison" not in " ".join(_imports(path)), path.name


def test_nothing_under_the_platform_imports_the_spike() -> None:
    root = SPIKE.parents[1] / "src" / "meridian"
    offenders = [
        p
        for p in root.rglob("*.py")
        if any(
            name.split(".")[0] in ("comparison", "claimgraph") for name in _imports(p)
        )
    ]

    assert offenders == []


def test_graph_and_production_rankings_in_a_fresh_process_never_open_the_labels() -> (
    None
):
    program = (
        "import builtins, io, sys\n"
        "from pathlib import Path\n"
        "opened = []\n"
        "real = io.open\n"
        "def recording(file, *a, **k):\n"
        "    opened.append(str(file))\n"
        "    return real(file, *a, **k)\n"
        "io.open = builtins.open = recording\n"
        "from claimgraph.build import build_graph\n"
        "from comparison import needs, rankings\n"
        "root = Path(sys.argv[1])\n"
        "graph = build_graph(root)\n"
        "inputs = rankings.claim_inputs(root)\n"
        "rankings.graph_lists(graph, inputs)\n"
        "rankings.production_sets(root, inputs)\n"
        "needs.answers(graph)\n"
        "loaded = [m for m in sys.modules if m.startswith('comparison')]\n"
        "labels = [p for p in opened if 'expected' in p]\n"
        "print(len(labels), sorted(loaded))\n"
    )
    repo = SPIKE.parents[1]

    done = subprocess.run(
        [sys.executable, "-c", program, str(repo / "data" / "synthetic")],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
        env={"PYTHONPATH": str(SPIKE / "src"), "PATH": ""},
    )

    count, _, loaded = done.stdout.strip().partition(" ")
    assert count == "0"
    assert "score" not in loaded
    assert "report" not in loaded
    assert "rule" not in loaded
