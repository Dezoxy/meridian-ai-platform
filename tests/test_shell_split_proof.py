"""Tests for scripts/shell_split_proof.py: a proof that a shell file was split by
moving lines only.

Each test makes a temporary git repository with a toy shell file (two "checks",
each with a header paragraph, a constant, a global and a function), writes a
split of it on disk with a manifest of ranges, and runs the script as the
session does: from the repository's root, with `--ref`, `--manifest` and
`--scope`. Every name and line below is invented.

Run: python3 -m unittest discover -s tests
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "shell_split_proof.py"

# 23 lines. Lines 6, 9, 12, 16 and 20 are blank.
TOY = """\
#!/usr/bin/env bash
# Toy: two checks.
# check one: reads the thing.
# check two: writes the thing.
set -euo pipefail

readonly ONE_URL='http://one'
readonly TWO_URL='http://two'

one_global=
two_global=

check_one() {
  echo one
}

check_two() {
  echo two
}

check_one
check_two
echo done
"""
TOY_LINES = TOY.count("\n")

HONEST = """\
== toy.d/one.sh
+ # shellcheck shell=bash
toy.sh 3-3
toy.sh 6-7
toy.sh 9-10
toy.sh 13-16
== toy.d/two.sh
+ # shellcheck shell=bash
toy.sh 4-4
toy.sh 8-8
toy.sh 11-12
toy.sh 17-20
== toy.sh
toy.sh 1-2
toy.sh 5-5
+ # shellcheck source=toy.d/one.sh
+ . "${KIND_DIR}/toy.d/one.sh"
+ # shellcheck source=toy.d/two.sh
+ . "${KIND_DIR}/toy.d/two.sh"
toy.sh 21-23
"""


class Repo:
    """A git repository in a temporary directory, with the toy file committed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q")
        self.write("toy.sh", TOY)
        self.commit("toy")

    def git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=a@example.org", "-c", "user.name=a", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def write(self, name: str, text: str | bytes) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def prove(
        self,
        manifest: str,
        scopes: tuple[str, ...] = ("toy.sh", "toy.d"),
        ref: str = "HEAD",
    ) -> subprocess.CompletedProcess[str]:
        (self.root / "manifest.txt").write_text(manifest, encoding="utf-8")
        arguments = ["--ref", ref, "--manifest", "manifest.txt"]
        for scope in scopes:
            arguments += ["--scope", scope]
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )


def toy_lines() -> list[str]:
    return TOY.splitlines()


def lines_of(numbers: list[tuple[int, int]]) -> str:
    """The text of the given 1-based inclusive ranges of the toy file."""
    toy = toy_lines()
    out = [line for a, b in numbers for line in toy[a - 1 : b]]
    return "\n".join(out) + "\n"


def write_honest(repo: Repo) -> None:
    """The honest split, built from the toy file by the manifest's own ranges."""
    one = [(3, 3), (6, 7), (9, 10), (13, 16)]
    two = [(4, 4), (8, 8), (11, 12), (17, 20)]
    header = "# shellcheck shell=bash\n"
    repo.write("toy.d/one.sh", header + lines_of(one))
    repo.write("toy.d/two.sh", header + lines_of(two))
    entry = lines_of([(1, 2), (5, 5)])
    entry += "# shellcheck source=toy.d/one.sh\n"
    entry += '. "${KIND_DIR}/toy.d/one.sh"\n'
    entry += "# shellcheck source=toy.d/two.sh\n"
    entry += '. "${KIND_DIR}/toy.d/two.sh"\n'
    entry += lines_of([(21, 23)])
    repo.write("toy.sh", entry)


class ShellSplitProofTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repo(Path(directory.name))

    def test_the_toy_has_the_lines_the_manifest_counts(self) -> None:
        # Guards the tests themselves: the ranges below are of 23 lines.
        self.assertEqual(TOY_LINES, 23)

    def test_an_honest_split_passes_and_lists_every_added_line(self) -> None:
        write_honest(self.repo)
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        out = run.stdout
        self.assertIn("toy.d/one.sh: 4 ranges, 9 lines moved, 1 added", out)
        self.assertIn("toy.d/two.sh: 4 ranges, 8 lines moved, 1 added", out)
        self.assertIn("toy.sh: 3 ranges, 6 lines moved, 4 added", out)
        self.assertIn("old toy.sh: 23 lines, each used exactly once", out)
        self.assertIn("added lines (6):", out)
        self.assertIn("  toy.d/one.sh:1: # shellcheck shell=bash", out)
        self.assertIn("  toy.d/two.sh:1: # shellcheck shell=bash", out)
        self.assertIn("  toy.sh:4: # shellcheck source=toy.d/one.sh", out)
        self.assertIn('  toy.sh:5: . "${KIND_DIR}/toy.d/one.sh"', out)
        self.assertIn("  toy.sh:6: # shellcheck source=toy.d/two.sh", out)
        self.assertIn('  toy.sh:7: . "${KIND_DIR}/toy.d/two.sh"', out)
        self.assertIn("notes (0):", out)
        self.assertTrue(out.rstrip().endswith("PROOF HOLDS"), out)

    def test_one_byte_changed_in_a_moved_line_fails(self) -> None:
        write_honest(self.repo)
        part = self.repo.root / "toy.d" / "one.sh"
        part.write_text(
            part.read_text().replace("http://one", "http://onf"), encoding="utf-8"
        )
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL toy.d/one.sh: line 4 differs", run.stdout)
        self.assertIn("http://onf", run.stdout)
        self.assertIn("http://one", run.stdout)

    def test_a_line_dropped_fails_and_names_it(self) -> None:
        write_honest(self.repo)
        # Line 20 (a blank) is left out of the manifest and of the file.
        two = self.repo.root / "toy.d" / "two.sh"
        two.write_text(two.read_text()[:-1], encoding="utf-8")
        manifest = HONEST.replace("toy.sh 17-20", "toy.sh 17-19")
        run = self.repo.prove(manifest)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL toy.sh: lines 20 are in no file", run.stdout)

    def test_a_line_used_twice_fails_and_names_the_ranges(self) -> None:
        write_honest(self.repo)
        two = self.repo.root / "toy.d" / "two.sh"
        two.write_text(two.read_text() + toy_lines()[6] + "\n", encoding="utf-8")
        manifest = HONEST.replace("toy.sh 17-20", "toy.sh 17-20\ntoy.sh 7-7")
        run = self.repo.prove(manifest)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL toy.sh: lines 7 are used twice", run.stdout)
        self.assertIn("toy.d/one.sh 6-7", run.stdout)
        self.assertIn("toy.d/two.sh 7-7", run.stdout)

    def test_an_added_line_not_declared_fails(self) -> None:
        write_honest(self.repo)
        part = self.repo.root / "toy.d" / "one.sh"
        part.write_text(part.read_text() + "echo smuggled\n", encoding="utf-8")
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL toy.d/one.sh: line 11 differs", run.stdout)
        self.assertIn("echo smuggled", run.stdout)
        self.assertIn("<end of file>", run.stdout)

    def test_a_new_file_in_scope_not_in_the_manifest_fails(self) -> None:
        write_honest(self.repo)
        self.repo.write("toy.d/extra.sh", "echo extra\n")
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn(
            "FAIL toy.d/extra.sh: new in scope and not in the manifest", run.stdout
        )

    def test_a_file_in_scope_changed_and_not_in_the_manifest_fails(self) -> None:
        self.repo.write("toy.d/other.sh", "# shellcheck shell=bash\nother() { :; }\n")
        self.repo.commit("another part")
        write_honest(self.repo)
        self.repo.write("toy.d/other.sh", "# shellcheck shell=bash\nother() { :x; }\n")
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn(
            "FAIL toy.d/other.sh: changed and not in the manifest", run.stdout
        )

    def test_a_file_in_scope_unchanged_and_not_named_passes(self) -> None:
        self.repo.write("toy.d/other.sh", "# shellcheck shell=bash\nother() { :; }\n")
        self.repo.commit("another part")
        write_honest(self.repo)
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_a_file_removed_and_not_named_fails(self) -> None:
        self.repo.write("toy.d/other.sh", "# shellcheck shell=bash\nother() { :; }\n")
        self.repo.commit("another part")
        write_honest(self.repo)
        (self.repo.root / "toy.d" / "other.sh").unlink()
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn(
            "FAIL toy.d/other.sh: removed and not named by the manifest", run.stdout
        )

    def test_a_falling_order_is_a_note_and_still_passes(self) -> None:
        # Part one takes the function (13-16) before its paragraph (3-3).
        write_honest(self.repo)
        one = [(13, 16), (3, 3), (6, 7), (9, 10)]
        self.repo.write("toy.d/one.sh", "# shellcheck shell=bash\n" + lines_of(one))
        manifest = HONEST.replace(
            "toy.sh 3-3\ntoy.sh 6-7\ntoy.sh 9-10\ntoy.sh 13-16\n",
            "toy.sh 13-16\ntoy.sh 3-3\ntoy.sh 6-7\ntoy.sh 9-10\n",
        )
        run = self.repo.prove(manifest)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("notes (1):", run.stdout)
        self.assertIn("NOTE toy.d/one.sh: toy.sh 3-3", run.stdout)
        self.assertIn("does not rise after 13-16", run.stdout)

    def test_a_second_cut_moves_lines_out_of_a_part_the_first_cut_wrote(self) -> None:
        # The first cut is committed; its REF holds the entry and two parts. The
        # second cut moves `echo done` out of the entry into a NEW part and
        # leaves the first two parts alone and unnamed.
        write_honest(self.repo)
        self.repo.commit("first cut")
        entry = (self.repo.root / "toy.sh").read_text().splitlines()
        self.assertEqual(len(entry), 10)
        self.repo.write("toy.d/three.sh", "# shellcheck shell=bash\n" + entry[9] + "\n")
        new_entry = entry[:7] + [
            "# shellcheck source=toy.d/three.sh",
            '. "${KIND_DIR}/toy.d/three.sh"',
            *entry[7:9],
        ]
        self.repo.write("toy.sh", "\n".join(new_entry) + "\n")
        manifest = """\
== toy.d/three.sh
+ # shellcheck shell=bash
toy.sh 10-10
== toy.sh
toy.sh 1-7
+ # shellcheck source=toy.d/three.sh
+ . "${KIND_DIR}/toy.d/three.sh"
toy.sh 8-9
"""
        run = self.repo.prove(manifest)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("old toy.sh: 10 lines, each used exactly once", run.stdout)
        self.assertNotIn("one.sh", run.stdout)

    def test_a_part_moved_whole_is_gone_or_opened_never_left_beside_it(self) -> None:
        # The first cut is committed. A second cut moves all of part one into
        # a new file. Part one gone from disk passes; part one left on disk is
        # a copy, and fails.
        write_honest(self.repo)
        self.repo.commit("first cut")
        one = self.repo.root / "toy.d" / "one.sh"
        text = one.read_text()
        self.repo.write("toy.d/new.sh", "# shellcheck shell=bash\n" + text)
        manifest = "== toy.d/new.sh\n+ # shellcheck shell=bash\ntoy.d/one.sh 1-10\n"
        copy = self.repo.prove(manifest)
        self.assertEqual(copy.returncode, 1, copy.stdout)
        self.assertIn(
            "FAIL toy.d/one.sh: named as an old file, still on disk", copy.stdout
        )
        one.unlink()
        moved = self.repo.prove(manifest)
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)

    def test_an_old_file_not_at_the_ref_ends_two(self) -> None:
        write_honest(self.repo)
        run = self.repo.prove(HONEST + "== toy.d/last.sh\ntoy.d/one.sh 1-1\n")
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("manifest line 22: toy.d/one.sh is not in HEAD", run.stderr)

    def test_a_bad_manifest_line_ends_two_and_names_the_line(self) -> None:
        write_honest(self.repo)
        run = self.repo.prove("== toy.sh\ntoy.sh 1-2\nwhat is this\n")
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertIn("manifest line 3: cannot read 'what is this'", run.stderr)

    def test_a_line_before_any_target_ends_two(self) -> None:
        run = self.repo.prove("toy.sh 1-2\n== toy.sh\n")
        self.assertEqual(run.returncode, 2)
        self.assertIn("manifest line 1: a line before any ==", run.stderr)

    def test_a_reversed_range_and_a_range_past_the_end_end_two(self) -> None:
        reversed_run = self.repo.prove("== toy.sh\ntoy.sh 5-2\n")
        self.assertEqual(reversed_run.returncode, 2)
        self.assertIn("manifest line 2: bad range", reversed_run.stderr)
        past = self.repo.prove("== toy.sh\ntoy.sh 1-24\n")
        self.assertEqual(past.returncode, 2)
        self.assertIn("has 23 lines at HEAD, not 24", past.stderr)

    def test_blank_lines_and_comments_in_a_manifest_are_ignored(self) -> None:
        write_honest(self.repo)
        manifest = "# a cut\n\n" + HONEST.replace(
            "== toy.sh", "\n# the entry\n== toy.sh"
        )
        run = self.repo.prove(manifest)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_an_added_empty_line_is_a_plus_alone(self) -> None:
        self.repo.write("toy.sh", "a\nb\n")
        self.repo.commit("two lines")
        self.repo.write("toy.sh", "a\n\nb\n")
        run = self.repo.prove("== toy.sh\ntoy.sh 1-1\n+\ntoy.sh 2-2\n", ("toy.sh",))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("  toy.sh:2: ", run.stdout)

    def test_a_final_newline_missing_in_the_old_file_is_kept_at_the_end(self) -> None:
        self.repo.write("toy.sh", "a\nb")
        self.repo.commit("no final newline")
        # `b` ends the new file as it ended the old one: no newline, a pass.
        self.repo.write("toy.sh", "x\nb")
        self.repo.write("other.sh", "a\n")
        manifest = "== toy.sh\n+ x\ntoy.sh 2-2\n== other.sh\ntoy.sh 1-1\n"
        scopes = ("toy.sh", "other.sh")
        ok = self.repo.prove(manifest, scopes)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        # A newline added to it is a change.
        self.repo.write("toy.sh", "x\nb\n")
        bad = self.repo.prove(manifest, scopes)
        self.assertEqual(bad.returncode, 1, bad.stdout)
        self.assertIn("ends with a newline", bad.stdout)

    def test_a_missing_final_newline_in_a_new_file_fails(self) -> None:
        write_honest(self.repo)
        part = self.repo.root / "toy.d" / "two.sh"
        part.write_text(part.read_text()[:-1], encoding="utf-8")
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("toy.d/two.sh", run.stdout)

    def test_a_target_that_is_not_on_disk_fails(self) -> None:
        run = self.repo.prove("== toy.d/nothing.sh\n+ x\n", ("toy.sh",))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL toy.d/nothing.sh: cannot be read on disk", run.stdout)

    def test_a_target_outside_every_scope_fails(self) -> None:
        # Lines 6-7 of the old file move to a file that no --scope covers: the
        # lines are accounted for and unread by anything that guards the scopes.
        write_honest(self.repo)
        self.repo.write("toy.d/one.sh", "# shellcheck shell=bash\n" + lines_of(
            [(3, 3), (9, 10), (13, 16)]
        ))
        self.repo.write("outside.sh", lines_of([(6, 7)]))
        manifest = HONEST.replace("toy.sh 6-7\n", "", 1).replace(
            "== toy.d/two.sh", "== outside.sh\ntoy.sh 6-7\n== toy.d/two.sh"
        )
        run = self.repo.prove(manifest)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL outside.sh: a target outside every --scope", run.stdout)
        self.assertNotIn("PROOF HOLDS", run.stdout)

    def test_an_old_file_outside_every_scope_fails(self) -> None:
        # Lines are copied out of a file that stays as it is and that no --scope
        # covers: they would be in two places, and nothing would say so.
        self.repo.write("other.sh", "copied\n")
        self.repo.commit("a file outside the scopes")
        write_honest(self.repo)
        part = self.repo.root / "toy.d" / "one.sh"
        part.write_text(part.read_text() + "copied\n", encoding="utf-8")
        manifest = HONEST.replace("toy.sh 13-16\n", "toy.sh 13-16\nother.sh 1-1\n", 1)
        run = self.repo.prove(manifest)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL other.sh: an old file outside every --scope", run.stdout)
        self.assertNotIn("PROOF HOLDS", run.stdout)

    def test_an_old_file_and_a_target_inside_a_directory_scope_are_in_scope(
        self,
    ) -> None:
        # The honest split names toy.sh (a file scope) and toy.d/ (a directory):
        # a path under a directory scope is in it, and a name that only starts
        # with the scope's text is not.
        write_honest(self.repo)
        self.assertEqual(self.repo.prove(HONEST).returncode, 0)
        self.repo.write("toy.dx/three.sh", "echo x\n")
        run = self.repo.prove(HONEST + "== toy.dx/three.sh\n+ echo x\n")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL toy.dx/three.sh: a target outside every --scope", run.stdout)

    def test_a_manifest_that_opens_no_target_fails(self) -> None:
        run = self.repo.prove("# nothing here\n\n", ("toy.sh",))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAIL the manifest opens no target", run.stdout)
        self.assertNotIn("PROOF HOLDS", run.stdout)

    def test_the_account_says_it_read_the_disk_when_scope_files_differ_from_head(
        self,
    ) -> None:
        write_honest(self.repo)  # toy.sh changed, two parts new: three files
        run = self.repo.prove(HONEST)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        first = run.stdout.splitlines()[0]
        self.assertIn("NOTE", first)
        self.assertIn("working tree", first)
        self.assertIn("3 files", first)

    def test_the_account_has_no_note_when_scope_files_equal_head(self) -> None:
        write_honest(self.repo)
        self.repo.commit("the split")
        run = self.repo.prove(HONEST, ref="HEAD~1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("NOTE", run.stdout.splitlines()[0])
        self.assertNotIn("working tree", run.stdout.splitlines()[0])

    def test_a_file_outside_the_scopes_that_differs_is_not_counted(self) -> None:
        write_honest(self.repo)
        self.repo.commit("the split")
        self.repo.write("notes.txt", "outside\n")
        run = self.repo.prove(HONEST, ref="HEAD~1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("working tree", run.stdout.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
