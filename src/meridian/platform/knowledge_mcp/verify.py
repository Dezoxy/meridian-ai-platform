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


def _stored(conn: psycopg.Connection) -> dict[Key, dict[str, str]]:
    columns = ("section", "title", "body", "source_sha256")
    return {
        (product, version, clause): dict(zip(columns, rest, strict=True))
        for product, version, clause, *rest in conn.execute(SELECT_STORED)
    }


def _compare(
    expected: dict[Key, dict[str, str]], stored: dict[Key, dict[str, str]]
) -> list[Difference]:
    found = [_difference(key, "not-stored") for key in expected.keys() - stored.keys()]
    found += [
        _difference(key, "not-in-manifest") for key in stored.keys() - expected.keys()
    ]
    for key in expected.keys() & stored.keys():
        want, have = expected[key], stored[key]
        kinds = [kind for column, kind in COMPARED if want[column] != have[column]]
        if want["source_sha256"] != have["source_sha256"]:
            kinds.append("source-hash-differs")
        found += [_difference(key, kind) for kind in kinds]
    return sorted(found, key=_order)


def verify_wordings(conn: psycopg.Connection, source: Path) -> Verification:
    """Compare the stored clauses with the wordings under ``source``. Reads the
    store and writes nothing; leaves the read transaction open for the caller to
    end. Raises ``IngestError`` when the wordings are refused, as the ingestion
    refuses them."""
    expected = _expected(source)
    stored = _stored(conn)
    return Verification(len(expected), len(stored), tuple(_compare(expected, stored)))


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


def refusal_event(error: IngestError, run_id: uuid.UUID) -> AuditEvent:
    """The audit row of a check the wordings' refusal stopped: the reason word
    only, as the ingestion's refusal row has it."""
    return AuditEvent(
        service=AUDIT_SERVICE,
        event=AUDIT_EVENT,
        outcome="refused",
        agent=INGESTION_AGENT,
        run_id=run_id,
        reference=AUDIT_REFERENCE,
        reason=error.reason,
    )
