"""The claims workload's evaluation: rule graders over the golden set.

These are rule graders only. T-29: the route and the payable amount are decided
by rules, so a rule can say whether they are right; whether a rationale is
good is a judgement, and an LLM judge is S050, not here. With a scripted model
(as the stack test runs) the grades are the pipeline's, not a real model's
(T-72): the model answers from the oracle and ignores the prompt.

The functions are pure. The caller loads the golden set and the registry and
decides where the report goes.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from meridian.platform.evaluation.fingerprints import golden_set_of, tools_fingerprint
from meridian.platform.evaluation.report import (
    AnsweredBy,
    Case,
    Fingerprints,
    Report,
    ReportError,
    read_text_file,
)
from meridian.platform.registry.models import Registry

from .proposal import TriageProposal

WORKLOAD = "claims-triage"
GRADERS = (
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
# The graders that must pass on every case.
ABSOLUTE = ("completed", "human_oversight")
# QA-06: the route matches the oracle's for at least 90 % of the claims.
TARGETS = {"route": 0.9}
# The graders that compare one field of the proposal with the oracle's.
FIELD_GRADERS = tuple(g for g in GRADERS if g not in ABSOLUTE)
ADJUSTER = "adjuster"
AUTO_APPROVE = "auto_approve"


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
    """Grade one claim's proposal against its oracle entry. With no proposal
    every grade is false."""
    claim_id = expected["claim_id"]
    if proposal is None:
        return Case(
            case=claim_id,
            grades=dict.fromkeys(GRADERS, False),
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
        grades={name: grades[name] for name in GRADERS},
        observed=_observed(proposal),
    )


def _auto_approval_limit(manifest_path: Path) -> int:
    """The golden set's auto-approval limit, from its manifest; refuse a manifest
    that is not JSON or has no integer limit."""
    try:
        manifest = json.loads(read_text_file(manifest_path))
    except json.JSONDecodeError:
        raise ReportError("the manifest is not valid JSON") from None
    limit = manifest.get("auto_approval_limit") if isinstance(manifest, dict) else None
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ReportError("the manifest has no integer auto_approval_limit")
    return limit


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
) -> Report:
    """The evaluation report of a run: one case per claim in ``expected``, sorted
    by claim id, graded against the policy of the claim."""
    limit = _auto_approval_limit(manifest_path)
    cases = tuple(
        grade(
            proposals.get(claim_id),
            expected[claim_id],
            policies[claims[claim_id]["policy_number"]],
            limit,
        )
        for claim_id in sorted(expected)
    )
    return Report(
        format=1,
        workload=WORKLOAD,
        answered_by=answered_by,
        fingerprints=Fingerprints(
            prompt=prompt,
            tools=tools_fingerprint(registry, WORKLOAD),
            golden_set=golden_set_of(manifest_path),
        ),
        absolute=ABSOLUTE,
        targets=TARGETS,
        cases=cases,
    )
