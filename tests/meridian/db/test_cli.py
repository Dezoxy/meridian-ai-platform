"""``meridian db migrate`` through Typer's test runner."""

import pytest
from dbsupport import OWNER, DatabaseHandle
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.migrations.runner import migration_files

runner = CliRunner()


def test_a_missing_variable_exits_1_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MIGRATIONS_DATABASE_URL_ENV, raising=False)

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert MIGRATIONS_DATABASE_URL_ENV in result.output


def test_an_unreachable_database_exits_1_without_printing_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = "postgresql://nobody:s3cret-value@127.0.0.1:1/none?connect_timeout=1"
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, dsn)

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert "s3cret-value" not in result.output


def test_migrate_prints_each_applied_name_then_reports_up_to_date(
    monkeypatch: pytest.MonkeyPatch, empty_database: DatabaseHandle
) -> None:
    dsn = empty_database.dsn(OWNER)
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, dsn)

    first = runner.invoke(app, ["db", "migrate"])
    second = runner.invoke(app, ["db", "migrate"])

    assert first.exit_code == 0, first.output
    assert first.stdout.splitlines() == [name for name, _ in migration_files()]
    assert second.exit_code == 0, second.output
    assert second.stdout.splitlines() == ["migrations: up to date"]
    assert empty_database.passwords[OWNER] not in first.output + second.output
