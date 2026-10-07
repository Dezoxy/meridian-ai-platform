"""``meridian db migrate`` through Typer's test runner."""

import shutil
from pathlib import Path

import pytest
from dbsupport import OWNER, SEED_ROLE, DatabaseHandle
from servicesupport import REPO_ROOT
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.db import (
    MIGRATIONS_DATABASE_URL_ENV,
    SEED_DATABASE_URL_ENV,
)
from meridian.platform.common.db import connect
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


def test_seed_policies_without_the_variable_exits_1_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SEED_DATABASE_URL_ENV, raising=False)

    result = runner.invoke(app, ["db", "seed-policies"])

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert SEED_DATABASE_URL_ENV in result.output


def test_seed_policies_with_only_the_owners_variable_refuses_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.delenv(SEED_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    monkeypatch.chdir(REPO_ROOT)

    result = runner.invoke(app, ["db", "seed-policies"])

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert SEED_DATABASE_URL_ENV in result.output
    assert MIGRATIONS_DATABASE_URL_ENV not in result.output
    with connect(fresh_database.dsn(OWNER), "test") as conn:
        assert conn.execute("SELECT count(*) FROM policy.policies").fetchone() == (0,)


def test_migrate_does_not_read_the_seeds_variable(
    monkeypatch: pytest.MonkeyPatch, empty_database: DatabaseHandle
) -> None:
    monkeypatch.delenv(MIGRATIONS_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(SEED_DATABASE_URL_ENV, empty_database.dsn(SEED_ROLE))

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert MIGRATIONS_DATABASE_URL_ENV in result.output


def test_seed_policies_prints_the_two_counts_and_commits(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(SEED_DATABASE_URL_ENV, fresh_database.dsn(SEED_ROLE))
    source = str(REPO_ROOT / "data" / "synthetic")

    first = runner.invoke(app, ["db", "seed-policies", "--from", source])
    second = runner.invoke(app, ["db", "seed-policies", "--from", source])

    assert first.exit_code == 0, first.output
    assert first.stdout.splitlines() == ["policies: 56", "claim history: 51"]
    assert second.stdout.splitlines() == ["policies: 56", "claim history: 51"]
    with connect(fresh_database.dsn(OWNER), "test") as conn:
        assert conn.execute("SELECT count(*) FROM policy.policies").fetchone() == (56,)
    assert fresh_database.passwords[SEED_ROLE] not in first.output + second.output


def test_seed_policies_defaults_to_data_synthetic(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(SEED_DATABASE_URL_ENV, fresh_database.dsn(SEED_ROLE))
    monkeypatch.chdir(REPO_ROOT)

    result = runner.invoke(app, ["db", "seed-policies"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["policies: 56", "claim history: 51"]


def test_seed_policies_refuses_a_source_that_is_not_synthetic_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    monkeypatch.setenv(SEED_DATABASE_URL_ENV, fresh_database.dsn(SEED_ROLE))
    source = tmp_path / "source"
    shutil.copytree(REPO_ROOT / "data" / "synthetic", source)
    manifest = source / "manifest.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '"synthetic": true', '"synthetic": false'
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["db", "seed-policies", "--from", str(source)])

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    with connect(fresh_database.dsn(OWNER), "test") as conn:
        assert conn.execute("SELECT count(*) FROM policy.policies").fetchone() == (0,)


def test_seed_policies_with_an_unreachable_database_does_not_print_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = "postgresql://nobody:s3cret-value@127.0.0.1:1/none?connect_timeout=1"
    monkeypatch.setenv(SEED_DATABASE_URL_ENV, dsn)

    result = runner.invoke(app, ["db", "seed-policies"])

    assert result.exit_code == 1
    assert "s3cret-value" not in result.output
