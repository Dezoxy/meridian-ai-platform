"""The facts the model is sent: the claim's, the policy's and the history's, read
from the run's input and from two tool results, and nothing else (S037).

**Nothing a person wrote as free text reaches the model.** A field is free text
when its contract allows any string: the claim's description, the names of the
documents, the city of the loss, a claimant's name or e-mail, and in the tools'
output schemas (``api/mcp/policy-mcp.json``) a policy's ``product`` and
``wording_version`` and a history entry's ``history_id``, ``peril`` and
``status``, which are bare strings there whatever the synthetic data holds. The
document below is built by naming fields, never by copying a result, so a field
a tool adds later cannot reach the model either. Every field is a number, a
date, a boolean or a word of a closed set, and each is parsed to that type
before it is written out, so no string passes through as it came.

``document`` is exactly this, and a test holds the list:

* ``claim.peril``: the run's input, one of ten words (``Peril``);
* ``claim.claimed_amount``: the input, an integer from 1 to 1,000,000;
* ``claim.loss_date`` and ``claim.reported_on``: the input, parsed as dates and
  written as ISO dates;
* ``claim.documents_received``: the input's count of documents, an integer from
  0 to 20 (the Claims API counts them and sends no name: the names are the
  claimant's, and the input stays in the run's checkpoint rows while it lives);
* ``policy.found``: ``policy_lookup``, a boolean. When it is false the policy
  object holds nothing else;
* ``policy.status``: ``policy_lookup``, ``active`` or ``lapsed`` (an enum in the
  contract);
* ``policy.start_date``, ``policy.end_date`` and ``policy.lapsed_on``:
  ``policy_lookup``, dates (a pattern in the contract, parsed again here);
* ``policy.deductible``, ``policy.limit`` and ``policy.sum_insured``:
  ``policy_lookup``, integers from 0 to 1,000,000,000;
* ``history.earlier_claims``: ``claim_history``, the number of entries;
* ``history.earlier_claims_same_peril``: the number of entries whose ``peril``
  equals the claim's, compared here: the entry's own text is never sent;
* ``history.earlier_paid_total``: ``claim_history``, the sum of the entries'
  integer ``paid_amount``;
* ``history.truncated``: ``claim_history``, a boolean.

Not sent, though they are not free text: the claim's ID and the policy number
(identifiers the model has no use for; triage leaves them out as well) and the
country of the loss. The claim's ID stays in the workflow's state only to
address the tools; the policy number is used for the two reads and kept
nowhere.

An answer that does not fit its contract is a ``GraphFailure`` with a fixed
word; pydantic's own error quotes the value that did not fit, so it is never
attached.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
)

from meridian.runtime.failures import GraphFailure

# The claims' perils (``claims_triage.models.Peril``); a test keeps the two equal.
Peril = Literal[
    "accidental_damage",
    "burglary",
    "burst_pipe",
    "collision",
    "fire",
    "flood",
    "glass",
    "storm",
    "theft",
    "third_party_liability",
]
MAX_DOCUMENTS = 20
MAX_HISTORY_ENTRIES = 100
MAX_MONEY = 1_000_000_000
CLAIM_UNFIT = "claim-unfit"
POLICY_UNFIT = "policy-record-unfit"
HISTORY_UNFIT = "history-unfit"

ClaimId = Annotated[str, StringConstraints(pattern=r"^CLM-[0-9]{4}$")]
PolicyNumber = Annotated[str, StringConstraints(pattern=r"^POL-[0-9]{4}$")]
Money = Annotated[int, Field(ge=0, le=MAX_MONEY, strict=True)]


class _Read(BaseModel):
    """What is read of an input or a result: the fields named here and no
    others. A field nobody named is ignored, never forwarded."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class ClaimInput(_Read):
    claim_id: ClaimId
    policy_number: PolicyNumber
    reported_on: date
    loss_date: date
    peril: Peril
    claimed_amount: Annotated[int, Field(ge=1, le=1_000_000, strict=True)]
    # How many documents arrived, never their names: the Claims API sends only
    # this (the names are the claimant's), and a caller that sent more is ignored.
    documents_received: Annotated[int, Field(ge=0, le=MAX_DOCUMENTS, strict=True)]


class PolicyRecord(_Read):
    status: Literal["active", "lapsed"]
    start_date: date
    end_date: date
    lapsed_on: date | None = None
    deductible: Money
    limit: Money
    sum_insured: Money | None = None


class PolicyAnswer(_Read):
    found: StrictBool
    policy: PolicyRecord | None = None


class HistoryEntry(_Read):
    # Compared with the claim's peril and never sent.
    peril: Annotated[str, Field(strict=True)]
    paid_amount: Money


class HistoryAnswer(_Read):
    entries: list[HistoryEntry] = Field(max_length=MAX_HISTORY_ENTRIES)
    truncated: StrictBool


@dataclass(frozen=True, slots=True)
class Claim:
    """What is kept of the run's input: the two identifiers that address the
    tools, the peril the history is compared with, and the claim's part of the
    document."""

    claim_id: str
    policy_number: str
    peril: str
    sent: dict[str, Any]


def _fitted[Read: BaseModel](model: type[Read], value: Any, code: str) -> Read:
    """``value`` as ``model``, or a ``GraphFailure`` with ``code``."""
    try:
        return model.model_validate(value)
    except ValidationError:
        raise GraphFailure(code) from None


def read_claim(run_input: Mapping[str, Any]) -> Claim:
    """The claim of the run's input (``{"claim": facts}``, the facts of a triage)."""
    claim = _fitted(ClaimInput, run_input.get("claim"), CLAIM_UNFIT)
    return Claim(
        claim_id=claim.claim_id,
        policy_number=claim.policy_number,
        peril=claim.peril,
        sent={
            "peril": claim.peril,
            "claimed_amount": claim.claimed_amount,
            "loss_date": claim.loss_date.isoformat(),
            "reported_on": claim.reported_on.isoformat(),
            "documents_received": claim.documents_received,
        },
    )


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def read_policy(answer: Mapping[str, Any]) -> dict[str, Any]:
    """The policy's part of the document: ``{"found": False}`` for a policy that
    is not found, else its status, dates and amounts."""
    read = _fitted(PolicyAnswer, answer, POLICY_UNFIT)
    if not read.found:
        return {"found": False}
    policy = read.policy
    if policy is None:
        raise GraphFailure(POLICY_UNFIT)
    return {
        "found": True,
        "status": policy.status,
        "start_date": policy.start_date.isoformat(),
        "end_date": policy.end_date.isoformat(),
        "lapsed_on": _iso(policy.lapsed_on),
        "deductible": policy.deductible,
        "limit": policy.limit,
        "sum_insured": policy.sum_insured,
    }


def read_history(answer: Mapping[str, Any], peril: str) -> dict[str, Any]:
    """The history's part of the document: counts and a total, no entry."""
    read = _fitted(HistoryAnswer, answer, HISTORY_UNFIT)
    return {
        "earlier_claims": len(read.entries),
        "earlier_claims_same_peril": sum(e.peril == peril for e in read.entries),
        "earlier_paid_total": sum(e.paid_amount for e in read.entries),
        "truncated": read.truncated,
    }


def document(
    claim: Claim, policy: dict[str, Any], history: dict[str, Any]
) -> dict[str, Any]:
    """The whole document the model is sent, as plain data."""
    return {"claim": claim.sent, "policy": policy, "history": history}
