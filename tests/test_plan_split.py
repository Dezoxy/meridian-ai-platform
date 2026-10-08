"""Tests for scripts/plan_split.py: the move of the plan into files (S097).

The script splits an old plan into one file for each Part C section and each
Part E entry and proves that the files hold the old text byte for byte. These
tests run it on a small plan that has the shapes the real one has: sections out
of number order, a section heading inside a fence, blank lines between
sections, entries with continuation lines, and relative links.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plan_split.py"


def load():
    spec = importlib.util.spec_from_file_location("plan_split", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plan_split"] = module  # a dataclass looks its module up here
    spec.loader.exec_module(module)
    return module


FENCE = "`" * 3

OLD_PLAN = f"""# Plan

## Part A

A link [the architecture](architecture/README.md) in the top.

## Part B

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S001 | One | x | done | — |
| S002 | Two | x | done | — |
| S095 | Last | x | todo | — |

## Part C — Step details

Template:

{FENCE}text
### S0xx — <title>
{FENCE}

### S002 — Two | with a pipe

Body of two, see [ADR 1](architecture/decisions/0001-x.md) and
[the web](https://example.com/x) and [up](#s001--one) and
[a section](#s001--one-more).

{FENCE}text
### S999 — a heading inside a fence
[not a link to fix](architecture/y.md)
{FENCE}

### S001 — One

Body of one, [dev](development-environment.md#x).


## Part D — Open questions

| # | Question |
|---|---|
| 1 | A question |

## Part E — Changelog

- **v0.1, 2026-09-29:** first entry
  with a continuation line.
- **v0.10, 2026-10-01:** second entry.
"""


class SplitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mod = load()
        self.old = self.root / "old.md"
        self.old.write_bytes(OLD_PLAN.encode())
        self.tree = self.root / "tree"
        # The files the plan's relative links name, so that they resolve.
        for name in (
            "architecture/README.md",
            "architecture/decisions/0001-x.md",
            "architecture/y.md",
            "development-environment.md",
        ):
            path = self.tree / "docs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.mod.main([*args, "--root", str(self.tree)])
        return status, out.getvalue(), err.getvalue()

    def split(self, overwrite=False):
        args = [str(self.old), *(["--overwrite"] if overwrite else [])]
        status, out, err = self.run_cli(*args)
        self.assertEqual(status, 0, err)
        return out

    def step(self, name):
        return (self.tree / "docs/plan/steps" / name).read_bytes().decode()

    def plan(self):
        return (self.tree / "docs/meridian-plan.md").read_text()


class Split(SplitCase):
    def test_each_section_is_a_file_of_its_own_text(self):
        self.split()
        self.assertTrue(self.step("S002.md").startswith("### S002 — Two | with"))
        self.assertTrue(self.step("S001.md").startswith("### S001 — One\n"))
        self.assertNotIn("### S001", self.step("S002.md"))

    def test_the_blank_lines_after_a_section_end_its_own_file(self):
        self.split()
        # S002 is followed by one blank line, S001 (the last section) by two
        # and then Part D's heading.
        self.assertTrue(self.step("S002.md").endswith(f"{FENCE}\n\n"))
        self.assertTrue(self.step("S001.md").endswith("#x).\n\n\n"))

    def test_a_heading_inside_a_fence_is_not_a_section(self):
        self.split()
        self.assertFalse((self.tree / "docs/plan/steps/S999.md").exists())
        self.assertFalse((self.tree / "docs/plan/steps/S0xx.md").exists())
        self.assertIn("### S999 — a heading inside a fence", self.step("S002.md"))

    def test_the_index_keeps_the_old_order_not_the_number_order(self):
        self.split()
        rows = [x for x in self.plan().split("\n") if x.startswith("| S00")]
        self.assertEqual([r.split(" | ")[0] for r in rows[-2:]], ["| S002", "| S001"])

    def test_a_pipe_in_a_title_is_escaped_in_the_index(self):
        self.split()
        self.assertIn("| S002 | Two \\| with a pipe | [S002.md]", self.plan())

    def test_entries_are_named_for_their_version_with_the_minor_padded(self):
        self.split()
        names = sorted(p.name for p in (self.tree / "docs/plan/changelog").iterdir())
        self.assertEqual(names, ["v0.01.md", "v0.10.md"])
        first = (self.tree / "docs/plan/changelog/v0.01.md").read_text()
        self.assertEqual(
            first,
            "- **v0.1, 2026-09-29:** first entry\n  with a continuation line.\n",
        )
        self.assertEqual(self.mod.entry_file(97), "v0.97.md")
        self.assertEqual(self.mod.entry_file(100), "v0.100.md")

    def test_the_plan_keeps_its_top_part_b_and_part_d(self):
        self.split()
        plan = self.plan()
        self.assertTrue(plan.startswith(OLD_PLAN[: OLD_PLAN.index("## Part C")]))
        self.assertIn("## Part D — Open questions\n\n| # | Question |", plan)
        self.assertNotIn("Body of two", plan)
        self.assertNotIn("first entry", plan)

    def test_the_template_stays_in_part_c(self):
        self.split()
        self.assertIn(f"{FENCE}text\n### S0xx — <title>\n{FENCE}\n", self.plan())

    def test_part_c_and_part_e_say_where_things_are(self):
        self.split()
        plan = self.plan()
        self.assertIn("`docs/plan/steps/S0NN.md`", plan)
        self.assertIn("`docs/plan/changelog/`", plan)
        self.assertIn("(`v0.01.md` to `v0.10.md`", plan)

    def test_nothing_is_deleted(self):
        keep = self.tree / "docs/plan/steps/notes.txt"
        keep.parent.mkdir(parents=True)
        keep.write_text("mine")
        self.split(overwrite=True)
        self.assertEqual(keep.read_text(), "mine")

    def test_a_step_file_already_there_joins_the_index_after_the_old_ones(self):
        extra = self.tree / "docs/plan/steps/S097.md"
        extra.parent.mkdir(parents=True)
        extra.write_text("### S097 — The move itself\n\nText.\n")
        self.split(overwrite=True)
        row = "| S097 | The move itself | [S097.md](plan/steps/S097.md) |"
        self.assertIn(row, self.plan())
        self.assertEqual(extra.read_text(), "### S097 — The move itself\n\nText.\n")

    def test_a_second_run_gives_the_same_tree(self):
        self.split()
        first = self.plan()
        self.split(overwrite=True)
        self.assertEqual(self.plan(), first)

    def test_a_tree_that_has_step_files_is_refused_without_overwrite(self):
        self.split()
        path = self.tree / "docs/plan/steps/S001.md"
        path.write_text("### S001 — One\n\nEdited by hand.\n")
        status, _, err = self.run_cli(str(self.old))
        self.assertEqual(status, 2)
        self.assertIn("--overwrite", err)
        self.assertEqual(path.read_text(), "### S001 — One\n\nEdited by hand.\n")

    def test_overwrite_writes_over_the_edited_step_files(self):
        self.split()
        path = self.tree / "docs/plan/steps/S001.md"
        path.write_text("### S001 — One\n\nEdited by hand.\n")
        self.split(overwrite=True)
        self.assertNotIn("Edited by hand", path.read_text())

    def test_the_padded_minor_is_in_part_e_sentence(self):
        self.old.write_bytes(OLD_PLAN.replace("v0.10,", "v0.3,").encode())
        self.split()
        self.assertIn("`v0.01.md` to `v0.03.md`", self.plan())

    def test_two_sections_of_one_step_are_refused(self):
        bad = OLD_PLAN.replace("### S001 — One", "### S002 — Two again")
        self.old.write_bytes(bad.encode())
        status, _, err = self.run_cli(str(self.old))
        self.assertEqual(status, 2)
        self.assertIn("one step", err)

    def test_a_plan_without_entries_is_refused(self):
        self.old.write_bytes(OLD_PLAN[: OLD_PLAN.index("- **v0.1")].encode())
        status, _, err = self.run_cli(str(self.old))
        self.assertEqual(status, 2)
        self.assertIn("no entry", err)


class Proof(SplitCase):
    def check(self, *more):
        return self.run_cli("--check", str(self.old), *more)

    def test_the_proof_passes_and_prints_counts_and_digests(self):
        self.split()
        status, out, _ = self.check()
        self.assertEqual(status, 0, out)
        self.assertIn("Part C, step files: old", out)
        self.assertIn("Part E, entry files: new", out)
        self.assertEqual(out.count("sha256 "), 6)
        self.assertIn("2 step files, 2 entry files: same", out)

    def test_one_changed_byte_in_a_step_file_fails_the_proof(self):
        self.split()
        path = self.tree / "docs/plan/steps/S001.md"
        path.write_text(path.read_text().replace("Body of one", "Body of 0ne"))
        status, out, _ = self.check()
        self.assertEqual(status, 1)
        self.assertIn("Part C, step files: DIFFERENT, first difference at byte", out)

    def test_one_changed_byte_in_an_entry_fails_the_proof(self):
        self.split()
        path = self.tree / "docs/plan/changelog/v0.10.md"
        path.write_text(path.read_text().replace("second", "sec0nd"))
        status, out, _ = self.check()
        self.assertEqual(status, 1)
        self.assertIn("Part E, entry files: DIFFERENT", out)

    def test_a_missing_blank_line_fails_the_proof(self):
        self.split()
        path = self.tree / "docs/plan/steps/S002.md"
        path.write_text(path.read_text().rstrip("\n") + "\n")
        status, out, _ = self.check()
        self.assertEqual(status, 1)
        self.assertIn("DIFFERENT", out)

    def test_a_step_missing_from_the_index_fails_the_proof(self):
        self.split()
        plan = self.tree / "docs/meridian-plan.md"
        plan.write_text(
            "".join(x for x in plan.read_text().splitlines(True) if "| S001 |" not in x)
        )
        status, out, _ = self.check()
        self.assertEqual(status, 1)
        self.assertIn("not in the index: S001", out)

    def test_a_change_to_part_b_fails_the_proof(self):
        self.split()
        plan = self.tree / "docs/meridian-plan.md"
        plan.write_text(plan.read_text().replace("| S095 | Last |", "| S095 | Lost |"))
        status, out, _ = self.check()
        self.assertEqual(status, 1)
        self.assertIn("rest of the plan: DIFFERENT", out)


class Links(SplitCase):
    def test_relative_links_in_a_step_file_get_two_levels_up(self):
        self.split()
        status, out, _ = self.run_cli("--fix-links")
        self.assertEqual(status, 0)
        self.assertIn(
            "[ADR 1](../../architecture/decisions/0001-x.md)", self.step("S002.md")
        )
        self.assertIn("[dev](../../development-environment.md#x)", self.step("S001.md"))
        self.assertIn("links fixed: 3", out)  # two paths and one anchor

    def test_an_external_link_an_anchor_and_a_fenced_link_stay(self):
        self.split()
        self.run_cli("--fix-links")
        text = self.step("S002.md")
        self.assertIn("[the web](https://example.com/x)", text)
        self.assertIn("[not a link to fix](architecture/y.md)", text)
        self.assertIn("[a section](#s001--one-more)", text)

    def test_an_anchor_of_another_steps_heading_points_at_its_file(self):
        self.split()
        self.run_cli("--fix-links")
        self.assertIn("[up](S001.md#s001--one)", self.step("S002.md"))

    def test_an_anchor_into_the_plan_points_at_the_step_file(self):
        self.split()
        plan = self.tree / "docs/meridian-plan.md"
        link = "[s](#s002--two--with-a-pipe)"
        text = plan.read_text().replace("## Part A\n", f"## Part A\n\n{link}\n")
        plan.write_text(text)
        self.run_cli("--fix-links")
        self.assertIn("[s](plan/steps/S002.md#s002--two--with-a-pipe)", self.plan())

    def test_the_proof_after_the_links_needs_the_fixed_flag(self):
        self.split()
        self.run_cli("--fix-links")
        status, out, _ = self.run_cli("--check", str(self.old))
        self.assertEqual(status, 1)
        self.assertIn("Part C, step files: DIFFERENT", out)
        status, out, _ = self.run_cli("--check", str(self.old), "--fixed")
        self.assertEqual(status, 0, out)

    def test_fixing_the_links_twice_changes_nothing_the_second_time(self):
        self.split()
        self.run_cli("--fix-links")
        once = self.step("S002.md")
        _, out, _ = self.run_cli("--fix-links")
        self.assertIn("links fixed: 0", out)
        self.assertEqual(self.step("S002.md"), once)

    def test_a_link_that_resolved_nowhere_before_the_move_is_left_alone(self):
        (self.tree / "docs/development-environment.md").unlink()
        self.split()
        self.run_cli("--fix-links")
        self.assertIn("[dev](development-environment.md#x)", self.step("S001.md"))


class Words(SplitCase):
    def head(self):
        olds = "".join(old for old, _ in self.mod.WORDS)
        return f"# Plan\n\n{olds}\n| S095 | Last | x | todo | — |\n\n"

    def text_with_words(self):
        return self.head() + OLD_PLAN[OLD_PLAN.index("## Part C") :]

    def test_each_wording_is_replaced_once_and_the_row_is_added(self):
        self.old.write_bytes(self.text_with_words().encode())
        self.split()
        status, _, err = self.run_cli("--words")
        self.assertEqual(status, 0, err)
        plan = self.plan()
        for old, new in self.mod.WORDS:
            self.assertNotIn(old, plan)
            self.assertIn(new, plan)
        rows = [x for x in plan.split("\n") if x.startswith("| S09")]
        self.assertEqual([r.split(" | ")[0] for r in rows], ["| S095", "| S097"])

    def test_a_second_run_changes_nothing(self):
        self.old.write_bytes(self.text_with_words().encode())
        self.split()
        self.run_cli("--words")
        once = self.plan()
        status, _, _ = self.run_cli("--words")
        self.assertEqual(status, 0)
        self.assertEqual(self.plan(), once)

    def test_a_wording_that_is_gone_stops_the_run(self):
        self.split()
        status, _, err = self.run_cli("--words")
        self.assertEqual(status, 2)
        self.assertIn("not there once", err)

    def test_the_words_are_only_looked_for_before_part_c(self):
        text = self.text_with_words() + "\n" + self.mod.WORDS[0][0]
        self.old.write_bytes(text.encode())
        self.split()
        status, _, _ = self.run_cli("--words")
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
