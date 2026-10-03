"""The adjuster's pages of the Claims API (S016): the queue, the claim, the
decision form and what T-07, T-33, T-70 and T-71 ask of them.

The pages are server-rendered and out of the OpenAPI contract. Their decision
form runs the code of ``POST /claims/{claim_id}/decision``; the tests here pin
that the results are the same (the claim's state, the decision's row, the audit
rows, the resume) and that a refusal reaches the page as the API's own text.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg.types.json import Jsonb
from servicesupport import claim_with_id, owner_rows
from workloads.claims_triage.test_claim_moves import (
    MoveRuntime,
    put_arrived,
    triages_of,
)
from workloads.claims_triage.test_claims_app import (
    CITATION,
    DECISION_ID,
    DECISION_OUTCOMES,
    OTHER_TENANT,
    OUTPUT,
    Runtime,
    awaiting_claim,
    claim_audit,
    claim_state,
    claims_dsn,
    decisions,
    failing_runtime,
    make_client,
    other_tenants_claim,
    resuming_runtime,
    set_claim,
)

from meridian.platform.common.audit import AuditUnavailable
from meridian.workloads.claims_triage import adjuster
from meridian.workloads.claims_triage import app as claims_app
from meridian.workloads.claims_triage.app import (
    BEING_APPLIED_DETAIL,
    DECIDED_OTHERWISE_DETAIL,
    NOT_WAITING_DETAIL,
    RESUME_FAILED_DETAIL,
)
from meridian.workloads.claims_triage.lifecycle import MAX_TRIAGES_PER_CLAIM
from meridian.workloads.claims_triage.models import DraftedBy
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.triaging import TRIAGE_CAP_DETAIL

TENANT = "claims-triage"
QUEUE_URL = "/adjuster/claims"
STYLESHEET_URL = "/adjuster/static/adjuster.css"
SAME_ORIGIN = {"Origin": "http://testserver"}
# What Chrome sends for the page's own post under ``Referrer-Policy:
# no-referrer``: an Origin of ``null``, and Fetch Metadata that says same-origin.
CHROME_SAME_ORIGIN = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; style-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
}
STYLESHEET_CACHE_CONTROL = "max-age=3600"
EMPTY_QUEUE = "No claim is waiting for an adjuster."
NO_SUCH_CLAIM = "no such claim"
FOREIGN_SITE = "the request came from another site"
AUDIT_DOWN = "the audit log is unavailable"
RESEND_TEXT = (
    "The decision is recorded and the run has not completed; "
    "sending it again completes it."
)
NO_STRUCTURED_PROPOSAL = "no structured proposal"
UNREADABLE_PROPOSAL = "the stored proposal could not be read"
NO_PROPOSAL = "no proposal is stored"
DATABASE_DOWN = "the database is unavailable"
MARKUP = "<script>alert(1)</script>"
MARKUP_IN_ATTRIBUTE = '"><img src=x onerror=alert(2)>'
ESCAPED_MARKUP = "&lt;script&gt;alert(1)&lt;/script&gt;"
CLAIMANT_NAME = "Bence Novak"
CLAIMANT_EMAIL = "bence.novak49@example.com"
CANARY = "description-canary-text-77"
LONG_AGO = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
# A proposal that shows every part the page lists: an exclusion the model found,
# a fraud indicator, a gap, a citation and the model's rationale.
RICH_PROPOSAL = OUTPUT | {
    "reason": "excluded",
    "recommendation": "reject",
    "exclusion_clause": "3.2",
    "fraud_indicators": ["early_loss"],
    "citations": [{**CITATION, "clause": "3.2"}],
    "gaps": ["claim_history"],
    "assessment": "applies",
    "unavailable_because": None,
    "rationale": "The roof was rotten before the storm.",
}


def url_of(claim_id: str) -> str:
    return f"{QUEUE_URL}/{claim_id}"


def decision_url(claim_id: str) -> str:
    return f"{url_of(claim_id)}/decision"


class Page(HTMLParser):
    """What the pages hold, read with the standard parser: every tag with its
    attributes, the text of each table row and of the whole page."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.rows: list[str] = []
        self._text: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "tr":
            self.rows.append("")
        elif tag in ("td", "th") and self.rows:
            self.rows[-1] += " "

    def handle_data(self, data: str) -> None:
        self._text.append(data)
        if self.rows:
            self.rows[-1] += data

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._text).split())

    def attributes(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]

    def links(self) -> list[str]:
        return [str(a["href"]) for a in self.attributes("a") if a.get("href")]

    def decision_buttons(self) -> list[str | None]:
        return [
            b.get("value")
            for b in self.attributes("button")
            if b.get("name") == "decision"
        ]


# ── fixtures: claims, proposals and trails written as the owner ─────────────
def put_claim(
    db: DatabaseHandle,
    claim_id: str,
    state: str = "awaiting_adjuster",
    *,
    changed_at: datetime = LONG_AGO,
    tenant: str = TENANT,
    run_id: uuid.UUID | None = None,
    triages: int = 0,
    **submission: Any,
) -> None:
    owner_rows(
        db,
        "INSERT INTO claims.claims "
        "(claim_id, tenant, submission, state, state_changed_at, run_id, triages) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING 1",
        (
            claim_id,
            tenant,
            Jsonb(claim_with_id(claim_id) | submission),
            state,
            changed_at,
            run_id,
            triages,
        ),
    )


def put_proposal(
    db: DatabaseHandle,
    claim_id: str,
    proposal: dict[str, Any] | None,
    *,
    created_at: datetime = LONG_AGO,
) -> None:
    """A stored proposal; ``None`` is a row of the walking skeleton, which has a
    draft and no document."""
    route, reason = (
        (proposal["route"], proposal["reason"]) if proposal else ("adjuster", "old")
    )
    owner_rows(
        db,
        "INSERT INTO claims.triage_proposals "
        "(proposal_id, claim_id, run_id, route, reason, draft, proposal, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING 1",
        (
            uuid.uuid4(),
            claim_id,
            uuid.uuid4(),
            route,
            reason,
            None if proposal else "an old draft",
            Jsonb(proposal) if proposal else None,
            created_at,
        ),
    )


def put_event(db: DatabaseHandle, run_id: uuid.UUID, event: str) -> None:
    owner_rows(
        db,
        "INSERT INTO audit.events (service, event, outcome, run_id) "
        "VALUES ('agent-runtime', %s, 'ok', %s) RETURNING 1",
        (event, run_id),
    )


def put_trail(db: DatabaseHandle, claim_id: str, events: list[str]) -> None:
    """The events of a run of the claim, each in a transaction of its own, so
    their ``recorded_at`` follows the order given."""
    run_id = uuid.uuid4()
    owner_rows(
        db,
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status) VALUES (%s, %s, 'claims-triage', %s, %s, 'Completed') RETURNING 1",
        (run_id, uuid.uuid4(), TENANT, claim_id),
    )
    for event in events:
        put_event(db, run_id, event)


def put_decision(
    db: DatabaseHandle, claim_id: str, word: str, run_id: uuid.UUID | None
) -> None:
    """A recorded word for ``run_id``; ``None`` is a decision with no run."""
    owner_rows(
        db,
        "INSERT INTO claims.decisions (claim_id, run_id, decision) "
        "VALUES (%s, %s, %s) RETURNING 1",
        (claim_id, run_id, word),
    )


def client_for(db: DatabaseHandle, runtime: Runtime | None = None) -> TestClient:
    return make_client(claims_dsn(db), runtime)


def page_run(client: TestClient, claim_id: str) -> str:
    """The ``run`` a browser would post: the hidden field of the claim's page as
    it reads now, or empty when the page has none (a claim with no run, a page
    that cannot be read)."""
    response = client.get(url_of(claim_id))
    if response.status_code != 200:
        return ""
    runs = [
        str(i.get("value"))
        for i in Page(response.text).attributes("input")
        if i.get("name") == "run"
    ]
    return runs[0] if runs else ""


def post_form(
    client: TestClient,
    claim_id: str,
    word: str,
    headers: dict[str, str] | None = None,
    run: str | None = None,
) -> httpx.Response:
    """The decision form's post. ``run`` is the page's hidden field (T-33): by
    default the page is read first, as a browser does; a test of a page read
    earlier gives the run it saw."""
    return client.post(
        decision_url(claim_id),
        data={
            "decision": word,
            "run": page_run(client, claim_id) if run is None else run,
        },
        headers=headers,
        follow_redirects=False,
    )


# ── the queue ───────────────────────────────────────────────────────────────
def test_the_queue_lists_the_claims_that_wait_of_the_tenant_oldest_first(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9302", "awaiting_adjuster", changed_at=LONG_AGO)
    put_claim(db, "CLM-9301", "awaiting_adjuster", changed_at=LONG_AGO)
    put_claim(db, "CLM-9303", "triage_failed", changed_at=LONG_AGO - timedelta(hours=1))
    put_claim(db, "CLM-9304", "awaiting_adjuster", changed_at=LONG_AGO + timedelta(1))
    other_states = ("submitted", "triaging", "approved", "rejected")
    for number, other_state in enumerate(other_states, start=1):
        put_claim(db, f"CLM-940{number}", other_state)
    put_claim(db, "CLM-9305", "awaiting_adjuster", tenant=OTHER_TENANT)

    response = client_for(db).get(QUEUE_URL)

    assert response.status_code == 200
    links = Page(response.text).links()
    # Oldest change first; the same moment by claim ID; another tenant's claim,
    # and any other state, absent.
    assert [link for link in links if link.startswith(f"{QUEUE_URL}/")] == [
        url_of(claim_id)
        for claim_id in ("CLM-9303", "CLM-9301", "CLM-9302", "CLM-9304")
    ]


def test_a_queue_row_shows_state_since_peril_amount_and_the_latest_reason(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", peril="storm", claimed_amount=2890)
    put_proposal(
        db, "CLM-9301", OUTPUT | {"reason": "over_threshold"}, created_at=LONG_AGO
    )
    put_proposal(db, "CLM-9301", OUTPUT, created_at=LONG_AGO + timedelta(hours=1))
    put_claim(db, "CLM-9302", "triage_failed")

    response = client_for(db).get(QUEUE_URL)

    rows = {row.split()[0]: row for row in Page(response.text).rows if row.split()}
    assert "awaiting_adjuster" in rows["CLM-9301"]
    assert "2026-10-01 12:00:00 UTC" in rows["CLM-9301"]
    assert "storm" in rows["CLM-9301"]
    assert "€2,890" in rows["CLM-9301"]
    # The latest proposal's reason, not the older one's.
    assert "unverified" in rows["CLM-9301"]
    assert "over_threshold" not in rows["CLM-9301"]
    # A claim with no proposal has no reason.
    assert "triage_failed" in rows["CLM-9302"]
    assert "unverified" not in rows["CLM-9302"]


def test_the_queue_shows_at_most_100_claims_the_oldest_ones(
    fresh_database: DatabaseHandle,
) -> None:
    owner_rows(
        fresh_database,
        "INSERT INTO claims.claims (claim_id, tenant, submission, state, "
        "state_changed_at) SELECT 'CLM-' || (1000 + i), %s, '{}'::jsonb, "
        "'awaiting_adjuster', %s + make_interval(mins => i) "
        "FROM generate_series(0, 100) AS i RETURNING 1",
        (TENANT, LONG_AGO),
    )

    response = client_for(fresh_database).get(QUEUE_URL)

    assert response.status_code == 200
    links = [x for x in Page(response.text).links() if x.startswith(f"{QUEUE_URL}/")]
    assert len(links) == 100
    assert url_of("CLM-1000") in links
    assert url_of("CLM-1099") in links
    assert url_of("CLM-1100") not in links


def test_an_empty_queue_says_so(fresh_database: DatabaseHandle) -> None:
    put_claim(fresh_database, "CLM-9301", "approved")

    response = client_for(fresh_database).get(QUEUE_URL)

    assert response.status_code == 200
    assert EMPTY_QUEUE in Page(response.text).text


def test_the_queue_without_the_database_is_503_with_the_apis_text() -> None:
    response = make_client().get(QUEUE_URL)

    assert response.status_code == 503
    assert DATABASE_DOWN in Page(response.text).text


# ── the claim ───────────────────────────────────────────────────────────────
def test_the_claim_page_shows_the_proposal_next_to_its_evidence(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", description="The roof tiles had fallen.")
    put_proposal(db, "CLM-9301", RICH_PROPOSAL)

    response = client_for(db).get(url_of("CLM-9301"))

    assert response.status_code == 200
    text = Page(response.text).text
    for shown in (
        "CLM-9301",
        "awaiting_adjuster",
        "POL-0049",
        "storm",
        "2026-07-13",
        "€2,890",
        "Linz",
        "photos",
        "The roof tiles had fallen.",
        # the proposal
        "adjuster",
        "excluded",
        "reject",
        "3.2",
        "early_loss",
        "claim_history",
        "HOME-STD",
        "2026-01",
        "The roof was rotten before the storm.",
        "replay-chat",
    ):
        assert shown in text, shown


def test_the_claim_page_shows_the_first_12_hex_of_the_prompt_version(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    version = "0123456789abcdef" * 4
    put_claim(db, "CLM-9301")
    drafted_by = RICH_PROPOSAL["drafted_by"] | {"prompt": version}
    put_proposal(db, "CLM-9301", RICH_PROPOSAL | {"drafted_by": drafted_by})

    response = client_for(db).get(url_of("CLM-9301"))

    assert response.status_code == 200
    text = Page(response.text).text
    assert "replay-chat, replay, replay, prompt 0123456789ab" in text
    assert "0123456789abc" not in text


def test_the_claim_page_of_a_proposal_stored_before_s017_names_no_prompt(
    fresh_database: DatabaseHandle,
) -> None:
    old = {k: v for k, v in RICH_PROPOSAL["drafted_by"].items() if k != "prompt"}
    put_claim(fresh_database, "CLM-9301")
    put_proposal(fresh_database, "CLM-9301", RICH_PROPOSAL | {"drafted_by": old})

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert response.status_code == 200
    text = Page(response.text).text
    assert "replay-chat, replay, replay" in text
    assert "prompt" not in text


def test_the_claim_page_leaves_out_the_claimants_name_and_email(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, "CLM-9301")
    put_proposal(fresh_database, "CLM-9301", OUTPUT)

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert response.status_code == 200
    assert CLAIMANT_NAME not in response.text
    assert CLAIMANT_EMAIL not in response.text
    assert "claimant" not in response.text.lower()


def test_the_claim_page_shows_the_trail_of_the_claim_in_order_and_no_other(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301")
    put_claim(db, "CLM-9302")
    # Written in this order; alphabetically the order would be the reverse.
    put_trail(db, "CLM-9301", ["run.started", "claim.awaiting", "a.last"])
    put_trail(db, "CLM-9302", ["run.of-another-claim"])

    response = client_for(db).get(url_of("CLM-9301"))

    assert response.status_code == 200
    html = response.text
    positions = [html.index(e) for e in ("run.started", "claim.awaiting", "a.last")]
    assert positions == sorted(positions)
    # Each row names the role the database stamped, and the service.
    (started,) = [r for r in Page(html).rows if "run.started" in r]
    assert {"meridian_owner", "agent-runtime"} <= set(started.split())
    assert "run.of-another-claim" not in html


def test_a_draft_only_proposal_is_said_to_have_no_structured_proposal(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, "CLM-9301")
    put_proposal(fresh_database, "CLM-9301", None)

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert response.status_code == 200
    assert NO_STRUCTURED_PROPOSAL in Page(response.text).text


def test_a_proposal_that_fails_validation_is_said_so_and_the_page_still_renders(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    put_claim(fresh_database, "CLM-9301")
    put_proposal(
        fresh_database,
        "CLM-9301",
        {"route": "adjuster", "reason": "unverified", "rationale": MARKUP},
    )

    with caplog.at_level(logging.DEBUG):
        response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert response.status_code == 200
    assert UNREADABLE_PROPOSAL in Page(response.text).text
    assert MARKUP not in response.text
    # The log names the claim and the exception's class, and nothing stored.
    messages = [r.getMessage() for r in caplog.records]
    assert any("CLM-9301" in m and "ValidationError" in m for m in messages)
    assert not any("alert(1)" in m for m in messages)


def test_a_claim_without_a_proposal_says_none_is_stored(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, "CLM-9301", "triage_failed")

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert response.status_code == 200
    assert NO_PROPOSAL in Page(response.text).text


def test_the_claim_page_shows_the_recorded_decision(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    put_claim(fresh_database, "CLM-9301", "approved", run_id=run_id)
    put_decision(fresh_database, "CLM-9301", "approve", run_id)

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert response.status_code == 200
    assert "Decision recorded: approve" in Page(response.text).text


def test_an_unknown_claim_and_another_tenants_claim_are_the_same_404_page(
    fresh_database: DatabaseHandle,
) -> None:
    other_tenants_claim(fresh_database)
    client = client_for(fresh_database)

    other = client.get(url_of(DECISION_ID))
    unknown = client.get(url_of("CLM-9999"))

    assert (other.status_code, unknown.status_code) == (404, 404)
    assert NO_SUCH_CLAIM in Page(other.text).text
    assert other.text == unknown.text.replace("CLM-9999", DECISION_ID)


def test_a_claim_id_that_is_not_one_is_422_and_reads_nothing() -> None:
    response = make_client().get(url_of("not-a-claim"))

    assert response.status_code == 422


# ── the decision buttons (T-33) ─────────────────────────────────────────────
def test_a_waiting_claim_has_three_unselected_buttons_below_the_proposal(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, "CLM-9301")
    put_proposal(fresh_database, "CLM-9301", RICH_PROPOSAL)

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    page = Page(response.text)
    assert page.decision_buttons() == ["approve", "reject", "request_documents"]
    assert all(
        b.get("type") == "submit"
        for b in page.attributes("button")
        if b.get("name") == "decision"
    )
    assert [f["method"] for f in page.attributes("form")] == ["post"]
    assert [f["action"] for f in page.attributes("form")] == [decision_url("CLM-9301")]
    # Nothing is preselected, focused or a default.
    assert_nothing_is_preselected(page)
    # The proposal, with its evidence, comes first.
    html = response.text
    first_button = html.index('name="decision"')
    for evidence in ("The roof was rotten", "3.2", "early_loss", "claim_history"):
        assert html.index(evidence) < first_button, evidence


@pytest.mark.parametrize(
    "state",
    [
        "submitted",
        "triaging",
        "documents_requested",
        "approved",
        "rejected",
        "withdrawn",
    ],
)
def test_a_claim_that_does_not_wait_has_no_decision_form(
    fresh_database: DatabaseHandle, state: str
) -> None:
    put_claim(fresh_database, "CLM-9301", state)

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    page = Page(response.text)
    assert response.status_code == 200
    assert page.decision_buttons() == []
    assert not page.attributes("form")


# ── send back, triage again, and a claim with no paused run (S048) ──────────
SEND_BACK_BUTTON = "Send back to triage"
TRIAGE_AGAIN_BUTTON = "Triage again"
SEND_BACK_LINE = "ends the paused run and triages the claim again"
CAP_LINE = "The claim has been triaged five times"
NO_RUN_LINE = "There is no paused run and no new proposal"
FAILED_LINE = "The triage failed and there is no proposal to read"
EARLIER_LINE = "The latest triage failed; the proposal shown is from an earlier triage"
NO_RUN_NO_PROPOSAL_LINE = "triaged five times, and there is no proposal"
ARRIVED_LABEL = "Documents that arrived later"
NOT_TRIAGEABLE = "the claim cannot be triaged again in its state"
THREE_DECISIONS = ["approve", "reject", "request_documents"]
CAP = MAX_TRIAGES_PER_CLAIM
CLAIM = "CLM-9301"


def triage_url(claim_id: str) -> str:
    return f"{url_of(claim_id)}/triage"


def post_triage(
    client: TestClient,
    claim_id: str,
    headers: dict[str, str] | None = None,
    run: str | None = None,
) -> httpx.Response:
    """The triage form's post; ``run`` as for ``post_form``."""
    return client.post(
        triage_url(claim_id),
        data={"run": page_run(client, claim_id) if run is None else run},
        headers=headers,
        follow_redirects=False,
    )


def moves_client(db: DatabaseHandle, runtime: MoveRuntime) -> TestClient:
    # make_client wants a ``Runtime``; it only uses the ``client`` it holds.
    return make_client(claims_dsn(db), runtime)  # type: ignore[arg-type]


def form_actions(page: Page) -> list[str]:
    return [str(f["action"]) for f in page.attributes("form")]


def button_fields(page: Page) -> list[tuple[str | None, str | None, str | None]]:
    """Each button's type, name and value, in page order."""
    return [
        (b.get("type"), b.get("name"), b.get("value"))
        for b in page.attributes("button")
    ]


def assert_nothing_is_preselected(page: Page) -> None:
    for _, attrs in page.tags:
        assert not {"checked", "selected", "autofocus"} & set(attrs)
    # The only fields are the forms' hidden ``run`` (T-33): a hidden ``decision``
    # would be a decision made for the adjuster.
    for field in page.attributes("input"):
        assert (field["type"], field["name"]) == ("hidden", "run")


def test_a_paused_claim_offers_send_back_in_a_form_of_its_own_after_the_decisions(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, run_id=uuid.uuid4(), triages=1)
    put_proposal(db, CLAIM, RICH_PROPOSAL)

    response = client_for(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert page.decision_buttons() == THREE_DECISIONS
    assert form_actions(page) == [decision_url(CLAIM), triage_url(CLAIM)]
    assert [f["method"] for f in page.attributes("form")] == ["post", "post"]
    # Every button is its own submit; send-back carries no field and no value.
    assert button_fields(page) == [
        ("submit", "decision", "approve"),
        ("submit", "decision", "reject"),
        ("submit", "decision", "request_documents"),
        ("submit", None, None),
    ]
    assert_nothing_is_preselected(page)
    assert SEND_BACK_BUTTON in page.text
    assert SEND_BACK_LINE in page.text
    assert CAP_LINE not in page.text
    assert NO_RUN_LINE not in page.text
    assert response.text.index('value="request_documents"') < response.text.index(
        SEND_BACK_BUTTON
    )


def test_sending_a_paused_claim_back_redirects_and_triages_it_again(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    old = uuid.uuid4()
    put_claim(db, CLAIM, run_id=old, triages=1)
    put_proposal(db, CLAIM, RICH_PROPOSAL)
    runtime = MoveRuntime()
    client = moves_client(db, runtime)

    response = post_triage(client, CLAIM, SAME_ORIGIN)

    assert response.status_code == 303
    assert response.headers["location"] == url_of(CLAIM)
    # The old run is ended once, then one new run starts.
    assert runtime.calls == ["resume", "start"]
    (resume,) = runtime.resumes
    assert resume.url.path == f"/runs/{old}/resume"
    assert decisions(db) == [(CLAIM, old, "send_back")]
    assert claim_state(db, CLAIM) == ("awaiting_adjuster", runtime.run_id)
    assert triages_of(db, CLAIM) == 2
    page = Page(client.get(url_of(CLAIM)).text)
    # The page shows the new state: the move to triaging and the move back to
    # the adjuster are in the trail, the claim waits on the new run, and the
    # send-back is not shown as a decision.
    assert "claim.triaging" in page.text
    assert "claim.awaiting_adjuster" in page.text
    assert "Decision recorded" not in page.text
    assert RESEND_TEXT not in page.text
    assert page.decision_buttons() == THREE_DECISIONS
    assert SEND_BACK_BUTTON in page.text


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Origin": "http://evil.example"}, id="another-origin"),
        pytest.param({"Sec-Fetch-Site": "cross-site"}, id="cross-site"),
    ],
)
def test_a_cross_site_post_of_the_send_back_form_is_403_and_nothing_moves(
    fresh_database: DatabaseHandle, headers: dict[str, str]
) -> None:
    db = fresh_database
    old = uuid.uuid4()
    put_claim(db, CLAIM, run_id=old, triages=1)
    runtime = MoveRuntime()

    response = post_triage(moves_client(db, runtime), CLAIM, headers)

    assert response.status_code == 403
    assert FOREIGN_SITE in Page(response.text).text
    assert runtime.calls == []
    assert claim_state(db, CLAIM) == ("awaiting_adjuster", old)
    assert triages_of(db, CLAIM) == 1
    assert decisions(db) == []
    assert claim_audit(db, CLAIM) == []


@pytest.mark.parametrize(("triages", "offered"), [(CAP - 1, True), (CAP, False)])
def test_send_back_is_offered_below_the_cap_and_hidden_at_it(
    fresh_database: DatabaseHandle, triages: int, offered: bool
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, run_id=uuid.uuid4(), triages=triages)

    response = client_for(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert page.decision_buttons() == THREE_DECISIONS
    assert (triage_url(CLAIM) in form_actions(page)) is offered
    assert (SEND_BACK_BUTTON in page.text) is offered
    assert (SEND_BACK_LINE in page.text) is offered
    # At the cap the page says why there is no button.
    assert (CAP_LINE in page.text) is not offered


def test_a_send_back_at_the_cap_renders_409_with_the_caps_text_and_moves_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    old = uuid.uuid4()
    put_claim(db, CLAIM, run_id=old, triages=CAP)
    runtime = MoveRuntime()

    response = post_triage(moves_client(db, runtime), CLAIM, SAME_ORIGIN)

    assert response.status_code == 409
    page = Page(response.text)
    assert f"409: {TRIAGE_CAP_DETAIL}" in page.text
    assert page.decision_buttons() == THREE_DECISIONS
    assert runtime.calls == []
    assert decisions(db) == []
    assert claim_state(db, CLAIM) == ("awaiting_adjuster", old)


def test_a_triage_post_for_a_claim_in_another_state_renders_409(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, "approved")
    runtime = MoveRuntime()

    response = post_triage(moves_client(fresh_database, runtime), CLAIM, SAME_ORIGIN)

    assert response.status_code == 409
    assert f"409: {NOT_TRIAGEABLE}" in Page(response.text).text
    assert runtime.calls == []
    assert claim_state(fresh_database, CLAIM) == ("approved", None)


def test_a_triage_post_for_an_unknown_claim_renders_the_404_page(
    fresh_database: DatabaseHandle,
) -> None:
    runtime = MoveRuntime()

    response = post_triage(moves_client(fresh_database, runtime), "CLM-9999")

    assert response.status_code == 404
    assert NO_SUCH_CLAIM in Page(response.text).text
    assert runtime.calls == []


def test_a_triage_post_with_the_database_down_renders_the_503_page() -> None:
    runtime = MoveRuntime()

    response = post_triage(make_client(runtime=runtime), CLAIM)  # type: ignore[arg-type]

    assert response.status_code == 503
    assert DATABASE_DOWN in Page(response.text).text
    assert runtime.calls == []


def test_a_send_back_whose_new_triage_fails_renders_the_failed_claim_with_the_notice(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    old, failed = uuid.uuid4(), uuid.uuid4()
    put_claim(db, CLAIM, run_id=old, triages=1)
    runtime = MoveRuntime(
        start_http=502, start_body={"run_id": str(failed), "status": "Failed"}
    )

    response = post_triage(moves_client(db, runtime), CLAIM, SAME_ORIGIN)

    assert response.status_code == 502
    page = Page(response.text)
    assert "502: the triage run did not complete; the claim is stored" in page.text
    assert FAILED_LINE in page.text
    assert page.decision_buttons() == THREE_DECISIONS
    assert TRIAGE_AGAIN_BUTTON in page.text
    assert "Decision recorded" not in page.text
    assert claim_state(db, CLAIM) == ("triage_failed", failed)
    assert decisions(db) == [(CLAIM, old, "send_back")]


@pytest.mark.parametrize("failed_run", [None, uuid.uuid4()], ids=["no-run", "run"])
def test_a_failed_triage_offers_the_three_decisions_and_triage_again(
    fresh_database: DatabaseHandle, failed_run: uuid.UUID | None
) -> None:
    put_claim(fresh_database, CLAIM, "triage_failed", run_id=failed_run, triages=1)

    response = client_for(fresh_database).get(url_of(CLAIM))

    page = Page(response.text)
    assert FAILED_LINE in page.text
    assert "a decision refers the claim to you and records it" in page.text
    assert "S048" not in page.text
    assert NO_PROPOSAL in page.text
    assert page.decision_buttons() == THREE_DECISIONS
    assert form_actions(page) == [decision_url(CLAIM), triage_url(CLAIM)]
    assert button_fields(page)[-1] == ("submit", None, None)
    assert len(page.attributes("button")) == 4
    assert_nothing_is_preselected(page)
    assert TRIAGE_AGAIN_BUTTON in page.text
    assert SEND_BACK_BUTTON not in page.text
    assert NO_RUN_LINE not in page.text
    assert CAP_LINE not in page.text


def test_a_failed_triage_over_an_older_proposal_says_the_proposal_is_from_before(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, "triage_failed", run_id=uuid.uuid4(), triages=2)
    put_proposal(db, CLAIM, RICH_PROPOSAL)

    response = client_for(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert EARLIER_LINE in page.text
    # It does not say there is no proposal over the one it shows.
    assert FAILED_LINE not in page.text
    assert "no proposal to read" not in page.text
    assert "The roof was rotten before the storm." in page.text
    assert page.decision_buttons() == THREE_DECISIONS
    assert form_actions(page) == [decision_url(CLAIM), triage_url(CLAIM)]
    # Every button is its own submit and none is preselected.
    assert [b[0] for b in button_fields(page)] == ["submit"] * 4
    assert_nothing_is_preselected(page)
    assert TRIAGE_AGAIN_BUTTON in page.text
    # The line comes above the buttons.
    assert response.text.index(EARLIER_LINE) < response.text.index('name="decision"')


def test_a_send_back_whose_new_triage_fails_over_the_old_proposal_says_so(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, run_id=uuid.uuid4(), triages=1)
    put_proposal(db, CLAIM, RICH_PROPOSAL)
    failed = uuid.uuid4()
    runtime = MoveRuntime(
        start_http=502, start_body={"run_id": str(failed), "status": "Failed"}
    )

    response = post_triage(moves_client(db, runtime), CLAIM, SAME_ORIGIN)

    assert response.status_code == 502
    page = Page(response.text)
    assert "502: the triage run did not complete; the claim is stored" in page.text
    assert EARLIER_LINE in page.text
    assert FAILED_LINE not in page.text


@pytest.mark.parametrize(
    ("with_proposal", "line"),
    [
        pytest.param(True, "the proposal shown predates the documents", id="proposal"),
        pytest.param(False, NO_RUN_NO_PROPOSAL_LINE, id="none"),
    ],
)
def test_a_claim_with_no_run_says_what_the_proposal_shown_is_or_that_there_is_none(
    fresh_database: DatabaseHandle, with_proposal: bool, line: str
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, "awaiting_adjuster", triages=CAP)
    if with_proposal:
        put_proposal(db, CLAIM, RICH_PROPOSAL)

    response = client_for(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert NO_RUN_LINE in page.text
    assert line in page.text
    assert (EARLIER_LINE in page.text) is False
    assert page.decision_buttons() == THREE_DECISIONS


def test_a_failed_triage_at_the_cap_has_the_decisions_and_no_triage_again(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, "triage_failed", triages=CAP)

    response = client_for(fresh_database).get(url_of(CLAIM))

    page = Page(response.text)
    assert page.decision_buttons() == THREE_DECISIONS
    assert form_actions(page) == [decision_url(CLAIM)]
    assert TRIAGE_AGAIN_BUTTON not in page.text
    assert FAILED_LINE in page.text
    assert CAP_LINE in page.text


@pytest.mark.parametrize("word", DECISION_OUTCOMES)
def test_a_decision_on_a_failed_triage_redirects_and_the_page_shows_it_decided(
    fresh_database: DatabaseHandle, word: str
) -> None:
    db = fresh_database
    state, _ = DECISION_OUTCOMES[word]
    put_claim(db, CLAIM, "triage_failed", run_id=uuid.uuid4(), triages=1)
    runtime = MoveRuntime()
    client = moves_client(db, runtime)

    response = post_form(client, CLAIM, word, SAME_ORIGIN)

    assert response.status_code == 303
    assert response.headers["location"] == url_of(CLAIM)
    assert runtime.calls == []
    assert decisions(db) == [(CLAIM, None, word)]
    assert claim_state(db, CLAIM) == (state, None)
    page = Page(client.get(url_of(CLAIM)).text)
    assert f"Decision recorded: {word}" in page.text
    # A decision with no run has nothing to resume, so nothing to send again.
    assert RESEND_TEXT not in page.text
    assert not page.attributes("form")
    assert not page.attributes("input")


def test_triage_again_from_the_page_triages_a_failed_claim(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, "triage_failed", run_id=uuid.uuid4(), triages=1)
    runtime = MoveRuntime()
    client = moves_client(db, runtime)

    response = post_triage(client, CLAIM, SAME_ORIGIN)

    assert response.status_code == 303
    assert response.headers["location"] == url_of(CLAIM)
    # A failed run is not paused: nothing is resumed, and no word is recorded.
    assert runtime.calls == ["start"]
    assert decisions(db) == []
    assert claim_state(db, CLAIM) == ("awaiting_adjuster", runtime.run_id)
    assert triages_of(db, CLAIM) == 2
    page = Page(client.get(url_of(CLAIM)).text)
    assert page.decision_buttons() == THREE_DECISIONS
    assert SEND_BACK_BUTTON in page.text
    assert FAILED_LINE not in page.text


def test_a_claim_referred_at_the_cap_says_it_has_no_run_and_offers_no_send_back(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, "awaiting_adjuster", triages=CAP)
    put_proposal(db, CLAIM, RICH_PROPOSAL)

    response = client_for(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert NO_RUN_LINE in page.text
    assert "triaged five times" in page.text
    assert "predates the documents" in page.text
    assert page.decision_buttons() == THREE_DECISIONS
    assert form_actions(page) == [decision_url(CLAIM)]
    assert SEND_BACK_BUTTON not in page.text
    assert TRIAGE_AGAIN_BUTTON not in page.text
    assert FAILED_LINE not in page.text
    # The line comes above the buttons.
    assert response.text.index(NO_RUN_LINE) < response.text.index('name="decision"')


@pytest.mark.parametrize("word", DECISION_OUTCOMES)
def test_a_decision_on_a_claim_referred_at_the_cap_is_shown_and_not_sent_again(
    fresh_database: DatabaseHandle, word: str
) -> None:
    db = fresh_database
    state, _ = DECISION_OUTCOMES[word]
    put_claim(db, CLAIM, "awaiting_adjuster", triages=CAP)
    runtime = MoveRuntime()
    client = moves_client(db, runtime)

    response = post_form(client, CLAIM, word, SAME_ORIGIN)

    assert response.status_code == 303
    assert runtime.calls == []
    assert decisions(db) == [(CLAIM, None, word)]
    assert claim_state(db, CLAIM) == (state, None)
    page = Page(client.get(url_of(CLAIM)).text)
    assert f"Decision recorded: {word}" in page.text
    assert RESEND_TEXT not in page.text
    assert not page.attributes("form")


@pytest.mark.parametrize("state", ["awaiting_adjuster", "triage_failed", "withdrawn"])
def test_a_state_no_decision_leads_to_shows_none_even_with_a_row_for_the_run(
    fresh_database: DatabaseHandle, state: str
) -> None:
    run_id = uuid.uuid4()
    put_claim(fresh_database, CLAIM, state, run_id=run_id, triages=1)
    put_decision(fresh_database, CLAIM, "approve", run_id)

    response = client_for(fresh_database).get(url_of(CLAIM))

    page = Page(response.text)
    assert "Decision recorded" not in page.text
    assert RESEND_TEXT not in page.text
    assert "decision" not in [i.get("name") for i in page.attributes("input")]


@pytest.mark.parametrize(
    ("state", "claims_run_is_the_old_one"),
    [
        pytest.param("triaging", True, id="triaging"),
        pytest.param("approved", False, id="decided-on-a-newer-run"),
        pytest.param("approved", True, id="word-is-not-a-decision"),
        pytest.param("documents_requested", False, id="asked-on-a-newer-run"),
    ],
)
def test_a_claim_sent_back_shows_no_decision_and_no_send_again_form(
    fresh_database: DatabaseHandle, state: str, claims_run_is_the_old_one: bool
) -> None:
    db = fresh_database
    old, newer = uuid.uuid4(), uuid.uuid4()
    put_claim(db, CLAIM, state, run_id=old if claims_run_is_the_old_one else newer)
    put_decision(db, CLAIM, "send_back", old)

    response = client_for(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert response.status_code == 200
    assert "Decision recorded" not in page.text
    assert "send_back" not in page.text
    assert RESEND_TEXT not in page.text
    assert not page.attributes("input")
    assert not page.attributes("form")


@pytest.mark.parametrize("claims_run_is_the_old_one", [True, False])
def test_a_withdrawn_claim_shows_no_decision_and_no_send_again_form(
    fresh_database: DatabaseHandle, claims_run_is_the_old_one: bool
) -> None:
    db = fresh_database
    old = uuid.uuid4()
    put_claim(db, CLAIM, "withdrawn", run_id=old if claims_run_is_the_old_one else None)
    put_decision(db, CLAIM, "withdrawn", old)

    response = client_for(db).get(url_of(CLAIM))

    page = Page(response.text)
    assert response.status_code == 200
    assert "Decision recorded" not in page.text
    assert RESEND_TEXT not in page.text
    assert not page.attributes("form")
    assert not page.attributes("input")


def test_a_decision_of_another_run_than_the_claims_is_not_the_one_shown(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    old, newer = uuid.uuid4(), uuid.uuid4()
    put_claim(db, CLAIM, "approved", run_id=newer)
    put_decision(db, CLAIM, "reject", old)

    response = client_for(db).get(url_of(CLAIM))

    assert "Decision recorded" not in Page(response.text).text


def test_documents_that_arrived_are_listed_after_the_submissions_and_labelled(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM, run_id=uuid.uuid4(), documents=["photos"])
    put_arrived(db, CLAIM, "invoice", timedelta(days=2))
    put_arrived(db, CLAIM, "estimate", timedelta(days=1))

    response = client_for(db).get(url_of(CLAIM))

    # In the order of arrival, in a row of their own, after the submission's.
    assert (
        f"Documents named photos {ARRIVED_LABEL} invoice, estimate Description"
        in Page(response.text).text
    )


def test_a_claim_with_no_documents_that_arrived_has_no_row_for_them(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)

    response = client_for(fresh_database).get(url_of(CLAIM))

    assert ARRIVED_LABEL not in Page(response.text).text


def test_markup_in_the_name_of_an_arrived_document_is_escaped(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, CLAIM)
    put_arrived(db, CLAIM, MARKUP + MARKUP_IN_ATTRIBUTE)

    response = client_for(db).get(url_of(CLAIM))

    assert response.status_code == 200
    html = response.text
    assert MARKUP not in html
    assert "<img" not in html
    assert ESCAPED_MARKUP in html
    assert not [t for t, _ in Page(html).tags if t in ("script", "img")]


def test_every_answer_of_the_triage_route_carries_the_security_headers(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", run_id=uuid.uuid4(), triages=1)
    put_claim(db, "CLM-9302", run_id=uuid.uuid4(), triages=CAP)
    client = moves_client(db, MoveRuntime())
    responses = {
        "403 page": post_triage(client, "CLM-9301", {"Origin": "http://x.y"}),
        "404 page": post_triage(client, "CLM-9999"),
        "409 page": post_triage(client, "CLM-9302"),
        "422": post_triage(client, "nonsense"),
        "303": post_triage(client, "CLM-9301", SAME_ORIGIN),
    }

    assert {n: r.status_code for n, r in responses.items()} == {
        "403 page": 403,
        "404 page": 404,
        "409 page": 409,
        "422": 422,
        "303": 303,
    }
    for name, response in responses.items():
        for header, value in SECURITY_HEADERS.items():
            assert response.headers.get(header) == value, (name, header)


# ── T-07: markup is text ────────────────────────────────────────────────────
def test_markup_in_the_description_and_the_rationale_is_escaped(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, "CLM-9301", description=MARKUP + MARKUP_IN_ATTRIBUTE)
    put_proposal(
        fresh_database,
        "CLM-9301",
        RICH_PROPOSAL | {"rationale": MARKUP + MARKUP_IN_ATTRIBUTE},
    )

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert response.status_code == 200
    html = response.text
    assert MARKUP not in html
    assert "<img" not in html
    assert html.count(ESCAPED_MARKUP) == 2  # once in each place
    assert not [t for t, _ in Page(html).tags if t in ("script", "img")]
    assert not [a for _, attrs in Page(html).tags for a in attrs if a == "onerror"]


def test_markup_in_a_citations_clause_is_escaped() -> None:
    """The proposal's validator refuses a clause that is not ``n.n``, so a
    stored one cannot carry markup; the template must not depend on that."""
    citation = {**CITATION, "clause": MARKUP + MARKUP_IN_ATTRIBUTE}
    proposal = TriageProposal.model_construct(
        **(
            RICH_PROPOSAL
            | {
                "citations": [citation],
                "drafted_by": DraftedBy.model_validate(RICH_PROPOSAL["drafted_by"]),
            }
        )
    )

    view = adjuster.ClaimView(
        claim_id="CLM-9301",
        state="awaiting_adjuster",
        since=LONG_AGO,
        received_at=LONG_AGO,
        facts=adjuster.facts_of(claim_with_id("CLM-9301")),
        proposal=proposal,
        proposal_note=None,
        decision=None,
        trail=(),
    )

    html = adjuster.render_claim(view)

    assert MARKUP not in html
    assert "<img" not in html
    assert ESCAPED_MARKUP in html


# ── the decision from the form ──────────────────────────────────────────────
@pytest.mark.parametrize("word", DECISION_OUTCOMES)
def test_a_decision_from_the_page_redirects_and_does_what_the_json_route_does(
    fresh_database: DatabaseHandle, word: str
) -> None:
    run_id = awaiting_claim(fresh_database)
    state, trigger = DECISION_OUTCOMES[word]
    resume = resuming_runtime(run_id)
    exporter = InMemorySpanExporter()
    client = make_client(claims_dsn(fresh_database), resume, exporter)

    response = post_form(client, DECISION_ID, word, SAME_ORIGIN)

    assert response.status_code == 303
    assert response.headers["location"] == url_of(DECISION_ID)
    assert claim_state(fresh_database, DECISION_ID) == (state, run_id)
    assert decisions(fresh_database) == [(DECISION_ID, run_id, word)]
    # The rows the JSON route's test expects: one for the decision, after the
    # two of the triage.
    assert claim_audit(fresh_database, DECISION_ID)[2:] == [
        (f"claim.{state}", state, trigger, run_id, "claims_api")
    ]
    (request,) = resume.requests
    assert request.url.path == f"/runs/{run_id}/resume"
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "claims.decide"]
    assert dict(span.attributes) == {
        "meridian.claim_id": DECISION_ID,
        "meridian.tenant": TENANT,
        "meridian.run_id": str(run_id),
    }


def test_the_page_after_the_redirect_shows_the_claim_decided(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = awaiting_claim(fresh_database)
    client = make_client(claims_dsn(fresh_database), resuming_runtime(run_id))

    response = client.post(
        decision_url(DECISION_ID),
        data={"decision": "reject", "run": str(run_id)},
    )

    # The test client followed the 303 to the claim's page.
    assert response.status_code == 200
    assert str(response.url).endswith(url_of(DECISION_ID))
    page = Page(response.text)
    assert "rejected" in page.text
    assert page.decision_buttons() == []


def test_a_decision_on_a_claim_decided_otherwise_renders_409_and_changes_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = awaiting_claim(fresh_database)
    client = make_client(claims_dsn(fresh_database), resuming_runtime(run_id))
    assert post_form(client, DECISION_ID, "approve").status_code == 303
    resume = resuming_runtime(run_id)

    response = post_form(
        make_client(claims_dsn(fresh_database), resume), DECISION_ID, "reject"
    )

    assert response.status_code == 409
    assert DECIDED_OTHERWISE_DETAIL in Page(response.text).text
    assert decisions(fresh_database) == [(DECISION_ID, run_id, "approve")]
    assert resume.requests == []


def test_a_decision_on_a_claim_that_does_not_wait_renders_409(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = awaiting_claim(fresh_database)
    set_claim(fresh_database, DECISION_ID, "approved")
    resume = Runtime()

    # The page was read while the claim waited; its form carries the same run.
    response = post_form(
        client_for(fresh_database, resume), DECISION_ID, "approve", run=str(run_id)
    )

    assert response.status_code == 409
    assert NOT_WAITING_DETAIL in Page(response.text).text
    assert decisions(fresh_database) == []
    assert resume.requests == []


def test_a_decision_on_an_unknown_claim_renders_the_404_page(
    fresh_database: DatabaseHandle,
) -> None:
    resume = Runtime()

    response = post_form(client_for(fresh_database, resume), "CLM-9999", "approve")

    assert response.status_code == 404
    assert NO_SUCH_CLAIM in Page(response.text).text
    assert resume.requests == []


@pytest.mark.parametrize(
    ("runtime", "status"),
    [
        pytest.param(lambda run_id: failing_runtime(run_id), 502, id="refused"),
        pytest.param(
            lambda _: Runtime(raises=httpx.ReadTimeout("slow")), 504, id="timeout"
        ),
    ],
)
def test_a_failed_resume_renders_the_page_with_a_button_that_sends_the_decision_again(
    fresh_database: DatabaseHandle, runtime: Any, status: int
) -> None:
    run_id = awaiting_claim(fresh_database)
    dsn = claims_dsn(fresh_database)

    response = post_form(make_client(dsn, runtime(run_id)), DECISION_ID, "approve")

    assert response.status_code == status
    page = Page(response.text)
    assert f"{status}: {RESUME_FAILED_DETAIL}" in page.text
    assert RESEND_TEXT in page.text
    # The decision stays recorded.
    assert claim_state(fresh_database, DECISION_ID) == ("approved", run_id)
    assert decisions(fresh_database) == [(DECISION_ID, run_id, "approve")]
    # One form, two hidden fields (the same decision, and the claim's run), one
    # submit.
    (form,) = page.attributes("form")
    assert (form["method"], form["action"]) == ("post", decision_url(DECISION_ID))
    fields = {str(i["name"]): str(i["value"]) for i in page.attributes("input")}
    assert fields == {"decision": "approve", "run": str(run_id)}
    assert {i["type"] for i in page.attributes("input")} == {"hidden"}
    assert len(page.attributes("button")) == 1
    # Sending it again, to a runtime that completes, redirects.
    again = client_for(fresh_database, resuming_runtime(run_id)).post(
        str(form["action"]), data=fields, follow_redirects=False
    )
    assert again.status_code == 303
    assert decisions(fresh_database) == [(DECISION_ID, run_id, "approve")]


def test_a_run_that_is_still_running_is_409_on_the_page_without_a_resend_button(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = awaiting_claim(fresh_database)
    dsn = claims_dsn(fresh_database)

    with caplog.at_level(logging.INFO, logger=claims_app.__name__):
        response = post_form(
            make_client(dsn, resuming_runtime(run_id, status="Running")),
            DECISION_ID,
            "approve",
        )

    assert response.status_code == 409
    page = Page(response.text)
    assert f"409: {BEING_APPLIED_DETAIL}" in page.text
    assert RESEND_TEXT not in page.text
    assert not page.attributes("form")
    assert not page.attributes("input")
    # The decision stays recorded, and the log names the claim and the run.
    assert decisions(fresh_database) == [(DECISION_ID, run_id, "approve")]
    assert any(
        DECISION_ID in m and str(run_id) in m
        for m in (r.getMessage() for r in caplog.records)
    )


def test_a_run_that_is_still_running_is_409_on_the_json_route_too(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = awaiting_claim(fresh_database)

    response = make_client(
        claims_dsn(fresh_database), resuming_runtime(run_id, status="Running")
    ).post(f"/claims/{DECISION_ID}/decision", json={"decision": "approve"})

    assert response.status_code == 409
    assert response.json() == {
        "detail": BEING_APPLIED_DETAIL,
        "claim_id": DECISION_ID,
        "run_id": str(run_id),
    }
    assert BEING_APPLIED_DETAIL == "the decision is being applied by another request"


def test_a_failed_audit_write_on_a_decision_renders_the_503_page_and_logs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def audit_down(*_: object) -> None:
        raise AuditUnavailable("OperationalError")

    monkeypatch.setattr(claims_app, "_record_decision", audit_down)
    resume = Runtime()

    with caplog.at_level(logging.ERROR):
        response = post_form(make_client(runtime=resume), DECISION_ID, "approve")

    # The status and text the shared JSON handler gives, as a page.
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert AUDIT_DOWN in Page(response.text).text
    assert resume.requests == []
    assert any("audit write failed" in r.getMessage() for r in caplog.records)


def test_the_same_failed_audit_write_on_the_json_route_is_the_same_503() -> None:
    def audit_down(*_: object) -> None:
        raise AuditUnavailable("OperationalError")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(claims_app, "_record_decision", audit_down)
        response = make_client().post(
            f"/claims/{DECISION_ID}/decision", json={"decision": "approve"}
        )

    assert (response.status_code, response.json()) == (503, {"detail": AUDIT_DOWN})


def test_a_decision_when_the_database_is_down_is_503_and_nothing_resumes() -> None:
    resume = Runtime()

    response = post_form(make_client(runtime=resume), DECISION_ID, "approve")

    assert response.status_code == 503
    assert DATABASE_DOWN in Page(response.text).text
    assert resume.requests == []


def test_a_decision_that_is_not_one_of_the_three_words_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = awaiting_claim(fresh_database)
    resume = Runtime()
    client = client_for(fresh_database, resume)

    for word in ("", "APPROVE", "delete", "approve,reject"):
        assert post_form(client, DECISION_ID, word).status_code == 422, word
    assert (
        client.post(decision_url(DECISION_ID), follow_redirects=False).status_code
        == 422
    )

    assert decisions(fresh_database) == []
    assert claim_state(fresh_database, DECISION_ID) == ("awaiting_adjuster", run_id)
    assert resume.requests == []


@pytest.mark.parametrize(
    "body",
    ["decision=approve&decision=reject", "decision=approve&decision=approve"],
)
def test_a_post_with_the_decision_field_twice_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle, body: str
) -> None:
    run_id = awaiting_claim(fresh_database)
    resume = Runtime()

    response = client_for(fresh_database, resume).post(
        decision_url(DECISION_ID),
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    # FastAPI's own 422 (the shared JSON answer), not the error page.
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert decisions(fresh_database) == []
    assert claim_state(fresh_database, DECISION_ID) == ("awaiting_adjuster", run_id)
    assert resume.requests == []


# ── a page read before the claim changed decides nothing (T-33) ─────────────
STALE_PAGE = "the claim changed after the page was read; read it again"


def hidden_runs(page: Page) -> list[str | None]:
    return [i.get("value") for i in page.attributes("input") if i.get("name") == "run"]


def a_page_read_on_one_run_then_the_claim_on_another(
    db: DatabaseHandle,
) -> tuple[TestClient, MoveRuntime, uuid.UUID, uuid.UUID]:
    """The claim waits on R1 and its page is read. Then an adjuster asks for
    documents, they arrive, and a new triage refers the claim again: it waits on
    R2. Returns the client, the runtime, R1 and R2."""
    first = uuid.uuid4()
    put_claim(db, CLAIM, run_id=first, triages=1)
    runtime = MoveRuntime()
    client = moves_client(db, runtime)
    assert page_run(client, CLAIM) == str(first)
    asked = client.post(
        f"/claims/{CLAIM}/decision", json={"decision": "request_documents"}
    )
    arrived = client.post(f"/claims/{CLAIM}/documents", json={"documents": ["invoice"]})
    assert (asked.status_code, arrived.status_code) == (200, 200)
    assert claim_state(db, CLAIM) == ("awaiting_adjuster", runtime.run_id)
    return client, runtime, first, runtime.run_id


def test_the_claims_page_carries_its_run_in_a_hidden_field_of_every_form(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    run_id = uuid.uuid4()
    put_claim(db, "CLM-9301", run_id=run_id, triages=1)
    put_claim(db, "CLM-9302", "triage_failed", triages=1)
    put_claim(db, "CLM-9303", "awaiting_adjuster", triages=CAP)
    client = client_for(db)

    for claim_id, shown in (
        ("CLM-9301", str(run_id)),
        ("CLM-9302", ""),
        ("CLM-9303", ""),
    ):
        page = Page(client.get(url_of(claim_id)).text)
        # One for each form (the decision's and, when offered, the triage's).
        assert hidden_runs(page) == [shown] * len(page.attributes("form")), claim_id
        for field in page.attributes("input"):
            assert (field["type"], field["name"]) == ("hidden", "run")


def test_the_approve_of_a_page_read_before_the_claim_moved_to_a_new_run_is_409(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, runtime, first, second = a_page_read_on_one_run_then_the_claim_on_another(
        db
    )
    resumed_before = [r.url.path for r in runtime.resumes]
    audit_before = claim_audit(db, CLAIM)

    response = post_form(client, CLAIM, "approve", run=str(first))

    assert response.status_code == 409
    page = Page(response.text)
    assert f"409: {STALE_PAGE}" in page.text
    # The page rendered with the answer is the claim as it is now: its form
    # carries R2, so the adjuster who reads it decides what is there.
    assert hidden_runs(page) == [str(second)] * 2
    assert decisions(db) == [(CLAIM, first, "request_documents")]
    assert claim_state(db, CLAIM) == ("awaiting_adjuster", second)
    assert claim_audit(db, CLAIM) == audit_before
    assert [r.url.path for r in runtime.resumes] == resumed_before
    assert not [p for p in resumed_before if str(second) in p]


def test_the_triage_form_of_a_page_read_before_the_claim_moved_to_a_new_run_is_409(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, runtime, first, second = a_page_read_on_one_run_then_the_claim_on_another(
        db
    )
    calls_before = list(runtime.calls)
    audit_before = claim_audit(db, CLAIM)
    triages_before = triages_of(db, CLAIM)

    response = post_triage(client, CLAIM, run=str(first))

    assert response.status_code == 409
    assert f"409: {STALE_PAGE}" in Page(response.text).text
    assert runtime.calls == calls_before
    assert decisions(db) == [(CLAIM, first, "request_documents")]
    assert claim_state(db, CLAIM) == ("awaiting_adjuster", second)
    assert triages_of(db, CLAIM) == triages_before
    assert claim_audit(db, CLAIM) == audit_before


def test_a_page_that_matches_the_claims_run_decides_it_and_sends_it_back(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, runtime, _, second = a_page_read_on_one_run_then_the_claim_on_another(db)

    decided = post_form(client, CLAIM, "reject", run=str(second))

    assert decided.status_code == 303
    assert decisions(db)[-1] == (CLAIM, second, "reject")
    assert runtime.resumes[-1].url.path == f"/runs/{second}/resume"


def test_the_triage_form_of_a_page_that_matches_the_claims_run_sends_it_back(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client, _, _, second = a_page_read_on_one_run_then_the_claim_on_another(db)

    response = post_triage(client, CLAIM, run=str(second))

    assert response.status_code == 303
    assert decisions(db)[-1] == (CLAIM, second, "send_back")


def test_an_empty_run_matches_a_claim_with_no_run_and_no_other(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", "triage_failed", triages=1)
    put_claim(db, "CLM-9302", "awaiting_adjuster", run_id=uuid.uuid4(), triages=1)
    put_claim(db, "CLM-9303", "awaiting_adjuster", triages=CAP)
    runtime = MoveRuntime()
    client = moves_client(db, runtime)

    no_run = post_form(client, "CLM-9301", "approve", run="")
    at_the_cap = post_form(client, "CLM-9303", "reject", run="")
    has_a_run = post_form(client, "CLM-9302", "approve", run="")
    has_a_run_triage = post_triage(client, "CLM-9302", run="")

    assert (no_run.status_code, at_the_cap.status_code) == (303, 303)
    assert (has_a_run.status_code, has_a_run_triage.status_code) == (409, 409)
    assert STALE_PAGE in Page(has_a_run.text).text
    assert [d[0] for d in decisions(db)] == ["CLM-9301", "CLM-9303"]


def test_a_failed_triage_of_a_claim_with_no_run_is_sent_again_on_an_empty_run(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, "triage_failed", triages=1)

    response = post_triage(moves_client(fresh_database, MoveRuntime()), CLAIM, run="")

    assert response.status_code == 303


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("decision=approve", id="no-run"),
        pytest.param("decision=approve&run=&run=", id="run-twice-empty"),
        pytest.param("decision=approve&run=a&run=a", id="run-twice"),
    ],
)
def test_a_decision_post_that_does_not_name_its_run_exactly_once_is_422(
    fresh_database: DatabaseHandle, body: str
) -> None:
    run_id = uuid.uuid4()
    put_claim(fresh_database, CLAIM, run_id=run_id, triages=1)
    runtime = MoveRuntime()

    response = moves_client(fresh_database, runtime).post(
        decision_url(CLAIM),
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert decisions(fresh_database) == []
    assert claim_state(fresh_database, CLAIM) == ("awaiting_adjuster", run_id)
    assert runtime.calls == []


@pytest.mark.parametrize("body", ["", "run=&run=", "run=A&run=B"])
def test_a_triage_post_that_does_not_name_its_run_exactly_once_is_422(
    fresh_database: DatabaseHandle, body: str
) -> None:
    run_id = uuid.uuid4()
    put_claim(fresh_database, CLAIM, run_id=run_id, triages=1)
    runtime = MoveRuntime()

    response = moves_client(fresh_database, runtime).post(
        triage_url(CLAIM),
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert decisions(fresh_database) == []
    assert triages_of(fresh_database, CLAIM) == 1
    assert runtime.calls == []


def test_a_run_that_is_not_an_id_never_matches_and_is_409(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    put_claim(fresh_database, CLAIM, run_id=run_id, triages=1)
    runtime = MoveRuntime()
    client = moves_client(fresh_database, runtime)

    decided = post_form(client, CLAIM, "approve", run="nonsense")
    sent_back = post_triage(client, CLAIM, run="nonsense")

    assert (decided.status_code, sent_back.status_code) == (409, 409)
    assert decisions(fresh_database) == []
    assert runtime.calls == []


# ── the resend button on a reload ───────────────────────────────────────────
def resend_forms(page: Page) -> list[str | None]:
    return [
        i.get("value") for i in page.attributes("input") if i.get("name") == "decision"
    ]


def test_a_decision_with_no_completed_run_after_it_shows_the_resend_button(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    run_id = uuid.uuid4()
    put_claim(db, "CLM-9301", "approved", run_id=run_id)
    put_trail(db, "CLM-9301", ["run.started", "run.completed"])  # before the decision
    put_decision(db, "CLM-9301", "approve", run_id)

    response = client_for(db).get(url_of("CLM-9301"))

    page = Page(response.text)
    assert response.status_code == 200
    assert RESEND_TEXT in page.text
    (form,) = page.attributes("form")
    assert (form["method"], form["action"]) == ("post", decision_url("CLM-9301"))
    assert [(i["type"], i["name"], i["value"]) for i in page.attributes("input")] == [
        ("hidden", "decision", "approve"),
        ("hidden", "run", str(run_id)),
    ]
    assert len(page.attributes("button")) == 1


def test_the_resend_button_carries_the_recorded_word(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    put_claim(fresh_database, "CLM-9301", "rejected", run_id=run_id)
    put_decision(fresh_database, "CLM-9301", "reject", run_id)

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    assert resend_forms(Page(response.text)) == ["reject"]


def test_a_run_completed_after_the_decision_leaves_no_resend_button(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    run_id = uuid.uuid4()
    put_claim(db, "CLM-9301", "approved", run_id=run_id)
    put_decision(db, "CLM-9301", "approve", run_id)
    put_trail(db, "CLM-9301", ["run.resumed", "run.completed"])

    response = client_for(db).get(url_of("CLM-9301"))

    page = Page(response.text)
    assert response.status_code == 200
    assert "Decision recorded: approve" in page.text
    assert RESEND_TEXT not in page.text
    assert not page.attributes("form")


def test_a_claim_with_no_decision_has_no_resend_button(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, "CLM-9301", "approved")
    put_trail(fresh_database, "CLM-9301", ["run.started", "run.completed"])

    response = client_for(fresh_database).get(url_of("CLM-9301"))

    page = Page(response.text)
    assert RESEND_TEXT not in page.text
    assert not page.attributes("form")


def test_a_run_completed_beyond_the_trail_limit_still_counts(
    fresh_database: DatabaseHandle,
) -> None:
    """The page shows 200 trail rows at most; whether the run completed is not
    read off those."""
    db = fresh_database
    run_id = uuid.uuid4()
    put_claim(db, "CLM-9301", "approved", run_id=run_id)
    put_decision(db, "CLM-9301", "approve", run_id)
    owner_rows(
        db,
        "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, "
        "status) VALUES (%s, %s, 'claims-triage', %s, 'CLM-9301', 'Completed') "
        "RETURNING 1",
        (run_id, uuid.uuid4(), TENANT),
    )
    # 250 rows, then the completion: it is the 251st of the trail.
    owner_rows(
        db,
        "INSERT INTO audit.events (service, event, outcome, run_id) "
        "SELECT 'agent-runtime', 'tool.called', 'ok', %s "
        "FROM generate_series(1, 250) RETURNING 1",
        (run_id,),
    )
    put_event(db, run_id, "run.completed")

    response = client_for(db).get(url_of("CLM-9301"))

    page = Page(response.text)
    assert "run.completed" not in page.text  # beyond what the page lists
    assert RESEND_TEXT not in page.text
    assert not page.attributes("form")


# ── T-70: a page of another site cannot post the decision ───────────────────
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Origin": "http://evil.example"}, id="another-origin"),
        # No Fetch Metadata, so the Origin decides, and ``null`` is no origin.
        pytest.param({"Origin": "null"}, id="null-origin-without-fetch-metadata"),
        pytest.param({"Sec-Fetch-Site": "cross-site"}, id="cross-site"),
        pytest.param({"Sec-Fetch-Site": "same-site"}, id="same-site"),
        pytest.param({"Sec-Fetch-Site": "something-new"}, id="unknown-fetch-site"),
        pytest.param({"Sec-Fetch-Site": ""}, id="empty-fetch-site"),
        pytest.param(
            {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
            id="own-origin-but-cross-site",
        ),
        pytest.param(
            {"Origin": "http://testserver", "Sec-Fetch-Site": "same-site"},
            id="own-origin-but-same-site",
        ),
    ],
)
def test_a_post_from_another_site_is_403_and_records_and_resumes_nothing(
    fresh_database: DatabaseHandle, headers: dict[str, str]
) -> None:
    run_id = awaiting_claim(fresh_database)
    audit_before = claim_audit(fresh_database, DECISION_ID)
    resume = resuming_runtime(run_id)

    response = post_form(
        client_for(fresh_database, resume), DECISION_ID, "approve", headers
    )

    assert response.status_code == 403
    assert FOREIGN_SITE in Page(response.text).text
    assert claim_state(fresh_database, DECISION_ID) == ("awaiting_adjuster", run_id)
    assert decisions(fresh_database) == []
    assert claim_audit(fresh_database, DECISION_ID) == audit_before
    assert resume.requests == []


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(SAME_ORIGIN | {"Sec-Fetch-Site": "same-origin"}, id="same-origin"),
        # Chrome's post of the page itself under the page's referrer policy.
        pytest.param(CHROME_SAME_ORIGIN, id="chrome-null-origin-same-origin"),
        # Fetch Metadata is authoritative: the browser sets it and a page cannot,
        # so an Origin that disagrees with it is not a reason to refuse.
        pytest.param(
            {"Origin": "http://evil.example", "Sec-Fetch-Site": "same-origin"},
            id="fetch-metadata-decides-over-the-origin",
        ),
        pytest.param({"Origin": "http://TESTSERVER"}, id="origin-in-capitals"),
        pytest.param({"Origin": "https://testserver"}, id="scheme-not-compared"),
        pytest.param({"Sec-Fetch-Site": "none"}, id="user-initiated"),
        pytest.param({}, id="no-header-at-all"),
    ],
)
def test_a_post_from_the_page_itself_or_a_client_with_no_origin_is_303(
    fresh_database: DatabaseHandle, headers: dict[str, str]
) -> None:
    run_id = awaiting_claim(fresh_database)
    resume = resuming_runtime(run_id)

    response = post_form(
        client_for(fresh_database, resume), DECISION_ID, "approve", headers
    )

    assert response.status_code == 303
    assert decisions(fresh_database) == [(DECISION_ID, run_id, "approve")]
    assert len(resume.requests) == 1


def test_chromes_post_of_the_page_itself_records_the_decision_and_redirects(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = awaiting_claim(fresh_database)
    resume = resuming_runtime(run_id)

    response = post_form(
        client_for(fresh_database, resume), DECISION_ID, "approve", CHROME_SAME_ORIGIN
    )

    assert response.status_code == 303
    assert response.headers["location"] == url_of(DECISION_ID)
    assert decisions(fresh_database) == [(DECISION_ID, run_id, "approve")]
    assert claim_state(fresh_database, DECISION_ID) == ("approved", run_id)
    assert len(resume.requests) == 1


def test_a_refused_post_is_logged_once_with_the_claim_and_no_header_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = {
        "Origin": "http://evil-canary.example",
        "Sec-Fetch-Site": "cross-site",
        "Referer": "http://referer-canary.example/",
    }

    with caplog.at_level(logging.DEBUG):
        response = post_form(make_client(), DECISION_ID, "approve", headers)

    assert response.status_code == 403
    refusals = [
        r for r in caplog.records if "cross-site post refused" in r.getMessage()
    ]
    (record,) = refusals
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"cross-site post refused for claim {DECISION_ID}"
    # What the app logged (the test client logs its own request line).
    logged = " ".join(
        r.getMessage() for r in caplog.records if r.name == adjuster.__name__
    )
    for value in ("evil-canary", "referer-canary"):
        assert value not in logged


def test_a_refused_post_to_a_path_that_is_no_claim_id_logs_no_path_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = make_client().post(
            f"{QUEUE_URL}/CLM-1%0AFORGED/decision",
            data={"decision": "approve"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )

    assert response.status_code in (403, 422)
    logged = " ".join(
        r.getMessage() for r in caplog.records if r.name == adjuster.__name__
    )
    assert "FORGED" not in logged


def test_the_origin_is_checked_before_the_decision_word() -> None:
    response = post_form(
        make_client(), DECISION_ID, "delete", {"Origin": "http://evil.example"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("origin", "host", "fetch_site", "cross_site"),
    [
        (None, "testserver", None, False),
        ("http://testserver", "testserver", None, False),
        ("http://testserver", "testserver", "same-origin", False),
        ("http://testserver", "testserver", "none", False),
        ("https://testserver", "testserver", None, False),
        ("http://TestServer", "testserver", None, False),
        ("http://testserver", "TESTSERVER", None, False),
        ("http://localhost:8000", "localhost:8000", None, False),
        ("http://localhost:8000", "localhost", None, True),
        ("http://localhost", "localhost:8000", None, True),
        ("http://localhost:8001", "localhost:8000", None, True),
        ("http://evil.example", "testserver", None, True),
        ("http://testserver.evil.example", "testserver", None, True),
        ("http://testserver@evil.example", "testserver", None, True),
        ("http://evil.example/testserver", "testserver", None, True),
        ("null", "testserver", None, True),
        ("NULL", "testserver", None, True),
        ("", "testserver", None, True),
        ("testserver", "testserver", None, True),
        ("http://testserver", None, None, True),
        (None, "testserver", "cross-site", True),
        (None, "testserver", "Cross-Site", True),
        (None, "testserver", "same-site", True),
        ("http://testserver", "testserver", "same-site", True),
        (None, "testserver", "something-new", True),
        ("http://testserver", "testserver", "", True),
        ("http://testserver", "testserver", "NONE", False),
        # Fetch Metadata, when present, decides alone.
        ("null", "testserver", "same-origin", False),
        ("http://evil.example", "testserver", "same-origin", False),
        ("http://evil.example", "testserver", "none", False),
        ("null", None, "same-origin", False),
        ("null", "testserver", "cross-site", True),
        (None, None, None, False),
    ],
)
def test_the_origin_check_refuses_exactly_what_t70_names(
    origin: str | None, host: str | None, fetch_site: str | None, cross_site: bool
) -> None:
    assert adjuster.is_cross_site(origin, host, fetch_site) is cross_site


# ── the headers and the stylesheet ──────────────────────────────────────────
def adjuster_responses(db: DatabaseHandle) -> dict[str, httpx.Response]:
    """One response of each kind the three routes and the stylesheet give, and
    of the shared JSON answers under ``/adjuster/`` (413, 404, 405, 422)."""
    put_claim(db, "CLM-9301")
    put_claim(db, "CLM-9302", "triage_failed")
    put_proposal(db, "CLM-9301", RICH_PROPOSAL)
    client = client_for(db)
    responses = {
        "queue": client.get(QUEUE_URL),
        "claim": client.get(url_of("CLM-9301")),
        "claim whose triage failed": client.get(url_of("CLM-9302")),
        "404 page": client.get(url_of("CLM-9999")),
        "422": client.get(url_of("nonsense")),
        "403 page": post_form(client, "CLM-9301", "approve", {"Origin": "http://x.y"}),
        "422 on the form": post_form(client, "CLM-9301", "nonsense"),
        "413": client.post(
            decision_url("CLM-9301"),
            content=b"decision=" + b"x" * (64 * 1024 + 1),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        "404 of an unknown path": client.get("/adjuster/nonsense"),
        "405": client.post(QUEUE_URL, data={"decision": "approve"}),
        "stylesheet": client.get(STYLESHEET_URL),
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(claims_app, "_record_decision", raise_audit_unavailable)
        responses["503 page"] = post_form(client, "CLM-9301", "approve")
    return responses


def raise_audit_unavailable(*_: object) -> None:
    raise AuditUnavailable("OperationalError")


def test_every_response_of_the_pages_carries_the_security_headers(
    fresh_database: DatabaseHandle,
) -> None:
    responses = adjuster_responses(fresh_database)
    assert [r.status_code for r in responses.values()] == [
        200,
        200,
        200,
        404,
        422,
        403,
        422,
        413,
        404,
        405,
        200,
        503,
    ]
    for name, response in responses.items():
        # The stylesheet holds no data, so it may be cached; the rest may not.
        expected = SECURITY_HEADERS | (
            {"cache-control": STYLESHEET_CACHE_CONTROL} if name == "stylesheet" else {}
        )
        for header, value in expected.items():
            assert response.headers.get(header) == value, (name, header)


def test_the_stylesheet_may_be_cached_and_every_other_response_may_not(
    fresh_database: DatabaseHandle,
) -> None:
    responses = adjuster_responses(fresh_database)

    cached = {
        n for n, r in responses.items() if r.headers["cache-control"] != "no-store"
    }

    assert cached == {"stylesheet"}
    assert responses["stylesheet"].headers["cache-control"] == STYLESHEET_CACHE_CONTROL


def test_the_redirect_after_a_decision_carries_the_security_headers(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = awaiting_claim(fresh_database)
    client = make_client(claims_dsn(fresh_database), resuming_runtime(run_id))

    response = post_form(client, DECISION_ID, "approve")

    assert response.status_code == 303
    for header, value in SECURITY_HEADERS.items():
        assert response.headers.get(header) == value, header
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"


def test_the_pages_hold_no_script_no_inline_style_and_no_handler(
    fresh_database: DatabaseHandle,
) -> None:
    responses = adjuster_responses(fresh_database)
    assert len(responses) > 5

    for name, response in responses.items():
        if not response.headers["content-type"].startswith("text/html"):
            continue
        page = Page(response.text)
        tag_names = {tag for tag, _ in page.tags}
        assert not {"script", "style", "iframe", "object", "embed"} & tag_names, name
        for _, attrs in page.tags:
            assert "style" not in attrs, name
            assert not [a for a in attrs if a.startswith("on")], name
        assert "<script" not in response.text.lower(), name
        assert "style=" not in response.text.lower(), name
        # One stylesheet, from the page's own origin; and the language.
        assert [a["href"] for a in page.attributes("link")] == [STYLESHEET_URL], name
        assert page.attributes("html")[0]["lang"] == "en", name
        assert "Synthetic data only." in page.text
        assert "(T-69)" in page.text


def test_the_stylesheet_is_css_served_from_the_package(
    fresh_database: DatabaseHandle,
) -> None:
    response = client_for(fresh_database).get(STYLESHEET_URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "{" in response.text
    assert "url(" not in response.text
    assert "@import" not in response.text


def test_the_headers_stay_on_the_pages_and_the_json_routes_are_unchanged(
    fresh_database: DatabaseHandle,
) -> None:
    client = client_for(fresh_database)

    health = client.get("/healthz")
    claims = client.post("/claims/CLM-9999/decision", json={"decision": "approve"})

    assert "content-security-policy" not in health.headers
    assert claims.status_code == 404
    assert claims.json() == {"detail": NO_SUCH_CLAIM}
    assert "content-security-policy" not in claims.headers


def test_the_pages_are_not_in_the_openapi_contract() -> None:
    paths = make_client().get("/openapi.json").json()["paths"]

    assert not [p for p in paths if p.startswith("/adjuster")]


# ── logs and spans (T-03) ───────────────────────────────────────────────────
def test_rendering_a_claim_and_posting_a_decision_log_nothing_the_claim_holds(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_database
    put_claim(db, "CLM-9301", description=f"The roof fell. {CANARY}")
    put_proposal(db, "CLM-9301", RICH_PROPOSAL | {"rationale": f"Why. {CANARY}"})
    run_id = awaiting_claim(db)
    exporter = InMemorySpanExporter()
    client = make_client(claims_dsn(db), resuming_runtime(run_id), exporter)

    with caplog.at_level(logging.DEBUG):
        queue = client.get(QUEUE_URL)
        page = client.get(url_of("CLM-9301"))
        decided = post_form(
            client, DECISION_ID, "approve", SAME_ORIGIN, run=str(run_id)
        )

    assert (queue.status_code, page.status_code, decided.status_code) == (200, 200, 303)
    assert CANARY in page.text  # the page holds it, so the scan can find it
    assert caplog.records, "no record was written, so nothing was checked"
    for record in caplog.records:
        message = record.getMessage()
        assert CANARY not in message
        assert CLAIMANT_NAME not in message
        assert CLAIMANT_EMAIL not in message
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert dict(spans["claims.adjuster.queue"].attributes) == {
        "meridian.tenant": TENANT
    }
    assert dict(spans["claims.adjuster.claim"].attributes) == {
        "meridian.claim_id": "CLM-9301",
        "meridian.tenant": TENANT,
    }
    for span in exporter.get_finished_spans():
        assert CANARY not in str(dict(span.attributes))
        assert CLAIMANT_NAME not in str(dict(span.attributes))


def test_a_database_failure_on_a_page_logs_the_class_and_not_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = make_client().get(url_of("CLM-9301"))

    assert response.status_code == 503
    assert DATABASE_DOWN in Page(response.text).text
    assert any("OperationalError" in r.getMessage() for r in caplog.records)
