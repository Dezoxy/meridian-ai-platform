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
under whatever `python3` the machine has, and the file is a list of names. Both
suffixes the hook lints (`.yml` and `.yaml`) are read. A `runs-on` that the
reader cannot turn into names (a block list, a mapping, an expression) fails
in its own test, which names the file and line and says the form is not read.

Run: python3 -m unittest discover -s tests
"""

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".github" / "actionlint.yaml"


def workflows(folder: Path) -> list[Path]:
    """Every workflow in a folder: `check-iac.sh` lints both suffixes."""
    return sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])


WORKFLOWS = workflows(ROOT / ".github" / "workflows")

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


def configured_labels(config: Path = CONFIG) -> set[str]:
    """The names under `self-hosted-runner:` then `labels:` in an actionlint config.

    actionlint reads the list from no other key, so a list under another key
    gives an empty set here too.
    """
    names: set[str] = set()
    in_section = in_list = False
    for line in config.read_text().splitlines():
        if re.match(r"^\S", line) and not line.startswith("#"):
            in_section = re.match(r"^self-hosted-runner:\s*(?:#.*)?$", line) is not None
            in_list = False
        elif in_section and re.match(r"^\s+labels:\s*(?:#.*)?$", line):
            in_list = True
        elif in_list and (item := re.match(r"^\s*-\s*(\S+)\s*(?:#.*)?$", line)):
            names.add(item[1].strip("'\""))
        elif in_list and line.strip() and not line.lstrip().startswith("#"):
            in_list = False
    return names


def is_a_name(label: str) -> bool:
    """Whether a label is a plain runner name this test can compare."""
    return re.fullmatch(r"[A-Za-z0-9._-]+", label) is not None


def classify(paths: list[Path], listed: set[str]) -> tuple[list[str], list[str]]:
    """(unreadable, unknown) as `file:line: label` for every `runs-on:` found.

    Unreadable is a form the reader could not turn into names (an empty value,
    which is a block list or a mapping, or an expression); unknown is a plain
    name that neither KNOWN nor `listed` holds.
    """
    unreadable, unknown = [], []
    for path in paths:
        for number, label in runner_labels(path):
            where = f"{path.name}:{number}: {label!r}"
            if not is_a_name(label):
                unreadable.append(where)
            elif label not in KNOWN and label not in listed:
                unknown.append(where)
    return unreadable, unknown


class RunnerLabels(unittest.TestCase):
    def test_the_workflows_are_found_and_each_names_a_runner(self) -> None:
        self.assertTrue(WORKFLOWS, "no workflow under .github/workflows")
        for path in WORKFLOWS:
            self.assertTrue(runner_labels(path), f"{path.name} has no runs-on")

    def test_the_linter_config_exists_and_lists_labels(self) -> None:
        self.assertTrue(CONFIG.is_file(), f"{CONFIG} is missing")
        self.assertIn("ubuntu-26.04", configured_labels())

    def test_every_runs_on_is_a_form_this_test_reads(self) -> None:
        listed = configured_labels() if CONFIG.is_file() else set()
        unreadable, _ = classify(WORKFLOWS, listed)
        self.assertEqual(
            unreadable,
            [],
            "this test does not read these runs-on forms (a block list, a "
            "mapping or an expression), so it cannot say whether actionlint "
            "knows their labels: write a plain name or a one-line list, or "
            "teach runner_labels the form",
        )

    def test_every_runner_label_is_known_to_actionlint_or_listed_in_its_config(
        self,
    ) -> None:
        listed = configured_labels() if CONFIG.is_file() else set()
        _, unknown = classify(WORKFLOWS, listed)
        self.assertEqual(
            unknown,
            [],
            "actionlint will report these labels as unknown: add each to "
            ".github/actionlint.yaml under self-hosted-runner.labels, with a "
            "comment that says when to remove it",
        )

    def test_the_workflow_folder_is_read_for_both_suffixes(self) -> None:
        names = {path.name for path in WORKFLOWS}
        self.assertTrue(any(name.endswith(".yml") for name in names), names)
        with tempfile.TemporaryDirectory() as folder:
            for name in ("a.yml", "b.yaml", "c.txt"):
                (Path(folder) / name).write_text("")
            found = [path.name for path in workflows(Path(folder))]
        self.assertEqual(found, ["a.yml", "b.yaml"])


class Classify(unittest.TestCase):
    """The test above can fail: each way it should is made to happen here."""

    def classify(self, text: str, listed: set[str] | None = None, suffix=".yml"):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"w{suffix}"
            path.write_text(text)
            return classify([path], listed or set())

    def test_a_label_nobody_listed_is_unknown(self) -> None:
        unreadable, unknown = self.classify("jobs:\n  a:\n    runs-on: ubuntu-99.99\n")
        self.assertEqual(unreadable, [])
        self.assertEqual(unknown, ["w.yml:3: 'ubuntu-99.99'"])

    def test_a_label_in_a_yaml_workflow_is_checked_too(self) -> None:
        _, unknown = self.classify(
            "jobs:\n  a:\n    runs-on: bogus-1\n", suffix=".yaml"
        )
        self.assertEqual(unknown, ["w.yaml:3: 'bogus-1'"])

    def test_a_listed_label_and_a_known_one_are_not_reported(self) -> None:
        text = "jobs:\n  a:\n    runs-on: ubuntu-99.99\n  b:\n    runs-on: x64\n"
        self.assertEqual(self.classify(text, {"ubuntu-99.99"}), ([], []))

    def test_a_form_the_reader_cannot_read_is_unreadable_with_file_and_line(
        self,
    ) -> None:
        forms = {
            "a block list": "    runs-on:\n      - ubuntu-26.04\n",
            "a mapping": "    runs-on:\n      group: big\n      labels: x\n",
            "an expression": "    runs-on: ${{ matrix.os }}\n",
            "a ternary": "    runs-on: ${{ a && 'x' || 'y' }}\n",
        }
        for form, body in forms.items():
            with self.subTest(form=form):
                unreadable, unknown = self.classify("jobs:\n  a:\n" + body)
                self.assertEqual(len(unreadable), 1, unreadable)
                self.assertTrue(unreadable[0].startswith("w.yml:3: "), unreadable)
                self.assertEqual(unknown, [])


class ConfigReader(unittest.TestCase):
    def read(self, text: str) -> set[str]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "actionlint.yaml"
            path.write_text(text)
            return configured_labels(path)

    def test_labels_under_the_runner_key_are_read(self) -> None:
        text = "self-hosted-runner:\n  labels:\n    - a-1  # why\n    - 'b-2'\n"
        self.assertEqual(self.read(text), {"a-1", "b-2"})

    def test_a_list_under_another_key_is_not_a_label_list(self) -> None:
        text = "something-else:\n  labels:\n    - ubuntu-26.04\n"
        self.assertEqual(self.read(text), set())

    def test_a_list_after_the_runner_section_has_ended_is_not_read(self) -> None:
        text = (
            "self-hosted-runner:\n  labels:\n    - a-1\npaths:\n  labels:\n    - b-2\n"
        )
        self.assertEqual(self.read(text), {"a-1"})

    def test_comments_at_the_left_margin_do_not_end_the_section(self) -> None:
        text = "self-hosted-runner:\n# note\n  labels:\n    - a-1\n"
        self.assertEqual(self.read(text), {"a-1"})


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
