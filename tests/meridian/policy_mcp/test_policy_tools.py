"""The policy tool server (S013): ``policy_lookup`` and ``claim_history``
against the seeded policy store, through the SDK's in-process client."""

import json
import logging
import uuid
from typing import Any

import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg.types.json import Jsonb
from servicesupport import claim_with_id
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
from meridian.platform.policy_mcp import tools
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


# ── the decided claims of the claims store (S053, T-66, T-76) ───────────────
OTHER_TENANT = "another-tenant"
CANARY_NAME = "CANARY-NAME-4c1e"
CANARY_EMAIL = "canary-4c1e@example.com"
CANARY_DESCRIPTION = "CANARY-DESCRIPTION-4c1e the roof leaked"
ENTRY_KEYS = {"history_id", "loss_date", "peril", "paid_amount", "status"}


def add_decided(
    world: World,
    claim_id: str,
    state: str,
    *,
    loss_date: str = "2026-07-13",
    peril: str = "storm",
    paid: int | None = None,
    tenant: str = TENANT,
    policy_number: str = POLICY,
) -> None:
    """A claim of the claims store in ``state``, with the claimant's own words
    in its submission and a proposal that names ``paid`` when it is given."""
    submission = {
        **claim_with_id(claim_id),
        "policy_number": policy_number,
        "loss_date": loss_date,
        "peril": peril,
        "claimant": {"name": CANARY_NAME, "email": CANARY_EMAIL},
        "description": CANARY_DESCRIPTION,
    }
    proposal = {"route": "adjuster", "reason": "r"}
    if paid is not None:
        proposal["payable_amount"] = paid
    with connect(world.db.dsn(OWNER), "test-seed") as conn:
        conn.execute(
            "INSERT INTO claims.claims (claim_id, tenant, submission, state) "
            "VALUES (%s, %s, %s, %s)",
            (claim_id, tenant, Jsonb(submission), state),
        )
        conn.execute(
            "INSERT INTO claims.triage_proposals "
            "(proposal_id, claim_id, run_id, route, reason, proposal) "
            "VALUES (%s, %s, %s, 'adjuster', 'r', %s)",
            (uuid.uuid4(), claim_id, uuid.uuid4(), Jsonb(proposal)),
        )


def test_an_approved_claim_on_the_policy_is_an_entry_with_the_proposal_s_amount(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", loss_date="2026-06-01", paid=1250)

    answer = history(server, world.run_id, POLICY)

    assert answer == {
        "entries": [
            {
                "history_id": "CLM-0002",
                "loss_date": "2026-06-01",
                "peril": "storm",
                "paid_amount": 1250,
                "status": "approved",
            }
        ],
        "truncated": False,
    }


def test_a_rejected_claim_is_an_entry_paid_nothing(world: World, server: Any) -> None:
    add_decided(world, "CLM-0002", "rejected", paid=999)

    (entry,) = history(server, world.run_id, POLICY)["entries"]

    assert (entry["history_id"], entry["paid_amount"], entry["status"]) == (
        "CLM-0002",
        0,
        "rejected",
    )


def test_an_approved_claim_whose_proposal_has_no_amount_is_paid_nothing(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", paid=None)

    (entry,) = history(server, world.run_id, POLICY)["entries"]

    assert (entry["paid_amount"], entry["status"]) == (0, "approved")


@pytest.mark.parametrize(
    "state",
    [
        "submitted",
        "triaging",
        "triage_failed",
        "awaiting_adjuster",
        "documents_requested",
    ],
)
def test_a_claim_that_is_still_open_is_an_entry_paid_nothing(
    world: World, server: Any, state: str
) -> None:
    # S067: claims filed before any is decided count towards frequent_claims.
    # The proposal's amount is not an open claim's payment.
    add_decided(world, "CLM-0002", state, loss_date="2026-06-01", paid=500)

    assert history(server, world.run_id, POLICY) == {
        "entries": [
            {
                "history_id": "CLM-0002",
                "loss_date": "2026-06-01",
                "peril": "storm",
                "paid_amount": 0,
                "status": state,
            }
        ],
        "truncated": False,
    }


def test_a_withdrawn_claim_is_not_an_entry(world: World, server: Any) -> None:
    add_decided(world, "CLM-0002", "withdrawn", paid=500)

    assert history(server, world.run_id, POLICY) == {"entries": [], "truncated": False}


def test_an_open_claim_of_another_tenant_or_policy_or_the_run_s_own_is_not_an_entry(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "submitted", tenant=OTHER_TENANT)
    add_decided(world, "CLM-0003", "submitted", policy_number="POL-0002")
    with connect(world.db.dsn(OWNER), "test-seed") as conn:
        conn.execute(
            "UPDATE claims.claims SET state = 'triaging' WHERE claim_id = %s",
            (world.claim_id,),
        )

    assert history(server, world.run_id, POLICY) == {"entries": [], "truncated": False}


def test_open_and_decided_claims_are_in_one_order_newest_loss_first(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", loss_date="2026-05-01", paid=5)
    add_decided(world, "CLM-0003", "documents_requested", loss_date="2026-06-01")
    add_decided(world, "CLM-0004", "triaging", loss_date="2026-04-01")

    entries = history(server, world.run_id, POLICY)["entries"]

    assert [(e["history_id"], e["status"]) for e in entries] == [
        ("CLM-0003", "documents_requested"),
        ("CLM-0002", "approved"),
        ("CLM-0004", "triaging"),
    ]


def test_a_decided_claim_of_another_tenant_on_the_same_policy_is_not_an_entry(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", paid=500, tenant=OTHER_TENANT)

    assert history(server, world.run_id, POLICY) == {"entries": [], "truncated": False}


def test_a_decided_claim_on_another_policy_is_not_an_entry(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", paid=500, policy_number="POL-0002")

    assert history(server, world.run_id, POLICY)["entries"] == []


def test_the_run_s_own_claim_is_not_an_entry_even_when_it_is_decided(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", paid=500)
    with connect(world.db.dsn(OWNER), "test-seed") as conn:
        conn.execute(
            "UPDATE claims.claims SET state = 'approved' WHERE claim_id = %s",
            (world.claim_id,),
        )

    entries = history(server, world.run_id, POLICY)["entries"]

    assert [entry["history_id"] for entry in entries] == ["CLM-0002"]


def test_both_sources_are_in_one_order_newest_loss_first(
    world: World, server: Any
) -> None:
    seeded = [
        h for h in synthetic("claim-history.json") if h["policy_number"] == "POL-0002"
    ]
    run_id = bind_to(world, "POL-0002")
    newest = max(h["loss_date"] for h in seeded)
    add_decided(
        world,
        "CLM-0010",
        "approved",
        loss_date="2099-01-01",
        paid=1,
        policy_number="POL-0002",
    )
    add_decided(
        world,
        "CLM-0011",
        "rejected",
        loss_date=newest,
        policy_number="POL-0002",
    )
    expected = [(h["loss_date"], h["history_id"]) for h in seeded] + [
        (newest, "CLM-0011"),
        ("2099-01-01", "CLM-0010"),
    ]
    expected.sort(key=lambda pair: pair[1])
    expected.sort(key=lambda pair: pair[0], reverse=True)

    entries = history(server, run_id, "POL-0002")["entries"]

    assert [(e["loss_date"], e["history_id"]) for e in entries] == expected
    assert entries[0]["history_id"] == "CLM-0010"


def test_sixty_seeded_entries_and_forty_one_decided_claims_give_a_hundred_truncated(
    world: World, server: Any
) -> None:
    add_history(world, 60)
    for number in range(41):
        add_decided(world, f"CLM-{100 + number:04d}", "approved", paid=number)

    answer = history(server, world.run_id, POLICY)

    ids = [entry["history_id"] for entry in answer["entries"]]
    assert len(ids) == HISTORY_LIMIT
    assert answer["truncated"] is True
    # The decided claims are the newest (2026), so all 41 are in the first 100
    # and the oldest 59 of the 60 seeded ones follow them.
    assert sum(i.startswith("CLM-") for i in ids) == 41
    assert sum(i.startswith("HIST-") for i in ids) == 59


def test_sixty_seeded_entries_and_forty_decided_claims_are_a_hundred_not_truncated(
    world: World, server: Any
) -> None:
    add_history(world, 60)
    for number in range(40):
        add_decided(world, f"CLM-{100 + number:04d}", "approved", paid=number)

    answer = history(server, world.run_id, POLICY)

    assert len(answer["entries"]) == HISTORY_LIMIT
    assert answer["truncated"] is False


def test_an_entry_of_a_decided_claim_holds_no_word_of_the_claimant(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", paid=500)
    add_decided(world, "CLM-0003", "rejected")
    add_decided(world, "CLM-0004", "awaiting_adjuster")

    result = run_call(
        server,
        "claim_history",
        {"policy_number": POLICY},
        run_id=world.run_id,
    )

    entries = result.structured_content["entries"]
    assert len(entries) == 3
    assert all(set(entry) == ENTRY_KEYS for entry in entries)
    carried = text_of(result) + json.dumps(result.structured_content)
    for canary in (CANARY_NAME, CANARY_EMAIL, CANARY_DESCRIPTION, "CANARY"):
        assert canary not in carried


def test_a_claim_without_a_loss_date_is_left_out_and_the_history_truncated(
    world: World, server: Any, caplog: pytest.LogCaptureFixture
) -> None:
    add_decided(world, "CLM-0002", "submitted", loss_date="not a date")
    add_decided(world, "CLM-0003", "approved", paid=7)

    with caplog.at_level(logging.WARNING, logger=tools.__name__):
        answer = history(server, world.run_id, POLICY)

    assert [entry["history_id"] for entry in answer["entries"]] == ["CLM-0003"]
    assert answer["truncated"] is True
    # One warning: the run and the count, nothing of the row (T-03).
    (record,) = [r for r in caplog.records if r.name == tools.__name__]
    assert f"run {world.run_id}" in record.getMessage()
    assert "left out 1 claims of the claims store" in record.getMessage()
    assert "CLM-0002" not in caplog.text
    assert "not a date" not in caplog.text


def test_a_decided_claim_with_an_over_long_peril_is_left_out_and_the_history_truncated(
    world: World, server: Any
) -> None:
    add_decided(world, "CLM-0002", "approved", peril="x" * 65)
    add_decided(world, "CLM-0003", "approved", paid=7)

    answer = history(server, world.run_id, POLICY)

    assert [entry["history_id"] for entry in answer["entries"]] == ["CLM-0003"]
    assert answer["truncated"] is True


def test_a_history_whose_decided_claims_can_all_be_read_is_not_truncated(
    world: World, server: Any, caplog: pytest.LogCaptureFixture
) -> None:
    add_decided(world, "CLM-0002", "approved", peril="x" * 64, paid=7)

    with caplog.at_level(logging.WARNING, logger=tools.__name__):
        answer = history(server, world.run_id, POLICY)

    assert [entry["history_id"] for entry in answer["entries"]] == ["CLM-0002"]
    assert answer["truncated"] is False
    assert caplog.records == []


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
