"""The triage graph on the golden set (S014): every golden claim
run through the graph with a stub model that answers as the oracle would, and
the full proposal held against the oracle's.
"""

import pytest
from graphsupport import (
    CLAIMS,
    EXPECTED,
    POLICIES,
    READ_TOOLS,
    StubTools,
    golden_model,
    triage,
)

from meridian.workloads.claims_triage.proposal import TriageProposal

# -- the golden set -----------------------------------------------------------


# S047: the one golden claim whose description says "I was in hospital": the
# model is not asked, so the assessment is unavailable and the claim, which the
# oracle recommends approving, keeps its route to an adjuster but no
# recommendation. The oracle is the insurer's answer with every fact known.
WITHHELD_FROM_THE_MODEL = "CLM-0012"


@pytest.mark.parametrize("claim_id", list(CLAIMS))
def test_the_graph_reproduces_the_oracle_on_every_golden_claim(claim_id: str) -> None:
    expected = EXPECTED[claim_id]
    # Empty for the claim on a policy number no policy has: it cites nothing
    policy = POLICIES.get(CLAIMS[claim_id]["policy_number"], {})
    model = golden_model(claim_id)
    tools = StubTools()
    withheld = claim_id == WITHHELD_FROM_THE_MODEL

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
        None if withheld else expected["recommendation"],
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
    asked = proposal.assessment != "not_needed" and not withheld
    assert proposal.gaps == (("exclusion_assessment",) if withheld else ())
    assert (proposal.assessment, proposal.unavailable_because) == (
        ("unavailable", "special-data") if withheld else (proposal.assessment, None)
    )
    assert len(model.calls) == int(asked)
    assert (proposal.drafted_by is None) == (not asked)
    assert set(tools.names()) <= set(READ_TOOLS)
    assert [w[0] for w in tools.writes] == (
        ["request_approval"] if proposal.route == "adjuster" else []
    )
