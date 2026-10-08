"""The documents-only decision of CI's ``classify`` job (S074).

``scripts/ci_classify.py`` decides whether a pull request changes nothing but
documents, so that CI runs only the tests that read documents. The one failure
to design against is a "true" for a change that can break code, so the rule is
closed (an allowlist, never a denylist) and every doubt is "false". These tests
hold the rule, the git diff it reads (a rename out of ``src/`` into ``docs/`` is
a change to ``src/``) and the lines the job writes for the workflow.

Run: python3 -m unittest discover -s tests
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_classify.py"
sys.path.insert(0, str(ROOT / "scripts"))

import ci_classify  # noqa: E402


class Allowlist(unittest.TestCase):
    def test_the_allowlist_is_exactly_the_documents_folder_and_five_root_files(
        self,
    ) -> None:
        self.assertEqual(ci_classify.DOCUMENTS_FOLDER, "docs/")
        self.assertEqual(
            ci_classify.ROOT_DOCUMENTS,
            frozenset({"README.md", "CLAUDE.md", "AGENTS.md", "NOTICE", "LICENSE"}),
        )

    def test_a_pull_request_of_documents_only_is_documents_only(self) -> None:
        for changed in (
            ["docs/meridian-plan.md"],
            ["docs/architecture/model/workspace.dsl"],
            ["docs/a/b/c/d.md", "README.md"],
            ["README.md", "CLAUDE.md", "AGENTS.md", "NOTICE", "LICENSE"],
        ):
            with self.subTest(changed=changed):
                self.assertTrue(ci_classify.documents_only("pull_request", changed))

    def test_one_file_off_the_list_makes_it_code(self) -> None:
        for outsider in (
            "infra/kind/README.md",
            "config/registry/README.md",
            "src/meridian/platform/migrations/README.md",
            "data/evaluation/README.md",
            "tests/meridian/test_ci_config.py",
            "scripts/ci_classify.py",
            ".github/workflows/python.yml",
            "Makefile",
            "pyproject.toml",
            "uv.lock",
            ".claude/settings.json",
            "docs",
            "NOTICE.txt",
            "README.md.bak",
            "readme.md",
            "LICENSE.md",
            "sub/README.md",
            "docsx/a.md",
            "Docs/a.md",
        ):
            with self.subTest(outsider=outsider):
                self.assertFalse(
                    ci_classify.documents_only(
                        "pull_request", ["docs/a.md", outsider, "README.md"]
                    )
                )
                self.assertFalse(
                    ci_classify.documents_only("pull_request", [outsider])
                )

    def test_a_path_that_is_not_a_plain_repository_path_is_code(self) -> None:
        for odd in (
            "",
            "docs/",
            "docs//a.md",
            "docs/../src/a.py",
            "docs/./a.md",
            "./docs/a.md",
            "/docs/a.md",
            "docs\\a.md",
            "docs/a.md\x00",
            "docs/a.md\n",
        ):
            with self.subTest(odd=odd):
                self.assertFalse(ci_classify.documents_only("pull_request", [odd]))

    def test_nothing_changed_is_not_documents_only(self) -> None:
        self.assertFalse(ci_classify.documents_only("pull_request", []))

    def test_a_failed_diff_is_not_documents_only(self) -> None:
        self.assertFalse(ci_classify.documents_only("pull_request", None))

    def test_a_push_is_never_documents_only(self) -> None:
        self.assertFalse(ci_classify.documents_only("push", ["docs/a.md"]))

    def test_any_other_event_is_never_documents_only(self) -> None:
        for event in ("", "workflow_dispatch", "merge_group", "pull_request_target"):
            with self.subTest(event=event):
                self.assertFalse(ci_classify.documents_only(event, ["docs/a.md"]))


class Shards(unittest.TestCase):
    def test_the_list_counts_from_one_to_the_number(self) -> None:
        self.assertEqual(ci_classify.shard_list(4), [1, 2, 3, 4])
        self.assertEqual(ci_classify.shard_list(1), [1])
        self.assertEqual(ci_classify.shard_list(7), [1, 2, 3, 4, 5, 6, 7])

    def test_a_count_that_is_not_a_positive_whole_number_is_refused(self) -> None:
        for count in ("", "0", "-1", "four", "1.5", " 4", "4 "):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    ci_classify.parse_shards(count)

    def test_a_count_is_parsed(self) -> None:
        self.assertEqual(ci_classify.parse_shards("4"), 4)


class Job(unittest.TestCase):
    """The script as the job runs it: a real repository, a real diff."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "repo"
        self.root.mkdir()
        self.output = Path(folder.name) / "github-output"
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "user.name", "ci")
        self.write("src/code.py", "x = 1\n" * 40)
        self.write("docs/a.md", "# a\n" * 40)
        self.write("README.md", "# readme\n" * 40)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        done = subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        return done.stdout

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self) -> None:
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "change")

    def run_job(
        self, event: str = "pull_request", base: str | None = None, shards: str = "4"
    ) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GITHUB_OUTPUT"] = str(self.output)
        self.output.write_text("", encoding="utf-8")
        command = [sys.executable, str(SCRIPT), "--event", event, "--shards", shards]
        command += ["--base", self.base if base is None else base]
        return subprocess.run(
            command, cwd=self.root, env=env, capture_output=True, text=True
        )

    def outputs(self) -> dict[str, str]:
        found = {}
        for line in self.output.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            found[key] = value
        return found

    def test_a_documents_change_is_documents_only(self) -> None:
        self.write("docs/a.md", "# changed\n")
        self.write("README.md", "# changed\n")
        self.commit()

        done = self.run_job()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "true")
        self.assertEqual(self.outputs()["shard_list"], "[1,2,3,4]")
        self.assertIn("docs/a.md", done.stdout)
        self.assertIn("README.md", done.stdout)
        self.assertIn("docs_only=true", done.stdout)

    def test_a_code_change_next_to_a_documents_change_is_code(self) -> None:
        self.write("docs/a.md", "# changed\n")
        self.write("src/code.py", "x = 2\n")
        self.commit()

        done = self.run_job()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "false")
        self.assertIn("src/code.py", done.stdout)

    def test_a_rename_out_of_code_into_documents_is_a_change_to_code(self) -> None:
        # git diff --name-only shows only the new name of a rename, which would
        # be a document; the deleted file is code.
        (self.root / "docs").mkdir(exist_ok=True)
        self.git("mv", "src/code.py", "docs/code.md")
        self.commit()

        done = self.run_job()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "false")
        self.assertIn("src/code.py", done.stdout)

    def test_a_path_with_odd_characters_is_read_whole(self) -> None:
        self.write("docs/é \"quoted\".md", "# x\n")
        self.commit()

        done = self.run_job()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "true")

    def test_a_deleted_documents_file_is_a_documents_change(self) -> None:
        self.git("rm", "--quiet", "docs/a.md")
        self.commit()

        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.outputs()["docs_only"], "true")

    def test_no_change_at_all_is_not_documents_only(self) -> None:
        done = self.run_job()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "false")

    def test_a_base_git_does_not_know_is_not_documents_only(self) -> None:
        self.write("docs/a.md", "# changed\n")
        self.commit()

        done = self.run_job(base="0" * 40)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "false")
        self.assertIn("docs_only=false", done.stdout)

    def test_no_base_is_not_documents_only(self) -> None:
        self.write("docs/a.md", "# changed\n")
        self.commit()

        done = self.run_job(base="")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "false")

    def test_a_push_is_not_documents_only_whatever_changed(self) -> None:
        self.write("docs/a.md", "# changed\n")
        self.commit()

        done = self.run_job(event="push")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["docs_only"], "false")
        self.assertEqual(self.outputs()["shard_list"], "[1,2,3,4]")

    def test_the_shard_list_follows_the_count_it_is_given(self) -> None:
        self.run_job(shards="6")

        self.assertEqual(self.outputs()["shard_list"], "[1,2,3,4,5,6]")

    def test_a_bad_shard_count_fails_the_job(self) -> None:
        done = self.run_job(shards="0")

        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("docs_only", self.outputs())

    def test_a_file_name_cannot_write_a_workflow_command(self) -> None:
        # A line of the log that starts with :: is a command to the runner.
        self.write("docs/x.md", "# x\n")
        self.write("::set-output name=docs_only::true", "x\n")
        self.commit()

        done = self.run_job()

        self.assertEqual(self.outputs()["docs_only"], "false")
        for line in done.stdout.splitlines():
            self.assertFalse(line.startswith("::"), line)


if __name__ == "__main__":
    unittest.main()
