# S037 spike: Microsoft Agent Framework 1.19.0 under the runtime's real clients

## What this is

A throwaway spike for plan step S037, contract P0. It measures what
`agent-framework-core` 1.19.0 does when the Agent Runtime's real
`ToolClient` and `ModelClient` run inside its steps and its checkpoints are
kept in PostgreSQL, before a second host is written. It writes no product
code: nothing under `src/`, no registry entry, no migration, no chart.

- It is **spike code, not platform code**. It is never deployed and nothing
  imports it. Capability label: implemented as a spike, tested, never run on
  a cluster.
- It is its own uv project with its own `uv.lock`, not a workspace member.
  The repository's package is a path dependency, so the probes import the
  real `meridian.runtime` clients. The root `pyproject.toml` and `uv.lock`
  are not changed.
- Renovate does not cover it: `.github/renovate.json` ignores `spikes/`, so
  the pins stay the versions the answers name.
- **No cluster, no model, no network beyond the download of the pinned
  packages.** The tool server is an in-process or loopback stand-in, the
  gateway is `httpx.MockTransport`, and the data is synthetic.

The probe workflow (`src/s037probe/flow.py`) has the shape of the design's
`claim-brief` agent: gather (two tool reads), draft (one model call), ask (a
tool write, then the pause), file (the recorded decision through a tool, then
a note). The store is `src/s037probe/store.py`: one table in a scratch schema,
keyed by a thread ID, JSON only.

## How to run

The tests that need PostgreSQL read the address of a database of your own from
`S037_DATABASE_URL` and fail without it. Use the image of the Makefile's
`PYTEST_DB_IMAGE`, bound to loopback:

```bash
docker run -d --name my-s037-db --tmpfs /var/lib/postgresql/data \
  -p 127.0.0.1:25680:5432 -e POSTGRES_HOST_AUTH_METHOD=trust <PYTEST_DB_IMAGE>
cd spikes/s037-agent-framework-host
uv sync
S037_DATABASE_URL=postgresql://postgres@127.0.0.1:25680/postgres uv run pytest
docker rm -f my-s037-db
```

Each test creates and drops the schema `s037_probe`; run them one at a time
(no xdist in this environment).

## How the spike is kept out of the repository's gates

| Gate | What keeps it out, or in |
|---|---|
| `pytest` at the root | `testpaths` in the root `pyproject.toml` lists `tests/meridian` and `tests/synthetic` only |
| `ruff check`, `ruff format --check` | **in**, as the first spike is: the root config lints `spikes/` (the per-file ignore for `spikes/**/tests/**` gives it `S101` and `S603`); `.venv/` is git-ignored, so ruff skips it. The spike's own code follows the root's rules |
| `lint-imports` | the contracts name `meridian.*` packages only |
| `make docs` | this README is read like any Markdown file (80 columns, links) |
| Renovate | `ignorePaths` in `.github/renovate.json` holds `spikes/**` |
| The image | `.dockerignore` is an allowlist (`pyproject.toml`, `uv.lock`, `src/`, `config/registry/`, two data files): `spikes/` is not on it |

## The answers

Each answer names the test that shows it (file `tests/test_pNN_*.py`). Where
the first spike (`spikes/s005-agent-framework`) or the design said otherwise,
it says so.

### Contradicts the design

1. **A failed step after the resume does not leave the pause answerable
   again.** The design says a failed resume leaves the run resumable, "a second
   resume answers it again and the step runs again". Measured: `responses=`
   from the latest checkpoint raises `RuntimeError("No pending requests found
   in workflow context.")`. The run is not lost: the framework writes a
   response-entry checkpoint and one per superstep, so the latest checkpoint
   after the failure holds the response in flight (`messages` non-empty,
   `pending_request_info_events` empty). A restore of that checkpoint with no
   responses (`workflow.run(checkpoint_id=latest)`) runs on from it, and only
   the failed step runs again. The host's resume must therefore branch on the
   shape of the latest checkpoint (probe 5, and 4).
2. **Spans made from events do not nest the clients' spans.** The design has
   the host make the leg's spans from the framework's events. A span the host
   starts on `executor_invoked` is not current inside the step, so the
   runtime's `runtime.tool` spans and the trace context the model client
   injects are children of the leg's span, not of the node span (LangGraph's
   `NodeSpans` makes its span current). Attributes can match; nesting needs a
   span opened inside the step (probe 9).
3. **The step bound is cumulative, not per leg.** The runtime's LangGraph bound
   is ten steps a leg. The framework's counter is restored from the checkpoint,
   so a bound set at build time counts the earlier legs too (probe 8).
4. **The framework logs the text of what a store raises.** A PostgreSQL error
   quotes the failing row, so a failed save puts the run's thread ID and the
   start of the checkpoint's JSON into a warning. The store must raise a
   class-name-only exception (probe 7).

### 1. The sync clients inside an async step

`tests/test_p01_sync_clients.py`. The workflow runs as the runtime's leg does:
`asyncio.run(workflow.run(...))` in a plain thread.

| Way | Tool client | Model client |
|---|---|---|
| (a) direct, no kept transport | **fails**: `anyio.run` in a running loop raises `RuntimeError("Already running asyncio...")`; the client logs `tool call failed: RuntimeError` and raises `ToolUnavailable` (reason `tool-unavailable`), with no cause. Same for an in-process server and an address | works, and blocks the loop |
| (b) direct, with a kept `ToolTransport` (an address target only) | works, but blocks the loop: a task on the loop cannot run during any call | works, and blocks the loop |
| (c) through `asyncio.to_thread` | works, the loop runs during the call; with and without a transport, in-process or over HTTP | works, the loop runs during the call |

- The transport is used only for a `str` target; an in-process `Server` falls
  back to `anyio.run` whichever way the step calls it.
- Limits and spans still behave in (b) and (c): `ToolCallLimit` and
  `ModelCallLimitError` come out of `workflow.run` with their own type, not
  wrapped in an exception group and not swallowed into an event; so does any
  step exception (`failure_reason` maps them to `tool-call-limit`,
  `model-call-limit` and `unexpected`). The `runtime.tool` span is a child of
  the span current in the step, and `asyncio.to_thread` copies the context, so
  the trace context in the call's `_meta` and in the gateway request header is
  the step's.
- **The host can rely on (c)**: every call to either client goes through
  `asyncio.to_thread` (or the loop's executor). It works with or without a
  kept transport.

### 2. The pause

`tests/test_p02_pause.py`. A class `Executor` with `@handler` calling
`ctx.request_info(request, Marker)` and a `@response_handler` method.

- `workflow.run` returns (it neither raises nor waits). The result has one
  `request_info` event (`request_id`, `data`, `response_type`,
  `source_executor_id`), no outputs, and final state
  `IDLE_WITH_PENDING_REQUESTS`.
- Detect a pause with `result.get_request_info_events()` (non-empty).
- `await workflow.resolve_pause_checkpoint_id(ids)` is not `None`: it is the
  last checkpoint written, the only one with the request in
  `pending_request_info_events`.
- The first spike's trap holds at 1.19.0: a function executor can call
  `request_info`, but its answer is dropped without an error and the resumed
  run has no output.

### 3. A resume in a new process

`tests/test_p03_resume.py`. The second process is a second interpreter
(`python -m s037probe.rig`) that shares only the table.

- It resumes from the store's latest checkpoint with a response. Only the
  steps after the pause run in it (`answered`, `file`); the brief comes out of
  the checkpoint.
- **Response type**: a marker dataclass of the host's own with no fields is
  enough. The runtime's empty object `{}` is coerced to it; `Marker()` works
  too. Anything else (a dict with a key, a string, an integer, `None`, a list)
  raises `ValueError("Response type mismatch for request ID ...")`, before the
  response-entry checkpoint, so the pause is as it was. The message names the
  types, not the value. The host should send `Marker()` itself and ignore the
  runtime's value.

### 4. Nothing to resume

`tests/test_p04_nothing_to_resume.py`.

| Case | The store says | The framework, if called |
|---|---|---|
| never started | `get_latest` is `None` (another thread's run is this case too) | `WorkflowCheckpointException("No checkpoint found with ID ...")` from the store's `load` |
| completed | latest checkpoint has no pending requests and no messages | `responses=` raises `RuntimeError("No pending requests found...")`; a restore with no responses is a **silent no-op** (no output, no error) |
| already answered (a second resume) | the same as completed | the same `RuntimeError` |

All three are decided from the latest checkpoint before the framework is
called, and map to `no-pending-pause`. Also: a resume that names the old pause
checkpoint's ID is allowed and answers the pause a second time (the first
spike saw it), so the host addresses "the latest", and resume-once stays the
runtime's conditional update.

### 5. A failed step after the resume

`tests/test_p05_failed_step_after_resume.py`. Counts of each step across start,
a resume that fails in `file`, and a resume that succeeds:

| Way to re-open | gather | draft | ask | answered | file |
|---|---|---|---|---|---|
| start | 1 | 1 | 1 | | |
| resume that fails in `file` | | | | 1 | 1 (raises) |
| `responses=` from the latest | refused: "No pending requests" | | | | |
| **restore the latest, no responses** | | | | | 1 |
| replay the pause checkpoint with a response | | | | 1 | 1 |

A failure inside the response handler is re-opened the same way (the latest
checkpoint is the response-entry one) and reruns both steps. A failure on the
second try writes no checkpoint and can be re-opened again. The three shapes of
the latest checkpoint tell the host which call to make: one pending request,
send the response with `responses=` and its checkpoint ID; no pending request
and messages in flight, restore it with no responses; neither, nothing to
resume.

### 6. The store

`tests/test_p06_store.py`.

- `typing.get_protocol_members(CheckpointStorage)` is six members: `save(
  checkpoint)`, `load(checkpoint_id)`, `list_checkpoints(*, workflow_name)`,
  `delete(checkpoint_id)`, `get_latest(*, workflow_name)` and
  `list_checkpoint_ids(*, workflow_name)`, all async. The framework itself
  calls only `save` and `load`; the rest are the host's.
- **Tying a checkpoint to the run**: a `WorkflowCheckpoint` has no field for it
  (`metadata` is the only free-form field and the framework leaves it empty).
  One workflow name for the agent (`claim-brief`) is enough: the store is built
  for one run, bound to the host's thread ID, and writes it as a column
  (`PRIMARY KEY (thread_id, checkpoint_id)`). Two runs of one name do not see
  each other, and a checkpoint ID of another thread is refused by `load` and
  `delete`.
- **Fields** of a checkpoint: `workflow_name`, `graph_signature_hash`,
  `checkpoint_id`, `previous_checkpoint_id`, `timestamp` (ISO 8601 text),
  `messages`, `state`, `pending_request_info_events`, `iteration_count`,
  `metadata`, `version` (`"1.0"`).
- **JSON round trip**: the document encoded and decoded again is equal field
  for field, including the pause's request event (`data`, `response_type`,
  `source_executor_id`). Types that need a rule: every dataclass a message or a
  request carries (`Gathered`, `Drafted`, `Filing`, `AskRequest`) and the
  response type (`Marker`), which is stored as a name inside the request event.
  `WorkflowEvent` and `WorkflowMessage` have `to_dict`/`from_dict`; `from_dict`
  of an event wants `module.qualname` names, so the codec keeps a second map.
  A string key starting with `__` is refused (it would collide with the
  codec's markers).
- **Refused at save**: leaving any one of the five types out of the rules
  fails the save of a checkpoint that holds it; so does an unregistered object,
  a non-string key, and a document that names an unregistered dataclass. Nothing
  is written. The error carries a class name only.
- **No pickle**: the framework's `encode_checkpoint_value`,
  `decode_checkpoint_value` and `_RestrictedUnpickler` and `pickle.load(s)`
  are replaced by functions that fail, and a start and a resume still work. A
  checkpoint whose state holds a payload in the framework's own `__pickled__`
  form (a `__reduce__` that writes a file) loads as the same plain dict of
  text; the file does not appear. The same text handed to `pickle.loads`
  does write it (the control). A document that names `subprocess:Popen` is
  refused with `KeyError`, not imported.

### 7. A save that fails

`tests/test_p07_failed_save.py`. As the first spike saw, the run goes on and
the runner logs `Failed to create checkpoint at iteration N: ... Note that
this does not fail the workflow run.`

- A pause whose save failed still returns its request event.
  `resolve_pause_checkpoint_id(ids)` is `None`, and `get_last_checkpoint_id()`
  is `None` (every save failed) or the last good one (only the pause's failed).
- **For a run that completed**: the runner's last ID agrees with the store's
  latest, so comparing them shows nothing. The store's latest checkpoint then
  still has the response in flight, so a later resume would run the last steps
  again. What shows it is the store's own record: `PostgresCheckpointStorage.
  failures` (the class name of each failed save) is empty only when every save
  of the leg worked. The host fails the leg when it is not empty, for a pause
  and for a completion, and checks `resolve_pause_checkpoint_id` for a pause as
  well.
- The text of the exception a store raises is logged. A raw PostgreSQL error
  quotes the failing row (thread ID, start of the body); the store catches
  `psycopg.Error` and raises a `WorkflowCheckpointException` with a class name.

### 8. A bound on steps

`tests/test_p08_step_bound.py`. `WorkflowBuilder(max_iterations=...)`; the
default is `DEFAULT_MAX_ITERATIONS = 100`. A superstep (a round in which every
executor with a message runs once) counts one; the probe workflow needs three
to pause and two more to finish. When messages are still in flight at the
bound, `workflow.run` raises `WorkflowConvergenceException("Runner did not
converge after N iterations.")` (its own type, no payload in the text); for a
loop, `N` executor runs. The counter is restored from the checkpoint, so the
bound is not per leg. A host that wants ten steps a leg builds the resume
leg's workflow with `max_iterations = latest.iteration_count + 10`. The bound
is not part of the graph signature, so a different one on resume is accepted.

### 9. Events and spans

`tests/test_p09_events_and_spans.py`.

- `run(..., stream=True)` yields an `executor_invoked` and an
  `executor_completed` (or `executor_failed`) event for each executor with its `executor_id`, inside
  `superstep_started`/`superstep_completed` events, `request_info` for a pause
  and `failed` with `details.error_type` before the exception. The non-stream
  result lists the same events. They carry **no time**: a host stamps them on
  arrival. `data` is the message the step got and the messages it sent (claim
  content): a span must never take it.
- The framework takes **no tracer provider** as an argument (`WorkflowBuilder`,
  `Workflow`, `run`): it reads the global one, so the platform's providers are
  not used by it.
- With no global provider (the platform sets none, and a full run of the
  framework, the clients and the store does not set one) the global provider
  stays the API's `ProxyTracerProvider`, the framework's spans are
  non-recording (invalid span context in every step), and nothing is
  exported. A switch `ENABLE_INSTRUMENTATION=false` exists (read in
  `observability.py`, not run here) and is not needed for this.
- With a global provider the spans are `workflow.build`, `workflow.run`,
  `executor.process <id>`, `edge_group.process <Type>` and `message.send`, with
  `executor.id`, and no payload in any attribute. The MCP SDK's client also
  makes `tools/call <raw tool name>` spans once a global provider exists; those
  are not the framework's.
- Spans made from the events are children of the leg's span and so are the
  clients' spans (see "Contradicts the design", 2).

### 10. One run per `Workflow` object

`tests/test_p10_one_run_per_workflow.py`. The first spike's note
(`_workflow.py:852`) is narrower than "one run": a **concurrent** second `run`
raises `WorkflowException("... already running ...")`. A second run after a
pause or after completion is allowed: after a pause it runs again from the
start with a warning that the earlier request is still pending (the steps
ran twice); after completion it works and its entry checkpoint is parented on
the first run's last one, so two runs share one lineage. The host builds a
workflow for each leg. Building one
takes about 0.2 ms (mean of 50, with the clients already built; the printed
figure is from `pytest -s`); the 45 ms seen when the test builds its stand-in
clients is loading the registry, not the workflow.

### 11. What the framework imports

`tests/test_p11_imports.py`, each in a fresh interpreter.

- After `import agent_framework`, and after building and running a pausing
  workflow of the framework alone: none of `openai`, `azure`,
  `azure.identity`, `azure.core`, `anthropic`, `boto3`, `litellm`, `mistralai`,
  `langgraph`, `langchain_core` is in `sys.modules`.
- After the runtime's app is imported, `langgraph` and `langchain_core` are
  loaded (they come from the runtime itself); importing the framework and
  running the probe workflow over the real clients adds nothing to that list.
- Dependencies of `agent-framework-core` 1.19.0 (from the lock;
  `uv tree --package agent-framework-core --no-dev`): `msgspec` 0.21.1,
  `opentelemetry-api` 1.45.0, `pydantic` 2.13.5 (with `pydantic-core`,
  `annotated-types`, `typing-inspection`), `python-dotenv` 1.2.4, `pyyaml`
  6.0.3 and `typing-extensions` 4.16.0. The root lock already has
  `opentelemetry-api`, `pydantic` and `pyyaml` at these versions; `msgspec` and
  `python-dotenv` are new to it. The `all` extra names the provider packages
  and `mcp`; none is installed.

## Files

| File | What it holds |
|---|---|
| `src/s037probe/flow.py` | the probe workflow and `Deps`, the one place that says how a step calls the clients |
| `src/s037probe/store.py`, `codec.py` | the PostgreSQL store and its JSON codec |
| `src/s037probe/support.py` | the stand-in tool server, the stub gateway and `Routed`, copied from `tests/meridian/toolsupport.py` and trimmed |
| `src/s037probe/rig.py` | legs as the probes run them, and the second-process entry point |
| `src/s037probe/probe_telemetry.py`, `probe_imports.py` | processes of their own for probes 9 and 11 |
| `tests/test_p01_...` to `test_p11_...` | one file for each probe |
