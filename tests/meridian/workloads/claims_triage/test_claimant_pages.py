"""The claimant's pages of the Claims API (S049): the start page and the claim
form, the status page, the documents form and the withdrawal.

The pages are server-rendered and out of the OpenAPI contract. The form runs the
code of ``POST /claims`` and the two posts the code of S048's routes; the tests
here pin that the results are the same, that the status page tells a claimant
what happens next and nothing of the proposal (T-65), that a form that does not
validate never shows a value in a message (T-03), and that a post another site
made is refused with the claimant's page (T-70).
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from servicesupport import claim_with_id, owner_rows
from workloads.claims_triage.test_adjuster_pages import (
    OTHER_TENANT,
    Page,
    client_for,
    put_claim,
    put_proposal,
)
from workloads.claims_triage.test_claim_moves import (
    MoveRuntime,
    arrived_names,
    put_arrived,
)
from workloads.claims_triage.test_claims_app import (
    CITATION,
    OUTPUT,
    REQUEST_DOCUMENTS_OUTPUT,
    Runtime,
    claim_audit,
    claim_state,
    claims_dsn,
    failing_runtime,
    make_client,
)

from meridian.platform.common.audit import AuditUnavailable
from meridian.workloads.claims_triage import claimant, triaging
from meridian.workloads.claims_triage.models import MAX_DOCUMENTS, DecisionFailure
from meridian.workloads.claims_triage.moves import (
    NOT_AWAITING_DOCUMENTS_DETAIL,
    TOO_MANY_DOCUMENTS_DETAIL,
)

CLAIMANT_MODULE = "meridian.workloads.claims_triage.claimant"
TRIAGING_MODULE = "meridian.workloads.claims_triage.triaging"
UNASSESSED_TEXT = (
    "Your claim is stored but could not be assessed now. Send the same form again."
)
START_URL = "/claimant/claims"
LOOKUP_URL = f"{START_URL}/lookup"
SAME_ORIGIN = {"Origin": "http://testserver"}
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; style-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
}
BANNER = (
    "Synthetic data only. Every name, address and description you enter must "
    "be fictional: never a real person's."
)
DIFFERENT_SUBMISSION = "the claim exists with a different submission"
NO_SUCH_CLAIM = "no such claim"
CROSS_SITE = "the request came from another site"
DATABASE_DOWN = "the database is unavailable"
AUDIT_DOWN = "the audit log is unavailable"
RUN_FAILED = "the triage run did not complete; the claim is stored"
LOOKUP_MESSAGE = "A claim ID is CLM- and four digits, for example CLM-0001."
FORM_FIELDS = (
    "claim_id",
    "policy_number",
    "peril",
    "loss_date",
    "reported_on",
    "claimed_amount",
    "city",
    "country",
    "description",
    "documents",
    "claimant_name",
    "claimant_email",
)
PERILS = (
    "accidental_damage",
    "burglary",
    "burst_pipe",
    "collision",
    "fire",
    "flood",
    "glass",
    "storm",
    "theft",
    "third_party_liability",
)
NAME_SENTINEL = "Sentinelname Sentinelfamily"
EMAIL_SENTINEL = "sentinel.mail77@example.com"
BAD_EMAIL_SENTINEL = "sentinelmail-88-without-an-at-sign"
DESCRIPTION_SENTINEL = "description-sentinel-55"
ALL_STATES = {
    "submitted": "We have received your claim and are assessing it.",
    "triaging": "We have received your claim and are assessing it.",
    "awaiting_adjuster": "An adjuster is reviewing your claim.",
    "triage_failed": "An adjuster is reviewing your claim.",
    "documents_requested": "We need more documents before we can go on.",
    "approved": "Your claim is approved.",
    "rejected": "Your claim is not approved.",
    "withdrawn": "You withdrew this claim.",
}
CAN_WITHDRAW = ("awaiting_adjuster", "documents_requested")
RECEIVED_AT = datetime(2026, 10, 1, 12, 30, 5, tzinfo=UTC)
WITHDRAW_BUTTON = "Withdraw this claim"
VOID_TAGS = ("input", "meta", "link", "br", "hr", "img")


def status_url(claim_id: str) -> str:
    return f"{START_URL}/{claim_id}"


def form_of(claim: dict[str, Any], **changes: str) -> dict[str, str]:
    """The fields of the claim form for a synthetic claim, as a browser posts."""
    form = {
        "claim_id": claim["claim_id"],
        "policy_number": claim["policy_number"],
        "peril": claim["peril"],
        "loss_date": claim["loss_date"],
        "reported_on": claim["reported_on"],
        "claimed_amount": str(claim["claimed_amount"]),
        "city": claim["loss_location"]["city"],
        "country": claim["loss_location"]["country"],
        "description": claim["description"],
        "documents": "\n".join(claim["documents"]),
        "claimant_name": claim["claimant"]["name"],
        "claimant_email": claim["claimant"]["email"],
    }
    return form | changes


def post_claim(
    client: TestClient,
    form: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return client.post(
        START_URL,
        data=form,
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def post_to(
    client: TestClient,
    url: str,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return client.post(
        url,
        data=data or {},
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


class Labels(HTMLParser):
    """The text of each ``<label for>``, by the id it names, and the ids of the
    controls."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.labels: dict[str, str] = {}
        self.controls: dict[str, str] = {}
        self._open: str | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "label":
            self._open = str(found.get("for"))
            self.labels[self._open] = ""
        elif tag in ("input", "select", "textarea") and found.get("id"):
            self.controls[str(found["id"])] = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self.labels[self._open] += data


class Options(HTMLParser):
    """The options of the peril select: value and whether it is selected."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.options: list[tuple[str, bool]] = []
        self._in_peril = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "select":
            self._in_peril = found.get("name") == "peril"
        elif tag == "option" and self._in_peril:
            self.options.append((str(found.get("value")), "selected" in found))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._in_peril = False


class Items(HTMLParser):
    """The text of each list item."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.items: list[str] = []
        self._open = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self._open = True
            self.items.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self._open = False

    def handle_data(self, data: str) -> None:
        if self._open:
            self.items[-1] += data


class Where(HTMLParser):
    """Where each needle is in the page: ``tag[attribute]`` for an attribute's
    value, the enclosing tag for text."""

    def __init__(self, html: str, needles: tuple[str, ...]) -> None:
        super().__init__()
        self.needles = needles
        self.found: list[tuple[str, str]] = []
        self._stack: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            self.found += [
                (needle, f"{tag}[{name}]")
                for needle in self.needles
                if value and needle in value
            ]
        if tag not in VOID_TAGS:
            self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._stack:
            while self._stack.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        here = self._stack[-1] if self._stack else ""
        self.found += [(needle, here) for needle in self.needles if needle in data]


def claims_held(db: DatabaseHandle) -> int:
    return owner_rows(db, "SELECT count(*) FROM claims.claims")[0][0]


def stored_submission(db: DatabaseHandle, claim_id: str) -> dict[str, Any]:
    ((submission,),) = owner_rows(
        db, "SELECT submission FROM claims.claims WHERE claim_id = %s", (claim_id,)
    )
    return submission


def put_received(db: DatabaseHandle, claim_id: str) -> None:
    owner_rows(
        db,
        "UPDATE claims.claims SET received_at = %s WHERE claim_id = %s RETURNING 1",
        (RECEIVED_AT, claim_id),
    )


def refused_page(response: httpx.Response, status: int) -> Page:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    return Page(response.text)


# ── the start page ──────────────────────────────────────────────────────────
def test_the_start_page_has_the_banner_every_field_with_its_label_and_the_headers() -> (
    None
):
    response = make_client().get(START_URL)

    assert response.status_code == 200
    page = Page(response.text)
    assert BANNER in page.text
    labels, options = Labels(response.text), Options(response.text)
    for field in FORM_FIELDS:
        assert field in labels.controls, field
        assert labels.labels[field].strip(), field
    forms = page.attributes("form")
    assert {"method": "post", "action": START_URL} in [
        {k: v for k, v in f.items() if k in ("method", "action")} for f in forms
    ]
    inputs = {str(i.get("id")): i for i in page.attributes("input")}
    assert inputs["loss_date"]["type"] == inputs["reported_on"]["type"] == "date"
    assert inputs["claimed_amount"]["type"] == "number"
    # Every peril, none preselected: an empty option comes first.
    assert options.options[0] == ("", False)
    assert [value for value, _ in options.options[1:]] == list(PERILS)
    assert not any(selected for _, selected in options.options)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "<script" not in response.text.lower()
    assert "style=" not in response.text.lower()


def test_the_start_page_has_the_lookup_form_and_a_link_to_itself() -> None:
    page = Page(make_client().get(START_URL).text)

    # A post: what is typed must not be in a URL (T-03).
    assert [f for f in page.attributes("form") if f.get("method") == "get"] == []
    (lookup,) = [f for f in page.attributes("form") if f.get("action") == LOOKUP_URL]
    assert lookup["method"] == "post"
    assert "claim_id" in [i.get("name") for i in page.attributes("input")]
    assert START_URL in page.links()


def test_the_peril_options_are_the_models_perils() -> None:
    from typing import get_args

    from meridian.workloads.claims_triage.models import Peril

    assert tuple(get_args(Peril)) == PERILS


# ── the claim form ──────────────────────────────────────────────────────────
def test_a_valid_post_is_303_and_stores_what_the_json_route_stores(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    claim = claim_with_id("CLM-9501")
    page_runtime, json_runtime = Runtime(), Runtime()

    response = post_claim(client_for(db, page_runtime), form_of(claim))
    json_response = client_for(db, json_runtime).post(
        "/claims", json=claim_with_id("CLM-9502")
    )

    assert response.status_code == 303
    assert response.headers["location"] == status_url("CLM-9501")
    assert json_response.status_code == 201
    assert stored_submission(db, "CLM-9501") == claim
    # The same row as the JSON route's, but for the ID.
    rows = owner_rows(
        db,
        "SELECT claim_id, tenant, submission - 'claim_id' FROM claims.claims "
        "ORDER BY claim_id",
    )
    assert rows[0][1:] == rows[1][1:]
    assert claim_state(db, "CLM-9501") == ("awaiting_adjuster", page_runtime.run_id)
    # The same audit events, of the same kinds.
    kinds = [row[:3] for row in claim_audit(db, "CLM-9501")]
    assert kinds == [row[:3] for row in claim_audit(db, "CLM-9502")]
    assert len(kinds) == 2
    assert len(page_runtime.requests) == len(json_runtime.requests) == 1


def test_the_triage_runs_in_a_span_with_the_claim_and_the_tenant_only(
    fresh_database: DatabaseHandle,
) -> None:
    exporter = InMemorySpanExporter()
    runtime = Runtime()
    client = make_client(claims_dsn(fresh_database), runtime, exporter)

    post_claim(client, form_of(claim_with_id("CLM-9501")))

    (span,) = [
        s for s in exporter.get_finished_spans() if s.name == "claims.claimant.submit"
    ]
    # The run's ID is set on it by the triage, as on the JSON route's span.
    assert dict(span.attributes) == {
        "meridian.claim_id": "CLM-9501",
        "meridian.tenant": "claims-triage",
        "meridian.run_id": str(runtime.run_id),
    }


def test_the_submit_span_is_a_child_of_the_requests_server_span(
    fresh_database: DatabaseHandle,
) -> None:
    exporter = InMemorySpanExporter()
    client = make_client(claims_dsn(fresh_database), Runtime(), exporter)

    post_claim(client, form_of(claim_with_id("CLM-9501")))

    spans = exporter.get_finished_spans()
    (server,) = [s for s in spans if s.kind == SpanKind.SERVER]
    (submit,) = [s for s in spans if s.name == "claims.claimant.submit"]
    assert submit.parent is not None
    assert submit.parent.span_id == server.context.span_id
    assert submit.context.trace_id == server.context.trace_id


def recording_thread(
    original: Callable[..., Any], seen: list[str]
) -> Callable[..., Any]:
    """``original``, noting whether it ran with an event loop running in its
    thread: ``asyncio.get_running_loop()`` raises ``RuntimeError`` off the loop."""

    def wrapper(*args: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append("thread")
        else:
            seen.append("event loop")
        return original(*args)

    return wrapper


def test_the_store_and_the_triage_run_off_the_event_loop(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored: list[str] = []
    triaged: list[str] = []
    monkeypatch.setattr(
        f"{CLAIMANT_MODULE}.store_claim",
        recording_thread(claimant.store_claim, stored),
    )
    monkeypatch.setattr(
        f"{CLAIMANT_MODULE}.triage_claim",
        recording_thread(claimant.triage_claim, triaged),
    )

    response = post_claim(
        client_for(fresh_database, Runtime()), form_of(claim_with_id("CLM-9501"))
    )

    assert response.status_code == 303
    assert stored == ["thread"]
    assert triaged == ["thread"]


INVALID_POSTS = {
    "a claim ID that is not one": (
        {"claim_id": "CLM-12"},
        "Claim ID: String should match pattern",
    ),
    "an amount that is not a number": (
        {"claimed_amount": "12a"},
        "Claimed amount: Input should be a valid integer",
    ),
    "an amount above the limit": (
        {"claimed_amount": "1000001"},
        "Claimed amount: Input should be less than or equal to 1000000",
    ),
    "a loss after the report": (
        {"loss_date": "2026-07-14", "reported_on": "2026-07-13"},
        "Claim: Value error, loss_date is after reported_on",
    ),
    "an e-mail address that is not one": (
        {"claimant_email": BAD_EMAIL_SENTINEL},
        "Your email: String should match pattern",
    ),
    "a peril that is not one": (
        {"peril": ""},
        "Peril: Input should be",
    ),
    "a country that is not two capitals": (
        {"country": "Austria"},
        "Country: String should match pattern",
    ),
}


@pytest.mark.parametrize(
    ("changes", "message"), INVALID_POSTS.values(), ids=INVALID_POSTS
)
def test_a_post_that_does_not_validate_is_the_form_again_and_stores_nothing(
    fresh_database: DatabaseHandle, changes: dict[str, str], message: str
) -> None:
    runtime = Runtime()
    claim = claim_with_id("CLM-9501")
    form = (
        form_of(
            claim,
            claimant_name=NAME_SENTINEL,
            claimant_email=EMAIL_SENTINEL,
            description=DESCRIPTION_SENTINEL,
        )
        | changes
    )

    response = post_claim(client_for(fresh_database, runtime), form)

    page = refused_page(response, 422)
    assert message in page.text
    assert claims_held(fresh_database) == 0
    assert runtime.requests == []
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    # It is the form: the claimant need not retype what was typed.
    assert {"method": "post", "action": START_URL} in [
        {k: v for k, v in f.items() if k in ("method", "action")}
        for f in page.attributes("form")
    ]
    sentinels = (NAME_SENTINEL, DESCRIPTION_SENTINEL, form["claimant_email"])
    where = Where(response.text, sentinels).found
    # Each is in its input's value or the textarea, and in no message.
    assert {(s, "input[value]") for s in (NAME_SENTINEL, form["claimant_email"])} | {
        (DESCRIPTION_SENTINEL, "textarea")
    } == set(where)
    assert len(where) == 3


def test_a_value_with_markup_is_shown_again_as_text() -> None:
    markup = '"><script>alert(1)</script>'
    form = form_of(claim_with_id("CLM-9501"), city=markup, claimed_amount="12a")

    response = post_claim(make_client(), form)

    assert response.status_code == 422
    assert "<script" not in response.text.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


MISSING_OR_TWICE = {
    "claim ID missing": ("claim_id", None),
    "documents missing": ("documents", None),
    "description twice": ("description", ["one", "two"]),
    "e-mail twice": ("claimant_email", ["a@example.com", "b@example.com"]),
}


@pytest.mark.parametrize(
    ("field", "value"), MISSING_OR_TWICE.values(), ids=MISSING_OR_TWICE
)
def test_a_field_missing_or_named_twice_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle, field: str, value: list[str] | None
) -> None:
    runtime = Runtime()
    form: dict[str, Any] = form_of(claim_with_id("CLM-9501"))
    if value is None:
        del form[field]
    else:
        form[field] = value

    response = post_claim(client_for(fresh_database, runtime), form)

    page = refused_page(response, 422)
    assert "exactly once" in page.text
    assert claims_held(fresh_database) == 0
    assert runtime.requests == []


def test_a_multipart_file_in_the_documents_field_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    # ``documents`` is the one field that may be empty: a file taken for empty
    # text would store the claim, so only the rule "exactly once, as text" refuses.
    form = form_of(claim_with_id("CLM-9501"))
    del form["documents"]
    runtime = Runtime()

    response = client_for(fresh_database, runtime).post(
        START_URL,
        data=form,
        files={"documents": ("documents.txt", b"a file, not text")},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    page = refused_page(response, 422)
    assert "exactly once" in page.text
    assert claims_held(fresh_database) == 0
    assert runtime.requests == []


def test_another_submission_under_a_stored_id_is_the_form_again_409_and_the_row_is_kept(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    claim = claim_with_id("CLM-9501")
    client = client_for(db, Runtime())
    assert post_claim(client, form_of(claim)).status_code == 303
    runtime = Runtime()

    response = post_claim(
        client_for(db, runtime), form_of(claim, claimed_amount="9999")
    )

    page = refused_page(response, 409)
    assert DIFFERENT_SUBMISSION in page.text
    assert {"method": "post", "action": START_URL} in [
        {k: v for k, v in f.items() if k in ("method", "action")}
        for f in page.attributes("form")
    ]
    assert stored_submission(db, "CLM-9501") == claim
    assert runtime.requests == []


def test_the_same_submission_posted_again_after_its_triage_is_303(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    form = form_of(claim_with_id("CLM-9501"))
    first_runtime, second_runtime = Runtime(), Runtime()
    assert post_claim(client_for(db, first_runtime), form).status_code == 303

    response = post_claim(client_for(db, second_runtime), form)

    # The claim holds a proposal: its state is the answer (T-65).
    assert response.status_code == 303
    assert response.headers["location"] == status_url("CLM-9501")
    assert second_runtime.requests == []
    assert claim_state(db, "CLM-9501") == ("awaiting_adjuster", first_runtime.run_id)


def test_a_triage_that_fails_is_303_and_the_status_page_says_an_adjuster_reviews_it(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client = client_for(db, failing_runtime())

    response = post_claim(client, form_of(claim_with_id("CLM-9501")))

    assert response.status_code == 303
    assert claim_state(db, "CLM-9501")[0] == "triage_failed"
    status = client.get(response.headers["location"])
    assert status.status_code == 200
    assert "An adjuster is reviewing your claim." in Page(status.text).text


def test_a_failed_audit_write_on_the_submission_is_the_503_claimant_page(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def audit_down(*_: object) -> None:
        raise AuditUnavailable("OperationalError")

    monkeypatch.setattr(f"{CLAIMANT_MODULE}.store_claim", audit_down)

    with caplog.at_level(logging.ERROR):
        response = post_claim(make_client(), form_of(claim_with_id("CLM-9501")))

    page = refused_page(response, 503)
    assert AUDIT_DOWN in page.text
    assert START_URL in page.links()
    assert any("audit write failed" in r.getMessage() for r in caplog.records)


def test_a_failed_audit_write_in_the_triage_is_the_503_claimant_page(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def audit_down(*_: object) -> None:
        raise AuditUnavailable("OperationalError")

    monkeypatch.setattr(f"{CLAIMANT_MODULE}.triage_claim", audit_down)

    response = post_claim(
        client_for(fresh_database, Runtime()), form_of(claim_with_id("CLM-9501"))
    )

    # Not swallowed with the refusals: the claim is stored, the page says 503.
    page = refused_page(response, 503)
    assert AUDIT_DOWN in page.text
    assert claims_held(fresh_database) == 1


def test_a_post_when_the_database_is_down_is_the_503_claimant_page() -> None:
    response = post_claim(make_client(), form_of(claim_with_id("CLM-9501")))

    page = refused_page(response, 503)
    assert DATABASE_DOWN in page.text
    assert "/adjuster/claims" not in page.links()


def test_a_database_failure_in_the_triage_step_is_the_503_claimant_page(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def database_down(*_: object) -> None:
        raise psycopg.OperationalError("the connection was lost")

    monkeypatch.setattr(f"{CLAIMANT_MODULE}.triage_claim", database_down)

    response = post_claim(
        client_for(fresh_database, Runtime()), form_of(claim_with_id("CLM-9501"))
    )

    page = refused_page(response, 503)
    assert DATABASE_DOWN in page.text
    assert claims_held(fresh_database) == 1


def test_a_stored_claim_whose_triage_never_started_is_not_left_unseen(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = fresh_database
    form = form_of(
        claim_with_id("CLM-9501"),
        claimant_name=NAME_SENTINEL,
        claimant_email=EMAIL_SENTINEL,
    )
    runtime = Runtime()
    client = client_for(db, runtime)
    real_take_triage = triaging.take_triage
    calls: list[object] = []

    def down_once(*args: Any) -> Any:
        calls.append(args)
        if len(calls) == 1:
            raise psycopg.OperationalError("the connection was lost")
        return real_take_triage(*args)

    monkeypatch.setattr(f"{TRIAGING_MODULE}.take_triage", down_once)

    with caplog.at_level(logging.INFO):
        response = post_claim(client, form)

    # Nothing will triage it and no queue lists it: the claimant is told to send
    # the form again, not shown a status page that says it is being assessed.
    page = refused_page(response, 503)
    assert UNASSESSED_TEXT in page.text
    assert START_URL in page.links()
    assert claim_state(db, "CLM-9501") == ("submitted", None)
    assert runtime.requests == []
    logged = [r.getMessage() for r in caplog.records if r.name == CLAIMANT_MODULE]
    assert any("CLM-9501" in message and "503" in message for message in logged)
    for needle in (NAME_SENTINEL, EMAIL_SENTINEL):
        assert needle not in caplog.text

    again = post_claim(client, form)

    assert again.status_code == 303
    assert again.headers["location"] == status_url("CLM-9501")
    assert claim_state(db, "CLM-9501")[0] == "awaiting_adjuster"
    assert len(runtime.requests) == 1


# ── the status page ─────────────────────────────────────────────────────────
def test_the_status_page_has_the_claim_the_time_and_one_sentence_for_each_state(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client = client_for(db)
    for number, (state, sentence) in enumerate(ALL_STATES.items(), start=1):
        claim_id = f"CLM-950{number}"
        put_claim(db, claim_id, state)
        put_received(db, claim_id)

        response = client.get(status_url(claim_id))

        assert response.status_code == 200, state
        text = Page(response.text).text
        assert claim_id in text
        assert "Received 2026-10-01 12:30:05 UTC" in text
        assert sentence in text, state
        # Exactly one of the eight sentences.
        assert [s for s in set(ALL_STATES.values()) if s in text] == [sentence]
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value


MARKERS = {
    "reason": "marker-reason-3141",
    "amount": "4817231",
    "fraud": "marker-fraud-indicator",
    "rationale": "marker-rationale-text",
    "clause": "marker-clause-2718",
    "product": "MARKER-PRODUCT",
    "gap": "marker-gap",
    "unavailable": "marker-unavailable",
    "name": "Markername Markerfamily",
    "email": "marker.email99@example.com",
    "description": "marker-description-1618",
    "deployment": "marker-deployment",
}


def test_the_status_page_in_no_state_shows_a_marker_of_the_proposal_or_the_submission(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    run_id = uuid.uuid4()
    proposal = OUTPUT | {
        "route": "request_documents",
        "reason": MARKERS["reason"],
        "recommendation": "reject",
        "payable_amount": int(MARKERS["amount"]),
        "exclusion_clause": MARKERS["clause"],
        "fraud_indicators": [MARKERS["fraud"]],
        "missing_documents": ["police report"],
        "citations": [
            {**CITATION, "clause": MARKERS["clause"], "product": MARKERS["product"]}
        ],
        "gaps": [MARKERS["gap"]],
        "unavailable_because": MARKERS["unavailable"],
        "rationale": MARKERS["rationale"],
        "drafted_by": OUTPUT["drafted_by"] | {"deployment": MARKERS["deployment"]},
    }
    client = client_for(db)
    for number, state in enumerate(ALL_STATES, start=1):
        claim_id = f"CLM-960{number}"
        put_claim(
            db,
            claim_id,
            state,
            run_id=run_id if state != "submitted" else None,
            claimant={"name": MARKERS["name"], "email": MARKERS["email"]},
            description=MARKERS["description"],
            claimed_amount=7340,
        )
        put_proposal(db, claim_id, proposal)

        response = client.get(status_url(claim_id))

        assert response.status_code == 200, state
        assert claim_id in response.text
        for kind, marker in MARKERS.items():
            assert marker not in response.text, (state, kind)
        # The submission's own facts and the run's ID are not shown either.
        for shown in ("7340", "7,340", "2890", str(run_id), "Linz", "storm"):
            assert shown not in response.text, (state, shown)


def test_the_status_page_reads_only_the_missing_documents_of_the_latest_proposal(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "documents_requested")
    # A document that is no valid proposal, but for the one field: the page
    # reads that expression only.
    put_proposal(
        db,
        "CLM-9301",
        {
            "route": "request_documents",
            "reason": "x",
            "missing_documents": ["police report"],
        },
    )

    response = client_for(db).get(status_url("CLM-9301"))

    assert response.status_code == 200
    assert "Documents asked for:" in Page(response.text).text
    assert Items(response.text).items == ["police report"]


def test_documents_asked_for_are_the_latest_proposals_list(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "documents_requested")
    put_proposal(
        db,
        "CLM-9301",
        REQUEST_DOCUMENTS_OUTPUT | {"missing_documents": ["old receipt"]},
        created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    )
    put_proposal(
        db,
        "CLM-9301",
        REQUEST_DOCUMENTS_OUTPUT
        | {"missing_documents": ["police report", "repair estimate"]},
        created_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
    )

    response = client_for(db).get(status_url("CLM-9301"))

    assert Items(response.text).items == ["police report", "repair estimate"]
    assert "Documents asked for:" in Page(response.text).text
    assert "Tell us below which documents you are sending." not in response.text
    assert "old receipt" not in response.text


EMPTY_LISTS = {
    "an empty list": [],
    "no proposal": None,
    "a list that is not": {"police report": 1},
    "names that are not text": ["police report", 5],
}


@pytest.mark.parametrize("stored", EMPTY_LISTS.values(), ids=EMPTY_LISTS)
def test_an_empty_or_unreadable_list_says_to_tell_us_which_documents_are_sent(
    fresh_database: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    stored: object,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "documents_requested")
    if stored is not None:
        put_proposal(
            db, "CLM-9301", REQUEST_DOCUMENTS_OUTPUT | {"missing_documents": stored}
        )

    with caplog.at_level(logging.DEBUG):
        response = client_for(db).get(status_url("CLM-9301"))

    text = Page(response.text).text
    assert "We need more documents before we can go on." in text
    assert "Tell us below which documents you are sending." in text
    assert "Documents asked for:" not in text
    assert Items(response.text).items == []
    if stored not in ([], None):
        # The claim's ID and that it was unreadable; no value.
        unreadable = [
            r.getMessage() for r in caplog.records if "CLM-9301" in r.getMessage()
        ]
        assert unreadable
        assert not any("police report" in message for message in unreadable)


def test_the_arrived_names_are_listed_and_markup_in_one_is_text(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "awaiting_adjuster")
    put_arrived(db, "CLM-9301", "<b>receipt</b>")
    put_arrived(db, "CLM-9301", "photos of the roof")

    response = client_for(db).get(status_url("CLM-9301"))

    assert "Documents that arrived" in Page(response.text).text
    assert Items(response.text).items == ["<b>receipt</b>", "photos of the roof"]
    assert "<b>receipt</b>" not in response.text
    assert "&lt;b&gt;receipt&lt;/b&gt;" in response.text


def test_no_arrived_names_no_heading_for_them(fresh_database: DatabaseHandle) -> None:
    put_claim(fresh_database, "CLM-9301", "awaiting_adjuster")

    response = client_for(fresh_database).get(status_url("CLM-9301"))

    assert response.status_code == 200
    assert "Documents that arrived" not in response.text


def test_the_documents_form_is_on_the_page_of_a_claim_that_waits_for_documents_only(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client = client_for(db)
    for number, state in enumerate(ALL_STATES, start=1):
        claim_id = f"CLM-970{number}"
        put_claim(db, claim_id, state)

        page = Page(client.get(status_url(claim_id)).text)

        actions = [str(f.get("action")) for f in page.attributes("form")]
        assert (f"{status_url(claim_id)}/documents" in actions) == (
            state == "documents_requested"
        ), state
        assert (f"{status_url(claim_id)}/withdrawal" in actions) == (
            state in CAN_WITHDRAW
        ), state
        has_button = WITHDRAW_BUTTON in page.text
        assert has_button == (state in CAN_WITHDRAW), state
        names = [i.get("name") for i in page.attributes("textarea")]
        assert names == (["documents"] if state == "documents_requested" else [])


def test_a_claim_of_another_tenant_or_none_is_the_claimant_404_page(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "awaiting_adjuster", tenant=OTHER_TENANT)
    client = client_for(db)

    for claim_id in ("CLM-9301", "CLM-9302"):
        response = client.get(status_url(claim_id))

        page = refused_page(response, 404)
        assert NO_SUCH_CLAIM in page.text
        assert BANNER in page.text
        assert "Back to your claims" in page.text
        assert START_URL in page.links()
        assert not [link for link in page.links() if link.startswith("/adjuster/")]


def test_the_status_page_when_the_database_is_down_is_the_claimant_503_page() -> None:
    response = make_client().get(status_url("CLM-9301"))

    page = refused_page(response, 503)
    assert DATABASE_DOWN in page.text
    assert BANNER in page.text


# ── the documents post ──────────────────────────────────────────────────────
DOCUMENTS_ID = "CLM-9301"


def waiting_for_documents(db: DatabaseHandle, **fields: Any) -> None:
    put_claim(db, DOCUMENTS_ID, "documents_requested", triages=1, **fields)


def documents_url(claim_id: str = DOCUMENTS_ID) -> str:
    return f"{status_url(claim_id)}/documents"


def test_a_documents_post_stores_each_name_once_and_is_303(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    waiting_for_documents(db)
    runtime = MoveRuntime()

    response = post_to(
        client_for(db, runtime),
        documents_url(),
        {"documents": " police report \n\npolice report\nphotos"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == status_url(DOCUMENTS_ID)
    assert arrived_names(db, DOCUMENTS_ID) == ["photos", "police report"]
    # The code of the JSON route: the claim is triaged with them.
    assert claim_state(db, DOCUMENTS_ID)[0] == "awaiting_adjuster"
    assert len(runtime.starts) == 1


def test_documents_beyond_the_bound_are_the_status_page_with_422_and_nothing_is_stored(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    waiting_for_documents(db)
    runtime = MoveRuntime()
    names = "\n".join(f"document {n:02}" for n in range(MAX_DOCUMENTS + 1))

    response = post_to(client_for(db, runtime), documents_url(), {"documents": names})

    # The bound is the model's (T-38): 21 distinct names do not validate.
    page = refused_page(response, 422)
    assert "Documents: Tuple should have at most 20 items" in page.text
    assert WITHDRAW_BUTTON in page.text
    assert arrived_names(db, DOCUMENTS_ID) == []
    assert runtime.calls == []


def test_documents_that_would_make_the_claim_hold_more_than_20_are_the_apis_409(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    waiting_for_documents(db)  # its submission names "photos"
    runtime = MoveRuntime()
    names = "\n".join(f"document {n:02}" for n in range(MAX_DOCUMENTS))

    response = post_to(client_for(db, runtime), documents_url(), {"documents": names})

    page = refused_page(response, 409)
    assert TOO_MANY_DOCUMENTS_DETAIL in page.text
    assert "409" in page.text
    assert arrived_names(db, DOCUMENTS_ID) == []
    assert claim_state(db, DOCUMENTS_ID)[0] == "documents_requested"
    assert runtime.calls == []


def test_a_documents_post_on_a_claim_that_does_not_wait_for_them_is_a_409_notice(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, DOCUMENTS_ID, "awaiting_adjuster")

    response = post_to(
        client_for(db, MoveRuntime()), documents_url(), {"documents": "photos"}
    )

    page = refused_page(response, 409)
    assert NOT_AWAITING_DOCUMENTS_DETAIL in page.text
    assert "An adjuster is reviewing your claim." in page.text
    assert arrived_names(db, DOCUMENTS_ID) == []


DOCUMENTS_REFUSED = {
    "no name": (
        {"documents": " \n \n"},
        "Documents: Tuple should have at least 1 item",
    ),
    "a name over 100 characters": (
        {"documents": "x" * 101},
        "Documents: String should have at most 100 characters",
    ),
    "the field twice": ({"documents": ["photos", "receipt"]}, "exactly once"),
    "no field": ({}, "exactly once"),
}


def test_a_multipart_file_in_the_documents_form_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    waiting_for_documents(db)
    runtime = MoveRuntime()

    response = client_for(db, runtime).post(
        documents_url(),
        files={"documents": ("documents.txt", b"a file, not text")},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    page = refused_page(response, 422)
    assert "exactly once" in page.text
    assert "We need more documents before we can go on." in page.text
    assert arrived_names(db, DOCUMENTS_ID) == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("data", "message"), DOCUMENTS_REFUSED.values(), ids=DOCUMENTS_REFUSED
)
def test_a_documents_post_that_does_not_validate_is_the_status_page_422(
    fresh_database: DatabaseHandle, data: dict[str, Any], message: str
) -> None:
    db = fresh_database
    waiting_for_documents(db)
    runtime = MoveRuntime()

    response = post_to(client_for(db, runtime), documents_url(), data)

    page = refused_page(response, 422)
    assert message in page.text
    assert "We need more documents before we can go on." in page.text
    assert arrived_names(db, DOCUMENTS_ID) == []
    assert runtime.calls == []


def test_a_documents_post_on_no_claim_is_the_claimant_404_page(
    fresh_database: DatabaseHandle,
) -> None:
    response = post_to(
        client_for(fresh_database, MoveRuntime()),
        documents_url("CLM-9999"),
        {"documents": "a"},
    )

    page = refused_page(response, 404)
    assert NO_SUCH_CLAIM in page.text
    assert BANNER in page.text


def test_a_documents_post_whose_names_were_stored_and_triage_failed_is_303(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    waiting_for_documents(db)
    runtime = MoveRuntime(start_http=502, start_body={"run_id": str(uuid.uuid4())})
    client = client_for(db, runtime)

    response = post_to(client, documents_url(), {"documents": "police report"})

    # The names are stored and the claim no longer waits for them: its state is
    # the answer, as for a submission whose triage failed.
    assert response.status_code == 303
    assert response.headers["location"] == status_url(DOCUMENTS_ID)
    assert arrived_names(db, DOCUMENTS_ID) == ["police report"]
    assert claim_state(db, DOCUMENTS_ID)[0] == "triage_failed"
    status = Page(client.get(response.headers["location"]).text)
    assert "An adjuster is reviewing your claim." in status.text
    assert Items(client.get(status_url(DOCUMENTS_ID)).text).items == ["police report"]


def test_a_documents_failure_with_nothing_stored_is_the_status_page_with_the_notice(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = fresh_database
    waiting_for_documents(db)

    def nothing_stored(*_: object) -> DecisionFailure:
        return DecisionFailure(503, DATABASE_DOWN)

    monkeypatch.setattr(f"{CLAIMANT_MODULE}.add_documents", nothing_stored)

    response = post_to(
        client_for(db, MoveRuntime()), documents_url(), {"documents": "police report"}
    )

    # The claim still waits for documents: the form is there to send them again.
    page = refused_page(response, 503)
    assert DATABASE_DOWN in page.text
    assert "We need more documents before we can go on." in page.text
    assert documents_url() in [str(f.get("action")) for f in page.attributes("form")]
    assert arrived_names(db, DOCUMENTS_ID) == []


def test_a_documents_failure_and_then_the_database_down_is_the_answers_error_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def triage_failed(*_: object) -> DecisionFailure:
        return DecisionFailure(502, RUN_FAILED)

    def database_down(*_: object) -> None:
        raise psycopg.OperationalError("the connection was lost")

    monkeypatch.setattr(f"{CLAIMANT_MODULE}.add_documents", triage_failed)
    monkeypatch.setattr(f"{CLAIMANT_MODULE}.load_status", database_down)

    response = post_to(make_client(), documents_url(), {"documents": "police report"})

    # The answer's status and text, not the read's (503, "the database is
    # unavailable"), on the claimant's error page.
    page = refused_page(response, 502)
    assert RUN_FAILED in page.text
    assert DATABASE_DOWN not in page.text
    assert BANNER in page.text
    assert "Back to your claims" in page.text


# ── the withdrawal ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("state", CAN_WITHDRAW)
def test_a_withdrawal_is_303_and_the_claim_is_withdrawn(
    fresh_database: DatabaseHandle, state: str
) -> None:
    db = fresh_database
    run_id = uuid.uuid4()
    put_claim(db, "CLM-9301", state, run_id=run_id)
    runtime = MoveRuntime()
    client = client_for(db, runtime)

    response = post_to(client, f"{status_url('CLM-9301')}/withdrawal")

    assert response.status_code == 303
    assert response.headers["location"] == status_url("CLM-9301")
    assert claim_state(db, "CLM-9301")[0] == "withdrawn"
    assert (
        "You withdrew this claim." in Page(client.get(status_url("CLM-9301")).text).text
    )
    # The paused run is ended, as the JSON route ends it.
    assert [r.url.path for r in runtime.resumes] == [f"/runs/{run_id}/resume"]


def test_a_withdrawal_of_a_claim_that_cannot_be_withdrawn_is_a_409_notice(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "approved")

    response = post_to(
        client_for(db, MoveRuntime()), f"{status_url('CLM-9301')}/withdrawal"
    )

    page = refused_page(response, 409)
    assert "the claim cannot be withdrawn in its state" in page.text
    assert "Your claim is approved." in page.text
    assert claim_state(db, "CLM-9301")[0] == "approved"


# ── a post another site made (T-70) ─────────────────────────────────────────
CROSS_SITE_HEADERS = {
    "another site's Origin": {"Origin": "https://evil.example"},
    "Sec-Fetch-Site cross-site": {"Sec-Fetch-Site": "cross-site"},
}


@pytest.mark.parametrize("headers", CROSS_SITE_HEADERS.values(), ids=CROSS_SITE_HEADERS)
def test_a_cross_site_post_of_each_form_is_403_the_claimant_page_and_changes_nothing(
    fresh_database: DatabaseHandle, headers: dict[str, str]
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "documents_requested", triages=1)
    runtime = MoveRuntime()
    client = client_for(db, runtime)
    posts = {
        "claim": (START_URL, form_of(claim_with_id("CLM-9501"))),
        "documents": (documents_url(), {"documents": "photos"}),
        "withdrawal": (f"{status_url('CLM-9301')}/withdrawal", {}),
    }

    for name, (url, data) in posts.items():
        response = post_to(client, url, data, headers)

        page = refused_page(response, 403)
        assert CROSS_SITE in page.text, name
        # The claimant's banner and way back; never the adjuster's.
        assert BANNER in page.text, name
        assert START_URL in page.links(), name
        assert "Back to your claims" in page.text, name
        assert not [x for x in page.links() if x.startswith("/adjuster/")], name
        assert "decide" not in page.text.lower(), name
        for header, value in SECURITY_HEADERS.items():
            assert response.headers[header] == value, name
    assert claims_held(db) == 1
    assert claim_state(db, "CLM-9301")[0] == "documents_requested"
    assert arrived_names(db, "CLM-9301") == []
    assert runtime.calls == []


def test_the_adjusters_cross_site_page_still_links_to_the_queue(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, "CLM-9301", "awaiting_adjuster")

    response = post_to(
        client_for(fresh_database, MoveRuntime()),
        "/adjuster/claims/CLM-9301/decision",
        {"decision": "approve", "run": ""},
        {"Origin": "https://evil.example"},
    )

    page = refused_page(response, 403)
    assert "/adjuster/claims" in page.links()
    assert "Back to the queue" in page.text
    assert "/claimant/claims" not in page.links()
    assert BANNER not in page.text


# ── the headers ─────────────────────────────────────────────────────────────
def test_the_headers_are_on_every_response_under_the_claimant_prefix(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "awaiting_adjuster")
    client = client_for(db, Runtime())
    responses = {
        "start page": client.get(START_URL),
        "status page": client.get(status_url("CLM-9301")),
        "404 claim": client.get(status_url("CLM-9302")),
        "422 form": post_claim(client, {}),
        "403": post_claim(
            client,
            form_of(claim_with_id("CLM-9501")),
            {"Origin": "https://evil.example"},
        ),
        "lookup": post_to(client, LOOKUP_URL, {"claim_id": "CLM-0042"}),
        "no route": client.get("/claimant/nothing"),
        "405": client.delete(START_URL),
        "422 claim ID": client.get("/claimant/claims/not-an-id"),
        "413": client.post(
            START_URL,
            content=b"a=" + b"x" * (70 * 1024),
            headers=SAME_ORIGIN | {"Content-Type": "application/x-www-form-urlencoded"},
        ),
    }

    for name, response in responses.items():
        for header, value in SECURITY_HEADERS.items():
            assert response.headers[header] == value, (name, header)
    assert [name for name, r in responses.items() if r.status_code == 404] == [
        "404 claim",
        "no route",
    ]
    assert responses["405"].status_code == 405
    assert responses["413"].status_code == 413


# ── the lookup ──────────────────────────────────────────────────────────────
LOOKUP_SENTINEL = "lookup.sentinel-31@example.com"


def test_a_lookup_by_a_valid_id_is_a_303_to_its_page() -> None:
    response = post_to(make_client(), LOOKUP_URL, {"claim_id": "CLM-0042"})

    assert response.status_code == 303
    assert response.headers["location"] == status_url("CLM-0042")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


LOOKUPS = ("clm-0042", "CLM-42", "CLM-00421", "CLM-0042\n", "", " CLM-0042", "../x")


@pytest.mark.parametrize("value", LOOKUPS)
def test_a_lookup_by_another_value_is_the_start_page_with_a_message_and_422(
    value: str,
) -> None:
    response = post_to(make_client(), LOOKUP_URL, {"claim_id": value})

    page = refused_page(response, 422)
    assert LOOKUP_MESSAGE in page.text
    assert "claim_id" in [i.get("name") for i in page.attributes("input")]
    assert "<script" not in response.text.lower()
    for name, header in SECURITY_HEADERS.items():
        assert response.headers[name] == header


def test_a_lookup_value_is_never_echoed() -> None:
    response = post_to(make_client(), LOOKUP_URL, {"claim_id": LOOKUP_SENTINEL})

    page = refused_page(response, 422)
    assert LOOKUP_MESSAGE in page.text
    assert LOOKUP_SENTINEL not in response.text


def test_a_lookup_that_names_the_id_twice_is_not_followed() -> None:
    response = post_to(
        make_client(), LOOKUP_URL, {"claim_id": ["CLM-0001", "CLM-0002"]}
    )

    page = refused_page(response, 422)
    assert LOOKUP_MESSAGE in page.text
    assert "location" not in response.headers


def test_a_lookup_with_no_field_is_the_start_page_with_a_message_and_422() -> None:
    response = post_to(make_client(), LOOKUP_URL, {})

    page = refused_page(response, 422)
    assert LOOKUP_MESSAGE in page.text


def test_a_lookup_that_sends_a_file_is_the_start_page_with_a_message_and_422() -> None:
    response = make_client().post(
        LOOKUP_URL,
        files={"claim_id": ("claim_id.txt", b"CLM-0042")},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    page = refused_page(response, 422)
    assert LOOKUP_MESSAGE in page.text
    assert "location" not in response.headers


def test_a_query_string_on_the_start_page_is_not_followed() -> None:
    client = make_client()

    for query in ("?claim_id=CLM-0042", "?claim_id=CLM-0001&claim_id=CLM-0002"):
        response = client.get(START_URL + query, follow_redirects=False)

        assert response.status_code == 200, query
        assert "location" not in response.headers, query
        page = Page(response.text)
        assert BANNER in page.text
        assert LOOKUP_MESSAGE not in page.text


def test_a_cross_site_lookup_post_is_403_the_claimant_page() -> None:
    client = make_client()

    for headers in CROSS_SITE_HEADERS.values():
        response = post_to(client, LOOKUP_URL, {"claim_id": "CLM-0042"}, headers)

        page = refused_page(response, 403)
        assert CROSS_SITE in page.text
        assert "location" not in response.headers
        assert START_URL in page.links()


def span_values(exporter: InMemorySpanExporter) -> list[str]:
    """Every name, attribute value and event attribute value of the spans."""
    seen: list[str] = []
    for span in exporter.get_finished_spans():
        seen.append(span.name)
        seen += [str(value) for value in (span.attributes or {}).values()]
        for event in span.events:
            seen += [str(value) for value in (event.attributes or {}).values()]
    return seen


def test_what_is_typed_in_the_lookup_is_on_no_span_of_the_request() -> None:
    exporter = InMemorySpanExporter()
    client = make_client(exporter=exporter)

    response = post_to(client, LOOKUP_URL, {"claim_id": LOOKUP_SENTINEL})

    assert response.status_code == 422
    seen = span_values(exporter)
    # The request was traced (the check is not vacuous), and none of it holds
    # the value, as typed or as a form or a URL encodes it.
    assert any(LOOKUP_URL in value for value in seen)
    for form_of_value in (LOOKUP_SENTINEL, LOOKUP_SENTINEL.replace("@", "%40")):
        assert not [value for value in seen if form_of_value in value]
    assert not [value for value in seen if "sentinel" in value]


# ── what is logged (T-03) ───────────────────────────────────────────────────
def leaks(caplog: pytest.LogCaptureFixture, needles: tuple[str, ...]) -> list[str]:
    seen = []
    for record in caplog.records:
        shown = " ".join(
            (
                record.getMessage(),
                str(record.args),
                str(record.exc_text),
                str(record.__dict__.get("exc_info")),
            )
        )
        seen += [needle for needle in needles if needle in shown]
    return seen


def test_nothing_of_the_claimant_is_logged_for_any_post_of_the_claim_form(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_database
    needles = (NAME_SENTINEL, EMAIL_SENTINEL, BAD_EMAIL_SENTINEL, DESCRIPTION_SENTINEL)
    client = client_for(db, Runtime())
    valid = form_of(
        claim_with_id("CLM-9501"),
        claimant_name=NAME_SENTINEL,
        claimant_email=EMAIL_SENTINEL,
        description=DESCRIPTION_SENTINEL,
    )

    with caplog.at_level(logging.DEBUG):
        assert post_claim(client, valid).status_code == 303
        assert (
            post_claim(
                client, valid | {"claim_id": "CLM-9502", "claimed_amount": "x"}
            ).status_code
            == 422
        )
        assert (
            post_claim(
                client, valid | {"claimant_email": BAD_EMAIL_SENTINEL}
            ).status_code
            == 422
        )
        assert post_claim(client, valid | {"claimed_amount": "9999"}).status_code == 409

    assert caplog.records
    assert leaks(caplog, needles) == []
    assert not any(needle in caplog.text for needle in needles)
