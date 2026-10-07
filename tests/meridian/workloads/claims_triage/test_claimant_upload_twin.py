"""The HTML twin of the upload route, ``POST /claimant/claims/{claim_id}/files``
(S070 F4a): what needs no database. Where it exists, its body limit, the checks
that come before the body is read (a post from another site, an encoded path),
its place in the OpenAPI contract (none), the pages' headers on its answers, and
the one pool of permits it shares with the JSON route. The tests that store a
file are in ``test_claimant_upload_pages.py``.
"""

import asyncio
import html
import re
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from servicesupport import multipart_body
from workloads.claims_triage.test_adjuster_pages import SECURITY_HEADERS
from workloads.claims_triage.test_claim_uploads_brakes import (
    PATH as JSON_PATH,
)
from workloads.claims_triage.test_claim_uploads_brakes import (
    claims_app,
    client_of,
    one_permit,
    quick_post,
    stalled_post,
    valid_form,
)

from meridian.platform.common.http import SMALL_BODY_LIMIT_BYTES
from meridian.workloads.claims_triage import claimant_uploads
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.uploads import (
    CONTENT_TYPE_PROBLEM,
    FORM_PROBLEM,
    KIND_PROBLEM,
    SATURATED_DETAIL,
    UPLOAD_BODY_LIMIT_BYTES,
)

TWIN_PATH = "/claimant/claims/{claim_id}/files"
TWIN = "/claimant/claims/CLM-0001/files"
DOCUMENTS = "/claimant/claims/CLM-0001/documents"
NO_DATABASE = "the database is unavailable"
TOO_LARGE_TEXT = "What you sent is too large."
NO_SUCH_PAGE = "There is no such page."
CROSS_SITE = "the request came from another site"
MULTIPART = {"Content-Type": "multipart/form-data; boundary=b"}
ERROR_PAGE = re.compile(r'<h1>(\d+)</h1>\s*<p role="alert">(.*?)</p>', re.DOTALL)


def switched(uploads: bool) -> FastAPI:
    return create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url="postgresql://claims_api@db.invalid/meridian",
            uploads_enabled=uploads,
        )
    )


def twin_client(*, uploads: bool = True) -> TestClient:
    return TestClient(switched(uploads), raise_server_exceptions=False)


def routes_of(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
        if method not in ("HEAD", "OPTIONS")
    }


def refusal_of(response: httpx.Response) -> tuple[int, str]:
    """The status and the sentence of the claimant's error page: its heading and
    the one alert paragraph, unescaped as a person reads them."""
    found = ERROR_PAGE.search(response.text)
    assert found, "the answer is not the claimant's error page"
    assert "Back to your claims" in response.text
    assert "Back to the queue" not in response.text
    assert response.headers["content-type"].startswith("text/html")
    return int(found.group(1)), html.unescape(found.group(2))


def has_the_pages_headers(response: httpx.Response) -> bool:
    return all(response.headers.get(k) == v for k, v in SECURITY_HEADERS.items())


def chunked(body: bytes, size: int = 32 * 1024) -> Iterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


def body_of_size(total: int) -> bytes:
    """A form of exactly ``total`` bytes: one file part and no kind (so a post
    that gets past the limit is a 422), padded in the file's bytes."""
    probe = multipart_body([("file", "f.pdf", "application/pdf", b"")])[0]
    return multipart_body(
        [("file", "f.pdf", "application/pdf", b"x" * (total - len(probe)))]
    )[0]


# ── where the twin exists ───────────────────────────────────────────────────
def test_the_twin_is_a_route_only_with_the_switch_on() -> None:
    assert ("POST", TWIN_PATH) in routes_of(switched(True))
    assert ("POST", TWIN_PATH) not in routes_of(switched(False))


def test_the_twin_is_the_path_the_charts_second_route_matches() -> None:
    assert claimant_uploads.TWIN_PATH == TWIN_PATH


def test_the_twin_is_not_in_the_openapi_contract() -> None:
    schema = switched(True).openapi()

    assert not [p for p in schema["paths"] if p.startswith("/claimant")]
    assert JSON_PATH.replace("CLM-0001", "{claim_id}") in schema["paths"]


def test_with_the_switch_off_the_twin_is_a_page_that_does_not_exist() -> None:
    response = twin_client(uploads=False).post(
        TWIN, content=body_of_size(1024), headers=MULTIPART
    )

    assert refusal_of(response) == (404, NO_SUCH_PAGE)


def test_with_the_switch_off_the_twins_path_keeps_the_64_kib_limit() -> None:
    response = twin_client(uploads=False).post(
        TWIN, content=body_of_size(SMALL_BODY_LIMIT_BYTES + 1), headers=MULTIPART
    )

    assert refusal_of(response) == (413, TOO_LARGE_TEXT)


# ── the body limit: the upload limit on the twin, 64 KiB on the rest ────────
def test_a_declared_length_one_byte_over_the_limit_is_the_413_page() -> None:
    response = twin_client().post(
        TWIN, content=b"x" * (UPLOAD_BODY_LIMIT_BYTES + 1), headers=MULTIPART
    )

    assert refusal_of(response) == (413, TOO_LARGE_TEXT)
    assert has_the_pages_headers(response)


def test_a_declared_length_at_the_limit_is_not_stopped_by_it() -> None:
    body = body_of_size(UPLOAD_BODY_LIMIT_BYTES)
    assert len(body) == UPLOAD_BODY_LIMIT_BYTES

    response = twin_client().post(TWIN, content=body, headers=MULTIPART)

    # No kind field: the form's own 422, which the limit let through.
    assert refusal_of(response) == (422, FORM_PROBLEM)


def test_a_streamed_body_one_byte_over_the_limit_is_the_same_413_page() -> None:
    over = body_of_size(UPLOAD_BODY_LIMIT_BYTES + 1)

    response = twin_client().post(TWIN, content=chunked(over), headers=MULTIPART)

    assert refusal_of(response) == (413, TOO_LARGE_TEXT)
    assert has_the_pages_headers(response)


def test_a_streamed_body_at_the_limit_is_not_stopped_by_it() -> None:
    at = body_of_size(UPLOAD_BODY_LIMIT_BYTES)

    response = twin_client().post(TWIN, content=chunked(at), headers=MULTIPART)

    assert refusal_of(response) == (422, FORM_PROBLEM)


def test_the_twin_takes_more_than_64_kib() -> None:
    response = twin_client().post(
        TWIN, content=body_of_size(SMALL_BODY_LIMIT_BYTES + 1), headers=MULTIPART
    )

    assert response.status_code != 413


def other_claimant_routes() -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for method, path in routes_of(switched(True))
        if path.startswith("/claimant/") and path != TWIN_PATH
    )


@pytest.mark.parametrize(("method", "path"), other_claimant_routes(), ids=str)
def test_every_other_claimant_path_keeps_64_kib_with_the_switch_on(
    method: str, path: str
) -> None:
    target = re.sub(r"\{[^}]+\}", "CLM-0001", path)
    client = twin_client()

    over = client.request(method, target, content=b"x" * (SMALL_BODY_LIMIT_BYTES + 1))
    at = client.request(method, target, content=b"x" * SMALL_BODY_LIMIT_BYTES)

    assert over.status_code == 413
    assert at.status_code != 413


def test_the_documents_form_next_door_is_still_413_over_64_kib() -> None:
    over = twin_client().post(
        DOCUMENTS,
        content=chunked(b"x" * (SMALL_BODY_LIMIT_BYTES + 1)),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert refusal_of(over) == (413, TOO_LARGE_TEXT)


# ── the form is checked before any database is asked ────────────────────────
def test_a_post_that_is_not_multipart_is_the_422_page() -> None:
    response = twin_client().post(
        TWIN, content=b"kind=photos", headers={"Content-Type": "text/plain"}
    )

    assert refusal_of(response) == (422, CONTENT_TYPE_PROBLEM)


@pytest.mark.parametrize(
    ("parts", "sentence"),
    [
        pytest.param([("kind", None, None, b"photos")], FORM_PROBLEM, id="no file"),
        pytest.param(
            [("file", "f.pdf", "application/pdf", b"x")], FORM_PROBLEM, id="no kind"
        ),
        pytest.param(
            [
                ("kind", None, None, b"invoice"),
                ("file", "f.pdf", "application/pdf", b"x"),
            ],
            KIND_PROBLEM,
            id="an unknown kind",
        ),
    ],
)
def test_a_form_of_the_wrong_shape_is_the_422_page_with_the_fixed_sentence(
    parts: list, sentence: str
) -> None:
    body, headers = multipart_body(parts)

    response = twin_client().post(TWIN, content=body, headers=headers)

    assert refusal_of(response) == (422, sentence)
    assert has_the_pages_headers(response)


def test_a_claim_id_that_is_not_one_is_the_pages_422() -> None:
    body, headers = valid_form()

    response = twin_client().post(
        "/claimant/claims/CLM-1/files", content=body, headers=headers
    )

    assert refusal_of(response)[0] == 422


# ── a post another site made: refused before a byte of the body is read ─────
@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "something-new"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://testserver.evil.example"},
    ],
)
# mutation: the cross-site check removed from the twin
def test_a_post_from_another_site_is_the_403_page_and_no_body_byte_is_read(
    headers: dict[str, str],
) -> None:
    pulled: list[int] = []
    body, content_type = valid_form()

    async def chunks():
        pulled.append(1)
        yield body

    async def scenario() -> httpx.Response:
        async with client_of(claims_app()) as client:
            return await client.post(
                TWIN, content=chunks(), headers=content_type | headers
            )

    response = asyncio.run(scenario())

    assert refusal_of(response) == (403, CROSS_SITE)
    assert has_the_pages_headers(response)
    assert pulled == []  # the app never asked for a chunk of it


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://testserver"},
    ],
)
def test_a_post_from_the_same_origin_or_a_client_with_no_origin_is_not_refused(
    headers: dict[str, str],
) -> None:
    body, content_type = valid_form()

    response = twin_client().post(TWIN, content=body, headers=content_type | headers)

    # Past the origin check the database is asked, and there is none here.
    assert refusal_of(response) == (503, NO_DATABASE)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/claimant/claims/CLM-0001/%66iles", id="an encoded letter"),
        pytest.param("/claimant/claims/CLM-0001%2Ffiles", id="an encoded slash"),
        pytest.param("/claimant/claims/CLM-%30001/files", id="an encoded digit"),
    ],
)
def test_an_encoded_path_is_the_404_page_of_a_path_that_is_no_route(path: str) -> None:
    client = twin_client()
    body, headers = valid_form()
    unknown = client.post(
        "/claimant/claims/CLM-0001/nosuch", content=body, headers=headers
    )

    response = client.post(path, content=body, headers=headers)

    # No database is reachable: had the route run, this would be a 503.
    assert refusal_of(response) == refusal_of(unknown) == (404, NO_SUCH_PAGE)
    assert has_the_pages_headers(response)


# ── the headers are the pages' own on the twin's answers ────────────────────
def test_the_database_down_answer_is_a_page_with_the_pages_headers() -> None:
    body, headers = valid_form()

    response = twin_client().post(TWIN, content=body, headers=headers)

    assert refusal_of(response) == (503, NO_DATABASE)
    assert has_the_pages_headers(response)
    assert "Retry-After" not in response.headers


# ── one pool of permits for both routes ─────────────────────────────────────
def twin_form_post(client: httpx.AsyncClient, chunks=None):
    body, headers = valid_form()
    return client.post(
        TWIN, content=body if chunks is None else chunks, headers=headers
    )


def test_a_twin_post_past_the_bound_is_the_503_page_with_a_wait_and_reads_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = one_permit(monkeypatch)
    pulled: list[int] = []

    async def scenario() -> httpx.Response:
        gate, held = asyncio.Event(), asyncio.Event()
        async with client_of(app) as client:
            stalled = asyncio.create_task(stalled_post(client, gate, held))  # JSON
            try:
                await asyncio.wait_for(held.wait(), timeout=5)

                async def chunks():
                    pulled.append(1)
                    yield valid_form()[0]

                refused = await asyncio.wait_for(
                    twin_form_post(client, chunks()), timeout=5
                )
            finally:
                gate.set()
            await stalled
            return refused

    refused = asyncio.run(scenario())

    assert refusal_of(refused) == (503, SATURATED_DETAIL)
    assert refused.headers["Retry-After"] == "5"
    assert has_the_pages_headers(refused)
    assert pulled == []


def test_a_json_post_past_the_bound_is_refused_when_the_twin_holds_the_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = one_permit(monkeypatch)

    async def scenario() -> httpx.Response:
        gate, held = asyncio.Event(), asyncio.Event()
        async with client_of(app) as client:
            stalled = asyncio.create_task(stalled_post(client, gate, held, TWIN))
            try:
                await asyncio.wait_for(held.wait(), timeout=5)
                refused = await asyncio.wait_for(quick_post(client), timeout=5)
            finally:
                gate.set()
            await stalled
            return refused

    refused = asyncio.run(scenario())

    assert refused.status_code == 503
    assert refused.json() == {"detail": SATURATED_DETAIL, "claim_id": "CLM-0001"}


def test_the_permit_comes_back_after_the_twin_refused_a_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = one_permit(monkeypatch)

    async def scenario() -> list[httpx.Response]:
        async with client_of(app) as client:
            bad = await client.post(
                TWIN, content=b"kind=photos", headers={"Content-Type": "text/plain"}
            )
            return [bad, await twin_form_post(client), await twin_form_post(client)]

    bad, failed, failed_again = asyncio.run(scenario())

    assert refusal_of(bad) == (422, CONTENT_TYPE_PROBLEM)
    # Two failures at the database: none for want of a permit.
    assert refusal_of(failed) == refusal_of(failed_again) == (503, NO_DATABASE)
