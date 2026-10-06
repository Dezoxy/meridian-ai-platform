"""The connection the refusal tests pass in fails the test when anything is
executed on it (S065): "nothing was written" is true by construction."""

import uuid

import pytest
from knowledgesupport import (
    REAL_SOURCE,
    TENANT,
    RefusalOnlyConnection,
    ScriptedGateway,
    ingest_without_database,
)

from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp.embedding_client import EmbeddingClient
from meridian.platform.knowledge_mcp.ingest import IngestError, ingest_wordings
from meridian.platform.registry import Registry

REACHED = "a refusal test reached the database"
Failed = pytest.fail.Exception


def test_the_stand_in_commits_in_one_transaction_unless_it_is_told_otherwise() -> None:
    assert RefusalOnlyConnection().autocommit is False
    assert RefusalOnlyConnection(autocommit=True).autocommit is True


@pytest.mark.parametrize(
    "use",
    ["execute", "executemany", "cursor", "commit", "rollback", "transaction", "copy"],
)
def test_using_the_stand_in_in_any_way_fails_the_test_with_a_message_that_says_so(
    use: str,
) -> None:
    connection = RefusalOnlyConnection()

    with pytest.raises(Failed, match=REACHED) as failed:
        getattr(connection, use)

    assert use in str(failed.value)


def test_a_refusal_before_the_first_write_passes_through_the_stand_in(
    registry: Registry,
) -> None:
    with pytest.raises(IngestError) as raised:
        ingest_without_database(ScriptedGateway().http(), registry, tenant="ghost")

    assert raised.value.reason == "registry-refused"


def test_a_run_that_reaches_the_write_fails_the_test_instead_of_passing_quietly(
    registry: Registry,
) -> None:
    gateway = ScriptedGateway()
    client = EmbeddingClient(
        gateway.http(), tenant=TENANT, agent=INGESTION_AGENT, run_id=uuid.uuid4()
    )

    with pytest.raises(Failed, match=REACHED):
        ingest_wordings(RefusalOnlyConnection(), REAL_SOURCE, client, registry)

    assert gateway.requests  # every batch was embedded: only the write is refused
