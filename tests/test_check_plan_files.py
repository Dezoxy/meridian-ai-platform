"""Tests for scripts/check_plan_files.py and the docs index's carve-out (S097).

The plan's step sections and change-log entries are files of their own, and the
check keeps those files and the plan one story. Each test builds a small tree
that is right, plants one violation and expects one finding; the clean tree must
pass, and the real repository must pass too.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_check_docs_consistency import load_checker_for  # noqa: E402

SCRIPT = HERE.parent / "scripts" / "check_plan_files.py"
FENCE = "`" * 3

PLAN = f"""# Plan

## Part B — Roadmap and step list

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S001 | One | x | done | — |
| S002 | Two | x | todo | — |

## Part C — Step details

Template:

{FENCE}text
### S0xx — <title>
{FENCE}

| Step | Title | File |
|---|---|---|
| S001 | One | [S001.md](plan/steps/S001.md) |

## Part D — Open questions

## Part E — Changelog

Entries live in `docs/plan/changelog/`.
"""

STEP = "### S001 — One\n\nBody.\n\n"
OLD_ENTRY = "- **v0.1, 2026-09-29:** first\n  continued.\n"
NEW_ENTRY = "- **#140, 2026-10-08:** a pull request's entry.\n"


def load():
    spec = importlib.util.spec_from_file_location("check_plan_files", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_plan_files"] = module
    spec.loader.exec_module(module)
    return module


class PlanCase(unittest.TestCase):
    """A right tree of the plan and its files, to plant a violation in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.mod = load()
        self.write("docs/meridian-plan.md", PLAN)
        self.write("docs/plan/steps/S001.md", STEP)
        self.write("docs/plan/changelog/v0.01.md", OLD_ENTRY)
        self.write("docs/plan/changelog/pr-0140.md", NEW_ENTRY)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        return path

    def found(self, ci=False):
        problems, notes = self.mod.problems(self.repo, ci)
        self.notes = notes
        return problems

    def one(self, ci=False):
        found = self.found(ci)
        self.assertEqual(len(found), 1, found)
        return found[0]


class PlanFiles(PlanCase):
    def test_a_right_tree_passes(self):
        self.assertEqual(self.found(), [])
        self.assertEqual(self.notes, [])

    def test_a_repository_without_a_plan_is_skipped(self):
        (self.repo / "docs/meridian-plan.md").unlink()
        self.assertEqual(self.found(), [])

    def test_missing_folders_are_findings_only_for_what_the_plan_promises(self):
        for name in ("steps/S001.md", "changelog/v0.01.md", "changelog/pr-0140.md"):
            (self.repo / "docs/plan" / name).unlink()
        found = self.found()
        # The index row and the done step in Part B each name the lost file.
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all("S001" in x for x in found), found)

    def test_a_plan_that_promises_no_file_needs_no_folder(self):
        bare = "## Part B — x\n\n## Part C — x\n\n## Part D — x\n\n## Part E — x\n"
        self.write("docs/meridian-plan.md", bare)
        for name in ("steps/S001.md", "changelog/v0.01.md", "changelog/pr-0140.md"):
            (self.repo / "docs/plan" / name).unlink()
        self.assertEqual(self.found(), [])


class StepFiles(PlanCase):
    def test_a_file_without_a_row_in_part_b_is_reported(self):
        self.write("docs/plan/steps/S003.md", "### S003 — Three\n")
        found = self.found()
        self.assertTrue(any("S003 has no row in Part B" in x for x in found), found)

    def test_a_file_the_index_does_not_list_is_reported(self):
        self.write("docs/plan/steps/S002.md", "### S002 — Two\n")
        self.assertIn("S002 is not in Part C's index", self.one())

    def test_an_index_row_without_a_file_is_reported(self):
        (self.repo / "docs/plan/steps/S001.md").unlink()
        self.assertIn("lists S001, which has no file", self.found()[0])

    def test_a_step_listed_twice_in_the_index_is_reported(self):
        row = "| S001 | One | [S001.md](plan/steps/S001.md) |\n"
        plan = PLAN.replace(row, row + row)
        self.write("docs/meridian-plan.md", plan)
        self.assertIn("lists S001 twice", self.one())

    def test_an_index_row_that_names_another_steps_file_is_reported(self):
        plan = PLAN.replace(
            "[S001.md](plan/steps/S001.md)", "[S002.md](plan/steps/S002.md)"
        )
        self.write("docs/meridian-plan.md", plan)
        self.assertTrue(any("names plan/steps/S002.md" in x for x in self.found()))

    def test_a_file_whose_first_line_is_another_steps_heading_is_reported(self):
        self.write("docs/plan/steps/S001.md", "### S002 — Two\n\nBody.\n")
        self.assertIn("first line must be the heading '### S001", self.one())

    def test_a_file_that_opens_with_a_blank_line_is_reported(self):
        self.write("docs/plan/steps/S001.md", "\n" + STEP)
        self.assertIn("first line must be the heading", self.one())

    def test_a_misnamed_file_is_reported(self):
        for name in ("s001.md", "S1.md", "S001.txt", "S0010.md"):
            with self.subTest(name=name):
                path = self.write(f"docs/plan/steps/{name}", STEP)
                self.assertIn("only S0NN.md files belong", self.one())
                path.unlink()

    def test_a_done_step_without_a_file_is_reported(self):
        plan = PLAN.replace("| S002 | Two | x | todo |", "| S002 | Two | x | done |")
        self.write("docs/meridian-plan.md", plan)
        self.assertIn("S002 as 'done', and S002 has no file", self.one())

    def test_a_doing_step_with_a_note_after_the_word_needs_its_file_too(self):
        plan = PLAN.replace("| todo |", "| doing: the first half |")
        self.write("docs/meridian-plan.md", plan)
        self.assertIn("'doing: the first half'", self.one())

    def test_a_todo_step_may_have_no_file(self):
        self.assertEqual(self.found(), [])

    def test_a_step_section_left_in_part_c_is_reported(self):
        plan = PLAN.replace(
            "## Part D", "### S002 — Two\n\nBody that belongs in a file.\n\n## Part D"
        )
        self.write("docs/meridian-plan.md", plan)
        self.assertIn("the plan holds a step section", self.one())

    def test_the_templates_heading_in_a_fence_is_not_a_section(self):
        self.assertEqual(self.found(), [])


class ChangelogFiles(PlanCase):
    def test_an_entry_left_in_part_e_is_reported(self):
        self.write("docs/meridian-plan.md", PLAN + "\n" + NEW_ENTRY)
        self.assertIn("Part E of the plan holds an entry", self.one())

    def test_an_old_entry_left_in_part_e_is_reported(self):
        self.write("docs/meridian-plan.md", PLAN + OLD_ENTRY)
        self.assertIn("Part E of the plan holds an entry", self.one())

    def test_a_name_and_a_label_that_disagree_are_reported(self):
        self.write("docs/plan/changelog/v0.01.md", OLD_ENTRY.replace("v0.1,", "v0.2,"))
        self.assertIn("named 01 but labelled v0.2", self.one())

    def test_a_pull_request_name_and_label_that_disagree_are_reported(self):
        self.write("docs/plan/changelog/pr-0140.md", NEW_ENTRY.replace("#140", "#141"))
        self.assertIn("named pr-0140 but labelled #141", self.one())

    def test_a_version_minor_that_is_not_padded_is_reported(self):
        self.write("docs/plan/changelog/v0.1.md", OLD_ENTRY)
        self.assertIn("a change-log file is v0.NN.md", self.found()[0])

    def test_a_version_minor_padded_too_far_is_reported(self):
        (self.repo / "docs/plan/changelog/v0.01.md").unlink()
        self.write("docs/plan/changelog/v0.001.md", OLD_ENTRY)
        self.assertIn("named 001 but labelled v0.1", self.one())

    def test_a_three_digit_minor_is_written_as_it_is(self):
        self.write(
            "docs/plan/changelog/v0.100.md", OLD_ENTRY.replace("v0.1,", "v0.100,")
        )
        self.assertEqual(self.found(), [])

    def test_two_files_with_one_label_are_reported(self):
        self.write("docs/plan/changelog/pr-0141.md", NEW_ENTRY)  # labelled #140 too
        found = self.found()
        self.assertTrue(any("also the label of" in x for x in found), found)

    def test_a_pull_request_number_is_four_digits(self):
        (self.repo / "docs/plan/changelog/pr-0140.md").unlink()
        self.write("docs/plan/changelog/pr-140.md", NEW_ENTRY)
        self.assertIn("a change-log file is v0.NN.md", self.one())

    def test_a_file_that_does_not_start_with_a_label_is_reported(self):
        self.write("docs/plan/changelog/pr-0140.md", "A pull request's entry.\n")
        self.assertIn("first line must start", self.one())

    def test_a_file_with_two_entries_is_reported(self):
        self.write("docs/plan/changelog/pr-0140.md", NEW_ENTRY + OLD_ENTRY)
        self.assertIn("a file holds one entry", self.one())

    def test_a_file_that_is_not_markdown_is_reported(self):
        self.write("docs/plan/changelog/notes.txt", "x")
        self.assertIn("a change-log file is v0.NN.md", self.one())


class Placeholder(PlanCase):
    STAND_IN = "- **#XXXX, 2026-10-08:** the entry of a pull request not yet open.\n"

    def setUp(self):
        super().setUp()
        self.write("docs/plan/changelog/pr-XXXX-s097.md", self.STAND_IN)

    def test_it_passes_on_a_machine_and_is_noted(self):
        self.assertEqual(self.found(ci=False), [])
        self.assertEqual(len(self.notes), 1)
        self.assertIn("pr-XXXX-s097.md: rename it", self.notes[0])

    def test_it_fails_under_ci(self):
        self.assertIn("pr-XXXX-s097.md: rename it", self.one(ci=True))

    def test_two_stand_ins_of_two_steps_are_not_one_label_twice(self):
        self.write("docs/plan/changelog/pr-XXXX-s098.md", self.STAND_IN)
        self.assertEqual(self.found(ci=False), [])
        self.assertEqual(len(self.notes), 2)

    def test_a_stand_in_with_a_real_label_is_reported_either_way(self):
        self.write(
            "docs/plan/changelog/pr-XXXX-s097.md", NEW_ENTRY.replace("140", "141")
        )
        self.assertIn("needs the label #XXXX", self.one(ci=False))

    def test_the_command_reads_the_environment_for_ci(self):
        self.mod.REPO = self.repo
        for value, status in (("true", 1), (None, 0)):
            env = {"GITHUB_ACTIONS": value} if value else {}
            with self.subTest(ci=value), mock.patch.dict(os.environ, env):
                if value is None:
                    os.environ.pop("GITHUB_ACTIONS", None)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(self.mod.main(), status)
                if value:
                    self.assertIn("[plan-files]", err.getvalue())
                else:
                    self.assertIn("[note]", out.getvalue())


class PartHeadings(PlanCase):
    """M3: a heading that cannot be found must not switch its check off."""

    def test_a_missing_heading_is_a_finding_for_each_part(self):
        for letter, head in (
            ("B", "## Part B — Roadmap and step list"),
            ("C", "## Part C — Step details"),
            ("D", "## Part D — Open questions"),
            ("E", "## Part E — Changelog"),
        ):
            with self.subTest(part=letter):
                self.write(
                    "docs/meridian-plan.md", PLAN.replace(head, head.replace("—", "-"))
                )
                found = self.found()
                self.assertTrue(
                    any(f"no '## Part {letter} — ' heading" in x for x in found), found
                )

    def test_an_entry_under_a_hyphenated_part_e_heading_is_not_missed(self):
        plan = PLAN.replace("## Part E — Changelog", "## Part E - Changelog")
        self.write("docs/meridian-plan.md", plan + NEW_ENTRY)
        self.assertTrue(any("Part E" in x for x in self.found()))

    def test_a_doubled_heading_is_a_finding_not_a_traceback(self):
        self.write("docs/meridian-plan.md", PLAN + "\n## Part E — Changelog\n")
        self.assertIn("Part E has two headings", self.found()[0])

    def test_parts_out_of_order_are_a_finding(self):
        plan = PLAN.replace("## Part D — Open questions", "## Part X")
        plan = plan.replace("## Part E — Changelog", "## Part D — Open questions")
        plan = plan.replace("## Part X", "## Part E — Changelog")
        self.write("docs/meridian-plan.md", plan)
        self.assertIn("not in that order", self.found()[0])

    def test_an_entry_of_any_label_left_in_part_e_is_reported(self):
        for label in ("v0.99", "PLAN-VERSION", "#145", "#XXXX", "v0.NN"):
            with self.subTest(label=label):
                entry = f"- **{label}, 2026-10-09:** left behind.\n"
                self.write("docs/meridian-plan.md", PLAN + entry)
                found = self.found()
                self.assertEqual(len(found), 1, found)
                self.assertIn("Part E of the plan holds an entry", found[0])

    def test_a_step_heading_in_any_old_form_is_reported(self):
        forms = (
            "### S002 — Two",
            "### S002 - Two",
            "### S002 – Two",
            "## S002 — Two",
            "#### S002 — Two",
        )
        for heading in forms:
            for where in ("## Part D", "## Part E"):
                with self.subTest(heading=heading, where=where):
                    plan = PLAN.replace(where, f"{heading}\n\nBody.\n\n{where}")
                    if where == "## Part E":
                        plan = PLAN.replace(
                            "## Part E — Changelog",
                            f"## Part E — Changelog\n\n{heading}\n",
                        )
                    self.write("docs/meridian-plan.md", plan)
                    found = self.found()
                    self.assertTrue(
                        any("the plan holds a step section" in x for x in found), found
                    )

    def test_a_heading_of_level_five_or_in_a_fence_is_not_a_section(self):
        fenced = f"{FENCE}text\n### S002 — Two\n{FENCE}\n"
        self.write(
            "docs/meridian-plan.md",
            PLAN.replace("## Part D", f"##### S002 — Two\n\n{fenced}\n## Part D"),
        )
        self.assertEqual(self.found(), [])


class OddFiles(PlanCase):
    """M4: an odd file is a finding, never a traceback."""

    def test_a_step_file_that_is_not_utf8(self):
        self.write("docs/plan/steps/S001.md", "")
        (self.repo / "docs/plan/steps/S001.md").write_bytes(b"### S001 \xff\xfe\n")
        self.assertIn("cannot be read as UTF-8", self.one())

    def test_a_directory_with_a_steps_name(self):
        (self.repo / "docs/plan/steps/S001.md").unlink()
        (self.repo / "docs/plan/steps/S001.md").mkdir()
        found = self.found()
        self.assertTrue(any("not a regular file" in x for x in found), found)

    def test_a_broken_link_with_a_steps_name(self):
        (self.repo / "docs/plan/steps/S001.md").unlink()
        (self.repo / "docs/plan/steps/S001.md").symlink_to(self.repo / "nowhere")
        found = self.found()
        self.assertTrue(any("not a regular file" in x for x in found), found)

    def test_a_directory_with_an_entrys_name(self):
        (self.repo / "docs/plan/changelog/pr-0145.md").mkdir()
        self.assertIn("pr-0145.md: not a regular file", self.one())

    def test_a_broken_link_with_an_entrys_name(self):
        (self.repo / "docs/plan/changelog/pr-0145.md").symlink_to(self.repo / "nowhere")
        self.assertIn("pr-0145.md: not a regular file", self.one())

    def test_a_change_log_file_that_is_not_utf8(self):
        (self.repo / "docs/plan/changelog/pr-0140.md").write_bytes(b"- **#140, \xff")
        self.assertIn("cannot be read as UTF-8", self.one())

    def test_a_plan_that_is_not_utf8(self):
        (self.repo / "docs/meridian-plan.md").write_bytes(PLAN.encode() + b"\xff\xfe")
        found = self.found()
        self.assertTrue(
            any("meridian-plan.md: cannot be read" in x for x in found), found
        )

    def test_the_command_exits_one_on_them(self):
        (self.repo / "docs/meridian-plan.md").write_bytes(b"\xff")
        self.mod.REPO = self.repo
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(self.mod.main(), 1)
        self.assertIn("[plan-files]", err.getvalue())


class ConflictMarkers(PlanCase):
    """M5: plan_port.py leaves markers for a person; nothing may commit them."""

    CONFLICT = "<<<<<<< main\nours\n=======\ntheirs\n>>>>>>> branch\n"

    def test_a_conflict_in_the_plan_a_step_file_and_an_entry(self):
        cases = (
            ("docs/meridian-plan.md", PLAN, "\n" + self.CONFLICT),
            ("docs/plan/steps/S001.md", STEP, self.CONFLICT),
            ("docs/plan/changelog/pr-0140.md", NEW_ENTRY, self.CONFLICT),
        )
        for name, right, conflict in cases:
            with self.subTest(file=name):
                self.write(name, right + conflict)
                found = [x for x in self.found() if "conflict marker" in x]
                self.assertEqual(len(found), 1, found)
                self.assertIn(name, found[0])
                self.write(name, right)

    def test_each_marker_alone_is_enough(self):
        for line in ("<<<<<<< main", ">>>>>>> branch", "<<<<<<<"):
            with self.subTest(line=line):
                self.write("docs/plan/steps/S001.md", STEP + line + "\n")
                self.assertIn("conflict marker at line 5", self.one())

    def test_the_equals_line_between_the_markers_is_counted(self):
        self.write(
            "docs/plan/steps/S001.md", STEP + "<<<<<<< main\n=======\n>>>>>>> branch\n"
        )
        self.assertIn("line 5, 6, 7", self.one())

    def test_a_bare_equals_line_outside_a_conflict_is_a_heading_underline(self):
        self.write("docs/plan/steps/S001.md", STEP + "Title\n=======\n")
        self.assertEqual(self.found(), [])

    def test_six_markers_or_a_marker_in_the_middle_of_a_line_are_not_markers(self):
        self.write("docs/plan/steps/S001.md", STEP + "<<<<<< six\nx <<<<<<< y\n")
        self.assertEqual(self.found(), [])


class OtherChecksReadTheFolders(unittest.TestCase):
    """The documents check's line-width, link and index rules, on docs/plan/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        for folder in ("docs/architecture", ".claude", ".agents"):
            (self.repo / folder).mkdir(parents=True)
        self.check = load_checker_for(self.repo)
        self.check.DOCS_INDEX = self.repo / "docs" / "README.md"
        self.check.ARCH_INDEX = self.repo / "docs" / "architecture" / "README.md"
        self.write("docs/README.md", "- [Plan](meridian-plan.md)\n")
        self.write("docs/architecture/README.md", "- [x](x.md)\n")
        self.write("docs/meridian-plan.md", "## Plan\n")
        self.write("docs/architecture/x.md", "x\n")

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self, check):
        found = self.check.Failures()
        check(found)
        return [detail for _, detail in found]

    def test_a_long_prose_line_in_a_step_file_is_reported(self):
        self.write(
            "docs/plan/steps/S001.md", "### S001 — One\n\n" + "word " * 30 + "\n"
        )
        found = self.run_check(self.check.check_line_width)
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/plan/steps/S001.md:3", found[0])

    def test_a_long_prose_line_in_an_entry_is_reported(self):
        self.write(
            "docs/plan/changelog/v0.01.md",
            "- **v0.1, 2026-09-29:** " + "word " * 30 + "\n",
        )
        found = self.run_check(self.check.check_line_width)
        self.assertIn("docs/plan/changelog/v0.01.md:1", found[0])

    def test_a_table_row_in_a_step_file_may_be_long(self):
        self.write(
            "docs/plan/steps/S001.md",
            "### S001 — One\n\n| a |\n|---|\n| " + "w " * 60 + "|\n",
        )
        self.assertEqual(self.run_check(self.check.check_line_width), [])

    def test_a_link_in_a_step_file_is_resolved_from_its_folder(self):
        self.write("docs/plan/steps/S001.md", "[ok](../../architecture/x.md)\n")
        self.assertEqual(self.run_check(self.check.check_links), [])

    def test_a_link_still_written_for_the_old_place_is_reported(self):
        self.write("docs/plan/steps/S001.md", "[old](architecture/x.md)\n")
        found = self.run_check(self.check.check_links)
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/plan/steps/S001.md: [old](architecture/x.md)", found[0])

    def test_a_broken_link_in_an_entry_is_reported(self):
        self.write(
            "docs/plan/changelog/v0.01.md",
            "- **v0.1, 2026-09-29:** [x](../../nope.md)\n",
        )
        self.assertEqual(len(self.run_check(self.check.check_links)), 1)

    def test_the_files_of_docs_plan_need_no_link_from_the_docs_index(self):
        self.write("docs/plan/steps/S001.md", "### S001 — One\n")
        self.write("docs/plan/changelog/v0.01.md", "- **v0.1, 2026-09-29:** x\n")
        self.assertEqual(self.run_check(self.check.check_docs_index), [])

    def test_another_document_still_needs_its_link(self):
        self.write("docs/notes.md", "Notes.\n")
        self.write("docs/plan/steps/S001.md", "### S001 — One\n")
        found = self.run_check(self.check.check_docs_index)
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md", found[0])


class TheRealRepository(unittest.TestCase):
    def test_the_plan_and_its_folders_agree(self):
        mod = load()
        problems, _ = mod.problems(mod.REPO, ci=False)
        self.assertEqual(problems, [])

    def test_make_docs_runs_the_check(self):
        makefile = (HERE.parent / "Makefile").read_text()
        self.assertIn("python3 scripts/check_plan_files.py", makefile)


if __name__ == "__main__":
    unittest.main()
