"""The shell-edit check: an advisory hook that names files a Bash command rewrote.

`.claude/hooks/check-shell-edits.sh` runs after every Bash call. Claude Code
(with `bashEditDiffEnabled`) puts the files the command changed into the
hook's input, `tool_response.bashEditDiff.changedFiles`; the hook says which of
them git tracks, because the edit gate and the advisory hooks (lint, boundary,
docs) read only what the Edit and Write tools change. These tests run the hook
as Claude Code does: the event's JSON on standard input, and what it prints on
standard output. The inputs follow the shape recorded from Claude Code 2.1.289
in a scratch project (see the hook's header).

Every path and name below is invented, in a temporary git repository.

Run: python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "check-shell-edits.sh"
BASH = shutil.which("bash") or "/bin/bash"
# What the hook may call; a test that takes one away says which.
TOOLS = ("cat", "dirname", "basename", "git", "jq", "realpath", "head", "tr", "sort")


class Run:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def context(self) -> str:
        """The advisory text; empty when the hook printed nothing."""
        if not self.stdout.strip():
            return ""
        out = json.loads(self.stdout)["hookSpecificOutput"]
        assert out["hookEventName"] == "PostToolUse", out
        return out["additionalContext"]


def event(
    command: str,
    changed: list[str] | None,
    *,
    cwd: Path | None = None,
    **extra: object,
) -> dict:
    """A PostToolUse event for a Bash call; `changed` None means no list."""
    response: dict = {"stdout": "", "stderr": "", "interrupted": False}
    if changed is not None:
        response["bashEditDiff"] = {
            "files": [],
            "moreFiles": 0,
            "changedFiles": changed,
        }
    base = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": response,
    }
    if cwd is not None:
        base["cwd"] = str(cwd)
    return base | extra


class ShellEdits(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.top = Path(folder.name).resolve()
        self.repo = self.top / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.track("src/app.py", "docs/guide.md", "uv.lock", "infra/main.tf")
        self.track("a file with spaces.md", "chart/values.yaml")
        (self.repo / "scratch.txt").write_text("not tracked\n")

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
        )

    def track(self, *names: str) -> None:
        for name in names:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        self.git("add", "--", *names)

    def path(self, name: str) -> str:
        return str(self.repo / name)

    def hook(
        self,
        payload: dict | str,
        *,
        project: Path | None = None,
        path: str | None = None,
        cwd: Path | None = None,
    ) -> Run:
        """Run the hook. `project` is CLAUDE_PROJECT_DIR (unset when None)."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        variables = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        if project is not None:
            variables["CLAUDE_PROJECT_DIR"] = str(project)
        if path is not None:
            variables["PATH"] = path
        done = subprocess.run(
            [BASH, str(HOOK)],
            input=text,
            capture_output=True,
            text=True,
            env=variables,
            cwd=cwd or self.repo,
            timeout=20,
            check=False,
        )
        return Run(done.returncode, done.stdout, done.stderr)

    def here(self, command: str, changed: list[str] | None, **extra: object) -> Run:
        return self.hook(event(command, changed, cwd=self.repo, **extra))

    def assertSilent(self, run: Run) -> None:
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "")

    def tools_without(self, *missing: str) -> str:
        """A PATH folder holding only the tools the hook may use, minus some."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for tool in TOOLS:
            found = shutil.which(tool)
            if tool not in missing and found:
                (Path(folder.name) / tool).symlink_to(found)
        return folder.name

    # What it says -----------------------------------------------------------

    def test_a_rewritten_tracked_source_file_is_named_in_one_line(self) -> None:
        run = self.here("sed -i s/a/b/ src/app.py", [self.path("src/app.py")])
        self.assertEqual(run.returncode, 0, run.stderr)
        text = run.context
        self.assertEqual(len(text.splitlines()), 1, text)
        self.assertIn("src/app.py", text)
        self.assertNotIn(str(self.repo), text)
        self.assertIn("Edit", text)
        self.assertIn("Write", text)
        self.assertIn("did not see", text)

    def test_the_output_is_advisory_context_and_never_a_decision(self) -> None:
        run = self.here("sed -i s/a/b/ src/app.py", [self.path("src/app.py")])
        out = json.loads(run.stdout)
        self.assertEqual(set(out), {"hookSpecificOutput"})
        self.assertEqual(
            set(out["hookSpecificOutput"]), {"hookEventName", "additionalContext"}
        )

    def test_every_tracked_file_the_command_rewrote_is_named(self) -> None:
        changed = [
            self.path(n) for n in ("src/app.py", "docs/guide.md", "chart/values.yaml")
        ]
        text = self.here("python3 rewrite.py", changed).context
        for name in ("src/app.py", "docs/guide.md", "chart/values.yaml"):
            self.assertIn(name, text)

    def test_a_file_name_with_spaces_is_named_whole(self) -> None:
        text = self.here("cmd", [self.path("a file with spaces.md")]).context
        self.assertIn("a file with spaces.md", text)

    def test_a_tracked_file_among_untracked_ones_is_the_only_one_named(self) -> None:
        changed = [self.path("scratch.txt"), self.path("src/app.py")]
        text = self.here("cmd", changed).context
        self.assertIn("src/app.py", text)
        self.assertNotIn("scratch.txt", text)

    def test_a_long_list_is_cut_and_says_how_many_more(self) -> None:
        names = [f"many/f{i:02d}.md" for i in range(14)]
        self.track(*names)
        text = self.here("cmd", [self.path(n) for n in names]).context
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("and 4 more", text)
        self.assertIn("many/f00.md", text)
        self.assertNotIn("many/f13.md", text)

    def test_a_subagents_input_names_the_agent(self) -> None:
        run = self.here(
            "sed -i s/a/b/ src/app.py",
            [self.path("src/app.py")],
            agent_id="a1",
            agent_type="implementer",
        )
        self.assertIn("implementer", run.context)

    def test_the_main_session_is_not_given_an_agent_name(self) -> None:
        text = self.here("sed -i s/a/b/ src/app.py", [self.path("src/app.py")]).context
        self.assertNotIn("agent", text)

    def test_a_shared_list_says_another_command_may_have_made_some_changes(
        self,
    ) -> None:
        payload = event("cmd", [self.path("src/app.py")], cwd=self.repo)
        payload["tool_response"]["bashEditDiff"]["shared"] = True
        text = self.hook(payload).context
        self.assertIn("another", text)
        plain = self.here("cmd", [self.path("src/app.py")]).context
        self.assertNotIn("another", plain)

    # What it leaves alone ---------------------------------------------------

    def test_an_untracked_file_is_not_named(self) -> None:
        self.assertSilent(self.here("echo x > scratch.txt", [self.path("scratch.txt")]))

    def test_a_file_that_is_new_and_not_added_is_not_named(self) -> None:
        (self.repo / "new.py").write_text("x\n")
        self.assertSilent(self.here("echo x > new.py", [self.path("new.py")]))

    def test_the_uv_lock_a_tool_is_meant_to_rewrite_is_not_named(self) -> None:
        self.assertSilent(self.here("uv lock", [self.path("uv.lock")]))

    def test_a_formatter_command_does_not_name_the_files_it_formats(self) -> None:
        cases = {
            "ruff format src": "src/app.py",
            "uv run ruff format src/app.py": "src/app.py",
            "terraform fmt infra": "infra/main.tf",
            "terraform -chdir=infra fmt": "infra/main.tf",
        }
        for command, name in cases.items():
            with self.subTest(command=command):
                self.assertSilent(self.here(command, [self.path(name)]))

    def test_a_formatter_command_still_names_a_file_it_does_not_format(self) -> None:
        text = self.here(
            "sed -i s/a/b/ docs/guide.md && ruff format src",
            [
                self.path("src/app.py"),
                self.path("docs/guide.md"),
            ],
        ).context
        self.assertIn("docs/guide.md", text)
        self.assertNotIn("src/app.py", text)

    def test_a_command_that_only_mentions_a_formatter_is_not_excused(self) -> None:
        for command in (
            "ruff check --fix src",
            "echo ruffformat",
            "sed -i s/fmt/x/ src/app.py",
        ):
            with self.subTest(command=command):
                self.assertIn(
                    "src/app.py", self.here(command, [self.path("src/app.py")]).context
                )

    def test_a_file_outside_the_repository_is_not_named(self) -> None:
        other = self.top / "elsewhere"
        other.mkdir()
        (other / "app.py").write_text("x\n")
        subprocess.run(
            ["git", "-C", str(other), "init", "-q"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(other), "add", "app.py"], check=True, capture_output=True
        )
        self.assertSilent(self.here("cmd", [str(other / "app.py")]))

    def test_a_path_that_climbs_out_of_the_repository_is_not_named(self) -> None:
        sneaky = str(self.repo / ".." / "repo2" / "x.py")
        self.assertSilent(self.here("cmd", [sneaky]))

    # The repository is found from the event, not only from the environment ----

    def test_a_worktree_is_found_from_the_working_directory_in_the_input(self) -> None:
        elsewhere = self.top / "main-checkout"
        elsewhere.mkdir()
        run = self.hook(
            event("cmd", [self.path("src/app.py")], cwd=self.repo), project=elsewhere
        )
        self.assertIn("src/app.py", run.context)

    def test_the_project_directory_serves_when_the_input_has_no_working_directory(
        self,
    ) -> None:
        run = self.hook(
            event("cmd", [self.path("src/app.py")]), project=self.repo, cwd=self.top
        )
        self.assertIn("src/app.py", run.context)

    def test_a_working_directory_below_the_top_still_finds_the_repository(self) -> None:
        run = self.hook(event("cmd", [self.path("src/app.py")], cwd=self.repo / "src"))
        self.assertIn("src/app.py", run.context)

    # A broken or absent input must never get in a session's way -------------

    def test_a_command_with_no_changed_file_list_is_silent(self) -> None:
        self.assertSilent(self.here("ls", None))

    def test_an_empty_changed_file_list_is_silent(self) -> None:
        self.assertSilent(self.here("ls", []))

    def test_a_response_without_a_diff_object_but_with_other_keys_is_silent(
        self,
    ) -> None:
        payload = event("ls", None, cwd=self.repo)
        payload["tool_response"]["bashEditDiff"] = {"unavailable": True}
        self.assertSilent(self.hook(payload))

    def test_a_response_that_is_a_string_is_silent(self) -> None:
        payload = event("ls", None, cwd=self.repo)
        payload["tool_response"] = "text"
        self.assertSilent(self.hook(payload))

    def test_a_list_holding_things_that_are_not_paths_is_silent(self) -> None:
        self.assertSilent(self.here("ls", [None, 3, {"a": 1}, ""]))  # type: ignore[list-item]

    def test_input_that_is_not_json_is_silent(self) -> None:
        self.assertSilent(self.hook("this is not json"))

    def test_empty_input_is_silent(self) -> None:
        self.assertSilent(self.hook(""))

    def test_a_json_value_that_is_not_an_object_is_silent(self) -> None:
        self.assertSilent(self.hook("[1, 2]"))

    def test_without_jq_it_is_silent_and_with_jq_the_same_setup_speaks(self) -> None:
        payload = event("cmd", [self.path("src/app.py")], cwd=self.repo)
        with_jq = self.hook(payload, path=self.tools_without())
        self.assertIn(
            "src/app.py", with_jq.context, "the stripped PATH is missing a tool"
        )
        self.assertSilent(self.hook(payload, path=self.tools_without("jq")))

    def test_without_git_it_is_silent_and_with_git_the_same_setup_speaks(self) -> None:
        payload = event("cmd", [self.path("src/app.py")], cwd=self.repo)
        self.assertIn(
            "src/app.py", self.hook(payload, path=self.tools_without()).context
        )
        self.assertSilent(self.hook(payload, path=self.tools_without("git")))

    def test_a_working_directory_that_is_not_a_repository_is_silent(self) -> None:
        loose = self.top / "loose"
        loose.mkdir()
        payload = event("cmd", [self.path("src/app.py")], cwd=loose)
        self.assertSilent(self.hook(payload, cwd=loose))


class Shipped(unittest.TestCase):
    """The hook as the repository wires it."""

    def settings(self) -> dict:
        return json.loads((ROOT / ".claude" / "settings.json").read_text())

    def entries(self) -> list[tuple[str, dict]]:
        return [
            (group["matcher"], hook)
            for group in self.settings()["hooks"]["PostToolUse"]
            for hook in group["hooks"]
            if "check-shell-edits.sh" in hook["command"]
        ]

    def test_the_hook_is_wired_on_bash_with_a_short_timeout(self) -> None:
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        matcher, hook = entries[0]
        self.assertEqual(matcher, "Bash")
        self.assertLessEqual(hook["timeout"], 5)
        self.assertGreaterEqual(hook["timeout"], 1)

    def test_the_hook_is_not_asynchronous_beyond_what_an_advisory_hook_needs(
        self,
    ) -> None:
        # An async hook's output reaches the session late or never; this one
        # prints one line in milliseconds and has no reason to be async.
        _, hook = self.entries()[0]
        self.assertFalse(hook.get("async", False))

    def test_the_hook_says_where_the_setting_that_delivers_the_list_must_stand(
        self,
    ) -> None:
        # Claude Code 2.1.289 reads bashEditDiffEnabled from user, flag and
        # policy settings only; one in the project's settings.json was not
        # honoured in a trial, so the hook's header says where it must be set.
        header = HOOK.read_text().split("set -uo pipefail")[0]
        self.assertIn("user, flag or policy settings", header)
        self.assertIn("CLAUDE_CODE_BASH_EDIT_DIFF", header)

    def test_the_edit_tool_hooks_are_still_wired_to_edit_and_write(self) -> None:
        by_matcher = {
            g["matcher"]: g["hooks"] for g in self.settings()["hooks"]["PostToolUse"]
        }
        commands = " ".join(h["command"] for h in by_matcher["Edit|Write"])
        for name in (
            "check-py.sh",
            "check-iac.sh",
            "check-docs.sh",
            "check-boundary.sh",
        ):
            self.assertIn(name, commands)

    def test_the_implementers_instructions_carry_the_rule(self) -> None:
        text = (ROOT / ".claude" / "agents" / "implementer.md").read_text()
        for needle in ("Edit", "Write", "sed -i", "deviation"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
