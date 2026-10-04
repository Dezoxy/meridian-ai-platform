"""The audit rows of the gateway's refusals, and the answers of a tenant limit
(T-49, S058).

A refusal row (``refused``) stands for one refusal and for the refusals
suppressed since the row before it, whose count is in ``suppressed``. The
refusals suppressed in a flood's last window are carried by no later row, so a
summary row (``suppressed``) stands for those and for nothing else: it names the
tenant and the reason and the count, and no agent, run, call or purpose, because
it counts refusals of both purposes and is not itself a refusal. It is written
once the flood has been quiet for two windows, with the next request of any
tenant, or when the app closes. A summary that cannot be written is tried again
after two windows, not with every request. All the refusals of a key are
therefore the ``refused`` rows plus the ``suppressed`` counts of both kinds of
row. The key of a request whose tenant the registry does not hold is the reason
alone, so its summary row has no tenant, while its ``refused`` rows carry the
name the caller sent: the sum holds per key, and for that key by reason, not by
the ``tenant`` column.
"""

import logging

from meridian.platform.common.http import HTTP_PAYLOAD_TOO_LARGE
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.gateway.budget import BudgetRefusalReason, Caller
from meridian.platform.gateway.ratelimit import RateRefusalReason
from meridian.platform.gateway.walk import AuditWriter, caller_fields

logger = logging.getLogger(__name__)

MODEL_CALL_EVENT = "model.call"
SUPPRESSED_OUTCOME = "suppressed"

HTTP_TOO_MANY_REQUESTS = 429
# One fixed text per refusal for a tenant limit; the reason is in the audit row.
TENANT_RATE_LIMIT_REACHED = "the tenant's rate limit is reached"
TENANT_BUDGET_USED_UP = "the tenant's budget is used up"
TENANT_REQUEST_TOO_LARGE = "the request is larger than the tenant's token limit"
LimitRefusalReason = RateRefusalReason | BudgetRefusalReason
LIMIT_ANSWERS: dict[LimitRefusalReason, tuple[int, str]] = {
    "tenant-request-rate": (HTTP_TOO_MANY_REQUESTS, TENANT_RATE_LIMIT_REACHED),
    "tenant-token-rate": (HTTP_TOO_MANY_REQUESTS, TENANT_RATE_LIMIT_REACHED),
    "tenant-token-budget": (HTTP_TOO_MANY_REQUESTS, TENANT_BUDGET_USED_UP),
    "tenant-cost-budget": (HTTP_TOO_MANY_REQUESTS, TENANT_BUDGET_USED_UP),
    "tenant-request-too-large": (HTTP_PAYLOAD_TOO_LARGE, TENANT_REQUEST_TOO_LARGE),
}


class RefusalAudit:
    """Writes the refusal rows of one gateway and the summaries of its floods,
    on one throttle."""

    def __init__(self, throttle: RefusalAuditThrottle, audit: AuditWriter) -> None:
        self._throttle = throttle
        self._audit = audit

    def record(
        self,
        caller: Caller,
        throttle_tenant: str | None,
        reason: str,
        facts: dict[str, str | None],
    ) -> None:
        """Write the row of a refusal when the throttle says it is due, with
        the count of the refusals it stands in for (T-49). The window starts
        when the refusal is due, so overlapping refusals leave one row, and a
        write that fails releases it and loses nothing: the next refusal is due
        and counts this one. The key is the tenant and the reason, not the
        purpose: the row's ``purpose`` is that of the call that wrote it, and
        its ``suppressed`` counts the refusals of both purposes."""
        carried = self._throttle.due(throttle_tenant, reason)
        if carried is None:
            return
        try:
            self._audit(
                MODEL_CALL_EVENT,
                "refused",
                **caller_fields(caller),
                **facts,
                reason=reason,
                suppressed=carried,
            )
        except BaseException:
            self._throttle.release(throttle_tenant, reason, carried)
            raise

    def write_ended(self, *, everything: bool = False) -> None:
        """Write one summary row for each count the throttle hands out: the
        flood has been quiet for two windows, or ``everything`` at shutdown.
        It never raises an ``Exception``: the request it rides on is not the
        one refused. A write that fails puts back that count and every count
        not yet written, so the next request tries again, and logs the class of
        the exception and nothing else (T-03, T-56); a ``BaseException`` puts
        them back and is re-raised."""
        ended = self._throttle.take_ended(everything=everything)
        for written, (tenant, reason, count) in enumerate(ended):
            try:
                self._audit(
                    MODEL_CALL_EVENT,
                    SUPPRESSED_OUTCOME,
                    tenant=tenant,
                    reason=reason,
                    suppressed=count,
                )
            except Exception as error:
                self._restore(ended[written:])
                logger.warning(
                    "the summary of suppressed refusals could not be written (%s)",
                    type(error).__name__,
                )
                return
            except BaseException:
                self._restore(ended[written:])
                raise

    def _restore(self, counts: list[tuple[str | None, str, int]]) -> None:
        for tenant, reason, count in counts:
            self._throttle.restore(tenant, reason, count)
