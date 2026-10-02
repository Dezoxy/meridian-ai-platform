"""Helpers shared by the tests of the knowledge ingestion (S012)."""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dbsupport import OWNER, DatabaseHandle
from fastapi.testclient import TestClient
from servicesupport import REPO_ROOT, FakeClock, owner_rows

from meridian.platform.common.db import connect
from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp.embedding_client import EmbeddingClient
from meridian.platform.knowledge_mcp.ingest import IngestCounts, ingest_wordings
from meridian.platform.registry import Registry

REAL_SOURCE = REPO_ROOT / "data" / "synthetic"
WORDING_NAMES = ("HOME-PLUS", "HOME-STD", "MOTOR-COMP", "MOTOR-TPL")
TENANT = "claims-triage"
CANARY = "CANARY-wording-text-2290"

SELECT_CHUNKS = (
    "SELECT product, wording_version, clause, section, title, body, source_sha256, "
    "deployment, model, dimensions, embedding::text, ingested_at "
    "FROM knowledge.chunks ORDER BY product, wording_version, clause"
)

Sleep = Callable[[float], None]
# The ``source`` fixture's builder (conftest.py).
Source = Callable[..., Path]


@dataclass(slots=True)
class Gateway:
    """The gateway app in replay mode over the test's database, on a fake
    clock the tests move by hand."""

    http: TestClient
    clock: FakeClock
    registry: Registry
    database: DatabaseHandle


def chunk_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(db, SELECT_CHUNKS)


def chunk_count(db: DatabaseHandle) -> int:
    ((count,),) = owner_rows(db, "SELECT count(*) FROM knowledge.chunks")
    return count


def audit_row_count(db: DatabaseHandle) -> int:
    ((count,),) = owner_rows(db, "SELECT count(*) FROM audit.events")
    return count


def embedding_reply(
    count: int,
    *,
    deployment: str = "replay-embedding",
    model: str = "replay-embedding",
    dimensions: int = 3,
    input_tokens: int = 5,
) -> dict[str, Any]:
    """What the gateway answers, for a stand-in that is not the gateway."""
    return {
        "call_id": str(uuid.uuid4()),
        "mode": "replay",
        "deployment": deployment,
        "provider": "replay",
        "model": model,
        "dimensions": dimensions,
        "embeddings": [[(index + 1) / 8] * dimensions for index in range(count)],
        "usage": {"input_tokens": input_tokens},
    }


# A script answers the call with this index (from 0) and these inputs, or
# returns None to let the stand-in give its good answer.
Script = Callable[[int, list[str]], httpx.Response | None]


@dataclass
class ScriptedGateway:
    """A stand-in for the gateway's ``/v1/embeddings`` that records what it was
    asked and answers as its script says."""

    script: Script | None = None
    requests: list[list[str]] = field(default_factory=list)
    headers: list[httpx.Headers] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["inputs"]
        index = len(self.requests)
        self.requests.append(inputs)
        self.headers.append(request.headers)
        if self.script is not None:
            scripted = self.script(index, inputs)
            if scripted is not None:
                return scripted
        return httpx.Response(200, json=embedding_reply(len(inputs)))

    def http(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(self)
        )


def too_many(retry_after: str | None = None) -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(429, json={"detail": "rate limit"}, headers=headers)


@dataclass
class Waits:
    """The injected ``sleep``: records the waits and never sleeps. ``also`` runs
    with each wait, for a test that moves a fake clock."""

    seconds: list[float] = field(default_factory=list)
    also: Sleep | None = None

    def __call__(self, wait: float) -> None:
        self.seconds.append(wait)
        if self.also is not None:
            self.also(wait)


def ingest(
    db: DatabaseHandle,
    http: httpx.Client,
    registry: Registry,
    *,
    tenant: str = TENANT,
    agent: str = INGESTION_AGENT,
    source: Path = REAL_SOURCE,
    sleep: Sleep | None = None,
    run_id: uuid.UUID | None = None,
    commit: bool = True,
) -> tuple[IngestCounts, uuid.UUID]:
    """Ingest as the owner role in one transaction and commit, as the CLI does
    (or roll back when ``commit`` is false); return the counts and the run ID
    the calls carried. The tenant and the agent are the client's."""
    run_id = run_id or uuid.uuid4()
    client = EmbeddingClient(http, tenant=tenant, agent=agent, run_id=run_id)
    extra = {} if sleep is None else {"sleep": sleep}
    with connect(db.dsn(OWNER), "test-ingest") as conn:
        counts = ingest_wordings(conn, source, client, registry, **extra)
        if commit:
            conn.commit()
        else:
            conn.rollback()
    return counts, run_id
