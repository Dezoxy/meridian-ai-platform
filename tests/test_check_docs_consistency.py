"""Regression tests for check_docs_consistency.check_heading_visibility.

Structurizr's Documentation tab HIDES a level-1 (`#`) heading: it does not
appear in the page and does not appear in the navigation. Every consumer of
this base shipped overview documents titled with `#`, so every one of them
rendered with its section titles missing -- while the PDF, which normalises
heading levels, looked correct. These tests pin the rule that catches it.

check_mermaid, check_split_tables and the agent prose-width exemption are pinned
below too.

Run: python3 -m unittest discover -s tests
"""

import importlib.util
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_docs_consistency.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_docs_consistency", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HeadingVisibility(unittest.TestCase):
    """Each case builds a throwaway docs/architecture/ tree and runs one check."""

    WORKSPACE = "Payment Platform"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Resolved, exactly as the checker builds REPO. On macOS /var is a
        # symlink to /private/var, so an unresolved root would sit outside
        # every path the check resolves.
        self.repo = Path(self.tmp.name).resolve()
        self.arch = self.repo / "docs" / "architecture"
        self.overview = self.arch / "overview"
        self.overview.mkdir(parents=True)
        (self.arch / "workspace.dsl").write_text(
            f'workspace "{self.WORKSPACE}" "An example." {{\n}}\n', encoding="utf-8"
        )
        self.check = load_checker()
        self.check.REPO = self.repo
        self.check.ARCH = self.arch
        self.check.OVERVIEW = self.overview

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, body, where=None):
        path = (where or self.overview) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
        return path

    def failures(self):
        f = self.check.Failures()
        self.check.check_heading_visibility(f)
        return [detail for _, detail in f]

    # -- the bug ------------------------------------------------------------

    def test_h1_section_title_is_rejected(self):
        """The convention every consumer shipped: rendered with no title."""
        self.write(
            "02-scope.md",
            """
            # Scope

            ## In scope

            Everything.
            """,
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("02-scope.md", found[0])
        self.assertIn("hidden", found[0])

    def test_every_h1_in_a_document_is_reported(self):
        self.write(
            "02-scope.md",
            """
            ## Scope

            # A later level-1 heading, also hidden

            Text.
            """,
        )
        self.assertEqual(len(self.failures()), 1)

    # -- the fix -------------------------------------------------------------

    def test_h2_section_title_passes(self):
        self.write(
            "02-scope.md",
            """
            ## Scope

            ### In scope

            Everything.
            """,
        )
        self.assertEqual(self.failures(), [])

    # -- the one legitimate level-1 heading ----------------------------------

    def test_workspace_title_is_allowed_when_it_opens_the_document(self):
        """`# Hopin` then `## Overview`: the PDF builder uses it as the cover."""
        self.write(
            "01-overview.md",
            f"""
            # {self.WORKSPACE}

            ## Overview

            Text.
            """,
        )
        self.assertEqual(self.failures(), [])

    def test_workspace_title_still_needs_a_visible_section_after_it(self):
        self.write(
            "01-overview.md",
            f"""
            # {self.WORKSPACE}

            Text with no section heading at all.
            """,
        )
        found = self.failures()
        self.assertTrue(any("no visible section heading" in d for d in found), found)

    def test_workspace_title_is_not_allowed_outside_the_first_document(self):
        self.write("01-overview.md", "## Overview\n\nText.\n")
        self.write("02-scope.md", f"# {self.WORKSPACE}\n\n## Scope\n\nText.\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("02-scope.md", found[0])

    def test_a_title_merely_resembling_the_workspace_is_hidden(self):
        """`# Architecture Overview` in workspace "Payment Platform" is not it."""
        self.write("01-overview.md", "# Architecture Overview\n\n## Context\n\nText.\n")
        self.assertEqual(len(self.failures()), 1)

    # -- structure -----------------------------------------------------------

    def test_skipped_heading_level_is_rejected(self):
        self.write(
            "02-scope.md",
            """
            ## Scope

            #### Jumped straight to level four

            Text.
            """,
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("skips", found[0])

    def test_document_without_any_heading_is_rejected(self):
        self.write("02-scope.md", "Only prose, no heading.\n")
        found = self.failures()
        self.assertTrue(any("no visible section heading" in d for d in found), found)

    def test_headings_inside_fenced_code_are_ignored(self):
        """A shell comment or a sample document in a fence is not a heading."""
        self.write(
            "02-scope.md",
            """
            ## Scope

            ```bash
            # a shell comment, not a heading
            ```

            Text.
            """,
        )
        self.assertEqual(self.failures(), [])

    # -- how the document set is assembled -----------------------------------

    def test_symlinked_register_is_checked_at_its_real_path(self):
        """Registers reach overview/ by symlink; the fix belongs in the target."""
        target = self.write(
            "constraints.md",
            "# Constraints\n\nText.\n",
            where=self.arch / "requirements",
        )
        (self.overview / "10-constraints.md").symlink_to(
            Path("..") / "requirements" / "constraints.md"
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("requirements/constraints.md", found[0])
        self.assertTrue(target.exists())

    def test_non_markdown_files_are_ignored(self):
        (self.overview / "notes.txt").write_text("# not markdown\n", encoding="utf-8")
        self.assertEqual(self.failures(), [])

    def test_missing_overview_directory_is_skipped_not_failed(self):
        """A repository adopts checks as it grows; absence is not an error."""
        self.check.OVERVIEW = self.arch / "does-not-exist"
        self.assertEqual(self.failures(), [])


def load_checker_for(repo):
    """The checker with every path it derived from REPO at import moved to a tree.

    The heading tests above patch three constants, which is all they read.
    check_mermaid and check_line_width also read the decisions folder, the view
    file, the generated views and the skill and agent folders, and each of those
    was computed from the real repository when the module loaded.
    """
    check = load_checker()
    arch = repo / "docs" / "architecture"
    check.REPO = repo
    check.ARCH = arch
    check.OVERVIEW = arch / "overview"
    check.ADR_DIR = arch / "decisions"
    check.VIEWS_DSL = arch / "model" / "views.dsl"
    check.MERMAID_VIEWS = arch / "generated" / "mermaid-views"
    check.SKILL_DIRS = (repo / ".claude" / "skills", repo / ".agents" / "skills")
    check.AGENT_DIRS = (repo / ".claude" / "agents", repo / ".claude" / "commands")
    check.VENDORED = repo / ".claude" / "rules"
    return check


class TreeCase(unittest.TestCase):
    """A throwaway repository with the checker pointed at it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Resolved, as in the heading tests: /var is a symlink on macOS.
        self.repo = Path(self.tmp.name).resolve()
        self.arch = self.repo / "docs" / "architecture"
        for folder in (self.arch, self.repo / ".claude", self.repo / ".agents"):
            folder.mkdir(parents=True, exist_ok=True)
        self.check = load_checker_for(self.repo)

    def write(self, name, body):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
        return path

    def run_check(self, check):
        f = self.check.Failures()
        check(f)
        return [detail for _, detail in f]


class Mermaid(TreeCase):
    """check_mermaid: the six rules of the model-or-Mermaid split."""

    def setUp(self):
        super().setUp()
        self.write(
            "docs/architecture/model/views.dsl",
            """
            systemContext paymentPlatform "SystemContext" "What depends on it?" {
                include *
            }
            container paymentPlatform "Containers" "What are the blocks?" {
                include *
            }
            """,
        )

    def failures(self):
        return self.run_check(self.check.check_mermaid)

    def derived(self, key="SystemContext", body="flowchart LR\n  a --> b"):
        return f"<!-- mermaid-view: {key} -->\n```mermaid\n{body}\n```\n"

    # -- rule 1: the fence closes ---------------------------------------------

    def test_hand_written_diagram_passes(self):
        self.write(
            "docs/notes.md",
            """
            # Notes

            ```mermaid
            stateDiagram-v2
                [*] --> Pending
                Pending --> Completed
            ```
            """,
        )
        self.assertEqual(self.failures(), [])

    def test_unclosed_backtick_fence_is_reported(self):
        self.write("docs/notes.md", "```mermaid\nflowchart LR\n  a --> b\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:1", found[0])
        self.assertIn("never closed", found[0])

    def test_unclosed_tilde_fence_is_reported(self):
        self.write("docs/notes.md", "Text.\n\n~~~mermaid\nflowchart LR\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:3", found[0])

    def test_unclosed_fence_of_another_language_is_not_this_checks_business(self):
        self.write("docs/notes.md", "```bash\necho hi\n")
        self.assertEqual(self.failures(), [])

    def test_tilde_fence_is_not_closed_by_backticks(self):
        """The backtick line is content; only ~~~ ends a tilde fence."""
        self.write(
            "docs/notes.md",
            "~~~mermaid\nflowchart LR\n```\n  a --> b\n~~~\n",
        )
        self.assertEqual(self.failures(), [])

    def test_example_inside_a_longer_fence_is_not_a_diagram(self):
        """A skill showing the syntax must not fail on the examples it shows.

        Two examples, so a scanner that let the first inner fence end the outer
        one would read the second as a real diagram.
        """
        self.write(
            "docs/notes.md",
            """
            ````markdown
            ```mermaid
            flowchart LR
            ```

            <!-- mermaid-view: NoSuchView -->
            ```mermaid
            C4Context
            ```
            ````
            """,
        )
        self.assertEqual(self.failures(), [])

    # -- rule 2: a known diagram type -----------------------------------------

    def test_every_documented_type_passes(self):
        types = (
            "flowchart",
            "graph",
            "sequenceDiagram",
            "stateDiagram",
            "stateDiagram-v2",
            "erDiagram",
            "classDiagram",
            "gantt",
            "timeline",
            "journey",
            "mindmap",
            "pie",
            "quadrantChart",
            "requirementDiagram",
            "gitGraph",
            "sankey-beta",
            "xychart-beta",
            "block-beta",
            "packet-beta",
            "kanban",
        )
        text = "".join(f"```mermaid\n{t}\n```\n\n" for t in types)
        self.write("docs/notes.md", text)
        self.assertEqual(self.failures(), [])

    def test_unknown_type_is_reported(self):
        self.write("docs/notes.md", "```mermaid\nflowcart LR\n  a --> b\n```\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:1", found[0])
        self.assertIn("flowcart", found[0])

    def test_type_may_follow_blank_lines_comments_and_front_matter(self):
        self.write(
            "docs/notes.md",
            """
            ```mermaid

            %% a comment
            %%{init: {"theme": "neutral"}}%%
            ---
            title: Payment states
            ---
            stateDiagram-v2
                [*] --> Pending
            ```
            """,
        )
        self.assertEqual(self.failures(), [])

    def test_empty_fence_is_reported(self):
        self.write("docs/notes.md", "```mermaid\n\n%% only a comment\n```\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("empty", found[0])

    # -- rule 3: structure is the model's -------------------------------------

    def test_c4_and_architecture_diagrams_are_rejected(self):
        for kind in (
            "C4Context",
            "C4Container",
            "C4Component",
            "C4Dynamic",
            "C4Deployment",
            "architecture-beta",
        ):
            with self.subTest(kind=kind):
                self.write("docs/notes.md", f"```mermaid\n{kind}\n```\n")
                found = self.failures()
                self.assertEqual(len(found), 1, found)
                self.assertIn(kind, found[0])
                self.assertIn("structure belongs to the Structurizr model", found[0])
                self.assertIn("mermaid-view: Key", found[0])

    # -- rule 4: provenance ---------------------------------------------------

    def test_derived_block_of_a_defined_view_passes(self):
        self.write("docs/architecture/README.md", self.derived())
        self.assertEqual(self.failures(), [])

    def test_derived_block_of_an_undefined_view_is_reported(self):
        self.write("docs/architecture/README.md", self.derived("NoSuchView"))
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("NoSuchView", found[0])
        self.assertIn("views.dsl", found[0])

    def test_view_key_is_not_checked_without_views_dsl(self):
        self.check.VIEWS_DSL.unlink()
        self.write("docs/architecture/README.md", self.derived("NoSuchView"))
        self.assertEqual(self.failures(), [])

    def test_comment_must_sit_directly_above_the_fence(self):
        self.write(
            "docs/architecture/README.md",
            "<!-- mermaid-view: SystemContext -->\n\n"
            "```mermaid\nflowchart LR\n  a --> b\n```\n",
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/architecture/README.md:1", found[0])
        self.assertIn("directly above", found[0])

    def test_comment_above_another_fence_is_reported(self):
        self.write(
            "docs/architecture/README.md",
            "<!-- mermaid-view: SystemContext -->\n```text\nnot a diagram\n```\n",
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("directly above", found[0])

    def test_comment_above_nothing_is_reported(self):
        self.write("docs/architecture/README.md", "<!-- mermaid-view: Containers -->\n")
        self.assertEqual(len(self.failures()), 1)

    def test_misspelt_comment_is_reported_not_ignored(self):
        """Ignored, the block would be a hand-drawn view nobody keeps in step."""
        self.write(
            "docs/architecture/README.md",
            "<!--mermaid-view:SystemContext-->\n```mermaid\nflowchart LR\n```\n",
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("malformed", found[0])

    def test_comment_shown_as_an_example_is_not_a_comment(self):
        self.write(
            "docs/notes.md",
            "Write `<!-- mermaid-view: Key -->` above the fence.\n\n"
            "```markdown\n<!-- mermaid-view: Key -->\n```\n",
        )
        self.assertEqual(self.failures(), [])

    # -- rule 5: not in what the Documentation tab imports --------------------

    def test_derived_block_in_an_overview_document_is_rejected(self):
        self.write("docs/architecture/overview/02-scope.md", self.derived())
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("overview/02-scope.md", found[0])
        self.assertIn("embed:SystemContext", found[0])

    def test_derived_block_behind_an_overview_symlink_is_rejected(self):
        """The register is imported through its symlink; the fix is in the target."""
        self.write("docs/architecture/requirements/constraints.md", self.derived())
        overview = self.arch / "overview"
        overview.mkdir(parents=True, exist_ok=True)
        (overview / "10-constraints.md").symlink_to(
            Path("..") / "requirements" / "constraints.md"
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("requirements/constraints.md", found[0])
        self.assertIn("imports", found[0])

    def test_derived_block_in_a_decision_is_rejected(self):
        self.write("docs/architecture/decisions/0001-use-it.md", self.derived())
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("decisions/0001-use-it.md", found[0])

    def test_hand_written_diagram_in_an_imported_document_is_fine(self):
        """Structurizr shows it as code unless the plugin is on; that is allowed."""
        self.write(
            "docs/architecture/overview/02-scope.md",
            "## Scope\n\n```mermaid\nflowchart LR\n  a --> b\n```\n",
        )
        self.assertEqual(self.failures(), [])

    def test_derived_block_in_a_register_that_is_not_symlinked_passes(self):
        self.write("docs/architecture/requirements/constraints.md", self.derived())
        self.assertEqual(self.failures(), [])

    # -- rule 6: the body is the generated view -------------------------------

    def generated(self, key="SystemContext", text="flowchart LR\n  a --> b\n"):
        path = self.check.MERMAID_VIEWS / f"{key}.mmd"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_block_equal_to_the_generated_view_passes(self):
        self.generated()
        self.write("docs/architecture/README.md", self.derived())
        self.assertEqual(self.failures(), [])

    def test_one_trailing_newline_is_ignored(self):
        self.generated(text="flowchart LR\n  a --> b")
        self.write("docs/architecture/README.md", self.derived())
        self.assertEqual(self.failures(), [])

    def test_stale_block_is_reported(self):
        self.generated(text="flowchart LR\n  a --> b\n  b --> c\n")
        self.write("docs/architecture/README.md", self.derived())
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/architecture/README.md:2", found[0])
        self.assertIn("stale", found[0])
        self.assertIn("make mermaid-views", found[0])

    def test_hand_edited_block_is_reported_as_stale(self):
        self.generated()
        self.write(
            "docs/architecture/README.md",
            self.derived(body="flowchart LR\n  a --> b\n  a --> c"),
        )
        self.assertEqual(len(self.failures()), 1)

    def test_missing_generated_view_is_skipped_not_failed(self):
        self.write("docs/architecture/README.md", self.derived())
        self.assertFalse(self.check.MERMAID_VIEWS.exists())
        self.assertEqual(self.failures(), [])

    def test_hand_written_diagrams_are_never_compared(self):
        self.generated()
        self.write(
            "docs/architecture/README.md",
            "```mermaid\nflowchart LR\n  x --> y\n```\n",
        )
        self.assertEqual(self.failures(), [])


class AgentPromptWidth(TreeCase):
    """Agent definitions are prompts: copies keep their upstream line lengths."""

    LONG = "word " * 24 + "end\n"  # 123 columns of ordinary, breakable prose

    def failures(self):
        return self.run_check(self.check.check_line_width)

    def test_agent_definition_is_exempt_from_the_prose_width_rule(self):
        self.write(
            ".claude/agents/reviewer.md", f"---\nname: reviewer\n---\n{self.LONG}"
        )
        self.assertEqual(self.failures(), [])

    def test_command_definition_is_exempt_from_the_prose_width_rule(self):
        self.write(
            ".claude/commands/build-fix.md", f"---\ndescription: x\n---\n{self.LONG}"
        )
        self.assertEqual(self.failures(), [])

    def test_the_same_line_in_documentation_is_still_reported(self):
        self.write("docs/notes.md", self.LONG)
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:1", found[0])

    def test_skills_stay_exempt(self):
        self.write(".claude/skills/demo/SKILL.md", self.LONG)
        self.assertEqual(self.failures(), [])


class SplitTables(TreeCase):
    """check_split_tables: a blank line ends a table, and the rows after it vanish.

    Markdown renders a row that follows a blank line as prose, so every row
    below the blank line leaves the table while the source still looks like
    one. A row followed by a delimiter row is the header of a new table.
    """

    TABLE = "| a | b |\n|---|---|\n| 1 | 2 |\n"

    def failures(self):
        return self.run_check(self.check.check_split_tables)

    def test_a_blank_line_inside_a_table_is_reported_with_the_cut_row(self):
        self.write("docs/notes.md", f"{self.TABLE}\n| 3 | 4 |\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:5", found[0])

    def test_the_message_names_the_row_it_was_cut_from(self):
        self.write("docs/notes.md", f"{self.TABLE}\n| 3 | 4 |\n")
        self.assertIn("line 3", self.failures()[0])

    def test_two_tables_a_blank_line_apart_are_fine(self):
        self.write("docs/notes.md", f"{self.TABLE}\n{self.TABLE}")
        self.assertEqual(self.failures(), [])

    def test_a_delimiter_row_may_carry_alignment_colons(self):
        for delimiter in ("|:--|--:|", "| :---: | --- |", "|-|-|", "|---|"):
            with self.subTest(delimiter=delimiter):
                self.write(
                    "docs/notes.md",
                    f"{self.TABLE}\n| c | d |\n{delimiter}\n| 5 | 6 |\n",
                )
                self.assertEqual(self.failures(), [])

    def test_a_row_followed_by_something_that_is_not_a_delimiter_row_is_cut_off(self):
        for second in ("| --- | x |", "| text | --- |", "| |", "| 5 | 6 |", "prose"):
            with self.subTest(second=second):
                self.write("docs/notes.md", f"{self.TABLE}\n| c | d |\n{second}\n")
                found = self.failures()
                self.assertEqual(len(found), 1, found)
                self.assertIn("docs/notes.md:5", found[0])

    def test_a_table_inside_a_fence_is_not_checked(self):
        for fence in ("```", "~~~", "````"):
            with self.subTest(fence=fence):
                self.write(
                    "docs/notes.md", f"{fence}\n{self.TABLE}\n| 3 | 4 |\n{fence}\n"
                )
                self.assertEqual(self.failures(), [])

    def test_a_shorter_fence_inside_a_longer_one_does_not_end_it(self):
        self.write("docs/notes.md", f"````\n```\n{self.TABLE}\n| 3 | 4 |\n````\n")
        self.assertEqual(self.failures(), [])

    def test_the_fence_ends_and_the_check_resumes_after_it(self):
        self.write("docs/notes.md", f"```\ncode\n```\n\n{self.TABLE}\n| 3 | 4 |\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:9", found[0])

    def test_an_unclosed_fence_hides_a_cut_row_to_the_end_of_the_file(self):
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                self.write(
                    "docs/notes.md", f"{fence}\n{self.TABLE}\n| 3 | 4 |\n| 5 | 6 |\n"
                )
                self.assertEqual(self.failures(), [])

    def test_a_row_after_a_fence_that_follows_a_table_is_not_cut_off(self):
        # The line above the blank line is a fence marker, not a table row.
        self.write("docs/notes.md", f"{self.TABLE}```\ncode\n```\n\n| 3 | 4 |\n")
        self.assertEqual(self.failures(), [])

    def test_a_table_at_the_end_of_a_file_passes(self):
        for tail in ("", "\n", "\n\n"):
            with self.subTest(tail=repr(tail)):
                table = self.TABLE.rstrip("\n")
                self.write("docs/notes.md", f"text\n\n{table}{tail}")
                self.assertEqual(self.failures(), [])

    def test_a_cut_row_on_the_last_line_is_reported(self):
        self.write("docs/notes.md", f"{self.TABLE}\n| 3 | 4 |")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:5", found[0])

    def test_an_indented_table_in_a_list_item_is_checked(self):
        self.write(
            "docs/notes.md",
            """
            - an item with a table

              | a | b |
              |---|---|
              | 1 | 2 |

              | 3 | 4 |
            """,
        )
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:7", found[0])

    def test_an_indented_row_followed_by_an_indented_delimiter_row_is_a_new_table(self):
        self.write(
            "docs/notes.md",
            """
            - an item

              | a | b |
              |---|---|
              | 1 | 2 |

              | c | d |
              |---|---|
              | 3 | 4 |
            """,
        )
        self.assertEqual(self.failures(), [])

    def test_several_blank_lines_count_as_one_gap(self):
        self.write("docs/notes.md", f"{self.TABLE}\n\n\n| 3 | 4 |\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:7", found[0])

    def test_a_blank_line_of_spaces_is_still_a_blank_line(self):
        self.write("docs/notes.md", f"{self.TABLE}   \n\t\n| 3 | 4 |\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:6", found[0])

    def test_the_rows_after_the_gap_are_one_finding_not_one_each(self):
        self.write("docs/notes.md", f"{self.TABLE}\n| 3 | 4 |\n| 5 | 6 |\n| 7 | 8 |\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md:5", found[0])

    def test_two_cuts_in_one_document_are_two_findings(self):
        self.write(
            "docs/notes.md",
            f"{self.TABLE}\n| 3 | 4 |\n\n| 5 | 6 |\n",
        )
        found = self.failures()
        self.assertEqual(len(found), 2, found)
        self.assertIn("docs/notes.md:5", found[0])
        self.assertIn("docs/notes.md:7", found[1])

    def test_prose_between_a_table_and_a_later_row_is_not_a_cut(self):
        # The row after the prose has no table above it to be cut from.
        self.write("docs/notes.md", f"{self.TABLE}\nA paragraph.\n\n| 3 | 4 |\n")
        self.assertEqual(self.failures(), [])

    def test_a_row_without_a_table_above_it_is_not_a_cut(self):
        self.write("docs/notes.md", "Text.\n\n| 3 | 4 |\n")
        self.assertEqual(self.failures(), [])

    def test_a_line_that_only_starts_or_only_ends_with_a_pipe_is_not_a_row(self):
        for line in ("| not closed", "not opened |", "a | b"):
            with self.subTest(line=line):
                self.write("docs/notes.md", f"{self.TABLE}\n{line}\n")
                self.assertEqual(self.failures(), [])

    def test_a_table_without_blank_lines_passes(self):
        self.write("docs/notes.md", f"{self.TABLE}| 3 | 4 |\n")
        self.assertEqual(self.failures(), [])

    def test_every_document_the_checker_reads_is_checked(self):
        self.write("README.md", f"{self.TABLE}\n| 3 | 4 |\n")
        self.write(".claude/notes/extra.md", f"{self.TABLE}\n| 3 | 4 |\n")
        found = self.failures()
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("README.md:5" in d for d in found), found)
        self.assertTrue(any(".claude/notes/extra.md:5" in d for d in found), found)


class Twins(TreeCase):
    """check_twins: the two instruction files are one text, or neither exists."""

    def failures(self):
        return self.run_check(self.check.check_twins)

    def test_identical_files_pass(self):
        self.write("AGENTS.md", "# Rules\n")
        self.write("CLAUDE.md", "# Rules\n")
        self.assertEqual(self.failures(), [])

    def test_differing_files_are_reported(self):
        self.write("AGENTS.md", "# Rules\n")
        self.write("CLAUDE.md", "# Other rules\n")
        found = self.failures()
        self.assertEqual(len(found), 1, found)
        self.assertIn("differ", found[0])

    def test_a_repository_with_neither_file_is_skipped(self):
        # A repository on its first day: the check has no subject yet.
        self.assertEqual(self.failures(), [])

    def test_one_file_without_its_twin_is_reported_not_a_traceback(self):
        for present, missing in (("CLAUDE.md", "AGENTS.md"), ("AGENTS.md", "CLAUDE.md")):
            with self.subTest(present=present):
                for name in ("AGENTS.md", "CLAUDE.md"):
                    (self.repo / name).unlink(missing_ok=True)
                self.write(present, "# Rules\n")
                found = self.failures()
                self.assertEqual(len(found), 1, found)
                self.assertIn(f"{missing} is missing", found[0])


class ThreatIds(TreeCase):
    """check_ids for the threat family: T-NN and, from the hundredth row, T-NNN."""

    THREAT_MODEL = "docs/architecture/security/threat-model.md"

    def setUp(self):
        super().setUp()
        # ID_OWNERS was built from the real repository when the module loaded;
        # keep its real threat pattern and point only the owning file at the tree.
        threat = self.arch / "security" / "threat-model.md"
        self.check.ID_OWNERS = {
            pattern: threat
            for pattern in self.check.ID_OWNERS
            if pattern.startswith(r"\bT-")
        }
        self.write(
            self.THREAT_MODEL,
            """
            ## Threats

            | ID | Threat |
            |---|---|
            | T-42 | A two-digit row. |
            | T-100 | A three-digit row. |
            """,
        )

    def failures(self):
        return self.run_check(self.check.check_ids)

    def test_a_cited_three_digit_threat_that_is_defined_passes(self):
        self.write("docs/notes.md", "The budget guard is T-100.\n")
        self.assertEqual(self.failures(), [])

    def test_a_cited_three_digit_threat_that_is_not_defined_is_reported(self):
        self.write("docs/notes.md", "The budget guard is T-101.\n")

        found = self.failures()

        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md cites T-101", found[0])

    def test_a_cited_two_digit_threat_that_is_defined_passes(self):
        self.write("docs/notes.md", "The budget guard is T-42.\n")
        self.assertEqual(self.failures(), [])

    def test_a_cited_two_digit_threat_that_is_not_defined_is_reported(self):
        self.write("docs/notes.md", "The budget guard is T-43.\n")

        found = self.failures()

        self.assertEqual(len(found), 1, found)
        self.assertIn("docs/notes.md cites T-43", found[0])

    def test_a_threat_defined_by_a_heading_of_three_digits_passes(self):
        self.write(
            self.THREAT_MODEL,
            """
            ### T-102 A heading row

            Text.
            """,
        )
        self.write("docs/notes.md", "See T-102.\n")
        self.assertEqual(self.failures(), [])

    def test_four_digits_and_one_digit_are_not_threat_ids(self):
        # Neither shape is an ID, so neither is checked, as before: T-1000 is
        # not read as T-100 (defined) or as an undefined T-10, and T-1 is not
        # an undefined ID either.
        self.write("docs/notes.md", "Not IDs: T-1000 and T-1 and T-10000.\n")
        self.assertEqual(self.failures(), [])


class ShippedExamples(unittest.TestCase):
    """The base's own example documents must model the correct convention."""

    def test_repository_overview_has_no_hidden_section_titles(self):
        check = load_checker()
        f = check.Failures()
        check.check_heading_visibility(f)
        self.assertEqual([d for _, d in f], [])

    def test_mermaid_check_runs_with_the_others(self):
        check = load_checker()
        self.assertIn(check.check_mermaid, check.CHECKS)

    def test_repository_mermaid_blocks_pass(self):
        check = load_checker()
        f = check.Failures()
        check.check_mermaid(f)
        self.assertEqual([d for _, d in f], [])

    def test_split_table_check_runs_with_the_others(self):
        check = load_checker()
        self.assertIn(check.check_split_tables, check.CHECKS)

    def test_repository_tables_are_not_split(self):
        check = load_checker()
        f = check.Failures()
        check.check_split_tables(f)
        self.assertEqual([d for _, d in f], [])


if __name__ == "__main__":
    unittest.main()
