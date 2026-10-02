"""The SQL of the knowledge store (S012). Placeholders only; the caller owns the
transaction.

The whole corpus is replaced in one go: the store mirrors its source, and a
search must never see half of an old ingestion and half of a new one.
"""

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import psycopg

# pgvector holds at most this many dimensions, in 4-byte floats.
PGVECTOR_MAX_DIMENSIONS = 16_000
FLOAT32_MAX = 3.4028234663852886e38
FLOAT32_MIN_NORMAL = 1.1754943508222875e-38

DELETE_CORPUS = "DELETE FROM knowledge.chunks"

INSERT_CHUNK = """
INSERT INTO knowledge.chunks (
    product, wording_version, clause, section, title, body, source_sha256,
    deployment, model, dimensions, embedding
) VALUES (
    %(product)s, %(wording_version)s, %(clause)s, %(section)s, %(title)s,
    %(body)s, %(source_sha256)s, %(deployment)s, %(model)s, %(dimensions)s,
    %(embedding)s::vector
)
"""


@dataclass(frozen=True, slots=True)
class ChunkRow:
    """One row of ``knowledge.chunks`` as the ingestion writes it."""

    product: str
    wording_version: str
    clause: str
    section: str
    title: str
    body: str
    source_sha256: str
    deployment: str
    model: str
    dimensions: int
    embedding: tuple[float, ...]


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form. ``repr`` of a float is the shortest text that reads
    back as the same number, so the value stored is the value computed (up to
    the 4-byte floats pgvector keeps)."""
    return "[" + ",".join(repr(component) for component in vector) + "]"


def usable_vector(vector: Sequence[float]) -> bool:
    """Whether pgvector 0.8.6 can compare ``vector`` by cosine distance.

    Not empty, not longer than pgvector holds, every component finite and inside
    the 4-byte float range (it refuses a literal outside it), and the sum of the
    squares in the 4-byte normal range. That last rule covers what the database
    does not refuse but cannot read: a component under 1e-45 is stored as 0, so
    a vector of them is all zero, and the distance from an all-zero vector is
    NaN; a norm that underflows reads as a distance of 0 or 1 whatever the
    other vector is, and one that overflows as NaN or 1 (probed 2026-10-02;
    ``tests/meridian/knowledge_mcp/test_vectors.py`` pins it).
    """
    if not 0 < len(vector) <= PGVECTOR_MAX_DIMENSIONS:
        return False
    if not all(math.isfinite(c) and abs(c) <= FLOAT32_MAX for c in vector):
        return False
    return FLOAT32_MIN_NORMAL <= math.fsum(c * c for c in vector) <= FLOAT32_MAX


def replace_corpus(conn: psycopg.Connection, rows: Sequence[ChunkRow]) -> None:
    """Delete every chunk and insert ``rows``, in the caller's transaction (the
    caller commits)."""
    parameters = [
        asdict(row) | {"embedding": vector_literal(row.embedding)} for row in rows
    ]
    with conn.cursor() as cursor:
        cursor.execute(DELETE_CORPUS)
        cursor.executemany(INSERT_CHUNK, parameters)
