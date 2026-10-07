"""The adjuster's download of a stored file (S070 F4b, T-38, review finding H-2).

``GET`` and ``HEAD`` ``/adjuster/claims/{claim_id}/files/{file_id}`` serve the
bytes a claimant stored, back to whoever reaches the page. They exist only when
``MERIDIAN_CLAIMS_DOWNLOADS`` is ``on`` (off by default; the chart sets it from
``route.downloads.enabled`` and refuses it unless the release is local, so the
download is not exposed beyond the machine before sign-in, S021). Anyone who
reaches the host can read every uploaded file by walking the claim IDs: that is
the exposure of the pages today (T-69), and the reason for the switch.

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
  replaces the policy of every response it sees; this route marks the request
  (``OWN_POLICY_SCOPE_KEY``) when it builds the 200, and only then does the
  middleware keep the policy. Its 404, 500 and 503 are the pages' own and carry
  the pages' policy.

An unknown claim, another tenant's claim, an unknown file and an id that is no
claim ID or file ID are one 404, the page of a claim that is not there. The claim
is found with its tenant first, then the file by claim and identifier.

A GET writes one audit row (``claim.file_downloaded``) in a connection of its
own, committed before the response is built: if the row cannot be written the
answer is the pages' 503 and no byte of the file leaves. The row names the claim
and the tenant, and not the file: the audit event has no field for one, and
``reference`` must stay the claim ID for the row to show in the claim's trail
(``audit.claim_trail``). A HEAD writes no row and sends no body.

Nothing on the server parses, decodes, previews or converts a stored file, and
nothing logs, spans or audits a file's name as sent, its declared type or its
bytes. Malware scanning is designed, not implemented.
"""

import re
import uuid

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
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
    OWN_POLICY_SCOPE_KEY,
    POLICY_HEADER,
    QUEUE_PATH,
    SECURITY_HEADERS,
    render_error,
)
from meridian.workloads.claims_triage.lifecycle import SERVICE_NAME
from meridian.workloads.claims_triage.triaging import claim_database_failure
from meridian.workloads.claims_triage.uploads import (
    UPLOADS_ENABLED_ENV,
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

# What is audited, from closed lists, in the form of the upload's row.
DOWNLOAD_EVENT = "claim.file_downloaded"
DOWNLOAD_OUTCOME = "served"
DOWNLOAD_REASON = "downloaded"

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


def add_download_route(app: FastAPI, *, dsn: str, tenant: str, tracer: Tracer) -> None:
    """Add the download. Called only when the switch is on: with it off the path
    is no route and answers the framework's 404. A plain ``def``: the read is
    synchronous, so it runs in the threadpool."""

    def not_found() -> Response:
        return render_error(404, NO_SUCH_CLAIM_DETAIL)

    # Not in the OpenAPI document, like the pages. HEAD is its own method: the
    # framework does not add it for a GET.
    @app.api_route(
        DOWNLOAD_PATH,
        methods=["GET", "HEAD"],
        include_in_schema=False,
        dependencies=[Depends(refuse_encoded_path)],
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
        identifier = uuid.UUID(file_id)
        with start_span(tracer, "claims.adjuster.download") as span:
            set_span_attributes(
                span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
            )
            try:
                with connect(dsn, SERVICE_NAME) as conn:
                    found = file_content(conn, tenant, claim_id, identifier)
                if found is None:
                    return not_found()
                if found.media_type not in EXTENSIONS:
                    # The table's CHECK keeps a stored type in the list; a type
                    # outside it is a table someone changed, and is not served.
                    return render_error(500, INTERNAL_ERROR)
                if request.method == "GET":
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
                return render_error(503, AUDIT_UNAVAILABLE)
            except psycopg.Error as exc:
                mark_error(span, exc)
                return render_error(*claim_database_failure(exc, claim_id))
        headers = download_headers(
            claim_id, identifier, found.media_type, len(found.content)
        )
        # The middleware of the pages keeps this response's policy.
        request.scope[OWN_POLICY_SCOPE_KEY] = True
        # A HEAD sends the headers of the GET, the length included, and no body:
        # an explicit Content-Length is not replaced by the empty body's.
        body = b"" if request.method == "HEAD" else found.content
        return Response(content=body, headers=headers)
