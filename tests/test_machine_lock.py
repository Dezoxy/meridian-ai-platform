"""The machine's test lock: one test run with a container or a suite at a time.

`scripts/machine_lock.sh` is sourced by the recipes of `make pytest`,
`make pytest-db` and `make alerts` (S099). It takes one exclusive `flock` in
the recipe's own shell, so a second session's run on the same machine waits and
says who holds the lock, and one that waited too long runs nothing. These tests
source it as a recipe does (`/bin/sh`, the label in `MACHINE_LOCK_LABEL`), each
with a lock folder of its own (`MERIDIAN_LOCK_DIR`), so they never meet a real
run's lock.

The order of two runs is read from files the commands leave, never from a
sleep, and every wait on a file has a deadline: a hang would otherwise end as
a cancelled job with no message.

Run: python3 -m unittest discover -s tests
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "machine_lock.sh"
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
DEADLINE_SECONDS = 30
HOLD = "touch started; while [ ! -e release ]; do sleep 0.05; done; touch first-done"


def recipe(target: str) -> str:
    return MAKEFILE.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]


@unittest.skipUnless(shutil.which("flock"), "needs flock, as the lock does")
class MachineLock(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name)
        self.locks = self.work / "locks"
        self.started: list[subprocess.Popen[str]] = []
        self.addCleanup(self.stop_what_is_left)

    def stop_what_is_left(self) -> None:
        (self.work / "release").touch()
        for process in self.started:
            try:
                process.wait(DEADLINE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def start(self, label: str, command: str, **env: str) -> subprocess.Popen[str]:
        with (self.work / f"{label}.err").open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(
                [
                    "/bin/sh",
                    "-c",
                    f'MACHINE_LOCK_LABEL={label} . "{SCRIPT}" && {command}',
                ],
                cwd=self.work,
                env={**os.environ, "MERIDIAN_LOCK_DIR": str(self.locks), **env},
                stdout=subprocess.DEVNULL,
                stderr=errors,
                text=True,
            )
        self.started.append(process)
        return process

    def said(self, label: str) -> str:
        return (self.work / f"{label}.err").read_text(encoding="utf-8")

    def wait_for(self, what: str, happened) -> None:
        deadline = time.monotonic() + DEADLINE_SECONDS
        while not happened():
            if time.monotonic() > deadline:
                self.fail(f"waited {DEADLINE_SECONDS} s for {what}")
            time.sleep(0.02)

    def test_a_run_gets_the_lock_records_itself_and_keeps_its_own_status(self) -> None:
        run = self.start("alone", "touch ran; exit 7")

        self.assertEqual(run.wait(DEADLINE_SECONDS), 7)
        self.assertTrue((self.work / "ran").exists())
        holder = (self.locks / "tests.holder").read_text(encoding="utf-8")
        self.assertRegex(
            holder,
            rf"^alone in {re.escape(str(self.work))} since "
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \(pid \d+\)\n$",
        )
        self.assertEqual(self.said("alone"), "")

    def test_a_second_run_waits_for_the_first_and_says_who_holds_the_lock(self) -> None:
        self.start("first", HOLD)
        self.wait_for("the first run to start", (self.work / "started").exists)
        second = self.start(
            "second", "[ -e first-done ] && touch in-order; touch second-done"
        )
        self.wait_for("the second run to say it waits", lambda: self.said("second"))

        self.assertFalse((self.work / "second-done").exists())
        self.assertRegex(
            self.said("second"),
            r"^machine lock: held by first in \S+ since \S+ \(pid \d+\); "
            rf"second in {re.escape(str(self.work))} waits up to 1800 s\n$",
        )

        (self.work / "release").touch()
        self.assertEqual(second.wait(DEADLINE_SECONDS), 0)
        self.assertTrue((self.work / "in-order").exists())
        # The lock's record is the one who holds it now.
        holder = (self.locks / "tests.holder").read_text(encoding="utf-8")
        self.assertTrue(holder.startswith("second in "), holder)

    def test_a_run_that_waited_too_long_runs_nothing_and_fails(self) -> None:
        self.start("first", HOLD)
        self.wait_for("the first run to start", (self.work / "started").exists)

        late = self.start("late", "touch late-ran", MERIDIAN_LOCK_WAIT="1")

        self.assertEqual(late.wait(DEADLINE_SECONDS), 75)
        self.assertFalse((self.work / "late-ran").exists())
        said = self.said("late").splitlines()
        self.assertEqual(len(said), 2, said)
        self.assertIn("held by first in ", said[0])
        self.assertRegex(
            said[1],
            r"^machine lock: still held after 1 s by first in .*; nothing was run$",
        )

    def test_a_wait_of_zero_is_no_way_past_a_held_lock(self) -> None:
        self.start("first", HOLD)
        self.wait_for("the first run to start", (self.work / "started").exists)

        hasty = self.start("hasty", "touch hasty-ran", MERIDIAN_LOCK_WAIT="0")

        self.assertEqual(hasty.wait(DEADLINE_SECONDS), 75)
        self.assertFalse((self.work / "hasty-ran").exists())

    def test_the_lock_is_free_again_when_its_run_has_ended(self) -> None:
        self.assertEqual(self.start("one", "exit 3").wait(DEADLINE_SECONDS), 3)

        after = self.start("two", "touch two-ran", MERIDIAN_LOCK_WAIT="0")

        self.assertEqual(after.wait(DEADLINE_SECONDS), 0)
        self.assertTrue((self.work / "two-ran").exists())
        self.assertEqual(self.said("two"), "")

    def test_a_wait_that_is_no_number_of_seconds_runs_nothing(self) -> None:
        for value in ("soon", "-1", "1.5", ""):
            with self.subTest(value=value):
                label = f"bad{len(self.started)}"
                run = self.start(label, "touch bad-ran", MERIDIAN_LOCK_WAIT=value)

                if value == "":
                    # An empty value is an unset one: the default.
                    self.assertEqual(run.wait(DEADLINE_SECONDS), 0)
                    (self.work / "bad-ran").unlink()
                    continue
                self.assertEqual(run.wait(DEADLINE_SECONDS), 64)
                self.assertFalse((self.work / "bad-ran").exists())
                self.assertIn("MERIDIAN_LOCK_WAIT", self.said(label))

    def test_the_holder_not_yet_written_is_said_so(self) -> None:
        # The first run has the lock and has not written its record yet: a
        # stand-in holds the lock file itself and writes none.
        self.locks.mkdir()
        self.started.append(
            subprocess.Popen(
                ["flock", str(self.locks / "tests.lock"), "/bin/sh", "-c", HOLD],
                cwd=self.work,
            )
        )
        self.wait_for("the stand-in to start", (self.work / "started").exists)

        late = self.start("late", "touch late-ran", MERIDIAN_LOCK_WAIT="0")

        self.assertEqual(late.wait(DEADLINE_SECONDS), 75)
        self.assertIn("held by (holder not yet recorded)", self.said("late"))


class WithoutFlock(unittest.TestCase):
    def test_a_machine_without_flock_runs_the_command_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder, "bin")
            empty.mkdir()
            run = subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    f'MACHINE_LOCK_LABEL=bare . "{SCRIPT}" && : >ran',
                ],
                cwd=folder,
                env={
                    "PATH": str(empty),
                    "MERIDIAN_LOCK_DIR": str(Path(folder, "locks")),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(Path(folder, "ran").exists())
            self.assertEqual(
                run.stderr,
                "machine lock: no flock on this machine, bare runs without the lock\n",
            )
            self.assertFalse(Path(folder, "locks").exists())


class TheMakefile(unittest.TestCase):
    """Which recipes take the lock, and where in them."""

    def test_the_lock_is_one_variable_that_names_the_target(self) -> None:
        self.assertIn(
            "\nMACHINE_LOCK = MACHINE_LOCK_LABEL=$@ . scripts/machine_lock.sh\n",
            MAKEFILE,
        )

    def test_pytest_takes_the_lock_in_front_of_its_run(self) -> None:
        self.assertEqual(
            recipe("pytest").strip(),
            "$(MACHINE_LOCK) && uv run pytest -n $(PYTEST_WORKERS) "
            "$(PYTEST_COVERAGE_ARGS) $(PYTEST_ARGS)",
        )

    def test_pytest_db_takes_it_before_it_removes_a_container_or_sets_a_trap(
        self,
    ) -> None:
        # The recipe's first act was to remove the containers of its name, and
        # its exit trap removes them again: both would end ANOTHER session's
        # database, so the lock comes before either, and a run that gives up
        # waiting leaves through no trap.
        lines = recipe("pytest-db").splitlines()

        self.assertEqual(lines[0], "\t@set -e; \\")
        self.assertEqual(lines[1], "\t$(MACHINE_LOCK); \\")
        self.assertIn("docker rm -f", lines[2])
        self.assertTrue(lines[3].lstrip().startswith("trap "), lines[3])

    def test_alerts_takes_it_for_each_container_it_starts(self) -> None:
        lines = [line.strip() for line in recipe("alerts").splitlines()]
        starts = [line for line in lines if "docker run" in line]

        self.assertEqual(len(starts), 2)
        for line in starts:
            self.assertTrue(line.startswith("$(MACHINE_LOCK) && docker run "), line)

    def test_no_other_recipe_takes_it_so_no_run_waits_on_itself(self) -> None:
        # `make eval` and `make eval-baseline` reach the database through
        # `$(MAKE) pytest-db`; a lock of their own would wait on that one's.
        self.assertEqual(MAKEFILE.count("$(MACHINE_LOCK)"), 4)
        for target in ("eval", "eval-baseline"):
            with self.subTest(target=target):
                self.assertIn("$(MAKE) pytest-db ", recipe(target))
                self.assertNotIn("MACHINE_LOCK", recipe(target))

    def test_a_dry_run_prints_the_lock_and_takes_none(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            environment = {
                k: v
                for k, v in os.environ.items()
                if k not in ("COVERAGE", "PYTEST_ARGS", "MAKEFLAGS", "MFLAGS")
            }
            printed = subprocess.run(
                ["make", "-n", "pytest", "pytest-db", "alerts"],
                cwd=ROOT,
                env={**environment, "MERIDIAN_LOCK_DIR": folder},
                capture_output=True,
                text=True,
                check=True,
            ).stdout

            self.assertEqual(os.listdir(folder), [])
        self.assertEqual(
            printed.count("MACHINE_LOCK_LABEL=pytest . scripts/machine_lock.sh"), 1
        )
        self.assertEqual(
            printed.count("MACHINE_LOCK_LABEL=pytest-db . scripts/machine_lock.sh"), 1
        )
        self.assertEqual(
            printed.count("MACHINE_LOCK_LABEL=alerts . scripts/machine_lock.sh"), 2
        )

    def test_the_help_lines_say_which_targets_take_the_lock(self) -> None:
        for target in ("pytest", "pytest-db", "alerts"):
            with self.subTest(target=target):
                (line,) = re.findall(rf"^## {target}\s+(.*)$", MAKEFILE, re.MULTILINE)
                self.assertIn("takes the machine's test lock", line)


if __name__ == "__main__":
    unittest.main()
