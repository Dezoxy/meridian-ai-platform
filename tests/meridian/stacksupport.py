"""The claims-triage graph through the real services, in one process (S014).

``build_stack`` assembles what ``test_triage_stack.py`` and the walking skeleton
run on: a migrated database with the policies and the claim history seeded and
the four wordings ingested through the replay gateway, the three tool servers
as the runtime's ``tool_servers``, the Agent Runtime with the real graph (loaded
from its entry point, as in production), and the Claims API on top. The gateway
runs in replay mode on a clock the test moves by hand: the tenant's rate windows
are real, so a test advances the clock between claims.

``ScriptedModel`` stands in for the model behind the runtime's HTTP client: it
answers ``POST /v1/chat`` in the gateway's reply shape, from the golden labels
(the description in the request finds the claim).
"""

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dbsupport import OWNER, DatabaseHandle
from fastapi.testclient import TestClient
from generator import catalogue
from knowledgesupport import Gateway, Waits, ingest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    GATEWAY_REPLY,
    REGISTRY_DIR,
    REPO_ROOT,
    FakeClock,
    owner_rows,
    synthetic_claims,
)
from toolsupport import (
    SYNTHETIC_DIR,
    World,
    claims_server,
    knowledge_server,
    policy_server,
)

from meridian.platform.common.db import connect
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.platform.policy_mcp.seed import seed_policies
from meridian.platform.registry import load_registry
from meridian.runtime.app import create_app as create_runtime
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage.app import create_app as create_claims_api
from meridian.workloads.claims_triage.models import ClaimFacts
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.rules import PolicyRecord, needs_assessment
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.wording import select_terms

# Seconds that clear the gateway's windows (10 requests per 10 seconds and 10,000
# tokens per minute for the claims-triage tenant) before the next claim.
WINDOW_SECONDS = 61


def load(name: str) -> Any:
    return json.loads((SYNTHETIC_DIR / name).read_text(encoding="utf-8"))


CLAIMS: dict[str, dict[str, Any]] = {c["claim_id"]: c for c in synthetic_claims()}
POLICIES: dict[str, dict[str, Any]] = {
    p["policy_number"]: p for p in load("policies.json")
}
EXPECTED: dict[str, dict[str, Any]] = {
    e["claim_id"]: e for e in load("expected-outcomes.json")
}
MANIFEST = SYNTHETIC_DIR / "manifest.json"
# The committed evaluation baseline (S017); `make eval-baseline` rewrites it.
EVAL_BASELINE = REPO_ROOT / "data" / "evaluation" / "claims-triage-baseline.json"


def replay_gateway(
    db: DatabaseHandle,
    exporter: InMemorySpanExporter | None = None,
    registry_dir: Path = REGISTRY_DIR,
) -> Gateway:
    """The gateway app in replay mode over ``db``, on a fake clock."""
    clock = FakeClock()
    app = create_gateway(
        GatewaySettings(
            registry_dir=registry_dir,
            mode="replay",
            environment="test",
            database_url=db.dsn("model_gateway"),
        ),
        tracer_provider=make_tracer_provider(
            "model-gateway", exporter or InMemorySpanExporter()
        ),
        meter_provider=make_meter_provider("model-gateway", InMemoryMetricReader()),
        clock=clock,
    )
    return Gateway(TestClient(app), clock, load_registry(registry_dir), db)


def seed_and_ingest(gateway: Gateway) -> None:
    """The policies and the claim history seeded, the four wordings ingested
    through the gateway; the gateway's windows are clear afterwards."""
    with connect(gateway.database.dsn(OWNER), "test-seed") as conn:
        seed_policies(conn, SYNTHETIC_DIR)
    ingest(
        gateway.database,
        gateway.http,
        gateway.registry,
        sleep=Waits(also=gateway.clock.advance),
    )
    gateway.clock.advance(WINDOW_SECONDS)


def new_runtime(
    db: DatabaseHandle,
    exporter: InMemorySpanExporter,
    tool_servers: dict[str, Any],
    model_http: httpx.Client,
) -> Any:
    """A newly built Agent Runtime app.

    It is built from nothing but the database, the tool servers and the client
    it calls the model through: it holds no checkpointer of its own (each
    request opens one on the database), so a run it did not start can only be
    resumed from what the database holds."""
    return create_runtime(
        RuntimeSettings(
            registry_dir=REGISTRY_DIR,
            gateway_url="http://gateway.test",
            database_url=db.dsn("agent_runtime"),
        ),
        tracer_provider=make_tracer_provider("agent-runtime", exporter),
        http_client=model_http,
        tool_servers=tool_servers,
    )


def claims_api_over_new_runtime(
    db: DatabaseHandle,
    exporter: InMemorySpanExporter,
    tool_servers: dict[str, Any],
    model_http: httpx.Client,
) -> TestClient:
    """A Claims API client on a Claims API over a newly built Agent Runtime."""
    runtime = new_runtime(db, exporter, tool_servers, model_http)
    claims = create_claims_api(
        ClaimsSettings(
            runtime_url="http://runtime.test",
            database_url=db.dsn("claims_api"),
        ),
        tracer_provider=make_tracer_provider("claims-api", exporter),
        http_client=TestClient(runtime),
    )
    return TestClient(claims)


@dataclass(slots=True)
class Stack:
    """The Claims API client and what a test reads or moves."""

    client: TestClient
    db: DatabaseHandle
    clock: FakeClock
    exporter: InMemorySpanExporter
    tool_servers: dict[str, Any] = field(repr=False)
    model_http: httpx.Client = field(repr=False)

    def post(self, claim: dict[str, Any], *, advance: bool = True) -> httpx.Response:
        """Post a claim; by default the clock moves first, so the claim finds
        the gateway's windows clear."""
        if advance:
            self.clock.advance(WINDOW_SECONDS)
        return self.client.post("/claims", json=claim)

    def decide(self, claim_id: str, decision: str) -> httpx.Response:
        """An adjuster's decision on a claim. The clock does not move: the
        resumed leg calls the claims tool server, not the gateway."""
        return self.client.post(
            f"/claims/{claim_id}/decision", json={"decision": decision}
        )

    def decide_in_page(self, claim_id: str, decision: str) -> httpx.Response:
        """The same decision from the adjuster's page: the page is read, and its
        form posts its hidden ``run`` (T-33) from the page's own origin. The
        redirect is not followed."""
        page = self.client.get(f"/adjuster/claims/{claim_id}")
        run = re.search(r'name="run" value="([^"]*)"', page.text)
        return self.client.post(
            f"/adjuster/claims/{claim_id}/decision",
            data={"decision": decision, "run": run.group(1) if run else ""},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    def triage_again(self, claim_id: str, *, advance: bool = True) -> httpx.Response:
        """Send a claim back to triage, or try a failed one again (S048). A
        triage runs, so by default the clock moves first, as in ``post``."""
        if advance:
            self.clock.advance(WINDOW_SECONDS)
        return self.client.post(f"/claims/{claim_id}/triage", json={})

    def withdraw(self, claim_id: str) -> httpx.Response:
        """The claimant withdraws a claim (S048). The clock does not move: ending
        the run resumes it, which calls the claims tool server, not the
        gateway."""
        return self.client.post(f"/claims/{claim_id}/withdrawal", json={})

    def report_documents(
        self, claim_id: str, names: list[str], *, advance: bool = True
    ) -> httpx.Response:
        """The names of the documents that arrived for a claim (S048). The claim
        is triaged with them, so by default the clock moves first, as in
        ``post``."""
        if advance:
            self.clock.advance(WINDOW_SECONDS)
        return self.client.post(
            f"/claims/{claim_id}/documents", json={"documents": names}
        )

    def submit_in_page(
        self, claim: dict[str, Any], *, advance: bool = True
    ) -> httpx.Response:
        """The claim as the claimant's form posts it (S049): the nested location
        and claimant flattened, the documents one per line, the amount and the
        dates as text. It is posted from the page's own origin and the redirect
        is not followed. A triage runs, so by default the clock moves first, as
        in ``post``."""
        if advance:
            self.clock.advance(WINDOW_SECONDS)
        fields = {
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
        return self.client.post(
            "/claimant/claims",
            data=fields,
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    def documents_in_page(
        self, claim_id: str, names: list[str], *, advance: bool = True
    ) -> httpx.Response:
        """The status page's documents form: one name per line. The claim is
        triaged with them, so by default the clock moves first, as in
        ``post``."""
        if advance:
            self.clock.advance(WINDOW_SECONDS)
        return self.client.post(
            f"/claimant/claims/{claim_id}/documents",
            data={"documents": "\n".join(names)},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    def withdraw_in_page(self, claim_id: str) -> httpx.Response:
        """The status page's withdrawal form, which has no field. The clock does
        not move, as in ``withdraw``."""
        return self.client.post(
            f"/claimant/claims/{claim_id}/withdrawal",
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    def resume_directly(self, claim_id: str, run_id: str) -> httpx.Response:
        """What anything that can call the runtime can do: resume a paused run
        with no decision recorded by the Claims API, through a runtime of its
        own over the same database and tool servers."""
        runtime = TestClient(
            new_runtime(self.db, self.exporter, self.tool_servers, self.model_http)
        )
        return runtime.post(
            f"/runs/{run_id}/resume",
            json={"tenant": "claims-triage", "reference": claim_id, "input": {}},
        )

    def restart_runtime(self) -> None:
        """Replace the Agent Runtime (and the Claims API that calls it) with
        newly built ones over the same database and the same tool servers, as a
        restarted pod would be. Nothing the old runtime held in memory
        survives."""
        self.client = claims_api_over_new_runtime(
            self.db, self.exporter, self.tool_servers, self.model_http
        )


def build_stack(
    db: DatabaseHandle, *, runtime_http: httpx.Client | None = None
) -> Stack:
    """The whole stack over ``db``. ``runtime_http`` replaces what the runtime
    calls the model through (the gateway itself when it is None)."""
    exporter = InMemorySpanExporter()
    gateway = replay_gateway(db, exporter)
    seed_and_ingest(gateway)
    world = World(db, uuid.uuid4())
    tool_servers = {
        "policy-mcp": policy_server(world, exporter),
        "knowledge-mcp": knowledge_server(world, gateway.http, exporter),
        "claims-mcp": claims_server(world, exporter),
    }
    model_http = runtime_http or gateway.http
    client = claims_api_over_new_runtime(db, exporter, tool_servers, model_http)
    exporter.clear()  # the spans of the ingestion are not a claim's trace
    return Stack(client, db, gateway.clock, exporter, tool_servers, model_http)


def service_of(span: ReadableSpan) -> str:
    return str(span.resource.attributes["service.name"])


def ancestors(span: ReadableSpan, spans: list[ReadableSpan]) -> list[str]:
    """Span names from ``span``'s parent up to the root."""
    by_id = {s.context.span_id: s for s in spans}
    names: list[str] = []
    parent = span.parent
    while parent is not None and parent.span_id in by_id:
        span = by_id[parent.span_id]
        names.append(span.name)
        parent = span.parent
    return names


def stored_proposals(db: DatabaseHandle) -> dict[str, TriageProposal]:
    """Each claim's stored proposal, read from ``claims.triage_proposals``."""
    rows = owner_rows(db, "SELECT claim_id, proposal FROM claims.triage_proposals")
    return {
        claim_id: TriageProposal.model_validate(document) for claim_id, document in rows
    }


def whole_wording(product: str) -> list[dict[str, Any]]:
    """Every clause of a product's wording, as ``wording_search`` returns them."""
    text = (SYNTHETIC_DIR / "wordings" / f"{product}.md").read_text(encoding="utf-8")
    return [
        {
            "clause": chunk.clause,
            "section": chunk.section,
            "title": chunk.title,
            "body": chunk.body,
            "keyword_match": False,
        }
        for chunk in parse_wording(text).chunks
    ]


def claims_that_ask_the_model() -> set[str]:
    """The golden claims whose run must ask the model whether a circumstance
    exclusion applies, decided by the rules' own predicate over the policy and
    the whole wording (a search that loses nothing finds what these see)."""
    asking = set()
    for claim_id, claim in CLAIMS.items():
        policy = POLICIES[claim["policy_number"]]
        record = PolicyRecord.model_validate(
            {name: policy.get(name) for name in PolicyRecord.model_fields}
        )
        facts = ClaimFacts.model_validate(
            {k: v for k, v in claim.items() if k != "claimant"}
        )
        terms = select_terms(
            facts.peril,
            whole_wording(policy["product"]),
            product=record.product,
            wording_version=record.wording_version,
        )
        if needs_assessment(facts, record, terms):
            asking.add(claim_id)
    return asking


def circumstance_clause(claim_id: str) -> str | None:
    """The catalogue's clause of the circumstance exclusion the expected
    outcome names, or None when it names none (or a peril exclusion, which the
    wording alone decides: the model is not asked)."""
    code = EXPECTED[claim_id]["exclusion"]
    if code is None:
        return None
    product = catalogue.PRODUCTS[POLICIES[CLAIMS[claim_id]["policy_number"]]["product"]]
    (exclusion,) = [e for e in product.exclusions if e.code == code]
    if exclusion.kind != catalogue.KIND_CIRCUMSTANCE:
        return None
    return catalogue.exclusion_clause(product, code)


def model_answer(verdict: str, clause: str | None, rationale: str) -> str:
    return json.dumps({"verdict": verdict, "clause": clause, "rationale": rationale})


@dataclass
class ScriptedModel:
    """The ``http_client`` of a runtime whose model is a script.

    ``answer`` gets the claim ID found from the description in the request's
    user message and returns the model's text; ``requests`` is what was asked
    (the claim IDs, in order). Only ``POST /v1/chat`` is answered."""

    answer: Callable[[str], str]
    requests: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/v1/chat")
        messages = json.loads(request.content)["messages"]
        description = json.loads(messages[1]["content"])["description"]
        (claim_id,) = [c for c, v in CLAIMS.items() if v["description"] == description]
        self.requests.append(claim_id)
        reply = {
            **GATEWAY_REPLY,
            "output": {"text": self.answer(claim_id), "finish_reason": "stop"},
        }
        return httpx.Response(200, json=reply)

    def http(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(self)
        )


def golden_answer(claim_id: str) -> str:
    """What the oracle's model says: the circumstance exclusion of a claim the
    golden set excludes by one, and none for any other claim."""
    clause = circumstance_clause(claim_id)
    if clause is None:
        return model_answer("none", None, "The description states no excluded fact.")
    return model_answer("applies", clause, "The description states an excluded fact.")


def none_answer(claim_id: str) -> str:
    return model_answer("none", None, "The description states no excluded fact.")
