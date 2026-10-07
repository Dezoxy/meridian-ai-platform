"""The claimant's upload form posts here: the HTML twin of ``POST
/claims/{claim_id}/files`` (S070, T-38).

The twin runs the JSON route's own handler (``add_upload_routes`` returns it), not
a copy of it: the same ``read_upload``, ``store_file``, permits and checks in the
same order, one pool of permits for both routes. Before the body is read it takes
the pages' cross-site check (the page equivalent of the JSON route's: a refused
post is the claimant's 403 page) and the JSON route's refusal of an encoded path
(the 404 page of a path that is no route).

A success is a redirect (303) to the status page, which lists the claim's files
as kind, size and received time; so is the same file sent again, which is a retry
after a lost answer. A client that went away gets an empty 400 nobody receives,
and a body that did not arrive in time the 408 page. A refusal is the claimant's
error page with the status of the JSON route's answer and its fixed sentence, and
``Retry-After`` where the JSON route sends one. A body over the limit that the
middleware counts while the form is read is the shared 413 page, so a declared
length and a streamed body read the same. The route is out of the OpenAPI
contract, as the pages are, and it exists only when the switch is on, as the JSON
route does. Nothing of the file but its bytes is read: no name, no declared type,
no page that shows either (T-03).
"""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from meridian.workloads.claims_triage.adjuster import ClaimId, require_same_origin
from meridian.workloads.claims_triage.claimant import (
    START_PATH,
    claimant_error,
    to_status,
)
from meridian.workloads.claims_triage.uploads import (
    UploadAbandoned,
    UploadConflict,
    UploadHandler,
    UploadRefusal,
    abandoned_response,
    refuse_encoded_path,
)

TWIN_PATH = START_PATH + "/{claim_id}/files"


def sentence_of(detail: object) -> str:
    """The fixed sentence of a refusal: a text as it is, and the 422's list of
    where-and-what as its messages (each is the route's own text, never anything
    the caller sent)."""
    if isinstance(detail, list):
        return " ".join(
            str(item["msg"])
            for item in detail
            if isinstance(item, dict) and "msg" in item
        )
    return str(detail)


def refusal_page(
    status: int, detail: object, retry_after: int | str | None = None
) -> HTMLResponse:
    page = claimant_error(status, sentence_of(detail))
    if retry_after is not None:
        page.headers["Retry-After"] = str(retry_after)
    return page


def add_claimant_upload_twin(app: FastAPI, upload: UploadHandler) -> None:
    """Add ``POST /claimant/claims/{claim_id}/files``, running ``upload``."""

    @app.post(
        TWIN_PATH,
        include_in_schema=False,
        dependencies=[Depends(require_same_origin), Depends(refuse_encoded_path)],
    )
    async def claimant_upload(claim_id: ClaimId, request: Request) -> Response:
        try:
            result = await upload(claim_id, request)
        except UploadConflict as exc:
            # The same file again is a retry after an answer that was lost (the
            # first post stored it): the form's success, and the status page lists
            # the file. The other two conflicts are refusals. The JSON route keeps
            # its 409, so that it never hands out the stored file's identifier.
            if exc.reason == "duplicate":
                return to_status(claim_id)
            return refusal_page(exc.status_code, exc.detail)
        except HTTPException as exc:
            # FastAPI's, which the route's own refusals are. The body limit raises
            # Starlette's, its parent, which this does not catch: that 413 goes on
            # to the claimant's shared 413 page, as a declared length does.
            retry_after = (exc.headers or {}).get("Retry-After")
            return refusal_page(exc.status_code, exc.detail, retry_after)
        if isinstance(result, UploadRefusal):
            return refusal_page(result.status, result.detail, result.retry_after)
        if isinstance(result, UploadAbandoned):
            return abandoned_response()
        return to_status(claim_id)
