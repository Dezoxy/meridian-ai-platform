"""Cut a policy wording into clause chunks (S012). Pure: no I/O.

A wording is Markdown: one header line that names the product and the wording
version, ``## <n>. <title>`` sections and ``### <n>.<m> <title>`` clauses, whose
numbers have no leading zero. A clause's body is the text from its heading to
the next heading, stripped. Once the first section heading has been seen, a
heading of any other form is an error: it would end a clause and drop what
follows. The limits are those of the columns of ``knowledge.chunks`` (migration
0005), so a chunk that passes here is one the table accepts.

An error names the line and the rule, never the text: a wording is content, and
a message that quotes it could reach a log or a terminal.
"""

import re
from dataclasses import dataclass

TITLE_MAX_CHARACTERS = 200
BODY_MAX_CHARACTERS = 4000
PRODUCT_MAX_CHARACTERS = 32
VERSION_MAX_CHARACTERS = 16

# The lengths are checked after the match, so a value over the limit is
# reported as that and not as a missing header.
HEADER_LINE = re.compile(
    r"Product code: `([A-Z0-9-]+)`\. Wording version: ([A-Za-z0-9-]+)\."
)
ANY_HEADING = re.compile(r"#{1,6} ")
SECTION_HEADING = re.compile(r"## ([1-9][0-9]?)\. (\S.*)")
CLAUSE_HEADING = re.compile(r"### ([1-9][0-9]?)\.([1-9][0-9]?) (\S.*)")
SECTION_LEVEL = "## "
CLAUSE_LEVEL = "### "


class WordingError(Exception):
    """The wording breaks a rule. The message names the line and the rule and
    holds no text of the document."""


@dataclass(frozen=True, slots=True)
class Chunk:
    clause: str
    section: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class WordingDocument:
    """``skipped_lines`` is the number of non-blank lines after the first
    section heading that belong to no clause: a section's introduction, the text
    between its heading and its first clause. They are not stored, on purpose
    (they name no clause a search could cite); the count makes that visible.
    Lines before the first section heading, the title and the header line, are
    not counted."""

    product: str
    wording_version: str
    chunks: tuple[Chunk, ...]
    skipped_lines: int


@dataclass(frozen=True, slots=True)
class _Section:
    number: str
    title: str


@dataclass(frozen=True, slots=True)
class _OpenClause:
    line: int
    number: str
    title: str
    section: str


def _header(lines: list[str]) -> tuple[str, str]:
    for line_number, line in enumerate(lines, start=1):
        found = HEADER_LINE.fullmatch(line.strip())
        if not found:
            continue
        for value, name, limit in (
            (found[1], "product code", PRODUCT_MAX_CHARACTERS),
            (found[2], "wording version", VERSION_MAX_CHARACTERS),
        ):
            if len(value) > limit:
                raise WordingError(
                    f"line {line_number}: the {name} is longer than {limit} characters"
                )
        return found[1], found[2]
    raise WordingError(
        "the wording has no product code and wording version header line"
    )


def _section(line: int, match: re.Match[str]) -> _Section:
    title = match[2].strip()
    if len(title) > TITLE_MAX_CHARACTERS:
        raise WordingError(
            f"line {line}: the section title is longer than "
            f"{TITLE_MAX_CHARACTERS} characters"
        )
    return _Section(match[1], title)


def _open_clause(
    line: int, match: re.Match[str], section: _Section | None, seen: set[str]
) -> _OpenClause:
    if section is None:
        raise WordingError(f"line {line}: a clause comes before any section")
    if match[1] != section.number:
        raise WordingError(
            f"line {line}: the clause number does not start with its section's number"
        )
    clause = f"{match[1]}.{match[2]}"
    if clause in seen:
        raise WordingError(f"line {line}: the clause number is repeated")
    title = match[3].strip()
    if len(title) > TITLE_MAX_CHARACTERS:
        raise WordingError(
            f"line {line}: the clause title is longer than "
            f"{TITLE_MAX_CHARACTERS} characters"
        )
    return _OpenClause(line, clause, title, section.title)


def _close(clause: _OpenClause, body_lines: list[str]) -> Chunk:
    body = "\n".join(body_lines).strip()
    if not body:
        raise WordingError(f"line {clause.line}: the clause body is empty")
    if len(body) > BODY_MAX_CHARACTERS:
        raise WordingError(
            f"line {clause.line}: the clause body is longer than "
            f"{BODY_MAX_CHARACTERS} characters"
        )
    return Chunk(clause.number, clause.section, clause.title, body)


def _chunks(lines: list[str]) -> tuple[tuple[Chunk, ...], int]:
    """The clauses and the number of skipped lines (see ``WordingDocument``)."""
    chunks: list[Chunk] = []
    seen: set[str] = set()
    section: _Section | None = None
    current: _OpenClause | None = None
    # A list, not a tuple rebuilt per line: a clause of 60,000 lines is
    # refused in linear time.
    body_lines: list[str] = []
    skipped = 0
    for line, text in enumerate(lines, start=1):
        if not ANY_HEADING.match(text):
            if current is not None:
                body_lines.append(text)
            elif section is not None and text.strip():
                skipped += 1
            continue
        if current is not None:
            chunks.append(_close(current, body_lines))
            current = None
            body_lines = []
        if text.startswith(CLAUSE_LEVEL):
            found = CLAUSE_HEADING.fullmatch(text.rstrip())
            if found is None:
                raise WordingError(
                    f"line {line}: the heading does not match the clause "
                    "heading rule (### n.m Title)"
                )
            current = _open_clause(line, found, section, seen)
            seen.add(current.number)
        elif text.startswith(SECTION_LEVEL):
            found = SECTION_HEADING.fullmatch(text.rstrip())
            if found is None:
                raise WordingError(
                    f"line {line}: the heading does not match the section "
                    "heading rule (## n. Title)"
                )
            section = _section(line, found)
        elif section is not None:
            raise WordingError(
                f"line {line}: the heading is neither a section (## n. Title) "
                "nor a clause (### n.m Title)"
            )
    if current is not None:
        chunks.append(_close(current, body_lines))
    return tuple(chunks), skipped


def parse_wording(text: str) -> WordingDocument:
    """The product, the wording version and the clauses of one wording, in
    document order. Raises ``WordingError`` for a wording that breaks a rule."""
    text = text.replace("\r\n", "\n")
    if "\x00" in text:
        line = text.count("\n", 0, text.index("\x00")) + 1
        raise WordingError(f"line {line}: the wording holds a NUL character")
    lines = text.split("\n")
    product, wording_version = _header(lines)
    chunks, skipped = _chunks(lines)
    if not chunks:
        raise WordingError("the wording has no clause")
    return WordingDocument(product, wording_version, chunks, skipped)
