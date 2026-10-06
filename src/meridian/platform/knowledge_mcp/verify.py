"""Compare the stored clauses with the manifest-verified wordings (S067, T-57).

Ingestion verifies each wording against its manifest before it writes the
clauses, and nothing read the stored clauses back: a clause rewritten afterwards
by anyone who can write the table stayed in the store and in every search. This
module reads the wordings as the ingestion does (``verified_documents``: the
same refusals, by the same code), cuts them the same way, reads the stored
clauses and says where the two differ, per ``(product, wording version,
clause)``.

A difference names the clause and its kind, never a text: the stored body is the
very thing that may have been rewritten, and the manifest's is content (a message
that quotes either could reach a terminal or a log). A stored name that is not
shaped like a product code, a version or a clause number is shown as ``?``: only
the stored text is untrusted here, and a name is text too. The check is read
only. It finds a change only when it is run: it is not a check at search time,
where the injection suite rewrites stored clauses on purpose.
"""

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg

from meridian.platform.common.audit import AuditEvent
from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp.ingest import (
    AUDIT_REFERENCE,
    AUDIT_SERVICE,
    IngestError,
    verified_documents,
)

AUDIT_EVENT = "knowledge.verify"
# The stored clauses are read through a named (server-side) cursor, this many
# rows at a time.
CURSOR_NAME = "verify_chunks"
FETCH_ROWS = 200
# The word of the audit row of a check that a database error stopped.
DATABASE_ERROR_REASON = "database-error"
SELECT_STORED = (
    "SELECT product, wording_version, clause, section, title, body, source_sha256 "
    "FROM knowledge.chunks"
)
# A stored name is shown only when it has the shape chunking and the table give
# a real one (chunking.HEADER_LINE, the clause check of migration 0005).
PRODUCT_SHAPE = re.compile(r"[A-Z0-9-]{1,32}")
VERSION_SHAPE = re.compile(r"[A-Za-z0-9-]{1,16}")
CLAUSE_SHAPE = re.compile(r"[1-9][0-9]?\.[1-9][0-9]?")
UNSHOWN = "?"

Kind = Literal[
    "body-differs",
    "title-differs",
    "section-differs",
    "source-hash-differs",
    "not-in-manifest",
    "not-stored",
]
# What a stored row holds that the manifest's chunk also gives, by column.
COMPARED: tuple[tuple[str, Kind], ...] = (
    ("section", "section-differs"),
    ("title", "title-differs"),
    ("body", "body-differs"),
)

Key = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Difference:
    """One clause and the kind of difference; the key is the stored one's as it
    may be shown (``?`` for a name that is not shaped like a real one)."""

    product: str
    wording_version: str
    clause: str
    kind: Kind

    @property
    def line(self) -> str:
        return (
            f"DIFFERENCE {self.product} {self.wording_version} "
            f"{self.clause} {self.kind}"
        )


@dataclass(frozen=True, slots=True)
class Verification:
    """``clauses`` is what the wordings give, ``stored`` what the table holds."""

    clauses: int
    stored: int
    differences: tuple[Difference, ...]

    @property
    def counts(self) -> str:
        return (
            f"clauses={self.clauses} stored={self.stored} "
            f"differences={len(self.differences)}"
        )


def _shown(value: str, shape: re.Pattern[str]) -> str:
    return value if shape.fullmatch(value) else UNSHOWN


def _difference(key: Key, kind: Kind) -> Difference:
    product, version, clause = key
    return Difference(
        _shown(product, PRODUCT_SHAPE),
        _shown(version, VERSION_SHAPE),
        _shown(clause, CLAUSE_SHAPE),
        kind,
    )


def _order(difference: Difference) -> tuple:
    parts = difference.clause.split(".")
    numbers = tuple(int(part) for part in parts if part.isdigit())
    return (
        difference.product,
        difference.wording_version,
        numbers,
        difference.clause,
        difference.kind,
    )


def _expected(source: Path) -> dict[Key, dict[str, str]]:
    """What the store should hold: each chunk's columns by clause key."""
    expected: dict[Key, dict[str, str]] = {}
    for sha256, document in verified_documents(source):
        for chunk in document.chunks:
            key = (document.product, document.wording_version, chunk.clause)
            expected[key] = {
                "section": chunk.section,
                "title": chunk.title,
                "body": chunk.body,
                "source_sha256": sha256,
            }
    return expected


def _kinds(want: dict[str, str], row: tuple[str, ...]) -> list[Kind]:
    """The kinds of difference between a stored row's four columns (in the
    order of ``SELECT_STORED`` after the key) and what the manifest gives."""
    section, title, body, source_sha256 = row
    have = {"section": section, "title": title, "body": body}
    kinds = [kind for column, kind in COMPARED if want[column] != have[column]]
    if want["source_sha256"] != source_sha256:
        kinds.append("source-hash-differs")
    return kinds


def _compare_stored(
    conn: psycopg.Connection, expected: dict[Key, dict[str, str]]
) -> tuple[int, list[Difference]]:
    """The number of stored clauses and the differences, comparing each row as it
    arrives from a server-side cursor, so that what is held is the manifest's
    clauses and the differences found, never the store."""
    found: list[Difference] = []
    seen: set[Key] = set()
    stored = 0
    with conn.cursor(name=CURSOR_NAME) as cursor:
        cursor.itersize = FETCH_ROWS
        cursor.execute(SELECT_STORED)
        for product, version, clause, *row in cursor:
            stored += 1
            key = (product, version, clause)
            if key not in expected:
                found.append(_difference(key, "not-in-manifest"))
                continue
            seen.add(key)
            found += [_difference(key, kind) for kind in _kinds(expected[key], row)]
    found += [_difference(key, "not-stored") for key in expected.keys() - seen]
    return stored, sorted(found, key=_order)


def verify_wordings(conn: psycopg.Connection, source: Path) -> Verification:
    """Compare the stored clauses with the wordings under ``source``. Reads the
    store and writes nothing; leaves the read transaction open for the caller to
    end. Raises ``IngestError`` when the wordings are refused, as the ingestion
    refuses them. The store is read through a server-side cursor, ``FETCH_ROWS``
    rows at a time: memory is the manifest's clauses and the differences found,
    whatever the store holds (read whole, 100,000 rows of 800 characters took
    316 MB, and the ingestion Job's limit is 192Mi)."""
    expected = _expected(source)
    stored, differences = _compare_stored(conn, expected)
    return Verification(len(expected), stored, tuple(differences))


def verification_event(result: Verification, run_id: uuid.UUID) -> AuditEvent:
    """The audit row of a check that ran: identifiers and counts, no text. The
    ingestion's tenant is the caller's and the check calls nobody, so none."""
    return AuditEvent(
        service=AUDIT_SERVICE,
        event=AUDIT_EVENT,
        outcome="differs" if result.differences else "verified",
        agent=INGESTION_AGENT,
        run_id=run_id,
        reference=AUDIT_REFERENCE,
        reason=result.counts,
    )


def _refused(run_id: uuid.UUID, reason: str) -> AuditEvent:
    return AuditEvent(
        service=AUDIT_SERVICE,
        event=AUDIT_EVENT,
        outcome="refused",
        agent=INGESTION_AGENT,
        run_id=run_id,
        reference=AUDIT_REFERENCE,
        reason=reason,
    )


def refusal_event(error: IngestError, run_id: uuid.UUID) -> AuditEvent:
    """The audit row of a check the wordings' refusal stopped: the reason word
    only, as the ingestion's refusal row has it."""
    return _refused(run_id, error.reason)


def database_error_event(run_id: uuid.UUID) -> AuditEvent:
    """The audit row of a check a database error stopped inside the check: a
    fixed word, never the server's message."""
    return _refused(run_id, DATABASE_ERROR_REASON)
