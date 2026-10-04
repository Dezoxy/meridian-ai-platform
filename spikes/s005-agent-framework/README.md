# S005 spike: Microsoft Agent Framework and LangGraph on one claim flow

## What this is

A throwaway spike for plan step S005. It builds the same three-step claim flow
with an approval pause twice, once in Microsoft Agent Framework
(`agent-framework-core` 1.19.0, "MAF" below) and once in LangGraph 1.2.12, and
runs one parametrized test suite against both. The flows differ only in
orchestration: the rules (`src/claimflow/rules.py`) are shared.

- It is **spike code, not platform code**. It is never deployed and nothing
  under `src/meridian/` imports it.
- It is its own uv project with its own `uv.lock`, not a workspace member.
- Renovate deliberately does not cover it: `.github/renovate.json` ignores
  `spikes/`. The pins are frozen evidence, so the observations below stay true of
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
   (`decision` is `approve` or `reject`, plus a non-empty `adjuster_id`); on
   resume it records the outcome with the adjuster and completes.

Both modules expose `start(claim_id, store_dir)` and
`resume(run_ref, decision, store_dir)`. `resume` takes the raw payload dict,
so a malformed payload reaches the framework unchecked. `run_ref` is a uuid
the application chooses at `start` and it stays the same until the run
completes, in both frameworks. The MAF module takes one more argument,
`store="file"|"json"`, to pick its checkpoint store (section 3).

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

Use `--framework langgraph` for the other one, and `--maf-store json` on both
commands to run MAF on the pickle-free store. `resume` refuses to run without
both `--decision` and `--adjuster-id`, so an empty payload is never sent by
accident. To send malformed input on purpose, pass `--raw-payload '<json>'`
instead, for example `--raw-payload '{}'`; it excludes the two flags.

| File | What it pins |
|---|---|
| `tests/test_rules.py` | the shared rules, both sides of the 2500 and date bounds, the `adjuster_id` rule |
| `tests/test_flow.py` | auto-approve, pause, resume, a real two-process resume |
| `tests/test_observations.py` | what each framework does on a bad resume, and the guard signal each gives |
| `tests/test_tools.py` | the JSON Schema each framework derives for `lookup_policy`, and the tool fields |
| `tests/test_framework_specifics.py` | checkpoint contents, replay, telemetry, quirks of one framework |
| `tests/test_langgraph_store.py` | LangGraph failed saves, strict msgpack, personal data at rest |
| `tests/test_maf_json_store.py` | the pickle-free MAF store, in two processes |
| `tests/test_cli.py` | the CLI refuses a resume without a decision unless `--raw-payload` says so |

Golden-set claims used: CLM-0005 (1220 EUR, auto-approve; its label in
`expected-outcomes.json` is cross-checked, never used to decide), CLM-0004
(2970 EUR), CLM-0007 (2501 EUR, one euro over), CLM-0002 (lapsed policy) and
CLM-0014 (policy active, loss date outside its term).

## Observations

Raw facts, each with the test that pins it or the source line it rests on.
Source citations are paths inside the spike's
`.venv/lib/python3.13/site-packages/` at the pinned versions;
`agent_framework/` is `agent-framework-core` 1.19.0. Where a fact was
observed at run time, the test is named instead. Three kinds of evidence are
not from the installed packages and are marked where they appear: pages
fetched from the vendors' documentation on 2026-09-30, upstream source files
of packages the spike does not install, and one upstream issue.

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
| Checkpoints | `FileCheckpointStorage(dir, allowed_checkpoint_types=[...])`, one JSON file per checkpoint; `CheckpointStorage` is a public protocol | `_workflows/_checkpoint.py:505`, `:579`, `:749`, `:134` |
| Run address | `await storage.get_latest(workflow_name=...)`, which returns `None` when the name has no checkpoint | `_checkpoint.py:184`, `:845` |
| Failed-save detector | `await workflow.resolve_pause_checkpoint_id(request_ids)` returns `None` when the pause was not saved | `_workflow.py:1328` |
| Shared state | `ctx.set_state(key, value)`, `ctx.get_state(key)`: untyped key/value state, in every checkpoint | `_workflow_context.py:437-443` |

Things the API forces or offers:

- The request and its response are handled by the same executor, and
  `@response_handler` registers on the class, so the approval step is a class
  while the other steps are functions. A function executor can call
  `request_info` but has no response handler: MAF logs "no matching response
  handler" and drops the response, so the resumed run ends with no output
  (`test_maf_function_executor_can_pause_but_its_response_is_dropped`).
- `ctx.yield_output` is typed by `WorkflowContext[Never, T]`.
- MAF has no run id. A run is a lineage of checkpoints, and the application
  chooses the handle (section 2).
- MAF also has an experimental functional API, `@workflow` and `@step`
  (`_workflows/_functional.py:1340`, `:572`, marked experimental at `:79`).
  Its checkpoint signature hash mixes the workflow name, the step names and a
  digest of the workflow function's bytecode (`:1238-1258`), so editing the
  function invalidates its checkpoints. A reviewer's scratch run of this flow
  in it paused, resumed and replayed like the graph API; that run is not in
  the spike.

**LangGraph**

| Need | API | Source |
|---|---|---|
| Graph | `StateGraph(TypedDict)`, `add_node`, `add_edge`, `compile(checkpointer=)` | `langgraph/graph/state.py:131`, `:376`, `:928`, `:1177` |
| Routing | `add_conditional_edges("assess", fn)` where `fn` returns a node name | `state.py:982` |
| Pause | `interrupt(value, response_schema=AdjusterDecision)` inside a node | `langgraph/types.py:887` |
| Resume | `graph.invoke(Command(resume=payload), config)` | `types.py:827`, `langgraph/pregel/main.py:3783` |
| Pause detection | `graph.get_state(config).interrupts` is non-empty (or `__interrupt__` in the `invoke` result) | `types.py:282`, `main.py:1392` |
| Checkpoints | `SqliteSaver.from_conn_string(path)`, one `.sqlite` file | `langgraph/checkpoint/sqlite/__init__.py:45`, `:99` |
| Address | `config={"configurable": {"thread_id": ...}}` | `main.py:1392` |

The spike detects a pause with `interrupts`, not `.next`. After a failed
pending-writes save `.next` still names `('approval',)` while no interrupt
was recorded
(`test_langgraph_failed_pending_writes_save_leaves_next_set_but_no_interrupt`).

A node that calls `interrupt` re-executes from its first line on resume
(`types.py:887` docstring), so a node should do nothing before `interrupt`
that must not run twice.

### 2. The resume payload and the failure modes

**Run identity.** Both flows now address a run by a handle the application
chooses, stable from start to completion; `RunResult.run_ref` is that handle.

- LangGraph has the handle built in: `thread_id`, with a checkpoint id per
  step underneath.
- MAF has none. A `WorkflowCheckpoint` is tied to a workflow definition and
  does not carry the id of the workflow instance that made it
  (`_checkpoint.py:35-50`); a run is a lineage of checkpoints grouped by
  `workflow_name`. The spike builds `WorkflowBuilder(name="claimflow-<uuid>")`
  and resumes from `storage.get_latest(workflow_name=...)`. When that returns
  `None`, the framework's signal for "no such run", `resume` raises
  `LookupError`. That is the only guard the spike adds.
- The name is a convention, not a check: on restore MAF compares the graph
  signature hash and not the workflow name (`_runner.py:316`,
  `test_maf_restore_checks_the_graph_hash_not_the_workflow_name`).

**Typing of the payload.** Both frameworks check types, and both enforce value
rules when the payload type carries them. `AdjusterDecision` has a
`__post_init__` that refuses a blank `adjuster_id`, and both frameworks run it
(`test_a_value_rule_of_the_payload_type_is_enforced_by_both_frameworks`).

- **MAF** coerces the payload to `response_type` and raises when the result is
  not an instance: `_coerce_request_info_response` (`_workflow.py:66-75`,
  called from `_send_responses_internal`, `:1120`). The dict to dataclass
  build is `_coerce_dict_to_dataclass` (`_workflows/_typing_utils.py:371-399`).
  It checks each field against its annotation with `_matches_annotation`
  (`:252`, called at `:390`); for a `Literal` that is the value match in
  `is_instance_of` (`:194-199`), so `decision="maybe"` fails. Then it calls
  the constructor (`:395`) and swallows any exception it raises (`:396`),
  returning the dict unchanged. A value rule in the constructor therefore
  works, but the caller reads `ValueError("Response type mismatch...")` and
  never the rule's own message.
- **LangGraph** validates only when `interrupt` is given a Pydantic model,
  dataclass or `TypedDict` as `response_schema` (`types.py:997-1016`). A JSON
  Schema `dict` is passed to clients but not enforced (`types.py` docstring,
  "used as-is, resume values are not validated"). The check runs inside the
  node on resume, so the error is a node failure, and it keeps the rule's
  message ("adjuster_id must not be empty"). The spike passes a dataclass.

**Results** (`tests/test_observations.py`, one test per row, run for both).
The last two columns are the signal a runtime guard would read to refuse the
call before the framework sees it; `has_pending_decision` in the test module
reads them (`test_the_guard_signal_tells_a_pending_decision_from_the_other_states`).

| Situation | MAF | LangGraph | MAF guard signal | LangGraph guard signal |
|---|---|---|---|---|
| Unknown run reference | Refused by the spike's `resume`: `LookupError`, from the framework's `None` | Not refused. An unknown thread id is a new thread: the graph starts from `START` with no input and fails in `validate` (`KeyError`), leaving the thread in the store (`test_langgraph_unknown_run_ref_leaves_a_new_thread_in_the_store`) | `get_latest(workflow_name=...)` is `None` | `get_state(config)` has `values == {}`, `next == ()`, `interrupts == ()` |
| Resume a completed run | Refused: `RuntimeError("No pending requests found...")` (`_workflow.py:1112`) | Not refused and not applied: returns the completed state unchanged, the new payload is dropped without a signal | The latest checkpoint has no `pending_request_info_events` | `interrupts == ()` |
| Resume twice (already decided) | Refused, same `RuntimeError`: the run's latest checkpoint is now the completion | Not refused and not applied: the second call returns the first decision, as if it were its own | as above | as above |
| Malformed payload (missing `adjuster_id`, missing `decision`, `decision="maybe"`, `adjuster_id=7`, a bare string) | Refused: `ValueError("Response type mismatch...")` before any executor runs (`_workflow.py:73`) | Refused: pydantic `ValidationError` raised by the node | unchanged: still pending | unchanged: still pending |
| Empty `adjuster_id` (a value rule) | Refused, message hidden: "Response type mismatch" | Refused, message kept | unchanged | unchanged |
| `{}` | Refused like any malformed payload (`test_maf_resume_with_an_empty_object_is_refused_like_any_malformed_payload`) | **Not refused**: the pause returns unchanged, no error (`test_langgraph_resume_with_an_empty_object_re_pauses_silently`) | unchanged | unchanged |
| After a refused payload | The pause is intact; a good payload completes | The pause is intact; a good payload completes, but see below | | |

Two LangGraph edges behind the last rows:

- `Command(resume={})` is read as a map of interrupt ids with no entries, so
  nothing is delivered and the node interrupts again. It is upstream issue
  langchain-ai/langgraph#8693, "Command resume misclassifies ordinary
  dictionary values as interrupt maps", open on 2026-09-30.
- A refused payload is not discarded. It is stored as a pending `__resume__`
  write next to the pause until the next resume overwrites it
  (`test_langgraph_a_refused_payload_stays_as_a_pending_resume_write`), and a
  plain `invoke(None, config)`, the documented "continue" call, replays it and
  fails again
  (`test_langgraph_a_plain_continue_replays_a_refused_payload`).

**Explicit-checkpoint replay.** Addressing a run by an explicit checkpoint id
instead of its handle replays from an immutable point in both frameworks, and
in both a second decision lands. This is framework-specific behaviour, not a
row of the shared table:

- MAF: `run(responses=..., checkpoint_id=<pause checkpoint>)` twice gives two
  completed runs with two different outcomes (`approve` by ADJ-0001, then
  `reject` by ADJ-0002), because nothing marks the pause as answered
  (`test_maf_replaying_the_pause_checkpoint_twice_lands_two_different_decisions`).
- LangGraph: time travel does the same, in two steps. `invoke(None, config
  pinned to the pause checkpoint)` forks the thread and pauses again, and the
  next `Command(resume=...)` on the thread lands a second decision
  (`test_langgraph_replaying_the_pause_checkpoint_lands_a_second_decision`).
  A `Command(resume=...)` pinned to the pause checkpoint on its own does not
  fork: it returns the first decision
  (`test_langgraph_a_resume_pinned_to_the_pause_checkpoint_does_not_fork`). MAF
  needs one call.

**Two resumes at once.** Two threads, lined up by a barrier, resume one pause
with `GOOD` (approve by ADJ-0001) and `OTHER` (reject by ADJ-0002). Each call
opens its own store connection, as two runtime replicas would. Neither
framework serializes them:

- MAF: both complete, with different outcomes. Each restores the same
  immutable pause checkpoint, so two checkpoints branch from it. It was 40 of
  40 in a scratch loop; the test retries up to ten fresh pauses in case a
  thread starts late and meets the sequential refusal
  (`test_maf_two_concurrent_resumes_of_one_pause_both_complete_with_different_outcomes`).
- LangGraph: never refused and no "database is locked" error, but the result
  varies. In a scratch loop of 30, 15 runs told the two callers different
  outcomes, 8 gave both callers approve and 7 both reject. The thread ends
  holding one decision, so in the disagreeing runs one caller was told
  `completed` with a decision that is not the stored one, a lost update. The
  test asserts what holds every time (no error, both `completed`, the stored
  decision is one of the two) and retries until the callers disagree
  (`test_langgraph_two_concurrent_resumes_are_never_refused_and_can_disagree`).

Threat model T-10 wants a decision to resume a run once, idempotently, and to
match the paused run's claim and tenant. Neither framework enforces that. With
the handle addressing above, a sequential second resume is refused by MAF and
dropped silently by LangGraph; either can be replayed by checkpoint id, and
two concurrent resumes both get through in both. Refusing, with a
compare-and-set on the pause, is the runtime's job, and the guard signals in
the table are what it reads. The guard signal alone is not enough for
concurrent calls: both callers can read "pending" before either decides.

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

Pickle is not a choice of the file store alone. The Microsoft Learn page
"Workflows - Checkpoints", section "Security Considerations" (fetched
2026-09-30), says `FileCheckpointStorage`, `CosmosCheckpointStorage` and
`FoundryCheckpointStore` all use pickle for non-JSON-native state behind a
restricted unpickler. The reviewers read the upstream sources of the Cosmos DB
store (`agent-framework-azure-cosmos`, `_checkpoint_storage.py`) and the Foundry
store (`foundry_state_store.py`), and the docstring of the durable extension's
`dt_serialization.py` (`agent-framework-durabletask`), which wrap the same
codec; none of those packages is installed here, so I cite them as read
upstream, not as checked in the venv. `FileCheckpointStorage.__init__` has no
JSON-only option (`_checkpoint.py:534-553`).

The way out is in the same page: "If your threat model does not permit
pickle-based serialization at all, use `InMemoryCheckpointStorage` or implement
a custom `CheckpointStorage` with an alternative serialization strategy."
`CheckpointStorage` is a public protocol (`_checkpoint.py:134`).
`src/claimflow/maf_json_store.py` is one: about 160 lines, only public names
from `agent_framework`, JSON on disk, and a type registry, so a file can only
turn into a registered dataclass. `--maf-store json` selects it. Pinned by
`tests/test_maf_json_store.py`: a start and a resume in two processes that
share only the store directory, with no `__pickled__` marker in any file; a
file that names `subprocess:Popen` is refused, not imported; a type outside
the registry fails the save. Its limits: it encodes only what this flow puts in
a checkpoint (primitives, lists, string-keyed dicts, workflow messages and
request events, registered dataclasses) and fails the save on anything else.
It is also not more private. Without pickle there is no base64 in front of the
claim: the intermediate checkpoints hold the claimant's name and email in the
clear, as LangGraph's rows do
(`test_json_store_keeps_the_claim_readable_in_the_intermediate_checkpoints`).
Do not point both stores at one directory.

Contents of the default (pickle) store, for a CLM-0004 pause (four files: the
entry checkpoint, then one per superstep):

- The claim is in the files. The two checkpoints written while the claim
  travels between steps hold the whole `ValidatedClaim` in `messages`: claimant
  name and email, the free-text description, the policy holder and address. The
  final pause checkpoint, the run's latest, holds only the pending request
  event with its `ApprovalRequest` (claim id, proposal, amount, validity),
  because the messages were consumed.
- **That is a consequence of the spike's message-passing shape, not a property
  of the framework.** MAF also has an untyped shared state,
  `ctx.set_state` / `ctx.get_state`, persisted in every checkpoint. A flow
  that keeps the claim there, as the upstream human-in-the-loop checkpoint
  sample (`checkpoint_with_human_in_the_loop.py`, read by the review, not
  installed here) keeps its brief, puts the claimant's email in the pause
  checkpoint too
  (`test_maf_shared_state_puts_the_claim_in_the_pause_checkpoint`).
- None of it is readable in the file text; a `grep` for the email finds nothing.
  That is base64, not encryption.
- All four files stay after the run completes. Completion deletes nothing; the
  storage has a `delete` (`_checkpoint.py:822`) that the flow does not call.
- Pinned by
  `test_maf_checkpoint_files_keep_personal_data_in_pickles_not_in_clear` and
  `test_maf_pause_checkpoint_holds_the_request_but_not_the_claim`.

**LangGraph**, `SqliteSaver`: a SQLite file with two tables, `checkpoints`
(`thread_id`, `checkpoint_ns`, `checkpoint_id`, `parent_checkpoint_id`, `type`,
`checkpoint`, `metadata`) and `writes` (`sqlite/__init__.py:142-160`), in WAL
mode (`:141`). Values are **msgpack** (`type` column is `msgpack`;
`JsonPlusSerializer`, `langgraph/checkpoint/serde/jsonplus.py:82`,
`:258-267`). `pickle_fallback` defaults to `False` (`:100`).

- Deserialization is permissive by default: any importable type is revived,
  with a warning (`jsonplus.py:107-114`). The serializer's docstring warns
  that "if an attacker can write directly to your checkpoint database, they
  may be able to trigger code execution when data is deserialized"
  (`jsonplus.py:85-90`), and `serde/_msgpack.py:3-6` says that without strict
  mode "any Python callable stored in checkpoint data will be imported and
  executed on load". So the write access to the store matters in both
  frameworks.
- `LANGGRAPH_STRICT_MSGPACK=true` restricts loading to an allowlist. The flag
  is read once, at import (`_msgpack.py:12`,
  `test_langgraph_strict_msgpack_is_read_once_at_import`), so it must be in
  the environment before `langgraph` is imported. This flow passes a pause and
  a resume under it, in a subprocess, because its state holds only primitives
  and dicts (`test_langgraph_flow_pauses_and_resumes_under_strict_msgpack`).
  Strict mode does not fail on an unlisted type: it returns it as a plain dict
  and logs "Blocked deserialization of ... not in allowed_msgpack_modules"
  (`test_langgraph_strict_msgpack_degrades_an_unlisted_type_to_a_dict`), so a
  flow that stores dataclasses in state would see dicts, not an error.

Contents: the state is the checkpoint. The channel values and the per-step
`writes` hold the claim dict and the policy dict, so the claimant name, email
and description are in the paused thread's rows, and msgpack keeps strings
readable, so they are visible to `grep` on the raw bytes
(`test_langgraph_checkpoint_rows_hold_personal_data_in_clear`). Nothing is
deleted on completion here either.

**Personal data and erasure.** LangGraph offers deletion, as MAF's store
does, and an encryption hook, which MAF's stores do not; the spike uses none
of them:

- `SqliteSaver.delete_thread(thread_id)` removes a thread's rows
  (`sqlite/__init__.py:484`), and an `EncryptedSerializer` wraps the
  serializer with a cipher (`serde/encrypted.py:8`). The test uses a toy XOR
  cipher only to show the hook: with it, the claimant's email is absent from
  the file, and the run still restores
  (`test_langgraph_encrypted_serializer_keeps_personal_data_out_of_the_file`).
  A real deployment passes an AES cipher.
- `delete_thread` is a SQL `DELETE`, and SQLite keeps deleted bytes in the
  file: after it the rows are gone and the email is still in the main file
  until a `VACUUM`, or from the start with `PRAGMA secure_delete = ON`
  (`test_langgraph_delete_thread_removes_rows_but_sqlite_keeps_the_bytes`).
- Data can sit in the `-wal` file while a connection is open, and then a
  `grep` of the main file alone misses it
  (`test_langgraph_personal_data_can_sit_in_the_wal_file_not_the_main_file`).
  The spike opens and closes a connection per call, and closing the last
  connection checkpoints the log, so after a spike run the main file holds the
  data (checked ad hoc). A long-lived service is the other case.

In both, what lands on disk depends on what the flow puts in messages, state
or shared state. A production flow that keeps personal data out of them is
possible in either; neither framework does it for you, and both keep a copy
per step.

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
  `executor.id`, `executor.type`, `message.payload_type` and the graph
  definition JSON under `workflow.definition`.
- **Workflow spans carry no payload, with the sensitive-data switch on or
  off.** The claimant email, name, description, the claim id and the adjuster
  id are all absent from the spans of a pause and resume
  (`test_maf_workflow_spans_carry_no_payload_whatever_the_sensitive_switch`).
  The switch is `enable_sensitive_data` (`observability.py:1158-1175`,
  `SENSITIVE_DATA_ENABLED` at `:1203`, set by `enable_sensitive_telemetry()`
  at `:1499`); it governs model-call content, and nothing under
  `agent_framework/_workflows/` reads it. It is not a switch for putting
  workflow payloads in spans.
- **LangGraph emits no OpenTelemetry of its own.** `grep -rl opentelemetry`
  over `langgraph/` and `langchain_core/` finds nothing; the only match in the
  install is `langsmith/_internal/otel/`, which is LangSmith's exporter and
  needs LangSmith tracing configured. Observed: a full LangGraph run under the
  same in-memory provider produced zero spans
  (`test_langgraph_emits_no_opentelemetry_spans_of_its_own`). I did not
  configure LangSmith.
- **A callback handler gives LangGraph per-node spans.** A `BaseCallbackHandler`
  of about 35 lines, passed in `config["callbacks"]`, opens one span per node.
  LangGraph tags each node run `graph:step:<n>`; filtering on that tag skips
  the graph itself and the router function, which carry other tags. A pause
  arrives through `on_chain_error` as a `GraphInterrupt` and must not be marked
  as an error; a real failure must
  (`test_langgraph_callbacks_give_one_span_per_node_and_a_pause_is_not_an_error`,
  `test_langgraph_callbacks_mark_a_failing_node_span_as_an_error`). The spike's
  handler is test code; a runtime would ship its own.

### 5. Dependency footprint

Measured from the lock file with `uv tree --package <name> --no-dev`, counting
distinct packages including the package itself:

| Install | Packages |
|---|---|
| `agent-framework-core==1.19.0` | 10, including its file store: `agent-framework-core`, `annotated-types`, `msgspec`, `opentelemetry-api`, `pydantic`, `pydantic-core`, `python-dotenv`, `pyyaml`, `typing-extensions`, `typing-inspection` |
| `langgraph==1.2.12` | 38, among them `langchain-core`, `langsmith`, `httpx`, `requests`, `websockets`, `orjson`, `ormsgpack`, `pydantic` |
| plus `langgraph-checkpoint-sqlite==3.1.1` | 3 more (`langgraph-checkpoint-sqlite`, `aiosqlite`, `sqlite-vec`): 41 in all |

No provider SDK (`openai`, `azure-*`, `anthropic`, ...) comes in for either.
The spike's `uv.lock` has 54 package entries: both installs, `pytest`,
`opentelemetry-sdk` and the spike itself. `langchain-core` arrives with
LangGraph, not by choice.

**Tool contract** (`tests/test_tools.py`, no model call):
`lookup_policy(policy_number: str)` with a docstring gives, in both, an object
schema with one property, `policy_number`, of type `string`, listed as
required, and the docstring as the description. MAF:
`FunctionTool.parameters()` (`agent_framework/_tools.py:1229`). LangGraph:
`tool.tool_call_schema.model_json_schema()`
(`langchain_core/tools/base.py:677`).

- MAF's tool has `approval_mode`, `max_invocations` and
  `max_invocation_exceptions` as fields of the tool (`_tools.py:662-668`).
  `approval_mode` defaults to `"never_require"` (`:662`), so the spike's tool
  passes nothing. `max_invocations` counts the **lifetime of the tool
  instance** and is never reset (`_tools.py:578-592`, `:784`, `:796`): a
  module-level tool accumulates across requests. The per-request limit is
  `FunctionInvocationConfiguration["max_function_calls"]` (`:1760-1769`,
  `:1819`).
- `approval_mode` is read by the agent tool loop (`_tools.py:2389`), not by
  `Workflow`: nothing under `_workflows/` refers to it. A workflow's approval
  is the `request_info` pause, as in this spike.
- LangChain's `BaseTool` has `return_direct`, `metadata`, `tags` and
  `handle_tool_error`, and no approval or invocation-limit field
  (`test_langchain_tool_has_no_approval_or_invocation_limit_field`). The
  documented answers sit outside the tool: LangChain's
  `HumanInTheLoopMiddleware` (LangChain's page
  `docs.langchain.com/oss/python/langchain/human-in-the-loop`, fetched
  2026-09-30; LangGraph's own prebuilt interrupt helpers are deprecated in
  favour of `langchain.agents.interrupt`, `langgraph/prebuilt/interrupt.py`),
  or an `interrupt()` call in the tool body. The second is the review's
  finding; the spike does not exercise it.
- The `langchain_core` decorator is needed only for `ToolNode` and the
  prebuilt agents (`langgraph/prebuilt/tool_node.py:76`, `:622`). A custom
  graph like this one can call a plain function from a node. The spike declares
  the LangGraph tool with `langchain_core.tools.tool`
  (`langchain_core/tools/convert.py:18`) so that the contract test has
  something to compare.

### 6. Line counts

| Module | Lines | Non-blank, non-comment |
|---|---|---|
| `src/claimflow/maf_flow.py` | 222 | 181 |
| `src/claimflow/langgraph_flow.py` | 116 | 92 |
| `src/claimflow/rules.py` (shared) | 157 | 113 |
| `src/claimflow/maf_json_store.py` (MAF, optional) | 164 | 138 |

Both flow modules include their own `start` and `resume` and result mapping.
The MAF module is larger for three reasons: the checkpoint type list and store
choice, the request dataclass with its handler pair, and the run addressing
(the workflow name, `get_latest`, and loading the latest checkpoint on resume
to learn the request id). The JSON store is extra code that only exists
because the default store is pickle-based. The suite has 135 tests.

### 7. Anything surprising

1. **MAF: a checkpoint that cannot be saved does not fail the run.** With the
   application types off the allowlist, every save is refused, the run still
   pauses and reports its request, and only `resolve_pause_checkpoint_id`
   returning `None` shows that nothing durable exists. The runner logs a
   warning and carries on (`_workflows/_runner.py:273`, "this does not fail the
   workflow run"). The spike's own `_to_run_result` is what turns the `None`
   into an error. Pinned by
   `test_maf_checkpoint_save_failure_does_not_fail_the_run`.
2. **LangGraph: a failed save raises the saver's exception, and durability
   decides how much ran first.** Observed with a `SqliteSaver` subclass whose
   `put` raises, on a pausing claim:

   | Durability | `invoke` | Nodes that ran before the failure | Checkpoint rows |
   |---|---|---|---|
   | `sync` | raises the saver's exception | none: the save comes before the next step | 0 |
   | `async` (the default, `pregel/main.py:2602-2603`) | raises the saver's exception | `validate` and `assess`: the work is done and lost | 0 |
   | `exit` | raises the saver's exception | `validate` and `assess` | 0 |

   Pinned by
   `test_langgraph_failed_save_at_default_durability_raises_after_the_nodes_ran`
   and `test_langgraph_failed_save_raises_and_durability_sets_how_much_ran_first`.
   The saves run on a background thread, so a hand-built connection needs
   `check_same_thread=False` (`from_conn_string` handles it). A failing `put_writes` raises too, and then
   `.next` is set with no interrupt (section 1). Unlike MAF, LangGraph does not
   let a failed save pass.
3. **LangGraph: a resume for an unknown thread creates the thread and runs the
   graph.** Only the missing `claim_id` stopped it here. With an all-optional
   state schema the bogus resume could have run the flow.
4. **LangGraph: `Command(resume=None)` crashes inside the framework** with
   `UnboundLocalError: resume_is_map`. The variable is bound only inside an
   `if`, and read at `langgraph/pregel/_loop.py:927`, where the code meant to
   raise `EmptyInputError`
   (`test_langgraph_resume_with_none_fails_inside_the_framework`).
5. **LangGraph: the second resume is silent, and `{}` re-pauses silently.**
   The caller of the second `resume` gets `completed` with the first adjuster's
   decision and no signal that theirs was dropped (section 2).
6. **MAF: the graph is hashed into every checkpoint and a changed graph is
   refused on restore** (`_runner.py:316`,
   `test_maf_refuses_to_restore_a_checkpoint_into_a_changed_graph`); the
   workflow name is not compared. I did not test the LangGraph equivalent.
7. **MAF: one run at a time per `Workflow` instance** (`_workflow.py:852`), so
   the spike builds a workflow per call.
8. **MAF pickles checkpoints; LangGraph uses msgpack.** T-10 and the store's
   access control matter more for a pickle store: the framework's own docstring
   says so (section 3). Both docstrings say a writable store can mean code
   execution; MAF has a documented pickle-free way out (a custom store, proved
   here), LangGraph has a strict mode that degrades to dicts.
9. **The validity rule is coarser than the golden set.** The rule reads
   `status == "active"`. CLM-0010's policy has status `lapsed` but
   `lapsed_on` (2026-06-04) is after the loss date (2026-05-03), and the golden
   set labels the claim `auto_approve`. Both flows propose `reject` for it. The
   spike follows the step's rule; a real rule needs `lapsed_on`.
10. **`interrupt(response_schema=...)` decides the comparison.** Without a
    validating schema LangGraph would have accepted every payload in the
    malformed row; with a dataclass it matches MAF's typed response. The
    comparison above uses it.

## Review

The first version of this spike addressed a MAF run by its pause checkpoint id.
That handle changes at every superstep and at completion, and it made "resume
twice" look like a MAF defect: the replay of an immutable checkpoint, which
LangGraph's time travel does as well, was compared with LangGraph's thread id.
Two independent reviews, one per framework, found this before merge, and the
other corrections above: the run addressing by workflow name, the explicit
replay in both frameworks, the value rules in the payload type, the shared
state, the pickle-free store, the failed-save behaviour of LangGraph, the
strict mode, the callback spans, the sensitive-data switch and the tool
fields. Each corrected observation is pinned by a test in this directory.
