"""The Claims API with the staff sign-in on (S021, Y4): which routes are guarded
and by what, walked on the production factory with no dependency override.

The closed list of open routes is written here, in the test. A route added
later is a failure of the first test until a person decides, on purpose, that it
is open."""

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from staffsigninsupport import (
    CLAIM,
    StaffRig,
    assert_reached_the_handler,
    browser,
    build_app,
    send,
    settings,
)

from meridian.platform.common.env import SettingsError
from meridian.platform.common.signinguard import route_reports, unguarded_routes
from meridian.workloads.claims_triage.app import create_app

# Every route that no sign-in dependency guards. Each is written out: a new route
# is not open until it is added here.
OPEN = {
    # FastAPI's own, and the liveness probe.
    "GET /openapi.json",
    "GET /docs",
    "GET /docs/oauth2-redirect",
    "GET /redoc",
    "GET /healthz",
    # Not guarded by this contract: a claim is filed, withdrawn, given documents
    # and files without a sign-in until the claimants have theirs (S093).
    "POST /claims",
    "POST /claims/{claim_id}/withdrawal",
    "POST /claims/{claim_id}/documents",
    "POST /claims/{claim_id}/files",
    # The claimant's pages and their upload twin: S093.
    "POST /claimant/claims/{claim_id}/files",  # S093
    "GET /claimant/claims",  # S093
    "POST /claimant/claims/lookup",  # S093
    "POST /claimant/claims",  # S093
    "GET /claimant/claims/{claim_id}",  # S093
    "POST /claimant/claims/{claim_id}/documents",  # S093
    "POST /claimant/claims/{claim_id}/withdrawal",  # S093
    # The stylesheet holds no data.
    "GET /adjuster/static/adjuster.css",
    # The flow itself: it is how a person gets a session.
    "GET /auth/start",
    "GET /auth/callback",
    "GET /auth/failed",
    "POST /auth/sign-out",
}
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
SAME_ORIGIN = {"Origin": "http://meridian.localhost:8088"}
OTHER_SITE = {"Origin": "https://other-site.example"}
RUN = "0b1f1c2e-6f0a-4f6e-8f1a-2f0c3b8a9d10"
FORM = {"decision": "approve", "run": ""}


@pytest.fixture(scope="module")
def signer() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(65537, 2048)


@pytest.fixture
def rig(signer: rsa.RSAPrivateKey) -> StaffRig:
    return StaffRig(signer)


@pytest.fixture
def app(rig: StaffRig) -> FastAPI:
    return build_app(rig)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return browser(app)


# The JSON form: a script's routes, answered as JSON, with a bearer token.
JSON_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", f"/adjuster/claims/{CLAIM}/proposal", {}),
    ("POST", f"/claims/{CLAIM}/decision", {"json": {"decision": "approve"}}),
    ("POST", f"/claims/{CLAIM}/triage", {"json": {}}),
    ("POST", f"/claims/{CLAIM}/brief", {"json": {}}),
    (
        "POST",
        f"/claims/{CLAIM}/brief/decision",
        {"json": {"decision": "approve", "run": RUN}},
    ),
    ("GET", f"/claims/{CLAIM}/brief", {}),
]
# The page form: the adjuster's pages and the download, answered as pages.
PAGE_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/adjuster/claims", {}),
    ("GET", f"/adjuster/claims/{CLAIM}", {}),
    ("GET", f"/adjuster/claims/{CLAIM}/files/{RUN}", {"headers": SAME_ORIGIN}),
    ("HEAD", f"/adjuster/claims/{CLAIM}/files/{RUN}", {"headers": SAME_ORIGIN}),
    (
        "POST",
        f"/adjuster/claims/{CLAIM}/decision",
        {"data": FORM, "headers": SAME_ORIGIN},
    ),
    (
        "POST",
        f"/adjuster/claims/{CLAIM}/triage",
        {"data": {"run": ""}, "headers": SAME_ORIGIN},
    ),
]
ids = [f"{method} {path.replace(CLAIM, '{claim}')}" for method, path, _ in JSON_ROUTES]
page_ids = [
    f"{method} {path.replace(CLAIM, '{claim}')}" for method, path, _ in PAGE_ROUTES
]


# ── what the route table says ───────────────────────────────────────────────
def test_every_route_is_guarded_or_on_the_closed_list(app: FastAPI) -> None:
    assert unguarded_routes(app, OPEN) == []


def test_the_closed_list_names_only_routes_that_exist_and_are_unguarded(
    app: FastAPI,
) -> None:
    unguarded = {
        label
        for report in route_reports(app)
        if not report.guarded
        for label in report.labels
    }

    assert unguarded == OPEN


def test_every_state_changing_route_outside_the_list_demands_the_adjuster_role(
    app: FastAPI,
) -> None:
    checked: list[str] = []
    for report in route_reports(app):
        for label, method in zip(report.labels, report.methods, strict=True):
            if method in STATE_CHANGING and label not in OPEN:
                assert report.roles == frozenset({"adjuster"}), label
                assert report.populations == frozenset({"staff"}), label
                checked.append(label)

    # The scan is not blind: these are the six it found.
    assert sorted(checked) == [
        "POST /adjuster/claims/{claim_id}/decision",
        "POST /adjuster/claims/{claim_id}/triage",
        "POST /claims/{claim_id}/brief",
        "POST /claims/{claim_id}/brief/decision",
        "POST /claims/{claim_id}/decision",
        "POST /claims/{claim_id}/triage",
    ]


def test_every_guarded_route_demands_the_adjuster_role_and_nothing_else(
    app: FastAPI,
) -> None:
    guarded = [r for r in route_reports(app) if r.guarded]

    assert len(guarded) == 11  # six JSON routes, the download and four pages
    assert all(r.roles == frozenset({"adjuster"}) for r in guarded)
    assert all(r.populations == frozenset({"staff"}) for r in guarded)


def test_the_app_has_no_mount_no_websocket_and_no_override(app: FastAPI) -> None:
    labels = [label for r in route_reports(app) for label in r.labels]

    assert not [x for x in labels if x.split()[0] in ("MOUNT", "WEBSOCKET", "ANY")]
    assert app.dependency_overrides == {}


def test_the_download_is_off_with_its_switch_and_nothing_else_changes(
    rig: StaffRig,
) -> None:
    app = build_app(rig, downloads_enabled=False)

    assert all("/files/{file_id}" not in r.path for r in route_reports(app))
    assert unguarded_routes(app, OPEN) == []


# ── a bearer token reaches every JSON route, and only with the role ─────────
@pytest.mark.parametrize(("method", "path", "kwargs"), JSON_ROUTES, ids=ids)
def test_a_valid_bearer_with_the_role_is_let_through_every_json_route(
    client: TestClient, rig: StaffRig, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = send(client, method, path, bearer=rig.bearer(), **kwargs)

    # The guard passed: what answers is the database that refuses the connection.
    assert_reached_the_handler(response)


@pytest.mark.parametrize(("method", "path", "kwargs"), JSON_ROUTES, ids=ids)
def test_a_valid_bearer_without_the_role_is_a_403_on_every_json_route(
    client: TestClient, rig: StaffRig, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = send(client, method, path, bearer=rig.bearer(("auditor",)), **kwargs)

    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}


@pytest.mark.parametrize(("method", "path", "kwargs"), JSON_ROUTES, ids=ids)
def test_no_credential_is_a_401_with_the_challenge_on_every_json_route(
    client: TestClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = send(client, method, path, **kwargs)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "request refused"}


# ── a session cookie reaches every page route ───────────────────────────────
@pytest.mark.parametrize(("method", "path", "kwargs"), PAGE_ROUTES, ids=page_ids)
def test_a_session_cookie_with_the_role_is_let_through_every_page_route(
    client: TestClient, rig: StaffRig, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = send(client, method, path, cookie=rig.cookie(), **kwargs)

    if method == "HEAD":
        like = send(client, "GET", path, cookie=rig.cookie(), **kwargs)
        assert_reached_the_handler(response, like=like)
    else:
        assert_reached_the_handler(response)


@pytest.mark.parametrize(("method", "path", "kwargs"), PAGE_ROUTES, ids=page_ids)
def test_a_session_without_the_role_is_the_403_page_on_every_page_route(
    client: TestClient, rig: StaffRig, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = send(client, method, path, cookie=rig.cookie(("auditor",)), **kwargs)

    assert response.status_code == 403
    if method != "HEAD":
        assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize(("method", "path", "kwargs"), PAGE_ROUTES, ids=page_ids)
def test_no_credential_on_a_page_route_is_a_redirect_to_sign_in_or_the_401_page(
    client: TestClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = send(client, method, path, **kwargs)

    if method in ("GET", "HEAD"):
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "/auth/start?return_to=/adjuster/"
        )
    else:
        assert response.status_code == 401
        assert "text/html" in response.headers["content-type"]
    assert "www-authenticate" not in response.headers


def test_a_bearer_token_also_reaches_a_page_route(
    client: TestClient, rig: StaffRig
) -> None:
    response = send(client, "GET", "/adjuster/claims", bearer=rig.bearer())

    assert_reached_the_handler(response)


# ── a cookie does not travel across sites on a route that changes state ─────
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [route for route in JSON_ROUTES if route[0] == "POST"],
    ids=[i for i, route in zip(ids, JSON_ROUTES, strict=True) if route[0] == "POST"],
)
def test_a_cookie_on_a_state_changing_json_route_from_another_site_is_a_403(
    client: TestClient, rig: StaffRig, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    refused = send(
        client, method, path, cookie=rig.cookie(), headers=OTHER_SITE, **kwargs
    )
    fetch = send(
        client,
        method,
        path,
        cookie=rig.cookie(),
        headers={"Sec-Fetch-Site": "cross-site"},
        **kwargs,
    )
    own = send(client, method, path, cookie=rig.cookie(), headers=SAME_ORIGIN, **kwargs)
    bare = send(client, method, path, cookie=rig.cookie(), **kwargs)
    scripted = send(
        client, method, path, bearer=rig.bearer(), headers=OTHER_SITE, **kwargs
    )

    assert refused.status_code == 403
    assert refused.json() == {"detail": "request refused"}
    assert fetch.status_code == 403
    # The same site, a request that names no site (a script with a cookie jar) and
    # a bearer token from anywhere are let through: only the cookie is ambient.
    for passed in (own, bare, scripted):
        assert_reached_the_handler(passed)


# ── the body is read before the guard ───────────────────────────────────────
def test_a_malformed_json_body_is_a_422_without_a_credential_and_a_bad_field_a_401(
    client: TestClient,
) -> None:
    path = f"/claims/{CLAIM}/decision"
    headers = {"Content-Type": "application/json"}

    malformed = send(client, "POST", path, content=b'{"decision": ', headers=headers)
    bad_field = send(client, "POST", path, json={"decision": "not-a-decision"})
    well_formed = send(client, "POST", path, json={"decision": "approve"})

    # The framework parses the body before it resolves the guard: all that is
    # learned is that the body is not valid JSON, and the answer says no more.
    assert malformed.status_code == 422
    assert "request refused" not in malformed.text
    assert "approve" not in malformed.text
    # A body that parses meets the guard first, whatever its fields hold.
    assert bad_field.status_code == 401
    assert well_formed.status_code == 401


def test_a_cookie_on_a_read_of_the_json_proposal_is_not_asked_for_its_origin(
    client: TestClient, rig: StaffRig
) -> None:
    response = send(
        client,
        "GET",
        f"/adjuster/claims/{CLAIM}/proposal",
        cookie=rig.cookie(),
        headers=OTHER_SITE,
    )

    assert_reached_the_handler(response)


# ── the form posts of the pages keep their own check, first ─────────────────
def test_a_form_post_from_another_site_is_the_403_page_before_it_is_a_401(
    client: TestClient,
) -> None:
    response = send(
        client,
        "POST",
        f"/adjuster/claims/{CLAIM}/decision",
        data=FORM,
        headers=OTHER_SITE,
    )

    assert response.status_code == 403
    assert "the request came from another site" in response.text


# ── the unguarded routes are still unguarded ────────────────────────────────
def test_the_routes_the_contract_leaves_open_answer_without_a_credential(
    client: TestClient,
) -> None:
    health = send(client, "GET", "/healthz")
    stylesheet = send(client, "GET", "/adjuster/static/adjuster.css")
    withdrawal = send(client, "POST", f"/claims/{CLAIM}/withdrawal", json={})
    lookup = send(client, "GET", "/claimant/claims")

    assert health.status_code == 200
    assert stylesheet.status_code == 200
    assert_reached_the_handler(withdrawal)
    assert lookup.status_code == 200


# ── the switch and the object agree ─────────────────────────────────────────
def test_the_switch_on_without_the_object_stops_the_build(rig: StaffRig) -> None:
    with pytest.raises(SettingsError) as raised:
        create_app(settings(signin="staff"))

    assert "MERIDIAN_SIGNIN" in str(raised.value)


def test_the_object_without_the_switch_stops_the_build(rig: StaffRig) -> None:
    with pytest.raises(SettingsError) as raised:
        create_app(settings(signin="off"), staff_signin=rig.staff())

    assert "MERIDIAN_SIGNIN" in str(raised.value)
