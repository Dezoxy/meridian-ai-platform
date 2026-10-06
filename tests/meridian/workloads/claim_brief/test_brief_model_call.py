"""What the claim-brief workload sends to the model (S037, W1a): which fields,
under which data class and token cap, and that nothing a person wrote as free
text is among them. The expected document is written out here field by field
and is the list in the module's docstring.
"""

import json
from typing import Any

import pytest
from claimbriefsupport import (
    ScriptedTools,
    claim_input,
    policy_answer,
    start_leg,
)
from dbsupport import OWNER
from hostsupport import BriefWorld
from servicesupport import owner_rows

from meridian.platform.common.db import connect
from meridian.runtime.model_client import DATA_CLASS_HEADER
from meridian.workloads.claim_brief import prompt
from meridian.workloads.claim_brief.prompt import (
    BRIEF_DATA_CLASS,
    BRIEF_OUTPUT_TOKENS,
    PROMPT_VERSION,
    SYSTEM_MESSAGE,
)

EMPTY_HISTORY: dict[str, Any] = {"entries": [], "truncated": False}
CHECKPOINT_BODIES = (
    "SELECT body::text FROM runtime.workflow_checkpoints WHERE thread_id = %s"
)
# The planted fields that are the run's input (the others are tool results).
INPUT_FIELDS = {"description", "document", "city", "claimant", "email", "extra-input"}
# The claim of claims.json (CLM-0001) on POL-0049, with no history.
DEFAULT_DOCUMENT: dict[str, Any] = {
    "claim": {
        "peril": "storm",
        "claimed_amount": 2890,
        "loss_date": "2026-07-13",
        "reported_on": "2026-07-13",
        "documents_received": 1,
    },
    "policy": {
        "found": True,
        "status": "active",
        "start_date": "2026-05-14",
        "end_date": "2027-05-13",
        "lapsed_on": None,
        "deductible": 150,
        "limit": 190000,
        "sum_insured": 190000,
    },
    "history": {
        "earlier_claims": 0,
        "earlier_claims_same_peril": 0,
        "earlier_paid_total": 0,
        "truncated": False,
    },
}


def the_call(world: BriefWorld) -> dict[str, Any]:
    """The one request the stub gateway received, as its JSON body."""
    (request,) = world.gateway.seen
    return json.loads(request.content)


def the_document(world: BriefWorld) -> dict[str, Any]:
    """The document in the user message of the one model call."""
    system, user = the_call(world)["messages"]
    assert system == {"role": "system", "content": SYSTEM_MESSAGE}
    assert user["role"] == "user"
    return json.loads(user["content"])


# ── the fields ──────────────────────────────────────────────────────────────
def test_the_model_is_sent_the_listed_fields_of_a_claim_the_real_servers_know(
    world: BriefWorld,
) -> None:
    start_leg(world)

    assert the_document(world) == DEFAULT_DOCUMENT


def test_the_history_the_real_server_returns_is_sent_as_counts_and_a_total(
    world: BriefWorld,
) -> None:
    with connect(world.db.dsn(OWNER), "test-seed") as conn:
        for row in [
            ("HIST-9001", "2025-03-01", "storm", 500),
            ("HIST-9002", "2025-09-01", "fire", 700),
            ("HIST-9003", "2026-01-01", "storm", 25),
        ]:
            conn.execute(
                "INSERT INTO policy.claim_history "
                "(history_id, policy_number, loss_date, peril, paid_amount, status) "
                "VALUES (%s, 'POL-0049', %s, %s, %s, 'closed')",
                row,
            )

    start_leg(world)

    assert the_document(world)["history"] == {
        "earlier_claims": 3,
        "earlier_claims_same_peril": 2,
        "earlier_paid_total": 1225,
        "truncated": False,
    }


def test_a_policy_that_is_not_found_is_sent_as_not_found_and_nothing_more(
    world: BriefWorld,
) -> None:
    tools = ScriptedTools({"found": False}, EMPTY_HISTORY)

    outcome = start_leg(world, tools)

    assert the_document(world)["policy"] == {"found": False}
    # The history is read whether or not the policy was found.
    assert [call[0] for call in tools.calls][:2] == ["policy_lookup", "claim_history"]
    assert outcome.status == "AwaitingApproval"


def test_a_lapsed_policy_with_no_sum_insured_is_sent_with_its_lapse_date(
    world: BriefWorld,
) -> None:
    answer = policy_answer(status="lapsed", lapsed_on="2026-06-01")
    del answer["policy"]["sum_insured"]

    start_leg(world, ScriptedTools(answer, EMPTY_HISTORY))

    policy = the_document(world)["policy"]
    assert policy["status"] == "lapsed"
    assert policy["lapsed_on"] == "2026-06-01"
    assert policy["sum_insured"] is None


# ── nothing a person wrote reaches the model ────────────────────────────────
def test_no_free_text_of_the_input_or_of_a_tool_result_reaches_the_model(
    world: BriefWorld,
) -> None:
    planted = {
        "description": "canary-description-6b1f",
        "document": "canary-document-name-02ac",
        "city": "canary-city-93d4",
        "claimant": "canary-claimant-name-5e70",
        "email": "canary-address@example.invalid",
        "extra-input": "canary-extra-input-b8e2",
        "product": "canary-product-41aa",
        "wording": "canary-wording-c7d0",
        "holder": "canary-holder-1f39",
        "history-id": "canary-history-id-7a52",
        "history-peril": "canary-history-peril-d3c6",
        "history-status": "canary-history-status-08be",
        "history-note": "canary-history-note-e4f1",
        "history-comment": "canary-history-comment-2b97",
    }
    # The Claims API sends none of the free text any more (the routes' own test
    # holds that); a caller that did send it still gets none of it to the model.
    run_input = claim_input(
        documents_received=2,
        description=planted["description"],
        documents=[planted["document"], "photos"],
        loss_location={"city": planted["city"], "country": "AT"},
        claimant={"name": planted["claimant"], "email": planted["email"]},
        notes=planted["extra-input"],
    )
    policy = policy_answer(
        product=planted["product"], wording_version=planted["wording"]
    )
    policy["policy"]["holder"] = {"name": planted["holder"]}
    history = {
        "entries": [
            {
                "history_id": planted["history-id"],
                "loss_date": "2026-01-01",
                "peril": planted["history-peril"],
                "paid_amount": 100,
                "status": planted["history-status"],
                "note": planted["history-note"],
            },
            {
                "history_id": "HIST-0001",
                "loss_date": "2025-01-01",
                "peril": "storm",
                "paid_amount": 250,
                "status": "closed",
            },
        ],
        "truncated": True,
        "comment": planted["history-comment"],
    }

    start_leg(world, ScriptedTools(policy, history), run_input)

    assert the_document(world) == {
        "claim": {**DEFAULT_DOCUMENT["claim"], "documents_received": 2},
        "policy": DEFAULT_DOCUMENT["policy"],
        "history": {
            "earlier_claims": 2,
            "earlier_claims_same_peril": 1,
            "earlier_paid_total": 350,
            "truncated": True,
        },
    }
    sent = world.gateway.seen[0].content.decode("utf-8")
    for field, canary in planted.items():
        assert canary not in sent, f"{field} reached the model"
    # What the steps keep in the checkpoints is the facts and the brief: no
    # tool result's text is there. The input is the framework's first message
    # and is in the entry checkpoint, and in no later one (the accepted leak
    # of the design: the run's input is held until the run ends).
    held = owner_rows(world.db, CHECKPOINT_BODIES, (str(world.thread_id),))
    assert held
    for field, canary in planted.items():
        rows = [row for row in held if canary in row[0]]
        if field in INPUT_FIELDS:
            assert len(rows) == 1, f"{field} is in {len(rows)} checkpoints"
            assert '"previous_checkpoint_id": null' in rows[0][0]
        else:
            assert rows == [], f"{field} reached the checkpoints"


def test_the_claim_and_the_policy_numbers_are_not_sent_either(
    world: BriefWorld,
) -> None:
    start_leg(world)

    sent = world.gateway.seen[0].content.decode("utf-8")
    assert "CLM-" not in sent
    assert "POL-" not in sent


# ── the call itself ─────────────────────────────────────────────────────────
def test_the_call_names_the_personal_data_class_and_the_token_cap_and_no_schema(
    world: BriefWorld,
) -> None:
    start_leg(world)

    (request,) = world.gateway.seen
    body = json.loads(request.content)
    assert request.headers[DATA_CLASS_HEADER] == BRIEF_DATA_CLASS == "personal"
    assert body["max_output_tokens"] == BRIEF_OUTPUT_TOKENS == 400
    assert set(body) == {"messages", "max_output_tokens"}


def test_the_system_message_says_the_document_is_data_and_asks_for_plain_text() -> None:
    lowered = SYSTEM_MESSAGE.lower()

    assert "plain text" in lowered
    assert "data" in lowered
    assert "instruction" in lowered
    assert "do not decide" in lowered or "does not decide" in lowered


# ── the prompt's version ────────────────────────────────────────────────────
def test_the_prompt_version_is_64_hex_digits_and_stable() -> None:
    assert len(PROMPT_VERSION) == 64
    assert int(PROMPT_VERSION, 16) >= 0
    assert prompt.prompt_version() == PROMPT_VERSION


@pytest.mark.parametrize(
    ("name", "other"),
    [
        ("SYSTEM_MESSAGE", "Another system message."),
        ("BRIEF_OUTPUT_TOKENS", 401),
        ("BRIEF_DATA_CLASS", "special"),
    ],
)
def test_the_prompt_version_changes_with_what_the_model_is_sent(
    monkeypatch: pytest.MonkeyPatch, name: str, other: object
) -> None:
    monkeypatch.setattr(prompt, name, other)

    assert prompt.prompt_version() != PROMPT_VERSION
