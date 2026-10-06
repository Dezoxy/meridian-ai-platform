"""The Claims Triage App's metrics: every triage, and the assessment's outcome (S064).

Two counters on the ``meridian.claims`` meter, both counted by
``run_taken_triage``, the one place every triage the Claims API takes passes:

- ``meridian.claims.triages`` (``meridian_claims_triages_total``) counts each
  triage once, however it ended, by ``meridian.outcome``: ``stored`` (the
  proposal is committed and the claim has moved on), ``failed`` with a
  ``meridian.reason`` word of ``TriageFailure``, or ``taken-over`` (another
  request took the triage, and this one's proposal was dropped: the 409). It is
  the counter of the Claims API to runtime hop: the runtime's own
  ``meridian.runtime.runs`` does not see a call that never reached it, and calls
  a run ``completed`` that this app then could not use.
- ``meridian.claims.assessments`` (``meridian_claims_assessments_total``) counts
  a stored proposal once, by the proposal's own ``assessment`` word and, when the
  assessment is unavailable, the ``unavailable_because`` word. It moves with the
  ``stored`` triage and with no other.

Both live here and not in the graph, so that the runtime's contract with a
workload's graph does not grow a meter (ADR 2).

Every label is a closed set the code defines (T-03, T-49): the assessment's
outcome is one of the words of ``AssessmentStatus`` and its reason one of
``UnavailableBecause`` (``Literal`` types the proposal model validates), and a
triage's outcome and failure reason are the ``Literal`` types below, so a word
nothing defined never reaches a label. The tenant is the app's own setting.
Nothing of the claim is a label: no ID, no peril, no route, no clause.

A method that records is wrapped by ``counted_safely``: a counter that raises is
one WARNING, never a triage that fails after its proposal was stored.
"""

from typing import Literal

from opentelemetry.sdk.metrics import MeterProvider

from meridian.platform.common.metrics import counted_safely, metric_attributes
from meridian.workloads.claims_triage.proposal import TriageProposal

METER_NAME = "meridian.claims"
TRIAGES = "meridian.claims.triages"

TriageOutcome = Literal["stored", "failed", "taken-over"]
# Why a triage failed. ``runtime-unreachable``: the call never got an answer
# (connection refused or dropped, TLS, a name that does not resolve);
# ``runtime-timeout``: this app stopped waiting; ``runtime-failed``: the runtime
# answered with an error status, a 504 of its own included (the hop worked);
# ``bad-output``: it answered 200 with something that is not a run, a run that
# is not a proposal, or a proposal its status does not fit; ``proposal-lost``:
# the database refused the write; ``unexpected``: an exception no branch of
# ``run_taken_triage`` expected (a bug), counted and raised.
TriageFailure = Literal[
    "runtime-unreachable",
    "runtime-timeout",
    "runtime-failed",
    "bad-output",
    "proposal-lost",
    "unexpected",
]


class ClaimsMeters:
    def __init__(self, meter_provider: MeterProvider, tenant: str) -> None:
        meter = meter_provider.get_meter(METER_NAME)
        self._tenant = tenant
        self._assessments = meter.create_counter(
            "meridian.claims.assessments",
            unit="{proposal}",
            description=(
                "Triage proposals stored, once each, by the assessment's outcome "
                "and, when it is unavailable, why."
            ),
        )
        self._triages = meter.create_counter(
            TRIAGES,
            unit="{triage}",
            description=(
                "Triages the Claims API took, once each, by how they ended: "
                "stored, failed (and why) or taken over by another request."
            ),
        )

    @counted_safely
    def proposal_stored(self, proposal: TriageProposal) -> None:
        """Count a proposal the Claims API has stored: its triage as ``stored``
        and its assessment."""
        attributes = {
            "meridian.tenant": self._tenant,
            "meridian.outcome": proposal.assessment,
        }
        if proposal.unavailable_because is not None:
            attributes["meridian.reason"] = proposal.unavailable_because
        self._assessments.add(1, metric_attributes(attributes))
        self._count_triage("stored")

    @counted_safely
    def triage_failed(self, reason: TriageFailure) -> None:
        """Count a triage that stored no proposal and was not taken over."""
        self._count_triage("failed", reason)

    @counted_safely
    def triage_taken_over(self) -> None:
        """Count a triage another request took over."""
        self._count_triage("taken-over")

    def _count_triage(
        self, outcome: TriageOutcome, reason: TriageFailure | None = None
    ) -> None:
        attributes = {"meridian.tenant": self._tenant, "meridian.outcome": outcome}
        if reason is not None:
            attributes["meridian.reason"] = reason
        self._triages.add(1, metric_attributes(attributes))
