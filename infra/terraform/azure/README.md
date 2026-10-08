# Azure platform module

`make azure-platform-validate` and `make azure-platform-scan` check this module
without an account. No command plans, applies or removes it yet, on purpose
(see "The doors that exist, and the one that does not").

Status: **written and validated, and NEVER applied** (hard rule 7: this is code
that `terraform validate` accepts and a policy scan passes at HIGH and
CRITICAL; it is not a deployed capability). It has never been planned against
an account, nothing in it has met Azure, no sign-in was made to write it and no
price has been paid. Every sentence below about what Azure will do is a reading
of a page or of the provider's schema, dated where it matters, or it says that
nothing read settles it. The module serves
[ADR 11](../../../docs/architecture/decisions/0011-run-the-azure-platform-per-demo-day-on-aks-with-envoy-at-the-edge.md)
and sits beside the persistent foundation, which [the Terraform
README](../README.md) describes and which the owner applied on 2026-09-30.

## What it declares

One environment for one demo day, in one region, made to be created and
removed together with its own state (the plan's C-04 narrows ADR 1 this way:
the foundation stays, this is made and removed). It installs nothing into the
cluster: no Helm release and no Kubernetes provider, so no controller makes a
load balancer or a volume that Terraform does not know. Running the Meridian
chart on this cluster is the second half of S020 and S022's. Everything it
makes carries the tags `project=meridian`, `environment=demo` and
`managed-by=terraform`, and the resource group alone may carry `expires-on`
(see `expires_on` below). In all, 44 managed resources, one ephemeral resource
and five data sources, read from the files:

- **Pin and group** (`main.tf`). Three data sources of the foundation (the
  caller's identity, the foundation's resource group, its Key Vault, read by
  the names the foundation gave them), the resource
  `terraform_data.subscription_pin` (two preconditions, see "The subscription
  pin") and the module's own resource group `rg-meridian-platform`, which
  waits for the pin.
- **Network** (`network.tf`). One virtual network `vnet-meridian`
  (`10.40.0.0/16`) with three subnets. `snet-nodes` (`10.40.0.0/22`) holds the
  nodes and has no security group on purpose: Microsoft's pages say AKS applies
  no group to its subnet and changes none attached to it, so every rule the
  cluster needs would have to be written by hand, and blocking traffic inside
  the subnet is not supported. `snet-postgres` (`10.40.4.0/24`) is delegated to
  the PostgreSQL flexible server service and declares the `Microsoft.Storage`
  service endpoint that the service adds to a delegated subnet itself, because
  the provider's block is not computed and a later plan would otherwise propose
  to remove it. `snet-endpoints` (`10.40.5.0/24`) holds the two private
  endpoints, has default outbound access off and the private-endpoint network
  policy set so that its group applies to endpoint traffic. The pod range
  (`10.244.0.0/16`) and the service range (`10.41.0.0/16`) of the cluster's
  overlay network are not subnets and are kept apart from every other range.
  The database's group has three rules: TCP 5432 from the nodes' range and the
  pod range, anything within its own range (Microsoft's page asks that port
  5432 be allowed inside the subnet, and a standby or a component of the
  service would need it), and a deny at priority 4096 from the
  `VirtualNetwork` tag, not from everything, so Azure's default rule for the
  load balancer's tag is not shadowed. The endpoints' group allows TCP 443 from
  the nodes' range and the pod range and denies every other inbound source. No
  rule is outbound. Each group is attached to its subnet by a separate
  association that waits for the group's rules, and the server and the two
  endpoints wait for the association.
- **Cluster** (`cluster.tf`). A user-assigned identity `id-meridian-aks` with
  the one role the control plane needs, `Network Contributor`, on the nodes'
  subnet and nowhere wider (a cluster in a network the customer manages needs
  it before it is created, which a system-assigned identity could not have);
  the AKS cluster `aks-meridian` on the Free control-plane tier at Kubernetes
  1.36 (the variable accepts 1.35 and 1.36); one system pool `system` of two
  `Standard_D2s_v5` nodes with 50 pods a node (AKS reserves memory by the pod
  limit, so 50 leaves an 8 GiB node about 7 GiB; 30 would leave room for 60
  pods on two nodes and kind runs 32 before AKS adds its own); Azure CNI in
  overlay mode with Cilium as the data plane and as the policy engine, because
  the chart's default-deny NetworkPolicies need an engine that enforces them,
  and the four settings must agree; the standard load balancer for outbound
  traffic. Authentication is Microsoft Entra with Azure roles for Kubernetes:
  local accounts off, run command off, no Entra group named, and the caller of
  the apply gets `Azure Kubernetes Service RBAC Cluster Admin` on the cluster
  alone. The API server is public, open to one to four `/32` addresses from a
  sensitive variable with no default. The OIDC issuer and workload identity are
  on. No automatic upgrade of the cluster or of the node image (the node
  image's channel is `None`; the cluster's is left out because the provider's
  list for it has no value for off and an unset channel is none), and the Azure
  Policy add-on and Container Insights are off. `node_provisioning_profile` is
  `Manual`, the provider's default, which Microsoft's page calls the value that
  switches node auto-provisioning off; no sentence defines `Manual` further.
- **Registry** (`registry.tf`). One Azure Container Registry
  `crmeridian<suffix>` (the suffix is six hex characters of the subscription's
  hash, as the foundation's vault name has), SKU Basic, admin user off, role
  assignment mode `LegacyRegistryPermissions` (the mode under which `AcrPull`
  is honoured), and `AcrPull` for the cluster's kubelet identity on the
  registry alone. **Basic has no private endpoint** (Premium only, about ten
  times the Basic price on the retail list read on 2026-10-07) and no
  anonymous pull, so the registry keeps a public endpoint and every request to
  it needs a Microsoft Entra identity: the demo day's choice.
- **Database** (`database.tf`). One PostgreSQL flexible server
  `psql-meridian-<suffix>`, version 17, burstable `B_Standard_B1ms`, 32 GiB,
  reachable by private access only (the delegated subnet and a private DNS zone
  named for the project with the link to the virtual network; no public
  endpoint), backups at 7 days (the least the service allows) with
  geo-redundancy off (fixed at creation; it would copy backups to the paired
  region, Sweden South for Sweden Central and North Europe for West Europe),
  no high availability. Password and Microsoft Entra authentication are both
  on (see "Residuals" for why), with no Entra administrator yet. The
  administrator is `meridianbootstrap`, letters only. Its password is made by
  an **ephemeral** `random_password` (40 characters from letters, digits, the
  hyphen and the underscore, at least one of each of four kinds) and reaches
  the server and a secret in the foundation's vault only through write-only
  arguments (`administrator_password_wo`, `value_wo`), which Terraform neither
  stores in the state nor prints in a plan. The server's `zone` is ignored (the
  service picks one, the provider's argument is not computed). The allow-list
  parameter `azure.extensions` is `vector`, in the case Microsoft's pages write
  it; `CREATE EXTENSION` is the second half's. One database `meridian` is made
  after the parameter. The secret is `platform-database-administrator`, with
  no expiry (see the scan's first finding).
- **Private endpoints** (`endpoints.tf`). Two private endpoints in
  `snet-endpoints`, one to the foundation's Key Vault (sub-resource `vault`)
  and one to the foundation's Azure OpenAI account in the module's region
  (sub-resource `account`), each with the Private Link DNS zone for its public
  name suffix and the zone's link to the virtual network. Both links set the
  resolution policy `NxDomainRedirect`, so a name the zone does not hold (a
  vault or an account with its endpoint somewhere else) goes to public DNS and
  does not vanish; that fails open on a missing record, which is acceptable
  only because the foundation's vault and account are public (below). The zone
  for the third suffix of an account, the AI Foundry one, is not written: the
  gateway calls the OpenAI suffix and accepts no other host (T-43).
- **Identities** (`identity.tf`). The foundation's OpenAI account in the
  module's region, read by data source. Two user-assigned identities, each
  federated to exactly one Kubernetes service account (subject
  `system:serviceaccount:<namespace>:<service account>`, built from variables
  held to DNS labels, fixed audience, no wildcard) with one role on one
  resource: the Model Gateway's identity has `Cognitive Services OpenAI User`
  on the OpenAI account, and the bootstrap's identity has `Key Vault Secrets
  User` on the administrator's secret alone, by the secret's versionless
  scope. No identity of the module has a role on a resource group, on the
  vault as a whole or on the subscription.
- **Logs** (`logs.tf`). One Log Analytics workspace `log-meridian` (per
  gigabyte, 30 days, a daily cap, shared-key sign-in off) and two diagnostic
  settings into it: the cluster's `kube-audit-admin` and `guard` categories
  (the audit events that change something, and the Entra integration's log),
  and the foundation's vault `AuditEvent`, so that a read of the
  administrator's password leaves a record. `kube-audit` (every event, reads
  too) is not sent: Microsoft's own cost note for AKS names it as the way to
  incur substantial cost.
- **Budgets** (`budget.tf`). The foundation's action group is read by data
  source. Two monthly budgets, one on the module's resource group and one on
  the cluster's node resource group, each with three notifications on actual
  spend at 50, 80 and 100 percent. See "Cost" for what the amount means.
- **Outputs** (`outputs.tf`). Ten, none a secret (see "Outputs").
- **Provider and state** (`providers.tf`, `versions.tf`). `azurerm` 5.x
  (locked at 5.8.0) with `resource_provider_registrations = "none"`, `random`
  3.x, Terraform 1.16. The state is remote, in the foundation's storage account
  under its own key `platform.tfstate`, with Microsoft Entra authentication and
  no shared key (see "State"). The provider block sets three behaviours that
  matter at removal: the workspace is purged, a resource group that still
  holds an unmanaged resource is not deleted, and a soft-deleted vault secret
  is recovered and never purged (see "Removal").

## What it relies on in the foundation

The module reads the foundation by data source and by the fixed names the
foundation gave its objects, never through the foundation's remote state: a
reader of a state can read every output and secret in it. It needs:

- the resource group `rg-meridian-foundation`, in one of the two allowed
  regions (the pin checks it);
- the Key Vault `kv-meridian-<suffix>` (RBAC authorisation, purge protection,
  7-day soft delete, public network access on), which the module writes one
  secret into, reaches through a private endpoint and sends audit events from;
- the Azure OpenAI account `oai-meridian-<key>-<suffix>` of the module's
  region (`sdc` for Sweden Central, `weu` for West Europe: the foundation holds
  `sdc` only, so a West Europe run stops at that read, before it proposes
  anything), which the module reaches through a private endpoint and gives one
  role on;
- the action group `ag-meridian-budget`, which emails the subscription's
  Owners, so that no address lives in this repository;
- the state storage account and its container, for the backend only;
- the caller's own rights: the module makes role assignments on three scopes
  (the subnet, the foundation's account and the foundation's vault secret) and
  asks the foundation's resources to approve two private endpoints, so the
  caller is the subscription's Owner (the infrastructure review's reading; the
  apply is by hand). The state account's role, Storage Blob Data Contributor,
  is the foundation's.

The module changes nothing on any of them. The one object it leaves on a
foundation resource is the vault's diagnostic setting, and the removal must
take it away (see "Removal").

### Variables

Thirteen, all in `variables.tf`, each with a type and a description; every one
but the sensitive one has a default and a validation that makes it a closed
list or a bounded number, so that a stray `TF_VAR_` or a variable file cannot
ask for a size that costs a hundred times as much (a budget only alerts).

| Variable | Default | Notes |
|---|---|---|
| `location` | `swedencentral` | `swedencentral` or `westeurope` (hard rule 3), equal to the foundation's list and to the pin's, held by a test |
| `api_server_authorized_ip_ranges` | none | One to four addresses, each one public IPv4 address as a /32 (not the open internet, a wider range, loopback, private, link-local, shared or multicast). Sensitive. The only sources the API server admits, beside AKS's own |
| `kubernetes_version` | `1.36` | `1.35` or `1.36` |
| `node_vm_size` | `Standard_D2s_v5` | Or `Standard_D4s_v5`: a closed list, a cost ceiling |
| `node_count` | `2` | Whole number, 1 to 3 |
| `database_sku_name` | `B_Standard_B1ms` | Or `B_Standard_B2s`: burstable only |
| `database_storage_mb` | `32768` | Or `65536` |
| `workload_namespace` | `meridian` | A DNS label |
| `gateway_service_account` | `model-gateway` | A DNS label: the one service account that may call the OpenAI account |
| `secrets_service_account` | `meridian-secrets` | A DNS label; a placeholder that the second half confirms or changes |
| `budget_amount_eur` | `25` | 1 to 200; **per budget** (see "Cost") |
| `log_daily_quota_gb` | `1` | 0.5 to 5 |
| `expires_on` | none | A real calendar date, `YYYY-MM-DD`: the tag `expires-on` on the resource group, a label only, nothing is removed on that date |

`api_server_authorized_ip_ranges` has no default and is **sensitive**: the
address is the owner's, and Terraform prints a variable that is not sensitive
in every plan. A refused value prints the sentence of the validation and not
the value. There is no variable file and no example value in the repository,
and a test fails on a `.tfvars` file in this directory: an address in a public
repository is a leak. Sensitive hides the value in the display only, not in the
state (see "Residuals").

## Outputs

Ten, none a secret and none derived from the administrator's password:
`resource_group_name`, `cluster_name`, `cluster_oidc_issuer_url`,
`registry_login_server`, `database_server_fqdn` (it resolves only inside the
virtual network), `database_administrator_login`,
`database_administrator_secret_name` (the name, never the value),
`gateway_identity_client_id`, `secrets_identity_client_id` and
`log_workspace_id`. Hosts, an issuer URL and identifiers appear on the screen
after an apply: the wrapper that a later pull request adds redacts them before
anything is shown, and until it exists nothing prints them.

## The subscription pin

The subscription is not in code. It comes from `ARM_SUBSCRIPTION_ID`, as the
foundation's does. The module computes the vault name the foundation would have
in that subscription, reads the vault by it, and refuses to go on in two cases:
the vault belongs to another Entra tenant than the one the provider is signed
in to, or the foundation's resource group is in a region the module does not
allow. Every resource depends on the pin through the module's resource group,
so a plan in a subscription without this project's foundation stops at the
read of the vault, before it proposes anything. **Written, and never seen to
refuse**: `terraform validate` does not evaluate a precondition, and nothing
was planned. A test holds the pin on the text, and holds that every resource
reaches it.

## The doors that exist, and the one that does not

| Command | What it does | Changes Azure | Who runs it |
|---|---|---|---|
| `make azure-platform-validate` | `terraform fmt -check`, `init -backend=false` and `validate`, through `infra/terraform/aws.sh validate azure`, with the committed lock read-only and the caller's `TF_*` variables dropped. Needs no Azure sign-in and no subscription | No | The session or the owner: free |
| `make azure-platform-scan` | Trivy's configuration scan of this directory, from the image pinned by digest in the Makefile, network off, read-only mount. Fails on a HIGH or CRITICAL finding that `.trivyignore` does not list. Needs Docker | No | The session or the owner: free |

**No plan, apply or removal door exists.** `aws.sh plan azure`, `apply azure`
and `destroy azure` print the usage line and run nothing, on purpose, and no
`make` target plans, applies or removes this module. A later pull request adds
the wrapper (a clean environment, the lock read-only, no variable or override
file, the default workspace, a plan record bound to its commit and hash, a
check of the password's version rule, a documented retry for role propagation,
redaction of Azure's host shapes and of an address) **together with** the
command guard's and the settings' rules for the new names, as the AWS module's
were, with a security review of its own. Until then the guard has no rule for
this directory beyond the ones that reach every Terraform call, so a Terraform
command typed by hand here (`plan`, `state`, `output`) gets no ask, and the
only barrier is that the machine where a session runs holds no Azure sign-in.
S071 plans such a sign-in on the development machine for a live model call, so
that barrier ends with it: the wrapper and the guard's rules come first, and
nobody runs Terraform by hand against this directory in the meantime.

`validate` is run from the repository root and leaves nothing in this
directory: `aws.sh` keeps the provider cache under the home directory
(`~/.cache/meridian-terraform/azure`, mode 700) and the cache is permanent
(the providers are about two gigabytes across the repository's modules);
delete that directory to clear it. A first install by two worktrees at the
same moment can fail one `validate` once; run it again. Nothing runs in CI:
Terraform and the scan in the pipeline are S022's.

## What `validate` and the scan cannot tell

`validate` reads the configuration against the provider's schema, offline; the
scan judges it against its checks. Neither knows Azure. Below is everything the
reviews and the two facts sheets (read on 2026-10-07 from Microsoft's pages and
the provider's source) left unread, grouped, each with how the first sign-in or
the first apply would show it. **Nothing in this list is a statement about
Azure, only about what was not read.**

### Before anything is created

1. **Whether any precondition refuses.** The first plan: a subscription with no
   foundation stops at the vault read; a wrong tenant stops at the pin's first
   condition.
2. **Seven resource providers are not registered, and the provider registers
   none.** `Network`, `Compute`, `ContainerService`, `ContainerRegistry`,
   `DBforPostgreSQL`, `ManagedIdentity` and `OperationalInsights`, each under
   `Microsoft.` (see the next section). A sign-in lists the states; without
   them the first apply fails at the first network resource, before anything
   bills.
3. **The vCPU quota and whether the `Dsv5` family is offered** to this
   subscription in the region. Two `Standard_D2s_v5` nodes use exactly 4 vCPU;
   the trial's regional limit is documented on no page (a six-year-old
   community answer says 4; a trial cannot ask for more). This is the review's
   likeliest failure of a first apply: it fails at the node pool with the
   database and the registry already billing. **One sign-in reads it, before
   the paid stop.** An upgrade or an image rotation needs a surge node (6 vCPU),
   so automatic upgrades are off and a node image upgrade needs the
   pay-as-you-go quota.
4. **PostgreSQL's offer in the region**: version 17, `B1ms` or `B2s`, burstable
   with private access (no page excludes or confirms it). A sign-in reads the
   capabilities; the apply fails at the server otherwise.
5. **Kubernetes 1.36 in the region** (the page says a GA version is available
   in all regions and points to a live tracker not read), `max_pods = 50` with
   overlay networking, and `run_command_enabled = false` honoured at creation.
   The cluster refuses, or a sign-in reads the cluster's properties after the
   first apply.
6. **The registry's name** is free. Read at the first apply, or with a
   name-check call after a sign-in.
7. **A West Europe account exists in the foundation** (`weu`). The read fails at
   plan time if not, and fails closed.

### Ordering and timing

8. **Role propagation.** Microsoft documents up to 60 minutes for the cluster
   identity's role on the subnet and up to 5 minutes for the caller's cluster
   role, and no retry. Nothing orders the cluster after the propagation: a
   first apply may fail at the cluster on a permission error and succeed on a
   second run; the wait belongs in the wrapper.
9. **Whether `kubelet_identity[0]` is populated** when the registry's role is
   created. A failure shows at the `AcrPull` assignment.
10. **Security groups and the subnets.** Microsoft's page asks that a group on
    the database's subnet allow port 5432 inside it and outbound to the
    `Storage` and Microsoft Entra service tags, and says no rule is required.
    The module's allow, intra-subnet allow and `VirtualNetwork` deny follow it,
    and there is no outbound rule. **Not read**: whether the service takes the
    group at all; whether the deny shuts a path the service itself uses
    (health, management); whether a group attached while the server is created
    races it (the module orders the association first, and a removal runs it
    back); and whether overlay pods really leave through their node's address
    when they reach another subnet (a reviewer's recall, no page read, which is
    why the pod range is a second source). The first apply fails or hangs at
    the server, or at an association with a conflict on the subnet; a second
    run is the first thing to try. That `NetworkSecurityGroupEnabled` is the
    value that makes the endpoints' group apply to endpoint traffic is read from
    Microsoft's page on the setting; a sign-in reads the subnet, and a
    connection from a pod that the group should refuse is the test.
11. **What the API answers on the delegated subnet** to the explicit
    `default_outbound_access_enabled = true` the provider sends (Microsoft says
    the private-subnet setting does not apply to delegated subnets, and not
    whether a value is refused). The first apply shows an error at
    `snet-postgres` if it is.
12. **The node resource group's budget.** The provider takes any resource group
    ID, and no page says Azure accepts a budget on the managed group or that a
    lockdown would stop it (none is set). The first apply: success, or the
    Budgets API refuses `budget-meridian-nodes` after the cluster exists; then
    the nodes' cost is watched by the foundation's subscription budget alone and
    the resource is deleted from the module.
13. **A parameter change and a database creation colliding** when something
    outside Terraform's lock makes one of them (the second half's Job). The
    provider serialises the two inside one apply; whether the service answers
    "busy" to two writes from outside is on no page.

### The plan after the first apply

14. **Whether a second plan is empty.** Three suspects were named and handled:
    the storage service endpoint on `snet-postgres` (declared), the server's
    `zone` (ignored) and the case of `vector` (written in lower case). A diff
    left after the first apply shows a field the code did not account for: the
    endpoint block carries something the API returns, or the service rewrites
    the case of the value.
15. **The administrator's name and password.** The provider refuses seven names
    and the `pg_` prefix; Microsoft's pages disagree on whether three of four
    character classes or all four are required (the 40-character password has
    all four); no page shows whether an underscore in the name is refused, so
    the name is letters alone.

### Identity, the vault and the endpoints

16. **A role on one secret.** Microsoft documents it as supported and not
    recommended, with an exception that is this module's case. **Not
    established**: that a secret made through the data plane (which is how the
    provider makes one) works as a scope. The first apply reads it at the end
    of the graph, with the cluster already billing. How long after the
    assignment the bootstrap identity can read the secret is read by the second
    half's Job, and the second demo day's recovery of the soft-deleted secret
    is read on the second day.
17. **Private endpoint auto-approval** to a vault and an account in another
    resource group: an endpoint stays in `Pending` if the caller lacks the
    approval right on the target. **Which resolution policy** the service gives
    a link that names none is not read (both Private Link links name one; the
    database's zone is not a Private Link zone and has none).
18. **The diagnostic settings' category names** `kube-audit-admin` and `guard`
    for the cluster (Microsoft's reference lists them) and `AuditEvent` for the
    vault (the contract's name, **not read from a page**): the apply refuses a
    setting with a wrong name. Whether a setting on a vault in another group
    works from this module is also not read.
19. **Whether the workspace's two shared keys still reach the state** with
    local authentication off: the schema keeps them as computed, sensitive
    attributes and no page says whether the API returns them. The first sign-in
    looks in the state or in the workspace's properties, and "Residuals" is
    corrected either way. That the diagnostic settings keep working with
    shared-key sign-in off (they go by resource ID) is the security review's
    expectation; the apply shows it by the settings' creation.

### Cost and removal

20. **The `FreeTierInfrastructureCost` meter** (a line on the retail list that
    no page explains). Read in cost analysis after the first demo day.
21. **Whether the authorised ranges lock the operator out**, because the
    address changed since the variable was set. The first cluster command shows
    it, and so does every one after an address change; the fix is a re-apply.
22. **The workspace on a second day.** With the purge flag on, a removal deletes
    it for good. A removal that stopped before the workspace leaves it, and the
    next create then recovers it or conflicts (the page names the error and the
    cause, not the deciding condition). The flag is read from the provider block
    at destroy time: a removal run with another provider block would not purge.
23. **Whether the delegated subnet deletes cleanly** straight after the server
    (see "Removal").
24. **What `enhanced_validation` would cover.** Everything in it is off by
    default in 5.x, and `preflight_enabled` needs credentials at plan time;
    which resources preflight covers is not read. Not turned on.

## The resource providers to register

The provider registers none (`resource_provider_registrations = "none"`, also
the 5.x default), and the foundation's script `state.sh` registers five:
`Storage`, `KeyVault`, `CognitiveServices`, `Insights` and `Consumption`. The
module needs these besides, all under `Microsoft.`: `Network` (the virtual
network, Private Link and private DNS are all in it), `Compute`,
`ContainerService`, `ContainerRegistry`, `DBforPostgreSQL`, `ManagedIdentity`
and `OperationalInsights`. (`Compute`, `ManagedIdentity`, `Network` and
`Storage` are in the provider's own `core` set, which this module does not
use.) `OperationsManagement`, which one
sheet lists, is not needed: there is no Container Insights. They are registered
by the owner, with a command, before the apply (the wrapper's contract), and
never by the provider, so that a plan changes nothing in Azure on its own
account. Microsoft's page lists these as not registered by default and gives
no per-service statement for most of them: the list is built from that general
rule, and a sign-in shows each state.

## Deliberately not built for the first apply

Each of these was written and then taken out, or never written, because it
needs a value no page read settles, and a billed data service at create must
not hang on one:

- **The server's logging.** No diagnostic setting for the server's own log, and
  none of the parameters `log_connections`, `log_checkpoints` and
  `connection_throttle.enable`. The category Azure takes for a flexible server
  is on no page read and the schema lists none; a setting that took every
  category would send the connection line of every pod's failed login into the
  workspace's daily cap, which also holds the cluster's audit log, so a flood of
  logins from any pod could fill the cap and hide what follows. The first
  sign-in must list the server's log categories; the setting then names the one
  that holds the connection log, in a workspace of its own or under a cap of its
  own. The two logging parameters are PostgreSQL 17's defaults already (`on`)
  and each would be one more unretried write to the server at create and at
  removal. The throttle is off by default; turned on, it would throttle one
  address after repeated failed logins, the pods reach the server from their
  nodes' addresses (probably), and the bootstrap Job could be slowed behind its
  neighbours' failures: turn it on after the bootstrap, from the second half.
- **An alert on the workspace's daily cap.** The provider has the alert
  resources and the module reads the foundation's action group, but the signal
  of "the cap was reached" (a table and an operation name) is on no page read.
  Microsoft's page on the daily cap says a banner and an event in the
  `Operation` table appear; the table and the event's wording were not read.
  The audit log is lossy by construction until this exists: when the cap is
  reached, ingestion stops silently until the reset hour, which varies per
  workspace and cannot be set.
- **The TLS floor as parameters.** `require_secure_transport` and
  `ssl_min_protocol_version` are not set. They are recalled to default to secure
  values, **not read**, and each parameter written is another write to the
  server. The chart's services verify the certificate (T-42), and the Azure root
  certificate for that is the second half's.
- **`anonymous_pull_enabled = false`.** Basic has no anonymous pull; whether the
  service takes an explicit false on Basic is not read. A comment keeps the line
  for the day of a move to Standard.
- **A customer-managed key, a disk encryption set, a private cluster, Defender,
  Azure Policy and Container Insights.** See the scan's findings and "What a
  production environment sets differently".

## The scan

`make azure-platform-scan` runs `trivy config` from the image pinned by tag and
digest in the Makefile (`TRIVY_IMAGE`, which Renovate reads), with no network, a
read-only root and no capabilities, so the checks are those of that image and a
newer check arrives with a new pin. It gates HIGH and CRITICAL only: on the
pinned image the module has none, and `.trivyignore` holds no entry (a test
holds the file's format for the day it does, one check ID a line with its
reason above it, and that an entry applies to every resource of the directory,
so the number of cluster, registry, server and subnet blocks is held).

At all severities the image raises **eight findings** (MEDIUM and LOW, run
against this module on 2026-10-07 and again for this README). None is hidden
by the ignore file. Each is a decision for the owner to see before any apply,
with the reviewers' compensation:

| Check | Severity | What it says | Why it holds here | Compensation (the reviews') | Production |
|---|---|---|---|---|---|
| `AZU-0017` | LOW | The vault secret has no expiry date | A one-day environment, and a date taken from the clock would make every plan propose a change. The security review calls that reason weaker than written: a plan-time timestamp with `ignore_changes` avoids the churn | The secret goes with the environment (soft-deleted, then recovered or gone); the password is dead once the server is; rotation is by version | A rotation policy and an expiry |
| `AZU-0019` | MEDIUM | `log_connections` is not on | Not built (above). It is `on` by default on PostgreSQL 17 (Microsoft's page); the check would be decoration without a log destination | None until the server's log has a destination | The parameter plus a diagnostic setting |
| `AZU-0021` | MEDIUM | Connection throttling is not on | Not built (above); it could slow the bootstrap Job | Turn on after the bootstrap | On, once the roles exist |
| `AZU-0024` | MEDIUM | `log_checkpoints` is not on | As `AZU-0019` | As `AZU-0019` | As `AZU-0019` |
| `AZU-0040` | MEDIUM | AKS logging to Azure Monitor (the `oms_agent` block) is off | Container Insights bills ingestion, and pod logs may hold personal data | The control-plane audit diagnostic setting; Loki in the cluster (S064) | Container Insights or Managed Prometheus, decided with the cost |
| `AZU-0065` | MEDIUM | The API server is not private | Design choice D6: a private cluster needs a jump host or `command invoke`, which a demo day does not earn | One to four `/32` ranges, Entra with Azure RBAC, local accounts off, run command off | A private cluster behind a jump host or a VPN |
| `AZU-0066` | LOW | The Azure Policy add-on is off | It adds an admission webhook and its own pods to a two-node cluster for a policy set a one-subscription environment does not have | Pod Security labels on kind's namespaces. **For Azure none exist**, so the second half must set them | Azure Policy with a baseline initiative |
| `AZU-0067` | LOW | No disk encryption set | Disks use platform-managed keys | None needed for this data class (synthetic only) | A customer-managed key |

The count is "eight" after the second review's fixes took out the server's
logging again; it was five in between (the three PostgreSQL checks were
satisfied by resources that the review then found to add failure surface).

## Cost

An estimate for this module's shape, from Azure's public retail price list and
Microsoft's pages **read on 2026-10-07** (Sweden Central, EUR, Linux,
pay-as-you-go list prices, no credits, no VAT; month lines divided by 730 hours;
a 12-hour day). **It is not a cost statement.** Prices went through a
summarising reader and not a raw read, six of the twelve lines are estimates
(marked), and the figures are read again before any cost is stated to the owner,
at the paid stop. The sum was made for the design, before the module's last
reviews.

| Line | One hour (EUR) | Status |
|---|---|---|
| Two `Standard_D2s_v5` nodes (2 x 0.0898) | 0.17960 | Price read |
| Two managed OS disks, P10 128 GiB (2 x 19.0778 a month) | 0.05227 | **Estimate**: assumes Premium SSD; whether the default is Premium or Standard SSD is not read; the disk-mount meter is not added |
| AKS Free control plane | 0.00000 | The pages say free; the retail list holds an unexplained meter (below) |
| PostgreSQL `B1MS` | 0.01750 | Price read |
| Database storage 32 GiB (0.1205 a GB-month) | 0.00528 | **Estimate**: 32 GiB taken as 32 GB; backup storage inside the free allowance assumed |
| Container Registry Basic (0.1466 a day) | 0.00611 | **Estimate**: billed per day, so a day with a registry may bill all of it |
| Two private endpoints (2 x 0.0088) | 0.01760 | **Estimate**: priced only under region `Global`; data processed not included |
| One Standard public IPv4 address | 0.00440 | Price read |
| Standard load balancer with a rule | 0.02200 | **Estimate**: priced only under `Global`; "first five rules" is an inference |
| **Sum without logs** | **0.30476** | **3.6571 for 12 hours** |
| One gigabyte of logs ingested | n/a | 0 while the 5 GB a month free allowance per billing account is unspent, 2.6311 if it is spent: **3.66 to 6.29 for 12 hours** |

About 0.31 an hour, 3.7 for twelve hours, up to 6.3 with a gigabyte of logs
past the free allowance. The target of QA-07 is EUR 10 a demo day.

**What the sum leaves out** (the infrastructure review's list, and what the
module added after the sheet):

- the second Standard public address that AKS may create for managed outbound
  traffic (+0.0044 an hour if so), when the Envoy Service takes its own address;
- the three private DNS zones (a few thousandths an hour: not priced) and the
  data processed through the load balancer (0.0044 a GB) and the two private
  endpoints (0.0088 a GB each way);
- the Premium OS disks, if the default is Standard SSD the line is lower;
- log ingestion past the free allowance, and the retention beyond what the
  ingestion price includes (31 days; the module keeps 30); the vault's
  `AuditEvent` setting sends into the same daily cap;
- **the `FreeTierInfrastructureCost` meter**, 0.0440 an hour on the retail
  list, which no page explains: with it, twelve hours rise by 0.528 to about
  4.19. Whether it is charged is read in cost analysis after the first demo day;
- persistent volumes the workloads claim, egress traffic, Key Vault and Azure
  OpenAI usage (the module holds no model deployment; the foundation does);
- anything the two budgets, the security groups and the identities cost: **not
  read**, no price for them was looked up.

The closed lists bound the day: at their ceilings (the larger node size, three
nodes, the larger database) the review put the day at roughly EUR 10 to 13
without logs, and a 5 GB daily cap alone adds about EUR 13 a day of ingestion at
the list price. These are the review's figures and were not recomputed.

**The budget amount is PER budget, and the mail arrives twice.** There is one
variable, `budget_amount_eur`, and the same amount applies to the budget on the
module's group and to the one on the node group: the default is EUR 25 on each
of two scopes. Each has three notifications, and all go to the foundation's one
action group, so a threshold passed on both reaches the Owners twice. Halve the
amount if one total is meant. A budget alerts and does not stop spend (T-15),
and an alert comes after the spend it reports. The group's budget alone would
miss most of the cost (the nodes, their disks, the load balancer and the
outbound address are billed in the node resource group, about 83 percent of the
sheet's sum), which is why the second budget exists; that Azure accepts it is
item 12 of the list above. A forgotten cluster at about 0.31 an hour reaches one
budget of 25 after roughly eighty hours; the `expires-on` tag is how a person
looking at the subscription tells a forgotten environment from a live one.

**The demo day's price depends on the account.** A free trial has a spending
limit and a vCPU limit that a pay-as-you-go subscription lacks: the plan's
section for S020 has the owner's checklist for the upgrade.

## Removal

Removal is **Terraform-driven only** and its command does not exist yet (see
"The doors that exist, and the one that does not"). What a removal leaves, and
what to do:

- **The vault's secret is soft-deleted, not gone.** The vault has purge
  protection and a 7-day soft-delete window; the provider is set to never purge
  a secret on removal (a purge would fail and the removal with it) and to
  recover a soft-deleted secret at the next create, writing the new password
  as a new version. The old versions, with the previous days' passwords, stay
  readable in the vault to anyone with a role on it; they are dead passwords
  once the server is gone. That a recovered secret then works as a role scope
  is item 16 of the list above.
- **The workspace is purged, and with it the audit log, with no export.** The
  provider block sets `permanently_delete_on_destroy = true`, so the removal
  destroys `log-meridian` for good: the cluster's control-plane audit log
  (`kube-audit-admin`, `guard`) and the vault's `AuditEvent` records, which
  include **every read of the administrator's password**. No copy is made, and
  no alert on the daily cap exists to say the capture stopped. Said plainly
  against hard rule 8, whose reason is that deleting an environment to clear a
  fault nobody has looked at destroys evidence: this module's design destroys
  the evidence of its own demo day at its removal, by default. The choice was
  made so that every demo day starts empty and the name is free at once (the
  default is a soft delete of 14 days, and a create under the same name, group
  and region inside it gets the OLD workspace back with the previous day's
  audit data); the owner may overturn it. **Look at the log before a removal if there
  was an incident, and never remove an environment to clear a fault nobody has
  looked at.** On a demo day with nothing to find, the loss is the design.
- **The database's data goes with the server**, and with it the platform's own
  audit table: nothing in the module keeps a backup past the server (7 days,
  with the server).
- **The node resource group goes with the cluster**, and the second budget goes
  first (it names the cluster's attribute). It is left behind only by an
  out-of-band deletion of the cluster or the group; then delete it by hand.
- **If the removal stops at `snet-postgres`** (the delegation or a service link
  still attached right after the server), run the same removal again after 10 to
  15 minutes: Microsoft documents that wait for service association links in
  general, and no page says the PostgreSQL service leaves one. The resource
  group stays until then because `prevent_deletion_if_contains_resources` is
  on, which is the designed outcome. **Do not delete the subnet or the group by
  hand.** The security-group association on that subnet is removed by
  Terraform first, and no page read says a group changes the delete.
- **A removal must be driven by Terraform.** The vault's diagnostic setting is
  an object on a foundation resource, and the role assignments on the
  foundation's account and secret are objects on foundation resources: an
  out-of-Terraform deletion of the group leaves them behind, and the next apply
  collides on the setting's name.

After a removal, look in the portal for what is left: the resource group and the
node resource group gone; no budget of this module; the vault's diagnostic
setting gone; the secret soft-deleted and nothing else in the vault changed; the
deleted-workspaces list empty (item 22). Keep the state until it is clean.
The state is the only list of what the module made, and it lives in the
foundation's storage account, which a lost laptop does not take.

## Residuals

What is true after everything above, and is not fixed here.

- **The foundation's vault and Azure OpenAI account are open to every address,
  and the vault receives the administrator's password.** Both have public
  network access on and a default action of Allow (the laptop's live mode and
  `make azure-smoke` use the public path, which is why D11 of the design left
  them). The module adds private endpoints, which give the cluster a private
  path and close nothing, and then writes the database administrator's password
  into that vault. A role is still needed to read it, but a valid token from any
  address reads it. The infrastructure review's default was to BLOCK a public
  data service. **The owner decided on a firewall on 2026-10-08 (it was undecided
  until then): default deny, the operator's address allowed, private endpoints
  for the cluster; designed, not written.** The two sides are in the plan's
  section for S020. A firewall is a change to the applied foundation (default
  Deny plus the operator's address, a sensitive variable with no default and no
  example), written free and applied by the owner at the next apply; it is not
  in this module.
- **The state is not secret-free.** The password is not in it (it is ephemeral,
  and the schema marks both arguments write-only). But `authorized_ip_ranges` is
  a plain attribute and the state holds the **operator's address**; a
  sensitive variable hides it from the display only. The state may hold the
  **workspace's two shared keys** (item 19). It holds names, identifiers, the
  tenant's and the subscription's, and addresses. The state is remote, with
  Entra authentication only and a lock, but the container holds both the
  foundation's state and this one, so one role reads both. The design's line
  "the state holds no secret" was true for the password only.
- **No firewall at the edge.** Envoy Gateway behind the cluster's load balancer
  is the decided edge (ADR 11); a web application firewall (T-02) is designed
  and not built.
- **The registry's endpoint is public** (Basic has no private endpoint), with
  Entra authentication only and the admin user off.
- **Egress is unrestricted at the Azure layer.** The cluster uses the load
  balancer for outbound traffic with no group, firewall or NAT gateway on the
  nodes' subnet. The only exfiltration control is the chart's default-deny
  policy under Cilium, which has not run on this engine (what kind's network
  plugin does is not what Cilium does: the second half sees it).
- **Azure RBAC for Kubernetes is not a boundary against an Azure Contributor.**
  A holder of Contributor or Owner on the cluster or its group can re-enable
  local accounts or run command through the management API and become cluster
  administrator. AKS itself gives the cluster identity Contributor on the node
  resource group. A pipeline identity that ran this module (S022) would need
  role-write on three scopes plus approval rights on both private-endpoint
  targets: Owner-level, which conflicts with hard rule 3's spirit; the owner
  applies by hand.
- **A pod that can create pods in the namespace may take an identity.** A
  federated credential is bound to a service account, and whoever may create a
  pod in `meridian` with that service account gets the token. The gateway's
  identity carries the OpenAI User role, so its holder bypasses the Model
  Gateway's guardrails.
- **`NxDomainRedirect` fails open on a missing record**, and the vault and the
  account are public by D11.
- **Account and tenant.** The environment is built in the trial's Entra tenant
  (the owner's decision of 2026-10-07; a trial cannot create another, and
  moving later means recreating the foundation). The module reads the tenant
  from the caller.

### Nine things the second half of S020 must do

The second half deploys into the cluster and is a separate change. These are
named by the reviews and are not done; each is a row in the threat model.

1. **The secrets identity's standing read.** Its role on the administrator's
   secret is needed for one bootstrap Job and stays for the environment's life;
   any pod that can run as the `meridian-secrets` service account gets the
   token. Make the role assignment a variable that is off after the bootstrap
   and create the service account only for the Job, and remove it after.
2. **Egress.** The token exchange of workload identity goes to the Microsoft
   Entra host on 443 from inside the pod, over the internet: no private endpoint
   exists for it on any AKS page, so the gateway's egress rule cannot be only
   "to the endpoints' subnet" (by name it needs the paid Advanced Container
   Networking Services, whose price was not read; by address it is the
   `AzureActiveDirectory` tag at a firewall). The metadata address (link-local)
   must be refused by the default-deny too: a pod that reaches it gets the
   kubelet identity's token, which carries `AcrPull` only. Measure the chart's
   policies on this engine.
3. **A second administrator and a break-glass path.** The applier is the
   cluster's only administrator, and a later apply by another principal
   replaces the assignment and removes the first person's access. The
   database's Entra authentication is on with no administrator.
4. **Pod Security labels on Azure's namespaces.** Kind's namespaces have them
   (`infra/kind/manifests/namespaces.yaml`); nothing makes them on Azure, and
   they are the compensation for the Azure Policy add-on being off.
5. **The password's version rule.** `administrator_password_wo_version` is the
   only thing that moves the server's password and the secret together, and a
   write-only value is not read back, so Terraform cannot see a mismatch. Raise
   it in the same change as any replacement of the server (a changed
   `version`, `delegated_subnet_id`, `private_dns_zone_id`,
   `geo_redundant_backup_enabled` or `administrator_login` replaces it), and
   before retrying a first apply that stopped after the server was made.
   Otherwise the server keeps the first password while the vault holds another,
   with no error, and the bootstrap Job fails at first use (the eleven service
   roles use their own passwords and keep working). Recovery is to raise the
   version, or an Owner resets the administrator's password. The wrapper's plan
   check refuses a plan that creates or replaces the server without rewriting
   the secret.
6. **The database's roles.** Entra authentication on the server beside
   passwords is the module's; the Entra administrator, the switch of the roles
   (T-42 expects workload identity to replace the passwords; D9 keeps password
   authentication because the chart's eleven roles log in with passwords), the
   eleven roles, `CREATE EXTENSION vector`, the revoke of `CONNECT` and `TEMP`
   from `PUBLIC`, by a Job in the cluster because the server has no public
   endpoint. What the administrator may do to the audit log's trigger (it is not
   a superuser; Microsoft's pages do not say whether it may disable a trigger
   it does not own) is **read on the server there**, not assumed.
7. **The server's logging**: list the log categories, build the setting, then
   the parameters; the throttle after the bootstrap; the TLS floor stated.
8. **The audit log's afterlife.** The daily-cap alert (read Microsoft's page on
   the cap and the operation it logs first), and an export before a removal, or
   a knowing acceptance of the loss.
9. **The edge.** The Envoy Gateway Service's own address with a DNS label by
   annotation (Terraform's part of the edge is nothing: a static address outside
   the node resource group would need `Network Contributor` on a resource group
   for the cluster's identity, which D10 refuses), and
   `loadBalancerSourceRanges` on it if the demo is not meant to be public; and
   the chart's other kind-only values.

## What a production environment sets differently

- A private cluster with a jump host or a VPN, no public endpoint of the API
  server.
- A firewall on the foundation's vault and account (no public path at all, with
  private endpoints and no `NxDomainRedirect` fail-open), and the state
  storage's own hardening.
- A managed web application firewall at the edge: ADR 11 compares Application
  Gateway WAF v2 and Application Gateway for Containers with the Envoy
  Gateway that is built, with their prices and a written design.
- Availability zones for the nodes and the database, high availability, a
  Standard-tier control plane with a service level, and a registry that can have
  a private endpoint (Premium).
- Controlled egress: a NAT gateway or a firewall, and the Entra host reached by
  address rules. Defender for Containers, Azure Policy and image signing with
  admission verification.
- Customer-managed keys and a disk encryption set; a rotation policy and an
  expiry on secrets; immutable storage for the audit log, an export, and an
  alert on the cap.
- A custom role that holds the single join action in place of `Network
  Contributor` on the subnet; resource locks on the foundation; a pipeline
  identity with narrow federation.
- Entra groups for cluster access, a named second administrator and a
  break-glass account; privileged-access elevation for the cluster admin role.

## The Region list

`location` accepts `swedencentral` and `westeurope`: the foundation's list, word
for word, which the pin's `allowed_locations` holds a second time, and a test
holds all three equal (hard rule 3: EU member states only). The default is
Sweden Central, which is cheaper than West Europe for every size the sheet
listed (the default node size is about 11 percent less), and the foundation's
OpenAI account is there. Everything regional takes the variable or the group's
location. Geo-redundant backup is off and there is no registry
geo-replication, so nothing copies data to a paired region. The private DNS
zones, the budgets and the role assignments are global control-plane objects
holding names, private addresses and identifiers, no data; the foundation's
action group is global. No claimant personal data moves in this half of S020:
nothing runs. What lands in the workspace is the operator's identity and
addresses from the Entra and audit logs, in the EU.
