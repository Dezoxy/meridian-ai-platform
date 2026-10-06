"""The files the workload scaffold renders: the golden set, the generated
evaluation module and its generated tests (S039)."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from meridian.platform.cli.scaffold import (
    MAX_NAME_CHARS,
    Plan,
    plan_workload,
    write_plan,
)
from meridian.platform.evaluation.fingerprints import golden_set_of
from meridian.platform.evaluation.report import ReportError

NAME = "fraud-review"
MODULE = "fraud_review"
GRAPHS = "meridian.graphs"
EVALUATIONS = "meridian.evaluations"
LONG_NAME = "a" * 20 + "-" + "b" * 19
RUFF_TIMEOUT_SECONDS = 60
REPO = Path(__file__).resolve().parents[3]
LEAKED = "a-path-or-a-name-the-error-must-not-repeat"
# The small tree has no ``meridian.platform`` for ruff's import sorter to find, so
# it is told what the real checkout's layout shows it: ``meridian`` is ours.
FIRST_PARTY = ("--config", 'lint.isort.known-first-party = ["meridian"]')


EVALUATION = f"src/meridian/workloads/{MODULE}/evaluation.py"
GOLDEN = f"data/evaluation/{NAME}/golden"
LONGEST_CASE = "a" * 64


def created_paths(module: str = MODULE, name: str = NAME) -> set[str]:
    return {
        f"src/meridian/workloads/{module}/__init__.py",
        f"src/meridian/workloads/{module}/graph.py",
        f"src/meridian/workloads/{module}/evaluation.py",
        f"tests/meridian/workloads/{module}/test_{module}_scaffold.py",
        f"data/evaluation/{name}/golden/cases.json",
        f"data/evaluation/{name}/golden/manifest.json",
    }


def snapshot(root: Path) -> dict[str, object]:
    """Every path under ``root``: a file's bytes, a symlink's target, a
    directory as ``None``, so that a stray directory or temp file shows."""
    found: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            found[relative] = None
        else:
            found[relative] = path.read_bytes()
    return found


def edit(path: Path, change: Callable[[str], str]) -> None:
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(change(text).encode("utf-8"))


def comment_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.lstrip()[:1] == "#"]


def written(root: Path, name: str = NAME) -> Plan:
    plan = plan_workload(root, name)
    write_plan(root, plan)
    return plan


def rendered_evaluation(plan: Plan, directory: Path) -> ModuleType:
    """The generated evaluation module, loaded from a file in ``directory``."""
    path = directory / "rendered_evaluation.py"
    path.write_text(plan.created[EVALUATION], encoding="utf-8")
    spec = importlib.util.spec_from_file_location("rendered_evaluation", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def golden_with(directory: Path, cases: object) -> Path:
    golden = directory / "golden"
    golden.mkdir()
    (golden / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    return golden


@pytest.fixture
def evaluation(root: Path, tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    plan = plan_workload(root, NAME)
    return rendered_evaluation(plan, tmp_path_factory.mktemp("rendered"))


def test_an_empty_golden_set_has_no_case_and_is_not_a_graded_one(
    evaluation: ModuleType, tmp_path: Path
) -> None:
    golden = golden_with(tmp_path, [])

    assert evaluation.EVALUATION.submissions(golden) == ()


@pytest.mark.parametrize("case", ["case-1", "A_1", "9", LONGEST_CASE])
def test_a_golden_set_with_a_case_is_refused_for_want_of_graders_before_a_post(
    evaluation: ModuleType, tmp_path: Path, case: str
) -> None:
    golden = golden_with(tmp_path, [{"case": case}])

    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.submissions(golden)

    assert str(refused.value) == evaluation.NO_GRADERS
    assert "write report()" in evaluation.NO_GRADERS


def test_the_report_has_no_graders_either(
    evaluation: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.report({}, tmp_path, None)

    assert str(refused.value) == evaluation.NO_GRADERS


@pytest.mark.parametrize("document", [["text"], [1], {"case": "a"}, "a", None])
def test_a_record_that_is_no_object_or_a_file_that_is_no_list_is_refused(
    evaluation: ModuleType, tmp_path: Path, document: object
) -> None:
    golden = golden_with(tmp_path, document)

    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.submissions(golden)

    assert str(refused.value) == evaluation.NOT_A_LIST


@pytest.mark.parametrize(
    "record", [{}, {"id": "a"}, {"case": 7}, {"case": None}, {"case": ["a"]}]
)
def test_a_record_without_a_text_case_field_is_refused_as_such(
    evaluation: ModuleType, tmp_path: Path, record: dict[str, object]
) -> None:
    golden = golden_with(tmp_path, [record])

    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.submissions(golden)

    assert str(refused.value) == evaluation.MISSING_CASE_FIELD


BAD_CASES = ["", " a", "a b", "-a", "_a", "a/b", "../x", "a\n", "é", "a" * 65]


@pytest.mark.parametrize("case", BAD_CASES, ids=repr)
def test_a_case_of_the_wrong_shape_is_refused_naming_the_shape_and_not_the_case(
    evaluation: ModuleType, tmp_path: Path, case: str
) -> None:
    golden = golden_with(tmp_path, [{"case": case}])

    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.submissions(golden)

    assert str(refused.value) == evaluation.BAD_CASE
    assert "letters, digits, `_` and `-`" in evaluation.BAD_CASE
    assert "at most 64 characters" in evaluation.BAD_CASE
    assert "starting with a letter or digit" in evaluation.BAD_CASE


def test_no_refusal_quotes_the_golden_sets_own_text(
    evaluation: ModuleType, tmp_path: Path
) -> None:
    value = "sentinel value that is no case"
    golden = golden_with(tmp_path, [{"case": value}])

    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.submissions(golden)
    with pytest.raises(ReportError) as path:
        evaluation.EVALUATION.answer_path(value)

    assert "sentinel" not in str(refused.value) + str(path.value)


def test_a_case_that_appears_twice_is_refused_as_such(
    evaluation: ModuleType, tmp_path: Path
) -> None:
    golden = golden_with(tmp_path, [{"case": "a"}, {"case": "b"}, {"case": "a"}])

    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.submissions(golden)

    assert str(refused.value) == evaluation.DUPLICATE_CASE


def test_the_three_case_refusals_differ(evaluation: ModuleType) -> None:
    texts = {
        evaluation.MISSING_CASE_FIELD,
        evaluation.BAD_CASE,
        evaluation.DUPLICATE_CASE,
    }

    assert len(texts) == 3


@pytest.mark.parametrize("case", ["case-1", LONGEST_CASE])
def test_an_answer_is_read_from_a_path_made_of_a_case_of_the_right_shape(
    evaluation: ModuleType, case: str
) -> None:
    path = evaluation.EVALUATION.answer_path(case)

    assert path == f"/{NAME}/cases/{case}"


@pytest.mark.parametrize("case", BAD_CASES, ids=repr)
def test_an_answer_path_is_never_made_of_a_case_of_the_wrong_shape(
    evaluation: ModuleType, case: str
) -> None:
    with pytest.raises(ReportError) as refused:
        evaluation.EVALUATION.answer_path(case)

    assert str(refused.value) == evaluation.BAD_CASE


def test_the_evaluation_says_its_cases_are_synthetic_and_hashed(
    evaluation: ModuleType,
) -> None:
    docstring = evaluation.__doc__
    assert docstring is not None

    assert "synthetic" in docstring
    assert "seeded generator" in docstring
    assert "data/synthetic/" in docstring
    assert "SHA-256" in docstring
    assert "cases.json" in docstring


def test_the_generated_tests_are_four_and_the_empty_workloads_says_so(
    root: Path,
) -> None:
    plan = plan_workload(root, NAME)

    text = plan.created[f"tests/meridian/workloads/{MODULE}/test_{MODULE}_scaffold.py"]

    assert text.count("\ndef test_") == 4
    assert "def test_the_empty_workloads_evaluation_is_published" in text
    assert "replace it with the first case" in text


def test_the_golden_set_text_is_an_empty_list_and_its_manifest(root: Path) -> None:
    plan = plan_workload(root, NAME)

    cases = plan.created[f"data/evaluation/{NAME}/golden/cases.json"]
    manifest = plan.created[f"data/evaluation/{NAME}/golden/manifest.json"]
    assert cases == "[]\n"
    # An empty set has no generator: a "synthetic" claim would be false.
    assert "synthetic" not in json.loads(manifest)
    digest = hashlib.sha256(cases.encode("utf-8")).hexdigest()
    assert manifest == (
        json.dumps(
            {
                "workload": NAME,
                "generator_version": "none",
                "seed": 0,
                "files": {"cases.json": digest},
            },
            indent=2,
        )
        + "\n"
    )


def test_the_generated_golden_set_passes_the_platform_fingerprint(
    root: Path,
) -> None:
    written(root)

    golden_set = golden_set_of(root / f"data/evaluation/{NAME}/golden/manifest.json")

    assert tuple(golden_set.files) == ("cases.json",)
    assert golden_set.workload == NAME
    assert golden_set.generator_version == "none"
    assert golden_set.seed == 0


def rendered_python(plan: Plan) -> dict[str, str]:
    return {path: text for path, text in plan.created.items() if path.endswith(".py")}


@pytest.mark.parametrize("length", [1, 20, MAX_NAME_CHARS])
def test_no_rendered_line_is_longer_than_88_characters(root: Path, length: int) -> None:
    half = length // 2
    name = "a" * length if length < 3 else "a" * half + "-" + "b" * (length - half - 1)

    plan = plan_workload(root, name)

    assert len(name) == length
    for path, text in plan.created.items():
        longest = max(len(line) for line in text.splitlines())
        assert longest <= 88, (path, longest)


@pytest.mark.parametrize("name", ["a", NAME, LONG_NAME])
def test_the_rendered_python_is_clean_under_ruff(root: Path, name: str) -> None:
    plan = plan_workload(root, name)
    write_plan(root, plan)
    ruff = str(Path(sys.executable).parent / "ruff")
    files = sorted(rendered_python(plan))

    for command in (["check"], ["format", "--check", "--diff"]):
        completed = subprocess.run(
            [ruff, *command, "--no-cache", *FIRST_PARTY, *files],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=RUFF_TIMEOUT_SECONDS,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(files) == 4


def test_a_template_substitutes_only_the_name_and_the_module(root: Path) -> None:
    plan = plan_workload(root, NAME)

    for path, text in plan.created.items():
        assert "$" not in text, path
        assert "{{" not in text, path
