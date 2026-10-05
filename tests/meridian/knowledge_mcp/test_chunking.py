"""``parse_wording``: a policy wording becomes clause chunks (S012)."""

import dataclasses
import json
from pathlib import Path

import pytest
from cputime import MAX_GROWTH, growth
from servicesupport import REPO_ROOT

from meridian.platform.knowledge_mcp.chunking import (
    BODY_MAX_CHARACTERS,
    PRODUCT_MAX_CHARACTERS,
    TITLE_MAX_CHARACTERS,
    VERSION_MAX_CHARACTERS,
    Chunk,
    WordingDocument,
    WordingError,
    parse_wording,
)

WORDINGS = REPO_ROOT / "data" / "synthetic" / "wordings"
OUTCOMES = REPO_ROOT / "data" / "synthetic" / "expected-outcomes.json"
CANARY = "CANARY-wording-text-6641"

HEADER = "Product code: `TEST-ONE`. Wording version: 2026-01.\n"
GOOD = (
    "# Title\n\n" + HEADER + "\n## 1. Definitions\n\n"
    "### 1.1 First\n\nBody one.\nSecond line.\n\n"
    "### 1.2 Second\n\nBody two.\n\n"
    "## 2. Cover\n\n"
    "### 2.1 Third\n\nBody three.\n"
)


def real_documents() -> dict[str, WordingDocument]:
    return {
        path.stem: parse_wording(path.read_text(encoding="utf-8"))
        for path in sorted(WORDINGS.glob("*.md"))
    }


def refused(text: str) -> WordingError:
    with pytest.raises(WordingError) as raised:
        parse_wording(text)
    return raised.value


# ── the repository's wordings ───────────────────────────────────────────────
def test_the_four_wordings_parse_to_eighty_five_clauses() -> None:
    documents = real_documents()

    assert sorted(documents) == ["HOME-PLUS", "HOME-STD", "MOTOR-COMP", "MOTOR-TPL"]
    assert sum(len(d.chunks) for d in documents.values()) == 85


def test_each_wording_names_the_product_and_version_its_file_says() -> None:
    for name, document in real_documents().items():
        text = (WORDINGS / f"{name}.md").read_text(encoding="utf-8")
        assert f"Product code: `{name}`. Wording version: 2026-01." in text
        assert (document.product, document.wording_version) == (name, "2026-01")


def test_every_golden_citation_resolves_to_a_chunk() -> None:
    documents = real_documents()
    outcomes = json.loads(OUTCOMES.read_text(encoding="utf-8"))
    citations = [c for outcome in outcomes for c in outcome["citations"]]
    assert citations

    for citation in citations:
        document = documents[citation["wording"]]
        assert citation["clause"] in {c.clause for c in document.chunks}, citation


def test_the_longest_real_clause_fits_the_columns_with_room_to_spare() -> None:
    chunks = [c for d in real_documents().values() for c in d.chunks]

    assert max(len(c.body) for c in chunks) < BODY_MAX_CHARACTERS
    assert max(len(c.title) for c in chunks) < TITLE_MAX_CHARACTERS


def test_a_real_clause_has_its_section_title_and_a_stripped_body() -> None:
    document = parse_wording((WORDINGS / "MOTOR-TPL.md").read_text(encoding="utf-8"))

    first = document.chunks[0]

    assert (first.clause, first.section, first.title) == (
        "1.1",
        "Definitions",
        "You and we",
    )
    assert first.body.startswith('In this wording "you" means')
    assert first.body == first.body.strip()
    assert "###" not in first.body
    assert "## " not in first.body


# ── the parse of a small document ───────────────────────────────────────────
def test_a_small_document_gives_its_clauses_in_order() -> None:
    document = parse_wording(GOOD)

    assert document == WordingDocument(
        product="TEST-ONE",
        wording_version="2026-01",
        chunks=(
            Chunk("1.1", "Definitions", "First", "Body one.\nSecond line."),
            Chunk("1.2", "Definitions", "Second", "Body two."),
            Chunk("2.1", "Cover", "Third", "Body three."),
        ),
        skipped_lines=0,
    )


def test_a_heading_before_the_first_section_is_ignored() -> None:
    text = GOOD.replace("# Title\n", "# Title\n\n#### Notes\n\n# Another\n")

    assert parse_wording(text).chunks == parse_wording(GOOD).chunks


def test_a_numbered_section_and_clause_of_two_digits_are_accepted() -> None:
    text = HEADER + "\n## 10. Late\n\n### 10.12 Twelfth\n\nBody.\n"

    document = parse_wording(text)

    assert document.chunks == (Chunk("10.12", "Late", "Twelfth", "Body."),)


def test_windows_line_endings_parse_the_same() -> None:
    assert parse_wording(GOOD.replace("\n", "\r\n")) == parse_wording(GOOD)


def test_the_results_are_frozen_and_slotted() -> None:
    document = parse_wording(GOOD)

    with pytest.raises(dataclasses.FrozenInstanceError):
        document.product = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        document.chunks[0].body = "other"  # type: ignore[misc]
    assert not hasattr(document, "__dict__")
    assert not hasattr(document.chunks[0], "__dict__")
    assert isinstance(document.chunks, tuple)


def test_a_clause_title_and_body_at_the_column_limits_are_accepted() -> None:
    title = "t" * TITLE_MAX_CHARACTERS
    body = "b" * BODY_MAX_CHARACTERS

    document = parse_wording(
        GOOD.replace("First", title).replace("Body one.\nSecond line.", body)
    )

    assert (document.chunks[0].title, document.chunks[0].body) == (title, body)


# ── what it refuses ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "line", "rule"),
    [
        pytest.param(
            GOOD.replace(HEADER, ""), None, "product code", id="no-header-line"
        ),
        pytest.param(
            GOOD.replace("## 1. Definitions\n\n", "", 1),
            5,
            "before any section",
            id="clause-before-a-section",
        ),
        pytest.param(
            GOOD.replace("### 1.2 Second", "### 2.2 Second"),
            12,
            "section",
            id="clause-number-of-another-section",
        ),
        pytest.param(
            GOOD.replace("### 1.2 Second", "### 1.1 Second"),
            12,
            "repeated",
            id="repeated-clause",
        ),
        pytest.param(GOOD.replace("Body two.", ""), 12, "empty", id="empty-body"),
        pytest.param(
            HEADER + "\n## 1. Definitions\n\nJust text.\n",
            None,
            "no clause",
            id="no-clause",
        ),
        pytest.param(
            GOOD.replace("First", "t" * (TITLE_MAX_CHARACTERS + 1)),
            7,
            "title",
            id="title-over-the-limit",
        ),
        pytest.param(
            GOOD.replace("Body one.", "b" * (BODY_MAX_CHARACTERS + 1)),
            7,
            "body",
            id="body-over-the-limit",
        ),
        pytest.param(
            GOOD.replace("Definitions", "s" * (TITLE_MAX_CHARACTERS + 1)),
            5,
            "section",
            id="section-title-over-the-limit",
        ),
        pytest.param(
            GOOD.replace("### 1.1 First", "### 1.123 First"),
            7,
            "heading",
            id="clause-heading-that-does-not-match",
        ),
        pytest.param(
            GOOD.replace("## 2. Cover", "## Cover"),
            16,
            "heading",
            id="section-heading-that-does-not-match",
        ),
        pytest.param("", None, "product code", id="empty-document"),
        pytest.param(
            GOOD.replace("Body one.", "Body one.\n\n#### A smaller heading\n\nHidden."),
            11,
            "neither a section",
            id="deeper-heading-inside-a-clause",
        ),
        pytest.param(
            GOOD.replace("Body two.", "Body two.\n\n# Another title\n\nHidden."),
            16,
            "neither a section",
            id="level-one-heading-after-the-first-section",
        ),
        pytest.param(
            GOOD.replace("## 2. Cover\n", "## 2. Cover\n\n###### Intro heading\n"),
            18,
            "neither a section",
            id="deeper-heading-in-a-section-introduction",
        ),
        pytest.param(
            GOOD + "\n#### Last words\n",
            22,
            "neither a section",
            id="deeper-heading-after-the-last-clause",
        ),
        pytest.param(
            GOOD.replace("### 1.1 First", "### 01.1 First"),
            7,
            "heading",
            id="clause-number-with-a-leading-zero-in-the-section-part",
        ),
        pytest.param(
            GOOD.replace("### 1.2 Second", "### 1.02 Second"),
            12,
            "heading",
            id="clause-number-with-a-leading-zero-in-the-clause-part",
        ),
        pytest.param(
            GOOD.replace("### 1.2 Second", "### 1.0 Second"),
            12,
            "heading",
            id="clause-number-zero",
        ),
        pytest.param(
            GOOD.replace("## 2. Cover", "## 02. Cover"),
            16,
            "heading",
            id="section-number-with-a-leading-zero",
        ),
        pytest.param(
            GOOD.replace("Body one.", "Body one.\x00"),
            None,
            "NUL",
            id="nul-in-a-body",
        ),
        pytest.param(
            GOOD.replace("First", "Fi\x00rst"), 7, "NUL", id="nul-in-a-clause-title"
        ),
        pytest.param(
            GOOD.replace("# Title", "# Ti\x00tle"), 1, "NUL", id="nul-in-the-title"
        ),
        pytest.param(
            GOOD.replace("Product code", "Product\x00code"),
            3,
            "NUL",
            id="nul-in-the-header-line",
        ),
        pytest.param(
            GOOD.replace("## 2. Cover\n", "## 2. Cover\n\nIntro\x00\n"),
            18,
            "NUL",
            id="nul-in-a-section-introduction",
        ),
    ],
)
def test_a_malformed_wording_is_refused_naming_the_line_and_the_rule(
    text: str, line: int | None, rule: str
) -> None:
    error = refused(text)

    message = str(error)
    assert rule in message
    if line is not None:
        assert f"line {line}" in message


def test_a_wording_with_a_header_whose_product_is_over_the_limit_is_refused() -> None:
    error = refused(GOOD.replace("TEST-ONE", "P" * (PRODUCT_MAX_CHARACTERS + 1)))

    assert "line 3" in str(error)
    assert f"longer than {PRODUCT_MAX_CHARACTERS} characters" in str(error)


def test_a_wording_with_a_header_whose_version_is_over_the_limit_is_refused() -> None:
    error = refused(GOOD.replace("2026-01", "9" * (VERSION_MAX_CHARACTERS + 1)))

    assert "line 3" in str(error)
    assert f"longer than {VERSION_MAX_CHARACTERS} characters" in str(error)


def test_a_header_product_and_version_at_the_limits_are_accepted() -> None:
    text = GOOD.replace("TEST-ONE", "P" * PRODUCT_MAX_CHARACTERS).replace(
        "2026-01", "9" * VERSION_MAX_CHARACTERS
    )

    document = parse_wording(text)

    assert document.product == "P" * PRODUCT_MAX_CHARACTERS
    assert document.wording_version == "9" * VERSION_MAX_CHARACTERS


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("### 1.2 Second", f"### 1.2 {CANARY}\n\n### 1.2 x"),
        lambda text: text.replace("### 1.1 First", f"### 1.1 {CANARY}\n### 1.1x"),
        lambda text: text.replace("Definitions", CANARY * 20),
        lambda text: text.replace("Body one.", CANARY * 400),
        lambda text: text.replace("Body two.", "").replace("First", CANARY),
        lambda text: text.replace("### 1.2 Second", f"### 2.2 {CANARY}"),
        lambda text: text.replace(HEADER, f"Product code: {CANARY}\n"),
        lambda text: text.replace("## 2. Cover", f"## {CANARY}"),
        lambda text: text.replace(
            "### 2.1 Third\n\nBody three.\n", f"### 2.1 {CANARY}\n"
        ),
        lambda text: text.replace("Body one.", f"Body one.\n#### {CANARY}"),
        lambda text: text.replace("Body one.", f"Body one.\x00 {CANARY}"),
        lambda text: text.replace("TEST-ONE", CANARY.upper() * 3),
    ],
)
def test_no_error_quotes_the_document_or_chains_a_cause(mutation) -> None:
    error = refused(mutation(GOOD))

    assert CANARY not in str(error)
    assert CANARY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_a_wording_can_come_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "TEST-ONE.md"
    path.write_text(GOOD, encoding="utf-8")

    assert parse_wording(path.read_text(encoding="utf-8")).product == "TEST-ONE"


# ── the lines that belong to no clause ──────────────────────────────────────
def test_a_section_introduction_is_counted_and_not_stored() -> None:
    text = GOOD.replace(
        "## 2. Cover\n", "## 2. Cover\n\nThe clauses below list cover.\nTwo lines.\n"
    )

    document = parse_wording(text)

    assert document.skipped_lines == 2
    assert document.chunks == parse_wording(GOOD).chunks
    assert all("list cover" not in c.body for c in document.chunks)


def test_a_document_without_an_introduction_skips_nothing() -> None:
    assert parse_wording(GOOD).skipped_lines == 0


def test_the_lines_before_the_first_section_are_not_counted() -> None:
    text = GOOD.replace("# Title\n", "# Title\n\nA preface of\ntwo lines.\n")

    assert parse_wording(text).skipped_lines == 0


def test_a_blank_line_in_an_introduction_is_not_counted() -> None:
    text = GOOD.replace("## 1. Definitions\n", "## 1. Definitions\n\n\n   \n\nOne.\n")

    assert parse_wording(text).skipped_lines == 1


def _non_blank_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.strip()]


def _after_the_first_section(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").split("\n")
    first = next(n for n, line in enumerate(lines) if line.startswith("## "))
    return lines[first:]


def test_the_four_real_wordings_skip_five_lines_each_all_in_section_introductions() -> (
    None
):
    for name, document in real_documents().items():
        after = _after_the_first_section(WORDINGS / f"{name}.md")
        headings = [line for line in after if line.startswith(("## ", "### "))]
        text_lines = [
            line for line in _non_blank_lines("\n".join(after)) if line not in headings
        ]
        stored = sum(len(_non_blank_lines(c.body)) for c in document.chunks)
        introductions: list[str] = []
        inside_introduction = False
        for line in after:
            if line.startswith("### "):
                inside_introduction = False
            elif line.startswith("## "):
                inside_introduction = True
            elif inside_introduction and line.strip():
                introductions.append(line)

        assert document.skipped_lines == 5, name
        # Every non-blank line after the first section is a heading, a stored
        # body line or one of the skipped lines, and the skipped ones are the
        # lines between a ## heading and the next ### heading.
        assert len(text_lines) - stored == document.skipped_lines, name
        assert len(introductions) == document.skipped_lines, name


# ── the cost of a refusal ───────────────────────────────────────────────────
def test_a_clause_of_sixty_thousand_lines_is_refused_quickly() -> None:
    def with_lines(count: int) -> str:
        return GOOD.replace("Body one.", "\n".join(["x"] * count))

    small, large = with_lines(15_000), with_lines(60_000)

    assert "longer than" in str(refused(small))
    assert "longer than" in str(refused(large))
    # CPU time at a small and a large count, not a limit on the wall clock: the
    # refusal costs time in proportion to the text.
    assert growth(refused, small, large) < MAX_GROWTH
