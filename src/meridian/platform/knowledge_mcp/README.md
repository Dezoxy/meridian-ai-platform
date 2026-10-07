# The knowledge service

The wordings' store, their ingestion, the hybrid search and the Knowledge MCP
tool server. This page holds what a diff of the code cannot show about the
ingestion's calls to the Model Gateway.

## An ingestion that ended on a 503

`meridian knowledge ingest` asks the gateway for the embeddings
(`embedding_client.py`). When the gateway answers 503, the command's last line
and its exit message say which of the gateway's four deliberate 503s it was:

```text
ERROR the model gateway refused the embedding call (model gateway answered 503; kind rate-store-unavailable)
```

The client compares the reply's `detail` with the gateway's four texts by
equality and keeps one fixed word. The text of a body is never logged or
stored, and a body longer than 256 bytes is not parsed. The search tool logs
the same word (`embedding call failed: status 503, kind <word>`) and tells an
agent nothing more than it did: the answer stays `gateway-unavailable`.

Status: **implemented; tested with scripted replies; not seen on a cluster.**

| Word | The gateway's text | What to look at |
|---|---|---|
| `rate-store-unavailable` | the rate store is unavailable | The rate store's pod (`kubectl -n meridian get pods`, `logs deploy/rate-store`) and the [rate store runbook](../../../../docs/operations/runbooks/rate-store.md). The gateway's log has `the rate store is unavailable (...)`, once a window |
| `database-unavailable` | the database is unavailable | The Platform Database and the [database failure runbook](../../../../docs/operations/runbooks/database-failure.md). Also what a failed close of the usage ledger's reservation answers; the gateway's log has `database error: <class> (sqlstate ...)` |
| `audit-unavailable` | the audit log is unavailable | The audit write failed; the gateway's log has `audit write failed`. The database again, which the audit write goes to |
| `provider-unavailable` | the model provider is unavailable | No deployment was called for the request: the registry's deployments for the embedding purpose and the data class (`config/registry/models.yaml`), their circuits and the time left. The [provider outage runbook](../../../../docs/operations/runbooks/provider-outage.md) |
| `unknown` | none of these | A body that is not the gateway's JSON, a text outside the four, a body over 256 bytes, or a 503 an edge or a proxy wrote. Read the Job's whole log and the gateway's: `kubectl -n meridian logs job/meridian-ingest-<tag>`, `logs deploy/model-gateway` |

The command does not retry a 503; it retries a 429 only. The audit row of the
refusal keeps its reason, `gateway-failed`: the word is in the command's output
and not in the row. The Job's output is not in Loki (see the operations
README), so read it from the Job.

## Known gap: the reply is read without a size bound

The embedding client reads a gateway reply without a size bound: httpx buffers
the whole body before the client looks at it, and only the per-read timeouts
limit it. The word's matching reads only what is already buffered and parses
nothing over 256 bytes, so it does not make this worse, and it does not fix it.
Designed, not built: read the reply as a stream and refuse one over a limit.
