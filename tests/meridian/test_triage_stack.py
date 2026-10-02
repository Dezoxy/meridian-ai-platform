"""S014's done-when: a claim posted to the Claims API is triaged by the real
graph through the runtime, the three tool servers, PostgreSQL and the gateway in
replay mode, all in one process (``stacksupport.build_stack``).

The first test lets the replay gateway answer the model call: replay text is not
a verdict, so every claim whose run asks the model gets the assessment
``unavailable``. The next ones replace the model with a script
(``ScriptedModel``) that answers from the golden labels, and then with ``none``
for every claim. The rest read one run's audit rows and trace, what happens when
the gateway's window is not cleared between claims, and that no claimant text
leaves the Claims API.

With the scripted model every one of the 40 proposals equals the oracle,
CLM-0024 (claimed above the policy's limit) included: the probes lose no clause
(see ``test_triage_retrieval.py``).
"""

import uuid
from collections import Counter
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from servicesupport import (
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    owner_rows,
)
from stacksupport import (
    CLAIMS,
    EXPECTED,
    POLICIES,
    ScriptedModel,
    Stack,
    ancestors,
    build_stack,
    circumstance_clause,
    claims_that_ask_the_model,
    golden_answer,
    none_answer,
    service_of,
    stored_proposals,
)
from toolsupport import application_log, holds

from meridian.workloads.claims_triage.proposal import TriageProposal

STEP_8_REASONS = ("fraud_indicator", "over_threshold", "unverified")
ABOVE_THE_LIMIT = "CLM-0024"
# One claim in force with a candidate exclusion (MOTOR-TPL, third-party
# liability, racing): the run searches four times and asks the model once.
IN_FORCE_WITH_CANDIDATE = "CLM-0011"
CIRCUMSTANCE_EXCLUDED = sorted(c for c in CLAIMS if circumstance_clause(c))


@pytest.fixture
def stack(fresh_database: DatabaseHandle) -> Stack:
    return build_stack(fresh_database)


def scripted_stack(db: DatabaseHandle, model: ScriptedModel) -> Stack:
    return build_stack(db, runtime_http=model.http())


def post_all(stack: Stack) -> None:
    for claim_id, claim in CLAIMS.items():
        response = stack.post(claim)
        assert response.status_code == 201, (claim_id, response.text)
        body = response.json()
        assert body["claim_id"] == claim_id
        assert body["run_status"] == "Completed", claim_id
        assert body["proposal"] is not None, claim_id


def differences(claim_id: str, proposal: TriageProposal) -> dict[str, Any]:
    """The fields of the proposal that are not the golden set's, as
    ``{field: (proposal, expected)}``: route, reason, recommendation, payable
    amount, fraud indicators, missing documents, the exclusion's clause and the
    citations (clause numbers in order, with the policy's product and wording
    version)."""
    expected = EXPECTED[claim_id]
    policy = POLICIES[CLAIMS[claim_id]["policy_number"]]
    wanted = {
        "route": expected["route"],
        "reason": expected["reason"],
        "recommendation": expected["recommendation"],
        "payable_amount": expected["payable_amount"],
        "fraud_indicators": expected["fraud_indicators"],
        "missing_documents": expected["missing_documents"],
        "exclusion_clause": (
            expected["citations"][0]["clause"]
            if expected["reason"] == "excluded"
            else None
        ),
        "citations": [
            {
                "product": policy["product"],
                "wording_version": policy["wording_version"],
                "clause": c["clause"],
            }
            for c in expected["citations"]
        ],
    }
    got = proposal.model_dump(mode="json")
    return {
        name: (got[name], value) for name, value in wanted.items() if got[name] != value
    }


def unequal(proposals: dict[str, TriageProposal]) -> dict[str, dict[str, Any]]:
    """For each claim whose proposal is not the oracle's, how it differs."""
    found = {c: differences(c, p) for c, p in proposals.items()}
    return {claim_id: diff for claim_id, diff in found.items() if diff}


def routes_and_reasons(
    proposals: dict[str, TriageProposal],
) -> tuple[Counter[str], Counter[str]]:
    return (
        Counter(p.route for p in proposals.values()),
        Counter(p.reason for p in proposals.values()),
    )


def auto_approved(proposals: dict[str, TriageProposal]) -> list[str]:
    return sorted(c for c, p in proposals.items() if p.route == "auto_approve")


# ── 1. replay chat ──────────────────────────────────────────────────────────
def test_the_golden_set_through_the_stack_with_the_replay_gateway(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    post_all(stack)

    proposals = stored_proposals(fresh_database)
    asked = {c for c, p in proposals.items() if p.assessment != "not_needed"}
    assert set(proposals) == set(CLAIMS)
    assert asked == claims_that_ask_the_model()
    assert len(asked) == 15
    # A run that needed no model equals the oracle.
    unasked = {c: p for c, p in proposals.items() if c not in asked}
    assert ABOVE_THE_LIMIT in unasked
    assert unequal(unasked) == {}
    # A run that asked the model got replay text, which is no verdict.
    for claim_id in asked:
        proposal = proposals[claim_id]
        assert proposal.assessment == "unavailable", claim_id
        assert "exclusion_assessment" in proposal.gaps, claim_id
        assert proposal.route != "auto_approve", claim_id
        assert proposal.rationale is None, claim_id
        assert proposal.drafted_by is not None, claim_id
        assert proposal.drafted_by.mode == "replay", claim_id
        if proposal.reason in STEP_8_REASONS:
            assert proposal.recommendation is None, claim_id
    routes, reasons = routes_and_reasons(proposals)
    assert dict(routes) == {"adjuster": 29, "request_documents": 6, "auto_approve": 5}
    assert dict(reasons) == {
        "over_threshold": 7,
        "policy_inactive": 6,
        "missing_documents": 6,
        "within_threshold": 5,
        "unverified": 7,
        "fraud_indicator": 6,
        "excluded": 3,
    }
    assert auto_approved(proposals) == [
        "CLM-0005",
        "CLM-0010",
        "CLM-0016",
        "CLM-0019",
        "CLM-0021",
    ]
    # Auto-approval needs no model call: the five are the golden set's
    # auto-approvals that did not ask (CLM-0011, 0015 and 0023 did).
    assert (
        set(auto_approved(proposals))
        == {c for c in EXPECTED if EXPECTED[c]["route"] == "auto_approve"} - asked
    )


# ── 2. a scripted model ─────────────────────────────────────────────────────
def test_a_scripted_model_gives_the_oracle_s_proposals(
    fresh_database: DatabaseHandle,
) -> None:
    model = ScriptedModel(golden_answer)
    stack = scripted_stack(fresh_database, model)

    post_all(stack)

    proposals = stored_proposals(fresh_database)
    assert len(proposals) == len(CLAIMS) == 40
    # The model was asked exactly where the rules need it, once per claim.
    assert model.requests == sorted(claims_that_ask_the_model())
    # Every one of the 40 equals the oracle in all eight fields, CLM-0024 (above
    # the policy's limit) included; the counts are the golden set's own.
    assert unequal(proposals) == {}
    # CLM-0024 cites the limit clause 4.2 the amounts probe found.
    assert [c.clause for c in proposals[ABOVE_THE_LIMIT].citations] == [
        "2.2",
        "4.1",
        "4.2",
    ]
    assert proposals[ABOVE_THE_LIMIT].recommendation == "approve"
    routes, reasons = routes_and_reasons(proposals)
    assert routes == Counter(e["route"] for e in EXPECTED.values())
    assert reasons == Counter(e["reason"] for e in EXPECTED.values())
    assert auto_approved(proposals) == sorted(
        c for c, e in EXPECTED.items() if e["route"] == "auto_approve"
    )
    for proposal in proposals.values():
        assert proposal.gaps == ()
        assert proposal.assessment != "unavailable"


def test_a_model_that_finds_no_exclusion_costs_four_wrong_approvals(
    fresh_database: DatabaseHandle,
) -> None:
    """T-26: what one wrong answer of the model costs. Answering ``none`` for
    every claim turns the five claims the golden set excludes by a circumstance
    into something else, and for four of them that is an automatic approval."""
    model = ScriptedModel(none_answer)
    stack = scripted_stack(fresh_database, model)

    post_all(stack)

    proposals = stored_proposals(fresh_database)
    changed_route = {
        c: (EXPECTED[c]["route"], p.route)
        for c, p in proposals.items()
        if p.route != EXPECTED[c]["route"]
    }
    assert changed_route == {
        c: ("adjuster", "auto_approve")
        for c in ("CLM-0026", "CLM-0031", "CLM-0037", "CLM-0038")
    }
    assert all(proposals[c].reason == "within_threshold" for c in changed_route)
    # The fifth, CLM-0001, stays with the adjuster: over the threshold, not
    # excluded. These five are the only claims that differ.
    assert CIRCUMSTANCE_EXCLUDED == [
        "CLM-0001",
        "CLM-0026",
        "CLM-0031",
        "CLM-0037",
        "CLM-0038",
    ]
    assert proposals["CLM-0001"].route == "adjuster"
    assert proposals["CLM-0001"].reason == "over_threshold"
    assert set(unequal(proposals)) == set(CIRCUMSTANCE_EXCLUDED)


# ── 4. one run's audit rows and its trace ───────────────────────────────────
def test_one_run_leaves_its_audit_rows_and_one_trace_across_five_services(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    response = stack.post(CLAIMS[IN_FORCE_WITH_CANDIDATE])

    assert response.status_code == 201
    run_id = uuid.UUID(response.json()["run_id"])

    # ── the rows of the run ─────────────────────────────────────────────────
    events = audit_events(fresh_database, run_id)
    tool_calls = [
        (e["service"], e["tool"], e["outcome"])
        for e in events
        if e["event"] == "tool.call"
    ]
    assert tool_calls == [
        ("policy-mcp", "policy_lookup", "completed"),
        ("policy-mcp", "claim_history", "completed"),
        ("knowledge-mcp", "wording_search", "completed"),
        ("knowledge-mcp", "wording_search", "completed"),
        ("knowledge-mcp", "wording_search", "completed"),
        ("knowledge-mcp", "wording_search", "completed"),
    ]
    model_calls = Counter(
        (e["service"], e["deployment"], e["outcome"])
        for e in events
        if e["event"] == "model.call"
    )
    assert model_calls == {
        ("model-gateway", "replay-embedding", "completed"): 4,
        ("model-gateway", "replay-chat", "completed"): 1,
    }
    assert sorted(e["event"] for e in events) == sorted(
        ["run.started", "run.completed"] + ["tool.call"] * 6 + ["model.call"] * 5
    )
    assert {e["tenant"] for e in events} == {"claims-triage"}
    assert {e["agent"] for e in events} == {"claims-triage"}

    # ── one trace, with the spans of every service that was called ──────────
    spans = list(stack.exporter.get_finished_spans())
    assert {service_of(s) for s in spans} == {
        "claims-api",
        "agent-runtime",
        "model-gateway",
        "policy-mcp",
        "knowledge-mcp",
    }
    assert len({s.context.trace_id for s in spans}) == 1
    names = Counter((service_of(s), s.name) for s in spans)
    assert names[("policy-mcp", "tool.call")] == 2
    assert names[("knowledge-mcp", "tool.call")] == 4
    assert names[("model-gateway", "gateway.embeddings")] == 4
    assert names[("model-gateway", "gateway.chat")] == 1
    # The trace is a chain, not a bag: a search's embedding descends from the
    # tool server's span, the runtime's tool span, the node, the run, the claim.
    (embedding, *_) = [s for s in spans if s.name == "gateway.embeddings"]
    above = ancestors(embedding, spans)
    chain = [
        "tool.call",
        "runtime.tool",
        "langgraph.node retrieve_terms",
        "runtime.run",
        "claims.submit",
    ]
    assert all(name in above for name in chain)
    assert sorted(chain, key=above.index) == chain
    (chat,) = [s for s in spans if s.name == "gateway.chat"]
    above_chat = ancestors(chat, spans)
    chain = ["langgraph.node assess", "runtime.run", "claims.submit"]
    assert all(name in above_chat for name in chain)
    assert sorted(chain, key=above_chat.index) == chain


# ── 5. the gateway's window is not cleared between claims ───────────────────
def test_the_third_claim_in_a_window_fails_when_a_search_is_refused(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    """The window is 10 requests per 10 seconds for chat and embeddings
    together, and a claim whose run asks the model costs five (four searches,
    one chat). Two claims with no clock advance in between fit exactly (10), so
    the second does not fail; the third makes its first request, the eleventh,
    with its first search and is refused there."""
    first, second, third = sorted(claims_that_ask_the_model())[:3]

    assert stack.post(CLAIMS[first], advance=False).status_code == 201
    assert stack.post(CLAIMS[second], advance=False).status_code == 201
    refused = stack.post(CLAIMS[third], advance=False)

    assert refused.status_code == 502
    body = refused.json()
    assert body["claim_id"] == third
    failed_run = uuid.UUID(body["run_id"])
    # The claim is stored and has no proposal.
    assert owner_rows(
        fresh_database,
        "SELECT claim_id FROM claims.claims WHERE claim_id = %s",
        (third,),
    ) == [(third,)]
    assert set(stored_proposals(fresh_database)) == {first, second}
    assert owner_rows(
        fresh_database,
        "SELECT status FROM runtime.runs WHERE run_id = %s",
        (failed_run,),
    ) == [("Failed",)]
    # The run was refused at its first search: the tool server says why, and the
    # run failed.
    events = audit_events(fresh_database, failed_run)
    assert [
        (e["service"], e["event"], e["tool"], e["outcome"], e["reason"])
        for e in events
        if e["event"] != "model.call"
    ] == [
        ("agent-runtime", "run.started", None, "started", None),
        ("policy-mcp", "tool.call", "policy_lookup", "completed", None),
        ("policy-mcp", "tool.call", "claim_history", "completed", None),
        ("knowledge-mcp", "tool.call", "wording_search", "refused", "gateway-busy"),
        ("agent-runtime", "run.failed", None, "failed", None),
    ]
    # The gateway's rows: one embedding refused for the tenant's request rate,
    # none answered and no chat call.
    assert Counter(
        (e["deployment"], e["outcome"], e["reason"])
        for e in events
        if e["event"] == "model.call"
    ) == {("replay-embedding", "refused", "tenant-request-rate"): 1}

    # Posted again after the window, the same claim is triaged and succeeds.
    stack.clock.advance(61)
    again = stack.post(CLAIMS[third], advance=False)
    assert again.status_code == 201
    assert uuid.UUID(again.json()["run_id"]) != failed_run
    assert set(stored_proposals(fresh_database)) == {first, second, third}


# ── 6. no claimant text in spans, audit rows, run rows or log records ───────
CANARY_NAME = "CANARY-name-5d2e"
CANARY_EMAIL = "CANARY-mail-5d2e@example.com"
CANARY_TEXT = "CANARY-text-5d2e"


def test_no_claimant_text_leaves_the_claims_api_but_in_the_stored_claim(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    claim = CLAIMS[IN_FORCE_WITH_CANDIDATE]
    canaries = (CANARY_NAME, CANARY_EMAIL, CANARY_TEXT)
    claim = {
        **claim,
        "claimant": {"name": CANARY_NAME, "email": CANARY_EMAIL},
        "description": f"{claim['description']} {CANARY_TEXT}",
    }

    with application_log() as records:
        response = stack.post(claim)

    assert response.status_code == 201
    proposal = stored_proposals(fresh_database)[IN_FORCE_WITH_CANDIDATE]
    # The model was asked, so the description did reach the gateway.
    assert proposal.assessment == "unavailable"
    # The claim itself, which the Claims API owns, holds all three.
    ((submission,),) = owner_rows(
        fresh_database, "SELECT submission::text FROM claims.claims"
    )
    assert all(canary in submission for canary in canaries)
    assert records, "no log record was written, so nothing was checked"
    audit_values = [
        str(value)
        for row in owner_rows(fresh_database, "SELECT * FROM audit.events")
        for value in row
    ]
    run_values = [
        str(value)
        for row in owner_rows(fresh_database, "SELECT * FROM runtime.runs")
        for value in row
    ]
    proposal_rows = [
        str(value)
        for row in owner_rows(fresh_database, "SELECT * FROM claims.triage_proposals")
        for value in row
    ]
    # The scans can see what is there: the logs hold the assessment's reason,
    # and the rows hold the claim's ID.
    assert holds(records, "not-json")
    assert any(IN_FORCE_WITH_CANDIDATE in value for value in audit_values)
    assert any(IN_FORCE_WITH_CANDIDATE in value for value in run_values)
    assert any(IN_FORCE_WITH_CANDIDATE in value for value in proposal_rows)
    for canary in canaries:
        assert_spans_hold_no_exception_and_no_canary(stack.exporter, canary)
        assert not any(canary in value for value in audit_values), canary
        assert not any(canary in value for value in run_values), canary
        assert not any(canary in value for value in proposal_rows), canary
        assert not holds(records, canary), canary
