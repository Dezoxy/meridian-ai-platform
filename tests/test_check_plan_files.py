"""Tests for scripts/check_plan_files.py and the docs index's carve-out (S097, S100).

The plan's step sections are files in folders of 20 steps, the follow-up backlog is
two files, and the check keeps those files and the plan one story. Each test builds
a small tree that is right, plants one violation and expects one finding; the
clean tree must pass, and the real repository must pass too. The status line, Part F
and ``--write`` (S101) are tested in test_plan_progress.py, which shares this
fixture.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_check_docs_consistency import load_checker_for  # noqa: E402

SCRIPT = HERE.parent / "scripts" / "check_plan_files.py"
FENCE = "`" * 3

BEGIN = '<!-- plan-progress: begin (written by "make plan-progress", never by hand) -->'
END = "<!-- plan-progress: end -->"
FINISHED_HEAD = (
    "\n### Finished steps\n\n| Finished | Step | Title | File |\n|---|---|---|---|\n"
)
FLIGHT_HEAD = (
    "\n### In flight\n\n"
    "| Started | Step | Title | Status | File |\n|---|---|---|---|---|\n"
)


def block(finished="", flight=""):
    """The lines between the markers, as the plan holds them, from literal rows."""
    return FINISHED_HEAD + finished + FLIGHT_HEAD + flight + "\n"


def part_f(between):
    return f"## Part F — Where the steps stand\n\n{BEGIN}\n{between}{END}\n"


# The literal block for the fixture's one step, S001 (done, finished 2026-09-28).
S001_ROW = "| 2026-09-28 | S001 | One | [S001.md](plan/steps/S000-S019/S001.md) |\n"
PLAN_BEFORE_F = f"""# Plan

## Part B — Roadmap and step list

| ID | Step | Done when | Depends |
|---|---|---|---|
| S001 | One | x | — |
| S002 | Two | x | — |

## Part C — Step details

Template:

{FENCE}text
### S0xx — <title>
{FENCE}

## Part D — Open questions

## Part E — Changelog

The change log ended with S100.

"""
PLAN = PLAN_BEFORE_F + part_f(block(S001_ROW))


def in_part_e(text):
    """The right plan with ``text`` at the end of Part E, before Part F."""
    return PLAN.replace("## Part F", text + "\n## Part F")


STATUS = "**Status:** done · **Started:** 2026-09-27 · **Finished:** 2026-09-28"
STEP = f"### S001 — One\n{STATUS}\nBody.\n\n"
ENTRY = "- **#140, 2026-10-08:** a pull request's entry.\n"
HEADER = "| Item | Raised in | Status | Home |\n|---|---|---|---|\n"
OPEN_ROW = "| An open item | S010 | open | S030 |\n"
CLOSED_ROW = "| A closed item | S010 | closed by S011 | S011 |\n"
BACKLOG = "# Open rows\n\nProse.\n\n" + HEADER + OPEN_ROW
BACKLOG_CLOSED = "# Closed rows\n\nProse.\n\n" + HEADER + CLOSED_ROW


def row(status, home="S030", item="An item", raised="S010"):
    return f"| {item} | {raised} | {status} | {home} |\n"


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
        self.write("docs/plan/steps/S000-S019/S001.md", STEP)
        self.write("docs/plan/backlog.md", BACKLOG)
        self.write("docs/plan/backlog-closed.md", BACKLOG_CLOSED)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        return path

    def found(self):
        return self.mod.problems(self.repo)

    def one(self):
        found = self.found()
        self.assertEqual(len(found), 1, found)
        return found[0]


class PlanFiles(PlanCase):
    def test_a_right_tree_passes(self):
        self.assertEqual(self.found(), [])

    def test_a_repository_without_a_plan_is_skipped(self):
        (self.repo / "docs/meridian-plan.md").unlink()
        self.assertEqual(self.found(), [])

    def test_a_plan_that_promises_no_file_needs_no_folder(self):
        bare = "## Part B — x\n\n## Part C — x\n\n## Part D — x\n\n## Part E — x\n\n"
        self.write("docs/meridian-plan.md", bare + part_f(block()))
        (self.repo / "docs/plan/steps/S000-S019/S001.md").unlink()
        self.assertEqual(self.found(), [])

    def test_the_command_passes_on_a_right_tree_and_says_what_it_holds(self):
        self.mod.REPO = self.repo
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.mod.main(), 0)
        self.assertIn(
            "plan files: step folders, statuses and backlog agree with the plan",
            out.getvalue(),
        )


class FolderOf(PlanCase):
    def test_a_step_is_floored_to_twenty(self):
        for step, folder in (
            ("S000", "S000-S019"),
            ("S019", "S000-S019"),
            ("S020", "S020-S039"),
            ("S021", "S020-S039"),
            ("S099", "S080-S099"),
            ("S100", "S100-S119"),
            ("S139", "S120-S139"),
        ):
            with self.subTest(step=step):
                self.assertEqual(self.mod.folder_of(step), folder)


class StepFolders(PlanCase):
    def test_a_flat_step_file_says_where_it_goes(self):
        (self.repo / "docs/plan/steps/S000-S019/S001.md").unlink()
        self.write("docs/plan/steps/S001.md", STEP)
        found = self.found()
        # A flat file is not in a range folder, so the block lacks it (stale).
        self.assertEqual(len(found), 2, found)
        self.assertEqual(
            found[0],
            "docs/plan/steps/S001.md: a step file sits in its folder of 20; "
            "move it to docs/plan/steps/S000-S019/S001.md",
        )
        self.assertIn("make plan-progress", found[1])

    def test_a_file_in_the_wrong_folder_names_the_right_one(self):
        (self.repo / "docs/plan/steps/S000-S019/S001.md").unlink()
        self.write("docs/plan/steps/S020-S039/S001.md", STEP)
        found = self.one()
        self.assertIn("docs/plan/steps/S020-S039/S001.md", found)
        self.assertIn("docs/plan/steps/S000-S019/", found)

    def test_a_folder_with_a_wrong_name_is_reported(self):
        wrong = ("S001-S020", "S020-S040", "S010-S029", "notes", "S00-S19", "s000-s019")
        for name in wrong:
            with self.subTest(name=name):
                path = self.write(f"docs/plan/steps/{name}/S001.md", STEP)
                found = self.one()
                self.assertIn(f"docs/plan/steps/{name}", found)
                self.assertIn("folder", found)
                path.unlink()
                path.parent.rmdir()

    def test_a_file_with_a_range_name_or_any_other_entry_in_steps_is_reported(self):
        for name in ("S020-S039", "README.md"):
            with self.subTest(name=name):
                path = self.write(f"docs/plan/steps/{name}", "x")
                self.assertIn(f"docs/plan/steps/{name}", self.one())
                path.unlink()

    def test_a_misnamed_file_in_a_folder_is_reported(self):
        for name in ("s001.md", "S1.md", "S001.txt", "S0010.md"):
            with self.subTest(name=name):
                path = self.write(f"docs/plan/steps/S000-S019/{name}", STEP)
                self.assertIn("only S0NN.md files belong", self.one())
                path.unlink()

    def test_an_empty_folder_of_the_right_name_passes(self):
        (self.repo / "docs/plan/steps/S020-S039").mkdir()
        self.assertEqual(self.found(), [])

    def test_a_file_without_a_row_in_part_b_is_reported(self):
        self.write("docs/plan/steps/S000-S019/S003.md", "### S003 — Three\n")
        found = self.found()
        self.assertTrue(any("S003 has no row in Part B" in x for x in found), found)

    def test_a_file_whose_first_line_is_another_steps_heading_is_reported(self):
        step = STEP.replace("### S001 — One", "### S002 — Two")
        self.write("docs/plan/steps/S000-S019/S001.md", step)
        self.assertIn("first line must be the heading '### S001", self.one())

    def test_a_file_that_opens_with_a_blank_line_is_reported(self):
        self.write("docs/plan/steps/S000-S019/S001.md", "\n" + STEP)
        self.assertIn("first line must be the heading", self.one())

    def test_a_step_section_left_in_part_c_is_reported(self):
        plan = PLAN.replace(
            "## Part D", "### S002 — Two\n\nBody that belongs in a file.\n\n## Part D"
        )
        self.write("docs/meridian-plan.md", plan)
        self.assertIn("the plan holds a step section", self.one())

    def test_the_templates_heading_in_a_fence_is_not_a_section(self):
        self.assertEqual(self.found(), [])


class BackLog(PlanCase):
    def test_the_header_left_in_the_plan_is_one_finding(self):
        plan = PLAN.replace("## Part D", HEADER + OPEN_ROW + OPEN_ROW + "\n## Part D")
        self.write("docs/meridian-plan.md", plan)
        found = self.one()
        self.assertIn("meridian-plan.md", found)
        self.assertIn("docs/plan/backlog.md", found)

    def test_the_header_in_a_fence_of_the_plan_is_not_the_table(self):
        fenced = f"{FENCE}text\n{HEADER}{FENCE}\n"
        plan = PLAN.replace("## Part D", fenced + "\n## Part D")
        self.write("docs/meridian-plan.md", plan)
        self.assertEqual(self.found(), [])

    def test_a_closed_row_in_the_open_file_is_reported(self):
        self.write("docs/plan/backlog.md", BACKLOG + CLOSED_ROW)
        self.assertEqual(
            self.one(),
            "docs/plan/backlog.md: a closed row ('A closed item'): move it, whole, "
            "to the end of docs/plan/backlog-closed.md",
        )

    def test_the_item_is_quoted_to_forty_characters(self):
        closed = row("done in S011", item="x" * 60, home="S011")
        self.write("docs/plan/backlog.md", BACKLOG + closed)
        self.assertIn(f"('{'x' * 40}')", self.one())

    def test_closed_in_part_and_closed_for_stay_in_the_open_file(self):
        for status in (
            "closed in part by S011; the rest is open",
            "closed for the first case; the second is open",
            "Closed in part",
        ):
            with self.subTest(status=status):
                self.write("docs/plan/backlog.md", BACKLOG + row(status))
                self.assertEqual(self.found(), [])

    def test_done_and_closed_start_a_closed_row_whatever_the_case(self):
        for status in ("closed by S011", "Closed by S011", "done", "Done in S011"):
            with self.subTest(status=status):
                self.write("docs/plan/backlog.md", BACKLOG + row(status))
                self.assertIn("a closed row", self.one())

    def test_a_struck_through_beginning_is_skipped_before_the_status_is_read(self):
        self.write("docs/plan/backlog.md", BACKLOG + row("~~open; x~~ closed by S001"))
        self.assertIn("a closed row", self.one())
        self.write("docs/plan/backlog.md", BACKLOG + row("~~open~~ decided"))
        self.assertEqual(self.found(), [])

    def test_an_open_row_in_the_closed_file_is_reported(self):
        for status in ("open", "declined in S011, with reasons", "partly closed"):
            with self.subTest(status=status):
                self.write(
                    "docs/plan/backlog-closed.md",
                    BACKLOG_CLOSED + row(status, item="A late item"),
                )
                found = self.one()
                self.assertIn("docs/plan/backlog-closed.md", found)
                self.assertIn("A late item", found)
                self.assertIn("docs/plan/backlog.md", found)
                self.assertIn("closed", found)

    def test_closed_in_part_and_closed_for_do_not_belong_in_the_closed_file(self):
        for status in ("closed in part by S011", "closed for one case"):
            with self.subTest(status=status):
                self.write("docs/plan/backlog-closed.md", BACKLOG_CLOSED + row(status))
                self.assertIn("backlog.md", self.one())

    def test_a_closed_row_with_no_home_is_fine_in_the_closed_file(self):
        self.write(
            "docs/plan/backlog-closed.md", BACKLOG_CLOSED + row("closed", home="none")
        )
        self.assertEqual(self.found(), [])

    def test_a_five_cell_row_is_read_by_its_last_two_cells(self):
        extra = "| An item | S010 | a stray cell | open | S030 |\n"
        self.write("docs/plan/backlog.md", BACKLOG + extra)
        self.assertEqual(self.found(), [])
        extra = "| An item | S010 | a stray cell | closed by S011 | S011 |\n"
        self.write("docs/plan/backlog.md", BACKLOG + extra)
        self.assertIn("a closed row", self.one())

    def test_an_escaped_pipe_is_not_a_cell_boundary(self):
        # Three cells with the escape; a plain split on `|` would read four.
        self.write("docs/plan/backlog.md", BACKLOG + "| a \\| b | open | S030 |\n")
        self.assertIn("fewer than four cells", self.one())

    def test_a_last_cell_ending_in_an_escaped_pipe_or_no_final_pipe(self):
        cells = self.mod.cells_of
        self.assertEqual(
            cells("| a | b | open | S030 \\|"), ["a", "b", "open", "S030 \\|"]
        )
        self.assertEqual(cells("| a | b | open | S030"), ["a", "b", "open", "S030"])
        for tail in ("S030 \\|", "S030"):
            with self.subTest(tail=tail):
                self.write(
                    "docs/plan/backlog.md", BACKLOG + f"| a | S010 | open | {tail}\n"
                )
                self.assertEqual(self.found(), [])

    def test_a_header_is_read_by_its_cells_in_the_files_and_the_plan(self):
        shapes = (
            "|Item|Raised in|Status|Home|",
            "| Item  | Raised in | Status | Home |",
            "| Item | Raised in | Status | Home | Notes |",
            "  | Item | Raised in | Status | Home |",
        )
        for shape in shapes:
            with self.subTest(shape=shape):
                text = "# Open\n\n" + shape + "\n|---|\n" + OPEN_ROW + CLOSED_ROW
                self.write("docs/plan/backlog.md", text)
                self.assertIn("a closed row", self.one())
                self.write("docs/plan/backlog.md", BACKLOG)
                plan = PLAN.replace("## Part D", f"{shape}\n|---|\n\n## Part D")
                self.write("docs/meridian-plan.md", plan)
                self.assertIn("left the plan", self.one())
                self.write("docs/meridian-plan.md", PLAN)

    def test_a_code_fence_never_closed_hides_the_rows_after_it(self):
        self.write("docs/plan/backlog.md", BACKLOG + FENCE + "\n" + CLOSED_ROW)
        self.assertEqual(
            self.one(),
            "docs/plan/backlog.md: a code fence is opened and never closed; "
            "the rows after it are not read",
        )

    def test_a_closed_fence_holding_a_table_row_is_not_read_as_a_row(self):
        fenced = f"{FENCE}text\n{CLOSED_ROW}{FENCE}\n"
        self.write("docs/plan/backlog.md", BACKLOG + "\n" + fenced)
        self.assertEqual(self.found(), [])

    def test_a_row_of_three_cells_is_reported(self):
        self.write("docs/plan/backlog.md", BACKLOG + "| An item | open | S030 |\n")
        found = self.one()
        self.assertIn("docs/plan/backlog.md", found)
        self.assertIn("An item", found)

    def test_a_row_of_three_cells_in_the_closed_file_is_reported(self):
        short = "| An item | done | S011 |\n"
        self.write("docs/plan/backlog-closed.md", BACKLOG_CLOSED + short)
        self.assertIn("docs/plan/backlog-closed.md", self.one())

    def test_an_open_row_must_name_a_step(self):
        for home in ("none", "", "a later step", "S12"):
            with self.subTest(home=home):
                self.write("docs/plan/backlog.md", BACKLOG + row("open", home=home))
                found = self.one()
                self.assertIn("docs/plan/backlog.md", found)
                self.assertIn("step", found)

    def test_a_missing_backlog_file_is_not_a_finding_here(self):
        (self.repo / "docs/plan/backlog.md").unlink()
        (self.repo / "docs/plan/backlog-closed.md").unlink()
        self.assertEqual(self.found(), [])

    def test_a_file_without_the_header_or_with_it_twice_is_reported(self):
        for text in ("# Open rows\n\n" + OPEN_ROW, BACKLOG + "\n" + HEADER):
            with self.subTest(text=text[:20]):
                self.write("docs/plan/backlog.md", text)
                self.assertIn("docs/plan/backlog.md", self.one())

    def test_a_table_row_above_the_header_is_prose(self):
        self.write("docs/plan/backlog.md", "| a | b |\n\n" + BACKLOG)
        self.assertEqual(self.found(), [])

    def test_conflict_markers_in_a_backlog_file_are_reported(self):
        conflict = "<<<<<<< main\nours\n=======\ntheirs\n>>>>>>> branch\n"
        for name, right in (
            ("docs/plan/backlog.md", BACKLOG),
            ("docs/plan/backlog-closed.md", BACKLOG_CLOSED),
        ):
            with self.subTest(file=name):
                self.write(name, right + conflict)
                found = [x for x in self.found() if "conflict marker" in x]
                self.assertEqual(len(found), 1, found)
                self.assertIn(name, found[0])
                self.write(name, right)

    def test_a_backlog_file_that_is_not_utf8_is_reported(self):
        for name in ("docs/plan/backlog.md", "docs/plan/backlog-closed.md"):
            with self.subTest(file=name):
                right = (self.repo / name).read_bytes()
                (self.repo / name).write_bytes(right + b"\xff\xfe")
                self.assertIn("cannot be read as UTF-8", self.one())
                (self.repo / name).write_bytes(right)

    def test_a_directory_or_broken_link_with_a_backlogs_name_is_reported(self):
        (self.repo / "docs/plan/backlog.md").unlink()
        (self.repo / "docs/plan/backlog.md").mkdir()
        (self.repo / "docs/plan/backlog-closed.md").unlink()
        (self.repo / "docs/plan/backlog-closed.md").symlink_to(self.repo / "nowhere")
        found = self.found()
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all("not a regular file" in x for x in found), found)


class ChangeLogGone(PlanCase):
    REMEDY = "the change log ended with S100 (2026-10-08)"

    def test_a_changelog_folder_is_one_finding_empty_or_not(self):
        folder = self.repo / "docs/plan/changelog"
        folder.mkdir()
        found = self.one()
        self.assertTrue(found.startswith("docs/plan/changelog: "), found)
        self.assertIn(self.REMEDY, found)
        self.assertIn("pull request's description", found)
        self.write("docs/plan/changelog/pr-0140.md", ENTRY)
        self.write("docs/plan/changelog/v0.01.md", ENTRY)
        self.assertIn(self.REMEDY, self.one())

    def test_a_changelog_file_or_a_broken_link_is_a_finding_too(self):
        self.write("docs/plan/changelog", "x")
        self.assertIn(self.REMEDY, self.one())
        (self.repo / "docs/plan/changelog").unlink()
        (self.repo / "docs/plan/changelog").symlink_to(self.repo / "nowhere")
        self.assertIn(self.REMEDY, self.one())

    def test_an_entry_left_in_part_e_is_reported_with_the_same_remedy(self):
        self.write("docs/meridian-plan.md", in_part_e(ENTRY))
        found = self.one()
        self.assertIn("Part E of the plan holds an entry", found)
        self.assertIn(self.REMEDY, found)

    def test_the_stand_in_and_the_ci_switch_are_gone(self):
        self.assertFalse(hasattr(self.mod, "check_changelog"))
        self.assertFalse(hasattr(self.mod, "CHANGELOG_FILE"))
        self.assertFalse(hasattr(self.mod, "label_of"))
        with self.assertRaises(TypeError):
            self.mod.problems(self.repo, True)

    def test_the_check_imports_no_migration_script(self):
        text = SCRIPT.read_text()
        self.assertNotIn("plan_split", text)
        self.assertNotIn("sys.path", text)


class PartHeadings(PlanCase):
    """M3: a heading that cannot be found must not switch its check off."""

    def test_a_missing_heading_is_a_finding_for_each_part(self):
        for letter, head in (
            ("B", "## Part B — Roadmap and step list"),
            ("C", "## Part C — Step details"),
            ("D", "## Part D — Open questions"),
            ("E", "## Part E — Changelog"),
            ("F", "## Part F — Where the steps stand"),
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
        self.write("docs/meridian-plan.md", plan + ENTRY)
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
                self.write("docs/meridian-plan.md", in_part_e(entry))
                found = self.found()
                self.assertEqual(len(found), 1, found)
                self.assertIn("Part E of the plan holds an entry", found[0])

    def test_a_step_heading_in_any_old_form_is_reported(self):
        forms = (
            "### S002 — Two",
            "### S002 - Two",
            "### S002 \N{EN DASH} Two",
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


class Scanning(PlanCase):
    """scan and heading_offsets lost their other home with the migration script."""

    def fenced(self, text):
        return [(line.text, line.fenced) for line in self.mod.scan(text)]

    def test_a_backtick_fence_covers_its_lines_and_its_own(self):
        text = f"a\n{FENCE}\nb\n{FENCE}\nc"
        self.assertEqual(
            self.fenced(text),
            [("a", False), (FENCE, True), ("b", True), (FENCE, True), ("c", False)],
        )

    def test_a_tilde_fence_is_a_fence(self):
        self.assertEqual(
            self.fenced("~~~\nb\n~~~\nc"),
            [("~~~", True), ("b", True), ("~~~", True), ("c", False)],
        )

    def test_a_fence_is_closed_only_by_a_marker_of_its_kind(self):
        text = f"~~~\n{FENCE}\nstill inside\n~~~\nout"
        self.assertEqual(
            [f for _, f in self.fenced(text)], [True, True, True, True, False]
        )

    def test_a_fence_is_closed_only_by_a_marker_at_least_as_long(self):
        text = f"````\n{FENCE}\nstill inside\n````\nout"
        self.assertEqual(
            [f for _, f in self.fenced(text)], [True, True, True, True, False]
        )

    def test_a_closing_marker_with_text_after_it_does_not_close(self):
        text = f"{FENCE}\n{FENCE}python\nstill inside\n{FENCE}\nout"
        self.assertEqual(
            [f for _, f in self.fenced(text)], [True, True, True, True, False]
        )

    def test_each_line_carries_its_offset(self):
        lines = self.mod.scan("ab\ncde\nf")
        self.assertEqual([line.start for line in lines], [0, 3, 7])

    def test_a_heading_is_found_at_its_offset(self):
        text = "x\n## Part B — a\ny\n## Part C — b\n"
        self.assertEqual(self.mod.heading_offsets(text), {"B": 2, "C": 18})

    def test_a_heading_inside_a_fence_is_not_found(self):
        text = f"{FENCE}\n## Part B — a\n{FENCE}\n## Part C — b\n"
        self.assertEqual(list(self.mod.heading_offsets(text)), ["C"])

    def test_a_doubled_heading_raises_plan_error(self):
        with self.assertRaises(self.mod.PlanError) as caught:
            self.mod.heading_offsets("## Part B — a\n## Part B — b\n")
        self.assertIn("Part B has two headings", str(caught.exception))


class OddFiles(PlanCase):
    """M4: an odd file is a finding, never a traceback."""

    STEP_PATH = "docs/plan/steps/S000-S019/S001.md"

    def test_a_step_file_that_is_not_utf8(self):
        (self.repo / self.STEP_PATH).write_bytes(b"### S001 \xff\xfe\n")
        self.assertIn("cannot be read as UTF-8", self.one())

    def test_a_directory_with_a_steps_name(self):
        (self.repo / self.STEP_PATH).unlink()
        (self.repo / self.STEP_PATH).mkdir()
        found = self.found()
        self.assertTrue(any("not a regular file" in x for x in found), found)

    def test_a_broken_link_with_a_steps_name(self):
        (self.repo / self.STEP_PATH).unlink()
        (self.repo / self.STEP_PATH).symlink_to(self.repo / "nowhere")
        found = self.found()
        self.assertTrue(any("not a regular file" in x for x in found), found)

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
    """A merge leaves markers for a person; nothing may commit them."""

    CONFLICT = "<<<<<<< main\nours\n=======\ntheirs\n>>>>>>> branch\n"
    STEP_PATH = "docs/plan/steps/S000-S019/S001.md"

    def test_a_conflict_in_the_plan_and_a_step_file(self):
        cases = (
            ("docs/meridian-plan.md", PLAN, "\n" + self.CONFLICT),
            (self.STEP_PATH, STEP, self.CONFLICT),
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
                self.write(self.STEP_PATH, STEP + line + "\n")
                self.assertIn("conflict marker at line 5", self.one())

    def test_the_equals_line_between_the_markers_is_counted(self):
        self.write(self.STEP_PATH, STEP + "<<<<<<< main\n=======\n>>>>>>> branch\n")
        self.assertIn("line 5, 6, 7", self.one())

    def test_a_bare_equals_line_outside_a_conflict_is_a_heading_underline(self):
        self.write(self.STEP_PATH, STEP + "Title\n=======\n")
        self.assertEqual(self.found(), [])

    def test_six_markers_or_a_marker_in_the_middle_of_a_line_are_not_markers(self):
        self.write(self.STEP_PATH, STEP + "<<<<<< six\nx <<<<<<< y\n")
        self.assertEqual(self.found(), [])


class OtherChecksReadTheFolders(unittest.TestCase):
    """The documents check's line-width, link and index rules, on docs/plan/."""

    STEP_PATH = "docs/plan/steps/S000-S019/S001.md"

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
        self.write(self.STEP_PATH, "### S001 — One\n\n" + "word " * 30 + "\n")
        found = self.run_check(self.check.check_line_width)
        self.assertEqual(len(found), 1, found)
        self.assertIn(f"{self.STEP_PATH}:3", found[0])

    def test_a_table_row_in_a_step_file_may_be_long(self):
        self.write(
            self.STEP_PATH,
            "### S001 — One\n\n| a |\n|---|\n| " + "w " * 60 + "|\n",
        )
        self.assertEqual(self.run_check(self.check.check_line_width), [])

    def test_a_long_table_row_in_the_backlog_is_allowed(self):
        self.write(
            "docs/plan/backlog.md",
            HEADER + "| " + "word " * 60 + "| S010 | open | S030 |\n",
        )
        self.assertEqual(self.run_check(self.check.check_line_width), [])

    def test_a_link_in_a_step_file_is_resolved_from_its_folder(self):
        self.write(self.STEP_PATH, "[ok](../../../architecture/x.md)\n")
        self.assertEqual(self.run_check(self.check.check_links), [])

    def test_a_link_still_written_for_the_old_place_is_reported(self):
        self.write(self.STEP_PATH, "[old](../../architecture/x.md)\n")
        found = self.run_check(self.check.check_links)
        self.assertEqual(len(found), 1, found)
        self.assertIn(f"{self.STEP_PATH}: [old](../../architecture/x.md)", found[0])

    def test_the_files_of_docs_plan_need_no_link_from_the_docs_index(self):
        self.write(self.STEP_PATH, "### S001 — One\n")
        self.write("docs/plan/backlog.md", BACKLOG)
        self.write("docs/plan/backlog-closed.md", BACKLOG_CLOSED)
        self.assertEqual(self.run_check(self.check.check_docs_index), [])

    def test_a_folder_named_plan_elsewhere_under_docs_still_needs_its_link(self):
        self.write("docs/architecture/plan/notes.md", "Notes.\n")
        found = self.run_check(self.check.check_docs_index)
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/architecture/plan/notes.md", found[0])

    def test_another_document_still_needs_its_link(self):
        self.write("docs/notes.md", "Notes.\n")
        self.write(self.STEP_PATH, "### S001 — One\n")
        found = self.run_check(self.check.check_docs_index)
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md", found[0])


class TheRealRepository(unittest.TestCase):
    def test_the_plan_and_its_files_agree(self):
        mod = load()
        self.assertEqual(mod.problems(mod.REPO), [])

    def test_make_docs_runs_the_check(self):
        makefile = (HERE.parent / "Makefile").read_text()
        self.assertIn("python3 scripts/check_plan_files.py", makefile)

    def test_make_plan_progress_writes_the_block(self):
        lines = (HERE.parent / "Makefile").read_text().split("\n")
        at = lines.index("plan-progress:")
        self.assertEqual(lines[at + 1], "\tpython3 scripts/check_plan_files.py --write")
        self.assertTrue(lines[at - 1].startswith("## plan-progress "))
        phony = next(x for x in lines if x.startswith(".PHONY:"))
        self.assertIn("plan-progress", phony.split())


if __name__ == "__main__":
    unittest.main()
