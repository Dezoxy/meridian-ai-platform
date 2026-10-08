"""Tests for the step status line, Part F and ``--write`` of check_plan_files.py (S101).

A step's status is one line in its own file; Part B's tables hold only the step
numbers; the plan's last part holds two generated tables between two marker
lines. Each test starts from the right tree of test_check_plan_files.py (one
done step, S001), plants one fault and expects one finding.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import io
import os
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_check_plan_files import (  # noqa: E402
    BACKLOG,
    BEGIN,
    END,
    ENTRY,
    FENCE,
    PLAN,
    PLAN_BEFORE_F,
    S001_ROW,
    STEP,
    PlanCase,
    block,
    in_part_e,
    part_f,
)

STEP_PATH = "docs/plan/steps/S000-S019/S001.md"
PLAN_PATH = "docs/meridian-plan.md"
FORM = "**Status:** <word> · **Started:** <date|—> · **Finished:** <date|—>"


def status(word="done", started="2026-09-27", finished="2026-09-28", note=""):
    return (
        f"**Status:** {word} · **Started:** {started} · **Finished:** {finished}{note}"
    )


def step_text(line, title="One", step="S001"):
    return f"### {step} — {title}\n{line}\nBody.\n\n"


def link(step):
    low = int(step[1:]) // 20 * 20
    return f"[{step}.md](plan/steps/S{low:03d}-S{low + 19:03d}/{step}.md)"


def done_row(step, title, finished):
    return f"| {finished} | {step} | {title} | {link(step)} |\n"


def flight_row(step, title, started, word):
    return f"| {started} | {step} | {title} | {word} | {link(step)} |\n"


def plan_with(finished="", flight=""):
    return PLAN_BEFORE_F + part_f(block(finished, flight))


class StatusLine(PlanCase):
    def put(self, text):
        self.write(STEP_PATH, text)

    def test_a_missing_line_names_the_file_and_the_form(self):
        self.put("### S001 — One\n\nBody.\n\n")
        found = self.one()
        self.assertIn(STEP_PATH, found)
        self.assertIn(FORM, found)

    def test_a_line_on_line_four_is_too_late(self):
        self.put(f"### S001 — One\n\nBody.\n{status()}\n")
        self.assertIn(FORM, self.one())

    def test_a_line_on_line_two_or_three_is_enough(self):
        self.put(f"### S001 — One\n\n{status()}\nBody.\n")
        self.assertEqual(self.found(), [])
        self.put(STEP)
        self.assertEqual(self.found(), [])

    def test_a_line_on_both_line_two_and_three_is_one_finding(self):
        self.put(f"### S001 — One\n{status()}\n{status()}\nBody.\n")
        found = self.one()
        self.assertIn(STEP_PATH, found)
        self.assertIn("both", found)
        self.assertIn(FORM, found)

    def test_an_unknown_word_names_the_words(self):
        self.put(step_text(status("finished")))
        found = self.one()
        self.assertIn("'finished'", found)
        self.assertIn("none of todo, doing, done, blocked, dropped", found)
        self.assertIn(FORM, found)

    def test_done_needs_a_finished_date(self):
        self.put(step_text(status("done", finished="—")))
        found = self.one()
        self.assertIn(STEP_PATH, found)
        self.assertIn("needs a Finished date", found)
        self.assertIn(FORM, found)

    def test_every_other_word_needs_finished_to_be_a_dash(self):
        for word in ("todo", "doing", "blocked", "dropped"):
            with self.subTest(word=word):
                self.put(step_text(status(word, finished="2026-09-28")))
                found = self.one()
                self.assertIn(f"a '{word}' step has Finished", found)
                self.assertIn(FORM, found)

    def test_a_date_of_another_shape_is_a_finding(self):
        for date in ("2026-9-28", "28.09.2026", "2026-09-28T10:00", "yesterday", ""):
            for field in ("started", "finished"):
                with self.subTest(date=date, field=field):
                    self.put(step_text(status(**{field: date})))
                    self.assertIn(FORM, self.one())

    def test_a_hyphen_or_a_dot_in_place_of_the_separator_is_a_finding(self):
        good = status()
        for bad in (good.replace(" · ", " - "), good.replace(" · ", " | ")):
            with self.subTest(bad=bad):
                self.put(step_text(bad))
                self.assertIn(FORM, self.one())

    def test_a_note_after_the_line_passes_and_is_not_in_the_block(self):
        self.put(step_text(status(note=" · first half written in plan v0.86")))
        self.assertEqual(self.found(), [])

    def test_an_empty_note_is_not_a_note(self):
        self.put(step_text(status() + " · "))
        self.assertIn(FORM, self.one())

    def test_todo_with_no_start_passes_and_stands_in_flight(self):
        self.put(step_text(status("todo", started="—", finished="—")))
        self.write(PLAN_PATH, plan_with(flight=flight_row("S001", "One", "—", "todo")))
        self.assertEqual(self.found(), [])

    def test_blocked_and_dropped_pass_and_stand_in_flight(self):
        for word in ("blocked", "dropped"):
            with self.subTest(word=word):
                self.put(step_text(status(word, finished="—")))
                row = flight_row("S001", "One", "2026-09-27", word)
                self.write(PLAN_PATH, plan_with(flight=row))
                self.assertEqual(self.found(), [])

    def test_a_step_file_without_a_row_in_part_b_is_still_reported(self):
        self.write(
            "docs/plan/steps/S000-S019/S003.md",
            step_text(status("todo", "—", "—"), "Three", "S003"),
        )
        found = self.found()
        self.assertTrue(any("S003 has no row in Part B" in x for x in found), found)

    def test_a_row_in_a_table_of_another_part_is_not_a_row_of_part_b(self):
        table = "\n| ID | Step |\n|---|---|\n| S003 | Three |\n"
        self.write(
            "docs/plan/steps/S000-S019/S003.md",
            step_text(status("todo", "—", "—"), "Three", "S003"),
        )
        for head in (
            "## Part C — Step details\n",
            "## Part D — Open questions\n",
            "## Part E — Changelog\n",
            "## Part F — Where the steps stand\n",
        ):
            with self.subTest(part=head[:9]):
                self.assertIn(head, PLAN)
                self.write(PLAN_PATH, PLAN.replace(head, head + table))
                found = self.found()
                self.assertTrue(
                    any("S003 has no row in Part B" in x for x in found), found
                )


class PartB(PlanCase):
    HEADER = "| ID | Step | Done when | Depends |"

    def test_a_status_column_is_one_finding_a_table(self):
        plan = PLAN.replace(self.HEADER, "| ID | Step | Done when | Status | Depends |")
        self.write(PLAN_PATH, plan)
        found = self.one()
        self.assertIn("Status", found)
        self.assertIn("S101", found)
        second = "| ID | Step | Status |\n|---|---|---|\n| S002 | Two | todo |\n"
        self.write(PLAN_PATH, plan.replace("## Part C", second + "\n## Part C"))
        self.assertEqual(len(self.found()), 2, self.found())

    def test_a_status_column_in_a_table_of_another_part_is_not_part_bs(self):
        table = "\n| ID | Step | Status |\n|---|---|---|\n"
        # Not Part F: its generated table of steps in flight has a Status column.
        for head in (
            "## Part C — Step details\n",
            "## Part D — Open questions\n",
            "## Part E — Changelog\n",
        ):
            with self.subTest(part=head[:9]):
                self.assertIn(head, PLAN)
                self.write(PLAN_PATH, PLAN.replace(head, head + table))
                self.assertEqual(self.found(), [])

    def test_the_header_in_a_fence_is_not_a_table(self):
        fenced = f"{FENCE}text\n| ID | Step | Status | Depends |\n{FENCE}\n"
        self.write(PLAN_PATH, PLAN.replace("## Part C", fenced + "\n## Part C"))
        self.assertEqual(self.found(), [])

    def test_another_table_with_a_status_column_is_not_a_step_table(self):
        other = "| Item | Status |\n|---|---|\n| a | done |\n"
        self.write(PLAN_PATH, PLAN.replace("## Part C", other + "\n## Part C"))
        self.assertEqual(self.found(), [])

    def test_the_word_done_in_a_cell_is_not_a_status(self):
        row = "| S002 | Two | done | doing |\n"
        self.write(PLAN_PATH, PLAN.replace("| S002 | Two | x | — |\n", row))
        self.assertEqual(self.found(), [])

    def test_a_row_with_no_file_is_fine_whatever_its_cells_say(self):
        extra = "| S003 | Three | done | doing |\n| S004 | Four | x | x | x | x |\n"
        self.write(PLAN_PATH, PLAN.replace("## Part C", extra + "\n## Part C"))
        self.assertEqual(self.found(), [])

    def test_no_message_wants_an_index_any_more(self):
        self.assertFalse(hasattr(self.mod, "INDEX_ROW"))
        self.assertFalse(hasattr(self.mod, "OPEN_STATUS"))
        self.assertTrue(hasattr(self.mod, "step_path"))

    def test_a_step_row_inside_a_fence_is_not_a_row(self):
        row = "| S002 | Two | x | — |\n"
        flight = flight_row("S002", "Two", "2026-09-27", "doing")
        plan = plan_with(finished=S001_ROW, flight=flight)
        self.write(PLAN_PATH, plan.replace(row, f"{FENCE}text\n{row}{FENCE}\n"))
        self.write(
            "docs/plan/steps/S000-S019/S002.md",
            step_text(status("doing", finished="—"), "Two", "S002"),
        )
        self.assertIn("S002 has no row in Part B", self.one())


class PartF(PlanCase):
    def test_a_plan_without_part_f_is_a_finding(self):
        self.write(PLAN_PATH, PLAN_BEFORE_F)
        # With no readable part B the step files have no row either: two findings.
        self.assertEqual(self.found()[0], "the plan has no '## Part F — ' heading")

    def test_part_f_before_part_e_is_a_finding(self):
        part = part_f(block(S001_ROW))
        plan = PLAN.replace(part, "").replace("## Part E", part + "\n## Part E")
        self.write(PLAN_PATH, plan)
        self.assertIn("B, C, D, E and F are not in that order", self.found()[0])

    def test_a_missing_begin_marker_is_one_finding(self):
        self.write(PLAN_PATH, PLAN.replace(BEGIN + "\n", ""))
        self.assertIn("the begin marker line", self.one())

    def test_a_missing_end_marker_is_one_finding(self):
        self.write(PLAN_PATH, PLAN.replace(END + "\n", ""))
        self.assertIn("the end marker line", self.one())

    def test_a_doubled_marker_is_one_finding(self):
        for marker in (BEGIN, END):
            with self.subTest(marker=marker):
                self.write(PLAN_PATH, PLAN.replace(marker, marker + "\n" + marker))
                self.assertIn("exactly once", self.one())

    def test_markers_in_the_wrong_order_are_one_finding(self):
        plan = PLAN.replace(BEGIN, "@@").replace(END, BEGIN).replace("@@", END)
        self.write(PLAN_PATH, plan)
        self.assertIn("begin marker must come before the end marker", self.one())

    def test_a_marker_only_inside_a_fence_is_not_there(self):
        self.write(PLAN_PATH, PLAN.replace(BEGIN, f"{FENCE}text\n{BEGIN}\n{FENCE}"))
        self.assertIn("the begin marker line", self.one())

    def test_a_marker_that_differs_by_a_word_is_not_the_marker(self):
        self.write(PLAN_PATH, PLAN.replace(" never by hand", " not by hand"))
        self.assertIn("the begin marker line", self.one())

    def test_an_entry_in_part_f_is_not_part_es_finding(self):
        self.write(PLAN_PATH, PLAN + "\n" + ENTRY)
        self.assertEqual(self.found(), [])

    def test_marker_lines_in_part_e_are_not_part_fs(self):
        self.write(PLAN_PATH, in_part_e(f"{BEGIN}\n{END}\n"))
        self.assertEqual(self.found(), [])


class Block(PlanCase):
    def test_a_changed_status_makes_the_block_stale(self):
        self.write(STEP_PATH, step_text(status("doing", finished="—")))
        found = self.one()
        self.assertIn("make plan-progress", found)
        self.assertIn('"Finished steps" and "In flight"', found)

    def test_a_new_step_file_makes_the_block_stale(self):
        row = "| S002 | Two | x | — |\n"
        self.assertIn(row, PLAN)
        self.write(
            "docs/plan/steps/S000-S019/S002.md",
            step_text(status("todo", "—", "—"), "Two", "S002"),
        )
        self.assertIn("make plan-progress", self.one())

    def test_a_deleted_step_file_makes_the_block_stale(self):
        (self.repo / STEP_PATH).unlink()
        self.assertIn("make plan-progress", self.one())

    def test_a_hand_edited_row_is_stale(self):
        self.write(PLAN_PATH, PLAN.replace("| One |", "| Uno |"))
        self.assertIn("make plan-progress", self.one())

    def test_a_hand_added_line_is_stale(self):
        self.write(PLAN_PATH, PLAN.replace(END, "A note.\n" + END))
        self.assertIn("make plan-progress", self.one())

    def test_no_step_at_all_is_two_empty_tables(self):
        (self.repo / STEP_PATH).unlink()
        self.write(PLAN_PATH, plan_with())
        self.assertEqual(self.found(), [])

    def test_a_step_with_a_status_finding_gives_that_one_not_the_stale_one(self):
        self.write(STEP_PATH, step_text(status("finished")))
        found = self.one()
        self.assertIn("status", found)
        self.assertNotIn("make plan-progress", found)

    def test_the_order_is_by_date_then_number_and_a_dash_starts_last(self):
        rows = "".join(f"| S00{n} | Step {n} | x | — |\n" for n in range(2, 9))
        base = PLAN.replace("| S002 | Two | x | — |\n", rows)
        files = {
            "S002": ("Step 2", status("done", "2026-09-01", "2026-09-28")),
            "S003": ("Step 3", status("done", "2026-09-01", "2026-09-28")),
            "S004": ("Step 4", status("todo", "—", "—")),
            "S005": ("Step 5", status("done", "2026-09-01", "2026-09-20")),
            "S006": ("Step 6", status("doing", "2026-10-01", "—", " · half")),
            "S007": ("Step 7", status("doing", "2026-09-30", "—")),
            "S008": ("Step 8", status("blocked", "2026-10-01", "—")),
        }
        for step, (title, line) in files.items():
            self.write(
                f"docs/plan/steps/S000-S019/{step}.md", step_text(line, title, step)
            )
        finished = (
            done_row("S005", "Step 5", "2026-09-20")
            + S001_ROW
            + done_row("S002", "Step 2", "2026-09-28")
            + done_row("S003", "Step 3", "2026-09-28")
        )
        flight = (
            flight_row("S007", "Step 7", "2026-09-30", "doing")
            + flight_row("S006", "Step 6", "2026-10-01", "doing")
            + flight_row("S008", "Step 8", "2026-10-01", "blocked")
            + flight_row("S004", "Step 4", "—", "todo")
        )
        right = base.replace(block(S001_ROW), block(finished, flight))
        self.write(PLAN_PATH, right)
        self.assertEqual(self.found(), [])
        # The same block with two rows swapped is stale: the order is checked.
        seven = flight_row("S007", "Step 7", "2026-09-30", "doing")
        six = flight_row("S006", "Step 6", "2026-10-01", "doing")
        swapped = flight.replace(seven + six, six + seven)
        self.assertNotEqual(swapped, flight)
        self.write(PLAN_PATH, base.replace(block(S001_ROW), block(finished, swapped)))
        self.assertIn("make plan-progress", self.one())

    def test_an_unescaped_pipe_in_a_title_is_escaped(self):
        for title, cell in (("One | Two", "One \\| Two"), ("a \\| b", "a \\| b")):
            with self.subTest(title=title):
                self.write(STEP_PATH, step_text(status(), title))
                row = done_row("S001", cell, "2026-09-28")
                self.write(PLAN_PATH, plan_with(finished=row))
                self.assertEqual(self.found(), [])
                self.write(PLAN_PATH, plan_with(finished=S001_ROW))
                self.assertIn("make plan-progress", self.one())


class Write(PlanCase):
    def run_main(self, *args):
        self.mod.REPO = self.repo
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.mod.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def plan_bytes(self):
        return (self.repo / PLAN_PATH).read_bytes()

    def stale(self):
        self.write(STEP_PATH, step_text(status("doing", finished="—")))

    def test_a_stale_tree_becomes_clean_and_a_second_run_changes_nothing(self):
        self.stale()
        self.assertEqual(len(self.found()), 1)
        code, out, _ = self.run_main("--write")
        self.assertEqual((code, out), (0, "plan progress: 0 finished, 1 in flight\n"))
        self.assertEqual(self.found(), [])
        written = self.plan_bytes()
        path = self.repo / PLAN_PATH
        os.utime(path, ns=(10**18, 10**18))
        code, out, _ = self.run_main("--write")
        self.assertEqual((code, out), (0, "plan progress: 0 finished, 1 in flight\n"))
        self.assertEqual(self.plan_bytes(), written)
        self.assertEqual(path.stat().st_mtime_ns, 10**18)

    def test_a_right_tree_is_not_touched(self):
        path = self.repo / PLAN_PATH
        os.utime(path, ns=(10**18, 10**18))
        code, out, _ = self.run_main("--write")
        self.assertEqual((code, out), (0, "plan progress: 1 finished, 0 in flight\n"))
        self.assertEqual(path.stat().st_mtime_ns, 10**18)
        self.assertEqual(self.plan_bytes(), PLAN.encode())

    def test_the_bytes_outside_the_markers_are_untouched(self):
        before = "Prose before.\r\n\tTabbed, trailing space \n"
        after = "\nAfter the end, with no final newline"
        plan = PLAN.replace(BEGIN, before + BEGIN).replace(END, END + after)
        plan = plan.rstrip("\n")
        self.write(PLAN_PATH, plan)
        self.stale()
        self.assertEqual(self.run_main("--write")[0], 0)
        written = self.plan_bytes().decode()
        head, rest = written.split(BEGIN + "\n", 1)
        between, tail = rest.split(END, 1)
        self.assertEqual(head, plan.split(BEGIN + "\n", 1)[0])
        self.assertEqual(tail, after)
        self.assertIn(flight_row("S001", "One", "2026-09-27", "doing"), between)

    def test_missing_markers_write_nothing_and_exit_one(self):
        plan = PLAN_BEFORE_F + "## Part F — Where the steps stand\n\nNo markers.\n"
        self.write(PLAN_PATH, plan)
        code, out, err = self.run_main("--write")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("[plan-files]", err)
        self.assertIn("the begin marker line", err)
        self.assertEqual(self.plan_bytes(), plan.encode())

    def test_a_bad_status_line_writes_nothing_and_exit_one(self):
        self.write(STEP_PATH, step_text(status("finished")))
        code, out, err = self.run_main("--write")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(STEP_PATH, err)
        self.assertEqual(self.plan_bytes(), PLAN.encode())

    def test_a_bad_heading_writes_nothing_too(self):
        self.write(STEP_PATH, STEP.replace("### S001", "### S002"))
        code, _, err = self.run_main("--write")
        self.assertEqual(code, 1)
        self.assertIn("first line must be", err)
        self.assertEqual(self.plan_bytes(), PLAN.encode())

    def test_a_finding_elsewhere_does_not_stop_the_write(self):
        self.write("docs/plan/backlog.md", BACKLOG + "| a | b |\n")
        self.stale()
        code, out, _ = self.run_main("--write")
        self.assertEqual(code, 0)
        self.assertIn("1 in flight", out)
        self.assertEqual(len(self.found()), 1)

    def test_step_files_that_cannot_be_listed_write_nothing(self):
        # A folder that cannot be read must not read as "no step": the block
        # would be rewritten without its rows.
        folder = self.repo / "docs/plan/steps"
        shutil.rmtree(folder)
        folder.write_text("not a folder\n", encoding="utf-8")
        code, out, err = self.run_main("--write")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("docs/plan/steps: cannot be listed", err)
        self.assertEqual(self.plan_bytes(), PLAN.encode())
        self.assertIn("cannot be listed", self.one())

    def test_a_plan_that_cannot_be_written_is_a_finding_not_a_traceback(self):
        if os.geteuid() == 0:
            self.skipTest("root writes a read-only file")
        self.stale()
        path = self.repo / PLAN_PATH
        path.chmod(0o444)
        code, out, err = self.run_main("--write")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"{PLAN_PATH}: cannot be written", err)
        self.assertEqual(self.plan_bytes(), PLAN.encode())

    def test_a_missing_plan_exits_one(self):
        (self.repo / PLAN_PATH).unlink()
        self.assertEqual(self.run_main("--write")[0], 1)

    def test_any_other_argument_prints_the_usage_and_exits_two(self):
        self.stale()
        before = self.plan_bytes()
        for args in (("--wrte",), ("write",), ("--write", "--write"), ("-h",)):
            with self.subTest(args=args):
                code, out, err = self.run_main(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage", err.lower())
                self.assertEqual(self.plan_bytes(), before)

    def test_no_argument_checks_as_before(self):
        self.stale()
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("[plan-files]", err)
        self.assertIn("make plan-progress", err)


if __name__ == "__main__":
    unittest.main()
