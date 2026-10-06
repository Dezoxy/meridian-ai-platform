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
| The rate store's password (S066): the gateway's address in Redis and Redis's access-control file, which holds the password's SHA-256 | Secret `rate-store-credentials` in `meridian` (keys `uri` and `users.acl`); the gateway reads the first as an environment variable and only the store mounts the second | `make up`, once, only if absent | [The rate store runbook](rate-store.md#a-password-was-rotated-or-the-two-disagree): delete the Secret, `make up`, restart the store and then the gateway; refused calls in between; not exercised |
| The database's certificate authority and server certificate | Secret `platform-db-ca` and CloudNativePG's own | CloudNativePG | CloudNativePG issues and renews them; the repository records no expiry to watch (the plan's backlog) |
| The password of the role `app` | Secret `platform-db-app` | CloudNativePG | Not used: that role cannot reach the `meridian` database |
| The cluster's admin credentials | `infra/kind/kubeconfig`, gitignored | `make up` | A new cluster: `make down`, `make up` (disposable on the development machine, hard rule 8) |
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
  | `gateway-upkeep-db` | The upkeep Job alone, which `make gateway-upkeep` applies for one run, outside the release (implemented and tested, and run on kind on 2026-10-06): no Deployment, no CronJob and no other Job holds it | Nothing restarts: each run is a new Job and reads the Secret afresh; a Job already running keeps what it read |
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
  eleven, and the rate store's.
- **The rate store's Secret is one of them** (S066). Whoever read
  `rate-store-credentials` holds the gateway's credential for the store: they
  can fill or empty any tenant's rate window and freeze the store with a script
  that never ends, which makes every model call a 503. The budgets and the
  ledger are in PostgreSQL and are not reached that way. A rotation restarts
  the store and then the gateway
  ([the rate store runbook](rate-store.md#a-password-was-rotated-or-the-two-disagree)).
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
  page is; it denies the other ways to a value (the list is in
  [what the guard does not see](#what-the-command-guard-does-not-see));
  it asks before `psql`, `pg_dump`, `pg_dumpall` or `pg_restore` through
  `kubectl exec`, `kubectl run` or `kubectl debug`, before `kubectl cnpg
  psql`, before `helm get manifest`, `values`, `hooks` and `all`, before
  `make grafana-password`, before `make gateway-upkeep` when it can
  change a tenant's budget ledger, and before the other printers of a
  credential listed below. It reads only the command a session types.
  The rule is the operator's to keep.
- **Do not edit `password` without `uri`**, or the other way round. The
  services read `uri`; CloudNativePG reads `password`.
- **Do not rotate while the database is down.** CloudNativePG cannot
  apply the new password, and the services would hold one the role does
  not have.
- **Do not turn on key authentication** for Azure OpenAI to get past a
  refused identity. A key is a secret this platform is built not to
  hold (T-18).

## What the command guard does not see

The guard (`.claude/hooks/guard-bash.sh`) is a pattern on the command a
session types, a guard for habits and not a boundary (T-87): it stops a
session from doing by reflex what it should stop and think about, and it
does not stop one that means to get past it. This is what it does about
secrets as of S075, and what it leaves. A backslash-newline is deleted
before any rule reads the command, as the shell deletes it, so a word
broken across two lines is read whole.

Denied, with a message that says what to do instead:

- `kubectl exec … -- env` and `-- printenv` (with options, `-u X`,
  `-C dir`, a redirect such as `2>&1`, a comment, a quoted name, a wrapper
  such as `timeout 5`, `sudo` or `sudo -u name`, and a line break or
  backtick after it), `set` and `export -p`, also inside `sh -c "…"`,
  `bash -c` and `ash -c` (with options before `-c`, such as `-o pipefail`
  or `--norc`; after `do`, `then`, `else`, `if` or `!`; in a nested `sh -c`;
  and past an escaped quote or a quote that touches the next word);
  `kubectl debug` is read the same way as `exec`. `env VAR=x some-command`
  passes. `printenv NAME` of one name passes unless the name looks secret
  (it holds `secret`, `pass`, `token`, `key`, `cred`, `auth`, `uri`, `url`,
  `dsn`, `conn`, `database`, `private`, `cert`, `sign` or `salt`), so
  `printenv HOME` passes and `printenv DATABASE_URL` is denied;
  `printenv` alone, with two names or with a `$` in a name is denied. A
  name outside that list that holds a secret passes: the rule reads the
  name, not the value;
- `kubectl exec … -- <anything>` (and `debug`) that names a mounted
  Secret path, `/var/run/secrets`, `/run/secrets`, `/etc/secrets` or
  `/proc/…/environ`, whatever the reader: `grep`, `tar`, `find -exec`,
  `awk`, `sed`, `od`, `dd`, `python` and the rest. It passes when the
  command is `ls`, `stat` or `test` (also as the whole body of an
  `sh -c`), which show a name and no content; `ls /proc/…/environ` is
  still denied, by the rule for that file;
- `kubectl exec … -- cat` (also `head`, `tail`, `less`, `base64`, `xxd`,
  `strings`) of a path under those directories, of a path with `secret`,
  `credential`, `password`, `.key`, or `token` or `creds` as a word
  (`tokenizer.json` passes); in an `sh -c` body, a reader and a mounted
  path anywhere in it, in either order;
- `kubectl get secret` with a verbosity of 8 or more (`-v=8`, `-v 9`,
  before or after `get`), which makes kubectl log the response body. This
  comes from kubectl's documented behaviour; nobody ran it against a
  cluster for this rule;
- `kubectl get --raw` of an API path that holds `/secrets`, the flag and
  the path in either order, also with a percent-encoded `secrets` (a path
  with more than 20 distinct escapes asks instead, since the guard
  decodes 20 and does not read the rest);
- `kubectl config view --raw` and `--flatten`;
- `kubectl create token`, with other flags between the two words
  (`create secret generic token` is another command and passes);
- `kubectl cp` in either direction when the pod path is of the kinds
  above;
- `az containerapp secret show` and `az containerapp secret list` with
  `--show-values`, `gcloud secrets versions access`,
  `aws secretsmanager get-secret-value` and `batch-get-secret-value`, and
  `aws ssm get-parameter`, `get-parameters`, `get-parameters-by-path` or
  `get-parameter-history` with `--with-decryption`;
- reading a kind node's `admin.conf` or any `kubeconfig` with `cat`,
  `less`, `bat`, `more`, `head`, `tail`, `echo`, `printf`, `xxd`,
  `base64` or `strings`, also inside a quoted `sh -c` body, in brackets or
  after an equals sign.

A secret reader followed by `--help` or `-h` in the same command part
passes (`aws secretsmanager get-secret-value --help`, `helm get values
-h`, `kubectl create token --help`): that prints usage, no value. A second
read in the same line, without `--help`, still counts.

Asked: the database forms, `helm get manifest|values|hooks|all` (options
may stand before the verb; a verb on the next line is not part of it, a
backslash-newline continuation is) and `kind get kubeconfig` (write it to
a file with `kind export kubeconfig` instead). Also:

- `make gateway-upkeep` and `infra/kind/upkeep.sh`, when the command can
  change a tenant's budget ledger: `credit`, `close` and `expire` with
  `--confirm`. It fails closed: it asks unless the command shows exactly
  one `ARGS=` whose first word is `reservations` or `expire`, with no
  `--confirm`. So `reservations` and a dry-run `expire` pass; a variable,
  any other word, an `ARGS` set by `export` in an earlier part of the line
  and a bare `make gateway-upkeep` ask. Two such commands in one line are
  judged one by one, and a deny in another part of the line still wins;
- `kubectl run` with `--overrides` or `--env`: the pod holds none of the
  workload's own environment, but these can put a Secret into it
  (`secretKeyRef`, `envFrom`, a volume), and its command prints it.
  `kubectl run` without them passes, and so do `docker run --env` and `uv
  run`;
- the other printers of a credential, as `az account get-access-token`
  already asks: `kubectl create --raw …/serviceaccounts/NAME/token`;
  `aws sts get-session-token`, `aws sts assume-role` (also with SAML or
  web identity), `aws configure export-credentials`, `aws configure get`
  of `aws_secret_access_key` or `aws_session_token`, `aws ecr
  get-login-password` and `aws eks get-token`; `gh auth token`; `gcloud
  auth print-access-token` and `print-identity-token`, also under
  `application-default`; `az acr credential show`, `az acr login
  --expose-token`, `az storage account keys list` and `az ad sp
  create-for-rbac`; `crictl inspect` (a container's environment, reached
  on a kind node with `docker exec`); and a path of the Secrets API
  (`/api/v1/secrets`, `/api/v1/namespaces/NAME/secrets`) anywhere in a
  command, which is how `kubectl proxy` with `curl` would read them.
  `helm get notes` and `helm get metadata` pass: metadata prints the
  chart's name, version and status, and notes prints `NOTES.txt`, which
  this chart does not have (a chart that renders a value into its notes
  would print it; that is a fact about this chart, not about Helm).

Timeouts. A hook that runs past its timeout does not block the call, so
the guard cannot be allowed to run long. The bounds below do not prevent
that for every shape (the first ones were measured on one-word segments
only); the watchdog does. The hook arms it before it reads the command: at
5 of the 10 seconds it answers `ask`, saying that the guard ran out of time
and the command was NOT read. It cannot interrupt a command in flight, one
regex match: the slowest single match at 8192 bytes took 0.23 to 0.27 s
on 2026-10-06 at a load average of 3 and of 18 (about 1 s at 16384), and
1.5 s at the slowdown a loaded machine showed, inside the margin of 5 s.
A test holds the two numbers together by reading the timeout from
`.claude/settings.json`. A command the watchdog cannot answer in time
still goes through unread: that is a way through the guard that no rule
closes, and the list below has the others.

Three bounds ask before the rules run, and each ask says that the command
was NOT read and may hold a form the guard would deny: more than 16384
bytes typed; more than 8192 bytes of command once heredoc bodies written
to a file are dropped; more than 1000 parts (split on newlines, `;`, `&&`,
`||` and `|`, the way the rules read it). Write the script with the Write
tool and run the file. A long command that holds a denied form therefore
asks, it does not deny.

Not seen:

- A script's inside. `bash x.sh` is read as that line; the lines in the
  file are not. `infra/kind/*.sh` run `kubectl exec … psql` and `kubectl
  get --raw` that way, and so does anything a session writes and runs.
- Quoting inside a word: `e''nv`, `secr''ets`, `get-secret-"value"`. The
  shell joins the pieces; the guard reads the pieces.
- A variable that holds the verb or the path (`$P`, `K=kind; $K get …`),
  and brace expansion (`{env,}`, `{secrets,}`).
- An alias for a tool (`alias a=aws`), and a command put together by
  `eval`.
- A file written and then run: a heredoc to a file and `bash` on it, or
  the Write tool and then `bash`. `bash x.sh` is read as that line, and
  the lines in the file are not: `infra/kind/*.sh` run `kubectl exec …
  psql` and `kubectl get --raw` that way. A heredoc body that only writes
  a file is not read; one fed to a shell, an interpreter, `. file` or
  `source` is, and so is the body of an unquoted delimiter that holds
  `$(` or a backtick. `$(cat <<'EOF' … )` used as a command is the one
  that passes though its body runs.
- An interpreter in the pod: `kubectl exec … -- python -c "import os;
  …"` of the environment, `awk` printing it, or an application's own
  debug endpoint. (A mounted path named in any exec is denied, as above;
  the environment has no path to name.)
- A value echoed by name: `sh -c "echo \$DATABASE_URL"` (the name is not
  `printenv`'s), and a command piped into a shell in the pod (`echo
  printenv | kubectl exec -i pod -- sh`), `kubectl exec pod env` without
  `--`, and a wrapper verb the rule does not list.
- A proxy and `curl`: `kubectl proxy` and then `curl` on a path that a
  variable builds. A literal path of the Secrets API in any command
  asks; one assembled in a variable is not seen.
- A local secret file read by a reader that is not on the list (`grep`,
  `awk`, `jq`, `cp`, `docker cp`) or named by a glob (`cat .en*`):
  `docker exec <kind node> cat /etc/kubernetes/admin.conf` is denied as a
  read of `admin.conf`; any other way to the kind node's credentials is
  not seen. A text that only mentions such a file in an `echo` is denied
  all the same, and so is `ls /proc/1/environ` in a pod (known false
  positives, left).
- A backslash-newline inside single quotes is deleted too, as in every
  other place: `echo 'git push --for\<newline>ce'` is denied though the
  shell would keep it. A path with more than 20 distinct percent-escapes
  asks. Quote parity decides whether a `<<` opens a heredoc, so a file
  name with an apostrophe in it (`cat > "it's.txt" <<EOF`) makes its body
  be read.
- `docker exec <container> psql` and `pg_dump`: the tests' own database
  container is reached that way, so these pass. A container that holds
  anything else is not covered.
- A tool's own API call, a browser (Grafana, a port-forward) and a
  person's terminal.
- Anything that is not typed as a command: a file the harness reads, a
  tool call that is not Bash.
- The guard's own files. The permission rules allow `Edit` and `Write`
  on `.claude/hooks/guard-bash.sh` and `.claude/settings.json` (only
  `.env` and `*.tfvars` are denied), so a session can change or switch
  off the guard. A deny rule on them is the owner's decision; it is
  recorded here and not built.
- The cloud consoles, and the cloud CLI's other ways to a value than
  the ones listed.

## Designed, not built

- Provider credentials in Key Vault, read through a workload identity
  (S020).
- The app registrations and the mock issuer's signing key for sign-in
  (S021).
- The pipeline's cloud identity by federation, with no stored secret, and
  the image signing key (S022).
- A schedule: nothing rotates on a timer today, and nothing warns before
  a certificate expires.
