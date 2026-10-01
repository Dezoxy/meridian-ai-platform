"""The policy tool server (S013): ``policy_lookup`` and ``claim_history``
against the seeded policy store, through the SDK's in-process client."""

import json
import uuid
from typing import Any

import pytest
from dbsupport import OWNER, DatabaseHandle
from toolsupport import (
    AGENT,
    CLAIM,
    POLICY,
    SYNTHETIC_DIR,
    TENANT,
    World,
    add_claim,
    add_run,
    audit_rows,
    run_call,
    seed_world,
    settings_for,
    text_of,
)

from meridian.platform.common.db import connect
from meridian.platform.policy_mcp.app import create_app
from meridian.platform.toolserver.wire import META_CALL_ID

HISTORY_LIMIT = 100
FIRST_EXTRA_HISTORY_ID = 1000


def synthetic(name: str) -> list[dict[str, Any]]:
    return json.loads((SYNTHETIC_DIR / name).read_text(encoding="utf-8"))


def stored_fields(record: dict[str, Any]) -> dict[str, Any]:
    """What the tool should return for a generated policy: no holder, no
    insured object, nulls left out."""
    keys = [
        "policy_number",
        "product",
        "wording_version",
        "start_date",
        "end_date",
        "status",
        "lapsed_on",
        "deductible",
        "sum_insured",
        "limit",
    ]
    return {k: record[k] for k in keys if record[k] is not None}


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


@pytest.fixture
def server(world: World) -> Any:
    return create_app(settings_for(world.db, "policy_mcp")).server


def bind_to(world: World, policy_number: str, claim_id: str = "CLM-0004") -> uuid.UUID:
    add_claim(world.db, claim_id, policy_number=policy_number)
    return add_run(world.db, claim_id)


def lookup(server: Any, run_id: uuid.UUID, policy_number: str) -> Any:
    return run_call(
        server, "policy_lookup", {"policy_number": policy_number}, run_id=run_id
    )


def test_policy_lookup_returns_the_stored_fields_of_the_policy(
    world: World, server: Any
) -> None:
    expected = next(
        p for p in synthetic("policies.json") if p["policy_number"] == POLICY
    )

    result = lookup(server, world.run_id, POLICY)

    assert result.is_error is False
    assert result.structured_content == {
        "found": True,
        "policy": stored_fields(expected),
    }


def test_policy_lookup_returns_no_holder_name_email_or_address(
    world: World, server: Any
) -> None:
    policy = next(p for p in synthetic("policies.json") if p["policy_number"] == POLICY)

    result = lookup(server, world.run_id, POLICY)

    carried = text_of(result) + json.dumps(result.structured_content)
    for secret in (
        policy["holder"]["name"],
        policy["holder"]["email"],
        policy["holder"]["address"]["street"],
        "holder",
        "insured_object",
    ):
        assert secret not in carried


def test_a_lapsed_policy_carries_its_lapse_date(world: World, server: Any) -> None:
    lapsed = next(p for p in synthetic("policies.json") if p["status"] == "lapsed")
    run_id = bind_to(world, lapsed["policy_number"])

    result = lookup(server, run_id, lapsed["policy_number"])

    policy = result.structured_content["policy"]
    assert (policy["status"], policy["lapsed_on"]) == ("lapsed", lapsed["lapsed_on"])


def test_an_active_policy_has_no_lapse_date(world: World, server: Any) -> None:
    result = lookup(server, world.run_id, POLICY)

    assert "lapsed_on" not in result.structured_content["policy"]


def test_a_motor_tpl_policy_has_no_sum_insured(world: World, server: Any) -> None:
    motor = next(p for p in synthetic("policies.json") if p["product"] == "MOTOR-TPL")
    assert motor["sum_insured"] is None
    run_id = bind_to(world, motor["policy_number"])

    result = lookup(server, run_id, motor["policy_number"])

    policy = result.structured_content["policy"]
    assert policy["product"] == "MOTOR-TPL"
    assert "sum_insured" not in policy
    assert policy["limit"] == motor["limit"]


def test_a_claim_that_names_a_policy_nobody_stored_gets_found_false(
    world: World, server: Any
) -> None:
    run_id = bind_to(world, "POL-9999")

    result = lookup(server, run_id, "POL-9999")

    assert result.is_error is False
    assert result.structured_content == {"found": False}


def history(server: Any, run_id: uuid.UUID, policy_number: str) -> dict[str, Any]:
    result = run_call(
        server, "claim_history", {"policy_number": policy_number}, run_id=run_id
    )
    assert result.is_error is False
    return result.structured_content


def test_claim_history_lists_every_entry_newest_loss_first(
    world: World, server: Any
) -> None:
    rows = [
        h for h in synthetic("claim-history.json") if h["policy_number"] == "POL-0002"
    ]
    expected = sorted(rows, key=lambda h: h["history_id"])
    expected = sorted(expected, key=lambda h: h["loss_date"], reverse=True)
    run_id = bind_to(world, "POL-0002")

    answer = history(server, run_id, "POL-0002")

    assert len(expected) > 1
    assert answer["entries"] == [
        {k: h[k] for k in ("history_id", "loss_date", "peril", "paid_amount", "status")}
        for h in expected
    ]
    assert answer["truncated"] is False


def test_claim_history_of_a_policy_without_claims_is_empty(
    world: World, server: Any
) -> None:
    assert history(server, world.run_id, POLICY) == {"entries": [], "truncated": False}


def add_history(world: World, count: int) -> None:
    with connect(world.db.dsn(OWNER), "test-seed") as conn:
        conn.cursor().executemany(
            "INSERT INTO policy.claim_history "
            "(history_id, policy_number, loss_date, peril, paid_amount, status) "
            "VALUES (%s, %s, '2025-01-01', 'storm', 10, 'closed')",
            [(f"HIST-{FIRST_EXTRA_HISTORY_ID + n}", POLICY) for n in range(count)],
        )


def test_a_hundred_and_one_entries_give_a_hundred_and_truncated(
    world: World, server: Any
) -> None:
    add_history(world, HISTORY_LIMIT + 1)

    answer = history(server, world.run_id, POLICY)

    assert len(answer["entries"]) == HISTORY_LIMIT
    assert answer["truncated"] is True
    assert answer["entries"][0]["history_id"] == f"HIST-{FIRST_EXTRA_HISTORY_ID}"


def test_exactly_a_hundred_entries_are_not_truncated(world: World, server: Any) -> None:
    add_history(world, HISTORY_LIMIT)

    answer = history(server, world.run_id, POLICY)

    assert len(answer["entries"]) == HISTORY_LIMIT
    assert answer["truncated"] is False


def test_a_completed_call_is_audited_with_the_tool_the_run_and_the_call_id(
    world: World, server: Any
) -> None:
    result = lookup(server, world.run_id, POLICY)

    (row,) = audit_rows(world.db)
    assert row["service"] == "policy-mcp"
    assert row["event"] == "tool.call"
    assert row["outcome"] == "completed"
    assert row["tool"] == "policy_lookup"
    assert (row["tenant"], row["agent"], row["run_id"], row["reference"]) == (
        TENANT,
        AGENT,
        world.run_id,
        CLAIM,
    )
    assert row["call_id"] == uuid.UUID(result.meta[META_CALL_ID])
    assert row["reason"] is None
