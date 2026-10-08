"""The guard refuses to run on an event loop, whatever the request holds (S021,
Y1d). The key-set client already refuses a call from a thread that runs a loop
(``CalledFromEventLoop``), but only when a bearer token with a key id reaches
it. A guard called on the loop by mistake therefore answered an anonymous
request 401 and a cookie request 200, and the first real bearer token in service
was a 500. The guard's own entry now refuses first, so every request shape trips.

The matrix, on a small app made here: three request shapes (no credential, a
session cookie, a bearer token with a key id) by four ways of reaching the
guard.

==============================  =============  ==========  ==========
way                             anonymous      cookie      bearer
==============================  =============  ==========  ==========
``def`` route with ``Depends``  401            200         200
``async def`` route, declared   401            200         200
``async def`` route, calls it   500            500         500
``async def`` dependency        500            500         500
==============================  =============  ==========  ==========

FastAPI runs a ``def`` dependency in its thread pool, whatever kind of route
declares it, so the first two rows are not refused. The last two put the call on
the loop.
"""

import logging
import secrets
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Request
from starlette.testclient import TestClient

from meridian.platform.common.http import UnexpectedErrorMiddleware
from meridian.platform.common.signin import Principal, Reason, SigninSettings
from meridian.platform.common.signinguard import Signin
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import (
    SessionKeys,
    SessionSettings,
    cookie_name,
    seal_session,
)

SHAPES = ("anonymous", "cookie", "bearer")
OFF_THE_LOOP = {"anonymous": 401, "cookie": 200, "bearer": 200}
ON_THE_LOOP = {"anonymous": 500, "cookie": 500, "bearer": 500}
INTERNAL = {"detail": "internal error"}


@pytest.fixture
def refusals() -> list[Reason]:
    """What the refusal hook was told: nothing, for a call refused on the loop."""
    return []


@pytest.fixture
def sessions() -> SessionSettings:
    return SessionSettings(SessionKeys(current=secrets.token_bytes(32)))


@pytest.fixture
def signin(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
    refusals: list[Reason],
) -> Signin:
    return Signin(
        signin_settings,
        signin_keys,
        sessions,
        lambda: signin_issuer.now,
        on_refusal=lambda reason, request, carried: refusals.append(reason),
    )


def build(signin: Signin) -> FastAPI:
    app = FastAPI()
    app.add_middleware(UnexpectedErrorMiddleware)  # the 500 and its log line
    guard = signin.principal()

    async def wrapper(request: Request) -> Principal:
        return guard(request)  # the guard, called on the event loop

    @app.get("/def")
    def by_def(who: Annotated[Principal, Depends(guard)]) -> dict[str, Any]:
        return {"via": who.via}

    @app.get("/async-declared")
    async def by_async(who: Annotated[Principal, Depends(guard)]) -> dict[str, Any]:
        return {"via": who.via}

    @app.get("/async-calls")
    async def calls_it(request: Request) -> dict[str, Any]:
        return {"via": guard(request).via}

    @app.get("/async-dependency")
    async def by_wrapper(who: Annotated[Principal, Depends(wrapper)]) -> dict[str, Any]:
        return {"via": who.via}

    return app


@pytest.fixture
def client(signin: Signin) -> TestClient:
    return TestClient(build(signin))


def send(
    client: TestClient,
    shape: str,
    path: str,
    signin_issuer: Any,
    sessions: SessionSettings,
) -> Any:
    """The request of one shape. The credentials are valid: any refusal comes
    from the call, not from the credential."""
    if shape == "cookie":
        principal = Principal(
            population="staff",
            issuer=signin_issuer.name,
            subject="subject-1",
            roles=frozenset({"adjuster"}),
            expires_at=int(signin_issuer.now) + 600,
            via="cookie",
        )
        value = seal_session(principal, sessions.keys, signin_issuer.now)
        client.cookies.set(cookie_name("staff"), value)
    if shape == "bearer":
        return client.get(
            path, headers={"Authorization": f"Bearer {signin_issuer.mint()}"}
        )
    return client.get(path)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("path", ["/def", "/async-declared"])
def test_a_guard_that_fastapi_runs_in_its_thread_pool_is_not_refused(
    client: TestClient,
    signin_issuer: Any,
    sessions: SessionSettings,
    path: str,
    shape: str,
) -> None:
    """An ``async def`` route that only declares the dependency is the case that
    must not be mistaken for a call on the loop."""
    response = send(client, shape, path, signin_issuer, sessions)

    assert response.status_code == OFF_THE_LOOP[shape]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("path", ["/async-calls", "/async-dependency"])
def test_a_guard_called_on_the_event_loop_is_a_500_for_every_request_shape(
    client: TestClient,
    signin_issuer: Any,
    sessions: SessionSettings,
    refusals: list[Reason],
    caplog: pytest.LogCaptureFixture,
    path: str,
    shape: str,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = send(client, shape, path, signin_issuer, sessions)

    assert response.status_code == ON_THE_LOOP[shape]
    assert response.json() == INTERNAL
    assert any(
        "CalledFromEventLoop" in record.getMessage() for record in caplog.records
    )
    assert refusals == []  # refused first: no sign-in refusal, no hook call
    assert signin_issuer.calls == 0  # and no key was asked for


@pytest.mark.parametrize("shape", SHAPES)
def test_nothing_of_the_request_is_in_the_log_of_a_call_on_the_event_loop(
    client: TestClient,
    signin_issuer: Any,
    sessions: SessionSettings,
    caplog: pytest.LogCaptureFixture,
    shape: str,
) -> None:
    token = signin_issuer.mint()
    with caplog.at_level(logging.DEBUG):
        send(client, shape, "/async-calls", signin_issuer, sessions)
    cookie = client.cookies.get(cookie_name("staff")) or "no cookie sent"

    text = " ".join(
        record.getMessage() + str(record.exc_info) + str(record.args)
        for record in caplog.records
    )
    assert "CalledFromEventLoop" in text
    for part in (token, cookie, "Bearer", "Authorization", cookie_name("staff")):
        assert part not in text
