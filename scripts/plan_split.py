#!/usr/bin/env python3
"""Move the plan's step sections and change log into files of their own (S097).

``docs/meridian-plan.md`` held every step's Part C section and every Part E
entry, about 24,000 lines that two sessions edited at once. This script does
the move from a given revision of that file, the same way every time, and
proves it:

- ``plan_split.py OLD`` reads the old plan (take it from git first, for example
  ``git show origin/main:docs/meridian-plan.md > OLD``: the script overwrites
  the plan in the tree) and writes ``docs/plan/steps/S0NN.md`` for each Part C
  section, ``docs/plan/changelog/v0.NN.md`` for each Part E entry, and the plan
  without them. It deletes nothing.
- ``plan_split.py --check OLD`` proves the move: the step files concatenated in
  the index's order equal the old sections, and the entry files concatenated in
  version order equal the old entries, byte for byte. It prints both byte
  counts and both SHA-256 digests of each and exits 1 on any difference.
- ``plan_split.py --fix-links`` fixes what the move itself breaks: a relative
  link in a step file gets ``../../`` in front, and a link that names a moved
  section's heading by its anchor points at the step's file. This is the one
  change to a moved section, so it runs after the proof, and
  ``--check OLD --fixed`` applies the same rewrite to the old text before it
  compares.
- ``plan_split.py --words`` makes the edits of wording the move needs in the
  plan's top, Part A and Part B, each an exact replacement that must match
  once, and adds S097's row to Part B. Without it a re-run would lose them.

Blank lines. A section runs from its ``### S0NN`` heading to the line before the
next heading (or before ``## Part D``), so the blank lines after a section are
the end of ITS file. An entry runs from its ``- **`` line to the line before the
next entry (or the end of the file); Part E has no blank line between entries.
The text before the first section or entry stays in the plan.

File names. A step file is ``S0NN.md``. An entry labelled ``v0.N`` is
``v0.NN.md``, the minor padded to two digits so that the files sort (a minor of
three digits is written as it is; the plan's version ends below 100). An entry
named for a pull request is ``pr-NNNN.md`` (four digits) and its label is
``#N``. ``pr-XXXX-<step>.md`` with the label ``#XXXX`` stands in until the pull
request has a number.

Python 3 standard library only. Run from anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLAN = "docs/meridian-plan.md"
STEPS = "docs/plan/steps"
CHANGELOG = "docs/plan/changelog"

FENCE_OPEN = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})\s*$")
PART_HEADING = re.compile(r"^## Part ([A-Z]) — ")
# A step is S and three digits, as in Part B. At a step S1000 (or a pull request
# 10000) this and the file-name rules in check_plan_files.py must be widened;
# until then a fourth digit is a finding there, not a silent miss.
SECTION_HEADING = re.compile(r"^### (S\d{3}) — (\S.*)$")
# The first line of a change-log entry: the label is v0.N (the plan's old
# version) or #N (a pull request; #XXXX until it has one), then the date.
ENTRY_HEAD = re.compile(r"^- \*\*(v0\.\d+|#\d+|#XXXX), (\d{4}-\d{2}-\d{2}):\*\*")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
EXTERNAL = ("http://", "https://", "mailto:", "embed:", "/")


class SplitError(Exception):
    """The old plan is not shaped as this script reads it."""


@dataclass
class Line:
    start: int
    text: str
    fenced: bool


def scan(text: str) -> list[Line]:
    """Each line with its offset and whether a fence covers it (fence lines too)."""
    lines: list[Line] = []
    opened: str | None = None
    pos = 0
    for raw in text.split("\n"):
        covered = opened is not None
        if opened is None:
            if opening := FENCE_OPEN.match(raw):
                opened, covered = opening["marker"], True
        elif (close := FENCE_CLOSE.match(raw)) and (
            close["marker"][0] == opened[0] and len(close["marker"]) >= len(opened)
        ):
            opened = None
        lines.append(Line(pos, raw, covered))
        pos += len(raw) + 1
    return lines


def heading_offsets(text: str) -> dict[str, int]:
    """The offset of each ``## Part X`` heading outside a fence; one each."""
    found: dict[str, int] = {}
    for line in scan(text):
        match = PART_HEADING.match(line.text)
        if match and not line.fenced:
            if match[1] in found:
                raise SplitError(f"Part {match[1]} has two headings")
            found[match[1]] = line.start
    return found


@dataclass
class Section:
    id: str
    title: str
    text: str


@dataclass
class Entry:
    minor: int
    text: str


@dataclass
class OldPlan:
    head: str  # everything before "## Part C"
    c_intro: str  # "## Part C" up to the first section
    sections: list[Section]
    d_part: str  # "## Part D" up to "## Part E"
    e_intro: str  # "## Part E" up to the first entry
    entries: list[Entry]


def parse_old(text: str, with_entries: bool = True) -> OldPlan:
    """Read an old-layout plan; ``with_entries=False`` leaves Part E's entries alone
    (plan_port reads a branch's, whose labels may be placeholders, itself)."""
    parts = heading_offsets(text)
    if not all(letter in parts for letter in "CDE"):
        raise SplitError("the plan has no Part C, D and E headings")
    c, d, e = parts["C"], parts["D"], parts["E"]
    if not c < d < e:
        raise SplitError("Parts C, D and E are not in that order")
    lines = [line for line in scan(text) if not line.fenced]
    starts = [
        (line.start, match)
        for line in lines
        if c <= line.start < d and (match := SECTION_HEADING.match(line.text))
    ]
    if not starts:
        raise SplitError("Part C holds no step section")
    bounds = [s for s, _ in starts] + [d]
    sections = [
        Section(m[1], m[2], text[bounds[i] : bounds[i + 1]])
        for i, (_, m) in enumerate(starts)
    ]
    ids = [s.id for s in sections]
    if len(set(ids)) != len(ids):
        raise SplitError("two sections are of one step")
    if not with_entries:
        head = text[c : starts[0][0]]
        return OldPlan(text[:c], head, sections, text[d:e], text[e:], [])
    old_head = re.compile(r"^- \*\*v0\.(\d+), \d{4}-\d{2}-\d{2}:\*\*")
    estarts = [
        (line.start, match)
        for line in lines
        if line.start > e and (match := old_head.match(line.text))
    ]
    if not estarts:
        raise SplitError("Part E holds no entry")
    ebounds = [s for s, _ in estarts] + [len(text)]
    entries = [
        Entry(int(m[1]), text[ebounds[i] : ebounds[i + 1]])
        for i, (_, m) in enumerate(estarts)
    ]
    minors = [x.minor for x in entries]
    if len(set(minors)) != len(minors):
        raise SplitError("two entries carry one version")
    return OldPlan(
        text[:c], text[c : starts[0][0]], sections, text[d:e],
        text[e : estarts[0][0]], entries,
    )  # fmt: skip


def entry_file(minor: int) -> str:
    return f"v0.{minor:02d}.md"


def template_fence(c_intro: str) -> str:
    """The fenced block of Part C's introduction: the section template."""
    lines = c_intro.split("\n")
    block = [t for t, f in zip(lines, scan(c_intro), strict=True) if f.fenced]
    if not block:
        raise SplitError("Part C's introduction holds no template block")
    return "\n".join(block) + "\n"


def new_part_c(old: OldPlan, extra: list[Section]) -> str:
    rows = "".join(
        f"| {s.id} | {s.title.replace('|', chr(92) + '|')} | "
        f"[{s.id}.md](plan/steps/{s.id}.md) |\n"
        for s in [*old.sections, *extra]
    )
    return (
        "## Part C — Step details\n\n"
        "Each step has a file of its own, `docs/plan/steps/S0NN.md`, and its\n"
        "`### S0NN — <title>` heading is the file's first line. A step that\n"
        "starts gets a file from the template below and a row in the index\n"
        "after it. Nothing in this part holds a step's section: `make docs`\n"
        "refuses one. Template:\n\n"
        f"{template_fence(old.c_intro)}\n"
        "| Step | Title | File |\n"
        "|---|---|---|\n"
        f"{rows}\n"
    )


def new_part_e(old: OldPlan) -> str:
    newest = max(e.minor for e in old.entries)
    return (
        "## Part E — Changelog\n\n"
        "Each entry is a file of its own in `docs/plan/changelog/`, the list\n"
        "item as it was written. The entries up to the plan's version\n"
        f"0.{newest} are named for it (`v0.01.md` to `v0.{newest:02d}.md`, the minor\n"
        "padded to two digits so that they sort). The plan has no version of\n"
        "its own any more: an entry is named for the number of the pull\n"
        "request that carries it, `pr-NNNN.md` with four digits, and its first\n"
        "line starts `- **#NNNN, YYYY-MM-DD:**`. It is added once the pull\n"
        "request exists, so that a number is never guessed. The newest\n"
        "entries are the highest `pr-` numbers. `make docs` refuses an entry\n"
        "in this file.\n"
    )


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def existing_extra_steps(root: Path, known: set[str]) -> list[Section]:
    """Step files already in the tree that the old plan has no section for.

    They are the move's own records (S097); they join the index after the old
    sections, in number order, and are not touched.
    """
    extra = []
    for path in sorted((root / STEPS).glob("S[0-9][0-9][0-9].md")):
        first = read_text(path).split("\n", 1)[0]
        match = SECTION_HEADING.match(first)
        if path.stem not in known and match and match[1] == path.stem:
            extra.append(Section(match[1], match[2], ""))
    return extra


def split(old_text: str, root: Path) -> tuple[int, int]:
    old = parse_old(old_text)
    for section in old.sections:
        write_text(root / STEPS / f"{section.id}.md", section.text)
    for entry in old.entries:
        write_text(root / CHANGELOG / entry_file(entry.minor), entry.text)
    extra = existing_extra_steps(root, {s.id for s in old.sections})
    plan = old.head + new_part_c(old, extra) + old.d_part + new_part_e(old)
    write_text(root / PLAN, plan)
    return len(old.sections), len(old.entries)


# -- links ------------------------------------------------------------------


def slug(heading: str) -> str:
    """GitHub's anchor of a heading: lower case, punctuation out, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def anchors_of(sections: list[Section]) -> dict[str, str]:
    """Anchor -> step id, for the heading of each section."""
    return {slug(f"{s.id} — {s.title}"): s.id for s in sections}


def step_sections(root: Path) -> list[Section]:
    found = []
    for path in sorted((root / STEPS).glob("S[0-9][0-9][0-9].md")):
        match = SECTION_HEADING.match(read_text(path).split("\n", 1)[0])
        if match:
            found.append(Section(match[1], match[2], ""))
    return found


def rewrite_links(text: str, rewrite: Callable[[str], str]):
    """Apply ``rewrite(target)`` to each link outside a fence; list the changes."""
    lines = scan(text)
    starts = [line.start for line in lines]
    changes: list[tuple[str, str]] = []

    def sub(match: re.Match[str]) -> str:
        if lines[bisect_right(starts, match.start()) - 1].fenced:
            return match[0]
        new = rewrite(match[2])
        if new != match[2]:
            changes.append((match[2], new))
            return f"[{match[1]}]({new})"
        return match[0]

    return LINK.sub(sub, text), changes


def fix_step_links(text: str, anchors: dict[str, str], root: Path):
    """A moved section's links, seen from docs/plan/steps/ instead of docs/.

    A target gets ``../../`` in front when it names a file that exists from
    docs/ and not from docs/plan/steps/, so a second run changes nothing and a
    link that was broken before the move is left as it was.
    """

    def rewrite(target: str) -> str:
        if target.startswith(EXTERNAL):
            return target
        if target.startswith("#"):
            step = anchors.get(target[1:])
            return f"{step}.md{target}" if step else target
        path, _, anchor = target.partition("#")
        if path == "meridian-plan.md" and anchors.get(anchor):
            return f"{anchors[anchor]}.md#{anchor}"
        if (root / "docs" / path).exists() and not (root / STEPS / path).exists():
            return "../../" + target
        return target

    return rewrite_links(text, rewrite)


def fix_plan_links(text: str, anchors: dict[str, str]):
    """Links in the plan itself to a moved section's heading."""

    def rewrite(target: str) -> str:
        step = anchors.get(target[1:]) if target.startswith("#") else None
        return f"plan/steps/{step}.md{target}" if step else target

    return rewrite_links(text, rewrite)


def fix_links(root: Path) -> int:
    sections = step_sections(root)
    anchors = anchors_of(sections)
    done: list[tuple[str, str, str]] = []
    for section in sections:
        path = root / STEPS / f"{section.id}.md"
        own = {a: s for a, s in anchors.items() if s != section.id}
        text, changes = fix_step_links(read_text(path), own, root)
        if changes:
            write_text(path, text)
            done += [(path.name, old, new) for old, new in changes]
    text, changes = fix_plan_links(read_text(root / PLAN), anchors)
    if changes:
        write_text(root / PLAN, text)
        done += [("meridian-plan.md", old, new) for old, new in changes]
    for name, old, new in done:
        print(f"{name}: ({old}) -> ({new})")
    print(f"links fixed: {len(done)}")
    return 0


# -- the proof ---------------------------------------------------------------


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def index_order(plan_text: str) -> list[str]:
    """The step ids of the plan's Part C index, in the table's order."""
    parts = heading_offsets(plan_text)
    return re.findall(
        r"^\| (S\d{3}) \| .* \| \[S\d{3}\.md\]\(plan/steps/S\d{3}\.md\) \|$",
        plan_text[parts["C"] : parts["D"]],
        re.MULTILINE,
    )


def first_difference(old: bytes, new: bytes) -> str:
    pairs = enumerate(zip(old, new, strict=False))
    at = next((i for i, (a, b) in pairs if a != b), min(len(old), len(new)))
    return f"first difference at byte {at} (line {old[:at].count(b'\n') + 1} of old)"


def check(old_text: str, root: Path, fixed: bool) -> int:
    old = parse_old(old_text)
    plan_text = read_text(root / PLAN)
    ids = {s.id for s in old.sections}
    order = [i for i in index_order(plan_text) if i in ids]
    sections = [s.text for s in old.sections]
    if fixed:
        anchors = anchors_of(old.sections)
        sections = [
            fix_step_links(
                s.text, {a: i for a, i in anchors.items() if i != s.id}, root
            )[0]
            for s in old.sections
        ]
    got_c = b"".join((root / STEPS / f"{i}.md").read_bytes() for i in order)
    minors = sorted(e.minor for e in old.entries)
    got_e = b"".join((root / CHANGELOG / entry_file(m)).read_bytes() for m in minors)
    pairs = [
        ("Part C, step files", "".join(sections).encode(), got_c),
        ("Part E, entry files", "".join(e.text for e in old.entries).encode(), got_e),
    ]
    if not fixed:
        # The rest of the plan is what it was: the top, Parts A and B, and D.
        parts = heading_offsets(plan_text)
        rest = plan_text[: parts["C"]] + plan_text[parts["D"] : parts["E"]]
        want = old.head + old.d_part
        pairs.append(("rest of the plan", want.encode(), rest.encode()))
    status = 0
    for name, want, have in pairs:
        print(f"{name}: old {len(want)} bytes, sha256 {digest(want)}")
        print(f"{name}: new {len(have)} bytes, sha256 {digest(have)}")
        if want != have:
            print(f"{name}: DIFFERENT, {first_difference(want, have)}")
            status = 1
    if ids - set(order):
        print("not in the index: " + ", ".join(sorted(ids - set(order))))
        status = 1
    verdict = "same" if status == 0 else "NOT the same"
    print(f"{len(order)} step files, {len(minors)} entry files: {verdict}")
    return status


# -- words ------------------------------------------------------------------

WORDS: list[tuple[str, str]] = [
    (  # the status block's "how to use"
        "add a `### S0xx` section\n"
        "  under Part C from the template, flip its status, "
        "and fill it in as you go.\n",
        "add\n"
        "  `docs/plan/steps/S0xx.md` (Part C) from the template, flip its status,\n"
        "  and fill it in as you go.\n",
    ),
    (
        "session protocol, open questions and its own changelog.\n",
        "session protocol, open questions, the step files in `docs/plan/steps/`\n"
        "  and its own changelog, one file an entry, in `docs/plan/changelog/`.\n",
    ),
    (  # Part A, step 3
        "3. **Open the step.** Add its section to Part C from the template and "
        "set the\n"
        "   status to `doing`.\n",
        "3. **Open the step.** Add its file `docs/plan/steps/S0NN.md` from Part "
        "C's\n"
        "   template and a row to Part C's index, and set the status to `doing`.\n",
    ),
    (  # step 6
        "open the PR, merge it with\n   `gh pr merge --squash`",
        "open the PR, add its\n"
        '   change-log entry ("Numbers are taken late", below), merge it with\n'
        "   `gh pr merge --squash`",
    ),
    (  # step 7
        "   record is in Part C, every open branch is pushed,",
        "   record is in its step file, every open branch is pushed,",
    ),
    (  # the cluster holder's paragraph
        "(Part C,\n  S075).",
        "(S075's\n  step file).",
    ),
    (  # "Numbers are taken late"
        "`T-NN`, ADR numbers and\n"
        "  the changelog's version are taken after merging `main` into the step's\n"
        "  branch, right before the pull request. A branch whose migration number\n"
        "  is not final is not deployed to the cluster: the runner records a\n"
        "  migration by name and hash.\n",
        "`T-NN` and ADR numbers are\n"
        "  taken after merging `main` into the step's branch, right before the pull\n"
        "  request. A branch whose migration number is not final is not deployed to\n"
        "  the cluster: the runner records a migration by name and hash. The plan\n"
        "  has no version of its own any more: a change-log entry is a file named\n"
        "  for the number of its pull request, `docs/plan/changelog/pr-NNNN.md`,\n"
        "  with the label `#NNNN` on its first line, and is added once the pull\n"
        "  request exists. Until then the file is `pr-XXXX-<step>.md` with the\n"
        "  label `#XXXX`, which `make docs` accepts on the machine and CI refuses:\n"
        "  rename it for the number, push, and the checks run again.\n",
    ),
    (  # "Push a step's branch at the end of a working day"
        "  its Part C section filled in.\n",
        "  its step file filled in.\n",
    ),
]

S097_ROW = (
    "| S097 | The plan in files | Each step's section and each change-log entry "
    "is a file of its own under `docs/plan/`, moved byte for byte and proved so "
    "by `scripts/plan_split.py --check`; a new entry is named for its pull "
    "request's number and the plan has no version of its own; `make docs` "
    "checks the files against Part B and refuses a section or an entry in the "
    "old place; `scripts/plan_port.py` carries a branch's edits of the old "
    "layout over. Part B's tables are not moved and still collide by rows "
    '(the owner, 2026-10-08: "Change log and step sections") | doing | — |'
)
ROW_AFTER = "| S095 | "


def apply_words(root: Path) -> int:
    text = read_text(root / PLAN)
    c = heading_offsets(text)["C"]
    head, rest = text[:c], text[c:]
    for old, new in WORDS:
        if head.count(old) == 1:
            head = head.replace(old, new)
        elif head.count(new) != 1:
            raise SplitError(f"the wording to change is not there once: {old[:60]!r}")
    if "\n| S097 | " not in head:
        lines = head.split("\n")
        at = [i for i, line in enumerate(lines) if line.startswith(ROW_AFTER)]
        if len(at) != 1:
            raise SplitError("the row of S095 is not there once; add S097's by hand")
        lines.insert(at[0] + 1, S097_ROW)
        head = "\n".join(lines)
    write_text(root / PLAN, head + rest)
    print(f"words: {len(WORDS)} replacements made, S097's row in Part B")
    return 0


# -- command line ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("old", nargs="?", help="the old plan, from git show")
    parser.add_argument("--check", metavar="OLD", help="prove the move against OLD")
    parser.add_argument("--fixed", action="store_true", help="--check after the links")
    parser.add_argument("--fix-links", action="store_true")
    parser.add_argument("--words", action="store_true")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=f"split into a tree that already has {STEPS}/ (edited step files are lost)",
    )
    parser.add_argument("--root", type=Path, default=REPO, help="the tree to write")
    args = parser.parse_args(argv)
    try:
        if args.check:
            return check(read_text(Path(args.check)), args.root, args.fixed)
        if args.fix_links:
            return fix_links(args.root)
        if args.words:
            return apply_words(args.root)
        if not args.old:
            parser.error("name the old plan, or one of --check, --fix-links, --words")
        if (args.root / STEPS).exists() and not args.overwrite:
            raise SplitError(
                f"{STEPS}/ exists: a split writes over every step file it makes, "
                f"edited ones included. Pass --overwrite if that is meant"
            )
        steps, entries = split(read_text(Path(args.old)), args.root)
        print(f"wrote {steps} step files, {entries} entry files and the plan")
        return 0
    except SplitError as error:
        print(f"plan_split: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
