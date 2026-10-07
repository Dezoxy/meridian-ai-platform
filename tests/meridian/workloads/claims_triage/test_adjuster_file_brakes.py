"""The download's own brakes, its cross-site check and the trail's filter (S070
F4d, the second security review's H-A and M-A).

Anyone who holds one file identifier could send ``GET`` of it without end: each
one read a megabyte, opened two connections and wrote an audit row, and 200 of
the rows pushed a claim's decisions out of the page's listing. The brakes are
the app's own and do not depend on which edge route served the path: a rate
(store-wide, GET and HEAD alike), a pool of readers of its own, a check that the
request is not from another site, and a trail that shows the newest rows and
counts the downloads in one line instead of listing them.
"""

import threading
import uuid

import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import owner_rows
from workloads.claims_triage.test_adjuster_file_download import (
    POLICY,
    SECURITY_HEADERS,
    download_client,
    download_rows,
    link,
    stored,
)
from workloads.claims_triage.test_adjuster_pages import TENANT, Page, url_of
from workloads.claims_triage.test_claim_uploads import (
    CANARY_BYTES,
    CLAIM,
    PDF,
    put_claim,
)

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.common.db import connect
from meridian.workloads.claims_triage import file_download
from meridian.workloads.claims_triage.claim_files import DOWNLOAD_EVENT, HEAD_FILE_SQL
from meridian.workloads.claims_triage.file_download import (
    BUSY_DETAIL,
    DOWNLOAD_RATE_PER_MINUTE,
    DOWNLOAD_RATE_WINDOW_SECONDS,
    MAX_CONCURRENT_DOWNLOADS,
    RATE_DETAIL,
)
from meridian.workloads.claims_triage.uploads import BUSY_RETRY_SECONDS

CROSS_SITE_SENTENCE = "the request came from another site"


def stored_file(db: DatabaseHandle, data: bytes = PDF) -> tuple[TestClient, str]:
    put_claim(db)
    client = download_client(db)
    return client, link(CLAIM, stored(client, data)["file_id"])


def test_the_numbers_are_thirty_a_minute_and_four_at_once() -> None:
    assert DOWNLOAD_RATE_PER_MINUTE == 30
    assert DOWNLOAD_RATE_WINDOW_SECONDS == 60
    assert MAX_CONCURRENT_DOWNLOADS == 4


# ── the rate: store-wide, GET and HEAD, no row for a refusal ────────────────
def test_the_thirty_first_request_in_a_minute_is_a_429_with_a_wait_and_no_row(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mutation "the download's limiter removed" turns this red.
    db = fresh_database
    client, url = stored_file(db)
    clock = [1000.0]
    monkeypatch.setattr(file_download, "now", lambda: clock[0])

    first = [client.get(url) for _ in range(DOWNLOAD_RATE_PER_MINUTE)]
    refused = client.get(url)

    assert {r.status_code for r in first} == {200}
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "60"
    assert RATE_DETAIL in refused.text
    assert PDF not in refused.content
    assert refused.headers[POLICY] == SECURITY_HEADERS[POLICY]
    assert "content-disposition" not in refused.headers
    assert len(download_rows(db)) == DOWNLOAD_RATE_PER_MINUTE
    # The window moves: one second short of a minute is still refused, then free.
    clock[0] += DOWNLOAD_RATE_WINDOW_SECONDS - 1
    assert client.get(url).status_code == 429
    clock[0] += 1.5
    assert client.get(url).status_code == 200
    assert len(download_rows(db)) == DOWNLOAD_RATE_PER_MINUTE + 1


def test_a_head_counts_against_the_same_limit(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, url = stored_file(db)

    statuses = [client.get(url).status_code for _ in range(DOWNLOAD_RATE_PER_MINUTE)]
    head = client.head(url)

    assert set(statuses) == {200}
    assert head.status_code == 429
    assert head.headers["retry-after"] == "60"
    assert client.get(url).status_code == 429


def test_a_not_found_costs_the_allowance_too_and_a_bad_shape_does_not(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, url = stored_file(db)
    unknown = link(CLAIM, str(uuid.uuid4()))

    shapeless = [client.get(link(CLAIM, "not-a-uuid")) for _ in range(40)]
    misses = [client.get(unknown) for _ in range(DOWNLOAD_RATE_PER_MINUTE)]
    after = client.get(url)

    assert {r.status_code for r in shapeless} == {404}
    assert {r.status_code for r in misses} == {404}
    assert after.status_code == 429
    assert download_rows(db) == []


def test_a_head_reads_the_type_and_size_and_never_the_bytes(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = fresh_database
    client, url = stored_file(db, PDF + CANARY_BYTES)

    def never(*_: object) -> None:
        raise AssertionError("a HEAD read a file's content")

    monkeypatch.setattr(file_download, "file_content", never)
    head = client.head(url)

    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(PDF + CANARY_BYTES))
    assert head.headers["content-type"] == "application/pdf"
    assert head.content == b""
    assert "content" not in HEAD_FILE_SQL
    assert download_rows(db) == []


def test_a_head_of_another_tenants_file_is_the_same_404(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, "CLM-9302", tenant="another-tenant")
    client = download_client(db)
    foreign = owner_rows(
        db,
        "INSERT INTO claims.claim_files (file_id, claim_id, kind, media_type, "
        "size_bytes, sha256, content) VALUES (%s, 'CLM-9302', 'photos', "
        "'application/pdf', %s, sha256(%s::bytea), %s) RETURNING file_id",
        (uuid.uuid4(), len(PDF), PDF, PDF),
    )[0][0]

    for method in (client.head, client.get):
        response = method(link("CLM-9302", str(foreign)))
        assert response.status_code == 404
    assert download_rows(db) == []


# ── the pool: four at once, the fifth is told so ────────────────────────────
def test_a_fifth_read_at_once_is_a_503_with_its_own_sentence_and_a_wait(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = fresh_database
    client, url = stored_file(db)
    entered = threading.Semaphore(0)
    release = threading.Event()
    real = file_download.file_content

    def held(*args: object):
        entered.release()
        assert release.wait(20)
        return real(*args)

    monkeypatch.setattr(file_download, "file_content", held)
    results: list[int] = []

    def get() -> None:
        results.append(TestClient(client.app).get(url).status_code)

    threads = [threading.Thread(target=get) for _ in range(MAX_CONCURRENT_DOWNLOADS)]
    for thread in threads:
        thread.start()
    for _ in threads:
        assert entered.acquire(timeout=20)

    fifth = client.get(url)
    release.set()
    for thread in threads:
        thread.join(20)

    assert fifth.status_code == 503
    assert fifth.headers["retry-after"] == str(BUSY_RETRY_SECONDS)
    assert BUSY_DETAIL in fifth.text
    assert fifth.headers[POLICY] == SECURITY_HEADERS[POLICY]
    assert PDF not in fifth.content
    assert results == [200] * MAX_CONCURRENT_DOWNLOADS
    # The permits came back, and the refusal wrote nothing.
    assert client.get(url).status_code == 200
    assert len(download_rows(db)) == MAX_CONCURRENT_DOWNLOADS + 1


def test_a_failing_read_gives_its_permit_back(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = fresh_database
    client, url = stored_file(db)
    real = file_download.file_content

    def broken(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(file_download, "file_content", broken)
    failed = [client.get(url) for _ in range(MAX_CONCURRENT_DOWNLOADS + 2)]
    monkeypatch.setattr(file_download, "file_content", real)

    assert {r.status_code for r in failed} == {500}
    assert client.get(url).status_code == 200


# ── the cross-site check ────────────────────────────────────────────────────
def test_a_click_a_typed_address_and_a_client_with_no_headers_pass(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, url = stored_file(db)

    def status(**headers: str) -> int:
        return client.get(url, headers=headers).status_code

    statuses = {
        "click from the claim page": status(**{"Sec-Fetch-Site": "same-origin"}),
        "typed address": status(**{"Sec-Fetch-Site": "none"}),
        "matching origin": status(Origin="http://testserver"),
        "no headers": status(),
    }

    assert set(statuses.values()) == {200}, statuses
    assert len(download_rows(db)) == len(statuses)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Sec-Fetch-Site": "cross-site"}, id="cross-site"),
        pytest.param({"Sec-Fetch-Site": "same-site"}, id="same-site"),
        pytest.param({"Sec-Fetch-Site": "surprise"}, id="unknown value"),
        pytest.param({"Origin": "https://evil.example"}, id="other origin"),
        pytest.param({"Origin": "null"}, id="null origin"),
    ],
)
def test_a_navigation_from_another_site_is_the_pages_403_and_writes_no_row(
    fresh_database: DatabaseHandle, headers: dict[str, str]
) -> None:
    # The mutation "the download's cross-site check removed" turns this red.
    db = fresh_database
    client, url = stored_file(db, PDF + CANARY_BYTES)

    refused = [client.request(m, url, headers=headers) for m in ("GET", "HEAD")]

    for response in refused:
        assert response.status_code == 403
        assert response.headers[POLICY] == SECURITY_HEADERS[POLICY]
        assert "content-disposition" not in response.headers
        assert CANARY_BYTES not in response.content
    assert CROSS_SITE_SENTENCE in refused[0].text
    assert download_rows(db) == []


def test_a_refused_request_does_not_spend_the_rate(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, url = stored_file(db)

    cross_site = {"Sec-Fetch-Site": "cross-site"}
    for _ in range(DOWNLOAD_RATE_PER_MINUTE + 5):
        assert client.get(url, headers=cross_site).status_code == 403

    assert client.get(url).status_code == 200


# ── the trail: the newest rows, the downloads counted and not listed ────────
def put_events(db: DatabaseHandle, count: int, event: str, reason: str) -> None:
    """``count`` rows of the Claims API about the claim, in one transaction (the
    database stamps ``db_role`` from the session); ``reason`` is numbered."""
    with connect(db.dsn("claims_api"), "claims-api") as conn:
        for index in range(count):
            record_event(
                conn,
                AuditEvent(
                    service="claims-api",
                    event=event,
                    outcome="ok",
                    tenant=TENANT,
                    reference=CLAIM,
                    reason=f"{reason}-{index:03d}",
                ),
            )


def trail_events(page: Page) -> list[str]:
    """The event cell of each row of the audit trail, in the page's order."""
    rows = [r.split() for r in page.rows if "claims_api" in r]
    return [cell for cells in rows for cell in cells if cell.startswith("claim.")]


def test_250_downloads_leave_the_later_decision_on_the_page_and_are_counted(
    fresh_database: DatabaseHandle,
) -> None:
    # The mutation "the trail's filter removed" turns this red.
    db = fresh_database
    put_claim(db, state="awaiting_adjuster")
    put_events(db, 250, DOWNLOAD_EVENT, "d")
    put_events(db, 1, "claim.approved", "later")

    response = download_client(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert response.status_code == 200
    assert trail_events(page) == ["claim.approved"]
    assert DOWNLOAD_EVENT not in " ".join(r for r in page.rows)
    assert "Downloads of this claim's files: 250, the latest at" in page.text
    assert "They are counted here and not listed below." in page.text
    assert "Older events are not listed" not in page.text


def test_with_no_download_the_page_has_no_downloads_line(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, state="awaiting_adjuster")
    put_events(db, 3, "claim.approved", "x")

    page = download_client(db).get(url_of(CLAIM))

    assert "Downloads of this claim's files" not in page.text
    assert "Older events are not listed" not in page.text


def test_the_page_shows_the_newest_200_in_time_order_and_says_older_ones_exist(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, state="awaiting_adjuster")
    put_events(db, 210, "claim.note", "n")

    response = download_client(db).get(url_of(CLAIM))

    page = Page(response.text)
    reasons = [r.split()[-1] for r in page.rows if "claim.note" in r]
    assert len(reasons) == 200
    assert reasons[0] == "n-010" and reasons[-1] == "n-209"
    assert reasons == sorted(reasons)
    assert "Older events are not listed: the latest 200 are shown." in page.text


def test_exactly_200_events_say_nothing_about_older_ones(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, state="awaiting_adjuster")
    put_events(db, 200, "claim.note", "n")

    page = Page(download_client(db).get(url_of(CLAIM)).text)

    assert len([r for r in page.rows if "claim.note" in r]) == 200
    assert "Older events are not listed" not in page.text


def test_another_claims_downloads_are_not_counted_on_this_page(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, state="awaiting_adjuster")
    put_claim(db, "CLM-9302", state="awaiting_adjuster")
    with connect(db.dsn("claims_api"), "claims-api") as conn:
        record_event(
            conn,
            AuditEvent(
                service="claims-api",
                event=DOWNLOAD_EVENT,
                outcome="served",
                tenant=TENANT,
                reference="CLM-9302",
            ),
        )

    page = Page(download_client(db).get(url_of(CLAIM)).text)

    assert "Downloads of this claim's files" not in page.text


# ── a response that cannot be built carries no download policy ──────────────
def test_the_mark_is_set_only_after_the_response_is_built(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = fresh_database
    client, url = stored_file(db)

    def cannot_build(*_: object, **__: object) -> None:
        raise RuntimeError("no response")

    monkeypatch.setattr(file_download, "Response", cannot_build)
    response = client.get(url)

    assert response.status_code == 500
    assert response.headers[POLICY] == SECURITY_HEADERS[POLICY]


# ── the file's identifier is a capability: kept out of the spans ────────────
def test_the_file_identifier_is_in_no_span_attribute_the_server_span_included(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    exporter = InMemorySpanExporter()
    put_claim(db)
    client = download_client(db, exporter=exporter)
    body = stored(client, PDF)
    exporter.clear()

    for method in (client.get, client.head):
        assert method(link(CLAIM, body["file_id"])).status_code == 200
    assert client.get(link(CLAIM, str(uuid.uuid4()))).status_code == 404

    spans = exporter.get_finished_spans()
    attributes = " ".join(str(dict(s.attributes)) for s in spans)
    assert len(spans) >= 6  # a server span and the route's own, three times
    assert body["file_id"] not in attributes
    assert "{file_id}" in attributes
    assert CLAIM in attributes
