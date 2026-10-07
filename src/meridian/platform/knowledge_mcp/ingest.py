"""Ingest the synthetic policy wordings into the knowledge store (S012).

Everything is checked before anything is embedded or written, and everything is
embedded before anything is written, so a refusal or a failure on the last
batch leaves the store as it was. The order is the registry (T-60), the
manifest (T-57, hard rule 2), the parse, the embedding, then one replace of the
corpus and one audit row in the caller's transaction (the caller commits).

An error names a file, a line or a rule, never a document: a wording is
content, and a message that quotes it could reach a terminal or a log. The
audit row carries identifiers and counts only; the text and the vectors are in
the table and nowhere else. This module logs nothing and writes nothing on a
refusal: ``IngestError`` carries a ``reason`` word, and ``refusal_event`` is the
row the caller may write for it (``meridian knowledge ingest`` does).
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp.chunking import (
    Chunk,
    WordingDocument,
    WordingError,
    parse_wording,
)
from meridian.platform.knowledge_mcp.embedding_client import (
    HTTP_TOO_MANY_REQUESTS,
    EmbeddingBatch,
    EmbeddingCallError,
    EmbeddingClient,
)
from meridian.platform.knowledge_mcp.store import ChunkRow, replace_corpus
from meridian.platform.registry import Registry

MANIFEST_FILE = "manifest.json"
WORDINGS_DIR = "wordings"
WORDING_KEY = re.compile(rf"{WORDINGS_DIR}/[^/]+\.md")
# The gateway takes 1 to 16 texts in a call.
BATCH_SIZE = 16
# A 429 is waited out: what the gateway asks for (5 s when it does not say),
# never less than a second (a wait of nothing would never end the loop) and
# never more than a minute at a time, and the waits of one ingestion stop at
# five minutes. The gateway sends no Retry-After when a budget is used up, and
# waiting does not help then: a batch whose 429 names no wait is retried twice.
DEFAULT_WAIT_SECONDS = 5.0
MIN_WAIT_SECONDS = 1.0
MAX_WAIT_SECONDS = 60.0
MAX_TOTAL_WAIT_SECONDS = 300.0
MAX_UNANNOUNCED_RETRIES = 2
# What the knowledge store holds is searched by claims workloads on behalf of
# any tenant, so only a tenant whose data may go nowhere an internal document
# may not go can ingest it (T-60).
STORE_DATA_CLASS = "internal"
AUDIT_SERVICE = "knowledge-ingestion"
AUDIT_EVENT = "knowledge.ingest"
AUDIT_REFERENCE = "wordings"


Reason = Literal[
    "registry-refused",
    "manifest-refused",
    "wording-refused",
    "gateway-failed",
    "gateway-busy",
    "not-transactional",
]


class IngestError(Exception):
    """The wordings cannot be ingested. The message is for the operator and
    quotes no document; ``reason`` is the word an audit row carries instead."""

    def __init__(self, message: str, reason: Reason) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IngestCounts:
    documents: int
    chunks: int
    input_tokens: int
    deployment: str
    model: str
    dimensions: int


@dataclass(frozen=True, slots=True)
class _Wording:
    name: str
    sha256: str
    document: WordingDocument


@dataclass(frozen=True, slots=True)
class _Item:
    wording: _Wording
    chunk: Chunk

    @property
    def text(self) -> str:
        return f"{self.chunk.title}\n{self.chunk.body}"


# ── 1. the registry ─────────────────────────────────────────────────────────
def _check_registry(registry: Registry, tenant: str) -> None:
    found = registry.tenant(tenant)
    if found is None:
        raise IngestError(
            f"tenant {tenant!r} is not in the registry", "registry-refused"
        )
    if not registry.tenant_may_run(tenant, INGESTION_AGENT):
        raise IngestError(
            f"tenant {tenant!r} may not run agent {INGESTION_AGENT!r}",
            "registry-refused",
        )
    reach = registry.data_class(found.data_class)
    allowed = registry.data_class(STORE_DATA_CLASS)
    if reach is None or allowed is None:
        raise IngestError(
            "the registry has no residency policy for the data class",
            "registry-refused",
        )
    outside = sorted(set(reach.residency) - set(allowed.residency))
    if outside:
        raise IngestError(
            f"tenant {tenant!r} has data class {found.data_class!r}, which may "
            f"reach residency {', '.join(outside)}; an {STORE_DATA_CLASS} "
            "document may not",
            "registry-refused",
        )


# ── 2. the manifest ─────────────────────────────────────────────────────────
def _read(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise IngestError(
            f"{label} cannot be read from the source directory", "manifest-refused"
        ) from exc


def _manifest(source: Path) -> dict[str, str]:
    """The hashes the manifest lists for wordings, by key; the manifest must say
    the data is synthetic (hard rule 2)."""
    try:
        manifest = json.loads(_read(source / MANIFEST_FILE, MANIFEST_FILE))
    except ValueError as exc:
        raise IngestError(
            f"{MANIFEST_FILE} is not valid JSON", "manifest-refused"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("synthetic") is not True:
        raise IngestError(
            f"{MANIFEST_FILE} does not say the data is synthetic; only the "
            "generator's output may be loaded (hard rule 2)",
            "manifest-refused",
        )
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise IngestError(f"{MANIFEST_FILE} lists no files", "manifest-refused")
    return {
        key: value
        for key, value in files.items()
        if isinstance(key, str) and WORDING_KEY.fullmatch(key)
    }


def _wording_files(source: Path) -> dict[str, Path]:
    directory = source / WORDINGS_DIR
    return {
        f"{WORDINGS_DIR}/{path.name}": path for path in sorted(directory.glob("*.md"))
    }


def _verified_wordings(source: Path) -> list[tuple[str, str, str]]:
    """``(key, sha256, text)`` of each wording, in file-name order, when the
    manifest lists exactly the wordings in the directory with their hashes."""
    listed = _manifest(source)
    present = _wording_files(source)
    missing = sorted(listed.keys() - present.keys())
    if missing:
        raise IngestError(
            f"{missing[0]} is listed in {MANIFEST_FILE} but is missing",
            "manifest-refused",
        )
    unlisted = sorted(present.keys() - listed.keys())
    if unlisted:
        raise IngestError(
            f"{unlisted[0]} is not listed in {MANIFEST_FILE}", "manifest-refused"
        )
    if not present:
        raise IngestError(
            f"the source holds no wording under {WORDINGS_DIR}/", "manifest-refused"
        )
    verified: list[tuple[str, str, str]] = []
    for key, path in present.items():
        data = _read(path, key)
        digest = hashlib.sha256(data).hexdigest()
        if digest != listed[key]:
            raise IngestError(
                f"{key} does not match its hash in {MANIFEST_FILE}; "
                "regenerate it with make synthetic",
                "manifest-refused",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise IngestError(f"{key} is not valid UTF-8", "manifest-refused") from None
        verified.append((key, digest, text))
    return verified


# ── 3. the parse ────────────────────────────────────────────────────────────
def _parse_all(source: Path) -> list[_Wording]:
    wordings: list[_Wording] = []
    seen: set[tuple[str, str]] = set()
    for key, digest, text in _verified_wordings(source):
        try:
            document = parse_wording(text)
        except WordingError as exc:
            raise IngestError(f"{key}: {exc}", "wording-refused") from exc
        identity = (document.product, document.wording_version)
        if identity in seen:
            raise IngestError(
                f"{key} has the same product code and wording version as "
                "another wording",
                "wording-refused",
            )
        seen.add(identity)
        wordings.append(_Wording(key, digest, document))
    return wordings


# ── 4. the embedding ────────────────────────────────────────────────────────
def _wait_for(error: EmbeddingCallError) -> float:
    asked = (
        DEFAULT_WAIT_SECONDS
        if error.retry_after_seconds is None
        else error.retry_after_seconds
    )
    return min(max(asked, MIN_WAIT_SECONDS), MAX_WAIT_SECONDS)


def _failure_message(error: EmbeddingCallError) -> str:
    """A status the gateway answered is a refusal; no status is a call that
    failed in transit or was answered with something that is not the contract.
    A 503 adds the fixed word of the gateway's refusal (``unknown`` when it is
    not one of the gateway's four): the Job's last line says which one it met."""
    if error.gateway_word is not None:
        return (
            "the model gateway refused the embedding call "
            f"({error}; kind {error.gateway_word})"
        )
    if error.status_code:
        return f"the model gateway refused the embedding call ({error})"
    return f"the embedding call to the model gateway failed ({error})"


class _Embedder:
    """Calls the gateway for one batch at a time and waits out its 429s, within
    one budget of waiting for the whole ingestion."""

    def __init__(self, client: EmbeddingClient, sleep: Callable[[float], None]) -> None:
        self._client = client
        self._sleep = sleep
        self._waited = 0.0

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        unannounced = 0
        while True:
            try:
                return self._client.embed(texts)
            except EmbeddingCallError as exc:
                if exc.status_code != HTTP_TOO_MANY_REQUESTS:
                    raise IngestError(_failure_message(exc), "gateway-failed") from None
                if exc.retry_after_seconds is None:
                    if unannounced == MAX_UNANNOUNCED_RETRIES:
                        raise IngestError(
                            "the model gateway kept answering 429 without a "
                            f"Retry-After; gave up after {unannounced} retries",
                            "gateway-busy",
                        ) from None
                    unannounced += 1
                wait = _wait_for(exc)
                if self._waited + wait > MAX_TOTAL_WAIT_SECONDS:
                    raise IngestError(
                        "the model gateway kept answering 429; gave up after "
                        f"waiting {self._waited:g} s",
                        "gateway-busy",
                    ) from None
                self._sleep(wait)
                self._waited += wait


def _batches(items: Sequence[_Item]) -> Iterator[Sequence[_Item]]:
    for start in range(0, len(items), BATCH_SIZE):
        yield items[start : start + BATCH_SIZE]


def _embed_all(
    items: Sequence[_Item], embedder: _Embedder
) -> tuple[list[tuple[float, ...]], list[EmbeddingBatch]]:
    vectors: list[tuple[float, ...]] = []
    batches: list[EmbeddingBatch] = []
    for batch_items in _batches(items):
        batch = embedder.embed([item.text for item in batch_items])
        first = batches[0] if batches else batch
        if (batch.deployment, batch.model, batch.dimensions) != (
            first.deployment,
            first.model,
            first.dimensions,
        ):
            raise IngestError(
                "the batches were embedded by different deployments, models or "
                "dimensions; their vectors are not comparable",
                "gateway-failed",
            )
        batches.append(batch)
        vectors.extend(batch.vectors)
    return vectors, batches


# ── 5. the write ────────────────────────────────────────────────────────────
def _rows(
    items: Sequence[_Item], vectors: Sequence[tuple[float, ...]], first: EmbeddingBatch
) -> list[ChunkRow]:
    return [
        ChunkRow(
            product=item.wording.document.product,
            wording_version=item.wording.document.wording_version,
            clause=item.chunk.clause,
            section=item.chunk.section,
            title=item.chunk.title,
            body=item.chunk.body,
            source_sha256=item.wording.sha256,
            deployment=first.deployment,
            model=first.model,
            dimensions=first.dimensions,
            embedding=vector,
        )
        for item, vector in zip(items, vectors, strict=True)
    ]


def refusal_event(
    error: IngestError, client: EmbeddingClient, registry: Registry
) -> AuditEvent:
    """The audit row of a refused ingestion: the reason word, the agent and the
    run, and the tenant only when the registry knows it (the name is the
    caller's, and an audit column should not hold what anyone typed). No message,
    file name or wording text. The library writes nothing; the caller does."""
    known = registry.tenant(client.tenant) is not None
    return AuditEvent(
        service=AUDIT_SERVICE,
        event=AUDIT_EVENT,
        outcome="refused",
        tenant=client.tenant if known else None,
        agent=INGESTION_AGENT,
        run_id=client.run_id,
        reference=AUDIT_REFERENCE,
        reason=error.reason,
    )


def ingest_wordings(
    conn: psycopg.Connection,
    source: Path,
    client: EmbeddingClient,
    registry: Registry,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> IngestCounts:
    """Replace the knowledge store with the wordings under ``source``, in the
    caller's transaction (the caller commits).

    ``conn`` must not be in autocommit mode: the delete and the inserts are one
    transaction, so a search sees the old corpus or the new one (T-58).
    ``client`` carries the tenant and the agent the embedding calls go out
    under, and the run ID the calls and the audit row share: the registry is
    checked for those values and no others. Raises ``IngestError`` before any
    write when the connection is not transactional, the client is not the
    ingestion agent's, or the registry, the manifest, a wording or the gateway
    refuses.
    """
    if conn.autocommit:
        raise IngestError(
            "the connection commits each statement by itself; the replace of the "
            "corpus must be one transaction",
            "not-transactional",
        )
    if client.agent != INGESTION_AGENT:
        raise IngestError(
            f"the embedding client must run as agent {INGESTION_AGENT!r}",
            "registry-refused",
        )
    tenant = client.tenant
    _check_registry(registry, tenant)
    wordings = _parse_all(source)
    items = [_Item(w, chunk) for w in wordings for chunk in w.document.chunks]
    vectors, batches = _embed_all(items, _Embedder(client, sleep))
    first = batches[0]
    replace_corpus(conn, _rows(items, vectors, first))
    input_tokens = sum(batch.input_tokens for batch in batches)
    record_event(
        conn,
        AuditEvent(
            service=AUDIT_SERVICE,
            event=AUDIT_EVENT,
            outcome="completed",
            tenant=tenant,
            agent=INGESTION_AGENT,
            run_id=client.run_id,
            reference=AUDIT_REFERENCE,
            deployment=first.deployment,
            model=first.model,
            input_tokens=input_tokens,
        ),
    )
    return IngestCounts(
        documents=len(wordings),
        chunks=len(items),
        input_tokens=input_tokens,
        deployment=first.deployment,
        model=first.model,
        dimensions=first.dimensions,
    )
