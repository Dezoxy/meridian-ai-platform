"""Every runner label a workflow runs on is one the workflow linter knows.

The advisory hook `check-iac.sh` runs actionlint on an edit of a workflow, and
actionlint refuses a label it has no name for. The installed one (1.7.12) does
not know `ubuntu-26.04`, so it reported the label on every edit of every
workflow until `.github/actionlint.yaml` named it.

A hook that nobody reads is not a gate, so this test is. A label a workflow
runs on is either one actionlint 1.7.12 knows (KNOWN, copied from its own
"available labels" message) or is listed in that file. A new label fails here,
and the fix is to add it to the file, with a comment, or to move the workflow
back to a label the linter knows.

The file is read with a regular expression, not a YAML parser: `make test` runs
under whatever `python3` the machine has, and the file is a list of names.

Run: python3 -m unittest discover -s tests
"""

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
CONFIG = ROOT / ".github" / "actionlint.yaml"

# The labels actionlint 1.7.12 lists as available. Not exhaustive for other
# versions; a newer actionlint that knows more is a reason to shrink the file,
# not this set.
KNOWN = {
    "windows-latest",
    "windows-latest-8-cores",
    "windows-2025",
    "windows-2025-vs2026",
    "windows-2022",
    "windows-11-arm",
    "ubuntu-slim",
    "ubuntu-latest",
    "ubuntu-latest-4-cores",
    "ubuntu-latest-8-cores",
    "ubuntu-latest-16-cores",
    "ubuntu-24.04",
    "ubuntu-24.04-arm",
    "ubuntu-22.04",
    "ubuntu-22.04-arm",
    "macos-latest",
    "macos-latest-xlarge",
    "macos-latest-large",
    "macos-26-intel",
    "macos-26-xlarge",
    "macos-26-large",
    "macos-26",
    "macos-15-intel",
    "macos-15-xlarge",
    "macos-15-large",
    "macos-15",
    "macos-14-xlarge",
    "macos-14-large",
    "macos-14",
    "self-hosted",
    "x64",
    "arm",
    "arm64",
    "linux",
    "macos",
    "windows",
}

RUNS_ON = re.compile(r"^\s*runs-on:\s*(?P<value>.*?)\s*(?:#.*)?$")


def runner_labels(path: Path) -> list[tuple[int, str]]:
    """(line, label) for every `runs-on:` of a workflow.

    A value that is not a plain name or a flow list of plain names (an
    expression, a matrix reference, a block list) is returned as it is, so the
    test fails on it and names the line: it needs a reader here first.
    """
    found = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        match = RUNS_ON.match(line)
        if match is None:
            continue
        value = match["value"].strip("'\"")
        if value.startswith("["):
            found += [
                (number, name.strip().strip("'\""))
                for name in value.strip("[]").split(",")
            ]
        else:
            found.append((number, value))
    return found


def configured_labels() -> set[str]:
    """The names under `labels:` in .github/actionlint.yaml."""
    names: set[str] = set()
    in_list = False
    for line in CONFIG.read_text().splitlines():
        if re.match(r"^\s*labels:\s*$", line):
            in_list = True
        elif in_list and (item := re.match(r"^\s*-\s*(\S+)\s*(?:#.*)?$", line)):
            names.add(item[1].strip("'\""))
        elif in_list and line.strip() and not line.lstrip().startswith("#"):
            in_list = False
    return names


class RunnerLabels(unittest.TestCase):
    def test_the_workflows_are_found_and_each_names_a_runner(self) -> None:
        self.assertTrue(WORKFLOWS, "no workflow under .github/workflows")
        for path in WORKFLOWS:
            self.assertTrue(runner_labels(path), f"{path.name} has no runs-on")

    def test_the_linter_config_exists_and_lists_labels(self) -> None:
        self.assertTrue(CONFIG.is_file(), f"{CONFIG} is missing")
        self.assertIn("ubuntu-26.04", configured_labels())

    def test_every_runner_label_is_known_to_actionlint_or_listed_in_its_config(
        self,
    ) -> None:
        listed = configured_labels() if CONFIG.is_file() else set()
        unknown = [
            f"{path.name}:{number}: {label}"
            for path in WORKFLOWS
            for number, label in runner_labels(path)
            if label not in KNOWN and label not in listed
        ]
        self.assertEqual(
            unknown,
            [],
            "actionlint will report these labels as unknown: add each to "
            ".github/actionlint.yaml under self-hosted-runner.labels, with a "
            "comment that says when to remove it",
        )

    def test_an_invented_label_is_in_neither_set(self) -> None:
        # The check above can fail: a label nobody listed is not passed.
        self.assertNotIn("ubuntu-99.99", KNOWN | configured_labels())


class Reader(unittest.TestCase):
    def read(self, text: str) -> list[tuple[int, str]]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "w.yml"
            path.write_text(text)
            return runner_labels(path)

    def test_a_plain_label_and_a_quoted_one_are_read_with_their_lines(self) -> None:
        text = "jobs:\n  a:\n    runs-on: ubuntu-26.04\n  b:\n    runs-on: 'x64'\n"
        self.assertEqual(self.read(text), [(3, "ubuntu-26.04"), (5, "x64")])

    def test_a_flow_list_gives_one_label_each(self) -> None:
        text = "jobs:\n  a:\n    runs-on: [self-hosted, linux]  # note\n"
        self.assertEqual(self.read(text), [(3, "self-hosted"), (3, "linux")])

    def test_a_block_list_is_returned_empty_so_the_test_fails_on_it(self) -> None:
        text = "jobs:\n  a:\n    runs-on:\n      - ubuntu-26.04\n"
        [(number, label)] = self.read(text)
        self.assertEqual(number, 3)
        self.assertNotIn(label, KNOWN)

    def test_an_expression_is_returned_whole_so_the_test_fails_on_it(self) -> None:
        text = "jobs:\n  a:\n    runs-on: ${{ matrix.os }}\n"
        [(_, label)] = self.read(text)
        self.assertNotIn(label, KNOWN)


if __name__ == "__main__":
    unittest.main()
