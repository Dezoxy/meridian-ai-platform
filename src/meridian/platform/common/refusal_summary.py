"""The summary rows of refusal floods, one writer for every service (T-49).

A refusal flood leaves one ``refused`` row per window (``RefusalAuditThrottle``);
the refusals it suppressed in its last window are carried by no later row. A
service writes those as one ``suppressed`` row per throttle key once the flood
has been quiet for two windows, on its next request or call and when it closes.
The row names the event the service's refusal rows already use, the key in
``reason`` (cut to the column's length) and the count in ``suppressed``, and
the tenant when the key has one. It names no agent, run, call or purpose: it
stands for refusals and is not itself one.

What differs between services is passed in: the writer of an audit row, which
holds the service's name and its database (so a test can stand in for it), and
the event name. A service whose throttle mixes two events keeps two throttles.
"""

import logging
from collections.abc import Callable

from meridian.platform.common.throttle import RefusalAuditThrottle

logger = logging.getLogger(__name__)

SUPPRESSED_OUTCOME = "suppressed"
# The audit table's text columns hold at most 128 characters (a check in
# migrations 0001 and 0002): a key longer than that is cut, so the row is
# written and the flood is counted.
REASON_MAX_LENGTH = 128

# ``audit(event, outcome, **fields)`` writes one audit row and raises when it
# cannot.
SummaryAudit = Callable[..., None]


def write_ended_summaries(
    throttle: RefusalAuditThrottle,
    audit: SummaryAudit,
    event: str,
    *,
    everything: bool = False,
) -> None:
    """Write one summary row for each count the throttle hands out: the flood
    has been quiet for two windows, or ``everything`` at shutdown. It never
    raises an ``Exception``: the request it rides on is not the one refused. A
    write that fails puts back that count and every count not yet written, so a
    later call tries again after two windows, and logs the class of the
    exception and nothing else (T-03, T-56); a ``BaseException`` puts them back
    and is re-raised.

    A summary is written at least once, not exactly once: a write that commits
    and then raises (the acknowledgement of the commit is lost) puts its count
    back, and a later call writes it again."""
    ended = throttle.take_ended(everything=everything)
    for written, (tenant, reason, count) in enumerate(ended):
        try:
            audit(
                event,
                SUPPRESSED_OUTCOME,
                tenant=tenant,
                reason=reason[:REASON_MAX_LENGTH],
                suppressed=count,
            )
        except Exception as error:
            _restore(throttle, ended[written:])
            logger.warning(
                "the summary of suppressed refusals could not be written (%s)",
                type(error).__name__,
            )
            return
        except BaseException:
            _restore(throttle, ended[written:])
            raise


def _restore(
    throttle: RefusalAuditThrottle, counts: list[tuple[str | None, str, int]]
) -> None:
    for tenant, reason, count in counts:
        throttle.restore(tenant, reason, count)
