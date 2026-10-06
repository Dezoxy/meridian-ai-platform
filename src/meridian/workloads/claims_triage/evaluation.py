"""The claims workload's evaluation: rule graders over the golden set, and, for a
judged run, the judge's groundedness, the cost and the latency.

T-29: the route and the payable amount are decided by rules, so a rule can say
whether they are right; whether a rationale is good is a judgement. The ten rule
graders are computed by ``grade`` from the proposal and the oracle alone: it
takes no judgement, and the judge's verdict is added afterwards, in its own
grader, never to override a rule (T-29, T-79). That grader is in neither
``ABSOLUTE`` nor ``TARGETS``. With a scripted model the grades are the
pipeline's, not a real model's (T-72); a recorded run replays a real model's
answers, a live run asks it.

The functions are pure. The caller loads the golden set and the registry and
decides where the report goes.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import JsonValue

from meridian.platform.evaluation.fingerprints import golden_set_of, tools_fingerprint
from meridian.platform.evaluation.judge import GRADER, Judgement
from meridian.platform.evaluation.report import (
    REPORT_FORMAT,
    AnsweredBy,
    Case,
    Fingerprints,
    Measured,
    Report,
    ReportError,
    ToolCall,
    read_json_file,
)
from meridian.platform.guardrails import ScreenSourceUnavailable, screen_fingerprint
from meridian.platform.registry.models import Registry

from .proposal import TriageProposal
from .wording import Clause

WORKLOAD = "claims-triage"
# The reason of a claim on a policy number no policy has; the only claim that
# may be graded with no policy.
POLICY_NOT_FOUND = "policy_not_found"
# Fixed, and it quotes nothing: the files' own text may be a policy number.
FILES_DISAGREE = "the golden set's files do not agree"
# The graders that compare the proposal with the oracle (S017).
RULE_GRADERS = (
    "completed",
    "route",
    "reason",
    "recommendation",
    "payable_amount",
    "fraud_indicators",
    "missing_documents",
    "exclusion_clause",
    "citations",
    "human_oversight",
)
GROUNDEDNESS = GRADER
COST = "cost"
# Graded in a live run only: the ledger times a file read in a recorded one.
LATENCY = "latency"
GRADERS = (*RULE_GRADERS, GROUNDEDNESS, COST)
# The graders that must pass on every case.
ABSOLUTE = ("completed", "human_oversight")
# QA-06: the route matches the oracle's for at least 90 % of the claims.
TARGETS = {"route": 0.9}
# QA-07: EUR 0.02 per claim, in millionths of a euro.
MAX_COST_MICRO_EUR_PER_CLAIM = 20_000
# QA-01's bound with gpt-4o, of which the model calls are a part.
MAX_MODEL_LATENCY_MS_PER_CLAIM = 30_000
# The graders that compare one field of the proposal with the oracle's.
FIELD_GRADERS = tuple(g for g in RULE_GRADERS if g not in ABSOLUTE)
ADJUSTER = "adjuster"
AUTO_APPROVE = "auto_approve"
# What ``observed`` says of the judge's verdict when there was none to ask for,
# or none came back.
NO_RATIONALE = "no-rationale"
UNJUDGED = "unjudged"
# A claim whose run the ledger never saw measured nothing.
NO_RUN = Measured(model_calls=0, input_tokens=0, output_tokens=0, cost_micro_eur=0)


class JudgeInputs(NamedTuple):
    """What the judge is asked about one proposal."""

    source: dict[str, JsonValue]
    statement: str


def _wanted(expected: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """What the oracle says the proposal holds, in the proposal's own shape: the
    exclusion's clause is the first citation's when the reason is ``excluded``,
    and each citation carries the policy's product and wording version."""
    return {
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


def _oversight(
    proposal: TriageProposal, expected_route: str, auto_approval_limit: int
) -> bool:
    """QA-06's absolute half, from the proposal alone and the expected route:
    false when an automatic approval is above the limit, has no amount or has a
    fraud indicator; when a rejection is recommended off the adjuster's route;
    or when a wrong route sends the claim anywhere but to an adjuster."""
    route = proposal.route
    if route == AUTO_APPROVE and (
        proposal.payable_amount is None
        or proposal.payable_amount > auto_approval_limit
        or bool(proposal.fraud_indicators)
    ):
        return False
    if proposal.recommendation == "reject" and route != ADJUSTER:
        return False
    return route in (expected_route, ADJUSTER)


def _observed(proposal: TriageProposal | None) -> dict[str, str | int | None]:
    """The proposal's route, reason, recommendation, payable amount and
    assessment, for a reader of the diff; nulls when there is no proposal."""
    return {
        "route": proposal.route if proposal else None,
        "reason": proposal.reason if proposal else None,
        "recommendation": proposal.recommendation if proposal else None,
        "payable_amount": proposal.payable_amount if proposal else None,
        "assessment": proposal.assessment if proposal else None,
    }


def grade(
    proposal: TriageProposal | None,
    expected: Mapping[str, Any],
    policy: Mapping[str, Any],
    auto_approval_limit: int,
) -> Case:
    """Grade one claim's proposal against its oracle entry with the ten rule
    graders; no judgement takes part. With no proposal every grade is false."""
    claim_id = expected["claim_id"]
    if proposal is None:
        return Case(
            case=claim_id,
            grades=dict.fromkeys(RULE_GRADERS, False),
            observed=_observed(None),
        )
    wanted = _wanted(expected, policy)
    got = proposal.model_dump(mode="json")
    grades = {"completed": True}
    grades.update({name: got[name] == wanted[name] for name in FIELD_GRADERS})
    grades["human_oversight"] = _oversight(
        proposal, expected["route"], auto_approval_limit
    )
    return Case(
        case=claim_id,
        grades={name: grades[name] for name in RULE_GRADERS},
        observed=_observed(proposal),
    )


def _statement(proposal: TriageProposal) -> str:
    """The verdict the proposal's assessment states, the clause when there is
    one, and the rationale, as one text. It must not read as a demand about the
    answer of the triage call: "Verdict:" or "Rationale:" would, and the judge
    refuses a statement that addresses the model."""
    if proposal.assessment == "applies":
        verdict = "An exclusion applies"
        if proposal.exclusion_clause is not None:
            verdict += f" (clause {proposal.exclusion_clause})"
    elif proposal.assessment == "none_applies":
        verdict = "No exclusion applies"
    else:
        verdict = f"The assessment is {proposal.assessment}"
    return f"{verdict}. {proposal.rationale}"


def judge_inputs(
    claim: Mapping[str, Any],
    proposal: TriageProposal,
    candidates: Sequence[Clause],
) -> JudgeInputs | None:
    """What the judge is asked whether the rationale is supported by: the
    claim's peril and description and the candidate clauses the model was shown
    (number, title, text), and the statement. Nothing else of the claim: no
    name, email, policy number or claim ID (T-03). None when the proposal has no
    rationale, so there is nothing to ground."""
    if proposal.rationale is None:
        return None
    source: dict[str, JsonValue] = {
        "peril": claim["peril"],
        "description": claim["description"],
        "clauses": [
            {"number": c.clause, "title": c.title, "text": c.body} for c in candidates
        ],
    }
    return JudgeInputs(source, _statement(proposal))


def _grounded(proposal: TriageProposal, judgement: Judgement | None) -> bool:
    """True when there is no rationale to ground, else the judgement's verdict;
    a rationale nobody judged is not grounded."""
    if proposal.rationale is None:
        return True
    return judgement is not None and judgement.grounded


def _judged_label(
    proposal: TriageProposal | None, judgement: Judgement | None
) -> str | None:
    if proposal is None:
        return None
    if proposal.rationale is None:
        return NO_RATIONALE
    return UNJUDGED if judgement is None else judgement.outcome


def _extra_grades(
    proposal: TriageProposal | None,
    judgement: Judgement | None,
    measured: Measured,
    *,
    live: bool,
) -> dict[str, bool]:
    """The grades a judged and measured run adds to the ten; all false when the
    claim has no proposal."""
    if proposal is None:
        return dict.fromkeys((GROUNDEDNESS, COST, *((LATENCY,) if live else ())), False)
    grades = {
        GROUNDEDNESS: _grounded(proposal, judgement),
        COST: measured.cost_micro_eur <= MAX_COST_MICRO_EUR_PER_CLAIM,
    }
    if live:
        grades[LATENCY] = (
            measured.latency_ms is not None
            and measured.latency_ms <= MAX_MODEL_LATENCY_MS_PER_CLAIM
        )
    return grades


def _judged_case(
    case: Case,
    proposal: TriageProposal | None,
    judgement: Judgement | None,
    measured: Measured,
    tools: Sequence[ToolCall],
    *,
    live: bool,
) -> Case:
    """``case`` (the ten rule grades) with the judge's, the cost's and the
    latency's added, what the judge said to be read in a diff, and what the run
    measured and called. The rule grades are copied as they are."""
    if proposal is None:
        judgement = None
    extra = _extra_grades(proposal, judgement, measured, live=live)
    return Case(
        case=case.case,
        grades={**case.grades, **extra},
        observed={
            **case.observed,
            "rationale": proposal.rationale if proposal else None,
            "groundedness": _judged_label(proposal, judgement),
            "judge_reason": judgement.reason if judgement else None,
        },
        tools=tuple(tools),
        measured=measured,
    )


def auto_approval_limit(manifest_path: Path) -> int:
    """The golden set's auto-approval limit, from its manifest; refuse a manifest
    that is not JSON or has no integer limit."""
    manifest = read_json_file(manifest_path)
    limit = manifest.get("auto_approval_limit") if isinstance(manifest, dict) else None
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ReportError("the manifest has no integer auto_approval_limit")
    return limit


SCREENS_UNREADABLE = "the source of the screens cannot be read to fingerprint them"


def screens_fingerprint() -> str:
    """The fingerprint of the screens for a report; ``ReportError`` with a
    fixed text where their source cannot be read. Both report builders use it."""
    try:
        return screen_fingerprint()
    except ScreenSourceUnavailable as exc:
        raise ReportError(SCREENS_UNREADABLE) from exc


def _check_parts(
    parts: Sequence[object | None], fingerprints: Sequence[str | None]
) -> None:
    """Refuse a report that is half judged and measured: the judgements, the
    measures and the tool calls come together or not at all, and the judge's and
    the recording's fingerprints belong to the judged kind."""
    given = [part is not None for part in parts]
    if any(given) and not all(given):
        raise ValueError(
            "judgements, measured and tools are given together or not at all"
        )
    if not all(given) and any(f is not None for f in fingerprints):
        raise ValueError(
            "a judge or recording fingerprint belongs to a judged and measured report"
        )


def _is_list_of(kind: type, value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, kind) for v in value)


def _is_readable(expected: Mapping[str, Any]) -> bool:
    """Whether ``_wanted`` and ``grade`` can read the expected record: each field
    they index is there and of the kind they use. A recommendation and a payable
    amount may be null (an integer is not a boolean); an ``excluded`` record's
    first citation names the exclusion's clause."""
    amount = expected.get("payable_amount")
    recommendation = expected.get("recommendation")
    citations = expected.get("citations")
    return (
        # A null is a value, a missing key is not: both read as None by ``get``.
        "payable_amount" in expected
        and "recommendation" in expected
        and isinstance(expected.get("route"), str)
        and isinstance(expected.get("reason"), str)
        and (recommendation is None or isinstance(recommendation, str))
        and (amount is None or _is_integer(amount))
        and _is_list_of(str, expected.get("fraud_indicators"))
        and _is_list_of(str, expected.get("missing_documents"))
        and isinstance(citations, list)
        and all(_names_a_clause(c) for c in citations)
        and (expected["reason"] != "excluded" or bool(citations))
    )


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _names_a_clause(citation: Any) -> bool:
    return isinstance(citation, dict) and isinstance(citation.get("clause"), str)


def _policy_is_readable(policy: Mapping[str, Any]) -> bool:
    """Whether ``_wanted`` can read the policy: its product and its wording
    version, the two fields each citation carries, are strings."""
    return isinstance(policy.get("product"), str) and isinstance(
        policy.get("wording_version"), str
    )


def _policy_of(
    expected: Mapping[str, Any],
    claim: Mapping[str, Any],
    policies: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """The claim's policy. A claim on a policy number no policy has is graded
    with an empty policy only when its label says ``policy_not_found`` and cites
    nothing (so no field of a policy is read); any other claim with no policy is
    a golden set whose files disagree. So is a record the grading cannot read: a
    claim with no policy number, an expected record that ``_is_readable`` refuses
    (a field missing or of another kind, citations that name no clause), a
    policy that ``_policy_is_readable`` refuses (no product or wording version
    of the kind a citation carries); the files are a folder a person chose. This
    is the one place the records are checked, before ``grade`` indexes them."""
    number = claim.get("policy_number")
    if not (isinstance(number, str) and _is_readable(expected)):
        raise ReportError(FILES_DISAGREE)
    reason, citations = expected["reason"], expected["citations"]
    policy = policies.get(number)
    if policy is not None:
        if not _policy_is_readable(policy):
            raise ReportError(FILES_DISAGREE)
        return policy
    if reason == POLICY_NOT_FOUND and not citations:
        return {}
    raise ReportError(FILES_DISAGREE)


def build_report(
    proposals: Mapping[str, TriageProposal | None],
    expected: Mapping[str, Mapping[str, Any]],
    claims: Mapping[str, Mapping[str, Any]],
    policies: Mapping[str, Mapping[str, Any]],
    *,
    manifest_path: Path,
    registry: Registry,
    answered_by: AnsweredBy,
    prompt: str,
    judgements: Mapping[str, Judgement] | None = None,
    measured: Mapping[str, Measured] | None = None,
    tools: Mapping[str, Sequence[ToolCall]] | None = None,
    judge_fingerprint: str | None = None,
    recording_fingerprint: str | None = None,
) -> Report:
    """The evaluation report of a run: one case per claim in ``expected``, sorted
    by claim id, graded against the policy of the claim.

    Without ``judgements``, ``measured`` and ``tools`` it grades the ten rule
    graders only. With them (all three, or ``ValueError``) it adds groundedness
    and cost, and latency in a live run, and each case holds its tool calls and
    what it measured; a claim the maps do not name measured nothing. The rule
    grades are computed before and without the judgements."""
    _check_parts(
        (judgements, measured, tools), (judge_fingerprint, recording_fingerprint)
    )
    limit = auto_approval_limit(manifest_path)
    live = answered_by.kind == "live"
    cases = []
    for claim_id in sorted(expected):
        proposal = proposals.get(claim_id)
        case = grade(
            proposal,
            expected[claim_id],
            _policy_of(expected[claim_id], claims[claim_id], policies),
            limit,
        )
        if judgements is not None and measured is not None and tools is not None:
            case = _judged_case(
                case,
                proposal,
                judgements.get(claim_id),
                measured.get(claim_id, NO_RUN),
                tools.get(claim_id, ()),
                live=live,
            )
        cases.append(case)
    return Report(
        format=REPORT_FORMAT,
        workload=WORKLOAD,
        answered_by=answered_by,
        fingerprints=Fingerprints(
            prompt=prompt,
            tools=tools_fingerprint(registry, WORKLOAD),
            golden_set=golden_set_of(manifest_path),
            judge=judge_fingerprint,
            recording=recording_fingerprint,
            screen=screens_fingerprint(),
        ),
        absolute=ABSOLUTE,
        targets=TARGETS,
        cases=tuple(cases),
    )
