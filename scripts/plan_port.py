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
did. A change to a change-log entry that was already there is reported, not
ported. ``--base``, ``--ours`` and ``--theirs`` name files instead of the
index, for a rehearsal.

Python 3 standard library and the ``git`` program. Run from the repository root
or name it with ``--root``.
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

REPO = Path(__file__).resolve().parents[1]
MARKER_C = "<<<plan-port: Part C>>>\n"
MARKER_E = "<<<plan-port: Part E>>>\n"


class PortError(Exception):
    """The three versions cannot be ported."""


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
    rows, conflicts = [], 0
    for section in [*new, *changed]:
        path = root / split.STEPS / f"{section.id}.md"
        theirs_text = split.read_text(path) if path.exists() else None
        ours_text = fixed(section)
        label = f"{section.id}.md"
        if theirs_text is None:
            split.write_text(path, ours_text)
            report.append(f"{label}: written whole (main has no file for it)")
            if section.id not in by_id:
                title = section.title.replace("|", "\\|")
                rows.append(
                    f"| {section.id} | {title} | "
                    f"[{section.id}.md](plan/steps/{section.id}.md) |"
                )
            continue
        base_text = fixed(by_id[section.id]) if section.id in by_id else ""
        merged, count = merge_file(
            theirs_text, base_text, ours_text, ("main", "base", "branch")
        )
        split.write_text(path, merged)
        conflicts += count
        how = "new on both sides" if section.id not in by_id else "three-way merge"
        report.append(f"{label}: {how}, {count} conflict(s)")
    return rows, conflicts


def port_entries(root, base_text, ours_text, report):
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
        report.append(
            f"entry {entry.label} was changed by the branch: NOT ported, edit "
            f"{split.CHANGELOG}/ by hand"
        )
    return len(new)


def port(root: Path, base_text: str, ours_text: str, theirs_text: str) -> int:
    try:
        base = split.parse_old(base_text, with_entries=False)
        ours = split.parse_old(ours_text, with_entries=False)
    except split.SplitError as error:
        raise PortError(
            f"the base and the branch must have the old layout ({error})"
        ) from error
    if "## Part C" not in theirs_text or MARKER_C in theirs_text:
        raise PortError("main's plan has no Part C")
    report: list[str] = []
    rows, conflicts = port_sections(root, base, ours, report)
    count = port_entries(root, base_text, ours_text, report)
    base_skel, _, _ = skeleton(base_text, True)
    ours_skel, _, _ = skeleton(ours_text, True)
    theirs_skel, theirs_c, theirs_e = skeleton(theirs_text, False)
    merged, plan_conflicts = merge_file(
        theirs_skel, base_skel, ours_skel, ("main", "base", "branch")
    )
    for marker in (MARKER_C, MARKER_E):
        if merged.count(marker) != 1:
            raise PortError("the merge of the plan's top lost a Part boundary")
    merged = merged.replace(MARKER_C, add_index_rows(theirs_c, rows))
    merged = merged.replace(MARKER_E, theirs_e)
    split.write_text(root / split.PLAN, merged)
    report.append(
        f"{split.PLAN}: main's, with the branch's top, Part A, Part B and "
        f"Part D merged, {plan_conflicts} conflict(s); {len(rows)} index row(s) added"
    )
    for line in report:
        print(line)
    total = conflicts + plan_conflicts
    print(
        f"ported: {len(report) - 1} file(s), {count} entry stand-in(s), "
        f"{total} conflict(s) to resolve"
    )
    if count:
        print(
            "rename each pr-XXXX-<step>.md to pr-NNNN.md and its label #XXXX to "
            "#NNNN once the pull request exists"
        )
    return 1 if total else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=REPO, help="the working tree")
    parser.add_argument("--base", type=Path, help="the merge base's plan")
    parser.add_argument("--ours", type=Path, help="the branch's plan")
    parser.add_argument("--theirs", type=Path, help="main's plan")
    args = parser.parse_args(argv)
    try:
        texts = [
            split.read_text(given) if given else from_index(args.root, stage)
            for stage, given in ((1, args.base), (2, args.ours), (3, args.theirs))
        ]
        return port(args.root, *texts)
    except PortError as error:
        print(f"plan_port: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
