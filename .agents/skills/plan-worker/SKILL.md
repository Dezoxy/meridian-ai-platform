---
name: plan-worker
description: Take a worker's role for a plan whose steps are handed out by a dispatcher session - one step, in this session's own worktree, to a merged pull request, then stop. Use only when the session's first message (a card) says it is a worker session of the plan, or the owner types /plan-worker. Not for a session that works through the plan on its own.
---

<!-- Canonical copy: development-base. A consumer repository copies this file
     and rewrites the last section, "This repository", for its own plan, paths
     and limits. -->

# You are a worker: one step, to a merged pull request, then stop

The owner runs several agent sessions on one machine, each in a git worktree
of its own, each with ONE step of the plan. Another session, the dispatcher,
hands the steps out and keeps the board. You do not talk to it unless you are
blocked on a decision that is the owner's; you leave it a status file.

## Read first, in this order

1. The instruction file of your worktree (`CLAUDE.md` or `AGENTS.md`) and the
   plan's session protocol.
2. Your step's file in the plan, and your step's row on the board. The card
   gives the board's path. Read the board; do NOT edit it: the dispatcher is
   its only writer.
3. The folder the row names for your step, if it names one: the contracts,
   reports and reviews of the work already done.
4. The plan's limits, if your harness reports usage: say the figure to the
   owner in your first message. Do not stop on a figure; the threshold is the
   owner's.

## What is yours and what is shared

1. **Your step, your branch, your worktree.** Branch from the default branch
   as it is on the remote, or continue the step's pushed branch when the card
   names one (merge the default branch into it first). Never stack on another
   open branch.
2. **Numbers.** The step's number is on your card. A number that is taken
   late (a threat row, a migration, a decision record) you take at your last
   merge of the default branch before the pull request; if the default branch
   took yours meanwhile, renumber.
3. **What exists once on the machine** is yours only when your card says so.
   If it does not, run no command against it at all, not even one that only
   reads.
4. **Tests go through the make targets that take the machine's test lock.**
   Two sessions then cannot run a suite or a test database at once: the
   second waits and says who holds the lock. Before a pull request run the
   tests of the areas you changed, not the whole suite; CI runs the whole
   suite.
5. **Scratch on disk**, in a folder named for your step, and remove what you
   made when you end.

## How the step runs

- As the plan's session protocol says: contracts to the implementer one
  after another, each in a worktree of its own; the repository's reviewers
  for what was built; the advisor at the protocol's fixed points. Write an
  open decision down before you ask about it.
- Nothing that costs money, no cloud command, no cloud credential: stop and
  tell the owner the cost first. The command guard asks or denies; do not
  work around it.
- A decision that shapes what comes later is the owner's: ask once, in plain
  words, with your recommendation, and go on with what does not depend on
  the answer.
- Open the pull request, merge it yourself on green checks, then fetch and
  compare each file you changed with the default branch.

## When you are done, or stopped

Write ONE status file, `status/<step>.md` beside the board: the date and time
(read the clock, do not guess it), what is merged (the pull request, the
default branch's commit), what is NOT done and why, the branches left and
their tips, what the next worker on this step must read, and anything the
dispatcher must tell the owner. Then tell the owner in one short message that
the step is done and the session can be archived.

If the dispatcher session is gone, you lose nothing: the board, your status
file and the pushed branches are enough for a new dispatcher or a new worker
to go on.

## This repository

- The protocol is Part A of `docs/meridian-plan.md` ("A dispatcher and
  workers", the contract's form, "Before pushing", "The pull request's
  description"); your step's file sits in the folder of its twenty
  numbers, `docs/plan/steps/S100-S119/S100.md`. There is no change log:
  the pull request's description is the record. A follow-up is a row of
  `docs/plan/backlog.md`, and a row you close moves to
  `docs/plan/backlog-closed.md`.
- The card says "cluster" or "no cluster". With "cluster" the kind cluster is
  yours: check its holder first (`make cluster-holder`), run its commands from
  the checkout the row names, and say when you delete or recreate it. With "no
  cluster", no `kubectl`, `helm` or `make up`, `deploy`, `smoke`, `demo`,
  `demo-seed`, `down`.
- The locked targets are `make pytest`, `make pytest-db` and `make alerts`
  (`make eval` through `pytest-db`); pass `PYTEST_WORKERS=4` and the paths in
  `PYTEST_ARGS`. One test FILE by hand (`uv run pytest <file> -n 4`) is fine
  without the lock. Before a pull request: those tests, `make lint`,
  `make docs`, `make test` and `make secret-scan`; never a whole local suite
  (`docs/development-environment.md`, "What runs before a pull request").
- Commit messages through a file (`git commit -F`); push only inside an `if`
  on the gates' exit statuses; `gh pr merge N --squash`.
- Scratch goes under `~/.cache/`, never under `/tmp`: on the development
  machine `/tmp` is memory.
