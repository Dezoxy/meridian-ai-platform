"""How the upload routes read their body (S070 F4c, the FastAPI review): a client
that goes away mid-body, a deadline on the read, and a guard for the parser that
keeps a file part out of ``/tmp``. No database: every answer here is one the app
gives before it opens a connection, or one a database that is not there gives.
"""

import asyncio
import logging
from tempfile import SpooledTemporaryFile

import httpx
import pytest
from servicesupport import multipart_body
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartParser
from workloads.claims_triage.test_claim_uploads_brakes import (
    NO_DATABASE,
    claims_app,
    client_of,
    one_permit,
    quick_post,
    stalled_post,
    valid_form,
)
from workloads.claims_triage.test_claimant_upload_twin import TWIN, refusal_of

from meridian.workloads.claims_triage import uploads as upload_module
from meridian.workloads.claims_triage.uploads import (
    KIND_FIELD_MAX_BYTES,
    MAX_FILE_BYTES,
    READ_TIMEOUT_DETAIL,
    UPLOAD_BODY_LIMIT_BYTES,
    MemoryOnlyParser,
)

JSON_PATH = "/claims/CLM-0001/files"
ROUTES = [
    pytest.param(JSON_PATH, id="the JSON route"),
    pytest.param(TWIN, id="the claimant's form"),
]


# ── a client that goes away in the middle of its body ───────────────────────
async def post_then_disconnect(app, path: str) -> list[dict]:
    """What uvicorn does when the peer closes the connection mid-body: the first
    bytes arrive, then ``http.disconnect``. (An httpx task that is cancelled is
    not that: it cancels the app's own task, which is another path.) Returns the
    messages the app sent."""
    body, headers = valid_form()
    inbox = [
        {"type": "http.request", "body": body[:30], "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict] = []

    async def receive() -> dict:
        return inbox.pop(0) if len(inbox) > 1 else inbox[0]

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "headers": [(b"host", b"testserver")]
        + [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    await app(scope, receive, send)
    return sent


@pytest.mark.parametrize("path", ROUTES)
# mutation: the disconnect catch removed
def test_a_client_that_disconnects_mid_body_is_no_500_and_no_error_line(
    caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        sent = asyncio.run(post_then_disconnect(claims_app(), path))

    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    assert start["status"] == 400  # nobody receives it: the peer is gone
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    (line,) = [r for r in caplog.records if r.name == upload_module.__name__]
    assert line.levelno == logging.INFO
    assert "ClientDisconnect" in line.getMessage()


@pytest.mark.parametrize("path", ROUTES)
def test_the_permit_comes_back_when_the_peer_disconnects_mid_body(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    app = one_permit(monkeypatch)

    async def scenario() -> list[httpx.Response]:
        async with client_of(app) as client:
            await post_then_disconnect(app, path)
            # One permit, so an upload that leaked it would now be refused as busy.
            return [await quick_post(client), await quick_post(client)]

    first, second = asyncio.run(scenario())

    assert first.json()["detail"] == second.json()["detail"] == NO_DATABASE


# ── a deadline on the read ──────────────────────────────────────────────────
@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_module, "READ_DEADLINE_SECONDS", 0.2)


@pytest.mark.parametrize("path", ROUTES)
# mutation: the read deadline removed
def test_a_body_that_does_not_finish_in_time_is_408_and_the_permit_comes_back(
    monkeypatch: pytest.MonkeyPatch, short_deadline: None, path: str
) -> None:
    app = one_permit(monkeypatch)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        gate, held = asyncio.Event(), asyncio.Event()  # gate: the body never ends
        async with client_of(app) as client:
            stalled = asyncio.create_task(stalled_post(client, gate, held, path))
            try:
                late = await asyncio.wait_for(stalled, timeout=5)
            finally:
                gate.set()
            return late, await quick_post(client)

    late, after = asyncio.run(scenario())

    if path == JSON_PATH:
        assert late.status_code == 408
        assert late.json() == {"detail": READ_TIMEOUT_DETAIL, "claim_id": "CLM-0001"}
    else:
        assert refusal_of(late) == (408, READ_TIMEOUT_DETAIL)
    # One permit: the next upload is not refused as busy.
    assert after.json()["detail"] == NO_DATABASE


def test_a_body_that_finishes_in_time_is_not_stopped_by_the_deadline() -> None:
    body, headers = valid_form()

    async def scenario() -> httpx.Response:
        async with client_of(claims_app()) as client:
            return await client.post(JSON_PATH, content=body, headers=headers)

    # The database is not there: the answer is the one after the read.
    assert asyncio.run(scenario()).json()["detail"] == NO_DATABASE


# ── the parser that keeps a file out of /tmp ────────────────────────────────
PART_BYTES = 1_100_000  # a little over 1 MiB and well under the body's limit


def rolled_to_disk(parser_class: type[MultiPartParser]) -> bool:
    """Whether the file part of a form of ``PART_BYTES`` is on disk after the
    parser has read it: ``SpooledTemporaryFile._rolled``, the internal the
    parser's class attribute decides."""

    async def run() -> bool:
        data = b"%PDF-" + b"\x00" * (PART_BYTES - 5)
        body, headers = multipart_body(
            [
                ("kind", None, None, b"photos"),
                ("file", "f.pdf", "application/pdf", data),
            ]
        )
        assert MAX_FILE_BYTES < PART_BYTES < len(body) <= UPLOAD_BODY_LIMIT_BYTES

        async def stream():
            yield body

        parser = parser_class(
            Headers(headers),
            stream(),
            max_files=1,
            max_fields=1,
            max_part_size=KIND_FIELD_MAX_BYTES,
        )
        form = await parser.parse()
        try:
            (file,) = [v for _, v in form.multi_items() if isinstance(v, UploadFile)]
            spool = file.file
            assert isinstance(spool, SpooledTemporaryFile)
            return bool(spool._rolled)
        finally:
            await form.close()

    return asyncio.run(run())


def test_a_part_just_over_1_mib_is_not_rolled_to_disk_by_the_routes_parser() -> None:
    assert rolled_to_disk(MemoryOnlyParser) is False


def test_the_stock_parser_does_roll_that_part_so_the_guard_can_fail() -> None:
    # The control: without the subclass's override the same part is on disk. If
    # Starlette renames what the override leans on, this stays True and the test
    # above goes red, which is the point of both.
    assert rolled_to_disk(MultiPartParser) is True
