"""The gateway's metrics: tokens, cost and calls (S011).

Three counters on the ``meridian.gateway`` meter. Tokens and cost are measured
usage: only an attempt the provider answered adds to them, with the provider's
own counts and the cost of those counts. What the ledger charged for a kept
reservation is in the ledger only. Calls count each request that reached the
handler once, when it ends, by outcome (a failed one names the kind of its last
attempt as the reason).

Every label is a registry identifier or a fixed word: tenant and agent are
attached only after routing passed, so a header's value never becomes a label
(T-49). ``metric_attributes`` refuses any key off the allowlist.
"""

from typing import Literal

from opentelemetry.sdk.metrics import MeterProvider

from meridian.platform.common.metrics import metric_attributes
from meridian.platform.gateway.budget import MICRO
from meridian.platform.registry.models import Deployment

METER_NAME = "meridian.gateway"


class CallRecord:
    """Counts one request once. The first ``end`` counts; later ones change
    nothing, so a request that is refused and then fails to audit its refusal
    is a refusal and not also a failure."""

    def __init__(self, meters: "GatewayMeters") -> None:
        self._meters = meters
        self._tenant: str | None = None
        self._agent: str | None = None
        self._ended = False

    def known(self, tenant: str, agent: str) -> None:
        """Routing passed: both are registry IDs now, safe as labels."""
        self._tenant, self._agent = tenant, agent

    def end(
        self,
        outcome: Literal["completed", "failed", "refused"],
        reason: str | None = None,
    ) -> None:
        if self._ended:
            return
        self._ended = True
        attributes = {"meridian.outcome": outcome}
        if reason is not None:
            attributes["meridian.reason"] = reason
        if self._tenant is not None and self._agent is not None:
            attributes["meridian.tenant"] = self._tenant
            attributes["meridian.agent"] = self._agent
        self._meters.add_call(attributes)


class GatewayMeters:
    def __init__(self, meter_provider: MeterProvider) -> None:
        meter = meter_provider.get_meter(METER_NAME)
        self._tokens = meter.create_counter(
            "meridian.gateway.tokens",
            unit="{token}",
            description="Tokens the providers counted for settled attempts.",
        )
        self._cost = meter.create_counter(
            "meridian.gateway.cost",
            unit="EUR",
            description="Cost of settled attempts at the registry's prices.",
        )
        self._calls = meter.create_counter(
            "meridian.gateway.calls",
            unit="{call}",
            description=(
                "Requests that reached the handler of POST /v1/chat, once each, "
                "by outcome."
            ),
        )

    def settled(
        self,
        tenant: str,
        agent: str,
        deployment: Deployment,
        input_tokens: int,
        output_tokens: int,
        charged_micro_eur: int,
    ) -> None:
        """An attempt the provider answered and the ledger settled."""
        labels = {
            "meridian.tenant": tenant,
            "meridian.agent": agent,
            "meridian.provider": deployment.provider,
            "gen_ai.request.model": deployment.model,
        }
        for token_type, count in (("input", input_tokens), ("output", output_tokens)):
            self._tokens.add(
                count, metric_attributes(labels | {"gen_ai.token.type": token_type})
            )
        self._cost.add(charged_micro_eur / MICRO, metric_attributes(labels))

    def call_record(self) -> CallRecord:
        return CallRecord(self)

    def add_call(self, attributes: dict[str, str]) -> None:
        self._calls.add(1, metric_attributes(attributes))
