"""The decision of CI's required check ``python`` (S074).

``scripts/ci_python_verdict.py`` is what the final job runs. The check must
never report success when tests that should have run did not, so the truth
table below is closed: exactly two combinations succeed, and every other cell
of the product of the jobs' results fails. The test walks the whole product, not
a sample, so a cell somebody adds to the script by accident cannot pass.

Run: python3 -m unittest discover -s tests
"""

import itertools
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_python_verdict.py"
sys.path.insert(0, str(ROOT / "scripts"))

import ci_python_verdict as verdict  # noqa: E402

# What GitHub reports for a job a later job needs, and what a missing value is.
RESULTS = ("success", "failure", "cancelled", "skipped", "", "SUCCESS", "unknown")
DOCS_ONLY = ("true", "false", "", "True", "1", "yes")

FULL_RUN = {
    "classify": "success",
    "docs_only": "false",
    "static": "success",
    "tests": "success",
    "docs_tests": "skipped",
    "evaluation": "success",
}
DOCUMENTS_RUN = {
    "classify": "success",
    "docs_only": "true",
    "static": "success",
    "tests": "skipped",
    "docs_tests": "success",
    "evaluation": "skipped",
}


def judge(**given: str) -> verdict.Verdict:
    return verdict.judge(**{**FULL_RUN, **given})


class TruthTable(unittest.TestCase):
    def test_a_full_run_where_everything_passed_succeeds_and_ran_the_shards(
        self,
    ) -> None:
        got = verdict.judge(**FULL_RUN)

        self.assertTrue(got.ok, got.reasons)
        self.assertTrue(got.shards_ran)

    def test_a_documents_run_where_its_job_passed_succeeds_without_shards(
        self,
    ) -> None:
        got = verdict.judge(**DOCUMENTS_RUN)

        self.assertTrue(got.ok, got.reasons)
        self.assertFalse(got.shards_ran)

    def test_exactly_two_of_the_whole_product_succeed(self) -> None:
        winners = []
        for cells in itertools.product(
            RESULTS, DOCS_ONLY, RESULTS, RESULTS, RESULTS, RESULTS
        ):
            given = dict(
                zip(
                    ("classify", "docs_only", "static", "tests", "docs_tests"),
                    cells[:5],
                    strict=True,
                )
            )
            given["evaluation"] = cells[5]
            if verdict.judge(**given).ok:
                winners.append(given)

        self.assertCountEqual(winners, [FULL_RUN, DOCUMENTS_RUN])

    def test_every_failure_says_why(self) -> None:
        for given in (
            {"tests": "failure"},
            {"tests": "skipped"},
            {"classify": "failure"},
            {"docs_only": ""},
        ):
            with self.subTest(given=given):
                got = judge(**given)
                self.assertFalse(got.ok)
                self.assertTrue(got.reasons)

    def test_the_failures_that_matter_most(self) -> None:
        # The ones the design exists for: the shards skipped, cancelled or
        # absent while the pull request was not documents-only.
        cases = [
            (FULL_RUN, {"tests": "skipped"}),
            (FULL_RUN, {"tests": "cancelled"}),
            (FULL_RUN, {"tests": ""}),
            (FULL_RUN, {"evaluation": "skipped"}),
            (FULL_RUN, {"static": "skipped"}),
            (FULL_RUN, {"classify": "skipped"}),
            (FULL_RUN, {"docs_tests": "success"}),
            (FULL_RUN, {"docs_only": ""}),
            (DOCUMENTS_RUN, {"docs_tests": "skipped"}),
            (DOCUMENTS_RUN, {"docs_tests": "failure"}),
            (DOCUMENTS_RUN, {"tests": "success"}),
            (DOCUMENTS_RUN, {"evaluation": "success"}),
            (DOCUMENTS_RUN, {"classify": "cancelled"}),
        ]
        for base, change in cases:
            with self.subTest(change=change, base=base["docs_only"]):
                self.assertFalse(verdict.judge(**{**base, **change}).ok)

    def test_a_skipped_job_that_the_fast_path_does_not_explain_fails(self) -> None:
        # docs_only is false, so the documents job and only it may be skipped.
        for name in ("classify", "static", "tests", "evaluation"):
            with self.subTest(job=name):
                self.assertFalse(judge(**{name: "skipped"}).ok)


class CoverageFiles(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def make(self, *numbers: int, text: str = "data") -> None:
        for number in numbers:
            (self.folder / f"shard-{number}.coverage").write_text(text, "utf-8")

    def test_one_file_per_shard_is_complete(self) -> None:
        self.make(1, 2, 3, 4)

        self.assertEqual(verdict.coverage_problems(self.folder, 4), [])

    def test_a_missing_shard_is_a_problem(self) -> None:
        self.make(1, 2, 4)

        problems = verdict.coverage_problems(self.folder, 4)

        self.assertEqual(len(problems), 1)
        self.assertIn("shard-3.coverage", problems[0])

    def test_no_files_is_a_problem_for_every_shard(self) -> None:
        self.assertEqual(len(verdict.coverage_problems(self.folder, 4)), 4)

    def test_a_folder_that_does_not_exist_is_a_problem(self) -> None:
        self.assertTrue(verdict.coverage_problems(self.folder / "absent", 4))

    def test_an_empty_file_is_a_problem(self) -> None:
        self.make(1, 2, 3)
        self.make(4, text="")

        problems = verdict.coverage_problems(self.folder, 4)

        self.assertEqual(len(problems), 1)
        self.assertIn("empty", problems[0])

    def test_a_file_of_another_shard_number_is_a_problem(self) -> None:
        self.make(1, 2, 3, 4, 5)

        problems = verdict.coverage_problems(self.folder, 4)

        self.assertEqual(len(problems), 1)
        self.assertIn("shard-5.coverage", problems[0])

    def test_a_stray_file_is_a_problem(self) -> None:
        self.make(1, 2, 3, 4)
        (self.folder / "other.coverage").write_text("x", "utf-8")

        self.assertTrue(verdict.coverage_problems(self.folder, 4))

    def test_the_count_is_the_one_it_is_given(self) -> None:
        self.make(1, 2, 3, 4)

        self.assertEqual(verdict.coverage_problems(self.folder, 4), [])
        self.assertTrue(verdict.coverage_problems(self.folder, 5))
        self.assertTrue(verdict.coverage_problems(self.folder, 3))

    def test_a_count_of_zero_is_a_problem_not_a_pass(self) -> None:
        self.assertTrue(verdict.coverage_problems(self.folder, 0))


class Command(unittest.TestCase):
    """The script as the job runs it."""

    def run_script(self, *args: str, output: Path | None = None):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        if output is not None:
            env["GITHUB_OUTPUT"] = str(output)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def arguments(self, cells: dict[str, str]) -> list[str]:
        return [
            "jobs",
            "--classify",
            cells["classify"],
            "--docs-only",
            cells["docs_only"],
            "--static",
            cells["static"],
            "--tests",
            cells["tests"],
            "--docs-tests",
            cells["docs_tests"],
            "--evaluation",
            cells["evaluation"],
        ]

    def test_a_full_run_exits_zero_and_says_the_shards_ran(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            done = self.run_script(*self.arguments(FULL_RUN), output=output)

            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(output.read_text("utf-8"), "shards_ran=true\n")

    def test_a_documents_run_exits_zero_and_says_the_shards_did_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            done = self.run_script(*self.arguments(DOCUMENTS_RUN), output=output)

            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(output.read_text("utf-8"), "shards_ran=false\n")

    def test_a_skipped_shard_job_exits_one_and_writes_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            done = self.run_script(
                *self.arguments({**FULL_RUN, "tests": "skipped"}), output=output
            )

            self.assertEqual(done.returncode, 1)
            self.assertIn("tests", done.stdout + done.stderr)
            self.assertFalse(output.exists() and output.read_text("utf-8"))

    def test_a_missing_argument_is_not_a_success(self) -> None:
        done = self.run_script("jobs", "--classify", "success")

        self.assertNotEqual(done.returncode, 0)

    def test_the_coverage_command_exits_one_for_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "shard-1.coverage").write_text("x", "utf-8")

            done = self.run_script("coverage-files", folder, "--shards", "4")

            self.assertEqual(done.returncode, 1)
            self.assertIn("shard-2.coverage", done.stdout + done.stderr)

    def test_the_coverage_command_exits_zero_for_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for number in (1, 2):
                (Path(folder) / f"shard-{number}.coverage").write_text("x", "utf-8")

            done = self.run_script("coverage-files", folder, "--shards", "2")

            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_the_coverage_command_refuses_a_shard_count_that_is_no_number(self) -> None:
        done = self.run_script("coverage-files", ".", "--shards", "four")

        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
