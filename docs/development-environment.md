# Development environment

Where the work on this repository runs, what that machine needs, and what
has to be carried to a new one. Written on 2026-10-05, when the work moved
from a laptop to a dedicated virtual machine, and brought up to date on
2026-10-06 with what that machine showed in its first two days. Nothing
here names a host, an address or an account: the repository is public.

## Why the work moved

The laptop this was built on has 8 cores and 16 GB. Docker Desktop runs
everything in one virtual machine of 8 GiB: the kind cluster and the
throwaway PostgreSQL the test suite uses share it. Measured on 2026-10-05,
the same whole suite (`GITHUB_ACTIONS=true make pytest-db`, about 8,900
tests, most with a database of their own):

| What else ran | Load average | Whole suite |
|---|---|---|
| One other session's tests | about 15 | 9 min 17 s |
| Two other sessions' tests | about 30 | 16 min 26 s |
| The cluster freshly deployed, a backup and indexing on the host | 60 to 130 | stopped at 47% after 35 min |

In the last row Docker's virtual machine was 1% idle, the cluster's
container was its largest consumer by far (3.9 GiB of memory; the test
database held 0.2 GiB) and the host was swapping. A gateway test that
passes alone failed in each run at that load (the plan's backlog has
it). The cluster was not broken: about a hundred processes shared the
machine the test database needed.

## What the machine needs

A Linux machine with Docker Engine runs the containers directly, with no
virtual machine in between, which is most of the gain. The second column
was the estimate made on the laptop before the move; the third is what
the machine the work moved to has, and it carries the work ("Working
fast on the virtual machine", below, has the measurements):

| Resource | Estimated before the move | The virtual machine, measured 2026-10-06 | Why |
|---|---|---|---|
| Processor | 8 cores of its own, 12 if two steps run side by side | 12 cores; the load stood at 1 with the cluster up and seven agents at work, and near 8 during a whole suite with ten workers | The suite's workers and PostgreSQL beside them, and the cluster's own processes |
| Memory | 24 to 32 GB | 11 GB, fixed. Enough: the cluster's node holds 3.3 GiB, and with six services deployed, five agents at work and one suite of four workers 5.5 GB stayed available | The estimate came from Docker Desktop's 8 GiB virtual machine, where the cluster and the test database shared one small memory; on Linux they share the host's |
| Disk | 80 GB or more | 123 GB, 47 GB used with the cluster's images and seven checkouts | About 30 images on the first `make up`, the image `make deploy` builds on every change, and one checkout per step and per implementer |

`infra/kind/README.md` says to give Docker at least 6 GiB for the cluster
alone; that is the floor for a demo, not for the suite beside it.

Tools, with the versions the cluster was tested with in
[`infra/kind/README.md`](../infra/kind/README.md) (Prerequisites) and the
pins in `infra/kind/pins.env`, the `Makefile` and the workflows:

- `git`, `gh` (signed in), `make`, `curl`, `jq`, `openssl`;
- `uv`, which installs Python 3.13 and the project (`uv sync`);
- Docker Engine, `kind`, `kubectl`, `helm`;
- `gitleaks` (the secret scan before a push) and `shellcheck`;
- Node.js, for the edit and session hooks under `.claude/hooks/node/` and
  the MCP server in `.mcp.json`;
- `terraform` and `az` only for the Azure steps (S007, S020).

Three things the laptop never showed, and what the virtual machine
answered on 2026-10-06:

- **Processor architecture.** The laptop is arm64, the virtual machine
  amd64. Every image pinned by digest resolved there: `make up` from no
  cluster and the first `make deploy` both ended with exit 0, and
  `make smoke` passed every line. The versions table of
  [`infra/kind/README.md`](../infra/kind/README.md#prerequisites) holds
  the virtual machine's tools beside the laptop's, as the result of S062,
  the step that ran these commands.
- **kind on Linux.** The cluster's file-watch limits are the host's;
  no pod failed to start for them. Read kind's known issues if one does
  with "too many open files".
- **The edge port.** `make smoke` and `make demo` reached the cluster at
  `127.0.0.1:8088` on the machine itself.

## What git carries and what it does not

Everything needed to build, test and deploy is in the repository. A fresh
clone plus the tools above is a working environment. What is not in git:

| Thing | Where | What to do |
|---|---|---|
| Private notes | `.context/` (ignored) | Copy by hand; never commit, never quote in a tracked file |
| Azure state settings | `infra/terraform/local.env` (ignored) | What `make azure-state` wrote. Copy by hand, or let that target write it again when S020 opens; never into a tracked file |
| Local permission answers | `.claude/settings.local.json` (ignored) | Leave behind; the assistant asks again |
| The kind cluster | Docker, on the old machine | Do not move. `make up`, then `make deploy`, rebuild it from the charts; `make smoke` and `make demo` prove it. The owner runs `make down` on the old machine |
| Cluster credentials | `infra/kind/kubeconfig` (ignored) | `make up` writes a new one |
| Python environment | `.venv/` | `uv sync` |
| Rendered architecture | `docs/architecture/generated/`, `workspace.json` | `make check`, `make export` when needed |
| Sign-ins | `gh`, `az`, Docker | Sign in again on the new machine; nothing to copy |
| The assistant's memory | `~/.claude/projects/<name from the checkout's path>/memory/` | Copy the folder to the name the new path gives, or the next session starts without it |
| The assistant's saved sessions | `~/.claude/session-data/` | Optional; `/resume-session` reads the latest |
| Unmerged work | Branches on GitHub | Each open step's branch is pushed at the end of a day, and its section in the plan's Part C says what is left; nothing is only local |

After a fresh clone open `/hooks` once, as `CLAUDE.md` says, or the edit
and command hooks do not run.

## First run on a new machine

```bash
git clone <this repository> && cd meridian-ai-platform
uv sync
make docs && make test && make lint          # no Docker needed
GITHUB_ACTIONS=true make pytest-db           # the whole suite; needs Docker
make up && make deploy && make smoke         # the cluster, from nothing
make demo                                    # one claim, end to end
```

Record how long the whole suite takes alone and beside the cluster; the
plan's Part A rests its rule on a suite beside the cluster on that number.
For the virtual machine both are recorded below.

## Rules that came out of the move

- **One whole suite at a time on a machine**, and none beside a deployed
  cluster unless the machine was measured to carry both. Measured on the
  virtual machine on 2026-10-05, alone: the whole suite takes 2 min 42 s
  with 4 workers (the default until then), 2 min 03 s with 8 and
  1 min 55 s with 10. Ten is the Makefile's default since 2026-10-06,
  the owner's decision; CI sets 4 for its four-core runner, and a step
  that runs beside others passes 4 (an implementer 3, and the session's own
  suite 6, while the cluster is up: the plan's Part A). Beside the deployed
  cluster the suite took 3 min 10 s with 4 workers, with 5.5 GB still
  available.
- **An unattended session runs in the Remote Control service on the
  machine, in tmux, not in a desktop session over SSH.** On the night of
  2026-10-05 a desktop session stood still for six hours while its
  client was away: a subagent it had called was only started when the
  owner came back.
- **A pull request is merged by hand once its checks are green.**
  GitHub's auto-merge was set on two pull requests that night and fired
  on neither.
- **A step that owns the cluster needed a person within reach on the
  laptop**: the command guard asked before every change to it, and an
  unattended session waited at that question. It now asks before a
  `kubectl delete`, before `kubectl apply --prune` or `--force`, and
  before a `kubectl apply`, `scale` or `rollout restart` that does not
  name the local cluster (another context or kubeconfig asks too); the
  other kubectl verbs never asked. A call names the local cluster with
  `--kubeconfig infra/kind/kubeconfig` (given as a path, or as a variable
  the same command assigned that path) or `--context kind-<name>`, on
  every `kubectl` of the command; where the command mentions `KUBECONFIG`
  only `--kubeconfig` counts, and a command over 8192 characters asks
  without being read. Deleting the kind cluster no longer asks either, for
  `make down`, `infra/kind/down.sh` and `kind delete cluster --name
  meridian`; a bare `kind delete` or one that names another cluster still
  asks. On 2026-10-06 the owner allowed the session to delete the local
  cluster and make it again when a test needs it, and the session says so
  when it does (`CLAUDE.md`, hard rule 8). What still asks is what costs
  money, leaves the machine or cannot be made again from the repository:
  Azure, Terraform, a Helm uninstall, an image push, a release, and a `gh
  pr merge --admin`, which merges past failing checks. Since S075 it
  also asks before `psql`, `pg_dump`, `pg_dumpall` or `pg_restore`
  through `kubectl exec`, `run` or `debug`, before `kubectl cnpg psql`
  and before `helm get manifest`, `values`, `hooks` or `all`, before
  `make gateway-upkeep` when it can change a tenant's budget ledger
  (`credit`, `close`, `expire --confirm`), and before the commands that
  print a credential, also inside `$(…)`, backticks and quotes; it denies
  the other ways to a Secret's value (the secret-rotation runbook lists
  them, and what the guard does not see: it is a guard for habits, and
  the session can edit the guard's own files, which is the owner's to
  decide). A known limit, older than this
  change: the hook has ten seconds, and with the machine loaded (a load
  average near 70) a command that carries a 70 KB heredoc, or one of
  4,000 segments, took it that long (1.3 s when idle), and Claude Code
  does not block a call whose hook ran out of time. So the hook arms a
  watchdog first: after 5 of its 10 seconds it answers `ask`, saying it
  ran out of time and did NOT read the command. The watchdog runs
  between commands and cannot stop one regex match in flight, so three
  bounds ask before the rules run: a command typed over 16384 bytes, a
  command whose text is over 8192 bytes once heredoc bodies written to a
  file are dropped (the slowest single match at 8192 bytes took 0.23 to
  0.42 s on 2026-10-06, on the shapes that cost most, at a load average
  of 3 to 18; it was about 1 s at 16384, which is why the bound is
  8192), and one of more than 1000 parts (split on newlines, `;`, `&&`,
  `||` and `|`; the cost follows the parts, 3 s of CPU for 8192 of them,
  0.25 s for 1000). The pass that drops heredoc bodies runs before the
  first two bounds and costs 0.04 s of CPU on its worst shape at 16384
  bytes (a heredoc marker followed by 16 KB of dots; it was 1.5 s until
  the second security pass), a test holds it under 0.5 s. A command of
  this repository's own has a handful of parts. Write a long script with
  the Write tool and run the file.
- **Push a step's branch at the end of a working day**, finished or not,
  with its section of the plan filled in. Work that exists on one
  machine is one disk away from lost.

## Working fast on the virtual machine

What was measured on 2026-10-05 and 2026-10-06 on the machine the work moved
to (12 cores, 11 GB of memory, which is fixed and will not grow), and what
follows from it. Everything here was seen on that machine; nothing is a
guess about another.

### What things cost

| What | Time | Note |
|---|---|---|
| The whole suite, alone, 4 workers | 2 min 42 s | 9,423 tests |
| The whole suite, alone, 8 workers | 2 min 03 s | |
| The whole suite, alone, 10 workers | 1 min 55 s | The `Makefile`'s default |
| The whole suite beside the deployed cluster, 4 workers | 3 min 10 s | 5.5 GB still available |
| `make up` from no cluster | 5 min 04 s | Every pinned image resolved on amd64 |
| The first `make deploy` | 1 min 30 s | 61 s of it waits out the ingestion's token window |
| `make smoke` | 40 to 52 s | Over 2026-10-06; 35 lines on the deployed cluster |
| `make demo` on a deployed cluster | 30 s | |
| The python job in CI | about 8 min | GitHub's four-core runner |
| One contract at an `implementer` | 3 to 15 min | Reading, tests first, the change, its gates |
| One review by a reviewer agent | 3 to 13 min | |

Memory with the cluster and six services deployed, five agents at work and
no suite running: 5.6 GB used, 5.7 GB available, about 2 GB of idle pages
in swap. The kind node holds 3.3 GiB of it.

### Where the time goes

The machine is not what a step waits for: with the cluster up and seven
agents at work the load stood at 1 of 12 cores. A step waits, in this
order, for the agents that write and review it, for the main session to
read each diff before it commits, and for CI. So the levers are how much
runs side by side and how little waits on something that is already
known.

### What makes it faster

- **Several steps at once, each in a worktree of its own** (the plan's
  Part A). Three steps ran side by side on 2026-10-06, with seven
  implementers at the peak.
- **A board of lanes, and a hook that reads it.** The session keeps
  `.claude/lanes.md` (not tracked): `Target: N` and a table `Lane | Step | Out
  now | Idle because | Next`. The `Stop` hook `check-lanes.sh` blocks the stop
  once, naming a lane with nothing out and no reason, or saying fewer than
  `Target` run (a lane with a reason does not count as running), or saying
  the board is not one it can read (a cell may not hold `|`). It reads that
  file only and cannot tell a true "out now" from a false one. It is Claude
  Code's only (Codex's hooks file has no `Stop` entry), it checks once per
  turn (a second stop in the same turn passes), and a session started in a
  fresh worktree has no board and so no check.
- **A hook that names files a shell command rewrote.** The edit gate and
  the lint, boundary and docs hooks see only what the Edit and Write tools
  change, and an implementer told to use them sometimes used `sed -i` or a
  heredoc instead. `check-shell-edits.sh`, an advisory `PostToolUse` hook
  on Bash, reads the changed-file list that Claude Code delivers when
  `bashEditDiffEnabled` is on and prints one line naming each tracked file
  the command rewrote, except `uv.lock` and what `ruff format` or
  `terraform fmt` formatted. The list is "best effort and in public beta".
  Claude Code 2.1.289 reads that setting from the user, flag and policy
  sources only: a trial showed that the repository's `.claude/settings.json`
  cannot turn it on, so the hook stays silent until `"bashEditDiffEnabled":
  true` stands in `~/.claude/settings.json` (or `claude --settings` passes
  it, or `CLAUDE_CODE_BASH_EDIT_DIFF=1` is set where Claude Code starts).
- **Several implementers inside one step when their files do not
  overlap.** Four contracts of one step ran at once, each on a branch cut
  from the step's branch; each finished contract was rebased onto the
  step's branch and fast-forwarded. Contracts that edit one file (the
  smoke script's checks) go one after another.
- **Every contract of a step written before the first one returns**, one
  concern each, with the migration numbers handed out in order. The next
  one then starts the moment the previous comes back.
- **An implementer gets a worktree of its own from the harness.** A
  subagent cannot write outside the session's worktree, and two that were
  sent to sibling worktrees stopped without writing. The main session
  reads the diff there, commits it and moves the commit with git; it
  copies no file.
- **Cluster commands run from a separate checkout** of the step's branch,
  with credentials exported into it by `kind export kubeconfig`, so
  `make deploy` never reads a script that an implementer is editing.
- **The cluster says who holds it** (S075). One step uses it at a time,
  and `make up`, `make deploy` and `make down` now read a record of the
  holder, in a ConfigMap in `kube-system`: a name, a short commit, a UTC
  time and a state. Another holder is named and the command stops before it
  changes anything; `TAKE_CLUSTER=1` in front of the same command takes the
  cluster (`TAKE_CLUSTER=1 make deploy`). `make up` and `make deploy` write
  the record when they start to change the cluster (state `changing`) and
  when they end well (`ok`), so a run that failed leaves `changing`, which
  stops another holder until someone has looked at what failed; the same
  holder may run again. `make cluster-holder` prints it. The holder
  is the checkout's branch. A session that runs from a detached checkout,
  as the main session does, sets `CLUSTER_HOLDER` (letters, digits, `.`,
  `_`, `/` and `-`, at most 100) to a name of its own, so that its commands
  and the cluster's record agree on who it is. It is a notice for an honest
  mistake, not a lock: two commands started in the same second both pass.
  `infra/kind/README.md` says which commands read the record and which do
  not.
- **Each test run has a database of its own**: `PYTEST_DB_CONTAINER` and
  `PYTEST_DB_PORT` per step, and per implementer when several of one
  step run at once. `make pytest-db` removes the container of its name
  when it starts, so two runs under one name destroy each other. It starts
  a throwaway Redis beside the database (S066, the gateway's shared rate
  windows), which needs a name and a port of its own the same way:
  `PYTEST_REDIS_CONTAINER` and `PYTEST_REDIS_PORT`, handed to the tests as
  `MERIDIAN_TEST_REDIS_URL`.
- **Named test files during a contract, the whole suite once per step.**
  A contract's own files take seconds; the whole suite is the main
  session's, alone, at the end.
- **Ten workers alone, four beside others.** The whole suite alone uses
  the default of ten. A run beside other steps passes `PYTEST_WORKERS=4`:
  three runs of ten would be thirty processes on twelve cores. While the
  cluster is up the session passes six and tells implementers three (a
  suite beside four implementers' test runs and a fresh cluster pushed the
  machine into swap twice on 2026-10-06), and before a whole suite it counts
  the test databases that are running and waits until at most one
  implementer is testing with a database.
- **Reviewers at once, and early.** A step's reviewers read the same
  commits and change nothing, so they run together, and they start when
  the last contract that changes product code is in, not when the last
  contract of test support is.
- **A pull request is merged by hand the moment its checks are green**,
  and the session does other work until then. Auto-merge never fired.
- **A fact-check before a documents step closes.** One reviewer read 54
  rows of two checklists against their evidence in nine minutes and found
  fifteen things; a person would have needed an afternoon.

### What does not help

- **More than ten workers.** The curve is flat after eight: a few long
  tests each hold one worker.
- **More cores.** They are idle now.
- **More memory.** It is not available, and the machine carries the
  cluster, the agents and one suite as it is.
- **A CI runner on this machine.** It would cut the eight minutes, and on
  a public repository it would let anybody's pull request run code here.
- **A faster mode for the session.** It is not offered for a desktop
  session.
- **Waiting.** A session that waits for a merge, for a review or for one
  agent while others could start is where the first night's six hours
  went, with a session that stood still while its client was away.

### What to keep to

- One whole suite at a time beside the cluster, with six workers, and an
  implementer's run beside it with three.
- A test database's port outside Linux's ephemeral range (32768 to
  60999). On 2026-10-06 a run on port 55638 failed to bind because another
  process had been given that port as a source port; this session's later
  runs used a port below the range.
- Nothing that an agent builds is committed unread: the reading is slower
  than the building and found a wrong design twice in two days (benign
  cases placed where they had to fail; a fingerprint that left out the
  code it was for).
- An unattended stretch runs in the Remote Control service in tmux.
- The session may delete the local cluster and make it again when a test
  needs it, and says so when it does; never to clear a fault nobody has
  looked at, because the cluster's database is the only copy of the audit
  log (hard rule 8; the owner, 2026-10-06). Deleting it frees about 3.3 GiB
  when the steps that need it are done.

A test database is a copy of one migrated template since S065, not a
database built from every migration. Measured on 2026-10-06 beside the
deployed cluster, the whole suite: 3 min 00 s before and 2 min 11 s after
with 4 workers, and 1 min 52 s after with 10, which is what the suite took
alone before. Still to measure: what it saves in CI.
