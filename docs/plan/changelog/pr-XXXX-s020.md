- **#XXXX, 2026-10-08:** S020 `doing` (first half done, the wrapper not
  written): the command guard decides the Azure platform wrapper's names and
  the rest of its surface before the wrapper exists, as two contracts of
  additions to the hook (GA1 and GA2, after a measurement of 535 shapes that
  found none of the new names decided and 263 shapes decided too weakly). GA1:
  `make azure-platform-plan`, `-apply` and `-destroy` and the script
  `infra/terraform/azure.sh` with `plan`, `apply` and `destroy`, in their
  plain, quoted and nested forms: the removal is denied (the command-word gate
  keeps a commit message, a pull request body, a search and an echo as they
  were), the apply and the plan ask, a pseudo-terminal or a trace around them
  and a `TF_*` or `ARM_*` assignment in front of them are denied,
  `AZURE_CONFIG_DIR` asks; the open medium of S071's sixth review (a quoted word
  after `env -S` hiding the build tool) is closed, and its backlog row with it.
  GA2: Terraform by hand in `infra/terraform/azure` (denied for apply, destroy,
  `plan -out`, import, taint, state writes, force-unlock, workspace changes and
  an init with a backend setting; asked for plan, show, output, console, refresh
  and state reads), readers and writers of the wrapper's local file, the saved
  plan and its record, the module's `.terraform` folder and `~/.azure` (denied,
  `rm` left free on purpose), a blob command that names the state (denied), and
  the cloud CLI's provider registration, cluster credentials, registry and
  database login, login, logout, account switch and `kubelogin` (asked); the
  settings add nine entries (the plan and apply targets ask, the removal is
  denied in four spellings, `~/.azure` is closed to Read, Edit and Write). The
  hook has 385 added lines against `main` and none removed; over the whole case
  file (3,676 rows, 342 new) and the 535 measured shapes no decision is weaker
  than `main`'s or than GA1's (0 of 4,211 each), 411 commands are tighter than
  `main`'s and 180 than GA1's, all wanted; the guard's script ends with 3,966
  `ok` and no failure, the worst new shape takes 0.76 s of CPU against 0.44 s on
  `main`; 26 of 27 mutants are killed, the last is equivalent. T-113 new (113
  threats); the runbook lists what the guard does not read for the new names;
  the Azure READMEs say the rules exist before the wrapper. Not seen: a live
  session (the wrapper does not exist and no Azure sign-in is on the machine),
  and one security review of both contracts is still to come.
