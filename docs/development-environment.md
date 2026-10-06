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
  `infra/kind/README.md` still lists the laptop's tools; S062, the step
  that ran these commands, adds the virtual machine's.
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
  that runs beside others passes 4. Beside the deployed cluster the
  suite took 3 min 10 s with 4 workers, with 5.5 GB still available.
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
  unattended session waited at that question. It now asks only before a
  `kubectl delete` and before a mutating call that names no cluster;
  `--kubeconfig infra/kind/kubeconfig` or `--context kind-<name>` on
  every `kubectl` of the command answers that second question. Deleting
  the kind cluster no longer asks either: on 2026-10-06 the owner allowed
  the session to delete it and make it again when a test needs it, and
  the session says so when it does (`CLAUDE.md`, hard rule 8). What still
  asks is what costs money, leaves the machine or cannot be made again
  from the repository: Azure, Terraform, a Helm uninstall, an image push,
  a release.
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
| `make smoke` | 40 to 43 s | |
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
- **Each test run has a database of its own**: `PYTEST_DB_CONTAINER` and
  `PYTEST_DB_PORT` per step, and per implementer when several of one
  step run at once. `make pytest-db` removes the container of its name
  when it starts, so two runs under one name destroy each other.
- **Named test files during a contract, the whole suite once per step.**
  A contract's own files take seconds; the whole suite is the main
  session's, alone, at the end.
- **Ten workers alone, four beside others.** The whole suite alone uses
  the default of ten. A run beside the cluster or beside other steps
  passes `PYTEST_WORKERS=4`: three runs of ten would be thirty processes
  on twelve cores.
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

- One whole suite at a time beside the cluster, with four workers.
- Nothing that an agent builds is committed unread: the reading is slower
  than the building and found a wrong design twice in two days (benign
  cases placed where they had to fail; a fingerprint that left out the
  code it was for).
- An unattended stretch runs in the Remote Control service in tmux.
- The cluster is deleted on the owner's word only. It frees about 3.3 GiB
  when the steps that need it are done.

A test database is a copy of one migrated template since S065, not a
database built from every migration. Measured on 2026-10-06 beside the
deployed cluster, the whole suite: 3 min 00 s before and 2 min 11 s after
with 4 workers, and 1 min 52 s after with 10, which is what the suite took
alone before. Still to measure: what it saves in CI.
