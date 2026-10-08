"""The decision of CI's required check ``python`` (S074).

``scripts/ci_python_verdict.py`` is what the final job runs. The check must
never report success when tests that should have run did not, so the truth
table below is closed: exactly one combination succeeds, and every other cell
of the product of the jobs' results fails. The test walks the whole product, not
a sample, so a cell somebody adds to the script by accident cannot pass. The
second half is the files the shards leave: coverage data and a report each, and
the report is what proves the shards together are the whole suite.

Run: python3 -m unittest discover -s tests
"""

import itertools
import json
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

FULL_RUN = {"static": "success", "tests": "success", "evaluation": "success"}

DIGEST = "ab" * 32
OTHER_DIGEST = "cd" * 32


def judge(**given: str) -> verdict.Verdict:
    return verdict.judge(**{**FULL_RUN, **given})


class TruthTable(unittest.TestCase):
    def test_a_run_where_every_job_passed_succeeds(self) -> None:
        got = verdict.judge(**FULL_RUN)

        self.assertTrue(got.ok, got.reasons)
        self.assertEqual(got.reasons, ())

    def test_exactly_one_of_the_whole_product_succeeds(self) -> None:
        winners = []
        for cells in itertools.product(RESULTS, repeat=3):
            given = dict(zip(("static", "tests", "evaluation"), cells, strict=True))
            if verdict.judge(**given).ok:
                winners.append(given)

        self.assertEqual(winners, [FULL_RUN])

    def test_every_failure_says_why(self) -> None:
        for given in (
            {"tests": "failure"},
            {"tests": "skipped"},
            {"static": "cancelled"},
            {"evaluation": ""},
        ):
            with self.subTest(given=given):
                got = judge(**given)
                self.assertFalse(got.ok)
                self.assertTrue(got.reasons)

    def test_a_failure_names_the_job_and_what_it_was(self) -> None:
        got = judge(tests="skipped")

        self.assertEqual(len(got.reasons), 1)
        self.assertIn("tests", got.reasons[0])
        self.assertIn("skipped", got.reasons[0])

    def test_the_failures_that_matter_most(self) -> None:
        # The ones the design exists for: the shards skipped, cancelled or
        # absent, and a job that did not run at all.
        for change in (
            {"tests": "skipped"},
            {"tests": "cancelled"},
            {"tests": ""},
            {"evaluation": "skipped"},
            {"static": "skipped"},
        ):
            with self.subTest(change=change):
                self.assertFalse(judge(**change).ok)

    def test_no_job_may_be_skipped(self) -> None:
        for name in ("static", "tests", "evaluation"):
            with self.subTest(job=name):
                self.assertFalse(judge(**{name: "skipped"}).ok)


def report(number: int, **changes: object) -> dict[str, object]:
    return {
        "shard": number,
        "shards": 4,
        "collected": 100,
        "kept": 25,
        "digest": DIGEST,
        **changes,
    }


class Files(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def make(self, *numbers: int, text: str = "data") -> None:
        for number in numbers:
            (self.folder / f"shard-{number}.coverage").write_text(text, "utf-8")

    def reports(self, *numbers: int, **changes: object) -> None:
        for number in numbers:
            self.put(number, report(number, **changes))

    def put(self, number: int, content: object) -> None:
        (self.folder / f"shard-{number}.report.json").write_text(
            json.dumps(content), "utf-8"
        )


class CoverageFiles(Files):
    def test_one_file_per_shard_is_complete(self) -> None:
        self.make(1, 2, 3, 4)

        self.assertEqual(verdict.coverage_problems(self.folder, 4), [])

    def test_the_reports_beside_the_data_are_not_strays(self) -> None:
        self.make(1, 2, 3, 4)
        self.reports(1, 2, 3, 4)

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

    def test_a_report_of_another_shard_number_is_a_problem(self) -> None:
        self.make(1, 2, 3, 4)
        self.reports(5)

        problems = verdict.coverage_problems(self.folder, 4)

        self.assertEqual(len(problems), 1)
        self.assertIn("shard-5.report.json", problems[0])

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


class ShardReports(Files):
    def one_problem(self, *words: str) -> None:
        problems = verdict.report_problems(self.folder, 4)

        self.assertEqual(len(problems), 1, problems)
        for word in words:
            self.assertIn(word, problems[0])

    def test_four_reports_that_agree_and_add_up_are_complete(self) -> None:
        self.reports(1, 2, 3, 4)

        self.assertEqual(verdict.report_problems(self.folder, 4), [])

    def test_a_missing_report_is_a_problem(self) -> None:
        self.reports(1, 2, 4)

        self.one_problem("shard-3.report.json", "missing")

    def test_a_folder_that_does_not_exist_is_a_problem(self) -> None:
        self.assertTrue(verdict.report_problems(self.folder / "absent", 4))

    def test_a_differing_digest_is_a_problem(self) -> None:
        self.reports(1, 2, 3)
        self.put(4, report(4, digest=OTHER_DIGEST))

        self.one_problem("digest")

    def test_a_total_that_differs_is_a_problem(self) -> None:
        self.reports(1, 2, 3)
        self.put(4, report(4, collected=101))

        problems = verdict.report_problems(self.folder, 4)

        self.assertTrue(problems)
        self.assertTrue(any("collected" in problem for problem in problems))

    def test_kept_counts_that_are_short_of_the_total_are_a_problem(self) -> None:
        self.reports(1, 2, 3)
        self.put(4, report(4, kept=24))

        self.one_problem("kept", "99", "100")

    def test_kept_counts_that_are_over_the_total_are_a_problem(self) -> None:
        self.reports(1, 2, 3)
        self.put(4, report(4, kept=26))

        self.one_problem("kept", "101", "100")

    def test_a_duplicate_shard_number_is_a_problem(self) -> None:
        # The file for shard 4 says it is shard 3: two shards then claim the
        # same part, and nothing claims the fourth.
        self.reports(1, 2, 3)
        self.put(4, report(3))

        problems = verdict.report_problems(self.folder, 4)

        self.assertTrue(problems)
        self.assertTrue(any("shard-4.report.json" in p for p in problems))

    def test_a_shard_count_that_is_not_the_one_given_is_a_problem(self) -> None:
        self.reports(1, 2, 3)
        self.put(4, report(4, shards=3))

        self.one_problem("shard count is 3, not 4")

    def test_a_report_that_is_not_json_is_a_problem(self) -> None:
        self.reports(1, 2, 3)
        (self.folder / "shard-4.report.json").write_text("{not json", "utf-8")

        self.one_problem("shard-4.report.json")

    def test_a_report_that_is_not_an_object_is_a_problem(self) -> None:
        self.reports(1, 2, 3)
        self.put(4, [1, 2, 3])

        self.one_problem("shard-4.report.json")

    def test_a_report_without_a_field_is_a_problem(self) -> None:
        self.reports(1, 2, 3)
        content = report(4)
        del content["digest"]
        self.put(4, content)

        self.one_problem("shard-4.report.json", "digest")

    def test_a_field_of_the_wrong_kind_is_a_problem(self) -> None:
        # A boolean is a number to Python; a count that is True is no count.
        for field, value in (
            ("kept", True),
            ("collected", "100"),
            ("shard", 4.0),
            ("digest", 12),
            ("digest", "xyz"),
        ):
            with self.subTest(field=field, value=value):
                self.reports(1, 2, 3)
                self.put(4, report(4, **{field: value}))

                self.assertTrue(verdict.report_problems(self.folder, 4))

    def test_the_count_is_the_one_it_is_given(self) -> None:
        self.reports(1, 2, 3, 4)

        self.assertEqual(verdict.report_problems(self.folder, 4), [])
        self.assertTrue(verdict.report_problems(self.folder, 5))
        self.assertTrue(verdict.report_problems(self.folder, 3))

    def test_a_count_of_zero_is_a_problem_not_a_pass(self) -> None:
        self.assertTrue(verdict.report_problems(self.folder, 0))

    def test_the_summary_says_the_total_and_the_four_kept_counts(self) -> None:
        for number, kept in enumerate((26, 24, 25, 25), start=1):
            self.put(number, report(number, kept=kept))

        self.assertEqual(verdict.report_problems(self.folder, 4), [])
        summary = verdict.report_summary(self.folder, 4)

        self.assertIn("100", summary)
        self.assertIn("26 + 24 + 25 + 25", summary)


class Command(unittest.TestCase):
    """The script as the job runs it."""

    def run_script(self, *args: str):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def arguments(self, cells: dict[str, str]) -> list[str]:
        return [
            "jobs",
            "--static",
            cells["static"],
            "--tests",
            cells["tests"],
            "--evaluation",
            cells["evaluation"],
        ]

    def test_a_full_run_exits_zero(self) -> None:
        done = self.run_script(*self.arguments(FULL_RUN))

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("succeeded", done.stdout)

    def test_the_verdict_needs_no_output_file_to_succeed_or_to_fail(self) -> None:
        # No step of the job reads an output of another, so a missing
        # $GITHUB_OUTPUT changes nothing (the review's M1).
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        env["GITHUB_OUTPUT"] = ""
        done = subprocess.run(
            [sys.executable, str(SCRIPT), *self.arguments(FULL_RUN)],
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_skipped_shard_job_exits_one_and_names_it(self) -> None:
        done = self.run_script(*self.arguments({**FULL_RUN, "tests": "skipped"}))

        self.assertEqual(done.returncode, 1)
        self.assertIn("tests", done.stdout + done.stderr)

    def test_a_missing_argument_is_not_a_success(self) -> None:
        done = self.run_script("jobs", "--static", "success")

        self.assertNotEqual(done.returncode, 0)

    def test_the_old_arguments_are_not_taken(self) -> None:
        done = self.run_script(*self.arguments(FULL_RUN), "--docs-only", "true")

        self.assertNotEqual(done.returncode, 0)

    def test_the_files_command_exits_one_for_a_missing_coverage_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "shard-1.coverage").write_text("x", "utf-8")

            done = self.run_script("coverage-files", folder, "--shards", "4")

            self.assertEqual(done.returncode, 1)
            self.assertIn("shard-2.coverage", done.stdout + done.stderr)

    def complete(self, folder: str, shards: int, **changes: object) -> None:
        for number in range(1, shards + 1):
            (Path(folder) / f"shard-{number}.coverage").write_text("x", "utf-8")
            content = report(number, shards=shards, kept=50, collected=50 * shards)
            (Path(folder) / f"shard-{number}.report.json").write_text(
                json.dumps({**content, **changes}), "utf-8"
            )

    def test_the_files_command_exits_zero_and_prints_the_total_and_the_kept(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.complete(folder, 2)

            done = self.run_script("coverage-files", folder, "--shards", "2")

            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("100", done.stdout)
            self.assertIn("50 + 50", done.stdout)

    def test_the_files_command_exits_one_for_a_missing_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.complete(folder, 2)
            (Path(folder) / "shard-2.report.json").unlink()

            done = self.run_script("coverage-files", folder, "--shards", "2")

            self.assertEqual(done.returncode, 1)
            self.assertIn("shard-2.report.json", done.stdout + done.stderr)

    def test_the_files_command_exits_one_for_a_differing_digest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.complete(folder, 2)
            content = report(2, shards=2, kept=50, collected=100, digest=OTHER_DIGEST)
            (Path(folder) / "shard-2.report.json").write_text(
                json.dumps(content), "utf-8"
            )

            done = self.run_script("coverage-files", folder, "--shards", "2")

            self.assertEqual(done.returncode, 1)
            self.assertIn("digest", done.stdout + done.stderr)

    def test_the_files_command_exits_one_for_kept_counts_that_are_short(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.complete(folder, 2)
            content = report(2, shards=2, kept=49, collected=100)
            (Path(folder) / "shard-2.report.json").write_text(
                json.dumps(content), "utf-8"
            )

            done = self.run_script("coverage-files", folder, "--shards", "2")

            self.assertEqual(done.returncode, 1)
            self.assertIn("kept", done.stdout + done.stderr)

    def test_the_files_command_refuses_a_shard_count_that_is_no_number(self) -> None:
        done = self.run_script("coverage-files", ".", "--shards", "four")

        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
