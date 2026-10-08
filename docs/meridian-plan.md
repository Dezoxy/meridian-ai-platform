# Meridian AI Platform — Plan

> **How to use this file:** this is the single living plan. Every step in
  Part B has an ID (`S001`…). When a step starts, add its file under
  `docs/plan/steps/`, in the folder of its twenty numbers (Part C), from
  the template, and fill it in as you go. The step tables hold no
  status: a step's status is the line under its file's heading, and
  Part F, at the end, lists the finished steps and the steps in flight
  from those lines (`make plan-progress` writes it).
  Nothing gets deleted; superseded decisions are struck through with a note.
  The one exception is the change log, which ended on 2026-10-08 and
  whose files left the tree by the owner's decision (Part E).
> **Architecture:** requirements, decisions, views and security live in
  [architecture/](architecture/README.md). This plan owns the step list, the
  session protocol, open questions, the step files in `docs/plan/steps/`
  and the follow-up backlog in `docs/plan/backlog.md` and
  `docs/plan/backlog-closed.md`.
> **Private context:** job targeting and owner notes live in the gitignored
  `.context/` folder. Never copy them into tracked files; this repository is
  public.

---

## Part A — How a session works

One step per branch and per worktree. Since 2026-10-08 the steps run in
worker sessions, one step each, handed out by a dispatcher session ("A
dispatcher and workers", below; up to five ran side by side in one
session before that). A session that grows long is compacted:
by the owner between steps, on the session's word, and by the harness
when its context fills (step 7 says how that is made safe): the advisor
re-reads the whole transcript on every call, uncached, and a long
context blurs what a step was for.

1. **Start small.** Read `CLAUDE.md`, the step table below and the detail
   section of the step you take. Read other files only when the step needs
   them; search for the symbol, then read the lines.
2. **One step, one branch.** Branch `sNNN-short-name` off `main`; never stack
   branches. If a step will not fit one session, split it here first.
3. **Open the step.** Add its file from Part C's template, in the folder
   of its twenty numbers (`docs/plan/steps/S100-S119/S100.md`), with the
   status `doing` and the day it starts, and run `make plan-progress`,
   which puts it under "In flight" in Part F ("A step's status", below).
   Take the step's open rows of `docs/plan/backlog.md` into its "done
   when".
4. **Plan, delegate, verify.** The main session (Opus) writes a short contract
   with paths, names and what not to touch (the form is below), delegates
   implementation to the `implementer` subagent (Sonnet at high effort), and
   consults the advisor where it has the best chance to change something (the
   owner, 2026-10-06: "when it has the bigest chance to make any sense"). The
   reasoning, with that day's count: the session had seventeen answered
   consultations and a record of what five of them changed; all five changed
   something, and the four at a design or at a surprise changed it before code
   was written, where a change is cheapest. Before a pull request the
   reviewers and the gates have already read the diff, so a consultation there
   is kept for what they did not see. Twelve of the seventeen left no record
   of their effect, which is why each one is recorded from now on.
   - **Always:** before the step's first contract goes out (the design); when
     a result contradicts the design or what was expected (a probe, a review
     that says the design is wrong, a gate that fails for a reason nobody
     predicted); before the session changes a step's scope or the order of its
     contracts on its own; and before it asks the owner for a yes to something
     that costs money or cannot be undone, so that the question put to the
     owner is the right one.
   - **Before a pull request, only when** something reached the branch that
     neither the advisor nor a reviewer has seen: a decision the session took
     alone after the last consultation, a part of the diff no reviewer read,
     or a security boundary (the list that calls for `feature-threat-model`)
     that changed after its review. Otherwise the reviewers and the gates are
     the check before the pull request.
   - **Not:** for a pull request of the plan or of documents alone, while
     contracts land that returned what was expected, or twice for one
     question.
   - **How:** the advisor reads the transcript and takes no question, and
     every call reads all of it again. So the session first writes the open
     decision and its alternatives into the step's design and says them in its
     message, and puts several open decisions into one call.
   - **Record:** the step's section says for each consultation when, at which
     of these points, and what it changed, or that it changed nothing (the
     template's Advisor line), so a step that skipped one shows it and the
     share of useful ones can be counted. On 2026-10-06 the session went
     eighty minutes without the advisor while five to seven agents returned
     results, designed two steps and closed a third in that time, and the call
     it then made found three things one of the designs had not settled (the
     owner, that day: "Why skipped?"). Nothing enforces the call but this
     record.
   It runs every gate itself and reads every changed file; a subagent's report
   is a claim, not evidence.
5. **Gates.** Always `make docs` and `make test`. `make check` when the model
   changed, `make mermaid` when views or Mermaid blocks changed, and the
   step's own "done when" criterion.
6. **Close.** Fill in the work log and verification, set `done` and the
   day it finished in the file's status line, run `make plan-progress`
   (the step moves to "Finished steps" in Part F, and nothing above
   Part F changes for a status), commit, go
   through "Before pushing" below, open the PR with a description that
   is the record ("The pull request's description", below), merge it with
   `gh pr merge --squash` as soon as every required check is green, and
   confirm the content landed on `main`. The session merges every pull
   request this way, by hand: `--auto` was set on pull requests 84 and 85
   and fired on neither, though every check was green and the state was
   clean, and the second waited six hours for the owner. It stops and
   asks the owner first, in chat, only for a decision that shapes what
   comes later: the design, a security boundary or an accepted risk, the
   cost, or the roadmap and the rules of this repository. The answer goes
   into the step's section, so a pull request carries no decision the owner
   has not taken; everything else the session decides and records. When
   the owner is away, a question does not stop the session: it is written
   into the step's section and the session takes what does not depend on
   it (the owner, 2026-10-05: "dont stop, at a promt or for a question,
   just put it away, note it and go on"). A follow-up that no step's "done when"
   covers goes into the follow-up backlog, a row at the end of the table
   in `docs/plan/backlog.md`, not only into the step's
   own section, and it names the step that will take it: one that exists,
   or a new row in "Backlog steps" added in the same pull request. A row
   is not left with the home `none` (the owner, 2026-10-06: every item
   belongs to a step, so that the step list is the whole of what is left).
   A row the step closes is not edited where it stands: it moves, whole,
   to the end of the table in `docs/plan/backlog-closed.md`, with a
   status that starts `closed by` and the step; a row closed in part
   stays and says what is left. `make docs` refuses a closed row in the
   first file, a row that is not closed in the second and an open row
   that names no step.
7. **Checkpoint.** At every close, and before a long stretch of work,
   nothing that matters is left in the conversation alone: the step's
   record is in its step file, every open branch is pushed, the contracts that
   are still to run are in a folder that outlives the session, and a
   short state note says what is merged, what is in flight on which
   branch and what comes next. The harness compacts the conversation by
   itself when the context is full, and after a checkpoint that loses
   nothing (the owner, 2026-10-06: "we should optimalise the conversation
   but it should be a routine"). On 2026-10-06 a compact by hand that ended
   interrupted stopped every background agent, the harness would not
   start them again without the owner's word, and the owner decided
   against compacting by hand that day. Since 2026-10-08 the owner
   compacts between steps ("after every step you should say, compact and
   we can go on"): the session says so when a step is merged and
   verified and this checkpoint is written, never in the middle of a
   step and never while an agent or a background command is out. A
   dispatcher says it at the three moments of "A dispatcher and
   workers"; a worker ends with its status file and is archived.

**The contract.** One concern per contract and about a page, in a scratch
file the `implementer` reads. A long contract gets worked around with
scripts, and the edit hooks never see those.

```text
# <step> contract <n>: <the one concern, in a sentence>
Worktree and branch. Do not commit, push, switch branches or stash.
## Why             the plan row or the finding this answers, quoted
## The change      numbered: paths, names, formats
## Tests first     the tests to add; see each fail before the change
## Do not touch    the paths and behaviours that stay as they are
## Edit-gate facts who imports the file, the public interface, the data
                   touched, the owner's instruction verbatim
## Gates you run   the commands (naming every directory the change reaches),
                   and which gates the main session keeps
## Report          per file what changed, the tests added, each gate's last
                   lines with its exit code, what the contract left open
```

**Before pushing.**

- `make secret-scan` (it runs
  `gitleaks git --log-opts="origin/main..HEAD" --redact`, and refuses a base
  git does not know, which gitleaks alone passes with nothing scanned). CI
  scans every commit of a pull request, so a finding in an early commit is
  not fixed by a later one.
- **Tests: the changed areas locally, the whole suite in CI** (the owner,
  2026-10-08, about 04:45 UTC, after "What does it mean full suite in your
  understanding?" and "Okay but ehy dont we just test those things that is
  modified?": "Affected tests, CI runs all (Recommended)"; offered beside it:
  keep the whole suite locally, and a mixed rule). It replaces ~~`GITHUB_ACTIONS=true
  make pytest-db` once, when the step changed Python~~ (a tool can behave
  differently when CI's variables are set; Typer's usage errors did, on pull
  request 27: that is one more thing CI's whole run is for).
  - **Locally, before a pull request:** the tests of the changed areas (the
    changed folders mapped to their test folders, `-n 4` at most; the
    import-contract tests always; after a merge of `main`, the tests its
    commits added), `make lint`, `make docs`, `make test` and
    `make secret-scan`. The cluster stays up.
  - **In CI, on the pull request:** the whole suite (the hosted runner took
    10 min 1 s to 14 min 29 s before coverage and 14 min 0 s with it, on
    2026-10-07, so about 15 minutes). A red run is fixed and pushed again;
    nothing merges red.
  - **Why, with the numbers:** the development machine has 11.4 GB; the kind
    cluster's node holds about 5 GB; the whole suite with coverage needs about
    5.5 GB at its peak (2026-10-08: 22,437 tests in 3 min 46 s with the
    cluster stopped); the machine froze twice, on 2026-10-07 and 08, and was
    rebooted once.
  - **A whole local run, when one is wanted by name,** is made with the
    cluster's node container stopped for its length (the owner, about 03:57
    UTC the same morning: "Stop cluster during suite (Recommended)"). The
    helper that does it lives outside the repository for now and is not a
    `make` target.
  - **The price, said plainly:** a test broken in a file the change did not
    touch is found in CI, about 15 minutes after the push, not before it
    (the same morning, two count pins in `test_alert_rules.py` were such
    tests).
- Read a gate's exit status, not its last lines. A gate piped into `tail`
  inside an `&&` chain hands on `tail`'s status, and a failing check passes:
  on 2026-10-06 a gate piped through another command before `&&` hid a
  failed `make docs` before a push.
- Read every source file a subagent changed. A green suite does not show a
  value that was hard-coded to match the one fixture.

**A step's status (S101).** It is the line under the heading of the
step's file (Part C's template has its form), and nowhere else. The
step tables of Part B hold no status: the owner, 2026-10-08, "it should
show us the plan itself without any modification so no todo and done
and other, and at the end it should order the done jobs that is linked
into the plan steps folder files", and, of the steps that are open,
"in-flight at the end of the plan would be good under the finished
job". So:

- **The words** are `todo`, `doing`, `done`, `blocked` and `dropped`.
  `done` carries the day the step finished; every other word has a dash
  there. A note may follow the line's third field, after ` · `.
- **A step is free** when it has no file, or a file that says `todo`,
  and every step it depends on is `done` by its own file. A step whose
  file says `doing` is taken.
- **Part F** ends with two tables, "Finished steps" and under it "In
  flight", each row linked to the step's file. `make plan-progress`
  writes them from the status lines, and `make docs` fails when they
  are not what the files say, when a status line has another form and
  when a step table gets a Status column again. Nobody types into them.
- **A conflict between Part F's two markers**, after a merge of `main`
  into a branch that opened or closed a step, is never resolved by
  hand: take either side whole, run `make plan-progress`, and `make
  docs` proves the result. The tables are sorted and hold no date of
  the run, so two branches produce the same lines from the same files.
- **What a step leaves open while it is `doing`** is said in its file
  ("Where it stands", "Left before it is done"), not in a table of the
  plan. The paragraph "Where the project stands" in Part F is written
  by hand, by the step that changes what exists.

**The pull request's description (S100).** It is the step's record
outside its own file. The repository squashes with the pull request's
title and description, so the description becomes the commit's message
on `main`, and `git log` is the change log. The plan had a change log of
its own until 2026-10-08, one entry a pull request; the owner asked "the
changelog makes sense here or the github owned prs enough?" and chose
"Stop and delete the old" (Part E). So no entry file is written, and the
description holds what the entry held:

- **The title** says what is true after the merge, as one sentence, and
  starts with the step (`S100: …`), or with `docs:` for a pull request
  of documents alone.
- **What:** the step and what changed, by area, in words a reader of
  `git log` follows without the diff.
- **Decisions:** each decision that was the owner's, in the owner's
  words and with its date; each the session took alone, with the
  alternative it rejected.
- **Evidence:** each gate that ran, what it printed and its exit status;
  what was not run, and why. "Tests pass" is not evidence.
- **Left open:** what the step did not do, and the row of
  `docs/plan/backlog.md` that carries each.

The description is written before the merge and read once more against
the branch right before it: `gh pr merge --squash` copies what stands
there, and a commit pushed after the description was written may have
made a sentence of it false.

**A dispatcher and workers (the owner, 2026-10-08; S099).** Three days of
five steps side by side in ONE session had used 86 % of the account's
weekly limit: a session that runs every step as its own sub-agents
re-reads its whole history on every turn. The owner first went back to
one step at a time ("Okay so we should go back only one step and then
archive and start a new step, it will save a lot of token i guess", and,
having compacted the session instead of archiving it, "after every step
you should say, compact and we can go on"), and the same morning asked
for more: "i want more session once with seperated worktree so i can
optimalise the context long and we can work quicker ... somehow we
should apply your policies where two worktree dont try the test in the
same time", and, to the session's proposal, "Okay, yes and you will be
the orchestrator this way". So:

- **A worker session carries ONE step** to a merged pull request, or to
  its written stop, in a worktree of its own; then it writes a status
  file and ends. The `plan-worker` skill is its brief. Inside the step
  contracts go out one after another, and a read-only reviewer may run
  beside an implementer.
- **One dispatcher session hands the steps out.** The `plan-dispatcher`
  skill is its brief. It keeps the board, a file outside the repository
  that it alone writes (a board in the tree would be edited by every
  branch); it gives a new step its number, chooses steps whose files do
  not meet, verifies each merge on `main` and puts up the next card. The
  owner starts each worker; the dispatcher cannot. It does not run a
  worker's step as its own sub-agents and does not read a worker's
  conversation. Its own small steps (the harness, the board's tooling)
  it does itself, to a merged pull request.
- **Two workers at once, at most**: one that holds the kind cluster and
  one that does not. The lanes of the table below still say what exists
  once, and at most one step in flight holds each; a step that rebuilds
  the images or moves the packages runs alone.
- **Scripts keep the sessions apart, not their good will.** `make
  pytest`, `make pytest-db` and `make alerts` take the machine's test
  lock (`scripts/machine_lock.sh`): a second session's run waits and
  says who holds it, and one that waited half an hour runs nothing. The
  cluster has its holder record (S075). A worker runs the tests of the
  areas it changed through those targets, and CI runs the whole suite.
  One test file run by hand takes no lock.
- **Compaction.** The dispatcher tells the owner to compact it after
  each step of its own, after a round of hand-outs is closed, and when
  its context has grown long; never while a background command or a
  sub-agent of its own is out. A worker is archived when its step is
  done.
- **The order is the owner's.** What can be built on kind comes before
  the paid cloud steps ("before we go to the azure payable thing we
  should go and do a lot of things, like seperated images, fake tenant
  and groups, users, roles and i think we have much more to solve before
  cloud"), and the owner means one tenant by that ("I meant one tenant
  not multi-tenant arch").
- **One board, not two.** The stop hook `check-lanes.sh` reads a board of
  sub-agent lanes inside one session (`.claude/lanes.md`). The
  dispatcher's board is not that file and the hook does not read it.

A session that is neither (the owner opens one and names a step) carries
that one step as a worker does and tells the owner to compact when it is
merged and verified. What follows is the rule of 2026-10-06 to
2026-10-08 for steps side by side in ONE session, kept for when the
owner asks for that again. Its lanes, and what it says of separate
sessions ("What is shared", "Numbers are taken late", "Who finishes
later, merges first"), hold for worker sessions today.

**Five steps side by side (2026-10-06 to 2026-10-08; not in force).**
This was the default, not an allowance: when a session starts, and
whenever a step closes, it fills up to five slots with steps whose
dependencies are `done` (three until the
afternoon of 2026-10-06, when the owner wrote "Okay can you start 5 steps
in total so we can go quicker on the steps"; a step the owner names on top
of the five is started too, as S077 was that day). The owner's decisions:
2026-10-06, "So back to multisteps… what steps could you start to proceed
more? Can we run 3 steps at once?", which reversed the one-at-a-time rule of
the day before (a night of one step at a time, and of a session that stood
still for six hours, had given two steps); and, the same day, "can we make
default the 2-3steps to do next to each other yeah if the situation allows
it, so deperated steps i mean, there eill be less colusion".

Steps are chosen so that they do not meet. Three things exist once, and at
most one running step may hold each:

| Lane | A step is in it when it | Why only one |
|---|---|---|
| Cluster | runs `make up`, `make deploy`, `make smoke`, `make demo` or `make demo-seed`, or changes what they deploy or check | One kind cluster; a second step's deploy changes what the first is verifying |
| Database | adds a migration | One sequence of numbers; a test database is built from every packaged file |
| Evaluation | changes a prompt, the triage graph, a tool's contract, a guardrail screen, the golden set or an injection case | One set of recorded answers and baselines |

A step in none of the three (documents, a spike, tests and tooling, code
that touches none of them) is free and fills any slot. With three lanes and
five slots, at least two running steps are free ones. The machine is the
other limit: implementers pass four test workers (three while the cluster
is up), and two whole suites do not run at once
(`docs/development-environment.md`). Among the ready
steps the session takes at most one per lane and prefers those with no file
in common; the plan and the root README are shared by every step and are
merged, not avoided. If fewer steps are ready, fewer run: a step whose
dependency is not `done` is never started to fill a slot. It is prepared
instead (mapped, its contracts written), so that it starts the moment its
dependency lands. The owner is asked only where the plan itself asks: a
step that costs money, an optional step beyond the plan's limit, a store or
a design that shapes what comes later.

Each step has its own worktree and branch, and its implementers work in
worktrees of their own. Inside a step, contracts whose files do not overlap
run side by side and the others one after another; a step's reviewers run
together. What follows holds for the steps of one session as for separate
sessions; the brief of each step, or of each session, says:

- **What is shared, and who owns it.** There is one kind cluster and one
  Azure environment: one session owns them, and the others run no
  `make deploy`, `make demo`, `make demo-seed`, `make down`,
  `make azure-state` or `make azure-apply`. There is one set of recorded
  model answers: only one session at a time changes a prompt or the triage
  graph, since that needs `make eval-record`.
- **Who holds the cluster is recorded** (S075). One step holds the kind
  cluster at a time. The cluster records its holder and the state of its
  last run (`make cluster-holder` prints them): a run of `make up` or
  `make deploy` that starts to change it writes `changing`, and one that
  ends well writes `ok`. `make up`, `make deploy` and `make down` stop for
  another holder unless `TAKE_CLUSTER=1` stands in front of the command.
  The main session runs from a detached checkout, so it sets
  `CLUSTER_HOLDER` to the step it acts for. The record is a notice, not a
  lock. A script that runs several commands against the cluster stops at
  the first one that fails, and nothing that deletes follows a step whose
  result was not read. On 2026-10-06 the session's own script did not: it
  ran `make deploy`, which failed, and went on to `make down`, which
  deleted the cluster and with it the audit rows of the failure (S075's
  step file).
- **What each step has of its own.** `PYTEST_DB_CONTAINER`,
  `PYTEST_DB_PORT` and `PYTEST_WORKERS` (4 beside other steps, 3 for an
  implementer while the cluster is up; the virtual machine ran the whole
  suite alone with 10 in under two minutes on 2026-10-05, and in 3 min 46 s
  on 2026-10-08 with 22,437 tests and coverage), for `make pytest-db` and
  `make eval`, so two test runs never meet. Since S099 the machine's test
  lock keeps two runs apart, one after the other, and the container's
  name and port may stay the defaults; the worker count is still passed.
- **Numbers are taken late.** Migration numbers, `T-NN` and ADR numbers are
  taken after merging `main` into the step's branch, right before the pull
  request. A branch whose migration number is not final is not deployed to
  the cluster: the runner records a migration by name and hash. The plan
  has no version and no change log of its own (S100): the pull request's
  description is the record ("The pull request's description", above),
  so nothing in the tree waits for the pull request's number.
- **Who finishes later, merges first.** That session runs
  `git merge origin/main` (no rebase and no force-push on a branch with a
  pull request), runs the gates again, then opens its pull request. After
  a merge of `main` into a step's branch ~~the whole suite runs on the merged
  tree before the branch is pushed~~ (until 2026-10-08) the tests the merged
  commits added run beside the changed areas', and CI runs the whole suite on
  the pull request ("Before pushing", above); a contract's gates name every
  directory its change reaches (2026-10-06: a merge pushed after the cheap
  gates left a test red on S076's branch).

A session does not see the others: it fetches and reads the open pull
requests before it assumes anything about them.

**Version updates.** Renovate (`.github/renovate.json`) opens grouped pull
requests on the first day of a month, from the day the owner installs the
app, and merges none. A session merges one like any other, once its
required checks pass, except where CI never runs what changed; the pull
request's body says so:

- `kind platform` and `postgresql images`: `make up` and `make smoke` on the
  branch, by the session that owns the cluster, and the versions table in
  `infra/kind/README.md`. The tags of the images a chart installs move in
  that pull request by hand, and Renovate proposes none of them (a new
  digest of the same tag it still does), so no image comes before its
  chart. The collector's images are outside that rule: their pin and the
  chart's appVersion differ, and their tags still arrive as updates.
- `base images`: `make deploy` on the branch, by that session. No job
  builds the image.
- `terraform`: `make azure-plan`, and the plan read; for the AWS module
  `make aws-validate` and `make aws-scan`, which need no account (S036); for
  the Google Cloud module `make gcp-validate` and `make gcp-scan`, which need
  no project and no credential (S078); for the AWS self-managed module
  `make aws-kubeadm-validate` and `make aws-kubeadm-scan`, which need no
  account (S079); and for the Google Cloud twin of that module
  `make gcp-kubeadm-validate` and `make gcp-kubeadm-scan`, which need no
  project and no credential (S079). Since S079 the `python` workflow installs
  the Terraform program the tests need (the `terraform` group moves that pin
  too, by the same reader); it runs no `init`, `validate`, plan or apply.
- The Trivy image of the scans (`TRIVY_IMAGE` in the `Makefile`, no group of
  its own): every scan on the branch (`aws-scan`, `gcp-scan`, `aws-kubeadm-scan`
  and `gcp-kubeadm-scan`), since CI does not run them and a newer image carries
  newer checks that can turn them red.
- `tooling`: the result of `Docs / Architecture PDF`, the one job that
  runs Pandoc. It is not a required check. Since S089 the required
  `derived diagrams` job renders every Mermaid block with the Mermaid
  image, so that pin is proven on the pull request itself.
- `agent framework`: never merged on green checks alone. Read the note on
  the pull request, run the three tests it names, and merge and release it
  only when `claims.briefs` holds no waiting brief (T-97, T-98).
- `mcp server`: the owner reads the release and merges, not a session.
  That package runs on the owner's laptop with control of a browser.

On that schedule a release is proposed once it is a week old. The hold is
advisory: a pull request asked for from the Dependency Dashboard arrives at
once with a pending `renovate/stability-days` status, which is not a
required check (the owner decided in S075 not to require it), and the
monthly refresh of the Python lock file has no hold; the Terraform lock
refresh is off, and the `terraform` group moves that lock after a week.
Read that status before merging one. A Python minor, a Kubernetes minor and a
PostgreSQL major are switched off there: each is a step's decision.
Advisories stay with GitHub's own security updates. A line added to
`infra/kind/pins.env`, an `_IMAGE` variable in the `Makefile` or a
`_VERSION` value in a workflow needs a reader in that file, and `make test`
fails without one; a pin of another shape needs a line in
`tests/test_renovate_config.py` too. An action in a workflow is pinned to a
commit hash, and the same test fails on a tag.

Cost rules:

- Docker-heavy targets (`make pdf`, `make mermaid-render`) run when their
  inputs changed, not on every step.
  CI renders every Mermaid block on every pull request (S089). Both
  targets run on the virtual machine too, whose Docker is rootless
  (S092). `Docs / Architecture PDF` refuses a start on a branch other
  than `main` and runs on a pull request only when the scripts, the
  `Makefile` or the workflow changed, so the PDF of a branch that
  changed documents alone is built with `make pdf` on the machine.
- Broad searches go to an Explore subagent, which returns conclusions instead
  of file dumps.
- The Azure environment exists only on demo days (C-04).
- For a whole local run, when one is wanted by name (since 2026-10-08 the
  whole suite runs in CI, "Before pushing"): one whole suite at a time on a
  machine, and none beside a busy cluster unless the machine was measured to
  carry both: on the laptop the suite
  took 9 minutes alone and did not finish in 35 beside a fresh deploy
  ([development environment](development-environment.md)). Before a whole
  suite the session counts the test databases that are running and waits
  until at most one implementer is testing with a database. While the kind
  cluster is up it passes six workers and tells implementers three. On
  2026-10-06 a suite beside four implementers' test runs and a fresh
  cluster pushed the machine into swap twice; a result from a swapping
  machine is not a result, and the suite is run again.
- Push a step's branch at the end of a working day, finished or not, with
  its step file filled in.
- Ask the owner only when an answer changes the design, the cost or a
  security boundary; otherwise decide, and record the decision in the step.

## Part B — Roadmap and step list

The tables below are the plan as it was written, and hold no status
(S101). A step's status is in its file, as one of `todo` · `doing` ·
`done` · `blocked` · `dropped`; Part F lists the finished steps and
those in flight, each with a link to its file. A step with no file has
not started.

Each step is sized for one focused session of two to four hours. Dependencies
are the step IDs in the last column. Every capability stays labelled
implemented, simulated or designed (C-07).

### Why this order

- **Walking skeleton early.** S009 connects claims API, runtime and gateway
  end to end before any layer is deep. From then on the demo always works,
  and each later step deepens one layer without breaking it.
- **Foundations before consumers.** The workspace and CI gates (S002), the
  synthetic data (S003) and the registry (S008) come before the services that
  read them.
- **Risk registers before risky code.** Threat model and data classification
  (S004) exist before the gateway handles its first real request (S010).
- **Spend late.** Cloud cost starts with the Azure foundation (S007) and stays
  small until M2 creates the full environment.

### Developer CLI

A thin `meridian` command grows with the steps that need it; it is not a
step of its own. Typer: commands from type hints, the same idiom as FastAPI
and Pydantic, at the cost of one dependency.

- **What it owns.** Platform work an agent developer does before opening a
  pull request. `make` keeps environment lifecycle (kind, the demo, the
  documentation gates); Terraform and Helm keep infrastructure.
- **One entry point.** CI runs the same command a developer runs, so a check
  that passes locally passes in CI. S008 adds `registry validate`; S017 adds
  ~~`eval run` and~~ `eval compare`; S050 adds `eval run` and `eval diff`;
  S039 adds `workload new`.
- **Boundary.** The CLI never approves, rejects or changes a claim; adjuster
  decisions stay in the UI, where they are audited (C-02). Commands that call
  the platform APIs, such as run inspection or audit search, need an Entra
  sign-in and stay designed until S021 exists.
- **Cost.** Evaluation uses ~~the replay provider~~ a scripted model, in
  process and at no cost (S017), ~~unless `--live` is passed (C-04; `--live`
  and a recorded model are S050)~~ and since S050 a recorded real model,
  replayed at no cost; recording again is `make eval-record`, which spends
  money (C-04) and is not a CLI flag (S050's decisions say why).
- **Placement.** `src/meridian/platform/cli/`, importing only platform
  packages. The Evaluation Harness reaches workloads through the ~~runtime
  API~~ Claims API (S017: every tool call needs the claim's row, so a run
  started on the runtime alone is refused), so the import contract from
  S002 covers the CLI too.

### Demo checkpoints

| After | What can be shown |
|---|---|
| S041 | A claim flows through API, runtime and gateway, visible as one trace |
| S015 | A triage proposal pauses for an adjuster and resumes on the decision |
| ~~S017~~ S050 | An evaluation report comparing two prompt versions |
| S018 | The full fifteen-minute demo on kind, from a clean checkout |
| S026 | The same demo on AKS, recorded, with the run's cost logged |
| S028 | An incident record written from a real game day |

### M0 — Bootstrap

| ID | Step | Done when | Depends |
|---|---|---|---|
| S000 | Plan, harness and architecture bootstrap | Model with five views, ADRs 1 to 3, constraints C-01 to C-07, harness (rules, skills, reviewers, hooks), Mermaid tooling; `make docs`, `make test` and `make check` pass | — |
| S001 | Commit and publish | First commit on `main`; public GitHub repository; docs CI green on GitHub; the README's derived diagram renders on GitHub; architecture-base Mermaid PR merged; agent-base `yarn.lock` reverted | S000 |
| S002 | Python workspace and CI gates | `pyproject.toml` uv workspace with empty ~~`src/platform` and `src/workloads`~~ `meridian.platform` and `meridian.workloads` packages under `src/meridian/` (see S002 decisions); ruff, pytest, an import-linter contract (no `langgraph` or `langchain` under `meridian.platform`) and gitleaks run in CI; a deliberate framework import in a platform package fails CI | S001 |
| S003 | Synthetic data and golden set | A seeded generator under `data/synthetic/` produces policies, policy-wording documents and first-notice-of-loss claims with labelled expected outcomes; a rerun produces identical output; no real names or documents | S002 |
| S004 | Security and quality registers | `security/threat-model.md` with T-IDs per trust boundary, `security/data-classification.md` with the data classes, `requirements/quality-attributes.md` with targets marked unmeasured; all symlinked into `overview/`; `make docs` resolves every cited ID | S001 |
| S005 | Agent framework spike | A three-step flow with an approval pause in Microsoft Agent Framework under `spikes/`, with notes; a decision matrix appended to ADR 2 | S002 |
| S006 | Local platform on kind | `make up` creates a kind cluster with ingress, PostgreSQL with pgvector, OpenTelemetry Collector, Prometheus, Grafana, Tempo and Loki from pinned Helm charts; a test trace appears in Grafana; `make down` removes it | S002 |
| S007 | Azure foundation | Terraform with remote state, a resource group, a budget with 50, 80 and 100 % alerts (C-04), Key Vault, and Azure OpenAI ~~`gpt-4.1-mini` plus `text-embedding-3-large` on DataZoneStandard in Sweden Central with a West Europe fallback~~ `gpt-4o` plus `text-embedding-3-large` on regional Standard in Sweden Central, with the West Europe fallback after the subscription upgrade (see S007 decisions); plan reviewed; apply confirmed by the owner | S001 |

### M1 — Claims triage on kind

| ID | Step | Done when | Depends |
|---|---|---|---|
| S008 | Platform registry | `config/registry/` YAML for models, providers, tools, agents, policies and tenants, with JSON Schemas; every deployment carries a residency label and allowed data classes; validated in CI; seeded for the claims workload; `meridian registry validate` is the check developers and CI both run | S002 |
| S040 | Harness refresh | The ECC plugin is off, so the harness this repository needs is copied in from development-base: the remaining drifted rules and skills re-copied, a code reviewer, the Python rules that fit, the skills later steps need, three slash commands, the gate and session hooks, the chrome-devtools MCP server and the git hook-bypass denies with their cases; `make docs`, `make test` and the guard-bash cases pass | S008 |
| S009 | Walking skeleton | A claim posted to the claims API starts a one-node LangGraph run that calls the gateway's replay provider and stores a ~~decision~~ triage proposal; ~~one trace spans API, runtime and gateway in Tempo; `make demo` runs it on kind~~ an end-to-end test proves one trace across API, runtime and gateway; per-service schemas, roles and migrations tested against PostgreSQL in CI (split on 2026-09-30: the kind half is S041) | S006, S008 |
| S041 | Walking skeleton on kind | One image for the three services, manifests in namespace `meridian`, an HTTPRoute on a `*.localhost` hostname, per-service database roles on the cluster; `make demo` posts a claim and the one trace spanning API, runtime and gateway is found in Tempo; `make smoke` stays green | S009 |
| S010 | Gateway routing ~~and resilience~~ | Registry-driven routing by data class and residency; Azure OpenAI adapter; ~~timeout, retry, circuit breaker and fallback to the second region;~~ a residency mismatch is refused and audited; contract tests pass (split on 2026-10-01: resilience is S042) | S004, S007, S009 |
| S042 | Gateway resilience | Timeout, retry, circuit breaker and fallback across a route's candidates, with a second `gpt-4o` deployment in Sweden Central as the real second candidate and the second region labelled designed until the subscription is upgraded; a fault injected into the first candidate is answered by the second, and every attempt is audited; contract tests pass | S010 |
| S011 | Gateway budgets and cost | Per-tenant quotas, rate limits and token budgets enforced, with the cost reserved before the call; cost metered per tenant, agent, model and provider; ~~one audit record per call; a Grafana cost panel~~ a call ID on every audit record of a call (split on 2026-10-01: the Grafana panel is S043) | S010 |
| S043 | Gateway cost panel | A Grafana dashboard on kind, provisioned as code, shows tokens and cost per tenant, agent, model and provider from the gateway's metrics; `make smoke` finds the series in Prometheus | S011, S041 |
| S045 | Gateway embeddings | `POST /v1/embeddings` on the Model Gateway: the embedding route walked like the chat route, with the same caller headers, residency filter, tenant limits, ledger and audit; a simulated replay embedding; the Azure OpenAI adapter; the registry gives each embedding deployment its dimensions and refuses a route whose candidates differ in model or dimensions; contract tests pass | S010, S011, S042 |
| S012 | Knowledge and retrieval | Policy wording ingested into pgvector; hybrid search; ~~the knowledge MCP server returns cited chunks;~~ retrieval checked against a labelled query set (split on 2026-10-02: the gateway's embedding endpoint is S045, and the knowledge MCP server is S046) | S003, S009, S045 |
| S046 | Knowledge MCP server | `wording_search` served by the knowledge tool server: the call is bound to the product and wording version of the run's own policy, the query is embedded through the gateway under the run's tenant and agent, and the answer is cited chunks under an output schema; the server's role and grants; contract tests pass | S012, S013 |
| S013 | Policy and claims MCP servers | Tool contracts in `api/mcp/`; policy and claims MCP servers; per-agent allowlists from the registry; mutating tools require an idempotency key; every call audited (split on 2026-10-01: in-process, as S009 was; the servers on kind are S044) | S008, S009 |
| S044 | Tool servers on kind | The tool servers that exist run in namespace `meridian` under their own database roles, a job seeds the policy tables from the synthetic data, and the runtime reaches the servers by their cluster names; `make smoke` calls one tool through the runtime's client and `make demo` stays green | S013, S041 |
| S014 | Triage graph ~~and guardrails~~ | Triage validates the policy, retrieves terms, screens fraud with rules and drafts a schema-validated proposal; ~~PII redaction and injection detection in place;~~ threat model updated (split on 2026-10-02: the guardrails are S047) | S011, S013, S046 |
| S047 | Guardrails | Personal data is redacted before a model call and in logs; claimant text is screened for injected instructions before the model reads it; a request carries its own data class, which can only be raised above the tenant's, and a `special` request makes no model call and goes to the adjuster; threat model updated (split on 2026-10-03: the provider's structured outputs are S051) | S014 |
| S015 | Human approval | Interrupt and resume with the PostgreSQL checkpointer; ~~the claim lifecycle from the architecture overview implemented and tested~~ the claim states that a triage run and an adjuster's decision drive, one triage of a claim at a time, and a state for a claim whose triage failed; approval decisions audited (split on 2026-10-03: the rest of the lifecycle is S048) | S014 |
| S048 | Claim lifecycle, the rest | An adjuster sends a claim back to triage; a claim whose triage failed is referred to an adjuster, who decides it with no paused run; a claimant withdraws; documents that arrive (metadata only, T-38) start a new triage, at most five triages per claim; ~~a claim whose documents miss the deadline is closed as rejected; the Claims API stamps the report date and a decided claim enters the claim history (T-66); a scheduled sweep ends runs left `Running`, paused runs that no claim points to, and checkpoints a failed delete left (T-63)~~ (split on 2026-10-03: the deadline and the sweep are S052, the report date and the claim history S053) | S015 |
| S016 | Adjuster UI | Server-rendered queue with claim, proposal, citations and fraud flags; approve, reject and request documents; audit trail; ~~time-boxed to two sessions~~ (split on 2026-10-03: the claimant's pages are S049) | S015 |
| S049 | Claimant pages | A claimant submits a claim and reads its status on server-rendered pages behind the staff route until claimants are identified (T-01); the form says the data must be fictional (T-04); the answer tells the claimant what happens next without describing the proposal (T-65) | S016 |
| S017 | Evaluation harness | Golden-set replay with rule ~~and LLM-judge~~ graders (~~tool choice, arguments, groundedness,~~ route, reason, recommendation, amount, fraud indicators, missing documents, citations, completion~~, latency, cost~~); a report per prompt version; a CI gate on prompt or tool changes; ~~`meridian eval run` and~~ `meridian eval compare` drive~~s~~ it locally and in CI (split on 2026-10-03: the judge, latency and cost, a recorded or live model and `eval run` are S050) | S003, S014 |
| S050 | Live evaluation | The golden set answered through the Model Gateway by a recorded model, a recording missing for a changed prompt failing the gate, and re-recorded with ~~`--live`~~ `make eval-record`; an LLM judge grades groundedness only, under an agent identity of its own, and cannot override the rule graders (T-29); latency and cost graded from the gateway's ledger; the tool names and arguments of each run kept with the results; `meridian eval run` against a deployed stack; a report comparing two prompt versions | S017, S054 |
| S051 | Structured outputs | The Model Gateway passes a JSON schema for the answer to providers that support it (Azure OpenAI's structured outputs), declared per agent in the registry and refused for a deployment that cannot honour it; the triage assessment asks for its three-field answer by schema and still reads it strictly; tried live | S047 |
| S052 | Scheduled sweep | A scheduled job ~~closes a claim whose documents miss the deadline as rejected~~ refers a claim whose documents miss the deadline to an adjuster (Part D question 3, answered on 2026-10-03), ends runs left `Running` that no resume takes over, paused runs that no claim points to, and checkpoints a failed delete left (T-63); a documents post whose triage failed while another move changed the claim is answered by what was stored, not by the claim's state afterwards (a `stored` flag on `DecisionFailure`; added on 2026-10-03 from S049) | S048 |
| S053 | The claimant's word checked | The Claims API stamps the report date once claimants submit their own claims, and a decided claim enters the claim history, so `late_report` and `frequent_claims` stop resting on the claimant's word (T-66); the claimant's pages answer a 422 for an ID in the path, 404, 405, 413 and 400 with a page, not the API's JSON, and no server span's `http.url` keeps a query string (platform-wide, T-03) (both added on 2026-10-03 from S049) | S048, S049 |
| S054 | Parallel tests | `make pytest-db` and the CI python job run the suite in parallel with `pytest-xdist`: a database per worker inside the one PostgreSQL container, ports for the stack tests in `tests/meridian/stacksupport.py` that do not collide, and an empty database of its own for the migration runner's concurrency test; the CI python job's time before and after recorded in the step. It unblocks a coverage gate, which is not added here | S049 |
| S018 | M1 exit | Views match the code and the register says so; threat model v1; a fifteen-minute demo script; the demo runs from a clean checkout with `make` | S016, S017, S041, S042, S043, S044, S047, S048, S052 |

### M2 — Azure, identity, delivery

| ID | Step | Done when | Depends |
|---|---|---|---|
| S019 | Hardened Helm charts | Probes, resource limits, default-deny NetworkPolicy, PodDisruptionBudgets, non-root read-only containers, pinned digests; `helm lint` and the infra reviewer pass | S018 |
| S055 | Service-to-service identity | On kind, each service proves which service it is to the one it calls: the Agent Runtime, the Model Gateway and the tool servers refuse a call that carries no identity or comes from a service the registry does not map; the tenant and agent a caller may name come from that mapping, and a header that disagrees is refused; the tool servers accept the runtime alone (T-08, T-24, T-48, T-50); the mechanism is chosen with the owner when the step opens and recorded in an ADR | S019 |
| S020 | Azure platform | Terraform adds the virtual network, AKS, ACR, PostgreSQL Flexible Server with pgvector and Workload Identity to Key Vault; the environment is created and removed with one command each. **First half, code only (2026-10-07): a module `infra/terraform/azure/` written, validated and scanned without an account and NEVER applied** (44 resources, two doors: `make azure-platform-validate` and `make azure-platform-scan`), its README, ADR 11, the deployment view `DeploymentAzure` (designed), the threat model's rows T-103 to T-107 and the Azure platform document brought to it. **What waits:** the wrapper and the command guard's rules for it (no door that plans, applies or removes exists); the owner's upgrade to pay-as-you-go by about 2026-10-30; the firewall for the foundation's vault and account, **decided by the owner on 2026-10-08** ("Deny, allow operator (Recommended)": default deny, the operator's address allowed, private endpoints for the cluster; **designed**, to be written as code in the foundation module and applied at the next apply the owner runs); the apply and its cost, stated at the paid stop; and the second half (the chart on the cluster, the roles, the egress rule, S022's push). The "done when" above is the whole step and is not met | S007, S019, S055, S056 |
| S021 | Identity | **The staff half (cut on 2026-10-07: claimants are S093, the edge layer is S094).** Entra ID sign-in for staff on Azure, and on kind a mock OIDC issuer, Keycloak (the owner's choice); four roles, platform-admin, agent-developer, adjuster and auditor, as a `roles` list claim in the token; the Claims API checks a staff token on its JSON routes and runs the sign-in for the adjuster's pages itself, with a session cookie it signs (the app layer first, the owner's order); a role is required on every route but a closed list; the tenant is resolved from the validated token through the registry; who decided is stored on the decision row (the audit rows' actor waits for S085's outbox); all of it behind a switch, `MERIDIAN_SIGNIN`, off by default until `make demo`, smoke and the evaluation client carry their own tokens. **Implemented in part (2026-10-08), wired to nothing:** the token check, the key-set client, the session cookie, the route guard and the pages' authorization-code flow are seven modules of `platform/common` with tests, three import-linter contracts guard them, and the Keycloak pin, the realm generator and an opt-in rig exist (the rig's six tests passed against the pinned image in a container); two security reviews and a re-check of the module ended `Y4 MAY WIRE IT`, and the security review of the flow said the same. **On kind (local only), an add-on that is off unless switched on:** Keycloak ran there on 2026-10-08 (runs KR1 to KR3), its realm holds a cast of seven synthetic test people since a rotation (six staff in four groups, one role a group, and one person in no group), `identity.sh users` lists them and one guarded command prints their disposable passwords on the owner's terminal; signing in with them does nothing in the pages yet. **Designed, not built:** a route's use of the guard or the flow (Y4), the switch `MERIDIAN_SIGNIN` (no code reads it), the actor and tenant (Y6), the scripts' tokens (Y7) and the decision record (Y8); no Entra tenant has issued a token (Part C has the design, its contracts, the built parts and the threat rows T-114 to T-121). **The owner, 2026-10-08,** asked "another question, we do we have to implemt keycloak? we are gonna use entr id not?", was told that Entra ID is the issuer on Azure and that Keycloak is only the local stand-in for the mock issuer, and chose "Keep it, opt-in only (Recommended)" (offered beside it: Entra only; keep only what exists): Keycloak is built on kind as an add-on that is OFF unless switched on and is not part of plain `make up`, and Entra ID is the issuer on Azure (designed) | The kind half: nothing unbuilt. The Entra half: S020's apply |
| S093 | Claimants sign in as themselves | The owner's decision of 2026-10-07: "Own claimant sign-in" (the session had recommended the pages behind the staff sign-in for now; the owner chose against it). A second realm beside staff, `meridian-claimants`, with members of the public and no role (on Azure Microsoft Entra External ID, designed and not asked about yet); a claimant's session of its own, shorter than a staff one; a claim is OWNED by the claimant who filed it (the claimant's subject is stored on the claim: a migration, a column added in one file and backfilled in the next); a claim's status, its documents, its withdrawal and uploads, where they exist, are refused to another claimant with the answer an unknown claim gets; a claim that exists without an owner is staff-only; each realm's token and cookie is refused on the other's routes. **Designed: nothing of it is built.** Open for the step's design: whether a claimant is also tied to a policy (T-76's planted claims), and the claimant's session length | S021 |
| S094 | Sign-in at the edge | The owner's order of 2026-10-07: "App layer first, edge second (Recommended)"; both layers are the committed end state, so this is a step and not a "later". First TLS at kind's edge, which is plain HTTP today; then Envoy Gateway's sign-in and role rules (its `SecurityPolicy`) in front of the same app checks, which stay. A step of its own because Envoy's sign-in forces `Secure` on its cookies with no switch, so it cannot be seen to work before the edge has TLS; because the proxy pod and the controller pod each need egress to the issuer, in a namespace that is default-deny today; and because the facts sheet marks six paths as ones only a run would tell (whether a browser keeps the forced `Secure` cookie on `http://*.localhost`, whether the controller takes an in-cluster issuer with a plain-HTTP token endpoint, which pods need the new egress, whether a certificate-authority route to the issuer works, whether a policy attaches to one named rule of the Claims API's route, and what the app then receives). **Designed: nothing of it is built** | S021 |
| S022 | Delivery pipeline | Build, SBOM, Trivy scan, cosign signing, push to ACR, kind smoke test, manual approval, deploy to AKS; the rollback runbook exercised; evidence attached to the release. **Hard condition (the owner, 2026-10-08):** the pipeline depends on the staff sign-in (S021) only, not on claimants' sign-in (S093) or sign-in at the edge (S094); so until S094 exists, the AKS edge admits the operator's address only, and the pipeline's deploy refuses an edge without that allowlist. The question was "Do S022 and S026 wait for S093 and S094?", asked with a table; the answer: "S021 only, allowlist (Recommended)" (the other options are not recorded). Designed: nothing of the allowlist or the refusal is built | S020, S021 |
| S023 | Mistral provider | Mistral Large 3 adapter on Azure AI Foundry, DataZoneStandard; the routing policy uses it; ADR 3's provider set updated | S010, S020 |
| S024 | Operations baseline | SLO definitions (targets, unmeasured), alert rules and dashboards as code; runbooks for provider outage, budget exhaustion, database failure, rollback and secret rotation | S011, S019 |
| S025 | AWS mapping | An AWS deployment view and an ADR mapping every Azure service to its AWS equivalent, written against the Azure platform as S020's row and the model design it (the owner, 2026-10-06: before Azure, so the dependency on S020 is lifted; when S020 has run, a mapping it falsified is corrected there) | S007, S019 |
| S077 | GCP mapping | A Google Cloud deployment view and an ADR mapping every Azure service to its Google Cloud equivalent, as S025 does for AWS and against the same designed Azure platform (the owner, 2026-10-06: "add another step for gcp like aws too and start it too"); where S025 and this step would say one thing twice (the table of what Azure is used for, the residency rule for a second and a third cloud), it is said once and both use it | S007, S019 |
| S026 | M2 exit | Environment created, fifteen-minute demo on AKS, environment removed; recorded; the run's cost logged. **The owner, 2026-10-08** ("S021 only, allowlist (Recommended)"): the demo waits for the staff sign-in (S021) and not for S093 or S094; the AKS edge admits the operator's address only until S094 exists, the condition written into S022's row. Designed, not built | S021, S022, S024 |

### Backlog steps

Made from the follow-up backlog by the owner on 2026-10-04. They harden
what exists and add no capability; none needs Azure, and none costs
money unless its row says so. They sit beside M2 and M3, not in a
milestone's exit.

- S056 to S061 change different files and were made to run in parallel,
  S056 owning the cluster (for one day, 2026-10-05, steps ran one at a
  time; Part A). Two places are shared, so the session that
  finishes later expects a merge there: S056 changes the `/healthz` route
  in the application files S058 and S059 work in, and nothing else in
  them; S060 and S061 both work under `workloads/claims_triage/`, S061 in
  `injection.py` and `evaluation.py` alone.
- S062 to S064 need the cluster and follow one another.
- S065 to S067 follow the steps whose files they share. S067 is the one
  step here that changes the triage graph's rules.

S062 is unblocked by S056 and owns the cluster: start it on the new
machine with `make up` and `make deploy`, which also show whether the
pinned images resolve there.

S068 to S076 were made on 2026-10-06, at the owner's request, from the 83 open
rows of the follow-up backlog that named no step, so that the step list is the
whole of what is left. Like the others they harden what exists and need no new
Azure resource. Two say what they cost: S071 calls a live model, and S070's
first item was the owner's decision on uploads, the one capability among them
(answered on 2026-10-07: build them, as S080, after the table below).
Each waits for the running steps that change its files (its last column).
Several rows are observations seen once: their step closes them with a
measurement or as not reproduced; it does not have to change code for each.
The grouping is the session's; the owner may move a row or merge two steps.

**Up to Azure (the owner, 2026-10-06).** "So we have a lot of steps before
azure… and add the aws template too and we will test it in a real aws
enviroment and you should go until azure step where we have to spend some
money on it". So the session goes on, without waiting for a go-ahead between
steps, through every step that needs no Azure resource and no payment: S064,
S066 and S037 (running that day), S067, S068 to S076, S025 and S077, and the
first halves of S036 and S078 (since the next paragraph: S078 whole, and the
first half of S079). It stops, and says what the next thing costs,
at each point where money is spent: a paid model call (S071 whole, and any
recording a changed prompt needs in S067), the apply of S036 in the owner's
AWS account ~~and of S078 in the owner's Google Cloud project~~ (dropped by
the next paragraph), the apply of S079 in that AWS account, and S020.
The parts of a step that need no payment are done and the paid part is parked
in its section, as the owner said of a blocked step on 2026-10-05. Decisions
inside those steps that are the owner's (retention periods, uploads,
node-exporter, a coverage gate, the Renovate hold, two guard rules) are asked
when their step opens and do not stop the others.

**Managed and self-managed Kubernetes (the owner, 2026-10-06).** After asking
why the kind cluster has one node, which is its control plane: "okay but when
we are at gcp, aws and azure we should prezent manages k8s and not managed
too", then "we should build managed so i guess aks on azure and not managed on
aws and gcp just scafold", and, to the session's first reading of that,
"Self-managed Kubernetes is a scaffold only, on AWS - no we will build it on
aws, gcp just scafold". So: on Azure the managed cluster (AKS) is the one that
is built and run (S020 and what follows). On AWS a cluster whose control plane
the owner's account runs itself, on the cloud's virtual machines, is built and
applied once (S079), after its cost is stated and the owner says yes. On Google
Cloud both kinds are a scaffold only: Terraform that is validated and scanned
and never applied, which drops the applied half of S078. The managed cluster of
S036 stays validated code; whether it is also applied is asked at S079's paid
stop (the session's suggestion, which the owner heard: one cluster at a time on
AWS, the self-managed one first). The comparison of the two kinds is written
for all three clouds, in their mapping documents (S079). The owner confirmed
both readings the same hour ("yes both are right, go on").

| ID | Step | Done when | Depends |
|---|---|---|---|
| S056 | Certificate lifecycle | On kind: no service goes on serving a certificate past its end with green probes, and an alert fires before one expires (T-89; the owner's choice on 2026-10-04: `/healthz` answers 503 once the loaded certificate is near its end, so the kubelet restarts the container, which loads the renewed one); the `meridian-services` issuer signs only for the `meridian` namespace and its URI prefix, and a request from another namespace is refused on the cluster (T-88; the owner's choice on 2026-10-04: cert-manager's approver-policy, with the built-in approver off); a certificate's key file is readable by the service's own user and group alone; `make deploy` stops before its Jobs on a cluster without the issuer; `make smoke` reads the audit reason of its 403 and tries a certificate from another CA | S055, S024 |
| S057 | Test and tooling hygiene | Without the cluster: a `make` target runs the secret scan a push needs; the tests that rest on a sleep, a wall-clock limit or a port closed before its use (the two resume races in `test_runtime_app.py`, four limits, `unused_port()`) hold by construction, shown by repeated runs under load; the registry tests find a deployment's entry by its key, not by adjacent lines; `test_scheduled_sweep_migration.py` and `test_sweep.py` are under the 800-line ceiling; `make docs` fails on a blank line that splits a table (not done: the checker is development-base's to change first, and the backlog row stays open); `check-iac.sh` lints the chart with the values `make helm-lint` uses; the CI python job's limit is set from its measured runs, and the time the recorded evaluation, the scaffold's first-run test and the injection stack test add is each measured and either cut or accepted with its number recorded | S054 |
| S058 | Gateway loose ends | In the Model Gateway: a provider's token counts are bounded before they reach the ledger; a refusal row carries the call's purpose; an embedding input that would pass the provider's 8,191 tokens is refused with an answer of its own, not a 502; the count of a refusal flood's last window is written; a request over its rate limit is refused before its text is redacted (T-73); contract tests pass | S045 |
| S059 | Runtime and tool server loose ends | The runtime's tool client lives longer than one call; `runtime.runs` text columns have length checks; an error answer without a reason is not read as the refusal `unknown`; one URL check in `common/env.py` serves every service address; `policy_lookup`'s output schema requires `policy` when `found` is true; `finish_run` writes a status only over the one it expects, so a late leg cannot overwrite the sweep's `Failed`; a tool server's waiting calls are bounded, and a search the runtime gave up on is not charged or audited as completed (T-62); no span processor or sampler can see a URL with its query; the tool servers have their entry in `test_openapi.py`; contract tests pass | S046, S052 |
| S060 | Claims pages and API loose ends | In the claims workload, without a change to the triage graph or a prompt: the adjuster's queue has a next page past 100 claims and shows that a referred claim's documents are overdue; documents posted after the deadline are shown to the adjuster as tried; the claimant's page says by when documents are due and picks the latest proposal with the tie-break the views use; a 500 or 503 under `/claimant/` is a page; `database_failure` carries the claim's ID, and a claim that is not valid facts is logged by field and error type, never by its text; the calls to the runtime have a timeout per phase; `AGENT` and `TRIAGE_LEASE_SECONDS` live where the sweep imports them without FastAPI | S053 |
| S061 | Scaffold, registry and evaluation plumbing | `meridian workload new` writes the new agent into the Agent Runtime's entry in `services.yaml`, says which line of an unusual file it refuses and what holds a taken name, and its comparison "the old agents plus exactly one" has a test that reaches it alone; `meridian registry validate` answers an unreadable registry directory with a message, not a traceback; `eval run` refuses a golden set that is not the workload's own, empty or not; the two entry-point groups share one loader and its trust checks; `injection.py` imports no private name, has a benign clause case, and a changed screen pattern asks for a new baseline | S039, S050 |
| S062 | Smoke and deploy loose ends | On kind: `make smoke` reads the alert rules, the health dashboard and the stores it does not read yet, proves more than one denied path (~~egress outside the cluster~~ egress from the Claims API to the gateway and to the API server's Service, the database's policy; changed on 2026-10-06: smoke reaches nothing outside the cluster, so an address on the internet is not tried) and notices a schedule that stopped after a success; `make demo` says so when a trace's readings alternate; finished migrate and seed Jobs remove themselves, and a target lists the `meridian:*` images no workload uses (removing them stays the owner's command); `make up`'s wait on the Gateway's `Programmed` condition and the wait after an interrupted deploy each end with a message that names the remedy; the network-policy tests of `test_helm_chart.py` are a file of their own | S056 |
| S063 | The cluster outside `meridian` | On kind: the `cert-manager` and `observability` namespaces have NetworkPolicies and Pod Security labels, so only Meridian's pods push to the collector (T-68, T-84); the Prometheus operator and kube-state-metrics read no Secret they do not need (T-68); the database pod reaches the API server's address alone; the platform charts' images are pinned by digest; telemetry to the collector is not clear text, or the threat register accepts it with its reason (T-90); the seed and the ingestion Jobs run under a role of their own (T-25); the expiry of the database's certificates, and what a renewed authority needs, are recorded | S062 |
| S064 | Metrics and logs | On kind: the services' logs reach Loki, and no access log keeps a query string (T-03); the runtime and the tool servers export metrics, among them a caller that cannot reach the gateway and the knowledge server's empty-store and stale-vector warnings; the assessment's outcomes are counted by reason word, and the sweep reports what a pass found; a rule fires on a series that went absent; the cost dashboard's queries survive a gap in the data | S059, S060, S063 |
| S065 | Database and migrations | `ensure_roles` has a lock timeout and names its isolation level; audit rows of one transaction can be ordered; the sweep's listing of leftover threads does not read every checkpoint row, and its confining trigger does no work for another role's update; a check refuses two migrations with one number before a pull request merges; a column added to `claims.claims` is backfilled without holding its lock, and the rule is written down; a test database is copied from a template; the ingestion's tests that need no database run without one; retention for `audit.events` and `gateway.usage`, the periods chosen by the owner when the step opens | S057, S059, S060 |
| S066 | Gateway ledger upkeep | A command of the gateway's own, under a role of its own and with an audit row, credits a tenant, closes a reservation a dead process left `reserved` and expires old ledger rows; the budget runbook names it; the rate windows are shared between gateway processes, so two pods in a rolling update do not each allow the full limits (T-45), the store chosen with the owner when the step opens | S058, S065 |
| S067 | Triage rules and screening | The one step of these that changes the triage graph's rules: claims of one policy that are open at the same time count for `frequent_claims` (T-76); the injection screen reads the description as posted, before the claimant's name is replaced; the redaction knows Hungarian forms of names and identifiers; the stored wording is compared with the manifest after ingestion (T-27, T-57); a wording version missing from `wording.EXCLUSION_CLAUSES` fails with a message that names it (moved here from S060, which may not change how the triage routes a claim); golden-set cases on the fraud indicators' boundaries and for an unknown policy number; `make eval` passes, and a change that needs `make eval-record` waits for the owner's yes, since it costs money. Built as (2026-10-06; implemented and tested, none of it run on a cluster): the Claims API screens the description as posted and hands the run one boolean, `posted_text_addresses_the_model`, which the assessor reads as a hit of its own screen (CLM-1053 and CLM-1054 are stopped, 26 of 66 attacks); the redaction finds the Hungarian national phone, tax number, domestic account number and personal identification number on their own and the social security and tax identification numbers after their word, and the claimant's name is replaced with Hungarian endings after a capital letter; a wording pair the table has no count for fails the run with the fixed code `wording-version-unknown` where the rules would read it (the log line names the product and version only when both pass a closed check), not with a message that names it, and a claim that is not valid facts fails with `claim-not-valid` after a log of its fields; the golden set holds 47 claims, seven of them new, from a second random stream, and the model is not asked about them; claims that are still open count for `frequent_claims` by the strict date rule of a decided one, and a withdrawn claim never does (migration 0025, the owner's decision of 2026-10-06); `meridian knowledge verify` compares the stored clauses with the manifest-verified wordings and the ingestion Job runs it after its write (migration 0026). The free replay passed after each landing that could move a recorded answer, and no paid recording was made | S060, S061, S064 |
| S068 | Database upkeep and retention | The owner names the retention periods for `audit.events` and `gateway.usage` first (open since S011; without them the step builds the mechanism and schedules nothing); an insert-only audit table has a way to expire rows; migration 0017's rewrite of a large audit table has a way through that is written down and tested; the static check on migrations says what it cannot see or sees it; `expire_ledger` works in batches; a holder of the upkeep credential cannot stall the gateway with an open transaction; a login that is a member of `claims_sweep` is confined or refused where the database is made; no role creates temporary tables it does not need. Built as (2026-10-07; implemented and tested against PostgreSQL, none of it run on a cluster; no number of days and no schedule is set anywhere): audit rows expire through one function of the owner's that only a session logged in as the upkeep role reaches, `meridian gateway expire-audit` with a dry run that counts, in batches of at most 10,000, each with an audit row, and the rows the upkeep role wrote itself are never removed by it (migrations 0027, 0028); the ledger expires in batches of 100 to 10,000 usage rows on one connection, with a closing call for the counters and credits (0029, 0030); 0017's way through is written down in the migrations' README as needed by no database that exists, a test shows that it fails closed at its first heavy statement and the two ways out are designed, not built; the static check on migrations sees more statements and the README lists what it does not see, each entry pinned by a test; a database default ends a transaction left idle after 60 s (0031), which stops a forgotten transaction and not a deliberate one, so a holder of the upkeep credential can still stall a counter for the statement timeout; a member of `claims_sweep`, or of `gateway_upkeep`, is refused at every `meridian db migrate` after the files are applied, which detects at the next deploy and does not prevent; `pg_temp` is last in the path of every trigger and definer function, with a test over the whole catalog, and the right to make temporary tables stays with PUBLIC (decided, not built). Not built: the periods and a schedule (the owner's, four questions in the section), the briefs' expiry (waits for the owner), a parser for the static check | S066 |
| S069 | Runtime and gateway edges | Without a change to a prompt or a rule: a validation error in the triage's two answers logs the field; the tool-call limits can differ by agent, or the plan says why not; a failed resumed leg does not leave the first leg's value to be read as the answer; `drafted_by` is right for a completion the filter withheld and the provider billed; the runtime's client of the gateway is bounded per call; a resumed leg that outlived its lease cannot write over the leg that took the run; `service_url_problem` refuses what the HTTP client refuses; a shed tool call's audit row names its run where that can be checked; the refusal flood's count covers the caller check and the throttles; an embedding input is bounded in tokens; the health check watches the certificate the server loaded; the ingestion's data class has a tenant of its own (T-60, the owner's decision when the step opens). Cut in two on 2026-10-06 (the design in Part C): a first half with no lane, and a second half on the cluster, the server's certificate and the health check (R11) and the ingestion's tenant (R12, which the owner decides at S020). Built as, first half (2026-10-07; implemented and tested, none of it run on a cluster, no real provider called): the Claims API logs the failed fields of the runtime's answer, of the triage proposal and of the brief's output, and counts a stored proposal as stored; a leg ends its run only over the `updated_at` its own start or claim wrote, with no new column, and a late leg answers the stored status with no output; a resume that carries a value is refused with a 422; the runtime's call to the gateway has a deadline of 30 s as a whole, a timeout per phase, a reply cap of 1 MiB and `Accept-Encoding: identity`; a refused prompt and a withheld completion are told apart on the wire (`X-Meridian-Completion: withheld` and three headers naming the deployment) and the withheld one has its drafter on record; `service_url_problem` also asks the HTTP client; a shed tool call's row names its run where the run's own row can be read; every service writes the summary of a refusal flood's last window through one writer; a module that exits at import is a failed load; the access log's path is unquoted to a fixed point and loses a userinfo part; the scaffold names the host; the tool span names its step. Not built, each as a decision with its reason in the section: limits per agent (R2), an embedding bound in tokens (R10), a ceiling on the rate limits (B13), a breaker shared between processes (B14), one word for the two limits (B18), a holder column and the second host's scaffold. Built as, second half (2026-10-07; implemented and tested, and seen on kind in two runs, K1 and K2): the five services that serve TLS start through `python -m meridian.platform.common.tlsstart`, which reads the certificate once, for uvicorn's own context and for the health check, so `/healthz` watches the certificate the server loaded (R11); the module refuses a start that would not ask for a client certificate and ends a start it cannot make with one `tlsstart:` line and exit status 3; K1 saw the 422 for a resume that carries a value, one `suppressed` row from each of the five services that keep a throttle and the access log's path for an address encoded twice, and K2 saw the deploy, the served certificates of four of the five equal to the issued ones, a renewal and a restart, and smoke's 46 lines twice | S064, S037 |
| S070 | Claims intake and what the adjuster is told | Uploads (T-38: the largest item here) are a step of their own, S080, split off when the owner chose on 2026-10-07 to build them ("Build now"); this step no longer holds them; a report dated as a recent loss is seen for what it is, or T-66 says why it cannot be; the adjuster's page marks a recommendation that rests on the model's answer, so a steered model's `approve` does not read as the rules'. Built as, first half (2026-10-07; implemented and tested against PostgreSQL and in the pages' own tests, none of it run on a cluster, nothing paid, no fingerprint moved): the adjuster's claim page says beside a recommendation whether it rests on a model's reading of the exclusion clauses or on the rules alone, and the queue marks it in a column, from one function over the stored fields (no new field, no migration); it reaches the 6 steered recommendations that wait for an adjuster and not the 28 automatic approvals, which no page lists; the page labels the loss date and the report date as not checked and shows two gaps in days, with no rule or bound, and T-66 says why; the claimant's name pattern is built from a read before the claim's row is locked and only for a request the claim can go on with, so a refused request pays no compile (a stale page and a documents post past the cap still do); the redaction is split into six modules by a proven move; the differential test classifies every lost run, its generator writes the forms it lacked and both date guards are pinned from both sides; the e-mail pass reads its placeholder from the mapping; and an international Hungarian phone number is cut at a space before a second number, in a form narrowed after a review (R3b) and narrowed again after a second (R3c, 951b72c), so that it turns the plain shape and not every text the row quoted. Not built here, each with its reason in the section: uploads (S080's: the owner decided to build them), a bound on what a name may replace, a reorder of the assessor's checks, the wider cut of a dotted number with a `06` group and a third date guard (the owner's questions), and the fix of three known leaks of the phone matcher | S067 |
| S071 | Measurements that need a live model | Costs money (52 chat calls for the injection cases, 40 attacks and 12 benign, about EUR 0.12 expected, where the golden recording's 55 chat calls cost EUR 0.12 as measured, and some embedding calls; the owner says yes before any, and the amount is stated first): a real model's answers to the injection cases the screen lets through, recorded beside the golden recording; a model's refusal of a structured request seen from a real provider; retrieval measured with a real embedding, in the evaluation and in S038's one failing check; the judge compared with labels a person wrote for a sample; a held-out set for the injection screen, and a decision on what a false alarm may cost; CLM-0034's `unsure` settled by a prompt or recorded as the right answer; retrieval over a graph measured again only if the synthetic data gains something relational to find | S067 |
| S072 | The cluster outside `meridian`, second round | On kind: the Prometheus and CloudNativePG operators' reach into Secrets and ConfigMaps of every namespace is narrowed or recorded as accepted with its reason; DNS and the collector cannot carry data out unseen (T-84), or the residual is stated; writes to Prometheus and Loki pass a policy, and the three hops behind the collector are encrypted or the plan says why not; egress from `observability` and the admission webhooks' port are bounded; `cnpg-system` and `envoy-gateway-system` have Pod Security labels and a policy; the owner decides whether node-exporter stays off; Tempo mounts no API token | S064, S066 |
| S073 | Renewals, upgrades and what smoke cannot see | On kind: a renewal is seen for the collector's certificate and the database's, and something alerts before the database's end; the services do not all restart in the same minute at a renewal; approver-policy is restarted when it hangs, and a repaired policy does not wait an hour for cert-manager's retry; a first install that fails has a way back that was tried; the chart bounds its rollback history and `make images` says what to remove; the scripts' `kubectl` calls have a request timeout; a manual sweep Job does not hide a stopped schedule; the failure paths of smoke's newer lines are seen once on a cluster with something broken on purpose; the line that reads approver-policy's wording says so when it fails; probes that time out under load have a recorded answer for the machine the cluster runs on now | S064, S066 |
| S074 | Test suite and file sizes | Without the cluster: `infra/kind/smoke.sh`, `test_kind_manifests.py` and the four test files over 800 lines are split along the lines their own tests already cut; the six functions over 50 lines are under it (counted by signature plus body without the docstring, as the section says: by the whole count the row used, four of the six, `assess`, `build_report`, `build_injection_report` and `summarise`, are still over); the template-database fixture survives a test that patches the runner's file list; the tests that failed once under load (a lost connection in a parallel run, a tool server's timeout, the gateway's fallback test) are run repeatedly on the machine the suite runs on now and either hold by construction or are closed as not reproduced, with the numbers; ~~the slowest test of the job is under ten seconds~~ (not met, and not what the step did: the gates' measurement of 2026-10-07 found the slowest test at 45.0 s without coverage, with seven of the ten slowest over ten seconds; the step gives every test a limit of 600 s instead); one CPU-time helper; `unused_port()` on macOS has its answer written down; ~~the owner decides whether CI gates on coverage~~ (answered 2026-10-07: yes, with a file size check and a per-test timeout; built) | S064, S066, S037 |
| S075 | Harness, guard and Renovate | `make docs` notices a blank line that splits a table; the command guard's known gaps to a Secret's values and to superuser SQL are closed or listed where a session reads them, and a hook that times out has a known outcome; a rule for an implementer that edits through the shell, and a guard or a rule for `make up` and `make down` from an old checkout (both the owner's); the workflow linter knows the runner label; Renovate's week of waiting is a required check or the plan says why not (the owner's decision), an image is not proposed before the chart that installs it, and the two pgvector versions are one | — |
| S076 | CLI, scaffold and loader small ends | No registry entry lets the runtime name an agent that no tenant lists without a check saying so (T-81); `services_edit` refuses an alias or a merge key by itself; the scaffold says which write failed and names the line it refuses in every case; the two entry-point loaders answer a bad entry in the same fixed words; the screen's fingerprint covers what it claims to; the workload's report builders refuse another workload's manifest; `meridian registry schemas` answers an unwritable directory with a message; the scaffold can write an agent with workers, or the plan says why a second graph of subgraphs is not built. Built as: `meridian registry validate` prints one NOTE, exit code unchanged, for each graph agent the runtime may name and no tenant lists, and `load_registry` refuses nothing new, so the owner's S061 decision stands; `services_edit` itself refuses an anchor, an alias or a merge key, naming the line; a refusal about a line of the person's file names the parser's line where the parser gives one; a failed or interrupted write names its kind and the error's class, the undo puts back every file that still holds the command's own bytes and names every path it did not restore (the scaffold's write and undo are in `scaffold_writes.py`); the two loaders word a refusal in one table of fixed sentences that quote no distribution's name and no import error's text, with the registry's agent ID in front for the graphs; the screen's digest is unchanged and the documents say what it covers and does not; both report builders compare the manifest's workload with their own; `meridian registry schemas` answers a directory it cannot update with one line; a second graph of subgraphs is not built, as a decision (the section says why). Implemented and tested, not run on a cluster | S037 |
| S080 | File uploads for a claim, before sign-in | A claimant uploads one PDF, JPEG or PNG of at most 1 MiB to a claim, five and 3 MiB to a claim, through the JSON route or the status page's form; the file is told by its first bytes and never by its declared type or name, and its name is not stored; an upload is not a document arrival and starts no triage; the adjuster lists a claim's files with "not scanned" beside each and, behind a second switch, downloads them as an attachment under a sandbox policy, with an audit row before the first byte; both switches are off by default, and the chart allows either only for a route host name that ends in `.localhost`; the app has brakes of its own besides the edge's second route, buffer and rate limit; nothing scans a file (designed, not built) and nothing deletes one. Built as (2026-10-07; implemented and tested against PostgreSQL and **run once on kind on 2026-10-07 with both switches on for that run only** (run RU1: by script, through the edge, not in a browser), nothing paid, no evaluation fingerprint moved, the owner's "Build now" of the same day for the uploads and "Build now, local-only switch" for the download): migration 0032 (`claims.claim_files`, `claims_api` may insert and select), the route and the form, the ceilings under advisory locks, the edge route and policies in the chart, the adjuster's list and download with brakes of their own, synthetic sample files from the seeded generator, and five reviews (database, security twice, FastAPI, platform boundary). Not built: a scanner, a delete or a retention period, a sign-in, a second host name for downloads | S070 |
| S089 | Import layering page; Mermaid rendered in CI | A page in the Documentation tab and the PDF says which Python package may import which, as the six import-linter contracts enforce it, with one Mermaid diagram of the layers and the output of a run; the `derived diagrams` job renders every Mermaid block on every pull request and fails on one that does not parse. Built as (2026-10-07, the owner's "okay do it and open pr"): `docs/architecture/code/import-layering.md`, symlinked into `overview/` as `40-import-layering.md`, and one step in `.github/workflows/docs.yml`. Implemented as a document and a gate; it is not a component view, which is S090 | — |
| S090 | Component view of the Agent Runtime | A component view in the Structurizr model answers which responsibilities sit inside the Agent Runtime and which of them is the only way out to a model, a tool and the database: components by responsibility and not one per file, with a register row and a PNG read at full size, and no other view changed. Built as (2026-10-07, the owner's "Component view (Recommended)", which drew it from today's code and took away the wait for S083): `RuntimeComponents`, six components (Run API, Run Records, LangGraph Host, Agent Framework Host, Model Client, Tool Client) in `docs/architecture/model/components.dsl`, 12 boxes and 15 arrows, embedded in the import layering page. Implemented as a view read from the code; nothing was run for it. The render under rootless Docker went to S092, and S082 brings the page and the view to the code it moves (a backlog row). The Model Gateway gets a view only when a question needs one | — |
| S091 | Data ownership views | Who reads and writes which schema of the Platform Database, as views of the model: the six schemas (`audit`, `claims`, `gateway`, `knowledge`, `policy`, `runtime`) as components of the database, each service with its own schema and the reads that cross a schema in one view, and the audit trail in a second if one view does not read; every arrow checked against the grants the migrations leave and compared with ADR 10's table, a difference recorded and not smoothed; register rows and PNGs read at full size. No ER diagram: S085 and S087 rewrite the tables. The owner, 2026-10-07: "should we add db view too?", then "okay" to this. Built as (2026-10-07): `DataOwnership` (11 boxes, 13 arrows, the seven grants on another service's schema drawn thicker) and `AuditTrail` (9 and 8), in `docs/architecture/model/data.dsl`, embedded in the data classification's inventory. The grants were read from the migration files' statements, not from a database's catalog; they agree with ADR 10's table. Implemented as views; nothing was run for them | S090 |
| S092 | Mermaid and PDF render under rootless Docker | `make mermaid-render` and `make pdf` finish on a machine whose Docker is rootless, as the virtual machine's is, and still finish in CI; the fix is made in development-base's copy of `scripts/render-mermaid.sh` first and copied here unchanged; with it, the two notes of S089's review (the render container needs no network; the script passes when it finds no block). Built as (2026-10-08): under rootless Docker the render and Pandoc containers start as their root, which there is the caller, and elsewhere as the caller, as before; the render container has no network; this repository's `make mermaid-render` fails when no block was extracted. Development-base's pull request 52 first, then the two scripts and a test copied byte for byte. Implemented, and run on the virtual machine: five diagrams rendered and a PDF of 377 pages written | — |
| S095 | Retention and erasure of uploaded files | The owner's decision of 2026-10-08: "Both, as a new step (Recommended)". A retention period for a claim's uploaded files is a setting, and it has no default that deletes anything: a local cluster deletes no file until the operator sets a period; a sweep deletes a file's BYTES once the period has passed, under a database role that may (a grant, so a migration; `claims_api` keeps SELECT and INSERT and no DELETE); an audited command erases one claim's files on request, run by a signed-in person (hence S021) whose name is on the audit row; the metadata row and the audit rows stay and say what was removed and when; a legal hold on a claim stops both the sweep and the command; the download of a file that is gone answers with a clear refusal, not a 404 of no route and not a 500; tested against PostgreSQL and seen once on kind. **Designed: nothing of it is built.** Open for the step's design: how the row keeps its size and hash while its bytes go (the table checks them), and the same bytes in the write-ahead log and in every backup, which the step's erasure does not reach and its design must say how long they live (T-111) | S080, S021 |
| S096 | The PDF: no row lost, and a brief edition | The architecture PDF loses no text where a table row is taller than a page, and a second, brief edition exists to hand to someone who will not read a register. Built as (2026-10-08; the owner's "do it" and "Records (Recommended)"): a table with a cell of more than 300 characters prints as records, one block of paragraphs per row, and any other wide table gets its column widths from its text (`scripts/pdf_tables.py`); `make pdf-brief` writes the brief, without the documents `docs/architecture/pdf-brief.txt` lists (the threat model and the Azure platform register), with the decisions as an index and a first page that says what it leaves out; the workflow builds both and attaches both to the release. Measured on the virtual machine: the full edition from 377 pages to 274 and from 117 pages with text past the bottom margin to none; the brief is 34 pages. The code is development-base's (its pull request 54), copied. Landscape pages for the threat model, the session's first proposal, were built and saved nothing (379 pages). Implemented; no release was published | S092 |
| S097 | The plan in files | Each step's section and each change-log entry is a file of its own under `docs/plan/`, moved byte for byte and proved so by `scripts/plan_split.py --check`; a new entry is named for its pull request's number and the plan has no version of its own; `make docs` checks the files against Part B and refuses a section or an entry in the old place; `scripts/plan_port.py` carries a branch's edits of the old layout over. Part B's tables are not moved and still collide by rows (the owner, 2026-10-08: "Change log and step sections") | — |
| S098 | Demo claims on kind | The owner, 2026-10-08, asked to "fill up the admin and non-admin side with tenants" and chose "Users, one organisation": test people to sign in with and seeded claims so the pages show content, in one insurer. This step is the claims: `make demo-seed` posts the first `COUNT` (40, at most 47) synthetic claims through the edge one at a time and prints a line a claim and the counts by state; kind only, through the API only, the model is simulated (replay), safe to run twice, decides nothing; and it has run once on the kind cluster with the smoke run after it. Not here: a second insurer, a sign-in, claimants' accounts (S093), the cast of test staff (S021, where the realm generator lives) | S080 |
| S099 | A dispatcher and workers: the test lock and the two briefs | Steps run in worker sessions, one step and one worktree each, handed out by a dispatcher session (the owner, 2026-10-08: "yes and you will be the orchestrator this way"); `make pytest`, `make pytest-db` and `make alerts` take one lock for the machine (`scripts/machine_lock.sh`), so a second session's run waits for the first and says who holds it, and one that waited too long runs nothing; the two roles are skills (`plan-dispatcher`, `plan-worker`) that name no path of the machine; Part A's "A dispatcher and workers" replaces "One step at a time, again"; the same goes into the development base in a pull request there | — |
| S100 | The plan's layout: step folders, the backlog in files, the change log ended | The step files sit in folders of twenty step numbers under `docs/plan/steps/` (the owner, 2026-10-08: "range the step for at 20, okay"), moved with `git mv`; the follow-up backlog is out of the plan file, its open rows in `docs/plan/backlog.md` and its closed rows in `docs/plan/backlog-closed.md` ("do the backlog seperation"), every row in exactly one of the two, byte for byte, proved by count and by digest; the change log ends and its 105 files leave the tree ("Stop and delete the old"), and Part A says what a pull request's description must hold instead; `make docs` checks the folders, holds each backlog row to its file by its status and refuses a change-log folder, with no import of a migration script (`plan_split.py` and `plan_port.py` are removed); every link resolves; the development base follows in a pull request there | — |
| S101 | The plan shows no status: a step's status is in its file, and the plan's last part lists the finished steps and those in flight | Part B's step tables have no Status column and their "Done when" cells are as they were (the owner, 2026-10-08: "it should show us the plan itself without any modification so no todo and done and other"): each of the 100 rows equals its old row with the status cell put back, byte for byte; the 15 status cells that held a note are whole in their step's file, as "Where it stands"; the plan ends with Part F, whose "Finished steps" table and, under it, "In flight" table ("in-flight at the end of the plan would be good under the finished job") are written by `make plan-progress` from the status line of each step's file, every step that has a file in exactly one of the two and linked to its file; `make docs` fails on a table that is not what the files say, on a status line of another form and on a Status column put back; the running "Status:" paragraph is in Part F and Part C's hand-kept index is gone, so a step that starts or finishes changes its file and Part F and nothing above; Part A says how a step opens and closes, when a step is free and how a conflict in the two tables is settled; `make docs` and `make test` pass; the development base follows in a pull request there | S100 |

### Toward services: a database each and six images

Made on 2026-10-07 from the owner's answers of the same day (S081's section,
and [ADR 10](architecture/decisions/0010-split-the-platform-into-services-with-a-database-each-and-their-own-releases.md)):
a database per service on one PostgreSQL server, five databases, six images
with independent versions, the tool servers trusting the authenticated caller,
the audit trail as an outbox per service, a fresh baseline per database, and
the building after the steps in flight. These steps are the building. They
are **designed**: nothing in S083 to S088 exists, and of S082 only its first
two moves do (implemented and tested, not deployed: S082's section); S081 is
the record that decides what they are. S080 is kept for the uploads step,
which another branch adds.

- **"Depends" on the steps in flight** means their pull requests are merged,
  not that the steps are `done`: S020 and S069 are not `done` when their pull
  requests merge, because S020's apply and S069's R12 wait on the owner.
  ~~S082's row names S020 and the range S069 to S074 as the owner's "the
  steps in flight" was read. S071 is in that range and its paid run waits on
  the owner's yes: S082 waits for the pull request of S071's free half (it
  changes the tests' support code the split moves), not for the paid run,
  which is a measurement and changes no service.~~ Superseded on 2026-10-07
  (S082's section): that list could never clear, since S020, S073 and S074
  stay `doing` for a long time. S082's row now names S081 and the four steps
  that were in flight on 2026-10-07 (S071's free half, S072's client
  certificates, S080 and S020's code half), merged; S071's paid run is still
  not waited for. This is the session's reading; the owner may say otherwise.
- **Two checkpoints for the owner:** after S083 the images half of the choice
  is delivered (six images, on one database, with no independent release safe
  before S088's contracts); after S087 the data half is.
- **Lanes (Part A):** S083 and S087 change what the cluster runs, so each is
  the cluster lane's. S085 adds migrations and S087 replaces the migration
  tree: the database lane's. S084 may add or change a tool's contract and then
  moves an evaluation fingerprint. S083 and S084 may run side by side.
- **The owner's two answers of the sixth round** are in Part D's 7 (the audit
  outbox, for S085) and 8 (a fresh baseline per database, for S087). What they
  leave open is the steps' own design: the gateway's audit row, how the
  adjuster's page reads the central trail, and where the archive lives.

| ID | Step | Done when | Depends |
|---|---|---|---|
| S081 | The decision record for the move toward services | [ADR 10](architecture/decisions/0010-split-the-platform-into-services-with-a-database-each-and-their-own-releases.md) is accepted and indexed: it says what the owner decided (five databases on one server, six images with independent versions, the tool servers trusting the authenticated caller with the lost double check accepted as a risk, the audit trail as an outbox per service, a fresh baseline per database, the building after the steps in flight) apart from the session's own design, lists the ten couplings with the step that replaces each, what stays shared, the consequences and the two checkpoints; this table exists and Part D's questions 7 and 8 are answered; `make docs`, `make check` and `make test` pass. Nothing is built. `doing` until the pull request is merged, then `done` | — |
| S082 | Code in the wrong place moves; import contracts per service | With no change in behaviour and no assertion of an existing test changed: the claims tool server leaves the claims workload's package, the Claims API no longer imports the runtime's models or its sweep's constants (the models both sides use sit in a module of their own), the sweep's runtime SQL sits with the runtime, and the workloads' graphs sit in a package of their own; an import contract keeps each service's package from importing another service's; `make lint`, `make pytest` and `make smoke` pass. Designed | S081, and the four steps that were in flight on 2026-10-07 merged (S071's free half, S072's client certificates, S080 and S020's code half): the session's reading of the owner's words, see the section |
| S083 | Six packages, six images, a tag per service | A `uv` workspace holds a common library (`common`, `registry` and `guardrails`), the tool-server library and one package per service, each with its own dependencies and version; the runtime's image installs the graphs' package; the jobs ship with the service that owns their data; one build file makes six images; the chart takes a tag per service and `make deploy` builds and loads all six; `make demo` and `make smoke` pass on kind with six images on one database. First checkpoint: the images half of the owner's choice. Designed | S082 |
| S084 | The tool servers take the binding from the caller; two reads become calls | A tool server takes a call's run, agent, tenant and claim from the Agent Runtime's authenticated call and reads neither `runtime.runs` nor `claims.claims`; a call without a binding is refused; the knowledge server asks the policy server for a policy's wording version and the policy server asks the claims tool server for a policy's other claims, each over mutual TLS under a registry entry, a chart value and a NetworkPolicy; ADR 4's one-caller rule is changed for the two paths and the change recorded; T-22 is rewritten with the accepted risk, stating what the double check caught and what is left. Designed | S082 |
| S085 | The audit outbox, the relay and the central trail | Each service writes its audit row into an audit table of its own in the transaction of its business write, with the insert-only and stamp triggers; a relay copies the rows into a central audit table that owns retention and serves the adjuster's trail by claim; a relay that lags or stops is seen by an alert, and the services keep writing; the gateway's audit row is settled in the design and said (in the ledger's transaction, or kept apart); built inside the one database, as a table per schema (the owner's decision, Part D's question 7). Designed | S082 |
| S086 | The sweep in two | The runtime sweeps its own runs and checkpoints under its own role, and the claims side asks the runtime for a run's state and moves its own claims; no statement spans runs and claims; each half is safe to run twice, and a test stops a pass between the halves; whether the claims side has an identity of its own towards the runtime is decided and recorded. Designed | S082 |
| S087 | Five databases | `claims`, `runtime`, `gateway`, `policy` and `knowledge` each have their own migration tree and ledger from a baseline, with no replay (the owner's decision, Part D's question 8); the 32 files and their 60 test files are archived outside the package's path (31 and 59 until S080's 0032 and its test; S082's section says how they were counted); each role and the server's rules name one database; kind, the tests' template databases, smoke, the runbooks' queries and the database lists of the Azure and AWS modules follow; the audit database exists; `make up`, `make smoke` and `make demo` pass on a recreated kind cluster. Second checkpoint: the data half of the owner's choice. T-25 is rewritten. Designed | S083, S084, S085, S086 |
| S088 | Contracts, versions and independent release | A committed, versioned contract for the gateway, the runtime and the Claims API's routes, each side tested against it, and a rule for how long an old version is served; the registry directory has a version of its own; CI builds, tests what changed and publishes the six images to a place the step chooses and records; one service is released alone while the others stay on their versions, and the run is recorded. Designed | S083, S087 |

### M3 — Reliability and operations

| ID | Step | Done when | Depends |
|---|---|---|---|
| S027 | Load test and SLO thresholds | A load test measures latency and error rate; SLO thresholds set from the measurements; an error-budget panel | S026 |
| S028 | Game day | Provider outage, budget exhaustion and database failure exercised; INC-001 written from the real timeline; rollback exercised | S027 |
| S029 | Backup and restore drill | PostgreSQL restored into a scratch environment; restore time measured and recorded | S020 |
| S030 | Provider change without breaking consumers | A model version swapped by a registry change only; consumer contract tests stay green; the evaluation compares both versions | S017, S023, S050 |
| S031 | Supervisor and workers | Triage split into a supervisor and workers with per-worker tool allowlists; the evaluation shows no regression | S017 |
| S032 | Injection evaluation suite | Prompt-injection cases in retrieved content and claimant text; guardrail effectiveness measured in the harness | S017, S047 |
| S033 | Read-only platform console | Four pages: registry with residency, tenants with budgets and usage, evaluation runs, audit search | S011, S021 |
| S034 | Governance documents | Provider onboarding process and service acceptance checklist, applied to the reference workload | S024 |
| S035 | M3 exit | Architecture PDF released; demo script v2; every capability labelled | S028, S033, S034 |

### M4 — Optional, at most one

S039 took the one. The owner lifted the limit for S038 on 2026-10-06
(its section says how), for S037 the same day ("We can do s037 if we
can now") and for S036 with it ("add the aws template too and we will
test it in a real aws enviroment"): all four are built or to be built.
S078 was added the same day, with S077 in M2, for Google Cloud, and S079
after it for a cluster that is not managed ("Managed and self-managed
Kubernetes" above).

| ID | Step | Done when | Depends |
|---|---|---|---|
| S036 | AWS Terraform, applied once | ~~The module passes `terraform validate` and a policy scan; it is never applied~~ Changed by the owner on 2026-10-06 ("add the aws template too and we will test it in a real aws enviroment"). Two halves. Without an account and without cost: the module for what S025 maps (network, cluster, registry, database with pgvector, workload identity to a secret store) in an EU region passes `terraform validate` and a policy scan, and one command each creates and removes it. With the owner, in the owner's AWS account, after the cost of an hour of it is stated and the owner says yes: it is applied once, what came up is recorded, it is removed, and the run's cost is logged. ADR 1 ("design AWS") gets a dated successor or note that says so. Since the owner's decision on managed and self-managed Kubernetes the same day, the apply of this managed cluster is asked at the paid stop of S079 and may be answered no; the first half is unchanged. First half built as (2026-10-07; implemented as code, checked without an account, never applied; nothing ran in AWS): `infra/terraform/aws/` is a module for a VPC of two public subnets with no NAT gateway, an EKS cluster at Kubernetes 1.36 with one managed node group of two `t3.large` and the Pod Identity and EBS CSI add-ons, one ECR repository, an RDS for PostgreSQL 17 instance on `db.t4g.small` whose password RDS keeps in Secrets Manager, one empty secret with a role that one named service account may assume, and a USD 25 monthly budget, in an EU Region (a validated variable, `eu-central-1` by default) with a local state under the owner's home and the provider pinned to one account; `infra/terraform/aws.sh` and five `make aws-*` targets validate, scan, plan, apply a saved plan and remove, and the scan is Trivy's from an image pinned by digest with three accepted findings, each with its reason; the command guard and the settings know the commands (T-100), and ADR 1 has a dated note. Not built in the first half: the `vector` extension (it needs a connection to the database), the Meridian chart and its controllers on the cluster, and the module in CI (S022) | S025 |
| S078 | GCP Terraform, ~~applied once~~ a scaffold only | As S036, for Google Cloud (the owner, 2026-10-06: "like aws too"). ~~Two halves.~~ Without a project and without cost: the module for what S077 maps (network, cluster, registry, database with pgvector, workload identity to a secret store) in an EU region passes `terraform validate` and a policy scan, and one command each creates and removes it. ~~With the owner, in the owner's Google Cloud project, after the cost of an hour of it is stated and the owner says yes: it is applied once, what came up is recorded, it is removed, and the run's cost is logged.~~ Changed by the owner on 2026-10-06 ("gcp just scafold"): the module is never applied; whether a scaffold carries commands that create and remove it at all, or a README that says how the owner would, is its design's to say. The successor or note to ADR 1 ~~that S036 writes~~ that S078 writes (corrected 2026-10-07: S036's first half wrote no second note and left it to its second half) names this cloud too. Built as (2026-10-07; implemented as code, checked by `terraform validate` and an offline Trivy scan, never planned and never applied; nothing ran in Google Cloud and no project exists): `infra/terraform/gcp/` is a module for a VPC with one regional subnet and Cloud NAT, a GKE Standard zonal cluster with Dataplane V2, workload identity and private nodes, one Artifact Registry repository, a Cloud SQL for PostgreSQL 17 instance in the Enterprise edition reached by Private Service Connect, one empty regional secret with one workload-identity binding and a budget in the billing account's own currency, in an EU Region (a validated variable of eleven, `europe-west3` by default) with the project pinned by a precondition that no run has seen refuse; `make gcp-validate` and `make gcp-scan` check it, the scan being the AWS scan's image with the same flags and accepting nothing. By design no command plans, applies or removes it, and the command guard and the settings are unchanged (the owner's "gcp just scafold"); the module's README says how the owner would apply it by hand, nobody having done so, and what would have to be built first; ADR 7 and ADR 1 each have a dated note | S077 |
| S079 | Self-managed Kubernetes: applied once on AWS, a scaffold on Google Cloud | The owner, 2026-10-06 ("we will build it on aws, gcp just scafold"). A cluster whose control plane the owner's account runs itself, on the cloud's virtual machines, beside the managed cluster of S036 and S078. Two halves. Without an account and without cost: a Terraform module for AWS beside S036's, with a small network of its own, that reuses S036's wrapper script, scan and the lessons of its reviews, and brings up the control plane and the workers with an installer the step's design chooses and says why (its threat note first: the cluster's certificates, its join token and its etcd are then the owner's to keep); its twin for Google Cloud; both pass `terraform validate` and a policy scan, and for AWS one command each creates and removes it. ADR 6, ADR 7 and the Azure platform document each gain the comparison of a managed and a self-managed cluster on that cloud: who runs and upgrades the control plane, where etcd and its backup live, how a pod gets a cloud identity, how a load balancer and a volume are made, what an hour costs, and why the platform's default stays managed. With the owner, in the owner's AWS account, after the cost of an hour of it is stated and the owner says yes: the owner applies it once from where no agent session holds credentials, what came up is recorded, it is removed, and the run's cost is logged; whether S036's managed cluster is applied as well is asked then. The Google Cloud twin is never applied. First half built as (2026-10-07; implemented as code, checked without an account, tested with stand-ins and never planned or applied; nothing ran in AWS or Google Cloud and no instance booted): `infra/terraform/aws-kubeadm/` is a module for a VPC with one public subnet and no NAT gateway, one control-plane instance and two workers (`t3.medium`, Ubuntu 24.04 from the publisher's public parameter) brought up by kubeadm from two boot scripts, Calico from a manifest pinned by version and SHA-256, and the join command passed through one write-only Parameter Store parameter, with no key pair, no port 22 and no secret in user data or in the state; `infra/terraform/aws.sh` plans, applies and removes it by the word `aws-kubeadm`, with a state, a saved plan and a record of its own and a redaction that knows instance identifiers, and `make aws-kubeadm-validate` and `make aws-kubeadm-scan` check it (the scan accepts three findings, each with its reason), while no `make` target plans, applies or removes it yet; the `python` workflow installs a pinned Terraform so that the 225 Terraform-marked tests run on the runner (not yet seen there); `infra/terraform/gcp-kubeadm/` is its twin on Compute Engine, checked by `make gcp-kubeadm-validate` and `make gcp-kubeadm-scan` (one finding accepted with its reason) and by a test that holds the boot scripts' common parts equal to the AWS module's, with no command that creates it; ADR 6, ADR 7 and the Azure platform document hold the comparison of a managed and a self-managed cluster, written from what was built; T-102 is new. Three reviews of the module found one critical and two high findings (the critical one would have failed the first apply after resources existed), all closed; a fourth, of the wrapper's second module, found no critical or high finding and two medium, which two contracts answer | S036, S078 |
| S037 | Second-framework workload | A small workload in Microsoft Agent Framework on the same platform contract. Built as: a second host behind the Agent Runtime's `Host` protocol, picked for each agent by the registry's `host` field, with a PostgreSQL checkpoint store of its own, and a second workload, `claim-brief`, that calls tools, pauses for an adjuster and is started by the Claims API; implemented, tested, and seen on kind once under replay (ADR 9, the second applied service acceptance) | S005, S018 |
| S038 | GraphRAG spike | A small knowledge graph of customer, policy, asset and claim; retrieval compared with hybrid search | S012 |
| S039 | Workload scaffold | `meridian workload new` generates a workload that passes registry validation, the import contract and an empty evaluation on its first run | S018 |

### Follow-up backlog

Follow-ups that no step's "done when" covers are rows of a table that
left this file on 2026-10-08 (S100; the owner: "do the backlog
seperation"). The open rows are in [plan/backlog.md](plan/backlog.md),
each naming the step that will take it; the closed rows are in
[plan/backlog-closed.md](plan/backlog-closed.md), as history. A step
that opens takes its rows into its own "done when". A row that closes
moves, whole, from the first file to the end of the second; a row closed
in part stays. `make docs` refuses a table under this heading and holds
each row to its file by its status.

## Part C — Step details

Each step has a file of its own, and its `### S0NN — <title>` heading
is the file's first line. The files sit in folders of twenty step
numbers under `docs/plan/steps/` (S100; the owner, 2026-10-08: "we
have too much file in the plan folder in one folder", and "range the
step for at 20, okay"): a folder is named for the first and the last
number it may hold, `S000-S019`, `S020-S039` and so on, so S100's file
is `docs/plan/steps/S100-S119/S100.md`, and a folder is made when its
first step starts. A step that starts gets a file from the template
below; Part F lists the files by what their status lines say (this part
held an index kept by hand until S101). Nothing in this part holds a
step's section, and no step file lies outside its folder: `make docs`
refuses both and says where the file belongs. Template:

```text
### S0xx — <title>
**Status:** doing · **Started:** YYYY-MM-DD · **Finished:** —
**Goal:** one sentence.
**Decisions:** bullets, with the alternative rejected and why.
**Advisor:** each consultation: when, at which point (the design, a
surprise, a change of scope or order, before a paid or irreversible
action, before the pull request and why), and what it changed, or that
it changed nothing; or that it was not consulted.
**Work log:** what was actually done, commands, links to PRs.
**Result / verification:** how we proved it is done.
**Follow-ups:** new steps or issues this created; those no step covers
also go into the follow-up backlog, `docs/plan/backlog.md`.
```

## Part D — Open questions

| # | Question | Needed by | Default if unanswered |
|---|---|---|---|
| 1 | How many hours per week, and when do interviews start? | S002 | Plan in two-week increments; cut M3 before M2 |
| 2 | Terraform state: HCP Terraform, as in the homelab, or an Azure Storage account? **Answered 2026-09-30: Azure Storage** in Sweden Central with Entra ID authentication (S007) | S007 | ~~HCP Terraform, for consistency with the homelab~~ |
| 3 | A claim whose documents miss the deadline is closed as rejected without a human. Keep that, or route it to the adjuster? **Answered 2026-10-03: route it to an adjuster**, with the reason that its documents are overdue; no claim is rejected without a person | ~~S015~~ ~~S048~~ S052 (moved with the deadline, 2026-10-03) | ~~Keep, recorded as a procedural closure in C-02~~ |
| 4 | Licence: keep all rights reserved, or publish under MIT or Apache-2.0? **Answered 2026-09-29: Apache-2.0**, copyright Dezoxy; `NOTICE` credits the MIT-licensed ECC material | Before anyone asks to reuse the code | ~~All rights reserved~~ |
| 5 | Should Meridian live in a dedicated work tenant instead of the trial account's default directory? It decides where S021's sign-in, roles and app registrations are created, and moving later means recreating the foundation. **Answered 2026-10-07: stay in the trial's tenant** (the owner first said "Move now"; a trial cannot create a tenant, and after the facts: "Stay in the trial's tenant after all"; S020, ADR 11) | S021, and the upgrade to pay-as-you-go by about 2026-10-30, which is already an account change | ~~Stay in the trial account's tenant; decide at the upgrade~~ |
| 6 | Should a session be stopped from editing the command guard's own files? The permission rules allow Edit and Write on `.claude/hooks/guard-bash.sh` and `.claude/settings.json`, so a session can weaken the guard that reads its commands (N4 of the third security review). Two ways: deny Edit and Write on `.claude/hooks/**` and `.claude/settings*.json`, or ask before each. The session recommends asking: a deny would also stop a session from fixing the guard when a review finds a hole, as S075 did after each of its three reviews, while an ask puts the edit in front of the owner | S075's pull request, if the owner wants it built there; no step needs it | Neither is built: the guard stays a guard for habits, and the gap is listed in the runbook and in the hook's header |
| 7 | Should the audit trail become an outbox when the services get a database each? An audit table in each service's database, written in the same transaction as the business write (so "no action without its row" holds for every service that has a database), and a relay that copies the rows into a central audit database, which owns retention and serves the adjuster's trail. **Answered 2026-10-07 (the sixth round, about 15:30 UTC): "Outbox per service (Recommended)"**, the session's recommendation, chosen as written ([ADR 10](architecture/decisions/0010-split-the-platform-into-services-with-a-database-each-and-their-own-releases.md), point 5). The alternatives not taken: one shared audit database (the audit row can no longer commit with the business write: the guarantee weakens or every write becomes a two-step exchange) and audit through the log pipeline (the trail the adjuster reads would rest on a log store). Its costs: a relay to run, a trail that lags by the relay's lag and stops growing if the relay dies, and a sixth database on the server. The Model Gateway already writes its audit row on a separate connection, so under the outbox it either keeps that or joins its ledger's transaction, which S085's design says: the one part of the question the answer does not settle | S085's design | ~~The outbox, as ADR 10 recommends, with the gateway's choice settled in S085's design and shown to the owner~~ |
| 8 | Should each of the five databases start from a baseline with no replay of the 31 migration files, the old files and their 59 test files archived outside the package's path? **Answered 2026-10-07 (the sixth round, about 15:30 UTC): "Fresh baseline per database (Recommended)"**, the session's recommendation, chosen as written (ADR 10, point 6). Honest only while no environment holds data: the kind cluster is disposable and the Azure database has never been created. A split with data would need expand, copy, switch and contract instead, and a replay would mean rewriting 13 files that mix schemas, which an applied file's rule forbids. The cost: a large deletion from the tree and a change in the plan's count of migrations | S087's design | ~~Baselines with no replay, as ADR 10 recommends, asked again at S087 if any environment then holds data~~ |

## Part E — Changelog

The plan's change log ended on 2026-10-08 (S100). It was one entry a
pull request: in this part until S097, then a file an entry under
`docs/plan/changelog/`, 98 named for the plan's version (`v0.01.md` to
`v0.98.md`) and 7 for a pull request (`pr-0137.md`, and `pr-0144.md` to
`pr-0149.md`). The owner asked "the changelog makes sense here or the
github owned prs enough?", was offered "Stop, keep the old
(Recommended)", "Keep as it is" and "Stop and delete the old", and
answered "Stop and delete the old".

So the 105 files left the tree, and they are in git's history up to the
commit `093fd8b`: `git ls-tree --name-only 093fd8b docs/plan/changelog/`
lists them and `git show 093fd8b:docs/plan/changelog/v0.57.md` prints
one. A step file that names an entry ("v0.57", "the change-log entry")
means those files. From S100's pull request on, the record of a change
is its pull request's description, which the squash commit carries into
`git log` (Part A, "The pull request's description"). `make docs`
refuses an entry in this part and a `docs/plan/changelog` folder.

## Part F — Where the steps stand

The parts above are the plan as it was written: no step table holds a
status (S101; the owner, 2026-10-08: "it should show us the plan itself
without any modification so no todo and done and other, and at the end
it should order the done jobs that is linked into the plan steps folder
files"). A step's status is one line in its own file, and this part
shows it: the two tables at its end, "Finished steps" and "In flight",
are written from those lines by `make plan-progress` and held to them
by `make docs`, so nobody types into them (Part A, "A step's status").
A step that starts or finishes changes its file and this part, and
nothing above.

### Where the project stands

One running paragraph, at the top of this file until S101. A step that
changes what exists says so here.

**Status:** bootstrap, 2026-10-04. The architecture model, the first decisions,
the engineering harness, a local platform on kind, the Azure foundation, the
platform registry and a walking skeleton of the Claims API, the Agent Runtime
and the Model Gateway exist; the skeleton runs on kind with `make demo`, from
one hardened Helm chart under a default-deny network policy (S019), where the
runtime, the gateway and the tool servers know the calling service from its
certificate (mutual TLS, S055) and refuse a tenant or agent the registry does
not let it name, the issuer signs only for the services' namespace and a service
asks for its own restart once a renewed certificate is mounted (S056), the
gateway routes a call to Azure OpenAI by data class and residency from a laptop,
falls back to a second deployment in the same region and holds each tenant to
its rate limits (the windows shared by its pods through a Redis on kind, S066)
and budgets (a Grafana dashboard on kind shows what each tenant, agent, model
and provider used), it answers embedding requests under the same controls (in
replay mode and against Azure from a laptop), three MCP tool servers and the
runtime's client for them run on kind, where the policy wordings are ingested
into pgvector and searched through one of those servers (with a simulated
embedding), a triage graph calls the tools in a fixed order and lets rules
decide each claim's route (a real model answered its one question, asked for by
schema, for the golden set from a laptop; on kind the model is simulated; the
runtime hosts a second agent framework behind the same protocol for a small
second workload, a claim brief that an adjuster decides (S037, seen on kind once
under replay); claimant text that holds special-category data or addresses the
model is not sent, and identifiers are redacted before any model call and in
logs), a claim it refers to an adjuster waits with its run paused in PostgreSQL
until the adjuster decides it on a server-rendered page that says whether the
recommendation it shows rests on a model's reading or on the rules alone (S070,
tested, not seen on kind) and the Claims API records the decision and resumes it
(or sends the claim back to triage), a claim whose triage failed is decided by
an adjuster, a claim can be withdrawn or get the documents it was asked for, at
most five triages each, a scheduled sweep refers a claim whose documents are
overdue to an adjuster and cleans up what a failed request left behind, a
claimant submits a claim (the API stamps its report date, and a decided or still
open claim counts in the policy's claim history), reads its status, reports
documents and withdraws it on server-rendered pages that say nothing of the
proposal (no sign-in yet), the adjuster's pages and the decision, triage and
brief routes demand a staff sign-in with the adjuster role when a switch that
is off by default is turned on (S021: seen on kind against a mock issuer,
never in a browser, and `make demo` leaves it off),
`make demo-seed` fills the adjuster's queue and the
claimant's lookup on kind with the first forty synthetic claims, deciding none
(S098), CI grades the golden set's proposals with rules and
an LLM judge against a reviewed baseline (the model's answers recorded from
Azure OpenAI and replayed through the gateway), alert rules, a health dashboard
and five runbooks exist as files, applied to the kind cluster and none
exercised, with service level objectives nobody has measured (S024), and no
service runs in Azure yet.

<!-- plan-progress: begin (written by "make plan-progress", never by hand) -->

### Finished steps

| Finished | Step | Title | File |
|---|---|---|---|
| 2026-09-29 | S000 | Plan, harness and architecture bootstrap | [S000.md](plan/steps/S000-S019/S000.md) |
| 2026-09-29 | S001 | Commit and publish | [S001.md](plan/steps/S000-S019/S001.md) |
| 2026-09-29 | S002 | Python workspace and CI gates | [S002.md](plan/steps/S000-S019/S002.md) |
| 2026-09-29 | S003 | Synthetic data and golden set | [S003.md](plan/steps/S000-S019/S003.md) |
| 2026-09-29 | S004 | Security and quality registers | [S004.md](plan/steps/S000-S019/S004.md) |
| 2026-09-30 | S005 | Agent framework spike | [S005.md](plan/steps/S000-S019/S005.md) |
| 2026-09-30 | S006 | Local platform on kind | [S006.md](plan/steps/S000-S019/S006.md) |
| 2026-09-30 | S007 | Azure foundation | [S007.md](plan/steps/S000-S019/S007.md) |
| 2026-09-30 | S008 | Platform registry | [S008.md](plan/steps/S000-S019/S008.md) |
| 2026-09-30 | S009 | Walking skeleton | [S009.md](plan/steps/S000-S019/S009.md) |
| 2026-09-30 | S040 | Harness refresh | [S040.md](plan/steps/S040-S059/S040.md) |
| 2026-10-01 | S010 | Gateway routing | [S010.md](plan/steps/S000-S019/S010.md) |
| 2026-10-01 | S011 | Gateway budgets and cost | [S011.md](plan/steps/S000-S019/S011.md) |
| 2026-10-01 | S013 | Policy and claims MCP servers | [S013.md](plan/steps/S000-S019/S013.md) |
| 2026-10-01 | S041 | Walking skeleton on kind | [S041.md](plan/steps/S040-S059/S041.md) |
| 2026-10-01 | S042 | Gateway resilience | [S042.md](plan/steps/S040-S059/S042.md) |
| 2026-10-02 | S012 | Knowledge and retrieval | [S012.md](plan/steps/S000-S019/S012.md) |
| 2026-10-02 | S014 | Triage graph | [S014.md](plan/steps/S000-S019/S014.md) |
| 2026-10-02 | S044 | Tool servers on kind | [S044.md](plan/steps/S040-S059/S044.md) |
| 2026-10-02 | S045 | Gateway embeddings | [S045.md](plan/steps/S040-S059/S045.md) |
| 2026-10-02 | S046 | Knowledge MCP server | [S046.md](plan/steps/S040-S059/S046.md) |
| 2026-10-03 | S015 | Human approval | [S015.md](plan/steps/S000-S019/S015.md) |
| 2026-10-03 | S016 | Adjuster UI | [S016.md](plan/steps/S000-S019/S016.md) |
| 2026-10-03 | S017 | Evaluation harness | [S017.md](plan/steps/S000-S019/S017.md) |
| 2026-10-03 | S043 | Gateway cost panel | [S043.md](plan/steps/S040-S059/S043.md) |
| 2026-10-03 | S047 | Guardrails | [S047.md](plan/steps/S040-S059/S047.md) |
| 2026-10-03 | S048 | Claim lifecycle, the rest | [S048.md](plan/steps/S040-S059/S048.md) |
| 2026-10-03 | S049 | Claimant pages | [S049.md](plan/steps/S040-S059/S049.md) |
| 2026-10-03 | S051 | Structured outputs | [S051.md](plan/steps/S040-S059/S051.md) |
| 2026-10-03 | S053 | The claimant's word checked | [S053.md](plan/steps/S040-S059/S053.md) |
| 2026-10-03 | S054 | Parallel tests | [S054.md](plan/steps/S040-S059/S054.md) |
| 2026-10-04 | S018 | M1 exit | [S018.md](plan/steps/S000-S019/S018.md) |
| 2026-10-04 | S019 | Hardened Helm charts | [S019.md](plan/steps/S000-S019/S019.md) |
| 2026-10-04 | S024 | Operations baseline | [S024.md](plan/steps/S020-S039/S024.md) |
| 2026-10-04 | S032 | Injection evaluation suite | [S032.md](plan/steps/S020-S039/S032.md) |
| 2026-10-04 | S039 | Workload scaffold | [S039.md](plan/steps/S020-S039/S039.md) |
| 2026-10-04 | S050 | Live evaluation | [S050.md](plan/steps/S040-S059/S050.md) |
| 2026-10-04 | S052 | Scheduled sweep | [S052.md](plan/steps/S040-S059/S052.md) |
| 2026-10-04 | S055 | Service-to-service identity | [S055.md](plan/steps/S040-S059/S055.md) |
| 2026-10-04 | S057 | Test and tooling hygiene | [S057.md](plan/steps/S040-S059/S057.md) |
| 2026-10-04 | S058 | Gateway loose ends | [S058.md](plan/steps/S040-S059/S058.md) |
| 2026-10-05 | S056 | Certificate lifecycle | [S056.md](plan/steps/S040-S059/S056.md) |
| 2026-10-05 | S059 | Runtime and tool server loose ends | [S059.md](plan/steps/S040-S059/S059.md) |
| 2026-10-05 | S060 | Claims pages and API loose ends | [S060.md](plan/steps/S060-S079/S060.md) |
| 2026-10-05 | S061 | Scaffold, registry and evaluation plumbing | [S061.md](plan/steps/S060-S079/S061.md) |
| 2026-10-06 | S025 | AWS mapping | [S025.md](plan/steps/S020-S039/S025.md) |
| 2026-10-06 | S031 | Supervisor and workers | [S031.md](plan/steps/S020-S039/S031.md) |
| 2026-10-06 | S034 | Governance documents | [S034.md](plan/steps/S020-S039/S034.md) |
| 2026-10-06 | S037 | Second-framework workload | [S037.md](plan/steps/S020-S039/S037.md) |
| 2026-10-06 | S038 | GraphRAG spike | [S038.md](plan/steps/S020-S039/S038.md) |
| 2026-10-06 | S062 | Smoke and deploy loose ends | [S062.md](plan/steps/S060-S079/S062.md) |
| 2026-10-06 | S063 | The cluster outside `meridian` | [S063.md](plan/steps/S060-S079/S063.md) |
| 2026-10-06 | S064 | Metrics and logs | [S064.md](plan/steps/S060-S079/S064.md) |
| 2026-10-06 | S065 | Database and migrations | [S065.md](plan/steps/S060-S079/S065.md) |
| 2026-10-06 | S066 | Gateway ledger upkeep | [S066.md](plan/steps/S060-S079/S066.md) |
| 2026-10-06 | S067 | Triage rules and screening | [S067.md](plan/steps/S060-S079/S067.md) |
| 2026-10-06 | S075 | Harness, guard and Renovate | [S075.md](plan/steps/S060-S079/S075.md) |
| 2026-10-06 | S076 | CLI, scaffold and loader small ends | [S076.md](plan/steps/S060-S079/S076.md) |
| 2026-10-06 | S077 | GCP mapping | [S077.md](plan/steps/S060-S079/S077.md) |
| 2026-10-07 | S068 | Database upkeep and retention | [S068.md](plan/steps/S060-S079/S068.md) |
| 2026-10-07 | S078 | GCP Terraform, a scaffold | [S078.md](plan/steps/S060-S079/S078.md) |
| 2026-10-07 | S081 | The decision record for the move toward services | [S081.md](plan/steps/S080-S099/S081.md) |
| 2026-10-07 | S089 | Import layering page; Mermaid rendered in CI | [S089.md](plan/steps/S080-S099/S089.md) |
| 2026-10-07 | S090 | Component view of the Agent Runtime | [S090.md](plan/steps/S080-S099/S090.md) |
| 2026-10-08 | S091 | Data ownership views | [S091.md](plan/steps/S080-S099/S091.md) |
| 2026-10-08 | S092 | Mermaid and PDF render under rootless Docker | [S092.md](plan/steps/S080-S099/S092.md) |
| 2026-10-08 | S096 | The PDF: no row lost, and a brief edition | [S096.md](plan/steps/S080-S099/S096.md) |
| 2026-10-08 | S097 | The plan in files | [S097.md](plan/steps/S080-S099/S097.md) |
| 2026-10-08 | S098 | Demo claims on kind | [S098.md](plan/steps/S080-S099/S098.md) |
| 2026-10-08 | S099 | A dispatcher and workers: the test lock and the two briefs | [S099.md](plan/steps/S080-S099/S099.md) |
| 2026-10-08 | S100 | The plan's layout: step folders, the backlog in files, the change log ended | [S100.md](plan/steps/S100-S119/S100.md) |
| 2026-10-08 | S101 | The plan shows no status: the finished steps and those in flight are its last part | [S101.md](plan/steps/S100-S119/S101.md) |

### In flight

| Started | Step | Title | Status | File |
|---|---|---|---|---|
| 2026-10-06 | S036 | AWS Terraform | doing | [S036.md](plan/steps/S020-S039/S036.md) |
| 2026-10-06 | S069 | Runtime and gateway edges | doing | [S069.md](plan/steps/S060-S079/S069.md) |
| 2026-10-06 | S073 | Renewals, upgrades and what smoke cannot see | doing | [S073.md](plan/steps/S060-S079/S073.md) |
| 2026-10-06 | S074 | Test suite and file sizes | doing | [S074.md](plan/steps/S060-S079/S074.md) |
| 2026-10-06 | S079 | Self-managed Kubernetes: applied once on AWS, a scaffold on Google Cloud | doing | [S079.md](plan/steps/S060-S079/S079.md) |
| 2026-10-07 | S020 | Azure platform | doing | [S020.md](plan/steps/S020-S039/S020.md) |
| 2026-10-07 | S021 | Identity | doing | [S021.md](plan/steps/S020-S039/S021.md) |
| 2026-10-07 | S070 | Claims intake and what the adjuster is told | doing | [S070.md](plan/steps/S060-S079/S070.md) |
| 2026-10-07 | S071 | Measurements that need a live model | doing | [S071.md](plan/steps/S060-S079/S071.md) |
| 2026-10-07 | S072 | The cluster outside `meridian`, second round | doing | [S072.md](plan/steps/S060-S079/S072.md) |
| 2026-10-07 | S080 | File uploads for a claim, before sign-in | doing | [S080.md](plan/steps/S080-S099/S080.md) |
| 2026-10-07 | S082 | Code in the wrong place moves; import contracts per service | doing | [S082.md](plan/steps/S080-S099/S082.md) |
| — | S095 | Retention and erasure of uploaded files | todo | [S095.md](plan/steps/S080-S099/S095.md) |

<!-- plan-progress: end -->
