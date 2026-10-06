"""The lane check: a Stop hook that names a lane of parallel work sitting idle.

`.claude/hooks/check-lanes.sh` reads `.claude/lanes.md` under the project
directory (a table the main session keeps: Lane | Step | Out now | Idle
because | Next, and an optional `Target: N` line) and, when the session is
about to stop, blocks the stop once if a lane is idle with no reason or fewer
lanes run than the target. These tests run the hook as Claude Code does: the
event's JSON on standard input, `CLAUDE_PROJECT_DIR` in the environment, and
its decision read from standard output.

Unittest and not the guard's cases file: a case here needs a board on disk, an
environment and a read of standard error, which one JSON line carries badly;
and `make test` runs these in CI, where the guard's runner is the docs job's.

Every lane and step below is invented.

Run: python3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "check-lanes.sh"

HEADER = "| Lane | Step | Out now | Idle because | Next |\n|---|---|---|---|---|\n"
RUNNING = "| 1 | S001 alpha | X1 (implementer) | | land X1 |\n"
RUNNING_TOO = "| 2 | S002 beta | a cluster run | | read it |\n"
IDLE = "| 3 | S003 gamma | | | |\n"
IDLE_WITH_REASON = "| 4 | S004 delta | | waits for S001 to merge | start it |\n"
EN_DASH = "–"
EM_DASH = "—"


def board(*rows: str, target: int | None = None, prose: str = "") -> str:
    text = prose
    if target is not None:
        text += f"Target: {target}\n\n"
    return text + HEADER + "".join(rows)


class Run:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def decision(self) -> dict[str, str]:
        """The hook's JSON; empty when it printed nothing (a pass)."""
        return json.loads(self.stdout) if self.stdout.strip() else {}


class Lanes(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name)
        self.file = self.project / ".claude" / "lanes.md"
        self.file.parent.mkdir()

    def write(self, text: str | bytes) -> None:
        if isinstance(text, bytes):
            self.file.write_bytes(text)
        else:
            self.file.write_text(text)

    def stop(
        self,
        event: dict | str | None = None,
        *,
        project: Path | None = None,
        cwd: Path | None = None,
    ) -> Run:
        """Run the hook; `project` is CLAUDE_PROJECT_DIR, unset when None."""
        payload = event if isinstance(event, str) else json.dumps(event or {})
        variables = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        if project is not None:
            variables["CLAUDE_PROJECT_DIR"] = str(project)
        done = subprocess.run(
            ["bash", str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            env=variables,
            cwd=cwd,
            timeout=20,
            check=False,
        )
        return Run(done.returncode, done.stdout, done.stderr)

    def here(self, event: dict | str | None = None) -> Run:
        """The session's own stop: the project directory is the temporary one."""
        return self.stop(event, project=self.project)

    def reason(self, event: dict | str | None = None) -> str:
        """The reason of a block; fails when the hook let the stop through."""
        run = self.here(event)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.decision.get("decision"), "block", run.stdout)
        return run.decision["reason"]

    def assertPasses(self, run: Run) -> None:
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "")

    # What passes -------------------------------------------------------------

    def test_a_session_without_a_board_is_not_checked(self) -> None:
        run = self.here({"hook_event_name": "Stop"})
        self.assertPasses(run)
        self.assertEqual(run.stderr, "")

    def test_an_empty_board_file_is_not_checked(self) -> None:
        self.write("")
        run = self.here()
        self.assertPasses(run)
        self.assertEqual(run.stderr, "")

    def test_a_table_with_no_rows_and_no_target_passes(self) -> None:
        self.write(board())
        self.assertPasses(self.here())

    def test_lanes_that_all_have_something_out_pass(self) -> None:
        self.write(board(RUNNING, RUNNING_TOO))
        self.assertPasses(self.here())

    def test_an_idle_lane_with_a_reason_passes_when_there_is_no_target(self) -> None:
        self.write(board(RUNNING, IDLE_WITH_REASON))
        self.assertPasses(self.here())

    def test_as_many_running_lanes_as_the_target_pass(self) -> None:
        self.write(board(RUNNING, RUNNING_TOO, IDLE_WITH_REASON, target=2))
        self.assertPasses(self.here())

    def test_more_running_lanes_than_the_target_pass(self) -> None:
        self.write(board(RUNNING, RUNNING_TOO, target=1))
        self.assertPasses(self.here())

    def test_the_target_line_may_carry_a_note_after_the_number(self) -> None:
        self.write(board(RUNNING, target=1).replace("Target: 1", "Target: 1 (owner)"))
        self.assertPasses(self.here())

    def test_prose_around_the_table_is_ignored(self) -> None:
        prose = "# Lanes\n\nA lane is RUNNING only while an agent is out.\n\n"
        self.write(board(RUNNING, prose=prose) + "\nUpdated now.\n")
        self.assertPasses(self.here())

    def test_header_case_and_spacing_do_not_matter(self) -> None:
        header = "|lane|STEP|Out Now|idle because|next|\n|:--|--:|:-:|---|---|\n"
        self.write(header + RUNNING)
        self.assertPasses(self.here())

    def test_windows_line_endings_are_read_like_unix_ones(self) -> None:
        self.write(board(RUNNING, IDLE_WITH_REASON, target=1).replace("\n", "\r\n"))
        self.assertPasses(self.here())

    def test_a_target_of_zero_asks_for_nothing(self) -> None:
        self.write(board(IDLE_WITH_REASON, target=0))
        self.assertPasses(self.here())

    def test_a_dash_in_out_now_with_a_reason_is_idle_with_a_reason(self) -> None:
        self.write(board(RUNNING, f"| 6 | S006 | {EM_DASH} | on the owner | |\n"))
        self.assertPasses(self.here())

    # What blocks -------------------------------------------------------------

    def test_an_idle_lane_with_no_reason_blocks_and_is_named(self) -> None:
        self.write(board(RUNNING, IDLE))
        run = self.here({"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.decision["decision"], "block")
        reason = run.decision["reason"]
        self.assertIn("lane 3", reason)
        self.assertIn("S003 gamma", reason)
        self.assertNotIn("lane 1", reason)
        self.assertIn("Idle because", reason)
        self.assertIn(".claude/lanes.md", reason)

    def test_every_idle_lane_is_named_and_a_lane_with_a_reason_is_not(self) -> None:
        self.write(board(IDLE, IDLE_WITH_REASON, "| 5 | S005 | | | |\n", RUNNING))
        reason = self.reason()
        self.assertIn("lane 3", reason)
        self.assertIn("lane 5", reason)
        self.assertNotIn("lane 4", reason)

    def test_the_block_is_one_json_object_on_one_line(self) -> None:
        self.write(board(IDLE))
        run = self.here()
        self.assertEqual(len(run.stdout.strip().splitlines()), 1)
        self.assertEqual(set(json.loads(run.stdout)), {"decision", "reason"})

    def test_fewer_running_lanes_than_the_target_block_with_the_count(self) -> None:
        self.write(board(RUNNING, IDLE_WITH_REASON, target=3))
        reason = self.reason()
        self.assertIn("1 of 3", reason)
        self.assertIn("Target", reason)
        self.assertNotIn("no reason", reason)

    def test_an_empty_table_under_a_target_blocks(self) -> None:
        self.write(board(target=2))
        self.assertIn("0 of 2", self.reason())

    def test_an_idle_lane_and_a_short_target_are_both_said(self) -> None:
        self.write(board(RUNNING, IDLE, target=3))
        reason = self.reason()
        self.assertIn("lane 3", reason)
        self.assertIn("1 of 3", reason)

    def test_a_dash_in_out_now_and_idle_because_counts_as_empty(self) -> None:
        for dash in ("-", EM_DASH, EN_DASH):
            with self.subTest(dash=dash):
                self.write(board(RUNNING, f"| 6 | S006 | {dash} | {dash} | |\n"))
                self.assertIn("lane 6", self.reason())

    def test_a_row_with_an_empty_step_is_named_by_its_lane(self) -> None:
        self.write(board("| 7 | | | | |\n"))
        self.assertIn("lane 7", self.reason())

    def test_a_board_with_prose_and_a_target_note_is_read_whole(self) -> None:
        prose = "# Lanes\n\nTarget: 2 (the owner)\n\nA lane is RUNNING when out.\n\n"
        self.write(prose + HEADER + RUNNING + IDLE)
        reason = self.reason()
        self.assertIn("lane 3", reason)
        self.assertIn("1 of 2", reason)

    def test_the_board_is_found_by_the_project_directory_not_the_working_one(
        self,
    ) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        other = Path(elsewhere.name)
        (other / ".claude").mkdir()
        (other / ".claude" / "lanes.md").write_text(board(IDLE))
        self.write(board(RUNNING))
        self.assertPasses(self.stop(project=self.project, cwd=other))

    def test_without_a_project_directory_the_working_one_is_used(self) -> None:
        self.write(board(IDLE))
        run = self.stop(cwd=self.project)
        self.assertEqual(run.decision.get("decision"), "block")

    # What the hook leaves alone ---------------------------------------------

    def test_it_blocks_once_and_lets_the_second_stop_through(self) -> None:
        self.write(board(IDLE, target=4))
        self.reason({"stop_hook_active": False})
        self.assertPasses(self.here({"stop_hook_active": True}))

    def test_a_subagents_stop_is_not_checked(self) -> None:
        self.write(board(IDLE))
        event = {"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "x"}
        self.assertPasses(self.here(event))

    def test_an_empty_agent_id_is_the_main_session(self) -> None:
        self.write(board(IDLE))
        self.reason({"agent_id": ""})

    # A board it cannot read is said to the session, once -----------------------

    UNREADABLE = "is not a board the hook can read"

    def assertUnreadable(self, why: str, event: dict | None = None) -> str:
        """The block's reason for a board that is not one; the second stop passes."""
        reason = self.reason(event)
        self.assertIn(self.UNREADABLE, reason)
        self.assertIn("lanes.md", reason)
        self.assertIn(why, reason)
        self.assertIn("'|'", reason, "the reason must say a cell may not hold a pipe")
        self.assertPasses(self.here({"stop_hook_active": True}))
        return reason

    def test_a_row_with_too_few_cells_is_said_and_blocks_once(self) -> None:
        self.write(board(RUNNING, "| 3 | S003 | |\n"))
        self.assertUnreadable("a row has 3 cells, not 5")

    def test_a_row_with_too_many_cells_is_said_and_blocks_once(self) -> None:
        self.write(board(IDLE, "| 3 | S003 | | | | extra |\n"))
        self.assertUnreadable("a row has 6 cells, not 5")

    def test_a_pipe_inside_a_cell_is_said_and_blocks_once(self) -> None:
        for cell in ("a | b", r"a \| b", "`a | b`"):
            with self.subTest(cell=cell):
                self.write(board(RUNNING, f"| 3 | S003 | {cell} | | |\n"))
                self.assertUnreadable("a row has 6 cells, not 5")

    def test_a_second_table_of_another_shape_is_said_and_blocks_once(self) -> None:
        legend = "\n| Word | Meaning |\n|---|---|\n| out | an agent is out |\n"
        self.write(board(RUNNING) + legend)
        self.assertUnreadable("a row has 2 cells, not 5")

    def test_a_table_without_the_five_columns_is_said_and_blocks_once(self) -> None:
        self.write("| Lane | Step | Notes |\n|---|---|---|\n| 1 | S001 | x |\n")
        self.assertUnreadable("the first table is not Lane | Step")

    def test_columns_in_another_order_are_said_and_blocks_once(self) -> None:
        header = (
            "| Lane | Step | Idle because | Out now | Next |\n|---|---|---|---|---|\n"
        )
        self.write(header + IDLE)
        self.assertUnreadable("the first table is not Lane | Step")

    def test_a_row_before_any_header_is_said_and_blocks_once(self) -> None:
        self.write(IDLE + HEADER)
        self.assertUnreadable("the first table is not Lane | Step")

    def test_a_target_that_is_not_a_number_is_said_and_blocks_once(self) -> None:
        for line in ("Target: many", "Target: the plan", "Target:"):
            with self.subTest(line=line):
                self.write(board(IDLE, prose=line + "\n"))
                self.assertUnreadable("Target is not a number")

    def test_a_target_of_eight_digits_is_said_and_blocks_once(self) -> None:
        self.write(board(RUNNING, prose="Target: 12345678\n"))
        self.assertUnreadable("Target is too large")

    def test_a_bold_target_is_read_as_a_target(self) -> None:
        for line in ("**Target:** 3", "**Target**: 3", "__Target:__ 3"):
            with self.subTest(line=line):
                self.write(board(RUNNING, prose=line + "\n\n"))
                self.assertIn("1 of 3", self.reason())

    def test_a_bold_target_that_is_not_a_number_is_not_ignored(self) -> None:
        self.write(board(RUNNING, prose="**Target:** many\n\n"))
        self.assertUnreadable("Target is not a number")

    def test_binary_content_with_no_table_is_a_pass(self) -> None:
        self.write(b"\x00\xff\xfe not | a | board\x00\nmore bytes \xc3\n")
        self.assertPasses(self.here())

    def test_binary_content_around_a_row_is_said_and_blocks_once(self) -> None:
        self.write(b"\x00\xff\xfe| not | a | board\x00\n| 1 | 2 | 3 | 4 | 5 |\n")
        self.assertUnreadable("the first table is not Lane | Step")

    def test_input_that_is_not_json_is_a_note_and_a_pass(self) -> None:
        self.write(board(IDLE))
        run = self.here("this is not json")
        self.assertPasses(run)
        self.assertEqual(len(run.stderr.strip().splitlines()), 1)

    def test_empty_input_and_null_are_not_json_events_and_pass(self) -> None:
        self.write(board(IDLE))
        for payload in ("", "  \n", "null", "[]", '"x"', "7", "true"):
            with self.subTest(payload=payload):
                run = self.here(payload)
                self.assertPasses(run)
                self.assertEqual(len(run.stderr.strip().splitlines()), 1)

    # Rows that look like something else ---------------------------------------

    def test_a_repeated_header_row_is_not_a_lane(self) -> None:
        again = HEADER + IDLE_WITH_REASON
        self.write(board(RUNNING, target=2) + "\n" + again)
        self.assertIn("1 of 2", self.reason())

    def test_a_second_table_of_the_same_shape_is_read_without_its_delimiter(
        self,
    ) -> None:
        self.write(board(RUNNING, target=2) + "\n" + HEADER + RUNNING_TOO)
        self.assertPasses(self.here())

    def test_a_row_of_five_dashes_is_an_idle_lane_not_a_delimiter(self) -> None:
        self.write(board(RUNNING, "| - | - | - | - | - |\n"))
        self.assertIn("lane -", self.reason())

    def test_a_row_of_dashes_in_the_second_place_is_the_delimiter(self) -> None:
        header = "| Lane | Step | Out now | Idle because | Next |\n"
        self.write(header + "|-|-|-|-|-|\n")
        self.assertPasses(self.here())

    # A board that is very large -----------------------------------------------

    def test_a_huge_board_still_gives_a_short_valid_block(self) -> None:
        rows = [f"| {n} | S{n} | | | |\n" for n in range(1, 20001)]
        self.write(board(*rows))
        reason = self.reason()
        self.assertLess(len(reason), 3000)
        self.assertIn("lane 1 ", reason)
        self.assertNotIn("lane 11 ", reason)
        self.assertIn("19990 more", reason)

    def test_a_few_idle_lanes_are_all_named_and_a_crowd_is_counted(self) -> None:
        self.write(board(*[f"| {n} | S{n} | | | |\n" for n in range(1, 11)]))
        reason = self.reason()
        self.assertIn("lane 10 ", reason)
        self.assertNotIn("more", reason)
        self.write(board(*[f"| {n} | S{n} | | | |\n" for n in range(1, 13)]))
        reason = self.reason()
        self.assertNotIn("lane 11 ", reason)
        self.assertIn("2 more", reason)

    def test_a_very_long_lane_name_is_cut_before_it_reaches_the_reason(self) -> None:
        self.write(board(f"| {'é' * 300000} | {'s' * 300000} | | | |\n"))
        reason = self.reason()
        self.assertLess(len(reason), 3000)
        self.assertIn("lane ", reason)

    def test_a_board_file_that_cannot_be_read_is_a_pass(self) -> None:
        self.write(board(IDLE))
        self.file.chmod(0)
        self.addCleanup(self.file.chmod, 0o600)
        if os.access(self.file, os.R_OK):
            self.skipTest("this user reads a file with no permission bits (root)")
        self.assertPasses(self.here())

    def test_a_directory_named_lanes_md_is_a_pass(self) -> None:
        self.file.mkdir()
        self.assertPasses(self.here())


class Shipped(unittest.TestCase):
    """The hook as the repository wires it."""

    def settings(self) -> dict:
        return json.loads((ROOT / ".claude" / "settings.json").read_text())

    def test_the_hook_is_wired_on_stop_without_async_and_with_a_timeout(self) -> None:
        entries = [
            hook
            for group in self.settings()["hooks"]["Stop"]
            for hook in group["hooks"]
            if "check-lanes.sh" in hook["command"]
        ]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].get("async", False), "an async hook cannot block")
        self.assertLessEqual(entries[0]["timeout"], 10)

    def test_the_existing_stop_and_session_end_entries_stay_async(self) -> None:
        for event in ("Stop", "SessionEnd"):
            others = [
                hook
                for group in self.settings()["hooks"][event]
                for hook in group["hooks"]
                if "check-lanes.sh" not in hook["command"]
            ]
            self.assertTrue(others)
            for hook in others:
                self.assertTrue(hook.get("async"), hook["command"])

    def test_the_board_is_not_tracked(self) -> None:
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn(".claude/lanes.md", ignored)


if __name__ == "__main__":
    unittest.main()
