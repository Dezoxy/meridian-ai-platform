# Meridian AI Platform — Plan

> **How to use this file:** this is the single living plan. Every step in
  Part B has an ID (`S001`…). When a step starts, add its file under
  `docs/plan/steps/`, in the folder of its twenty numbers (Part C), from
  the template, and fill it in as you go. The step tables hold no
  status: a step's status is the line under its file's heading, and
  Part E, at the end, lists the steps by those lines (`make
  plan-progress` writes it). This file holds the plan and nothing that
  ages: a step's row is one plan sentence, rewritten when a decision
  changes it, and the reason, the date and the owner's words go into
  the step's file (Part A, "What the plan holds"). Nothing is deleted
  from a step file or a decision record.
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
   which puts it under "In flight" in Part E ("A step's status", below).
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
   (the step moves to "Finished steps" in Part E, and nothing above
   Part E changes for a status), commit, go
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
- **Part E** is three tables, "Finished steps", "In flight" and "Not
  started" (a file that says `todo`), each row linked to the step's
  file. `make plan-progress` writes them from the status lines, and
  `make docs` fails when they are not what the files say, when a status
  line has another form and when a step table gets a Status column
  again. Nobody types into them.
- **A conflict between Part E's two markers**, after a merge of `main`
  into a branch that opened or closed a step, is never resolved by
  hand: take either side whole, run `make plan-progress`, and `make
  docs` proves the result. The tables are sorted and hold no date of
  the run, so two branches produce the same lines from the same files.
- **What a step leaves open while it is `doing`** is said in its file
  ("Where it stands", "Left before it is done"), not in a table of the
  plan.

**What the plan holds, and what it does not (S102).** The plan says what
is to be built, and reads the same the day after a step finished as the
day before. Its step rows had become the place where a step's history
was written (50 of 102 rows over 500 characters, the longest 3,462),
because this file's top said that nothing gets deleted and nothing read
a row. The owner, 2026-10-08: "i jsut want to close out these kind of
problem". So:

- **A step row is one plan sentence:** what is true when the step is
  done, the whole row in at most 500 characters. No date, no
  struck-through text, no quote of the owner and no word on what is
  built so far (`Built as`, `Designed`, `Implemented`, `not met`, a
  status word).
- **A decision that changes a step rewrites its row to the new truth.**
  The reason, the date and the owner's words go into the step's file.
  Nothing is deleted from a step file or a decision record; the rows
  keep no history.
- **No status by hand.** A step's status is its file's line, Part E is
  generated, and what exists today is the root README's to say.
- **Part D holds open questions only.** An answered one moves, whole,
  to `docs/plan/questions-closed.md` and keeps its number.
- **The plan's text outside its rows does not grow.** `make docs` holds
  it under a ceiling (`FIXED_TEXT_MAX` in `scripts/plan_gate.py`) that is
  lowered when the plan shrinks. A new rule takes the room of an
  old sentence; raising the ceiling is the owner's decision.
- **A pull request titled for a step changes that step's file**
  (`S102: …` changes `S102.md`); CI refuses one that does not.
- **What the gates cannot see.** A check counts and matches; it does not
  read prose for truth. A row can be short, clean and wrong, and a build
  label in words the gate does not know passes. The `docs-sync` audit
  before every pull request stays the judgment half.

**The pull request's description (S100).** It is the step's record
outside its own file. The repository squashes with the pull request's
title and description, so the description becomes the commit's message
on `main`, and `git log` is the change log. The plan's own change log
ended with S100; its 105 entries are in git at commit `093fd8b`, under
`docs/plan/changelog/`. The description holds what an entry held:

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
  has no version: the pull request's description is the record ("The
  pull request's description", above), so nothing in the tree waits for
  the pull request's number.
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

The tables below are the plan, and hold no status (S101) and no history
(S102): a row is one plan sentence, and what a step decided, built and
left open is in its file. A step's status is in its file, as one of
`todo` · `doing` · `done` · `blocked` · `dropped`; Part E lists the
steps by it, each with a link to its file. A step with no file has not
started.

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
| S002 | Python workspace and CI gates | `pyproject.toml` uv workspace with empty `meridian.platform` and `meridian.workloads` packages under `src/meridian/`; ruff, pytest, an import-linter contract (no `langgraph` or `langchain` under `meridian.platform`) and gitleaks run in CI; a deliberate framework import in a platform package fails CI | S001 |
| S003 | Synthetic data and golden set | A seeded generator under `data/synthetic/` produces policies, policy-wording documents and first-notice-of-loss claims with labelled expected outcomes; a rerun produces identical output; no real names or documents | S002 |
| S004 | Security and quality registers | `security/threat-model.md` with T-IDs per trust boundary, `security/data-classification.md` with the data classes, `requirements/quality-attributes.md` with targets marked unmeasured; all symlinked into `overview/`; `make docs` resolves every cited ID | S001 |
| S005 | Agent framework spike | A three-step flow with an approval pause in Microsoft Agent Framework under `spikes/`, with notes; a decision matrix appended to ADR 2 | S002 |
| S006 | Local platform on kind | `make up` creates a kind cluster with ingress, PostgreSQL with pgvector, OpenTelemetry Collector, Prometheus, Grafana, Tempo and Loki from pinned Helm charts; a test trace appears in Grafana; `make down` removes it | S002 |
| S007 | Azure foundation | Terraform with remote state, a resource group, a budget with 50, 80 and 100 % alerts (C-04), Key Vault, and Azure OpenAI `gpt-4o` plus `text-embedding-3-large` on regional Standard in Sweden Central, with the West Europe fallback after the subscription upgrade; plan reviewed; apply confirmed by the owner | S001 |

### M1 — Claims triage on kind

| ID | Step | Done when | Depends |
|---|---|---|---|
| S008 | Platform registry | `config/registry/` YAML for models, providers, tools, agents, policies and tenants, with JSON Schemas; every deployment carries a residency label and allowed data classes; validated in CI; seeded for the claims workload; `meridian registry validate` is the check developers and CI both run | S002 |
| S040 | Harness refresh | The ECC plugin is off, so the harness this repository needs is copied in from development-base: the remaining drifted rules and skills re-copied, a code reviewer, the Python rules that fit, the skills later steps need, three slash commands, the gate and session hooks, the chrome-devtools MCP server and the git hook-bypass denies with their cases; `make docs`, `make test` and the guard-bash cases pass | S008 |
| S009 | Walking skeleton | A claim posted to the claims API starts a one-node LangGraph run that calls the gateway's replay provider and stores a triage proposal; an end-to-end test proves one trace across API, runtime and gateway; per-service schemas, roles and migrations tested against PostgreSQL in CI | S006, S008 |
| S041 | Walking skeleton on kind | One image for the three services, manifests in namespace `meridian`, an HTTPRoute on a `*.localhost` hostname, per-service database roles on the cluster; `make demo` posts a claim and the one trace spanning API, runtime and gateway is found in Tempo; `make smoke` stays green | S009 |
| S010 | Gateway routing | Registry-driven routing by data class and residency; Azure OpenAI adapter; a residency mismatch is refused and audited; contract tests pass | S004, S007, S009 |
| S042 | Gateway resilience | Timeout, retry, circuit breaker and fallback across a route's candidates, with a second `gpt-4o` deployment in Sweden Central as the real second candidate and the second region labelled designed until the subscription is upgraded; a fault injected into the first candidate is answered by the second, and every attempt is audited; contract tests pass | S010 |
| S011 | Gateway budgets and cost | Per-tenant quotas, rate limits and token budgets enforced, with the cost reserved before the call; cost metered per tenant, agent, model and provider; a call ID on every audit record of a call | S010 |
| S043 | Gateway cost panel | A Grafana dashboard on kind, provisioned as code, shows tokens and cost per tenant, agent, model and provider from the gateway's metrics; `make smoke` finds the series in Prometheus | S011, S041 |
| S045 | Gateway embeddings | `POST /v1/embeddings` on the Model Gateway: the embedding route walked like the chat route, with the same caller headers, residency filter, tenant limits, ledger and audit; a simulated replay embedding; the Azure OpenAI adapter; the registry gives each embedding deployment its dimensions and refuses a route whose candidates differ in model or dimensions; contract tests pass | S010, S011, S042 |
| S012 | Knowledge and retrieval | Policy wording ingested into pgvector; hybrid search; retrieval checked against a labelled query set | S003, S009, S045 |
| S046 | Knowledge MCP server | `wording_search` served by the knowledge tool server: the call is bound to the product and wording version of the run's own policy, the query is embedded through the gateway under the run's tenant and agent, and the answer is cited chunks under an output schema; the server's role and grants; contract tests pass | S012, S013 |
| S013 | Policy and claims MCP servers | Tool contracts in `api/mcp/`; policy and claims MCP servers, in process; per-agent allowlists from the registry; mutating tools require an idempotency key; every call audited | S008, S009 |
| S044 | Tool servers on kind | The tool servers that exist run in namespace `meridian` under their own database roles, a job seeds the policy tables from the synthetic data, and the runtime reaches the servers by their cluster names; `make smoke` calls one tool through the runtime's client and `make demo` stays green | S013, S041 |
| S014 | Triage graph | Triage validates the policy, retrieves terms, screens fraud with rules and drafts a schema-validated proposal; threat model updated | S011, S013, S046 |
| S047 | Guardrails | Personal data is redacted before a model call and in logs; claimant text is screened for injected instructions before the model reads it; a request carries its own data class, which can only be raised above the tenant's, and a `special` request makes no model call and goes to the adjuster; threat model updated | S014 |
| S015 | Human approval | Interrupt and resume with the PostgreSQL checkpointer; the claim states that a triage run and an adjuster's decision drive, one triage of a claim at a time, and a state for a claim whose triage failed; approval decisions audited | S014 |
| S048 | Claim lifecycle, the rest | An adjuster sends a claim back to triage; a claim whose triage failed is referred to an adjuster, who decides it with no paused run; a claimant withdraws; documents that arrive (metadata only, T-38) start a new triage, at most five triages per claim | S015 |
| S016 | Adjuster UI | Server-rendered queue with claim, proposal, citations and fraud flags; approve, reject and request documents; audit trail | S015 |
| S049 | Claimant pages | A claimant submits a claim and reads its status on server-rendered pages behind the staff route until claimants are identified (T-01); the form says the data must be fictional (T-04); the answer tells the claimant what happens next without describing the proposal (T-65) | S016 |
| S017 | Evaluation harness | Golden-set replay with rule graders (route, reason, recommendation, amount, fraud indicators, missing documents, citations, completion); a report per prompt version; a CI gate on prompt or tool changes; `meridian eval compare` drives it locally and in CI | S003, S014 |
| S050 | Live evaluation | The golden set is answered through the Model Gateway by a recorded model, re-recorded with `make eval-record`, and a recording missing for a changed prompt fails the gate; an LLM judge grades groundedness only and cannot override the rule graders (T-29); latency and cost are graded from the gateway's ledger; `meridian eval run` works against a deployed stack, and a report compares two prompt versions | S017, S054 |
| S051 | Structured outputs | The Model Gateway passes a JSON schema for the answer to providers that support it (Azure OpenAI's structured outputs), declared per agent in the registry and refused for a deployment that cannot honour it; the triage assessment asks for its three-field answer by schema and still reads it strictly; tried live | S047 |
| S052 | Scheduled sweep | A scheduled job refers a claim whose documents miss the deadline to an adjuster, ends runs left `Running` that no resume takes over, paused runs that no claim points to, and checkpoints a failed delete left (T-63); a documents post whose triage failed while another move changed the claim is answered by what was stored, not by the claim's state afterwards | S048 |
| S053 | The claimant's word checked | The Claims API stamps the report date once claimants submit their own claims, and a decided claim enters the claim history, so `late_report` and `frequent_claims` stop resting on the claimant's word (T-66); the claimant's pages answer a 422 for an ID in the path, 404, 405, 413 and 400 with a page, not the API's JSON, and no server span's `http.url` keeps a query string (platform-wide, T-03) | S048, S049 |
| S054 | Parallel tests | `make pytest-db` and the CI python job run the suite in parallel with `pytest-xdist`: a database per worker inside the one PostgreSQL container, ports for the stack tests in `tests/meridian/stacksupport.py` that do not collide, and an empty database of its own for the migration runner's concurrency test; the CI python job's time before and after recorded in the step. It unblocks a coverage gate, which is not added here | S049 |
| S018 | M1 exit | Views match the code and the register says so; threat model v1; a fifteen-minute demo script; the demo runs from a clean checkout with `make` | S016, S017, S041, S042, S043, S044, S047, S048, S052 |

### M2 — Azure, identity, delivery

| ID | Step | Done when | Depends |
|---|---|---|---|
| S019 | Hardened Helm charts | Probes, resource limits, default-deny NetworkPolicy, PodDisruptionBudgets, non-root read-only containers, pinned digests; `helm lint` and the infra reviewer pass | S018 |
| S055 | Service-to-service identity | On kind, each service proves which service it is to the one it calls: the Agent Runtime, the Model Gateway and the tool servers refuse a call that carries no identity or comes from a service the registry does not map; the tenant and agent a caller may name come from that mapping, and a header that disagrees is refused; the tool servers accept the runtime alone (T-08, T-24, T-48, T-50); the mechanism is recorded in an ADR | S019 |
| S020 | Azure platform | Terraform adds the virtual network, AKS, ACR, PostgreSQL Flexible Server with pgvector and Workload Identity to Key Vault; the foundation's Key Vault and Azure OpenAI account deny by default, allow the operator's address and are reached from the cluster by private endpoints; the environment is created and removed with one command each | S007, S019, S055, S056 |
| S021 | Identity | **The staff half (cut on 2026-10-07: claimants are S093, the edge layer is S094).** Entra ID sign-in for staff on Azure, and on kind a mock OIDC issuer, Keycloak (the owner's choice); four roles, platform-admin, agent-developer, adjuster and auditor, as a `roles` list claim in the token; the Claims API checks a staff token on its JSON routes and runs the sign-in for the adjuster's pages itself, with a session cookie it signs (the app layer first, the owner's order); a role is required on every route but a closed list; the tenant is resolved from the validated token through the registry; who decided is stored on the decision row (the audit rows' actor waits for S085's outbox); all of it behind a switch, `MERIDIAN_SIGNIN`, off by default until `make demo`, smoke and the evaluation client carry their own tokens. **Implemented in part (2026-10-08), wired to nothing:** the token check, the key-set client, the session cookie, the route guard and the pages' authorization-code flow are seven modules of `platform/common` with tests, three import-linter contracts guard them, and the Keycloak pin, the realm generator and an opt-in rig exist (the rig's six tests passed against the pinned image in a container); two security reviews and a re-check of the module ended `Y4 MAY WIRE IT`, and the security review of the flow said the same. **On kind (local only), an add-on that is off unless switched on:** Keycloak ran there on 2026-10-08 (runs KR1 to KR3), its realm holds a cast of seven synthetic test people since a rotation (six staff in four groups, one role a group, and one person in no group), `identity.sh users` lists them and one guarded command prints their disposable passwords on the owner's terminal; signing in with them does nothing in the pages yet. **Designed, not built:** a route's use of the guard or the flow (Y4), the switch `MERIDIAN_SIGNIN` (no code reads it), the actor and tenant (Y6), the scripts' tokens (Y7) and the decision record (Y8); no Entra tenant has issued a token (Part C has the design, its contracts, the built parts and the threat rows T-114 to T-121). **The owner, 2026-10-08,** asked "another question, we do we have to implemt keycloak? we are gonna use entr id not?", was told that Entra ID is the issuer on Azure and that Keycloak is only the local stand-in for the mock issuer, and chose "Keep it, opt-in only (Recommended)" (offered beside it: Entra only; keep only what exists): Keycloak is built on kind as an add-on that is OFF unless switched on and is not part of plain `make up`, and Entra ID is the issuer on Azure (designed) | The kind half: nothing unbuilt. The Entra half: S020's apply |
| S093 | Claimants sign in as themselves | Claimants sign in on a realm of their own beside staff, with no role and a session shorter than a staff one; a claim is owned by the claimant who filed it, whose subject is stored on the claim; a claim's status, documents, withdrawal and uploads are refused to another claimant with the answer an unknown claim gets; a claim without an owner is staff-only; each realm's token and cookie is refused on the other's routes | S021 |
| S094 | Sign-in at the edge | TLS at kind's edge, then Envoy Gateway's sign-in and role rules (its `SecurityPolicy`) in front of the app's own checks, which stay; the proxy and the controller reach the issuer through egress rules of their own in a namespace that denies by default; each of the six paths that only a run can tell is seen on kind and recorded | S021 |
| S022 | Delivery pipeline | Build, SBOM, Trivy scan, cosign signing, push to ACR, kind smoke test, manual approval, deploy to AKS; the rollback runbook exercised; evidence attached to the release; until S094 exists the AKS edge admits the operator's address only, and the deploy refuses an edge without that allowlist | S020, S021 |
| S023 | Mistral provider | Mistral Large 3 adapter on Azure AI Foundry, DataZoneStandard; the routing policy uses it; ADR 3's provider set updated | S010, S020 |
| S024 | Operations baseline | SLO definitions (targets, unmeasured), alert rules and dashboards as code; runbooks for provider outage, budget exhaustion, database failure, rollback and secret rotation | S011, S019 |
| S025 | AWS mapping | An AWS deployment view and an ADR mapping every Azure service to its AWS equivalent, written against the Azure platform as S020's row and the model design it; a mapping that S020's run falsifies is corrected there | S007, S019 |
| S077 | GCP mapping | A Google Cloud deployment view and an ADR mapping every Azure service to its Google Cloud equivalent, as S025 does for AWS and against the same designed Azure platform; what both steps would say twice (the table of what Azure is used for, the residency rule for a second and a third cloud) is said once and both use it | S007, S019 |
| S026 | M2 exit | Environment created, fifteen-minute demo on AKS, environment removed; recorded; the run's cost logged; the AKS edge admits the operator's address only until S094 exists | S021, S022, S024 |

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
| S056 | Certificate lifecycle | On kind: no service goes on serving a certificate past its end with green probes (`/healthz` answers 503 near the end, so the container restarts), and an alert fires before one expires (T-89); the `meridian-services` issuer signs only for the `meridian` namespace and its URI prefix (T-88); a certificate's key file is readable by the service's own user and group alone; `make deploy` stops before its Jobs on a cluster without the issuer | S055, S024 |
| S057 | Test and tooling hygiene | Without the cluster: a `make` target runs the secret scan a push needs; the tests that rest on a sleep, a wall-clock limit or a port closed before its use hold by construction, shown by repeated runs under load; the registry tests find a deployment's entry by its key; the two sweep test files are under the 800-line ceiling; `check-iac.sh` lints the chart with the values `make helm-lint` uses; the CI python job's limit is set from its measured runs | S054 |
| S058 | Gateway loose ends | In the Model Gateway: a provider's token counts are bounded before they reach the ledger; a refusal row carries the call's purpose; an embedding input that would pass the provider's 8,191 tokens is refused with an answer of its own, not a 502; the count of a refusal flood's last window is written; a request over its rate limit is refused before its text is redacted (T-73); contract tests pass | S045 |
| S059 | Runtime and tool server loose ends | The runtime's tool client lives longer than one call; `runtime.runs` text columns have length checks; `finish_run` writes a status only over the one it expects, so a late leg cannot overwrite the sweep's `Failed`; a tool server's waiting calls are bounded, and a search the runtime gave up on is not charged or audited as completed (T-62); no span processor or sampler can see a URL with its query; contract tests pass | S046, S052 |
| S060 | Claims pages and API loose ends | In the claims workload, without a change to the triage graph or a prompt: the adjuster's queue has a next page past 100 claims and shows that a referred claim's documents are overdue; the claimant's page says by when documents are due; a 500 or 503 under `/claimant/` is a page; a claim that is not valid facts is logged by field and error type, never by its text; the calls to the runtime have a timeout per phase | S053 |
| S061 | Scaffold, registry and evaluation plumbing | `meridian workload new` writes the new agent into the Agent Runtime's entry in `services.yaml` and says which line of an unusual file it refuses and what holds a taken name; `meridian registry validate` answers an unreadable registry directory with a message, not a traceback; `eval run` refuses a golden set that is not the workload's own; the two entry-point groups share one loader and its trust checks | S039, S050 |
| S062 | Smoke and deploy loose ends | On kind: `make smoke` reads the alert rules, the health dashboard and the stores it does not read yet, proves more than one denied path inside the cluster and notices a schedule that stopped after a success; `make demo` says so when a trace's readings alternate; finished migrate and seed Jobs remove themselves; the waits of `make up` and of an interrupted deploy each end with a message that names the remedy | S056 |
| S063 | The cluster outside `meridian` | On kind: the `cert-manager` and `observability` namespaces have NetworkPolicies and Pod Security labels, so only Meridian's pods push to the collector (T-68, T-84); the Prometheus operator and kube-state-metrics read no Secret they do not need; the database pod reaches the API server's address alone; the platform charts' images are pinned by digest; the seed and the ingestion Jobs run under a role of their own (T-25) | S062 |
| S064 | Metrics and logs | On kind: the services' logs reach Loki, and no access log keeps a query string (T-03); the runtime and the tool servers export metrics, among them a caller that cannot reach the gateway and the knowledge server's empty-store and stale-vector warnings; the assessment's outcomes are counted by reason word, and the sweep reports what a pass found; a rule fires on a series that went absent; the cost dashboard's queries survive a gap in the data | S059, S060, S063 |
| S065 | Database and migrations | `ensure_roles` has a lock timeout and names its isolation level; audit rows of one transaction can be ordered; the sweep's listing of leftover threads does not read every checkpoint row; a check refuses two migrations with one number before a pull request merges; a column added to `claims.claims` is backfilled without holding its lock, and the rule is written down; a test database is copied from a template | S057, S059, S060 |
| S066 | Gateway ledger upkeep | A command of the gateway's own, under a role of its own and with an audit row, credits a tenant, closes a reservation a dead process left `reserved` and expires old ledger rows; the budget runbook names it; the rate windows are shared between gateway processes, so two pods in a rolling update do not each allow the full limits (T-45), the store chosen with the owner when the step opens | S058, S065 |
| S067 | Triage rules and screening | The one step of these that changes the triage graph's rules: claims of one policy that are open at the same time count for `frequent_claims` (T-76); the injection screen reads the description as posted, before the claimant's name is replaced; the redaction knows Hungarian forms of names and identifiers; the stored wording is compared with the manifest after ingestion (T-27, T-57); `make eval` passes | S060, S061, S064 |
| S068 | Database upkeep and retention | An insert-only audit table has a way to expire rows and `expire_ledger` works in batches, each by a command the upkeep role alone reaches; the retention periods and a schedule are the owner's and are set nowhere until named; migration 0017's rewrite of a large audit table has a way through that is written down and tested; the static check on migrations says what it cannot see; a member of `claims_sweep` is refused at the next migrate | S066 |
| S069 | Runtime and gateway edges | Without a change to a prompt or a rule: a validation error in the triage's answers logs the field; a failed or late resumed leg neither leaves nor overwrites another leg's result; a completion the filter withheld has its drafter on record; the runtime's call to the gateway is bounded; a shed tool call's audit row names its run; the health check watches the certificate the server loaded; the ingestion's data class has a tenant of its own (T-60) | S064, S037 |
| S070 | Claims intake and what the adjuster is told | A report dated as a recent loss is seen for what it is, or T-66 says why it cannot be; the adjuster's page marks a recommendation that rests on the model's answer, so a steered model's `approve` does not read as the rules' | S067 |
| S071 | Measurements that need a live model | Costs money, and the owner says yes to a stated amount before any call: a real model's answers to the injection cases the screen lets through, recorded beside the golden recording; a model's refusal of a structured request seen from a real provider; retrieval measured with a real embedding; the judge compared with labels a person wrote for a sample; a held-out set for the injection screen | S067 |
| S072 | The cluster outside `meridian`, second round | On kind: the Prometheus and CloudNativePG operators' reach into Secrets and ConfigMaps of every namespace is narrowed or recorded as accepted; DNS and the collector cannot carry data out unseen (T-84), or the residual is stated; writes to Prometheus and Loki pass a policy; egress from `observability` and the admission webhooks' port are bounded; `cnpg-system` and `envoy-gateway-system` have Pod Security labels and a policy | S064, S066 |
| S073 | Renewals, upgrades and what smoke cannot see | On kind: a renewal is seen for the collector's certificate and the database's, and something alerts before the database's end; the services do not all restart in the same minute at a renewal; approver-policy is restarted when it hangs; a first install that fails has a way back that was tried; the scripts' `kubectl` calls have a request timeout; a manual sweep Job does not hide a stopped schedule | S064, S066 |
| S074 | Test suite and file sizes | Without the cluster: `infra/kind/smoke.sh`, `test_kind_manifests.py` and the test files over 800 lines are split along the lines their own tests already cut; the functions over 50 lines are under it; the tests that failed once under load are run repeatedly and either hold by construction or are closed as not reproduced, with the numbers; every test has a time limit, and CI gates on coverage and on file size | S064, S066, S037 |
| S075 | Harness, guard and Renovate | `make docs` notices a blank line that splits a table; the command guard's known gaps to a Secret's values and to superuser SQL are closed or listed where a session reads them, and a hook that times out has a known outcome; a rule for an implementer that edits through the shell, and a guard or a rule for `make up` and `make down` from an old checkout; the workflow linter knows the runner label; an image is not proposed before the chart that installs it | — |
| S076 | CLI, scaffold and loader small ends | No registry entry lets the runtime name an agent that no tenant lists without a check saying so (T-81); `services_edit` refuses an alias or a merge key by itself; the scaffold says which write failed and names the line it refuses in every case; the two entry-point loaders answer a bad entry in the same fixed words; the workload's report builders refuse another workload's manifest | S037 |
| S080 | File uploads for a claim, before sign-in | A claimant uploads one PDF, JPEG or PNG of at most 1 MiB to a claim, five and 3 MiB to a claim; the file is told by its first bytes, and its name is not stored; an upload starts no triage; the adjuster lists a claim's files, each marked as not scanned, and, behind a second switch, downloads them as an attachment with an audit row before the first byte; both switches are off by default | S070 |
| S089 | Import layering page; Mermaid rendered in CI | A page in the Documentation tab and the PDF says which Python package may import which, as the six import-linter contracts enforce it, with one Mermaid diagram of the layers and the output of a run; the `derived diagrams` job renders every Mermaid block on every pull request and fails on one that does not parse | — |
| S090 | Component view of the Agent Runtime | A component view in the Structurizr model answers which responsibilities sit inside the Agent Runtime and which of them is the only way out to a model, a tool and the database: components by responsibility and not one per file, with a register row and a PNG read at full size, and no other view changed | — |
| S091 | Data ownership views | Who reads and writes which schema of the Platform Database, as views of the model: the six schemas as components of the database, each service with its own schema and the reads that cross a schema in one view; every arrow checked against the grants the migrations leave and compared with ADR 10's table, a difference recorded and not smoothed; register rows and PNGs read at full size | S090 |
| S092 | Mermaid and PDF render under rootless Docker | `make mermaid-render` and `make pdf` finish on a machine whose Docker is rootless, as the virtual machine's is, and still finish in CI; the fix is made in development-base's copy of `scripts/render-mermaid.sh` first and copied here unchanged; the render container has no network, and the render fails when it finds no block | — |
| S095 | Retention and erasure of uploaded files | A retention period for a claim's uploaded files is a setting with no default that deletes anything; a sweep deletes a file's bytes once the period has passed, under a database role that may; an audited command erases one claim's files on request, run by a signed-in person whose name is on the audit row; the metadata and audit rows stay; a legal hold on a claim stops both; seen once on kind | S080, S021 |
| S096 | The PDF: no row lost, and a brief edition | The architecture PDF loses no text where a table row is taller than a page, and a second, brief edition exists to hand to someone who will not read a register | S092 |
| S097 | The plan in files | Each step's section is a file of its own under `docs/plan/steps/`, moved byte for byte and proved so; the plan has no version of its own; `make docs` checks the files against Part B and refuses a section in the old place | — |
| S098 | Demo claims on kind | `make demo-seed` posts the first `COUNT` (40, at most 47) synthetic claims through the edge one at a time and prints a line a claim and the counts by state: kind only, through the API only, the model simulated, safe to run twice and deciding nothing; it has run once on the kind cluster with the smoke run after it | S080 |
| S099 | A dispatcher and workers: the test lock and the two briefs | Steps run in worker sessions, one step and one worktree each, handed out by a dispatcher session; `make pytest`, `make pytest-db` and `make alerts` take one lock for the machine, so a second session's run waits for the first and says who holds it; the two roles are skills (`plan-dispatcher`, `plan-worker`) that name no path of the machine | — |
| S100 | The plan's layout: step folders, the backlog in files, the change log ended | The step files sit in folders of twenty step numbers under `docs/plan/steps/`; the follow-up backlog is out of the plan file, every row in exactly one of `docs/plan/backlog.md` (open) and `docs/plan/backlog-closed.md`; the change log ends, and Part A says what a pull request's description must hold instead; `make docs` checks the folders and holds each backlog row to its file | — |
| S101 | The plan shows no status: the finished steps and those in flight are its last part | Part B's step tables have no Status column, and a step's status is one line of its own file; the plan's last part lists the finished steps and those in flight, written by `make plan-progress` from those lines and each linked to its file; `make docs` fails on a table that is not what the files say and on a Status column put back | S100 |
| S102 | The plan reads as a plan, and a gate keeps it so | Each step row is one plan sentence, and what it held besides is in the step's file; the plan has no change-log part and no status written by hand; `make docs` refuses a row that is too long or holds a date, struck-through text, a quote of the owner or a build label, an answered question, and a plan that grew outside its rows; CI refuses a pull request titled for a step that leaves the step's file unchanged | S101 |

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
- **The owner's two answers of the sixth round** are questions 7 (the audit
  outbox, for S085) and 8 (a fresh baseline per database, for S087) of
  [plan/questions-closed.md](plan/questions-closed.md). What they
  leave open is the steps' own design: the gateway's audit row, how the
  adjuster's page reads the central trail, and where the archive lives.

| ID | Step | Done when | Depends |
|---|---|---|---|
| S081 | The decision record for the move toward services | [ADR 10](architecture/decisions/0010-split-the-platform-into-services-with-a-database-each-and-their-own-releases.md) is accepted and indexed: it says what the owner decided apart from the session's own design, lists the ten couplings with the step that replaces each, what stays shared, the consequences and the two checkpoints; `make docs`, `make check` and `make test` pass | — |
| S082 | Code in the wrong place moves; import contracts per service | With no change in behaviour and no assertion of an existing test changed: the claims tool server leaves the claims workload's package, the Claims API no longer imports the runtime's models or its sweep's constants, the sweep's runtime SQL sits with the runtime, and the workloads' graphs sit in a package of their own; an import contract keeps each service's package out of another's | S081; S020, S071, S072, S080 |
| S083 | Six packages, six images, a tag per service | A `uv` workspace holds a common library, the tool-server library and one package per service, each with its own dependencies and version; the jobs ship with the service that owns their data; one build file makes six images; the chart takes a tag per service and `make deploy` builds and loads all six; `make demo` and `make smoke` pass on kind with six images on one database | S082 |
| S084 | The tool servers take the binding from the caller; two reads become calls | A tool server takes a call's run, agent, tenant and claim from the Agent Runtime's authenticated call and reads neither `runtime.runs` nor `claims.claims`; a call without a binding is refused; the knowledge server asks the policy server for a policy's wording version and the policy server asks the claims tool server for a policy's other claims, each over mutual TLS | S082 |
| S085 | The audit outbox, the relay and the central trail | Each service writes its audit row into an audit table of its own in the transaction of its business write, with the insert-only and stamp triggers; a relay copies the rows into a central audit table that owns retention and serves the adjuster's trail by claim; a relay that lags or stops is seen by an alert, and the services keep writing; built inside the one database, as a table per schema | S082 |
| S086 | The sweep in two | The runtime sweeps its own runs and checkpoints under its own role, and the claims side asks the runtime for a run's state and moves its own claims; no statement spans runs and claims; each half is safe to run twice, and a test stops a pass between the halves; whether the claims side has an identity of its own towards the runtime is decided and recorded | S082 |
| S087 | Five databases | `claims`, `runtime`, `gateway`, `policy` and `knowledge` each have their own migration tree and ledger from a baseline, with no replay; the old migration files and their tests are archived outside the package's path; each role and the server's rules name one database; the audit database exists; `make up`, `make smoke` and `make demo` pass on a recreated kind cluster; T-25 is rewritten | S083, S084, S085, S086 |
| S088 | Contracts, versions and independent release | A committed, versioned contract for the gateway, the runtime and the Claims API's routes, each side tested against it, and a rule for how long an old version is served; the registry directory has a version of its own; CI builds, tests what changed and publishes the six images to a place the step chooses and records; one service is released alone while the others stay on their versions, and the run is recorded | S083, S087 |

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
| S036 | AWS Terraform, applied once | Without an account and without cost: the module for what S025 maps (network, cluster, registry, database with pgvector, workload identity to a secret store) in an EU region passes `terraform validate` and a policy scan, and one command each creates and removes it. In the owner's AWS account, once the owner says yes to a stated cost: it is applied once, recorded and removed, and the run's cost is logged | S025 |
| S078 | GCP Terraform, a scaffold | Without a project and without cost: the module for what S077 maps (network, cluster, registry, database with pgvector, workload identity to a secret store) in an EU region passes `terraform validate` and a policy scan; it is never planned or applied, and its README says how the owner would apply it by hand; a dated note to ADR 1 names this cloud too | S077 |
| S079 | Self-managed Kubernetes: applied once on AWS, a scaffold on Google Cloud | A cluster whose control plane the owner's account runs itself, beside the managed clusters of S036 and S078: a Terraform module for AWS and its twin for Google Cloud pass `terraform validate` and a policy scan, and the mapping documents compare the two kinds on each cloud; once the owner says yes to a stated cost, the AWS module is applied once, recorded and removed; the twin is never applied | S036, S078 |
| S037 | Second-framework workload | A small workload in Microsoft Agent Framework on the same platform contract: a second host behind the Agent Runtime's `Host` protocol, picked for each agent by the registry's `host` field, and a second workload, `claim-brief`, that calls tools, pauses for an adjuster and is started by the Claims API | S005, S018 |
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
below; Part E lists the files by what their status lines say (this part
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

Open questions only. One that is answered moves, whole, to
[plan/questions-closed.md](plan/questions-closed.md) and keeps its
number; a new one takes the next number that neither table holds.

| # | Question | Needed by | Default if unanswered |
|---|---|---|---|
| 1 | How many hours per week, and when do interviews start? | S002 | Plan in two-week increments; cut M3 before M2 |
| 6 | Should a session be stopped from editing the command guard's own files? The permission rules allow Edit and Write on `.claude/hooks/guard-bash.sh` and `.claude/settings.json`, so a session can weaken the guard that reads its commands (N4 of the third security review). Two ways: deny Edit and Write on `.claude/hooks/**` and `.claude/settings*.json`, or ask before each. The session recommends asking: a deny would also stop a session from fixing the guard when a review finds a hole, as S075 did after each of its three reviews, while an ask puts the edit in front of the owner | S075's pull request, if the owner wants it built there; no step needs it | Neither is built: the guard stays a guard for habits, and the gap is listed in the runbook and in the hook's header |

## Part E — Where the steps stand

The parts above are the plan: no step table holds a status. A step's
status is one line in its own file, and this part shows it: its three
tables, "Finished steps", "In flight" and "Not started", are written
from those lines by `make plan-progress` and held to them by `make
docs`, so nobody types into them (Part A, "A step's status"). A step
that starts or finishes changes its file and this part, and nothing
above. What exists today is said in the root [README](../README.md),
not here.

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
| 2026-10-08 | S102 | The plan reads as a plan, and a gate keeps it so | [S102.md](plan/steps/S100-S119/S102.md) |

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

### Not started

| Step | Title | File |
|---|---|---|
| S022 | Delivery pipeline | [S022.md](plan/steps/S020-S039/S022.md) |
| S026 | M2 exit | [S026.md](plan/steps/S020-S039/S026.md) |
| S083 | Six packages, six images, a tag per service | [S083.md](plan/steps/S080-S099/S083.md) |
| S084 | The tool servers take the binding from the caller; two reads become calls | [S084.md](plan/steps/S080-S099/S084.md) |
| S085 | The audit outbox, the relay and the central trail | [S085.md](plan/steps/S080-S099/S085.md) |
| S086 | The sweep in two | [S086.md](plan/steps/S080-S099/S086.md) |
| S087 | Five databases | [S087.md](plan/steps/S080-S099/S087.md) |
| S088 | Contracts, versions and independent release | [S088.md](plan/steps/S080-S099/S088.md) |
| S093 | Claimants sign in as themselves | [S093.md](plan/steps/S080-S099/S093.md) |
| S094 | Sign-in at the edge | [S094.md](plan/steps/S080-S099/S094.md) |
| S095 | Retention and erasure of uploaded files | [S095.md](plan/steps/S080-S099/S095.md) |

<!-- plan-progress: end -->
