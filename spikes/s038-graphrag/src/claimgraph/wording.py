"""What a wording says about itself, read from its text alone.

The clauses are cut by the platform's own parser, so a clause here is a clause
the hybrid search indexes. Three recognisers read the text:

- a cross-reference is ``clause n.m`` anywhere in a clause body, in any case;
- an exclusion names its perils in its own sentence, ``This exclusion applies
  to claims for a, b and c``, which ends at a comma-and-which or a full stop;
- a cover clause (a clause in the section titled "What is covered") and a
  documents clause (a title that starts "Documents for") name their peril in
  the title, which is the peril's name with spaces or hyphens for underscores.

Text between a section heading and its first clause belongs to no clause: the
parser drops it, and so do these recognisers. They count what it holds.
"""

import re
from dataclasses import dataclass

from meridian.platform.knowledge_mcp.chunking import Chunk, parse_wording

COVER_SECTION = "what is covered"
EXCLUSION_SECTION = "what is not covered"

REFERENCE = re.compile(r"\bclause\s+(\d+\.\d+)", re.IGNORECASE)
EXCLUSION_SENTENCE = re.compile(
    r"This exclusion applies to claims for (.+?)(?:, which|\.)"
)
PERIL_SEPARATOR = re.compile(r",\s*|\s+and\s+")
DOCUMENTS_TITLE = re.compile(r"documents for (.+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClauseFacts:
    number: str
    title: str
    section_number: str
    section_title: str
    body: str
    references: tuple[str, ...]
    excluded_perils: tuple[str, ...]
    is_cover: bool
    is_exclusion: bool
    documents_peril: str | None


@dataclass(frozen=True, slots=True)
class WordingFacts:
    product: str
    wording_version: str
    clauses: tuple[ClauseFacts, ...]
    introduction_references: int


def slug(text: str) -> str:
    """A peril's name: lower case, runs of other characters as one underscore."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def references(body: str) -> tuple[str, ...]:
    """The clause numbers a body names, each once, in order of appearance."""
    return tuple(dict.fromkeys(REFERENCE.findall(body)))


def excluded_perils(body: str) -> tuple[str, ...]:
    """The perils the exclusion sentence of a body names, or () without one."""
    found = EXCLUSION_SENTENCE.search(" ".join(body.split()))
    if found is None:
        return ()
    return tuple(slug(p) for p in PERIL_SEPARATOR.split(found[1]) if p)


def _introduction_references(text: str) -> int:
    """References in the lines between a section heading and its first clause."""
    introduction: list[str] = []
    collecting = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            collecting = False
        elif line.startswith("## "):
            collecting = True
        elif collecting:
            introduction.append(line)
    return len(REFERENCE.findall(" ".join(introduction)))


def _clause_facts(chunk: Chunk) -> ClauseFacts:
    section = chunk.section
    perils = excluded_perils(chunk.body)
    documents = DOCUMENTS_TITLE.fullmatch(chunk.title)
    return ClauseFacts(
        number=chunk.clause,
        title=chunk.title,
        section_number=chunk.clause.split(".")[0],
        section_title=section,
        body=chunk.body,
        references=references(chunk.body),
        excluded_perils=perils,
        is_cover=section.casefold() == COVER_SECTION,
        is_exclusion=section.casefold() == EXCLUSION_SECTION,
        documents_peril=slug(documents[1]) if documents else None,
    )


def read_wording(text: str) -> WordingFacts:
    """The facts of one wording's text."""
    document = parse_wording(text)
    clauses = tuple(_clause_facts(chunk) for chunk in document.chunks)
    return WordingFacts(
        product=document.product,
        wording_version=document.wording_version,
        clauses=clauses,
        introduction_references=_introduction_references(text),
    )
