"""The adjuster's download of a stored file (S070 F4b and F4d, T-38, H-2, H-A).

``GET`` and ``HEAD`` ``/adjuster/claims/{claim_id}/files/{file_id}`` serve the
bytes a claimant stored, back to whoever reaches the page. They exist only when
``MERIDIAN_CLAIMS_DOWNLOADS`` is ``on`` (off by default; the chart sets it from
``route.downloads.enabled`` and refuses it unless the route's host name is a
``.localhost`` one, which checks a string and not who can reach the edge: see
``route.yaml``). Anyone who reaches the host can read every uploaded file by
walking the claim IDs: that is the exposure of the pages today (T-69), and the
reason for the switch.

What the response is, and why:

- the stored media type, from the closed list, as ``Content-Type``; a stored
  type outside the list is a 500 and no byte;
- an attachment under a fixed ASCII name built from the claim, the file and an
  extension of the stored type: nothing the uploader sent (the file name is not
  stored), no ``filename*``;
- ``nosniff``, ``no-store``, a same-origin resource policy, ``DENY`` framing and
  an exact ``Content-Length``;
- a ``Content-Security-Policy`` of its own, ``default-src 'none';
  frame-ancestors 'none'; sandbox``, with no ``allow-`` token, so a file that is
  two things at once cannot run in the pages' origin. The pages' middleware
  replaces the policy of every response it sees; this route builds the 200 and
  then marks the request (``OWN_POLICY_SCOPE_KEY``), and only then does the
  middleware keep the policy. Its 404, 429, 500 and 503 are the pages' own and
  carry the pages' policy.

An unknown claim, another tenant's claim, an unknown file and an id that is no
claim ID or file ID are one 404, the page of a claim that is not there. The claim
is found with its tenant first, then the file by claim and identifier.

The route's own brakes, none of which depends on the edge (the principle of the
upload route's: the app's brakes must not depend on which edge route served the
path). In this order, each before the file is read and each writing no audit row:

1. a request from another site is the pages' 403 (a click on the claim page
   sends ``Sec-Fetch-Site: same-origin`` and a typed address ``none``, which
   pass), as for the pages' posts;
2. the store-wide rate: at most ``DOWNLOAD_RATE_PER_MINUTE`` requests in the last
   minute, GET and HEAD alike, then 429 with ``Retry-After``. The window lives
   in the process: a second replica has a window of its own, so N replicas allow
   N times the rate (the claims API runs one). The rows it bounds are written one
   a GET, in an audit table that nothing deletes from;
3. at most ``MAX_CONCURRENT_DOWNLOADS`` reads and audit writes at once, in a pool
   of their own, then 503 with its own sentence and ``Retry-After``.

A GET writes one audit row (``claim.file_downloaded``) in a connection of its
own, committed before the response is built: if the row cannot be written the
answer is the pages' 503 and no byte of the file leaves. The row names the claim
and the tenant, and not the file: the audit event has no field for one, and
``reference`` must stay the claim ID for the row to show in the claim's trail
(``audit.claim_trail``), where the adjuster's page counts these rows in one line
and does not list them. A HEAD sends the type and size from the file's row, never
its bytes, writes no row and sends no body.

Nothing on the server parses, decodes, previews or converts a stored file, and
nothing logs, spans or audits a file's name as sent, its declared type or its
bytes. The file's identifier is a capability: it is kept out of this route's own
span and replaced in the framework's server span, whose recorded URL would hold
it (the uvicorn access line still does). Malware scanning is designed, not
implemented.
"""

import re
import threading
import time
import uuid
from collections import deque
from collections.abc import MutableMapping, Sequence
from functools import partial

import psycopg
from fastapi import Depends, FastAPI, Request, params
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.trace import Tracer

from meridian.platform.common.audit import AuditEvent, AuditUnavailable, write_audit
from meridian.platform.common.db import connect
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import AUDIT_UNAVAILABLE, INTERNAL_ERROR
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.workloads.claims_triage.adjuster import (
    CLAIM_ID_PATTERN,
    NO_SUCH_CLAIM_DETAIL,
    QUEUE_PATH,
    render_error,
)
from meridian.workloads.claims_triage.claim_files import (
    DOWNLOAD_EVENT,
    FileHead,
    file_head,
)
from meridian.workloads.claims_triage.lifecycle import SERVICE_NAME
from meridian.workloads.claims_triage.page_security import (
    OWN_POLICY_SCOPE_KEY,
    POLICY_HEADER,
    SECURITY_HEADERS,
    require_same_origin,
)
from meridian.workloads.claims_triage.triaging import claim_database_failure
from meridian.workloads.claims_triage.uploads import (
    BUSY_RETRY_SECONDS,
    UPLOADS_ENABLED_ENV,
    FileContent,
    file_content,
    refuse_encoded_path,
)

DOWNLOADS_ENABLED_ENV = "MERIDIAN_CLAIMS_DOWNLOADS"
DOWNLOAD_PATH = QUEUE_PATH + "/{claim_id}/files/{file_id}"
# A file's identifier as the app writes it (``str`` of a version 4 UUID): lower
# case, hyphenated, nothing else. The edge's route for the download matches the
# same shape; a test holds the two expressions equal.
FILE_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# The download's own policy. ``sandbox`` with no ``allow-`` token: no script, no
# form, no popup, and an opaque origin, were a browser ever to render the bytes.
DOWNLOAD_POLICY = "default-src 'none'; frame-ancestors 'none'; sandbox"
# The extension of each stored type: the closed list of ``uploads.MediaType``.
EXTENSIONS = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}

# What is audited, from closed lists, in the form of the upload's row (the event's
# name is ``claim_files.DOWNLOAD_EVENT``, which the adjuster's page reads too).
DOWNLOAD_OUTCOME = "served"
DOWNLOAD_REASON = "downloaded"

# The app's own rate limit on downloads: the requests (GET and HEAD) of the last
# minute in the whole store, no caller having an identity before S021. Each GET
# reads up to 1 MiB, opens two connections and writes an audit row, and the file
# identifiers are listed on the adjuster's page for anyone. 30 a minute is the
# edge's allowance for the download rule (``route.downloads.rateLimit``) and one
# adjuster opening the files of several claims; at that rate a claim's page, which
# counts these rows in one line, is not displaced, and the audit table grows by at
# most 43,200 rows a day for a process. Past it: 429, ``Retry-After`` one window.
DOWNLOAD_RATE_PER_MINUTE = 30
DOWNLOAD_RATE_WINDOW_SECONDS = 60
# How many downloads read and audit at once, in a pool of their own (the
# uploads' is another): a read holds one megabyte and two connections, the
# thread pool is 40 and shared with every other route, and four is as many as one
# adjuster could use. Past it: 503 at once, ``Retry-After`` as the uploads' busy.
MAX_CONCURRENT_DOWNLOADS = 4
RATE_DETAIL = "too many downloads in the last minute; try again shortly"
BUSY_DETAIL = "downloads are busy: too many at once; try again shortly"
# What a server span's URL holds in place of the file's identifier.
REDACTED_FILE_ID = "{file_id}"

NEEDS_UPLOADS = (
    f"{DOWNLOADS_ENABLED_ENV} is on, but {UPLOADS_ENABLED_ENV} is not: there is "
    "nothing to download without the uploads that store it"
)


def downloads_enabled_of(raw: str | None) -> bool:
    """The switch from the variable's value, as the uploads switch is read: off
    when it is unset, else exactly ``on`` or ``off`` (the chart renders ``on``);
    any other word, a typo among them, stops the start rather than turning a door
    that serves stored files on or off by guess."""
    if raw is None or raw == "off":
        return False
    if raw == "on":
        return True
    raise SettingsError(f"{DOWNLOADS_ENABLED_ENV} must be on or off")


def check_uploads_stand_beside(*, downloads: bool, uploads: bool) -> None:
    """Downloads on with uploads off is refused at start, with a fixed sentence."""
    if downloads and not uploads:
        raise SettingsError(NEEDS_UPLOADS)


def now() -> float:
    """The clock of the rate window (a function, so that a test can move it)."""
    return time.monotonic()


class RateWindow:
    """At most ``limit`` takes in any ``seconds``, counted in this process: a
    deque of the moments taken, under a lock (the route runs in the thread pool).
    A refused take is not counted."""

    def __init__(self, limit: int, seconds: int) -> None:
        self._limit = limit
        self._seconds = seconds
        self._taken: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self) -> bool:
        moment = now()
        with self._lock:
            while self._taken and moment - self._taken[0] >= self._seconds:
                self._taken.popleft()
            if len(self._taken) >= self._limit:
                return False
            self._taken.append(moment)
            return True


def download_headers(
    claim_id: str, file_id: uuid.UUID, media_type: str, size: int
) -> dict[str, str]:
    """The headers of a served file, all of them from stored values and fixed
    text. The pages' headers are repeated here, and the policy is the download's
    own."""
    pages = {k: v for k, v in SECURITY_HEADERS.items() if k != POLICY_HEADER}
    return {
        **pages,
        POLICY_HEADER: DOWNLOAD_POLICY,
        "Content-Type": media_type,
        "Content-Disposition": (
            f'attachment; filename="{claim_id}-{file_id}.{EXTENSIONS[media_type]}"'
        ),
        "Content-Length": str(size),
    }


def refused(
    status: int, detail: str, retry_after: int, *, signin: bool = False
) -> Response:
    """The pages' error page for a brake, with the wait."""
    response = render_error(status, detail, signin=signin)
    response.headers["Retry-After"] = str(retry_after)
    return response


def keep_file_id_out_of_server_span(file_id: str) -> None:
    """Replace the file's identifier in the attributes of the framework's server
    span, which records the request's URL and target and so would carry it out of
    the process (as the query is dropped from them, ``drop_query_from_span``). The
    span is still recording while its route runs."""
    server = trace.get_current_span()
    if not server.is_recording():
        return
    for key, value in list((getattr(server, "attributes", None) or {}).items()):
        if isinstance(value, str) and file_id in value:
            server.set_attribute(key, value.replace(file_id, REDACTED_FILE_ID))


def add_download_route(
    app: FastAPI,
    *,
    dsn: str,
    tenant: str,
    tracer: Tracer,
    guards: Sequence[params.Depends] = (),
) -> None:
    """Add the download. Called only when the switch is on: with it off the path
    is no route and answers the framework's 404. A plain ``def``: the read is
    synchronous, so it runs in the threadpool. ``guards`` are the sign-in's
    dependencies (S021, Y4), after the two checks of the request that need no
    identity, so a post from another site is a 403 before it is a 401; empty by
    default."""
    window = RateWindow(DOWNLOAD_RATE_PER_MINUTE, DOWNLOAD_RATE_WINDOW_SECONDS)
    permits = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

    # The pages say there is no sign-in unless one is on, which the guards show.
    signin = bool(guards)
    error_page = partial(render_error, signin=signin)

    def not_found() -> Response:
        return error_page(404, NO_SUCH_CLAIM_DETAIL)

    def serve(
        claim_id: str, identifier: uuid.UUID, head: bool, scope: MutableMapping
    ) -> Response:
        """Read the file (its type and size only, for a HEAD), write the GET's
        row, and build the response; an answer of the pages' for anything else."""
        found: FileContent | FileHead | None
        with start_span(tracer, "claims.adjuster.download") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                with connect(dsn, SERVICE_NAME) as conn:
                    read = file_head if head else file_content
                    found = read(conn, tenant, claim_id, identifier)
                if found is None:
                    return not_found()
                if found.media_type not in EXTENSIONS:
                    # The table's CHECK keeps a stored type in the list; a type
                    # outside it is a table someone changed, and is not served.
                    return error_page(500, INTERNAL_ERROR)
                if not head:
                    # Written and committed before the response exists: no row,
                    # no bytes.
                    write_audit(
                        dsn,
                        AuditEvent(
                            service=SERVICE_NAME,
                            event=DOWNLOAD_EVENT,
                            outcome=DOWNLOAD_OUTCOME,
                            reason=DOWNLOAD_REASON,
                            tenant=tenant,
                            reference=claim_id,
                        ),
                    )
            except AuditUnavailable as exc:
                mark_error(span, exc)
                return error_page(503, AUDIT_UNAVAILABLE)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return error_page(*claim_database_failure(exc, claim_id))
        if isinstance(found, FileHead):
            size, body = found.size_bytes, b""
        else:
            size, body = len(found.content), found.content
        # A HEAD sends the headers of the GET, the length included, and no body:
        # an explicit Content-Length is not replaced by the empty body's.
        headers = download_headers(claim_id, identifier, found.media_type, size)
        response = Response(content=body, headers=headers)
        # Marked only once the response exists (were it not built, the 500 would
        # carry the download's policy or none): the middleware of the pages then
        # keeps this response's policy.
        scope[OWN_POLICY_SCOPE_KEY] = True
        return response

    # Not in the OpenAPI document, like the pages. HEAD is its own method: the
    # framework does not add it for a GET.
    @app.api_route(
        DOWNLOAD_PATH,
        methods=["GET", "HEAD"],
        include_in_schema=False,
        dependencies=[
            Depends(refuse_encoded_path),
            Depends(require_same_origin),
            *guards,
        ],
    )
    def download_file(claim_id: str, file_id: str, request: Request) -> Response:
        # Both ids are parsed here and not by the framework, whose answer to a
        # malformed one is a 422 that would tell a caller the shape is wrong
        # where an unknown id is a 404: one answer for all of them.
        if not (
            re.fullmatch(CLAIM_ID_PATTERN, claim_id)
            and re.fullmatch(FILE_ID_PATTERN, file_id)
        ):
            return not_found()
        keep_file_id_out_of_server_span(file_id)
        if not window.take():
            return refused(
                429, RATE_DETAIL, DOWNLOAD_RATE_WINDOW_SECONDS, signin=signin
            )
        if not permits.acquire(blocking=False):
            return refused(503, BUSY_DETAIL, BUSY_RETRY_SECONDS, signin=signin)
        try:
            return serve(
                claim_id, uuid.UUID(file_id), request.method == "HEAD", request.scope
            )
        finally:
            permits.release()
