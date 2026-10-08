#!/usr/bin/env python3
"""How a Markdown table is printed in the architecture PDF.

LaTeX cannot break a table row across pages. A row taller than a page runs
past the bottom margin and off the sheet, and the PDF loses that text without
a word; a register with paragraphs in its cells does this on most pages. Two
rules follow, both for print alone: the Markdown, GitHub and Structurizr's
Documentation tab keep the table as it is written.

  Records  A table with a cell longer than RECORD_CELL characters prints one
           block per row instead: the first cell in bold with the short
           columns beside it, then each long column as a paragraph under its
           name. A column is long when one of its cells is, so every block of
           a table has the same shape. Paragraphs break across pages and use
           the whole text width.

  Widths   Any other table that Pandoc will wrap gets its column widths from
           its text. Pandoc reads relative widths from the dashes of the
           separator line, and `|---|---|` gives a two-letter ID as much room
           as a sentence. A column is never narrower than its longest word,
           header included, so no word runs into the next column.

Tables inside a code fence are examples and are left alone. Only tables whose
lines start with a pipe are handled, which is how this repository writes
them: a table inside a block quote, or one written without the outer pipes, is
passed on as it is. A pipe inside a code span stays in its cell, as Pandoc
reads it.

build_architecture_pdf_source.py imports this module; the canonical copy
lives in development-base (scripts/) and repositories copy it unchanged.
"""

from __future__ import annotations

import re

# A cell longer than this makes its table print as records. A cell of 300
# characters in a column a sixth of the page wide is about 25 lines.
RECORD_CELL = 300
# A column whose cells all fit this length sits on a record's first line.
SHORT_CELL = 60
# Pandoc wraps a pipe table, and reads widths from its dashes, only when one
# of the table's lines is longer than its --columns, 72 by default.
WIDE_LINE = 72
# Characters of body text across the page, and the dashes shared out.
LINE_CHARS = 95
DASHES = 120
# No single word claims more than this share of the page for its column.
WORD_SHARE = 0.3

# A fence opens with three or more of one mark and closes on a line of at
# least as many of the same mark and nothing else.
FENCE_OPEN = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})\s*$")
ROW = re.compile(r"^\s*\|")
SEPARATOR = re.compile(r"^\s*\|(\s*:?-+:?\s*\|)+\s*$")
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
TICKS = re.compile(r"`+")
# What would make a paragraph a heading, a quote or a list if it began one.
BLOCK_START = re.compile(r"^(#|>|[-+*]\s|\d+[.)]\s)")


def cells(line: str) -> list[str]:
    """A row's cells, trimmed.

    A pipe ends a cell unless a backslash stands before it or it is inside a
    code span, which runs from a row of backticks to the next row as long.
    """
    row = line.strip()
    row = row[1:] if row.startswith("|") else row
    found: list[str] = []
    start = index = 0
    while index < len(row):
        char = row[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            ticks = TICKS.match(row, index).group()
            close = row.find(ticks, index + len(ticks))
            # A row of backticks with no partner is text, not a code span.
            index = close + len(ticks) if close != -1 else index + len(ticks)
            continue
        if char == "|":
            found.append(row[start:index])
            start = index + 1
        index += 1
    if row[start:].strip() or not found:
        found.append(row[start:])
    return [cell.strip() for cell in found]


def label(name: str) -> str:
    """A column's name without the emphasis its header gave it."""
    return name.strip().strip("*_").strip()


def inline(text: str) -> str:
    """Text that stays a paragraph where it starts one."""
    return "\\" + text if BLOCK_START.match(text) else text


def shown(cell: str) -> str:
    """The text a reader sees: a link by its text, code without its ticks."""
    return LINK.sub(r"\1", cell).replace("`", "").replace("\\|", "|")


def record(head: list[str], row: list[str], long: set[int]) -> list[str]:
    """One row as paragraphs: the ID and the short columns, then the long ones.

    `long` holds the columns that print as a paragraph under their name. It is
    decided for the table, not for the row, so every block has the same shape.
    """
    row = [cell.replace("\\|", "|") for cell in row]
    lead: list[str] = []
    paragraphs: list[str] = []
    for column, cell in enumerate(row):
        name = label(head[column]) if column < len(head) else ""
        if not cell:
            continue
        if column in long:
            paragraphs += [f"*{name}.* {cell}" if name else inline(cell), ""]
        elif column == 0:
            lead.append(cell if "**" in cell else f"**{cell}**")
        else:
            lead.append(f"{name}: {cell}" if name else cell)
    return ([inline(" · ".join(lead)), ""] if lead else []) + paragraphs


def separator(head: list[str], marks: list[str], rows: list[list[str]]) -> str:
    """A separator line whose dashes give each column its share of the page."""
    columns = len(marks)
    weight, floor = [], []
    for column in range(columns):
        texts = [shown(head[column])] if column < len(head) else []
        texts += [shown(row[column]) for row in rows if column < len(row)]
        mean = sum(len(text) for text in texts) / max(len(texts), 1)
        word = max((len(word) for text in texts for word in text.split()), default=1)
        # The square root keeps a long column from starving the short ones.
        weight.append(max(mean, 1) ** 0.5)
        floor.append(min((word + 2) / LINE_CHARS, WORD_SHARE))
    share = [0.0] * columns
    free = set(range(columns))
    # A column whose share falls under its longest word takes the word's
    # width; the others then share what is left, until nothing changes.
    while free:
        room = 1 - sum(share[column] for column in range(columns) if column not in free)
        total = sum(weight[column] for column in free)
        short = [c for c in free if room * weight[c] / total < floor[c]]
        if not short or room <= 0:
            for column in free:
                share[column] = max(room * weight[column] / total, 0.01)
            break
        for column in short:
            share[column] = floor[column]
            free.discard(column)
    scale = sum(share)
    parts = []
    for column, mark in enumerate(marks):
        dashes = "-" * max(3, round(DASHES * share[column] / scale))
        left = ":" if mark.startswith(":") else ""
        right = ":" if mark.endswith(":") else ""
        parts.append(f"{left}{dashes}{right}")
    return "|" + "|".join(parts) + "|"


def print_tables(lines: list[str]) -> list[str]:
    """The lines with every table outside a code fence made ready for print."""
    out: list[str] = []
    index = 0
    fence: str | None = None
    while index < len(lines):
        line = lines[index]
        if fence is None:
            if opening := FENCE_OPEN.match(line):
                fence = opening["marker"]
        elif (closing := FENCE_CLOSE.match(line)) and (
            closing["marker"][0] == fence[0] and len(closing["marker"]) >= len(fence)
        ):
            fence = None
            out.append(line)
            index += 1
            continue
        is_table = (
            fence is None
            and ROW.match(line)
            and index + 1 < len(lines)
            and SEPARATOR.match(lines[index + 1])
        )
        if not is_table:
            out.append(line)
            index += 1
            continue
        end = index + 2
        while end < len(lines) and ROW.match(lines[end]):
            end += 1
        head, marks = cells(line), cells(lines[index + 1])
        rows = [cells(row) for row in lines[index + 2 : end]]
        indent = line[: len(line) - len(line.lstrip())]
        if any(len(cell) > RECORD_CELL for row in rows for cell in row):
            long = {
                column
                for row in rows
                for column, cell in enumerate(row)
                if len(cell) > SHORT_CELL
            }
            # A block is a paragraph, and a paragraph starts after a blank line.
            if out and out[-1].strip():
                out.append("")
            for row in rows:
                blocks = record(head, row, long)
                out += [indent + text if text else "" for text in blocks]
            # The blank line that ended the table follows the last block.
            if out and out[-1] == "" and end < len(lines) and lines[end] == "":
                out.pop()
        elif any(len(text) > WIDE_LINE for text in lines[index:end]):
            out += [
                line,
                indent + separator(head, marks, rows),
                *lines[index + 2 : end],
            ]
        else:
            out += lines[index:end]
        index = end
    return out
