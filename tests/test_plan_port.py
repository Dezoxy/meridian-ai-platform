"""Tests for scripts/plan_port.py: a branch's edits of the old plan, carried over.

Each case builds three plans of the old layout (the merge base, main's version
and the branch's), lets plan_split.py turn main's into the new layout in a
scratch tree, and runs the helper on the three versions. It needs the git
program for ``git merge-file``, as the helper does.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FENCE = "`" * 3


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BASE = f"""# Plan

## Part A

Rules.

## Part B

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S001 | One | first | done | — |
| S002 | Two | second | todo | — |

## Part C — Step details

Template:

{FENCE}text
### S0xx — <title>
{FENCE}

### S001 — One

line 1
line 2
line 3
line 4
line 5

### S002 — Two

Two's body.

## Part D — Open questions

| # | Question |
|---|---|
| 1 | Asked |

## Part E — Changelog

- **v0.1, 2026-09-29:** first entry.
- **v0.2, 2026-09-30:** second entry.
"""


def edit(text, old, new):
    if old not in text:
        raise ValueError(f"the test plan has no {old!r}")
    return text.replace(old, new, 1)


class PortCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "tree"
        self.split = load("plan_split")
        self.port = load("plan_port")
        self.base = BASE
        self.main = BASE
        self.ours = BASE
        self.stand_ins = []

    def run_port(self):
        """Split main's version into the tree, then port the branch's edits."""
        self.root.mkdir(exist_ok=True)
        self.split.split(self.main, self.root)
        paths = {}
        for name in ("base", "ours"):
            paths[name] = self.dir / f"{name}.md"
            paths[name].write_bytes(getattr(self, name).encode())
        args = [
            "--root", str(self.root),
            "--base", str(paths["base"]),
            "--ours", str(paths["ours"]),
            "--theirs", str(self.root / "docs/meridian-plan.md"),
        ]  # fmt: skip
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.port.main(args)
        return status, out.getvalue(), err.getvalue()

    def file(self, name):
        return (self.root / name).read_text()

    def plan(self):
        return self.file("docs/meridian-plan.md")

    def step(self, name):
        return self.file(f"docs/plan/steps/{name}")


class Sections(PortCase):
    def test_a_section_the_branch_added_arrives_whole_in_its_file(self):
        section = "### S003 — Three\n\nThree's body, [x](architecture/x.md).\n\n"
        self.ours = edit(BASE, "## Part D", section + "## Part D")
        (self.root / "docs/architecture").mkdir(parents=True)
        (self.root / "docs/architecture/x.md").write_text("x")
        status, out, _ = self.run_port()
        self.assertEqual(status, 0, out)
        self.assertEqual(
            self.step("S003.md"),
            section.replace("(architecture", "(../../architecture"),
        )
        self.assertIn("| S003 | Three | [S003.md](plan/steps/S003.md) |", self.plan())
        self.assertIn("S003.md: written whole", out)

    def test_the_new_index_row_goes_after_the_last_row(self):
        self.ours = edit(BASE, "## Part D", "### S003 — Three\n\nBody.\n\n## Part D")
        self.run_port()
        rows = [x for x in self.plan().split("\n") if x.startswith("| S00")]
        self.assertEqual(rows[-1][:8], "| S003 |")
        self.assertEqual(rows[-2][:8], "| S002 |")

    def test_a_section_both_sides_changed_in_different_lines_is_merged(self):
        self.main = edit(BASE, "line 1", "main's line 1")
        self.ours = edit(BASE, "line 5", "the branch's line 5")
        status, out, _ = self.run_port()
        self.assertEqual(status, 0, out)
        text = self.step("S001.md")
        self.assertIn("main's line 1", text)
        self.assertIn("the branch's line 5", text)
        self.assertNotIn("<<<<<<<", text)

    def test_a_section_both_sides_changed_in_one_line_is_marked_and_counted(self):
        self.main = edit(BASE, "line 3", "main's line 3")
        self.ours = edit(BASE, "line 3", "the branch's line 3")
        status, out, _ = self.run_port()
        self.assertEqual(status, 1)
        self.assertIn("<<<<<<< main", self.step("S001.md"))
        self.assertIn(">>>>>>> branch", self.step("S001.md"))
        self.assertIn("S001.md: three-way merge, 1 conflict(s)", out)
        self.assertIn("1 conflict(s) to resolve", out)

    def test_a_section_only_main_changed_is_left_as_main_has_it(self):
        self.main = edit(BASE, "line 2", "main's line 2")
        status, out, _ = self.run_port()
        self.assertEqual(status, 0)
        self.assertIn("main's line 2", self.step("S001.md"))
        self.assertNotIn("S001.md:", out)

    def test_a_section_that_gained_only_a_blank_line_is_not_a_change(self):
        # A section inserted right after it moves its trailing blank line.
        self.ours = edit(BASE, "line 5\n\n### S002", "line 5\n### S002")
        _, out, _ = self.run_port()
        self.assertNotIn("S001.md:", out)

    def test_a_section_both_sides_added_is_merged_from_an_empty_base(self):
        self.main = edit(BASE, "## Part D", "### S003 — Three\n\nmain's.\n\n## Part D")
        self.ours = edit(
            BASE, "## Part D", "### S003 — Three\n\nthe branch's.\n\n## Part D"
        )
        status, out, _ = self.run_port()
        self.assertEqual(status, 1)
        self.assertIn("S003.md: new on both sides", out)
        self.assertIn("<<<<<<< main", self.step("S003.md"))

    def test_nothing_is_deleted(self):
        self.ours = edit(BASE, "## Part D", "### S003 — Three\n\nBody.\n\n## Part D")
        keep = self.root / "docs/plan/steps/notes.txt"
        keep.parent.mkdir(parents=True)
        keep.write_text("mine")
        self.run_port()
        self.assertEqual(keep.read_text(), "mine")
        self.assertTrue((self.root / "docs/plan/steps/S001.md").exists())


class Entries(PortCase):
    def test_an_entry_the_branch_added_becomes_a_stand_in_file(self):
        entry = "- **v0.3, 2026-10-08:** S002 done;\n  two lines.\n"
        self.ours = BASE + entry
        status, out, _ = self.run_port()
        self.assertEqual(status, 0, out)
        self.assertEqual(
            self.file("docs/plan/changelog/pr-XXXX-s002.md"),
            "- **#XXXX, 2026-10-08:** S002 done;\n  two lines.\n",
        )
        self.assertIn("rename each pr-XXXX-<step>.md", out)

    def test_a_placeholder_label_is_taken_for_an_entry_too(self):
        self.ours = BASE + "- **PLAN-VERSION, 2026-10-08:** S001 changed.\n"
        self.run_port()
        text = self.file("docs/plan/changelog/pr-XXXX-s001.md")
        self.assertEqual(text, "- **#XXXX, 2026-10-08:** S001 changed.\n")

    def test_two_entries_of_one_step_get_two_files(self):
        self.ours = (
            BASE + "- **v0.3, 2026-10-08:** S002 a.\n- **v0.4, 2026-10-08:** S002 b.\n"
        )
        self.run_port()
        self.assertIn("S002 a.", self.file("docs/plan/changelog/pr-XXXX-s002.md"))
        self.assertIn("S002 b.", self.file("docs/plan/changelog/pr-XXXX-s002-2.md"))

    def test_an_entry_that_names_no_step_gets_a_generic_name(self):
        self.ours = BASE + "- **v0.3, 2026-10-08:** a rule changed.\n"
        self.run_port()
        self.assertTrue((self.root / "docs/plan/changelog/pr-XXXX-entry.md").exists())

    def test_the_stand_in_is_refused_by_the_check_under_ci_only(self):
        self.ours = BASE + "- **v0.3, 2026-10-08:** S002 a.\n"
        self.run_port()
        check = load("check_plan_files")
        _, notes = check.problems(self.root, ci=False)
        problems, _ = check.problems(self.root, ci=True)
        self.assertEqual(len(notes), 1)
        self.assertTrue(any("pr-XXXX-s002.md" in x for x in problems))

    def test_an_old_entry_the_branch_changed_is_reported_not_ported(self):
        self.ours = edit(BASE, "second entry.", "second entry, edited.")
        status, out, _ = self.run_port()
        self.assertEqual(status, 0)
        self.assertIn("entry v0.2 was changed by the branch: NOT ported", out)
        self.assertEqual(
            self.file("docs/plan/changelog/v0.02.md"),
            "- **v0.2, 2026-09-30:** second entry.\n",
        )

    def test_main_entries_stay_as_main_has_them(self):
        self.main = BASE + "- **v0.3, 2026-10-08:** main's own.\n"
        self.ours = BASE + "- **v0.3, 2026-10-08:** the branch's, same version.\n"
        self.run_port()
        self.assertIn("main's own", self.file("docs/plan/changelog/v0.03.md"))
        self.assertIn("the branch's", self.file("docs/plan/changelog/pr-XXXX-entry.md"))


class PlanFile(PortCase):
    def test_the_plan_is_main_with_the_branchs_rows_merged_in(self):
        self.ours = edit(
            BASE, "| S002 | Two | second | todo |", "| S002 | Two | second | doing |"
        )
        self.main = edit(BASE, "Rules.", "Main's rules.")
        status, out, _ = self.run_port()
        plan = self.plan()
        self.assertEqual(status, 0, out)
        self.assertIn("| S002 | Two | second | doing |", plan)
        self.assertIn("Main's rules.", plan)
        self.assertIn("[S001.md](plan/steps/S001.md)", plan)  # main's Part C
        self.assertNotIn("<<<plan-port", plan)

    def test_main_keeps_its_part_e_and_its_index(self):
        self.main = BASE + "- **#139, 2026-10-08:** main's newer entry.\n"
        self.ours = edit(BASE, "Rules.", "New rules.")
        self.run_port()
        plan = self.plan()
        self.assertIn("New rules.", plan)
        self.assertIn("## Part E — Changelog", plan)
        self.assertNotIn("first entry", plan)

    def test_a_row_both_sides_edited_is_marked_and_counted(self):
        self.ours = edit(
            BASE, "| S002 | Two | second |", "| S002 | Two | the branch's |"
        )
        self.main = edit(BASE, "| S002 | Two | second |", "| S002 | Two | main's |")
        status, out, _ = self.run_port()
        self.assertEqual(status, 1)
        self.assertIn("<<<<<<< main", self.plan())
        self.assertIn("docs/meridian-plan.md: main's, with the branch's top", out)
        self.assertIn("1 conflict(s)", out)

    def test_part_d_is_merged_too(self):
        self.ours = edit(BASE, "| 1 | Asked |", "| 1 | Asked, answered |")
        self.run_port()
        self.assertIn("| 1 | Asked, answered |", self.plan())


class Refusals(PortCase):
    def test_a_branch_that_already_has_the_new_layout_is_refused(self):
        self.root.mkdir()
        new = self.dir / "new.md"
        new.write_text(
            "## Part A\n\n## Part B\n\n## Part C — x\n\n## Part D\n\n## Part E\n"
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.port.main(
                ["--root", str(self.root), "--base", str(new), "--ours", str(new),
                 "--theirs", str(new)]
            )  # fmt: skip
        self.assertEqual(status, 2)
        self.assertIn("must have the old layout", err.getvalue())

    def test_outside_a_merge_the_missing_stage_is_said(self):
        self.root.mkdir()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.port.main(["--root", str(self.root)])
        self.assertEqual(status, 2)
        self.assertIn("has no stage 1", err.getvalue())


if __name__ == "__main__":
    unittest.main()
