# MCP tool-server contracts

What each tool server answers to `tools/list`: one JSON file per server, with
its description and, for every tool, the name, description, input schema,
output schema, annotations and, under `_meta`, what the server enforces for
it: the scope, and whether it needs an idempotency key or an approval.
Status: **implemented** (S013), proven in-process against PostgreSQL; the
servers run on kind in S044.

| File | Server |
|---|---|
| `policy-mcp.json` | `policy_lookup`, `claim_history` |
| `claims-mcp.json` | `add_claim_note`, `request_approval` |

`knowledge-mcp` has no file yet: it arrives with S046, and a server is
published only when every one of its tools has an output schema.

The files are generated from `config/registry/tools.yaml` and never edited by
hand.

```bash
uv run meridian registry contracts           # write the files
uv run meridian registry contracts --check   # fail on a stale, missing or orphan file
```

`make registry` runs the check, and CI runs `make registry`.

A caller sends two things in the request's `_meta`, not as tool arguments: the
run ID (`meridian/run`) and, for a tool that writes, the idempotency key
(`meridian/idempotency-key`, 64 lowercase hexadecimal characters). The server
reads tenant, agent and claim from the run's own record and refuses a call
whose bound argument names another claim or policy. The names and the refusal
reasons are in `src/meridian/platform/toolserver/wire.py`.
