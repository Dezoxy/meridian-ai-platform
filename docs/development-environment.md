# Development environment

Where the work on this repository runs, what that machine needs, and what
has to be carried to a new one. Written on 2026-10-05, when the work moved
from a laptop to a dedicated virtual machine. Nothing here names a host, an
address or an account: the repository is public.

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
virtual machine in between, which is most of the gain. The numbers below
are an estimate from the measurements above, not a tested minimum:

| Resource | Suggested | Why |
|---|---|---|
| Processor | 8 cores of its own, 12 if two steps run side by side | The suite uses 3 or 4 workers and PostgreSQL beside them, and the cluster's own processes filled the rest of 8 cores |
| Memory | 24 to 32 GB | The cluster held 3.9 GiB; Docker's 8 GiB and a 16 GB host were not enough for it, a suite and the sessions together |
| Disk | 80 GB or more | About 30 images on the first `make up`, the image `make deploy` builds on every change, and one checkout per parallel step |

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

Things to check on the first day, because the laptop never showed them:

- **Processor architecture.** The laptop is arm64. The test database's
  image is published for amd64 and arm64 (checked). The other pinned
  images are pinned by digest: confirm each resolves on the new machine
  with the first `make up` and `make deploy`, and record the result in
  the versions table of `infra/kind/README.md`.
- **kind on Linux.** The cluster's file-watch limits are the host's;
  read kind's known issues if a pod fails to start with "too many open
  files".
- **The edge port.** `make smoke` and `make demo` reach the cluster at
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
| Unmerged work | Branches on GitHub | Listed in the plan under "In flight"; nothing is only local |

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

## Rules that came out of the move

- **One whole suite at a time on a machine**, and none beside a deployed
  cluster unless the machine was measured to carry both.
- **A step that owns the cluster needs a person within reach**: the
  command guard asks before every change to it, and an unattended
  session waits at that question.
- **Push a step's branch at the end of a working day**, finished or not,
  with its section of the plan filled in. Work that exists on one
  machine is one disk away from lost.
