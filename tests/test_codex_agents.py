"""Tests for scripts/codex_agents.py: the Codex twins of the Claude agents.

The Claude agent files are the source. A twin nobody regenerates goes stale
silently, so `--check` has to see three things: a twin that differs, a twin
that is missing, and a twin whose agent is gone.

Run: python3 -m unittest discover -s tests
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_agents.py"

AGENT = """---
name: {name}
description: Reviews "{name}" things, and nothing else.
tools: Read, Grep
model: sonnet
effort: high
---

You review {name} things.
"""


def load(repo: Path):
    spec = importlib.util.spec_from_file_location("codex_agents", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SRC = repo / ".claude" / "agents"
    module.DST = repo / ".codex" / "agents"
    return module


class CodexAgents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / ".claude" / "agents").mkdir(parents=True)
        self.gen = load(self.repo)

    def agent(self, name):
        path = self.repo / ".claude" / "agents" / f"{name}.md"
        path.write_text(AGENT.format(name=name), encoding="utf-8")
        return path

    def twin(self, name):
        return self.repo / ".codex" / "agents" / f"{name}.toml"

    def test_generate_writes_one_twin_per_agent(self):
        self.agent("alpha")
        self.agent("beta")
        self.assertEqual(self.gen.main([]), 0)
        text = self.twin("alpha").read_text(encoding="utf-8")
        self.assertIn('name = "alpha"', text)
        self.assertIn('model_reasoning_effort = "high"', text)
        self.assertIn("You review alpha things.", text)
        self.assertIn("Prompt Defense Baseline", text)
        self.assertTrue(self.twin("beta").exists())

    def test_a_quote_in_the_description_is_escaped(self):
        self.agent("alpha")
        self.gen.main([])
        self.assertIn(
            'description = "Reviews \\"alpha\\" things, and nothing else."',
            self.twin("alpha").read_text(encoding="utf-8"),
        )

    def test_check_passes_on_fresh_twins(self):
        self.agent("alpha")
        self.gen.main([])
        self.assertEqual(self.gen.main(["--check"]), 0)

    def test_check_is_skipped_when_the_repository_has_no_twins(self):
        self.agent("alpha")
        self.assertEqual(self.gen.main(["--check"]), 0)
        self.assertFalse((self.repo / ".codex").exists())

    def test_check_fails_on_a_stale_twin(self):
        path = self.agent("alpha")
        self.gen.main([])
        path.write_text(
            AGENT.format(name="alpha") + "\nOne more rule.\n", encoding="utf-8"
        )
        self.assertEqual(self.gen.main(["--check"]), 1)

    def test_check_fails_on_a_missing_twin(self):
        self.agent("alpha")
        self.gen.main([])
        self.agent("beta")
        self.assertEqual(self.gen.main(["--check"]), 1)

    def test_check_fails_on_a_twin_whose_agent_is_gone(self):
        self.agent("alpha")
        path = self.agent("beta")
        self.gen.main([])
        path.unlink()
        self.assertEqual(self.gen.main(["--check"]), 1)

    def test_check_writes_nothing(self):
        path = self.agent("alpha")
        self.gen.main([])
        before = self.twin("alpha").read_text(encoding="utf-8")
        path.write_text(AGENT.format(name="alpha") + "\nMore.\n", encoding="utf-8")
        self.gen.main(["--check"])
        self.assertEqual(self.twin("alpha").read_text(encoding="utf-8"), before)

    def test_generate_removes_a_twin_whose_agent_is_gone(self):
        self.agent("alpha")
        path = self.agent("beta")
        self.gen.main([])
        path.unlink()
        self.gen.main([])
        self.assertFalse(self.twin("beta").exists())
        self.assertTrue(self.twin("alpha").exists())

    def test_an_agent_without_frontmatter_is_an_error(self):
        (self.repo / ".claude" / "agents" / "broken.md").write_text(
            "no frontmatter\n", encoding="utf-8"
        )
        with self.assertRaises(SystemExit):
            self.gen.main([])


class ShippedTwins(unittest.TestCase):
    def test_the_committed_twins_are_current(self):
        spec = importlib.util.spec_from_file_location("codex_agents", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
