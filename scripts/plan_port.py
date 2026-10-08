#!/usr/bin/env python3
"""Carry a branch's edits of the old plan layout over to the new one (S097).

A branch cut before the plan moved into files (S097) edited ``docs/meridian-plan.md``
for its step's Part C section, its change-log entry and its rows in Part B.
Merging ``main`` into it conflicts in that file. Run this while the merge is
open::

    git merge origin/main        # conflicts in docs/meridian-plan.md
    python3 scripts/plan_port.py
    # read what it printed, resolve the marked files, git add, git commit

It reads the three versions from git's index (base, the branch's, main's) and:

- writes each Part C section the branch added to ``docs/plan/steps/S0NN.md``,
  and for a section the branch changed does a three-way merge of the branch's
  section against main's file with ``git merge-file`` (conflict markers are
  left in the file and counted in the report); a link inside a section gets the
  path it needs from its new folder, as ``plan_split.py --fix-links`` gives;
- writes each change-log entry the branch added to
  ``docs/plan/changelog/pr-XXXX-<step>.md`` with the label ``#XXXX``, which
  ``make docs`` accepts on a machine and CI refuses until the file is renamed
  ``pr-NNNN.md`` with the label ``#NNNN`` for the branch's pull request;
- rebuilds the plan as main's, with the branch's changes to the top, Part A,
  Part B and Part D merged three ways (conflicts marked and counted), and a row
  in Part C's index for each step the branch added.

It never deletes a file, never runs ``git add`` or commit, and prints what it
did. What it does not carry over it says, each on a line with ``NOT ported``,
and counts in the last line (and exits 1): a change to an entry that was already
there, to Part C's or Part E's introduction, and a section or an entry the
branch deleted. ``--base``, ``--ours`` and ``--theirs`` name files instead of
the index, for a rehearsal.

It runs once per merge. It refuses to run again when the plan in the tree holds
no conflict marker any more (the first run, or you, already resolved it) or when
a ``pr-XXXX-*.md`` stand-in already exists, and says how to start over.

Python 3 standard library and the ``git`` program. Run it inside the repository
(the root is the one ``git rev-parse --show-toplevel`` names) or name the root
with ``--root``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_split as split

MARKER_C = "<<<plan-port: Part C>>>\n"
MARKER_E = "<<<plan-port: Part E>>>\n"
CONFLICT_OPEN = re.compile(r"^<{7} ", re.MULTILINE)
START_OVER = (
    "To start over: 'git checkout -m -- docs/meridian-plan.md' brings the "
    "conflict back, 'git checkout -- docs/plan/steps' restores main's step "
    "files, then delete what git status lists as new under docs/plan/ (the "
    "step files this run wrote and every pr-XXXX stand-in) and run it once more."
)


class PortError(Exception):
    """The three versions cannot be ported."""


def toplevel() -> Path:
    """The repository of the current directory, not of this script's checkout."""
    done = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, check=False
    )
    if done.returncode != 0:
        raise PortError("not inside a git work tree: run it there, or pass --root")
    return Path(done.stdout.decode("utf-8").strip())


def refuse_a_second_run(root: Path, from_index: bool) -> None:
    """H1: a second run would nest markers, add a second stand-in and wipe a
    resolution; it is refused, with the way to start over."""
    stand_ins = sorted((root / split.CHANGELOG).glob("pr-XXXX-*.md"))
    if stand_ins:
        names = ", ".join(p.name for p in stand_ins)
        raise PortError(
            f"a stand-in is already in {split.CHANGELOG}/ ({names}): this merge "
            f"was ported already. {START_OVER}"
        )
    plan = root / split.PLAN
    if from_index and not (
        plan.is_file() and CONFLICT_OPEN.search(split.read_text(plan))
    ):
        raise PortError(
            f"{split.PLAN} holds no conflict marker: it was ported or resolved "
            f"already, and running again would write over that. {START_OVER}"
        )


def from_index(root: Path, stage: int) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "show", f":{stage}:{split.PLAN}"],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise PortError(
            f"git has no stage {stage} of {split.PLAN}: run this during a merge "
            f"that conflicted in it, or name the versions with --base, --ours "
            f"and --theirs"
        )
    return done.stdout.decode("utf-8")


def merge_file(current: str, base: str, other: str, labels: tuple[str, str, str]):
    """``git merge-file -p``: the merged text and the number of conflicts."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, text in (("current", current), ("base", base), ("other", other)):
            path = Path(tmp) / name
            path.write_bytes(text.encode("utf-8"))
            paths.append(str(path))
        done = subprocess.run(
            ["git", "merge-file", "-p", "-L", labels[0], "-L", labels[1],
             "-L", labels[2], *paths],
            capture_output=True,
            check=False,
        )  # fmt: skip
    if done.returncode < 0 or done.returncode > 127:
        raise PortError("git merge-file failed: " + done.stderr.decode("utf-8"))
    return done.stdout.decode("utf-8"), done.returncode


def skeleton(text: str, old_layout: bool) -> tuple[str, str, str]:
    """The plan with Part C and Part E each replaced by a marker.

    Returns the skeleton, Part C's text and Part E's text. For the old layout
    Part C runs from its heading to Part D's; the new layout is the same.
    """
    parts = split.heading_offsets(text)
    c, d, e = parts["C"], parts["D"], parts["E"]
    return (
        text[:c] + MARKER_C + text[d:e] + MARKER_E,
        text[c:d],
        text[e:],
    )


def same_text(a: str, b: str) -> bool:
    """Equal but for the blank lines at the end: a section inserted after another
    moves that one's trailing blank line, which is no edit of it."""
    return a.rstrip("\n") == b.rstrip("\n")


def added_or_changed(base: list, ours: list, key) -> tuple[list, list]:
    before = {key(x): x for x in base}
    new = [x for x in ours if key(x) not in before]
    changed = [
        x
        for x in ours
        if key(x) in before and not same_text(x.text, before[key(x)].text)
    ]
    return new, changed


# An entry's first line as a branch writes it: its label may be a placeholder
# (PLAN-VERSION, v0.NN) until the version is taken late, so any label counts.
LENIENT_ENTRY = re.compile(r"^- \*\*([^,*\n]+), \d{4}-\d{2}-\d{2}:\*\*")


@dataclass
class LabelledEntry:
    label: str
    text: str


def entries_of(plan_text: str) -> list[LabelledEntry]:
    """The entries of an old-layout Part E, whatever their labels."""
    e = split.heading_offsets(plan_text)["E"]
    starts = [
        (line.start, match[1])
        for line in split.scan(plan_text)
        if line.start > e
        and not line.fenced
        and (match := LENIENT_ENTRY.match(line.text))
    ]
    bounds = [s for s, _ in starts] + [len(plan_text)]
    return [
        LabelledEntry(label, plan_text[bounds[i] : bounds[i + 1]])
        for i, (_, label) in enumerate(starts)
    ]


def entry_stand_in(root: Path, entry: LabelledEntry) -> Path:
    """A free ``pr-XXXX-<step>.md`` for an entry, named for its first step."""
    step = re.search(r"\bS(\d{3})\b", entry.text)
    stem = f"pr-XXXX-s{step[1]}" if step else "pr-XXXX-entry"
    folder = root / split.CHANGELOG
    path, n = folder / f"{stem}.md", 1
    while path.exists():
        n += 1
        path = folder / f"{stem}-{n}.md"
    return path


def add_index_rows(part_c: str, rows: list[str]) -> str:
    """The rows go after the last row of Part C's index."""
    if not rows:
        return part_c
    lines = part_c.split("\n")
    last = [i for i, ln in enumerate(lines) if split_index_row(ln)]
    if not last:
        raise PortError("main's Part C has no index table to add rows to")
    lines[last[-1] + 1 : last[-1] + 1] = rows
    return "\n".join(lines)


def split_index_row(line: str) -> bool:
    return bool(re.match(r"^\| S\d{3} \| .* \| \[S\d{3}\.md\]\(plan/steps/", line))


def port_sections(root, base, ours, report):
    """Write the branch's sections into the step files; return new step rows."""
    anchors = split.anchors_of(ours.sections)

    def fixed(section):
        own = {a: i for a, i in anchors.items() if i != section.id}
        return split.fix_step_links(section.text, own, root)[0]

    new, changed = added_or_changed(base.sections, ours.sections, lambda s: s.id)
    by_id = {s.id: s for s in base.sections}
    ported, conflicts = [], 0
    for section in [*new, *changed]:
        path = root / split.STEPS / f"{section.id}.md"
        theirs_text = split.read_text(path) if path.exists() else None
        ours_text = fixed(section)
        label = f"{section.id}.md"
        ported.append(section)
        if theirs_text is None:
            split.write_text(path, ours_text)
            report.append(f"{label}: written whole (main has no file for it)")
            continue
        base_text = fixed(by_id[section.id]) if section.id in by_id else ""
        merged, count = merge_file(
            theirs_text, base_text, ours_text, ("main", "base", "branch")
        )
        split.write_text(path, merged)
        conflicts += count
        how = "new on both sides" if section.id not in by_id else "three-way merge"
        report.append(f"{label}: {how}, {count} conflict(s)")
    return ported, conflicts


def index_rows(theirs_c: str, ported) -> list[str]:
    """A row for each ported step that main's index does not list yet."""
    listed = {
        m[1]
        for line in theirs_c.split("\n")
        if (m := re.match(r"^\| (S\d{3}) \| .* \| \[S\d{3}\.md\]\(plan/steps/", line))
    }
    return [
        f"| {s.id} | {s.title.replace('|', chr(92) + '|')} | "
        f"[{s.id}.md](plan/steps/{s.id}.md) |"
        for s in ported
        if s.id not in listed
    ]


def e_intro(plan_text: str) -> str:
    """Part E's text before its first entry (an old-layout plan)."""
    start = split.heading_offsets(plan_text)["E"]
    entries = entries_of(plan_text)
    end = plan_text.index(entries[0].text, start) if entries else len(plan_text)
    return plan_text[start:end]


def port_entries(root, base_text, ours_text, report, skipped):
    base, ours = entries_of(base_text), entries_of(ours_text)
    new, changed = added_or_changed(base, ours, lambda e: e.label)
    for entry in new:
        path = entry_stand_in(root, entry)
        text = re.sub(r"^- \*\*[^,*\n]+,", "- **#XXXX,", entry.text, count=1)
        split.write_text(path, text if text.endswith("\n") else text + "\n")
        report.append(
            f"{path.name}: the branch's entry {entry.label}, relabelled #XXXX"
        )
    for entry in changed:
        skipped.append(
            f"entry {entry.label} was changed by the branch: NOT ported, edit "
            f"{split.CHANGELOG}/ by hand"
        )
    kept = {e.label for e in ours}
    for entry in base:
        if entry.label not in kept:
            skipped.append(
                f"entry {entry.label} was deleted by the branch: NOT ported, "
                f"delete its file in {split.CHANGELOG}/ by hand if that is meant"
            )
    return len(new)


def not_ported(base, ours, base_text, ours_text, skipped):
    """M1 and M2: edits to the introductions and deleted sections."""
    if not same_text(base.c_intro, ours.c_intro):
        skipped.append(
            "Part C's introduction was edited by the branch: NOT ported, carry "
            "the edit into the plan's Part C by hand"
        )
    if not same_text(e_intro(base_text), e_intro(ours_text)):
        skipped.append(
            "Part E's introduction was edited by the branch: NOT ported, carry "
            "the edit into the plan's Part E by hand"
        )
    kept = {s.id for s in ours.sections}
    for section in base.sections:
        if section.id not in kept:
            skipped.append(
                f"{section.id} was deleted by the branch: NOT ported, delete "
                f"{split.STEPS}/{section.id}.md and its index row by hand if "
                f"that is meant"
            )


def port(root: Path, base_text: str, ours_text: str, theirs_text: str) -> int:
    # Everything that can refuse comes before anything is written (L4).
    try:
        base = split.parse_old(base_text, with_entries=False)
        ours = split.parse_old(ours_text, with_entries=False)
    except split.SplitError as error:
        raise PortError(
            f"the base and the branch must have the old layout ({error})"
        ) from error
    try:
        parts = split.heading_offsets(theirs_text)
    except split.SplitError as error:
        raise PortError(f"main's plan: {error}") from error
    missing = [k for k in "CDE" if k not in parts]
    if missing or MARKER_C in theirs_text or MARKER_E in theirs_text:
        raise PortError(
            "main's plan is not in the new layout: no heading for Part "
            + ", ".join(missing or ["C"])
        )
    base_skel, _, _ = skeleton(base_text, True)
    ours_skel, _, _ = skeleton(ours_text, True)
    theirs_skel, theirs_c, theirs_e = skeleton(theirs_text, False)
    merged, plan_conflicts = merge_file(
        theirs_skel, base_skel, ours_skel, ("main", "base", "branch")
    )
    for marker in (MARKER_C, MARKER_E):
        if merged.count(marker) != 1:
            raise PortError("the merge of the plan's top lost a Part boundary")
    report: list[str] = []
    skipped: list[str] = []
    not_ported(base, ours, base_text, ours_text, skipped)
    ported, conflicts = port_sections(root, base, ours, report)
    count = port_entries(root, base_text, ours_text, report, skipped)
    rows = index_rows(theirs_c, ported)
    merged = merged.replace(MARKER_C, add_index_rows(theirs_c, rows))
    merged = merged.replace(MARKER_E, theirs_e)
    split.write_text(root / split.PLAN, merged)
    files = len(report)
    report.append(
        f"{split.PLAN}: main's, with the branch's top, Part A, Part B and "
        f"Part D merged, {plan_conflicts} conflict(s); {len(rows)} index row(s) added"
    )
    for line in [*report, *skipped]:
        print(line)
    total = conflicts + plan_conflicts
    print(
        f"ported: {files + 1} file(s), {count} entry stand-in(s), "
        f"{total} conflict(s) to resolve, {len(skipped)} NOT ported"
    )
    if count:
        print(
            "rename each pr-XXXX-<step>.md to pr-NNNN.md and its label #XXXX to "
            "#NNNN once the pull request exists"
        )
    return 1 if total or skipped else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, help="the working tree (default: git's)")
    parser.add_argument("--base", type=Path, help="the merge base's plan")
    parser.add_argument("--ours", type=Path, help="the branch's plan")
    parser.add_argument("--theirs", type=Path, help="main's plan")
    args = parser.parse_args(argv)
    try:
        root = args.root or toplevel()
        given = (args.base, args.ours, args.theirs)
        from_index_stages = not any(given)
        texts = [
            split.read_text(path) if path else from_index(root, stage)
            for stage, path in zip((1, 2, 3), given, strict=True)
        ]
        refuse_a_second_run(root, from_index_stages)
        return port(root, *texts)
    except PortError as error:
        print(f"plan_port: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
