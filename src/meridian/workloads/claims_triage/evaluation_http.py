"""The claims workload's side of ``meridian eval run`` (S050, T-80): which
claims to post, where to read each proposal and how to grade the answers.

The platform's command posts the golden set's claims to a deployed Claims API
and reads each proposal from the adjuster's JSON route. The grading is the
workload's: the ten rule graders of ``evaluation.build_report``, over the cases
that ran, against the oracle and the policies of the golden set. A run against a
deployed stack has no judge, no ledger and no tool calls (tool arguments are
claim data), so the report holds the ten grades and nothing else.

Published in the entry-point group ``meridian.evaluations`` (pyproject.toml).
"""

import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import JsonValue, ValidationError

from meridian.platform.evaluation.report import (
    AnsweredBy,
    Report,
    ReportError,
    read_json_file,
)
from meridian.platform.evaluation.workload import Submission
from meridian.platform.registry.models import Registry

from .assessment import PROMPT_VERSION
from .evaluation import WORKLOAD, build_report
from .proposal import TriageProposal

CLAIMS_PATH = "/claims"
ANSWER_PATH = "/adjuster/claims/{claim_id}/proposal"
CLAIMS_FILE = "claims.json"
EXPECTED_FILE = "expected-outcomes.json"
POLICIES_FILE = "policies.json"
MANIFEST_FILE = "manifest.json"
# What a proposal's ``drafted_by.mode`` says about who answered the model's turn.
ANSWERED_BY_MODE = {
    "replay": AnsweredBy(kind="replay", label="simulated"),
    "recorded": AnsweredBy(kind="recorded", label="real"),
    "live": AnsweredBy(kind="live", label="real"),
}
# No claim asked the model, so nothing says who would have answered.
NO_MODEL_ASKED = ANSWERED_BY_MODE["replay"]
# The shape of a claim ID, repeated from ClaimFacts.claim_id (models.py): a test
# keeps the two equal. fullmatch, not match: "$" would accept a trailing newline.
CLAIM_ID = re.compile(r"CLM-[0-9]{4}")
# Fixed, and it quotes nothing: the ID is the file's own text and goes into a
# path, a log line and a CI annotation.
NOT_A_CLAIM_ID = "a claim in the golden set has an ID that is not a claim ID"
NOT_A_LIST = "a golden set file is not a list of records"
FILES_DISAGREE = "the golden set's files do not agree"
UNKNOWN_CASE = "an answer is for a claim the golden set does not hold"
NO_ANSWER_TO_GRADE = "no case has an answer to grade"
UNKNOWN_MODE = "a proposal names a mode this evaluation does not know"
SEVERAL_MODES = "the proposals name more than one mode"
SEVERAL_PROMPTS = "the proposals name more than one prompt"


def _records(golden_set: Path, name: str, key: str) -> dict[str, dict[str, Any]]:
    """The file's records by ``key``; refuse a file that is not a list of
    objects each with a text ``key`` that no other record has."""
    document = read_json_file(golden_set / name)
    if not isinstance(document, list):
        raise ReportError(NOT_A_LIST)
    found: dict[str, dict[str, Any]] = {}
    for record in document:
        if not isinstance(record, dict) or not isinstance(record.get(key), str):
            raise ReportError(NOT_A_LIST)
        if record[key] in found:
            raise ReportError(NOT_A_LIST)
        found[record[key]] = record
    return found


def _proposal_of(answer: JsonValue) -> TriageProposal | None:
    """The answer's proposal; ``None`` for an answer without one, or with one
    that is not a proposal: a claim with no proposal fails every grade."""
    document = answer.get("proposal") if isinstance(answer, dict) else None
    if document is None:
        return None
    try:
        return TriageProposal.model_validate(document)
    except ValidationError:
        return None


def _answered_by(proposals: Collection[TriageProposal | None]) -> AnsweredBy:
    """The one mode the proposals name; ``replay`` when none names a mode."""
    modes = {p.drafted_by.mode for p in proposals if p and p.drafted_by}
    if len(modes) > 1:
        raise ReportError(SEVERAL_MODES)
    if not modes:
        return NO_MODEL_ASKED
    (mode,) = modes
    if mode not in ANSWERED_BY_MODE:
        raise ReportError(UNKNOWN_MODE)
    return ANSWERED_BY_MODE[mode]


def _prompt_of(proposals: Collection[TriageProposal | None]) -> str:
    """The prompt version the stack's proposals name (it is the stack's prompt
    that was graded, not this tree's); this tree's when none names one."""
    prompts = {
        p.drafted_by.prompt
        for p in proposals
        if p and p.drafted_by and p.drafted_by.prompt
    }
    if len(prompts) > 1:
        raise ReportError(SEVERAL_PROMPTS)
    return next(iter(prompts), PROMPT_VERSION)


class ClaimsEvaluation:
    """Satisfies ``WorkloadEvaluation``."""

    workload = WORKLOAD
    # The field of the adjuster's answer that names its claim.
    case_field = "claim_id"

    def submissions(self, golden_set: Path) -> Sequence[Submission]:
        claims = _records(golden_set, CLAIMS_FILE, "claim_id")
        if not all(CLAIM_ID.fullmatch(case) for case in claims):
            raise ReportError(NOT_A_CLAIM_ID)
        return tuple(
            Submission(case, CLAIMS_PATH, claims[case]) for case in sorted(claims)
        )

    def answer_path(self, case: str) -> str:
        # The ID goes into a path: only the shape of a claim ID may.
        if CLAIM_ID.fullmatch(case) is None:
            raise ReportError(NOT_A_CLAIM_ID)
        return ANSWER_PATH.format(claim_id=case)

    def report(
        self, answers: Mapping[str, JsonValue], golden_set: Path, registry: Registry
    ) -> Report:
        if not answers:
            raise ReportError(NO_ANSWER_TO_GRADE)
        claims = _records(golden_set, CLAIMS_FILE, "claim_id")
        expected = _records(golden_set, EXPECTED_FILE, "claim_id")
        policies = _records(golden_set, POLICIES_FILE, "policy_number")
        if not set(answers) <= set(claims):
            raise ReportError(UNKNOWN_CASE)
        if not set(answers) <= set(expected):
            raise ReportError(FILES_DISAGREE)
        if not {claims[c].get("policy_number") for c in answers} <= set(policies):
            raise ReportError(FILES_DISAGREE)
        proposals = {case: _proposal_of(answer) for case, answer in answers.items()}
        return build_report(
            proposals,
            {case: expected[case] for case in answers},
            {case: claims[case] for case in answers},
            policies,
            manifest_path=golden_set / MANIFEST_FILE,
            registry=registry,
            answered_by=_answered_by(proposals.values()),
            prompt=_prompt_of(proposals.values()),
        )


EVALUATION = ClaimsEvaluation()
