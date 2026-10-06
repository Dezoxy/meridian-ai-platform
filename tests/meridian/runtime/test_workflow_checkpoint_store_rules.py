"""What the store does not let through (S037, X1): a delete under a live run, and
text or numbers PostgreSQL's ``jsonb`` would refuse or change. Against the
database, because the rules are the database's."""

import dataclasses
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from agent_framework import WorkflowCheckpoint
from agent_framework.exceptions import WorkflowCheckpointException
from dbsupport import OWNER, DatabaseHandle
from servicesupport import owner_rows
from workflowsupport import CHECKPOINT_TYPES, Dials, MemoryStore, leg, start

from meridian.runtime.workflow_checkpoints import PostgresCheckpointStore

COUNT = "SELECT count(*) FROM runtime.workflow_checkpoints WHERE thread_id = %s"
ADD_RUN = (
    "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, status) "
    "VALUES (%s, %s, 'claim-brief', 'claims-triage', 'CLM-0001', %s)"
)
SET_STATUS = "UPDATE runtime.runs SET status = %s WHERE thread_id = %s"
REFUSED = "cannot save a checkpoint: CodecRefusal "


def store_for(db: DatabaseHandle, thread: uuid.UUID) -> PostgresCheckpointStore:
    return PostgresCheckpointStore(db.dsn("agent_runtime"), thread, CHECKPOINT_TYPES)


def rows_of(db: DatabaseHandle, thread: uuid.UUID) -> int:
    return owner_rows(db, COUNT, (str(thread),))[0][0]


def a_checkpoint(**changes: Any) -> WorkflowCheckpoint:
    """A real checkpoint (the pause of the small workflow), changed as asked and
    with an ID of its own."""
    memory = MemoryStore()
    start(Dials(), memory)
    changes.setdefault("checkpoint_id", str(uuid.uuid4()))
    return dataclasses.replace(memory.saved[-1], **changes)


def as_owner(db: DatabaseHandle, statement: str, params: tuple[Any, ...]) -> None:
    with psycopg.connect(db.dsn(OWNER), autocommit=True) as conn:
        conn.execute(statement, params)


def save_some(db: DatabaseHandle, thread: uuid.UUID, count: int = 2) -> None:
    store = store_for(db, thread)
    for _ in range(count):
        checkpoint = a_checkpoint()
        leg(lambda cp=checkpoint: store.save(cp))


# ── forget does not delete under a run that is alive ────────────────────────
@pytest.mark.parametrize("status", ["Running", "AwaitingApproval"])
def test_forget_deletes_nothing_while_a_run_of_the_thread_is_unfinished(
    fresh_database: DatabaseHandle, status: str
) -> None:
    # A leg that outlived its lease ends the run and forgets; a takeover leg now
    # holds the run, and its rows are not the old leg's to delete.
    thread = uuid.uuid4()
    as_owner(fresh_database, ADD_RUN, (uuid.uuid4(), thread, status))
    save_some(fresh_database, thread)

    forgotten = leg(store_for(fresh_database, thread).forget)

    assert forgotten == 0
    assert rows_of(fresh_database, thread) == 2


@pytest.mark.parametrize("status", ["Completed", "Failed"])
def test_forget_deletes_the_rows_once_the_run_is_recorded_as_ended(
    fresh_database: DatabaseHandle, status: str
) -> None:
    thread = uuid.uuid4()
    as_owner(fresh_database, ADD_RUN, (uuid.uuid4(), thread, "Running"))
    save_some(fresh_database, thread)
    store = store_for(fresh_database, thread)
    assert leg(store.forget) == 0

    as_owner(fresh_database, SET_STATUS, (status, thread))
    forgotten = leg(store.forget)

    assert forgotten == 2
    assert rows_of(fresh_database, thread) == 0


def test_forget_only_looks_at_the_runs_of_its_own_thread(
    fresh_database: DatabaseHandle,
) -> None:
    mine, other = uuid.uuid4(), uuid.uuid4()
    as_owner(fresh_database, ADD_RUN, (uuid.uuid4(), other, "Running"))
    save_some(fresh_database, mine)

    forgotten = leg(store_for(fresh_database, mine).forget)

    assert forgotten == 2


def test_forget_deletes_the_rows_of_a_thread_no_run_owns(
    fresh_database: DatabaseHandle,
) -> None:
    thread = uuid.uuid4()
    save_some(fresh_database, thread, count=3)

    forgotten = leg(store_for(fresh_database, thread).forget)

    assert forgotten == 3


# ── text the database refuses ───────────────────────────────────────────────
NUL = "before\x00after"
LONE_SURROGATE = "before\ud800after"
PLANTED: dict[str, Callable[[str], dict[str, Any]]] = {
    "a value of the state": lambda text: {"state": {"note": text}},
    "a key of the state": lambda text: {"state": {text: "x"}},
    "the checkpoint's ID": lambda text: {"checkpoint_id": text},
    "the workflow's name": lambda text: {"workflow_name": text},
    "a value deep in the metadata": lambda text: {"metadata": {"d": [{"x": text}]}},
}


@pytest.mark.parametrize("text", [NUL, LONE_SURROGATE], ids=["nul", "surrogate"])
@pytest.mark.parametrize("where", list(PLANTED))
def test_text_the_database_would_refuse_is_refused_at_save_with_a_fixed_reason(
    fresh_database: DatabaseHandle, where: str, text: str
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    checkpoint = a_checkpoint(**PLANTED[where](text))

    with pytest.raises(WorkflowCheckpointException) as raised:
        leg(lambda: store.save(checkpoint))

    assert str(raised.value) == REFUSED + "(unstorable-text)"
    assert store.failures == ("CodecRefusal",)
    assert rows_of(fresh_database, thread) == 0


def test_text_with_every_other_character_is_saved_and_comes_back_as_it_went(
    fresh_database: DatabaseHandle,
) -> None:
    store = store_for(fresh_database, uuid.uuid4())
    odd = 'tab\t newline\n quote" backslash\\ ä中\U0001f600 ￾ \x7f'
    checkpoint = a_checkpoint(state={"note": odd})

    leg(lambda: store.save(checkpoint))

    loaded = leg(lambda: store.load(checkpoint.checkpoint_id))
    assert loaded.state == {"note": odd}


# ── numbers jsonb would give back changed ───────────────────────────────────
# The rule, found against the database (the tests below): the JSON text Python
# writes for a float of ten to the sixteenth or more (``1e+16``) has an exponent,
# jsonb turns it into digits, and a number of digits with no fraction is read
# back as an integer. A float below that is written in positional form, which
# jsonb keeps, and comes back a float. So: a float with abs(value) >= 1e16 is
# refused at save as not restorable.
def stored_float(db: DatabaseHandle, value: float) -> Any:
    store = store_for(db, uuid.uuid4())
    checkpoint = a_checkpoint(state={"n": value})
    leg(lambda: store.save(checkpoint))
    return leg(lambda: store.load(checkpoint.checkpoint_id)).state["n"]


BELOW = [0.0, 1.0, -1.5, 0.1, 1e15, -1e15, 2.0**53, 9007199254740993.0]
BELOW += [9999999999999998.0, 1e-5, 1e-300, 5e-324, 1.7976931348623157e-300]
FROM = [1e16, -1e16, 1.5e22, 1e300, 2.0**70]


@pytest.mark.parametrize("value", BELOW)
def test_a_float_below_ten_to_the_sixteenth_comes_back_from_jsonb_as_it_went(
    fresh_database: DatabaseHandle, value: float
) -> None:
    back = stored_float(fresh_database, value)

    assert back == value
    assert type(back) is float


@pytest.mark.parametrize("value", FROM)
def test_the_database_gives_a_float_from_ten_to_the_sixteenth_up_back_as_an_int(
    fresh_database: DatabaseHandle, value: float
) -> None:
    # The finding the codec's rule rests on, asked of the database alone.
    with psycopg.connect(fresh_database.dsn(OWNER), autocommit=True) as conn:
        ((back,),) = conn.execute(
            "SELECT %s::jsonb", (json.dumps({"n": value}),)
        ).fetchall()

    assert type(back["n"]) is int
    # The digits of the shortest text of the float, not its exact value.
    assert back["n"] == int(Decimal(repr(value)))


@pytest.mark.parametrize("value", FROM)
def test_such_a_float_is_refused_at_save_as_not_restorable(
    fresh_database: DatabaseHandle, value: float
) -> None:
    thread = uuid.uuid4()
    store = store_for(fresh_database, thread)
    checkpoint = a_checkpoint(state={"n": value})

    with pytest.raises(WorkflowCheckpointException) as raised:
        leg(lambda: store.save(checkpoint))

    assert str(raised.value) == REFUSED + "(not-restorable)"
    assert rows_of(fresh_database, thread) == 0


@pytest.mark.parametrize("value", [10**16, 10**22, 2**70, -(2**70)])
def test_an_integer_of_any_size_comes_back_as_an_integer(
    fresh_database: DatabaseHandle, value: int
) -> None:
    back = stored_float(fresh_database, value)  # type: ignore[arg-type]

    assert back == value
    assert type(back) is int
