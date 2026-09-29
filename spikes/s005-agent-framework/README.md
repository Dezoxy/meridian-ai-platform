# S005 spike: Microsoft Agent Framework and LangGraph on one claim flow

## What this is

A throwaway spike for plan step S005. It builds the same three-step claim flow
with an approval pause twice, once in Microsoft Agent Framework
(`agent-framework-core` 1.19.0) and once in LangGraph 1.2.12, and runs one
parametrized test suite against both. The flows differ only in orchestration:
the rules (`src/claimflow/rules.py`) are shared.

- It is **spike code, not platform code**. It is never deployed and nothing
  under `src/meridian/` imports it.
- It is its own uv project with its own `uv.lock`, not a workspace member.
- Dependabot deliberately does not cover it: the root `uv` entry watches `/`
  only. The pins are frozen evidence, so the observations below stay true of
  the versions they name.
- **No model call, no network, no credentials.** Input is the synthetic golden
  set under `data/synthetic/`, read in place.

The flow, identical in both:

1. **validate**: load the claim and its policy; the policy is valid when
   `status == "active"` and `start_date <= loss_date <= end_date`.
2. **assess**: an invalid policy proposes `reject` and needs approval; a claim
   over `APPROVAL_THRESHOLD_EUR = 2500` proposes `approve` and needs approval;
   anything else is `auto_approve` and completes without a pause.
3. **approval**, only when needed: the run pauses for an adjuster decision
   (`decision` is `approve` or `reject`, plus an `adjuster_id`); on resume it
   records the outcome with the adjuster and completes.

Both modules expose `start(claim_id, store_dir)` and
`resume(run_ref, decision, store_dir)`. `resume` takes the raw payload dict,
so a malformed payload reaches the framework unchecked.

## How to run

```bash
cd spikes/s005-agent-framework
uv sync
uv run pytest
```

The project is not a package, so the CLI needs `src` on the path. This runs the
pause and the resume as two separate processes that share only the store
directory:

```bash
export PYTHONPATH=src
uv run python -m claimflow start --framework maf --claim-id CLM-0004 \
  --store-dir /tmp/s005-store
# copy "run_ref" from the JSON it prints
uv run python -m claimflow resume --framework maf --run-ref <run_ref> \
  --decision approve --adjuster-id ADJ-0042 --store-dir /tmp/s005-store
```

Use `--framework langgraph` for the other one. Leave out `--adjuster-id` to
send a malformed decision.

| File | What it pins |
|---|---|
| `tests/test_rules.py` | the shared rules, both sides of the 2500 and date bounds |
| `tests/test_flow.py` | auto-approve, pause, resume, a real two-process resume |
| `tests/test_observations.py` | what each framework does on a bad resume |
| `tests/test_tools.py` | the JSON Schema each framework derives for `lookup_policy` |
| `tests/test_framework_specifics.py` | checkpoint contents, telemetry, quirks |

Golden-set claims used: CLM-0005 (1220 EUR, auto-approve; its label in
`expected-outcomes.json` is cross-checked, never used to decide), CLM-0004
(2970 EUR), CLM-0007 (2501 EUR, one euro over), CLM-0002 (lapsed policy) and
CLM-0014 (policy active, loss date outside its term).

## Observations

Raw facts, each with the test that pins it or the source line it rests on.
Source citations are paths inside the spike's
`.venv/lib/python3.13/site-packages/` at the pinned versions;
`agent_framework/` is `agent-framework-core` 1.19.0. Where a fact was
observed at run time, the test is named instead.

### 1. APIs used

**Microsoft Agent Framework**

| Need | API | Source |
|---|---|---|
| Steps | `@executor(id=...)` on a function; `Executor` subclass with `@handler` | `agent_framework/_workflows/_function_executor.py:215`, `_executor.py:36`, `_executor.py:567` |
| Graph | `WorkflowBuilder(name=, start_executor=, checkpoint_storage=)`, `.add_edge`, `.build()` | `_workflows/_workflow_builder.py:53`, `:91`, `:230`, `:727` |
| Routing | `.add_switch_case_edge_group(source, [Case(condition, target), Default(target)])`; `add_edge` also takes a `condition` | `_workflow_builder.py:338`, `:230` |
| Pause | `await ctx.request_info(request_data, response_type)` in a handler | `_workflows/_workflow_context.py:404` |
| Resume handler | `@response_handler` on a method of the same executor | `_workflows/_request_info_mixin.py:121` |
| Run, resume | `await workflow.run(message)`, or `run(responses={request_id: value}, checkpoint_id=..., checkpoint_storage=...)` | `_workflows/_workflow.py:782`, `:1084` |
| Pause detection | `result.get_request_info_events()`, `result.get_final_state()` | `_workflow.py:169`, `:177` |
| Checkpoints | `FileCheckpointStorage(dir, allowed_checkpoint_types=[...])`; one JSON file per checkpoint | `_workflows/_checkpoint.py:505`, `:579`, `:749` |
| Pause address | `await workflow.resolve_pause_checkpoint_id(request_ids)` | `_workflow.py:1328` |

Two things the API forces. The request and its response are handled by the
same executor, and `@response_handler` registers on the class, so the approval
step is a class while the other steps are functions (I did not try a function
executor for it). `ctx.yield_output` is typed by `WorkflowContext[Never, T]`.

**LangGraph**

| Need | API | Source |
|---|---|---|
| Graph | `StateGraph(TypedDict)`, `add_node`, `add_edge`, `compile(checkpointer=)` | `langgraph/graph/state.py:131`, `:376`, `:928`, `:1177` |
| Routing | `add_conditional_edges("assess", fn)` where `fn` returns a node name | `state.py:982` |
| Pause | `interrupt(value, response_schema=AdjusterDecision)` inside a node | `langgraph/types.py:887` |
| Resume | `graph.invoke(Command(resume=payload), config)` | `types.py:827`, `langgraph/pregel/main.py:3783` |
| Pause detection | `graph.get_state(config).next` is non-empty | `main.py:1392` |
| Checkpoints | `SqliteSaver.from_conn_string(path)`, one `.sqlite` file | `langgraph/checkpoint/sqlite/__init__.py:45`, `:99` |
| Address | `config={"configurable": {"thread_id": ...}}` | `main.py:1392` |

A node that calls `interrupt` re-executes from its first line on resume
(`types.py:887` docstring), so a node should do nothing before `interrupt`
that must not run twice.

### 2. The resume payload and the four failure modes

A run is addressed differently. MAF has no run identity: a
`WorkflowCheckpoint` is tied to a workflow definition and does not carry the
workflow instance id (`_checkpoint.py:35-50`), the caller-visible handle is a
checkpoint id (a random uuid, `_checkpoint.py:89`) and it changes at every
superstep and at completion. LangGraph addresses a run by a `thread_id` the
caller chooses (a random uuid here), stable across pause and completion, with a
checkpoint id per step underneath.

Typing of the payload:

- **MAF** coerces the payload to `response_type` and raises when the result is
  not an instance: `_coerce_request_info_response`
  (`_workflow.py:66-75`, called from `_send_responses_internal`, `:1108`). A
  dataclass field of type `Literal["approve", "reject"]` is checked
  (`_workflows/_typing_utils.py:194-199`), which I had not expected.
  Field types are checked; values are not: `adjuster_id=""` is accepted.
- **LangGraph** validates only when `interrupt` is given a Pydantic model,
  dataclass or `TypedDict` as `response_schema` (`types.py:997-1016`). A JSON
  Schema `dict` is passed to clients but not enforced (`types.py` docstring,
  "used as-is, resume values are not validated"). The check runs inside the node
  on resume, so the error is a node failure. The spike passes a dataclass.

Results (`tests/test_observations.py`, one test per row, run for both):

| Situation | MAF | LangGraph |
|---|---|---|
| Unknown run reference | Refused: `WorkflowCheckpointException` from the store (`_checkpoint.py:749-765`); a `../` id is refused too (`:554`) | Not refused. An unknown thread id is a new thread: the graph starts from `START` with no input and fails in `validate` (`KeyError`), leaving the thread in the store (`test_langgraph_unknown_run_ref_leaves_a_new_thread_in_the_store`) |
| Resume a completed run | Refused: `RuntimeError("No pending requests found...")` (`_workflow.py:1112`); the ref is the final checkpoint | Not refused and not applied: returns the completed state unchanged, the new payload is dropped without a signal |
| Resume twice from the same pause | Not refused. The pause checkpoint is immutable, so the second resume replays from it: two completed runs with different outcomes for one pause (`approve` by ADJ-0001, `reject` by ADJ-0002) | Not refused and not applied: the second call returns the first decision, as if it were its own |
| Malformed payload (missing `adjuster_id`, missing `decision`, `decision="maybe"`, `adjuster_id=7`, a bare string) | Refused: `ValueError("Response type mismatch...")` before any executor runs (`_workflow.py:73`) | Refused: pydantic `ValidationError` raised by the node |
| After a refused payload | The pause is intact; a good payload completes | The pause is intact; a good payload completes |

Threat model T-10 wants a decision to resume a run once, idempotently, and to
match the paused run's claim and tenant. Neither framework enforces that: the
first three rows are the runtime's job in both, and MAF's replay makes the
"twice" case worse than LangGraph's silent drop.

### 3. Checkpoint format on disk

**MAF**, `FileCheckpointStorage`: one `<checkpoint_id>.json` per checkpoint.
The structure is JSON; every non-primitive value is a **pickle**, base64-encoded
and marked `__pickled__` (`_checkpoint_encoding.py:1-40`, `:83`). Loading uses a
restricted unpickler with an allowlist of safe builtins, all `agent_framework`
types, `openai.types`, plus the types named in `allowed_checkpoint_types` or
`register_checkpoint_type` (`:64`, `:157`). The module's own docstring says the
allowlist is not a security boundary and the store must be a trusted, access
controlled location (`:16-40`). A type missing from the allowlist makes the
save fail (`_checkpoint.py:579-606`); see section 7.

Contents, for a CLM-0004 pause (four files: the entry checkpoint, then one per
superstep):

- The claim is in the files. The two checkpoints written while the claim
  travels between steps hold the whole `ValidatedClaim` in `messages`: claimant
  name and email, the free-text description, the policy holder and address. The
  final pause checkpoint, the one the run reference points at, holds only the
  pending request event with its `ApprovalRequest` (claim id, proposal,
  amount, validity), because the messages were consumed.
- None of it is readable in the file text; a `grep` for the email finds nothing.
  That is base64, not encryption.
- All four files stay after the run completes. Completion deletes nothing; the
  storage has a `delete` (`_checkpoint.py:822`) that the flow does not call.
- Pinned by
  `test_maf_checkpoint_files_keep_personal_data_in_pickles_not_in_clear` and
  `test_maf_pause_checkpoint_holds_the_request_but_not_the_claim`.

**LangGraph**, `SqliteSaver`: a SQLite file with two tables, `checkpoints`
(`thread_id`, `checkpoint_ns`, `checkpoint_id`, `parent_checkpoint_id`, `type`,
`checkpoint`, `metadata`) and `writes` (`sqlite/__init__.py:142-160`). Values
are **msgpack** (`type` column is `msgpack`; `JsonPlusSerializer`,
`langgraph/checkpoint/serde/jsonplus.py:82`, `:258-267`). `pickle_fallback`
defaults to `False` (`:100`). Deserialization is permissive by default (any
type, with a warning) unless `LANGGRAPH_STRICT_MSGPACK=true`
(`jsonplus.py:107-114`, `serde/_msgpack.py:12`). I ran with the default.

Contents: the state is the checkpoint. The channel values and the per-step
`writes` hold the claim dict and the policy dict, so the claimant name, email
and description are in the paused thread's rows, and msgpack keeps strings
readable, so they are visible to `grep` on the raw bytes
(`test_langgraph_checkpoint_rows_hold_personal_data_in_clear`). Nothing is
deleted on completion here either.

In both, what lands on disk depends on what the flow puts in messages or state.
A production flow that keeps personal data out of them is possible in either;
neither framework does it for you, and both keep a copy per step.

### 4. Telemetry

- **MAF emits OpenTelemetry natively.** Span names are `workflow.build`,
  `workflow.run`, `executor.process`, `edge_group.process` and `message.send`
  (`agent_framework/observability.py:301-324`), created in `_workflow.py:565`,
  `_workflow_builder.py:809` and `_workflow_context.py:334`. Instrumentation is
  on by default (`observability.py:1107`, `:1195`). The core package depends on
  `opentelemetry-api` only, so without an SDK and a tracer provider the spans
  are no-ops.
- **Observed** with `opentelemetry-sdk` 1.45.0 and an in-memory exporter
  (`test_maf_emits_opentelemetry_spans_once_a_tracer_provider_exists`). The
  observed names carry a suffix: `executor.process validate`,
  `edge_group.process SwitchCaseEdgeGroup`, `message.send`. Attributes include
  `executor.id`, `executor.type`, `message.type` and the graph definition JSON
  under `workflow.definition`. Payloads are not in the spans by default: the
  claimant email, name, description and the claim id are all absent from the
  spans of a CLM-0004 run (checked ad hoc). The sensitive-data switch is
  `enable_sensitive_data` (`observability.py:1203`); I did not turn it on.
- **LangGraph emits no OpenTelemetry of its own.** `grep -rl opentelemetry`
  over `langgraph/` and `langchain_core/` finds nothing; the only match in the
  install is `langsmith/_internal/otel/`, which is LangSmith's exporter and
  needs LangSmith tracing configured. Observed: a full LangGraph run under the
  same in-memory provider produced zero spans
  (`test_langgraph_emits_no_opentelemetry_spans_of_its_own`). I did not
  configure LangSmith.

### 5. Dependency footprint

Measured by installing each framework alone into a fresh venv and running
`uv pip list`:

| Install | Packages (including itself) |
|---|---|
| `agent-framework-core==1.19.0` | 10: `agent-framework-core`, `annotated-types`, `msgspec`, `opentelemetry-api`, `pydantic`, `pydantic-core`, `python-dotenv`, `pyyaml`, `typing-extensions`, `typing-inspection` |
| `langgraph==1.2.12` + `langgraph-checkpoint-sqlite==3.1.1` | 41, among them `langchain-core`, `langsmith`, `httpx`, `requests`, `websockets`, `orjson`, `ormsgpack`, `aiosqlite`, `sqlite-vec`, `pydantic` |

No provider SDK (`openai`, `azure-*`, `anthropic`, ...) comes in for either.
The spike's `uv.lock` has 54 package entries: both installs, `pytest`,
`opentelemetry-sdk` and the spike itself. `langchain-core` arrives with
LangGraph, not by choice: LangGraph has no tool decorator of its own, so the
LangGraph tool is declared with `langchain_core.tools.tool`
(`langchain_core/tools/convert.py:18`).

Tool contract (`tests/test_tools.py`, no model call):
`lookup_policy(policy_number: str)` with a docstring gives, in both, an object
schema with one property, `policy_number`, of type `string`, listed as
required, and the docstring as the description. MAF:
`FunctionTool.parameters()` (`agent_framework/_tools.py:1229`). LangGraph:
`tool.tool_call_schema.model_json_schema()`
(`langchain_core/tools/base.py:677`). MAF's tool has `approval_mode`,
`max_invocations` and `max_invocation_exceptions` as fields of the tool
(`_tools.py:662`); LangChain's `BaseTool` has `return_direct`, `metadata`,
`tags` and `handle_tool_error`, and no approval or invocation-limit field.

### 6. Line counts

| Module | Lines | Non-blank, non-comment |
|---|---|---|
| `src/claimflow/maf_flow.py` | 188 | 152 |
| `src/claimflow/langgraph_flow.py` | 116 | 92 |
| `src/claimflow/rules.py` (shared) | 149 | 107 |

Both flow modules include their own `start` and `resume` and result mapping.
The MAF module is larger for three reasons: the pickle allowlist, the request
dataclass with its handler pair, and the pause-address logic
(`resolve_pause_checkpoint_id`, then loading the checkpoint on resume to learn
the request id).

### 7. Anything surprising

1. **MAF: a checkpoint that cannot be saved does not fail the run.** With the
   application types off the allowlist, every save is refused, the run still
   pauses and reports its request, and only `resolve_pause_checkpoint_id`
   returning `None` shows that nothing durable exists. The runner logs a
   warning and carries on (`_workflows/_runner.py:273`, "this does not fail the
   workflow run"). Pinned by
   `test_maf_checkpoint_save_failure_does_not_fail_the_run`.
2. **LangGraph: a resume for an unknown thread creates the thread and runs the
   graph.** Only the missing `claim_id` stopped it here. With an all-optional
   state schema the bogus resume could have run the flow.
3. **LangGraph: `Command(resume=None)` crashes inside the framework** with
   `UnboundLocalError: resume_is_map`. The variable is bound only inside an
   `if`, and read at `langgraph/pregel/_loop.py:926-927`, where the code meant
   to raise `EmptyInputError`
   (`test_langgraph_resume_with_none_fails_inside_the_framework`).
4. **LangGraph: the second resume is silent.** The caller of the second
   `resume` gets `completed` with the first adjuster's decision and no signal
   that theirs was dropped.
5. **MAF: the graph is hashed into every checkpoint and a changed graph is
   refused on restore** (`_runner.py:316`,
   `test_maf_refuses_to_restore_a_checkpoint_into_a_changed_graph`). Checkpoints
   also group by `workflow_name`, so the name must be explicit and stable. I did
   not test the LangGraph equivalent.
6. **MAF: one run at a time per `Workflow` instance** (`_workflow.py:852`), so
   the spike builds a workflow per call.
7. **MAF pickles checkpoints; LangGraph uses msgpack.** T-10 and the store's
   access control matter more for a pickle store: the framework's own docstring
   says so (section 3).
8. **The validity rule is coarser than the golden set.** The rule reads
   `status == "active"`. CLM-0010's policy has status `lapsed` but
   `lapsed_on` (2026-06-04) is after the loss date (2026-05-03), and the golden
   set labels the claim `auto_approve`. Both flows propose `reject` for it. The
   spike follows the step's rule; a real rule needs `lapsed_on`.
9. **`interrupt(response_schema=...)` decides the comparison.** Without a
   validating schema LangGraph would have accepted every payload in the
   malformed row; with a dataclass it matches MAF's typed response. The
   comparison above uses it.
