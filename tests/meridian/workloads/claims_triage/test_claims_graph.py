"""The triage graph (S014): five nodes, the tools in a fixed order, one model call.

No database and no tool server: a stub answers each tool from the synthetic data
and records the calls, and a stub stands in for the model client. The stub
``wording_search`` returns every clause of the policy's wording (the real search
is the next contract's). The first group runs the 40 golden claims, the second
pins the full proposal of one claim per reason, the third the calls, the fourth
what fails and what does not, the fifth what the state and the logs hold.
"""

import json
import logging
from collections.abc import Callable
from functools import cache
from importlib.metadata import entry_points
from typing import Any, cast

import pytest
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.platform.registry import load_registry
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
from meridian.workloads.claims_triage.assessment import ASSESSMENT_OUTPUT_TOKENS
from meridian.workloads.claims_triage.graph import build
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
    data and records every call. A tool it does not know (a write tool) raises.

    ``errors`` makes a tool raise; ``answers`` replaces a tool's answer; ``tamper``
    changes the answer of the n-th ``wording_search`` call."""

    def __init__(
        self,
        *,
        errors: dict[str, Exception] | None = None,
        answers: dict[str, dict[str, Any]] | None = None,
        tamper: Callable[[int, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors = errors or {}
        self.answers = answers or {}
        self.tamper = tamper
        self.searches = 0

    def call(
        self, tool: str, arguments: dict[str, Any], *, step: str | None = None
    ) -> ToolResult:
        self.calls.append((tool, dict(arguments)))
        assert step is None, "a read tool takes no step"
        if tool in self.errors:
            raise self.errors[tool]
        if tool in self.answers:
            return ToolResult(self.answers[tool], replayed=False, call_id=None)
        data = {
            "policy_lookup": self._policy,
            "claim_history": self._history,
            "wording_search": self._search,
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


def run_graph(
    model: StubModel, tools: StubTools, claim: dict[str, Any]
) -> dict[str, Any]:
    graph = build(cast(ModelClient, model), cast(ToolClient, tools))
    return graph.compile().invoke({"claim": claim})


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


# -- one claim per reason -----------------------------------------------------

DRAFTED_BY = {"deployment": "eu-chat", "provider": "azure-openai", "mode": "live"}


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
        drafted_by=DRAFTED_BY,
    )
    assert output["route"] != "auto_approve"


def test_a_model_answer_that_cannot_be_trusted_never_auto_approves() -> None:
    for text in ("", "[]", '{"verdict": "none"}', model_answer("applies", "9.9")):
        output, _, _ = triage("CLM-0011", StubModel(text))

        assert (output["route"], output["reason"]) == ("adjuster", "unverified")
        assert output["assessment"] == "unavailable"
        assert output["gaps"] == ["exclusion_assessment"]


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
def test_no_run_calls_a_write_tool_or_the_model_twice(claim_id: str) -> None:
    model, tools = golden_model(claim_id), StubTools()

    triage(claim_id, model, tools)

    assert set(tools.names()) <= set(READ_TOOLS)
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

    with pytest.raises(ValueError, match="wording"):
        triage("CLM-0011", model, StubTools(tamper=other_version))

    assert model.calls == []


def test_a_search_answer_for_another_product_fails_the_run() -> None:
    def other_product(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "product": "HOME-STD"}

    with pytest.raises(ValueError, match="wording"):
        triage("CLM-0011", tools=StubTools(tamper=other_product))


def test_a_policy_answer_that_does_not_fit_fails_the_run() -> None:
    policy = StubTools._policy({"policy_number": CLAIMS["CLM-0011"]["policy_number"]})
    broken = {"found": True, "policy": {**policy["policy"], "status": CANARY}}
    tools = StubTools(answers={"policy_lookup": broken})

    with pytest.raises(ValueError, match="does not fit") as raised:
        triage("CLM-0011", tools=tools)

    assert CANARY not in str(raised.value)
    assert tools.names() == ["policy_lookup"]


def test_a_history_entry_that_does_not_fit_fails_the_run() -> None:
    entry = {"history_id": "HIST-0001", "loss_date": CANARY, "peril": "storm"}
    tools = StubTools(
        answers={"claim_history": {"entries": [entry], "truncated": False}}
    )

    with pytest.raises(ValueError, match="does not fit") as raised:
        triage("CLM-0011", tools=tools)

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

    with pytest.raises(ValueError) as raised:
        triage(
            "CLM-0011", tools=StubTools(tamper=other_version), description=description
        )

    assert CANARY not in str(raised.value)
    assert "2026-01" not in str(raised.value)
    assert not any(CANARY in record.getMessage() for record in caplog.records)


# -- the factory --------------------------------------------------------------


def test_the_graph_is_returned_uncompiled() -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))

    assert not hasattr(graph, "invoke")


def test_the_graph_has_five_nodes_in_a_line_with_one_branch() -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))

    assert list(graph.nodes) == [
        "lookup_policy",
        "load_history",
        "retrieve_terms",
        "assess",
        "propose",
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
