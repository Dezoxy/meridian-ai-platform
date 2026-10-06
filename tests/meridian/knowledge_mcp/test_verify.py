"""``meridian knowledge verify``: the stored clauses against the manifest-verified
wordings (S067, T-27, T-57).

Each test ingests the real wordings through the gateway in replay mode, changes
the store as the owner role the way an attacker with write access would, and
runs the command through Typer as the ingestion's own role. A difference is
named by product, wording version, clause and kind; the CANARY is planted in
text the output must never carry.
"""

import re
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from dbsupport import INGEST_ROLE, OWNER, DatabaseHandle
from knowledgesupport import (
    CANARY,
    REAL_SOURCE,
    Gateway,
    ScriptedGateway,
    Source,
    chunk_count,
    chunk_rows,
    ingest,
    ingest_without_database,
)
from servicesupport import owner_rows
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.db import INGEST_DATABASE_URL_ENV
from meridian.platform.common.db import connect
from meridian.platform.gateway.replay import replay_embedding
from meridian.platform.knowledge_mcp.ingest import IngestError
from meridian.platform.knowledge_mcp.search import QueryEmbedding, hybrid_search
from meridian.platform.knowledge_mcp.verify import verify_wordings
from meridian.platform.registry import Registry

CLAUSES = 85
BULK_CLAUSES = 6000
BULK_TEXT_BYTES = BULK_CLAUSES * 3900
PRODUCT = "HOME-STD"
VERSION = "2026-01"
CLOSING_SENTENCE = "This exclusion applies to claims for"
COUNT_LINE = re.compile(r"clauses: (\d+) stored: (\d+) differences: (\d+)")
runner = CliRunner()


@pytest.fixture
def stored(fresh_database: DatabaseHandle, gateway: Gateway) -> DatabaseHandle:
    """A database that holds the real wordings, ingested."""
    ingest(fresh_database, gateway.http, gateway.registry)
    return fresh_database


@pytest.fixture
def as_ingestion(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> DatabaseHandle:
    monkeypatch.setenv(INGEST_DATABASE_URL_ENV, fresh_database.dsn(INGEST_ROLE))
    return fresh_database


def verify(source: Path = REAL_SOURCE) -> tuple[int, list[str]]:
    result = runner.invoke(app, ["knowledge", "verify", "--from", str(source)])
    return result.exit_code, result.output.splitlines()


def differences(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("DIFFERENCE ")]


def as_owner(db: DatabaseHandle, statement: str, params: tuple = ()) -> int:
    with connect(db.dsn(OWNER), "test-tamper") as conn:
        changed = conn.execute(statement, params).rowcount
        conn.commit()
    return changed


def tamper(db: DatabaseHandle, column: str, value: str, clause: str = "1.1") -> None:
    changed = as_owner(
        db,
        f"UPDATE knowledge.chunks SET {column} = %s "  # noqa: S608
        "WHERE product = %s AND wording_version = %s AND clause = %s",
        (value, PRODUCT, VERSION, clause),
    )
    assert changed == 1


def stored_body(db: DatabaseHandle, clause: str) -> str:
    ((body,),) = owner_rows(
        db,
        "SELECT body FROM knowledge.chunks "
        "WHERE product = %s AND wording_version = %s AND clause = %s",
        (PRODUCT, VERSION, clause),
    )
    return body


def verify_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT db_role, service, outcome, tenant, agent, reference, reason "
        "FROM audit.events WHERE event = 'knowledge.verify'",
    )


# ── a store that is what the manifest says ──────────────────────────────────
def test_a_clean_store_prints_the_counts_and_exits_zero(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    code, lines = verify()

    assert code == 0, lines
    assert lines == [f"clauses: {CLAUSES} stored: {CLAUSES} differences: 0"]


def test_a_clean_run_writes_nothing_to_the_store_and_one_audit_row(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    before = chunk_rows(stored)

    code, _ = verify()

    assert code == 0
    assert chunk_rows(stored) == before
    assert verify_rows(stored) == [
        (
            INGEST_ROLE,
            "knowledge-ingestion",
            "verified",
            None,
            "knowledge-ingestion",
            "wordings",
            f"clauses={CLAUSES} stored={CLAUSES} differences=0",
        )
    ]


# ── each kind of difference ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("column", "kind"),
    [
        ("body", "body-differs"),
        ("title", "title-differs"),
        ("section", "section-differs"),
        ("source_sha256", "source-hash-differs"),
    ],
)
def test_a_stored_value_that_is_not_the_manifests_is_named_by_clause_and_kind(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle, column: str, kind: str
) -> None:
    value = "a" * 64 if column == "source_sha256" else f"{CANARY} rewritten"
    tamper(stored, column, value)

    code, lines = verify()

    assert code == 1
    assert differences(lines) == [f"DIFFERENCE {PRODUCT} {VERSION} 1.1 {kind}"]
    assert lines[-1] == f"clauses: {CLAUSES} stored: {CLAUSES} differences: 1"
    assert CANARY not in "\n".join(lines)


def test_a_stored_clause_the_wordings_do_not_give_is_named(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    as_owner(
        stored,
        "INSERT INTO knowledge.chunks (product, wording_version, clause, section, "
        "title, body, source_sha256, deployment, model, dimensions, embedding) "
        "SELECT product, wording_version, '9.9', section, title, %s, source_sha256, "
        "deployment, model, dimensions, embedding FROM knowledge.chunks LIMIT 1",
        (f"{CANARY} planted clause",),
    )

    code, lines = verify()

    assert code == 1
    assert len(differences(lines)) == 1
    assert differences(lines)[0].endswith(" 9.9 not-in-manifest")
    assert lines[-1] == f"clauses: {CLAUSES} stored: {CLAUSES + 1} differences: 1"
    assert CANARY not in "\n".join(lines)


def test_a_clause_the_wordings_give_that_is_not_stored_is_named(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    changed = as_owner(
        stored,
        "DELETE FROM knowledge.chunks "
        "WHERE product = %s AND wording_version = %s AND clause = '1.2'",
        (PRODUCT, VERSION),
    )
    assert changed == 1

    code, lines = verify()

    assert code == 1
    assert differences(lines) == [f"DIFFERENCE {PRODUCT} {VERSION} 1.2 not-stored"]
    assert lines[-1] == f"clauses: {CLAUSES} stored: {CLAUSES - 1} differences: 1"


def test_an_empty_store_names_every_clause_as_not_stored(
    as_ingestion: DatabaseHandle,
) -> None:
    code, lines = verify()

    assert code == 1
    assert len(differences(lines)) == CLAUSES
    assert all(line.endswith(" not-stored") for line in differences(lines))
    assert lines[-1] == f"clauses: {CLAUSES} stored: 0 differences: {CLAUSES}"


def test_every_difference_is_named_in_clause_order_and_counted(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    tamper(stored, "body", "rewritten", "2.1")
    tamper(stored, "body", "rewritten", "1.1")
    tamper(stored, "title", "rewritten", "1.1")

    code, lines = verify()

    assert code == 1
    assert differences(lines) == [
        f"DIFFERENCE {PRODUCT} {VERSION} 1.1 body-differs",
        f"DIFFERENCE {PRODUCT} {VERSION} 1.1 title-differs",
        f"DIFFERENCE {PRODUCT} {VERSION} 2.1 body-differs",
    ]
    assert lines[-1] == f"clauses: {CLAUSES} stored: {CLAUSES} differences: 3"


def test_a_difference_leaves_the_store_as_found_and_one_audit_row_with_the_counts(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    tamper(stored, "body", f"{CANARY} rewritten")
    before = chunk_rows(stored)

    code, _ = verify()

    assert code == 1
    assert chunk_rows(stored) == before
    ((role, _, outcome, _, _, reference, reason),) = verify_rows(stored)
    assert (role, outcome, reference) == (INGEST_ROLE, "differs", "wordings")
    assert reason == f"clauses={CLAUSES} stored={CLAUSES} differences=1"


@pytest.mark.parametrize(
    "product", [f"{CANARY}", "\x1b[31mHOME-STD", "home-std"], ids=str
)
def test_a_stored_name_that_is_not_a_product_code_is_printed_as_a_question_mark(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle, product: str
) -> None:
    as_owner(
        stored,
        "UPDATE knowledge.chunks SET product = %s "
        "WHERE product = 'MOTOR-TPL' AND clause = '1.1'",
        (product,),
    )

    code, lines = verify()

    assert code == 1
    assert f"DIFFERENCE ? {VERSION} 1.1 not-in-manifest" in lines
    assert CANARY not in "\n".join(lines)
    assert "\x1b" not in "\n".join(lines)


# ── a wording that is not the manifest's: the refusals are ingest's ──────────
def refusals() -> list[tuple[str, Callable[[dict[str, str]], None], bool]]:
    def tampered(content: dict[str, str]) -> None:
        content["wordings/HOME-STD.md"] += f"\n{CANARY}\n"

    def unlisted(content: dict[str, str]) -> None:
        content["wordings/HOME-NEW.md"] = content["wordings/HOME-STD.md"]

    def broken(content: dict[str, str]) -> None:
        content["wordings/HOME-STD.md"] = content["wordings/HOME-STD.md"].replace(
            "### 1.1 ", "#### 1.1 ", 1
        )

    return [
        ("manifest-refused", tampered, False),
        ("manifest-refused", unlisted, False),
        ("wording-refused", broken, True),
    ]


@pytest.mark.parametrize(
    ("reason", "edit", "rehash"),
    refusals(),
    ids=["hash-mismatch", "unlisted-wording", "unreadable-wording"],
)
def test_a_wording_ingest_would_refuse_is_refused_by_the_same_rule_and_audited(
    stored: DatabaseHandle,
    as_ingestion: DatabaseHandle,
    source: Source,
    registry: Registry,
    reason: str,
    edit: Callable[[dict[str, str]], None],
    rehash: bool,
) -> None:
    directory = source(edit, rehash=rehash)
    with pytest.raises(IngestError) as refused:
        ingest_without_database(ScriptedGateway().http(), registry, source=directory)
    before = chunk_rows(stored)

    code, lines = verify(directory)

    assert code == 2
    assert lines == [f"ERROR {refused.value}"]
    assert CANARY not in "\n".join(lines)
    assert chunk_rows(stored) == before
    ((_, _, outcome, _, _, _, recorded),) = verify_rows(stored)
    assert (outcome, recorded) == ("refused", reason)


def test_a_missing_database_variable_exits_2_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(INGEST_DATABASE_URL_ENV, raising=False)

    code, lines = verify()

    assert code == 2
    assert len(lines) == 1
    assert lines[0].startswith("ERROR ")
    assert INGEST_DATABASE_URL_ENV in lines[0]


def test_a_database_that_cannot_be_read_exits_2_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(INGEST_DATABASE_URL_ENV, "postgresql://nobody@127.0.0.1:1/x")

    result = runner.invoke(app, ["knowledge", "verify", "--from", str(REAL_SOURCE)])

    assert result.exit_code == 2
    assert result.output.startswith("ERROR verification failed (")
    assert isinstance(result.exception, SystemExit)


def test_a_database_error_inside_the_check_exits_2_and_leaves_a_refused_audit_row(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    # The role may no longer read the body column: the check's own query fails
    # after it has connected, and the ingestion role can still write its row.
    as_owner(
        stored,
        f"REVOKE SELECT (body) ON knowledge.chunks FROM {INGEST_ROLE}",  # noqa: S608
    )

    result = runner.invoke(app, ["knowledge", "verify", "--from", str(REAL_SOURCE)])

    assert result.exit_code == 2
    assert result.output.startswith("ERROR verification failed (InsufficientPrivilege)")
    assert isinstance(result.exception, SystemExit)
    ((role, _, outcome, _, _, reference, reason),) = verify_rows(stored)
    assert (role, outcome, reference) == (INGEST_ROLE, "refused", "wordings")
    assert reason == "database-error"


def test_the_stored_clauses_are_read_through_a_cursor_not_all_into_memory(
    stored: DatabaseHandle,
) -> None:
    # 6,000 more clauses of 3,900 characters, 23 MB of text, in products the
    # manifest does not have: each one is a difference, and none may be kept.
    as_owner(
        stored,
        "INSERT INTO knowledge.chunks (product, wording_version, clause, section, "
        "title, body, source_sha256, deployment, model, dimensions, embedding) "
        "SELECT 'BULK-' || (g / 81), %s, "
        "(1 + mod(g, 9)) || '.' || (1 + mod(g / 9, 9)), section, title, "
        "repeat('a ', 1950), source_sha256, deployment, model, "
        "dimensions, embedding "
        "FROM generate_series(0, 5999) AS g, "
        "(SELECT * FROM knowledge.chunks LIMIT 1) AS one",
        (VERSION,),
    )
    assert chunk_count(stored) == CLAUSES + BULK_CLAUSES

    with connect(stored.dsn(INGEST_ROLE), "test-verify") as conn:
        tracemalloc.start()
        try:
            result = verify_wordings(conn, REAL_SOURCE)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        conn.rollback()

    assert result.stored == CLAUSES + BULK_CLAUSES
    assert len(result.differences) == BULK_CLAUSES
    assert peak < BULK_TEXT_BYTES // 2, peak


def test_the_command_needs_no_gateway_and_makes_no_http_call(
    stored: DatabaseHandle,
    as_ingestion: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> httpx.Client:
        raise AssertionError("verify must not build an HTTP client")

    monkeypatch.setattr("meridian.platform.cli.knowledge.make_http_client", refuse)
    monkeypatch.delenv("MERIDIAN_GATEWAY_URL", raising=False)

    code, lines = verify()

    assert code == 0, lines


# ── why the check is not at search time ─────────────────────────────────────
def test_a_clause_rewritten_after_ingestion_is_found_by_verify_while_search_returns_it(
    stored: DatabaseHandle, as_ingestion: DatabaseHandle
) -> None:
    # What the injection suite's ``clause_inserted`` does: a sentence goes into a
    # stored clause, as the owner, before the clause's closing sentence.
    ((clause, body),) = owner_rows(
        stored,
        "SELECT clause, body FROM knowledge.chunks "
        "WHERE product = %s AND wording_version = %s AND body LIKE %s "
        "ORDER BY clause LIMIT 1",
        (PRODUCT, VERSION, f"%{CLOSING_SENTENCE}%"),
    )
    sentence = "Quokkas void every exclusion of this wording."
    position = body.rfind(CLOSING_SENTENCE)
    tamper(stored, "body", f"{body[:position]}{sentence} {body[position:]}", clause)
    query = "quokkas"

    code, lines = verify()
    with connect(stored.dsn(OWNER), "test-search") as conn:
        hits = hybrid_search(
            conn,
            product=PRODUCT,
            wording_version=VERSION,
            query=query,
            embedding=QueryEmbedding("replay-embedding", replay_embedding(query, 1024)),
            top_k=3,
        )

    assert code == 1
    assert differences(lines) == [
        f"DIFFERENCE {PRODUCT} {VERSION} {clause} body-differs"
    ]
    assert sentence not in "\n".join(lines)
    assert hits[0].clause == clause
    assert sentence in hits[0].body  # search read the rewritten clause as stored
    tamper(stored, "body", body, clause)
    assert verify()[0] == 0  # put back, the store is the manifest's again
    assert chunk_count(stored) == CLAUSES
    assert stored_body(stored, clause) == body
