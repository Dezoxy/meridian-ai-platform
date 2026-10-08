"""render-mermaid.sh: which user and which network the render container gets.

The script starts one container per diagram. These tests put a stub `docker`
first on the PATH, which answers `docker info` with the security options a
test gives it and records the arguments of every `docker run`, so nothing is
pulled and no daemon is needed.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "render-mermaid.sh"

STUB = """#!/usr/bin/env bash
if [ "$1" = "info" ]; then
  [ -n "${STUB_INFO_FAILS:-}" ] && exit 1
  printf '%s\\n' "${STUB_SECURITY}"
  exit 0
fi
printf '%s\\n' "$*" >> "${STUB_LOG}"
exit "${STUB_RUN_STATUS:-0}"
"""

ROOTFUL = "[name=seccomp,profile=builtin name=cgroupns]"
ROOTLESS = "[name=seccomp,profile=builtin name=rootless name=cgroupns]"


class RenderMermaid(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        stub = self.bin / "docker"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.diagrams = root / "diagrams"
        self.diagrams.mkdir()
        self.log = root / "docker.log"

    def run_script(
        self, security: str, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "MERMAID_IMAGE": "example/mermaid-cli:1.0.0",
            "STUB_SECURITY": security,
            "STUB_LOG": str(self.log),
            **extra,
        }
        return subprocess.run(
            [str(SCRIPT), str(self.diagrams)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def runs(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_the_container_runs_as_the_caller_when_docker_is_not_rootless(self) -> None:
        (self.diagrams / "a.mmd").write_text("flowchart LR\n  a --> b\n")

        result = self.run_script(ROOTFUL)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.runs()), 1)
        self.assertIn(f" -u {os.getuid()}:{os.getgid()} ", f" {self.runs()[0]} ")

    def test_the_container_runs_as_its_root_when_docker_is_rootless(self) -> None:
        # Under rootless Docker the container's root is the caller, and the
        # caller's own IDs name a user that cannot write the mounted folder.
        (self.diagrams / "a.mmd").write_text("flowchart LR\n  a --> b\n")

        result = self.run_script(ROOTLESS)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" -u 0:0 ", f" {self.runs()[0]} ")

    def test_the_container_runs_as_the_caller_when_docker_info_fails(self) -> None:
        (self.diagrams / "a.mmd").write_text("flowchart LR\n  a --> b\n")

        result = self.run_script("", STUB_INFO_FAILS="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f" -u {os.getuid()}:{os.getgid()} ", f" {self.runs()[0]} ")

    def test_the_container_has_no_network(self) -> None:
        (self.diagrams / "a.mmd").write_text("flowchart LR\n  a --> b\n")

        for security in (ROOTFUL, ROOTLESS):
            with self.subTest(security=security):
                self.log.unlink(missing_ok=True)
                self.run_script(security)
                self.assertIn(" --network none ", f" {self.runs()[0]} ")

    def test_a_failed_render_fails_the_script_and_names_the_file(self) -> None:
        (self.diagrams / "a.mmd").write_text("flowchart LR\n  a --> b\n")
        (self.diagrams / "b.mmd").write_text("flowchart LR\n  c --> d\n")

        result = self.run_script(ROOTFUL, STUB_RUN_STATUS="1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.runs()), 2, "every file is tried")
        self.assertIn("a.mmd", result.stderr)
        self.assertIn("2 of 2 diagram(s) failed", result.stderr)

    def test_a_folder_without_diagrams_is_not_an_error_and_starts_nothing(self) -> None:
        result = self.run_script(ROOTFUL)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.runs(), [])


if __name__ == "__main__":
    unittest.main()
