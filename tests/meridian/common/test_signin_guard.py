"""The sign-in dependencies on a tiny app made here (S021, T-05): bearer and
cookie, 401, 403 and 503 with the shared bodies, the 401's header, a route
without the role, the refusal hook and the log throttle, and the walk over the
route table (the walk's reports: test_signin_routes.py)."""

import logging
import secrets
import time
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from starlette.applications import Starlette
from starlette.testclient import TestClient

from meridian.platform.common.signin import (
    Principal,
    Reason,
    SigninSettings,
)
from meridian.platform.common.signinguard import (
    Signin,
    _bearer_token_of,
    unguarded_routes,
)
from meridian.platform.common.signinkeys import REFETCH_INTERVAL_SECONDS, KeySet
from meridian.platform.common.signinsession import (
    SessionKeys,
    SessionSettings,
    cookie_name,
    seal_session,
)
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS

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

    @app.get("/who")
    def who_is(
        who: Annotated[Principal, Depends(signin.principal())],
    ) -> dict[str, Any]:
        return {"via": who.via, "issuer": who.issuer}

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


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        ("bearer abc.def.ghi", "abc.def.ghi"),
        ("BEARER abc", "abc"),
        ("Bearer", None),
        ("Bearer ", None),
        ("Bearer a b", None),
        ("Bearer  a", None),
        ("Basic abc", None),
        ("abc", None),
        ("", None),
    ],
)
def test_the_bearer_token_is_read_from_the_header(
    header: str, expected: str | None
) -> None:
    assert _bearer_token_of(header) == expected


# ── what the principal says about how it came ───────────────────────────────
def test_a_bearer_principal_says_bearer_and_a_cookie_principal_says_cookie(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    by_token = client.get("/who", headers=bearer(signin_issuer.mint()))
    client.cookies.set(cookie_name("staff"), staff_cookie(sessions, signin_issuer))
    by_cookie = client.get("/who")

    assert by_token.json() == {"via": "bearer", "issuer": signin_issuer.name}
    assert by_cookie.json() == {"via": "cookie", "issuer": signin_issuer.name}


# ── 503: the issuer's keys cannot be had ────────────────────────────────────
def test_with_the_key_url_down_the_answer_is_503_with_the_shared_body_and_no_header(
    client: TestClient, signin_issuer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    token = signin_issuer.mint()
    signin_issuer.down = True

    with caplog.at_level(logging.DEBUG):
        json_form = client.get("/me", headers=bearer(token))
        page = client.get("/page", headers=bearer(token))

    for response in (json_form, page):
        assert response.status_code == 503
        assert response.json() == REFUSED
        assert "www-authenticate" not in response.headers
    assert any("keys-unavailable" in r.getMessage() for r in caplog.records)


def test_an_unknown_key_id_with_the_keys_up_is_still_a_401(
    client: TestClient, signin_issuer: Any
) -> None:
    response = client.get("/me", headers=bearer(signin_issuer.mint(kid="kid-x")))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_cookie_still_works_while_the_key_url_is_down(
    client: TestClient, sessions: SessionSettings, signin_issuer: Any
) -> None:
    signin_issuer.down = True
    client.cookies.set(cookie_name("staff"), staff_cookie(sessions, signin_issuer))

    assert client.get("/me").status_code == 200


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
        "via": "cookie",
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


# ── one log line per reason per window ──────────────────────────────────────
class MovingClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def sign_in_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "sign-in" in r.getMessage()]


def test_a_flood_of_one_reason_logs_one_line_and_the_next_window_carries_the_count(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    signin_issuer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = MovingClock(signin_issuer.now)
    client = TestClient(build(Signin(signin_settings, signin_keys, None, clock)))
    wrong_audience = bearer(signin_issuer.mint(aud="elsewhere"))

    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            client.get("/me")
        client.get("/me", headers=wrong_audience)
        assert sign_in_lines(caplog) == [
            "sign-in refused: no-credential",
            "sign-in refused: audience",
        ]
        clock.now += REFUSAL_AUDIT_SECONDS - 1
        client.get("/me")
        assert len(sign_in_lines(caplog)) == 2
        clock.now += 1
        client.get("/me")

    assert sign_in_lines(caplog)[2:] == [
        "sign-in refused: no-credential (and 5 more since the last line)"
    ]


# ── the hook of a refusal ───────────────────────────────────────────────────
def hooked(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
    hook: Any,
) -> TestClient:
    signin = Signin(
        signin_settings,
        signin_keys,
        sessions,
        lambda: signin_issuer.now,
        on_refusal=hook,
    )
    return TestClient(build(signin))


def test_the_hook_is_called_for_a_new_reason_with_the_reason_and_the_request(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
    signin_clock: Any,
) -> None:
    calls: list[tuple[Reason, str, str]] = []

    def hook(reason: Reason, request: Request, carried: int) -> None:
        assert carried == 0  # the first of its reason: nothing was held back
        calls.append((reason, request.method, request.url.path))

    client = hooked(signin_settings, signin_keys, sessions, signin_issuer, hook)
    signin_issuer.down = True  # nothing cached yet: the keys cannot be had
    client.get("/page", headers=bearer(signin_issuer.mint()))
    signin_issuer.down = False
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    client.get("/me")
    client.get("/me", headers=bearer(signin_issuer.mint(aud="elsewhere")))
    client.get("/admin", headers=bearer(signin_issuer.mint(roles=["adjuster"])))

    assert calls == [
        (Reason.KEYS_UNAVAILABLE, "GET", "/page"),
        (Reason.NO_CREDENTIAL, "GET", "/me"),
        (Reason.AUDIENCE, "GET", "/me"),
        (Reason.NO_ROLE, "GET", "/admin"),
    ]


def test_the_hook_is_not_called_for_a_request_that_is_let_through(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
) -> None:
    calls: list[Reason] = []

    def hook(reason: Reason, request: Request, carried: int) -> None:
        calls.append(reason)

    client = hooked(signin_settings, signin_keys, sessions, signin_issuer, hook)

    response = client.get("/me", headers=bearer(signin_issuer.mint()))

    assert response.status_code == 200
    assert calls == []


def test_a_hook_that_raises_leaves_the_request_refused_and_logs_only_its_class(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
    signin_clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def hook(reason: Reason, request: Request, carried: int) -> None:
        raise RuntimeError("hook-canary-6 subject-canary-5")

    client = hooked(signin_settings, signin_keys, sessions, signin_issuer, hook)
    signin_issuer.down = True  # nothing cached: the first token meets no keys

    with caplog.at_level(logging.DEBUG):
        unavailable = client.get("/me", headers=bearer(signin_issuer.mint()))
        anonymous = client.get("/me")
        signin_issuer.down = False
        signin_clock.advance(REFETCH_INTERVAL_SECONDS)
        no_role = client.get(
            "/admin", headers=bearer(signin_issuer.mint(roles=["adjuster"]))
        )

    statuses = [r.status_code for r in (unavailable, anonymous, no_role)]
    assert statuses == [503, 401, 403]
    assert anonymous.json() == unavailable.json() == no_role.json() == REFUSED
    assert anonymous.headers["www-authenticate"] == "Bearer"
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in failures] == [
        "the hook of a sign-in refusal failed: RuntimeError"
    ]
    for record in caplog.records:
        assert "canary" not in str(record.__dict__) + record.getMessage()
        assert record.exc_info is None


def test_a_hook_that_keeps_failing_is_logged_once_per_window(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    sessions: SessionSettings,
    signin_issuer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def hook(reason: Reason, request: Request, carried: int) -> None:
        raise ValueError("text")

    client = hooked(signin_settings, signin_keys, sessions, signin_issuer, hook)
    wrong_audience = bearer(signin_issuer.mint(aud="elsewhere"))

    with caplog.at_level(logging.DEBUG):
        # Two reasons, so the hook is called twice and fails twice: one line.
        statuses = [client.get("/me").status_code for _ in range(4)]
        statuses.append(client.get("/me", headers=wrong_audience).status_code)

    assert statuses == [401] * 5
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1


def throttled_client(
    signin_settings: SigninSettings,
    signin_keys: KeySet,
    clock: MovingClock,
    hook: Any,
) -> TestClient:
    signin = Signin(signin_settings, signin_keys, None, clock, on_refusal=hook)
    return TestClient(build(signin))


def test_a_flood_of_one_reason_calls_the_hook_once_and_a_second_reason_once_more(
    signin_settings: SigninSettings, signin_keys: KeySet, signin_issuer: Any
) -> None:
    calls: list[tuple[Reason, int]] = []

    def hook(reason: Reason, request: Request, carried: int) -> None:
        calls.append((reason, carried))

    clock = MovingClock(signin_issuer.now)
    client = throttled_client(signin_settings, signin_keys, clock, hook)

    statuses = {client.get("/me").status_code for _ in range(120)}
    assert statuses == {401}  # every one of them is refused, called or not
    assert calls == [(Reason.NO_CREDENTIAL, 0)]

    wrong_audience = bearer(signin_issuer.mint(aud="elsewhere"))
    assert client.get("/me", headers=wrong_audience).status_code == 401
    assert client.get("/me", headers=wrong_audience).status_code == 401
    assert calls == [(Reason.NO_CREDENTIAL, 0), (Reason.AUDIENCE, 0)]

    clock.now += REFUSAL_AUDIT_SECONDS  # the next window: the count comes with it
    client.get("/me")
    assert calls[2:] == [(Reason.NO_CREDENTIAL, 119)]


def test_a_hook_that_sleeps_does_not_slow_the_refusals_after_the_first(
    signin_settings: SigninSettings, signin_keys: KeySet, signin_issuer: Any
) -> None:
    def slow(reason: Reason, request: Request, carried: int) -> None:
        time.sleep(1.0)

    clock = MovingClock(signin_issuer.now)
    client = throttled_client(signin_settings, signin_keys, clock, slow)
    began = time.perf_counter()
    client.get("/me")
    first = time.perf_counter() - began
    assert first >= 1.0  # the hook ran, and it is as slow as it is

    began = time.perf_counter()
    statuses = {client.get("/me").status_code for _ in range(40)}
    rest = time.perf_counter() - began

    assert statuses == {401}
    assert rest < 1.0  # forty refusals did not call it once


def test_a_hook_is_not_called_again_for_the_count_it_was_given_when_it_failed(
    signin_settings: SigninSettings, signin_keys: KeySet, signin_issuer: Any
) -> None:
    calls: list[int] = []

    def failing(reason: Reason, request: Request, carried: int) -> None:
        calls.append(carried)
        raise ValueError("text")

    clock = MovingClock(signin_issuer.now)
    client = throttled_client(signin_settings, signin_keys, clock, failing)

    statuses = {client.get("/me").status_code for _ in range(10)}

    assert statuses == {401}
    assert calls == [0]  # not retried at the next request


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
