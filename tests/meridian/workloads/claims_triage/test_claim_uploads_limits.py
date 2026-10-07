"""The upload route's limits and switches that need no database (S070, T-38):
the per-route body limit and what keeps every other route at 64 KiB, the
settings, the signatures, and the other services' limits unchanged."""

import asyncio
import re
from collections.abc import Iterator

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from servicesupport import REGISTRY_DIR, REPO_ROOT, multipart_body

from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import (
    GATEWAY_BODY_LIMIT_BYTES,
    SMALL_BODY_LIMIT_BYTES,
    BodyLimitMiddleware,
)
from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.runtime.app import create_app as create_runtime
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage import claim_files as claim_files_module
from meridian.workloads.claims_triage import uploads as upload_module
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.claimant_uploads import TWIN_PATH
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.uploads import (
    DEFAULT_CEILING_BYTES,
    DEFAULT_CEILING_ROWS,
    ENVELOPE_ALLOWANCE_BYTES,
    MAX_CEILING_BYTES,
    MAX_CEILING_ROWS,
    MIN_CEILING_BYTES,
    MIN_CEILING_ROWS,
    UPLOAD_BODY_LIMIT_BYTES,
    UPLOAD_PATH,
    UPLOADS_CEILING_ENV,
    UPLOADS_ENABLED_ENV,
    UPLOADS_ROWS_ENV,
    ceiling_bytes_of,
    ceiling_rows_of,
    sniff_media_type,
    uploads_enabled_of,
)

DSN = "postgresql://claims_api@db.invalid/meridian"
MIB = 1024 * 1024
ENV = {
    "MERIDIAN_RUNTIME_URL": "http://runtime.invalid:8080",
    "MERIDIAN_DATABASE_URL": DSN,
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NO_KIND = [("file", "f.pdf", "application/pdf", b"%PDF-1.4")]


def claims_app(*, uploads: bool) -> FastAPI:
    return create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=DSN,
            uploads_enabled=uploads,
        )
    )


def methods_and_paths(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) the app answers, read from the app itself, so a
    route added later cannot be left out of the tests below."""
    pairs = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
        if method not in ("HEAD", "OPTIONS")
    }
    return sorted(pairs)


APP_WITH_UPLOADS = claims_app(uploads=True)
ROUTES = methods_and_paths(APP_WITH_UPLOADS)
UPLOAD_ROUTE = ("POST", UPLOAD_PATH)
# The JSON route's HTML twin (F4a) has the upload limit as well; its own tests
# are in ``test_claimant_upload_twin.py``.
TWIN_ROUTE = ("POST", TWIN_PATH)
FILE_ROUTES = (UPLOAD_ROUTE, TWIN_ROUTE)


def concrete(path: str) -> str:
    """The path with its parameters filled in with a claim's ID."""
    return re.sub(r"\{[^}]+\}", "CLM-0001", path)


# ── the signatures ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("content", "media_type"),
    [
        (b"%PDF-", "application/pdf"),
        (b"%PDF-1.7\n...", "application/pdf"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\xff\xd8\xff\xdb\x00", "image/jpeg"),
        (PNG_SIGNATURE, "image/png"),
        (PNG_SIGNATURE + b"\x00\x00\x00\rIHDR", "image/png"),
        # The first bytes decide, whatever follows (a polyglot is its first type).
        (PNG_SIGNATURE + b"%PDF-", "image/png"),
        (b"%PDF-" + PNG_SIGNATURE, "application/pdf"),
        (b"\xff\xd8\xff%PDF-", "image/jpeg"),
    ],
)
def test_the_type_is_the_one_the_first_bytes_name(
    content: bytes, media_type: str
) -> None:
    assert sniff_media_type(content) == media_type


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"%PDF",
        b" %PDF-",
        b"%pdf-",
        b"\xff\xd8",
        b"\xff\xd9\xff",
        PNG_SIGNATURE[:-1],
        b"\x89png\r\n\x1a\n",
        b"GIF89a",
        b"<svg",
    ],
)
def test_anything_else_has_no_type(content: bytes) -> None:
    assert sniff_media_type(content) is None


# ── the settings ────────────────────────────────────────────────────────────
def test_uploads_are_off_and_the_ceilings_are_128_mib_and_2000_rows_by_default() -> (
    None
):
    built = ClaimsSettings.from_env(ENV)

    assert built.uploads_enabled is False
    assert built.uploads_ceiling_bytes == DEFAULT_CEILING_BYTES == 128 * MIB
    assert built.uploads_ceiling_rows == DEFAULT_CEILING_ROWS == 2000


def test_the_variables_are_named_for_the_chart() -> None:
    # The chart renders the first (``on``) and leaves the other to its default.
    assert UPLOADS_ENABLED_ENV == "MERIDIAN_CLAIMS_UPLOADS"
    assert UPLOADS_CEILING_ENV == "MERIDIAN_UPLOADS_CEILING_BYTES"


@pytest.mark.parametrize(("raw", "expected"), [("on", True), ("off", False)])
def test_the_switch_reads_on_and_off_and_nothing_else(raw: str, expected: bool) -> None:
    built = ClaimsSettings.from_env(ENV | {UPLOADS_ENABLED_ENV: raw})

    assert built.uploads_enabled is expected


@pytest.mark.parametrize(
    "raw", ["", "On", "ON", "1", "yes", "true", "false", " on", "o", "enabled"]
)
def test_a_switch_that_is_neither_on_nor_off_stops_the_start_naming_the_variable(
    raw: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        uploads_enabled_of(raw)

    # The message is one fixed sentence: it quotes nothing that was set.
    assert str(raised.value) == f"{UPLOADS_ENABLED_ENV} must be on or off"


def test_the_ceiling_is_read_in_whole_bytes() -> None:
    built = ClaimsSettings.from_env(ENV | {UPLOADS_CEILING_ENV: str(8 * MIB)})

    assert built.uploads_ceiling_bytes == 8 * MIB


@pytest.mark.parametrize("value", [MIN_CEILING_BYTES, MAX_CEILING_BYTES])
def test_the_floor_and_the_cap_themselves_are_accepted(value: int) -> None:
    assert ceiling_bytes_of(str(value)) == value
    assert (
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=DSN,
            uploads_ceiling_bytes=value,
        ).uploads_ceiling_bytes
        == value
    )


@pytest.mark.parametrize(
    "raw",
    [
        str(MIN_CEILING_BYTES - 1),
        str(MAX_CEILING_BYTES + 1),
        "0",
        "-1",
        "",
        "256MiB",
        "1e9",
        "268435456.0",
        "٢٥٦٠٠٠٠٠٠",  # Arabic-Indic digits: ``int`` would read them
        "9" * 40,
    ],
)
def test_a_ceiling_outside_the_floor_and_the_cap_stops_the_start_without_its_value(
    raw: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        ceiling_bytes_of(raw)

    # One fixed sentence, whatever was set: the floor and the cap are in it, the
    # value is not.
    assert str(raised.value) == (
        f"{UPLOADS_CEILING_ENV} must be a whole number of bytes "
        f"from {MIN_CEILING_BYTES} to {MAX_CEILING_BYTES}"
    )


def test_the_floor_holds_one_claims_worth_and_the_cap_is_under_the_kind_database() -> (
    None
):
    assert MIN_CEILING_BYTES == 3 * MIB
    assert MAX_CEILING_BYTES == 256 * MIB  # the kind database is 2 Gi


def test_settings_built_in_code_are_held_to_the_same_floor_and_cap() -> None:
    for value in (MIN_CEILING_BYTES - 1, MAX_CEILING_BYTES + 1):
        with pytest.raises(ValueError):
            ClaimsSettings(
                runtime_url="http://runtime.invalid",
                database_url=DSN,
                uploads_ceiling_bytes=value,
            )


# ── the route's own limit: 1 MiB, the envelope and nothing more ─────────────
def test_the_edges_buffer_for_the_route_is_the_apps_limit() -> None:
    """The chart's second route buffers what the app's limit says (F3 wrote its
    default once, here it is held equal): the edge and the app refuse together."""
    values = yaml.safe_load(
        (REPO_ROOT / "infra" / "helm" / "meridian" / "values.yaml").read_text()
    )

    uploads = values["route"]["uploads"]

    assert uploads["requestBufferLimit"] == str(UPLOAD_BODY_LIMIT_BYTES) == "1126400"
    assert uploads["enabled"] is False


def test_the_envelope_allowance_holds_the_longest_honest_envelope() -> None:
    """The arithmetic in the constant's comment, as a body: a 70-character
    boundary (the format's longest), a file name of 255 bytes, a declared type of
    127 characters, and the one field of the longest kind."""
    parts = [
        ("kind", None, None, b"accident_statement"),
        ("file", "n" * 255, "t/" + "x" * 125, b""),
    ]
    body, _ = multipart_body(parts, boundary="b" * 70)

    assert len(body) <= ENVELOPE_ALLOWANCE_BYTES


def upload_client(*, uploads: bool = True) -> TestClient:
    return TestClient(claims_app(uploads=uploads), raise_server_exceptions=False)


def chunked(body: bytes, size: int = 32 * 1024) -> Iterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


MULTIPART = {"Content-Type": "multipart/form-data; boundary=b"}


def test_a_declared_length_over_the_routes_limit_is_413() -> None:
    response = upload_client().post(
        concrete(UPLOAD_PATH),
        content=b"x" * (UPLOAD_BODY_LIMIT_BYTES + 1),
        headers=MULTIPART,
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "the request body is too large"}


def test_a_declared_length_at_the_routes_limit_is_not_stopped_by_the_limit() -> None:
    envelope = len(multipart_body(NO_KIND)[0]) - len(NO_KIND[0][3])
    data = b"x" * (UPLOAD_BODY_LIMIT_BYTES - envelope)
    body, headers = multipart_body([("file", "f.pdf", "application/pdf", data)])
    assert len(body) == UPLOAD_BODY_LIMIT_BYTES

    response = upload_client().post(
        concrete(UPLOAD_PATH), content=body, headers=headers
    )

    # No kind field: the form's own 422, which the limit let through.
    assert response.status_code == 422


def test_a_chunked_body_over_the_routes_limit_is_413_and_at_it_is_not() -> None:
    envelope = len(multipart_body(NO_KIND)[0]) - len(NO_KIND[0][3])
    at = multipart_body(
        [
            (
                "file",
                "f.pdf",
                "application/pdf",
                b"x" * (UPLOAD_BODY_LIMIT_BYTES - envelope),
            )
        ]
    )[0]
    over = multipart_body(
        [
            (
                "file",
                "f.pdf",
                "application/pdf",
                b"x" * (UPLOAD_BODY_LIMIT_BYTES - envelope + 1),
            )
        ]
    )[0]
    assert (len(at), len(over)) == (
        UPLOAD_BODY_LIMIT_BYTES,
        UPLOAD_BODY_LIMIT_BYTES + 1,
    )
    client = upload_client()
    path = concrete(UPLOAD_PATH)

    stopped = client.post(path, content=chunked(over), headers=MULTIPART)
    let_through = client.post(path, content=chunked(at), headers=MULTIPART)

    assert stopped.status_code == 413
    assert let_through.status_code == 422


# ── every other route keeps 64 KiB ──────────────────────────────────────────
def test_the_upload_route_is_in_the_app_the_routes_are_read_from() -> None:
    assert UPLOAD_ROUTE in ROUTES
    assert TWIN_ROUTE in ROUTES
    assert ("POST", "/claims") in ROUTES
    assert ("POST", "/claims/{claim_id}/documents") in ROUTES


@pytest.mark.parametrize(
    ("method", "path"),
    [route for route in ROUTES if route not in FILE_ROUTES],
    ids=lambda value: str(value),
)
# mutation: the per-route limit applied to all routes
def test_every_other_route_answers_413_just_over_64_kib(method: str, path: str) -> None:
    client = upload_client()

    over = client.request(
        method, concrete(path), content=b"x" * (SMALL_BODY_LIMIT_BYTES + 1)
    )

    assert over.status_code == 413


def test_every_other_route_answers_a_streamed_body_just_over_64_kib_with_413() -> None:
    client = upload_client()
    body = b"x" * (SMALL_BODY_LIMIT_BYTES + 1)

    for path in ("/claims", "/claims/CLM-0001/documents", "/claims/CLM-0001/decision"):
        response = client.post(
            path, content=chunked(body), headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 413, path


@pytest.mark.parametrize(
    ("method", "path"),
    [route for route in ROUTES if route not in FILE_ROUTES],
    ids=lambda value: str(value),
)
def test_no_other_route_is_stopped_at_64_kib_less_than_it_was(
    method: str, path: str
) -> None:
    """A body of exactly 64 KiB is still not the limit's to refuse."""
    response = upload_client().request(
        method, concrete(path), content=b"x" * SMALL_BODY_LIMIT_BYTES
    )

    assert response.status_code != 413


def test_the_upload_route_alone_takes_more_than_64_kib() -> None:
    response = upload_client().post(
        concrete(UPLOAD_PATH),
        content=b"x" * (SMALL_BODY_LIMIT_BYTES + 1),
        headers=MULTIPART,
    )

    assert response.status_code != 413


# ── the switch: off, the route is not there and its limit is not either ─────
def test_with_the_switch_off_the_route_answers_404_and_is_no_route() -> None:
    app = claims_app(uploads=False)
    client = TestClient(app, raise_server_exceptions=False)
    body, headers = multipart_body([("kind", None, None, b"photos"), *NO_KIND])

    response = client.post(concrete(UPLOAD_PATH), content=body, headers=headers)

    assert response.status_code == 404
    assert UPLOAD_ROUTE not in methods_and_paths(app)


def test_with_the_switch_off_the_upload_path_keeps_the_64_kib_limit() -> None:
    response = upload_client(uploads=False).post(
        concrete(UPLOAD_PATH),
        content=b"x" * (SMALL_BODY_LIMIT_BYTES + 1),
        headers=MULTIPART,
    )

    assert response.status_code == 413


def test_the_switch_is_off_by_default_in_code_too() -> None:
    app = create_app(
        ClaimsSettings(runtime_url="http://runtime.invalid", database_url=DSN)
    )

    assert UPLOAD_ROUTE not in methods_and_paths(app)


# ── which apps pass which limits ────────────────────────────────────────────
def limit_configuration(app: FastAPI) -> tuple[int, object]:
    (found,) = [m for m in app.user_middleware if m.cls is BodyLimitMiddleware]
    return found.kwargs["max_bytes"], found.kwargs.get("route_limits")


def test_the_claims_api_passes_two_entries_when_uploads_are_on_and_none_when_off() -> (
    None
):
    assert limit_configuration(APP_WITH_UPLOADS) == (
        SMALL_BODY_LIMIT_BYTES,
        {
            UPLOAD_ROUTE: UPLOAD_BODY_LIMIT_BYTES,
            TWIN_ROUTE: UPLOAD_BODY_LIMIT_BYTES,
        },
    )
    assert limit_configuration(claims_app(uploads=False)) == (
        SMALL_BODY_LIMIT_BYTES,
        None,
    )


def test_the_gateway_and_the_runtime_pass_no_route_limit_and_keep_their_own() -> None:
    gateway = create_gateway(
        GatewaySettings(
            registry_dir=REGISTRY_DIR,
            mode="replay",
            environment="test",
            database_url=DSN,
        )
    )
    runtime = create_runtime(
        RuntimeSettings(
            registry_dir=REGISTRY_DIR,
            gateway_url="http://gateway.invalid",
            database_url=DSN,
        )
    )

    assert limit_configuration(gateway) == (GATEWAY_BODY_LIMIT_BYTES, None)
    assert limit_configuration(runtime) == (SMALL_BODY_LIMIT_BYTES, None)


# ── a post another site made is refused before a byte is read (T-70) ────────
def a_form_post(headers: dict[str, str]):
    body, content_type = multipart_body(
        [("kind", None, None, b"photos"), *NO_KIND], boundary="b"
    )
    return upload_client().post(
        concrete(UPLOAD_PATH), content=body, headers=content_type | headers
    )


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
def test_a_post_from_another_site_is_403_before_the_form_is_read(
    headers: dict[str, str],
) -> None:
    pulled: list[int] = []
    body, content_type = multipart_body(
        [("kind", None, None, b"photos"), *NO_KIND], boundary="b"
    )

    async def chunks():
        pulled.append(1)
        yield body

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=APP_WITH_UPLOADS, raise_app_exceptions=False
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                concrete(UPLOAD_PATH), content=chunks(), headers=content_type | headers
            )

    response = asyncio.run(scenario())

    assert response.status_code == 403
    assert response.json() == {"detail": "the request came from another site"}
    assert pulled == []  # the app never asked for a chunk of the body


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
    response = a_form_post(headers)

    # Past the origin check, the form is read and the database is asked: there is
    # none here, so the answer is the 503 of a database that is not there.
    assert response.status_code == 503


# ── the row ceiling beside the byte ceiling ─────────────────────────────────
def test_the_row_ceiling_is_read_in_whole_rows_from_its_variable() -> None:
    built = ClaimsSettings.from_env(ENV | {UPLOADS_ROWS_ENV: "500"})

    assert UPLOADS_ROWS_ENV == "MERIDIAN_UPLOADS_CEILING_ROWS"
    assert built.uploads_ceiling_rows == 500


@pytest.mark.parametrize("value", [MIN_CEILING_ROWS, MAX_CEILING_ROWS])
def test_the_row_floor_and_cap_themselves_are_accepted(value: int) -> None:
    assert ceiling_rows_of(str(value)) == value
    settings = ClaimsSettings(
        runtime_url="http://runtime.invalid",
        database_url=DSN,
        uploads_ceiling_rows=value,
    )
    assert settings.uploads_ceiling_rows == value


@pytest.mark.parametrize(
    "raw",
    [str(MIN_CEILING_ROWS - 1), str(MAX_CEILING_ROWS + 1), "0", "-5", "", "2k", "1e3"],
)
def test_a_row_ceiling_outside_the_floor_and_cap_stops_the_start_without_its_value(
    raw: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        ceiling_rows_of(raw)

    assert str(raised.value) == (
        f"{UPLOADS_ROWS_ENV} must be a whole number of rows "
        f"from {MIN_CEILING_ROWS} to {MAX_CEILING_ROWS}"
    )


def test_the_row_floor_is_one_claims_five_files() -> None:
    assert MIN_CEILING_ROWS == upload_module.MAX_FILES_PER_CLAIM == 5


# ── named columns everywhere, and the content in two queries only ───────────
def sql_constants() -> dict[str, str]:
    """The queries of the upload route and of the list the pages read (which is
    in its own module: ``uploads.py`` cannot import the pages' modules back)."""
    return {
        name: value
        for module in (upload_module, claim_files_module)
        for name, value in vars(module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


def test_every_query_names_its_columns() -> None:
    queries = sql_constants()

    assert "FILE_CONTENT_SQL" in queries
    for name, text in queries.items():
        assert "*" not in text.replace("count(*)", ""), name
        assert "::text" not in text, name


def test_the_content_is_selected_only_by_the_download_query_by_claim_and_file() -> None:
    mentions = {
        name
        for name, text in sql_constants().items()
        if re.search(r"\bcontent\b", text)
    }

    # The insert writes it and the download reads it; nothing else names it.
    assert mentions == {"INSERT_FILE_SQL", "FILE_CONTENT_SQL"}
    text = upload_module.FILE_CONTENT_SQL
    assert text.startswith("SELECT media_type, content FROM claims.claim_files")
    assert "claim_id = %s AND file_id = %s" in text
