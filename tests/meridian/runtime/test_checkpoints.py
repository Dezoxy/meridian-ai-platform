"""The PostgreSQL saver the runtime opens (S015): its connection, and that it
keeps LangGraph's strict msgpack mode."""

import secrets
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from meridian.platform.common.db import CONNECT_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_MS
from meridian.runtime import SERVICE_NAME, runs
from meridian.runtime.checkpoints import open_saver


@dataclass
class Outsider:
    """A type that is not on LangGraph's allowlist of types to revive."""

    secret: str


def config_of(thread: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}


def put_outsider(saver: PostgresSaver, thread: str) -> dict[str, Any]:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"claim": Outsider("value")}
    checkpoint["channel_versions"] = {"claim": "1"}
    return saver.put(
        config_of(thread), checkpoint, {"source": "input", "step": 0}, {"claim": "1"}
    )


def stored_value(saver: PostgresSaver, thread: str) -> Any:
    found = saver.get_tuple(config_of(thread))
    assert found is not None
    return found.checkpoint["channel_values"]["claim"]


def test_the_saver_has_a_connection_of_its_own_set_up_for_the_runtime(
    migrated_database: DatabaseHandle,
) -> None:
    with open_saver(migrated_database.dsn("agent_runtime")) as saver:
        conn = saver.conn
        settings = conn.execute(
            "SELECT current_setting('search_path') AS search_path, "
            "current_setting('statement_timeout') AS statement_timeout, "
            "current_setting('application_name') AS application_name, "
            "current_user AS role"
        ).fetchone()
        connect_timeout = conn.info.get_parameters()["connect_timeout"]

    assert conn.closed
    # dict rows, as the library's queries need; and the same limits as connect().
    assert settings == {
        "search_path": "runtime",
        "statement_timeout": f"{STATEMENT_TIMEOUT_MS // 1000}s",
        "application_name": SERVICE_NAME,
        "role": "agent_runtime",
    }
    assert connect_timeout == str(CONNECT_TIMEOUT_SECONDS)


def test_the_connection_commits_what_the_saver_writes(
    migrated_database: DatabaseHandle,
) -> None:
    thread = f"commit-{secrets.token_hex(4)}"
    with open_saver(migrated_database.dsn("agent_runtime")) as saver:
        put_outsider(saver, thread)
        assert saver.conn.autocommit is True

    with psycopg.connect(migrated_database.dsn(OWNER)) as conn:
        counts = conn.execute(
            "SELECT count(*) FROM runtime.checkpoints WHERE thread_id = %s", (thread,)
        ).fetchone()

    assert counts == (1,)


def test_the_connection_is_closed_when_the_block_raises(
    migrated_database: DatabaseHandle,
) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        open_saver(migrated_database.dsn("agent_runtime")) as saver,
    ):
        conn = saver.conn
        raise RuntimeError("boom")

    assert conn.closed


def test_a_database_that_refuses_the_connection_raises_a_psycopg_error() -> None:
    with (
        pytest.raises(psycopg.Error),
        open_saver("postgresql://agent_runtime@127.0.0.1:1/none"),
    ):
        pytest.fail("the block must not run")


def test_a_type_outside_the_allowlist_is_not_revived_by_the_runtimes_saver(
    migrated_database: DatabaseHandle,
) -> None:
    thread = f"strict-{uuid.uuid4()}"
    dsn = migrated_database.dsn("agent_runtime")
    with open_saver(dsn) as saver:
        put_outsider(saver, thread)

        revived = stored_value(saver, thread)

        # The control: the same stored bytes, read by a serializer that is
        # told to allow the type, do come back as it. So the test above can
        # only pass because of the allowlist, not because nothing was stored.
        permissive = PostgresSaver(
            saver.conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[Outsider])
        )
        allowed = stored_value(permissive, thread)
        saver.delete_thread(thread)

    assert not isinstance(revived, Outsider)
    assert revived == {"secret": "value"}
    assert allowed == Outsider("value")


class PauseState(TypedDict, total=False):
    answer: Any


def test_a_node_that_fails_after_its_pause_leaves_that_pause_pending_and_resumable(
    migrated_database: DatabaseHandle,
) -> None:
    """The premise of a resumed leg that fails leaving the run paused: a node
    that raises after ``interrupt`` returns leaves the thread's one interrupt
    pending, and resuming again addresses it and finishes the node, whose work
    then happens once. What the node reads is the first resume's value (see
    the last assertions)."""
    work: list[Any] = []
    tool_works = False

    def decide(state: PauseState) -> PauseState:
        answer = interrupt("approve?")
        if not tool_works:
            raise RuntimeError("the tool is down")
        work.append(answer)
        return {"answer": answer}

    builder = StateGraph(PauseState)
    builder.add_node("decide", decide)
    builder.add_edge(START, "decide")
    builder.add_edge("decide", END)
    config = {"configurable": {"thread_id": f"premise-{uuid.uuid4()}"}}
    with open_saver(migrated_database.dsn("agent_runtime")) as saver:
        graph = builder.compile(checkpointer=saver)
        try:
            graph.invoke({}, config, durability="sync")
            (paused,) = graph.get_state(config).interrupts

            with pytest.raises(RuntimeError, match="the tool is down"):
                graph.invoke(
                    runs._resume_command(graph, config, {"ok": 1}),
                    config,
                    durability="sync",
                )
            (still_pending,) = graph.get_state(config).interrupts
            tool_works = True
            graph.invoke(
                runs._resume_command(graph, config, {"ok": 2}),
                config,
                durability="sync",
            )
            finished = graph.get_state(config)
        finally:
            saver.delete_thread(config["configurable"]["thread_id"])

    assert still_pending.id == paused.id
    assert still_pending.value == "approve?"
    assert finished.interrupts == ()
    # The first resume's value was stored with the pause's checkpoint, so the
    # node reads it again; the second resume's value is not what it reads.
    assert finished.values == {"answer": {"ok": 1}}
    assert work == [{"ok": 1}]
