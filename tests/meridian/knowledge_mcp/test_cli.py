"""``meridian knowledge ingest`` through Typer's test runner (S012)."""

import functools
import re
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from knowledgesupport import (
    CANARY,
    REAL_SOURCE,
    TENANT,
    ScriptedGateway,
    Source,
    chunk_count,
    too_many,
)
from servicesupport import REGISTRY_DIR, REPO_ROOT, owner_rows
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import knowledge as knowledge_cli
from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.common.db import connect
from meridian.platform.common.env import REGISTRY_DIR_ENV
from meridian.platform.knowledge_mcp.ingest import ingest_wordings

GATEWAY_URL = "http://gateway.invalid:8080"
SECRET = "s3cret-value"  # noqa: S105 (a test password, not a credential)
# Typer styles a usage error when it thinks a terminal is there, as it does in
# GitHub Actions, and the escape codes then split an option's name.
ANSI_STYLE = re.compile(r"\x1b\[[0-9;]*m")
runner = CliRunner()


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptedGateway, list[str]]:
    """The gateway's stand-in behind the CLI's HTTP client factory, and the
    addresses the factory was asked for."""
    gateway = ScriptedGateway()
    asked: list[str] = []

    def factory(url: str) -> httpx.Client:
        asked.append(url)
        return gateway.http()

    monkeypatch.setattr(knowledge_cli, "make_http_client", factory)
    return gateway, asked


@pytest.fixture
def configured(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> DatabaseHandle:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, GATEWAY_URL)
    monkeypatch.setenv(REGISTRY_DIR_ENV, str(REGISTRY_DIR))
    return fresh_database


def ingest_args(*extra: str, tenant: str = TENANT) -> list[str]:
    return [
        "knowledge",
        "ingest",
        "--tenant",
        tenant,
        "--from",
        str(REAL_SOURCE),
        *extra,
    ]


def test_the_tenant_option_is_required() -> None:
    result = runner.invoke(app, ["knowledge", "ingest"])

    assert result.exit_code == 2
    assert "--tenant" in ANSI_STYLE.sub("", result.output)


def test_a_missing_database_variable_exits_1_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MIGRATIONS_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, GATEWAY_URL)

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert MIGRATIONS_DATABASE_URL_ENV in result.output


def test_a_missing_gateway_variable_exits_1_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, "postgresql://unused")
    monkeypatch.delenv(knowledge_cli.GATEWAY_URL_ENV, raising=False)

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert knowledge_cli.GATEWAY_URL_ENV in result.output


@pytest.mark.parametrize("value", ["gateway:8080", "ftp://gateway", "http://"])
def test_a_gateway_address_that_is_not_http_exits_1_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, "postgresql://unused")
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, value)

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert knowledge_cli.GATEWAY_URL_ENV in result.output
    assert value not in result.output


@pytest.mark.parametrize(
    "value",
    [
        f"http://operator:{SECRET}@gateway.invalid:8080",
        f"https://operator:{SECRET}@gateway.invalid",
        "http://operator@gateway.invalid",
        f"http://:{SECRET}@gateway.invalid",
    ],
)
def test_a_gateway_address_with_a_user_name_or_password_exits_1_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, "postgresql://unused")
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, value)

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert "user name" in result.output
    assert knowledge_cli.GATEWAY_URL_ENV in result.output
    assert SECRET not in result.output
    assert "operator" not in result.output


@pytest.mark.parametrize(
    "value", ["http://[::1", "http://gateway:abc", "http://gateway:99999"]
)
def test_a_gateway_address_urlsplit_cannot_take_exits_1_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, "postgresql://unused")
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, value)

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert knowledge_cli.GATEWAY_URL_ENV in result.output
    assert value not in result.output
    assert isinstance(result.exception, SystemExit)  # not a ValueError


@pytest.mark.parametrize("value", ["http://gate\x01way:8080", "http://gateway/\x7f"])
def test_a_gateway_address_httpx_cannot_take_exits_1_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # urlsplit takes these; the real client factory (not replaced) does not.
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, "postgresql://unused")
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, value)
    monkeypatch.setenv(REGISTRY_DIR_ENV, str(REGISTRY_DIR))

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert knowledge_cli.GATEWAY_URL_ENV in result.output
    assert value not in result.output
    assert isinstance(result.exception, SystemExit)  # not an httpx.InvalidURL


def test_a_registry_that_does_not_load_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, "postgresql://unused")
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, GATEWAY_URL)
    monkeypatch.setenv(REGISTRY_DIR_ENV, str(tmp_path))

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR the registry does not load: ")


def test_a_tenant_the_registry_refuses_exits_1_and_calls_nothing(
    configured: DatabaseHandle, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    gateway, _ = stand_in

    result = runner.invoke(app, ingest_args(tenant="evaluation"))

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert "may not run agent" in result.output
    assert gateway.requests == []
    assert chunk_count(configured) == 0


def test_a_source_that_is_not_the_generators_exits_1_and_calls_nothing(
    configured: DatabaseHandle,
    stand_in: tuple[ScriptedGateway, list[str]],
    tmp_path: Path,
) -> None:
    gateway, _ = stand_in

    result = runner.invoke(
        app, ["knowledge", "ingest", "--tenant", TENANT, "--from", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "manifest.json" in result.output
    assert gateway.requests == []
    assert chunk_count(configured) == 0


def test_an_unreachable_database_exits_1_without_printing_the_dsn(
    monkeypatch: pytest.MonkeyPatch, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    dsn = f"postgresql://nobody:{SECRET}@127.0.0.1:1/none?connect_timeout=1"
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, dsn)
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, GATEWAY_URL)
    monkeypatch.setenv(REGISTRY_DIR_ENV, str(REGISTRY_DIR))

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert SECRET not in result.output


def test_a_gateway_that_fails_exits_1_and_stores_nothing(
    configured: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = ScriptedGateway(script=lambda index, inputs: httpx.Response(500, json={}))
    monkeypatch.setattr(knowledge_cli, "make_http_client", lambda url: failing.http())

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert "500" in result.output
    assert chunk_count(configured) == 0


def test_a_successful_run_prints_the_counts_and_commits(
    configured: DatabaseHandle, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    gateway, asked = stand_in

    result = runner.invoke(app, ingest_args())

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "documents: 4",
        "chunks: 85",
        "deployment: replay-embedding",
        "input tokens: 30",  # the stand-in reports 5 for each of the 6 batches
    ]
    assert asked == [GATEWAY_URL]
    assert chunk_count(configured) == 85
    assert configured.passwords[OWNER] not in result.output
    assert {h["X-Meridian-Tenant"] for h in gateway.headers} == {TENANT}
    assert {h["X-Meridian-Agent"] for h in gateway.headers} == {"knowledge-ingestion"}
    assert len({h["X-Meridian-Run"] for h in gateway.headers}) == 1


def test_the_source_defaults_to_the_synthetic_data_under_the_working_directory(
    configured: DatabaseHandle,
    stand_in: tuple[ScriptedGateway, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)

    result = runner.invoke(app, ["knowledge", "ingest", "--tenant", TENANT])

    assert result.exit_code == 0, result.output
    assert "chunks: 85" in result.stdout


def test_a_second_run_replaces_the_rows_it_does_not_add_to_them(
    configured: DatabaseHandle, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    first = runner.invoke(app, ingest_args())
    second = runner.invoke(app, ingest_args())

    assert (first.exit_code, second.exit_code) == (0, 0)
    assert chunk_count(configured) == 85


def test_the_http_client_factory_builds_a_client_on_the_gateway_address() -> None:
    with knowledge_cli.make_http_client(GATEWAY_URL) as http:
        assert str(http.base_url).rstrip("/") == GATEWAY_URL
        assert http.timeout.read is not None
        assert http.timeout.read >= 20  # the gateway's own limit per provider attempt


def test_the_http_client_ignores_proxy_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")

    with knowledge_cli.make_http_client(GATEWAY_URL) as http:
        assert http.trust_env is False


# ── a refused ingestion leaves one audit row ────────────────────────────────
SELECT_INGESTION_ROWS = (
    "SELECT service, event, outcome, tenant, agent, run_id, reference, reason, "
    "deployment, model, input_tokens, output_tokens, data_class, tool "
    "FROM audit.events WHERE service = 'knowledge-ingestion'"
)


def ingestion_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(db, SELECT_INGESTION_ROWS)


def refused_row(tenant: str | None, reason: str, run_id: uuid.UUID) -> tuple:
    return (
        "knowledge-ingestion",
        "knowledge.ingest",
        "refused",
        tenant,
        "knowledge-ingestion",
        run_id,
        "wordings",
        reason,
        None,
        None,
        None,
        None,
        None,
        None,
    )


def one_row(db: DatabaseHandle) -> tuple:
    (row,) = ingestion_rows(db)
    return row


def test_a_tenant_the_registry_knows_but_refuses_leaves_a_row_that_names_it(
    configured: DatabaseHandle, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    result = runner.invoke(app, ingest_args(tenant="evaluation"))

    row = one_row(configured)
    assert result.exit_code == 1
    assert row == refused_row("evaluation", "registry-refused", row[5])
    assert isinstance(row[5], uuid.UUID)


def test_a_tenant_the_registry_does_not_know_leaves_a_row_without_the_name(
    configured: DatabaseHandle, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    result = runner.invoke(app, ingest_args(tenant=f"ghost-{CANARY}"))

    row = one_row(configured)
    assert result.exit_code == 1
    assert row == refused_row(None, "registry-refused", row[5])
    assert CANARY not in str(owner_rows(configured, "SELECT * FROM audit.events"))


def test_a_source_that_is_not_the_generators_leaves_a_manifest_refused_row(
    configured: DatabaseHandle,
    stand_in: tuple[ScriptedGateway, list[str]],
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app, ["knowledge", "ingest", "--tenant", TENANT, "--from", str(tmp_path)]
    )

    row = one_row(configured)
    assert result.exit_code == 1
    assert row == refused_row(TENANT, "manifest-refused", row[5])
    # Neither the message nor the file name nor the path is on the row.
    assert "manifest.json" not in str(row)
    assert str(tmp_path) not in str(row)


def test_a_wording_that_breaks_a_rule_leaves_a_wording_refused_row(
    configured: DatabaseHandle,
    stand_in: tuple[ScriptedGateway, list[str]],
    source: Source,
) -> None:
    directory = source(
        lambda content: content.update(
            {
                "wordings/HOME-STD.md": content["wordings/HOME-STD.md"].replace(
                    "### 1.2 ", f"### 2.2 {CANARY} ", 1
                )
            }
        )
    )

    result = runner.invoke(
        app, ["knowledge", "ingest", "--tenant", TENANT, "--from", str(directory)]
    )

    row = one_row(configured)
    assert result.exit_code == 1
    assert row == refused_row(TENANT, "wording-refused", row[5])
    assert "HOME-STD" not in str(row)
    assert CANARY not in str(owner_rows(configured, "SELECT * FROM audit.events"))


def test_a_gateway_that_fails_leaves_a_gateway_failed_row_with_the_run_of_its_call(
    configured: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = ScriptedGateway(script=lambda index, inputs: httpx.Response(500, json={}))
    monkeypatch.setattr(knowledge_cli, "make_http_client", lambda url: failing.http())

    result = runner.invoke(app, ingest_args())

    row = one_row(configured)
    assert result.exit_code == 1
    assert row == refused_row(TENANT, "gateway-failed", row[5])
    assert str(row[5]) == failing.headers[0]["X-Meridian-Run"]
    assert chunk_count(configured) == 0


def test_a_gateway_that_stays_busy_leaves_a_gateway_busy_row(
    configured: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy = ScriptedGateway(script=lambda index, inputs: too_many())
    monkeypatch.setattr(knowledge_cli, "make_http_client", lambda url: busy.http())
    # No wait for real: the default of ``sleep`` is bound when the function is.
    monkeypatch.setattr(
        knowledge_cli,
        "ingest_wordings",
        functools.partial(ingest_wordings, sleep=lambda seconds: None),
    )

    result = runner.invoke(app, ingest_args())

    row = one_row(configured)
    assert result.exit_code == 1
    assert "429" in result.output
    assert len(busy.requests) == 3  # one answer, two retries, and then it stops
    assert row == refused_row(TENANT, "gateway-busy", row[5])


def test_a_connection_that_commits_by_itself_leaves_a_not_transactional_row(
    configured: DatabaseHandle,
    stand_in: tuple[ScriptedGateway, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, _ = stand_in

    def autocommitting(dsn: str, application_name: str) -> psycopg.Connection:
        conn = connect(dsn, application_name)
        conn.autocommit = True
        return conn

    monkeypatch.setattr(knowledge_cli, "connect", autocommitting)

    result = runner.invoke(app, ingest_args())

    row = one_row(configured)
    assert result.exit_code == 1
    assert "transaction" in result.output
    assert gateway.requests == []
    assert row == refused_row(TENANT, "not-transactional", row[5])
    assert chunk_count(configured) == 0


def test_a_refusal_leaves_exactly_one_row_and_no_other_audit_row_of_the_ingestion(
    configured: DatabaseHandle, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    runner.invoke(app, ingest_args(tenant="evaluation"))

    ((count,),) = owner_rows(configured, "SELECT count(*) FROM audit.events")
    assert count == 1


def test_a_completed_run_still_leaves_exactly_one_completed_row(
    configured: DatabaseHandle, stand_in: tuple[ScriptedGateway, list[str]]
) -> None:
    result = runner.invoke(app, ingest_args())

    row = one_row(configured)
    assert result.exit_code == 0, result.output
    assert row[:5] == (
        "knowledge-ingestion",
        "knowledge.ingest",
        "completed",
        TENANT,
        "knowledge-ingestion",
    )
    assert row[7] is None  # no reason
    ((count,),) = owner_rows(configured, "SELECT count(*) FROM audit.events")
    assert count == 1  # the stand-in gateway writes none


def test_a_refusal_that_cannot_be_audited_says_so_on_a_second_error_line(
    configured: DatabaseHandle,
    stand_in: tuple[ScriptedGateway, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(conn: psycopg.Connection, event: object) -> None:
        raise psycopg.OperationalError(f"server said {SECRET}")

    monkeypatch.setattr(knowledge_cli, "record_event", broken)

    result = runner.invoke(app, ingest_args(tenant="evaluation"))

    lines = result.output.splitlines()
    assert result.exit_code == 1
    assert len(lines) == 2
    assert lines[0].startswith("ERROR ") and "may not run agent" in lines[0]
    assert lines[1].startswith("ERROR ") and "audit" in lines[1]
    assert "OperationalError" in lines[1]
    assert SECRET not in result.output
    assert ingestion_rows(configured) == []


def test_a_refusal_that_cannot_be_audited_for_a_closed_connection_still_exits_1(
    configured: DatabaseHandle,
    stand_in: tuple[ScriptedGateway, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def closed(conn: psycopg.Connection, event: object) -> None:
        conn.close()
        conn.execute("SELECT 1")

    monkeypatch.setattr(knowledge_cli, "record_event", closed)

    result = runner.invoke(app, ingest_args(tenant="evaluation"))

    assert result.exit_code == 1
    assert len(result.output.splitlines()) == 2
