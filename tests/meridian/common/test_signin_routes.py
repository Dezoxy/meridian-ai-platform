"""The walk over an app's routes (S021, T-05): what it reports for each route
(methods, guard, roles demanded, population), the routes it must not lose (HEAD
only, OPTIONS only, a mount, a websocket), a marker made by hand, and a
dependency override."""

from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from meridian.platform.common.signin import Principal, SigninSettings
from meridian.platform.common.signinguard import (
    GUARD_ATTRIBUTE,
    ROLE_ATTRIBUTE,
    RouteReport,
    Signin,
    route_reports,
    unguarded_routes,
)
from meridian.platform.common.signinkeys import KeySet

BARE = {"docs_url": None, "redoc_url": None, "openapi_url": None}


@pytest.fixture
def signin(signin_settings: SigninSettings, signin_keys: KeySet) -> Signin:
    return Signin(signin_settings, signin_keys)


def claimants(signin_issuer: Any, signin_keys: KeySet) -> Signin:
    settings = SigninSettings(
        population="claimant",
        issuer=signin_issuer.name,
        audience=signin_issuer.audience,
        keys_url=signin_issuer.url,
    )
    return Signin(settings, signin_keys)


def report_for(app: FastAPI, label: str) -> RouteReport:
    found = [r for r in route_reports(app) if label in r.labels]
    assert len(found) == 1, label
    return found[0]


# ── what each route reports ─────────────────────────────────────────────────
def test_a_route_reports_its_methods_guard_roles_and_population(
    signin: Signin,
) -> None:
    app = FastAPI(**BARE)

    @app.get("/plain")
    def plain(who: Annotated[Principal, Depends(signin.principal())]) -> str:
        return "plain"

    @app.post("/admin")
    def admin(
        who: Annotated[Principal, Depends(signin.require_role("platform-admin"))],
    ) -> str:
        return "admin"

    @app.get("/open")
    def open_route() -> str:
        return "open"

    assert report_for(app, "GET /plain") == RouteReport(
        path="/plain",
        labels=("GET /plain",),
        methods=("GET",),
        guarded=True,
        roles=frozenset(),
        populations=frozenset({"staff"}),
    )
    assert report_for(app, "POST /admin") == RouteReport(
        path="/admin",
        labels=("POST /admin",),
        methods=("POST",),
        guarded=True,
        roles=frozenset({"platform-admin"}),
        populations=frozenset({"staff"}),
    )
    assert report_for(app, "GET /open") == RouteReport(
        path="/open",
        labels=("GET /open",),
        methods=("GET",),
        guarded=False,
        roles=frozenset(),
        populations=frozenset(),
    )


def test_the_principal_dependency_alone_demands_no_role(signin: Signin) -> None:
    app = FastAPI(**BARE)

    @app.post("/changes")
    def changes(who: Annotated[Principal, Depends(signin.principal())]) -> str:
        return "changed"

    report = report_for(app, "POST /changes")

    assert report.guarded
    assert report.roles == frozenset()


def test_a_route_that_demands_two_roles_reports_both(signin: Signin) -> None:
    app = FastAPI(**BARE)

    @app.get(
        "/both",
        dependencies=[
            Depends(signin.require_role("auditor")),
            Depends(signin.require_role("adjuster", json_form=False)),
        ],
    )
    def both() -> str:
        return "both"

    assert report_for(app, "GET /both").roles == frozenset({"auditor", "adjuster"})


def test_a_route_reports_the_population_of_its_guard(
    signin: Signin, signin_issuer: Any, signin_keys: KeySet
) -> None:
    other = claimants(signin_issuer, signin_keys)
    app = FastAPI(**BARE)

    @app.get("/staff")
    def staff(who: Annotated[Principal, Depends(signin.principal())]) -> str:
        return "staff"

    @app.get("/claimant")
    def claimant(who: Annotated[Principal, Depends(other.principal())]) -> str:
        return "claimant"

    assert report_for(app, "GET /staff").populations == frozenset({"staff"})
    assert report_for(app, "GET /claimant").populations == frozenset({"claimant"})


def test_a_router_dependency_gives_its_routes_its_role(signin: Signin) -> None:
    app = FastAPI(**BARE)
    router = APIRouter(dependencies=[Depends(signin.require_role("auditor"))])

    @router.get("/inside")
    def inside() -> str:
        return "inside"

    app.include_router(router)

    assert report_for(app, "GET /inside").roles == frozenset({"auditor"})


def test_a_router_included_twice_reports_the_inclusion_without_the_guard(
    signin: Signin,
) -> None:
    app = FastAPI(**BARE)
    router = APIRouter()

    @router.get("/shared")
    def shared() -> str:
        return "shared"

    guard = [Depends(signin.principal())]
    app.include_router(router, prefix="/guarded", dependencies=guard)
    app.include_router(router, prefix="/bare")

    assert unguarded_routes(app, open_routes=[]) == ["GET /bare/shared"]


# ── a marker made by hand is no guard ───────────────────────────────────────
@dataclass(frozen=True)
class LookAlike:
    population: str = "staff"
    role: str = "platform-admin"


def test_a_dependency_that_only_carries_the_marker_attribute_is_not_a_guard() -> None:
    def forged_true() -> str:
        return "forged"

    def forged_object() -> str:
        return "forged"

    setattr(forged_true, GUARD_ATTRIBUTE, True)
    setattr(forged_object, GUARD_ATTRIBUTE, LookAlike())
    app = FastAPI(**BARE)

    @app.get("/true")
    def by_true(value: Annotated[str, Depends(forged_true)]) -> str:
        return value

    @app.get("/object")
    def by_object(value: Annotated[str, Depends(forged_object)]) -> str:
        return value

    assert unguarded_routes(app, open_routes=[]) == ["GET /object", "GET /true"]


@pytest.mark.parametrize("forged_mark", [LookAlike(), "platform-admin", True])
def test_a_dependency_that_only_carries_the_role_attribute_demands_no_role(
    forged_mark: object,
) -> None:
    def forged() -> str:
        return "forged"

    setattr(forged, ROLE_ATTRIBUTE, forged_mark)
    app = FastAPI(**BARE)

    @app.get("/forged")
    def route(value: Annotated[str, Depends(forged)]) -> str:
        return value

    report = report_for(app, "GET /forged")
    assert (report.guarded, report.roles) == (False, frozenset())


# ── routes the walk used to lose, and ones it names ─────────────────────────
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_a_route_that_answers_only_head_or_options_is_reported(
    method: str, signin: Signin
) -> None:
    app = FastAPI(**BARE)

    @app.api_route("/only", methods=[method])
    def only() -> str:
        return "only"

    assert unguarded_routes(app, open_routes=[]) == [f"{method} /only"]
    assert report_for(app, f"{method} /only").methods == (method,)


def test_a_guarded_head_only_route_is_reported_with_its_guard(signin: Signin) -> None:
    app = FastAPI(**BARE)

    guard = [Depends(signin.principal())]

    @app.api_route("/only", methods=["HEAD"], dependencies=guard)
    def only() -> str:
        return "only"

    assert unguarded_routes(app, open_routes=[]) == []
    assert report_for(app, "HEAD /only").guarded


def test_a_get_route_is_not_also_listed_under_the_head_starlette_adds(
    signin: Signin,
) -> None:
    app = FastAPI(**BARE)
    app.router.routes.append(Route("/plain", lambda request: None, methods=["GET"]))

    assert unguarded_routes(app, open_routes=[]) == ["GET /plain"]
    assert report_for(app, "GET /plain").methods == ("GET",)


def test_a_mount_and_a_websocket_are_each_named_by_one_label(signin: Signin) -> None:
    app = FastAPI(**BARE)
    app.mount("/static", Starlette())

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        await websocket.close()

    assert unguarded_routes(app, open_routes=[]) == ["MOUNT /static", "WEBSOCKET /ws"]
    assert unguarded_routes(app, open_routes=["MOUNT /static", "WEBSOCKET /ws"]) == []
    assert report_for(app, "MOUNT /static").methods == ()
    assert report_for(app, "WEBSOCKET /ws").methods == ()
    # The docstring names the same two labels.
    assert '"MOUNT /path"' in unguarded_routes.__doc__  # type: ignore[operator]
    assert '"WEBSOCKET /path"' in unguarded_routes.__doc__  # type: ignore[operator]


def test_a_route_for_every_method_is_named_as_any(signin: Signin) -> None:
    class Everything:
        def __init__(self, scope: Any, receive: Any, send: Any) -> None:
            raise NotImplementedError

    app = FastAPI(**BARE)
    app.router.routes.append(Route("/everything", Everything))

    assert unguarded_routes(app, open_routes=[]) == ["ANY /everything"]


# ── a dependency override ───────────────────────────────────────────────────
def test_a_dependency_override_is_reported_though_the_table_shows_the_guard(
    signin: Signin,
) -> None:
    app = FastAPI(**BARE)

    @app.get("/guarded")
    def guarded(who: Annotated[Principal, Depends(signin.principal())]) -> str:
        return "let in"

    assert unguarded_routes(app, open_routes=[]) == []

    app.dependency_overrides[signin.principal()] = lambda: None
    found = unguarded_routes(app, open_routes=["GET /guarded"])

    assert [line.split()[0] for line in found] == ["DEPENDENCY_OVERRIDE"]
    # The route table still says guarded; the override is why it is not.
    assert report_for(app, "GET /guarded").guarded
    assert TestClient(app).get("/guarded").status_code == 200


def test_the_closed_list_cannot_allow_an_override(signin: Signin) -> None:
    app = FastAPI(**BARE)
    app.dependency_overrides[signin.principal()] = lambda: None
    [label] = unguarded_routes(app, open_routes=[])

    assert unguarded_routes(app, open_routes=[label]) == [label]


def test_an_app_without_overrides_reports_none(signin: Signin) -> None:
    app = FastAPI(**BARE)

    assert app.dependency_overrides == {}
    assert unguarded_routes(app, open_routes=[]) == []
