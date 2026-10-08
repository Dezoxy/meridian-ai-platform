"""Tests for scripts/plan_gate.py: what keeps the plan's history out of it (S102).

The plan holds what is to be built and nothing that ages: a step row is one plan
sentence, no status is typed by hand, Part D holds open questions only, and the
text outside the rows stays under a ceiling. Each test starts from the right
tree of test_check_plan_files.py, plants one fault and expects one finding; the
passes that must stay passes are tested beside the refusals, and the real
repository must pass.

Run: python3 -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_check_plan_files import (  # noqa: E402
    FENCE,
    FIGURE,
    PLAN,
    QUESTION_HEADER,
    STEP_HEADER,
    PlanCase,
    load,
    plan_gate,
)

PLAN_PATH = "docs/meridian-plan.md"
CLOSED_PATH = "docs/plan/questions-closed.md"
GOOD = "| S002 | Two | x | — |\n"
D_HEAD = "## Part D — Open questions\n\n"
REMEDY = "a row is one plan sentence: put the rest into the step's file"


def row_line(cell="x", step="S002"):
    return f"| {step} | Two | {cell} | — |"


def question(number, text="A question?", needed="S002", default="none"):
    return f"| {number} | {text} | {needed} | {default} |\n"


def sized(make, length):
    """The line ``make(filler)`` builds, with filler so that it is ``length`` long."""
    room = length - len(make(""))
    if room < 0:
        raise ValueError(f"{length} is shorter than the line without filler")
    return make("x" * room)


class Rows(PlanCase):
    def plant(self, line, plan=PLAN):
        self.write(PLAN_PATH, plan.replace(GOOD, line + "\n"))

    def test_a_row_of_four_cells_without_a_fault_passes(self):
        self.plant(row_line("a plain sentence, designed and simulated, merged"))
        self.assertEqual(self.found(), [])

    def test_a_row_of_three_or_five_cells_is_one_finding(self):
        for line in ("| S002 | Two | x |", "| S002 | Two | x | — | — |"):
            with self.subTest(line=line):
                self.plant(line)
                found = self.one()
                self.assertIn("S002", found)
                self.assertIn("cells, not four", found)
                self.assertIn(REMEDY, found)

    def test_the_row_limit_is_exact_and_the_count_is_said(self):
        self.assertEqual(plan_gate.ROW_MAX, 500)
        self.plant(sized(row_line, 500))
        self.assertEqual(self.found(), [])
        self.plant(sized(row_line, 501))
        found = self.one()
        self.assertIn("501 characters", found)
        self.assertIn("ROW_MAX", found)
        self.assertIn(REMEDY, found)

    def test_a_date_is_one_finding(self):
        self.plant(row_line("cut on 2026-10-07"))
        found = self.one()
        self.assertIn("S002", found)
        self.assertIn("2026-10-07", found)
        self.assertIn(REMEDY, found)

    def test_a_struck_through_text_is_one_finding(self):
        self.plant(row_line("~~old~~ new"))
        found = self.one()
        self.assertIn("struck-through", found)
        self.assertIn(REMEDY, found)

    def test_a_quote_of_the_owner_needs_both_the_word_and_a_quote(self):
        self.plant(row_line('the owner said "do it"'))
        self.assertIn("owner", self.one())
        self.plant(row_line('The Owner said "do it"'))
        self.assertIn("owner", self.one())
        self.plant(row_line("the owner decides which perils are automatic"))
        self.assertEqual(self.found(), [])
        self.plant(row_line('a "quoted" word'))
        self.assertEqual(self.found(), [])

    def test_a_build_label_is_one_finding_and_is_named(self):
        labels = (
            ("Built as the gate", "Built as"),
            ("not built", "not built"),
            ("Not built", "Not built"),
            ("not met", "not met"),
            ("Not met", "Not met"),
            ("not done", "not done"),
            ("Not done", "Not done"),
            ("Designed in S010", "Designed"),
            ("Implemented", "Implemented"),
            ("implemented and tested", "implemented and tested"),
            ("`todo`", "`todo`"),
            ("`doing`", "`doing`"),
            ("`done`", "`done`"),
            ("`blocked`", "`blocked`"),
            ("`dropped`", "`dropped`"),
        )
        for text, matched in labels:
            with self.subTest(text=text):
                self.plant(row_line(f"x {text} x"))
                found = self.one()
                self.assertIn("S002", found)
                self.assertIn(f"'{matched}'", found)
                self.assertIn(REMEDY, found)

    def test_plan_words_are_not_build_labels(self):
        words = (
            "designed",
            "simulated",
            "implemented",
            "merged",
            "built",
            "a `make docs` run",
            "`finished`",
            "undone",
            "Redesigned",
        )
        for word in words:
            with self.subTest(word=word):
                self.plant(row_line(f"x {word} x"))
                self.assertEqual(self.found(), [])

    def test_each_fault_of_a_row_is_its_own_finding(self):
        self.plant(row_line("2026-10-07 ~~x~~ Built as y"))
        found = self.found()
        self.assertEqual(len(found), 3, found)
        self.assertTrue(all("S002" in x for x in found), found)

    def test_a_table_with_other_cells_is_not_a_step_table(self):
        demo = "| After | What can be shown |\n|---|---|\n"
        demo += "| S041 | Shown on 2026-10-08, ~~old~~, Built as x |\n"
        self.write(PLAN_PATH, PLAN.replace("## Part C", demo + "\n## Part C"))
        self.assertEqual(self.found(), [])

    def test_a_step_table_with_another_header_is_one_finding(self):
        # A fifth column, or a renamed one, would take the table out of the gate.
        for header in (
            "| ID | Step | Done when | Depends | Notes |",
            "| ID | Step | Done | Depends |",
        ):
            with self.subTest(header=header):
                self.write(PLAN_PATH, PLAN.replace(STEP_HEADER, header))
                found = self.one()
                self.assertIn("ID | Step | Done when | Depends", found)

    def test_a_row_after_the_table_ended_is_not_a_row(self):
        late = "\n| S003 | Three | on 2026-10-08 | — |\n"
        self.write(PLAN_PATH, PLAN.replace("## Part C", late + "\n## Part C"))
        self.assertEqual(self.found(), [])

    def test_a_row_in_a_fence_or_in_another_part_is_not_gated(self):
        faulty = row_line("on 2026-10-08")
        self.plant(f"{FENCE}text\n{faulty}\n{FENCE}")
        self.assertEqual(self.found(), [])
        table = f"{STEP_HEADER}\n|---|---|---|---|\n{faulty}\n\n"
        self.write(PLAN_PATH, PLAN.replace("Template:", table + "Template:"))
        self.assertEqual(self.found(), [])

    def test_a_cell_that_is_not_a_step_number_is_not_a_step_row(self):
        for first in ("S02", "S0021", "Step", "—"):
            with self.subTest(first=first):
                self.plant(f"| {first} | Two | on 2026-10-08 | — |")
                self.assertEqual(self.found(), [])


class RowExceptions(PlanCase):
    FAULTY = "| S021 | Identity | cut on 2026-10-07, ~~old~~ | S020 |\n"
    CLEAN = "| S021 | Identity | Sign-in works | S020 |\n"

    def plant(self, row):
        self.write(PLAN_PATH, PLAN.replace(GOOD, GOOD + row))

    def test_the_list_holds_only_s021_and_can_only_shrink(self):
        self.assertLessEqual(set(plan_gate.ROW_EXCEPTIONS), {"S021"})
        for step, why in plan_gate.ROW_EXCEPTIONS.items():
            self.assertRegex(step, r"^S\d{3}$")
            self.assertTrue(why.strip(), step)

    def test_a_faulty_row_of_a_step_on_the_list_passes(self):
        self.plant(self.FAULTY)
        self.assertEqual(self.found(), [])

    def test_a_faulty_row_of_a_step_off_the_list_is_a_finding(self):
        self.plant(self.FAULTY.replace("S021", "S022"))
        self.assertEqual(len(self.found()), 2)

    def test_a_clean_row_of_a_step_on_the_list_is_a_finding(self):
        self.plant(self.CLEAN)
        found = self.one()
        self.assertIn("remove S021 from ROW_EXCEPTIONS", found)

    def test_a_step_on_the_list_with_no_row_is_no_finding(self):
        self.assertNotIn("S021", PLAN)
        self.assertEqual(self.found(), [])


class NoStatusByHand(PlanCase):
    def add(self, text):
        self.write(PLAN_PATH, PLAN.replace("## Part C", text + "\n\n## Part C"))

    def test_a_status_line_is_one_finding(self):
        self.add("**Status:** doing · **Started:** 2026-10-08 · **Finished:** —")
        found = self.one()
        self.assertIn("**Status:**", found)
        self.assertIn("README", found)

    def test_a_status_line_in_a_fence_or_mid_line_is_text(self):
        self.add(f"{FENCE}text\n**Status:** doing\n{FENCE}")
        self.assertEqual(self.found(), [])
        self.add("A note on **Status:** in prose.")
        self.assertEqual(self.found(), [])

    def test_the_heading_where_the_project_stands_is_one_finding(self):
        for heading in (
            "## Where the project stands",
            "#### Where the project stands",
            "# where the project stands #",
        ):
            with self.subTest(heading=heading):
                self.add(heading)
                found = self.one()
                self.assertIn("Where the project stands", found)
                self.assertIn("README", found)

    def test_a_similar_heading_or_a_heading_in_a_fence_passes(self):
        self.add("## Where the project stands now\n\n## The project stands")
        self.assertEqual(self.found(), [])
        self.add(f"{FENCE}text\n## Where the project stands\n{FENCE}")
        self.assertEqual(self.found(), [])


class OpenQuestions(PlanCase):
    def put(self, *rows, plan=PLAN):
        table = QUESTION_HEADER + "".join(rows)
        self.write(PLAN_PATH, plan.replace(D_HEAD, D_HEAD + table + "\n"))

    def test_open_questions_pass(self):
        self.put(question(1), question(6))
        self.assertEqual(self.found(), [])

    def test_an_answered_question_is_one_finding(self):
        for word in ("**Answered 2026-09-30:**", "**Answered 2026-10-07 (late): x**"):
            with self.subTest(word=word):
                self.put(question(1), question(2, f"Q? {word} yes"))
                found = self.one()
                self.assertIn("question 2", found)
                self.assertIn("end of docs/plan/questions-closed.md", found)

    def test_a_struck_through_question_is_one_finding(self):
        self.put(question(2, "Q?", default="~~old~~ new"))
        found = self.one()
        self.assertIn("question 2", found)
        self.assertIn("questions-closed.md", found)

    def test_a_question_that_is_both_answered_and_struck_is_one_finding(self):
        self.put(question(2, "Q? **Answered 2026-01-02:** a", default="~~old~~"))
        self.one()

    def test_the_question_limit_is_exact_and_the_count_is_said(self):
        self.assertEqual(plan_gate.QUESTION_ROW_MAX, 900)

        def make(filler):
            return question(3, "Q?" + filler).rstrip("\n")

        self.put(sized(make, 900) + "\n")
        self.assertEqual(self.found(), [])
        self.put(sized(make, 901) + "\n")
        found = self.one()
        self.assertIn("question 3", found)
        self.assertIn("901 characters", found)
        self.assertIn("QUESTION_ROW_MAX", found)

    def test_a_row_in_a_fence_the_header_or_another_part_is_not_a_question(self):
        self.put(
            f"{FENCE}text\n" + question(4, "**Answered 2026-01-02:** a") + f"{FENCE}\n"
        )
        self.assertEqual(self.found(), [])
        plan = PLAN.replace(
            "Template:", question(5, "**Answered 2026-01-02:** a") + "\nTemplate:"
        )
        self.write(PLAN_PATH, plan)
        self.assertEqual(self.found(), [])


class AnsweredQuestions(PlanCase):
    HEAD = "# Answered questions\n\nProse.\n\n" + QUESTION_HEADER
    DONE = question(2, "Q? **Answered 2026-09-30:** yes")

    def closed(self, text):
        self.write(CLOSED_PATH, text)

    def test_a_missing_file_is_skipped_and_a_right_one_passes(self):
        self.assertEqual(self.found(), [])
        self.closed(
            self.HEAD + self.DONE + question(3, "Q? **Answered 2026-01-02:** a")
        )
        self.assertEqual(self.found(), [])

    def test_a_file_without_the_header_or_with_it_twice_is_one_finding(self):
        for text in ("# Answered\n\n" + self.DONE, self.HEAD + self.DONE + self.HEAD):
            with self.subTest(text=text[:20]):
                self.closed(text)
                found = self.one()
                self.assertIn(CLOSED_PATH, found)
                self.assertIn("exactly once", found)

    def test_a_row_that_is_not_answered_belongs_in_part_d(self):
        self.closed(self.HEAD + self.DONE + question(3, "Q? still open"))
        found = self.one()
        self.assertIn(CLOSED_PATH, found)
        self.assertIn("3", found)
        self.assertIn("Part D", found)

    def test_a_row_without_a_number_is_one_finding(self):
        self.closed(
            self.HEAD
            + self.DONE
            + "| x | Q? **Answered 2026-01-02:** a | S002 | none |\n"
        )
        found = self.one()
        self.assertIn(CLOSED_PATH, found)
        self.assertIn("number", found)

    def test_a_code_fence_never_closed_is_one_finding(self):
        self.closed(self.HEAD + self.DONE + FENCE + "\n" + question(3, "open"))
        found = self.one()
        self.assertIn("never closed", found)

    def test_a_row_in_a_closed_fence_is_not_read(self):
        fenced = f"{FENCE}text\n" + question(3, "open") + f"{FENCE}\n"
        self.closed(self.HEAD + self.DONE + fenced)
        self.assertEqual(self.found(), [])

    def test_a_number_in_one_table_twice_is_one_finding(self):
        self.closed(self.HEAD + self.DONE + self.DONE)
        found = self.one()
        self.assertIn("2", found)
        self.assertIn("exactly one", found)
        self.closed(self.HEAD + self.DONE)
        table = QUESTION_HEADER + question(1) + question(1)
        self.write(PLAN_PATH, PLAN.replace(D_HEAD, D_HEAD + table + "\n"))
        self.assertIn("exactly one", self.one())

    def test_a_number_in_both_tables_is_one_finding(self):
        self.closed(self.HEAD + self.DONE)
        table = QUESTION_HEADER + question(2)
        self.write(PLAN_PATH, PLAN.replace(D_HEAD, D_HEAD + table + "\n"))
        found = self.one()
        self.assertIn("2", found)
        self.assertIn("exactly one", found)


class FixedText(PlanCase):
    def test_the_ceiling_is_met_from_both_sides(self):
        figure = self.figure()
        self.assertEqual(plan_gate.FIXED_TEXT_SLACK, 2048)
        for limit in (figure, figure + plan_gate.FIXED_TEXT_SLACK):
            with self.subTest(limit=limit):
                plan_gate.FIXED_TEXT_MAX = limit
                self.assertEqual(self.found(), [])
        plan_gate.FIXED_TEXT_MAX = figure - 1
        found = self.one()
        self.assertIn(f"{figure:,} bytes", found)
        self.assertIn("over FIXED_TEXT_MAX", found)
        self.assertIn("owner's decision (Part A)", found)

    def test_a_ceiling_far_over_the_text_says_where_to_lower_it(self):
        figure = self.figure()
        plan_gate.FIXED_TEXT_MAX = figure + plan_gate.FIXED_TEXT_SLACK + 1
        found = self.one()
        self.assertIn(f"{figure:,} bytes", found)
        self.assertIn("lower FIXED_TEXT_MAX to", found)
        self.assertIn("owner's decision (Part A)", found)
        suggested = re.search(r"lower FIXED_TEXT_MAX to ([\d,]+)", found)[1]
        plan_gate.FIXED_TEXT_MAX = int(suggested.replace(",", ""))
        self.assertEqual(self.found(), [])

    def test_text_counts_in_bytes_and_each_line_with_its_newline(self):
        before = self.figure()
        self.write(PLAN_PATH, PLAN.replace(D_HEAD, D_HEAD + "ab\n"))
        self.assertEqual(self.figure(), before + 3)
        self.write(PLAN_PATH, PLAN.replace(D_HEAD, D_HEAD + "a—\n"))
        self.assertEqual(self.figure(), before + 5)

    def test_step_rows_question_rows_and_the_block_are_not_fixed_text(self):
        before = self.figure()
        rows = GOOD + row_line("a" * 300, "S003") + "\n"
        self.write(PLAN_PATH, PLAN.replace(GOOD, rows))
        self.assertEqual(self.figure(), before)
        # The table's header and separator lines and a blank line are fixed text;
        # the question row is not.
        table = QUESTION_HEADER + question(1, "q" * 200) + "\n"
        self.write(PLAN_PATH, PLAN.replace(D_HEAD, D_HEAD + table))
        self.assertEqual(self.figure(), before + len(QUESTION_HEADER.encode()) + 1)
        self.write(PLAN_PATH, PLAN.replace("### In flight", "### In flight\n\nnote"))
        self.assertEqual(self.figure(), before)

    def test_a_plan_without_its_markers_has_no_figure(self):
        self.write(PLAN_PATH, PLAN.replace("<!-- plan-progress: end -->\n", ""))
        plan_gate.FIXED_TEXT_MAX = 0
        found = self.found()
        self.assertEqual(len(found), 1, found)
        self.assertIsNone(FIGURE.search(found[0]))


class AfterTheReview(PlanCase):
    """What the Python review found (S102): each was an escape or a false alarm."""

    def questions(self, *rows):
        table = QUESTION_HEADER + "".join(rows)
        self.write(PLAN_PATH, PLAN.replace(D_HEAD, D_HEAD + table + "\n"))

    def closed(self, *rows):
        self.write(CLOSED_PATH, "# Answered\n\n" + QUESTION_HEADER + "".join(rows))

    def in_table(self, line):
        self.write(PLAN_PATH, PLAN.replace(GOOD, GOOD + line + "\n"))

    def test_the_word_answered_in_a_question_does_not_close_it(self):
        self.questions(
            question(40, "Is x needed?", default="Assume yes if not answered by then"),
            question(41, "Was the last one Answered?"),
        )
        self.assertEqual(self.found(), [])

    def test_a_closed_row_needs_the_marker_not_the_word(self):
        self.closed(question(2, "Q? It was answered somewhere"))
        self.assertIn("question 2 is not answered", self.one())

    def test_a_status_line_above_part_b_is_one_finding(self):
        self.write(PLAN_PATH, PLAN.replace("## Part B", "**Status:** x\n\n## Part B"))
        self.assertIn("**Status:**", self.one())

    def test_a_number_of_two_digits_twice_is_one_finding(self):
        self.questions(question(1), question(10))
        self.assertEqual(self.found(), [])
        self.questions(question(10), question(10))
        self.assertIn("question 10", self.one())

    def test_a_number_with_a_leading_zero_is_the_same_number(self):
        self.questions(question(7))
        self.closed(question("07", "Q? **Answered 2026-01-02:** a"))
        self.assertIn("question 7", self.one())

    def test_a_number_of_five_thousand_digits_is_no_traceback(self):
        self.questions(question("9" * 5000))
        self.assertTrue(all("QUESTION_ROW_MAX" in x for x in self.found()))

    def test_a_question_number_without_spaces_is_a_question(self):
        self.questions("|7| Q? **Answered 2026-01-02:** a | S002 | none |\n")
        self.assertIn("question 7", self.one())

    def test_a_question_number_dressed_up_is_one_finding(self):
        for cell in ("**7**", "`7`", "7."):
            with self.subTest(cell=cell):
                self.questions(f"| {cell} | Q? | S002 | none |\n")
                self.assertIn("bare number", self.one())

    def test_a_step_cell_dressed_up_is_one_finding(self):
        for cell in ("**S003**", "s003", "[S003](x.md)", "S003:"):
            with self.subTest(cell=cell):
                self.in_table(f"| {cell} | Three | on 2026-10-08 | — |")
                self.assertIn("bare step ID", self.one())

    def test_an_indented_table_line_is_one_finding(self):
        self.in_table("  | S003 | Three | on 2026-10-08 | — |")
        self.assertIn("indented", self.one())
        self.questions("  | 3 | Q? **Answered 2026-01-02:** a | S002 | none |\n")
        self.assertIn("indented", self.one())

    def test_struck_through_text_in_html_is_one_finding(self):
        for text in ("<del>old</del> new", "<s>old</s> new"):
            with self.subTest(text=text):
                self.write(PLAN_PATH, PLAN.replace(GOOD, row_line(text) + "\n"))
                self.assertIn("struck-through", self.one())

    def test_the_closed_files_header_is_read_by_all_four_cells(self):
        self.closed()
        self.assertEqual(self.found(), [])
        head = "| # | Query | Needed by | Default if unanswered |\n|---|---|---|---|\n"
        self.write(CLOSED_PATH, "# Answered\n\n" + head)
        self.assertIn("exactly once, not 0 times", self.one())


class TheRealPlan(unittest.TestCase):
    def test_the_real_plan_passes_the_gate(self):
        mod = load()
        self.assertEqual(mod.problems(mod.REPO), [])

    def test_the_gate_imports_nothing_outside_the_standard_library(self):
        text = (HERE.parent / "scripts" / "plan_gate.py").read_text()
        imports = [x for x in text.split("\n") if x.startswith(("import ", "from "))]
        self.assertTrue(
            all(x.split()[1] in {"__future__", "re", "math"} for x in imports), imports
        )


if __name__ == "__main__":
    unittest.main()
