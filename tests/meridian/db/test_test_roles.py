"""The test roles are cluster-wide, so parallel workers must not race on them.

Under pytest-xdist every worker runs the ``db_passwords`` fixture against the
same server (S054): two workers that both found no role both ran CREATE ROLE,
and each worker's own random passwords overwrote the others'.
"""

import inspect
import secrets
import threading
from types import SimpleNamespace
from typing import Any

import dbsupport
import psycopg
import pytest
from dbsupport import (
    OWNER,
    ROLES_LOCK_KEY,
    SERVICE_ROLES,
    WORKERINPUT_KEY,
    ensure_roles,
    new_passwords,
    session_passwords,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sweepmigrationsupport import as_role_by_set_role

THREADS = 8
ROUNDS = 5
ALL_ROLES = (OWNER, *SERVICE_ROLES)
# Long enough that a live lock holder is certain to outlast it, short enough
# that the test does not wait: PostgreSQL ends the wait itself.
SHORT_TIMEOUT_SECONDS = 0.2
# The server's own limit on any one statement in the tests that hold the lock.
STATEMENT_TIMEOUT_MS = 5000
# Nothing listens on port 1: a test that passes it shows that no connection
# is opened.
UNREACHABLE_DSN = "host=127.0.0.1 port=1 dbname=none connect_timeout=1"


def _race(
    admin_dsn: str, passwords: dict[str, str], rounds: int
) -> list[BaseException]:
    """Call ``ensure_roles`` from THREADS threads at once, ``rounds`` times;
    return what they raised."""
    failures: list[BaseException] = []

    def worker(barrier: threading.Barrier) -> None:
        try:
            barrier.wait()
            ensure_roles(admin_dsn, passwords)
        except BaseException as exc:  # collected, then asserted
            failures.append(exc)

    for _ in range(rounds):
        barrier = threading.Barrier(THREADS)
        threads = [
            threading.Thread(target=worker, args=(barrier,)) for _ in range(THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    return failures


def test_roles_set_up_by_several_workers_at_once_all_succeed(
    db_admin_dsn: str, db_passwords: dict[str, str]
) -> None:
    # The session's own passwords, never others: the roles are cluster-wide and
    # the session's DSNs carry these. The roles exist already, so this is the
    # ALTER race ("tuple concurrently updated" without the lock).
    assert _race(db_admin_dsn, db_passwords, ROUNDS) == []


def test_roles_that_do_not_exist_yet_are_created_once_by_racing_workers(
    db_admin_dsn: str,
) -> None:
    # The CREATE race (a UniqueViolation without the lock) needs roles nobody
    # has made, so these are this test's own and are dropped afterwards.
    scratch = {
        f"meridian_test_role_{secrets.token_hex(4)}": secrets.token_urlsafe(16)
        for _ in range(2)
    }
    try:
        failures = _race(db_admin_dsn, scratch, rounds=1)

        with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
            found = admin.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(scratch),),
            ).fetchall()
        assert failures == []
        assert {row[0] for row in found} == set(scratch)
    finally:
        with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
            for role in scratch:
                admin.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


def test_the_roles_keep_their_attributes(
    db_admin_dsn: str, db_passwords: dict[str, str]
) -> None:
    ensure_roles(db_admin_dsn, db_passwords)

    with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
        rows = admin.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = ANY(%s)",
            (list(ALL_ROLES),),
        ).fetchall()

    assert {row[0] for row in rows} == set(ALL_ROLES)
    assert all(row[1] and not any(row[2:]) for row in rows)


def test_a_worker_takes_the_passwords_the_controller_handed_it() -> None:
    handed = {role: f"handed-{role}" for role in ALL_ROLES}
    config = SimpleNamespace(workerinput={WORKERINPUT_KEY: handed, "workerid": "gw0"})

    assert session_passwords(config) == handed


def test_without_xdist_fresh_passwords_are_made_for_every_role() -> None:
    config = SimpleNamespace()

    first = session_passwords(config)
    second = session_passwords(config)

    assert set(first) == set(ALL_ROLES)
    assert all(first.values())
    assert len(set(first.values())) == len(ALL_ROLES)
    assert first != second


def test_new_passwords_are_random_per_role_and_per_call() -> None:
    first = new_passwords()
    second = new_passwords()

    assert set(first) == set(ALL_ROLES)
    assert first != second


def _scratch_roles() -> dict[str, str]:
    return {
        f"meridian_test_role_{secrets.token_hex(4)}": secrets.token_urlsafe(16)
        for _ in range(2)
    }


def _existing(admin_dsn: str, roles: dict[str, str]) -> set[str]:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        rows = admin.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(roles),)
        ).fetchall()
    return {row[0] for row in rows}


def _drop(admin_dsn: str, roles: dict[str, str]) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        for role in roles:
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
            )


def test_the_roles_and_the_template_builder_take_their_lock_through_one_helper() -> (
    None
):
    # One helper takes the lock for both, so the two cannot drift apart again,
    # and ensure_roles stays well under the 50 lines a function may have.
    roles = inspect.getsource(ensure_roles)
    template = inspect.getsource(dbsupport.ensure_template)

    assert "_advisory_lock(" in roles
    assert "_advisory_lock(" in template
    assert "pg_advisory_xact_lock" not in roles
    assert len(roles.splitlines()) < 50


class RecordingConnection:
    """A stand-in for the connection ``as_role_by_set_role`` opens: answers the
    names the test gives and records whether it was closed."""

    def __init__(self, names: tuple[str, str] | None, fail_on: str = "") -> None:
        self.names = names
        self.fail_on = fail_on
        self.closed = False

    def execute(self, query: Any, params: Any = None) -> "RecordingConnection":
        if self.fail_on and self.fail_on in str(query):
            raise psycopg.errors.InsufficientPrivilege(self.fail_on)
        return self

    def fetchone(self) -> tuple[str, str] | None:
        return self.names

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _as_role_with(
    monkeypatch: pytest.MonkeyPatch, connection: RecordingConnection
) -> Any:
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    database = SimpleNamespace(admin_dsn="host=none", name="none")
    return as_role_by_set_role(database, "claims_sweep")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("connection", "error"),
    [
        (RecordingConnection(("claims_sweep", "claims_sweep")), AssertionError),
        (RecordingConnection(("postgres", "postgres")), AssertionError),
        (RecordingConnection(None), AssertionError),
        (RecordingConnection(None, fail_on="SET ROLE"), psycopg.Error),
    ],
    ids=["session-user-is-the-role", "role-not-set", "no-names", "set-role-refused"],
)
def test_a_failed_set_role_closes_the_connection_it_opened(
    monkeypatch: pytest.MonkeyPatch, connection: RecordingConnection, error: type
) -> None:
    with pytest.raises(error):
        _as_role_with(monkeypatch, connection)

    assert connection.closed


def test_a_set_role_that_works_hands_the_connection_over_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = RecordingConnection(("postgres", "claims_sweep"))

    handed = _as_role_with(monkeypatch, connection)

    assert handed is connection
    assert not connection.closed


def _with_statement_timeout(admin_dsn: str) -> str:
    """The DSN with a server-side ``statement_timeout``: if ``lock_timeout``
    regressed, the wait for a lock the test holds would end in an error after a
    few seconds, not hang the run."""
    return make_conninfo(
        admin_dsn, options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
    )


def test_a_lock_held_by_another_worker_ends_in_an_error_naming_lock_and_wait(
    db_admin_dsn: str,
) -> None:
    scratch = _scratch_roles()
    guarded = _with_statement_timeout(db_admin_dsn)
    try:
        with psycopg.connect(db_admin_dsn) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (ROLES_LOCK_KEY,)
            )

            with pytest.raises(RuntimeError) as raised:
                ensure_roles(
                    guarded, scratch, lock_timeout_seconds=SHORT_TIMEOUT_SECONDS
                )

            assert _existing(db_admin_dsn, scratch) == set()
    finally:
        _drop(db_admin_dsn, scratch)

    message = str(raised.value)
    assert "roles' advisory lock" in message
    assert f"{SHORT_TIMEOUT_SECONDS} seconds" in message
    assert isinstance(raised.value.__cause__, psycopg.errors.LockNotAvailable)


def test_once_the_lock_is_released_the_next_call_sets_the_roles_up(
    db_admin_dsn: str,
) -> None:
    scratch = _scratch_roles()
    guarded = _with_statement_timeout(db_admin_dsn)
    try:
        with psycopg.connect(db_admin_dsn) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (ROLES_LOCK_KEY,)
            )
            with pytest.raises(RuntimeError):
                ensure_roles(
                    guarded, scratch, lock_timeout_seconds=SHORT_TIMEOUT_SECONDS
                )

        # The default timeout: the roles' lock is cluster-wide and other workers
        # queue on it, so a short wait could fail here under load.
        ensure_roles(guarded, scratch)

        assert _existing(db_admin_dsn, scratch) == set(scratch)
    finally:
        _drop(db_admin_dsn, scratch)


@pytest.mark.parametrize("seconds", [0, -1, -0.5])
def test_a_timeout_of_zero_or_below_is_refused_because_zero_means_no_timeout(
    seconds: float,
) -> None:
    # PostgreSQL reads lock_timeout = 0 as "wait for ever", the very hang the
    # timeout is there to end. The DSN cannot connect, so the ValueError also
    # shows that no connection is opened for it (and the test needs no database).
    with pytest.raises(ValueError, match="positive"):
        ensure_roles(UNREACHABLE_DSN, {}, lock_timeout_seconds=seconds)


def test_the_roles_transaction_runs_read_committed_whatever_the_server_defaults_to(
    db_admin_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    class Spy(psycopg.Connection[Any]):
        """Asks the server which isolation level each statement ran under."""

        def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
            cursor = super().execute(query, params, **kwargs)
            level = super().execute("SELECT current_setting('transaction_isolation')")
            seen.append(level.fetchone()[0])  # type: ignore[index]
            return cursor

    serializable = make_conninfo(
        db_admin_dsn, options="-c default_transaction_isolation=serializable"
    )
    monkeypatch.setattr(psycopg, "connect", Spy.connect)

    ensure_roles(serializable, {})

    assert seen
    assert set(seen) == {"read committed"}
