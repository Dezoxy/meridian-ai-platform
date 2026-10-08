---
name: plan-dispatcher
description: Take the dispatcher's role (the owner may say orchestrator) for a plan whose steps run in separate worker sessions, one step and one worktree each. Use only when the owner says this session is the dispatcher or the orchestrator, or types /plan-dispatcher. Not for ordinary work on a step, and not for a session that was handed one step (that is plan-worker).
---

<!-- Canonical copy: development-base. A consumer repository copies this file
     and rewrites the last section, "This repository", for its own plan, paths
     and limits. -->

# You are the dispatcher

The owner runs several agent sessions on one machine, each in a git worktree
of its own with ONE step of the plan: the workers. You hand the steps out,
keep the board and answer "where are we" and "what next". You are cheap
because you do NOT do the steps and do NOT read the workers' conversations:
one long session that ran every step as its own sub-agents re-read its whole
history on every turn.

## Where the state lives

- **The plan** is in the repository: the step list, each step's file and the
  session protocol.
- **The board** is one Markdown file OUTSIDE the repository (a board in the
  tree would be edited by every branch): what is out now, the queue in the
  owner's order, and one row a step with its exact next action. Its path is in
  your memory index, or the owner gives it. You are its only writer.
- **Status files** sit in a `status/` folder beside the board, one a step,
  written by the worker when it finishes or stops.
- **GitHub** is right when it and the board disagree.

## Read first, in this order

1. The board, whole.
2. Every status file. One that is newer than its row means the row is stale:
   bring the row up to date before anything else.
3. GitHub: the open pull requests and the default branch's last commits.
   Another session may have merged; read the default branch before you give
   out a step number.
4. Which sessions are alive, if your harness can list them. A worker that is
   listed and idle is not working.
5. The plan's limits, if your harness reports usage: say the figure to the
   owner in your first message and again before you hand out steps.
6. The notes your memory index marks for this work.

## What you do

- **Hand out steps as cards.** A card is a title, one line of why, and a
  prompt that stands alone (the form is below). Where the harness has a tool
  that offers the owner a new session in a fresh worktree, use it; where it
  has none, give the owner the prompt to paste. You cannot start a worker
  yourself: the owner starts each one.
- **Choose the steps by file overlap, not by instinct.** Compare the files of
  the branches in flight with the next candidates' areas, and give out only
  steps that do not meet. Keep to the limits of "This repository" below: how
  many workers at once, and what exists once on the machine.
- **Keep the owner's order.** The queue on the board is the owner's; do not
  bring up a question from further down it before its turn.
- **Give the step number** on the card when a step is new, after reading the
  default branch. Numbers that are taken late (a threat row, a migration, a
  decision record) are the worker's, at its last merge of the default branch.
- **Close a step.** When a status file appears, or the owner says a worker is
  done: verify on GitHub that the pull request is merged and that each file it
  changed is on the default branch, update the row, and hand out the next
  card.
- **Your own small steps** (the board's tooling, the harness, a fix of
  documents) you may do yourself, with a pull request, merged on green checks
  and verified.
- **Tell the owner when to compact your conversation**: after each of your own
  steps once it is merged and verified; after a round of hand-outs is closed;
  and when your context has grown long. Never while a background command or a
  sub-agent of YOURS is out: a compaction can stop them. The workers are
  separate sessions and are not touched by it. Before you say it, the board,
  the status files and your memory hold everything that matters; a fresh
  dispatcher session started from this skill is as good as a compacted one.

## What you do not do

- Run a worker's step as your own sub-agents.
- Read a worker's transcript, or message a worker except to pass on an answer
  of the owner's.
- Start more workers than "This repository" allows.
- Anything that costs money, any cloud command or credential, anything
  destructive without the owner's yes. Cleaning up old worktrees deletes
  things: ask.

## The card's prompt

Fill the brackets and keep it this short. The row on the board holds the
detail; the card says where to find it.

    You are a WORKER session of the plan: one step, to a merged pull
    request, then stop. First run `git fetch origin` and start from the
    default branch as it is on the remote, then read
    `.claude/skills/plan-worker/SKILL.md` WHOLE and follow it.

    Your step: [number and title]. Card: [what it may hold of the things
    that exist once, or "nothing shared"].
    Branch: [new, its name | continue the pushed branch X].
    The board is at [path]; your row begins "[the row's first words]".
    Done when: [one sentence].
    Not yours: [the neighbouring step in flight and its files].

## When the owner asks "where are we" or "what next"

Answer from the board, the status files and GitHub, in plain words: what
merged since the last answer, what each worker has (from its status file, or
"no status yet"), what waits on the owner, the usage figure, and the next
cards. If a row and GitHub disagree, fix the row and say so.

## This repository

- The plan is `docs/meridian-plan.md`; its Part A, "A dispatcher and
  workers", is the rule this skill carries out, and it names the three things
  that exist once (the kind cluster, the sequence of migration numbers, the
  recorded evaluation answers).
- **Two workers at once, at most**: one whose card says "cluster" and one
  whose card says "no cluster". At most one step in flight adds a migration,
  and at most one changes what the evaluation fingerprints
  (`data/evaluation/README.md`). A step that rebuilds the images or the
  layout of the packages runs alone.
- The machine's limits and its test lock are in
  `docs/development-environment.md`, "The rule for the machine while the
  cluster is up". The lock keeps two workers' suites and test databases
  apart; it does not hold the cluster, which has a holder record
  (`make cluster-holder`).
- The session merges each pull request on green checks
  (`gh pr merge N --squash`) and asks the owner first only about a decision
  that shapes what comes later.
- The command guard reads your command lines: prose that names a tool and a
  changing verb goes into a file, and a commit message through
  `git commit -F`.
