"""The adjuster's file download is behind a switch of its own (S070 F4b).

``MERIDIAN_CLAIMS_DOWNLOADS`` is ``on`` or ``off``, unset is off and anything
else stops the start; it needs the uploads switch on. With it off the route does
not exist and the adjuster's list shows no link, byte for byte as F4a rendered
it. The pages' middleware overwrites the headers it owns; the download's own
policy survives it only for a response the route marked, and no other route of
the pages can keep a policy of its own. None of these tests needs a database.
"""

import asyncio
import hashlib
import inspect
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from servicesupport import claim_with_id
from workloads.claims_triage.test_adjuster_pages import SECURITY_HEADERS, Page

from meridian.platform.common.env import SettingsError
from meridian.workloads.claims_triage import adjuster, file_download, page_security
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.claim_files import FileSummary
from meridian.workloads.claims_triage.file_download import (
    DOWNLOAD_PATH,
    DOWNLOAD_POLICY,
    DOWNLOADS_ENABLED_ENV,
    downloads_enabled_of,
)
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.uploads import UPLOADS_ENABLED_ENV

CLAIM = "CLM-9301"
FILE = uuid.UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
MOMENT = datetime(2026, 10, 2, 8, 30, 15, tzinfo=UTC)
ENV = {
    "MERIDIAN_RUNTIME_URL": "http://runtime.invalid",
    "MERIDIAN_DATABASE_URL": "postgresql://claims_api@db.invalid/meridian",
}
SENTENCE = "These files come from an unauthenticated upload and are not scanned."
POLICY_HEADER = "content-security-policy"


# ── the switch: four states and what is not one ─────────────────────────────
def test_the_variable_is_named_and_parsed_as_the_uploads_switch_is() -> None:
    assert DOWNLOADS_ENABLED_ENV == "MERIDIAN_CLAIMS_DOWNLOADS"
    assert downloads_enabled_of(None) is False
    assert downloads_enabled_of("off") is False
    assert downloads_enabled_of("on") is True


@pytest.mark.parametrize(
    "raw", ["", "ON", "On", "true", "1", "yes", " on", "on ", "enabled", "off\n"]
)
def test_any_other_word_stops_the_start_with_a_fixed_sentence(raw: str) -> None:
    with pytest.raises(SettingsError) as raised:
        downloads_enabled_of(raw)

    assert str(raised.value) == f"{DOWNLOADS_ENABLED_ENV} must be on or off"


@pytest.mark.parametrize(
    ("uploads", "downloads", "outcome"),
    [
        (None, None, (False, False)),
        ("off", "off", (False, False)),
        ("on", None, (True, False)),
        ("on", "off", (True, False)),
        ("on", "on", (True, True)),
        ("off", "on", "refused"),
        (None, "on", "refused"),
    ],
)
def test_the_four_states_and_downloads_on_with_uploads_off_is_refused_at_start(
    uploads: str | None, downloads: str | None, outcome: Any
) -> None:
    environ = dict(ENV)
    if uploads is not None:
        environ[UPLOADS_ENABLED_ENV] = uploads
    if downloads is not None:
        environ[DOWNLOADS_ENABLED_ENV] = downloads

    if outcome == "refused":
        with pytest.raises(SettingsError) as raised:
            ClaimsSettings.from_env(environ)
        assert str(raised.value) == (
            f"{DOWNLOADS_ENABLED_ENV} is on, but {UPLOADS_ENABLED_ENV} is not: "
            "there is nothing to download without the uploads that store it"
        )
        return
    settings = ClaimsSettings.from_env(environ)
    assert (settings.uploads_enabled, settings.downloads_enabled) == outcome


def test_settings_built_in_code_refuse_the_same_pair() -> None:
    with pytest.raises(SettingsError):
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=ENV["MERIDIAN_DATABASE_URL"],
            uploads_enabled=False,
            downloads_enabled=True,
        )


def client(*, uploads: bool, downloads: bool) -> TestClient:
    settings = ClaimsSettings(
        runtime_url="http://runtime.invalid",
        database_url=ENV["MERIDIAN_DATABASE_URL"],
        uploads_enabled=uploads,
        downloads_enabled=downloads,
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def download_url(claim_id: str = CLAIM, file_id: uuid.UUID = FILE) -> str:
    return f"/adjuster/claims/{claim_id}/files/{file_id}"


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_with_downloads_off_the_route_does_not_exist(method: str) -> None:
    for uploads_on in (False, True):
        response = client(uploads=uploads_on, downloads=False).request(
            method, download_url()
        )

        assert response.status_code == 404
        if method == "GET":
            assert response.json() == {"detail": "Not Found"}


def test_with_downloads_on_the_route_exists_and_is_in_no_contract() -> None:
    app = client(uploads=True, downloads=True)

    paths = app.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/adjuster")]
    methods = {
        method
        for route in app.app.routes
        if getattr(route, "path", "") == DOWNLOAD_PATH
        for method in getattr(route, "methods", ())
    }
    assert methods == {"GET", "HEAD"}


def test_the_routes_path_is_the_adjusters_claim_path_with_files_and_a_file() -> None:
    assert DOWNLOAD_PATH == "/adjuster/claims/{claim_id}/files/{file_id}"


# ── the list: no link with the switch off, byte for byte what F4a rendered ──
def view(files: tuple[FileSummary, ...]) -> adjuster.ClaimView:
    return adjuster.ClaimView(
        claim_id=CLAIM,
        state="awaiting_adjuster",
        since=MOMENT,
        received_at=MOMENT,
        facts=adjuster.facts_of(claim_with_id(CLAIM)),
        proposal=None,
        proposal_note="no proposal is stored",
        decision=None,
        trail=(),
        files=files,
    )


TWO_FILES = (
    FileSummary(uuid.UUID(int=7), "photos", "image/png", 1234, "ab" * 32, MOMENT),
    FileSummary(
        uuid.UUID(int=8), "police_report", "application/pdf", 1014, "cd" * 32, MOMENT
    ),
)
# The claim page as F4a (a90f2b1) rendered it for these two views, taken from
# that tree before the page changed: the whole page, as a digest.
F4A_PAGE_SHA256 = {
    "with files": "af2d6ea1f703b92c3f5913ead330b4aa0db59ece455eabb0ef6aa2fe83e07136",
    "no files": "be785930a77a426d434aded5f81b0cbc92a16f9988b3249aa805f93584e8abc6",
}
F4A_FILES_SECTION = """\
<h2>Files the claimant sent</h2>
<table>
<thead>
<tr><th scope="col">Kind</th><th scope="col">Media type</th><th scope="col">Size</th><th scope="col">Received</th><th scope="col">SHA-256</th><th scope="col">Scan</th></tr>
</thead>
<tbody>
<tr><td>photos</td><td>image/png</td><td>1,234 bytes</td><td>2026-10-02 08:30:15 UTC</td><td><span title="abababababababababababababababababababababababababababababababab">abababababab</span></td><td>not scanned</td></tr>
<tr><td>police_report</td><td>application/pdf</td><td>1,014 bytes</td><td>2026-10-02 08:30:15 UTC</td><td><span title="cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd">cdcdcdcdcdcd</span></td><td>not scanned</td></tr>
</tbody>
</table>
<p>Malware scanning is designed, not implemented: no file listed here has been scanned.</p>

<h2>Triage proposal</h2>"""  # noqa: E501


def digest(page: str) -> str:
    return hashlib.sha256(page.encode()).hexdigest()


def test_with_downloads_off_the_page_is_byte_for_byte_what_f4a_rendered() -> None:
    with_files = adjuster.render_claim(view(TWO_FILES))
    no_files = adjuster.render_claim(view(()))
    explicit = adjuster.render_claim(view(TWO_FILES), downloads=False)

    assert digest(with_files) == F4A_PAGE_SHA256["with files"]
    assert digest(no_files) == F4A_PAGE_SHA256["no files"]
    assert explicit == with_files
    assert F4A_FILES_SECTION in with_files
    assert "<a " not in with_files.partition("<h2>Files")[2].partition("<h2>Tri")[0]
    assert SENTENCE not in with_files


def test_with_downloads_on_each_row_has_a_link_a_rel_and_not_scanned_beside_it() -> (
    None
):
    page = adjuster.render_claim(view(TWO_FILES), downloads=True)

    parsed = Page(page)
    links = [a for a in parsed.attributes("a") if "/files/" in str(a.get("href"))]
    assert [a["href"] for a in links] == [
        download_url(CLAIM, f.file_id) for f in TWO_FILES
    ]
    assert {a["rel"] for a in links} == {"noopener noreferrer"}
    rows = [r for r in parsed.rows if "not scanned" in r]
    assert len(rows) == 2
    assert all("not scanned download" in r for r in rows)
    assert page.count(SENTENCE) == 1
    # One sentence, above the table; the page's other sentence stays.
    assert page.index(SENTENCE) < page.index("<table>", page.index("Files the"))
    assert "Malware scanning is designed, not implemented" in page


def test_with_downloads_on_a_claim_with_no_files_shows_no_sentence_and_no_link() -> (
    None
):
    page = adjuster.render_claim(view(()), downloads=True)

    assert digest(page) == F4A_PAGE_SHA256["no files"]


def test_the_page_changes_in_the_files_section_alone_when_downloads_are_on() -> None:
    off = adjuster.render_claim(view(TWO_FILES))
    on = adjuster.render_claim(view(TWO_FILES), downloads=True)

    def around(page: str) -> tuple[str, str]:
        head, _, rest = page.partition("<h2>Files the claimant sent</h2>")
        return head, rest.partition("<h2>Triage proposal</h2>")[2]

    assert around(on) == around(off)
    assert on != off


# ── the policy survives the pages' middleware, and only for a marked response
async def run_middleware(
    headers: list[tuple[bytes, bytes]], *, marked: bool, path: str
) -> dict[str, str]:
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        if marked:
            scope[page_security.OWN_POLICY_SCOPE_KEY] = True
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict[str, Any]] = []

    async def collect(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    scope = {"type": "http", "path": path, "method": "GET", "headers": []}
    await page_security.SecurityHeadersMiddleware(inner)(scope, receive, collect)
    return {k.decode(): v.decode() for k, v in sent[0]["headers"]}


OWN = [(b"content-security-policy", DOWNLOAD_POLICY.encode())]


@pytest.mark.parametrize("path", [DOWNLOAD_PATH, "/adjuster/claims", "/claimant/x"])
def test_a_response_the_route_marked_keeps_its_own_policy(path: str) -> None:
    got = asyncio.run(run_middleware(OWN, marked=True, path=path))

    assert got["content-security-policy"] == DOWNLOAD_POLICY
    # Every other header of the pages is still set.
    for name, value in SECURITY_HEADERS.items():
        if name != POLICY_HEADER:
            assert got[name] == value, name


def test_a_response_nobody_marked_gets_the_pages_policy_whatever_it_set() -> None:
    got = asyncio.run(
        run_middleware(OWN, marked=False, path="/adjuster/claims/CLM-0001")
    )

    assert got["content-security-policy"] == SECURITY_HEADERS[POLICY_HEADER]


def test_the_policy_is_the_sandbox_one_with_no_allow_token() -> None:
    assert DOWNLOAD_POLICY == "default-src 'none'; frame-ancestors 'none'; sandbox"
    assert "allow-" not in DOWNLOAD_POLICY
    assert re.search(r"(^|; )sandbox$", DOWNLOAD_POLICY)


def test_every_route_of_the_pages_but_the_download_keeps_the_pages_policy() -> None:
    # The database is unreachable: each handler answers an error page (or a 422,
    # a 404, a 405), and every one of them goes through the pages' middleware.
    app = client(uploads=True, downloads=True)
    filled = {"claim_id": CLAIM, "file_id": str(FILE)}
    seen: list[tuple[str, str]] = []

    for route in app.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(("/adjuster/", "/claimant/")) or path == DOWNLOAD_PATH:
            continue
        for method in sorted(getattr(route, "methods", ()) - {"HEAD"}):
            response = app.request(method, path.format(**filled))
            assert (
                response.headers[POLICY_HEADER] == (SECURITY_HEADERS[POLICY_HEADER])
            ), (method, path, response.status_code)
            seen.append((method, path))
    # The queue, the claim, the proposal, the stylesheet, the two posts of the
    # claim page and the claimant's pages: a list this short would be a bug.
    assert len(seen) >= 8
    assert ("GET", "/adjuster/claims/{claim_id}") in seen
    for url, method in (
        ("/adjuster/nowhere", "GET"),
        ("/adjuster/claims", "DELETE"),
        ("/claimant/nowhere", "GET"),
        (download_url(), "POST"),
        (download_url(), "DELETE"),
        (download_url("CLM-12", FILE), "GET"),
    ):
        response = app.request(method, url)
        assert response.headers[POLICY_HEADER] == SECURITY_HEADERS[POLICY_HEADER], url


def test_no_module_of_the_package_but_the_download_sets_the_mark() -> None:
    # Every file of the package, so a future one is read too: the key is defined
    # and read by the pages' middleware, and set by the download alone.
    package = Path(file_download.__file__).parent
    names = {
        path.name
        for path in package.glob("*.py")
        if "OWN_POLICY_SCOPE_KEY" in path.read_text(encoding="utf-8")
    }

    assert names == {"page_security.py", "file_download.py"}
    assert "OWN_POLICY_SCOPE_KEY" in inspect.getsource(file_download)
    assert "scope[OWN_POLICY_SCOPE_KEY] = True" in inspect.getsource(file_download)
