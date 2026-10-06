"""The relational needs that the plan, the backlog, the threat model and the
triage's rules write down, each with the place it is written and the spike
question that answers it. A need nobody wrote down is not here.

``where`` is ``(file, line, phrase)``: the phrase is on that line of that file
at the commit this branch was cut from. The lines move when those documents are
edited, so a test checks the phrase is in the file, not the line.

``counted`` is the spike's reading of the rule's third point, and the numbers
decide it: a need counts when it is ``relational`` (a relation between entities
of the graph, which is this module's one judgement, given with its reason), its
question needs more than one hop (more than a lookup by policy number) and its
answer on the committed data is not the trivial one for at least one start.
"""

from dataclasses import dataclass
from datetime import date

from claimgraph.census import census
from claimgraph.model import Graph
from claimgraph.questions import events_before_loss

PLAN = "docs/meridian-plan.md"
THREATS = "docs/architecture/security/threat-model.md"
RULES = "src/meridian/workloads/claims_triage/rules.py"


@dataclass(frozen=True, slots=True)
class Need:
    key: str
    need: str
    where: tuple[tuple[str, int, str], ...]
    question: str
    max_hops: int
    relational: bool
    reason: str


NEEDS = (
    Need(
        "frequent_claims",
        "how many claims and history entries a policy had in the 365 days "
        "before a loss (the fraud indicator frequent_claims)",
        (
            (RULES, 163, "recent = sum(1 for e in history if earliest <= e.loss_date"),
            (PLAN, 350, "`late_report` and `frequent_claims`"),
            (PLAN, 403, "count for `frequent_claims` (T-76)"),
            (THREATS, 161, "the holder's next claim carries `frequent_claims`"),
        ),
        "events_before_loss",
        1,
        True,
        "a relation between a policy and its earlier events",
    ),
    Need(
        "open_at_the_same_time",
        "claims of one policy that are open at the same time count for "
        "frequent_claims (backlog S067, T-76)",
        (
            (PLAN, 403, "claims of one policy that are open at the same time"),
            (PLAN, 510, "Claims of one policy that are open at the same time"),
            (THREATS, 161, "claims that are open at the same time are not counted"),
        ),
        "events_before_loss (the claims it returns)",
        1,
        True,
        "a relation between a policy and its earlier events",
    ),
    Need(
        "holder_of_a_policy",
        "the policies a claimant holds, so that a claim naming another policy "
        "can be refused (T-22, T-76; identity is S021)",
        (
            (THREATS, 107, "the policy store holds no holder to check it against"),
            (THREATS, 161, "nothing ties a claimant to a policy until S021"),
        ),
        "policies_of_customer",
        1,
        True,
        "a relation between a customer and the policies they hold",
    ),
    Need(
        "terms_of_a_claim",
        "the clauses of the policy's wording that bear on a claim (the triage "
        "retrieves terms; T-64 is the risk that the search misses one)",
        (
            (PLAN, 340, "retrieves terms"),
            (THREATS, 149, "the search does not return a clause the rules need"),
        ),
        "clauses_bearing_on_claim",
        3,
        False,
        "not a relation between customers, policies, assets and claims: it is "
        "the retrieval task that points 1 and 2 of the rule measure, and "
        "counted here it would make point 3 true of any retrieval",
    ),
)


def _claims_with_an_earlier_claim(graph: Graph) -> int:
    """Claims whose policy has another claim in the 365 days before its loss."""
    found = 0
    for claim in graph.nodes("Claim"):
        policy = graph.out_edges(claim.id, ("claimed_on",))[0].dst
        loss = date.fromisoformat(str(claim.attrs["loss_date"]))
        prior = events_before_loss(graph, policy, loss).claims
        found += any(c.id != claim.id for c in prior)
    return found


def answers(graph: Graph) -> list[dict]:
    """Each need with its hops and its non-trivial answers on the committed
    data: the census's counts of the question that answers it."""
    asked = census(graph)["questions"]
    non_trivial = {
        "events_before_loss": asked["events_before_loss"],
        "events_before_loss (the claims it returns)": {
            "starts": asked["events_before_loss"]["starts"],
            "non_trivial": _claims_with_an_earlier_claim(graph),
        },
        "policies_of_customer": asked["policies_of_customer"],
        "clauses_bearing_on_claim": asked["clauses_bearing_on_claim"],
    }
    return [
        {
            "need": n.key,
            "where": [{"file": f, "line": ln, "phrase": p} for f, ln, p in n.where],
            "question": n.question,
            "hops": n.max_hops,
            "starts": non_trivial[n.question]["starts"],
            "non_trivial": non_trivial[n.question]["non_trivial"],
            "relational": n.relational,
            "reason": n.reason,
            "counted": n.relational
            and n.max_hops > 1
            and non_trivial[n.question]["non_trivial"] > 0,
        }
        for n in NEEDS
    ]
