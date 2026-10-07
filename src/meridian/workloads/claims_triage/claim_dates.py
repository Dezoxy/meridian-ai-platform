"""The claim's dates as the adjuster's page shows them (S070, T-66).

The loss date is the claimant's statement and nothing checks it; the report
date is as submitted with the claim and the API does not check it either (the
claimant's form stamps it, the JSON route keeps the caller's, and nothing stored
says which). The page labels both so, and shows two gaps in days, computed from
those dates: from the loss to the report, and from the report to the day the API
received the claim. Nothing here is stored or sent to the
model; a date that is missing or not an ISO day gives no gap.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

# The insurer's time zone: the report date is the date there, not in UTC (with
# UTC a loss dated today would be after the report for two hours after local
# midnight). The claimant's form stamps the report date in it, and imports it
# from here; this module imports only the standard library.
REPORT_TIME_ZONE = ZoneInfo("Europe/Vienna")

# The labels hold no word the page must not print (a test reads them).
LOSS_DATE_LABEL = "Loss date (as stated in the claim, not checked)"
REPORTED_ON_LABEL = "Reported on (as submitted with the claim, not checked by the API)"
LOSS_TO_REPORT_LABEL = "Days from the loss to the report (from the two dates above)"
REPORT_TO_RECEIVED_LABEL = "Days from the report to the day the API received it"

_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _day(value: object) -> date | None:
    """A stored ISO day (``2026-07-13``), or ``None`` for anything else."""
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _days(count: int) -> str:
    return f"{count} day" if abs(count) == 1 else f"{count} days"


def day_gaps(claim: object, received_at: datetime | None) -> dict[str, str]:
    """The gaps the claim's dates allow, as page rows (label to text), in the
    page's order. A gap whose dates are missing or malformed is left out. The
    second can be negative: the JSON route keeps a report date the caller chose.
    ``received_at`` must be aware (the column is ``timestamptz``, so the
    database driver returns an aware value): ``astimezone`` would read a naive
    one as the server's local time, so a naive one raises ``ValueError``, a
    programming error, for any claim, before a day is counted."""
    if received_at is not None and received_at.utcoffset() is None:
        raise ValueError("received_at must be aware: it has no time zone")
    if not isinstance(claim, Mapping):
        return {}
    lost, reported = _day(claim.get("loss_date")), _day(claim.get("reported_on"))
    rows: dict[str, str] = {}
    if lost is not None and reported is not None:
        rows[LOSS_TO_REPORT_LABEL] = _days((reported - lost).days)
    if reported is not None and received_at is not None:
        received = received_at.astimezone(REPORT_TIME_ZONE).date()
        rows[REPORT_TO_RECEIVED_LABEL] = _days((received - reported).days)
    return rows
