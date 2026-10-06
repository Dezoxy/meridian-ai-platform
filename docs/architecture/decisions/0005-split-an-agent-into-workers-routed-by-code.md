# 5. Split an agent into workers routed by code, each with a tool list of its own

Date: 2026-10-06

## Status

Accepted

## Context

Until S031 the claims triage was one graph of seven nodes with one tool
list: the agent `claims-triage` held six tools, and any node of its graph
could call any of them. The node that reads the policy wording, whose
text an attacker can influence, ran under the same allowlist as the node
that writes a claim note (T-21, T-26). The model already chose no tool
and no route: the graph's code calls the tools in a fixed order and
`rules.decide` chooses where a claim goes (S014, T-30).

The plan asked for the triage "split into a supervisor and workers with
per-worker tool allowlists", with an evaluation that shows no regression.
Two things had to be decided before any code: who decides which worker
runs next, and where a worker's tool list is enforced.

## Decision drivers

- T-21 and hard rule 6: a tool call is allowlisted and audited; the
  allowlist should be as narrow as the work needs.
- T-22: a tool server reads tenant, agent and claim from the run's own
  record, and takes nothing of that kind on the caller's word.
- T-30 and S014: rules decide the route; the model chooses no tool and
  no argument.
- ADR 2: the platform's packages do not import the agent framework, and
  the runtime's contract with a graph does not change for one workload.
- The evaluation's recorded answers are found by the hash of the model
  request. A change to the request needs a new, paid recording.

## Considered options

Who decides which worker runs next:

1. Code and rules: the supervisor is a parent graph whose fixed edges and
   the existing rules hand the claim to each worker.
2. A model as supervisor: a model call reads the state and names the next
   worker.

What a worker is:

3. A part of one agent: the agent's registry entry declares its workers,
   each with a tool list that is a subset of the agent's.
4. An agent of its own: one registry agent per worker, each with an entry
   point.

Where a worker's tool list is enforced:

5. In the runtime's tool client and again in each tool server.
6. In the runtime's tool client alone.

## Decision

Options 1, 3 and 5. The owner chose 1 and 5 on 2026-10-06, each as the
session had recommended; 3 is the session's.

How it is built:

- **A worker is declared in the registry, inside its agent.**
  `config/registry/agents.yaml` gives `claims-triage` four workers:
  `intake` (policy lookup, claim history), `terms` (wording search),
  `assessor` (no tool: it asks the model the one question) and
  `approvals` (the approval request, its outcome, the claim note). A
  registry check holds every worker's list inside the agent's and the
  lists together equal to it, so no tool is left to nobody.
- **The agent stays the unit everything else knows.** The run, the
  tenant's list, the gateway's caller, budgets, the cost dashboard and
  the evaluation see one agent, as before.
- **The supervisor is the parent graph and holds no client.** It gets
  no tool client and no model client, only the compiled workers. Its
  nodes are the workers' subgraphs, the rules, and the pause for the
  adjuster; its edges are the routing the graph had, as code. Each
  worker's builder is handed the view of its own worker, or the model
  client for the assessor, and never both.
- **The runtime enforces a worker's list.** A worker calls tools through
  a view of the run's tool client bound to its ID. The view refuses a
  tool off the worker's list before anything is sent, and counts against
  the run's one limit. For an agent that declares workers the client
  itself, with no worker, calls nothing: a call site that forgot its
  worker is refused, not let through on the agent's whole list.
- **The tool server enforces it again.** A call carries the worker's ID
  in the request's `_meta` (`meridian/worker`). The server still reads
  tenant, agent and claim from the run's own row. It accepts the name
  only as a worker of that row's agent, and then allows that worker's
  tools alone.
- **The model request does not change.** It goes to the gateway under
  the agent `claims-triage` with the same messages, output budget and
  response schema, so the recorded answers still answer it.
- **The pause and the work after it are one node of the supervisor.**
  The first design had a node that only paused, followed by the
  approvals worker. A probe through the runtime showed why that fails:
  LangGraph consumes a pause when its node finishes, so a failure in the
  worker after it left the run with no pause to resume, and the next
  decision ended the run as failed instead of trying again
  (`test_a_pause_node_of_its_own_before_a_failing_subgraph_cannot_be_resumed_twice`).
  The pause node therefore runs the approvals worker's second half
  itself, as the single node did before the split, and the runtime's
  resume is untouched.

What the probes measured, for the next workload that uses subgraphs
(`tests/meridian/runtime/test_runtime_subgraphs.py`, through the real
runtime and PostgreSQL):

- Each graph of a run, the parent and every subgraph, counts its own
  steps for each leg against the runtime's limit of ten. The triage's
  supervisor takes at most six steps in a leg and no worker more than
  two.
- A worker invoked from the pause node keeps a checkpoint of its own.
  When its last node fails, the next resume runs the pause node again
  from its first line, and the worker continues at the node that failed.
  For the triage this means the recorded decision is read once when the
  note has to be written again, where it was read twice before.
- A finished run leaves no checkpoint row, a subgraph's included, and a
  worker's nodes carry the worker's ID on their spans.

## Consequences

Positive:

- A node that is steered, or simply wrong, reaches its own worker's
  tools and no others. The node that reads retrieved wording can search
  wording and nothing else; the node that asks the model holds no tool.
- The limit holds at two places that fail separately: a bug in the
  graph is stopped by the runtime, and a call that bypasses the
  runtime's view is stopped by the server.
- Naming the worker does not reopen T-22. The name only narrows: what
  any name can reach is the agent's own list, which the server allowed
  before.
- No model call was added, the route is still the rules', and the
  evaluation replays without a new recording: both baselines moved in
  the fingerprint of the tools' contracts and in nothing else.
- A refusal says why: the audit row and the failed run carry one of
  four new reasons, apart from the agent's own.

Negative / accepted trade-offs:

- The tool server takes the worker's name on the runtime's word. It can
  tell that the caller is the runtime (ADR 4) and that the name is a
  worker of the run's agent, not which node of the graph made the call.
- The registry now carries structure that mirrors the graph. A worker
  added to one and not the other is refused at run time, not at build
  time, except where the registry check sees it.
- The supervisor's pause node runs a worker, so "the supervisor only
  routes" has one exception, and its span names no worker.
- The limits stay per run: four model calls and sixteen tool calls for
  all workers together. A worker has no budget of its own.
- One workload uses workers. The scaffold for a new agent still writes
  an agent without them, which is checked as it always was.

Rejected options:

- Option 2: a model call per step, a route the model chooses (against
  S014 and T-30), a new surface for prompt injection in the one place
  that hands out work, and a paid recording before the evaluation could
  pass. It fits when the order of work cannot be written down, which is
  not this workload.
- Option 4: every tenant and the runtime's identity would list each
  worker, each would need an entry point and so be startable alone
  through the run API, and the agent that the gateway and the tool
  servers see for one claim would change along the way.
- Option 6: the server would go on checking the whole agent's list, so
  the narrower list would rest on the caller alone.
- A pause inside a worker's subgraph: it survives a failed resume too,
  but the second resume then does not pass the pause again, which
  differs from what the runtime's resume was built and tested for.

Reconsider when a workload needs an order of work that cannot be fixed
in code, or when workers of one run must be told apart by more than a
name the runtime sends: a credential per worker is what that would take.
