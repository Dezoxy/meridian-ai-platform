"""The file size check: a source file over 800 lines needs a listed exception (S074).

`scripts/check_file_sizes.py` reads the files git tracks, so each test builds a
small repository in a temporary directory (`git init`, `git add`, no commit)
and runs the script on it as `make lint` does, reading its exit status and its
standard error. The real repository is checked last.

Run: python3 -m unittest discover -s tests
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_file_sizes.py"
EXCEPTIONS = "scripts/file-size-exceptions.txt"
REASON = "split along its sections once a row of the plan homes it"


def lines(count: int) -> str:
    return "x = 1\n" * count


class Run:
    def __init__(self, done: subprocess.CompletedProcess[str]) -> None:
        self.returncode = done.returncode
        self.stdout = done.stdout
        self.stderr = done.stderr


class Sizes(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.git("init", "--quiet")

    def git(self, *args: str) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", *args], cwd=self.root, check=True, env=env)

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_check(self, exceptions: str | None = None) -> Run:
        if exceptions is not None:
            self.write(EXCEPTIONS, exceptions)
        self.git("add", "-A")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return Run(
            subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(self.root)],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        )

    def test_a_file_at_the_ceiling_and_a_file_under_it_pass(self) -> None:
        self.write("src/at.py", lines(800))
        self.write("src/under.py", lines(10))

        run = self.run_check()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("2 source files", run.stdout)

    def test_a_file_one_line_over_the_ceiling_fails_and_the_line_says_what_to_do(
        self,
    ) -> None:
        self.write("src/over.py", lines(801))

        run = self.run_check()

        self.assertEqual(run.returncode, 1)
        (first, *_) = run.stderr.splitlines()
        self.assertIn("src/over.py: 801 lines, over the ceiling of 800", first)
        self.assertIn("split it", first)
        self.assertIn(EXCEPTIONS, first)

    def test_a_last_line_with_no_newline_counts_as_a_line(self) -> None:
        self.write("src/bare.py", lines(800) + "y = 2")

        run = self.run_check()

        self.assertEqual(run.returncode, 1)
        self.assertIn("src/bare.py: 801 lines", run.stderr)

    def test_a_shell_file_over_the_ceiling_fails_under_infra_and_scripts(self) -> None:
        self.write("infra/kind/long.sh", lines(801))
        self.write("scripts/long.sh", lines(801))

        run = self.run_check()

        self.assertEqual(run.returncode, 1)
        self.assertIn("infra/kind/long.sh: 801 lines", run.stderr)
        self.assertIn("scripts/long.sh: 801 lines", run.stderr)

    def test_vendored_generated_and_data_paths_are_not_read(self) -> None:
        for path in (
            ".claude/hooks/big.py",
            ".agents/skills/big.py",
            ".codex/big.py",
            "data/synthetic/policies/big.py",
            "data/evaluation/big.json",
            "docs/architecture/generated/big.py",
            "src/big.json",
            "tests/big.sh",
        ):
            self.write(path, lines(2000))
        self.write("data/synthetic/generator/small.py", lines(5))

        run = self.run_check()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("1 source files", run.stdout)

    def test_the_generator_and_the_spikes_are_read(self) -> None:
        self.write("data/synthetic/generator/big.py", lines(801))
        self.write("spikes/s0/big.py", lines(801))

        run = self.run_check()

        self.assertEqual(run.returncode, 1)
        self.assertIn("data/synthetic/generator/big.py: 801 lines", run.stderr)
        self.assertIn("spikes/s0/big.py: 801 lines", run.stderr)

    def test_a_file_that_git_does_not_track_is_not_read(self) -> None:
        self.write("src/tracked.py", lines(5))
        self.write(".gitignore", "src/ignored.py\n")
        self.write("src/ignored.py", lines(2000))

        run = self.run_check()

        self.assertEqual(run.returncode, 0, run.stderr)

    def test_an_excepted_file_at_its_recorded_count_passes(self) -> None:
        self.write("src/big.py", lines(900))

        run = self.run_check(f"src/big.py 900 {REASON}\n")

        self.assertEqual(run.returncode, 0, run.stderr)

    def test_an_excepted_file_that_grew_by_one_line_fails_and_says_not_to_raise_it(
        self,
    ) -> None:
        self.write("src/big.py", lines(901))

        run = self.run_check(f"src/big.py 900 {REASON}\n")

        self.assertEqual(run.returncode, 1)
        (first, *_) = run.stderr.splitlines()
        self.assertIn("src/big.py: 901 lines, over the 900 recorded", first)
        self.assertIn(f"{EXCEPTIONS}:1", first)
        self.assertIn("do not raise the count", first)

    def test_an_excepted_file_that_shrank_but_is_still_over_passes(self) -> None:
        self.write("src/big.py", lines(850))

        run = self.run_check(f"src/big.py 900 {REASON}\n")

        self.assertEqual(run.returncode, 0, run.stderr)

    def test_a_stale_exception_for_a_file_now_at_the_ceiling_names_the_line_to_remove(
        self,
    ) -> None:
        self.write("src/big.py", lines(800))

        run = self.run_check(f"# a comment\n\nsrc/big.py 900 {REASON}\n")

        self.assertEqual(run.returncode, 1)
        (first, *_) = run.stderr.splitlines()
        self.assertIn(f"{EXCEPTIONS}:3", first)
        self.assertIn("src/big.py is now 800 lines", first)
        self.assertIn("remove this line", first)

    def test_a_stale_exception_for_a_file_that_is_gone_fails(self) -> None:
        self.write("src/other.py", lines(5))

        run = self.run_check(f"src/gone.py 900 {REASON}\n")

        self.assertEqual(run.returncode, 1)
        self.assertIn(f"{EXCEPTIONS}:1", run.stderr)
        self.assertIn("src/gone.py is not a tracked source file", run.stderr)

    def test_an_exception_for_a_vendored_file_is_stale_too(self) -> None:
        self.write(".claude/hooks/big.py", lines(900))

        run = self.run_check(f".claude/hooks/big.py 900 {REASON}\n")

        self.assertEqual(run.returncode, 1)
        self.assertIn("is not a tracked source file", run.stderr)

    def test_a_reason_of_four_words_fails_and_five_words_pass(self) -> None:
        self.write("src/big.py", lines(900))

        four = self.run_check("src/big.py 900 one two three four\n")
        five = self.run_check("src/big.py 900 one two three four five\n")

        self.assertEqual(four.returncode, 1)
        self.assertIn("a reason of 4 words; write at least 5", four.stderr)
        self.assertEqual(five.returncode, 0, five.stderr)

    def test_a_line_without_a_whole_number_for_the_count_fails(self) -> None:
        self.write("src/big.py", lines(900))

        run = self.run_check(f"src/big.py many {REASON}\n")

        self.assertEqual(run.returncode, 1)
        self.assertIn(f"{EXCEPTIONS}:1: a line is `path count reason`", run.stderr)

    def test_a_path_listed_twice_fails(self) -> None:
        self.write("src/big.py", lines(900))

        run = self.run_check(f"src/big.py 900 {REASON}\nsrc/big.py 900 {REASON}\n")

        self.assertEqual(run.returncode, 1)
        self.assertIn("src/big.py is listed twice (first at line 1)", run.stderr)

    def test_a_missing_exceptions_file_is_an_empty_list(self) -> None:
        self.write("src/big.py", lines(801))

        run = self.run_check()

        self.assertEqual(run.returncode, 1)
        self.assertIn("src/big.py: 801 lines", run.stderr)

    def test_every_failure_is_one_line_and_the_total_closes_the_output(self) -> None:
        self.write("src/a.py", lines(801))
        self.write("src/b.py", lines(802))

        run = self.run_check()

        output = run.stderr.splitlines()
        self.assertEqual(len(output), 3)
        self.assertTrue(output[-1].endswith("2 problem(s)"))

    def test_the_repository_passes_with_its_own_exceptions_file(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        done = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
