"""The Claims Triage App's metric: the assessment's outcome, by reason word (S064).

One counter on the ``meridian.claims`` meter, which reaches Prometheus as
``meridian_claims_assessments_total``. ``meridian.claims.assessments`` counts a
triage's proposal once, when the Claims API has stored it (the row is committed
and the claim has moved on), by the proposal's own ``assessment`` word and, when
the assessment is unavailable, the ``unavailable_because`` word. A triage that
stores no proposal counts nothing here: a run that failed, an answer that is not
a proposal, a proposal the database refused and a triage another request took
over. The runtime counts a failed run itself (``meridian.runtime.runs``). The
counter lives here and not in the graph so that the runtime's contract with a
workload's graph does not grow a meter (ADR 2).

Every label is a closed set the code defines (T-03, T-49): the outcome is one of
the four words of ``AssessmentStatus``, the reason one of the words of
``UnavailableBecause``, both ``Literal`` types the proposal model validates, so a
word the model did not define never reaches a label. The tenant is the app's own
setting. Nothing of the claim is a label: no ID, no peril, no route, no clause.
"""

from opentelemetry.sdk.metrics import MeterProvider

from meridian.platform.common.metrics import metric_attributes
from meridian.workloads.claims_triage.proposal import TriageProposal

METER_NAME = "meridian.claims"


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

    def proposal_stored(self, proposal: TriageProposal) -> None:
        """Count a proposal the Claims API has stored."""
        attributes = {
            "meridian.tenant": self._tenant,
            "meridian.outcome": proposal.assessment,
        }
        if proposal.unavailable_because is not None:
            attributes["meridian.reason"] = proposal.unavailable_because
        self._assessments.add(1, metric_attributes(attributes))
