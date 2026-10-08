- **#XXXX, 2026-10-08:** S097 `doing`: the plan's step sections and change log
  are files of their own. Part C's 78 sections are `docs/plan/steps/S0NN.md`
  and Part E's 98 entries are `docs/plan/changelog/v0.NN.md`, moved byte for
  byte (proved by `scripts/plan_split.py --check`: 1,426,448 and 81,587 bytes,
  equal digests); the plan keeps its top, Part A, Part B and Part D, an index
  of the step files and a paragraph on the change log. The plan has no version
  of its own any more (the owner, 2026-10-08: "Pull request number
  (Recommended)"): this entry and every later one is named `pr-NNNN.md` for its
  pull request, and the file is `pr-XXXX-<step>.md` until the number exists,
  which `make docs` accepts on a machine and CI refuses. `make docs` also runs
  `scripts/check_plan_files.py` (a step file has its row and its place in the
  index, a change-log file's name and label agree, no section or entry is left
  in the old place), and the docs index no longer asks for a link to each file
  under `docs/plan/`. `scripts/plan_port.py` carries a branch's edits of the old
  layout over during a merge of `main`; rehearsed on `origin/s021-signin`. Part
  B's tables are unchanged and still collide by rows. Not built: the
  development base's template (after this pull request).
