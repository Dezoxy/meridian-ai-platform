"""Tests for scripts/pdf_tables.py: how a Markdown table is printed in the PDF.

LaTeX cannot break a table row across pages. A row taller than a page runs
past the bottom margin and off the sheet, and the PDF loses that text without
a word. So a table with a cell too long for that prints as records, one block
of paragraphs per row, and paragraphs break. Every other table that Pandoc
would wrap gets column widths from its text, where equal widths gave a
two-letter ID as much room as a paragraph.

Pure functions; nothing here starts Docker or Pandoc.

Run: python3 -m unittest discover -s tests
"""

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pdf_tables.py"


def load():
    spec = importlib.util.spec_from_file_location("pdf_tables", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tables = load()

LONG = ("word " * 80).strip()  # 399 characters: more than a record's limit
WIDE = ("a sentence that is long enough to make Pandoc wrap the table " * 2).strip()


def printed(text):
    return "\n".join(tables.print_tables(text.splitlines()))


class Records(unittest.TestCase):
    def test_a_table_with_a_long_cell_prints_as_one_block_per_row(self):
        text = (
            "| ID | Threat | Status |\n"
            "|---|---|---|\n"
            f"| T-01 | {LONG} | Designed |\n"
            "| T-02 | short | Built |\n"
        )

        out = printed(text)

        self.assertNotIn("|---", out)
        self.assertIn("**T-01** · Status: Designed", out)
        self.assertIn(f"*Threat.* {LONG}", out)
        self.assertIn("**T-02** · Status: Built", out)

    def test_a_column_prints_the_same_way_in_every_block(self):
        # "short" is a short cell of a long column: it gets its paragraph too,
        # or one threat would sit beside its ID and the next under it.
        text = (
            "| ID | Threat | Status |\n"
            "|---|---|---|\n"
            f"| T-01 | {LONG} | Designed |\n"
            "| T-02 | short | Built |\n"
        )

        lines = tables.print_tables(text.splitlines())

        self.assertIn("*Threat.* short", lines)
        self.assertNotIn("**T-02** · Threat: short · Status: Built", lines)

    def test_every_block_is_a_paragraph_of_its_own(self):
        text = f"| ID | Threat |\n|---|---|\n| T-01 | {LONG} |\n| T-02 | x |\n"

        lines = tables.print_tables(text.splitlines())

        lead = lines.index("**T-01**")
        self.assertEqual(lines[lead + 1], "")
        self.assertTrue(lines[lead + 2].startswith("*Threat.* "))
        self.assertEqual(lines[lead + 3], "")

    def test_an_escaped_pipe_is_text_in_a_record(self):
        text = f"| ID | Note |\n|---|---|\n| A | a \\| b {LONG} |\n"

        self.assertIn("*Note.* a | b word", printed(text))

    def test_an_empty_cell_is_left_out(self):
        text = f"| ID | Note | Owner |\n|---|---|---|\n| A | {LONG} |  |\n"

        out = printed(text)

        self.assertIn("**A**", out)
        self.assertNotIn("Owner", out)

    def test_a_first_cell_already_in_bold_is_not_wrapped_again(self):
        text = f"| ID | Note |\n|---|---|\n| **A** | {LONG} |\n"

        out = printed(text)

        self.assertIn("**A**", out)
        self.assertNotIn("****", out)

    def test_a_long_first_cell_is_a_labelled_paragraph(self):
        text = f"| Finding | Owner |\n|---|---|\n| {LONG} | me |\n"

        out = printed(text)

        self.assertIn(f"*Finding.* {LONG}", out)
        self.assertIn("Owner: me", out)

    def test_a_table_inside_a_code_fence_is_left_alone(self):
        text = f"```markdown\n| ID | Threat |\n|---|---|\n| T-01 | {LONG} |\n```\n"

        self.assertEqual(printed(text), text.rstrip("\n"))

    def test_a_fence_closes_only_on_its_own_kind_and_length(self):
        # Four backticks hold a three-backtick line and a table: all example.
        example = (
            f"````markdown\n```\n| ID | Threat |\n|---|---|\n| A | {LONG} |\n````\n"
        )

        self.assertEqual(printed(example), example.rstrip("\n"))

    def test_a_table_after_a_closed_fence_is_printed(self):
        # A ``` line inside a ~~~ fence closes nothing, so the fence ends at ~~~.
        text = f"~~~\n```\n~~~\n\n| ID | Threat |\n|---|---|\n| A | {LONG} |\n"

        self.assertIn("**A**", printed(text))

    def test_a_pipe_inside_code_does_not_end_a_cell(self):
        text = f"| ID | Command | Note |\n|---|---|---|\n| A | `a|b` | {LONG} |\n"

        out = printed(text)

        self.assertIn("**A** · Command: `a|b`", out)
        self.assertIn(f"*Note.* {LONG}", out)

    def test_a_column_name_in_bold_gives_a_clean_label(self):
        text = f"| ID | **Note** |\n|---|---|\n| A | {LONG} |\n"

        out = printed(text)

        self.assertIn(f"*Note.* {LONG}", out)
        self.assertNotIn("***", out)

    def test_a_cell_without_a_column_name_cannot_start_a_heading_or_a_list(self):
        text = f"| ID | |\n|---|---|\n| A | # {LONG} |\n| B | - {LONG} |\n"

        lines = tables.print_tables(text.splitlines())

        self.assertIn(f"\\# {LONG}", lines)
        self.assertIn(f"\\- {LONG}", lines)

    def test_records_start_a_paragraph_of_their_own(self):
        text = f"Before.\n| ID | Threat |\n|---|---|\n| A | {LONG} |\n"

        lines = tables.print_tables(text.splitlines())

        self.assertEqual(lines[:3], ["Before.", "", "**A**"])

    def test_the_text_around_a_table_is_kept(self):
        text = f"Before.\n\n| ID | Threat |\n|---|---|\n| A | {LONG} |\n\nAfter.\n"

        lines = tables.print_tables(text.splitlines())

        self.assertEqual(lines[0], "Before.")
        self.assertEqual(lines[-1], "After.")
        self.assertEqual(lines[-2], "")


class Widths(unittest.TestCase):
    def separator(self, text):
        return tables.print_tables(text.splitlines())[1]

    def dashes(self, text):
        cells = self.separator(text).strip("|").split("|")
        return [len(cell.strip(": ")) for cell in cells]

    def test_a_table_of_short_cells_stays_as_it_is(self):
        text = "| ID | Name |\n|---|---|\n| A | one |\n| B | two |\n"

        self.assertEqual(printed(text), text.rstrip("\n"))

    def test_columns_get_width_by_their_text(self):
        text = f"| ID | Threat |\n|---|---|\n| T-01 | {WIDE} |\n"

        short, long = self.dashes(text)

        self.assertGreater(long, 3 * short)

    def test_a_column_is_never_narrower_than_its_longest_word(self):
        # "Boundary" is eight letters in a column whose cells hold four: with
        # widths from the cells alone the header ran into its neighbour.
        text = f"| ID | Boundary | Threat |\n|---|---|---|\n| T-01 | TB-1 | {WIDE} |\n"

        ident, boundary, _threat = self.dashes(text)

        share = boundary / sum(self.dashes(text))
        self.assertGreaterEqual(share * tables.LINE_CHARS, len("Boundary"))
        self.assertGreaterEqual(boundary, ident)

    def test_a_link_counts_by_its_text_not_its_address(self):
        link = "[x](https://example.org/a/very/long/address/that/is/not/printed)"
        text = f"| ID | Source | Threat |\n|---|---|---|\n| T-01 | {link} | {WIDE} |\n"

        ident, source, _threat = self.dashes(text)

        self.assertLessEqual(source, 2 * ident)

    def test_alignment_marks_survive(self):
        text = f"| ID | Cost | Threat |\n|:--|--:|:-:|\n| T-01 | 1 | {WIDE} |\n"

        cells = self.separator(text).strip("|").split("|")

        self.assertTrue(cells[0].startswith(":") and not cells[0].endswith(":"))
        self.assertTrue(cells[1].endswith(":") and not cells[1].startswith(":"))
        self.assertTrue(cells[2].startswith(":") and cells[2].endswith(":"))

    def test_the_rows_are_not_changed(self):
        text = f"| ID | Threat |\n|---|---|\n| T-01 | {WIDE} |\n"

        lines = tables.print_tables(text.splitlines())

        self.assertEqual(lines[0], "| ID | Threat |")
        self.assertEqual(lines[2], f"| T-01 | {WIDE} |")

    def test_printing_twice_changes_nothing_more(self):
        text = f"| ID | Threat |\n|---|---|\n| T-01 | {WIDE} |\n"

        once = tables.print_tables(text.splitlines())

        self.assertEqual(tables.print_tables(once), once)


if __name__ == "__main__":
    unittest.main()
