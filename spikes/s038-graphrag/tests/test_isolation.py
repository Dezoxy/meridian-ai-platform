"""No oracle leakage. The labels file is read only by code that scores a
retrieval, and the generator that wrote both the wordings and the labels is
not read at all. The builder and the questions are shown to open neither, by
three different checks: the source text, the paths opened at run time and the
modules loaded after a run in a fresh process."""

import ast
import builtins
import io
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from claimgraph import files
from claimgraph.build import build_graph
from claimgraph.census import census
from claimgraph.questions import (
    claims_of_customer,
    claims_on_asset,
    clauses_bearing_on_claim,
    customers_sharing_address,
    events_before_loss,
    policies_of_customer,
)
from claimgraph.variant import build_variant

SPIKE = Path(__file__).resolve().parents[1]
SOURCES = sorted((SPIKE / "src" / "claimgraph").glob("*.py"))
FORBIDDEN_TEXT = ("expected-outcomes", "expected_outcomes", "generator/", "/generator")
FORBIDDEN_MODULE_PARTS = ("generator", "scor", "evaluation", "workloads", "expected")
ALLOWED_FILES = {
    "policies.json",
    "claim-history.json",
    "claims.json",
    "wordings/HOME-PLUS.md",
    "wordings/HOME-STD.md",
    "wordings/MOTOR-COMP.md",
    "wordings/MOTOR-TPL.md",
}


def test_the_sources_name_neither_the_labels_file_nor_the_generator() -> None:
    offenders = sum(
        1
        for path in SOURCES
        for text in FORBIDDEN_TEXT
        if text in path.read_text(encoding="utf-8")
    )

    assert SOURCES
    assert offenders == 0


def test_the_sources_import_no_scorer_generator_or_workload_module() -> None:
    imported: list[str] = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
    offenders = sum(
        1 for name in imported for part in FORBIDDEN_MODULE_PARTS if part in name
    )

    assert "meridian.platform.knowledge_mcp.chunking" in imported
    assert offenders == 0


def _everything(data_dir: Path) -> None:
    graph = build_graph(data_dir)
    variant = build_variant(data_dir)
    for built in (graph, variant):
        census(built)
        for claim in built.nodes("Claim"):
            clauses_bearing_on_claim(built, claim.id)
            events_before_loss(built, "POL-0001", date(2026, 1, 1))
        for customer in built.nodes("Customer"):
            policies_of_customer(built, customer.id)
            customers_sharing_address(built, customer.id)
            claims_of_customer(built, customer.id)
        for asset in built.nodes("Asset"):
            claims_on_asset(built, asset.id)


def test_a_full_run_opens_only_the_three_data_files_and_the_four_wordings(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    real_open = io.open

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", recording_open)
    monkeypatch.setattr(builtins, "open", recording_open)

    _everything(data_dir)

    relative = {p.relative_to(data_dir).as_posix() for p in opened}
    assert relative == ALLOWED_FILES
    assert not [p for p in opened if "expected" in p.name or "generator" in p.parts]


def test_every_read_goes_through_the_one_helper(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[Path] = []
    real = files.read_text

    def recording(path: Path) -> str:
        read.append(path)
        return real(path)

    monkeypatch.setattr(files, "read_text", recording)

    _everything(data_dir)

    assert {p.relative_to(data_dir).as_posix() for p in read} == ALLOWED_FILES


def test_after_a_full_run_in_a_fresh_process_no_scorer_is_loaded() -> None:
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from claimgraph.build import build_graph\n"
        "from claimgraph.census import census\n"
        "from claimgraph.variant import build_variant\n"
        "root = Path(sys.argv[1])\n"
        "census(build_graph(root)); census(build_variant(root))\n"
        "bad = [m for m in sys.modules\n"
        "       if m.startswith(('meridian.workloads', 'meridian.runtime'))\n"
        "       or 'scor' in m or 'evaluation' in m\n"
        "       or m.split('.')[0] == 'generator']\n"
        "print(len(bad))\n"
    )
    data_dir = SPIKE.parents[1] / "data" / "synthetic"

    done = subprocess.run(
        [sys.executable, "-c", program, str(data_dir)],
        capture_output=True,
        text=True,
        check=True,
        cwd=SPIKE.parents[1],
        env={"PYTHONPATH": str(SPIKE / "src"), "PATH": ""},
    )

    assert done.stdout.strip() == "0"
