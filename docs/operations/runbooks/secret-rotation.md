# Runbook: Secret rotation

A credential leaked or may have, a provider refuses the gateway's
credential, or a secret is simply due.

Status (S024): written from `infra/kind/up.sh`, the chart and the
Terraform, not exercised. The platform holds few secrets today: database
passwords on kind and no provider key at all. Rotating one is the owner's
decision (hard rule 8 in `CLAUDE.md`); a session asks first. The secrets
that arrive with Azure (S020), sign-in (S021) and the pipeline (S022) are
designed, and each gets its procedure here when it exists.

No command on this page prints a secret, with one exception that exists
to print one: `make grafana-password`. A person runs that one, on a
private terminal, never a session: a session's output is its transcript,
and the harness asks the owner before a session may run it.

## What you see

- The alert `MeridianModelCredentialRefused`: a provider answered 401 or
  403 to the gateway's own credential. See
  [the gateway's credential](#the-gateways-credential-at-the-provider).
- A finding of the secret scan: the required check `secret scan` in CI,
  GitHub's push protection, or `make secret-scan` before a push. See
  [a secret in the repository](#a-secret-in-the-repository).
- A password that was printed, pasted, shown on a shared screen or kept
  in a file that left the laptop.
- A service or the sweep that answers 503 after a rotation: its password
  and its role's no longer match.

## What exists and where

| Secret | Where | Made by | Rotation |
|---|---|---|---|
| The eleven database roles' passwords | Kubernetes Secrets in `meridian`: `meridian-owner-db`, `claims-api-db`, `agent-runtime-db`, `model-gateway-db`, `policy-mcp-db`, `claims-mcp-db`, `knowledge-mcp-db`, `claims-sweep-db`, `gateway-upkeep-db`, `policy-seed-db`, `knowledge-ingest-db` (keys `username`, `password`, `uri`) | `make up`, once, only if absent | Below; not exercised |
| Grafana's admin password | Secret `grafana-admin` in `observability` | `make up`, once, only if absent | Below; not exercised |
| The database's certificate authority and server certificate | Secret `platform-db-ca` and CloudNativePG's own | CloudNativePG | CloudNativePG issues and renews them; the repository records no expiry to watch (the plan's backlog) |
| The password of the role `app` | Secret `platform-db-app` | CloudNativePG | Not used: that role cannot reach the `meridian` database |
| The cluster's admin credentials | `infra/kind/kubeconfig`, gitignored | `make up` | A new cluster: `make down`, `make up` (the owner's) |
| A key for Azure OpenAI | Does not exist: key authentication is off on the account | — | Nothing to rotate; `make azure-smoke` checks it stays off |
| The gateway's identity in live mode | The developer's own `az login` on the laptop | The owner | Below |
| Secrets in Key Vault | None yet | — | Designed (S020) |
| The Terraform state's access | Entra ID only; shared keys are off on the storage account | `make azure-state` | Nothing to rotate; access is a role assignment |
| Tokens in the workflows | Only GitHub's own per-run token; no repository secret | GitHub | Nothing to rotate |

On kind the Kubernetes Secrets sit unencrypted in the node's etcd, which
is accepted for a laptop cluster (T-42).

## A database role's password

`make up` creates each Secret only when it is absent and never overwrites
one. It generates the password, writes `password` and `uri` together (the
`uri` holds the password), and hands both to `kubectl` on stdin, so the
password is never an argument and never printed. CloudNativePG then
applies the Secret's password to the role.

So the rotation the scripts support is: remove the Secret, let `make up`
make a new one, restart what uses it. Run it from a clean checkout of
`main` only: `make up` upgrades every release of the platform with the
pins and values of the tree it runs in, and removes a dashboard whose
file that tree lacks. For one role, here `claims_api`, in a first
terminal:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k delete secret claims-api-db
make up
```

and in a second terminal, as soon as `make up` logs
`platform-db is ready`:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k rollout restart deploy/claims-api
k rollout status deploy/claims-api
```

- The `delete` is the owner's to run, and it cannot be taken back: the
  old password was never printed and is gone with the Secret.
- **Expect an outage of that one service, of minutes.** The pod holds the
  old password from when it started. It keeps working until
  CloudNativePG applies the new one, then every new connection fails and
  it answers 503 until it restarts. `make up` goes on for several
  minutes after it has made the Secret, upgrading the other releases, so
  do not wait for it to end: restart from the second terminal. A pod
  that happens to restart between the delete and the new Secret cannot
  start (`CreateContainerConfigError`) until the Secret exists.
- **`rollout status` does not prove the password works.** `/healthz`
  touches no database, so a pod with a wrong password is ready. Prove it
  with a request that reaches the database, or with the connections
  query in [database failure](database-failure.md#connections-run-out):
  the role has connections that started after the restart.
- `make up` waits until CloudNativePG reports every role reconciled. To
  read that afterwards, ask for the Cluster's
  `.status.managedRolesStatus.cannotReconcile`: it is empty when no role
  failed. That shows nothing failed, not that the new password was
  applied.
- Restart exactly the workload that reads the Secret:

  | Secret | Read by | After a rotation |
  |---|---|---|
  | `claims-api-db`, `agent-runtime-db`, `model-gateway-db`, `policy-mcp-db`, `claims-mcp-db`, `knowledge-mcp-db` | The Deployment of the same name without `-db` | `rollout restart` of that Deployment |
  | `claims-sweep-db` | The sweep's CronJob | Nothing: every run reads it afresh |
  | `gateway-upkeep-db` | No workload: the operator's `meridian gateway` command (designed on kind: nothing yet reads the Secret there) | Nothing restarts: the next run of the command reads the new Secret |
  | `meridian-owner-db` | The migration Job alone | Nothing: every `make deploy` reads it afresh |
  | `policy-seed-db` | The seed Job alone | Nothing: every `make deploy` reads it afresh |
  | `knowledge-ingest-db` | The ingestion Job alone | Nothing: `make deploy` reads it afresh, and an ingestion runs once per image, so a rotation shows at the next new image |

- Restarting the Model Gateway while calls are in flight leaves their
  reservations `reserved`, charged until the period ends
  ([budget exhaustion](budget-exhaustion.md#what-spent-it)). Rotate its
  password when no triage runs.
- This has not been run. Do it first on a cluster that can be thrown
  away. If `make up` stops after the delete, run it again: it converges,
  and creates what is missing. A new cluster (`make down`, `make up`,
  `make deploy`) is the last way out, and it loses the data
  ([database failure](database-failure.md#the-data-is-lost)).

After a leak:

- Rotate every role whose Secret could have been read, not only the one
  that was seen: on kind one reader of the namespace's Secrets reads all
  eleven.
- **A new password does not end a session that is already open.** After
  the restart the owner ends that role's remaining sessions, as the
  superuser: `pg_terminate_backend` over the rows of `pg_stat_activity`
  for the role. It is not given as a block here, because it is not a
  query that only reads.
- **If the owner role's password leaked, the records are no longer
  evidence.** `meridian_owner` owns the schemas, so it can drop the
  triggers that keep the audit table insert-only and the ledger's rows
  closed. Treat `audit.events` and `gateway.usage` as untrusted from the
  moment of the leak.
- Before a cluster is deleted for a security reason, the owner copies
  `audit.events` and `gateway.usage` out of it, into a file outside the
  repository. On kind the cluster is the only copy.

To check which role wrote what while a password was exposed, the audit
table records the database role of every row
([how to run it](../README.md#looking-into-the-database-on-kind)):

```sql
SELECT db_role, service, event, count(*) AS events,
       min(recorded_at) AS first, max(recorded_at) AS last
FROM audit.events
WHERE recorded_at > now() - interval '24 hours'
GROUP BY db_role, service, event
ORDER BY db_role, service, event;
```

A row whose `db_role` is not its service's own role is the thing to look
for. The ingestion's rows (service `knowledge-ingestion`) name
`knowledge_ingest` since S063, where they named `meridian_owner`; the seed
writes none. It is a narrow check. It cannot tell a stolen password used under
its own service's name from the service itself, and it sees writes only:
nothing records a read, and reading is what a stolen password is mostly
used for.

## Grafana's admin password

The same shape: `make up` creates `grafana-admin` only when it is absent.
Remove the Secret (the owner's), run `make up`, and restart Grafana's
Deployment in `observability`, which reads the password at start and
keeps no database of its own on kind. `make grafana-password` prints the
new one, on purpose and to the terminal only. Not exercised.

## The gateway's credential at the provider

The gateway holds no key. In live mode it asks Azure for a token as the
identity it runs under and Azure OpenAI checks that identity's role
(Cognitive Services OpenAI User). Today live mode runs on a laptop only,
as the developer who ran `az login`. So `MeridianModelCredentialRefused`
means one of:

| Cause | Check | Fix |
|---|---|---|
| The login expired | `az account show` | `az login` (the owner's) |
| The login is in another tenant | The tenant against `MERIDIAN_AZURE_TENANT_ID` | `az login --tenant` for the pinned one |
| The role assignment is gone | `make azure-plan`: the plan would add it back | The owner reads the plan and applies it |

A refused credential ends the call at once: the gateway does not try the
second deployment, because both share the credential, and the failure
does not open a circuit.

If a token was sent to the wrong address (T-43), it stays usable until it
expires and cannot be recalled; the gateway refreshes its own five
minutes before expiry. Revoking the sessions of the account is the
owner's, in Entra ID.

On Azure the gateway's identity is designed: a workload identity with the
same role (S020), and no secret to rotate there either.

## A secret in the repository

The repository is public. A secret that reached GitHub is burned the
moment it is pushed, whether or not the commit is later removed.

1. **Rotate first.** Make the secret worthless where it is used; the
   sections above say how for each kind.
2. Then remove it from the tree in a new commit.
3. Removing it from history needs a rewritten branch and a force-push,
   which the owner decides and runs; it does not replace step 1. Forks
   and GitHub's cached views of a commit keep the secret after a
   force-push.
4. Report it as `SECURITY.md` says if someone else's data was exposed.

Before every push: `make secret-scan`, which runs
`gitleaks git --log-opts="origin/main..HEAD" --redact` after checking that
git knows `origin/main` (gitleaks answers a base it cannot find with "0
commits scanned" and success). CI scans every commit of a pull request, so
a finding in an early commit is not fixed by a later one.

## What not to do

- **Do not print a Secret** to look at it: no `kubectl get secret -o
  yaml`, no `echo` of a password, no password as a command-line argument.
  Do not rely on the harness to stop it. Its hook denies a `get` of a
  Secret with any output format but `name` and `wide`, with a namespace
  flag in front or behind a shell function too, as every command on this
  page is; it asks before `psql` through `kubectl exec` and before
  `make grafana-password`. It reads only the command a session types: a
  script's inside, a file a pod mounts and a pod's environment it does
  not see, and a person's terminal never meets it. The rule is the
  operator's to keep.
- **Do not edit `password` without `uri`**, or the other way round. The
  services read `uri`; CloudNativePG reads `password`.
- **Do not rotate while the database is down.** CloudNativePG cannot
  apply the new password, and the services would hold one the role does
  not have.
- **Do not turn on key authentication** for Azure OpenAI to get past a
  refused identity. A key is a secret this platform is built not to
  hold (T-18).

## Designed, not built

- Provider credentials in Key Vault, read through a workload identity
  (S020).
- The app registrations and the mock issuer's signing key for sign-in
  (S021).
- The pipeline's cloud identity by federation, with no stored secret, and
  the image signing key (S022).
- A schedule: nothing rotates on a timer today, and nothing warns before
  a certificate expires.
