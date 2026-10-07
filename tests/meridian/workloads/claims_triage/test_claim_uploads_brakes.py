"""The upload route's own brakes, which must not depend on which edge route served
the path (S070 F2b, the security review's H-1 and M-1): a bound on concurrent
uploads, a refusal of an encoded path, and the rate setting. No database here;
the rate limit and the lock wait that need one are in ``test_claim_uploads.py``."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from servicesupport import multipart_body

from meridian.platform.common.env import SettingsError
from meridian.workloads.claims_triage import uploads as upload_module
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.uploads import (
    DEFAULT_RATE_PER_MINUTE,
    MAX_RATE_PER_MINUTE,
    MIN_RATE_PER_MINUTE,
    UPLOADS_RATE_ENV,
    rate_per_minute_of,
)

DSN = "postgresql://claims_api@db.invalid/meridian"
ENV = {
    "MERIDIAN_RUNTIME_URL": "http://runtime.invalid:8080",
    "MERIDIAN_DATABASE_URL": DSN,
}
PATH = "/claims/CLM-0001/files"
PDF = b"%PDF-1.4\nsynthetic\n%%EOF\n"
SATURATED = "uploads are busy: too many at once; try again shortly"
NO_DATABASE = "the database is unavailable"


def claims_app() -> FastAPI:
    return create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=DSN,
            uploads_enabled=True,
        )
    )


def valid_form() -> tuple[bytes, dict[str, str]]:
    return multipart_body(
        [("kind", None, None, b"photos"), ("file", "f.pdf", "application/pdf", PDF)]
    )


# ── the rate setting ────────────────────────────────────────────────────────
def test_the_rate_is_30_a_minute_by_default_between_a_floor_of_6_and_a_cap_of_600() -> (
    None
):
    built = ClaimsSettings.from_env(ENV)

    assert UPLOADS_RATE_ENV == "MERIDIAN_UPLOADS_RATE_PER_MINUTE"
    assert built.uploads_rate_per_minute == DEFAULT_RATE_PER_MINUTE == 30
    assert (MIN_RATE_PER_MINUTE, MAX_RATE_PER_MINUTE) == (6, 600)


def test_the_rate_is_read_in_whole_numbers_from_its_variable() -> None:
    assert (
        ClaimsSettings.from_env(ENV | {UPLOADS_RATE_ENV: "12"}).uploads_rate_per_minute
        == 12
    )


@pytest.mark.parametrize("value", [MIN_RATE_PER_MINUTE, MAX_RATE_PER_MINUTE])
def test_the_rate_floor_and_cap_themselves_are_accepted(value: int) -> None:
    assert rate_per_minute_of(str(value)) == value
    settings = ClaimsSettings(
        runtime_url="http://runtime.invalid",
        database_url=DSN,
        uploads_rate_per_minute=value,
    )
    assert settings.uploads_rate_per_minute == value


@pytest.mark.parametrize(
    "raw",
    [
        str(MIN_RATE_PER_MINUTE - 1),
        str(MAX_RATE_PER_MINUTE + 1),
        "0",
        "-6",
        "",
        "30/min",
        "3e1",
        "٣٠",  # Arabic-Indic digits: ``int`` would read them
    ],
)
def test_a_rate_outside_the_floor_and_cap_stops_the_start_without_its_value(
    raw: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        rate_per_minute_of(raw)

    assert str(raised.value) == (
        f"{UPLOADS_RATE_ENV} must be a whole number of uploads a minute "
        f"from {MIN_RATE_PER_MINUTE} to {MAX_RATE_PER_MINUTE}"
    )


# ── an encoded path is refused before the body is read ──────────────────────
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/claims/CLM-0001/%66iles", id="an encoded letter"),
        pytest.param("/claims/CLM-0001%2Ffiles", id="an encoded slash"),
        pytest.param("/claims/CLM-%30001/files", id="an encoded digit"),
        pytest.param("/claims%2FCLM-0001/files", id="an encoded slash earlier"),
        pytest.param("/claims/CLM-0001/files%25", id="an encoded percent"),
        pytest.param("/claims/CLM-0001/files%00", id="an encoded NUL"),
    ],
)
def test_a_path_that_holds_a_percent_is_the_404_of_a_path_that_is_no_route(
    path: str,
) -> None:
    client = TestClient(claims_app(), raise_server_exceptions=False)
    body, headers = valid_form()
    unknown = client.post("/claims/CLM-0001/nosuch", content=body, headers=headers)

    response = client.post(path, content=body, headers=headers)

    # No database is reachable: had the route run, this would be a 503.
    assert response.status_code == unknown.status_code == 404
    assert response.json() == unknown.json()


def test_a_percent_in_the_query_is_not_a_percent_in_the_path() -> None:
    client = TestClient(claims_app(), raise_server_exceptions=False)
    body, headers = valid_form()

    response = client.post(PATH + "?x=%41", content=body, headers=headers)

    # Past the path check, the form is read and the database asked: none here.
    assert response.status_code == 503
    assert response.json()["detail"] == NO_DATABASE


def test_the_plain_path_is_not_refused() -> None:
    client = TestClient(claims_app(), raise_server_exceptions=False)
    body, headers = valid_form()

    assert client.post(PATH, content=body, headers=headers).status_code == 503


# ── a bound on concurrent uploads ───────────────────────────────────────────
async def stalled_post(
    client: httpx.AsyncClient,
    gate: asyncio.Event,
    held: asyncio.Event,
    path: str = PATH,
):
    """An upload whose body stops after its first bytes until ``gate`` is set:
    the request holds whatever the route holds while a body arrives. ``held`` is
    set when the app has asked for the chunk after the first, which it does only
    once it has taken its permit and started to read: a test waits for it instead
    of for a time."""
    body, headers = valid_form()

    async def chunks():
        yield body[:20]
        held.set()
        await gate.wait()
        yield body[20:]

    return await client.post(path, content=chunks(), headers=headers)


async def quick_post(client: httpx.AsyncClient):
    body, headers = valid_form()
    return await client.post(PATH, content=body, headers=headers)


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


def one_permit(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(upload_module, "MAX_CONCURRENT_UPLOADS", 1)
    return claims_app()


# mutation: the semaphore removed
def test_an_upload_past_the_bound_is_503_at_once_with_its_own_sentence_and_a_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = one_permit(monkeypatch)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        gate, held = asyncio.Event(), asyncio.Event()
        async with client_of(app) as client:
            first = asyncio.create_task(stalled_post(client, gate, held))
            try:
                # The first holds the permit, its body unfinished.
                await asyncio.wait_for(held.wait(), timeout=5)
                refused = await asyncio.wait_for(quick_post(client), timeout=5)
            finally:
                gate.set()  # whatever happened, the first is let go
            return refused, await first

    refused, first = asyncio.run(scenario())

    assert refused.status_code == 503
    assert refused.json() == {"detail": SATURATED, "claim_id": "CLM-0001"}
    assert refused.headers["Retry-After"] == "5"
    # The first went on to the database, which is not there: not the busy answer.
    assert first.status_code == 503
    assert first.json()["detail"] == NO_DATABASE


def test_the_refusal_reads_no_byte_of_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = one_permit(monkeypatch)
    pulled: list[int] = []

    async def scenario() -> httpx.Response:
        gate, held = asyncio.Event(), asyncio.Event()
        async with client_of(app) as client:
            first = asyncio.create_task(stalled_post(client, gate, held))
            await asyncio.wait_for(held.wait(), timeout=5)

            async def chunks():
                pulled.append(1)
                yield valid_form()[0]

            headers = valid_form()[1]
            try:
                refused = await asyncio.wait_for(
                    client.post(PATH, content=chunks(), headers=headers), timeout=5
                )
            finally:
                gate.set()
            await first
            return refused

    refused = asyncio.run(scenario())

    assert refused.status_code == 503
    assert refused.json()["detail"] == SATURATED
    assert pulled == []  # the app never asked for a chunk of it


def test_the_permit_comes_back_after_a_refusal_a_failure_and_a_finished_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = one_permit(monkeypatch)

    async def scenario() -> list[httpx.Response]:
        async with client_of(app) as client:
            bad_type = await client.post(
                PATH, content=b"kind=photos", headers={"Content-Type": "text/plain"}
            )
            return [bad_type, await quick_post(client), await quick_post(client)]

    refused, failed, failed_again = asyncio.run(scenario())

    # A 422 before the database, then two failures at it: none for want of a
    # permit, so the permit came back each time.
    assert refused.status_code == 422
    assert failed.json()["detail"] == failed_again.json()["detail"] == NO_DATABASE


def test_the_permit_comes_back_when_the_client_goes_away_mid_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = one_permit(monkeypatch)

    async def scenario() -> httpx.Response:
        gate, held = asyncio.Event(), asyncio.Event()  # gate never set
        async with client_of(app) as client:
            first = asyncio.create_task(stalled_post(client, gate, held))
            await asyncio.wait_for(held.wait(), timeout=5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            return await quick_post(client)

    after = asyncio.run(scenario())

    assert after.status_code == 503
    assert after.json()["detail"] == NO_DATABASE


def test_each_app_has_its_own_permits(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = one_permit(monkeypatch), claims_app()

    async def scenario() -> httpx.Response:
        gate, held = asyncio.Event(), asyncio.Event()
        async with client_of(first) as busy, client_of(second) as other:
            stalled = asyncio.create_task(stalled_post(busy, gate, held))
            try:
                await asyncio.wait_for(held.wait(), timeout=5)
                answer = await asyncio.wait_for(quick_post(other), timeout=5)
            finally:
                gate.set()
            await stalled
            return answer

    assert asyncio.run(scenario()).json()["detail"] == NO_DATABASE
