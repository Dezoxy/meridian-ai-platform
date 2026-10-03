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

import os
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from servicesupport import (
    REGISTRY_DIR,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    owner_rows,
)
from stacksupport import (
    CLAIMS,
    EVAL_BASELINE,
    EXPECTED,
    MANIFEST,
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

from meridian.platform.evaluation.compare import compare
from meridian.platform.evaluation.report import (
    AnsweredBy,
    Report,
    load_report,
    write_report,
)
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage.assessment import PROMPT_VERSION
from meridian.workloads.claims_triage.evaluation import build_report
from meridian.workloads.claims_triage.mcp_server import tools as claims_tools
from meridian.workloads.claims_triage.models import DECISION_NOTES
from meridian.workloads.claims_triage.proposal import TriageProposal

STEP_8_REASONS = ("fraud_indicator", "over_threshold", "unverified")
ABOVE_THE_LIMIT = "CLM-0024"
# One claim in force with a candidate exclusion (MOTOR-TPL, third-party
# liability, racing): the run searches four times and asks the model once.
IN_FORCE_WITH_CANDIDATE = "CLM-0011"
# A claim on a lapsed policy: the rules refer it to an adjuster without asking
# the model, so its run calls no search and no gateway.
LAPSED_POLICY = "CLM-0002"
CIRCUMSTANCE_EXCLUDED = sorted(c for c in CLAIMS if circumstance_clause(c))
# What the answer to a posted claim says for each route: the run's status and
# the claim's state. Only the adjuster's route waits.
AFTER_TRIAGE = {
    "adjuster": ("AwaitingApproval", "awaiting_adjuster"),
    "auto_approve": ("Completed", "approved"),
    "request_documents": ("Completed", "documents_requested"),
}
# The decision an adjuster takes on a recommendation, and the state it gives.
DECISION_FOR_RECOMMENDATION = {
    "approve": "approve",
    "reject": "reject",
    None: "request_documents",
}
STATE_AFTER_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "request_documents": "documents_requested",
}
# The 29 paused claims of the replay run, by the decision their proposal's
# recommendation leads to (a claim with no recommendation asks for documents).
DECIDED_APPROVE = 8
DECIDED_REJECT = 9
DECIDED_REQUEST_DOCUMENTS = 12
CHECKPOINT_TABLES = (
    "runtime.checkpoints",
    "runtime.checkpoint_blobs",
    "runtime.checkpoint_writes",
)


@pytest.fixture
def stack(fresh_database: DatabaseHandle) -> Stack:
    return build_stack(fresh_database)


def scripted_stack(db: DatabaseHandle, model: ScriptedModel) -> Stack:
    return build_stack(db, runtime_http=model.http())


def post_all(stack: Stack) -> None:
    """Post the golden set. A claim routed to the adjuster leaves its run
    paused and its claim awaiting the adjuster; any other route completes the
    run and ends the claim's triage in the state the route gives."""
    for claim_id, claim in CLAIMS.items():
        response = stack.post(claim)
        assert response.status_code == 201, (claim_id, response.text)
        body = response.json()
        assert body["claim_id"] == claim_id
        assert body["proposal"] is not None, claim_id
        route = body["proposal"]["route"]
        assert (body["run_status"], body["state"]) == AFTER_TRIAGE[route], claim_id


def states(db: DatabaseHandle) -> dict[str, str]:
    return dict(owner_rows(db, "SELECT claim_id, state FROM claims.claims"))


def decision_for(proposal: TriageProposal) -> str:
    """What an adjuster does with a proposal: follows its recommendation, and
    asks for documents when it recommends nothing."""
    return DECISION_FOR_RECOMMENDATION[proposal.recommendation]


def table_count(db: DatabaseHandle, table: str) -> int:
    ((count,),) = owner_rows(db, f"SELECT count(*) FROM {table}")  # noqa: S608
    return int(count)


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


def write_evaluation_report(
    proposals: dict[str, TriageProposal], tmp_path: Path
) -> tuple[Report, Path]:
    """Grade the scripted run's proposals with the claims workload's graders and
    write the report: to ``MERIDIAN_EVAL_REPORT`` when set (``make eval`` and CI
    read it from there), else into ``tmp_path``."""
    report = build_report(
        proposals,
        EXPECTED,
        CLAIMS,
        POLICIES,
        manifest_path=MANIFEST,
        registry=load_registry(REGISTRY_DIR),
        answered_by=AnsweredBy(kind="scripted", label="simulated"),
        prompt=PROMPT_VERSION,
    )
    named = os.environ.get("MERIDIAN_EVAL_REPORT")
    destination = Path(named) if named else tmp_path / "claims-triage-report.json"
    write_report(report, destination)
    return report, destination


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

    # ── the adjuster decides every paused claim ─────────────────────────────
    paused = sorted(c for c, p in proposals.items() if p.route == "adjuster")
    assert Counter(states(fresh_database).values()) == {
        "awaiting_adjuster": 29,
        "approved": 5,
        "documents_requested": 6,
    }
    assert len(paused) == 29
    decisions = {c: decision_for(proposals[c]) for c in paused}
    for claim_id in paused:
        answer = stack.decide(claim_id, decisions[claim_id])
        assert answer.status_code == 200, (claim_id, answer.text)
        body = answer.json()
        assert body["claim_id"] == claim_id
        assert body["run_status"] == "Completed", claim_id
        assert body["state"] == STATE_AFTER_DECISION[decisions[claim_id]], claim_id
    # Every recommendation the golden set's replay run makes was followed, and
    # the three decisions all occur.
    assert Counter(decisions.values()) == {
        "approve": DECIDED_APPROVE,
        "reject": DECIDED_REJECT,
        "request_documents": DECIDED_REQUEST_DOCUMENTS,
    }
    assert Counter(states(fresh_database).values()) == {
        "approved": 5 + DECIDED_APPROVE,
        "rejected": DECIDED_REJECT,
        "documents_requested": 6 + DECIDED_REQUEST_DOCUMENTS,
    }
    # 29 requests, 29 notes and 29 decisions, one of each per paused claim; the
    # runs that ended left no checkpoint behind.
    for table in ("claims.approval_requests", "claims.notes", "claims.decisions"):
        assert table_count(fresh_database, table) == 29, table
    assert {
        claim_id: decision
        for claim_id, decision in owner_rows(
            fresh_database, "SELECT claim_id, decision FROM claims.decisions"
        )
    } == decisions
    for table in CHECKPOINT_TABLES:
        assert table_count(fresh_database, table) == 0, table
    assert owner_rows(
        fresh_database, "SELECT status, count(*) FROM runtime.runs GROUP BY status"
    ) == [("Completed", 40)]


# ── 2. a scripted model ─────────────────────────────────────────────────────
def test_a_scripted_model_gives_the_oracle_s_proposals(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    model = ScriptedModel(golden_answer)
    stack = scripted_stack(fresh_database, model)

    post_all(stack)

    proposals = stored_proposals(fresh_database)
    # S017: the report is written first, so a failing run leaves one to read.
    report, destination = write_evaluation_report(proposals, tmp_path)
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
    # S017's gate: the report equals the committed baseline's verdicts. When the
    # destination is the baseline itself, the run regenerates it (make
    # eval-baseline) and there is nothing to compare. A missing baseline fails.
    if destination.resolve() != EVAL_BASELINE.resolve():
        outcome = compare(load_report(EVAL_BASELINE), report)
        assert outcome.passed, (outcome.problems, outcome.regressions)


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
def test_one_paused_run_leaves_its_audit_rows_and_one_trace_across_six_services(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    """The claim is referred to an adjuster, so the run pauses after the one
    write the graph makes before it: the approval request, through the claims
    tool server (the sixth service of the trace)."""
    response = stack.post(CLAIMS[IN_FORCE_WITH_CANDIDATE])

    assert response.status_code == 201
    assert response.json()["run_status"] == "AwaitingApproval"
    assert response.json()["state"] == "awaiting_adjuster"
    run_id = uuid.UUID(response.json()["run_id"])

    # ── the rows of the run ─────────────────────────────────────────────────
    all_events = audit_events(fresh_database, run_id)
    # The Claims API's row about the run is the claim's move, not the run's.
    assert [
        (e["service"], e["event"], e["outcome"], e["reason"])
        for e in all_events
        if e["service"] == "claims-api"
    ] == [
        ("claims-api", "claim.awaiting_adjuster", "awaiting_adjuster", "rules-referred")
    ]
    events = [e for e in all_events if e["service"] != "claims-api"]
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
        ("claims-mcp", "request_approval", "completed"),
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
        ["run.started", "run.awaiting_approval"]
        + ["tool.call"] * 7
        + ["model.call"] * 5
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
        "claims-mcp",
    }
    assert len({s.context.trace_id for s in spans}) == 1
    names = Counter((service_of(s), s.name) for s in spans)
    assert names[("policy-mcp", "tool.call")] == 2
    assert names[("knowledge-mcp", "tool.call")] == 4
    assert names[("claims-mcp", "tool.call")] == 1
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


# ── 4b. a restart between the pause and the decision (QA-08) ────────────────
def test_a_decision_is_served_by_a_runtime_built_after_the_run_paused(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    """QA-08, in process: the runtime that paused the run is replaced by a new
    one, over the same database and the same tool servers, and the decision is
    served by the new one. Only the checkpoint in the database carries the run
    across."""
    posted = stack.post(CLAIMS[LAPSED_POLICY])
    assert posted.status_code == 201
    assert (posted.json()["run_status"], posted.json()["state"]) == AFTER_TRIAGE[
        "adjuster"
    ]
    run_id = uuid.UUID(posted.json()["run_id"])
    assert table_count(fresh_database, "runtime.checkpoints") > 0
    stack.restart_runtime()
    stack.exporter.clear()

    answer = stack.decide(LAPSED_POLICY, "reject")

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "claim_id": LAPSED_POLICY,
        "state": "rejected",
        "run_id": str(run_id),
        "run_status": "Completed",
    }
    # The run's own rows, in order: it started, paused, was resumed, read the
    # recorded decision and wrote its note through the claims tool server, and
    # completed.
    events = audit_events(fresh_database, run_id)
    assert [
        (e["service"], e["event"], e["tool"], e["outcome"])
        for e in events
        if e["service"] == "agent-runtime"
        or e["tool"] in ("approval_outcome", "add_claim_note")
    ] == [
        ("agent-runtime", "run.started", None, "started"),
        ("agent-runtime", "run.awaiting_approval", None, "paused"),
        ("agent-runtime", "run.resumed", None, "resumed"),
        ("claims-mcp", "tool.call", "approval_outcome", "completed"),
        ("claims-mcp", "tool.call", "add_claim_note", "completed"),
        ("agent-runtime", "run.completed", None, "completed"),
    ]
    # The claim's rows, in order: the Claims API moved it three times.
    assert owner_rows(
        fresh_database,
        "SELECT event, outcome, reason FROM audit.events "
        "WHERE service = 'claims-api' AND reference = %s ORDER BY recorded_at",
        (LAPSED_POLICY,),
    ) == [
        ("claim.triaging", "triaging", "triage-started"),
        ("claim.awaiting_adjuster", "awaiting_adjuster", "rules-referred"),
        ("claim.rejected", "rejected", "adjuster-rejected"),
    ]
    assert owner_rows(
        fresh_database, "SELECT claim_id, run_id, decision FROM claims.decisions"
    ) == [(LAPSED_POLICY, run_id, "reject")]
    assert table_count(fresh_database, "claims.notes") == 1
    for table in CHECKPOINT_TABLES:
        assert table_count(fresh_database, table) == 0, table
    # One trace for the decision, across the three services it called.
    spans = list(stack.exporter.get_finished_spans())
    assert {service_of(s) for s in spans} == {
        "claims-api",
        "agent-runtime",
        "claims-mcp",
    }
    assert len({s.context.trace_id for s in spans}) == 1
    assert sorted(
        s.attributes["meridian.tool"]
        for s in spans
        if service_of(s) == "claims-mcp" and s.name == "tool.call"
    ) == ["add_claim_note", "approval_outcome"]
    (note,) = [
        s
        for s in spans
        if service_of(s) == "claims-mcp"
        and s.name == "tool.call"
        and s.attributes["meridian.tool"] == "add_claim_note"
    ]
    above = ancestors(note, spans)
    chain = [
        "runtime.tool",
        "langgraph.node await_decision",
        "runtime.resume",
        "claims.decide",
    ]
    assert all(name in above for name in chain), above
    assert sorted(chain, key=above.index) == chain


# ── 4b2. the adjuster's page (S016) ─────────────────────────────────────────
def test_a_referred_claim_is_decided_in_the_adjusters_page_as_it_is_by_the_api(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    """The claim the rules refer to an adjuster is in the page's queue, its page
    shows the run's own rows (through the Claims API's view of the audit log),
    and a decision posted from the form redirects, completes the run and
    writes the note, as ``Stack.decide`` does."""
    posted = stack.post(CLAIMS[LAPSED_POLICY])
    assert posted.status_code == 201
    run_id = uuid.UUID(posted.json()["run_id"])

    queue = stack.client.get("/adjuster/claims")
    page = stack.client.get(f"/adjuster/claims/{LAPSED_POLICY}")

    assert queue.status_code == 200
    assert f"/adjuster/claims/{LAPSED_POLICY}" in queue.text
    assert page.status_code == 200
    assert "run.awaiting_approval" in page.text
    assert 'name="decision"' in page.text
    answer = stack.decide_in_page(LAPSED_POLICY, "reject")

    assert answer.status_code == 303, answer.text
    assert answer.headers["location"] == f"/adjuster/claims/{LAPSED_POLICY}"
    assert run_status(fresh_database, run_id) == "Completed"
    assert states(fresh_database)[LAPSED_POLICY] == "rejected"
    assert owner_rows(
        fresh_database, "SELECT claim_id, run_id, decision FROM claims.decisions"
    ) == [(LAPSED_POLICY, run_id, "reject")]
    assert owner_rows(fresh_database, "SELECT note FROM claims.notes") == [
        (DECISION_NOTES["reject"],)
    ]
    for table in CHECKPOINT_TABLES:
        assert table_count(fresh_database, table) == 0, table
    decided_page = stack.client.get(f"/adjuster/claims/{LAPSED_POLICY}")
    assert "run.completed" in decided_page.text
    assert 'name="decision"' not in decided_page.text


# ── 4c. the run reads the decision the Claims API recorded (T-31) ───────────
def run_status(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    ((status,),) = owner_rows(
        db, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )
    return str(status)


def test_a_run_resumed_with_no_recorded_decision_stays_paused_and_writes_no_note(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    posted = stack.post(CLAIMS[LAPSED_POLICY])
    assert posted.status_code == 201
    run_id = uuid.UUID(posted.json()["run_id"])

    forged = stack.resume_directly(LAPSED_POLICY, str(run_id))

    assert forged.status_code == 502
    assert forged.json()["status"] == "AwaitingApproval"
    assert run_status(fresh_database, run_id) == "AwaitingApproval"
    assert table_count(fresh_database, "claims.notes") == 0
    assert table_count(fresh_database, "claims.decisions") == 0
    assert table_count(fresh_database, "runtime.checkpoints") > 0
    assert states(fresh_database)[LAPSED_POLICY] == "awaiting_adjuster"
    reasons = [
        e["reason"]
        for e in audit_events(fresh_database, run_id)
        if e["event"] == "run.resume_failed"
    ]
    assert reasons == ["decision-not-recorded"]

    answer = stack.decide(LAPSED_POLICY, "reject")

    assert answer.status_code == 200, answer.text
    assert answer.json()["run_status"] == "Completed"
    assert answer.json()["state"] == "rejected"
    assert run_status(fresh_database, run_id) == "Completed"
    assert table_count(fresh_database, "claims.notes") == 1
    assert owner_rows(fresh_database, "SELECT note FROM claims.notes") == [
        (DECISION_NOTES["reject"],)
    ]
    for table in CHECKPOINT_TABLES:
        assert table_count(fresh_database, table) == 0, table


def test_a_resumed_leg_whose_note_fails_once_is_finished_by_the_same_decision(
    stack: Stack, fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = stack.post(CLAIMS[LAPSED_POLICY])
    run_id = uuid.UUID(posted.json()["run_id"])
    real_insert = claims_tools.insert_or_replay
    failures = [RuntimeError("injected")]

    def insert_once_failing(conn: Any, call: Any, store: Any) -> Any:
        if store is claims_tools.NOTES and failures:
            raise failures.pop()
        return real_insert(conn, call, store)

    monkeypatch.setattr(claims_tools, "insert_or_replay", insert_once_failing)

    first = stack.decide(LAPSED_POLICY, "approve")

    assert first.status_code == 502
    assert first.json() == {
        "detail": "the decision is recorded; the run did not complete",
        "claim_id": LAPSED_POLICY,
        "run_id": str(run_id),
    }
    assert failures == []
    assert run_status(fresh_database, run_id) == "AwaitingApproval"
    assert table_count(fresh_database, "claims.notes") == 0
    assert table_count(fresh_database, "claims.decisions") == 1

    again = stack.decide(LAPSED_POLICY, "approve")

    assert again.status_code == 200, again.text
    assert again.json() == {
        "claim_id": LAPSED_POLICY,
        "state": "approved",
        "run_id": str(run_id),
        "run_status": "Completed",
    }
    assert run_status(fresh_database, run_id) == "Completed"
    assert table_count(fresh_database, "claims.notes") == 1
    assert table_count(fresh_database, "claims.decisions") == 1
    assert owner_rows(fresh_database, "SELECT note FROM claims.notes") == [
        (DECISION_NOTES["approve"],)
    ]
    for table in CHECKPOINT_TABLES:
        assert table_count(fresh_database, table) == 0, table


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
    # The claim is stored, has no proposal and waits to be triaged again.
    assert owner_rows(
        fresh_database,
        "SELECT claim_id FROM claims.claims WHERE claim_id = %s",
        (third,),
    ) == [(third,)]
    assert set(stored_proposals(fresh_database)) == {first, second}
    assert states(fresh_database)[third] == "triage_failed"
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
        ("agent-runtime", "run.failed", "wording_search", "failed", "tool-refused"),
        ("claims-api", "claim.triage_failed", None, "triage_failed", "triage-failed"),
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
    assert states(fresh_database)[third] == again.json()["state"] != "triage_failed"
    assert again.json()["state"] in {state for _, state in AFTER_TRIAGE.values()}


# ── 6. no claimant text in spans, audit rows, run rows or log records ───────
CANARY_NAME = "CANARY-name-5d2e"
CANARY_EMAIL = "CANARY-mail-5d2e@example.com"
CANARY_TEXT = "CANARY-text-5d2e"


def values_in(db: DatabaseHandle, table: str) -> list[str]:
    """Every value of every row of ``table``, as the text a scan reads (a
    ``bytea`` column is its ``repr``, which keeps printable ASCII as it is)."""
    return [
        str(value)
        for row in owner_rows(db, f"SELECT * FROM {table}")  # noqa: S608
        for value in row
    ]


def test_claimant_text_stays_in_the_claim_and_a_waiting_runs_checkpoint(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    """The claimant's name and email are in ``claims.claims`` only. The
    description is there too, and in the checkpoint of a run that waits for an
    adjuster (the graph's state holds the claim's facts); the checkpoint is
    deleted when the run ends, so after the decision it is in the claim alone."""
    claim = CLAIMS[IN_FORCE_WITH_CANDIDATE]
    canaries = (CANARY_NAME, CANARY_EMAIL, CANARY_TEXT)
    claim = {
        **claim,
        "claimant": {"name": CANARY_NAME, "email": CANARY_EMAIL},
        "description": f"{claim['description']} {CANARY_TEXT}",
    }
    # Where a canary may be found; everything else is scanned for its absence.
    elsewhere = (
        "audit.events",
        "runtime.runs",
        "claims.triage_proposals",
        "claims.notes",
        "claims.approval_requests",
        "claims.decisions",
    )

    with application_log() as records:
        response = stack.post(claim)

        assert response.status_code == 201
        assert response.json()["state"] == "awaiting_adjuster"
        # ── while the claim waits ───────────────────────────────────────────
        # The checkpoint holds the description (the scan can see it, so its
        # silence elsewhere means something), and none of the three canaries
        # is in any other table, runtime.checkpoints included.
        carried = values_in(fresh_database, "runtime.checkpoint_blobs") + values_in(
            fresh_database, "runtime.checkpoint_writes"
        )
        assert any(CANARY_TEXT in value for value in carried)
        assert not any(CANARY_NAME in value for value in carried)
        assert not any(CANARY_EMAIL in value for value in carried)
        for table in (*elsewhere, "runtime.checkpoints"):
            held = values_in(fresh_database, table)
            for canary in canaries:
                assert not any(canary in value for value in held), (table, canary)

        decided = stack.decide(IN_FORCE_WITH_CANDIDATE, "approve")

    assert decided.status_code == 200
    assert decided.json()["run_status"] == "Completed"
    proposal = stored_proposals(fresh_database)[IN_FORCE_WITH_CANDIDATE]
    # The model was asked, so the description did reach the gateway.
    assert proposal.assessment == "unavailable"
    # The claim itself, which the Claims API owns, holds all three.
    ((submission,),) = owner_rows(
        fresh_database, "SELECT submission::text FROM claims.claims"
    )
    assert all(canary in submission for canary in canaries)
    assert records, "no log record was written, so nothing was checked"
    # ── after the decision ──────────────────────────────────────────────────
    for table in CHECKPOINT_TABLES:
        assert table_count(fresh_database, table) == 0, table
    scanned = {table: values_in(fresh_database, table) for table in elsewhere}
    # The scans can see what is there: the logs hold the assessment's reason,
    # and the rows hold the claim's ID.
    assert holds(records, "not-json")
    for table in ("audit.events", "runtime.runs", "claims.triage_proposals"):
        assert any(IN_FORCE_WITH_CANDIDATE in value for value in scanned[table]), table
    for table in ("claims.notes", "claims.approval_requests", "claims.decisions"):
        assert scanned[table], table
        assert any(IN_FORCE_WITH_CANDIDATE in value for value in scanned[table]), table
    for canary in canaries:
        assert_spans_hold_no_exception_and_no_canary(stack.exporter, canary)
        for table, held in scanned.items():
            assert not any(canary in value for value in held), (table, canary)
        assert not holds(records, canary), canary
