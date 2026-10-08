"""Tests for the Mermaid handling in scripts/build_architecture_pdf_source.py.

The PDF is what people are handed, and Pandoc cannot draw Mermaid. So the
builder turns every diagram into an image the renderer makes: a fence in a page
becomes `![](generated/mermaid-pdf/<sha12>.png)` with its source saved beside,
and so does the image link Structurizr's Mermaid plugin leaves in ADRs, which
has to be decoded back to source first. A link that cannot be decoded must stop
the build: the alternative is a PDF with a hole where the diagram belongs.

Only pure functions are tested; nothing here starts Docker or Pandoc.

Run: python3 -m unittest discover -s tests
"""

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "build_architecture_pdf_source.py"
)

URL = "https://mermaid.ink"
SOURCE = "stateDiagram-v2\n    [*] --> Running\n    Running --> Done\n"
# Written by Structurizr's plugin with mermaid.url https://mermaid.ink,
# mermaid.format svg and mermaid.compress false, from SOURCE.
PLAIN_PAYLOAD = (
    "c3RhdGVEaWFncmFtLXYyCiAgICBbKl0gLS0-IFJ1bm5pbmcKICAgIFJ1bm5pbmcgLS0-IERvbmUK"
)
PLAIN_LINK = f"![]({URL}/svg/{PLAIN_PAYLOAD})"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_pdf_source", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load_builder()


def pako_payload(source):
    """What the plugin writes with mermaid.compress true."""
    document = json.dumps({"code": source, "mermaid": {"theme": "default"}})
    packed = zlib.compress(document.encode("utf-8"))
    return "pako:" + base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


def digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


class DecodePluginLink(unittest.TestCase):
    def test_plain_link_from_the_plugin_decodes_to_the_source(self):
        self.assertEqual(build.decode_plugin_link(PLAIN_PAYLOAD), SOURCE)

    def test_the_fixture_is_what_the_plugin_would_write(self):
        encoded = base64.urlsafe_b64encode(SOURCE.encode("utf-8")).decode("ascii")
        self.assertEqual(encoded, PLAIN_PAYLOAD)

    def test_pako_link_decodes_to_the_source(self):
        self.assertEqual(build.decode_plugin_link(pako_payload(SOURCE)), SOURCE)

    def test_padding_is_optional(self):
        padded = base64.urlsafe_b64encode(b"graph LR\n  a-->b").decode("ascii")
        self.assertTrue(padded.endswith("="))
        self.assertEqual(build.decode_plugin_link(padded), "graph LR\n  a-->b")
        self.assertEqual(
            build.decode_plugin_link(padded.rstrip("=")), "graph LR\n  a-->b"
        )

    def test_non_ascii_source_survives(self):
        source = 'flowchart LR\n  a["Kéri a fizetést"] --> b\n'
        self.assertEqual(build.decode_plugin_link(pako_payload(source)), source)


class UndecodableLinks(unittest.TestCase):
    """Each of these must stop the build with a message, not emit an image."""

    def assertStops(self, payload):
        with self.assertRaises(SystemExit) as raised:
            build.decode_plugin_link(payload)
        message = str(raised.exception)
        self.assertIn("cannot decode the Mermaid link", message)
        self.assertIn("mermaid.compress", message)
        return message

    def test_not_base64(self):
        self.assertStops("!!not base64!!")

    def test_base64_of_bytes_that_are_not_text(self):
        self.assertStops(base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode("ascii"))

    def test_empty_source(self):
        self.assertStops(base64.urlsafe_b64encode(b"   \n").decode("ascii"))

    def test_pako_prefix_on_a_payload_that_is_not_compressed(self):
        self.assertStops("pako:" + PLAIN_PAYLOAD)

    def test_pako_without_a_code_field(self):
        packed = zlib.compress(json.dumps({"mermaid": {}}).encode("utf-8"))
        self.assertStops("pako:" + base64.urlsafe_b64encode(packed).decode("ascii"))

    def test_pako_that_is_not_json(self):
        packed = zlib.compress(b"not json")
        self.assertStops("pako:" + base64.urlsafe_b64encode(packed).decode("ascii"))

    def test_a_long_link_is_shortened_in_the_message(self):
        message = self.assertStops("!" * 200)
        self.assertNotIn("!" * 100, message)


class MermaidUrl(unittest.TestCase):
    def test_read_from_the_views_configuration_properties(self):
        workspace = {"views": {"configuration": {"properties": {"mermaid.url": URL}}}}
        self.assertEqual(build.mermaid_url(workspace), URL)

    def test_trailing_slash_is_dropped(self):
        workspace = {
            "views": {"configuration": {"properties": {"mermaid.url": URL + "/"}}}
        }
        self.assertEqual(build.mermaid_url(workspace), URL)

    def test_absent_means_the_plugin_is_off(self):
        for workspace in (
            {},
            {"views": {}},
            {"views": {"configuration": {}}},
            {"views": {"configuration": {"properties": {}}}},
            {"views": {"configuration": {"properties": {"mermaid.url": ""}}}},
        ):
            with self.subTest(workspace=workspace):
                self.assertIsNone(build.mermaid_url(workspace))


class Images(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.arch = Path(self.tmp.name).resolve()
        self.generated = self.arch / "generated"
        self.generated.mkdir()
        self.folder = self.generated / "mermaid-pdf"

    def sources(self):
        return {p.name: p.read_text() for p in sorted(self.folder.glob("*.mmd"))}

    def image_line(self, source):
        return f"![](generated/mermaid-pdf/{digest(source)}.png)"

    # -- fences ---------------------------------------------------------------

    def test_a_fence_becomes_an_image_line_and_a_saved_source(self):
        lines = ["Text.", "", "```mermaid", *SOURCE.splitlines(), "```", "", "After."]
        out = build.mermaid_images(lines, self.generated, None)
        self.assertEqual(
            out, ["Text.", "", "", self.image_line(SOURCE), "", "", "After."]
        )
        self.assertEqual(self.sources(), {f"{digest(SOURCE)}.mmd": SOURCE})

    def test_the_image_gets_a_paragraph_to_itself(self):
        """A fence may sit right under a sentence; an inline image is no figure."""
        out = build.mermaid_images(
            ["A sentence.", "```mermaid", "graph LR", "```"], self.generated, None
        )
        self.assertEqual(out[:3], ["A sentence.", "", out[2]])
        self.assertTrue(out[2].startswith("![](generated/mermaid-pdf/"))
        self.assertEqual(out[3], "")

    def test_tilde_fence_and_uppercase_info_string(self):
        out = build.mermaid_images(
            ["~~~Mermaid", "graph LR", "~~~"], self.generated, None
        )
        self.assertIn(self.image_line("graph LR\n"), out)

    def test_two_fences_with_the_same_source_share_one_file(self):
        block = ["```mermaid", "graph LR", "```"]
        out = build.mermaid_images([*block, "", *block], self.generated, None)
        self.assertEqual(out.count(self.image_line("graph LR\n")), 2)
        self.assertEqual(len(self.sources()), 1)

    def test_other_fences_pass_through_untouched(self):
        lines = ["```bash", "echo hi", "```", "", "~~~", "plain", "~~~"]
        self.assertEqual(build.mermaid_images(lines, self.generated, None), lines)
        self.assertFalse(self.folder.exists())

    def test_a_longer_fence_can_show_a_mermaid_example(self):
        lines = [
            "````markdown",
            "```mermaid",
            "graph LR",
            "```",
            "",
            "```mermaid",
            "graph TD",
            "```",
            "````",
        ]
        self.assertEqual(build.mermaid_images(lines, self.generated, None), lines)
        self.assertFalse(self.folder.exists())

    def test_inline_code_that_looks_like_a_fence_is_text(self):
        lines = ["```mermaid```", "still prose"]
        self.assertEqual(build.mermaid_images(lines, self.generated, None), lines)

    def test_an_indented_fence_keeps_its_indent(self):
        out = build.mermaid_images(
            ["- item", "  ```mermaid", "  graph LR", "  ```"], self.generated, None
        )
        self.assertIn("  " + self.image_line("  graph LR\n"), out)

    def test_an_unclosed_mermaid_fence_stops_the_build(self):
        with self.assertRaises(SystemExit) as raised:
            build.mermaid_images(["```mermaid", "graph LR"], self.generated, None)
        self.assertIn("never closed", str(raised.exception))

    # -- plugin links ---------------------------------------------------------

    def test_a_plain_plugin_link_becomes_the_same_image_as_the_fence(self):
        out = build.mermaid_images(
            ["Before.", PLAIN_LINK, "After."], self.generated, URL
        )
        self.assertEqual(out, ["Before.", self.image_line(SOURCE), "After."])
        self.assertEqual(self.sources(), {f"{digest(SOURCE)}.mmd": SOURCE})

    def test_a_pako_plugin_link_decodes_too(self):
        link = f"![]({URL}/png/{pako_payload(SOURCE)})"
        out = build.mermaid_images([link], self.generated, URL)
        self.assertEqual(out, [self.image_line(SOURCE)])
        self.assertEqual(self.sources(), {f"{digest(SOURCE)}.mmd": SOURCE})

    def test_a_self_hosted_server_with_a_trailing_slash_and_a_path(self):
        url = "http://localhost:3000/mermaid"
        out = build.mermaid_images(
            [f"![]({url}/svg/{PLAIN_PAYLOAD})"], self.generated, url
        )
        self.assertEqual(out, [self.image_line(SOURCE)])

    def test_links_to_other_servers_are_not_ours_to_decode(self):
        lines = [
            "![logo](https://example.com/svg/abc)",
            f"![]({URL}/svg/{PLAIN_PAYLOAD})",
        ]
        out = build.mermaid_images(lines, self.generated, "https://kroki.io")
        self.assertEqual(out, lines)
        self.assertFalse(self.folder.exists())

    def test_without_a_configured_url_no_link_is_touched(self):
        self.assertEqual(
            build.mermaid_images([PLAIN_LINK], self.generated, None), [PLAIN_LINK]
        )

    def test_an_undecodable_link_stops_the_build(self):
        with self.assertRaises(SystemExit) as raised:
            build.mermaid_images([f"![]({URL}/svg/@@@)"], self.generated, URL)
        self.assertIn("cannot decode the Mermaid link", str(raised.exception))

    def test_a_link_shown_inside_a_code_fence_is_left_alone(self):
        lines = ["```markdown", PLAIN_LINK, "```"]
        self.assertEqual(build.mermaid_images(lines, self.generated, URL), lines)

    # -- the whole document ---------------------------------------------------

    def test_body_turns_fences_into_images_and_leaves_the_rest_as_before(self):
        lines = ["## Payment states", "", "```mermaid", "graph LR", "```", "", "Done."]
        text, embedded = build.body(lines, {}, self.generated)
        self.assertIn(self.image_line("graph LR\n"), text.splitlines())
        self.assertIn("# Payment states", text)
        self.assertEqual(embedded, set())

    def test_body_decodes_links_only_when_given_the_url(self):
        text, _ = build.body([PLAIN_LINK], {}, self.generated, mermaid=URL)
        self.assertEqual(text.strip(), self.image_line(SOURCE))
        text, _ = build.body([PLAIN_LINK], {}, self.generated)
        self.assertEqual(text.strip(), PLAIN_LINK)

    def test_body_without_mermaid_is_what_it_was(self):
        lines = [
            "## A",
            "",
            "See [the guide](docs/guide.md).",
            "",
            "```bash",
            "ls",
            "```",
        ]
        text, _ = build.body(lines, {}, self.generated)
        self.assertEqual(
            text.splitlines(),
            ["# A", "", "See the guide.", "", "```bash", "ls", "```"],
        )
        self.assertFalse(self.folder.exists())

    def test_adr_content_is_converted_through_decisions(self):
        workspace = {
            "documentation": {
                "decisions": [
                    {
                        "id": "1",
                        "format": "Markdown",
                        "content": f"# 1. Use it\n\n## Status\n\nAccepted\n\n{PLAIN_LINK}\n",
                    }
                ]
            }
        }
        text, _ = build.decisions(workspace, {}, self.generated, URL)
        self.assertIn(self.image_line(SOURCE), text.splitlines())

    def test_reset_empties_the_folder(self):
        build.mermaid_image("graph LR\n", self.generated)
        self.assertTrue(self.folder.exists())
        build.reset_mermaid(self.generated)
        self.assertFalse(self.folder.exists())
        build.reset_mermaid(self.generated)  # nothing there is not an error


class Edition(unittest.TestCase):
    """edition() names the PDF; a repository without commits must not crash it."""

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.cwd)
        os.chdir(self.tmp.name)
        self.arch = Path("docs/architecture")
        self.arch.mkdir(parents=True)
        (self.arch / "workspace.dsl").write_text("workspace {}\n")
        self.git("init", "--quiet")

    def git(self, *args):
        config = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        config += ["-c", "commit.gpgsign=false"]
        return subprocess.run(
            ["git", *config, *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_repository_without_commits_is_uncommitted(self):
        sha, dirty = build.edition(self.arch)
        self.assertEqual(sha, "uncommitted")
        self.assertTrue(dirty)  # the untracked model is what `git status` sees

    def test_single_commit_gives_a_seven_character_sha(self):
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "model")
        sha, dirty = build.edition(self.arch)
        self.assertEqual(len(sha), 7)
        self.assertEqual(sha, self.git("rev-parse", "HEAD")[:7])
        self.assertFalse(dirty)


class Tables(unittest.TestCase):
    """body() prints tables through pdf_tables; its own tests have the rules."""

    def test_a_table_with_a_long_cell_reaches_the_pdf_as_records(self):
        long = ("word " * 80).strip()
        table = ["| ID | Threat |", "|---|---|", f"| T-01 | {long} |"]
        lines = ["## Threats", "", *table]
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = build.body(lines, {}, Path(tmp))
        self.assertIn("**T-01**", text.splitlines())
        self.assertNotIn("|---|---|", text)

    def test_a_link_in_a_record_is_still_made_plain(self):
        long = ("word " * 80).strip()
        row = f"| T-01 | [guide](docs/g.md) {long} |"
        lines = ["| ID | Threat |", "|---|---|", row]
        with tempfile.TemporaryDirectory() as tmp:
            text, _ = build.body(lines, {}, Path(tmp))
        self.assertIn("*Threat.* guide word", text)


class Brief(unittest.TestCase):
    """The brief: the full edition's source with some documents left out, the
    decisions as an index, and a first page that says what is not in it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.arch = Path(self.tmp.name) / "docs" / "architecture"
        (self.arch / "overview").mkdir(parents=True)
        (self.arch / "security").mkdir()
        (self.arch / "overview" / "01-overview.md").write_text("## Overview\n\nText.\n")
        (self.arch / "security" / "threat-model.md").write_text(
            "## Threat model\n\nRows.\n"
        )
        (self.arch / "overview" / "20-threat-model.md").symlink_to(
            "../security/threat-model.md"
        )
        self.pages = sorted((self.arch / "overview").glob("*.md"))

    def listing(self, text):
        (self.arch / build.BRIEF_OMIT).write_text(text)

    def test_without_a_listing_every_page_stays(self):
        kept, left_out = build.brief_pages(self.pages, self.arch)
        self.assertEqual(kept, self.pages)
        self.assertEqual(left_out, [])

    def test_a_listed_register_is_left_out_through_its_symlink(self):
        self.listing("# what the brief leaves out\nsecurity/threat-model.md\n")
        kept, left_out = build.brief_pages(self.pages, self.arch)
        self.assertEqual([page.name for page in kept], ["01-overview.md"])
        self.assertEqual([page.name for page in left_out], ["20-threat-model.md"])

    def test_a_register_symlinked_in_from_outside_can_be_left_out(self):
        outside = self.arch.parent / "operations"
        outside.mkdir()
        (outside / "runbooks.md").write_text("## Runbooks\n\nSteps.\n")
        (self.arch / "overview" / "30-runbooks.md").symlink_to(
            "../../operations/runbooks.md"
        )
        pages = sorted((self.arch / "overview").glob("*.md"))
        self.listing("overview/30-runbooks.md\n")
        kept, left_out = build.brief_pages(pages, self.arch)
        self.assertEqual([page.name for page in left_out], ["30-runbooks.md"])
        self.assertEqual(len(kept), 2)

    def test_a_listed_path_that_leaves_the_directory_stops_the_build(self):
        (self.arch.parent / "secret.md").write_text("## Secret\n")
        self.listing("../secret.md\n")
        with self.assertRaises(SystemExit):
            build.brief_pages(self.pages, self.arch)

    def test_a_page_can_be_listed_by_its_own_path(self):
        self.listing("overview/01-overview.md\n")
        kept, _ = build.brief_pages(self.pages, self.arch)
        self.assertEqual([page.name for page in kept], ["20-threat-model.md"])

    def test_a_listed_file_that_no_page_shows_stops_the_build(self):
        # A typo would otherwise leave the document in the brief unnoticed.
        self.listing("security/threats.md\n")
        with self.assertRaises(SystemExit) as stop:
            build.brief_pages(self.pages, self.arch)
        self.assertIn("security/threats.md", str(stop.exception))

    def test_a_brief_that_leaves_every_page_out_stops_the_build(self):
        self.listing("overview/01-overview.md\nsecurity/threat-model.md\n")
        with self.assertRaises(SystemExit):
            build.brief_pages(self.pages, self.arch)

    def test_the_title_of_a_page_is_its_first_heading(self):
        self.assertEqual(build.page_title(self.pages[1]), "Threat model")

    def test_the_decisions_become_an_index_in_number_order(self):
        workspace = {
            "documentation": {
                "decisions": [
                    {
                        "id": "10",
                        "title": "Split it",
                        "status": "Proposed",
                        "date": "2026-10-07T00:00:00Z",
                        "format": "Markdown",
                        "content": "# 10. Split it\n\nA long text.\n",
                    },
                    {
                        "id": "2",
                        "title": "Use a | pipe",
                        "status": "Accepted",
                        "date": "2026-09-29T00:00:00Z",
                        "format": "Markdown",
                        "content": "# 2. Use a pipe\n",
                    },
                ]
            }
        }
        text, count = build.decision_index(workspace)
        lines = text.splitlines()
        self.assertEqual(count, 2)
        self.assertIn("# Decisions", lines)
        rows = [line for line in lines if line.startswith("| ")]
        self.assertEqual(rows[1], "| 2 | Use a \\| pipe | Accepted | 2026-09-29 |")
        self.assertEqual(rows[2], "| 10 | Split it | Proposed | 2026-10-07 |")
        self.assertNotIn("A long text.", text)

    def test_a_decision_with_a_missing_or_odd_field_still_gives_one_row(self):
        workspace = {
            "documentation": {
                "decisions": [
                    {
                        "id": "3",
                        "title": "Two\nlines",
                        "status": "Accepted | superseded",
                        "date": None,
                        "format": "Markdown",
                        "content": "# 3. Two lines\n",
                    }
                ]
            }
        }
        text, _ = build.decision_index(workspace)
        rows = [line for line in text.splitlines() if line.startswith("| ")]
        self.assertEqual(rows[1], "| 3 | Two lines | Accepted \\| superseded |  |")

    def test_no_decisions_means_no_index(self):
        self.assertEqual(build.decision_index({}), ("", 0))

    def test_the_first_page_says_what_the_brief_leaves_out(self):
        note = build.brief_note(["Threat model", "Azure platform"], 11)
        self.assertIn("Threat model", note)
        self.assertIn("Azure platform", note)
        self.assertIn("11 decisions", note)
        self.assertIn("full edition", note)

    def test_the_first_page_of_a_brief_that_leaves_nothing_out_says_so(self):
        note = build.brief_note([], 0)
        self.assertIn("full edition", note)
        self.assertNotIn("0 decisions", note)

    def test_the_brief_has_its_own_file_name_with_the_same_edition_at_the_end(self):
        day = build.dt.date(2026, 10, 8)
        full = build.pdf_name("Payment Platform", day, "af49b21", False)
        brief = build.pdf_name("Payment Platform", day, "af49b21", False, brief=True)
        self.assertEqual(full, "payment-platform-architecture-2026-10-08-af49b21.pdf")
        self.assertEqual(
            brief, "payment-platform-architecture-brief-2026-10-08-af49b21.pdf"
        )

    def test_the_cover_of_the_brief_says_brief(self):
        day = build.dt.date(2026, 10, 8)
        brief = build.header("P", "af49b21", day, brief=True)
        self.assertIn("Architecture brief", brief)
        self.assertIn("Architecture documentation", build.header("P", "af49b21", day))


class BriefFromTheCommandLine(unittest.TestCase):
    """The script itself, with --brief, on a workspace of one page and one ADR."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.arch = self.root / "docs" / "architecture"
        (self.arch / "overview").mkdir(parents=True)
        (self.arch / "generated").mkdir()
        (self.arch / "workspace.dsl").write_text("workspace {\n    !docs overview\n}\n")
        (self.arch / "overview" / "01-overview.md").write_text("## Overview\n\nText.\n")
        (self.arch / "overview" / "20-threats.md").write_text("## Threats\n\nRows.\n")
        (self.arch / "pdf-brief.txt").write_text("overview/20-threats.md\n")
        workspace = {
            "name": "Demo",
            "views": {},
            "documentation": {
                "decisions": [
                    {
                        "id": "1",
                        "title": "Use it",
                        "status": "Accepted",
                        "date": "2026-01-15T00:00:00Z",
                        "format": "Markdown",
                        "content": "# 1. Use it\n\nThe whole text of the decision.\n",
                    }
                ]
            },
        }
        (self.arch / "generated" / "workspace.json").write_text(json.dumps(workspace))
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)

    def build(self, *extra):
        output = self.arch / "generated" / "out.md"
        result = subprocess.run(
            ["python3", str(SCRIPT), "docs/architecture", "docs/architecture/generated"]
            + [str(output), *extra],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, output.read_text() if output.exists() else ""

    def test_the_brief_leaves_the_listed_page_and_the_decisions_text_out(self):
        result, text = self.build("--brief")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-architecture-brief-", result.stdout)
        self.assertIn("# About this brief", text)
        self.assertIn("- Threats", text)
        self.assertNotIn("Rows.", text)
        self.assertIn("| 1 | Use it | Accepted | 2026-01-15 |", text)
        self.assertNotIn("The whole text of the decision.", text)

    def test_without_the_flag_the_full_edition_holds_everything(self):
        result, text = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("-brief-", result.stdout)
        self.assertNotIn("About this brief", text)
        self.assertIn("Rows.", text)
        self.assertIn("The whole text of the decision.", text)

    def test_an_argument_it_does_not_know_is_refused(self):
        result, _ = self.build("--short")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
