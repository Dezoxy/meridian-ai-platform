"""Tests for scripts/check_pr_step.py: a step's pull request changes its file (S102).

Each test makes a small git repository in a temporary directory (no network, a fixed
author through the environment, no global config) and runs the script as a subprocess
with ``PR_TITLE`` set and the repository as the working directory. The title is
untrusted text: one test hands it a command substitution and a newline.

Run: python3 -m unittest discover -s tests
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_pr_step.py"
STEP_FILE = "docs/plan/steps/S100-S119/S102.md"
OTHER_FILE = "docs/plan/steps/S100-S119/S101.md"
EARLIER_FILE = "docs/plan/steps/S020-S039/S021.md"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
NOTHING = "pr step: the title names no step; nothing to check"


class PrStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()

    def env(self, title=None, base=None):
        env = {k: v for k, v in os.environ.items() if k not in ("PR_TITLE", "PR_BASE")}
        env.update(GIT_ENV)
        env["GIT_CEILING_DIRECTORIES"] = str(self.repo.parent)
        if title is not None:
            env["PR_TITLE"] = title
        if base is not None:
            env["PR_BASE"] = base
        return env

    def git(self, *args):
        done = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env(),
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def init(self):
        self.git("init", "-q")
        for name in (STEP_FILE, OTHER_FILE, EARLIER_FILE):
            self.write(name, f"{name}\n")
        return self.commit("first")

    def run_script(self, title=None, base=None, cwd=None):
        done = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=cwd or self.repo,
            env=self.env(title, base),
            capture_output=True,
            text=True,
        )
        return done.returncode, done.stdout + done.stderr

    def after(self, change):
        """A repository whose second commit applies ``change`` (name -> text|None)."""
        self.init()
        for name, text in change.items():
            if text is None:
                (self.repo / name).unlink()
            else:
                self.write(name, text)
        self.commit("second")


class NamedStep(PrStep):
    def test_the_steps_file_changed_passes_and_says_which(self):
        self.after({STEP_FILE: "changed\n"})
        code, out = self.run_script("S102: the plan reads as a plan")
        self.assertEqual(code, 0, out)
        self.assertIn("S102", out)
        self.assertIn(STEP_FILE, out)

    def test_the_steps_file_added_passes(self):
        self.after({"docs/plan/steps/S100-S119/S103.md": "new\n"})
        code, out = self.run_script("S103: a new step")
        self.assertEqual(code, 0, out)

    def test_the_steps_file_renamed_in_passes(self):
        self.init()
        self.git("mv", OTHER_FILE, "docs/plan/steps/S100-S119/S104.md")
        self.commit("second")
        code, out = self.run_script("S104: moved here")
        self.assertEqual(code, 0, out)

    def test_the_steps_file_unchanged_fails_with_the_remedy(self):
        self.after({OTHER_FILE: "changed\n"})
        code, out = self.run_script("S102: the plan reads as a plan")
        self.assertEqual(code, 1, out)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("S102", out)
        self.assertIn(STEP_FILE, out)
        self.assertIn("the step's record is its file", out)

    def test_the_steps_file_only_deleted_fails(self):
        self.after({STEP_FILE: None})
        code, out = self.run_script("S102: the plan reads as a plan")
        self.assertEqual(code, 1, out)

    def test_another_steps_file_changed_fails(self):
        self.after({EARLIER_FILE: "changed\n"})
        code, out = self.run_script("S102: the plan reads as a plan")
        self.assertEqual(code, 1, out)

    def test_a_step_below_twenty_is_in_the_folder_of_its_range(self):
        self.after({EARLIER_FILE: "changed\n"})
        code, out = self.run_script("S021: sign-in")
        self.assertEqual(code, 0, out)
        self.assertIn(EARLIER_FILE, out)

    def test_the_comma_form_and_the_space_form_name_the_step(self):
        self.after({OTHER_FILE: "changed\n"})
        for title in ("S102, first part: x", "S102 first part", "S102:x"):
            with self.subTest(title=title):
                code, out = self.run_script(title)
                self.assertEqual(code, 1, out)
                self.assertIn("S102", out)
        code, out = self.run_script("S101, first part: x")
        self.assertEqual(code, 0, out)


class NoStep(PrStep):
    def test_a_title_that_names_no_step_has_nothing_to_check(self):
        self.after({OTHER_FILE: "changed\n"})
        for title in (
            "docs: S102 is done",
            "s102: lower case",
            "XS102: prefixed",
            "S1020: four digits",
            "S10: two digits",
            "S102",
            "S102x: letter",
            "chore(deps): update a pin",
            " S102: leading space",
        ):
            with self.subTest(title=title):
                code, out = self.run_script(title)
                self.assertEqual(code, 0, out)
                self.assertEqual(out.strip(), NOTHING)


class FailClosed(PrStep):
    def test_no_title_or_an_empty_one_is_exit_two(self):
        self.after({STEP_FILE: "changed\n"})
        for title in (None, "", "   "):
            with self.subTest(title=title):
                code, out = self.run_script(title)
                self.assertEqual(code, 2, out)
                self.assertEqual(len(out.strip().splitlines()), 1, out)
                self.assertIn("PR_TITLE", out)

    def test_a_repository_with_one_commit_is_exit_two_not_a_pass(self):
        self.init()
        code, out = self.run_script("S102: x")
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)

    def test_a_directory_that_is_no_repository_is_exit_two(self):
        bare = self.repo / "bare"
        bare.mkdir()
        code, out = self.run_script("S102: x", cwd=bare)
        self.assertEqual(code, 2, out)

    def test_a_base_that_does_not_exist_is_exit_two(self):
        self.after({STEP_FILE: "changed\n"})
        code, out = self.run_script("S102: x", base="0" * 40)
        self.assertEqual(code, 2, out)

    def test_a_title_that_names_no_step_needs_no_repository(self):
        bare = self.repo / "bare"
        bare.mkdir()
        code, out = self.run_script("docs: x", cwd=bare)
        self.assertEqual(code, 0, out)


class Base(PrStep):
    def test_pr_base_is_used_in_place_of_the_first_parent(self):
        first = self.init()
        self.write(STEP_FILE, "changed\n")
        self.commit("second")
        self.write(OTHER_FILE, "changed\n")
        self.commit("third")
        code, out = self.run_script("S102: x")
        self.assertEqual(code, 1, out)
        code, out = self.run_script("S102: x", base=first)
        self.assertEqual(code, 0, out)

    def test_an_empty_pr_base_is_the_first_parent(self):
        self.after({STEP_FILE: "changed\n"})
        code, out = self.run_script("S102: x", base="")
        self.assertEqual(code, 0, out)


class UntrustedTitle(PrStep):
    TITLE = "S001: $(touch pwned) `touch pwned2`\n\"quote\" 'q' ; touch pwned3 #"

    def check(self, expected):
        code, out = self.run_script(self.TITLE)
        self.assertEqual(code, expected, out)
        for text in ("pwned", "touch", "$(", "`", "quote"):
            self.assertNotIn(text, out)
        self.assertIn("S001", out)
        self.assertEqual(sorted(p.name for p in self.repo.glob("pwned*")), [])

    def test_the_title_is_judged_by_its_step_alone_and_is_never_run(self):
        self.init()
        self.write("docs/plan/steps/S000-S019/S001.md", "changed\n")
        self.commit("second")
        self.check(0)

    def test_the_title_is_not_printed_when_the_check_fails_either(self):
        self.after({OTHER_FILE: "changed\n"})
        self.check(1)

    def test_a_title_with_no_step_is_not_printed(self):
        self.after({OTHER_FILE: "changed\n"})
        code, out = self.run_script('docs: $(touch pwned) `x`\n"y"')
        self.assertEqual((code, out.strip()), (0, NOTHING))
        self.assertFalse((self.repo / "pwned").exists())

    def test_the_script_uses_no_shell_and_no_title_in_a_command(self):
        text = SCRIPT.read_text()
        self.assertNotIn("shell=True", text)
        self.assertNotIn("os.system", text)


if __name__ == "__main__":
    unittest.main()
