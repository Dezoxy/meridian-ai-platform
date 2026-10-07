"""Prove that a split of a Python file moved every unit and changed none (S074).

A split is a mechanical move. This script holds that the old file, read from a
git revision (``HEAD`` by default, so the file may already be removed), is
fully accounted for by the new files and the support modules the move made or
grew:

- Every top-level unit of the old file (def, async def, class, assignment,
  annotated assignment or any other statement, decorators included, imports and
  the module docstring aside) appears exactly once among the new files and the
  support modules, with the same source text, line for line, comments inside
  it and the comment block directly above it included.
- Inside each new file the units that came from the old file keep the order
  they had there.
- Every banner line (``# ──`` or ``# -- title ---``) of the old file is in one
  new file, once.
- Every other comment line at column 0 of the old file (one parted from its
  unit by a blank line, a trailing or an orphan one) is in the new files or the
  support modules: counted by text, so a line is lost when fewer are held than
  the old file had. A support module's own comment lines at its revision do not
  count towards that.
- No unit of a support module is a test (``test_*`` function, ``Test*`` class)
  or an autouse fixture: pytest neither collects nor applies them there. A
  support module's units that were already there at the revision are not
  looked at.
- What the new files and support modules hold that neither the old file nor the
  support module's own revision had is listed (imports and module docstrings
  aside; a docstring is listed as a count of lines). A support module's units
  that were already there at the revision are not "new".

Usage, one group per old file (``--old`` starts a group)::

    uv run python scripts/split_proof.py \\
        --old tests/meridian/db/test_a.py --new tests/meridian/db/test_a_one.py \\
        tests/meridian/db/test_a_two.py --support tests/meridian/asupport.py

It prints unit names and counts, never a unit's text, and exits 1 when any group
fails. Run it from the repository root. It does not collect tests: the counts
before and after are ``pytest --collect-only -q`` on the old file and the new
ones.
"""

import ast
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

BANNER = "# ──"
RULED = "# -- "


def is_banner(line: str) -> bool:
    """A section banner: ``# ── title`` or ``# -- title ----`` (ruled to the end)."""
    return line.startswith(BANNER) or (
        line.startswith(RULED) and line.rstrip().endswith("---")
    )


@dataclass(frozen=True)
class Unit:
    key: str
    text: str
    order: int
    # What pytest would do with the unit in a test file and not in a support
    # module: "a test", "a test class", "an autouse fixture", or "" for none.
    problem: str = ""


@dataclass
class Group:
    old: str
    new: list[str] = field(default_factory=list)
    support: list[str] = field(default_factory=list)


def git_show(ref: str, path: str) -> str | None:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}:{path}"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        return None
    shown = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=True
    )
    return shown.stdout


def key_of(node: ast.stmt) -> str:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return f"{type(node).__name__} {node.name}"
    if isinstance(node, ast.Assign):
        return "Assign " + ", ".join(ast.unparse(t) for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return "AnnAssign " + ast.unparse(node.target)
    return f"{type(node).__name__} {ast.unparse(node)[:60]}"


def pytest_problem(node: ast.stmt) -> str:
    """What pytest would collect or apply if ``node`` were in a test file."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        if node.name.startswith("test_"):
            return "a test"
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            named = ast.unparse(decorator.func).rsplit(".", 1)[-1] == "fixture"
            for keyword in decorator.keywords:
                is_false = (
                    isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                )
                if named and keyword.arg == "autouse" and not is_false:
                    return "an autouse fixture"
    if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
        return "a test class"
    return ""


def comment_lines(source: str) -> list[str]:
    """The comment lines at column 0 that are not banners, in order."""
    return [
        line
        for line in source.splitlines()
        if line.startswith("#") and not is_banner(line)
    ]


def is_docstring(node: ast.stmt, index: int) -> bool:
    return (
        index == 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def units_of(source: str) -> tuple[list[Unit], int, list[str]]:
    """The units, the docstring's line count and the banner lines of ``source``."""
    lines = source.splitlines()
    units: list[Unit] = []
    docstring_lines = 0
    for index, node in enumerate(ast.parse(source).body):
        if is_docstring(node, index):
            docstring_lines = (node.end_lineno or node.lineno) - node.lineno + 1
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        decorators = getattr(node, "decorator_list", [])
        start = min([node.lineno, *(d.lineno for d in decorators)])
        end = node.end_lineno or node.lineno
        above: list[str] = []
        cursor = start - 2
        while (
            cursor >= 0
            and lines[cursor].startswith("#")
            and not is_banner(lines[cursor])
        ):
            above.insert(0, lines[cursor])
            cursor -= 1
        text = "\n".join([*above, *lines[start - 1 : end]])
        units.append(Unit(key_of(node), text, len(units), pytest_problem(node)))
    banners = [line for line in lines if is_banner(line)]
    return units, docstring_lines, banners


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def prove(group: Group, ref: str, known: set[tuple[str, str]]) -> list[str]:
    """``known`` holds the (key, text) of every group's old units: a support
    module shared by two groups holds the other's units, which are not new."""
    failures: list[str] = []
    old_source = git_show(ref, group.old)
    if old_source is None:
        return [f"{group.old} is not in {ref}"]
    old_units, _, old_banners = units_of(old_source)
    old_by_key = {unit.key: unit for unit in old_units}
    if len(old_by_key) != len(old_units):
        failures.append("the old file has two units under one key")

    found: dict[str, list[tuple[str, Unit]]] = {unit.key: [] for unit in old_units}
    new_units: list[tuple[str, Unit]] = []
    docstrings: dict[str, int] = {}
    banners_new: list[str] = []
    comments_new: Counter[str] = Counter()
    for path in group.new:
        new_source = read(path)
        units, doc_lines, banners = units_of(new_source)
        docstrings[path] = doc_lines
        banners_new += banners
        comments_new.update(comment_lines(new_source))
        last_order = -1
        for unit in units:
            match = old_by_key.get(unit.key)
            if match is None:
                new_units.append((path, unit))
                continue
            found[unit.key].append((path, unit))
            if match.order < last_order:
                failures.append(f"order: {unit.key} is out of its old order in {path}")
            last_order = max(last_order, match.order)
    for path in group.support:
        before_source = git_show(ref, path)
        before_units, _, before_banners = (
            units_of(before_source) if before_source else ([], 0, [])
        )
        before = {u.key: u for u in before_units}
        support_source = read(path)
        units, doc_lines, banners = units_of(support_source)
        banners_new += (Counter(banners) - Counter(before_banners)).elements()
        comments_new.update(
            Counter(comment_lines(support_source))
            - Counter(comment_lines(before_source or ""))
        )
        for unit in units:
            if unit.key in before and before[unit.key].text == unit.text:
                if unit.key in old_by_key:
                    failures.append(f"collision: {unit.key} was already in {path}")
                continue
            if unit.problem:
                failures.append(
                    f"support: {unit.key} in {path} is {unit.problem}, which "
                    "pytest neither collects nor applies in a support module"
                )
            if unit.key in old_by_key:
                found[unit.key].append((path, unit))
            elif (unit.key, unit.text) not in known:
                new_units.append((path, unit))

    for unit in old_units:
        places = found[unit.key]
        if not places:
            failures.append(f"lost: {unit.key}")
        elif len(places) > 1:
            where = ", ".join(path for path, _ in places)
            failures.append(f"twice: {unit.key} in {where}")
        elif places[0][1].text != unit.text:
            failures.append(f"changed: {unit.key} in {places[0][0]}")
    old_comments = comment_lines(old_source)
    missing = Counter(old_comments) - comments_new
    for number, line in enumerate(old_source.splitlines(), start=1):
        if line in missing and line in old_comments and missing[line] > 0:
            missing[line] -= 1
            failures.append(f"comment lost: line {number} of {group.old}")
    for banner in (Counter(old_banners) - Counter(banners_new)).elements():
        failures.append(f"banner lost: {banner}")
    for banner in (Counter(banners_new) - Counter(old_banners)).elements():
        failures.append(f"banner added: {banner}")

    moved = sum(1 for places in found.values() if len(places) == 1)
    print(f"{group.old}: {len(old_units)} units, {moved} found once")
    for path in group.new:
        count = sum(1 for places in found.values() for p, _ in places if p == path)
        print(f"  {path}: {count} units, docstring {docstrings[path]} lines")
    for path in group.support:
        count = sum(1 for places in found.values() for p, _ in places if p == path)
        print(f"  {path} (support): {count} units from the old file")
    print(f"  new units (the old file had none like them): {len(new_units)}")
    for path, unit in new_units:
        print(f"    {unit.key} in {path}")
        failures.append(f"added: {unit.key} in {path} (a move adds no unit)")
    return failures


def parse(arguments: list[str]) -> list[Group]:
    groups: list[Group] = []
    target: list[str] | None = None
    for argument in arguments:
        if argument == "--old":
            groups.append(Group(old=""))
            target = None
        elif argument == "--new" and groups:
            target = groups[-1].new
        elif argument == "--support" and groups:
            target = groups[-1].support
        elif groups and not groups[-1].old:
            groups[-1].old = argument
        elif target is not None:
            target.append(argument)
        else:
            raise SystemExit(f"unexpected argument: {argument}")
    return groups


def main(arguments: list[str]) -> int:
    ref = "HEAD"
    if arguments[:1] == ["--ref"]:
        ref, arguments = arguments[1], arguments[2:]
    groups = parse(arguments)
    if not groups or any(not g.old or not g.new for g in groups):
        raise SystemExit(
            "usage: split_proof.py [--ref REF] --old F --new G... [--support H...]"
        )
    known: set[tuple[str, str]] = set()
    for group in groups:
        old_source = git_show(ref, group.old)
        if old_source is not None:
            known |= {(unit.key, unit.text) for unit in units_of(old_source)[0]}
    failed = 0
    for group in groups:
        failures = prove(group, ref, known)
        for failure in failures:
            print(f"  FAIL {failure}")
        failed += len(failures)
    print("PROOF FAILED" if failed else "PROOF HOLDS: every unit moved once, unchanged")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
