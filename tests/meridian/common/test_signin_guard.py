"""The sign-in dependencies on a tiny app made here (S021, T-05): bearer and
cookie, 401 and 403 with the shared bodies, the 401's header, a route without
the role, and the walk over the route table."""

import logging
import secrets
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.applications import Starlette
from starlette.testclient import TestClient

from meridian.platform.common.signin import Principal, SigninSettings
from meridian.platform.common.signinguard import Signin, unguarded_routes
from meridian.platform.common.signinkeys import KeySet
from meridian.platform.common.signinsession import (
    SessionKeys,
    SessionSettings,
    cookie_name,
    seal_session,
)

REFUSED = {"detail": "request refused"}
ENDS = 1_800_000_600


@pytest.fixture
def seal() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def sessions(seal: bytes) -> SessionSettings:
    return SessionSettings(SessionKeys(current=seal))


@pytest.fixture
def signin(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
) -> Signin:
    return Signin(signin_settings, signin_keys, sessions, lambda: signin_issuer.now)


def build(signin: Signin) -> FastAPI:
    app = FastAPI()

    @app.get("/me")
    def me(who: Annotated[Principal, Depends(signin.principal())]) -> dict[str, Any]:
        return {"roles": sorted(who.roles)}

    @app.get("/page")
    def page(
        who: Annotated[Principal, Depends(signin.principal(json_form=False))],
    ) -> dict[str, Any]:
        return {"roles": sorted(who.roles)}

    @app.get("/admin")
    def admin(
        who: Annotated[Principal, Depends(signin.require_role("platform-admin"))],
    ) -> dict[str, Any]:
        return {"roles": sorted(who.roles)}

    @app.get("/admin-page")
    def admin_page(
        who: Annotated[
            Principal,
            Depends(signin.require_role("platform-admin", json_form=False)),
        ],
    ) -> dict[str, Any]:
        return {"roles": sorted(who.roles)}

    @app.get("/open")
    def open_route() -> dict[str, str]:
        return {"open": "yes"}

    return app


@pytest.fixture
def client(signin: Signin) -> TestClient:
    return TestClient(build(signin))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── bearer ──────────────────────────────────────────────────────────────────
def test_a_good_bearer_token_reaches_the_route(
    client: TestClient, signin_issuer: Any
) -> None:
    token = signin_issuer.mint(roles=["adjuster"])

    response = client.get("/me", headers=bearer(token))

    assert response.status_code == 200
    assert response.json() == {"roles": ["adjuster"]}


def test_a_route_with_no_credential_answers_401_with_the_shared_body_and_header(
    client: TestClient,
) -> None:
    response = client.get("/me")

    assert response.status_code == 401
    assert response.json() == REFUSED
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_bad_token_answers_the_same_401(
    client: TestClient, signin_issuer: Any
) -> None:
    good = client.get("/me")
    bad = client.get("/me", headers=bearer(signin_issuer.mint(aud="elsewhere")))

    assert bad.status_code == good.status_code == 401
    assert bad.json() == good.json() == REFUSED
    assert bad.headers["www-authenticate"] == "Bearer"


def test_a_401_for_a_page_has_the_same_body_and_no_authenticate_header(
    client: TestClient,
) -> None:
    response = client.get("/page")

    assert response.status_code == 401
    assert response.json() == REFUSED
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "header",
    ["Basic abc", "Bearer", "Bearer a b", "Token abc", "abc"],
)
def test_an_authorization_header_that_is_not_a_bearer_token_is_refused(
    client: TestClient, header: str
) -> None:
    response = client.get("/me", headers={"Authorization": header})

    assert response.status_code == 401
    assert response.json() == REFUSED


def test_the_bearer_scheme_is_case_insensitive(
    client: TestClient, signin_issuer: Any
) -> None:
    token = signin_issuer.mint()

    response = client.get("/me", headers={"Authorization": f"bearer {token}"})

    assert response.status_code == 200


def test_two_authorization_headers_are_refused(
    client: TestClient, signin_issuer: Any
) -> None:
    token = signin_issuer.mint()

    response = client.get(
        "/me",
        headers=[  # type: ignore[arg-type]
            ("Authorization", f"Bearer {token}"),
            ("Authorization", f"Bearer {token}"),
        ],
    )

    assert response.status_code == 401


# ── 403: a valid principal without the role ─────────────────────────────────
def test_a_valid_principal_without_the_role_gets_403_with_the_shared_body(
    client: TestClient, signin_issuer: Any
) -> None:
    token = signin_issuer.mint(roles=["adjuster"])

    response = client.get("/admin", headers=bearer(token))

    assert response.status_code == 403
    assert response.json() == REFUSED
    assert "www-authenticate" not in response.headers


def test_the_role_lets_the_principal_through(
    client: TestClient, signin_issuer: Any
) -> None:
    token = signin_issuer.mint(roles=["adjuster", "platform-admin"])

    response = client.get("/admin", headers=bearer(token))

    assert response.status_code == 200


def test_no_credential_on_a_role_route_is_401_not_403(
    client: TestClient, signin_issuer: Any
) -> None:
    anonymous = client.get("/admin")
    page = client.get("/admin-page")

    assert (anonymous.status_code, page.status_code) == (401, 401)
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert "www-authenticate" not in page.headers


def test_a_principal_with_no_roles_claim_is_403_on_a_role_route(
    client: TestClient, signin_issuer: Any
) -> None:
    token = signin_issuer.mint(drop=("roles",))

    assert client.get("/admin", headers=bearer(token)).status_code == 403
    assert client.get("/me", headers=bearer(token)).json() == {"roles": []}


# ── the session cookie ──────────────────────────────────────────────────────
def staff_cookie(sessions: SessionSettings, signin_issuer: Any, **changes: Any) -> str:
    fields: dict[str, Any] = {
        "population": "staff",
        "issuer": signin_issuer.name,
        "subject": "subject-1",
        "roles": frozenset({"adjuster"}),
        "expires_at": ENDS,
    }
    principal = Principal(**{**fields, **changes})
    return seal_session(principal, sessions.keys, signin_issuer.now)


def test_a_session_cookie_reaches_the_route(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    client.cookies.set(cookie_name("staff"), staff_cookie(sessions, signin_issuer))

    response = client.get("/me")

    assert response.status_code == 200
    assert response.json() == {"roles": ["adjuster"]}


def test_the_session_cookie_carries_the_role_check_too(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    client.cookies.set(cookie_name("staff"), staff_cookie(sessions, signin_issuer))

    assert client.get("/admin").status_code == 403


def test_the_claimant_s_cookie_does_not_open_a_staff_route(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    claimant = staff_cookie(sessions, signin_issuer, population="claimant")
    # Under the staff cookie's name, and under its own.
    client.cookies.set(cookie_name("staff"), claimant)
    client.cookies.set(cookie_name("claimant"), claimant)

    response = client.get("/me")

    assert response.status_code == 401


def test_a_cookie_of_another_issuer_is_refused(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    other = staff_cookie(sessions, signin_issuer, issuer="https://other.example/realm")
    client.cookies.set(cookie_name("staff"), other)

    assert client.get("/me").status_code == 401


@pytest.mark.parametrize(("at", "status"), [(ENDS - 1, 200), (ENDS, 401)])
def test_a_cookie_is_refused_from_its_expiry_on(
    at: int,
    status: int,
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
) -> None:
    clocked = Signin(signin_settings, signin_keys, sessions, lambda: at)
    client = TestClient(build(clocked))
    client.cookies.set(cookie_name("staff"), staff_cookie(sessions, signin_issuer))

    assert client.get("/me").status_code == status


def test_a_cookie_sealed_with_another_key_is_refused(
    client: TestClient, signin_issuer: Any
) -> None:
    foreign = SessionSettings(SessionKeys(current=secrets.token_bytes(32)))
    client.cookies.set(cookie_name("staff"), staff_cookie(foreign, signin_issuer))

    assert client.get("/me").status_code == 401


def test_a_bad_bearer_token_is_not_rescued_by_a_good_cookie(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    client.cookies.set(cookie_name("staff"), staff_cookie(sessions, signin_issuer))

    response = client.get("/me", headers=bearer(signin_issuer.mint(aud="elsewhere")))

    assert response.status_code == 401


def test_a_good_bearer_token_wins_over_a_cookie_of_another_person(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    client.cookies.set(
        cookie_name("staff"),
        staff_cookie(sessions, signin_issuer, roles=frozenset({"platform-admin"})),
    )

    response = client.get("/me", headers=bearer(signin_issuer.mint(roles=["auditor"])))

    assert response.json() == {"roles": ["auditor"]}


def test_without_session_settings_a_cookie_is_ignored(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
) -> None:
    bearer_only = Signin(signin_settings, signin_keys, None, lambda: signin_issuer.now)
    client = TestClient(build(bearer_only))
    client.cookies.set(cookie_name("staff"), staff_cookie(sessions, signin_issuer))

    assert client.get("/me").status_code == 401


# ── the dependency is one function per form ─────────────────────────────────
def test_the_same_dependency_is_given_each_time(signin: Signin) -> None:
    assert signin.principal() is signin.principal()
    assert signin.principal(json_form=False) is signin.principal(json_form=False)
    assert signin.principal() is not signin.principal(json_form=False)


# ── the log line holds the reason and nothing else ──────────────────────────
def test_a_refusal_logs_its_reason_only(
    client: TestClient, signin_issuer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    token = signin_issuer.mint(sub="subject-canary-5", aud="elsewhere")

    with caplog.at_level(logging.DEBUG):
        client.get("/me", headers=bearer(token))

    lines = [r.getMessage() for r in caplog.records if "sign-in" in r.getMessage()]
    assert lines == ["sign-in refused: audience"]


# ── the route walk ──────────────────────────────────────────────────────────
OWN_ROUTES = [
    "GET /docs",
    "GET /docs/oauth2-redirect",
    "GET /openapi.json",
    "GET /redoc",
]


def test_the_walk_names_the_routes_with_no_sign_in_dependency(signin: Signin) -> None:
    found = unguarded_routes(build(signin), open_routes=[])

    assert found == sorted([*OWN_ROUTES, "GET /open"])


def test_the_walk_leaves_out_the_closed_list(signin: Signin) -> None:
    closed = [*OWN_ROUTES, "GET /open"]

    assert unguarded_routes(build(signin), open_routes=closed) == []


def test_a_name_on_the_closed_list_covers_that_method_and_path_only(
    signin: Signin,
) -> None:
    app = build(signin)

    @app.post("/open")
    def posted() -> dict[str, str]:
        return {}

    found = unguarded_routes(app, open_routes=[*OWN_ROUTES, "GET /open"])

    assert found == ["POST /open"]


def test_a_route_with_another_dependency_is_not_guarded(signin: Signin) -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def other() -> str:
        return "not a sign-in"

    @app.get("/other")
    def route(value: Annotated[str, Depends(other)]) -> str:
        return value

    assert unguarded_routes(app, open_routes=[]) == ["GET /other"]


def test_a_dependency_on_the_router_guards_its_routes(signin: Signin) -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter(dependencies=[Depends(signin.principal())])

    @router.get("/inside")
    def inside() -> str:
        return "inside"

    app.include_router(router)

    @app.get("/outside")
    def outside() -> str:
        return "outside"

    assert unguarded_routes(app, open_routes=[]) == ["GET /outside"]


def test_a_dependency_in_the_route_decorator_guards_it(signin: Signin) -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/decorated", dependencies=[Depends(signin.require_role("auditor"))])
    def decorated() -> str:
        return "decorated"

    assert unguarded_routes(app, open_routes=[]) == []


def test_a_guard_buried_in_another_dependency_is_found(signin: Signin) -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def wrapper(who: Annotated[Principal, Depends(signin.principal())]) -> str:
        return "wrapped"

    @app.get("/wrapped")
    def route(value: Annotated[str, Depends(wrapper)]) -> str:
        return value

    assert unguarded_routes(app, open_routes=[]) == []


def test_a_mount_is_named_unless_it_is_on_the_closed_list(signin: Signin) -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", Starlette())

    assert unguarded_routes(app, open_routes=[]) == ["MOUNT /static"]
    assert unguarded_routes(app, open_routes=["MOUNT /static"]) == []


def test_the_walk_is_a_read_of_the_route_table_and_fetches_no_key(
    signin: Signin, signin_issuer: Any
) -> None:
    unguarded_routes(build(signin), open_routes=[])

    assert signin_issuer.calls == 0
