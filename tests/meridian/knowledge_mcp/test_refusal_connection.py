"""The connection the refusal tests pass in fails the test when anything is
executed on it (S065): "nothing was written" is true by construction."""

import typing
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


@pytest.mark.parametrize("name", ["__wrapped__", "__setstate__", "_mock_methods"])
def test_a_name_python_or_a_tool_probes_on_its_own_is_an_attribute_error(
    name: str,
) -> None:
    connection = RefusalOnlyConnection()

    with pytest.raises(AttributeError):
        getattr(connection, name)

    assert not hasattr(connection, name)


def test_a_with_block_on_the_stand_in_fails_the_test_instead_of_a_type_error() -> None:
    # A bare TypeError is an Exception, which code under test may swallow;
    # a failed test is not.
    with pytest.raises(Failed, match=REACHED) as failed, RefusalOnlyConnection():
        pytest.fail("the body of the with block ran")

    assert "__enter__" in str(failed.value)


def test_leaving_a_with_block_on_the_stand_in_fails_the_test_too() -> None:
    connection = RefusalOnlyConnection()

    with pytest.raises(Failed, match=REACHED) as failed:
        connection.__exit__(None, None, None)

    assert "__exit__" in str(failed.value)


def test_the_stand_ins_getattr_never_returns() -> None:
    hints = typing.get_type_hints(RefusalOnlyConnection.__getattr__)

    assert hints["return"] is typing.NoReturn


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
