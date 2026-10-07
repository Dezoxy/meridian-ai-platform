"""The files a claim holds, as the pages list them (S070).

Moved out of ``uploads.py``: the adjuster's and the claimant's pages read the
list, and ``uploads.py`` imports the adjuster's module (the claim ID, the
cross-site check), so the list cannot live there without an import cycle. This
module imports nothing of the Claims API. It selects no content: the one query
that does is the download's, in ``uploads.py``.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg

# The audit event of a download of a file (``file_download`` writes it; the
# adjuster's page counts it in one line instead of listing it).
DOWNLOAD_EVENT = "claim.file_downloaded"

LIST_FILES_SQL = (
    "SELECT f.file_id, f.kind, f.media_type, f.size_bytes, encode(f.sha256, 'hex'), "
    "f.received_at FROM claims.claim_files AS f "
    "JOIN claims.claims AS c ON c.claim_id = f.claim_id "
    "WHERE f.claim_id = %s AND c.tenant = %s ORDER BY f.received_at, f.file_id"
)


@dataclass(frozen=True, slots=True)
class FileSummary:
    """One file of a claim, as a page lists it."""

    file_id: uuid.UUID
    kind: str
    media_type: str
    size_bytes: int
    sha256: str
    received_at: datetime


def list_files(
    conn: psycopg.Connection, tenant: str, claim_id: str
) -> tuple[FileSummary, ...]:
    """The claim's files in arrival order: none for a claim the tenant does not
    have. Never the content."""
    rows = conn.execute(LIST_FILES_SQL, (claim_id, tenant)).fetchall()
    return tuple(FileSummary(*row) for row in rows)


# The type and size of one file, for a HEAD of the download: by claim and
# identifier, found with the claim's tenant, and no content column.
HEAD_FILE_SQL = (
    "SELECT f.media_type, f.size_bytes FROM claims.claim_files AS f "
    "JOIN claims.claims AS c ON c.claim_id = f.claim_id "
    "WHERE f.claim_id = %s AND f.file_id = %s AND c.tenant = %s"
)


@dataclass(frozen=True, slots=True)
class FileHead:
    """One file's stored type and size: what a HEAD answers with."""

    media_type: str
    size_bytes: int


def file_head(
    conn: psycopg.Connection, tenant: str, claim_id: str, file_id: uuid.UUID
) -> FileHead | None:
    """The file's type and size, by claim and identifier: ``None`` unless the
    tenant's claim holds that file. Never the content."""
    row = conn.execute(HEAD_FILE_SQL, (claim_id, file_id, tenant)).fetchone()
    return None if row is None else FileHead(*row)


def size_text(size_bytes: int) -> str:
    """A file's size as a page says it."""
    return f"{size_bytes:,} bytes"
