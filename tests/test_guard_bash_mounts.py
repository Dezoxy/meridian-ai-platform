"""The command guard knows every path the chart mounts a Secret at.

The guard (.claude/hooks/guard-bash.sh) denies a command run in a pod that
names a mounted Secret path unless the command is ls, stat or test: it reads the
path, not the reader. The paths are a list in the hook (mount_path), and the
chart is where a new one appears. This reads the chart's templates, finds every
volume that is a Secret and the path each is mounted at, and runs the hook on
a read of each: a Secret mounted somewhere the hook does not name fails here,
so the next mount cannot be forgotten. A Secret volume or a mount of a shape
this cannot read fails too, rather than being skipped.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "infra" / "helm" / "meridian" / "templates"
HOOK = ROOT / ".claude" / "hooks" / "guard-bash.sh"

# The mounts the chart has today: a parser that finds fewer has broken.
KNOWN = {"/etc/meridian/tls", "/etc/meridian/db-ca", "/etc/redis-acl"}

DEFINE = re.compile(
    r'\{\{-?\s*define\s+"([^"]+)"\s*-?\}\}\s*(.*?)\s*\{\{-?\s*end\s*-?\}\}', re.DOTALL
)
INCLUDE = re.compile(r'^\{\{-?\s*include\s+"([^"]+)"[^}]*\}\}$')
SECRET_VOLUME = re.compile(r"-\s+name:\s*(\S+)[ \t]*\n[ \t]+secret:[ \t]*\n")
SECRET_KEY = re.compile(r"^[ \t]*secret:[ \t]*$", re.MULTILINE)
MOUNT = re.compile(r"-\s+name:\s*(\S+)[ \t]*\n[ \t]+mountPath:[ \t]*(.+?)[ \t]*\n")
PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")


class UnreadableChart(Exception):
    """A Secret volume or mount of a shape this test does not read."""


def chart_text():
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TEMPLATES.iterdir())
    )


def literal_defines(text):
    """The templates that are one literal path (tlsDirectory and the like)."""
    return {
        name: body.strip()
        for name, body in DEFINE.findall(text)
        if PATH.match(body.strip())
    }


def secret_mount_paths(text):
    """Every path a Secret volume is mounted at, named by the chart."""
    defines = literal_defines(text)
    volumes = set(SECRET_VOLUME.findall(text))
    if len(SECRET_KEY.findall(text)) != len(SECRET_VOLUME.findall(text)):
        raise UnreadableChart(
            "a `secret:` in the templates that is not a volume this test reads "
            "(`- name: X` and then `secret:`): teach it the new shape"
        )
    paths = set()
    for name, raw in MOUNT.findall(text):
        if name not in volumes:
            continue
        include = INCLUDE.match(raw)
        path = defines.get(include.group(1)) if include else raw
        if not (path and PATH.match(path)):
            raise UnreadableChart(
                f"the mount of the Secret volume {name} is at {raw}, which this "
                "test cannot resolve to a path: teach it the new shape"
            )
        paths.add(path)
    return paths


def decision(command):
    request = json.dumps({"tool_input": {"command": command}})
    done = subprocess.run(
        ["bash", str(HOOK)], input=request, capture_output=True, text=True, check=True
    )
    if not done.stdout.strip():
        return "none"
    return json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"]


class ChartMountsTest(unittest.TestCase):
    def test_the_chart_mounts_the_secrets_this_test_knows(self):
        self.assertEqual(KNOWN, secret_mount_paths(chart_text()))

    def test_a_volume_that_is_not_a_secret_is_not_a_secret_mount(self):
        text = (
            "- name: cfg\n  configMap:\n    name: x\n"
            "- name: cfg\n  mountPath: /etc/cfg\n"
        )
        self.assertEqual(set(), secret_mount_paths(text))

    def test_a_secret_mount_the_parser_cannot_resolve_fails(self):
        text = (
            "- name: s\n  secret:\n    secretName: x\n"
            "- name: s\n  mountPath: {{ .Values.where }}\n"
        )
        with self.assertRaises(UnreadableChart):
            secret_mount_paths(text)

    def test_a_secret_volume_of_another_shape_fails(self):
        text = "volumes:\n  secret:\n    secretName: x\n"
        with self.assertRaises(UnreadableChart):
            secret_mount_paths(text)


@unittest.skipUnless(shutil.which("jq"), "the hook needs jq")
class HookKnowsMountsTest(unittest.TestCase):
    def test_a_read_of_each_secret_mount_in_a_pod_is_denied(self):
        for path in sorted(secret_mount_paths(chart_text())):
            with self.subTest(path=path):
                self.assertEqual(
                    "deny", decision(f"kubectl exec pod -- awk 1 {path}/x")
                )

    def test_ls_stat_and_test_of_each_secret_mount_pass(self):
        for path in sorted(secret_mount_paths(chart_text())):
            for verb in ("ls", "stat", "test -r"):
                with self.subTest(path=path, verb=verb):
                    self.assertEqual(
                        "none", decision(f"kubectl exec pod -- {verb} {path}/x")
                    )

    def test_the_check_can_fail_a_path_the_hook_does_not_name(self):
        # The same command on a path no rule names is not denied: so the deny
        # above says something about the paths it was run on.
        self.assertEqual(
            "none", decision("kubectl exec pod -- awk 1 /opt/unnamed/dir/x")
        )


if __name__ == "__main__":
    unittest.main()
