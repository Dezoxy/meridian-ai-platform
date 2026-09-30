"""The database fixtures refuse a test database that is not on this machine."""

import pytest
from dbsupport import (
    ALLOW_REMOTE_ENV,
    RemoteDatabaseRefusedError,
    require_loopback,
)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres@localhost:5432/postgres",
        "postgresql://postgres@127.0.0.1:55432/postgres",
        "postgresql://postgres@[::1]:5432/postgres",
        "host=localhost dbname=postgres user=postgres",
        "postgresql:///postgres?host=/var/run/postgresql",
        "postgresql:///postgres",
    ],
)
def test_a_loopback_or_local_database_is_accepted(dsn: str) -> None:
    require_loopback(dsn, {})


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres@db.example.com:5432/postgres",
        "postgresql://postgres@10.0.0.5/postgres",
        "postgresql://postgres@localhost.evil.example/postgres",
        "postgresql://postgres@/postgres?host=localhost,db.example.com",
        "host=localhost hostaddr=10.0.0.5 dbname=postgres",
    ],
)
def test_any_other_host_is_refused_without_printing_the_dsn(dsn: str) -> None:
    with pytest.raises(RemoteDatabaseRefusedError, match=ALLOW_REMOTE_ENV) as raised:
        require_loopback(dsn, {})

    assert "postgres@" not in str(raised.value)


def test_a_remote_pghost_counts_when_the_dsn_names_no_host() -> None:
    with pytest.raises(RemoteDatabaseRefusedError):
        require_loopback("postgresql:///postgres", {"PGHOST": "db.example.com"})


def test_the_explicit_override_allows_a_remote_host() -> None:
    require_loopback(
        "postgresql://postgres@db.example.com/postgres", {ALLOW_REMOTE_ENV: "1"}
    )


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_the_value_1_is_an_override(value: str) -> None:
    with pytest.raises(RemoteDatabaseRefusedError):
        require_loopback(
            "postgresql://postgres@db.example.com/postgres", {ALLOW_REMOTE_ENV: value}
        )
