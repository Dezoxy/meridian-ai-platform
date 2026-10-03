"""The triage graph (S014, S015): seven nodes, the tools in a fixed order, one
model call, and a pause for an adjuster.

No database and no tool server: a stub answers each tool from the synthetic data
and records the calls, and a stub stands in for the model client. The stub
``wording_search`` returns every clause of the policy's wording (the real search
is the next contract's). The first group runs the 40 golden claims, the second
pins the full proposal of one claim per reason, the third the calls, the fourth
what fails and what does not, the fifth what the state and the logs hold, the
sixth the pause: the approval request, the decision read from the record on
resume (never from the resume value), the note.
Every graph is compiled with a ``MemorySaver`` under one thread ID, as the
runtime compiles it with its checkpointer.
"""

import json
import logging
from collections.abc import Callable
from functools import cache
from importlib.metadata import entry_points
from types import MappingProxyType
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.platform.registry import load_registry
from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.graphs import load_graph_factory
from meridian.runtime.model_client import (
    ChatResult,
    ModelCallError,
    ModelCallLimitError,
    ModelClient,
)
from meridian.runtime.tool_client import (
    ToolCallLimit,
    ToolClient,
    ToolNotAllowed,
    ToolRefused,
    ToolResult,
    ToolUnavailable,
)
from meridian.workloads.claims_triage import assessment as assessment_module
from meridian.workloads.claims_triage import wording as wording_module
from meridian.workloads.claims_triage.assessment import ASSESSMENT_OUTPUT_TOKENS
from meridian.workloads.claims_triage.graph import build
from meridian.workloads.claims_triage.models import DECISION_NOTES
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.wording import AMOUNTS_PROBE, TIMING_PROBE

SYNTHETIC = REPO_ROOT / "data" / "synthetic"
CANARY = "CANARY-7f3a91"
READ_TOOLS = ("policy_lookup", "claim_history", "wording_search")
POLICY_FIELDS = (
    "policy_number",
    "product",
    "wording_version",
    "start_date",
    "end_date",
    "status",
    "lapsed_on",
    "deductible",
    "limit",
    "sum_insured",
)
RATIONALE = "The description states no excluded fact."
LOGGER = "meridian.workloads.claims_triage"
REQUEST_ID = "5b0b0b0e-8f1c-4c63-9d0a-2f6f3a1d7e11"
NOTE_ID = "c1d2e3f4-0a1b-4c2d-8e3f-4a5b6c7d8e9f"
WRITE_ANSWERS: dict[str, dict[str, Any]] = {
    "request_approval": {"request_id": REQUEST_ID, "replayed": False},
    "add_claim_note": {"note_id": NOTE_ID, "replayed": False},
}
THREAD = {"configurable": {"thread_id": "claims-graph-test"}}


def load(name: str) -> Any:
    return json.loads((SYNTHETIC / name).read_text(encoding="utf-8"))


CLAIMS: dict[str, dict[str, Any]] = {c["claim_id"]: c for c in load("claims.json")}
POLICIES: dict[str, dict[str, Any]] = {
    p["policy_number"]: p for p in load("policies.json")
}
HISTORY: list[dict[str, Any]] = load("claim-history.json")
EXPECTED: dict[str, dict[str, Any]] = {
    e["claim_id"]: e for e in load("expected-outcomes.json")
}


def facts(claim_id: str, **changes: Any) -> dict[str, Any]:
    """What the runtime is sent: the claim without the claimant."""
    record = {k: v for k, v in CLAIMS[claim_id].items() if k != "claimant"}
    return {**record, **changes}


@cache
def wording(product: str) -> tuple[str, list[dict[str, Any]]]:
    document = parse_wording(
        (SYNTHETIC / "wordings" / f"{product}.md").read_text(encoding="utf-8")
    )
    chunks = [
        {
            "clause": c.clause,
            "section": c.section,
            "title": c.title,
            "body": c.body,
            "keyword_match": False,
        }
        for c in document.chunks
    ]
    return document.wording_version, chunks


def model_answer(
    verdict: str = "none", clause: str | None = None, rationale: str = RATIONALE
) -> str:
    return json.dumps({"verdict": verdict, "clause": clause, "rationale": rationale})


class StubModel:
    """Stands in for ModelClient; remembers what it was asked."""

    def __init__(self, result: str | Exception | None = None) -> None:
        self.result = model_answer() if result is None else result
        self.calls: list[tuple[list[dict[str, str]], int | None]] = []

    def chat(
        self, messages: list[dict[str, str]], *, max_output_tokens: int | None = None
    ) -> ChatResult:
        self.calls.append((messages, max_output_tokens))
        if isinstance(self.result, Exception):
            raise self.result
        return ChatResult(
            text=self.result,
            deployment="eu-chat",
            provider="azure-openai",
            model="gpt-x",
            mode="live",
            input_tokens=10,
            output_tokens=10,
            finish_reason="stop",
        )


class StubTools:
    """Stands in for ToolClient: answers the three read tools from the synthetic
    data and the two write tools with a fixed ID, and records every call. A read
    call goes to ``calls`` and a write call, with its step, to ``writes``: a test
    of the read order is not about the writes, and a test of the writes is not
    about the reads. A tool it does not know raises.

    ``errors`` makes a tool raise; ``answers`` replaces a tool's answer; ``tamper``
    changes the answer of the n-th ``wording_search`` call. ``recorded`` is the
    decision the Claims API recorded, which ``approval_outcome`` answers (a test
    sets it between the pause and the resume, as the API does); with none the
    answer holds no outcome."""

    def __init__(
        self,
        *,
        errors: dict[str, Exception] | None = None,
        answers: dict[str, dict[str, Any]] | None = None,
        tamper: Callable[[int, dict[str, Any]], dict[str, Any]] | None = None,
        recorded: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.writes: list[tuple[str, dict[str, Any], str | None]] = []
        self.errors = errors or {}
        self.answers = answers or {}
        self.tamper = tamper
        self.recorded = recorded
        self.searches = 0

    def call(
        self, tool: str, arguments: dict[str, Any], *, step: str | None = None
    ) -> ToolResult:
        if tool in WRITE_ANSWERS:
            self.writes.append((tool, dict(arguments), step))
        else:
            self.calls.append((tool, dict(arguments)))
            assert step is None, "a read tool takes no step"
        if tool in self.errors:
            raise self.errors[tool]
        if tool in self.answers:
            return ToolResult(self.answers[tool], replayed=False, call_id=None)
        if tool in WRITE_ANSWERS:
            return ToolResult(WRITE_ANSWERS[tool], replayed=False, call_id=None)
        data = {
            "policy_lookup": self._policy,
            "claim_history": self._history,
            "wording_search": self._search,
            "approval_outcome": self._outcome,
        }[tool](arguments)
        return ToolResult(data, replayed=False, call_id=None)

    @staticmethod
    def _policy(arguments: dict[str, Any]) -> dict[str, Any]:
        record = POLICIES.get(arguments["policy_number"])
        if record is None:
            return {"found": False}
        # The tool's schema has no null: an absent date or sum is left out.
        policy = {f: record[f] for f in POLICY_FIELDS if record.get(f) is not None}
        return {"found": True, "policy": policy}

    def _outcome(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {} if self.recorded is None else {"outcome": self.recorded}

    @staticmethod
    def _history(arguments: dict[str, Any]) -> dict[str, Any]:
        entries = [
            {k: v for k, v in e.items() if k != "policy_number"}
            for e in HISTORY
            if e["policy_number"] == arguments["policy_number"]
        ]
        return {"entries": entries, "truncated": False}

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        version, chunks = wording(arguments["product"])
        answer = {
            "product": arguments["product"],
            "wording_version": version,
            "chunks": chunks,
        }
        number = self.searches
        self.searches += 1
        return self.tamper(number, answer) if self.tamper else answer

    def names(self) -> list[str]:
        return [tool for tool, _ in self.calls]


def compiled(
    model: StubModel, tools: StubTools, saver: MemorySaver | None = None
) -> CompiledStateGraph:
    graph = build(cast(ModelClient, model), cast(ToolClient, tools))
    return graph.compile(checkpointer=saver or MemorySaver())


def run_graph(
    model: StubModel, tools: StubTools, claim: dict[str, Any]
) -> dict[str, Any]:
    """The state a run reaches: where it pauses for an adjuster, the state so far
    (without LangGraph's own ``__interrupt__`` key)."""
    result = compiled(model, tools).invoke({"claim": claim}, THREAD)
    result.pop("__interrupt__", None)
    return result


def resume(graph: CompiledStateGraph, value: Any) -> dict[str, Any]:
    """Resume the one pending pause as the runtime does (``runs._resume_command``):
    keyed by the interrupt's ID, so ``value`` reaches the node verbatim, ``{}``
    and a non-dict included."""
    (pending,) = graph.get_state(THREAD).interrupts
    return graph.invoke(Command(resume={pending.id: value}), THREAD)


def triage(
    claim_id: str,
    model: StubModel | None = None,
    tools: StubTools | None = None,
    **changes: Any,
) -> tuple[dict[str, Any], StubModel, StubTools]:
    model = model or StubModel()
    tools = tools or StubTools()
    result = run_graph(model, tools, facts(claim_id, **changes))
    return result["output"], model, tools


# -- the golden set -----------------------------------------------------------


def golden_model(claim_id: str) -> StubModel:
    """A model that answers as the oracle would: it finds the circumstance
    exclusion of a claim that was excluded, and none for any other claim."""
    expected = EXPECTED[claim_id]
    if expected["reason"] == "excluded":
        return StubModel(model_answer("applies", expected["citations"][0]["clause"]))
    return StubModel()


@pytest.mark.parametrize("claim_id", list(CLAIMS))
def test_the_graph_reproduces_the_oracle_on_every_golden_claim(claim_id: str) -> None:
    expected = EXPECTED[claim_id]
    policy = POLICIES[CLAIMS[claim_id]["policy_number"]]
    model = golden_model(claim_id)
    tools = StubTools()

    output, _, _ = triage(claim_id, model, tools)

    proposal = TriageProposal.model_validate(output)
    assert (
        proposal.route,
        proposal.reason,
        proposal.recommendation,
        proposal.payable_amount,
    ) == (
        expected["route"],
        expected["reason"],
        expected["recommendation"],
        expected["payable_amount"],
    )
    assert list(proposal.fraud_indicators) == expected["fraud_indicators"]
    assert list(proposal.missing_documents) == expected["missing_documents"]
    assert proposal.exclusion_clause == (
        expected["citations"][0]["clause"] if expected["reason"] == "excluded" else None
    )
    assert [c.model_dump() for c in proposal.citations] == [
        {
            "product": policy["product"],
            "wording_version": policy["wording_version"],
            "clause": c["clause"],
        }
        for c in expected["citations"]
    ]
    assert proposal.gaps == ()
    assert len(model.calls) <= 1
    assert (len(model.calls) == 1) == (proposal.assessment != "not_needed")
    assert (proposal.drafted_by is None) == (proposal.assessment == "not_needed")
    assert set(tools.names()) <= set(READ_TOOLS)
    assert [w[0] for w in tools.writes] == (
        ["request_approval"] if proposal.route == "adjuster" else []
    )


# -- one claim per reason -----------------------------------------------------

DRAFTED_BY = {
    "deployment": "eu-chat",
    "provider": "azure-openai",
    "mode": "live",
    "prompt": assessment_module.PROMPT_VERSION,
}


def expected_proposal(**changes: Any) -> dict[str, Any]:
    """A proposal with every field, as the graph writes it to ``output``."""
    return {
        "route": "adjuster",
        "reason": "unverified",
        "recommendation": None,
        "payable_amount": None,
        "exclusion_clause": None,
        "fraud_indicators": [],
        "missing_documents": [],
        "citations": [],
        "gaps": [],
        "assessment": "not_needed",
        "unavailable_because": None,
        "rationale": None,
        "drafted_by": None,
        **changes,
    }


def cited(product: str, *clauses: str) -> list[dict[str, str]]:
    return [
        {"product": product, "wording_version": "2026-01", "clause": c} for c in clauses
    ]


def test_within_threshold_with_the_model_finding_no_exclusion() -> None:
    output, model, _ = triage("CLM-0011")  # third-party liability, one candidate

    assert output == expected_proposal(
        route="auto_approve",
        reason="within_threshold",
        recommendation="approve",
        payable_amount=460,
        citations=cited("MOTOR-TPL", "2.1", "4.1"),
        assessment="none_applies",
        rationale=RATIONALE,
        drafted_by=DRAFTED_BY,
    )
    assert len(model.calls) == 1


def test_within_threshold_without_any_candidate_exclusion_needs_no_model() -> None:
    output, model, _ = triage("CLM-0005")  # glass: no circumstance exclusion names it

    assert output == expected_proposal(
        route="auto_approve",
        reason="within_threshold",
        recommendation="approve",
        payable_amount=920,
        citations=cited("MOTOR-COMP", "2.4", "4.1"),
    )
    assert model.calls == []


def test_over_threshold() -> None:
    output, model, _ = triage("CLM-0004")

    assert output == expected_proposal(
        reason="over_threshold",
        recommendation="approve",
        payable_amount=2820,
        citations=cited("HOME-PLUS", "2.3", "4.1"),
    )
    assert model.calls == []


def test_fraud_indicator() -> None:
    output, _, _ = triage("CLM-0012")

    assert output == expected_proposal(
        reason="fraud_indicator",
        recommendation="approve",
        payable_amount=2470,
        fraud_indicators=["late_report"],
        citations=cited("MOTOR-TPL", "2.1", "4.1", "5.1"),
        assessment="none_applies",
        rationale=RATIONALE,
        drafted_by=DRAFTED_BY,
    )


def test_excluded_by_a_circumstance_exclusion_the_model_found() -> None:
    answer = model_answer("applies", "3.2", "The claimant says the car was racing.")

    output, _, _ = triage("CLM-0037", StubModel(answer))

    assert output == expected_proposal(
        reason="excluded",
        recommendation="reject",
        exclusion_clause="3.2",
        citations=cited("MOTOR-TPL", "3.2"),
        assessment="applies",
        rationale="The claimant says the car was racing.",
        drafted_by=DRAFTED_BY,
    )


def test_excluded_by_a_peril_exclusion_needs_no_model() -> None:
    output, model, _ = triage("CLM-0022")  # flood, which HOME-STD does not cover

    assert output == expected_proposal(
        reason="excluded",
        recommendation="reject",
        exclusion_clause="3.1",
        citations=cited("HOME-STD", "3.1"),
    )
    assert model.calls == []


def test_policy_inactive() -> None:
    output, model, _ = triage("CLM-0002")

    assert output == expected_proposal(
        reason="policy_inactive",
        recommendation="reject",
        citations=cited("HOME-PLUS", "6.2"),
    )
    assert model.calls == []


def test_missing_documents() -> None:
    output, _, _ = triage("CLM-0003")

    assert output == expected_proposal(
        route="request_documents",
        reason="missing_documents",
        missing_documents=["photos", "repair_estimate"],
        citations=cited("HOME-PLUS", "5.5"),
        assessment="none_applies",
        rationale=RATIONALE,
        drafted_by=DRAFTED_BY,
    )


def test_policy_not_found() -> None:
    output, model, tools = triage("CLM-0001", policy_number="POL-9999")

    assert output == expected_proposal(reason="policy_not_found")
    assert model.calls == []
    assert tools.calls == [("policy_lookup", {"policy_number": "POL-9999"})]


def test_nothing_payable() -> None:
    # The claim is below the policy's deductible.
    output, _, _ = triage("CLM-0004", claimed_amount=1)

    assert output == expected_proposal(
        reason="nothing_payable", citations=cited("HOME-PLUS", "2.3", "4.1")
    )


def test_unverified_when_the_models_answer_is_not_json() -> None:
    model = StubModel("I think the claim is fine.")

    output, _, _ = triage("CLM-0011", model)  # otherwise within_threshold

    assert output == expected_proposal(
        reason="unverified",
        payable_amount=460,
        citations=cited("MOTOR-TPL", "2.1", "4.1"),
        gaps=["exclusion_assessment"],
        assessment="unavailable",
        unavailable_because="not-json",
        drafted_by=DRAFTED_BY,
    )
    assert output["route"] != "auto_approve"


def test_a_model_answer_that_cannot_be_trusted_never_auto_approves() -> None:
    cases = (
        ("", "not-json"),
        ("[]", "not-json"),
        ('{"verdict": "none"}', "not-the-format"),
        (model_answer("applies", "9.9"), "unknown-clause"),
        (model_answer("unsure"), "unsure"),
    )
    for text, because in cases:
        output, _, _ = triage("CLM-0011", StubModel(text))

        assert (output["route"], output["reason"]) == ("adjuster", "unverified")
        assert output["assessment"] == "unavailable"
        assert output["unavailable_because"] == because
        assert output["gaps"] == ["exclusion_assessment"]


def test_a_user_message_over_the_gateways_limit_makes_no_call_and_is_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assessment_module, "MAX_USER_MESSAGE_CHARS", 100)

    output, model, _ = triage("CLM-0011")

    assert model.calls == []
    assert output == expected_proposal(
        reason="unverified",
        payable_amount=460,
        citations=cited("MOTOR-TPL", "2.1", "4.1"),
        gaps=["exclusion_assessment"],
        assessment="unavailable",
        unavailable_because="too-long",
    )


def test_a_proposal_without_an_assessment_in_the_state_is_a_failure() -> None:
    """The node ``propose`` of a claim with a policy: a state that lacks the
    assessment is a bug of the graph, and the proposal is not defaulted."""
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))
    state = {
        "claim": facts("CLM-0011"),
        "policy": StubTools._policy(
            {"policy_number": CLAIMS["CLM-0011"]["policy_number"]}
        )["policy"],
        "history": [],
        "history_truncated": False,
        "chunks": wording("MOTOR-TPL")[1],
        "assessed": None,
        "output": {},
    }

    with pytest.raises(GraphFailure) as raised:
        graph.nodes["propose"].runnable.invoke(state)

    assert raised.value.code == "missing-assessment"


@pytest.mark.parametrize("node", ["retrieve_terms", "assess"])
def test_a_node_that_needs_a_policy_fails_without_one(node: str) -> None:
    """``lookup_policy`` routes a claim without a policy to ``propose``, so a
    state that reaches a later node with none is a bug of the graph."""
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))
    state = {
        "claim": facts("CLM-0011"),
        "policy": None,
        "history": [],
        "history_truncated": False,
        "chunks": [],
        "assessed": None,
        "output": {},
    }

    with pytest.raises(GraphFailure) as raised:
        graph.nodes[node].runnable.invoke(state)

    assert raised.value.code == "missing-policy"


@pytest.fixture
def other_wording_version(monkeypatch: pytest.MonkeyPatch) -> str:
    """A wording version that is not the one every other test uses: the table
    of exclusion clauses knows it for MOTOR-TPL, which has two."""
    version = "2031-07"
    table = {**wording_module.EXCLUSION_CLAUSES, ("MOTOR-TPL", version): 2}
    monkeypatch.setattr(wording_module, "EXCLUSION_CLAUSES", MappingProxyType(table))
    return version


def tools_for_version(version: str) -> StubTools:
    """Tools whose policy and search answers carry ``version``."""
    policy = StubTools._policy({"policy_number": CLAIMS["CLM-0011"]["policy_number"]})
    policy["policy"]["wording_version"] = version

    def versioned(_: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "wording_version": version}

    return StubTools(answers={"policy_lookup": policy}, tamper=versioned)


def test_the_citations_carry_the_policys_wording_version(
    other_wording_version: str,
) -> None:
    tools = tools_for_version(other_wording_version)

    output, model, _ = triage("CLM-0011", tools=tools)

    assert output["citations"] == [
        {"product": "MOTOR-TPL", "wording_version": other_wording_version, "clause": c}
        for c in ("2.1", "4.1")
    ]
    assert (output["route"], output["gaps"]) == ("auto_approve", [])
    document = json.loads(model.calls[0][0][1]["content"])
    assert document["wording"] == {"product": "MOTOR-TPL", "version": "2031-07"}


def test_the_citation_of_an_excluding_clause_carries_the_policys_version(
    other_wording_version: str,
) -> None:
    tools = tools_for_version(other_wording_version)
    answer = model_answer("applies", "3.2", "Racing.")

    output, _, _ = triage("CLM-0037", StubModel(answer), tools=tools)

    assert output["citations"] == [
        {
            "product": "MOTOR-TPL",
            "wording_version": other_wording_version,
            "clause": "3.2",
        }
    ]


def test_a_wording_version_that_the_table_does_not_know_is_never_complete() -> None:
    output, _, _ = triage("CLM-0011", tools=tools_for_version("2031-07"))

    assert output["gaps"] == ["exclusion_clauses"]
    assert (output["route"], output["reason"]) == ("adjuster", "unverified")
    assert output["recommendation"] is None


# -- the calls ----------------------------------------------------------------


def test_the_tools_are_called_in_a_fixed_order_with_the_policys_product() -> None:
    _, _, tools = triage("CLM-0011")

    number = CLAIMS["CLM-0011"]["policy_number"]
    assert tools.calls == [
        ("policy_lookup", {"policy_number": number}),
        ("claim_history", {"policy_number": number}),
        ("wording_search", {"query": "Third-party liability", "product": "MOTOR-TPL"}),
        (
            "wording_search",
            {
                "query": "This exclusion applies to claims for third-party liability.",
                "product": "MOTOR-TPL",
            },
        ),
        ("wording_search", {"query": AMOUNTS_PROBE, "product": "MOTOR-TPL"}),
        ("wording_search", {"query": TIMING_PROBE, "product": "MOTOR-TPL"}),
    ]


def test_a_policy_not_in_force_is_searched_once() -> None:
    _, _, tools = triage("CLM-0002")  # lapsed

    number = CLAIMS["CLM-0002"]["policy_number"]
    assert tools.calls == [
        ("policy_lookup", {"policy_number": number}),
        ("claim_history", {"policy_number": number}),
        ("wording_search", {"query": TIMING_PROBE, "product": "HOME-PLUS"}),
    ]


def test_a_search_names_no_top_k_and_none_of_the_claims_words() -> None:
    _, _, tools = triage("CLM-0011")

    searches = [
        arguments for tool, arguments in tools.calls if tool == "wording_search"
    ]
    assert all(set(arguments) == {"query", "product"} for arguments in searches)
    assert all(
        CLAIMS["CLM-0011"]["description"] not in arguments["query"]
        for arguments in searches
    )


def test_no_policy_means_no_history_call_and_no_search() -> None:
    _, _, tools = triage("CLM-0001", policy_number="POL-9999")

    assert tools.names() == ["policy_lookup"]


@pytest.mark.parametrize(
    ("claim_id", "calls"),
    [
        ("CLM-0011", 1),  # in force, cover found, one candidate
        ("CLM-0037", 1),
        ("CLM-0002", 0),  # not in force
        ("CLM-0022", 0),  # a peril exclusion decides
        ("CLM-0005", 0),  # a peril with no candidate exclusion
    ],
)
def test_the_model_is_called_only_when_the_rules_need_its_fact(
    claim_id: str, calls: int
) -> None:
    model = golden_model(claim_id)

    triage(claim_id, model)

    assert len(model.calls) == calls


def test_the_model_call_carries_the_assessments_fixed_shape() -> None:
    _, model, _ = triage("CLM-0011")

    ((messages, max_output_tokens),) = model.calls
    document = json.loads(messages[1]["content"])
    assert max_output_tokens == ASSESSMENT_OUTPUT_TOKENS
    assert set(document) == {"peril", "description", "wording", "clauses"}
    assert document["wording"] == {"product": "MOTOR-TPL", "version": "2026-01"}
    assert [c["clause"] for c in document["clauses"]] == ["3.2"]
    claimant = CLAIMS["CLM-0011"]["claimant"]
    assert claimant["name"] not in messages[1]["content"]
    assert claimant["email"] not in messages[1]["content"]


@pytest.mark.parametrize("claim_id", list(CLAIMS))
def test_a_run_before_any_decision_writes_at_most_the_approval_request(
    claim_id: str,
) -> None:
    model, tools = golden_model(claim_id), StubTools()

    output, _, _ = triage(claim_id, model, tools)

    assert set(tools.names()) <= set(READ_TOOLS)
    assert len(tools.writes) == (1 if output["route"] == "adjuster" else 0)
    assert {tool for tool, _, _ in tools.writes} <= {"request_approval"}
    assert len(model.calls) <= 1


# -- what fails and what does not ---------------------------------------------

TOOL_ERRORS = {
    "refused: no-corpus": ToolRefused("wording_search", "no-corpus"),
    "refused: stale-vectors": ToolRefused("wording_search", "stale-vectors"),
    "refused: gateway-busy": ToolRefused("wording_search", "gateway-busy"),
    "refused: gateway-refused": ToolRefused("wording_search", "gateway-refused"),
    "refused: unknown": ToolRefused("wording_search", "unknown"),
    "unavailable": ToolUnavailable("wording_search"),
    "not allowed": ToolNotAllowed("wording_search"),
    "call limit": ToolCallLimit("wording_search"),
}


@pytest.mark.parametrize("tool", READ_TOOLS)
@pytest.mark.parametrize("error", TOOL_ERRORS.values(), ids=list(TOOL_ERRORS))
def test_a_tool_error_fails_the_run_and_no_later_step_runs(
    tool: str, error: Exception
) -> None:
    model, tools = StubModel(), StubTools(errors={tool: error})

    with pytest.raises(type(error)) as raised:
        run_graph(model, tools, facts("CLM-0011"))

    assert raised.value is error
    assert tools.names() == list(READ_TOOLS[: READ_TOOLS.index(tool) + 1])
    assert model.calls == []


@pytest.mark.parametrize(
    "error", [ModelCallError(502), ModelCallLimitError()], ids=["gateway", "limit"]
)
def test_a_model_client_error_fails_the_run(error: Exception) -> None:
    with pytest.raises(type(error)) as raised:
        triage("CLM-0011", StubModel(error))

    assert raised.value is error


def test_a_search_answer_for_another_wording_version_fails_the_run() -> None:
    def other_version(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "wording_version": "2026-02"} if number == 1 else answer

    model = StubModel()

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", model, StubTools(tamper=other_version))

    assert raised.value.code == "other-wording"
    assert model.calls == []


def test_a_search_answer_for_another_product_fails_the_run() -> None:
    def other_product(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "product": "HOME-STD"}

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", tools=StubTools(tamper=other_product))

    assert raised.value.code == "other-wording"


def test_a_policy_answer_that_does_not_fit_fails_the_run() -> None:
    policy = StubTools._policy({"policy_number": CLAIMS["CLM-0011"]["policy_number"]})
    broken = {"found": True, "policy": {**policy["policy"], "status": CANARY}}
    tools = StubTools(answers={"policy_lookup": broken})

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", tools=tools)

    assert raised.value.code == "policy-record-unfit"
    assert CANARY not in str(raised.value)
    assert tools.names() == ["policy_lookup"]


def test_a_history_entry_that_does_not_fit_fails_the_run() -> None:
    entry = {"history_id": "HIST-0001", "loss_date": CANARY, "peril": "storm"}
    tools = StubTools(
        answers={"claim_history": {"entries": [entry], "truncated": False}}
    )

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", tools=tools)

    assert raised.value.code == "history-entry-unfit"
    assert CANARY not in str(raised.value)
    assert tools.names() == ["policy_lookup", "claim_history"]


def test_a_truncated_history_is_a_gap_and_not_a_failure() -> None:
    answer = {"entries": [], "truncated": True}

    output, _, _ = triage(
        "CLM-0004", tools=StubTools(answers={"claim_history": answer})
    )

    assert output["gaps"] == ["claim_history"]
    assert (output["reason"], output["recommendation"]) == ("over_threshold", None)


def test_a_claim_that_is_not_valid_facts_fails_the_run() -> None:
    with pytest.raises(ValueError, match="validation error"):
        triage("CLM-0011", peril="meteor")


def test_a_claim_that_still_carries_the_claimant_is_refused_by_the_graph() -> None:
    model, tools = StubModel(), StubTools()

    with pytest.raises(ValidationError):
        run_graph(model, tools, CLAIMS["CLM-0011"])

    assert model.calls == []
    assert tools.calls == []


# -- the state and the logs ---------------------------------------------------

PLAIN = (dict, list, str, int, bool, type(None))
STATE_KEYS = {
    "claim",
    "policy",
    "history",
    "history_truncated",
    "chunks",
    "assessed",
    "output",
    "request_id",
    "decision",
}


def assert_plain(value: Any, where: str = "state") -> None:
    assert isinstance(value, PLAIN), f"{where} holds a {type(value).__name__}"
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            assert_plain(item, f"{where}.{key}")
    if isinstance(value, list):
        for position, item in enumerate(value):
            assert_plain(item, f"{where}[{position}]")


@pytest.mark.parametrize("claim_id", ["CLM-0011", "CLM-0002", "CLM-0005"])
def test_the_state_after_a_run_holds_only_plain_data(claim_id: str) -> None:
    result = run_graph(golden_model(claim_id), StubTools(), facts(claim_id))

    assert set(result) == STATE_KEYS
    assert_plain(result)


def test_the_state_of_a_claim_without_a_policy_holds_only_plain_data() -> None:
    result = run_graph(
        StubModel(), StubTools(), facts("CLM-0001", policy_number="POL-9999")
    )

    assert set(result) == STATE_KEYS
    assert result["policy"] is None
    assert_plain(result)


def test_no_log_line_and_no_error_holds_claim_text_or_a_tool_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    description = f"A tree fell on the car. {CANARY}-claim"
    model = StubModel(f"not json {CANARY}-model")

    triage("CLM-0011", model, description=description)
    messages = [record.getMessage() for record in caplog.records]

    assert not any(CANARY in message for message in messages)

    def other_version(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "wording_version": f"{CANARY}-v"}

    with pytest.raises(GraphFailure) as raised:
        triage(
            "CLM-0011", tools=StubTools(tamper=other_version), description=description
        )

    assert failure_reason(raised.value) == "other-wording"
    assert CANARY not in str(raised.value)
    assert "2026-01" not in str(raised.value)
    assert not any(CANARY in record.getMessage() for record in caplog.records)


# -- the pause ----------------------------------------------------------------

DECISIONS = ("approve", "reject", "request_documents")
# A claim per way to reach the adjuster route: a rule, an inactive policy, and
# no policy at all.
ADJUSTER_CLAIMS = [
    pytest.param("CLM-0004", {}, "over_threshold", id="over-threshold"),
    pytest.param("CLM-0002", {}, "policy_inactive", id="policy-inactive"),
    pytest.param(
        "CLM-0001", {"policy_number": "POL-9999"}, "policy_not_found", id="no-policy"
    ),
]


def paused(
    claim_id: str = "CLM-0004",
    tools: StubTools | None = None,
    model: StubModel | None = None,
    saver: MemorySaver | None = None,
    **changes: Any,
) -> tuple[CompiledStateGraph, StubTools]:
    """A graph that ran a claim up to its pause."""
    tools = tools or StubTools()
    graph = compiled(model or StubModel(), tools, saver)
    graph.invoke({"claim": facts(claim_id, **changes)}, THREAD)
    return graph, tools


def approval_request(reason: str, claim_id: str = "CLM-0004") -> tuple[Any, ...]:
    return (
        "request_approval",
        {"claim_id": claim_id, "reason": reason},
        "request-approval",
    )


def decision_note(decision: str, claim_id: str = "CLM-0004") -> tuple[Any, ...]:
    return (
        "add_claim_note",
        {"claim_id": claim_id, "note": DECISION_NOTES[decision]},
        "decision-note",
    )


@pytest.mark.parametrize(("claim_id", "changes", "reason"), ADJUSTER_CLAIMS)
def test_a_claim_routed_to_an_adjuster_requests_approval_and_pauses(
    claim_id: str, changes: dict[str, Any], reason: str
) -> None:
    graph, tools = paused(claim_id, **changes)

    snapshot = graph.get_state(THREAD)
    assert snapshot.next == ("await_decision",)
    (pending,) = snapshot.interrupts
    assert pending.value == {"request_id": REQUEST_ID}
    assert tools.writes == [approval_request(reason, claim_id)]
    proposal = TriageProposal.model_validate(snapshot.values["output"])
    assert (proposal.route, proposal.reason) == ("adjuster", reason)
    assert snapshot.values["request_id"] == REQUEST_ID
    assert snapshot.values["decision"] is None


@pytest.mark.parametrize("decision", DECISIONS)
def test_a_decision_is_noted_with_its_fixed_text_and_the_run_completes(
    decision: str,
) -> None:
    graph, tools = paused()
    output = graph.get_state(THREAD).values["output"]
    tools.recorded = decision

    result = resume(graph, {})

    assert "__interrupt__" not in result
    snapshot = graph.get_state(THREAD)
    assert snapshot.next == ()
    assert snapshot.interrupts == ()
    assert tools.calls[-1] == ("approval_outcome", {"claim_id": "CLM-0004"})
    assert tools.names().count("approval_outcome") == 1
    assert tools.writes == [approval_request("over_threshold"), decision_note(decision)]
    assert snapshot.values["decision"] == decision
    assert snapshot.values["request_id"] == REQUEST_ID
    assert snapshot.values["output"] == output


def test_the_three_notes_are_different_fixed_texts() -> None:
    assert set(DECISION_NOTES) == set(DECISIONS)
    assert len(set(DECISION_NOTES.values())) == 3
    assert all(1 <= len(note) <= 2000 for note in DECISION_NOTES.values())


def test_a_graph_compiled_anew_resumes_the_run_from_the_checkpoint() -> None:
    saver = MemorySaver()
    paused(saver=saver)
    tools = StubTools(recorded="approve")
    graph = compiled(StubModel(), tools, saver)

    resume(graph, {})

    assert tools.writes == [decision_note("approve")]
    assert tools.names() == ["approval_outcome"]
    assert graph.get_state(THREAD).values["decision"] == "approve"


@pytest.mark.parametrize(
    ("claim_id", "route"),
    [
        ("CLM-0011", "auto_approve"),
        ("CLM-0005", "auto_approve"),
        ("CLM-0003", "request_documents"),
    ],
)
def test_the_other_routes_complete_without_a_write_or_a_pause(
    claim_id: str, route: str
) -> None:
    model = golden_model(claim_id)
    tools = StubTools()
    graph = compiled(model, tools)

    result = graph.invoke({"claim": facts(claim_id)}, THREAD)

    assert "__interrupt__" not in result
    assert result["output"]["route"] == route
    assert (result["request_id"], result["decision"]) == (None, None)
    snapshot = graph.get_state(THREAD)
    assert (snapshot.next, snapshot.interrupts) == ((), ())
    assert tools.writes == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"decision": "reject"}, id="another-word"),
        pytest.param({"decision": "approve"}, id="the-same-word"),
        pytest.param({"decision": "escalate"}, id="unknown-word"),
        pytest.param({"decision": "approve", "note": CANARY}, id="forged-note"),
        pytest.param({}, id="empty-dict"),
        pytest.param("reject", id="bare-string"),
        pytest.param(["reject"], id="list"),
        pytest.param(5, id="number"),
        pytest.param(None, id="null"),
    ],
)
def test_the_run_reads_the_record_and_never_the_resume_value(value: Any) -> None:
    """T-31: the Claims API records the decision, the run only reads it. Whatever
    the resume carries, the note and the state follow the record."""
    graph, tools = paused()
    tools.recorded = "approve"

    resume(graph, value)

    assert tools.writes == [
        approval_request("over_threshold"),
        decision_note("approve"),
    ]
    assert graph.get_state(THREAD).values["decision"] == "approve"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="no-outcome"),
        pytest.param({"outcome": None}, id="null-outcome"),
    ],
)
def test_a_run_with_no_recorded_decision_fails_before_any_write(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"approval_outcome": answer})
    graph, _ = paused(tools=tools)

    with pytest.raises(GraphFailure) as raised:
        resume(graph, {"decision": "approve"})

    assert raised.value.code == "decision-not-recorded"
    assert tools.writes == [approval_request("over_threshold")]
    snapshot = graph.get_state(THREAD)
    # What the runtime reads to keep the run AwaitingApproval.
    assert len(snapshot.interrupts) == 1
    assert snapshot.values["decision"] is None


def test_a_run_that_found_no_decision_completes_when_one_is_recorded() -> None:
    graph, tools = paused()
    with pytest.raises(GraphFailure):
        resume(graph, {})
    tools.recorded = "request_documents"

    resume(graph, {})

    assert tools.writes == [
        approval_request("over_threshold"),
        decision_note("request_documents"),
    ]
    assert graph.get_state(THREAD).values["decision"] == "request_documents"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"outcome": "escalate"}, id="unknown-word"),
        pytest.param({"outcome": "APPROVE"}, id="capitals"),
        pytest.param({"outcome": " approve"}, id="padded"),
        pytest.param({"outcome": ["approve"]}, id="list-word"),
        pytest.param({"outcome": 1}, id="number-word"),
        pytest.param({"outcome": "approve", "note": "ok"}, id="extra-field"),
        pytest.param({"Outcome": "approve"}, id="other-key"),
        pytest.param({"decision": "approve"}, id="another-key"),
        pytest.param({"outcome": f"{CANARY}-word", CANARY: CANARY}, id="canary"),
    ],
)
def test_an_outcome_answer_that_does_not_fit_fails_the_run_before_any_write(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"approval_outcome": answer})
    graph, _ = paused(tools=tools)

    with pytest.raises(GraphFailure) as raised:
        resume(graph, {})

    assert raised.value.code == "approval-outcome-unfit"
    assert CANARY not in str(raised.value)
    assert raised.value.__cause__ is None
    assert tools.writes == [approval_request("over_threshold")]


def test_a_rerun_of_the_approval_request_sends_the_same_payload_and_step() -> None:
    """T-23: the idempotency key is the run, the tool and the step, so a node
    that runs again must send the same payload under the same step and be
    answered with the stored request."""
    tools = StubTools()
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, tools))
    state = {
        "claim": facts("CLM-0004"),
        "output": expected_proposal(reason="over_threshold"),
    }
    node = graph.nodes["request_approval"].runnable

    first, second = node.invoke(state), node.invoke(state)

    assert first == second == {"request_id": REQUEST_ID}
    assert tools.writes == [approval_request("over_threshold")] * 2


def test_a_run_replayed_from_the_checkpoint_before_the_request_sends_it_again() -> None:
    graph, tools = paused()
    (before,) = [
        s for s in graph.get_state_history(THREAD) if s.next == ("request_approval",)
    ]

    graph.invoke(None, before.config)

    assert tools.writes == [approval_request("over_threshold")] * 2
    assert graph.get_state(THREAD).next == ("await_decision",)


def test_a_resume_that_failed_after_its_note_sends_the_same_note_again() -> None:
    """The node that paused runs from its start when the run resumes, so a
    resume that failed after sending the note sends it again: the same one."""
    tools = StubTools(answers={"add_claim_note": {}}, recorded="reject")
    graph, _ = paused(tools=tools)
    with pytest.raises(GraphFailure):
        resume(graph, {})
    del tools.answers["add_claim_note"]

    resume(graph, {})

    assert tools.writes[1:] == [decision_note("reject")] * 2
    assert graph.get_state(THREAD).values["decision"] == "reject"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="empty"),
        pytest.param({"request_id": REQUEST_ID}, id="no-replayed"),
        pytest.param({"request_id": "not-a-uuid", "replayed": False}, id="not-a-uuid"),
        pytest.param({"request_id": REQUEST_ID, "replayed": "yes"}, id="not-a-bool"),
        pytest.param(
            {"request_id": REQUEST_ID, "replayed": False, "more": 1}, id="extra-field"
        ),
        pytest.param({"request_id": f"{CANARY}-id", "replayed": CANARY}, id="canary"),
    ],
)
def test_an_approval_answer_that_does_not_fit_fails_the_run(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"request_approval": answer})

    with pytest.raises(GraphFailure) as raised:
        paused(tools=tools)

    assert raised.value.code == "approval-request-unfit"
    assert CANARY not in str(raised.value)
    assert len(tools.writes) == 1


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="empty"),
        pytest.param({"note_id": NOTE_ID}, id="no-replayed"),
        pytest.param({"note_id": "not-a-uuid", "replayed": False}, id="not-a-uuid"),
        pytest.param({"note_id": NOTE_ID, "replayed": 3}, id="not-a-bool"),
        pytest.param({"request_id": NOTE_ID, "replayed": False}, id="other-key"),
        pytest.param({"note_id": f"{CANARY}-id", "replayed": CANARY}, id="canary"),
    ],
)
def test_a_note_answer_that_does_not_fit_fails_the_run(
    answer: dict[str, Any],
) -> None:
    tools = StubTools(answers={"add_claim_note": answer}, recorded="approve")
    graph, _ = paused(tools=tools)

    with pytest.raises(GraphFailure) as raised:
        resume(graph, {})

    assert raised.value.code == "decision-note-unfit"
    assert CANARY not in str(raised.value)
    assert [w[0] for w in tools.writes] == ["request_approval", "add_claim_note"]


@pytest.mark.parametrize("tool", ["request_approval", "add_claim_note"])
def test_a_write_tool_error_fails_the_run_unchanged(tool: str) -> None:
    error = ToolRefused(tool, "idempotency-conflict")
    tools = StubTools(errors={tool: error}, recorded="approve")

    with pytest.raises(ToolRefused) as raised:
        graph, _ = paused(tools=tools)
        resume(graph, {})

    assert raised.value is error


def test_the_state_after_a_resumed_run_holds_only_plain_data() -> None:
    graph, tools = paused()
    tools.recorded = "request_documents"
    resume(graph, {})

    values = graph.get_state(THREAD).values

    assert set(values) == STATE_KEYS
    assert (values["request_id"], values["decision"]) == (
        REQUEST_ID,
        "request_documents",
    )
    assert_plain(values)


def test_no_log_line_and_no_error_of_the_pause_holds_claim_text_or_a_tool_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    description = f"A tree fell on the car. {CANARY}-claim"
    unfit = {"request_id": f"{CANARY}-id", "replayed": f"{CANARY}-flag"}

    graph, _ = paused(tools=StubTools(recorded="approve"), description=description)
    resume(graph, {})
    with pytest.raises(GraphFailure) as bad_request:
        paused(tools=StubTools(answers={"request_approval": unfit}))
    graph, _ = paused(
        tools=StubTools(answers={"add_claim_note": unfit}, recorded="approve")
    )
    with pytest.raises(GraphFailure) as bad_note:
        resume(graph, {})
    graph, _ = paused(
        tools=StubTools(answers={"approval_outcome": {"outcome": f"{CANARY}-word"}})
    )
    with pytest.raises(GraphFailure) as bad_decision:
        resume(graph, {f"{CANARY}-key": f"{CANARY}-word"})

    assert [failure_reason(e.value) for e in (bad_request, bad_note, bad_decision)] == [
        "approval-request-unfit",
        "decision-note-unfit",
        "approval-outcome-unfit",
    ]
    assert not any(
        CANARY in str(e.value) for e in (bad_request, bad_note, bad_decision)
    )
    assert not any(CANARY in record.getMessage() for record in caplog.records)


# -- the factory --------------------------------------------------------------


def test_the_graph_is_returned_uncompiled() -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))

    assert not hasattr(graph, "invoke")


def test_the_graph_has_seven_nodes_and_two_branches() -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))

    assert list(graph.nodes) == [
        "lookup_policy",
        "load_history",
        "retrieve_terms",
        "assess",
        "propose",
        "request_approval",
        "await_decision",
    ]


def test_the_entry_point_is_installed_from_the_meridian_distribution() -> None:
    (entry,) = [
        e for e in entry_points(group="meridian.graphs") if e.name == "claims-triage"
    ]

    assert entry.value == "meridian.workloads.claims_triage.graph:build"
    assert entry.dist is not None and entry.dist.name == "meridian"


def test_the_runtime_loads_the_workload_graph_through_the_registry() -> None:
    factory = load_graph_factory("claims-triage", load_registry(REGISTRY_DIR))

    assert factory is build
