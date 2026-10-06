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
import tomllib
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


# Images that are pinned and that the "keeps the chart's tag" rule does NOT cover,
# each with the reason. Everything else that names an image must be matched by
# the rule, so a new chart image cannot slip past unnoticed.
OTEL = "ghcr.io/open-telemetry/opentelemetry-collector"
NOT_A_CHARTS_TAG = {
    # Not a chart's default: the kind node image, moved by a step (a minor by hand).
    "kindest/node": "the Kubernetes minor is a step's decision",
    # Not a chart's default: cluster.imageName, with a PostgreSQL and pgvector pair.
    "ghcr.io/cloudnative-pg/postgresql": "set by the cluster chart's imageName",
    # The pin is 0.162.0 and the chart's appVersion 0.161.0: outside the rule until
    # the two agree (the owner's call which way they go).
    f"{OTEL}-releases/opentelemetry-collector": "pin and chart disagree",
    # The log agent (S064) is a second release of the collector's chart with the
    # contrib build of the collector's release: it follows the core image's side.
    f"{OTEL}-releases/opentelemetry-collector-contrib": "follows the core image",
    # smoke.sh's load generator, the collector's release; no chart installs it.
    f"{OTEL}-contrib/telemetrygen": "no chart installs it",
    # The test database: no chart installs it.
    "pgvector/pgvector": "the tests' database, no chart",
    # The rate store (S066) and the tests' Redis are one image whose tag is the
    # repository's own choice, in Meridian's own chart: its own group moves it.
    "redis": "the repository's own choice, in its own Renovate group",
    # The documentation toolchain, run by the Makefile and CI: no chart.
    "structurizr/structurizr": "documentation tooling, no chart",
    "pandoc/extra": "documentation tooling, no chart",
    "minlag/mermaid-cli": "documentation tooling, no chart",
}
IMAGE_NAME = r"[a-z0-9][a-z0-9./-]*"
DIGEST_LINE = rf"^[A-Z][A-Z0-9_]*=({IMAGE_NAME}):[\w.-]+@sha256:[a-f0-9]{{64}}"
TAG_UNDER_COMMENT = (
    rf"^# renovate: datasource=docker depName=({IMAGE_NAME})\n"
    r"[A-Z][A-Z0-9_]*_IMAGE_TAG="
)
MAKEFILE_IMAGE = rf"^[A-Z][A-Z0-9_]*_IMAGE\s*[:?]?=\s*({IMAGE_NAME}):"


def pinned_images(pins: str, makefile: str) -> set[str]:
    """The name of every image the pins file and the Makefile pin."""
    names = set(re.findall(DIGEST_LINE, pins, re.MULTILINE))
    names |= set(re.findall(TAG_UNDER_COMMENT, pins, re.MULTILINE))
    names |= set(re.findall(MAKEFILE_IMAGE, makefile, re.MULTILINE))
    return names


def chart_image_rule(config: dict) -> dict:
    """The package rule that leaves an image's tag to its chart's pull request."""
    (rule,) = [
        rule
        for rule in config["packageRules"]
        if "docker.io/envoyproxy/gateway" in rule.get("matchPackageNames", [])
    ]
    return rule


def unguarded_images(pins: str, makefile: str, config: dict) -> list[str]:
    """Pinned images that the rule does not match and no exception explains."""
    patterns = chart_image_rule(config)["matchPackageNames"]
    return sorted(
        name
        for name in pinned_images(pins, makefile)
        if name not in NOT_A_CHARTS_TAG
        and not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    )


def python_package_names(pyproject: str) -> list[str]:
    """The name of every Python package pyproject.toml's tables depend on."""
    data = tomllib.loads(pyproject)
    specs = list(data["project"].get("dependencies", []))
    for extra in data["project"].get("optional-dependencies", {}).values():
        specs.extend(extra)
    for group in data.get("dependency-groups", {}).values():
        specs.extend(spec for spec in group if isinstance(spec, str))
    names = (re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec) for spec in specs)
    return sorted({match.group().lower() for match in names if match})


def agent_framework_rule(config: dict) -> dict:
    """The package rule that gives the second agent framework a pull request."""
    (rule,) = [
        rule
        for rule in config["packageRules"]
        if "agent-framework-core" in matched_names(rule, ["agent-framework-core"])
    ]
    return rule


def matched_names(rule: dict, names: list[str]) -> list[str]:
    """The names a rule's matchPackageNames patterns select (globs, as Renovate)."""
    patterns = rule.get("matchPackageNames", [])
    return [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns)]


def unwatched_framework_pins(pyproject: str, config: dict) -> list[str]:
    """Pinned agent-framework packages the framework's rule does not match."""
    framework = [
        name
        for name in python_package_names(pyproject)
        if name.startswith("agent-framework")
    ]
    try:
        rule = agent_framework_rule(config)
    except ValueError:  # no rule, or more than one
        return framework
    return sorted(set(framework) - set(matched_names(rule, framework)))


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
        (group,) = [
            r
            for r in rules
            if name in r.get("matchPackageNames", []) and "groupName" in r
        ]
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

    def test_every_pinned_image_keeps_its_charts_tag_or_is_a_named_exception(
        self,
    ) -> None:
        pins = (ROOT / PINS).read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertEqual(unguarded_images(pins, makefile, self.config), [])
        # The test sees the pins it is meant to guard.
        self.assertIn("quay.io/prometheus/prometheus", pinned_images(pins, makefile))
        self.assertIn("docker.io/grafana/loki", pinned_images(pins, makefile))

    def test_a_chart_image_that_no_rule_matches_is_reported(self) -> None:
        digest = "sha256:" + "a" * 64
        pins = (
            "# renovate: datasource=docker depName=example.org/chart/new\n"
            "NEW_IMAGE_TAG=1.0.0\n"
            f"NEW_IMAGE_DIGEST={digest}\n"
        )
        makefile = f"OTHER_IMAGE := example.org/thing/other:1.2.3@{digest}\n"

        unguarded = unguarded_images(pins, makefile, self.config)

        expected = ["example.org/chart/new", "example.org/thing/other"]
        self.assertEqual(unguarded, expected)

    def test_the_rule_for_a_chart_s_images_disables_the_tag_and_keeps_the_digest(
        self,
    ) -> None:
        rule = chart_image_rule(self.config)

        self.assertEqual(rule["matchDatasources"], ["docker"])
        self.assertEqual(rule["matchUpdateTypes"], ["major", "minor", "patch"])
        self.assertIs(rule["enabled"], False)
        self.assertNotIn("digest", rule["matchUpdateTypes"])
        self.assertIn("collector", rule["description"])

    def test_the_rule_matches_no_chart_and_no_exception(self) -> None:
        pins = (ROOT / PINS).read_text(encoding="utf-8")
        patterns = chart_image_rule(self.config)["matchPackageNames"]
        charts = re.findall(r"^# renovate: datasource=helm depName=(\S+)", pins, re.M)

        self.assertTrue(charts)
        for name in [*charts, *NOT_A_CHARTS_TAG]:
            with self.subTest(name=name):
                self.assertFalse(any(fnmatch.fnmatchcase(name, p) for p in patterns))

    def test_the_terraform_lock_refresh_is_off_and_the_others_are_not(self) -> None:
        terraform = self.config["terraform"]

        self.assertEqual(terraform["lockFileMaintenance"], {"enabled": False})
        self.assertIn("provider", terraform["description"])
        self.assertEqual(set(terraform), {"description", "lockFileMaintenance"})
        self.assertEqual(
            self.config["lockFileMaintenance"],
            {"enabled": True, "schedule": ["* * 1 * *"]},
        )

    def test_the_envoy_gateway_chart_waits_a_week_like_the_other_charts(self) -> None:
        pins = (ROOT / PINS).read_text(encoding="utf-8")
        rules = self.config["packageRules"]
        week = rules[0]
        (chart,) = [
            r
            for r in rules
            if "envoyproxy/gateway-helm" in r.get("matchPackageNames", [])
        ]

        # The chart is read with the docker datasource, which the week's rule lacks.
        self.assertIn(
            "# renovate: datasource=docker depName=envoyproxy/gateway-helm\n", pins
        )
        self.assertNotIn("docker", week["matchDatasources"])
        self.assertEqual(chart["matchDatasources"], ["docker"])
        self.assertEqual(chart["minimumReleaseAge"], week["minimumReleaseAge"])
        self.assertEqual(chart["matchUpdateTypes"], ["major", "minor", "patch"])

    def test_the_notes_say_a_chart_s_pull_request_moves_its_image_tags_by_hand(
        self,
    ) -> None:
        rules = self.config["packageRules"]
        for group in ("kind platform", "cert-manager", "prometheus image"):
            (rule,) = [r for r in rules if r.get("groupName") == group]
            note = " ".join(rule["prBodyNotes"])
            with self.subTest(group=group):
                self.assertIn("by hand", note)
                self.assertNotIn("A tag Renovate proposes", note)

    def test_the_agent_framework_arrives_in_a_pull_request_of_its_own(self) -> None:
        rules = self.config["packageRules"]
        rule = agent_framework_rule(self.config)
        (python,) = [r for r in rules if r.get("groupName") == "python"]

        self.assertTrue(rule["groupName"])
        self.assertNotEqual(rule["groupName"], python["groupName"])
        self.assertEqual(
            [r["groupName"] for r in rules if r.get("groupName") == rule["groupName"]],
            [rule["groupName"]],
        )
        # A later rule wins, so the framework's group must come after the python one.
        self.assertGreater(rules.index(rule), rules.index(python))
        # Told from another ecosystem's package by its manager, as the python
        # group is (an image or an npm package of the same name is not its).
        self.assertEqual(rule["matchManagers"], python["matchManagers"])
        self.assertEqual(rule["matchManagers"], ["pep621"])
        self.assertTrue(rule["addLabels"])

    def test_the_agent_framework_rule_matches_the_pin_and_no_other_package(
        self,
    ) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        names = python_package_names(pyproject)
        rule = agent_framework_rule(self.config)

        self.assertIn("agent-framework-core", names)
        self.assertRegex(pyproject, r'"agent-framework-core==\d+\.\d+\.\d+"')
        # Every other Python package, among them the first framework and the
        # clients that look alike, stays in the python group.
        self.assertIn("langgraph", names)
        self.assertEqual(
            matched_names(rule, names),
            [n for n in names if n.startswith("agent-framework")],
        )
        # An adapter a later release pulls in is seen too; a look-alike is not.
        adapters = ["agent-framework-anthropic", "agent-framework-a2a"]
        self.assertEqual(matched_names(rule, adapters), adapters)
        for other in ("langgraph", "agent-frameworks", "my-agent-framework-core"):
            self.assertEqual(matched_names(rule, [other]), [])

    def test_a_pinned_framework_package_the_rule_does_not_match_is_reported(
        self,
    ) -> None:
        pyproject = (
            '[project]\nname = "x"\n'
            'dependencies = ["agent-framework-core==1.19.0", '
            '"agent-framework-newadapter[extra]==0.1.0", "httpx==0.28.1"]\n'
            '[dependency-groups]\ndev = ["agent-framework-devtools==0.2.0"]\n'
        )
        narrow = json.loads(json.dumps(self.config))
        agent_framework_rule(narrow)["matchPackageNames"] = ["agent-framework-core"]
        without = json.loads(json.dumps(self.config))
        without["packageRules"].remove(agent_framework_rule(without))

        self.assertEqual(unwatched_framework_pins(pyproject, self.config), [])
        self.assertEqual(
            unwatched_framework_pins(pyproject, narrow),
            ["agent-framework-devtools", "agent-framework-newadapter"],
        )
        self.assertEqual(
            unwatched_framework_pins(pyproject, without),
            [
                "agent-framework-core",
                "agent-framework-devtools",
                "agent-framework-newadapter",
            ],
        )

    def test_the_agent_framework_note_names_what_a_bump_can_break_and_its_tests(
        self,
    ) -> None:
        rule = agent_framework_rule(self.config)
        note = " ".join(rule["prBodyNotes"])
        tests = (
            "tests/meridian/workloads/claim_brief/test_brief_stored_shapes.py",
            "tests/meridian/test_agent_framework_dependency.py",
            "tests/meridian/test_import_contracts.py",
        )

        for words in ("T-97", "T-98", "no brief waits", "green checks alone", *tests):
            with self.subTest(words=words):
                self.assertIn(words, note)
        for path in tests:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())
        for row in ("T-97", "T-98"):
            threats = ROOT / "docs/architecture/security/threat-model.md"
            self.assertIn(f"| {row} |", threats.read_text(encoding="utf-8"))

    def test_the_workload_readme_says_how_to_see_that_no_brief_waits(self) -> None:
        readme = ROOT / "src/meridian/workloads/claim_brief/README.md"
        text = " ".join(readme.read_text(encoding="utf-8").split())

        self.assertIn("claims.briefs", text)
        self.assertIn("awaiting_decision", text)
        self.assertIn("agent-framework-core", text)

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
