"""The documents group: the test files a change to documents alone can break (S074).

A pull request that changes only files on the closed allowlist of
``scripts/ci_classify.py`` runs one job, ``make pytest-documents``, instead of
the four shards. That is safe only if every test that reads a document is in
that job, and a hand-kept list goes stale the day somebody adds a test. So this
test finds the tests that read documents and holds each one to be in
``tests/documents-group.txt``, the file the Makefile target and this test both
read. The list may hold more than the finding (over-inclusion costs minutes,
never a missed test); it may not hold less.

What counts as a reference to a document, in any string of a Python file under
``tests/meridian`` or ``tests/synthetic`` (the files pytest collects):

- ``docs/`` or the path part ``docs`` on its own (``REPO_ROOT / "docs"``);
- ``README``, ``CLAUDE.md``, ``AGENTS.md``, ``NOTICE``, ``LICENSE``;
- ``.md`` anywhere, so a string ending ``.md`` and a glob ``*.md``.

Strings, not comments: a comment reads nothing. A support module (``dbsupport``,
``servicesupport``, ...) that holds such a string counts for every test file
that imports it, directly or through another support module, and a
``conftest.py`` that does counts for every test file below it. The files under
``tests/`` itself (``tests/test_*.py``) are not collected by pytest; ``make
test`` runs them in the documentation job, which runs on every pull request.

Limits, written down: a test that reads a document through a path it builds
from something that holds none of the patterns above, or through code in
``src/`` (none reads ``docs/`` or a root document today; they only mention them
in comments and docstrings), is not found. The rule above is the way a document
is named in this repository; the ``docs-sync`` skill is where a new way is
caught, and the list is where it is added by hand.
"""

import ast
from functools import cache
from pathlib import Path

from servicesupport import REPO_ROOT

TESTS = REPO_ROOT / "tests"
GROUP_FILE = TESTS / "documents-group.txt"
# Where pytest collects from, and the folders its pythonpath puts first.
TEST_TREES = (TESTS / "meridian", TESTS / "synthetic")
IMPORT_ROOTS = (TESTS / "meridian", REPO_ROOT / "data" / "synthetic")
MARKERS = (
    "docs/",
    "README",
    "CLAUDE.md",
    "AGENTS.md",
    "NOTICE",
    "LICENSE",
    ".md",
)


def names_a_document(text: str) -> bool:
    return text == "docs" or any(marker in text for marker in MARKERS)


@cache
def strings_of(path: Path) -> tuple[str, ...]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Cannot be read as Python: take the whole text as one string, so that
        # a file nobody can parse is included rather than skipped.
        return (source,)
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


@cache
def imported_modules(path: Path) -> frozenset[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return frozenset()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return frozenset(found)


def unit_files(name: str, roots: tuple[Path, ...] = IMPORT_ROOTS) -> list[Path]:
    """The files a bare import name stands for on pytest's pythonpath."""
    found: list[Path] = []
    for root in roots:
        if (root / f"{name}.py").is_file():
            found.append(root / f"{name}.py")
        if (root / name).is_dir():
            found.extend(sorted((root / name).rglob("*.py")))
    return found


def reaches_a_document(
    path: Path,
    seen: set[Path] | None = None,
    roots: tuple[Path, ...] = IMPORT_ROOTS,
) -> bool:
    """Does the file, or a support module it imports, name a document?"""
    seen = set() if seen is None else seen
    if path in seen:
        return False
    seen.add(path)
    if any(names_a_document(text) for text in strings_of(path)):
        return True
    return any(
        reaches_a_document(module, seen, roots)
        for name in imported_modules(path)
        for module in unit_files(name, roots)
    )


def collected_test_files() -> list[Path]:
    return sorted(
        path
        for tree in TEST_TREES
        for path in tree.rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


def conftests_above(path: Path) -> list[Path]:
    found = []
    for folder in path.parents:
        if (folder / "conftest.py").is_file():
            found.append(folder / "conftest.py")
        if folder == TESTS:
            break
    return found


def readers_of_documents() -> list[str]:
    """Repository-relative paths, sorted: every test file the rule finds."""
    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in collected_test_files()
        if reaches_a_document(path)
        or any(reaches_a_document(conftest) for conftest in conftests_above(path))
    )


def group() -> list[str]:
    lines = GROUP_FILE.read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def test_every_test_that_reads_a_document_is_in_the_group() -> None:
    missing = sorted(set(readers_of_documents()) - set(group()))

    assert not missing, (
        f"{len(missing)} test files name a document and are not in "
        f"{GROUP_FILE.relative_to(REPO_ROOT)}; add these lines:\n" + "\n".join(missing)
    )


def test_the_group_lists_only_test_files_that_exist_once_each() -> None:
    listed = group()
    collected = {
        path.relative_to(REPO_ROOT).as_posix() for path in collected_test_files()
    }

    assert listed, "the group is empty: the fast path would run no test"
    assert len(listed) == len(set(listed)), "a path is listed twice"
    assert sorted(listed) == listed, "the list is sorted, one path per line"
    assert set(listed) <= collected, sorted(set(listed) - collected)


def test_the_finding_is_not_empty_and_includes_files_known_to_read_documents() -> None:
    # A finder that matched nothing would let the first test pass for ever.
    found = set(readers_of_documents())

    assert len(found) >= 10
    # test_helm_chart reads infra/kind/README.md.
    assert "tests/meridian/test_helm_chart.py" in found
    # test_ci_config reads the Makefile and the workflow, no document.
    assert "tests/meridian/test_ci_config.py" not in found


def test_the_rule_names_what_is_a_document_and_what_is_not() -> None:
    for text in ("docs/plan.md", "docs", "*.md", "see the README", "CLAUDE.md"):
        assert names_a_document(text), text
    for text in ("gateway", "documents", "doc", "src/meridian", "Makefile"):
        assert not names_a_document(text), text


def test_the_rule_follows_imports_through_support_modules(tmp_path: Path) -> None:
    # A scratch tree: a test file that names nothing itself but imports a
    # support module that imports one that names a document is found; one that
    # imports a clean module is not; an import cycle ends.
    (tmp_path / "readssupport.py").write_text('PATH = "docs/a"\n', "utf-8")
    (tmp_path / "middlesupport.py").write_text("import readssupport\n", "utf-8")
    (tmp_path / "cleansupport.py").write_text('NAME = "gateway"\n', "utf-8")
    (tmp_path / "cycleone.py").write_text("import cycletwo\n", "utf-8")
    (tmp_path / "cycletwo.py").write_text("import cycleone\n", "utf-8")
    through = tmp_path / "test_through.py"
    through.write_text("from middlesupport import x\n", "utf-8")
    clean = tmp_path / "test_clean.py"
    clean.write_text("import cleansupport\nimport cycleone\nimport os\n", "utf-8")
    roots = (tmp_path,)

    assert reaches_a_document(through, roots=roots)
    assert not reaches_a_document(clean, roots=roots)


def test_a_string_in_a_comment_is_not_a_reference(tmp_path: Path) -> None:
    quiet = tmp_path / "test_quiet.py"
    quiet.write_text("# see docs/plan.md\nX = 1\n", "utf-8")
    loud = tmp_path / "test_loud.py"
    loud.write_text('X = "docs/plan.md"\n', "utf-8")
    broken = tmp_path / "test_broken.py"
    broken.write_text("def (:\n", "utf-8")

    assert not reaches_a_document(quiet, roots=())
    assert reaches_a_document(loud, roots=())
    # A file that cannot be parsed is included, not skipped (it names nothing
    # the finder can see, so it is taken as one string with its text).
    assert strings_of(broken) == ("def (:\n",)
