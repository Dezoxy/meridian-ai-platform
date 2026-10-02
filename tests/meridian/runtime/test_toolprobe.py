"""The tool probe (S044): one call per tool server through the runtime's client.

Two tests reach the three real servers over HTTP, on loopback ports and each on
its own database role (they need a database: ``make pytest-db``). Every other
test needs none: it uses the in-process stand-in (``StandIn``) through the
probe's ``targets`` argument, or addresses nobody listens on.
"""

import json
import logging
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from dbsupport import DatabaseHandle
from servicesupport import REGISTRY_DIR, REPO_ROOT
from toolsupport import (
    CANARY,
    GATEWAY_URL,
    StandIn,
    application_log,
    audit_rows,
    holds,
    knowledge_settings_for,
    refused,
    serve,
    settings_for,
    unused_port,
)

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.knowledge_mcp.app import create_app as create_knowledge_app
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.wire import META_IDEMPOTENCY_KEY, META_RUN
from meridian.runtime.toolprobe import Answer, main, succeeded
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

DSN = "postgresql://agent_runtime:s3cret-value@db.invalid/meridian"
HOSTS = ("127.0.0.1:*",)
# The registry's servers in file order, each with the first tool of an agent's
# allowlist that it serves.
PAIRS = [
    ("policy-mcp", "policy_lookup"),
    ("knowledge-mcp", "wording_search"),
    ("claims-mcp", "add_claim_note"),
]


def environ_for(servers: Mapping[str, str], registry_dir: Path = REGISTRY_DIR) -> dict:
    return {
        "MERIDIAN_GATEWAY_URL": "http://gateway.invalid:8080",
        "MERIDIAN_DATABASE_URL": DSN,
        "MERIDIAN_REGISTRY_DIR": str(registry_dir),
        "MERIDIAN_TOOL_SERVERS": json.dumps(servers),
    }


def probe(
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    targets: Mapping[str, Any] | None = None,
) -> tuple[int, list[str], str]:
    """The exit status, the stdout lines and the stderr text of one run."""
    status = main(environ, targets=targets)
    captured = capsys.readouterr()
    return status, captured.out.splitlines(), captured.err


def ours(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """The records of the application's own loggers: the HTTP libraries log an
    address at DEBUG and INFO, which the probe's default logging never writes."""
    return [r for r in records if r.name.startswith("meridian.")]


def every_server_unreachable() -> dict[str, str]:
    return {server: f"http://127.0.0.1:{unused_port()}" for server, _ in PAIRS}


@contextmanager
def real_servers(db: DatabaseHandle) -> Iterator[dict[str, str]]:
    """The three tool servers on loopback ports, each on its own database role;
    yield their addresses."""
    gateway = httpx.Client(base_url=GATEWAY_URL)
    apps = {
        "policy-mcp": create_policy_app(
            settings_for(db, "policy_mcp", hosts=HOSTS),
            tracer_provider=make_tracer_provider("policy-mcp"),
        ),
        "knowledge-mcp": create_knowledge_app(
            knowledge_settings_for(db, hosts=HOSTS),
            http=gateway,
            tracer_provider=make_tracer_provider("knowledge-mcp"),
        ),
        "claims-mcp": create_claims_app(
            settings_for(db, "claims_mcp", hosts=HOSTS),
            tracer_provider=make_tracer_provider("claims-mcp"),
        ),
    }
    try:
        with ExitStack() as stack:
            yield {
                server: stack.enter_context(serve(app.app))
                for server, app in apps.items()
            }
    finally:
        gateway.close()


# ── the three real servers ──────────────────────────────────────────────────
def test_the_three_real_servers_each_answer_unknown_run_and_the_probe_exits_0(
    fresh_database: DatabaseHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    with real_servers(fresh_database) as servers:
        status, lines, err = probe(capsys, environ_for(servers))

    assert lines == [f"{server} {tool} unknown-run" for server, tool in PAIRS]
    assert err == ""
    assert status == 0


def test_a_refused_probe_leaves_no_more_than_the_reason_word(
    fresh_database: DatabaseHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    with application_log() as records, real_servers(fresh_database) as servers:
        status, lines, err = probe(capsys, environ_for(servers))

    assert status == 0
    rows = audit_rows(fresh_database)
    assert sorted((row["service"], row["tool"]) for row in rows) == sorted(
        (server, tool) for server, tool in PAIRS
    )
    for row in rows:
        # The run does not exist, so the row names no tenant, agent or claim.
        assert (row["event"], row["outcome"], row["reason"]) == (
            "tool.call",
            "refused",
            "unknown-run",
        )
        assert (row["tenant"], row["agent"], row["reference"]) == (None, None, None)
    # Nothing of the call or of a server's address: the lines are the three
    # answers and the logs hold no warning or error.
    assert not [r for r in records if r.levelno >= 30]
    assert not holds(ours(records), "127.0.0.1")
    assert err == ""
    assert all(re.fullmatch(r"[a-z-]+ [a-z_]+ [a-z-]+", line) for line in lines)


# ── a server the probe cannot reach ─────────────────────────────────────────
def test_a_server_with_no_address_is_no_address_and_is_not_called(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stand_in = StandIn()

    with application_log() as records:
        status, lines, _ = probe(
            capsys,
            environ_for({}),
            targets={
                "knowledge-mcp": stand_in.server,
                "claims-mcp": stand_in.server,
            },
        )

    assert lines == [
        "policy-mcp policy_lookup no-address",
        "knowledge-mcp wording_search completed",
        "claims-mcp add_claim_note completed",
    ]
    assert status == 1
    # The probe says it itself: the client was not asked, so it logged nothing
    # about a missing address.
    assert not holds(records, "has no address")
    assert [seen.name for seen in stand_in.calls] == [
        "wording_search",
        "add_claim_note",
    ]


def test_a_server_nobody_listens_on_is_unavailable_and_names_no_address(
    capsys: pytest.CaptureFixture[str],
) -> None:
    servers = every_server_unreachable()
    ports = [address.rsplit(":", 1)[1] for address in servers.values()]

    with application_log() as records:
        status, lines, err = probe(capsys, environ_for(servers))

    assert lines == [f"{server} {tool} unavailable" for server, tool in PAIRS]
    assert status == 1
    for port in ports:
        assert port not in "\n".join(lines) + err
        assert not holds(ours(records), port)
    assert [r.getMessage() for r in ours(records)] == [
        "tool call failed: ExceptionGroup(ConnectError)"
    ] * len(PAIRS)


def test_the_module_run_as_a_script_leaves_only_the_answers_and_a_class_name() -> None:
    servers = every_server_unreachable()
    environ = {**os.environ, **environ_for(servers)}

    done = subprocess.run(
        [sys.executable, "-m", "meridian.runtime.toolprobe"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        env=environ,
        check=False,
    )

    assert done.returncode == 1
    assert done.stdout.splitlines() == [
        f"{server} {tool} unavailable" for server, tool in PAIRS
    ]
    # The default logging writes the client's own line: a class name.
    assert set(done.stderr.splitlines()) == {
        "tool call failed: ExceptionGroup(ConnectError)"
    }
    for address in servers.values():
        assert address.rsplit(":", 1)[1] not in done.stdout + done.stderr


# ── a server that answers something else ────────────────────────────────────
def test_a_server_that_completes_the_call_is_completed_and_fails_the_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stand_in = StandIn()
    targets = {server: stand_in.server for server, _ in PAIRS}

    status, lines, _ = probe(capsys, environ_for({}), targets=targets)

    assert lines == [f"{server} {tool} completed" for server, tool in PAIRS]
    assert status == 1


def test_another_refusal_prints_its_reason_word_and_fails_the_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stand_in = StandIn(answer=lambda name, args: refused("run-not-running"))
    targets = {server: stand_in.server for server, _ in PAIRS}

    status, lines, _ = probe(capsys, environ_for({}), targets=targets)

    assert lines == [f"{server} {tool} run-not-running" for server, tool in PAIRS]
    assert status == 1


def test_a_refusal_with_a_made_up_reason_prints_unknown_and_nothing_of_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stand_in = StandIn(answer=lambda name, args: refused(f"because-{CANARY}"))
    targets = {server: stand_in.server for server, _ in PAIRS}

    status, lines, err = probe(capsys, environ_for({}), targets=targets)

    assert lines == [f"{server} {tool} unknown" for server, tool in PAIRS]
    assert CANARY not in "\n".join(lines) + err
    assert status == 1


# ── the registry decides what is called ─────────────────────────────────────
def test_a_server_none_of_whose_tools_is_on_an_allowlist_is_no_tool(
    plant: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    registry_dir = plant(("agents.yaml", "      - wording_search\n", ""))
    stand_in = StandIn()
    targets = {server: stand_in.server for server, _ in PAIRS}

    status, lines, _ = probe(
        capsys, environ_for({}, registry_dir=registry_dir), targets=targets
    )

    assert lines == [
        "policy-mcp policy_lookup completed",
        "knowledge-mcp - no-tool",
        "claims-mcp add_claim_note completed",
    ]
    assert [seen.name for seen in stand_in.calls] == ["policy_lookup", "add_claim_note"]
    assert status == 1


def test_the_probe_sends_a_fresh_run_and_a_key_only_where_the_tool_needs_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stand_in = StandIn()
    targets = {server: stand_in.server for server, _ in PAIRS}
    registry = load_registry(REGISTRY_DIR)
    (claims_tool,) = [t for t in registry.tools if t.id == "add_claim_note"]
    assert claims_tool.idempotency_key_required  # the premise

    probe(capsys, environ_for({}), targets=targets)
    probe(capsys, environ_for({}), targets=targets)

    runs = [seen.meta[META_RUN] for seen in stand_in.calls]
    assert len(runs) == 6
    assert len(set(runs)) == 6
    for run in runs:
        uuid.UUID(run)
    for seen in stand_in.calls:
        assert seen.arguments == {}
        assert (META_IDEMPOTENCY_KEY in seen.meta) == (seen.name == "add_claim_note")


# ── the exit status ─────────────────────────────────────────────────────────
def test_the_probe_succeeds_only_with_a_server_and_every_answer_unknown_run() -> None:
    unknown = Answer("policy-mcp", "policy_lookup", "unknown-run")

    assert succeeded([unknown]) is True
    completed = Answer("claims-mcp", "add_claim_note", "completed")
    unavailable = Answer("claims-mcp", "add_claim_note", "unavailable")
    assert succeeded([unknown, completed]) is False
    assert succeeded([unknown, unavailable]) is False
    assert succeeded([]) is False


# ── settings and registry that do not load ──────────────────────────────────
def test_a_missing_variable_is_one_error_line_on_stderr_and_nothing_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {k: v for k, v in environ_for({}).items() if k != "MERIDIAN_GATEWAY_URL"}

    status, lines, err = probe(capsys, environ)

    assert status == 1
    assert lines == []
    assert err == "ERROR MERIDIAN_GATEWAY_URL is required\n"
    assert "s3cret-value" not in err


def test_a_present_but_invalid_setting_is_one_error_line_naming_the_field_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = f"not-a-url-{CANARY}"
    environ = {**environ_for({}), "MERIDIAN_GATEWAY_URL": value}

    status, lines, err = probe(capsys, environ)

    assert status == 1
    assert lines == []
    assert err == "ERROR the runtime's settings are not valid: gateway_url\n"
    # Neither the value nor pydantic's own text.
    assert CANARY not in err
    assert "validation error" not in err
    assert "s3cret-value" not in err


def test_a_registry_that_does_not_load_is_one_error_line_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, lines, err = probe(capsys, environ_for({}, registry_dir=tmp_path))

    assert status == 1
    assert lines == []
    assert err.startswith("ERROR ")
    assert err.count("\n") == 1
    assert "s3cret-value" not in err
