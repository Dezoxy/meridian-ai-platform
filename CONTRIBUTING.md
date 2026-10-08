# Contributing to Meridian AI Platform

Meridian is a portfolio project with one owner,
[@Dezoxy](https://github.com/Dezoxy), who reviews and merges every change.
This file says how a change gets from an idea to `main`. It is short on
purpose: the rules live in the files it links, and it does not repeat them.

## Before you start

- **A small fix** (a typo, a broken link, a command that fails as written):
  open a pull request.
- **Anything larger:** open an issue first and say what you want to change
  and why. The roadmap is the step list in
  [docs/meridian-plan.md](docs/meridian-plan.md), Part B. A step marked
  `doing` is taken; a `todo` step whose dependencies are `done` is free.
  The owner answers whether the change fits and which step it belongs to,
  and assigns the issue to you. Work that starts without that answer may
  be declined.
- **A vulnerability:** never an issue. [SECURITY.md](SECURITY.md) says how
  to report it privately.

## Set up

You need `git`, `make` and [`uv`](https://docs.astral.sh/uv/), which
installs Python 3.13 and the project. Docker is needed for the
architecture model, the diagrams and the database tests; `gitleaks` for the
secret scan. The cluster tools (`kind`, `kubectl`, `helm`) are needed only
for a change under `infra/`.
[docs/development-environment.md](docs/development-environment.md) has the
full list and what the machine should have.

```bash
git clone https://github.com/<you>/meridian-ai-platform && cd meridian-ai-platform
uv sync
make docs && make test && make lint      # no Docker needed
```

Nothing a contributor needs costs money. The targets that call a live model
or a cloud account (`make eval-record`, `make gateway-live`, and every
`azure-`, `aws-` and `gcp-` target that plans or applies) run in the
owner's accounts only; leave them alone.

## Rules a change must keep

[CLAUDE.md](CLAUDE.md) holds the hard rules and the conventions, for people
and for coding assistants alike (`AGENTS.md` is its byte-identical twin).
Read "Hard rules" before the first edit. The ones that most often turn a
pull request back:

- No secret, token or real personal data in any file. Data is synthetic,
  from the generator under `data/synthetic/`.
- Every model call goes through the Model Gateway, and nothing under
  `src/meridian/platform/` imports an agent framework.
  [The import rules](docs/architecture/code/import-layering.md) show the
  layers; `make lint` enforces them.
- A document says of each capability whether it is implemented, simulated
  or designed.
- One concern per pull request. No refactor, rename or reformat beside it.

Some folders have rules a diff cannot show (the registry, the evaluation
set, migrations, the kind scripts). "Read before you touch" in `CLAUDE.md`
names the README to read for each.

## Make the change

1. Branch from `main` in your fork. Never base a branch on another open
   pull request.
2. Write the test first and see it fail, then the change.
3. Fix the documents the change made untrue, in the same pull request.
   Prose wraps at 80 columns.
4. Run the gates that fit what you touched:

   ```bash
   make docs                          # always
   make test && make lint             # always for code or scripts
   uv run pytest tests/meridian/<area> -n 4   # the folders you changed
   make check                         # the model or a decision record (Docker)
   make mermaid                       # a view or a Mermaid block (Docker)
   make secret-scan                   # before every push (gitleaks)
   ```

   The whole test suite runs in CI, not on your machine.

## Open the pull request

- **Title:** what is true after the change, as one sentence. Pull requests
  are squash-merged, so the title becomes the commit on `main`.
- **Body:** fill in the [template](.github/pull_request_template.md). Under
  Evidence, say what you ran and what it printed. "Tests pass" with no
  output is not evidence, and a check you could not run is named as not
  run.
- **Checks:** five must be green before a merge: `docs consistency`,
  `architecture model`, `derived diagrams`, `secret scan` and `python`.
- **The plan's files:** if your change is a step, update its row and its
  file under `docs/plan/steps/`. The plan keeps no change log: the pull
  request's description is the record, and the squash commit carries it.
  A new step's number is the owner's to give; do not pick one yourself.

## Review and merge

The owner reviews every pull request
([CODEOWNERS](.github/CODEOWNERS)). Review weighs, in this order: the hard
rules, correctness and tests, security, and whether the documents still
tell the truth. Expect questions about evidence. The owner squash-merges;
the branch is deleted on merge.

## Working with a coding assistant

Much of this repository was written with one, and it ships the harness for
it: instructions, reviewers, skills and hooks under `.claude/`, `.agents/`
and `.codex/`. Use it if you like. Two things stay yours: you have read
what you submit, and the evidence in the pull request is what ran on your
machine, not what the assistant said would pass.

## Licence

The project is under the [Apache License 2.0](LICENSE). By opening a pull
request you agree that your contribution is under the same licence
(section 5 of the licence). Material copied from another project keeps its
own licence and gets a line in [NOTICE](NOTICE).
