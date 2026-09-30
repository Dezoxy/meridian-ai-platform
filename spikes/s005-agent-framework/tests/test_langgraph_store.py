"""What LangGraph's SQLite saver does on a failed save, in strict mode, and to data.

Everything here is LangGraph-specific; the shared table is in test_observations.py.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from claimflow import langgraph_flow, rules
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from support import OVER_THRESHOLD_CLAIM, run_cli, run_python

GOOD = {"decision": "approve", "adjuster_id": "ADJ-0001"}
STRICT = {"LANGGRAPH_STRICT_MSGPACK": "true"}


class SaveRefused(RuntimeError):
    """What the failing saver raises, so a test can tell it from a LangGraph error."""


class RefusingSaver(SqliteSaver):
    def put(self, *args: Any, **kwargs: Any) -> Any:
        raise SaveRefused("the checkpoint was refused")


class RefusingWritesSaver(SqliteSaver):
    def put_writes(self, *args: Any, **kwargs: Any) -> Any:
        raise SaveRefused("the pending writes were refused")


def _connect(store_dir: Path) -> sqlite3.Connection:
    store_dir.mkdir(parents=True, exist_ok=True)
    # Durable modes save from a background thread, so the connection is shared.
    return sqlite3.connect(store_dir / langgraph_flow.DB_FILE, check_same_thread=False)


@pytest.fixture
def steps_run(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names of the rule functions the graph's nodes have called so far."""
    calls: list[str] = []
    for name in ("validate", "assess"):
        original = getattr(rules, name)

        def spy(*args: Any, _name: str = name, _original: Any = original) -> Any:
            calls.append(_name)
            return _original(*args)

        monkeypatch.setattr(rules, name, spy)
    return calls


def _checkpoint_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0]


# --- a failed save -----------------------------------------------------------


def test_langgraph_failed_save_at_default_durability_raises_after_the_nodes_ran(
    store_dir: Path, steps_run: list[str]
) -> None:
    conn = _connect(store_dir)
    graph = langgraph_flow.build_graph(RefusingSaver(conn))
    config = {"configurable": {"thread_id": "t"}}

    with pytest.raises(SaveRefused):  # the saver's own exception, not wrapped
        graph.invoke({"claim_id": OVER_THRESHOLD_CLAIM}, config)  # durability="async"

    assert steps_run == ["validate", "assess"]  # the work was done and is lost
    assert _checkpoint_rows(conn) == 0
    conn.close()


@pytest.mark.parametrize(
    ("durability", "nodes_run"),
    [
        ("sync", []),  # saved before the next step starts: nothing ran unsaved
        ("exit", ["validate", "assess"]),  # saved only when the graph exits
    ],
)
def test_langgraph_failed_save_raises_and_durability_sets_how_much_ran_first(
    store_dir: Path, steps_run: list[str], durability: str, nodes_run: list[str]
) -> None:
    conn = _connect(store_dir)
    graph = langgraph_flow.build_graph(RefusingSaver(conn))
    config = {"configurable": {"thread_id": "t"}}

    with pytest.raises(SaveRefused):
        graph.invoke({"claim_id": OVER_THRESHOLD_CLAIM}, config, durability=durability)

    assert steps_run == nodes_run
    assert _checkpoint_rows(conn) == 0
    conn.close()


def test_langgraph_failed_pending_writes_save_leaves_next_set_but_no_interrupt(
    store_dir: Path,
) -> None:
    # Why the spike reads `snapshot.interrupts` and not `snapshot.next`.
    conn = _connect(store_dir)
    graph = langgraph_flow.build_graph(RefusingWritesSaver(conn))
    config = {"configurable": {"thread_id": "t"}}

    with pytest.raises(SaveRefused):
        graph.invoke({"claim_id": OVER_THRESHOLD_CLAIM}, config)

    snapshot = graph.get_state(config)
    assert snapshot.next == ("approval",)  # looks paused ...
    assert snapshot.interrupts == ()  # ... but no interrupt was recorded
    conn.close()


# --- strict msgpack ----------------------------------------------------------


def test_langgraph_flow_pauses_and_resumes_under_strict_msgpack(
    tmp_path: Path,
) -> None:
    # The state holds only primitives and dicts, so no type needs listing.
    store = str(tmp_path / "store")

    paused = run_cli(
        "start", "--framework", "langgraph", "--claim-id", OVER_THRESHOLD_CLAIM,
        "--store-dir", store, env=STRICT,
    )  # fmt: skip
    done = run_cli(
        "resume", "--framework", "langgraph", "--run-ref", paused["run_ref"],
        "--decision", "approve", "--adjuster-id", "ADJ-0042", "--store-dir", store,
        env=STRICT,
    )  # fmt: skip

    assert paused["status"] == "awaiting_approval"
    assert (done["status"], done["outcome"]) == ("completed", "approve")


def test_langgraph_strict_msgpack_is_read_once_at_import() -> None:
    code = (
        "from langgraph.checkpoint.serde import _msgpack;"
        "print(_msgpack.STRICT_MSGPACK_ENABLED)"
    )

    default = run_python("-c", code)
    strict = run_python("-c", code, env=STRICT)

    assert (default.stdout.strip(), strict.stdout.strip()) == ("False", "True")


def test_langgraph_strict_msgpack_degrades_an_unlisted_type_to_a_dict() -> None:
    code = (
        "from claimflow.rules import AdjusterDecision\n"
        "from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer\n"
        "serde = JsonPlusSerializer()\n"
        "typed = serde.dumps_typed(AdjusterDecision('approve', 'ADJ-1'))\n"
        "print(serde.loads_typed(typed))\n"
    )

    strict = run_python("-c", code, env=STRICT)

    assert strict.returncode == 0  # it does not fail ...
    assert strict.stdout.strip() == "{'decision': 'approve', 'adjuster_id': 'ADJ-1'}"
    assert "Blocked deserialization" in strict.stderr  # ... it warns and degrades


# --- personal data at rest ---------------------------------------------------


def _email() -> str:
    return rules.validate(OVER_THRESHOLD_CLAIM).claim["claimant"]["email"]


def _where(store_dir: Path, needle: str) -> set[str]:
    """Which of the main file and the write-ahead log hold the bytes."""
    main = store_dir / langgraph_flow.DB_FILE
    found = set()
    for label, path in (("main", main), ("wal", Path(f"{main}-wal"))):
        if path.exists() and needle.encode() in path.read_bytes():
            found.add(label)
    return found


def _completed_thread(conn: sqlite3.Connection, saver: SqliteSaver) -> None:
    graph = langgraph_flow.build_graph(saver)
    config = {"configurable": {"thread_id": "t"}}
    graph.invoke({"claim_id": OVER_THRESHOLD_CLAIM}, config)
    graph.invoke(Command(resume=GOOD), config)
    conn.commit()


def test_langgraph_personal_data_can_sit_in_the_wal_file_not_the_main_file(
    store_dir: Path,
) -> None:
    conn = _connect(store_dir)
    _completed_thread(conn, SqliteSaver(conn))

    assert _where(store_dir, _email()) == {"wal"}  # a grep of the main file misses it

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert _where(store_dir, _email()) == {"main"}
    conn.close()


@pytest.mark.parametrize(
    ("secure_delete", "left_after_delete"),
    [(False, {"main"}), (True, set())],
    ids=["default", "secure_delete"],
)
def test_langgraph_delete_thread_removes_rows_but_sqlite_keeps_the_bytes(
    store_dir: Path, secure_delete: bool, left_after_delete: set[str]
) -> None:
    conn = _connect(store_dir)
    if secure_delete:
        conn.execute("PRAGMA secure_delete = ON")
    saver = SqliteSaver(conn)
    _completed_thread(conn, saver)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    saver.delete_thread("t")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    assert _checkpoint_rows(conn) == 0
    assert _where(store_dir, _email()) == left_after_delete
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert _where(store_dir, _email()) == set()  # only a VACUUM clears the default
    conn.close()


class _ToyCipher:
    """A reversible byte flip that stands in for a real cipher. Not secure.

    It only shows where `EncryptedSerializer` hooks in: a real deployment passes
    an AES cipher (`EncryptedSerializer.from_pycryptodome_aes`, not installed here).
    """

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        return "toy", bytes(byte ^ 0x5A for byte in plaintext)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        return bytes(byte ^ 0x5A for byte in ciphertext)


def test_langgraph_encrypted_serializer_keeps_personal_data_out_of_the_file(
    store_dir: Path,
) -> None:
    conn = _connect(store_dir)
    saver = SqliteSaver(conn, serde=EncryptedSerializer(_ToyCipher()))
    graph = langgraph_flow.build_graph(saver)
    config = {"configurable": {"thread_id": "t"}}

    graph.invoke({"claim_id": OVER_THRESHOLD_CLAIM}, config)
    result = graph.invoke(Command(resume=GOOD), config)  # and it still restores
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    assert result["outcome"] == "approve"
    assert _where(store_dir, _email()) == set()
    conn.close()
