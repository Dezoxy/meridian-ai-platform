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
import subprocess
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
        self.theirs_edit = None  # (old, new) applied to main's plan after the split

    def run_port(self):
        """Split main's version into the tree, then port the branch's edits."""
        self.root.mkdir(exist_ok=True)
        self.split.split(self.main, self.root)
        if self.theirs_edit:
            plan = self.root / "docs/meridian-plan.md"
            plan.write_text(plan.read_text().replace(*self.theirs_edit))
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
        self.assertEqual(status, 1)
        self.assertIn("entry v0.2 was changed by the branch: NOT ported", out)
        self.assertIn("0 conflict(s) to resolve, 1 NOT ported", out)
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


class NotPorted(PortCase):
    """M1 and M2: what the helper cannot carry over, it says, and exits 1."""

    def test_an_edited_part_c_introduction_is_reported(self):
        self.ours = edit(BASE, "Template:", "Template, as the branch words it:")
        status, out, _ = self.run_port()
        self.assertEqual(status, 1)
        self.assertIn("Part C's introduction was edited by the branch: NOT ported", out)
        self.assertIn("1 NOT ported", out.strip().split("\n")[-1])

    def test_an_edited_part_e_introduction_is_reported(self):
        self.ours = edit(
            BASE, "## Part E — Changelog\n", "## Part E — Changelog\n\nA note.\n"
        )
        status, out, _ = self.run_port()
        self.assertEqual(status, 1)
        self.assertIn("Part E's introduction was edited by the branch: NOT ported", out)

    def test_an_introduction_that_only_gained_a_trailing_blank_line_is_not_an_edit(
        self,
    ):
        self.ours = edit(
            BASE, "{0}\n\n### S001".format(FENCE), "{0}\n\n\n### S001".format(FENCE)
        )
        status, out, _ = self.run_port()
        self.assertEqual(status, 0, out)

    def test_a_deleted_section_is_reported_and_its_file_stays(self):
        self.ours = edit(BASE, "### S002 — Two\n\nTwo's body.\n\n", "")
        status, out, _ = self.run_port()
        self.assertEqual(status, 1)
        self.assertIn("S002 was deleted by the branch: NOT ported", out)
        self.assertTrue((self.root / "docs/plan/steps/S002.md").exists())

    def test_a_deleted_entry_is_reported_and_its_file_stays(self):
        self.ours = edit(BASE, "- **v0.2, 2026-09-30:** second entry.\n", "")
        status, out, _ = self.run_port()
        self.assertEqual(status, 1)
        self.assertIn("entry v0.2 was deleted by the branch: NOT ported", out)
        self.assertTrue((self.root / "docs/plan/changelog/v0.02.md").exists())

    def test_a_run_that_ports_everything_exits_zero_and_counts_none(self):
        self.ours = edit(BASE, "line 5", "the branch's line 5")
        status, out, _ = self.run_port()
        self.assertEqual(status, 0)
        self.assertIn("0 NOT ported", out)


class BeforeAnythingIsWritten(PortCase):
    """L4: main's plan is checked first, so a refusal leaves the tree alone."""

    def test_a_plan_of_main_without_part_d_writes_nothing(self):
        self.ours = edit(BASE, "## Part D", "### S003 — Three\n\nBody.\n\n## Part D")
        self.ours += "- **v0.3, 2026-10-08:** S003 a.\n"
        self.theirs_edit = ("## Part D — Open questions", "## Part D - Open questions")
        status, _, err = self.run_port()
        self.assertEqual(status, 2)
        self.assertIn("no heading for Part D", err)
        self.assertFalse((self.root / "docs/plan/steps/S003.md").exists())
        names = [p.name for p in (self.root / "docs/plan/changelog").iterdir()]
        self.assertFalse([n for n in names if n.startswith("pr-XXXX")])

    def test_a_doubled_heading_in_mains_plan_is_a_refusal_not_a_traceback(self):
        self.theirs_edit = (
            "## Part E — Changelog",
            "## Part E — Changelog\n\n## Part E — Changelog",
        )
        status, _, err = self.run_port()
        self.assertEqual(status, 2)
        self.assertIn("main's plan", err)


class RootFromGit(unittest.TestCase):
    """L5: the root is the repository of the current directory."""

    def test_the_root_is_the_repository_of_the_current_directory(self):
        port = load("plan_port")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            git(repo, "init", "-q")
            (repo / "docs").mkdir()
            with contextlib.chdir(repo / "docs"):
                self.assertEqual(port.toplevel(), repo)

    def test_outside_a_work_tree_the_root_must_be_named(self):
        port = load("plan_port")
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with self.assertRaises(port.PortError):
                port.toplevel()


def git(repo, *args):
    """Run git in a scratch repository; return the completed process."""
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip


def tree_state(repo):
    """The plan, the step files and the change-log files, as bytes by name."""
    names = ["docs/meridian-plan.md"]
    for folder in ("docs/plan/steps", "docs/plan/changelog"):
        names += sorted(
            p.relative_to(repo).as_posix()
            for p in (repo / folder).glob("*")
            if p.is_file()
        )
    return {name: (repo / name).read_bytes() for name in names}


class RealMerge(unittest.TestCase):
    """H1: in a real conflicted merge, the second run changes nothing and says why."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.split = load("plan_split")
        self.port = load("plan_port")

    def merge(self, with_entry):
        repo = self.repo
        for step in (["init", "-q", "-b", "main"],):
            self.assertEqual(git(repo, *step).returncode, 0)
        plan = repo / "docs/meridian-plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text(BASE)
        git(repo, "add", "-A")
        self.assertEqual(git(repo, "commit", "-q", "-m", "base").returncode, 0)
        git(repo, "checkout", "-q", "-b", "feature")
        ours = edit(BASE, "## Part D", "### S003 — Three\n\nThree's body.\n\n## Part D")
        ours = edit(
            ours, "| S002 | Two | second | todo |", "| S002 | Two | second | doing |"
        )
        # An edit inside the region main moved out of the plan is what conflicts.
        ours = edit(ours, "line 3", "the branch's line 3")
        if with_entry:
            ours += "- **v0.3, 2026-10-08:** S003 added.\n"
        plan.write_text(ours)
        git(repo, "commit", "-q", "-am", "feature")
        git(repo, "checkout", "-q", "main")
        self.split.split(BASE, repo)
        git(repo, "add", "-A")
        self.assertEqual(
            git(repo, "commit", "-q", "-m", "main splits the plan").returncode, 0
        )
        git(repo, "checkout", "-q", "feature")
        done = git(repo, "merge", "--no-edit", "main")
        self.assertNotEqual(done.returncode, 0, "the merge was meant to conflict")
        self.assertIn("<<<<<<<", plan.read_text())

    def run_port(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.port.main(["--root", str(self.repo), *args])
        return status, out.getvalue(), err.getvalue()

    def test_the_first_run_ports_from_the_index_and_the_second_changes_nothing(self):
        self.merge(with_entry=True)
        status, out, _ = self.run_port()
        self.assertEqual(status, 0, out)
        self.assertIn("S003.md: written whole", out)
        self.assertIn("| S003 | Three | [S003.md](plan/steps/S003.md) |", self.plan())
        after_first = tree_state(self.repo)
        status, _, err = self.run_port()
        self.assertEqual(status, 2)
        self.assertIn("a stand-in is already in", err)
        self.assertIn("To start over", err)
        self.assertEqual(tree_state(self.repo), after_first)

    def test_a_second_run_without_a_stand_in_is_refused_for_the_missing_markers(self):
        self.merge(with_entry=False)
        status, out, _ = self.run_port()
        self.assertEqual(status, 0, out)
        self.assertNotIn("<<<<<<<", self.plan())
        after_first = tree_state(self.repo)
        status, _, err = self.run_port()
        self.assertEqual(status, 2)
        self.assertIn("holds no conflict marker", err)
        self.assertIn("To start over", err)
        self.assertEqual(tree_state(self.repo), after_first)

    def test_a_resolution_made_by_hand_is_not_wiped(self):
        self.merge(with_entry=False)
        self.run_port()
        plan = self.repo / "docs/meridian-plan.md"
        plan.write_text(plan.read_text() + "\nA hand-written line.\n")
        status, _, _ = self.run_port()
        self.assertEqual(status, 2)
        self.assertIn("A hand-written line.", plan.read_text())

    def test_the_root_comes_from_git_when_it_is_not_given(self):
        self.merge(with_entry=False)
        out = io.StringIO()
        with contextlib.chdir(self.repo / "docs"), contextlib.redirect_stdout(out):
            status = self.port.main([])
        self.assertEqual(status, 0, out.getvalue())
        self.assertTrue((self.repo / "docs/plan/steps/S003.md").exists())

    def test_starting_over_as_the_message_says_allows_a_new_run(self):
        self.merge(with_entry=True)
        self.run_port()
        git(self.repo, "checkout", "-m", "--", "docs/meridian-plan.md")
        git(self.repo, "checkout", "--", "docs/plan/steps")
        (self.repo / "docs/plan/steps/S003.md").unlink()
        for stand_in in (self.repo / "docs/plan/changelog").glob("pr-XXXX-*.md"):
            stand_in.unlink()
        status, out, err = self.run_port()
        self.assertEqual(status, 0, err + out)
        self.assertIn("S003.md: written whole", out)

    def plan(self):
        return (self.repo / "docs/meridian-plan.md").read_text()


if __name__ == "__main__":
    unittest.main()
