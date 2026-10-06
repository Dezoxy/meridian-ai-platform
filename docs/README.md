# Documentation

- [Plan](meridian-plan.md): the living step list, the session protocol and
  the open questions. Start every working session there.
- [Architecture](architecture/README.md): the model, its views, the decisions
  and the requirements. Start there to understand the system.
- [The fifteen-minute demo](demo.md): one claim through the platform on a
  laptop, and what to show around it. Start there to see it run.
- [Operations](operations/README.md): how the platform is watched and what
  to do when it breaks. Start there when an alert fires. A baseline (S024):
  the [service level objectives](operations/slo.md) are proposals nobody has
  measured, and the runbooks were written from the code and not exercised:
  [provider outage](operations/runbooks/provider-outage.md),
  [budget exhaustion](operations/runbooks/budget-exhaustion.md),
  [database failure](operations/runbooks/database-failure.md),
  [rollback](operations/runbooks/rollback.md),
  [secret rotation](operations/runbooks/secret-rotation.md),
  [certificate expiry](operations/runbooks/certificate-expiry.md) and
  [rate store](operations/runbooks/rate-store.md).

- [Governance](governance/README.md): how a model provider is onboarded and
  when a workload counts as accepted. Start there before adding a provider or
  a workload. Designed (S034): written and applied once on paper, enforced by
  no gate beyond the checks the documents name:
  [provider onboarding](governance/provider-onboarding.md) and
  [its application to Azure OpenAI](governance/provider-onboarding-azure-openai.md),
  [service acceptance](governance/service-acceptance.md) and
  [its application to claims triage](governance/service-acceptance-claims-triage.md).

- [Development environment](development-environment.md): what the machine
  the work runs on needs, what git does not carry to a new one, and the
  first run there. Start there on a new machine.

Developer guide and evaluation documents are added when their subject
exists, milestone by milestone.
