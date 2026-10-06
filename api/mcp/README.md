# MCP tool-server contracts

What each tool server answers to `tools/list`: one JSON file per server, with
its description and, for every tool, the name, description, input schema,
output schema, annotations and, under `_meta`, what the server enforces for
it: the scope, and whether it needs an idempotency key or an approval.
Status: **implemented** (S013, S046, S015), proven in-process against
PostgreSQL; the servers run on kind in S044.

| File | Server |
|---|---|
| `policy-mcp.json` | `policy_lookup`, `claim_history` |
| `claims-mcp.json` | `add_claim_note`, `request_approval`, `approval_outcome` |
| `knowledge-mcp.json` | `wording_search` |

A server is published only when every one of its tools has an output
schema.

The files are generated from `config/registry/tools.yaml` and never edited by
hand.

```bash
uv run meridian registry contracts           # write the files
uv run meridian registry contracts --check   # fail on a stale, missing or orphan file
```

`make registry` runs the check, and CI runs `make registry`.

A caller sends these in the request's `_meta`, not as tool arguments: the
run ID (`meridian/run`), for a tool that writes, the idempotency key
(`meridian/idempotency-key`, 64 lowercase hexadecimal characters) and, for an
agent that declares workers (S031), the worker making the call
(`meridian/worker`, an ID of at most 64 characters of `a-z0-9-`). The server
checks that the worker is one of the run's agent and that the tool is on its
list; the name only narrows and is never read as tenant or agent. The server
reads tenant, agent and claim from the run's own record and refuses a call
whose bound argument names another claim or policy, or, for
`wording_search`, another product than that of the claim's policy; the
wording version searched is the policy's and is not an argument. The names
and the refusal reasons are in `src/meridian/platform/toolserver/wire.py`.
