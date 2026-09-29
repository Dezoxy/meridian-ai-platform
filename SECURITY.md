# Security policy

Meridian AI Platform is a portfolio project. It runs on synthetic data only
and has no production deployment, customers or real personal data.

## Reporting a vulnerability

Report it privately through GitHub: **Security** tab, then **Report a
vulnerability**. Please do not open a public issue. Expect an answer within a
week.

In scope: code, infrastructure definitions, CI workflows and anything that
could leak a secret or let the platform's guardrails be bypassed, such as the
model gateway's residency and budget checks or the tool allowlists.

## Secrets

The repository holds no secrets. Secret scanning with push protection is
enabled, and CI runs gitleaks on every pull request. Runtime secrets live in
Azure Key Vault or in Kubernetes Secrets created out of band.
