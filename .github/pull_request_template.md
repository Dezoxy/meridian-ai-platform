<!-- The title: what is true after the merge, as one sentence, starting
with the step ("S002: ...") or with "docs:" for documents alone. This
description is the record of the change: the squash commit carries it
into git log, and the plan keeps no change log (docs/meridian-plan.md,
Part A, "The pull request's description"). -->

## What

<!-- The step in docs/meridian-plan.md, e.g. S002, and what changed, by
area. Update the step's status and its file under docs/plan/steps/ in
this PR. -->

## Decisions

<!-- Each decision that was the owner's, in the owner's words and with
its date; each you took alone, with the alternative you rejected. -->

## Evidence

<!-- What each check printed and its exit status; name what was not run. -->

- [ ] `make docs` passes
- [ ] `make test` passes
- [ ] `make check` passes (model or ADR changes)
- [ ] `make mermaid` passes (views or Mermaid changes)
- [ ] The step's "done when" criterion is met; evidence below
- [ ] docs-sync: the docs this branch falsified are fixed, or "docs audited
      against the diff, no drift"

## Left open

<!-- What this change did not do, and the row of docs/plan/backlog.md
that carries each; "nothing" when nothing. -->
