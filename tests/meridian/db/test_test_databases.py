"""A test database is copied from a template, not migrated (S065).

``fresh_database`` and ``migrated_database`` used to create a database, create
the extension and apply every migration, for every test. They now copy one
template per set of migrations (``CREATE DATABASE ... TEMPLATE``), built once
per server under an advisory lock. These tests pin what a test can observe of
the copy, and how the template is built, named and kept clean.

The template under test here is the session's real one (``db_template``) or a
name of the test's own, dropped afterwards: a test never builds, patches or
drops the real template.
"""

import ast
import hashlib
import re
import secrets
import threading
from collections.abc import Callable, Iterator
from typing import Any

import dbsupport
import psycopg
import pytest
from dbsupport import (
    OWNER,
    PACKAGED_MIGRATIONS,
    SERVICE_ROLES,
    TEMPLATE_PREFIX,
    DatabaseHandle,
    copy_database,
    drop_database,
    ensure_template,
    template_lock_name,
    template_name,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from servicesupport import REPO_ROOT

from meridian.platform.migrations import runner

THREADS = 6
# Long enough that a live lock holder is certain to outlast it, short enough
# that the test does not wait: PostgreSQL ends the wait itself.
SHORT_TIMEOUT_SECONDS = 0.2
LEDGER = "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name"
HEX_16 = re.compile(r"[0-9a-f]{16}")


def _ledger_of(handle: DatabaseHandle) -> list[tuple[str, str]]:
    with psycopg.connect(handle.dsn(OWNER)) as conn:
        return [(name, sha) for name, sha in conn.execute(LEDGER)]


def _packaged_ledger() -> list[tuple[str, str]]:
    return [
        (name, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for name, text in PACKAGED_MIGRATIONS
    ]


def _databases_like(admin_dsn: str, name: str) -> list[str]:
    """Every database whose name is ``name`` or starts with it."""
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        rows = admin.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY 1",
            (name + "%",),
        ).fetchall()
    return [row[0] for row in rows]


@pytest.fixture
def own_template_name(db_admin_dsn: str) -> Iterator[str]:
    """A template name that does not exist yet; whatever the test left under
    it (or under its building name) is dropped afterwards."""
    name = f"mtpl_{secrets.token_hex(4)}"
    try:
        yield name
    finally:
        with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
            for leftover in _databases_like(db_admin_dsn, name):
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(leftover)
                    )
                )


@pytest.fixture
def copy_of_template(
    db_admin_dsn: str, db_passwords: dict[str, str], db_template: str
) -> Iterator[Callable[[], DatabaseHandle]]:
    """Make copies of the session's template; drop them afterwards."""
    made: list[DatabaseHandle] = []

    def make() -> DatabaseHandle:
        handle = copy_database(db_admin_dsn, db_passwords, db_template)
        made.append(handle)
        return handle

    try:
        yield make
    finally:
        for handle in made:
            drop_database(handle)


# ── what a test observes of a copy ──────────────────────────────────────────
def test_two_fresh_databases_are_distinct_and_share_one_schema(
    fresh_database: DatabaseHandle,
    copy_of_template: Callable[[], DatabaseHandle],
) -> None:
    other = copy_of_template()
    with psycopg.connect(fresh_database.dsn(OWNER), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.meridian_migrations (name, sha256) "
            "VALUES ('9999_planted.sql', 'planted')"
        )

    planted_here = _ledger_of(fresh_database)
    planted_there = _ledger_of(other)

    assert fresh_database.name != other.name
    assert ("9999_planted.sql", "planted") in planted_here
    assert ("9999_planted.sql", "planted") not in planted_there
    assert planted_there == _packaged_ledger()


def test_a_copy_records_every_packaged_migration_with_its_hash(
    fresh_database: DatabaseHandle,
) -> None:
    ledger = _ledger_of(fresh_database)

    assert ledger == _packaged_ledger()
    assert len(ledger) == len(PACKAGED_MIGRATIONS) > 0


def test_a_copy_has_the_owner_the_extension_and_working_roles(
    db_admin_dsn: str, fresh_database: DatabaseHandle
) -> None:
    with psycopg.connect(
        make_conninfo(db_admin_dsn, dbname=fresh_database.name), autocommit=True
    ) as conn:
        database_owner = conn.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database "
            "WHERE datname = current_database()"
        ).fetchone()
        extension = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        schema_owners = {
            name: owner
            for name, owner in conn.execute(
                "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname IN "
                "('audit', 'claims', 'gateway', 'knowledge', 'policy', 'runtime')"
            )
        }
    logins = []
    for role in (OWNER, *SERVICE_ROLES):
        with psycopg.connect(fresh_database.dsn(role)) as conn:
            logins.append(conn.execute("SELECT current_user").fetchone())

    assert database_owner == (OWNER,)
    assert extension == (1,)
    assert len(schema_owners) == 6
    assert set(schema_owners.values()) == {OWNER}
    assert logins == [(role,) for role in (OWNER, *SERVICE_ROLES)]


def test_a_dropped_copy_is_gone_and_the_template_stays(
    db_admin_dsn: str, db_template: str, copy_of_template: Callable[[], DatabaseHandle]
) -> None:
    handle = copy_of_template()
    assert _databases_like(db_admin_dsn, handle.name) == [handle.name]

    drop_database(handle)

    assert _databases_like(db_admin_dsn, handle.name) == []
    assert _databases_like(db_admin_dsn, db_template) == [db_template]


# ── the template's name ─────────────────────────────────────────────────────
def test_the_template_is_named_after_the_packaged_files() -> None:
    name = template_name()

    assert name == template_name(PACKAGED_MIGRATIONS)
    assert name.startswith(TEMPLATE_PREFIX)
    assert HEX_16.fullmatch(name.removeprefix(TEMPLATE_PREFIX))


def test_a_changed_file_changes_the_templates_name() -> None:
    files = list(PACKAGED_MIGRATIONS)
    name, text = files[3]
    changed = [*files[:3], (name, text + "\n"), *files[4:]]

    assert template_name(changed) != template_name(files)


def test_a_renamed_file_changes_the_templates_name() -> None:
    files = list(PACKAGED_MIGRATIONS)
    name, text = files[3]
    renamed = [*files[:3], (name.replace(".sql", "_x.sql"), text), *files[4:]]

    assert template_name(renamed) != template_name(files)


def test_a_file_added_or_removed_changes_the_templates_name() -> None:
    files = list(PACKAGED_MIGRATIONS)
    added = [*files, ("9999_planted.sql", "SELECT 1;")]

    assert template_name(added) != template_name(files)
    assert template_name(files[:-1]) != template_name(files)


def test_a_character_moved_between_name_and_text_changes_the_name() -> None:
    first = [("0001_a.sql", "bSELECT 1;")]
    second = [("0001_ab.sql", "SELECT 1;")]

    assert template_name(first) != template_name(second)


# ── how it is built ─────────────────────────────────────────────────────────
def test_the_template_is_built_once_when_several_threads_ask_at_once(
    db_admin_dsn: str,
    db_passwords: dict[str, str],
    own_template_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[str] = []
    real_apply = dbsupport.apply_migrations

    def counting_apply(
        conn: psycopg.Connection, files: list[tuple[str, str]] | None = None
    ) -> list[str]:
        builds.append(own_template_name)
        return real_apply(conn, files=files)

    monkeypatch.setattr(dbsupport, "apply_migrations", counting_apply)
    found: list[str] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(THREADS)

    def worker() -> None:
        try:
            barrier.wait()
            found.append(
                ensure_template(db_admin_dsn, db_passwords, name=own_template_name)
            )
        except BaseException as exc:  # collected, then asserted
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert found == [own_template_name] * THREADS
    assert len(builds) == 1
    assert _databases_like(db_admin_dsn, own_template_name) == [own_template_name]


def test_a_built_template_is_migrated_and_nothing_is_connected_to_it(
    db_admin_dsn: str, db_passwords: dict[str, str], own_template_name: str
) -> None:
    ensure_template(db_admin_dsn, db_passwords, name=own_template_name)

    with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
        sessions = admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s",
            (own_template_name,),
        ).fetchone()
        allows = admin.execute(
            "SELECT datallowconn FROM pg_database WHERE datname = %s",
            (own_template_name,),
        ).fetchone()
    handle = copy_database(db_admin_dsn, db_passwords, own_template_name)
    try:
        ledger = _ledger_of(handle)
    finally:
        drop_database(handle)

    assert sessions == (0,)
    assert allows == (False,)
    assert ledger == _packaged_ledger()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(make_conninfo(db_admin_dsn, dbname=own_template_name))


def test_copying_works_while_other_tests_hold_connections_to_their_copies(
    db_admin_dsn: str,
    db_template: str,
    fresh_database: DatabaseHandle,
    copy_of_template: Callable[[], DatabaseHandle],
) -> None:
    held = copy_of_template()
    with (
        psycopg.connect(fresh_database.dsn(OWNER)) as first,
        psycopg.connect(held.dsn(OWNER)) as second,
    ):
        first.execute("SELECT 1")
        second.execute("SELECT 1")

        late = copy_of_template()
        late_ledger = _ledger_of(late)

    with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
        on_template = admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s",
            (db_template,),
        ).fetchone()
    assert late_ledger == _packaged_ledger()
    assert on_template == (0,)


def test_postgresql_refuses_to_copy_a_database_that_has_a_session(
    db_admin_dsn: str,
    db_passwords: dict[str, str],
    copy_of_template: Callable[[], DatabaseHandle],
) -> None:
    # The reason nothing may ever connect to the template: this is the rule the
    # design rests on, shown on a copy that is not the template.
    source = copy_of_template()

    with psycopg.connect(source.dsn(OWNER)) as session:
        session.execute("SELECT 1")

        with pytest.raises(psycopg.errors.ObjectInUse):
            copy_database(db_admin_dsn, db_passwords, source.name)

    later = copy_database(db_admin_dsn, db_passwords, source.name)
    try:
        assert _ledger_of(later) == _packaged_ledger()
    finally:
        drop_database(later)


def test_a_build_that_fails_leaves_nothing_behind_and_a_retry_builds(
    db_admin_dsn: str,
    db_passwords: dict[str, str],
    own_template_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planted = ("9998_planted.sql", "SELECT 1 / 0;")
    with monkeypatch.context() as patched:
        # The failure is planted where the builder hands its list to the
        # runner, as in the second-net test below: patching the runner's own
        # list or the public name does not reach the build.
        patched.setattr(
            dbsupport,
            "apply_migrations",
            lambda conn, files=None: runner.apply_migrations(
                conn, files=[*files, planted]
            ),
        )

        with pytest.raises(psycopg.errors.DivisionByZero):
            ensure_template(db_admin_dsn, db_passwords, name=own_template_name)
    left_after_failure = _databases_like(db_admin_dsn, own_template_name)

    ensure_template(db_admin_dsn, db_passwords, name=own_template_name)

    assert left_after_failure == []
    assert _databases_like(db_admin_dsn, own_template_name) == [own_template_name]


def test_a_building_database_left_by_a_dead_builder_is_replaced(
    db_admin_dsn: str, db_passwords: dict[str, str], own_template_name: str
) -> None:
    with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(
                sql.Identifier(f"{own_template_name}_building")
            )
        )

    ensure_template(db_admin_dsn, db_passwords, name=own_template_name)

    assert _databases_like(db_admin_dsn, own_template_name) == [own_template_name]


def test_a_patched_migration_list_does_not_change_the_template(
    db_admin_dsn: str,
    db_passwords: dict[str, str],
    own_template_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What the migration tests do: apply a prefix of the files. The builder
    # passes the packaged list it read at import, so the template is built from
    # all the packaged files and matches them.
    prefix = list(PACKAGED_MIGRATIONS[:3])
    monkeypatch.setattr(runner, "migration_files", lambda: prefix)
    monkeypatch.setattr(runner, "_packaged_files", lambda: prefix)

    ensure_template(db_admin_dsn, db_passwords, name=own_template_name)

    handle = copy_database(db_admin_dsn, db_passwords, own_template_name)
    try:
        ledger = _ledger_of(handle)
    finally:
        drop_database(handle)
    assert ledger == _packaged_ledger()
    assert template_name() == template_name(PACKAGED_MIGRATIONS)


def test_a_rebound_public_list_changes_neither_the_template_nor_its_name(
    db_admin_dsn: str,
    db_passwords: dict[str, str],
    own_template_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The list the builder applies, the check compares and the name hashes is
    # kept under a private name: rebinding the public one (a test that applies a
    # prefix of the files) leaves a template of all the files under the real
    # name, not a template of the prefix that passes its own check.
    real_files = tuple(runner.migration_files())
    real_ledger = [
        (name, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for name, text in real_files
    ]
    monkeypatch.setattr(dbsupport, "PACKAGED_MIGRATIONS", real_files[:3])

    ensure_template(db_admin_dsn, db_passwords, name=own_template_name)

    handle = copy_database(db_admin_dsn, db_passwords, own_template_name)
    try:
        ledger = _ledger_of(handle)
    finally:
        drop_database(handle)
    assert ledger == real_ledger
    assert len(real_ledger) > 3
    assert template_name() == template_name(real_files)


def test_no_call_under_src_gives_apply_migrations_a_list_of_files() -> None:
    # The runner's ``files`` argument is for the tests' template builder only:
    # a caller that passed its own list could apply files the package does not
    # hold, in an order nothing sorts. ``meridian db migrate`` calls it bare.
    calls: list[tuple[str, int]] = []
    given: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if called != "apply_migrations":
                continue
            calls.append((path.name, node.lineno))
            if len(node.args) > 1 or any(
                k.arg in (None, "files") for k in node.keywords
            ):
                given.append(f"{path.name}:{node.lineno}")
    assert calls, "no call found: the walker reads nothing"
    assert given == []


def test_a_template_that_does_not_match_the_packaged_files_is_refused(
    db_admin_dsn: str,
    db_passwords: dict[str, str],
    own_template_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The second net: a builder that applied fewer files than the package holds
    # (reached here without a patch of the runner's list) leaves nothing under
    # the name.
    prefix = list(PACKAGED_MIGRATIONS[:3])
    monkeypatch.setattr(
        dbsupport,
        "apply_migrations",
        lambda conn, files=None: runner.apply_migrations(conn, files=prefix),
    )

    with pytest.raises(RuntimeError, match="does not match"):
        ensure_template(db_admin_dsn, db_passwords, name=own_template_name)

    assert _databases_like(db_admin_dsn, own_template_name) == []


# ── the lock ────────────────────────────────────────────────────────────────
def test_a_builder_gives_up_naming_the_lock_when_the_holder_never_lets_go(
    db_admin_dsn: str, db_passwords: dict[str, str], own_template_name: str
) -> None:
    with psycopg.connect(db_admin_dsn) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (template_lock_name(own_template_name),),
        )

        with pytest.raises(RuntimeError, match="advisory lock") as raised:
            ensure_template(
                db_admin_dsn,
                db_passwords,
                name=own_template_name,
                lock_timeout_seconds=SHORT_TIMEOUT_SECONDS,
            )

    assert template_lock_name(own_template_name) in str(raised.value)
    assert _databases_like(db_admin_dsn, own_template_name) == []


@pytest.mark.parametrize("timeout", [0, -1])
def test_a_lock_timeout_of_zero_or_below_is_refused(
    db_admin_dsn: str,
    db_passwords: dict[str, str],
    own_template_name: str,
    timeout: Any,
) -> None:
    # PostgreSQL reads a lock_timeout of 0 as no timeout at all.
    with pytest.raises(ValueError, match="lock_timeout_seconds"):
        ensure_template(
            db_admin_dsn,
            db_passwords,
            name=own_template_name,
            lock_timeout_seconds=timeout,
        )

    assert _databases_like(db_admin_dsn, own_template_name) == []
