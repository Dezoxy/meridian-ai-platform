"""Every version pinned outside a format Renovate knows has a reader.

Renovate reads workflows, lock files, Dockerfiles and Terraform by itself.
The pins below sit in KEY=value files, Makefile variables, workflow
environment values and JSON arguments, so
.github/renovate.json carries a regular expression for each shape. A pin no
expression matches is not an error anywhere: it just stops being watched.
These tests are that error, for the shapes PIN_LINES names. A pin of
another shape (a new file, a variable that does not end in _IMAGE or
_VERSION) needs a line there as well as a reader.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".github" / "renovate.json"
WORKFLOW = ".github/workflows/docs.yml"
PINS = "infra/kind/pins.env"

# Per glob, the lines that pin a version.
NO_VERSION = r"CLUSTER_NAME=|[A-Z0-9_]*_(?:CHART|REPO|REPOSITORY|DIGEST)="
NPX = r"^.*\"(?:@[a-z0-9-]+/)?[a-z][a-z0-9-]*@\d"
PIN_LINES = {
    # Every line of the pins file, except the ones that hold no version.
    PINS: rf"^(?!{NO_VERSION})[A-Z][A-Z0-9_]*=",
    "Makefile": r"^[A-Z][A-Z0-9_]*_IMAGE\s*[:?]?=",
    ".github/workflows/*.yml": r"^\s*[A-Z][A-Z0-9_]*_VERSION:",
    ".mcp.json": NPX,
    ".codex/config.toml.example": NPX,
}


def python_pattern(javascript: str) -> re.Pattern[str]:
    """Renovate names a group (?<name>...); Python wants (?P<name>...)."""
    return re.compile(re.sub(r"\(\?<(?=[A-Za-z])", "(?P<", javascript))


def readers_for(path: str, config: dict) -> list[re.Pattern[str]]:
    """The expressions of every custom manager that reads the path."""
    found = []
    for manager in config["customManagers"]:
        patterns = [pattern.strip("/") for pattern in manager["managerFilePatterns"]]
        if any(re.search(pattern, path) for pattern in patterns):
            found.extend(python_pattern(text) for text in manager["matchStrings"])
    return found


def unread_pins(path: str, pins: str, text: str, config: dict) -> list[str]:
    """The lines of a file that match `pins` and that no reader's match touches."""
    readers = readers_for(path, config)
    read = [match.span() for reader in readers for match in reader.finditer(text)]
    unread = []
    for line in re.finditer(pins, text, re.MULTILINE):
        end = text.find("\n", line.start())
        end = len(text) if end == -1 else end
        if not any(start < end and line.start() < stop for start, stop in read):
            unread.append(text[line.start() : end].strip())
    return unread


def pinned_files() -> list[tuple[str, str]]:
    """Each existing file a glob of PIN_LINES names, with its pin pattern."""
    found = []
    for glob, pins in PIN_LINES.items():
        for path in sorted(ROOT.glob(glob)):
            found.append((path.relative_to(ROOT).as_posix(), pins))
    return found


@unittest.skipUnless(CONFIG.is_file(), "no .github/renovate.json")
class Readers(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def unread(self, glob: str, text: str, path: str | None = None) -> list[str]:
        return unread_pins(path or glob, PIN_LINES[glob], text, self.config)

    def comment_reader(self, path: str) -> re.Pattern[str]:
        """The one reader of a file that needs a comment above the line."""
        readers = readers_for(path, self.config)
        (reader,) = [r for r in readers if "renovate:" in r.pattern]
        return reader

    def test_every_pin_has_a_reader(self) -> None:
        for path, pins in pinned_files():
            with self.subTest(path=path):
                text = (ROOT / path).read_text(encoding="utf-8")
                self.assertEqual(unread_pins(path, pins, text, self.config), [])

    def test_a_workflow_version_without_its_comment_is_reported(self) -> None:
        text = '        env:\n          TOOL_VERSION: "1.2.3"\n'
        unread = self.unread(".github/workflows/*.yml", text, WORKFLOW)
        self.assertEqual(unread, ['TOOL_VERSION: "1.2.3"'])

    def test_an_image_without_a_digest_is_reported(self) -> None:
        text = "NEW_IMAGE ?= example.org/thing:1.2.3\n"
        self.assertEqual(self.unread("Makefile", text), [text.strip()])

    def test_an_image_with_a_digest_is_read(self) -> None:
        text = f"NEW_IMAGE := example.org/thing:1.2.3@sha256:{'a' * 64}\n"
        self.assertEqual(self.unread("Makefile", text), [])

    def test_a_scoped_package_is_read(self) -> None:
        text = '  "args": ["-y", "@scope/server@1.2.3"]\n'
        self.assertEqual(self.unread(".mcp.json", text), [])

    def test_a_comment_names_where_the_version_comes_from(self) -> None:
        text = (
            "          # renovate: datasource=github-releases depName=owner/tool"
            " extractVersion=^v(?<version>.+)$\n"
            '          TOOL_VERSION: "1.2.3"\n'
        )
        match = self.comment_reader(WORKFLOW).search(text)
        self.assertIsNotNone(match)
        found = match.group("datasource", "depName", "extractVersion", "currentValue")
        expected = ("github-releases", "owner/tool", "^v(?<version>.+)$", "1.2.3")
        self.assertEqual(found, expected)

    def test_a_pins_line_without_its_comment_is_reported(self) -> None:
        for line in ("TEMPO_VERSION=3.1.0", "GATEWAY_API_TAG=v1.2.3", "TOOL_REF=abc"):
            with self.subTest(line=line):
                text = f"TEMPO_CHART=tempo\n{line}\n"
                self.assertEqual(self.unread(PINS, text), [line])

    def test_a_pins_line_that_is_not_a_version_needs_no_reader(self) -> None:
        text = "CLUSTER_NAME=x\nA_CHART=y\nA_REPO=https://z\nAN_IMAGE_REPOSITORY=r\n"
        self.assertEqual(self.unread(PINS, text), [])

    def test_a_helm_comment_carries_the_chart_s_repository(self) -> None:
        text = (
            "# renovate: datasource=helm depName=tempo"
            " registryUrl=https://charts.example.org\n"
            "TEMPO_VERSION=3.1.0\n"
        )
        match = self.comment_reader(PINS).search(text)
        found = match.group("datasource", "depName", "registryUrl", "currentValue")
        expected = ("helm", "tempo", "https://charts.example.org", "3.1.0")
        self.assertEqual(found, expected)

    def test_a_split_image_carries_its_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        text = (
            "# renovate: datasource=docker depName=example.org/collector\n"
            f"COLLECTOR_IMAGE_TAG=0.1.0\nCOLLECTOR_IMAGE_DIGEST={digest}\n"
        )
        match = self.comment_reader(PINS).search(text)
        found = match.group("currentValue", "currentDigest")
        self.assertEqual(found, ("0.1.0", digest))

    def test_the_expressions_stay_within_what_renovate_s_engine_reads(self) -> None:
        # RE2 has no lookaround and no backreference; Python would accept both.
        for manager in self.config["customManagers"]:
            for text in manager["matchStrings"]:
                with self.subTest(expression=text[:40]):
                    self.assertIsNone(re.search(r"\(\?<?[=!]|\\[1-9]|\\k<", text))

    def test_nothing_is_merged_by_renovate(self) -> None:
        self.assertNotIn("automerge", CONFIG.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
