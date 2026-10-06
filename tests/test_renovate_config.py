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

import fnmatch
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

    def test_the_cert_manager_chart_is_read_as_a_helm_chart_of_its_group(self) -> None:
        text = (ROOT / PINS).read_text(encoding="utf-8")
        found = {}
        for match in self.comment_reader(PINS).finditer(text):
            found[match.group("depName")] = match.group(
                "datasource", "registryUrl", "currentValue"
            )
        datasource, registry, version = found["cert-manager"]
        self.assertEqual(datasource, "helm")
        self.assertRegex(version, r"^v\d+\.\d+\.\d+$")
        self.assertEqual(
            registry, re.search(r"^CERT_MANAGER_REPO=(\S+)$", text, re.M).group(1)
        )
        (group,) = [
            rule
            for rule in self.config["packageRules"]
            if rule.get("groupName") == "kind platform"
        ]
        self.assertEqual(group["matchFileNames"], [PINS])
        self.assertIn("helm", self.config["packageRules"][0]["matchDatasources"])

    def test_the_approver_policy_chart_is_read_from_the_cert_manager_repository(
        self,
    ) -> None:
        text = (ROOT / PINS).read_text(encoding="utf-8")
        found = {}
        for match in self.comment_reader(PINS).finditer(text):
            found[match.group("depName")] = match.group(
                "datasource", "registryUrl", "currentValue"
            )
        datasource, registry, version = found["cert-manager-approver-policy"]
        self.assertEqual(datasource, "helm")
        self.assertRegex(version, r"^v\d+\.\d+\.\d+$")
        self.assertEqual(
            registry, re.search(r"^CERT_MANAGER_REPO=(\S+)$", text, re.M).group(1)
        )
        self.assertEqual(
            version, re.search(r"^APPROVER_POLICY_VERSION=(\S+)$", text, re.M).group(1)
        )

    def test_cert_manager_and_approver_policy_arrive_in_one_group(self) -> None:
        # approver-policy v0.28.0 is built against one cert-manager release, so
        # the two charts and cert-manager's images move together (S063).
        text = (ROOT / PINS).read_text(encoding="utf-8")
        names = [m.group("depName") for m in self.comment_reader(PINS).finditer(text)]
        rules = self.config["packageRules"]
        (group,) = [r for r in rules if r.get("groupName") == "cert-manager"]
        (platform,) = [r for r in rules if r.get("groupName") == "kind platform"]

        def in_group(name: str) -> bool:
            return any(fnmatch.fnmatchcase(name, p) for p in group["matchPackageNames"])

        family = [
            name
            for name in names
            if name in ("cert-manager", "cert-manager-approver-policy")
            or name.startswith("quay.io/jetstack/")
        ]
        self.assertEqual(len(family), 7)
        self.assertEqual([name for name in names if in_group(name)], family)
        self.assertFalse(in_group("quay.io/prometheus/prometheus"))
        # A later rule wins, so the group must come after the platform's.
        self.assertGreater(rules.index(group), rules.index(platform))
        note = " ".join(group["prBodyNotes"])
        for words in ("approver-policy", "cert-manager", "`make up`", "`make smoke`"):
            self.assertIn(words, note)

    def test_promtool_and_the_prometheus_pin_arrive_in_one_group(self) -> None:
        # The Makefile's PROMTOOL_IMAGE follows the Prometheus image the chart
        # installs (pins.env): one group, so they move in one pull request.
        name = "quay.io/prometheus/prometheus"
        pins = (ROOT / PINS).read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        in_pins = [m.group("depName") for m in self.comment_reader(PINS).finditer(pins)]
        readers = readers_for("Makefile", self.config)
        (image_reader,) = [r for r in readers if "currentDigest" in r.pattern]
        in_makefile = [m.group("depName") for m in image_reader.finditer(makefile)]
        rules = self.config["packageRules"]
        (group,) = [r for r in rules if name in r.get("matchPackageNames", [])]
        (platform,) = [r for r in rules if r.get("groupName") == "kind platform"]

        self.assertIn(name, in_pins)
        self.assertIn(name, in_makefile)
        self.assertTrue(group["groupName"])
        self.assertNotEqual(group["groupName"], platform["groupName"])
        # No file restriction: the rule reaches the pins file and the Makefile.
        self.assertNotIn("matchFileNames", group)
        self.assertNotIn("matchManagers", group)
        # A later rule wins, so the group must come after the platform's.
        self.assertGreater(rules.index(group), rules.index(platform))

    def test_the_rate_store_and_the_test_redis_arrive_in_one_group(self) -> None:
        # The rate store's image in the pins file (RATE_STORE_IMAGE, S066) is the
        # Makefile's PYTEST_REDIS_IMAGE: one group, so the tests and the cluster
        # never run different Redis releases.
        name = "redis"
        pins = (ROOT / PINS).read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        image_reader = self.image_reader(PINS)
        in_pins = {m.group("depName"): m for m in image_reader.finditer(pins)}
        in_makefile = {
            m.group("depName"): m
            for m in self.image_reader("Makefile").finditer(makefile)
        }
        rules = self.config["packageRules"]
        (group,) = [r for r in rules if name in r.get("matchPackageNames", [])]
        (platform,) = [r for r in rules if r.get("groupName") == "kind platform"]

        # Each file's reader takes the pin, tag and digest.
        self.assertIn(name, in_pins)
        self.assertIn(name, in_makefile)
        pinned = in_pins[name].group("currentValue", "currentDigest")
        tested = in_makefile[name].group("currentValue", "currentDigest")
        self.assertEqual(pinned, tested)
        self.assertTrue(group["groupName"])
        self.assertNotEqual(group["groupName"], platform["groupName"])
        # No file restriction: the rule reaches the pins file and the Makefile.
        self.assertNotIn("matchFileNames", group)
        self.assertNotIn("matchManagers", group)
        # A later rule wins, so the group must come after the platform's.
        self.assertGreater(rules.index(group), rules.index(platform))
        note = " ".join(group["prBodyNotes"])
        for words in ("RATE_STORE_IMAGE", "PYTEST_REDIS_IMAGE", "`make smoke`"):
            self.assertIn(words, note)

    def test_the_redis_group_takes_the_image_only_and_not_the_python_client(
        self,
    ) -> None:
        # pyproject.toml pins the client `redis` (pypi), and the pep621 manager
        # reads it under the same package name as the image's. A later rule wins,
        # so a name-only rule would carry a client bump into the image's pull
        # request, with notes about the image. The datasource tells them apart.
        rules = self.config["packageRules"]
        (group,) = [r for r in rules if "redis" in r.get("matchPackageNames", [])]
        (python,) = [r for r in rules if r.get("groupName") == "python"]
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertEqual(group["matchDatasources"], ["docker"])
        self.assertRegex(pyproject, r'"redis==\d+\.\d+\.\d+"')
        # The client stays in the Python group: that rule is the manager's.
        self.assertEqual(python["matchManagers"], ["pep621"])
        self.assertNotIn("matchPackageNames", python)
        self.assertGreater(rules.index(group), rules.index(python))

    def image_reader(self, path: str) -> re.Pattern[str]:
        """The reader of ``path`` for an image pinned as name:tag@digest."""
        (reader,) = [
            r
            for r in readers_for(path, self.config)
            if r.pattern.startswith("=") and "currentDigest" in r.pattern
        ]
        return reader

    def test_the_platform_note_names_the_collector_as_the_tag_that_is_not_the_charts(
        self,
    ) -> None:
        (platform,) = [
            r
            for r in self.config["packageRules"]
            if r.get("groupName") == "kind platform"
        ]
        note = " ".join(platform["prBodyNotes"])

        self.assertIn("collector", note)
        self.assertIn("appVersion", note)
        self.assertNotIn("are the tags that chart version installs by default.", note)

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

    def test_every_action_is_pinned_to_a_commit(self) -> None:
        # A tag can be moved to other code; a commit hash cannot (T-36).
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for action in re.findall(r"^\s*(?:- )?uses:\s*(\S+)", text, re.MULTILINE):
                with self.subTest(workflow=path.name, action=action):
                    self.assertRegex(action, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
