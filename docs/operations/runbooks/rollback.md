# Runbook: Rollback

A release, a registry change or a prompt change made things worse, and the
last good version must run again.

Status (S024): written from `infra/kind/deploy.sh`, the chart and the
migration runner, not exercised. No script performs a rollback today. On
Azure the delivery pipeline is designed (S022), and its "done when"
exercises this runbook; the game day (S028) exercises it again.

## What you see

- `MeridianServiceUnavailable` after a deploy: a service has had no
  available replica for 5 minutes.
- `MeridianGatewayInternalErrors`: calls fail inside the gateway.
- `MeridianGatewayRefusingByPolicy` after a registry change: calls are
  refused with `no-route`, `no-allowed-deployment`, `agent-not-allowed`
  or `unknown-tenant`.
- `make deploy` or `make smoke` fails after a change that passed CI.

`MeridianGatewayRefusingByPolicy` can also be raised by a caller on
purpose: until callers are identified (S055, S021), five requests that
name an unknown tenant are enough. Look at what changed before rolling
anything back.

## First, is it the release

1. Is the database up? If `MeridianDatabaseNotReady` fires, go to
   [database failure](database-failure.md); a rollback fixes nothing
   there.
2. Did anything change? These only read:

   ```sh
   h() { helm --kubeconfig infra/kind/kubeconfig --kube-context kind-meridian -n meridian "$@"; }
   k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
   h history meridian
   h status meridian
   k get deploy -o wide
   k logs deploy/<the failing service> --tail=50
   ```

   `history` lists each revision with its time; `get deploy -o wide`
   shows the image tag every service runs. Read logs on a private
   terminal ([why](../README.md#reading-logs)).
3. Which commit was the last good one: `git log --oneline` on `main`,
   against the time of the last good revision.

## What a release is on kind

- **The image** is built from the working tree by `make deploy` and
  tagged with the first twelve hex digits of its own ID. An unchanged
  tree gives the same tag. Old images stay on the node until the cluster
  is deleted. `make deploy` takes no tag: it always builds what is
  checked out.
- **The registry** (`config/registry/`) is part of the image and is read
  once when a service starts. So a registry change is a release.
- **The migrations** run before the services, as the Job
  `meridian-migrate-<tag>`, and only forwards. The ledger
  `public.meridian_migrations` records each applied file by name and
  checksum. There is no down-migration.

## What to do

### Deploy the last good commit

This is the one path the scripts support, and it is what a rollback means
on kind.

1. Check the migrations between the two commits:

   ```sh
   git diff --stat <good> <bad> -- src/meridian/platform/migrations/
   ```

   No file listed: go on. A file listed: the schema stays at the newer
   version, because nothing undoes a migration. The older image's runner
   looks only for its own files, finds them applied and reports that it
   is up to date. Read each migration between the commits. Most add
   tables and columns and leave older code working; one that drops or
   redefines something the older code reads (0014 replaced a view and an
   index) means the older image must not run. Then fix forward instead: a
   new commit on top of the bad one.
2. Check what the good commit lacks. Rolling back also takes away every
   control added since: a network policy, a tightened route or residency
   label in the registry, a guardrail.

   ```sh
   git log --oneline <good>..<bad> -- infra/helm config/registry src/meridian/platform
   ```

   If a commit in that list closed a hole, rolling back opens it again.
   Fix forward, or take the rollback knowingly and write it down.
3. In the checkout that holds the cluster's credentials
   (`infra/kind/kubeconfig`, which is gitignored, so a new worktree has
   none), check out the good commit and run `make deploy`. It builds that
   tree, runs the migration and seed Jobs, upgrades the release, ingests
   the wordings again, which adds a wait of about a minute, and waits for
   the six rollouts. **Do not run `make up` in the older tree** to get
   credentials: it would upgrade the whole platform with that tree's
   pins, apply its older list of database roles and remove dashboards it
   does not have.
4. `make smoke`, by the session that owns the cluster. Then return the
   checkout to `main`.

A rolling update starts the new pod before the old one stops. For that
moment two gateway pods each allow a tenant's full rate windows (T-45).
A gateway pod that stops with calls in flight leaves their reservations
`reserved`, charged until the period ends
([budget exhaustion](budget-exhaustion.md#what-spent-it)).

### `helm rollback`, for a change with no migration and no new image

`helm rollback meridian <revision>` puts back the previous revision's
objects, with the image tag they named; the image is still on the node.
It runs no Job and touches no schema. It puts back that revision's
network policies too, so read `helm get values meridian --revision <n>`
first: a revision from before a policy existed removes it. No script
uses it and it has not been tried here, so prefer the path above; it is
the owner's to run. It does not wait for the rollouts: check them with
`kubectl rollout status`.

### A registry change

Revert the pull request, then deploy as above. `make registry` must pass
on the reverted tree: it compares the registry with the snapshot of
Terraform's deployment outputs and refuses a deployed model that is not
registered, so a revert that removes a deployment Terraform still has
fails there. Revert the registry and its snapshot together.

### A prompt change

Revert the prompt together with its recording and its baseline. The
evaluation gate replays a real model's recorded answers, keyed by what
the provider would be sent, so a prompt without its recording answers
`not-recorded` and the gate fails. Recording again (`make eval-record`)
spends money and needs the owner's Azure login; a full revert needs
neither.

### The first install failed

A first `helm upgrade --install` that fails can leave a release Helm then
refuses to upgrade ("has no deployed releases"). `make deploy` prints the
`status` and `history` commands. The cure is an uninstall, which deletes
the release's objects and is the owner's to run; it has not been tried.

## What not to do

- **Do not edit `public.meridian_migrations`**, and do not change a
  migration file that was applied. The runner refuses a file whose
  checksum differs from the one it recorded; a new file is the way to
  change the schema.
- **Do not edit the objects with `kubectl edit` or `kubectl scale`.**
  The next deploy applies the chart over them, and until then the
  cluster runs something no commit describes.
- **Do not uninstall the release** to get a clean start. It removes the
  network policies with the services.
- **Do not roll back across a migration** the older code cannot read.
  Fix forward.

## On Azure

Designed (S022). The pipeline builds the image once, signs it and deploys
it by digest after a manual approval, so the last good release is a
digest the chart can be given again; the chart already takes one. Whether
a rollback there runs migrations, and how an approval is recorded for it,
is decided when S022 exercises this runbook.

## Afterwards

- Write down the revision, the two commits and the migrations between
  them.
- If the cause was a registry or a prompt change that passed its gate,
  the gate missed something: add the case to the evaluation or to the
  registry's checks before the change is tried again.
