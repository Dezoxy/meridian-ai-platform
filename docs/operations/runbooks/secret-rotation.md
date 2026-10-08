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
  `make grafana-password`, before `make identity-passwords` and
  `infra/kind/identity.sh passwords` (the test users' passwords of the local
  sign-in issuer; the settings ask for the target too), before `make
  gateway-upkeep` when it can
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
  or `--norc`; after `do`, `then`, `else`, `if` or `!`; in a nested `sh -c`,
  with quotes of its own, such as `sh -c 'sh -c "env"'`, to three levels;
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
  Secret path, `/var/run/secrets`, `/run/secrets`, `/etc/secrets`, the
  chart's own mounts (`/etc/meridian`, which holds the services' TLS key
  and the database CA, and `/etc/redis-acl`, the rate store's ACL file) or
  `/proc/…/environ`, whatever the reader: `grep`, `tar`, `find -exec`,
  `awk`, `sed`, `od`, `dd`, `python` and the rest. It passes when the
  command is `ls`, `stat` or `test` (also as the whole body of an
  `sh -c`), which show a name and no content, or `redis-cli` (the rate
  store runbook's probe names its certificate and key files and prints
  none of them); `ls /proc/…/environ` is
  still denied, by the rule for that file. Every ` -- ` of the call is
  tried, so `ls -- /etc/secrets` is denied for its second one (a false
  deny, left), and `/etc/meridian` also covers a ConfigMap of public
  certificates (`/etc/meridian/telemetry-ca`), another one. A test reads
  the chart's templates and fails when a Secret is mounted at a path the
  hook does not name, so the next mount is not forgotten;
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
  the path in either order, also with a percent-encoded `secrets` and with
  an encoded separator before it (`%26`, `%3B`, `%7C` and `%0A` are read as
  plain characters, not as the end of the command); a path with more than
  20 distinct escapes asks instead, since the guard decodes 20 and does
  not read the rest;
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
  `base64` or `strings`, also inside a quoted `sh -c` body, in brackets,
  after an equals sign or behind a backslash (`\cat .env`, which skips an
  alias).

A secret reader followed by `--help` as an argument in the same command part
passes (`aws secretsmanager get-secret-value --help`, `helm get values
--help`, `kubectl create token --help`): that prints usage, no value. So
does `-h`, except for `gh`, where `-h` is `--hostname` and `gh auth token -h
github.com` prints the token (it asks). A `--help` or `-h` inside a quoted
value (`--query 'a --help b'`) or after a `#` is no argument and does not
count, and neither does one in a later command of the line. A second read
in the same line, without `--help`, still counts.

Asked: the database forms, `helm get manifest|values|hooks|all` (options
may stand before the verb; a verb on the next line is not part of it, a
backslash-newline continuation is) and `kind get kubeconfig` (write it to
a file with `kind export kubeconfig` instead). Also:

- `make gateway-upkeep` (also `gmake` and `gnumake`) and
  `infra/kind/upkeep.sh`, when the command can change a tenant's budget
  ledger: `credit`, `close` and `expire` with `--confirm`. The script is
  read when it is run: at the start of a command or a bracket, after
  `VAR=value` words, `env`, `time`, `nohup`, `exec`, `command` or `sudo`,
  after an interpreter with flags (`bash -x upkeep.sh`; `bash -n` only
  checks the syntax and passes), after `source` or `.`, and in the body of
  a `bash -c '…'`. `cat`, `shellcheck`, `git diff` and `grep` of its name
  pass. It fails closed: it asks unless the command shows exactly one
  `ARGS=` whose first word is `reservations` or `expire`, with no
  `--confirm`. So `reservations` and a dry-run `expire` pass; a variable, an
  `ARGS` that holds a `$` or a backtick anywhere (make and the shell expand
  it, even in single quotes, into a word the guard cannot read), any other
  word, an `ARGS` set by `export` in an earlier part of the line and a bare
  `make gateway-upkeep` ask. Two such commands in one line are judged one by
  one, and a deny in another part of the line still wins;
- `kubectl run` with `--overrides` or `--env`: the pod holds none of the
  workload's own environment, but these can put a Secret into it
  (`secretKeyRef`, `envFrom`, a volume), and its command prints it.
  `kubectl run` without them passes, and so do `docker run --env` and `uv
  run`;
- the other printers of a credential, as `az account get-access-token`
  already asks: `kubectl create --raw …/serviceaccounts/NAME/token`;
  `aws sts get-session-token`, `get-federation-token` and `assume-role`
  (also with SAML or web identity), `aws configure export-credentials`,
  `aws configure get` of `aws_secret_access_key` or `aws_session_token`,
  `aws ecr get-login-password` and `get-authorization-token`, and `aws eks
  get-token`; `gh auth token` and `gh auth status --show-token` (or `-t`);
  `gcloud auth print-access-token` and `print-identity-token`, also under
  `application-default`; `az acr credential show`, `az acr login
  --expose-token`, `az storage account keys list`, `az ad sp
  create-for-rbac` and `az ad sp credential reset`; `crictl inspect` (a
  container's environment, reached on a kind node with `docker exec`); and
  a path of the Secrets API (`/api/v1/secrets`,
  `/api/v1/namespaces/NAME/secrets`) anywhere in a command, which is how
  `kubectl proxy` with `curl` would read them. A printer asks wherever it
  stands: after a blank, at the end, before a separator, a closing bracket,
  a quote or a backtick, so `$(gh auth token)`, `` `gh auth token` ``,
  `TOKEN=$(gcloud auth print-access-token)` and `gh auth token|wc -c` ask as
  `gh auth token` does.
  `helm get notes` and `helm get metadata` pass: metadata prints the
  chart's name, version and status, and notes prints `NOTES.txt`, which
  this chart does not have (a chart that renders a value into its notes
  would print it; that is a fact about this chart, not about Helm).

The AWS environment (S036; the rules are implemented and tested, and the
module they guard, `infra/terraform/aws/`, is checked and not applied
anywhere). The guard stops a session from doing
by reflex what only the owner should do there, and it is not the barrier for
the second half of the step: with credentials on the machine a session
reaches every call below by a variable, a quote in the middle of a word, a
script file, `python3`, `uv` or a container. The barrier is where the
credentials are, a machine or an operating-system user where no session runs
and no credential file is readable by one
(`infra/terraform/aws/README.md`, "What stops a session, and what does
not"). The rules read the command with prose blanked, so a commit message
(`-m`, `--message`) or a pull request body (`--body`) that names one passes.
Nothing else is blanked: the guard reads a quoted word as a use of what it
names (a search, an `echo`, a commit message given with `-b`, `-t` or
`--subject`), so search with the Grep tool and write a message to a file.
The searches and pseudo-terminal rows marked "false alarm by design" in
`tests/guard-bash-cases.jsonl` hold this.

Denied:

- `make aws-destroy` and `infra/terraform/aws.sh destroy`, in the runner
  forms the Azure rules read (`bash`, `./`, a path, after `cd … &&`, in
  `bash -c`, behind `env`, `time`, `xargs` before the script, `gmake`; a
  backtick after the target ends it). Not read: a variable or stdin that holds
  the target (`for t in aws-destroy; do make $t; done`,
  `echo aws-destroy | xargs make`);
- `aws.sh` or `make aws-plan|apply|destroy` in a command that also names a
  pseudo-terminal tool (`script`, `unbuffer`, `expect`, `socat`, `setsid`,
  `pty`) anywhere, because the wrapper's terminal check is `[[ -t 0 ]]` and a
  pseudo-terminal passes it. A search for the word in the wrapper
  (`grep -n script infra/terraform/aws.sh`) is denied too, a false alarm by
  design;
- `aws.sh` or `make aws-*` run traced (`bash -x`, `-v`, `-xv`, `-o xtrace`,
  `sh -x`, `set -x`, `set -o xtrace`) or with `SHELLOPTS=`, `BASH_XTRACEFD=`,
  `BASH_ENV=`, `ENV=` or `PS4=` set, which print what the script keeps out of
  its output or run code before its first line;
- a `TF_*` or `AWS_ENDPOINT_URL*` assignment in a command that runs
  `terraform`, `tofu`, `aws`, `aws.sh` or `make aws-*`. This holds for the
  Azure foundation too: `TF_VAR_x=1 terraform -chdir=infra/terraform/foundation
  validate` is denied;
- `terraform` or `tofu` by hand where the module is, which is where the same
  command names it (`-chdir=…/aws`, a `cd` into it in the same command, or the
  name of its state directory, state file or plan), or where the working
  directory the harness passes in the hook's input (`cwd`) is the module's or
  under it. Whether that `cwd` follows a `cd` made by an earlier Bash call is
  not verified, so a `cd` in one call and `terraform apply` in the next may
  pass. The verbs are `apply`, `destroy`, `plan -out`, `import`,
  `state mv|rm|push`, `force-unlock` and `workspace new|delete|select` of
  another name than `default` (`default2`, `defaults`, `default-prod` and
  `default_x` are other names); `validate`, `fmt` and `init -backend=false`
  pass. A bare
  `tofu destroy` is denied as `terraform destroy` is, and a bare `tofu apply`
  asks as `terraform apply` does;
- the `aws` CLI (also the `amazon/aws-cli` image and `uvx --from awscli aws`)
  for `iam create-access-key`, `create-service-specific-credential` and
  `reset-service-specific-credential`, `rds generate-db-auth-token`,
  `kms decrypt`, `sso get-role-credentials`, any `delete-*`, `terminate-*` or
  `purge-*` operation (also `batch-` and `force-`), `s3 rm` and `s3 rb`, and
  the flags `--skip-final-snapshot` and `--force-delete-without-recovery`;
  `--help` passes. The secret readers and the session, role, registry and
  cluster token printers are S075's rules above, which already deny or ask;
- a reader (`cat`, `grep`, `rg`, `sed`, `awk`, `cut`, `od`, `nl`, `tac`,
  `diff`, `jq`, `cp`, `tar`, `python3`, `perl`, `source` and the rest of the
  list in the hook) of the local file `local.env*`, a `.tfstate`, `.tfplan` or
  `.tfplan.meta` file, `terraform.tfstate.d`, `meridian-aws` (the state's
  directory), `~/.aws`, `~/.terraformrc`, `~/.terraform.d` and `.tfvars` or
  `.tfvars.json` (a `.tfvars.example` passes); `ls`, `stat`, `test` and `git
  check-ignore` of them pass;
- a write (`>`, `>>`, `tee`, `cp`, `mv`, `install`, `ln`, `dd`, `rsync`,
  `truncate`, `sed -i`) to `~/.terraformrc`, `~/.gitconfig`,
  `~/.config/git/`, `~/.aws/`, `.terraform/environment` or `aws.tfplan*`.

Asked:

- `make aws-plan` and `aws.sh plan`, which sign in with the owner's
  credentials; and `make aws-apply` and `aws.sh apply`, whose text says that
  it costs money and that the owner runs it from where no session holds the
  credentials;
- `make … TRIVY_IMAGE=` and `PROMTOOL_IMAGE=`, which replace a pinned image
  (the Makefile's `:=` yields to the command line, not to the environment);
- `terraform` or `tofu` where the module is (as above, the same command or
  the `cwd`) with `plan` (it signs in with the owner's credentials), `show`,
  `output`, `console`, `refresh`, `state list|show|pull` and
  `workspace select default` (the README's way back to the default workspace,
  also with two blanks or a quoted `"default"`; `workspace show` asks too, as a
  `show`), and `-auto-approve` anywhere. The
  Azure foundation's `terraform output`, `state list` and `plan` pass, as
  before. The settings add an ask for the bare `terraform plan` and for `-chdir`
  into the module, which holds in Claude Code only (Codex reads no settings;
  the hook's own ask for `plan` above is what Codex has);
- any `aws` call whose operation is not on the read list: `describe-*`,
  `list-*`, `sts get-caller-identity`, `help`, `--version`, `sso login` and
  `logout`, `configure list` and `get`, `s3 ls` and the `ssm get-parameter`
  family (S075's cases pass those). S075 denies `--with-decryption` and asks
  for `configure get` of a secret key or a token when they are typed plain;
  the quotes are stripped here, so `aws "ssm" get-parameter --with-decryption`
  and `aws "configure" "get" aws_secret_access_key` are not listed reads and
  ask. The `get-*` operations other than those ask, on purpose (fail
  closed). `aws` is read
  where a command starts, after `VAR=value`, `env`, `time`, `nohup`, `exec`,
  `command`, `sudo`, `nice` or `xargs` (each with no option of its own), `if`,
  `do`, a bracket or a quote, in the `amazon/aws-cli` image and after `uvx
  --from awscli`; a path that ends in `aws` is not a call. A quoted service or
  operation is read without its quotes (`aws "ec2" run-instances` asks), and a
  token of nothing but quotes asks. A call after more than 256 bytes of what
  stands before it, and a ninth call in one command, ask unread. `--help`
  passes. `eks update-kubeconfig` asks, because it writes `~/.kube/config`;
- `python3` or `uv run` that names `boto3`, `botocore` or `awscli`.

Passes unasked: `make aws-validate`, `make aws-scan`,
`infra/terraform/aws.sh validate`, `shellcheck` and `bash -n` of the wrapper,
and every Azure command as before (`make azure-plan` and `azure-smoke` pass,
`azure-state` and `azure-apply` ask).

The settings add a second layer for the removal: `Bash(make aws-destroy*)`,
`Bash(infra/terraform/aws.sh destroy*)` and the `./` form are denied there, so
a hook that timed out or crashed does not let it through in Claude Code
(`Bash(make *)` is allowed).

Not seen, once, for this family (nothing below is built; each is a way past a
guard for habits, and the barrier is where the credentials are):

- Wrappers before `aws` that the ask does not read: `timeout`, `nice -n`,
  `watch`, `stdbuf`, `env -i`, `sudo -u`, `xargs -I{}`, `find -exec`, a brace
  group `{ aws …; }`, a path prefix (`~/.local/bin/aws`, `$HOME/…`, `./aws`)
  and `uv run aws`. The delete and credential denies read the same shapes
  anywhere in the command and still deny them.
- The `hashicorp/terraform` image run, `terragrunt` (except `-auto-approve`)
  and `terraform state replace-provider` and `login`.
- Readers after a `cd` into a credentials directory (`cd ~/.aws && cat
  credentials`), through `find … -exec cat`, a redirect (`< file`, `while read
  … < file`), a glob (`cat ~/.a*/credentials`, `cat infra/terraform/lo*`), a
  recursive search of the home or of `infra/terraform` (`grep -r AKIA ~`), or
  `python3` with `pathlib`.
- `git config --global` and `--system` (a write of `~/.gitconfig` through git),
  with a key that runs a program (`core.fsmonitor`, `alias.x '!…'`,
  `include.path`, `core.sshCommand`); `git add -f` of the local file.
- The session's own start-up files under the home directory (`~/.bashrc`,
  `~/.profile`, `~/.local/bin/*`): the home-file list above is by name and not
  complete, and when the owner's shell shares the home directory those steer as
  strongly as `~/.gitconfig`.
- The wrapper or its removal named in a variable (`T=destroy; aws.sh $T`) or
  in a script file; a pseudo-terminal made inside a script file; the `aws` CLI
  through `python3` that is not `boto3`, through a container that is not
  `amazon/aws-cli`, or through `uvx awscli` and `pipx run awscli`.
- `AWS_PROFILE`, `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`,
  `GIT_CONFIG*` and `GIT_DIR` assignments (they point at other files; the
  contract lists only `TF_*` and `AWS_ENDPOINT_URL*`); `make SHELL=` and
  `.SHELLFLAGS=`.
- The settings: `Read`, `Edit` and `Write` are denied for the paths in
  `.claude/settings.json`, and whether the bare `Grep` and `Glob` tools obey the
  `Read` denies is not verified; the `./` patterns are relative to the
  session's directory, and whether they match a file reached by an absolute
  path from another checkout is not verified; Codex reads no settings and runs
  this hook through `.codex/hooks`, a link to `.claude/hooks`.
- The two generic rules for the removal verb (`terraform` and `tofu` followed
  by `destroy`, and by `apply` for the ask) read the whole command, not the
  command with prose blanked: a commit message or a pull request body that
  names `terraform destroy` or `tofu destroy` is denied for both tools (write
  the message to a file and pass the file).
- The second tool's bare state-surgery verbs (`tofu state rm`, `import`,
  `force-unlock`) answer none where `terraform`'s ask, unless the module is
  named or is the working directory.
- `gh … -b`, `-t` and `gh pr merge --subject` are not blanked as `--body` and
  `--title` are, so a body given with `-b` that names `make aws-destroy` is
  denied.
- A working directory that reaches the module only through `..` or a link
  (`…/foundation/../aws`) is not seen; one that ends in `/..` counts as the
  module, and fails closed.

The Azure platform module (S020, GA1, GA2 and GA3; the rules are implemented and
tested against a case file and were never seen in a live session). The module,
`infra/terraform/azure/`, is checked and never applied, and the wrapper the
rules guard, `infra/terraform/azure.sh` with the targets
`make azure-platform-plan`, `azure-platform-apply` and `azure-platform-destroy`,
does not exist yet: the rules come first, on purpose. They are the AWS family's
twins, added beside the older rules (the hook's hunks for them add lines and
change none, and a differential over the whole case file finds no decision
weaker than before). What they read differs by block. GA1's scanner reads the
raw command, because a bash pattern cannot know a quote: a name counts where
`make` or the script is the command word of a part, so a commit message or a
search that names one passes, and a body that holds the name at a line start
does not. GA2's blocks and GA3's read the command with prose blanked (the
quoted value of `-m`, `--message`, `--body`, `--title` and `--notes` is
emptied). The barrier is the same one as for the AWS rules: that no Azure
sign-in is on the machine a session runs on, which S071 plans to end for a live
model call.

Denied:

- `make azure-platform-destroy` (also `gmake`, `$MAKE`) and
  `infra/terraform/azure.sh destroy` (by path, `./`, `bash`, `sh`, behind
  `cd … &&`), where `make` or the script is the command word of a part, behind
  assignments, `env`, `sudo`, `time`, `nohup`, `xargs` and the like; a quoted
  target after a make option; the same inside the body of `bash -c`, `sh -c`,
  `eval` and `su -c`; and after `env -S` with a quoted word before the build
  tool, a shape that also reads the AWS targets (`env -S '' make -m
  "aws-destroy"`), which closes the open medium of S071's sixth review. An
  `echo`, a `printf`, a search, a commit message and a pull request body that
  name the target are what they were;
- the removal behind a runner word (GA3), read as the AWS twin reads it
  (the build tool, anything in the same part, the target; or the script,
  anything in the same part, `destroy`) when one of these stands in the same
  command: `ssh`, `doas`, `pkexec`, `chroot`, `nsenter`, `flock`, `stdbuf`,
  `ionice`, `taskset`, `unshare`, `watch`, `parallel`, `tmux`, `screen`,
  `faketty`, `winpty`, `source`, `su`, `fish`; the dot command; `-exec`; a pipe
  into a shell; `<<<`; a substitution (`$(` or a backtick); `system(`,
  `execSync`, `spawnSync`, `subprocess`, `popen`, `child_process`, `os.exec`,
  `os.spawn`; a backslashed `make` or shell; `--eval`, `--rcfile`,
  `--init-file`; and `-c --` before a body. The word alone is not read: a
  search, an `echo`, a commit message or a pull request body that names the
  removal and no runner passes, so do `python3 -c "print('make
  azure-platform-apply')"` (a bare interpreter with `-c` is not a runner), a
  pipe into `shellcheck` or `shfmt`, and a search beside a file named
  `ssh.md`. GA1's scanner also reads `/usr/bin/env make …` and `env -S` behind
  a path now;
- the wrapper's names under a pseudo-terminal tool or traced, as the AWS twins
  (a search for the word `script` in a command that names the wrapper is
  denied too, a false alarm by design; the message says the way round, which is
  to leave the word out, `bash -n azure.sh && echo ok` passes, or to search
  with the Grep tool), and a `TF_*=` or any `ARM_*=`
  assignment in front of them (the foundation's own `make azure-plan`,
  `azure-apply` and `azure-smoke` do not move);
- `terraform` or `tofu` by hand where the module is: `-chdir` or a `cd` into
  `infra/terraform/azure` (the directory itself, at most one slash after it;
  `azure-x` and the foundation are other directories), the working directory
  the harness passes (at any depth), or the plan's or the state's name
  (`azure.tfplan`, `azure.tfstate`). The verbs are the AWS twin's list, which is
  `apply`, `destroy`, `plan -out`, `import`, `state mv|rm|push`,
  `force-unlock` and `workspace new|delete|select` of another name than
  `default`, and three more: `taint` and `untaint`, `state replace-provider`,
  and `init` with `-backend-config`, `-migrate-state` or `-force-copy`.
  `validate`, `fmt`, `providers`, `init`, `init -backend=false` and
  `init -reconfigure` pass;
- a reader (the AWS twin's list, with `unzip`) of the wrapper's local file
  `local.env-azure` (a name with `.example` after it is not named, though the
  older AWS list denies a reader of it all the same), of `azure.tfplan` and its
  record `azure.tfplan.meta`, of the module's `.terraform/` folder (a bare
  `.terraform` counts when a `cd` into the module or the harness directory says
  where the command runs; the lock file `.terraform.lock.hcl` is not the
  folder), and of the cloud CLI's folder `~/.azure` (also `$HOME/.azure`,
  `${HOME}/.azure`, an absolute home and a relative `.azure`);
- a writer of the same: `>`, `>>`, `tee`, `touch`, `install`, `mv`, `ln`, `cp`,
  `dd`, `rsync`, `truncate` and `sed -i`, and the dot command and `source` of
  the local file, which would run it in the session's shell;
- `az storage blob` with `upload`, `download`, `delete`, `undelete`, `lease`,
  `snapshot`, `copy` or `sync` (the `-batch` forms too) in a part that names
  the state, `tfstate` or the state account's name prefix `stmeridiantf`
  (behind a prefix or in a shell's body too; a part that starts with a printing
  command and holds no substitution is a message or a search and is not read).
  `az storage blob list` and `show` pass.

Asked:

- `make azure-platform-apply` and `azure.sh apply`, whose text says that it
  costs money and that the owner runs it after reading the plan, in a terminal
  of their own; `make azure-platform-plan` and `azure.sh plan`, whose text says
  that they use the sign-in and read the remote state, which holds the
  operator's address since the foundation's firewall (the settings allow `make
  *`, so the settings add an ask for both targets and deny the removal in the
  forms a session types); the same two behind a runner word (GA3: the removal's
  list above, plus the apply and the plan under `ssh -tt`, `tmux`, `screen`,
  `faketty` and `winpty`, and a shell with `--rcfile`), whose text is the same;
- `AZURE_CONFIG_DIR=` in front of the wrapper's names (it moves the sign-in the
  wrapper uses);
- any command that holds one of the names when the scanner of GA1 did not run
  (`python3` is missing from the PATH, or it raised): the hook asks and says
  that the name rules did not read the command. It never answers none on a
  command the gate recognised. Measured with no `python3` on the PATH, the
  plain removal, `-C`, `bash -c`, `sudo`, the script's removal and `TF_LOG=1`
  with the plan all ask where they were none; the settings deny the four plain
  spellings of the removal, and the removal behind a runner word and the
  readers of the closed files are still denied, because the ask comes after
  those denies. Not made a deny without `python3`: that would need a pure-bash
  rule for the plain form, the AWS twin's pattern copied as it is, which
  flipped four prose rows;
- `terraform` or `tofu` where the module is (as above) with `plan` (it signs
  in), `show`, `output`, `console`, `refresh`, `state list|show|pull` and
  `workspace select default` (`workspace show` asks as a `show`). The settings
  allow `terraform -chdir=* plan*` and ask for `terraform -chdir=*azure* plan*`
  (the AWS pair's twin, added at the tip before GA3); whether an ask outranks an
  allow in the settings is not verified, so the hook's ask is the one that was
  measured (26 rows). That entry does not match `tofu -chdir=*azure* plan*`,
  `-chdir=$MOD`, a path to the binary or `-chdir azure` with a space: those
  match no settings rule and rely on the hook, which asks only when it sees the
  module's name;
- the cloud CLI's `provider register` and `provider unregister`, `aks
  get-credentials` and `aks command invoke`, `acr login`, `postgres
  flexible-server execute` and `connect`, `login`, `logout` and `account set`,
  and `kubelogin`, where the sub-command stands right after `az` (a flag without
  a value may stand between), because `login` is also a value (`--auth-mode
  login`); `--help` passes. A part that starts with a printing command (`echo`,
  `printf`, `rg`, `grep`, `cat`, `git` and the like, behind `sudo`, `time`,
  `nohup`, `command` or `exec`) and holds no substitution is a message or a
  search and is not read, so `rg -n 'az login' docs` passes;
- `make identity-passwords` and `infra/kind/identity.sh passwords`, which print
  the test users' passwords of the local sign-in issuer into the transcript: the
  Grafana password's ask, beside it, and the settings ask for the target too
  (GA3; the first form came with the tip before). The command is read
  with prose blanked, so a commit message or a pull request body that names the
  target passes. A shell's body in quotes (`bash -c '…'`, `ssh host '…'`, `tmux
  new '…'`), a quoted target or sub-command (`make "identity-passwords"`,
  `identity.sh "passwords"`) and the script by its name without a path (`cd
  infra/kind && bash identity.sh passwords`) ask; so does a pseudo-terminal word
  (`script`, `unbuffer`, `expect`, `socat`, `setsid`, `pty`) beside the target's
  or the script's name, because a pseudo-terminal passes the script's own check
  for a terminal and needs no `MERIDIAN_IDENTITY_SHOW=1`. A part that starts
  with a printing command or `gh` and holds no substitution is not read in
  those quoted and bare forms, so `rg "identity.sh passwords" docs` passes. Not
  read: the sub-command in a variable or split by empty quotes
  (`identity.sh $A`, `pass""words`), as the Grafana rule does not read them.

Passes unasked: `make azure-platform-validate` and `azure-platform-scan` (the
module's two free doors), `terraform -chdir=infra/terraform/foundation …` as
before, `ls`, `stat`, `test`, `wc`, `file`, `sha256sum` and `chmod` of the
closed files, and `rm` of the local file or of the plan, on purpose: removing a
door closes it.

Not seen, once, for this family (nothing below is built; each is a way past a
guard for habits; the answers are the tip's, measured on 2026-10-08, and where
the AWS twin on main answers the same shape it is said):

- The wrapper's target in a variable, a loop or a brace, a quote in the middle
  of a word, a name read across a pipe by `xargs`, a copied or linked script and
  `MAKEFLAGS`: `T=azure-platform-destroy; make $T`, `for t in destroy; do make
  azure-platform-$t; done`, `make azure-platform-{destroy,}`, `make
  azure-pl''atform-destroy`, `echo azure-platform-destroy | xargs make`,
  `ln -s infra/terraform/azure.sh /tmp/a && /tmp/a destroy`, `cp
  infra/terraform/azure.sh /tmp/a.sh && bash /tmp/a.sh destroy`,
  `MAKEFLAGS="-- azure-platform-destroy" make`, `infra/terraform/azure.sh $CMD`;
  also the build tool in quotes (`"make" azure-platform-destroy`) and found by
  `which` (`$(which make) azure-platform-destroy`). All none, and none for the
  AWS twin too.
- A runner that is on no list. The removal behind it is read by the AWS twin
  (deny) and not here (none, measured): `chrt -f 1`, `systemd-run --user`,
  `strace -f`, `numactl -N 0`, `busybox`, `bwrap --bind / /`, `docker run --rm
  img`, `chronic`, `setpriv --reuid=1`, `runuser -u x`, a pipe into `at now`, `cron`,
  `builtin eval`, `env -S'make azure-platform-destroy'` (the split string
  joined to the flag) and a path before a prefix other than `env`
  (`/usr/bin/sudo make azure-platform-destroy`, `/usr/bin/time make
  azure-platform-destroy`). The list is closed on purpose:
  the twin's rule, which reads the loose form with no runner word, flipped four
  prose rows, and each word added is a word that a message may hold.
- A pseudo-terminal made by `ssh -tt`, `tmux` or `screen` is not on the word
  list that denies a pseudo-terminal around the names (the AWS twin's:
  `script`, `unbuffer`, `expect`, `socat`, `setsid`, `pty`); behind them the
  removal is denied and the apply and the plan ask since GA3. `make --trace` and
  `make -d` ask by the target and are not denied.
- A reader or a writer the AWS twin's list does not name, so all none and all
  none for the twin: a redirect from the file (`< local.env-azure cat`,
  `$(<local.env-azure)`), an editor or viewer (`vim`, `view`, `code`), `gzip
  -c`, `zcat`, `tr`, `paste`, `column`, `rev`, `docker run --env-file
  local.env-azure alpine env`, `bash local.env-azure`, a recursive search of the
  parent folder (`grep -r . infra/terraform`, `rg -uuu x infra/terraform`), a
  `cd` into `~/.azure` followed by a reader of a bare name (`cd ~/.azure && cat
  config`), `find ~/.azure -exec cat {} \;`, a glob or a variable that builds
  the path, and `python3` with `pathlib`. `rm` is left on purpose. Cloud CLI
  commands the rules do not name: `az extension add`, `az account clear`, `az
  configure --defaults` (none; the cloud CLI has no twin).
- A project folder named `.azure` (the Azure Developer CLI makes one) is read as
  the CLI's folder, and so is a filter such as `jq .azure x.json` (the AWS
  twin's `jq .aws x.json` is denied the same way; `jq .azure.subscription
  x.json` passes). A search for the module's working folder is denied too
  (`cd infra/terraform/azure && rg -n ".terraform" .`, and `cat
  .terraform/environment` after the `cd`). An escaped dot does NOT help after
  the `cd` (`rg -n "\.terraform" .` is denied there too, measured in the
  review's re-check); what passes is a bracket for the dot (`rg -n
  "[.]terraform" .`) or the search from outside the module with a path and no
  `cd` (`rg -n "\.terraform" infra/terraform/azure`).
- A message or a comment body given with `-b` (`gh pr comment -b`) is not
  blanked, so one that names a reader and a closed file, or the removal beside
  a runner word, is denied, as for the AWS twin. A body for `git commit -F -` or
  `gh … --body-file -` in a heredoc is read as the command it is part of: a line
  that names a target at its start is denied (the removal) or asked (the apply
  and the plan), as the twin does; write the file with the editor tool and pass
  its path.
- GA1's scanner reads the raw command and its name rules need `python3`: with
  none, a command that holds a name asks (above), and a name rule that needs a
  pure-bash reading of the plain form is not built.
- Parked until the wrapper is built (the wrapper's step may change the names;
  the measured commands and the fixes are in the step's section "Guard rules to
  add when the wrapper is built"): a flag with a value between `az` and the
  sub-command (`az --subscription x login`, `az -o none storage blob upload -c
  tfstate …`); a credential handed out for the state or the deletion of its
  container (`az storage container generate-sas`, `az storage account
  show-connection-string`, `az storage account keys renew`, `az ad app
  credential reset`, `az storage container delete -n tfstate` asks by the
  generic rule only); `az` through `python3` or a container, and the state blob
  read or written by another tool (`azcopy`, `curl` on the blob's address, the
  storage SDK) or named only by a variable.
- A subdirectory of the module as a root (`-chdir=infra/terraform/azure/x`): it
  is another root, and the older rows of the case file ask about one.
- The harness directory is the module only when the call's `cwd` says so;
  whether it follows a `cd` of an earlier call is not verified.

Timeouts. A hook that runs past its timeout does not block the call, so
the guard cannot be allowed to run long. The bounds below do not prevent
that for every shape (the first ones were measured on one-word segments
only); the watchdog does. The hook arms it before it reads the command: at
5 of the 10 seconds it answers `ask`, saying that the guard ran out of time
and the command was NOT read. It cannot interrupt a command in flight, one
regex match: the slowest single match at 8192 bytes took 0.23 to 0.42 s
on 2026-10-06 (0.23 to 0.27 s at a load average of 3 and of 18, 0.25 to
0.42 s in the third review; about 1 s at 16384), and 1.5 s at the slowdown
a loaded machine showed, inside the margin of 5 s. One shape was found
slower in the last round and closed there: a tool's name repeated about
2000 times before a verb it reads (`aws aws … secretsmanager
get-secret-value`) took 17 to 24 s of CPU, in the cut of the text after the
match, not in the match; a match over 256 bytes now counts as not helped
and the same inputs take 0.4 s or less.
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
  a file is not read; one fed to a shell, to an interpreter the hook
  lists (`bash`, `sh`, `ksh`, `python`, `node`, `perl`, `ruby`, `awk`,
  `ssh`, `env`, `xargs`, `eval` and a few more), to `. file` or `source`
  is, and so is the body of an unquoted delimiter that holds `$(` or a
  backtick. A heredoc whose delimiter the hook cannot read whole
  (`END-OF-DATA`, `EOF.txt`) opens no body, so what follows it is read.
  `$(cat <<'EOF' … )` used as a command passes though its body runs.
- A heredoc piped into an interpreter that is not on that list: `gawk`,
  `nodejs` and `deno` are three of them (`bun`, `Rscript` and `make -f -`
  are others). Its body is taken for a file write and dropped.
- Quote kinds mixed in the head of a heredoc: whether a `<<` is inside a
  quote is decided by counting quote characters before it, which is a
  parity and not a parse. `cat '"' "<<EOF"` and `echo '"'; cat "<<EOF"`
  fool it, and the body that follows is dropped as a file write.
- A hook file overwritten by a redirect or a heredoc
  (`cat <<'EOF' >.git/hooks/pre-commit`): `rm`, `mv`, `chmod` and
  `truncate` of those paths are denied, writing one is not.
- A secret-shaped variable whose name matches nothing in the list above
  (`REDIS_PW`, `PW`, `LANGFUSE_SK`, `GITHUB_PAT`): `printenv NAME` passes.
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
  on `.claude/hooks/guard-bash.sh` and `.claude/settings.json` (`.env`,
  `*.tfvars` and, since S036, the AWS wrapper's closed files and the
  home-directory files that steer Terraform, git and the aws CLI are
  denied, these two are not), so a session can change or switch off the
  guard. A deny rule on them is the owner's decision; it is
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
