"""The test roles are cluster-wide, so parallel workers must not race on them.

Under pytest-xdist every worker runs the ``db_passwords`` fixture against the
same server (S054): two workers that both found no role both ran CREATE ROLE,
and each worker's own random passwords overwrote the others'.
"""

import secrets
import threading
from types import SimpleNamespace

import psycopg
from dbsupport import (
    OWNER,
    SERVICE_ROLES,
    WORKERINPUT_KEY,
    ensure_roles,
    new_passwords,
    session_passwords,
)
from psycopg import sql

THREADS = 8
ROUNDS = 5
ALL_ROLES = (OWNER, *SERVICE_ROLES)


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
